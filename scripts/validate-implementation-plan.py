#!/usr/bin/env python3
"""Validate implementation-plan structure and definition-of-done state."""

from __future__ import annotations

import re
import sys
from pathlib import Path

ALLOWED_CLASSIFICATIONS = {"verified", "partial", "missing", "ambiguous"}
ALLOWED_STATUSES = {"pending", "in_progress", "complete", "blocked"}
FINAL_TITLE = "Final documentation and specification audit"
SHA = re.compile(r"^[0-9a-f]{40}$")
MATRIX_ID = re.compile(r"^[A-Z][A-Z0-9]*(?:[-_][A-Z0-9]+)+$")
REQUIRED_TASK_FIELDS = (
    "Status", "Dependencies", "Scope", "Acceptance criteria", "Verification",
    "Documentation impact",
)


def fail(message: str) -> None:
    raise SystemExit(f"implementation-plan: {message}")


def section(text: str, title: str) -> str:
    matches = list(re.finditer(
        rf"^## {re.escape(title)}\s*$\n(.*?)(?=^##\s|\Z)", text, re.M | re.S
    ))
    if len(matches) != 1:
        fail(f"requires exactly one `## {title}` section")
    return matches[0].group(1)


def parse_dependencies(value: str, task_number: int) -> list[int]:
    value = value.strip()
    if value.lower() == "none":
        return []
    if not value:
        fail(f"Task {task_number} has an empty dependency list")
    result: list[int] = []
    for item in (part.strip() for part in value.split(",")):
        match = re.fullmatch(r"Tasks?\s+(\d+)(?:\s*[-–—]\s*(\d+))?", item, re.I)
        if not match:
            fail(f"Task {task_number} has malformed dependencies: {value}")
        start = int(match.group(1))
        end = int(match.group(2) or start)
        if end < start:
            fail(f"Task {task_number} has a descending dependency range: {item}")
        result.extend(range(start, end + 1))
    if len(result) != len(set(result)):
        fail(f"Task {task_number} repeats a dependency")
    return result


def parse_tasks(text: str) -> list[dict[str, object]]:
    malformed = [
        line for line in text.splitlines()
        if line.startswith("## Task") and not re.fullmatch(r"## Task\s+\d+:\s*\S.*", line)
    ]
    if malformed:
        fail(f"malformed task heading: {malformed[0]}")
    headings = list(re.finditer(r"^## Task\s+(\d+):\s*(.+?)\s*$", text, re.M))
    if not headings:
        fail("plan has no numbered tasks")

    tasks: list[dict[str, object]] = []
    seen_titles: set[str] = set()
    for index, heading in enumerate(headings):
        number = int(heading.group(1))
        expected = index + 1
        if number != expected:
            fail(f"task numbers must be unique and contiguous (expected Task {expected}, found Task {number})")
        title = heading.group(2).strip()
        if title in seen_titles:
            fail(f"duplicate task title: {title}")
        seen_titles.add(title)
        end = headings[index + 1].start() if index + 1 < len(headings) else len(text)
        body = text[heading.end():end]
        fields: dict[str, str] = {}
        for field in REQUIRED_TASK_FIELDS:
            values = re.findall(rf"^- {re.escape(field)}:\s*(.*?)\s*$", body, re.M)
            if len(values) != 1:
                fail(f"Task {number} requires exactly one canonical `- {field}:` field")
            if field not in {"Documentation impact"} and not values[0].strip():
                fail(f"Task {number} has an empty `- {field}:` field")
            fields[field] = values[0].strip()
        status = fields["Status"]
        if status not in ALLOWED_STATUSES:
            fail(f"Task {number} has invalid status `{status}`")
        dependencies = parse_dependencies(fields["Dependencies"], number)
        tasks.append({
            "number": number,
            "title": title,
            "status": status,
            "dependencies": dependencies,
            "body": body,
        })

    numbers = {int(task["number"]) for task in tasks}
    graph: dict[int, list[int]] = {}
    final_number = None
    for task in tasks:
        if task["title"] == FINAL_TITLE:
            if final_number is not None:
                fail("plan has more than one final audit task")
            final_number = int(task["number"])
    for task in tasks:
        number = int(task["number"])
        dependencies = list(task["dependencies"])
        unknown = sorted(set(dependencies) - numbers)
        if unknown:
            fail(f"Task {number} references unknown dependencies: {unknown}")
        if number in dependencies:
            fail(f"Task {number} cannot depend on itself")
        if any(dependency > number for dependency in dependencies):
            # Only the final audit may depend on appended remediation tasks
            # that follow it in the ledger. Any other forward dependency is a
            # defect: a task may only depend on tasks it can actually await.
            if number != final_number:
                fail(f"Task {number} dependencies must refer to earlier tasks")
        graph[number] = dependencies

    visiting: set[int] = set()
    visited: set[int] = set()

    def visit(number: int) -> None:
        if number in visiting:
            fail("task dependency graph contains a cycle")
        if number in visited:
            return
        visiting.add(number)
        for dependency in graph[number]:
            visit(dependency)
        visiting.remove(number)
        visited.add(number)

    for number in graph:
        visit(number)
    return tasks


def parse_task_references(cell: str, *, context: str) -> set[int]:
    if not cell.strip():
        return set()
    refs = parse_dependencies(cell, 0)
    return set(refs)


def table_cells(line: str) -> list[str]:
    return [cell.strip() for cell in line.strip().strip("|").split("|")]


def validate_matrix(text: str, task_numbers: set[int], task_statuses: dict[int, str], complete: bool) -> None:
    matrix = section(text, "Specification conformance matrix")
    lines = [line.strip() for line in matrix.splitlines() if line.strip()]
    table_lines = [line for line in lines if line.startswith("|")]
    if len(table_lines) < 3:
        fail("conformance matrix has no machine-checkable requirement rows")
    header = table_cells(table_lines[0])
    if header != ["ID", "Spec §", "Classification", "Evidence", "Task"]:
        fail("conformance matrix header must be exactly `ID | Spec § | Classification | Evidence | Task`")
    separator = table_cells(table_lines[1])
    if len(separator) != 5 or any(not re.fullmatch(r":?-{3,}:?", cell) for cell in separator):
        fail("conformance matrix separator is malformed")

    seen_ids: set[str] = set()
    for line in table_lines[2:]:
        cells = table_cells(line)
        if len(cells) != 5:
            fail(f"conformance row must have exactly five cells: {line}")
        requirement_id, spec_section, classification, evidence, task_cell = cells
        if not MATRIX_ID.fullmatch(requirement_id):
            fail(f"invalid conformance requirement ID: {requirement_id}")
        if requirement_id in seen_ids:
            fail(f"duplicate conformance requirement ID: {requirement_id}")
        seen_ids.add(requirement_id)
        if not spec_section or not evidence:
            fail(f"conformance row {requirement_id} requires spec and evidence cells")
        if classification not in ALLOWED_CLASSIFICATIONS:
            fail(f"conformance row {requirement_id} has invalid classification `{classification}`")
        task_refs = parse_task_references(task_cell, context=requirement_id)
        if not task_refs.issubset(task_numbers):
            fail(f"conformance row {requirement_id} references an unknown task")
        if classification != "verified" and not task_refs:
            fail(f"non-verified conformance row {requirement_id} must reference an existing task")
        if classification != "verified":
            unresolved = sorted(number for number in task_refs if task_statuses[number] == "complete")
            if len(task_refs) == len(unresolved):
                fail(
                    f"non-verified conformance row {requirement_id} references only completed "
                    f"tasks ({unresolved}); a pending/in-progress/blocked task must own it"
                )
        if complete and classification != "verified":
            fail(f"completion rejected while conformance row {requirement_id} is `{classification}`")
        if complete and re.search(
            r"\b(defer(?:red|ral)?|unavailable|unevidenced|blocked|pending|skip(?:ped)?)\b",
            evidence,
            re.I,
        ):
            fail(f"completion rejected while verified row {requirement_id} describes a blocker")
    if not seen_ids:
        fail("conformance matrix has no requirement rows")


def validate_interactions(text: str) -> None:
    inventory = section(text, "Interaction acceptance inventory").lower()
    for term in ("input", "semantic", "production", "evidence"):
        if term not in inventory:
            fail(f"interaction inventory must describe `{term}` coverage")


def validate_final_task(tasks: list[dict[str, object]], fresh: bool) -> None:
    finals = [task for task in tasks if task["title"] == FINAL_TITLE]
    if len(finals) != 1:
        fail(f"plan requires exactly one task titled `{FINAL_TITLE}`")
    final = finals[0]
    final_index = tasks.index(final)
    appended = tasks[final_index + 1:]
    if fresh and appended:
        fail("a fresh plan must end with the final documentation and specification audit")
    all_other = {int(task["number"]) for task in tasks if task is not final}
    dependencies = set(int(value) for value in final["dependencies"])
    if dependencies != all_other:
        fail("final audit must depend on every other task and no others")
    if appended:
        expected = int(final["number"]) + 1
        for task in appended:
            if int(task["number"]) != expected:
                fail("appended remediation tasks after the final audit must be uniquely and contiguously numbered")
            if task["status"] == "complete":
                fail("a completed appended task after the final audit contradicts the active ledger")
            expected += 1
        if final["status"] == "complete":
            fail("a completed final audit may not have appended tasks after it")
    elif final is not tasks[-1]:
        fail("final documentation and specification audit must be the last task")
    body = str(final["body"]).lower()
    for term in ("definition of done", "conformance", "interaction", "open", "review", "clean"):
        if term not in body:
            fail(f"final audit task must explicitly cover `{term}`")


def validate_front_matter(text: str, mode: str) -> None:
    front = re.match(r"\A---\n(.*?)\n---(?:\n|\Z)", text, re.S)
    if not front:
        fail("front matter must start on the first line and be terminated")
    fields: dict[str, str] = {}
    for line in front.group(1).splitlines():
        match = re.fullmatch(r"([a-z_]+):\s*(\S.*?)\s*", line)
        if not match or match.group(1) in fields:
            fail("front matter has malformed or duplicate fields")
        fields[match.group(1)] = match.group(2)
    expected = {"spec_path", "spec_commit", "spec_blob", "base_commit", "status"}
    if set(fields) != expected:
        fail("front matter fields do not match the implementation-plan schema")
    if not fields["spec_path"] or Path(fields["spec_path"]).is_absolute():
        fail("front matter spec_path must be repository-relative")
    for field in ("spec_commit", "spec_blob", "base_commit"):
        if not SHA.fullmatch(fields[field]):
            fail(f"front matter {field} must be a 40-character Git object ID")
    expected_status = "active" if mode == "planning" else "complete"
    if fields["status"] != expected_status:
        fail(f"front matter must have exactly `status: {expected_status}`")
    if len(re.findall(r"^# Implementation Plan\s*$", text, re.M)) != 1:
        fail("plan requires exactly one `# Implementation Plan` title")


def main() -> None:
    if len(sys.argv) != 3 or sys.argv[1] not in {"planning", "complete"}:
        raise SystemExit("usage: validate-implementation-plan.py planning|complete .factory/artifacts/implementation-plan.md")
    mode, path = sys.argv[1], Path(sys.argv[2])
    text = path.read_text(encoding="utf-8")
    validate_front_matter(text, mode)
    tasks = parse_tasks(text)
    task_numbers = {int(task["number"]) for task in tasks}
    task_statuses = {int(task["number"]): str(task["status"]) for task in tasks}
    validate_matrix(text, task_numbers, task_statuses, complete=mode == "complete")
    validate_interactions(text)

    if mode == "planning":
        non_pending = [task for task in tasks if task["status"] != "pending"]
        fresh = not non_pending
        validate_final_task(tasks, fresh=fresh)
        if fresh:
            for task in tasks:
                if any(dep > int(task["number"]) for dep in task["dependencies"]):
                    fail("fresh plans must not contain forward dependencies")
        elif not any(task["status"] == "complete" for task in tasks):
            fail("an active-cycle plan must preserve its completed task ledger")
    else:
        unfinished = [task for task in tasks if task["status"] != "complete"]
        if unfinished:
            details = ", ".join(
                f"Task {task['number']}={task['status']}" for task in unfinished
            )
            fail(f"completion requires every task complete ({details})")
        validate_final_task(tasks, fresh=False)


if __name__ == "__main__":
    main()
