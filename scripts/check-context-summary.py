#!/usr/bin/env python3
"""Validate the Ralph context summary against the plan, sidecar, and facts.

`scripts/ralph-context-summary.py` produces `.factory/artifacts/context-summary.md`
for fresh implementation contexts. This checker proves the summary is
contamination-free and truthful:

- it contains no completion prose: no reserved lifecycle tokens, no
  `complete`/`completed`/`completion` claims, no `status: complete` front
  matter, no "definition of done satisfied" phrasing;
- open tasks, unresolved facts, blocked/partial rows, and exact receipt refs
  match the plan, the conformance sidecar, and the blocked-facts ledger
  exactly (nothing stale, nothing invented);
- `--contamination-only` performs only the prose/token checks (used by the
  checkpoint hook, where the plan may legitimately advance during a run).

Usage:
  scripts/check-context-summary.py [--summary PATH] [--root ROOT] [--contamination-only]
"""

from __future__ import annotations

import argparse
import importlib.util
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SUMMARY_DEFAULT = ".factory/artifacts/context-summary.md"

FORBIDDEN_TOKENS = (
    "LOOP_COMPLETE",
    "PLAN_COMPLETE",
    "AUDIT_COMPLETE",
    "MAINTENANCE_COMPLETE",
    "MAINTENANCE_PLAN_COMPLETE",
)
FORBIDDEN_PATTERNS = (
    r"\bstatus\s*[:=]+\s*complete\b",
    r"\bcomplete\b",
    r"\bcompleted\b",
    r"\bcompletion\b",
    r"\bdefinition of done (satisfied|met|accepted)\b",
    r"\ball requirements? (verified|satisfied|met)\b",
    r"\ball tasks? (complete|done|finished)\b",
    r"\bevery task complete\b",
    r"\bcompletion accepted\b",
    r"\bcompletion claim\b",
    r"\bno more gaps\b",
)

REQUIRED_SECTIONS = (
    "Active task",
    "Open tasks",
    "Unresolved facts",
    "Blocked/partial conformance rows",
    "Exact receipt refs",
)


def fail(message: str) -> None:
    raise SystemExit(f"context-summary: {message}")


def load_script_module(name: str, path: Path):
    if path.is_symlink() or not path.is_file():
        fail(f"factory script is unavailable: {path}")
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        fail(f"cannot load factory script: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def check_contamination(text: str) -> None:
    for token in FORBIDDEN_TOKENS:
        if token in text:
            fail(f"summary contains the reserved lifecycle token {token}")
    for pattern in FORBIDDEN_PATTERNS:
        match = re.search(pattern, text, re.I)
        if match:
            fail(f"summary contains completion prose: {match.group(0)!r}")


def parse_sections(text: str) -> dict[str, str]:
    sections: dict[str, str] = {}
    matches = list(re.finditer(r"^## ([^\n]+)\s*$\n", text, re.M))
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        sections[match.group(1).strip()] = text[match.end():end]
    for name in REQUIRED_SECTIONS:
        if name not in sections:
            fail(f"summary is missing the `{name}` section")
    return sections


def parse_open_tasks(lines: str) -> set[tuple[int, str, str]]:
    result: set[tuple[int, str, str]] = set()
    for line in lines.splitlines():
        if not line.strip():
            continue
        match = re.fullmatch(r"- Task (\d+) \(([a-z_]+)\): (.+)", line.strip())
        if not match:
            fail(f"malformed open-task line: {line}")
        result.add((int(match.group(1)), match.group(2), match.group(3)))
    return result


def parse_facts(lines: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for line in lines.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped in {"- none"}:
            continue
        match = re.fullmatch(r"- (FACT-\d{3,}) \(open\): (.+)", stripped)
        if not match:
            fail(f"malformed unresolved-fact line: {line}")
        result[match.group(1)] = match.group(2)
    return result


def parse_rows(lines: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for line in lines.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped == "- none":
            continue
        match = re.fullmatch(r"- ([A-Z][A-Z0-9]*(?:[-_][A-Z0-9]+)+) \((.*?)\): (.*)", stripped)
        if not match:
            fail(f"malformed blocked/partial row line: {line}")
        result[match.group(1)] = stripped
    return result


def parse_receipts(lines: str) -> list[str]:
    result: list[str] = []
    for line in lines.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped == "- none":
            continue
        if not stripped.startswith("- "):
            fail(f"malformed receipt-ref line: {line}")
        result.append(stripped[2:])
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", default=str(ROOT))
    parser.add_argument("--contamination-only", action="store_true")
    parser.add_argument("summary", nargs="?", default=None)
    args = parser.parse_args()
    root = Path(args.root).resolve()
    summary_path = root / (args.summary or SUMMARY_DEFAULT)
    if args.summary and Path(args.summary).is_absolute():
        summary_path = Path(args.summary)
    if summary_path.is_symlink() or not summary_path.is_file():
        fail(f"context summary is missing: {summary_path}")
    text = summary_path.read_text(encoding="utf-8")
    check_contamination(text)
    sections = parse_sections(text)
    if args.contamination_only:
        print("context-summary: contamination check passed")
        return 0

    summary = load_script_module("ralph_context_summary", root / "scripts/ralph-context-summary.py")
    plan_path = root / ".factory/artifacts/implementation-plan.md"
    if not plan_path.is_file() or plan_path.is_symlink():
        fail(f"plan is missing: {plan_path}")
    plan_text = plan_path.read_text(encoding="utf-8")
    meta, tasks = summary.parse_plan(plan_path)
    open_tasks = [task for task in tasks if task["status"] != "complete"]
    expected_tasks = {
        (task["number"], task["status"], task["title"]) for task in open_tasks
    }
    actual_tasks = parse_open_tasks(sections["Open tasks"])
    if actual_tasks != expected_tasks:
        fail(
            "open-task set drifts from the plan"
            + (f"; unexpected: {sorted(actual_tasks - expected_tasks)}" if actual_tasks - expected_tasks else "")
            + (f"; missing: {sorted(expected_tasks - actual_tasks)}" if expected_tasks - actual_tasks else "")
        )

    expected_active = None
    for task in sorted(open_tasks, key=lambda item: item["number"]):
        if task["status"] in {"pending", "in_progress"}:
            deps = summary.parse_dependency_numbers(task["dependencies"])
            open_numbers = {item["number"] for item in open_tasks}
            if not deps or deps.isdisjoint(open_numbers):
                expected_active = task
                break
    active_lines = [line for line in sections["Active task"].splitlines() if line.strip()]
    if expected_active is None:
        if active_lines != ["- none (no ready task yet)"]:
            fail("active-task line does not match the plan (expected none)")
    else:
        expected_line = f"- Task {expected_active['number']} ({expected_active['status']}): {expected_active['title']}"
        if active_lines != [expected_line]:
            fail("active-task line does not match the first ready task")

    facts = summary.load_facts(root)
    expected_facts = {fact["id"]: fact["title"] for fact in facts}
    actual_facts = parse_facts(sections["Unresolved facts"])
    if actual_facts != expected_facts:
        fail("unresolved-fact set drifts from the blocked-facts ledger")

    sidecar = summary.load_sidecar(root)
    rows = summary.matrix_rows(plan_text)
    non_verified = {row[0]: row for row in rows if row[1] != "verified"}
    fact_refs_by_row: dict[str, list[str]] = {}
    if sidecar is not None:
        for requirement in sidecar.get("requirements", []):
            if isinstance(requirement, dict):
                fact_refs_by_row[requirement.get("id", "")] = sorted(requirement.get("fact_refs", []))
    expected_row_lines: set[str] = set()
    for row_id, (row_id2, classification, task_cell) in sorted(non_verified.items()):
        assert row_id2 == row_id
        fact_refs = ",".join(fact_refs_by_row.get(row_id, []))
        expected_row_lines.add(
            f"- {row_id} ({classification}): tasks={task_cell}"
            + (f"; facts={fact_refs}" if fact_refs else "")
        )
    actual_rows = parse_rows(sections["Blocked/partial conformance rows"])
    if set(actual_rows.values()) != expected_row_lines or set(actual_rows) != set(non_verified):
        fail("blocked/partial row set drifts from the plan matrix and sidecar")

    expected_receipts: set[str] = set()
    if sidecar is not None:
        for requirement in sidecar.get("requirements", []):
            if isinstance(requirement, dict) and requirement.get("classification") in {"blocked", "partial"}:
                expected_receipts.update(requirement.get("receipts", []))
    actual_receipts = set(parse_receipts(sections["Exact receipt refs"]))
    if actual_receipts != expected_receipts:
        fail(
            "exact receipt refs drift from the blocked/partial sidecar rows"
            + (f"; unexpected: {sorted(actual_receipts - expected_receipts)}" if actual_receipts - expected_receipts else "")
            + (f"; missing: {sorted(expected_receipts - actual_receipts)}" if expected_receipts - actual_receipts else "")
        )

    print("context-summary: valid (active task, unresolved facts, and exact receipt refs)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
