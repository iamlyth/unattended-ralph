#!/usr/bin/env python3
"""Validate the machine-readable conformance sidecar against the implementation plan.

The sidecar `.factory/artifacts/conformance.json` is the machine-readable
binding for every row of the plan's specification conformance matrix. Free-text
plan cells alone never prove acceptance: a row may claim `verified` only with a
structured sidecar entry declaring evidence tier, required capabilities, the
exact evidence commit, and receipt/artifact refs.

Rules:
- requirement IDs must agree exactly across the committed §24 registry
  (`.factory/schemas/factory-plan-v1.requirements.json`), the plan matrix, the
  sidecar, and the requirement-policy map: a missing, extra, renumbered, or
  duplicated ID anywhere fails closed (no ID may silently fall out of one
  authority while staying in another), and every committed JSON load uses an
  object-pairs hook that rejects duplicate object keys;
- the plan matrix and the sidecar must agree on every row *exactly*:
  a matrix classification cell that diverges from the sidecar (for example
  `missing` where the sidecar says `partial`) fails closed;
- `required_tier` and `required_capabilities` are assigned by the separately
  committed requirement-policy map (`.factory/requirement-policy.json`), never
  self-declared by the sidecar; the sidecar must match both exactly
  (self-declared tier or capability relaxation is rejected);
- `verified` rows cannot be satisfied by a lower evidence tier than their
  declared required tier, by an undeclared/unevidenced capability, by a
  human-tier self-attestation, or by free-text refs: every receipt/artifact
  ref must exist as a Git blob at the declared evidence commit in both modes
  (working-tree presence is never enough), and the evidence commit must
  resolve to a real commit object. A `verified` row's required capabilities
  are run through the capability-evidence checker in planning mode as well as
  complete mode — an unevidenced capability fails now, not only at completion;
- `blocked`/`partial` rows stay representable (planning) but fail
  implementation completion (complete), and must be bound to open facts;
- `missing`/`ambiguous`/`not_applicable` rows may carry no receipt/artifact
  refs and may not claim any evidence tier above the baseline, so a missing or
  excluded behavior can never be proxied into apparent acceptance;
- refs are repository-relative paths inside tracked prefixes only: absolute
  paths, `..` traversal, prefix-boundary bypasses (`.factoryx/…`), control
  characters, backslashes, empty components, and symlinked intermediates are
  rejected;
- every trusted Git call runs the PATH-pinned absolute executable resolved by
  `.factory/loop/gitutil.py` (never an unqualified `git` from a
  caller-controlled `PATH`), with a sanitized environment that strips the
  complete `GIT_CONFIG*` family and every object-store/index/work-tree
  redirector, with `GIT_NO_REPLACE_OBJECTS=1` so replace refs can never shadow
  an evidence object, with a finite timeout, and only full 40-hex commit
  arguments (ref names are refused);
- in complete mode, receipt/artifact refs must exist as Git blobs at the
  declared evidence commit, the evidence commit must resolve to a real commit
  object, every declared capability must be evidenced by an accepted exact-
  commit runner receipt, and human-tier claims are rejected because human/
  golden approval is out-of-band and non-automatable until a verifiable
  external attestation mechanism exists;
- complete mode requires every row `verified`.
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
CLASSIFICATIONS = {"verified", "partial", "missing", "ambiguous", "blocked", "not_applicable"}
TIERS = ["unit", "simulated", "private_integration", "installed", "real_system", "human"]
TIER_INDEX = {tier: index for index, tier in enumerate(TIERS)}
MATRIX_ID = re.compile(r"^[A-Z][A-Z0-9]*(?:[-_][A-Z0-9]+)+$")
SHA = re.compile(r"^[0-9a-f]{40}$")
CAPABILITY = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
FACT_ID = re.compile(r"^FACT-[0-9]{3,}$")
# Repository-relative refs may start only inside these tracked namespaces, or
# be a single bare component at the repository root.  A first component is a
# boundary match: `.factoryx/…`, `tests2/…`, or `scripts_evil/…` are rejected
# even though they carry a matching prefix.
SAFE_REF_PREFIXES = ("src", "tests", "scripts", "data", "docs", "cmake", "packaging", "third_party", ".github", ".forgejo", ".factory")
# The single runtime namespace whose receipts may be cited in `receipts`:
# `.factory-state/audit-receipts/<safe>.json`.  These are **live** runtime
# receipts (the exact-commit coordinator-bounded machine receipts minted
# under the ignored `.factory-state/` namespace), not Git-tracked artifacts;
# they are validated against the live filesystem (nofollow, owner 0600,
# single link, schema, exit 0, digests, coordinator) instead of a Git blob.
# No other `.factory-state/...` path is ever a valid conformance ref.
RUNTIME_RECEIPT_PREFIX = ".factory-state/audit-receipts/"
RUNTIME_RECEIPT_TAG = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}\.json$")
PLAN_PATH = ROOT / ".factory/artifacts/implementation-plan.md"
SIDECAR_DEFAULT = ROOT / ".factory/artifacts/conformance.json"
FACTS_DEFAULT = ROOT / ".factory/artifacts/blocked-facts.json"
POLICY_DEFAULT = ROOT / ".factory/requirement-policy.json"
REGISTRY_DEFAULT = ROOT / ".factory/schemas/factory-plan-v1.requirements.json"


def fail(message: str) -> None:
    raise SystemExit(f"conformance: {message}")


def no_duplicate_keys(pairs: list) -> dict:
    """JSON object-pairs hook: reject duplicate object keys fail-closed.

    A duplicate key in any sidecar/registry/policy/ledger load silently
    overwrites its predecessor under a plain ``dict`` decode and can hide a
    drifted authority; every committed-data load in this validator (and its
    blocked-facts / capability checkers) uses this hook instead.
    """
    result: dict = {}
    for key, value in pairs:
        if key in result:
            fail(f"duplicate JSON object key: {key!r}")
        result[key] = value
    return result


def plan_matrix(text: str) -> dict[str, str] | None:
    match = re.search(r"^## Specification conformance matrix\s*$\n(.*?)(?=^##\s|\Z)", text, re.M | re.S)
    if not match:
        return None
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


def load_script_module(name: str, path: Path):
    """Load a dashed-name factory script as an importable module."""
    if not path.is_file() or path.is_symlink():
        fail(f"factory script is unavailable: {path}")
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        fail(f"cannot load factory script: {path}")
    module = importlib.util.module_from_spec(spec)
    # Register before exec: a loaded authority may declare module-level
    # dataclasses whose ``cls.__module__`` must resolve through
    # ``sys.modules`` (a hidden evidence/receipt authority does).
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(name, None)
        raise
    return module


def load_sidecar(path: Path) -> dict:
    if path.is_symlink() or not path.is_file():
        fail(f"conformance sidecar must be a regular tracked file: {path}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=no_duplicate_keys)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        fail(f"cannot parse conformance sidecar {path}: {exc}")
    if not isinstance(data, dict) or data.get("schema") != "ralph-conformance/v1":
        fail(f"conformance sidecar schema must be ralph-conformance/v1: {path}")
    requirements = data.get("requirements")
    if not isinstance(requirements, list):
        fail("conformance sidecar must declare a requirements array")
    return data


def load_registry(root: Path) -> set[str]:
    """Load the committed §24 requirement-ID registry.

    The registry (`.factory/schemas/factory-plan-v1.requirements.json`) is
    part of the acceptance boundary (PLAN-01, Task 18 item 3): the plan matrix,
    the conformance sidecar, and the requirement-policy map must each carry
    exactly these IDs. A missing, malformed, or divergent registry fails
    closed so a requirement can never silently drop out of one file while
    staying in another.
    """
    path = root / ".factory/schemas/factory-plan-v1.requirements.json"
    if path.is_symlink() or not path.is_file():
        fail(f"conformance registry is missing or unsafe: {path}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=no_duplicate_keys)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        fail(f"cannot parse conformance registry {path}: {exc}")
    if not isinstance(data, dict) or data.get("schema") != "factory-plan/v1/requirements":
        fail(f"conformance registry schema must be factory-plan/v1/requirements: {path}")
    ids = data.get("requirement_ids")
    if not isinstance(ids, list) or not all(isinstance(item, str) and item for item in ids):
        fail(f"conformance registry must declare a requirement_ids array: {path}")
    registry: set[str] = set()
    for requirement_id in ids:
        if not MATRIX_ID.fullmatch(requirement_id):
            fail(f"conformance registry has an invalid requirement ID: {requirement_id}")
        if requirement_id in registry:
            fail(f"conformance registry has a duplicate requirement ID: {requirement_id}")
        registry.add(requirement_id)
    if not registry:
        fail(f"conformance registry declares no requirement IDs: {path}")
    return registry


def load_requirement_policy(root: Path) -> dict[str, dict]:
    """Load the separately committed requirement-policy map (id -> tier + capabilities)."""
    path = root / ".factory/requirement-policy.json"
    if path.is_symlink() or not path.is_file():
        fail(f"requirement policy must be a regular tracked file: {path}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=no_duplicate_keys)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        fail(f"cannot parse requirement policy {path}: {exc}")
    if not isinstance(data, dict) or data.get("schema") != "ralph-requirement-policy/v1":
        fail(f"requirement policy schema must be ralph-requirement-policy/v1: {path}")
    entries = data.get("requirements")
    if not isinstance(entries, list):
        fail("requirement policy must declare a requirements array")
    policy: dict[str, dict] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            fail("every requirement-policy entry must be an object")
        requirement_id = entry.get("id")
        required_tier = entry.get("required_tier")
        if not isinstance(requirement_id, str) or not MATRIX_ID.fullmatch(requirement_id):
            fail(f"requirement policy has an invalid id: {requirement_id!r}")
        if required_tier not in TIER_INDEX:
            fail(f"requirement policy {requirement_id} has invalid required_tier {required_tier!r}")
        capabilities = entry.get("required_capabilities", [])
        if not isinstance(capabilities, list) or not all(
            isinstance(item, str) and CAPABILITY.fullmatch(item) for item in capabilities
        ):
            fail(f"requirement policy {requirement_id} has invalid required_capabilities")
        if len(capabilities) != len(set(capabilities)):
            fail(f"requirement policy {requirement_id} repeats a required capability")
        if requirement_id in policy:
            fail(f"requirement policy has duplicate id {requirement_id}")
        policy[requirement_id] = {
            "required_tier": required_tier,
            "required_capabilities": list(capabilities),
        }
    return policy


def load_facts(root: Path, path: Path) -> dict:
    try:
        ledger = load_script_module("validate_blocked_facts", root / "scripts/validate-blocked-facts.py")
    except (OSError, UnicodeError) as exc:
        fail(f"blocked-facts validator is unavailable: {exc}")
    data = ledger.load_ledger(path)
    ledger.validate_ledger(root, data)
    return data


FACTS_CACHE: dict[str, object] = {}
EVIDENCE_CACHE: dict[str, object] = {}


def blocked_facts_module(root: Path):
    key = str(root)
    if key not in FACTS_CACHE:
        FACTS_CACHE[key] = load_script_module(
            "validate_blocked_facts", root / "scripts/validate-blocked-facts.py"
        )
    return FACTS_CACHE[key]


def validate_ref_safety(ref: str, where: str) -> None:
    """Reject any repository-relative path that can escape or bypass Git boundaries.

    Rejections: non-string/empty refs, absolute paths, `..` or `.` components,
    control characters (including NUL), backslash separators, empty path
    components (`//`), prefix-boundary aliases (`.factoryx/…`, `testsrc/…`),
    and any first component outside the tracked ref namespaces — with one
    explicit exception: a receipt ref of the exact safe shape
    `.factory-state/audit-receipts/<tag>.json` (a **live runtime receipt**,
    not a Git-tracked artifact) is allowed in `receipts` and is validated
    against the live filesystem instead of a Git blob.  All other ref
    existence checks read Git blobs at the declared commit only, so a
    symlinked or tampered working-tree path can never certify (or falsify) a
    claim.
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
    # The runtime-receipt namespace is the only `.factory-state` ref allowed,
    # and only in `receipts`; it must be exactly the safe audit-receipts
    # shape (no traversal, no symlink, no nested path).
    if parts[0] == ".factory-state":
        if where.endswith(".receipts"):
            remainder = "/".join(parts[1:])
            if remainder.startswith("audit-receipts/") and RUNTIME_RECEIPT_TAG.fullmatch(
                remainder[len("audit-receipts/"):]
            ):
                return
        fail(
            f"{where} uses the runtime namespace outside the only allowed "
            f"shape `.factory-state/audit-receipts/<tag>.json`: {ref!r}"
        )
    if len(parts) > 1 and parts[0] not in SAFE_REF_PREFIXES:
        fail(f"{where} first component must be a tracked refs prefix: {ref!r}")


def validate_requirement(requirement: dict, index: int) -> None:
    if not isinstance(requirement, dict):
        fail(f"requirements[{index}] must be an object")
    expected = {
        "id", "spec_sections", "classification", "evidence_tier", "required_tier",
        "required_capabilities", "evidence_commit", "receipts", "artifacts",
        "fact_refs", "reason",
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
            validate_ref_safety(ref, f"requirements[{index}].{ref_field}")
    fact_refs = requirement["fact_refs"]
    if not isinstance(fact_refs, list) or not all(isinstance(item, str) and FACT_ID.fullmatch(item) for item in fact_refs):
        fail(f"requirements[{index}].fact_refs must be valid fact IDs")
    if len(fact_refs) != len(set(fact_refs)):
        fail(f"requirements[{index}].fact_refs must be unique")
    if classification in {"blocked", "partial"} and not fact_refs:
        fail(
            f"requirements[{index}] classification {classification} requires at least one "
            f"open blocked-facts reference (unavailable evidence must be fact-bound)"
        )
    if classification not in {"blocked", "partial"} and fact_refs:
        fail(f"requirements[{index}] only blocked/partial rows may reference facts")
    reason = requirement["reason"]
    if not isinstance(reason, str):
        fail(f"requirements[{index}].reason must be a string")
    if classification in {"blocked", "not_applicable"} and not reason.strip():
        fail(f"requirements[{index}] classification {classification} requires an explicit reason")
    if classification == "not_applicable" and not (re.search(r"[§][0-9]", reason) or "SPEC.md" in reason):
        fail(f"requirements[{index}] not_applicable requires a spec-scoped reason (a §section or docs/SPEC.md)")
    # No self-attested human evidence anywhere: human approval is out-of-band.
    if requirement["evidence_tier"] == "human":
        fail(
            f"requirements[{index}] claims human-tier evidence, which cannot be "
            f"self-attested by an unattended gate; force a finding"
        )
    # A missing, ambiguous, or excluded behavior may never carry evidence refs
    # and must not claim an evidence tier above the base: a stub row cannot be
    # proxied into apparent acceptance.
    if classification in {"missing", "ambiguous", "not_applicable"}:
        if requirement["receipts"] or requirement["artifacts"]:
            fail(f"requirements[{index}] classification {classification} may not carry receipt/artifact refs")
        if requirement["evidence_tier"] != "unit":
            fail(
                f"requirements[{index}] classification {classification} must declare "
                f"evidence_tier unit (no proxy evidence above the base tier)"
            )


def load_pinned_git(root: Path):
    """Load the canonical pinned-Git authority (``.factory/loop/gitutil.py``).

    Every trusted Git call of this validator (and of the blocked-facts and
    capability checkers it loads) runs the PATH-pinned absolute executable
    resolved by that module with a sanitized environment, ``GIT_NO_REPLACE_OBJECTS=1``,
    and a finite timeout: an unqualified ``git`` from a caller-controlled
    ``PATH`` or a GIT_CONFIG/object-store redirector can never substitute a
    different binary or repository behind the evidence boundary.
    """
    path = root / ".factory/loop/gitutil.py"
    try:
        return load_script_module("factory_gitutil", path)
    except Exception as exc:  # GitBoundaryError and import failures alike
        fail(f"pinned Git authority is unavailable: {exc}")


def load_evidence_module(root: Path):
    """Load the hidden evidence/receipt authority (``.factory/loop/evidence.py``).

    The hardened no-follow receipt validation (owner/mode/link-count/inode,
    schema, argv digest, adjacent stdout/stderr artifact digests) is the
    authority's own — the conformance validator never reimplements it.  The
    authority's flat-import mode resolves ``gitutil`` from the repository's
    own hidden loop namespace, so that namespace is placed on ``sys.path``
    for the module (never the caller's own tree).
    """
    key = str(Path(root).resolve())
    if key not in EVIDENCE_CACHE:
        loop = root / ".factory" / "loop"
        if str(loop) not in sys.path:
            sys.path.insert(0, str(loop))
        path = loop / "evidence.py"
        try:
            EVIDENCE_CACHE[key] = load_script_module("factory_evidence", path)
        except Exception as exc:  # ReceiptError and import failures alike
            fail(f"hidden evidence authority is unavailable: {exc}")
    return EVIDENCE_CACHE[key]


def runtime_receipt_ref(ref: str) -> bool:
    """True when ``ref`` is a live runtime receipt (not a Git-tracked artifact)."""
    parts = Path(ref).parts
    return bool(parts) and parts[0] == ".factory-state"


def validate_runtime_receipt(root: Path, ref: str, evidence_commit: str) -> None:
    """Validate one live runtime receipt at the declared evidence commit.

    The receipt must pass the hidden authority's hardened no-follow
    validation (regular single-link current-user-owned mode-0600 files,
    ``ralph-audit-receipt/v1`` schema, argv digest, stdout/stderr digest
    artifacts), exit 0, be bound to the exact row ``evidence_commit``, and
    carry the audit coordinator round/nonce binding of the protected
    ``.factory-state/audit-coordinator.json`` when it exists.  A missing,
    symlinked, stale, forged, or non-passing receipt fails closed.  Runtime
    receipts are validated against the live filesystem — never a Git blob
    (the ignored runtime namespace is not tracked).
    """
    evidence = load_evidence_module(root)
    try:
        receipt = evidence.validate_receipt(root, ref)
    except evidence.EvidenceError as exc:
        fail(f"runtime receipt {ref} is unsafe or invalid: {exc}")
    if receipt.get("exit_code") != 0:
        fail(
            f"runtime receipt {ref} did not exit 0; a PASS row cannot cite a "
            "failing receipt"
        )
    if receipt.get("evidence_commit") != evidence_commit:
        fail(
            f"runtime receipt {ref} evidence_commit "
            f"{str(receipt.get('evidence_commit'))[:12]} does not equal the "
            f"row evidence_commit {evidence_commit[:12]} (stale receipt)"
        )
    # Coordinator binding: when the protected coordinator state exists, the
    # receipt's round/nonce must match it exactly (LOW5).
    coordinator = root / ".factory-state/audit-coordinator.json"
    if coordinator.exists() or coordinator.is_symlink():
        if coordinator.is_symlink() or not coordinator.is_file():
            fail("audit coordinator state is unsafe (symlink or missing file)")
        try:
            raw = evidence.secure_read_bytes(
                coordinator, maximum=16384, what="audit coordinator state"
            )[0]
        except evidence.EvidenceError as exc:
            fail(f"audit coordinator state is unsafe: {exc}")
        try:
            state = json.loads(raw.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError) as exc:
            fail(f"audit coordinator state is invalid: {exc}")
        expected = {"schema", "round", "base_commit", "nonce", "created_at"}
        if (
            not isinstance(state, dict)
            or set(state) != expected
            or state.get("schema") != "ralph-audit-coordinator/v1"
            or type(state.get("round")) is not int
            or state["round"] < 1
            or not isinstance(state.get("base_commit"), str)
            or not SHA.fullmatch(state["base_commit"])
            or not isinstance(state.get("nonce"), str)
            or not re.fullmatch(r"[0-9a-f]{64}", state["nonce"])
        ):
            fail("audit coordinator state is invalid")
        if receipt.get("coordinator_round") != state["round"]:
            fail(
                f"runtime receipt {ref} belongs to round "
                f"{receipt.get('coordinator_round')}, not the active "
                f"coordinator round {state['round']}"
            )
        if receipt.get("coordinator_nonce") != state["nonce"]:
            fail(
                f"runtime receipt {ref} does not match the active audit "
                "coordinator nonce"
            )


def trusted_git_env(module) -> dict:
    """Sanitized environment plus replace-ref disabling for trusted Git calls.

    ``gitutil.sanitize_git_environment`` strips the complete ``GIT_CONFIG*``
    family and every object-store/index/work-tree/helper redirector; setting
    ``GIT_NO_REPLACE_OBJECTS=1`` additionally makes object resolution ignore
    any ``refs/replace`` refs, so a planted replace object can never shadow a
    real evidence blob or commit.
    """
    return module.sanitize_git_environment(
        {**os.environ, "GIT_NO_REPLACE_OBJECTS": "1"}
    )


def require_sha(commit: str, where: str) -> None:
    """Reject any non-full-SHA object argument before it reaches pinned Git.

    A ref name (``HEAD``, ``refs/…``, a branch, or a tag) resolves to a
    mutable or replaceable object; the evidence boundary only ever queries
    exact 40-hex commit IDs, so anything else is refused rather than
    resolved.
    """
    if not isinstance(commit, str) or not SHA.fullmatch(commit):
        fail(f"{where} refuses a non-commit object argument: {commit!r}")


def commit_exists(root: Path, commit: str) -> bool:
    require_sha(commit, "trusted Git commit lookup")
    git = load_pinned_git(root)
    result = git.git_run(
        ["-C", str(root), "cat-file", "-e", f"{commit}^{{commit}}"],
        env=trusted_git_env(git),
        timeout=git.GIT_TIMEOUT,
    )
    return result.returncode == 0


def reference_exists(root: Path, ref: str, commit: str) -> bool:
    # Evidence refs are always validated as Git blobs at the declared evidence
    # commit, in planning mode as well as complete mode. Working-tree presence
    # is never sufficient: a stale, uncommitted, or tampered ref is rejected
    # even when the row is not yet claiming completion. The commit argument is
    # a full 40-hex SHA (never a ref), replace refs are disabled, and the call
    # is finite-bounded.
    require_sha(commit, "trusted Git blob lookup")
    git = load_pinned_git(root)
    result = git.git_run(
        ["-C", str(root), "cat-file", "-e", f"{commit}:{ref}"],
        env=trusted_git_env(git),
        timeout=git.GIT_TIMEOUT,
    )
    return result.returncode == 0


def check_capability_evidence(root: Path, capabilities: list[str]) -> None:
    try:
        checker = load_script_module("check_capability_evidence", root / "scripts/check-capability-evidence.py")
    except (OSError, UnicodeError) as exc:
        fail(f"capability evidence checker is unavailable: {exc}")
    for capability in capabilities:
        checker.verify_capability(root, capability)


def validate_verified_claim(root: Path, requirement: dict) -> None:
    """Acceptance checks that must hold for a `verified` claim in ANY mode."""
    requirement_id = requirement["id"]
    evidence_tier = requirement["evidence_tier"]
    required_tier = requirement["required_tier"]
    if requirement["fact_refs"]:
        fail(f"requirement {requirement_id} claims verified but still references facts")
    if required_tier == "human":
        fail(
            f"requirement {requirement_id} requires the human evidence tier, which is "
            f"out-of-band and non-automatable; keep the row as a finding until a "
            f"verifiable external attestation mechanism exists"
        )
    if TIER_INDEX[evidence_tier] < TIER_INDEX[required_tier]:
        fail(f"requirement {requirement_id} claims verified at tier {evidence_tier} below required tier {required_tier}")
    if required_tier in {"real_system", "human"} and not requirement["required_capabilities"]:
        fail(f"requirement {requirement_id} requires a real-system/human tier but declares no required capabilities")
    commit = requirement["evidence_commit"]
    if not commit_exists(root, commit):
        fail(f"requirement {requirement_id} evidence_commit {commit[:12]} does not resolve to a commit")
    refs = requirement["receipts"] + requirement["artifacts"]
    if not refs:
        fail(f"requirement {requirement_id} claims verified with no receipt or artifact refs (free-text row)")
    for ref in refs:
        if runtime_receipt_ref(ref):
            # A live runtime receipt is validated against the live filesystem
            # (hardened no-follow owner/mode/link-count, schema, exit 0,
            # exact row evidence_commit, digests, coordinator binding) — it
            # is runtime evidence under the ignored namespace and can never
            # be a Git blob at the evidence commit.
            validate_runtime_receipt(root, ref, commit)
            continue
        if not reference_exists(root, ref, commit):
            fail(
                f"requirement {requirement_id} references receipt/artifact {ref} that is not a "
                f"Git blob at evidence commit {commit[:12]} (stale, uncommitted, or tampered)"
            )


def validate_complete(root: Path, data: dict, policy: dict[str, dict]) -> None:
    for index, requirement in enumerate(data["requirements"]):
        validate_requirement(requirement, index)
        classification = requirement["classification"]
        if classification != "verified":
            fail(f"completion rejected while requirement {requirement['id']} is `{classification}` (blocked must fail implementation completion)")
        validate_verified_claim(root, requirement)
        capabilities = requirement["required_capabilities"]
        if capabilities:
            check_capability_evidence(root, capabilities)


def cross_check_policy(data: dict, policy: dict[str, dict]) -> None:
    """Bind required_tier and required_capabilities to the separate policy map."""
    sidecar_ids = {requirement["id"] for requirement in data["requirements"]}
    for requirement in data["requirements"]:
        requirement_id = requirement["id"]
        if requirement_id not in policy:
            fail(f"requirement {requirement_id} is absent from the requirement-policy map")
        if requirement["required_tier"] != policy[requirement_id]["required_tier"]:
            fail(
                f"requirement {requirement_id} self-declares required_tier "
                f"{requirement['required_tier']} but the requirement-policy map assigns "
                f"{policy[requirement_id]['required_tier']}"
            )
        if sorted(requirement["required_capabilities"]) != sorted(policy[requirement_id]["required_capabilities"]):
            fail(
                f"requirement {requirement_id} self-declares required_capabilities "
                f"{requirement['required_capabilities']} but the requirement-policy map assigns "
                f"{policy[requirement_id]['required_capabilities']}"
            )
    unbound = sorted(set(policy) - sidecar_ids)
    if unbound:
        fail(f"requirement-policy map declares requirements absent from the sidecar: {unbound}")


def cross_check_registry(data: dict, matrix: dict[str, str], policy: dict[str, dict], registry: set[str]) -> None:
    """Exact-set binding: sidecar == plan matrix == policy == §24 registry."""
    data_ids = {requirement["id"] for requirement in data["requirements"]}
    matrix_ids = set(matrix)
    policy_ids = set(policy)
    for label, ids in (("sidecar", data_ids), ("plan matrix", matrix_ids), ("requirement policy", policy_ids)):
        if ids != registry:
            missing = sorted(registry - ids)
            extra = sorted(ids - registry)
            fail(
                f"{label} requirement IDs drift from the §24 registry"
                + (f"; missing from {label}: {missing}" if missing else "")
                + (f"; {label} extra: {extra}" if extra else "")
            )


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
    for requirement in data["requirements"]:
        requirement_id = requirement["id"]
        plan_classification = matrix[requirement_id]
        side_classification = requirement["classification"]
        # The plan matrix and the sidecar must agree on every row exactly: a
        # matrix cell that diverges (e.g. `missing` vs `partial`) lets the
        # human-readable plan and the machine authority drift apart.
        if plan_classification != side_classification:
            fail(
                f"plan row {requirement_id} classification {plan_classification} "
                f"differs from the sidecar classification {side_classification}"
            )


def cross_check_facts(data: dict, facts: dict) -> None:
    """Bind every blocked/partial row to open facts and keep the ledger bidirectional."""
    by_id = {fact["id"]: fact for fact in facts["facts"]}
    sidecar_ids = {requirement["id"] for requirement in data["requirements"]}
    referenced: dict[str, set[str]] = {fact["id"]: set() for fact in facts["facts"]}
    for requirement in data["requirements"]:
        requirement_id = requirement["id"]
        for fact_id in requirement["fact_refs"]:
            if fact_id not in by_id:
                fail(f"requirement {requirement_id} references unknown fact {fact_id}")
            fact = by_id[fact_id]
            if requirement["classification"] in {"blocked", "partial"} and fact["status"] != "open":
                fail(
                    f"requirement {requirement_id} is {requirement['classification']} but its "
                    f"fact {fact_id} is resolved; resolve the row or keep the fact open"
                )
            referenced[fact_id].add(requirement_id)
    for fact in facts["facts"]:
        fact_id = fact["id"]
        listed = set(fact["requirements"])
        unknown = sorted(listed - sidecar_ids)
        if unknown:
            fail(f"fact {fact_id} lists unknown requirements: {unknown}")
        if listed != referenced[fact_id]:
            only_ledger = sorted(listed - referenced[fact_id])
            only_sidecar = sorted(referenced[fact_id] - listed)
            fail(
                f"fact {fact_id} requirements drift with the conformance sidecar"
                + (f"; listed but unreferenced: {only_ledger}" if only_ledger else "")
                + (f"; referenced but unlisted: {only_sidecar}" if only_sidecar else "")
            )
        if not listed and fact["status"] == "open":
            fail(f"open fact {fact_id} is not referenced by any blocked/partial requirement")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("planning", "complete"))
    parser.add_argument("path", nargs="?", default=str(SIDECAR_DEFAULT))
    parser.add_argument("--facts", default=str(FACTS_DEFAULT))
    parser.add_argument("--root", default=str(ROOT))
    args = parser.parse_args()
    root = Path(args.root).resolve()
    data = load_sidecar(root / args.path if not Path(args.path).is_absolute() else Path(args.path))
    facts_path = root / args.facts if not Path(args.facts).is_absolute() else Path(args.facts)
    facts = load_facts(root, facts_path)
    for index, requirement in enumerate(data["requirements"]):
        validate_requirement(requirement, index)
        if any(
            other["id"] == requirement["id"]
            for other in data["requirements"][:index]
        ):
            fail(f"conformance sidecar has duplicate requirement ID: {requirement['id']}")
    plan_text = (root / ".factory/artifacts/implementation-plan.md").read_text(encoding="utf-8")
    matrix = plan_matrix(plan_text)
    if not data["requirements"] and matrix is None:
        # Template state: an empty sidecar with no plan matrix is a valid
        # product-neutral placeholder; nothing can drift.
        print(f"conformance: {args.mode} valid (template, 0 requirements)")
        return 0
    if matrix is None:
        fail("plan has no Specification conformance matrix but the sidecar declares requirements")
    if not data["requirements"]:
        fail("sidecar declares no requirements but the plan has a conformance matrix (drift)")
    registry = load_registry(root)
    policy = load_requirement_policy(root)
    cross_check_registry(data, matrix, policy, registry)
    cross_check_policy(data, policy)
    cross_check(data, matrix)
    cross_check_facts(data, facts)
    for requirement in data["requirements"]:
        if requirement["classification"] == "verified":
            validate_verified_claim(root, requirement)
            # Capability evidence is a real acceptance gate in planning mode
            # too: a `verified` row whose required capability is undeclared or
            # lacks an accepted exact-commit runner receipt fails now, not only
            # at completion.
            capabilities = requirement["required_capabilities"]
            if capabilities:
                check_capability_evidence(root, capabilities)
    if args.mode == "complete":
        validate_complete(root, data, policy)
        blocked = blocked_facts_module(root)
        blocked.check_complete(root, facts)
    print(f"conformance: {args.mode} valid ({len(data['requirements'])} requirements)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
