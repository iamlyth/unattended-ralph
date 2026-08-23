#!/usr/bin/env python3
"""Structured tester/auditor findings authority — Task 10 (FIND-01, §16).

This module implements the §16 findings flow of ``docs/FACTORY-LOOP-SPEC.md``
on top of the committed ``factory-findings-receipt/v1`` and
``factory-findings/v1`` schemas and the hardened no-follow atomic I/O of the
``factory-state/v1`` authority (``state.py`` / ``factory_state_io.py``).

Security contract (every claim fails closed):

* **mint** — the trusted orchestrator (:class:`~campaign.Campaign`) mints one
  *receipt* under the ignored ``.factory-state/`` evidence namespace at the
  exact moment a verification or audit phase classifies ``findings`` or
  ``blocked`` through the deterministic classification functions.  The
  receipt binds the campaign id, the round/phase, the **exact commit at
  which the untrusted phase ran** (``phase_base_commit``), the phase tag
  recorded in the state digest ledger, the SHA-256 digest of the **exact
  structured phase-result bytes the orchestrator read** (``result_digest``),
  the deterministic-gate evidence, and the structured findings/blocked
  references.  Receipt publication is write-once (``no_replace=True``), so a
  pre-planted receipt at a canonical name fails the genuine mint closed.
* **consume** — at the start of the next planning phase the orchestrator
  re-reads the previous round's receipts through the hardened no-follow
  bounded reader, re-validates every receipt against the committed schema,
  and binds every receipt to (a) the current campaign id, (b) the exact
  source round, (c) the recorded phase outcome of this run
  (``phase_records``), (d) the exact recorded phase-base commit, (e) the
  reachability of that commit from the current HEAD, (f) the phase tag in
  the state digest ledger, and (g) the digest of the exact structured
  result bytes this run consumed (``record.result_digest``).  A receipt
  that is missing for a phase that recorded findings/blocked, that exists
  for a phase that recorded ``pass``, or that carries stale/foreign/forged
  bindings — a wrong campaign, round, outcome, phase-base commit, result
  digest, or an unrecorded phase tag — fails closed.
* **deterministic planner input** — the consumed receipts are projected into
  one deterministic ``factory-findings/v1`` payload whose bytes (sorted-key
  canonical JSON, no wall-clock time, no prose claims, no copies of the
  plan) are the digest-bound planner input.  The payload is delivered only
  to the next planner prompt; it is never handed to the developer, never
  read by the deterministic selector (a pure function of plan + state), and
  never stored as a runtime task queue, event stream, memory, or context
  summary.

Blocked references (``blocked_on``) remain structured findings in the
payload — the §13.4/§14 rule that a non-final ``blocked`` advances to the
next planner exactly like ``findings`` with the blocker explicit in the
plan.  The receipts and the derived payload are evidence artifacts under the
ignored ``.factory-state/`` namespace and never orchestration state.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Dict, List, Mapping, Optional, Sequence, Tuple

try:  # package import (the hidden `.factory/loop/` package)
    from . import state as state_module
except ImportError:  # flat import used by the hidden `.factory/tests/` suite
    import state as state_module  # type: ignore[no-redef]

# ---------------------------------------------------------------------------
# Constants and schema loading
# ---------------------------------------------------------------------------

PAYLOAD_SCHEMA_NAME = "factory-findings/v1"
PAYLOAD_SCHEMA_FILE = "factory-findings-v1.schema.json"
RECEIPT_SCHEMA_NAME = "factory-findings-receipt/v1"
RECEIPT_SCHEMA_FILE = "factory-findings-receipt-v1.schema.json"
PHASE_RESULT_SCHEMA_NAME = "factory-phase-result/v1"
PHASE_RESULT_SCHEMA_FILE = "factory-phase-result-v1.schema.json"

RECEIPT_NAME_TEMPLATE = "factory-findings-receipt-round-{round}-{phase}.json"
RESULT_NAME_TEMPLATE = "factory-phase-result-round-{round}-{phase}.json"
RECEIPT_NAME_RE = re.compile(
    r"^factory-findings-receipt-round-([0-9]+)-(verification|audit)\.json$"
)
RESULT_NAME_RE = re.compile(
    r"^factory-phase-result-round-([0-9]+)-(verification|audit)\.json$"
)
MAX_RECEIPT_BYTES = 256 * 1024
MAX_PAYLOAD_BYTES = 512 * 1024
MAX_LEDGER_BYTES = state_module.LEDGER_MAX
MAX_RESULT_BYTES = 256 * 1024

PHASE_TAG_RE = re.compile(r"^r([0-9]+)\.([a-z]+)\.([0-9]+)\.a([0-9]+)$")
SHA40_RE = re.compile(r"^[0-9a-f]{40}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
SAFE_CAMPAIGN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")

FINDING_PHASES = ("verification", "audit")


class FindingsError(Exception):
    """Base class for every fail-closed findings-authority failure."""


class FindingsMalformedError(FindingsError):
    """A receipt/payload artifact is unsafe, oversized, or non-conforming."""


class FindingsStaleError(FindingsError):
    """A receipt is bound to a stale round, campaign, or unreachable commit."""


class FindingsSyntheticError(FindingsError):
    """A receipt exists for a phase that never produced findings (forged)."""


class FindingsForeignError(FindingsError):
    """A receipt belongs to a different campaign or unknown phase run."""


class FindingsReceiptError(FindingsError):
    """A recorded findings/blocked phase is missing its receipt (claim only)."""


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _load_schema(name: str) -> Dict[str, object]:
    here = Path(__file__).resolve().parents[1]  # .factory/
    path = here / "schemas" / name
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise FindingsError(f"cannot load the committed schema {path}: {exc}") from exc
    if len(data) > 256 * 1024:
        raise FindingsError(f"the committed schema {path} is oversized")
    try:
        schema = json.loads(data)
    except ValueError as exc:
        raise FindingsError(f"the committed schema {path} is not JSON") from exc
    if not isinstance(schema, dict):
        raise FindingsError(f"the committed schema {path} is not an object")
    return schema


def _json_type(value: object) -> str:
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    if value is None:
        return "null"
    raise FindingsError(f"value of type {type(value).__name__} is not JSON-serializable")


def _check_instance(instance: object, schema: object, path: str, context: str) -> None:
    """Validate ``instance`` against the JSON-Schema subset the committed
    schemas use (type/enum/pattern/minLength/minimum/minItems/items/properties/
    required/additionalProperties).  Any mismatch fails closed."""
    if not isinstance(schema, dict):
        return
    expected = schema.get("type")
    if expected is not None:
        types = expected if isinstance(expected, list) else [expected]
        if _json_type(instance) not in types:
            raise FindingsMalformedError(
                f"{context} violation at {path or '(root)'}: expected "
                f"{expected!r}, got {_json_type(instance)!r}"
            )
    if "enum" in schema and instance not in schema["enum"]:
        raise FindingsMalformedError(
            f"{context} violation at {path or '(root)'}: value {instance!r} "
            f"is not one of {schema['enum']!r}"
        )
    if isinstance(instance, str):
        if "minLength" in schema and len(instance) < schema["minLength"]:
            raise FindingsMalformedError(
                f"{context} violation at {path or '(root)'}: string below the "
                "minimum length"
            )
        if "pattern" in schema and re.fullmatch(schema["pattern"], instance) is None:
            raise FindingsMalformedError(
                f"{context} violation at {path or '(root)'}: pattern mismatch"
            )
    if isinstance(instance, (int, float)) and not isinstance(instance, bool):
        if "minimum" in schema and instance < schema["minimum"]:
            raise FindingsMalformedError(
                f"{context} violation at {path or '(root)'}: value below minimum"
            )
    if isinstance(instance, list):
        if "minItems" in schema and len(instance) < schema["minItems"]:
            raise FindingsMalformedError(
                f"{context} violation at {path or '(root)'}: array below the "
                "minimum item count"
            )
        if "items" in schema:
            for index, item in enumerate(instance):
                _check_instance(item, schema["items"], f"{path}[{index}]", context)
    if isinstance(instance, dict):
        if "properties" in schema:
            for key, subschema in schema["properties"].items():
                if key in instance:
                    _check_instance(
                        instance[key], subschema, f"{path}.{key}", context
                    )
        if "required" in schema:
            for key in schema["required"]:
                if key not in instance:
                    raise FindingsMalformedError(
                        f"{context} violation at {path or '(root)'}: missing "
                        f"required field {key!r}"
                    )
        if schema.get("additionalProperties") is False:
            declared = set((schema.get("properties") or {}).keys())
            for key in instance:
                if key not in declared:
                    raise FindingsMalformedError(
                        f"{context} violation at {path or '(root)'}: extra "
                        f"field {key!r}"
                    )


_PAYLOAD_SCHEMA: Optional[Dict[str, object]] = None
_RECEIPT_SCHEMA: Optional[Dict[str, object]] = None
_PHASE_RESULT_SCHEMA: Optional[Dict[str, object]] = None


def _payload_schema() -> Dict[str, object]:
    global _PAYLOAD_SCHEMA
    if _PAYLOAD_SCHEMA is None:
        _PAYLOAD_SCHEMA = _load_schema(PAYLOAD_SCHEMA_FILE)
    return _PAYLOAD_SCHEMA


def _receipt_schema() -> Dict[str, object]:
    global _RECEIPT_SCHEMA
    if _RECEIPT_SCHEMA is None:
        _RECEIPT_SCHEMA = _load_schema(RECEIPT_SCHEMA_FILE)
    return _RECEIPT_SCHEMA


def _phase_result_schema() -> Dict[str, object]:
    """The committed ``factory-phase-result/v1`` schema, loaded independently.

    Task 10 review (REQ 3): the findings authority re-validates the exact
    preserved phase-result bytes at consumption with the authoritative
    committed schema — never a self-derived digest or a tolerant re-parse —
    so a tampered receipt whose ``findings``/``blocked_on`` contradict the
    exact result bytes this run consumed fails closed.
    """
    global _PHASE_RESULT_SCHEMA
    if _PHASE_RESULT_SCHEMA is None:
        _PHASE_RESULT_SCHEMA = _load_schema(PHASE_RESULT_SCHEMA_FILE)
    return _PHASE_RESULT_SCHEMA


# ---------------------------------------------------------------------------
# Deterministic canonical bytes
# ---------------------------------------------------------------------------


def receipt_bytes(receipt: Mapping[str, object]) -> bytes:
    """The exact canonical receipt bytes (same bytes the atomic writer stores)."""
    return (
        json.dumps(dict(receipt), sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )
        + b"\n"
    )


def payload_bytes(payload: Mapping[str, object]) -> bytes:
    """The deterministic planner-input bytes (sorted canonical JSON)."""
    return json.dumps(dict(payload), sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )


# ---------------------------------------------------------------------------
# Receipt minting (trusted orchestrator only)
# ---------------------------------------------------------------------------


def receipt_name(round_number: int, phase: str) -> str:
    if (
        isinstance(round_number, bool)
        or not isinstance(round_number, int)
        or round_number < 1
    ):
        raise FindingsError("receipt round must be a positive integer")
    if phase not in FINDING_PHASES:
        raise FindingsError(f"receipt phase must be one of {FINDING_PHASES!r}")
    return RECEIPT_NAME_TEMPLATE.format(round=round_number, phase=phase)


def build_receipt(
    *,
    campaign_id: str,
    round_number: int,
    phase: str,
    phase_tag: str,
    phase_base_commit: str,
    outcome: str,
    result_digest: str,
    findings: Sequence[str] = (),
    blocked_on: Sequence[str] = (),
    gate_ran: bool = False,
    gate_exit: Optional[int] = None,
    capability_ran: bool = False,
    capability_exit: Optional[int] = None,
) -> Dict[str, object]:
    """Build one orchestrator-minted findings receipt (pure, fail-closed).

    Every binding is validated at build time so an invalid receipt can never
    be published: the campaign id, round, phase, phase tag, phase-base
    commit, outcome, and the exact result digest must all be well-formed.
    """
    if not SAFE_CAMPAIGN_ID_RE.fullmatch(campaign_id):
        raise FindingsError(f"unsafe campaign id {campaign_id!r} in a findings receipt")
    if phase not in FINDING_PHASES:
        raise FindingsError(f"unsafe findings phase {phase!r}")
    if outcome not in ("findings", "blocked"):
        raise FindingsError(f"a findings receipt requires outcome findings|blocked, got {outcome!r}")
    if not SHA40_RE.fullmatch(phase_base_commit):
        raise FindingsError("a findings receipt requires a 40-hex phase-base commit")
    if not SHA256_RE.fullmatch(result_digest):
        raise FindingsError("a findings receipt requires a 64-hex result digest")
    if not PHASE_TAG_RE.fullmatch(phase_tag):
        raise FindingsError(f"unsafe phase tag {phase_tag!r} in a findings receipt")
    if not isinstance(gate_ran, bool) or not isinstance(capability_ran, bool):
        raise FindingsError("findings receipt gate flags must be booleans")
    for value in (gate_exit, capability_exit):
        if value is not None and (
            isinstance(value, bool) or not isinstance(value, int) or value < 0
        ):
            raise FindingsError("findings receipt gate exits must be non-negative integers or null")
    findings_list = [str(item) for item in findings]
    blocked_list = [str(item) for item in blocked_on]
    receipt: Dict[str, object] = {
        "schema": RECEIPT_SCHEMA_NAME,
        "campaign_id": campaign_id,
        "round": round_number,
        "phase": phase,
        "phase_tag": phase_tag,
        "phase_base_commit": phase_base_commit,
        "outcome": outcome,
        "result_digest": result_digest,
        "gate_ran": gate_ran,
        "gate_exit": gate_exit,
        "capability_ran": capability_ran,
        "capability_exit": capability_exit,
        "findings": findings_list,
        "blocked_on": blocked_list,
    }
    validate_receipt(receipt)
    return receipt


def validate_receipt(receipt: Mapping[str, object]) -> None:
    """Fail closed unless the receipt conforms to the committed schema."""
    _check_instance(dict(receipt), _receipt_schema(), "", "factory-findings-receipt")
    tag = receipt.get("phase_tag")
    if not isinstance(tag, str) or not PHASE_TAG_RE.fullmatch(tag):
        raise FindingsMalformedError("receipt phase tag is not well-formed")
    match = PHASE_TAG_RE.fullmatch(tag)
    if match and (int(match.group(1)) != receipt.get("round") or match.group(2) != receipt.get("phase")):
        raise FindingsMalformedError(
            "receipt phase tag round/phase prefix does not match the receipt round/phase"
        )


def publish_receipt(root, receipt: Mapping[str, object]) -> str:
    """Atomically publish one findings receipt (write-only, idempotent).

    The receipt is evidence under the ignored ``.factory-state/`` namespace,
    written through the hardened no-follow atomic writer with
    ``no_replace=True``: a pre-planted receipt at the canonical name fails
    the genuine mint closed instead of being silently replaced, so a forged
    receipt cannot masquerade as the orchestrator's mint.

    Task 10 review (REQ 1): publication is **deterministically idempotent**
    across the receipt-mint crash window.  When the canonical receipt
    already exists and its hardened bytes are *exactly equal* to the
    expected canonical bytes of this mint (the previous mint of this same
    run wrote them and the state advance was interrupted), the mint accepts
    the existing receipt and the rerun never wedges.  Any other existing
    content — a tampered, foreign, stale, or replayed receipt — fails
    closed.  Returns the repository-relative receipt name.
    """
    validate_receipt(receipt)
    name = receipt_name(int(receipt["round"]), str(receipt["phase"]))
    expected = receipt_bytes(receipt)
    try:
        state_module.atomic_write_json(
            root, name, dict(receipt), no_replace=True
        )
    except state_module.StateIOError as exc:
        # Crash-window rerun: only a byte-exact re-mint of this same run is
        # accepted idempotently; anything else fails closed.
        try:
            existing = state_module.read_bytes(
                root, name, maximum=MAX_RECEIPT_BYTES, missing_ok=False
            )
        except state_module.StateIOError as read_exc:
            raise FindingsError(
                f"cannot publish the findings receipt {name}: a receipt "
                "already exists and cannot be safely re-read "
                f"({read_exc})"
            ) from read_exc
        if existing == expected:
            return name
        raise FindingsError(
            f"cannot publish the findings receipt {name}: a receipt with "
            "different bytes already exists; a pre-planted, tampered, "
            "foreign, or stale receipt fails closed (only the byte-exact "
            "re-mint of the same run is accepted as crash recovery)"
        ) from exc
    return name


# ---------------------------------------------------------------------------
# Preserved phase-result bytes (the exact content the receipt authenticates)
# ---------------------------------------------------------------------------


def result_name(round_number: int, phase: str) -> str:
    """The canonical preserved phase-result artifact name for a round/phase."""
    if (
        isinstance(round_number, bool)
        or not isinstance(round_number, int)
        or round_number < 1
    ):
        raise FindingsError("phase-result round must be a positive integer")
    if phase not in FINDING_PHASES:
        raise FindingsError(f"phase-result phase must be one of {FINDING_PHASES!r}")
    return RESULT_NAME_TEMPLATE.format(round=round_number, phase=phase)


def preserve_phase_result(
    root, round_number: int, phase: str, raw: bytes
) -> str:
    """Preserve the exact structured phase-result bytes as evidence.

    Task 10 review (REQ 3): the orchestrator preserves the exact bytes of
    the structured result a findings/blocked verification/audit phase
    produced, under the ignored ``.factory-state/`` namespace, so the
    next-round findings authority can authenticate every receipt against
    the exact trusted phase-result bytes (``result_digest`` **and** parsed
    content) instead of only a self-derived digest.  Publication is atomic
    no-replace with the same deterministic idempotency as the receipt
    mint: a byte-exact re-preserve across the crash window is accepted and
    any other pre-existing content fails closed.  Returns the
    repository-relative artifact name.
    """
    name = result_name(round_number, phase)
    if not isinstance(raw, bytes) or len(raw) > MAX_RESULT_BYTES:
        raise FindingsError(
            f"cannot preserve the phase result {name}: the bytes are not a "
            "bounded byte string"
        )
    try:
        state_module.atomic_write(root, name, raw, no_replace=True)
    except state_module.StateIOError as exc:
        try:
            existing = state_module.read_bytes(
                root, name, maximum=MAX_RESULT_BYTES, missing_ok=False
            )
        except state_module.StateIOError as read_exc:
            raise FindingsError(
                f"cannot preserve the phase result {name}: a marker already "
                f"exists and cannot be safely re-read ({read_exc})"
            ) from read_exc
        if existing != raw:
            raise FindingsError(
                f"cannot preserve the phase result {name}: a different marker "
                "already exists; a tampered or foreign phase-result artifact "
                "fails closed"
            ) from exc
    return name


def read_preserved_phase_result(
    root, round_number: int, phase: str
) -> Optional[Tuple[Dict[str, object], str]]:
    """Bounded no-follow read + strict schema validation of the preserved
    phase-result artifact; returns ``(parsed, raw_digest)`` or ``None`` when
    no artifact exists.  Any unsafe, oversized, malformed, or non-conforming
    artifact raises :class:`FindingsMalformedError`.
    """
    name = result_name(round_number, phase)
    try:
        raw = state_module.read_bytes(
            root, name, maximum=MAX_RESULT_BYTES, missing_ok=True
        )
    except state_module.StateIOError as exc:
        raise FindingsMalformedError(
            f"cannot safely read the preserved phase result {name}: {exc}"
        ) from exc
    if raw is None:
        return None
    raw_digest = sha256(raw)
    try:
        data = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise FindingsMalformedError(
            f"the preserved phase result {name} is not valid JSON: {exc}"
        ) from exc
    if not isinstance(data, dict):
        raise FindingsMalformedError(
            f"the preserved phase result {name} is not an object"
        )
    _check_instance(
        data, _phase_result_schema(), "", "factory-phase-result"
    )
    return data, raw_digest


def validate_receipt_content(
    receipt: Mapping[str, object], result: Mapping[str, object], name: str
) -> None:
    """Authenticate the receipt's findings/blocked_on against the exact
    trusted phase-result bytes' parsed content (REQ 3).  A receipt whose
    structured content contradicts the exact result bytes this run consumed
    — the findings list, the blocked references, or the outcome — fails
    closed; never a self-digest-only check."""
    expected_findings = [str(item) for item in result.get("findings", [])]
    expected_blocked = [str(item) for item in result.get("blocked_on", [])]
    if [str(item) for item in receipt.get("findings", [])] != expected_findings:
        raise FindingsSyntheticError(
            f"findings receipt {name} claims findings that contradict the "
            "exact preserved phase-result bytes; a tampered receipt fails "
            "closed"
        )
    if [str(item) for item in receipt.get("blocked_on", [])] != expected_blocked:
        raise FindingsSyntheticError(
            f"findings receipt {name} claims blocked references that "
            "contradict the exact preserved phase-result bytes; a tampered "
            "receipt fails closed"
        )
    if str(result.get("outcome", "")) not in ("findings", "blocked"):
        raise FindingsSyntheticError(
            f"findings receipt {name} binds a preserved phase result whose "
            "outcome is not findings|blocked; a synthetic artifact fails "
            "closed"
        )


# ---------------------------------------------------------------------------
# Receipt consumption (next-round planner input)
# ---------------------------------------------------------------------------


def read_receipt(
    root, round_number: int, phase: str
) -> Optional[Tuple[Dict[str, object], str]]:
    """Bounded no-follow read + schema validation of one findings receipt.

    Returns ``(data, raw_digest)`` or ``None`` when no receipt exists.  Any
    unsafe, oversized, malformed, or non-conforming file raises
    :class:`FindingsMalformedError`.
    """
    name = receipt_name(round_number, phase)
    try:
        raw = state_module.read_bytes(
            root, name, maximum=MAX_RECEIPT_BYTES, missing_ok=True
        )
    except state_module.StateIOError as exc:
        raise FindingsMalformedError(f"cannot safely read the findings receipt {name}: {exc}") from exc
    if raw is None:
        return None
    raw_digest = sha256(raw)
    try:
        data = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise FindingsMalformedError(f"the findings receipt {name} is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise FindingsMalformedError(f"the findings receipt {name} is not an object")
    validate_receipt(data)
    return data, raw_digest


def _ledger_has_tag(root, tag: str) -> bool:
    """True when the authoritative strict state parser recorded ``tag``.

    Task 10 review (REQ 3): the ledger is read only through the hardened
    no-follow bounded reader and the authoritative strict state parser
    (:func:`state.read_phase_digest_ledger`) — never through a tolerant
    per-line re-parse — so a malformed, duplicated, or unsafe ledger line
    fails the findings authority closed instead of being silently ignored.
    """
    try:
        ledger = state_module.read_phase_digest_ledger(root)
    except state_module.StateError as exc:
        raise FindingsError(
            f"cannot read the state digest ledger through the authoritative "
            f"parser: {exc}"
        ) from exc
    return tag in ledger


def _phase_for(records: Sequence, round_number: int, phase: str):
    """The phase-history record for ``(round_number, phase)`` or ``None``."""
    for record in records:
        if getattr(record, "round", None) == round_number and getattr(
            record, "phase", None
        ) == phase:
            return record
    return None


def build_payload(
    *,
    campaign_id: str,
    source_round: int,
    entries: Sequence[Mapping[str, object]],
) -> Dict[str, object]:
    """Assemble the deterministic next-planner findings payload."""
    if not entries:
        raise FindingsError("a findings payload requires at least one entry")
    payload: Dict[str, object] = {
        "schema": PAYLOAD_SCHEMA_NAME,
        "campaign_id": campaign_id,
        "source_round": source_round,
        "entries": [dict(entry) for entry in entries],
    }
    validate_payload(payload)
    return payload


def validate_payload(payload: Mapping[str, object]) -> None:
    """Fail closed unless the payload conforms to the committed schema."""
    _check_instance(dict(payload), _payload_schema(), "", "factory-findings")
    if not isinstance(payload.get("source_round"), int) or isinstance(
        payload.get("source_round"), bool
    ):
        raise FindingsError("payload source_round must be an integer")


def consume_next_round_findings(
    root,
    *,
    campaign_id: str,
    source_round: int,
    head: str,
    is_ancestor,
    phase_records: Sequence,
) -> Optional[bytes]:
    """Derive the deterministic next-planner findings payload bytes.

    Reads the receipts of ``source_round`` for every findings phase, binds
    each to the current campaign, the exact source round, the phase the run
    recorded (``phase_records``), the recorded phase-base commit, the
    reachability of that commit from ``head``, and the state digest ledger's
    phase tag.  Returns ``None`` (no findings flowed to the next planner) or
    the canonical ``factory-findings/v1`` bytes.  Every failure class —
    malformed artifacts, stale/foreign/replayed receipts, synthetic receipts
    for phases that recorded a pass, missing receipts for phases that
    recorded findings/blocked, and receipt-only claims without the required
    structured bindings — fails closed with a dedicated error.
    """
    if (
        isinstance(source_round, bool)
        or not isinstance(source_round, int)
        or source_round < 1
    ):
        raise FindingsError("source_round must be a positive integer")
    if not SHA40_RE.fullmatch(head):
        raise FindingsError("head must be a 40-hex commit")
    entries: List[Dict[str, object]] = []
    for phase in FINDING_PHASES:
        found = read_receipt(root, source_round, phase)
        record = _phase_for(phase_records, source_round, phase)
        if found is None:
            if record is not None and record.outcome in ("findings", "blocked"):
                raise FindingsReceiptError(
                    f"phase round {source_round} {phase} recorded "
                    f"{record.outcome} but its findings receipt is missing; a "
                    "findings claim without receipt evidence fails closed"
                )
            continue
        receipt, raw_digest = found
        if record is None:
            raise FindingsForeignError(
                f"a findings receipt for round {source_round} {phase} exists "
                "but this campaign recorded no such phase; a foreign or "
                "leftover receipt fails closed"
            )
        if record.outcome not in ("findings", "blocked"):
            raise FindingsSyntheticError(
                f"a findings receipt for round {source_round} {phase} exists "
                f"but the phase recorded outcome {record.outcome!r}; a "
                "synthetic receipt for a pass phase fails closed"
            )
        if str(receipt["outcome"]) != str(record.outcome):
            raise FindingsSyntheticError(
                f"findings receipt {receipt_name(source_round, phase)} claims "
                f"outcome {receipt['outcome']!r} but the recorded phase "
                f"classified {record.outcome!r}; a forged receipt fails closed"
            )
        if str(record.head_commit) != str(receipt["phase_base_commit"]):
            raise FindingsSyntheticError(
                f"findings receipt {receipt_name(source_round, phase)} binds "
                f"phase_base_commit {receipt['phase_base_commit']} but the "
                f"recorded phase ran at {record.head_commit}; a forged receipt "
                "fails closed"
            )
        record_result_digest = getattr(record, "result_digest", "") or ""
        if record_result_digest and str(receipt["result_digest"]) != record_result_digest:
            raise FindingsSyntheticError(
                f"findings receipt {receipt_name(source_round, phase)} binds "
                f"result_digest {receipt['result_digest']} but the recorded "
                f"phase consumed the exact result bytes with digest "
                f"{record_result_digest}; a tampered receipt fails closed"
            )
        if str(receipt["campaign_id"]) != campaign_id:
            raise FindingsForeignError(
                f"findings receipt round {source_round}/{phase} belongs to "
                f"campaign {receipt['campaign_id']!r}, not {campaign_id!r}"
            )
        if int(receipt["round"]) != source_round:
            raise FindingsStaleError(
                f"findings receipt round {receipt['round']} does not match "
                f"the source round {source_round}"
            )
        if not is_ancestor(str(receipt["phase_base_commit"]), head):
            raise FindingsStaleError(
                f"findings receipt round {source_round} {phase} binds the "
                f"unreachable phase base {receipt['phase_base_commit']}; a "
                "stale receipt fails closed"
            )
        tag = str(receipt["phase_tag"])
        if not _ledger_has_tag(root, tag):
            raise FindingsSyntheticError(
                f"findings receipt round {source_round} {phase} binds a phase "
                f"tag {tag!r} that the state digest ledger never recorded; a "
                "synthetic receipt fails closed"
            )
        name = receipt_name(source_round, phase)
        # REQ 3: authenticate the receipt against the exact trusted
        # phase-result bytes this run consumed — matching ``result_digest``
        # **and** the parsed content (findings/blocked_on/outcome) — never a
        # self-digest-only check.  The preserved artifact must exist (the
        # orchestrator preserves it before minting), must be byte-exact to
        # the recorded digest, and its parsed content must equal the receipt.
        preserved = read_preserved_phase_result(root, source_round, phase)
        if preserved is None:
            raise FindingsReceiptError(
                f"findings receipt round {source_round} {phase} has no "
                "preserved phase-result artifact; a receipt whose content "
                "cannot be authenticated against the exact trusted result "
                "bytes fails closed"
            )
        result_data, result_raw_digest = preserved
        if str(receipt["result_digest"]) != result_raw_digest:
            raise FindingsSyntheticError(
                f"findings receipt {name} binds result_digest "
                f"{receipt['result_digest']} but the preserved phase-result "
                f"bytes digest to {result_raw_digest}; a tampered receipt or "
                "artifact fails closed"
            )
        if result_raw_digest != record_result_digest:
            raise FindingsSyntheticError(
                f"the preserved phase-result bytes of round {source_round} "
                f"{phase} digest to {result_raw_digest} but the recorded "
                f"phase consumed result digest {record_result_digest}; a "
                "tampered artifact fails closed"
            )
        validate_receipt_content(receipt, result_data, name)
        name = receipt_name(source_round, phase)
        entries.append(
            {
                "phase": str(receipt["phase"]),
                "phase_base_commit": str(receipt["phase_base_commit"]),
                "outcome": str(receipt["outcome"]),
                "phase_tag": tag,
                "result_digest": str(receipt["result_digest"]),
                "receipt_path": name,
                "receipt_digest": raw_digest,
                "findings": [str(item) for item in receipt.get("findings", [])],
                "blocked_on": [str(item) for item in receipt.get("blocked_on", [])],
                "gate_ran": bool(receipt.get("gate_ran", False)),
                "gate_exit": (
                    int(receipt["gate_exit"])
                    if receipt.get("gate_exit") is not None
                    else None
                ),
                "capability_ran": bool(receipt.get("capability_ran", False)),
                "capability_exit": (
                    int(receipt["capability_exit"])
                    if receipt.get("capability_exit") is not None
                    else None
                ),
            }
        )
    if not entries:
        return None
    payload = build_payload(
        campaign_id=campaign_id, source_round=source_round, entries=entries
    )
    raw = payload_bytes(payload)
    if len(raw) > MAX_PAYLOAD_BYTES:
        raise FindingsError("the next-planner findings payload exceeds the bound")
    return raw


if __name__ == "__main__":
    # Never invoked by a model role; the findings authority is consumed only
    # by the trusted campaign orchestrator.
    raise SystemExit("factory-findings: not a standalone entrypoint")
