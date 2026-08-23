#!/usr/bin/env python3
"""Check that declared capabilities are covered by tracked contracts and exact-commit receipts.

A capability is evidenced only when all of these hold:
- the capability is declared in `.factory/environment.toml`;
- a tracked contract exists (see `check-capability-contracts.py`);
- the commit-bound runner aggregate `.factory-state/runner-evidence.json`
  lists the capability as evidenced (probe passed and verifier exit 0);
- the receipt logs do not contradict the contract: a must-not-skip token or a
  deny-simulated marker inside the contract's probe scope means the probe was
  skipped or simulated and the capability is unevidenced.

Missing contract, missing receipt, or a skip never auto-reclassifies a row.
The strong aggregate binding is enforced separately by
`scripts/check-factory-runner-evidence.py`; this checker adds the
contract-level probe/skip guards over the accepted aggregate.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import sys
from pathlib import Path
import tomllib

ROOT = Path(__file__).resolve().parent.parent
NAME = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
MAX_LOG_SCAN = 32 * 1024 * 1024


def fail(message: str) -> None:
    raise SystemExit(f"capability-evidence: {message}")


def no_duplicate_keys(pairs: list) -> dict:
    """JSON object-pairs hook: reject duplicate object keys fail-closed."""
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


_PINNED_GIT_CACHE: dict[str, object] = {}


def load_pinned_git(root: Path):
    """Load the canonical pinned-Git authority (``.factory/loop/gitutil.py``).

    The runner aggregate is bound to HEAD by a trusted Git call; that call runs
    the PATH-pinned absolute executable with a sanitized environment,
    ``GIT_NO_REPLACE_OBJECTS=1``, and a finite timeout — never an unqualified
    ``git`` from a caller-controlled ``PATH``.
    """
    key = str(root)
    if key not in _PINNED_GIT_CACHE:
        try:
            _PINNED_GIT_CACHE[key] = load_script_module(
                "factory_gitutil", root / ".factory/loop/gitutil.py"
            )
        except Exception as exc:  # GitBoundaryError and import failures alike
            fail(f"pinned Git authority is unavailable: {exc}")
    return _PINNED_GIT_CACHE[key]


def declared_capabilities(root: Path) -> set[str]:
    path = root / ".factory/environment.toml"
    if path.is_symlink() or not path.is_file():
        fail(f"missing environment declaration: {path}")
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, tomllib.TOMLDecodeError) as exc:
        fail(f"cannot parse environment declaration {path}: {exc}")
    capabilities: set[str] = set()
    for entry in data.get("tools", []) + data.get("runners", []):
        if not isinstance(entry, dict):
            fail("tools/runners entries must be tables")
        items = entry.get("capabilities", [])
        if not isinstance(items, list) or not all(isinstance(item, str) and item for item in items):
            fail("capabilities must be an array of strings")
        capabilities.update(items)
    return capabilities


def contract_for(root: Path, capability: str) -> dict:
    path = root / ".factory/capability-contracts.json"
    if path.is_symlink() or not path.is_file():
        fail(f"missing tracked capability contract file: {path}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=no_duplicate_keys)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        fail(f"cannot parse {path}: {exc}")
    if not isinstance(data, dict) or data.get("schema") != "ralph-capability-contract/v1":
        fail(f"contract file schema is invalid: {path}")
    contracts = data.get("capabilities", [])
    if not isinstance(contracts, list):
        fail("contract file has no capabilities array")
    for contract in contracts:
        if isinstance(contract, dict) and contract.get("name") == capability:
            return contract
    fail(f"no tracked contract for declared capability {capability} (unevidenced)")


def aggregate_evidence(root: Path) -> tuple[set[str], list[Path]]:
    aggregate = root / ".factory-state/runner-evidence.json"
    if aggregate.is_symlink() or not aggregate.is_file():
        fail(f"runner evidence aggregate is missing: {aggregate}")
    try:
        data = json.loads(aggregate.read_text(encoding="utf-8"), object_pairs_hook=no_duplicate_keys)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        fail(f"invalid runner evidence aggregate {aggregate}: {exc}")
    if not isinstance(data, dict) or data.get("schema") != "factory-runner-aggregate/v1":
        fail(f"runner evidence aggregate schema is invalid: {aggregate}")
    head = git_head(root)
    if data.get("commit") != head:
        fail(f"runner evidence aggregate is not bound to HEAD {head[:12]} (stale receipts are unevidenced)")
    records = data.get("runners", [])
    if not isinstance(records, list):
        fail("runner evidence aggregate has no runners array")
    evidenced: set[str] = set()
    manifests: list[Path] = []
    for record in records:
        if not isinstance(record, dict):
            fail("runner aggregate record is invalid")
        capabilities = record.get("capabilities")
        if not isinstance(capabilities, list) or not all(isinstance(item, str) and item for item in capabilities):
            fail("runner aggregate record has invalid capabilities")
        evidenced.update(capabilities)
        relative = record.get("manifest")
        if isinstance(relative, str):
            relative_path = Path(relative)
            if relative_path.is_absolute() or ".." in relative_path.parts:
                fail(f"runner manifest path escapes the repository: {relative}")
            path = (root / relative_path).resolve()
            if not path.is_relative_to(root.resolve()):
                fail(f"runner manifest path escapes the repository: {relative}")
            if path.is_file() and not path.is_symlink():
                manifests.append(path)
    return evidenced, manifests


def git_head(root: Path) -> str:
    git = load_pinned_git(root)
    result = git.git_run(
        ["-C", str(root), "rev-parse", "--verify", "HEAD"],
        env=git.sanitize_git_environment(
            {**os.environ, "GIT_NO_REPLACE_OBJECTS": "1"}
        ),
        timeout=git.GIT_TIMEOUT,
    )
    if result.returncode:
        fail(f"cannot resolve HEAD of {root}")
    return result.stdout.strip()


def probe_scope(lines: list[str], marker: str) -> tuple[list[str], bool]:
    """Return the contract's probe scope and whether the marker was seen.

    With a marker, the scope is the log text after the marker line to EOF; the
    marker must be present (must-execute). Without a marker the scope is the
    whole log.
    """
    if not marker:
        return lines, True
    for index, line in enumerate(lines):
        if marker in line:
            return lines[index + 1:], True
    return [], False


def scan_tokens(scope: list[str], tokens: list[str]) -> list[str]:
    hits: set[str] = set()
    for token in tokens:
        pattern = re.compile(re.escape(token), re.IGNORECASE)
        for line in scope:
            if pattern.search(line):
                hits.add(token)
                break
    return sorted(hits)


def verify_capability(root: Path, capability: str) -> None:
    declared = declared_capabilities(root)
    if capability not in declared:
        fail(f"capability {capability} is not declared in .factory/environment.toml")
    contract = contract_for(root, capability)
    evidenced, manifests = aggregate_evidence(root)
    if capability not in evidenced:
        fail(f"capability {capability} has no accepted runner receipt (unevidenced)")
    if contract.get("must_execute") is not True:
        fail(f"contract for {capability} must set must_execute=true")
    marker = contract.get("probe_marker", "")
    skip_tokens = contract.get("must_not_skip", [])
    deny_tokens = contract.get("deny_simulated_markers", [])
    if not marker and not skip_tokens and not deny_tokens:
        return
    if not manifests:
        fail(f"capability {capability} has no receipt logs to check (unevidenced)")
    marker_seen_any = False
    for manifest_path in manifests:
        try:
            manifest_data = json.loads(
                manifest_path.read_text(encoding="utf-8"),
                object_pairs_hook=no_duplicate_keys,
            )
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            fail(f"invalid runner manifest {manifest_path}: {exc}")
        if not isinstance(manifest_data, dict) or manifest_data.get("schema") != "factory-runner-receipt/v1":
            fail(f"runner manifest schema is invalid: {manifest_path}")
        if manifest_data.get("result") != "pass" or manifest_data.get("exit_code") != 0:
            fail(f"runner manifest does not prove a clean pass: {manifest_path}")
        manifest_dir = manifest_path.parent
        for log_name in ("stdout.log", "stderr.log"):
            log_path = manifest_dir / log_name
            if log_path.is_symlink() or not log_path.is_file():
                fail(f"receipt log is missing or unsafe: {log_path}")
            if log_path.stat().st_size > MAX_LOG_SCAN:
                fail(f"receipt log exceeds the scan limit: {log_path}")
            lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
            scope, marker_seen = probe_scope(lines, marker)
            marker_seen_any |= marker_seen
            # Only marker-scoped contracts scan per-probe sections; the
            # unmarked log of a marked contract contributes no scope.
            if marker and not marker_seen:
                continue
            skipped = scan_tokens(scope, skip_tokens)
            if skipped:
                fail(f"receipt for {capability} shows a skipped probe ({skipped}); unevidenced")
            denied = scan_tokens(scope, deny_tokens)
            if denied:
                fail(f"receipt for {capability} shows simulated/denied markers ({denied}); unevidenced")
    if marker and not marker_seen_any:
        fail(f"receipt for {capability} does not show probe marker {marker!r} (must-execute)")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--capabilities", help="comma-separated capabilities to require (default: all declared)")
    parser.add_argument("--root", default=str(ROOT), help="repository root (testing)")
    args = parser.parse_args()
    root = Path(args.root).resolve()
    declared = sorted(declared_capabilities(root))
    wanted = declared if args.capabilities is None else [item for item in args.capabilities.split(",") if item]
    if not wanted:
        if args.capabilities is not None:
            fail("no capabilities requested; a gate must never pass with an empty capability set")
        print("capability-evidence: valid (no declared capabilities)")
        return 0
    for capability in wanted:
        if not NAME.fullmatch(capability):
            fail(f"invalid capability name: {capability}")
        if capability not in declared:
            fail(f"capability {capability} is not declared in the environment")
        verify_capability(root, capability)
    print(f"capability-evidence: evidenced ({len(wanted)}): {', '.join(wanted)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
