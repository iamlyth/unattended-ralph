#!/usr/bin/env python3
"""Validate the tracked capability-contract file against the capability declaration.

A contract is the machine-readable statement of how a declared capability is
probed. Rules:
- every capability declared in `.factory/environment.toml` (tools and runners)
  must have exactly one contract, otherwise it is unevidenced;
- a contract for a capability that is not declared claims an unavailable
  capability and is rejected;
- contracts define probe argv, must-execute, meaningful nonempty must-not-skip
  markers, and deny-simulated markers; probe argv must be executable by name or a
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


def runner_names(environment_path: Path) -> list[str]:
    """Return the declared runner names from the environment declaration."""
    if environment_path.is_symlink() or not environment_path.is_file():
        fail(f"environment declaration must be a regular tracked file: {environment_path}")
    try:
        data = tomllib.loads(environment_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, tomllib.TOMLDecodeError) as exc:
        fail(f"cannot parse {environment_path}: {exc}")
    names: list[str] = []
    for entry in data.get("runners", []):
        if not isinstance(entry, dict):
            fail("runners entries must be tables")
        name = entry.get("name")
        if not isinstance(name, str) or not NAME.fullmatch(name):
            fail("runner name must be a lowercase runner class name")
        names.append(name)
    if len(names) != len(set(names)):
        fail("runner names must be unique")
    return names


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


def required_capabilities(config_path: Path) -> list[str]:
    """Return the campaign required-capabilities list from ``config.toml``.

    The required-capabilities validation is fail-closed: a missing
    ``config.toml``, a missing ``[campaign]`` table, or a missing/
    malformed ``required_capabilities`` array is rejected so a drifted or
    deleted config can never silently waive a required capability.
    """
    if config_path.is_symlink() or not config_path.is_file():
        fail(f"config must be a regular tracked file: {config_path}")
    try:
        data = tomllib.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, tomllib.TOMLDecodeError) as exc:
        fail(f"cannot parse {config_path}: {exc}")
    campaign = data.get("campaign")
    if not isinstance(campaign, dict):
        fail("config.toml must declare a [campaign] table")
    required = campaign.get("required_capabilities")
    if not isinstance(required, list) or not all(
        isinstance(item, str) and item for item in required
    ):
        fail("[campaign].required_capabilities must be an array of non-empty strings")
    return required


def load_contracts(path: Path) -> list[dict]:
    if path.is_symlink() or not path.is_file():
        fail(f"capability-contracts must be a regular tracked file: {path}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=no_duplicate_keys)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        fail(f"cannot parse {path}: {exc}")
    if not isinstance(data, dict) or data.get("schema") not in {"ralph-capability-contract/v1","ralph-capability-contract/v2"}:
        fail("contract file schema must be ralph-capability-contract/v1 or v2")
    contracts = data.get("capabilities", [])
    if not isinstance(contracts, list):
        fail("contracts must be an array")
    return contracts


def validate_contract(contract: dict, index: int) -> tuple[str, str]:
    v2="authority_probe" in contract
    required = ({"name", "candidate_probe_argv", "probe_marker", "must_execute", "must_not_skip",
                 "deny_simulated_markers", "runner_class", "authority_probe"} if v2 else
                {"name", "probe_argv", "probe_marker", "must_execute", "must_not_skip", "deny_simulated_markers"})
    optional = {"status", "probe_stage", "probe_stdout_contains", "probe_is_verify_run", "artifact_requirements", "runner_class"}
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
    # Candidate argv is informational source-fixture routing only. Execution is
    # authorized exclusively by the externally enrolled root descriptor below.
    argv = contract["candidate_probe_argv" if v2 else "probe_argv"]
    if not isinstance(argv, list) or not argv or not all(isinstance(item, str) and item and not any(ord(ch)<32 for ch in item) for item in argv):
        fail(f"contracts[{index}] probe argv must be a non-empty control-free string array")
    fixture_tokens = [item for item in argv if is_fixture_option_token(item)]
    if fixture_tokens:
        fail(f"contracts[{index}].candidate_probe_argv carries fixture option {fixture_tokens!r}")
    if v2:
        authority=contract["authority_probe"]
        afields={"probe_id","descriptor_sha256","authority_sha256","expected_semantics","must_execute","must_not_skip","deny_simulation"}
        if (not isinstance(authority,dict) or set(authority)!=afields
                or authority.get("probe_id")!=f"factory-root-probe:{contract['runner_class']}:{name}:v1"
                or not re.fullmatch(r"[0-9a-f]{64}",str(authority.get("descriptor_sha256","")))
                or not re.fullmatch(r"[0-9a-f]{64}",str(authority.get("authority_sha256","")))
                or authority.get("expected_semantics")!="capability-specific-root-authority"
                or authority.get("must_execute") is not True or authority.get("must_not_skip") is not True
                or authority.get("deny_simulation") is not True):
            fail(f"contracts[{index}].authority_probe is invalid")
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
    artifact_requirements = contract.get("artifact_requirements", {"required": [], "files": {}})
    if (not isinstance(artifact_requirements, dict) or set(artifact_requirements) != {"required", "files"}
            or not isinstance(artifact_requirements["required"], list)
            or not isinstance(artifact_requirements["files"], dict)
            or not set(artifact_requirements["required"]).issubset(artifact_requirements["files"])
            or not all(isinstance(k,str) and k and "/" not in k and isinstance(v,str) and v
                       for k,v in artifact_requirements["files"].items())):
        fail(f"contracts[{index}].artifact_requirements is invalid")
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
        if (not isinstance(markers, list) or not markers
                or not all(isinstance(item, str) and TOKEN.fullmatch(item) and item for item in markers)):
            fail(f"contracts[{index}].{field} must be a non-empty array of non-empty tokens")
    return name, status


def main() -> int:
    contracts_path = ROOT / ".factory/capability-contracts.json"
    declared = sorted(set(declared_capabilities(ROOT / ".factory/environment.toml")))
    contracts = load_contracts(contracts_path)
    authority_doc=None; authority_digest=None
    authority_path=ROOT/"deploy/factory-runner-authority-v1/authority.json"
    if any('authority_probe' in c for c in contracts):
        try:
            authority_raw=authority_path.read_bytes(); authority_doc=json.loads(authority_raw,object_pairs_hook=no_duplicate_keys)
        except (OSError,UnicodeError,json.JSONDecodeError) as exc:
            fail(f"cannot read external root authority enrollment request: {exc}")
        authority_digest=__import__('hashlib').sha256(authority_raw).hexdigest()
    named: list[str] = []
    declared_named: list[str] = []
    for index, contract in enumerate(contracts):
        name, status = validate_contract(contract, index)
        if 'authority_probe' in contract:
            ap=contract['authority_probe']; cls=contract['runner_class']
            try: descriptor=authority_doc['classes'][cls]['capabilities'][name]
            except (KeyError,TypeError): fail(f"contracts[{index}] has no matching external root authority descriptor")
            if (ap['authority_sha256']!=authority_digest or ap['probe_id']!=descriptor.get('probe_id') or ap['descriptor_sha256']!=descriptor.get('descriptor_sha256')):
                fail(f"contracts[{index}] external root authority binding is stale")
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
    # A declared contract's runner_class must name a runner that is actually
    # declared in .factory/environment.toml: a declared contract bound to an
    # undeclared runner class claims a capability against a runner the factory
    # cannot execute, which is unevidenced and rejected.
    runners = runner_names(ROOT / ".factory/environment.toml")
    runner_set = set(runners)
    undeclared_class = sorted(
        {
            contract["runner_class"]
            for contract in contracts
            if contract.get("status", "declared") == "declared"
            and contract.get("runner_class")
            and contract["runner_class"] not in runner_set
        }
    )
    if undeclared_class:
        fail(
            f"declared contracts bind to runner classes not declared in "
            f"environment.toml: {undeclared_class}"
        )
    # Every capability required by the campaign must be provided by at least
    # one declared runner; a required capability that no declared runner
    # provides can never be evidenced and is rejected rather than silently
    # waived by a drifted or deleted config.
    required = required_capabilities(ROOT / ".factory/config.toml")
    provided = set(declared)
    missing_required = sorted(set(required) - provided)
    if missing_required:
        fail(
            f"required capabilities have no declared runner providing them: "
            f"{missing_required}"
        )
    classes = sorted(
        {
            contract.get("runner_class")
            for contract in contracts
            if contract.get("runner_class")
        }
    )
    print(
        f"capability-contracts: valid ({len(named)} contracts, {len(declared)} declared capabilities, "
        f"{len(candidates)} candidates, runner classes {classes}, declared runners {runners})"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
