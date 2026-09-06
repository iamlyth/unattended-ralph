#!/usr/bin/env python3
"""Check that declared capabilities are covered by tracked contracts and exact-commit receipts.

A capability is evidenced only when all of these hold:
- the capability is declared in `.factory/environment.toml`;
- a tracked contract exists (see `check-capability-contracts.py`);
- the canonical strong runner validator accepts the exact aggregate,
  detached signatures, manifest/log digests, commit/tree/environment/archive/
  verifier bindings, and complete declared-runner coverage;
- that strongly validated aggregate lists the capability as evidenced;
- the receipt logs do not contradict the contract: a must-not-skip token or a
  deny-simulated marker inside the contract's probe scope means the probe was
  skipped or simulated and the capability is unevidenced.

Missing contract, missing receipt, or a skip never auto-reclassifies a row.
This checker invokes the canonical strong validator itself before inspecting
contract probe logs; callers cannot accidentally accept the aggregate-only
shape by omitting a separate gate.
"""

from __future__ import annotations

import argparse
import importlib.util
import hashlib
import json
import os
import re
import sys
from pathlib import Path
import tomllib

ROOT = Path(__file__).resolve().parents[2]
NAME = re.compile(r"^[a-z0-9](?:[a-z0-9._-]{0,62}[a-z0-9])?$")
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
_STRONG_EVIDENCE_CACHE: dict[tuple[str, str, str, str], tuple[str, set[str], dict]] = {}


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
    if not isinstance(data, dict) or data.get("schema") not in {"ralph-capability-contract/v1","ralph-capability-contract/v2"}:
        fail(f"contract file schema is invalid: {path}")
    contracts = data.get("capabilities", [])
    if not isinstance(contracts, list):
        fail("contract file has no capabilities array")
    for contract in contracts:
        if isinstance(contract, dict) and contract.get("name") == capability:
            return contract
    fail(f"no tracked contract for declared capability {capability} (unevidenced)")


def strong_runner_evidence(root: Path) -> tuple[str, set[str], dict]:
    """Run the canonical full runner validator for this exact repository."""
    campaign_id = os.environ.get("FACTORY_CAMPAIGN_ID", "")
    readiness_nonce = os.environ.get("FACTORY_READINESS_NONCE", "")
    head = git_head(root)
    key = (str(root.resolve()), campaign_id, readiness_nonce, head)
    if key in _STRONG_EVIDENCE_CACHE:
        return _STRONG_EVIDENCE_CACHE[key]
    checker_path = root / ".factory/tools/check-factory-runner-evidence.py"
    try:
        checker = load_script_module("factory_runner_evidence", checker_path)
        if Path(checker.ROOT).resolve() != root.resolve():
            fail("strong runner checker resolved a foreign repository root")
        digest, capabilities, aggregate = checker.validate(
            head,
            expected_campaign_id=campaign_id,
            expected_readiness_nonce=readiness_nonce,
            include_view=True,
        )
    except SystemExit as exc:
        detail = str(exc) or "validation failed"
        fail(f"strong runner evidence rejected: {detail}")
    except Exception as exc:
        fail(f"strong runner evidence checker is unavailable: {exc}")
    if (
        not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest)
        or not isinstance(capabilities, list)
        or not all(isinstance(item, str) and NAME.fullmatch(item) for item in capabilities)
        or not isinstance(aggregate,dict) or aggregate.get("schema")!="factory-runner-aggregate/v4"
    ):
        fail("strong runner checker returned an invalid validation result")
    result = digest, set(capabilities), aggregate
    _STRONG_EVIDENCE_CACHE[key] = result
    return result


def aggregate_evidence(root: Path) -> tuple[set[str], dict[str, list[tuple[str, Path]]]]:
    """Read only the canonical v4 aggregate in the explicit readiness namespace."""
    strong_digest, strong_capabilities, data = strong_runner_evidence(root)
    campaign_id = os.environ.get("FACTORY_CAMPAIGN_ID", "")
    readiness_nonce = os.environ.get("FACTORY_READINESS_NONCE", "")
    if not NAME.fullmatch(campaign_id) or not re.fullmatch(r"[0-9a-f]{64}", readiness_nonce):
        fail("explicit campaign/readiness namespace is required")
    aggregate = (root / ".factory-state" / "runner-evidence" /
                 campaign_id / readiness_nonce / "aggregate.json")
    # `data` is the immutable classification view returned by the strong
    # validator.  Do not reopen aggregate by pathname after validation.
    if (not isinstance(data, dict)
            or set(data) != {"schema", "campaign_id", "readiness_nonce", "commit", "tree", "environment_blob", "runners"}
            or data.get("schema") != "factory-runner-aggregate/v4"):
        fail(f"runner evidence aggregate schema is invalid: {aggregate}")
    if data.get("campaign_id") != campaign_id or data.get("readiness_nonce") != readiness_nonce:
        fail("runner evidence aggregate campaign/readiness binding is stale or replayed")
    head = git_head(root)
    if data.get("commit") != head:
        fail(f"runner evidence aggregate is not bound to HEAD {head[:12]} (stale receipts are unevidenced)")
    records = data.get("runners", [])
    if not isinstance(records, list):
        fail("runner evidence aggregate has no runners array")
    evidenced: set[str] = set()
    manifests: dict[str, list[tuple[str, Path]]] = {}
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
                for capability in capabilities:
                    manifests.setdefault(capability, []).append((str(record.get("name", "")), path))
    if evidenced != strong_capabilities:
        fail("aggregate capabilities differ from the strongly validated runner set")
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

    A marker is an exact capability delimiter.  Scope ends at the next
    capability delimiter, so output from a later gate/probe can never satisfy
    or poison this capability's contract.
    """
    if not marker:
        return lines, True
    delimiter = re.compile(r"^--- [a-z0-9][a-z0-9._-]* capability contract(?: \(candidate\))? ---$")
    matches = [index for index, line in enumerate(lines) if line == marker]
    if len(matches) != 1:
        return [], False
    start = matches[0] + 1
    end = next((index for index in range(start, len(lines)) if delimiter.fullmatch(lines[index])), len(lines))
    return lines[start:end], True


def scan_tokens(scope: list[str], tokens: list[str]) -> list[str]:
    hits: set[str] = set()
    for token in tokens:
        pattern = re.compile(re.escape(token), re.IGNORECASE)
        for line in scope:
            if pattern.search(line):
                hits.add(token)
                break
    return sorted(hits)


def validate_structured_artifacts(manifest_path: Path, manifest: dict, contract: dict, capability: str) -> None:
    requirements=contract.get("artifact_requirements", {"required":[],"files":{}})
    required=requirements.get("required",[]); files=requirements.get("files",{})
    descriptors={item.get("path"):item for item in manifest.get("artifacts",[]) if isinstance(item,dict)}
    expected={f"{capability}/{name}" for name in required}
    if not expected.issubset(descriptors): fail(f"receipt for {capability} omits required signed artifacts")
    for name,media in files.items():
        path=f"{capability}/{name}"
        if path in descriptors and descriptors[path].get("media_type") != media:
            fail(f"receipt for {capability} has wrong artifact media type: {name}")
    artifact_dir=manifest_path.parent/"artifacts"/capability
    def load(name):
        path=artifact_dir/name
        try: return json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=no_duplicate_keys)
        except (OSError,UnicodeError,json.JSONDecodeError) as exc: fail(f"invalid structured artifact {name}: {exc}")
    # Product semantics are intentionally not interpreted here. The immutable
    # root authority's pinned semantic analyzer accepted these exact signed
    # bytes before the signer could run; this checker verifies descriptors,
    # digests, class/probe IDs, and declared artifact requirements only.


def verify_capability(root: Path, capability: str) -> None:
    declared = declared_capabilities(root)
    if capability not in declared:
        fail(f"capability {capability} is not declared in .factory/environment.toml")
    contract = contract_for(root, capability)
    evidenced, manifests_by_capability = aggregate_evidence(root)
    if capability not in evidenced:
        fail(f"capability {capability} has no accepted runner receipt (unevidenced)")
    manifests = manifests_by_capability.get(capability, [])
    runner_class = contract.get("runner_class")
    if runner_class is not None and (
        not isinstance(runner_class, str) or not manifests
        or any(name != runner_class for name, _path in manifests)
    ):
        fail(f"capability {capability} is evidenced by the wrong runner principal/class")
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
    required_output = contract.get("probe_stdout_contains", [])
    if not isinstance(required_output, list) or not all(isinstance(item, str) and item for item in required_output):
        fail(f"contract for {capability} has malformed required output markers")
    required_seen: set[str] = set()
    for _runner_name, manifest_path in manifests:
        try:
            manifest_data = json.loads(
                manifest_path.read_text(encoding="utf-8"),
                object_pairs_hook=no_duplicate_keys,
            )
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            fail(f"invalid runner manifest {manifest_path}: {exc}")
        if not isinstance(manifest_data, dict) or manifest_data.get("schema") != "factory-runner-receipt/v3":
            fail(f"runner manifest schema is invalid: {manifest_path}")
        authority_probe=contract.get('authority_probe',{})
        if (manifest_data.get("result") != "pass" or manifest_data.get("exit_code") != 0
                or authority_probe.get('authority_sha256')!=manifest_data.get('authority_sha256')
                or authority_probe.get('probe_id')!=f"factory-root-probe:{runner_class}:{capability}:v1"
                or authority_probe.get('must_execute') is not True
                or authority_probe.get('must_not_skip') is not True
                or authority_probe.get('deny_simulation') is not True):
            fail(f"runner manifest does not bind the executed root authority probe: {manifest_path}")
        validate_structured_artifacts(manifest_path, manifest_data, contract, capability)
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
            if log_name == "stdout.log":
                for required in required_output:
                    if any(required in line for line in scope):
                        required_seen.add(required)
            skipped = scan_tokens(scope, skip_tokens)
            if skipped:
                fail(f"receipt for {capability} shows a skipped probe ({skipped}); unevidenced")
            denied = scan_tokens(scope, deny_tokens)
            if denied:
                fail(f"receipt for {capability} shows simulated/denied markers ({denied}); unevidenced")
    if marker and not marker_seen_any:
        fail(f"receipt for {capability} does not show probe marker {marker!r} (must-execute)")
    missing_output = sorted(set(required_output) - required_seen)
    if missing_output:
        fail(f"receipt for {capability} omits required probe results {missing_output} (partial/substituted evidence)")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--capabilities", help="comma-separated capabilities to require (default: all declared)")
    parser.add_argument("--root", default=str(ROOT), help="repository root (testing)")
    args = parser.parse_args()
    root = Path(args.root).resolve()
    # The complete generic contract authority always runs first, including
    # the zero-capability case; readiness cannot accidentally validate only
    # aggregate shape while skipping contract/schema policy.
    validator = load_script_module(
        "factory_capability_contracts",
        root / ".factory/tools/check-capability-contracts.py",
    )
    try:
        if validator.main(root) != 0:
            fail("full capability contract validation failed")
    except SystemExit as exc:
        fail(f"full capability contract validation failed ({exc.code})")
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
