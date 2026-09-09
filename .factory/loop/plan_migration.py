#!/usr/bin/env python3
"""Deterministic v1 -> v2 plan migration and task archival (Phase 2D1).

The concise active plan (``factory-plan/v2``) carries only active/pending/
in_progress/blocked tasks; completed tasks and the plan acceptance/evidence
history live in the strict committed sidecars (``plan_sidecars.py``).  This
module is the trusted migration/compaction authority:

* :func:`migrate_plan` translates a committed ``factory-plan/v1`` plan into
  the v2 plan plus the archive/history sidecars.  Every task/status/
  acceptance/evidence datum of the completed tasks is preserved losslessly in
  the archive sidecar (exact commit/evidence references are data, never
  authority); the active plan keeps only genuinely unfinished tasks with
  concise Scope/Acceptance, the current blocker, and the latest actionable
  failure.  The migration is idempotent (a v2 plan is a no-op), atomic
  (sidecars are written before the plan, and the plan's ``sidecars:`` binding
  makes any partial pair fail closed), and crash-safe (temp + fsync + rename
  for every file).  Legacy v1 plans remain readable until migrated; the
  migration never runs silently during a campaign.
* :func:`archive_task` moves one independently verified completed task from
  the active v2 plan into the archive sidecar (append-only, provenance
  ``campaign``) and rewrites the plan without it.  Reopening an archived task
  is an explicit semantic migration back into the active plan with
  provenance, never model prose.
* :func:`rehash_plan` recomputes the sidecar digests and rewrites the plan's
  ``sidecars:`` front-matter binding so a planner edit that adds/removes
  active tasks keeps the plan-sidecar pair consistent.

The module is part of the hidden control-plane package and is imported
exactly like its siblings (package mode from the hidden loop, flat mode from
the hidden ``.factory/tests/`` suite).  It uses only the Python standard
library plus the committed hidden-loop modules (``plan_parser``,
``plan_sidecars``).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

try:  # package import (the hidden `.factory/loop/` package)
    from . import plan_parser
    from . import plan_sidecars
except ImportError:  # flat import used by the hidden `.factory/tests/` suite
    import plan_parser  # type: ignore[no-redef]
    import plan_sidecars  # type: ignore[no-redef]

DEFAULT_PLAN_PATH = ".factory/artifacts/implementation-plan.md"
DEFAULT_ARCHIVE_PATH = ".factory/artifacts/plan-archive.jsonl"
DEFAULT_HISTORY_PATH = ".factory/artifacts/plan-history.jsonl"

# The active plan is bounded: a concise plan that grows past this ceiling is
# a defect (the whole point of the v2 format is a small injected prompt).
PLAN_SIZE_CEILING = 64 * 1024
# A concise Scope/Acceptance paragraph is bounded so one task cannot bloat the
# active prompt; the full text stays recoverable from Git history and the
# archive sidecar.
CONCISE_FIELD_MAX = 1200


class PlanMigrationError(Exception):
    """Base class for every fail-closed migration failure."""


class PlanMigrationStateError(PlanMigrationError):
    """The plan-sidecar pair is inconsistent or already migrated."""


def _as_root(root) -> Path:
    root = Path(root).absolute()
    try:
        info = os.stat(root, follow_symlinks=False)
    except OSError as exc:
        raise PlanMigrationError(f"cannot stat repository root {root}: {exc}") from exc
    if not stat.S_ISDIR(info.st_mode):
        raise PlanMigrationError(f"repository root is not a directory: {root}")
    return root


def _read_bounded(path: Path, maximum: int, what: str) -> bytes:
    try:
        info = os.stat(path, follow_symlinks=False)
    except OSError as exc:
        raise PlanMigrationError(f"cannot inspect {what} {path}: {exc}") from exc
    if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode):
        raise PlanMigrationError(f"{what} {path} is not a regular non-symlink file")
    if info.st_size > maximum:
        raise PlanMigrationError(
            f"{what} {path} is {info.st_size} bytes, exceeding the {maximum}-byte cap"
        )
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise PlanMigrationError(f"cannot read {what} {path}: {exc}") from exc
    if len(data) > maximum:
        raise PlanMigrationError(f"{what} {path} grew past its size cap")
    return data


def _condense(text: str, *, maximum: int = CONCISE_FIELD_MAX) -> str:
    """Concise first-paragraph condensation of a task field.

    The full text is preserved losslessly for audit (the v1 plan stays in Git
    history and completed tasks are archived verbatim); the active prompt gets
    the first paragraph, bounded, with no fabricated content.
    """
    text = text.strip()
    if not text:
        return text
    paragraphs = [part.strip() for part in re.split(r"\n\s*\n", text) if part.strip()]
    first = paragraphs[0] if paragraphs else text
    # Collapse internal single newlines so the rendered field is one logical
    # line (the parser rejects continuation lines on structured fields).
    first = re.sub(r"\s*\n\s*", " ", first)
    if len(first) <= maximum:
        return first
    # Bound at a sentence boundary inside the cap when possible.
    cut = first[:maximum]
    boundary = max(cut.rfind(". "), cut.rfind(": "), cut.rfind("; "))
    if boundary > maximum // 2:
        return cut[: boundary + 1].rstrip()
    return cut.rstrip() + " …"


def _task_to_archive_record(
    task: plan_parser.Task, *, archived_commit: str, provenance: str
) -> plan_sidecars.ArchiveRecord:
    """Lossless archive record for one completed v1 task.

    Every field of the v1 task — including the full Scope/Acceptance/
    Verification/Documentation impact and the Evidence narrative — is
    preserved verbatim; exact commit/evidence references are data.
    """
    fields = task.fields
    evidence = fields.get("Evidence", "")
    evidence_refs: List[str] = []
    for line in evidence.splitlines():
        token = line.strip().strip("`")
        if token and not any(ch in token for ch in " \t"):
            evidence_refs.append(token)
    return plan_sidecars.ArchiveRecord(
        schema=plan_sidecars.ARCHIVE_SCHEMA,
        task_id=task.number,
        title=task.title,
        priority=task.priority,
        dependencies=tuple(task.dependencies),
        status="complete",
        scope=fields.get("Scope", ""),
        acceptance=fields.get("Acceptance criteria", ""),
        verification=fields.get("Verification", ""),
        documentation_impact=fields.get("Documentation impact", ""),
        evidence_refs=tuple(evidence_refs),
        archived_commit=archived_commit,
        provenance=provenance,
    )


def _render_v2_plan(
    plan: plan_parser.Plan,
    active_tasks: Sequence[plan_parser.Task],
    *,
    archive_digest: str,
    history_digest: str,
    goal: str,
    architecture: str,
) -> str:
    """Render the concise v2 plan document (active tasks only)."""
    lines: List[str] = [
        "---",
        "schema: factory-plan/v2",
        f"spec_path: {plan.spec_path}",
        f"spec_commit: {plan.spec_commit}",
        f"spec_blob: {plan.spec_blob}",
        f"base_commit: {plan.base_commit}",
        f"status: {plan.status}",
        f"sidecars: {json.dumps({'archive': archive_digest, 'history': history_digest}, sort_keys=True, separators=(',', ':'))}",
        "---",
        "",
        "# Implementation Plan",
        "",
        "## Goal and non-goals",
        "",
        goal.strip(),
        "",
        "## Architecture and constraints",
        "",
        architecture.strip(),
        "",
    ]
    for task in active_tasks:
        lines.append(f"## Task {task.number}: {task.title}")
        lines.append("")
        lines.append(f"- Status: {task.status}")
        deps = ", ".join(
            f"Task {dep}" for dep in task.dependencies
        ) if task.dependencies else "None"
        lines.append(f"- Dependencies: {deps}")
        lines.append(f"- Priority: {task.priority}")
        lines.append(f"- Scope: {_condense(task.fields.get('Scope', ''))}")
        lines.append(
            f"- Acceptance criteria: {_condense(task.fields.get('Acceptance criteria', ''))}"
        )
        lines.append(f"- Verification: {_condense(task.fields.get('Verification', ''))}")
        lines.append(
            f"- Documentation impact: {_condense(task.fields.get('Documentation impact', ''))}"
        )
        if task.blocked_on:
            lines.append(f"- Blocked on: {re.sub(r'\s*\n\s*', ' ', task.blocked_on)}")
        if task.latest_failure:
            lines.append(
                f"- Latest failure: {re.sub(r'\s*\n\s*', ' ', task.latest_failure)}"
            )
        if task.write_scopes:
            lines.append(f"- Write scopes: {', '.join(task.write_scopes)}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _canonical_section_text(plan: plan_parser.Plan, title: str) -> str:
    """The raw text of one canonical section of a parsed plan."""
    for block in plan._blocks:
        if block.heading == f"## {title}":
            return "\n".join(block.lines[1:]).strip()
    return ""


def migrate_plan(
    root,
    *,
    plan_path: str = DEFAULT_PLAN_PATH,
    archive_path: str = DEFAULT_ARCHIVE_PATH,
    history_path: str = DEFAULT_HISTORY_PATH,
    archived_commit: Optional[str] = None,
) -> Dict[str, object]:
    """Migrate a committed v1 plan to v2 + sidecars (idempotent, atomic).

    Returns a report dict.  A v2 plan is a no-op (the pair is re-verified).
    A v1 plan is split: completed tasks are archived losslessly, unfinished
    tasks stay active with concise fields, and the sidecars are written
    before the plan so any partial pair fails closed on the plan's binding.
    """
    root = _as_root(root)
    plan_file = root / plan_path
    archive_file = root / archive_path
    history_file = root / history_path
    plan_bytes = _read_bounded(plan_file, plan_sidecars.SIDECAR_MAX_BYTES, "plan")
    # A v2 plan can only be parsed against its bound archive records (the
    # final-audit dependency closure references archived completed tasks).
    # A v1 plan may predate the sidecars entirely (empty archive).
    try:
        archive_bytes = _read_bounded(
            archive_file, plan_sidecars.SIDECAR_MAX_BYTES, "archive sidecar"
        )
        archive_records = plan_sidecars.parse_archive(archive_bytes)
    except PlanMigrationError:
        if not archive_file.exists():
            archive_bytes = b""
            archive_records = []
        else:
            raise
    except plan_sidecars.PlanSidecarError as exc:
        raise PlanMigrationError(
            f"the archive sidecar is not usable: {exc}"
        ) from exc
    try:
        plan = plan_parser.Plan.from_bytes(
            plan_bytes, archive_records=archive_records
        )
    except plan_parser.PlanError as exc:
        raise PlanMigrationError(f"the plan does not parse: {exc}") from exc

    if plan.schema == plan_parser.SCHEMA_NAME:
        # Already v2: verify the pair and report a no-op.
        try:
            binding = plan_sidecars.load_plan_binding(root, plan_bytes)
        except plan_sidecars.PlanSidecarError as exc:
            raise PlanMigrationStateError(
                f"the v2 plan-sidecar pair is inconsistent: {exc}"
            ) from exc
        return {
            "schema": plan_parser.SCHEMA_NAME,
            "migrated": False,
            "active_tasks": [task.number for task in plan.tasks],
            "archived_tasks": sorted(
                record.task_id for record in binding["archive"]
            ),
            "binding_digest": binding["binding_digest"],
        }

    if plan.schema != plan_parser.SCHEMA_V1:
        raise PlanMigrationError(f"unsupported plan schema {plan.schema!r}")

    active_tasks = [
        task for task in plan.tasks if task.status != "complete"
    ]
    completed_tasks = [
        task for task in plan.tasks if task.status == "complete"
    ]
    if not active_tasks:
        raise PlanMigrationError(
            "a v1 plan with no unfinished tasks cannot be migrated to an "
            "active v2 plan; the lifecycle is complete"
        )
    if archived_commit is None:
        archived_commit = "0" * 40
    if not plan_sidecars.SHA40_RE.fullmatch(archived_commit):
        raise PlanMigrationError("archived_commit must be a 40-hex commit")

    archive_records = [
        _task_to_archive_record(
            task, archived_commit=archived_commit, provenance="migration"
        )
        for task in completed_tasks
    ]
    archive_bytes = plan_sidecars.serialize_archive(archive_records)
    archive_digest = plan_sidecars.sidecar_digest(archive_bytes)

    history_records = [
        plan_sidecars.HistoryRecord(
            schema=plan_sidecars.HISTORY_SCHEMA,
            event="migrated",
            task_id=None,
            commit=archived_commit,
            plan_digest=plan_sidecars.sidecar_digest(plan_bytes),
            detail=(
                f"migrated factory-plan/v1 plan ({len(plan.tasks)} tasks, "
                f"{len(completed_tasks)} archived) to factory-plan/v2"
            ),
        )
    ]
    history_bytes = plan_sidecars.serialize_history(history_records)
    history_digest = plan_sidecars.sidecar_digest(history_bytes)

    v2_text = _render_v2_plan(
        plan,
        active_tasks,
        archive_digest=archive_digest,
        history_digest=history_digest,
        goal=_condense(_canonical_section_text(plan, "Goal and non-goals")),
        architecture=_condense(
            _canonical_section_text(plan, "Architecture and constraints")
        ),
    )
    v2_bytes = v2_text.encode("utf-8")
    if len(v2_bytes) > PLAN_SIZE_CEILING:
        raise PlanMigrationError(
            f"the migrated v2 plan is {len(v2_bytes)} bytes, exceeding the "
            f"{PLAN_SIZE_CEILING}-byte ceiling"
        )

    # Atomic, crash-safe, no partial pair: sidecars first, plan last.  A crash
    # leaves (old plan, old sidecars), (old plan, new sidecars), or (new plan,
    # new sidecars); the v2 plan's binding makes the middle state fail closed
    # and the v1 plan never reads the sidecars.
    plan_sidecars._atomic_write_committed(archive_file, archive_bytes)
    plan_sidecars._atomic_write_committed(history_file, history_bytes)
    plan_sidecars._atomic_write_committed(plan_file, v2_bytes)

    return {
        "schema": plan_parser.SCHEMA_NAME,
        "migrated": True,
        "active_tasks": [task.number for task in active_tasks],
        "archived_tasks": [record.task_id for record in archive_records],
        "archive_digest": archive_digest,
        "history_digest": history_digest,
        "plan_bytes": len(v2_bytes),
        "v1_plan_bytes": len(plan_bytes),
    }


def archive_task(
    root,
    task_id: int,
    *,
    plan_path: str = DEFAULT_PLAN_PATH,
    archive_path: str = DEFAULT_ARCHIVE_PATH,
    history_path: str = DEFAULT_HISTORY_PATH,
    commit: str,
) -> Dict[str, object]:
    """Archive one independently verified completed task (append-only).

    The task must exist in the active v2 plan with status ``complete`` (the
    transient developer worktree state) or be moved from the plan verbatim;
    it is appended to the archive sidecar with provenance ``campaign`` and
    removed from the plan, and the plan's ``sidecars:`` binding is rewritten.
    Reopening an archived task is an explicit semantic migration back into
    the active plan, never model prose.
    """
    root = _as_root(root)
    if not plan_sidecars.SHA40_RE.fullmatch(commit):
        raise PlanMigrationError("commit must be a 40-hex commit")
    plan_file = root / plan_path
    archive_file = root / archive_path
    history_file = root / history_path
    plan_bytes = _read_bounded(plan_file, plan_sidecars.SIDECAR_MAX_BYTES, "plan")
    try:
        archive_records = plan_sidecars.read_archive(root)
    except plan_sidecars.PlanSidecarError as exc:
        raise PlanMigrationError(
            f"the archive sidecar is not usable: {exc}"
        ) from exc
    try:
        plan = plan_parser.Plan.from_bytes(
            plan_bytes, archive_records=archive_records
        )
    except plan_parser.PlanError as exc:
        raise PlanMigrationError(f"the plan does not parse: {exc}") from exc
    if plan.schema != plan_parser.SCHEMA_NAME:
        raise PlanMigrationError(
            "archive_task requires a factory-plan/v2 plan; migrate first"
        )
    existing = archive_records
    if any(record.task_id == task_id for record in existing):
        raise PlanMigrationError(f"task {task_id} is already archived")
    task = next((t for t in plan.tasks if t.number == task_id), None)
    if task is None:
        raise PlanMigrationError(f"task {task_id} is absent from the active plan")
    if task.status != "complete":
        raise PlanMigrationError(
            f"task {task_id} has status {task.status!r}; only an independently "
            "verified completed task may be archived"
        )

    record = _task_to_archive_record(
        task, archived_commit=commit, provenance="campaign"
    )
    archive_bytes = plan_sidecars.serialize_archive([*existing, record])
    archive_digest = plan_sidecars.sidecar_digest(archive_bytes)

    history = plan_sidecars.read_history(root)
    history_records = [
        *history,
        plan_sidecars.HistoryRecord(
            schema=plan_sidecars.HISTORY_SCHEMA,
            event="archived",
            task_id=task_id,
            commit=commit,
            plan_digest=plan_sidecars.sidecar_digest(plan_bytes),
            detail=f"archived completed task {task_id} after verified completion",
        ),
    ]
    history_bytes = plan_sidecars.serialize_history(history_records)
    history_digest = plan_sidecars.sidecar_digest(history_bytes)

    remaining = [t for t in plan.tasks if t.number != task_id]
    v2_text = _render_v2_plan(
        plan,
        remaining,
        archive_digest=archive_digest,
        history_digest=history_digest,
        goal=_canonical_section_text(plan, "Goal and non-goals"),
        architecture=_canonical_section_text(plan, "Architecture and constraints"),
    )
    v2_bytes = v2_text.encode("utf-8")
    if len(v2_bytes) > PLAN_SIZE_CEILING:
        raise PlanMigrationError(
            f"the archived v2 plan is {len(v2_bytes)} bytes, exceeding the "
            f"{PLAN_SIZE_CEILING}-byte ceiling"
        )
    plan_sidecars._atomic_write_committed(archive_file, archive_bytes)
    plan_sidecars._atomic_write_committed(history_file, history_bytes)
    plan_sidecars._atomic_write_committed(plan_file, v2_bytes)
    return {
        "archived": task_id,
        "remaining_tasks": [t.number for t in remaining],
        "archive_digest": archive_digest,
        "history_digest": history_digest,
    }


def rehash_plan(
    root,
    *,
    plan_path: str = DEFAULT_PLAN_PATH,
    archive_path: str = DEFAULT_ARCHIVE_PATH,
    history_path: str = DEFAULT_HISTORY_PATH,
) -> Dict[str, object]:
    """Rewrite the plan's ``sidecars:`` binding to the current sidecar digests.

    A planner edit that adds/removes active tasks (or reorders them) does not
    change the sidecars; this command recomputes the digests and rewrites the
    plan front matter so the plan-sidecar pair stays consistent.  The plan
    must already be v2 and must parse with the current archive records.
    """
    root = _as_root(root)
    plan_file = root / plan_path
    archive_file = root / archive_path
    history_file = root / history_path
    plan_bytes = _read_bounded(plan_file, plan_sidecars.SIDECAR_MAX_BYTES, "plan")
    archive_bytes = _read_bounded(
        archive_file, plan_sidecars.SIDECAR_MAX_BYTES, "archive sidecar"
    )
    history_bytes = _read_bounded(
        history_file, plan_sidecars.SIDECAR_MAX_BYTES, "history sidecar"
    )
    archive_records = plan_sidecars.parse_archive(archive_bytes)
    try:
        plan = plan_parser.Plan.from_bytes(
            plan_bytes, archive_records=archive_records
        )
    except plan_parser.PlanError as exc:
        raise PlanMigrationError(f"the v2 plan does not parse: {exc}") from exc
    if plan.schema != plan_parser.SCHEMA_NAME:
        raise PlanMigrationError("rehash_plan requires a factory-plan/v2 plan")
    archive_digest = plan_sidecars.sidecar_digest(archive_bytes)
    history_digest = plan_sidecars.sidecar_digest(history_bytes)
    if plan.sidecars == {"archive": archive_digest, "history": history_digest}:
        return {"rehashed": False, "archive_digest": archive_digest,
                "history_digest": history_digest}
    text = plan_bytes.decode("utf-8")
    binding = json.dumps(
        {"archive": archive_digest, "history": history_digest},
        sort_keys=True, separators=(",", ":"),
    )
    new_text, count = re.subn(
        r"(?m)^sidecars: .*$",
        f"sidecars: {binding}",
        text,
        count=1,
    )
    if count != 1:
        raise PlanMigrationError("the v2 plan has no sidecars front-matter line")
    plan_sidecars._atomic_write_committed(plan_file, new_text.encode("utf-8"))
    return {"rehashed": True, "archive_digest": archive_digest,
            "history_digest": history_digest}


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="factory-plan-migration",
        description=(
            "Deterministic factory-plan/v1 -> v2 migration and task archival "
            "(Phase 2D1)."
        ),
    )
    parser.add_argument(
        "--root",
        metavar="ROOT",
        default=str(Path(__file__).resolve().parent.parent.parent),
        help="canonical repository root (default: this repository)",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_migrate = sub.add_parser("migrate", help="migrate v1 plan to v2 + sidecars")
    p_migrate.add_argument("--plan-path", default=DEFAULT_PLAN_PATH)
    p_migrate.add_argument("--archive-path", default=DEFAULT_ARCHIVE_PATH)
    p_migrate.add_argument("--history-path", default=DEFAULT_HISTORY_PATH)
    p_migrate.add_argument(
        "--archived-commit", default=None,
        help="exact commit at which the completed tasks were verified",
    )

    p_archive = sub.add_parser(
        "archive-task", help="archive one verified completed task"
    )
    p_archive.add_argument("task_id", type=int)
    p_archive.add_argument("--commit", required=True)
    p_archive.add_argument("--plan-path", default=DEFAULT_PLAN_PATH)
    p_archive.add_argument("--archive-path", default=DEFAULT_ARCHIVE_PATH)
    p_archive.add_argument("--history-path", default=DEFAULT_HISTORY_PATH)

    p_rehash = sub.add_parser(
        "rehash", help="rewrite the plan sidecars binding to the current digests"
    )
    p_rehash.add_argument("--plan-path", default=DEFAULT_PLAN_PATH)
    p_rehash.add_argument("--archive-path", default=DEFAULT_ARCHIVE_PATH)
    p_rehash.add_argument("--history-path", default=DEFAULT_HISTORY_PATH)

    args = parser.parse_args(argv)
    root = Path(args.root)
    try:
        if args.command == "migrate":
            report = migrate_plan(
                root,
                plan_path=args.plan_path,
                archive_path=args.archive_path,
                history_path=args.history_path,
                archived_commit=args.archived_commit,
            )
        elif args.command == "archive-task":
            report = archive_task(
                root,
                args.task_id,
                plan_path=args.plan_path,
                archive_path=args.archive_path,
                history_path=args.history_path,
                commit=args.commit,
            )
        else:
            report = rehash_plan(
                root,
                plan_path=args.plan_path,
                archive_path=args.archive_path,
                history_path=args.history_path,
            )
    except (PlanMigrationError, plan_sidecars.PlanSidecarError) as exc:
        print(f"factory-plan-migration: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(report, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
