#!/usr/bin/env python3
"""Validate an independent campaign audit and its immutable Git binding."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import re
import subprocess
import tomllib

ROOT = Path(__file__).resolve().parent.parent
os.environ["GIT_NO_REPLACE_OBJECTS"] = "1"
SHA = r"[0-9a-f]{40}"


def fail(message: str) -> None:
    raise SystemExit(f"campaign-audit: {message}")


def git(*args: str) -> str:
    return subprocess.check_output(["git", *args], cwd=ROOT, text=True).strip()


def parse(path: Path) -> tuple[dict[str, str], str]:
    if path.is_symlink() or not path.is_file():
        fail("report must be a regular file")
    text = path.read_text(encoding="utf-8")
    match = re.match(r"\A---\n(.*?)\n---\n(.*)\Z", text, re.S)
    if not match:
        fail("missing front matter")
    meta: dict[str, str] = {}
    for line in match.group(1).splitlines():
        if ": " not in line:
            fail(f"malformed metadata line: {line}")
        key, value = line.split(": ", 1)
        if key in meta:
            fail(f"duplicate metadata key: {key}")
        meta[key] = value
    expected = ["schema", "round", "audit_base_commit", "plan_commit", "plan_blob", "environment_blob", "result"]
    if list(meta) != expected:
        fail(f"metadata keys/order must be exactly {expected}")
    return meta, match.group(2)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("metadata", "complete"))
    parser.add_argument("path", nargs="?", default="CAMPAIGN_AUDIT.md")
    parser.add_argument("--expected-round", type=int)
    parser.add_argument("--expected-base")
    args = parser.parse_args()
    meta, body = parse(ROOT / args.path)
    if meta["schema"] != "ralph-campaign-audit/v1":
        fail("unsupported schema")
    try:
        round_number = int(meta["round"])
    except ValueError:
        fail("round must be an integer")
    if round_number < 1:
        fail("round must be positive")
    for key in ("audit_base_commit", "plan_commit", "plan_blob", "environment_blob"):
        if not re.fullmatch(SHA, meta[key]):
            fail(f"{key} must be a full object ID")
    if git("replace", "-l"):
        fail("Git replacement objects are forbidden during an audit")
    git("cat-file", "-e", f"{meta['audit_base_commit']}^{{commit}}")
    if args.mode == "complete":
        if args.expected_round is None or not args.expected_base:
            fail("completed validation requires supervisor-owned expected round and base")
        if round_number != args.expected_round or meta["audit_base_commit"] != args.expected_base:
            fail("report round/base does not match the supervisor-owned audit binding")
        if not re.fullmatch(SHA, args.expected_base):
            fail("expected audit base is invalid")
    if subprocess.run(
        ["git", "merge-base", "--is-ancestor", meta["audit_base_commit"], "HEAD"],
        cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    ).returncode:
        fail("audit base is not an ancestor of HEAD")
    if git("rev-parse", f"{meta['audit_base_commit']}:IMPLEMENTATION_PLAN.md") != meta["plan_blob"]:
        fail("plan blob does not match audit base")
    if git("log", "-1", "--format=%H", meta["audit_base_commit"], "--", "IMPLEMENTATION_PLAN.md") != meta["plan_commit"]:
        fail("plan commit does not match audit base")
    if git("rev-parse", f"{meta['audit_base_commit']}:factory-environment.toml") != meta["environment_blob"]:
        fail("environment blob does not match audit base")
    changed: set[str] = set(filter(None, git("diff", "--name-only", f"{meta['audit_base_commit']}..HEAD").splitlines()))
    commits = git("rev-list", f"{meta['audit_base_commit']}..HEAD").splitlines()
    if git("rev-list", "--min-parents=2", f"{meta['audit_base_commit']}..HEAD"):
        fail("merge commits are forbidden during an independent audit")
    for commit in commits:
        changed.update(filter(None, git("diff-tree", "--no-commit-id", "--name-only", "-r", "--root", commit).splitlines()))
    forbidden = changed - {"CAMPAIGN_AUDIT.md", ".ralph/agent/scratchpad.md"}
    if forbidden:
        fail(f"audit commits changed forbidden paths: {sorted(forbidden)}")
    if args.mode == "metadata":
        print(f"round={round_number} result={meta['result']}")
        return 0
    if meta["result"] not in {"pass", "findings"}:
        fail("completed result must be pass or findings")
    evidence = re.search(r"^## Evidence reviewed\s*\n(.*?)(?=^## |\Z)", body, re.M | re.S)
    if not evidence:
        fail("missing Evidence reviewed section")
    evidence_rules = {
        "Specification": r"`docs/[^`]+`",
        "Production paths": r"`[^`]*[/][^`]+`",
        "Executable evidence": r"`[^`]+`.*\b(PASS|FAIL|BLOCKED)\b",
        "Environment limits": r"`factory-environment\.toml`",
    }
    for field, detail_pattern in evidence_rules.items():
        match = re.search(rf"^- {field}:\s+(.+)$", evidence.group(1), re.M)
        if not match or not re.search(detail_pattern, match.group(1)):
            fail(f"Evidence reviewed requires a concrete '- {field}:' reference/result")
    findings = re.findall(r"^## Finding ([1-9][0-9]*):\s+(.+)$", body, re.M)
    if meta["result"] == "pass":
        if findings or not re.search(r"^## Findings\s*\n\s*None\.\s*$", body, re.M):
            fail("pass requires an explicit Findings section containing None.")
        with (ROOT / "factory.toml").open("rb") as stream:
            required_capabilities = tomllib.load(stream).get("campaign", {}).get("required_capabilities", [])
        with (ROOT / "factory-environment.toml").open("rb") as stream:
            environment = tomllib.load(stream)
        if not isinstance(required_capabilities, list) or not all(isinstance(item, str) and item for item in required_capabilities):
            fail("campaign.required_capabilities must be a string array")
        declared = {
            capability
            for entry in environment.get("tools", []) + environment.get("runners", [])
            for capability in entry.get("capabilities", [])
        }
        missing = sorted(set(required_capabilities) - declared)
        if missing:
            fail(f"pass lacks declared required environment capabilities: {missing}")
    else:
        if not findings:
            fail("findings result requires at least one numbered Finding section")
        numbers = [int(number) for number, _ in findings]
        if numbers != list(range(1, len(numbers) + 1)):
            fail("findings must be uniquely and consecutively numbered")
        for number, _ in findings:
            block = re.search(rf"^## Finding {number}:.*?(?=^## Finding |\Z)", body, re.M | re.S)
            assert block
            for field in ("Requirement", "Production evidence", "Required remediation"):
                if not re.search(rf"^- {field}:\s+\S", block.group(0), re.M):
                    fail(f"Finding {number} requires '- {field}:'")
    print(f"campaign-audit: round {round_number} accepted with result {meta['result']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
