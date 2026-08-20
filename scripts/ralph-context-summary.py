#!/usr/bin/env python3
"""Generate the durable Ralph context summary for a fresh implementation context.

A fresh Ralph implementation context receives ONLY:
- the active task (the first pending/in-progress task whose dependencies are
  complete) plus the other open (non-complete) tasks;
- unresolved (open) blocked-facts entries;
- the blocked/partial conformance rows and the exact receipt refs bound to
  them.

The summary deliberately excludes every prior completion claim, resolution
prose, campaign/loop runtime state, and any `status: complete` front matter.
`scripts/check-context-summary.py` validates the summary against the plan,
sidecar, and facts ledger and rejects contamination (completion prose, stale
task/fact claims, or mismatched receipt refs).

Usage:
  scripts/ralph-context-summary.py [--root ROOT] [--out PATH]
  (writes .factory/artifacts/context-summary.md by default)
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MATRIX_ID = re.compile(r"^[A-Z][A-Z0-9]*(?:[-_][A-Z0-9]+)+$")
OUT_DEFAULT = ".factory/artifacts/context-summary.md"


def fail(message: str) -> None:
    raise SystemExit(f"ralph-context-summary: {message}")


def parse_plan(path: Path) -> tuple[dict[str, str], list[dict]]:
    text = path.read_text(encoding="utf-8")
    front = re.match(r"\A---\n(.*?)\n---", text, re.S)
    if not front:
        fail("plan has no front matter")
    meta: dict[str, str] = {}
    for line in front.group(1).splitlines():
        if ": " in line:
            key, value = line.split(": ", 1)
            meta[key.strip()] = value.strip()
    tasks: list[dict] = []
    for match in re.finditer(r"^## Task\s+(\d+):\s*(.+?)\s*$", text, re.M):
        number = int(match.group(1))
        body_end = text.find("## Task", match.end())
        body = text[match.end(): body_end if body_end != -1 else len(text)]
        status_match = re.search(r"^- Status: (\S+)\s*$", body, re.M)
        deps_match = re.search(r"^- Dependencies: (.+?)\s*$", body, re.M)
        tasks.append({
            "number": number,
            "title": match.group(2).strip(),
            "status": status_match.group(1) if status_match else "unknown",
            "dependencies": deps_match.group(1).strip() if deps_match else "unknown",
        })
    return meta, tasks


def parse_dependency_numbers(value: str) -> set[int]:
    if not value or value.lower() == "none":
        return set()
    numbers: set[int] = set()
    for item in (part.strip() for part in value.split(",")):
        match = re.fullmatch(r"Tasks?\s+(\d+)(?:\s*[-–—]\s*(\d+))?", item, re.I)
        if match:
            start = int(match.group(1))
            end = int(match.group(2) or start)
            numbers.update(range(start, end + 1))
    return numbers


def matrix_rows(text: str) -> list[tuple[str, str, str]]:
    match = re.search(r"^## Specification conformance matrix\s*$\n(.*?)(?=^##\s|\Z)", text, re.M | re.S)
    if not match:
        return []
    rows: list[tuple[str, str, str]] = []
    for line in match.group(1).splitlines():
        line = line.strip()
        if not line.startswith("|"):
            continue
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if len(cells) != 5 or cells[0] == "ID":
            continue
        if all(re.fullmatch(r":?-{3,}:?", cell) for cell in cells):
            continue
        if MATRIX_ID.fullmatch(cells[0]):
            rows.append((cells[0], cells[2], cells[4]))
    return rows


def load_sidecar(root: Path) -> dict | None:
    path = root / ".factory/artifacts/conformance.json"
    if not path.is_file() or path.is_symlink():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        fail(f"cannot parse conformance sidecar {path}: {exc}")


def load_facts(root: Path) -> list[dict]:
    path = root / ".factory/artifacts/blocked-facts.json"
    if not path.is_file() or path.is_symlink():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        fail(f"cannot parse facts ledger {path}: {exc}")
    return [
        fact for fact in data.get("facts", [])
        if isinstance(fact, dict) and fact.get("status") == "open"
    ]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", default=str(ROOT))
    parser.add_argument("--out", default=None)
    args = parser.parse_args()
    root = Path(args.root).resolve()
    out = root / (args.out or OUT_DEFAULT)
    if args.out and Path(args.out).is_absolute():
        out = Path(args.out)

    plan_path = root / ".factory/artifacts/implementation-plan.md"
    if not plan_path.is_file() or plan_path.is_symlink():
        fail(f"plan is missing: {plan_path}")
    plan_text = plan_path.read_text(encoding="utf-8")
    meta, tasks = parse_plan(plan_path)

    open_tasks = [task for task in tasks if task["status"] != "complete"]
    open_numbers = {task["number"] for task in open_tasks}
    active = None
    for task in sorted(open_tasks, key=lambda item: item["number"]):
        if task["status"] not in {"pending", "in_progress"}:
            continue
        deps = parse_dependency_numbers(task["dependencies"])
        if not deps or deps.isdisjoint(open_numbers):
            active = task
            break

    rows = matrix_rows(plan_text)
    non_verified = [row for row in rows if row[1] != "verified"]
    facts = load_facts(root)
    sidecar = load_sidecar(root)

    fact_refs_by_row: dict[str, list[str]] = {}
    if sidecar is not None:
        for requirement in sidecar.get("requirements", []):
            if isinstance(requirement, dict):
                fact_refs_by_row[requirement.get("id", "")] = list(requirement.get("fact_refs", []))

    receipt_refs: set[str] = set()
    if sidecar is not None:
        for requirement in sidecar.get("requirements", []):
            if isinstance(requirement, dict) and requirement.get("classification") in {"blocked", "partial"}:
                receipt_refs.update(requirement.get("receipts", []))

    lines = [
        "# Ralph Context Summary",
        "",
        "Generated for a fresh implementation context only; this file is not",
        "release evidence and contains no satisfied/done claims.",
        "",
        "## Active task",
        f"- Task {active['number']} ({active['status']}): {active['title']}"
        if active else "- none (no ready task yet)",
        "",
        "## Open tasks",
    ]
    for task in sorted(open_tasks, key=lambda item: item["number"]):
        lines.append(f"- Task {task['number']} ({task['status']}): {task['title']}")
    lines.append("")
    lines.append("## Unresolved facts")
    if facts:
        for fact in sorted(facts, key=lambda item: item.get("id", "")):
            lines.append(f"- {fact['id']} (open): {fact['title']}")
    else:
        lines.append("- none")
    lines.append("")
    lines.append("## Blocked/partial conformance rows")
    if non_verified:
        for row_id, classification, task_cell in sorted(non_verified):
            fact_refs = ",".join(sorted(fact_refs_by_row.get(row_id, [])))
            lines.append(
                f"- {row_id} ({classification}): tasks={task_cell}"
                + (f"; facts={fact_refs}" if fact_refs else "")
            )
    else:
        lines.append("- none")
    lines.append("")
    lines.append("## Exact receipt refs")
    if receipt_refs:
        for ref in sorted(receipt_refs):
            lines.append(f"- {ref}")
    else:
        lines.append("- none")
    lines.append("")

    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines), encoding="utf-8")
    print(f"ralph-context-summary: wrote {out.relative_to(root)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
