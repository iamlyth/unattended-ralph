#!/usr/bin/env python3
"""Validate the tracked capability-contract file against the capability declaration.

A contract is the machine-readable statement of how a declared capability is
probed. Rules:
- every capability declared in `.factory/environment.toml` (tools and runners)
  must have exactly one contract, otherwise it is unevidenced;
- a contract for a capability that is not declared claims an unavailable
  capability and is rejected;
- contracts define probe argv, must-execute, must-not-skip markers, and
  deny-simulated markers; probe argv must be executable by name or a
  repository-relative tracked path.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
import tomllib

ROOT = Path(__file__).resolve().parent.parent
NAME = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
TOKEN = re.compile(r"^[^\x00-\x1f\x7f]{1,128}$")
BARE_NAME = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]*$")
# Fixture/simulation option tokens are structural fail-closed: a committed
# contract probe argv may never carry a token that equals or is prefixed by
# one of these, so the exact probe command can never switch into fixture mode.
FIXTURE_OPTION_PREFIXES = ("--fixture", "--fixture-dir", "--fixture-facts")


def is_fixture_option_token(token: str) -> bool:
    """True when a probe argv token equals or is prefixed by a fixture option.

    Covers `--fixture`, `--fixture=/path`, `--fixture-dir`, `--fixture-dir=/p`,
    `--fixture-facts`, and `--fixture-facts=/p`; any such token in a committed
    probe argv would let the probe run in fixture mode, so it is rejected.
    """
    return token == "--fixture" or any(
        token.startswith(prefix) for prefix in FIXTURE_OPTION_PREFIXES
    )


def fail(message: str) -> None:
    raise SystemExit(f"capability-contracts: {message}")


def no_duplicate_keys(pairs: list) -> dict:
    """JSON object-pairs hook: reject duplicate object keys fail-closed.

    A duplicate key in the committed contract file silently overwrites its
    predecessor under a plain ``dict`` decode and can hide a drifted or
    tampered authority; every committed-data load uses this hook instead
    (Task 14 duplicate-key hardening).
    """
    result: dict = {}
    for key, value in pairs:
        if key in result:
            fail(f"duplicate JSON object key: {key!r}")
        result[key] = value
    return result


def declared_capabilities(environment_path: Path) -> list[str]:
    if environment_path.is_symlink() or not environment_path.is_file():
        fail(f"environment declaration must be a regular tracked file: {environment_path}")
    try:
        data = tomllib.loads(environment_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, tomllib.TOMLDecodeError) as exc:
        fail(f"cannot parse {environment_path}: {exc}")
    capabilities: list[str] = []
    for entry in data.get("tools", []) + data.get("runners", []):
        if not isinstance(entry, dict):
            fail("tools/runners entries must be tables")
        items = entry.get("capabilities", [])
        if not isinstance(items, list) or not all(isinstance(item, str) and item for item in items):
            fail("capabilities must be a non-empty array of strings per entry")
        capabilities.extend(items)
    return capabilities


def load_contracts(path: Path) -> list[dict]:
    if path.is_symlink() or not path.is_file():
        fail(f"capability-contracts must be a regular tracked file: {path}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=no_duplicate_keys)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        fail(f"cannot parse {path}: {exc}")
    if not isinstance(data, dict) or data.get("schema") != "ralph-capability-contract/v1":
        fail("contract file schema must be ralph-capability-contract/v1")
    contracts = data.get("capabilities", [])
    if not isinstance(contracts, list):
        fail("contracts must be an array")
    return contracts


def validate_contract(contract: dict, index: int) -> tuple[str, str]:
    required = {
        "name", "probe_argv", "probe_marker", "must_execute", "must_not_skip", "deny_simulated_markers",
    }
    optional = {"status", "probe_stage", "probe_stdout_contains", "probe_is_verify_run", "runner_class"}
    if not isinstance(contract, dict):
        fail(f"contracts[{index}] must be an object")
    if not required.issubset(set(contract)) or not set(contract).issubset(required | optional):
        fail(f"contracts[{index}] fields are invalid (required {sorted(required)}, optional {sorted(optional)})")
    name = contract["name"]
    if not isinstance(name, str) or not NAME.fullmatch(name):
        fail(f"contracts[{index}].name is invalid")
    status = contract.get("status", "declared")
    if status not in ("declared", "candidate"):
        fail(f"contracts[{index}].status must be declared or candidate")
    argv = contract["probe_argv"]
    if not isinstance(argv, list) or not argv or not all(isinstance(item, str) and item for item in argv):
        fail(f"contracts[{index}].probe_argv must be a non-empty array of strings")
    if any(any(ord(char) < 32 for char in item) for item in argv):
        fail(f"contracts[{index}].probe_argv must be control-character-free")
    fixture_tokens = [item for item in argv if is_fixture_option_token(item)]
    if fixture_tokens:
        fail(
            f"contracts[{index}].probe_argv carries a fixture/simulation option "
            f"token {fixture_tokens!r}; a committed probe can never run in fixture mode"
        )
    probe0 = argv[0]
    if "/" in probe0:
        relative = Path(probe0)
        if relative.is_absolute():
            if ".." in relative.parts or relative.parts[1] not in {"bin", "usr"}:
                fail(f"contracts[{index}].probe_argv must be a bare command name, a scripts/tests path, or a /bin|/usr/bin binary")
        else:
            if ".." in relative.parts or len(relative.parts) != 2 or relative.parts[0] not in {"scripts", "tests"}:
                fail(f"contracts[{index}].probe_argv must start with a bare command name or a scripts/tests path")
            if not (ROOT / probe0).is_file():
                fail(f"contracts[{index}].probe_argv names a missing tracked script: {probe0}")
    elif not BARE_NAME.fullmatch(probe0):
        fail(f"contracts[{index}].probe_argv[0] is not a bare command name: {probe0}")
    marker = contract["probe_marker"]
    if not isinstance(marker, str):
        fail(f"contracts[{index}].probe_marker must be a string")
    if marker and len(marker) > 256:
        fail(f"contracts[{index}].probe_marker is too long")
    stage = contract.get("probe_stage", "post")
    if stage not in ("env", "post"):
        fail(f"contracts[{index}].probe_stage must be env or post")
    if contract.get("probe_is_verify_run") not in (None, True, False):
        fail(f"contracts[{index}].probe_is_verify_run must be a boolean")
    runner_class = contract.get("runner_class")
    if runner_class is not None and (
        not isinstance(runner_class, str) or not NAME.fullmatch(runner_class)
    ):
        fail(f"contracts[{index}].runner_class must be a lowercase runner class name")
    contains = contract.get("probe_stdout_contains", [])
    if not isinstance(contains, list) or not all(isinstance(item, str) and TOKEN.fullmatch(item) and item for item in contains):
        fail(f"contracts[{index}].probe_stdout_contains must be an array of non-empty tokens")
    if contract["must_execute"] is not True:
        fail(f"contracts[{index}].must_execute must be true")
    for field in ("must_not_skip", "deny_simulated_markers"):
        markers = contract[field]
        if not isinstance(markers, list) or not all(isinstance(item, str) and TOKEN.fullmatch(item) and item for item in markers):
            fail(f"contracts[{index}].{field} must be an array of non-empty tokens")
    return name, status


def main() -> int:
    contracts_path = ROOT / ".factory/capability-contracts.json"
    declared = sorted(set(declared_capabilities(ROOT / ".factory/environment.toml")))
    contracts = load_contracts(contracts_path)
    named: list[str] = []
    declared_named: list[str] = []
    for index, contract in enumerate(contracts):
        name, status = validate_contract(contract, index)
        named.append(name)
        if status == "declared":
            declared_named.append(name)
    if len(named) != len(set(named)):
        fail("contract names must be unique")
    missing = sorted(set(declared) - set(declared_named))
    if missing:
        fail(f"declared capabilities lack a declared contract (unevidenced): {missing}")
    unavailable = sorted(set(declared_named) - set(declared))
    if unavailable:
        fail(f"declared contract claims an undeclared/unavailable capability: {unavailable}")
    # Candidate contracts are the designed-but-unprovisioned capability probes:
    # they may be tracked before the capability is declared, but they must not
    # claim an already-declared capability (a declared capability's contract
    # must be promoted to declared status when the capability is declared).
    candidates = sorted(set(named) - set(declared_named))
    for name in candidates:
        if name in declared:
            fail(f"capability {name} is declared but its contract is still candidate")
    classes = sorted(
        {
            contract.get("runner_class")
            for contract in contracts
            if contract.get("runner_class")
        }
    )
    print(
        f"capability-contracts: valid ({len(named)} contracts, {len(declared)} declared capabilities, "
        f"{len(candidates)} candidates, runner classes {classes})"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
