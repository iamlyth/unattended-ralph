#!/usr/bin/env python3
"""Shared commit-substance classifier for the Git commit boundary (Phase 2D1).

The Git commit boundary (``.factory/tools/git-commit-guard.sh`` and the model
command layer ``.factory/tools/pi-cli-shims/git``) rejects metadata-only
progress.  This module is the shared classifier both layers call:

* :func:`is_administrative_only` — a staged path set is *administrative-only*
  when every path is the implementation plan, the plan sidecars, the bug
  ledgers, or the campaign audit/evidence sidecars.  Such a set carries no
  product or harness code and can only manufacture progress when the plan
  itself changed meaningfully.
* :func:`plan_change_is_semantic` — a plan revision is *semantic* when it
  adds/removes/reorders a task or edits a task's title, priority,
  dependencies, Scope, Acceptance criteria, current blocker (``Blocked on``),
  or latest actionable failure (``Latest failure``).  Evidence/status/prose/
  timestamp edits and front-matter digest churn are not meaningful.
* :func:`classify_commit` — the fail-closed entry point: an administrative-
  only staged set is valid exactly when the plan change is semantic.

The classifier parses both plan revisions with the committed deterministic
parser (v1 and v2 both supported; a v2 plan is parsed against its bound
archive sidecar records).  A plan that does not parse fails closed (never
"semantic by default").  The classifier is a pure function of the staged
paths and the two plan byte strings plus the sidecar bytes; it performs no
I/O beyond what the caller supplies.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

try:  # package import (the hidden `.factory/loop/` package)
    from . import plan_parser
    from . import plan_sidecars
except ImportError:  # flat import used by the hidden `.factory/tests/` suite
    import plan_parser  # type: ignore[no-redef]
    import plan_sidecars  # type: ignore[no-redef]

# The administrative-only staged set: the implementation plan, the plan
# sidecars, the bug ledgers, and the campaign audit/evidence sidecars.  A
# commit staged entirely inside this set is valid only when the plan carries
# a genuine semantic planning change.
ADMINISTRATIVE_PATHS = frozenset({
    ".factory/artifacts/implementation-plan.md",
    ".factory/artifacts/plan-archive.jsonl",
    ".factory/artifacts/plan-history.jsonl",
    ".factory/artifacts/conformance.json",
    ".factory/artifacts/extension-conformance.json",
    ".factory/artifacts/blocked-facts.json",
    ".factory/artifacts/campaign-audit.md",
    ".factory/artifacts/campaign-smoke-evidence.json",
    ".factory/artifacts/maintenance-plan.md",
    ".factory/bugs/open.md",
    ".factory/bugs/closed.md",
})

# The semantic per-task fields: a change to any of these is genuine planning
# work.  Status/Evidence/Verification/Documentation impact/prose edits are
# not meaningful (they are lifecycle bookkeeping or evidence narrative).
SEMANTIC_TASK_FIELDS = (
    "title", "priority", "dependencies", "scope", "acceptance",
    "blocked_on", "latest_failure",
)


class SubstanceError(Exception):
    """A staged set or plan revision cannot be classified (fail closed)."""


def is_administrative_only(staged_paths: Sequence[str]) -> bool:
    """True when every staged path is in the administrative-only set.

    An empty staged set is not administrative-only (it is an empty commit,
    rejected by the guard before classification).  A path outside the set —
    any product code, harness code, test, or documentation — makes the commit
    substantive.
    """
    if not staged_paths:
        return False
    return all(path in ADMINISTRATIVE_PATHS for path in staged_paths)


def _task_signature(task: plan_parser.Task) -> Dict[str, object]:
    """The semantic signature of one task (non-semantic fields excluded)."""
    fields = task.fields
    return {
        "title": task.title,
        "priority": task.priority,
        "dependencies": sorted(task.dependencies),
        "scope": fields.get("Scope", ""),
        "acceptance": fields.get("Acceptance criteria", ""),
        "blocked_on": task.blocked_on,
        "latest_failure": task.latest_failure,
    }


def _plan_signature(plan: plan_parser.Plan) -> Dict[int, Dict[str, object]]:
    """Ordered semantic signature of a plan's tasks (task id -> signature)."""
    return {task.number: _task_signature(task) for task in plan.tasks}


def _parse_plan_bytes(
    data: bytes, archive_records: Optional[Sequence[object]]
) -> plan_parser.Plan:
    try:
        return plan_parser.Plan.from_bytes(data, archive_records=archive_records)
    except plan_parser.PlanError as exc:
        raise SubstanceError(f"the plan does not parse: {exc}") from exc


def plan_change_is_semantic(
    old_plan_bytes: bytes,
    new_plan_bytes: bytes,
    *,
    archive_records: Optional[Sequence[object]] = None,
) -> bool:
    """True when the plan revision is a genuine semantic planning change.

    A revision is semantic when the ordered task set changed (a task was
    added, removed, or reordered) or when any task's semantic fields (title,
    priority, dependencies, Scope, Acceptance criteria, current blocker, or
    latest actionable failure) changed.  Status/Evidence/Verification/
    Documentation impact/prose edits, front-matter digest churn, and
    byte-identical revisions are not meaningful.  A plan that does not parse
    fails closed (never semantic by default).
    """
    old_plan = _parse_plan_bytes(old_plan_bytes, archive_records)
    new_plan = _parse_plan_bytes(new_plan_bytes, archive_records)
    old_signature = _plan_signature(old_plan)
    new_signature = _plan_signature(new_plan)
    if old_signature == new_signature:
        return False
    # A reorder is semantic: compare the ordered task-id sequence.
    old_order = [task.number for task in old_plan.tasks]
    new_order = [task.number for task in new_plan.tasks]
    if old_order != new_order:
        return True
    # Same order, same ids: any differing semantic field is meaningful.
    for task_id in old_signature:
        if task_id not in new_signature:
            return True  # task removed
        if old_signature[task_id] != new_signature[task_id]:
            return True
    for task_id in new_signature:
        if task_id not in old_signature:
            return True  # task added
    return False


def classify_commit(
    staged_paths: Sequence[str],
    old_plan_bytes: bytes,
    new_plan_bytes: bytes,
    *,
    archive_records: Optional[Sequence[object]] = None,
) -> Tuple[bool, str]:
    """``(allowed, reason)`` for a staged set at the commit boundary.

    A staged set with any substantive path is allowed.  An administrative-only
    set is allowed exactly when the plan change is semantic; otherwise it is
    rejected as metadata-only progress.  A plan that does not parse fails
    closed.
    """
    if not staged_paths:
        return False, "empty staged set"
    if not is_administrative_only(staged_paths):
        return True, "substantive commit (staged path outside the administrative set)"
    if old_plan_bytes == new_plan_bytes:
        return False, (
            "administrative-only commit with no plan change is metadata-only "
            "progress"
        )
    try:
        semantic = plan_change_is_semantic(
            old_plan_bytes, new_plan_bytes, archive_records=archive_records
        )
    except SubstanceError as exc:
        return False, f"administrative-only commit cannot be classified: {exc}"
    if semantic:
        return True, (
            "administrative-only commit with a genuine semantic planning "
            "change (task add/remove/reorder or title/priority/dependencies/"
            "Scope/Acceptance/blocker/latest-failure edit)"
        )
    return False, (
        "administrative-only commit with only evidence/status/prose edits is "
        "metadata-only progress"
    )


def main(argv: Optional[Sequence[str]] = None) -> int:
    import argparse
    import json
    import sys

    parser = argparse.ArgumentParser(
        prog="factory-substance",
        description="Shared commit-substance classifier (Phase 2D1).",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_classify = sub.add_parser(
        "classify", help="classify a staged set against the plan revisions"
    )
    p_classify.add_argument("--staged", nargs="*", default=[])
    p_classify.add_argument("--old-plan", required=True)
    p_classify.add_argument("--new-plan", required=True)
    p_classify.add_argument("--archive", default=None)

    args = parser.parse_args(argv)
    if args.command == "classify":
        try:
            old_bytes = Path(args.old_plan).read_bytes()
            new_bytes = Path(args.new_plan).read_bytes()
        except OSError as exc:
            print(f"factory-substance: cannot read a plan: {exc}", file=sys.stderr)
            return 2
        archive_records = None
        if args.archive:
            try:
                archive_records = plan_sidecars.parse_archive(
                    Path(args.archive).read_bytes()
                )
            except (OSError, plan_sidecars.PlanSidecarError) as exc:
                print(
                    f"factory-substance: cannot read the archive sidecar: {exc}",
                    file=sys.stderr,
                )
                return 2
        try:
            allowed, reason = classify_commit(
                args.staged, old_bytes, new_bytes,
                archive_records=archive_records,
            )
        except SubstanceError as exc:
            print(f"factory-substance: {exc}", file=sys.stderr)
            return 1
        print(json.dumps({"allowed": allowed, "reason": reason}, sort_keys=True))
        return 0 if allowed else 1
    return 2


if __name__ == "__main__":
    sys.exit(main())
