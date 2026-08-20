#!/usr/bin/env python3
"""Validate the append-only blocked-facts ledger.

`.factory/artifacts/blocked-facts.json` records every fact whose required
conformance evidence is unavailable (undeclared/unevidenced capability,
missing real system service, missing hardware target, open product defect, or
pending human decision). Blocked/partial conformance rows must reference an
open fact (`validate-conformance.py` enforces the cross-reference).

Rules:
- the ledger is append-only: facts are never deleted, reordered, or renumbered
  (IDs are unique and strictly ascending in array order);
- an `open` fact requires explicit blocking evidence and no resolution;
- a `resolved` fact requires a validated resolution;
- a `receipt` resolution requires exact machine-receipt/runner-manifest JSON at
  the evidence commit with a clean exit;
- an `artifact` resolution requires an exact non-documentation artifact at the
  evidence commit (`.md` documentation alone can never resolve a normative
  requirement);
- a `decision` resolution requires an explicit human identity and a spec
  location that permits the decision (e.g. SPEC §11.2.6 human-approved
  deferral);
- cross-checks against the conformance sidecar (every fact referenced by
  exactly the requirements it lists; every open fact referenced by at least
  one blocked/partial row; no verified row may reference a fact) are enforced
  by `validate-conformance.py` through `cross_check_facts()`.

Usage:
  scripts/validate-blocked-facts.py [planning|complete] [path] [--root ROOT]
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FACT_ID = re.compile(r"^FACT-[0-9]{3,}$")
SHA = re.compile(r"^[0-9a-f]{40}$")
SAFE_PREFIXES = (".factory", "src", "tests", "scripts", "data", "docs", "cmake", "packaging", "third_party", ".github", ".forgejo")
RECEIPT_SCHEMAS = {"ralph-audit-receipt/v1", "factory-runner-receipt/v1"}
LEDGER_DEFAULT = ROOT / ".factory/artifacts/blocked-facts.json"


def fail(message: str) -> None:
    raise SystemExit(f"blocked-facts: {message}")


def load_ledger(path: Path) -> dict:
    if path.is_symlink() or not path.is_file():
        fail(f"blocked-facts ledger must be a regular tracked file: {path}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        fail(f"cannot parse blocked-facts ledger {path}: {exc}")
    if not isinstance(data, dict) or data.get("schema") != "ralph-blocked-facts/v1":
        fail(f"blocked-facts ledger schema must be ralph-blocked-facts/v1: {path}")
    facts = data.get("facts")
    if not isinstance(facts, list):
        fail("blocked-facts ledger must declare a facts array")
    return data


def reference_exists(root: Path, ref: str, commit: str) -> bool:
    path = root / ref
    if path.exists() and not path.is_symlink():
        return True
    result = subprocess.run(
        ["git", "cat-file", "-e", f"{commit}:{ref}"],
        cwd=root, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    return result.returncode == 0


def regular_json(path: Path) -> dict:
    if path.is_symlink() or not path.is_file():
        fail(f"unsafe or missing evidence file: {path}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        fail(f"invalid evidence file {path}: {exc}")
    if not isinstance(data, dict):
        fail(f"evidence must be an object: {path}")
    return data


def validate_ref(root: Path, fact_id: str, ref: str, commit: str, kind: str) -> None:
    if not ref or Path(ref).is_absolute() or ".." in Path(ref).parts:
        fail(f"fact {fact_id} {kind} ref is unsafe: {ref}")
    if not ref.startswith(SAFE_PREFIXES) and "/" in ref:
        fail(f"fact {fact_id} {kind} ref must start with a tracked prefix: {ref}")
    if ref.lower().endswith(".md"):
        fail(
            f"fact {fact_id} {kind} ref is documentation; documentation alone "
            f"cannot resolve a normative requirement: {ref}"
        )
    if not reference_exists(root, ref, commit):
        fail(f"fact {fact_id} {kind} ref does not exist at commit {commit[:12]}: {ref}")


def validate_receipt_ref(root: Path, fact_id: str, ref: str, commit: str) -> None:
    validate_ref(root, fact_id, ref, commit, "receipt")
    data = regular_json(root / ref)
    schema = data.get("schema")
    if schema not in RECEIPT_SCHEMAS:
        fail(f"fact {fact_id} receipt {ref} has an unknown schema {schema!r}")
    if schema == "factory-runner-receipt/v1":
        if data.get("result") != "pass" or data.get("exit_code") != 0:
            fail(f"fact {fact_id} receipt {ref} does not prove a clean pass")
    else:
        if data.get("exit_code") != 0:
            fail(f"fact {fact_id} receipt {ref} has a nonzero exit")
        if not isinstance(data.get("argv"), list) or not data["argv"]:
            fail(f"fact {fact_id} receipt {ref} has no argv binding")


def validate_fact(root: Path, fact: dict, index: int) -> None:
    if not isinstance(fact, dict):
        fail(f"facts[{index}] must be an object")
    expected = {
        "id", "title", "status", "capabilities", "requirements",
        "blocking_evidence", "resolution",
    }
    if set(fact) != expected:
        fail(f"facts[{index}] fields do not match the blocked-facts schema")
    fact_id = fact["id"]
    if not isinstance(fact_id, str) or not FACT_ID.fullmatch(fact_id):
        fail(f"facts[{index}].id is invalid: {fact_id}")
    title = fact["title"]
    if not isinstance(title, str) or not title.strip():
        fail(f"fact {fact_id} requires a non-empty title")
    status = fact["status"]
    if status not in {"open", "resolved"}:
        fail(f"fact {fact_id} has invalid status {status!r}")
    capabilities = fact["capabilities"]
    if not isinstance(capabilities, list) or not all(
        isinstance(item, str) and item for item in capabilities
    ):
        fail(f"fact {fact_id} capabilities must be a string array")
    requirements = fact["requirements"]
    if not isinstance(requirements, list) or not all(
        isinstance(item, str) and item for item in requirements
    ):
        fail(f"fact {fact_id} requirements must be a string array")
    blocking_evidence = fact["blocking_evidence"]
    if not isinstance(blocking_evidence, str):
        fail(f"fact {fact_id} blocking_evidence must be a string")
    resolution = fact["resolution"]
    if status == "open":
        if resolution is not None:
            fail(f"fact {fact_id} is open but has a resolution")
        if not blocking_evidence.strip():
            fail(f"open fact {fact_id} requires explicit blocking evidence")
        return
    # status == resolved
    if not isinstance(resolution, dict):
        fail(f"resolved fact {fact_id} requires a resolution object")
    resolution_expected = {"type", "refs", "evidence_commit", "resolved_at", "reason"}
    resolution_optional = {"reviewer", "human", "spec_permitted"}
    resolution_type = resolution["type"]
    if resolution_type not in {"receipt", "artifact", "decision"}:
        fail(f"fact {fact_id} resolution type is invalid: {resolution_type!r}")
    if resolution_type in {"receipt", "artifact"}:
        if set(resolution) != resolution_expected:
            fail(f"fact {fact_id} {resolution_type} resolution fields do not match the schema")
    else:
        if set(resolution) != resolution_expected | resolution_optional:
            fail(f"fact {fact_id} decision resolution fields do not match the schema")
    commit = resolution["evidence_commit"]
    if not isinstance(commit, str) or not SHA.fullmatch(commit):
        fail(f"fact {fact_id} resolution evidence_commit must be a 40-character commit")
    resolved_at = resolution["resolved_at"]
    if not isinstance(resolved_at, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", resolved_at):
        fail(f"fact {fact_id} resolution resolved_at must be an ISO-8601 date")
    reason = resolution["reason"]
    if not isinstance(reason, str) or not reason.strip():
        fail(f"fact {fact_id} resolution requires a reason")
    refs = resolution["refs"]
    if not isinstance(refs, list) or not all(isinstance(item, str) for item in refs):
        fail(f"fact {fact_id} resolution refs must be a string array")
    if resolution_type in {"receipt", "artifact"}:
        if not refs:
            fail(f"fact {fact_id} {resolution_type} resolution requires exact refs")
        for ref in refs:
            if resolution_type == "receipt":
                validate_receipt_ref(root, fact_id, ref, commit)
            else:
                validate_ref(root, fact_id, ref, commit, "artifact")
    else:
        if refs:
            fail(f"fact {fact_id} decision resolution must not carry new refs")
        reviewer = resolution.get("reviewer")
        if not isinstance(reviewer, str) or not reviewer.strip():
            fail(f"fact {fact_id} decision resolution requires a human reviewer identity")
        if resolution.get("human") is not True:
            fail(f"fact {fact_id} decision resolution requires human: true")
        spec_permitted = resolution.get("spec_permitted")
        if not isinstance(spec_permitted, str) or not (
            re.search(r"[§][0-9]", spec_permitted) or "SPEC.md" in spec_permitted
        ):
            fail(
                f"fact {fact_id} decision resolution requires a spec-permitted "
                f"location (a §section or docs/SPEC.md)"
            )


def validate_ledger(root: Path, data: dict) -> list[dict]:
    facts = data["facts"]
    if any(not isinstance(fact, dict) for fact in facts):
        fail("every ledger entry must be an object")
    ids = [fact["id"] for fact in facts]
    if len(ids) != len(set(ids)):
        duplicates = sorted({item for item in ids if ids.count(item) > 1})
        fail(f"fact IDs must be unique; duplicates: {duplicates}")
    previous = 0
    for fact_id in ids:
        if not FACT_ID.fullmatch(fact_id):
            fail(f"invalid fact ID: {fact_id}")
        number = int(fact_id.split("-", 1)[1])
        if number <= previous:
            fail(
                "fact ledger is append-only: IDs must be strictly ascending in "
                f"array order (found {fact_id} after FACT-{previous:03d})"
            )
        previous = number
    for index, fact in enumerate(facts):
        validate_fact(root, fact, index)
    return facts


def check_complete(root: Path, data: dict) -> None:
    """Completion requires every fact resolved with a validated resolution."""
    for fact in data["facts"]:
        if fact["status"] != "resolved":
            fail(f"completion rejected while fact {fact['id']} is open")
        validate_fact(root, fact, 0)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "mode", nargs="?", default="planning", choices=("planning", "complete")
    )
    parser.add_argument("path", nargs="?", default=str(LEDGER_DEFAULT))
    parser.add_argument("--root", default=str(ROOT))
    args = parser.parse_args()
    root = Path(args.root).resolve()
    path = root / args.path if not Path(args.path).is_absolute() else Path(args.path)
    data = load_ledger(path)
    facts = validate_ledger(root, data)
    if args.mode == "complete":
        check_complete(root, data)
    print(f"blocked-facts: {args.mode} valid ({len(facts)} facts)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
