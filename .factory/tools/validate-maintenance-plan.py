#!/usr/bin/env python3
"""Strictly parse and validate .factory/artifacts/maintenance-plan.md (stdlib only)."""
from __future__ import annotations

import argparse
import json
import pathlib
import re
import sys

KEYS = (
    "bug_id", "bug_fingerprint", "spec_path", "spec_commit", "spec_blob",
    "base_commit", "status",
)
FIELDS = (
    "Status", "Dependencies", "Scope", "Acceptance criteria", "Verification",
    "Documentation impact",
)
TASK_STATUSES = {"pending", "in_progress", "complete", "blocked"}
FINAL_TITLE = "Maintenance verification and documentation audit"
TASK_HEADER = re.compile(r"^## Task ([1-9][0-9]*): (.+)$")
FIELD_LINE = re.compile(r"^- ([A-Za-z][A-Za-z ]*):(?: (.*))?$")


def fail(message: str) -> None:
    raise ValueError(message)


def parse_dependencies(value: str, number: int) -> list[int]:
    value = value.strip()
    if value.lower() == "none":
        return []
    dependencies: list[int] = []
    for item in (part.strip() for part in value.split(",")):
        match = re.fullmatch(r"Tasks? ([1-9][0-9]*)(?:\s*[-–—]\s*([1-9][0-9]*))?", item, re.I)
        if not match:
            fail(f"Task {number}: malformed Dependencies {value!r}")
        start = int(match.group(1))
        end = int(match.group(2) or start)
        if end < start:
            fail(f"Task {number}: descending dependency range {item!r}")
        dependencies.extend(range(start, end + 1))
    if len(dependencies) != len(set(dependencies)):
        fail(f"Task {number}: duplicate dependency")
    return dependencies


def parse(text: str) -> tuple[dict[str, str], list[dict[str, object]]]:
    lines = text.splitlines()
    if not lines or lines[0] != "---":
        fail("front matter must start on the first line")
    try:
        end = lines.index("---", 1)
    except ValueError:
        fail("front matter is not terminated")

    metadata: dict[str, str] = {}
    for number, line in enumerate(lines[1:end], 2):
        match = re.fullmatch(r"([A-Za-z_][A-Za-z0-9_]*):[ \t]*(.*)", line)
        if not match:
            fail(f"line {number}: invalid front-matter entry")
        key, value = match.groups()
        if key in metadata:
            fail(f"duplicate front-matter key: {key}")
        if key not in KEYS:
            fail(f"unknown front-matter key: {key}")
        if not value:
            fail(f"empty front-matter value: {key}")
        metadata[key] = value
    missing = [key for key in KEYS if key not in metadata]
    if missing:
        fail(f"missing front-matter key(s): {', '.join(missing)}")
    if set(metadata) != set(KEYS):
        fail("front matter must contain exactly the seven required keys")
    if tuple(metadata) != KEYS:
        fail("front-matter keys must use the required order")

    headers: list[tuple[int, int, str]] = []
    for index, line in enumerate(lines[end + 1 :], end + 2):
        if line.startswith("## Task"):
            match = TASK_HEADER.fullmatch(line)
            if not match:
                fail(f"line {index}: malformed task heading")
            headers.append((index - 1, int(match.group(1)), match.group(2)))
    if not headers:
        fail("plan must contain at least one task")

    tasks: list[dict[str, object]] = []
    for position, (start, number, title) in enumerate(headers):
        if number != position + 1:
            fail("task numbering must be contiguous starting at 1")
        stop = headers[position + 1][0] if position + 1 < len(headers) else len(lines)
        found: dict[str, str] = {}
        task_lines = lines[start + 1 : stop]
        entries: list[tuple[int, str, str | None]] = []
        for offset, line in enumerate(task_lines):
            match = FIELD_LINE.fullmatch(line)
            if match:
                name, value = match.groups()
                entries.append((offset, name, value))
        unknown_fields = [name for _offset, name, _value in entries if name not in FIELDS]
        if unknown_fields:
            fail(f"Task {number}: unknown field(s): {', '.join(unknown_fields)}")
        if tuple(name for _offset, name, _value in entries) != FIELDS:
            fail(f"Task {number}: fields must appear exactly once in the required order")
        for entry_index, (offset, name, value) in enumerate(entries):
            if name in found:
                fail(f"Task {number}: duplicate {name}")
            next_offset = entries[entry_index + 1][0] if entry_index + 1 < len(entries) else len(task_lines)
            content = []
            if value and value.strip():
                content.append(value.strip())
            content.extend(
                line.strip() for line in task_lines[offset + 1 : next_offset]
                if line.strip()
            )
            if name == "Status" and (value is None or not value.strip()):
                fail(f"Task {number}: Status must be non-empty on its field line")
            if not content:
                fail(f"Task {number}: {name} must be non-empty")
            found[name] = "\n".join(content)
        missing_fields = [name for name in FIELDS if name not in found]
        if missing_fields:
            fail(f"Task {number}: missing {', '.join(missing_fields)}")
        if found["Status"] not in TASK_STATUSES:
            fail(f"Task {number}: invalid Status {found['Status']!r}")
        dependencies = parse_dependencies(found["Dependencies"], number)
        tasks.append({
            "number": number,
            "title": title,
            "fields": found,
            "dependencies": dependencies,
        })

    numbers = {int(task["number"]) for task in tasks}
    for task in tasks:
        number = int(task["number"])
        dependencies = list(task["dependencies"])
        unknown = sorted(set(dependencies) - numbers)
        if unknown:
            fail(f"Task {number}: unknown dependencies {unknown}")
        if number in dependencies:
            fail(f"Task {number}: cannot depend on itself")
        if any(dependency > number for dependency in dependencies):
            fail(f"Task {number}: dependencies must refer to earlier tasks")

    if tasks[-1]["title"] != FINAL_TITLE:
        fail(f"last task title must be exactly {FINAL_TITLE!r}")
    expected_final = numbers - {int(tasks[-1]["number"])}
    if set(tasks[-1]["dependencies"]) != expected_final:
        fail("final maintenance audit must depend on every prior task and no others")
    return metadata, tasks


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("planning", "complete", "current", "metadata"))
    parser.add_argument("path", nargs="?", default=".factory/artifacts/maintenance-plan.md")
    args = parser.parse_args()
    text = pathlib.Path(args.path).read_text(encoding="utf-8")
    metadata, tasks = parse(text)

    if args.mode == "planning":
        if metadata["status"] != "active":
            fail("planning mode requires front-matter status active")
        non_pending = [task["number"] for task in tasks if task["fields"]["Status"] != "pending"]
        if non_pending:
            fail(f"planning mode requires every task pending (non-pending: {non_pending})")
    elif args.mode == "complete":
        if metadata["status"] != "complete":
            fail("complete mode requires front-matter status complete")
        unfinished = [task["number"] for task in tasks if task["fields"]["Status"] != "complete"]
        if unfinished:
            fail(f"complete mode requires every task complete (unfinished: {unfinished})")
    elif metadata["status"] not in {"active", "complete"}:
        fail("front-matter status must be active or complete")

    if args.mode == "metadata":
        print(json.dumps(metadata, separators=(",", ":")))
    else:
        print(f"maintenance-plan: valid ({len(tasks)} tasks, status={metadata['status']})")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError) as exc:
        print(f"maintenance-plan: {exc}", file=sys.stderr)
        raise SystemExit(1)
