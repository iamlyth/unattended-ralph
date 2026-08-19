#!/usr/bin/env python3
"""Validate the machine-readable conformance sidecar against the implementation plan.

The sidecar `.factory/artifacts/conformance.json` is the machine-readable
binding for every row of the plan's specification conformance matrix. Free-text
plan cells alone never prove acceptance: a row may claim `verified` only with a
structured sidecar entry declaring evidence tier, required capabilities, the
exact evidence commit, and receipt/artifact refs.

Rules:
- requirement IDs and plan-matrix IDs must match exactly;
- `verified` rows cannot be satisfied by a lower evidence tier than their
  declared required tier, or by an undeclared/unevidenced capability;
- `blocked` rows stay representable (planning) but fail implementation
  completion (complete);
- `not_applicable` requires an explicit spec-scoped reason;
- every `verified` row must name an exact evidence commit and either a receipt
  or an artifact ref that exists at that commit (free-text rows are rejected);
- complete mode requires every row `verified`.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CLASSIFICATIONS = {"verified", "partial", "missing", "ambiguous", "blocked", "not_applicable"}
TIERS = ["unit", "simulated", "private_integration", "installed", "real_system", "human"]
TIER_INDEX = {tier: index for index, tier in enumerate(TIERS)}
MATRIX_ID = re.compile(r"^[A-Z][A-Z0-9]*(?:[-_][A-Z0-9]+)+$")
SHA = re.compile(r"^[0-9a-f]{40}$")
CAPABILITY = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
PLAN_PATH = ROOT / ".factory/artifacts/implementation-plan.md"
ENVIRONMENT_PATH = ROOT / ".factory/environment.toml"
SIDECAR_DEFAULT = ROOT / ".factory/artifacts/conformance.json"


def fail(message: str) -> None:
    raise SystemExit(f"conformance: {message}")


def plan_matrix(text: str) -> dict[str, str]:
    match = re.search(r"^## Specification conformance matrix\s*$\n(.*?)(?=^##\s|\Z)", text, re.M | re.S)
    if not match:
        fail("plan has no Specification conformance matrix section")
    rows: dict[str, str] = {}
    for line in match.group(1).splitlines():
        line = line.strip()
        if not line.startswith("|"):
            continue
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if len(cells) != 5 or cells[0] == "ID":
            continue
        if all(re.fullmatch(r":?-{3,}:?", cell) for cell in cells):
            continue
        requirement_id, classification = cells[0], cells[2]
        if not MATRIX_ID.fullmatch(requirement_id):
            fail(f"plan matrix has an invalid requirement ID: {requirement_id}")
        rows[requirement_id] = classification
    return rows


def load_sidecar(path: Path) -> dict:
    if path.is_symlink() or not path.is_file():
        fail(f"conformance sidecar must be a regular tracked file: {path}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        fail(f"cannot parse conformance sidecar {path}: {exc}")
    if not isinstance(data, dict) or data.get("schema") != "ralph-conformance/v1":
        fail(f"conformance sidecar schema must be ralph-conformance/v1: {path}")
    requirements = data.get("requirements")
    if not isinstance(requirements, list) or not requirements:
        fail("conformance sidecar must declare at least one requirement")
    return data


def validate_requirement(requirement: dict, index: int) -> None:
    if not isinstance(requirement, dict):
        fail(f"requirements[{index}] must be an object")
    expected = {
        "id", "spec_sections", "classification", "evidence_tier", "required_tier",
        "required_capabilities", "evidence_commit", "receipts", "artifacts", "reason",
    }
    if set(requirement) != expected:
        fail(f"requirements[{index}] fields do not match the conformance schema")
    requirement_id = requirement["id"]
    if not isinstance(requirement_id, str) or not MATRIX_ID.fullmatch(requirement_id):
        fail(f"requirements[{index}].id is invalid: {requirement_id}")
    sections = requirement["spec_sections"]
    if not isinstance(sections, list) or not sections or not all(isinstance(item, str) and item for item in sections):
        fail(f"requirements[{index}].spec_sections must be a non-empty string array")
    classification = requirement["classification"]
    if classification not in CLASSIFICATIONS:
        fail(f"requirements[{index}] has invalid classification {classification!r}")
    for tier_field in ("evidence_tier", "required_tier"):
        tier = requirement[tier_field]
        if tier not in TIER_INDEX:
            fail(f"requirements[{index}].{tier_field} has invalid tier {tier!r}")
    capabilities = requirement["required_capabilities"]
    if not isinstance(capabilities, list) or not all(isinstance(item, str) and CAPABILITY.fullmatch(item) for item in capabilities):
        fail(f"requirements[{index}].required_capabilities must be valid capability names")
    if len(capabilities) != len(set(capabilities)):
        fail(f"requirements[{index}].required_capabilities must be unique")
    commit = requirement["evidence_commit"]
    if not isinstance(commit, str) or not SHA.fullmatch(commit):
        fail(f"requirements[{index}].evidence_commit must be a 40-character commit")
    for ref_field in ("receipts", "artifacts"):
        refs = requirement[ref_field]
        if not isinstance(refs, list) or not all(isinstance(item, str) and item for item in refs):
            fail(f"requirements[{index}].{ref_field} must be a string array")
        for ref in refs:
            path = Path(ref)
            if path.is_absolute() or ".." in path.parts or not ref.startswith((".factory", "src", "tests", "scripts", "data")):
                fail(f"requirements[{index}].{ref_field} has an unsafe ref: {ref}")
    reason = requirement["reason"]
    if not isinstance(reason, str):
        fail(f"requirements[{index}].reason must be a string")
    if classification in {"blocked", "not_applicable"} and not reason.strip():
        fail(f"requirements[{index}] classification {classification} requires an explicit reason")
    if classification == "not_applicable" and not (re.search(r"[§][0-9]", reason) or "SPEC.md" in reason):
        fail(f"requirements[{index}] not_applicable requires a spec-scoped reason (a §section or docs/SPEC.md)")


def reference_exists(root: Path, ref: str, commit: str) -> bool:
    path = root / ref
    if path.exists() and not path.is_symlink():
        return True
    result = subprocess.run(
        ["git", "cat-file", "-e", f"{commit}:{ref}"],
        cwd=root, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    return result.returncode == 0


def check_capability_evidence(root: Path, capabilities: list[str]) -> None:
    sys.path.insert(0, str(root / "scripts"))
    try:
        import check_capability_evidence as checker
    except ImportError as exc:
        fail(f"capability evidence checker is unavailable: {exc}")
    for capability in capabilities:
        checker.verify_capability(root, capability)


def validate_complete(root: Path, data: dict) -> None:
    for index, requirement in enumerate(data["requirements"]):
        validate_requirement(requirement, index)
        classification = requirement["classification"]
        if classification != "verified":
            fail(f"completion rejected while requirement {requirement['id']} is `{classification}` (blocked must fail implementation completion)")
        evidence_tier = requirement["evidence_tier"]
        required_tier = requirement["required_tier"]
        if TIER_INDEX[evidence_tier] < TIER_INDEX[required_tier]:
            fail(f"requirement {requirement['id']} claims verified at tier {evidence_tier} below required tier {required_tier}")
        if required_tier in {"real_system", "human"} and not requirement["required_capabilities"]:
            fail(f"requirement {requirement['id']} requires a real-system/human tier but declares no required capabilities")
        capabilities = requirement["required_capabilities"]
        if capabilities:
            check_capability_evidence(root, capabilities)
        commit = requirement["evidence_commit"]
        refs = requirement["receipts"] + requirement["artifacts"]
        if not refs:
            fail(f"requirement {requirement['id']} claims verified with no receipt or artifact refs (free-text row)")
        for ref in refs:
            if not reference_exists(root, ref, commit):
                fail(f"requirement {requirement['id']} references missing receipt/artifact {ref} at commit {commit}")


def cross_check(data: dict, matrix: dict[str, str]) -> None:
    sidecar_ids = {requirement["id"] for requirement in data["requirements"]}
    if sidecar_ids != set(matrix):
        missing_in_sidecar = sorted(set(matrix) - sidecar_ids)
        missing_in_plan = sorted(sidecar_ids - set(matrix))
        if missing_in_sidecar or missing_in_plan:
            fail(
                "sidecar and plan matrix IDs differ"
                + (f"; missing from sidecar: {missing_in_sidecar}" if missing_in_sidecar else "")
                + (f"; missing from plan: {missing_in_plan}" if missing_in_plan else "")
            )
    plan_classifications = {"verified", "partial", "missing", "ambiguous"}
    for requirement in data["requirements"]:
        requirement_id = requirement["id"]
        plan_classification = matrix[requirement_id]
        side_classification = requirement["classification"]
        if plan_classification == "verified" and side_classification != "verified":
            fail(f"plan row {requirement_id} is verified but the sidecar says {side_classification}")
        if plan_classification != "verified" and side_classification == "verified":
            fail(f"plan row {requirement_id} is {plan_classification} but the sidecar claims verified")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("planning", "complete"))
    parser.add_argument("path", nargs="?", default=str(SIDECAR_DEFAULT))
    parser.add_argument("--root", default=str(ROOT))
    args = parser.parse_args()
    root = Path(args.root).resolve()
    data = load_sidecar(root / args.path if not Path(args.path).is_absolute() else Path(args.path))
    for index, requirement in enumerate(data["requirements"]):
        validate_requirement(requirement, index)
    plan_text = (root / ".factory/artifacts/implementation-plan.md").read_text(encoding="utf-8")
    matrix = plan_matrix(plan_text)
    cross_check(data, matrix)
    if args.mode == "complete":
        validate_complete(root, data)
    print(f"conformance: {args.mode} valid ({len(data['requirements'])} requirements)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
