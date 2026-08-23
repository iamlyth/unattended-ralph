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
- a `decision` resolution (human identity + spec location) is out-of-band and
  non-automatable: unattended gates never accept agent-authored `human: true`,
  reviewer strings, or environment reviewer identity, so every decision
  resolution is rejected and the fact must stay open until a verifiable
  external attestation mechanism exists;
- in complete mode every receipt/artifact ref must exist as a Git blob at the
  declared evidence commit (working-tree presence is never enough) and receipt
  content is read from that blob, never from the working tree;
- cross-checks against the conformance sidecar (every fact referenced by
  exactly the requirements it lists; every open fact referenced by at least
  one blocked/partial row; no verified row may reference a fact) are enforced
  by `validate-conformance.py` through `cross_check_facts()`.

Usage:
  scripts/validate-blocked-facts.py [planning|complete] [path] [--root ROOT]
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FACT_ID = re.compile(r"^FACT-[0-9]{3,}$")
SHA = re.compile(r"^[0-9a-f]{40}$")
SAFE_PREFIXES = ("src", "tests", "scripts", "data", "docs", "cmake", "packaging", "third_party", ".github", ".forgejo", ".factory")
RECEIPT_SCHEMAS = {"ralph-audit-receipt/v1", "factory-runner-receipt/v1"}
LEDGER_DEFAULT = ROOT / ".factory/artifacts/blocked-facts.json"


def fail(message: str) -> None:
    raise SystemExit(f"blocked-facts: {message}")


def no_duplicate_keys(pairs: list) -> dict:
    """JSON object-pairs hook: reject duplicate object keys fail-closed.

    A duplicate key in the ledger (or in a receipt blob) silently overwrites
    its predecessor under a plain ``dict`` decode and can hide a drifted or
    tampered authority; every committed-data load uses this hook instead.
    """
    result: dict = {}
    for key, value in pairs:
        if key in result:
            fail(f"duplicate JSON object key: {key!r}")
        result[key] = value
    return result


def load_script_module(name: str, path: Path):
    """Load a dashed-name factory script as an importable module."""
    if not path.is_file() or path.is_symlink():
        fail(f"factory script is unavailable: {path}")
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        fail(f"cannot load factory script: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_pinned_git(root: Path):
    """Load the canonical pinned-Git authority (``.factory/loop/gitutil.py``)."""
    path = root / ".factory/loop/gitutil.py"
    try:
        return load_script_module("factory_gitutil", path)
    except Exception as exc:  # GitBoundaryError and import failures alike
        fail(f"pinned Git authority is unavailable: {exc}")


def trusted_git_env(module) -> dict:
    """Sanitized environment plus replace-ref disabling for trusted Git calls."""
    return module.sanitize_git_environment(
        {**os.environ, "GIT_NO_REPLACE_OBJECTS": "1"}
    )


def require_sha(commit: str, where: str) -> None:
    """Reject any non-full-SHA object argument before it reaches pinned Git."""
    if not isinstance(commit, str) or not SHA.fullmatch(commit):
        fail(f"{where} refuses a non-commit object argument: {commit!r}")


def load_ledger(path: Path) -> dict:
    if path.is_symlink() or not path.is_file():
        fail(f"blocked-facts ledger must be a regular tracked file: {path}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=no_duplicate_keys)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        fail(f"cannot parse blocked-facts ledger {path}: {exc}")
    if not isinstance(data, dict) or data.get("schema") != "ralph-blocked-facts/v1":
        fail(f"blocked-facts ledger schema must be ralph-blocked-facts/v1: {path}")
    facts = data.get("facts")
    if not isinstance(facts, list):
        fail("blocked-facts ledger must declare a facts array")
    return data


def reference_exists(root: Path, ref: str, commit: str, *, blob_only: bool) -> bool:
    # The ref must be a Git blob at the declared evidence commit in every
    # mode (planning included): stale or uncommitted working-tree presence
    # never certifies a resolution and cannot be proxied into evidence. The
    # commit argument is a full 40-hex SHA (never a ref), replace refs are
    # disabled, and the call is finite-bounded.
    require_sha(commit, "trusted Git blob lookup")
    git = load_pinned_git(root)
    result = git.git_run(
        ["-C", str(root), "cat-file", "-e", f"{commit}:{ref}"],
        env=trusted_git_env(git),
        timeout=git.GIT_TIMEOUT,
    )
    return result.returncode == 0


def validate_ref_safety(ref: str, where: str) -> None:
    """Reject any repository-relative path that can escape or bypass Git boundaries.

    Mirrors `scripts/validate-conformance.py`: absolute paths, `..`/`.`
    components, control characters (including NUL), backslash separators,
    empty components (`//`), prefix-boundary aliases (`.factoryx/…`), and
    first components outside the tracked ref namespaces are rejected.
    """
    if not isinstance(ref, str) or not ref:
        fail(f"{where} must be a non-empty string")
    if any(ord(ch) < 0x20 or ord(ch) == 0x7f for ch in ref):
        fail(f"{where} contains control characters: {ref!r}")
    if "\\" in ref:
        fail(f"{where} uses a backslash path separator: {ref!r}")
    if "//" in ref:
        fail(f"{where} contains an empty path component: {ref!r}")
    path = Path(ref)
    if path.is_absolute():
        fail(f"{where} is an absolute path: {ref!r}")
    parts = path.parts
    if not parts or any(part in ("", ".", "..") for part in parts):
        fail(f"{where} contains an unsafe path component: {ref!r}")
    if len(parts) > 1 and parts[0] not in SAFE_PREFIXES:
        fail(f"{where} first component must be a tracked refs prefix: {ref!r}")


def validate_ref(root: Path, fact_id: str, ref: str, commit: str, kind: str, *, blob_only: bool) -> None:
    validate_ref_safety(ref, f"fact {fact_id} {kind} ref")
    if ref.lower().endswith(".md"):
        fail(
            f"fact {fact_id} {kind} ref is documentation; documentation alone "
            f"cannot resolve a normative requirement: {ref}"
        )
    if not reference_exists(root, ref, commit, blob_only=blob_only):
        fail(f"fact {fact_id} {kind} ref does not exist at commit {commit[:12]}: {ref}")


def blob_json(root: Path, fact_id: str, ref: str, commit: str) -> dict:
    """Read a receipt's content from the declared Git blob at the evidence commit.

    Complete mode validates the exact committed content, never the working tree:
    a tampered or stale working-tree copy cannot certify a resolution.
    """
    require_sha(commit, "trusted Git blob read")
    git = load_pinned_git(root)
    result = git.git_run(
        ["-C", str(root), "show", f"{commit}:{ref}"],
        env=trusted_git_env(git),
        timeout=git.GIT_TIMEOUT,
    )
    if result.returncode:
        fail(f"fact {fact_id} receipt {ref} is not a Git blob at commit {commit[:12]}")
    try:
        data = json.loads(result.stdout, object_pairs_hook=no_duplicate_keys)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        fail(f"fact {fact_id} receipt {ref} is invalid JSON at commit {commit[:12]}: {exc}")
    if not isinstance(data, dict):
        fail(f"fact {fact_id} receipt {ref} must be an object at commit {commit[:12]}")
    return data


def validate_receipt_ref(root: Path, fact_id: str, ref: str, commit: str, *, blob_only: bool) -> None:
    validate_ref(root, fact_id, ref, commit, "receipt", blob_only=blob_only)
    # Receipt content is always read from the declared Git blob at the
    # evidence commit, never from the working tree: a tampered or stale
    # working-tree copy can neither certify nor break a resolution.
    data = blob_json(root, fact_id, ref, commit)
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


def validate_fact(root: Path, fact: dict, index: int, *, blob_only: bool = False) -> None:
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
    resolution_type = resolution["type"]
    if resolution_type == "decision":
        fail(
            f"fact {fact_id} uses a decision resolution: human decisions and golden "
            f"approval are out-of-band and non-automatable. Unattended gates never "
            f"accept agent-authored human:true, reviewer strings, or environment "
            f"reviewer identity. Keep the fact open as a finding until a verifiable "
            f"external attestation mechanism exists."
        )
    if resolution_type not in {"receipt", "artifact"}:
        fail(f"fact {fact_id} resolution type is invalid: {resolution_type!r}")
    if resolution_type in {"receipt", "artifact"}:
        if set(resolution) != resolution_expected:
            fail(f"fact {fact_id} {resolution_type} resolution fields do not match the schema")
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
    if not refs:
        fail(f"fact {fact_id} {resolution_type} resolution requires exact refs")
    for ref in refs:
        if resolution_type == "receipt":
            validate_receipt_ref(root, fact_id, ref, commit, blob_only=blob_only)
        else:
            validate_ref(root, fact_id, ref, commit, "artifact", blob_only=blob_only)


def validate_ledger(root: Path, data: dict, *, blob_only: bool = False) -> list[dict]:
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
        validate_fact(root, fact, index, blob_only=blob_only)
    return facts


def check_complete(root: Path, data: dict) -> None:
    """Completion requires every fact resolved with a validated resolution."""
    for fact in data["facts"]:
        if fact["status"] != "resolved":
            fail(f"completion rejected while fact {fact['id']} is open")
        validate_fact(root, fact, 0, blob_only=True)


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
    # Complete mode validates receipt/artifact refs against the declared Git
    # blobs only, never the working tree: a tampered working-tree copy cannot
    # break a clean committed receipt and cannot certify one either.
    facts = validate_ledger(root, data, blob_only=(args.mode == "complete"))
    if args.mode == "complete":
        check_complete(root, data)
    print(f"blocked-facts: {args.mode} valid ({len(facts)} facts)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
