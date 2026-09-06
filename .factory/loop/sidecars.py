#!/usr/bin/env python3
"""Strict campaign-bound sidecars for pre-round hooks and readiness.

The canonical control-state file ``.factory-state/factory-loop.json`` carries
*exactly* the §11 field set of ``factory-state/v1`` (STATE-01).  The pre-round
hook cursor/digests/results and the round-zero readiness binding/cursor/status
are campaign-bound, coordinator-owned extension data that MUST NOT appear as
fields, phases, or outcomes in canonical state.  They live in two strict
sidecar documents under the ignored ``.factory-state/`` namespace, each with a
committed JSON schema:

* ``pre-round-hooks.json`` (``factory-pre-round-hook-state/v1``) — the exact
  ordered pre-round hook configuration digest, bound commit, chained result
  digest, and the monotonic started/completed round cursors;
* ``readiness.json`` (``factory-readiness-state/v1``) — the round-zero
  readiness applicability, campaign nonce, attempt/cursor/status, accepted
  commit/tree/environment, the five separate result digests, and the terminal
  outcome.

Readiness runs *before* canonical state initialization, so canonical
``current_phase`` is never ``readiness`` and ``current_round`` starts at 1 per
§11.  A readiness-only campaign publishes only the separate readiness result
schema (``factory-readiness-result/v2``) and never initializes canonical
state.

Every sidecar write is atomic, no-follow, and ownership/mode/link-count
checked through the established dirfd authority
``.factory/loop/factory_state_io.py`` (``state_dir``/``read_bytes``/
``atomic_write_json``), exactly like the canonical state file.  Sidecars are
never fields, phases, or outcomes in canonical state; they are evidence of
coordinator-owned extension bookkeeping only.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import re
import stat
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Dict, Mapping, Optional

PRE_ROUND_SCHEMA = "factory-pre-round-hook-state/v1"
READINESS_SCHEMA = "factory-readiness-state/v1"
PRE_ROUND_FILE = "pre-round-hooks.json"
READINESS_FILE = "readiness.json"
SIDECAR_MAX = 16 * 1024

SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
SHA40_RE = re.compile(r"^[0-9a-f]{40}$")
IDENT_RE = re.compile(r"^[a-z0-9](?:[a-z0-9._-]{0,62}[a-z0-9])?$")

# Readiness binding/cursor/status fields (moved verbatim from the former
# ``factory-state/v2`` ``readiness`` field).
READINESS_FIELDS = (
    "required", "nonce", "attempt", "cursor", "status", "accepted_commit",
    "tree", "environment_blob", "specification_sha256", "plan_sha256",
    "conformance_sha256", "policy_sha256", "contracts_sha256",
    "install_manifest_sha256", "command_authority_sha256",
    "human_authority_sha256", "trust_authority_sha256",
    "aggregate_sha256", "capability_result_sha256", "core_result_sha256",
    "conformance_result_sha256", "human_result_sha256", "result_sha256",
    "terminal_outcome",
)


class SidecarError(Exception):
    """A sidecar document was forged, unsafe, or moved."""


class SidecarBindingError(SidecarError):
    """A write-once sidecar binding or expected campaign binding mismatch."""


class SidecarTransitionError(SidecarError):
    """A sidecar cursor/attempt transition violates its monotonic contract."""


def _load_factory_state_io() -> object:
    path = Path(__file__).resolve().parent / "factory_state_io.py"
    spec = importlib.util.spec_from_file_location("factory_state_io", path)
    if spec is None or spec.loader is None:
        raise SidecarError(f"cannot load established state I/O at {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_fio = _load_factory_state_io()
read_bytes = _fio.read_bytes
atomic_write_json = _fio.atomic_write_json
StateIOError = _fio.StateIOError


def _as_root(root) -> Path:
    root = Path(root).absolute()
    info = os.stat(root, follow_symlinks=False)
    if not stat.S_ISDIR(info.st_mode):
        raise SidecarError(f"not a directory: {root}")
    return root


# ---------------------------------------------------------------------------
# Pre-round hook sidecar
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PreRoundHookState:
    """Campaign-bound pre-round hook cursor/digests/results sidecar."""

    schema: str
    campaign_id: str
    configuration_digest: str
    commit: str
    results_digest: str
    started_round: int
    completed_round: int

    def to_dict(self) -> Dict[str, object]:
        return {
            "schema": self.schema,
            "campaign_id": self.campaign_id,
            "configuration_digest": self.configuration_digest,
            "commit": self.commit,
            "results_digest": self.results_digest,
            "started_round": self.started_round,
            "completed_round": self.completed_round,
        }


def empty_pre_round_hooks(
    *, campaign_id: str, configuration_digest: str = "0" * 64,
    commit: str = "0" * 40,
) -> PreRoundHookState:
    """Canonical non-authorizing pre-round hook binding for a fresh campaign."""
    if not IDENT_RE.fullmatch(campaign_id):
        raise SidecarError("campaign id is invalid")
    if not SHA256_RE.fullmatch(configuration_digest):
        raise SidecarError("pre-round configuration digest must be SHA-256")
    if not SHA40_RE.fullmatch(commit):
        raise SidecarError("pre-round commit must be SHA-1")
    return PreRoundHookState(
        schema=PRE_ROUND_SCHEMA, campaign_id=campaign_id,
        configuration_digest=configuration_digest, commit=commit,
        results_digest="0" * 64, started_round=0, completed_round=0,
    )


def _validate_pre_round(value: object) -> None:
    if not isinstance(value, Mapping) or set(value) != {
        "schema", "campaign_id", "configuration_digest", "commit",
        "results_digest", "started_round", "completed_round",
    }:
        raise SidecarError("pre-round sidecar must contain exactly its field set")
    if value.get("schema") != PRE_ROUND_SCHEMA:
        raise SidecarError(f"pre-round sidecar schema must be {PRE_ROUND_SCHEMA!r}")
    if not isinstance(value.get("campaign_id"), str) or not IDENT_RE.fullmatch(
        str(value["campaign_id"])
    ):
        raise SidecarError("pre-round sidecar campaign id is invalid")
    for name in ("configuration_digest", "results_digest"):
        if not isinstance(value.get(name), str) or not SHA256_RE.fullmatch(
            str(value[name])
        ):
            raise SidecarError(f"pre-round {name} must be SHA-256")
    if not isinstance(value.get("commit"), str) or not SHA40_RE.fullmatch(
        str(value["commit"])
    ):
        raise SidecarError("pre-round commit must be SHA-1")
    for name in ("started_round", "completed_round"):
        if isinstance(value.get(name), bool) or not isinstance(
            value.get(name), int
        ) or int(value[name]) < 0:
            raise SidecarError(f"pre-round {name} must be a non-negative integer")
    if int(value["completed_round"]) > int(value["started_round"]):
        raise SidecarError("pre-round completion cannot precede its start")
    if int(value["started_round"]) - int(value["completed_round"]) > 1:
        raise SidecarError("pre-round cursor may have at most one ambiguous round")


def parse_pre_round(data: object) -> PreRoundHookState:
    if not isinstance(data, dict):
        raise SidecarError("pre-round sidecar must be a JSON object")
    _validate_pre_round(data)
    return PreRoundHookState(
        schema=str(data["schema"]), campaign_id=str(data["campaign_id"]),
        configuration_digest=str(data["configuration_digest"]),
        commit=str(data["commit"]), results_digest=str(data["results_digest"]),
        started_round=int(data["started_round"]),
        completed_round=int(data["completed_round"]),
    )


def read_pre_round(root, *, expected_campaign_id: Optional[str] = None) -> PreRoundHookState:
    """Securely reopen and validate the pre-round hook sidecar."""
    root = _as_root(root)
    try:
        data = read_bytes(root, PRE_ROUND_FILE, maximum=SIDECAR_MAX)
    except StateIOError as exc:
        raise SidecarError(f"cannot read pre-round sidecar: {exc}") from exc
    try:
        state = parse_pre_round(json.loads(data.decode("utf-8")))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise SidecarError(f"pre-round sidecar is malformed: {exc}") from exc
    if expected_campaign_id is not None and state.campaign_id != expected_campaign_id:
        raise SidecarBindingError(
            "pre-round sidecar campaign id does not match the campaign binding"
        )
    return state


def write_pre_round(root, state: PreRoundHookState) -> None:
    root = _as_root(root)
    _validate_pre_round(state.to_dict())
    try:
        atomic_write_json(root, PRE_ROUND_FILE, state.to_dict())
    except StateIOError as exc:
        raise SidecarError(f"cannot write pre-round sidecar: {exc}") from exc


def begin_pre_round_hooks(
    state: PreRoundHookState, *, current_round: int
) -> PreRoundHookState:
    """Durably claim this round before any hook side effect can occur.

    Recovery never re-executes an ambiguous claimed round.  A caller that
    observes ``started == current_round > completed`` must terminate the
    campaign for operator review.
    """
    _validate_pre_round(state.to_dict())
    if state.completed_round == current_round:
        raise SidecarTransitionError("pre-round hooks already completed this round")
    expected_previous = current_round - 1
    if (
        state.started_round != expected_previous
        or state.completed_round != expected_previous
    ):
        raise SidecarTransitionError("pre-round hook cursor is ambiguous or rewound")
    return replace(state, started_round=current_round)


def complete_pre_round_hooks(
    state: PreRoundHookState, chained_result_digest: str, *, current_round: int
) -> PreRoundHookState:
    """Bind the canonical ordered result chain before the planner starts."""
    _validate_pre_round(state.to_dict())
    if state.started_round != current_round:
        raise SidecarTransitionError("pre-round hooks were not claimed for this round")
    if state.completed_round != current_round - 1:
        raise SidecarTransitionError("pre-round hook completion cursor is not monotonic")
    if not isinstance(chained_result_digest, str) or not SHA256_RE.fullmatch(
        chained_result_digest
    ):
        raise SidecarError("pre-round result chain must be a SHA-256 digest")
    return replace(
        state, results_digest=chained_result_digest, completed_round=current_round
    )


# ---------------------------------------------------------------------------
# Readiness sidecar
# ---------------------------------------------------------------------------


def empty_readiness(*, required: bool = False) -> Dict[str, object]:
    """Canonical non-authorizing readiness binding for a fresh campaign."""
    return {
        "required": required, "nonce": "0" * 64, "attempt": 0, "cursor": 0,
        "status": "pending" if required else "not_required",
        "accepted_commit": "0" * 40, "tree": "0" * 40,
        "environment_blob": "0" * 40,
        **{name: "0" * 64 for name in READINESS_FIELDS if name.endswith("_sha256")},
        "terminal_outcome": "pending" if required else "not_required",
    }


def _validate_readiness(value: object, *, required: bool) -> None:
    if not isinstance(value, Mapping) or set(value) != set(READINESS_FIELDS):
        raise SidecarError("readiness sidecar must contain exactly the readiness fields")
    if type(value.get("required")) is not bool or bool(value["required"]) != required:
        raise SidecarError("readiness required binding is inconsistent")
    if type(value.get("attempt")) is not int or int(value["attempt"]) < 0:
        raise SidecarError("readiness attempt must be non-negative")
    if type(value.get("cursor")) is not int or not 0 <= int(value["cursor"]) <= 6:
        raise SidecarError("readiness cursor is invalid")
    if value.get("status") not in {"not_required", "pending", "acquiring", "complete", "findings", "human_blocked", "infrastructure_failure"}:
        raise SidecarError("readiness status is invalid")
    if value.get("terminal_outcome") not in {"not_required", "pending", "pass", "findings", "blocked", "infrastructure_failure"}:
        raise SidecarError("readiness terminal outcome is invalid")
    for name in ("accepted_commit", "tree", "environment_blob"):
        if not isinstance(value.get(name), str) or not SHA40_RE.fullmatch(str(value[name])):
            raise SidecarError(f"readiness {name} must be exact SHA-1")
    if not isinstance(value.get("nonce"), str) or not SHA256_RE.fullmatch(str(value["nonce"])):
        raise SidecarError("readiness nonce must be SHA-256")
    for name in READINESS_FIELDS:
        if name.endswith("_sha256") and (not isinstance(value.get(name), str) or not SHA256_RE.fullmatch(str(value[name]))):
            raise SidecarError(f"readiness {name} must be SHA-256")
    if required and value["nonce"] == "0" * 64:
        raise SidecarError("required readiness must have a campaign nonce")
    if value["status"] == "complete":
        required_digests = (
            "aggregate_sha256", "capability_result_sha256", "core_result_sha256",
            "conformance_result_sha256", "human_result_sha256", "result_sha256",
        )
        if value["terminal_outcome"] != "pass" or value["cursor"] != 6 or any(value[name] == "0" * 64 for name in required_digests):
            raise SidecarError("completed readiness lacks every nonzero bound result")


@dataclass(frozen=True)
class ReadinessState:
    """Campaign-bound round-zero readiness binding/cursor/status sidecar."""

    schema: str
    campaign_id: str
    readiness: Mapping[str, object]

    def to_dict(self) -> Dict[str, object]:
        return {
            "schema": self.schema,
            "campaign_id": self.campaign_id,
            "readiness": dict(self.readiness),
        }


def parse_readiness(data: object) -> ReadinessState:
    if not isinstance(data, dict) or set(data) != {"schema", "campaign_id", "readiness"}:
        raise SidecarError("readiness sidecar must contain exactly its field set")
    if data.get("schema") != READINESS_SCHEMA:
        raise SidecarError(f"readiness sidecar schema must be {READINESS_SCHEMA!r}")
    if not isinstance(data.get("campaign_id"), str) or not IDENT_RE.fullmatch(
        str(data["campaign_id"])
    ):
        raise SidecarError("readiness sidecar campaign id is invalid")
    readiness = data.get("readiness")
    if not isinstance(readiness, dict):
        raise SidecarError("readiness sidecar readiness must be an object")
    _validate_readiness(readiness, required=bool(readiness.get("required")))
    return ReadinessState(
        schema=str(data["schema"]), campaign_id=str(data["campaign_id"]),
        readiness=dict(readiness),
    )


def read_readiness(root, *, expected_campaign_id: Optional[str] = None) -> ReadinessState:
    """Securely reopen and validate the readiness sidecar."""
    root = _as_root(root)
    try:
        data = read_bytes(root, READINESS_FILE, maximum=SIDECAR_MAX)
    except StateIOError as exc:
        raise SidecarError(f"cannot read readiness sidecar: {exc}") from exc
    try:
        state = parse_readiness(json.loads(data.decode("utf-8")))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise SidecarError(f"readiness sidecar is malformed: {exc}") from exc
    if expected_campaign_id is not None and state.campaign_id != expected_campaign_id:
        raise SidecarBindingError(
            "readiness sidecar campaign id does not match the campaign binding"
        )
    return state


def write_readiness(root, state: ReadinessState) -> None:
    root = _as_root(root)
    parse_readiness(state.to_dict())
    try:
        atomic_write_json(root, READINESS_FILE, state.to_dict())
    except StateIOError as exc:
        raise SidecarError(f"cannot write readiness sidecar: {exc}") from exc


def update_readiness(
    state: ReadinessState, readiness: Mapping[str, object]
) -> ReadinessState:
    """Durably advance the trusted round-zero readiness cursor."""
    parse_readiness(state.to_dict())
    previous = state.readiness
    if int(readiness.get("attempt", -1)) < int(previous["attempt"]) or int(readiness.get("cursor", -1)) < int(previous["cursor"]):
        raise SidecarTransitionError("readiness attempt/cursor may not rewind")
    for name in ("required", "nonce", "accepted_commit", "tree", "environment_blob", "specification_sha256", "plan_sha256", "conformance_sha256", "policy_sha256", "contracts_sha256", "install_manifest_sha256", "command_authority_sha256", "human_authority_sha256", "trust_authority_sha256"):
        if readiness.get(name) != previous.get(name):
            raise SidecarBindingError(f"readiness binding `{name}` may not change")
    return ReadinessState(
        schema=state.schema, campaign_id=state.campaign_id,
        readiness=dict(readiness),
    )
