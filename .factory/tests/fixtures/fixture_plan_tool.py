#!/usr/bin/env python3
"""Fixture `factory-plan/v1` generator for the hidden campaign suite (Task 9).

The hidden campaign tests drive deterministic fixture repos.  Every fixture
plan must be a *valid* ``factory-plan/v1`` document (the committed parser is
part of the acceptance boundary), so this committed tool generates the full
canonical plan from a JSON spec and validates it with the real parser before
writing it.  It is harness-only and lives under ``.factory/tests/fixtures/``.

The JSON spec:

.. code-block:: json

    {
      "spec_path": "docs/SPEC.md",
      "spec_commit": "<40hex>",
      "spec_blob": "<40hex>",
      "base_commit": "<40hex>",
      "lifecycle": "active",
      "tasks": [
        {"number": 1, "title": "Implement the fixture feature",
         "status": "pending", "priority": 10, "dependencies": [],
         "blocked_on": null, "scope": "...", "acceptance": "...",
         "verification": "`src/work-1.md`", "documentation": "none",
         "evidence": ""}
      ]
    }

The generator enforces the parser's structural rules itself: contiguous task
numbers, the final-audit task last (depending on every other task), blocked
tasks naming exact references, at most one ``in_progress`` task, the §24
conformance matrix covering every registry ID with a lifecycle-consistent
classification, and the four interaction boundaries.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Dict, List, Optional

HERE = Path(__file__).resolve().parent
LOOP = HERE.parent.parent / "loop"
sys.path.insert(0, str(LOOP))

import plan_parser  # noqa: E402

FINAL_AUDIT_TITLE = plan_parser.FINAL_AUDIT_TITLE
MATRIX_HEADER = "| ID | Spec § | Classification | Evidence | Task |"
MATRIX_SEPARATOR = "|----|--------|--------------|----------|------|"


def _registry_ids(registry: Path) -> List[str]:
    data = json.loads(registry.read_bytes())
    if not isinstance(data, dict) or not isinstance(data.get("requirement_ids"), list):
        raise SystemExit(f"invalid requirement registry {registry}")
    ids = [str(item) for item in data["requirement_ids"]]
    if len(set(ids)) != len(ids) or not ids:
        raise SystemExit("requirement registry must be a unique non-empty id list")
    return ids


def generate(spec: Dict[str, object], registry: Path) -> str:
    spec_path = spec["spec_path"]
    spec_commit = spec["spec_commit"]
    spec_blob = spec["spec_blob"]
    base_commit = spec["base_commit"]
    lifecycle = spec["lifecycle"]
    raw_tasks = spec["tasks"]
    if lifecycle not in ("active", "complete"):
        raise SystemExit(f"lifecycle must be active|complete, got {lifecycle!r}")
    if not isinstance(raw_tasks, list) or not raw_tasks:
        raise SystemExit("spec requires a non-empty task list")

    tasks: List[Dict[str, object]] = []
    for index, raw in enumerate(raw_tasks):
        number = int(raw["number"])
        if number != index + 1:
            raise SystemExit(
                f"task numbers must be contiguous from 1 (expected {index + 1}, "
                f"got {number})"
            )
        status = raw["status"]
        if status not in plan_parser.TASK_STATUSES:
            raise SystemExit(f"task {number} has invalid status {status!r}")
        tasks.append(raw)

    # The final task must be the audit task and depend on every other task.
    last = tasks[-1]
    last["title"] = FINAL_AUDIT_TITLE
    last["dependencies"] = list(range(1, len(tasks)))

    if lifecycle == "complete":
        for raw in tasks:
            raw["status"] = "complete"

    for raw in tasks:
        if raw["status"] == "blocked" and not raw.get("blocked_on"):
            raise SystemExit(
                f"task {raw['number']} is blocked but names no exact reference"
            )
        if raw["status"] in ("blocked", "in_progress"):
            for dep in raw.get("dependencies", []):
                dep_status = next(
                    (t["status"] for t in tasks if t["number"] == dep), None
                )
                if dep_status != "complete":
                    raise SystemExit(
                        f"task {raw['number']} has a non-complete dependency"
                    )
    if sum(1 for t in tasks if t["status"] == "in_progress") > 1:
        raise SystemExit("at most one task may be in_progress")

    registry_ids = _registry_ids(registry)
    if lifecycle == "active":
        row_task = next(
            (t["number"] for t in tasks if t["status"] != "complete"), 1
        )
        classification = "missing"
    else:
        row_task = len(tasks)
        classification = "verified"
    matrix = "\n".join(
        f"| {rid} | §5, §7 | {classification} | fixture evidence | "
        f"Task {row_task} |"
        for rid in registry_ids
    )

    blocks: List[str] = [
        "---",
        f"spec_path: {spec_path}",
        f"spec_commit: {spec_commit}",
        f"spec_blob: {spec_blob}",
        f"base_commit: {base_commit}",
        f"status: {lifecycle}",
        "---",
        "",
        "# Implementation Plan",
        "",
        "## Goal and non-goals",
        "",
        "Goal: exercise the Task 9 phase/campaign orchestrator deterministically.",
        "",
        "Non-goals: no product code beyond the fixture scope.",
        "",
        "## Architecture and constraints",
        "",
        "Fixture plan: Python 3.11 standard library only; the plan is the sole",
        "task authority.",
        "",
        "## Specification conformance matrix",
        "",
        MATRIX_HEADER,
        MATRIX_SEPARATOR,
        matrix,
        "",
        "## Interaction acceptance inventory",
        "",
        "- input boundary: every role receives only the fresh allowlisted inputs.",
        "- semantic boundary: no runtime task ledger, memory, or event stream.",
        "- production boundary: harness state stays under hidden namespaces.",
        "- evidence boundary: machine receipts and deterministic gates only.",
        "",
    ]
    for raw in tasks:
        number = raw["number"]
        title = raw["title"]
        status = raw["status"]
        deps = raw.get("dependencies", [])
        dep_text = ", ".join(f"Task {dep}" for dep in deps) if deps else "None"
        blocks.extend(
            [
                f"## Task {number}: {title}",
                "",
                f"- Status: {status}",
                f"- Dependencies: {dep_text}",
                f"- Priority: {raw.get('priority', 10)}",
                f"- Scope: {raw.get('scope', 'fixture-scoped work only.')}",
                f"- Acceptance criteria: {raw.get('acceptance', 'fixture acceptance gate passes.')}",
                f"- Verification: {raw.get('verification', '`src/work.md`')}",
                f"- Documentation impact: {raw.get('documentation', 'none.')}",
            ]
        )
        if raw.get("blocked_on"):
            blocks.append(f"- Blocked on: {raw['blocked_on']}")
        if raw.get("write_scopes"):
            scopes = raw["write_scopes"]
            if not isinstance(scopes, list) or not scopes or any(
                not isinstance(item, str) or not item for item in scopes
            ):
                raise SystemExit(
                    f"task {number} has a malformed write_scopes list"
                )
            blocks.append(f"- Write scopes: {', '.join(scopes)}")
        if raw.get("evidence"):
            blocks.append(f"- Evidence: {raw['evidence']}")
        blocks.append("")
    blocks.append("")

    text = "\n".join(blocks)
    plan = plan_parser.Plan.from_text(text)
    serialized = plan.serialize()
    plan_parser.parse_plan(serialized)
    return serialized


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="fixture_plan_tool")
    parser.add_argument("--spec", required=True, metavar="JSON")
    parser.add_argument("--registry", required=True, metavar="PATH")
    parser.add_argument("--out", required=True, metavar="PATH")
    args = parser.parse_args(argv)
    try:
        spec = json.loads(Path(args.spec).read_bytes())
    except (OSError, ValueError) as exc:
        print(f"fixture_plan_tool: cannot read spec: {exc}", file=sys.stderr)
        return 2
    if not isinstance(spec, dict):
        print("fixture_plan_tool: spec must be a JSON object", file=sys.stderr)
        return 2
    text = generate(spec, Path(args.registry))
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(text.encode("utf-8"))
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    print(f"wrote {out} sha256={digest}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
