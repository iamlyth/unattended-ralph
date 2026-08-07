#!/usr/bin/env python3
"""Validate implementation-plan structure and definition-of-done state."""

from __future__ import annotations

import re
import sys
from pathlib import Path

ALLOWED_CLASSIFICATIONS = {"verified", "partial", "missing", "ambiguous"}
ALLOWED_STATUSES = {"pending", "in_progress", "complete", "blocked"}
FINAL_TITLE = "Final documentation and specification audit"


def fail(message: str) -> None:
    raise SystemExit(f"implementation-plan: {message}")


def section(text: str, title: str) -> str:
    match = re.search(
        rf"^## {re.escape(title)}\s*$\n(.*?)(?=^##\s|\Z)", text, re.M | re.S
    )
    if not match:
        fail(f"missing required `## {title}` section")
    return match.group(1)


def parse_tasks(text: str) -> list[dict[str, object]]:
    headings = list(re.finditer(r"^## Task\s+(\d+):\s*(.+?)\s*$", text, re.M))
    if not headings:
        fail("plan has no numbered tasks")

    tasks: list[dict[str, object]] = []
    seen: set[int] = set()
    for index, heading in enumerate(headings):
        number = int(heading.group(1))
        if number in seen:
            fail(f"duplicate Task {number}")
        seen.add(number)
        end = headings[index + 1].start() if index + 1 < len(headings) else len(text)
        body = text[heading.end():end]
        statuses = re.findall(r"^- Status:\s*(\S+)\s*$", body, re.M)
        if len(statuses) != 1 or statuses[0] not in ALLOWED_STATUSES:
            fail(f"Task {number} requires exactly one canonical `- Status:` field")
        dependency_match = re.search(r"^- Dependencies:\s*(.+?)\s*$", body, re.M)
        if not dependency_match:
            fail(f"Task {number} requires `- Dependencies:`")
        tasks.append(
            {
                "number": number,
                "title": heading.group(2).strip(),
                "status": statuses[0],
                "dependencies": dependency_match.group(1).strip(),
                "body": body,
            }
        )
    return tasks


def validate_matrix(text: str, task_numbers: set[int], complete: bool) -> None:
    matrix = section(text, "Specification conformance matrix")
    rows: list[tuple[str, str]] = []
    for line in matrix.splitlines():
        if not line.lstrip().startswith("|"):
            continue
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        classification = next(
            (cell.lower() for cell in cells if cell.lower() in ALLOWED_CLASSIFICATIONS),
            None,
        )
        if classification:
            rows.append((classification, " | ".join(cells)))
    if not rows:
        fail("conformance matrix has no machine-checkable requirement rows")

    for classification, row in rows:
        if complete and classification != "verified":
            fail(f"completion rejected while conformance row is `{classification}`: {row}")
        if classification != "verified":
            task_refs = {int(value) for value in re.findall(r"\bTask\s+(\d+)\b", row, re.I)}
            if not task_refs or not task_refs.issubset(task_numbers):
                fail(f"non-verified conformance row must reference an existing task: {row}")


def validate_interactions(text: str) -> None:
    inventory = section(text, "Interaction acceptance inventory").lower()
    for term in ("controller", "pointer", "semantic", "production"):
        if term not in inventory:
            fail(f"interaction inventory must describe `{term}` coverage")


def validate_final_task(tasks: list[dict[str, object]]) -> None:
    finals = [task for task in tasks if task["title"] == FINAL_TITLE]
    if len(finals) != 1:
        fail(f"plan requires exactly one task titled `{FINAL_TITLE}`")
    final = finals[0]
    all_other = {int(task["number"]) for task in tasks if task is not final}
    dependencies = {int(value) for value in re.findall(r"\d+", str(final["dependencies"]))}
    missing = sorted(all_other - dependencies)
    if missing:
        fail(f"final audit must depend on every other task (missing: {missing})")
    body = str(final["body"]).lower()
    for term in ("definition of done", "conformance", "interaction", "open", "review", "clean"):
        if term not in body:
            fail(f"final audit task must explicitly cover `{term}`")


def main() -> None:
    if len(sys.argv) != 3 or sys.argv[1] not in {"planning", "complete"}:
        raise SystemExit("usage: validate-implementation-plan.py planning|complete IMPLEMENTATION_PLAN.md")
    mode, path = sys.argv[1], Path(sys.argv[2])
    text = path.read_text(encoding="utf-8")
    tasks = parse_tasks(text)
    task_numbers = {int(task["number"]) for task in tasks}

    validate_matrix(text, task_numbers, complete=mode == "complete")
    validate_interactions(text)
    validate_final_task(tasks)

    expected_front_status = "active" if mode == "planning" else "complete"
    if not re.search(rf"^status:\s*{expected_front_status}\s*$", text, re.M):
        fail(f"front matter must have `status: {expected_front_status}`")

    if mode == "planning":
        non_pending = [task for task in tasks if task["status"] != "pending"]
        if non_pending:
            fail("every task in a fresh plan must be pending")
    else:
        unfinished = [task for task in tasks if task["status"] != "complete"]
        if unfinished:
            fail(f"{len(unfinished)} task(s) are not complete")


if __name__ == "__main__":
    main()
