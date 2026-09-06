#!/usr/bin/env python3
"""Single mutable control-state authority: ``factory-state/v2`` (STATE-01).

This module implements the one minimal mutable control-state file
``.factory-state/factory-loop.json`` specified by FACTORY-LOOP-SPEC § 11 and
its § 11 transition table.  It is the deterministic state authority of the
trusted control plane (Task 4); the runtime file lives outside Git under the
ignored ``.factory-state/`` namespace and carries *exactly* the § 11 field
set:

``schema``, ``repository_identity``, ``branch``, ``campaign_id``,
``rounds_requested``, ``current_round`` (monotonic, 1-based), ``current_phase``,
``specification_digest``, ``plan_digest``, ``role_prompt_digests`` and
``audit_objectives_digest`` (bound at campaign start), ``phase_base_commit``,
``selected_task_id`` (if any), ``attempt_number`` (monotonic within the current
task, reset to zero only on a trusted task/phase transition),
``phase_started_at_monotonic`` and ``attempt_started_at_monotonic`` (monotonic
time for timeout recovery), and ``last_outcome`` (a trusted control-plane
enum, never an evidence claim).  No wall-clock timestamp and no additional
field is accepted: parsing rejects both extra and missing fields.

The § 11 transition table is enforced as exactly this edge set (outcome →
target phase; a *terminal* target ends the campaign and accepts no further
transition):

::

    planning       planned              -> implementation
    planning       failed               -> failed            (terminal)
    planning       interrupted          -> interrupted       (terminal)
    implementation task_completed       -> verification
    implementation work_exhausted       -> verification
    implementation blocked              -> verification
    implementation task_failed          -> verification
    implementation interrupted          -> interrupted       (terminal)
    verification   pass                 -> audit
    verification   findings             -> audit
    verification   blocked              -> audit
    audit          pass | findings | blocked -> planning(next round)
                                      when current_round < rounds_requested
    audit          pass                 -> success            (final, terminal)
    audit          findings             -> findings           (final, terminal)
    audit          blocked              -> blocked            (final, terminal)
    audit          interrupted          -> interrupted        (terminal)
    audit          infrastructure_failure -> infrastructure_failure (terminal)

An interrupted audit and an untrusted audit (``infrastructure_failure``)
are the two terminal fail-closed closes that have no nonfinal ``audit --
planning(next round)`` edge: they always end the campaign (Task 9, review
B1), never advance the round, and are persisted in the authoritative
control state so a later run refuses to re-execute them.

Round advances only on ``audit --nonfinal``; phase never moves backwards
within a round; counters are monotonic.  Retry bookkeeping (an interrupted
planning phase or an implementation ``task_progress``/``task_failed``/
``interrupted`` retry whose attempt budget remains) is a trusted
:func:`record_retry` that updates ``last_outcome`` without claiming a phase
transition; attempts begin through :func:`begin_attempt`, which increments
``attempt_number`` within the current task and resets it on a trusted task
transition.

Campaign-scoped bindings (``schema``, ``repository_identity``, ``branch``,
``campaign_id``, ``rounds_requested``, ``specification_digest``,
``role_prompt_digests``, ``audit_objectives_digest``) are write-once: only
:func:`init_state` may establish them and no transition may change them.
``plan_digest`` and ``phase_base_commit`` bind a completed planning phase and
are write-once until the next trusted ``planning -> implementation``
transition.  ``plan_digest`` is the SHA-256 of the exact bytes of the
committed ``factory-plan/v1`` plan document (the canonical
``.factory/artifacts/implementation-plan.md`` at the bound
``phase_base_commit``) that the deterministic plan parser accepted; it is
bound only by the trusted ``planning -> implementation`` edge and is
write-once until the next such edge of the next round (Task 19 S8).

Crash-window and orphan recovery (Task 19 S2) is deterministic and explicit:
:func:`recover_state` restores the single authoritative state from the last
validated quarantine when a torn atomic update left no canonical file (never
creating a second authority), removes validated orphaned temporaries from a
torn write that never published, and fails closed on ambiguous or unsafe
leftover artifacts; :func:`init_state` runs recovery first and then publishes
atomically with no-replace semantics (Task 19 S1), so a campaign never
clobbers existing state or an existing campaign binding.  Monotonic markers
are strictly positive (a zeroed ``now=0`` epoch marker is rejected as tamper,
Task 19 S3), and an active attempt can never precede the phase that owns it
(``attempt_started_at_monotonic >= phase_started_at_monotonic``, S9).

All file I/O reuses the established dirfd/no-follow authority
``scripts/factory_state_io.py``: atomic publication through a mode-0600
temporary file and ``linkat``, with ownership/mode/link-count and
(dev, inode) identity checks on every open/read/update.  Loading additionally
re-validates the recorded ``repository_identity`` against the canonical root
directory descriptor, so a control-state file moved between repositories (or
a forged identity) fails closed, and the optional expected campaign binding
(branch, campaign id, digests, round budget) makes a forged binding fail
closed.

The harness records the state digest before every untrusted phase in an
append-only evidence ledger (``.factory-state/state-digest-ledger.jsonl``,
never orchestration state) and reopens and re-validates the file after the
phase; any same-UID mutation of content, mode, owner, link-count, or
pathname identity that was not produced by the trusted transition fails
closed (:meth:`record_phase_digest` / :meth:`verify_phase_digest`).
``state_digest`` is a deterministic SHA-256 of the canonical JSON encoding of
the state model, so any semantic mutation (forged field, rewound counter,
changed binding or phase) changes the digest; file-metadata mutations are
caught by the no-follow identity checks.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
from dataclasses import dataclass, field, replace
from pathlib import Path
import re
import stat
import sys
import time
from typing import Callable, Dict, List, Mapping, Optional, Tuple

SCHEMA_NAME = "factory-state/v2"
LEGACY_SCHEMA_NAME = "factory-state/v1"
STATE_FILE_NAME = "factory-loop.json"
DIGEST_LEDGER_NAME = "state-digest-ledger.jsonl"

# One mutable control-state file plus one append-only evidence ledger; the
# ledger is never orchestration state (FACTORY-LOOP-SPEC §11).
STATE_FILE_MAX = 16 * 1024
LEDGER_MAX = 1024 * 1024

# §11 phases: the four lifecycle phases and the terminal campaign states of
# the §11 transition table.
PHASES = ("readiness", "planning", "implementation", "verification", "audit")
TERMINAL_PHASES = (
    "success", "findings", "blocked", "failed", "interrupted",
    "infrastructure_failure",
)
PHASE_VALUES = PHASES + TERMINAL_PHASES

# Trusted control-plane outcome enum (§13). ``last_outcome`` is exactly one
# of these values (or ``None`` before the first trusted outcome); it is set by
# the trusted harness, never by model output.
OUTCOMES = (
    "planned", "failed", "interrupted",
    "task_completed", "task_progress", "task_failed",
    "work_exhausted", "blocked",
    "pass", "findings", "infrastructure_failure",
    "success",
)

# The §11 transition table: (source phase, trusted outcome) -> target phase.
# The ``audit`` rows depend on round finality and are handled explicitly in
# ``advance``; they are listed here only for documentation and for the
# machine-readable table probe.
TRANSITIONS: Dict[Tuple[str, str], str] = {
    ("readiness", "pass"): "planning",
    ("readiness", "findings"): "findings",
    ("readiness", "blocked"): "blocked",
    ("readiness", "infrastructure_failure"): "infrastructure_failure",
    ("planning", "planned"): "implementation",
    ("planning", "failed"): "failed",
    ("planning", "interrupted"): "interrupted",
    ("planning", "infrastructure_failure"): "infrastructure_failure",
    ("implementation", "task_completed"): "verification",
    ("implementation", "work_exhausted"): "verification",
    ("implementation", "blocked"): "verification",
    ("implementation", "task_failed"): "verification",
    ("implementation", "interrupted"): "interrupted",
    ("verification", "pass"): "audit",
    ("verification", "findings"): "audit",
    ("verification", "blocked"): "audit",
    ("verification", "infrastructure_failure"): "infrastructure_failure",
    ("audit", "interrupted"): "interrupted",
    ("audit", "infrastructure_failure"): "infrastructure_failure",
}
# ``audit --final--> success | findings | blocked``; the same outcomes advance
# to the next round's planning when the audit is non-final.  The two terminal
# abort outcomes (``interrupted`` / ``infrastructure_failure``) always end the
# campaign and never advance the round (Task 9 review B1).
AUDIT_FINAL_TARGETS = {"pass": "success", "findings": "findings", "blocked": "blocked"}
AUDIT_ABORT_TARGETS = {
    "interrupted": "interrupted",
    "infrastructure_failure": "infrastructure_failure",
}

# Outcomes that retry the same phase/attempt without claiming a §11 phase
# transition (their attempt budget remains): a planning step interrupted
# before a valid checkpoint (§13.1) and implementation attempts that retry
# the same plan task (§13.2).
RETRY_OUTCOMES: Dict[str, Tuple[str, ...]] = {
    "planning": ("interrupted",),
    "implementation": ("task_progress", "task_failed", "interrupted"),
}

# §13 outcomes that may legitimately be recorded in a *persisted* state of
# each live phase, derived edge-for-edge from the §11 transition table, the
# retry table, and ``init`` (Task 19 S9): ``advance`` records the outcome of
# the completed step for a non-terminal target, ``record_retry`` records the
# retry outcome without a phase change, and ``init`` records ``None``.  A
# ``last_outcome`` outside the owning phase's set is a forged phase/outcome
# combination and fails closed.
PHASE_OUTCOMES: Dict[str, frozenset] = {
    "readiness": frozenset({"pass"}),
    "planning": frozenset({"interrupted", "pass", "findings", "blocked"}),
    "implementation": frozenset(
        {"planned", "task_progress", "task_failed", "interrupted"}
    ),
    "verification": frozenset(
        {"task_completed", "work_exhausted", "blocked", "task_failed"}
    ),
    "audit": frozenset({"pass", "findings", "blocked"}),
}

# Campaign-scoped fields bound once by ``init`` and immutable afterwards.
BINDING_FIELDS = (
    "schema", "repository_identity", "branch", "campaign_id",
    "rounds_requested", "specification_digest", "role_prompt_digests",
    "audit_objectives_digest", "pre_round_hook_configuration_digest",
    "pre_round_hook_commit",
)
FIELD_NAMES: Tuple[str, ...] = (
    "schema", "repository_identity", "branch", "campaign_id",
    "rounds_requested", "current_round", "current_phase",
    "specification_digest", "plan_digest", "role_prompt_digests",
    "audit_objectives_digest", "pre_round_hook_configuration_digest",
    "pre_round_hook_commit", "pre_round_hook_results_digest", "pre_round_hook_started_round",
    "pre_round_hook_completed_round", "phase_base_commit", "selected_task_id",
    "attempt_number", "phase_started_at_monotonic",
    "attempt_started_at_monotonic", "last_outcome", "readiness",
)

SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
SHA40_RE = re.compile(r"^[0-9a-f]{40}$")
IDENTITY_RE = re.compile(r"^[0-9a-f]+:[0-9a-f]+$")
SAFE_TAG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
# Orphaned artifacts of the established atomic writer (Task 19 S2): a mode-0600
# temporary ``.{name}.{32-hex}`` from a write that never published and a
# quarantined ``.{name}.quarantine-{32-hex}`` copy of the last validated
# state from an interrupted update.  Only these exact shapes are ever
# recognized by recovery; any other name is preserved untouched.
TEMP_ORPHAN_RE = re.compile(rf"^\.{re.escape(STATE_FILE_NAME)}\.[0-9a-f]{{32}}$")
QUARANTINE_ORPHAN_RE = re.compile(
    rf"^\.{re.escape(STATE_FILE_NAME)}\.quarantine-[0-9a-f]{{32}}$"
)

READINESS_FIELDS = (
    "required", "nonce", "attempt", "cursor", "status", "accepted_commit",
    "tree", "environment_blob", "specification_sha256", "plan_sha256",
    "conformance_sha256", "policy_sha256", "contracts_sha256",
    "install_manifest_sha256", "command_authority_sha256",
    "human_authority_sha256", "trust_authority_sha256",
    "aggregate_sha256", "capability_result_sha256", "core_result_sha256",
    "conformance_result_sha256", "human_result_sha256", "result_sha256", "terminal_outcome",
)


def empty_readiness(*, required: bool = False) -> Dict[str, object]:
    """Canonical non-authorizing readiness binding for legacy/non-production state."""
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
        raise StateTamperError("`readiness` must contain exactly the readiness binding fields")
    if type(value.get("required")) is not bool or bool(value["required"]) != required:
        raise StateTamperError("readiness required binding is inconsistent")
    if type(value.get("attempt")) is not int or int(value["attempt"]) < 0:
        raise StateTamperError("readiness attempt must be non-negative")
    if type(value.get("cursor")) is not int or not 0 <= int(value["cursor"]) <= 6:
        raise StateTamperError("readiness cursor is invalid")
    if value.get("status") not in {"not_required", "pending", "acquiring", "complete", "findings", "human_blocked", "infrastructure_failure"}:
        raise StateTamperError("readiness status is invalid")
    if value.get("terminal_outcome") not in {"not_required", "pending", "pass", "findings", "blocked", "infrastructure_failure"}:
        raise StateTamperError("readiness terminal outcome is invalid")
    for name in ("accepted_commit", "tree", "environment_blob"):
        if not isinstance(value.get(name), str) or not SHA40_RE.fullmatch(str(value[name])):
            raise StateTamperError(f"readiness {name} must be exact SHA-1")
    if not isinstance(value.get("nonce"), str) or not SHA256_RE.fullmatch(str(value["nonce"])):
        raise StateTamperError("readiness nonce must be SHA-256")
    for name in READINESS_FIELDS:
        if name.endswith("_sha256") and (not isinstance(value.get(name), str) or not SHA256_RE.fullmatch(str(value[name]))):
            raise StateTamperError(f"readiness {name} must be SHA-256")
    if required and value["nonce"] == "0" * 64:
        raise StateTamperError("required readiness must have a campaign nonce")
    if value["status"] == "complete":
        required_digests = (
            "aggregate_sha256", "capability_result_sha256", "core_result_sha256",
            "conformance_result_sha256", "human_result_sha256", "result_sha256",
        )
        if value["terminal_outcome"] != "pass" or value["cursor"] != 6 or any(value[name] == "0" * 64 for name in required_digests):
            raise StateTamperError("completed readiness lacks every nonzero bound result")


def update_readiness(state: "FactoryState", readiness: Mapping[str, object]) -> "FactoryState":
    """Durably advance the trusted round-zero readiness cursor."""
    state.validate()
    if state.current_phase != "readiness":
        raise StateTransitionError("readiness updates are accepted only at round zero")
    previous = state.readiness
    if int(readiness.get("attempt", -1)) < int(previous["attempt"]) or int(readiness.get("cursor", -1)) < int(previous["cursor"]):
        raise StateTransitionError("readiness attempt/cursor may not rewind")
    for name in ("required", "nonce", "accepted_commit", "tree", "environment_blob", "specification_sha256", "plan_sha256", "conformance_sha256", "policy_sha256", "contracts_sha256", "install_manifest_sha256", "command_authority_sha256", "human_authority_sha256", "trust_authority_sha256"):
        if readiness.get(name) != previous.get(name):
            raise StateBindingError(f"readiness binding `{name}` may not change")
    result = replace(state, readiness=dict(readiness))
    result.validate()
    return result

# Deterministic internal race hooks (Task 19 hardening): module-private,
# one-shot, and unreachable from the trusted CLI.  Production never sets
# them; the hidden conformance suite installs a hook to force the exact race
# branch that a live crash cannot reproduce deterministically.  Each hook is
# consumed (read and cleared) immediately before the operation it perturbs,
# so a stale hook can never fire on a later unrelated call, and a hook that
# fired while its operation raised leaves no residue behind.
#
#   ``_RACE_BEFORE_RESTORE_LINK(root, directory_fd)`` — called inside the
#   restore dirfd scope immediately before recovery's no-replace ``linkat``
#   publishes the canonical name; a hook that creates the canonical name
#   forces the ``FileExistsError`` raced-canonical fail-closed branch.
#
#   ``_RACE_AFTER_RESTORE_LINK(root, directory_fd)`` — called inside the
#   restore dirfd scope immediately after the ``linkat``; a hook that
#   replaces the canonical name or the quarantine forces the substituted
#   canonical/quarantine re-validation and identity fail-closed branches.
#
#   ``_LEDGER_RACE_AFTER_DUP_CHECK(root, directory_fd)`` — called inside the
#   single validated ledger scope between the duplicate-tag read and the
#   append; a hook that swaps or creates the ledger forces the
#   ledger-identity fail-closed branches of ``record_phase_digest``, and a
#   hook that appends the same tag to the same inode forces the
#   concurrent-duplicate fail-closed branch (at most one same-tag writer
#   succeeds and the ledger stays valid, hardening L5).
_RACE_BEFORE_RESTORE_LINK: Optional[Callable[[Path, int], None]] = None
_RACE_AFTER_RESTORE_LINK: Optional[Callable[[Path, int], None]] = None
_LEDGER_RACE_AFTER_DUP_CHECK: Optional[Callable[[Path, int], None]] = None


class StateError(Exception):
    """Base class for every fail-closed control-state failure."""


class StateTamperError(StateError):
    """The control-state file or its contents were forged, unsafe, or moved."""


class StateTransitionError(StateError):
    """A transition, retry, or attempt violates the §11 transition table."""


class StateBindingError(StateError):
    """A write-once binding or an expected campaign binding mismatch."""


class StateDigestError(StateError):
    """The before/after phase digest ledger is missing, forged, or mismatched."""


def _load_factory_state_io() -> object:
    """Load the established dirfd/no-follow state I/O utility.

    ``scripts/factory_state_io.py`` is the committed authority for safe
    lifecycle-marker I/O (atomic no-follow writes, ownership/mode/link-count
    checks, bounded reads) and is deliberately reused rather than copied.
    """
    path = (
        Path(__file__).resolve().parent.parent.parent
        / "scripts" / "factory_state_io.py"
    )
    spec = importlib.util.spec_from_file_location("factory_state_io", path)
    if spec is None or spec.loader is None:
        raise StateError(f"cannot load established state I/O at {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_gitutil() -> object:
    """Load the PATH-pinned Git executable runner (GIT-01).

    ``gitutil.py`` pins an absolute Git executable at import time and
    sanitizes the invocation environment; every trusted state/branch Git
    call in this module goes through it, so an attacker-controlled PATH can
    never substitute a different ``git`` behind the guarded commit boundary
    (FACTORY-LOOP-SPEC §12).  It is loaded by committed file path (the same
    idiom as :func:`_load_factory_state_io`) so the module works both when
    imported directly from the hidden test suite and as a package member.
    The import-time resolution failure of the pinned Git executable
    (``GitBoundaryError``) is routed into the state contract as
    :class:`StateError`, so no caller of this module ever sees a bare
    ``GitBoundaryError`` escape.
    """
    path = Path(__file__).resolve().parent / "gitutil.py"
    spec = importlib.util.spec_from_file_location("gitutil", path)
    if spec is None or spec.loader is None:
        raise StateError(f"cannot load PATH-pinned Git runner at {path}")
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except Exception as exc:
        raise StateError(
            f"cannot initialize the PATH-pinned Git runner at {path}: {exc}"
        ) from exc
    return module


_git = _load_gitutil()
_fio = _load_factory_state_io()
read_json = _fio.read_json
read_bytes = _fio.read_bytes
atomic_write = _fio.atomic_write
atomic_write_text = _fio.atomic_write_text
atomic_write_json = _fio.atomic_write_json
campaign_state_directory = _fio.campaign_state_directory
StateIOError = _fio.StateIOError


def _as_root(root) -> Path:
    root = Path(root).absolute()
    info = os.stat(root, follow_symlinks=False)
    if not stat.S_ISDIR(info.st_mode):
        raise StateError(f"not a directory: {root}")
    return root


def repository_identity(root) -> str:
    """Deterministic repository identity of the canonical root directory.

    The identity is the ``dev:inode`` pair of the root directory opened
    with ``O_DIRECTORY`` and ``O_NOFOLLOW`` (and close-on-exec so the
    descriptor is never inherited across an exec boundary), so a
    control-state file moved to a different checkout (different pathname or
    inode) fails closed on load.
    """
    root = _as_root(root)
    if sys.platform != "linux" or not hasattr(os, "O_NOFOLLOW"):
        raise StateError("required Linux no-follow primitives are unavailable")
    descriptor = os.open(
        root,
        os.O_RDONLY
        | os.O_DIRECTORY
        | os.O_NOFOLLOW
        | getattr(os, "O_CLOEXEC", 0),
    )
    try:
        info = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    return f"{info.st_dev:x}:{info.st_ino:x}"


def live_branch(root) -> str:
    """The live Git branch of ``root`` (used by ``init`` when none is given).

    The branch is resolved through the PATH-pinned absolute Git executable
    (``gitutil.GIT_EXECUTABLE``), never through an unqualified ``git`` that a
    caller-controlled PATH could substitute (GIT-01).  Every trusted
    invocation is finite-bounded (Task 9 review MED): the pinned Git
    executable may never wait forever, so the call passes
    ``gitutil.GIT_TIMEOUT``, and a pinned-runner failure (``GitBoundaryError``
    — timeout, missing binary, broken pipe) is routed into the state contract
    as :class:`StateError`, so no caller sees a bare ``GitBoundaryError``
    escape.
    """
    try:
        result = _git.git_run(
            ["-C", str(_as_root(root)), "rev-parse", "--abbrev-ref", "HEAD"],
            timeout=_git.GIT_TIMEOUT,
        )
    except _git.GitBoundaryError as exc:
        raise StateError(
            f"the pinned Git runner failed resolving the live branch of "
            f"{root}: {exc}"
        ) from exc
    if result.returncode != 0:
        raise StateError(f"cannot resolve the live Git branch of {root}")
    branch = result.stdout.strip()
    if not branch or branch == "HEAD":
        raise StateError(f"live Git branch of {root} is not a named branch")
    return branch


@dataclass(frozen=True)
class FactoryState:
    """Immutable ``factory-state/v1`` model.

    The dataclass is frozen so the transition helpers return new instances
    and can never mutate an existing state in place; equality is therefore
    deterministic (used by the hidden conformance suite to prove that a
    trusted transition produces exactly the expected state).
    """

    schema: str
    repository_identity: str
    branch: str
    campaign_id: str
    rounds_requested: int
    current_round: int
    current_phase: str
    specification_digest: str
    plan_digest: str
    role_prompt_digests: Mapping[str, str]
    audit_objectives_digest: str
    pre_round_hook_configuration_digest: str
    pre_round_hook_commit: str
    pre_round_hook_results_digest: str
    pre_round_hook_started_round: int
    pre_round_hook_completed_round: int
    phase_base_commit: str
    selected_task_id: Optional[int]
    attempt_number: int
    phase_started_at_monotonic: int
    attempt_started_at_monotonic: int
    last_outcome: Optional[str]
    readiness: Mapping[str, object] = field(default_factory=empty_readiness)

    def to_dict(self) -> Dict[str, object]:
        """Deterministic JSON-ready dict (role digests sorted by role)."""
        return {
            "schema": self.schema,
            "repository_identity": self.repository_identity,
            "branch": self.branch,
            "campaign_id": self.campaign_id,
            "rounds_requested": self.rounds_requested,
            "current_round": self.current_round,
            "current_phase": self.current_phase,
            "specification_digest": self.specification_digest,
            "plan_digest": self.plan_digest,
            "role_prompt_digests": dict(sorted(self.role_prompt_digests.items())),
            "audit_objectives_digest": self.audit_objectives_digest,
            "pre_round_hook_configuration_digest": self.pre_round_hook_configuration_digest,
            "pre_round_hook_commit": self.pre_round_hook_commit,
            "pre_round_hook_results_digest": self.pre_round_hook_results_digest,
            "pre_round_hook_started_round": self.pre_round_hook_started_round,
            "pre_round_hook_completed_round": self.pre_round_hook_completed_round,
            "phase_base_commit": self.phase_base_commit,
            "selected_task_id": self.selected_task_id,
            "attempt_number": self.attempt_number,
            "phase_started_at_monotonic": self.phase_started_at_monotonic,
            "attempt_started_at_monotonic": self.attempt_started_at_monotonic,
            "last_outcome": self.last_outcome,
            "readiness": dict(self.readiness),
        }

    def validate(self) -> None:
        """Validate every structural invariant (``parse_state`` does the same)."""
        _validate_state(self)


def _expect_int(data: Mapping[str, object], key: str) -> int:
    value = data.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise StateTamperError(f"`{key}` must be an integer")
    return value


def _expect_str(
    data: Mapping[str, object],
    key: str,
    *,
    pattern: Optional[re.Pattern[str]] = None,
) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value:
        raise StateTamperError(f"`{key}` must be a non-empty string")
    if pattern is not None and not pattern.fullmatch(value):
        raise StateTamperError(
            f"`{key}` must match {getattr(pattern, 'pattern', pattern)!r}"
        )
    return value


def _validate_state(state: FactoryState) -> None:
    """Enforce every ``factory-state/v1`` structural invariant.

    Called by ``parse_state`` and (defense in depth) before every transition
    helper and digest computation, so an invalid caller-built model can never
    reach the transition table or the digest ledger.
    """
    if state.schema != SCHEMA_NAME:
        raise StateTamperError(
            f"schema must be exactly {SCHEMA_NAME!r}, got {state.schema!r}"
        )
    if not IDENTITY_RE.fullmatch(state.repository_identity):
        raise StateTamperError(
            "`repository_identity` must be a dev:inode hex pair"
        )
    if not isinstance(state.branch, str) or not state.branch:
        raise StateTamperError("`branch` must be a non-empty string")
    if not isinstance(state.campaign_id, str) or not state.campaign_id:
        raise StateTamperError("`campaign_id` must be a non-empty string")
    if (
        isinstance(state.rounds_requested, bool)
        or not isinstance(state.rounds_requested, int)
        or state.rounds_requested < 1
    ):
        raise StateTamperError(
            "`rounds_requested` must be a positive integer"
        )
    if (
        isinstance(state.current_round, bool)
        or not isinstance(state.current_round, int)
        or state.current_round < 0
    ):
        raise StateTamperError("`current_round` must be a non-negative integer")
    if state.current_round > state.rounds_requested:
        raise StateTamperError(
            "`current_round` may not exceed `rounds_requested`"
        )
    if state.current_round == 0 and state.current_phase != "readiness" and not (
        state.current_phase in TERMINAL_PHASES and bool(state.readiness.get("required"))
    ):
        raise StateTamperError("round zero is reserved for readiness or its terminal outcome")
    if state.current_phase == "readiness" and state.current_round != 0:
        raise StateTamperError("readiness must be phase round zero")
    _validate_readiness(state.readiness, required=(state.current_phase == "readiness" or bool(state.readiness.get("required"))))
    if state.current_phase not in PHASE_VALUES:
        raise StateTamperError(
            "`current_phase` must be one of "
            + ", ".join(PHASE_VALUES)
            + f", got {state.current_phase!r}"
        )
    for name, digest in (
        ("specification_digest", state.specification_digest),
        ("plan_digest", state.plan_digest),
        ("audit_objectives_digest", state.audit_objectives_digest),
        ("pre_round_hook_configuration_digest", state.pre_round_hook_configuration_digest),
        ("pre_round_hook_results_digest", state.pre_round_hook_results_digest),
    ):
        if not isinstance(digest, str) or not SHA256_RE.fullmatch(digest):
            raise StateTamperError(
                f"`{name}` must be a 64-character lowercase SHA-256 hex digest"
            )
    if (
        not isinstance(state.role_prompt_digests, Mapping)
        or not state.role_prompt_digests
    ):
        raise StateTamperError(
            "`role_prompt_digests` must be a non-empty mapping"
        )
    for role, digest in state.role_prompt_digests.items():
        if (
            not isinstance(role, str)
            or not role
            or not isinstance(digest, str)
            or not SHA256_RE.fullmatch(digest)
        ):
            raise StateTamperError(
                "`role_prompt_digests` must map each role to a 64-character "
                "SHA-256 hex digest"
            )
    if not isinstance(state.pre_round_hook_commit, str) or not SHA40_RE.fullmatch(
        state.pre_round_hook_commit
    ):
        raise StateTamperError("`pre_round_hook_commit` must be a 40-character Git commit")
    for name, value in (
        ("pre_round_hook_started_round", state.pre_round_hook_started_round),
        ("pre_round_hook_completed_round", state.pre_round_hook_completed_round),
    ):
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise StateTamperError(f"`{name}` must be a non-negative integer")
    if state.pre_round_hook_completed_round > state.pre_round_hook_started_round:
        raise StateTamperError("a pre-round hook completion cannot precede its start")
    if state.pre_round_hook_started_round > state.current_round:
        raise StateTamperError("pre-round hook cursor cannot exceed the current round")
    if state.pre_round_hook_started_round - state.pre_round_hook_completed_round > 1:
        raise StateTamperError("pre-round hook cursor may have at most one ambiguous round")
    if (
        state.pre_round_hook_configuration_digest != "0" * 64
        and state.current_phase not in ("planning", "infrastructure_failure") and (
            state.pre_round_hook_completed_round < state.current_round
            or state.pre_round_hook_started_round != state.pre_round_hook_completed_round
        )
    ):
        raise StateTamperError("every phase after planning requires completed hooks for its round")
    if (
        not isinstance(state.phase_base_commit, str)
        or not SHA40_RE.fullmatch(state.phase_base_commit)
    ):
        raise StateTamperError(
            "`phase_base_commit` must be a 40-character lowercase Git commit "
            "object ID"
        )
    if state.selected_task_id is not None and (
        isinstance(state.selected_task_id, bool)
        or not isinstance(state.selected_task_id, int)
        or state.selected_task_id < 1
    ):
        raise StateTamperError(
            "`selected_task_id` must be a positive integer when present"
        )
    if (
        isinstance(state.attempt_number, bool)
        or not isinstance(state.attempt_number, int)
        or state.attempt_number < 0
    ):
        raise StateTamperError("`attempt_number` must be a non-negative integer")
    if state.attempt_number > 0 and state.selected_task_id is None:
        raise StateTamperError(
            "`attempt_number` may be positive only while a task is selected "
            "(`selected_task_id` must be present)"
        )
    if state.selected_task_id is not None and state.attempt_number == 0:
        raise StateTamperError(
            "a selected task must have begun at least one attempt "
            "(`attempt_number` must be positive)"
        )
    if state.current_phase != "implementation" and (
        state.selected_task_id is not None or state.attempt_number != 0
    ):
        raise StateTamperError(
            "a task is selected only during the implementation phase"
        )
    phase_marker = state.phase_started_at_monotonic
    if (
        isinstance(phase_marker, bool)
        or not isinstance(phase_marker, int)
        or phase_marker <= 0
    ):
        raise StateTamperError(
            "`phase_started_at_monotonic` must be a positive monotonic marker "
            "(a zeroed `now=0` epoch marker is rejected as tamper)"
        )
    attempt_marker = state.attempt_started_at_monotonic
    if (
        isinstance(attempt_marker, bool)
        or not isinstance(attempt_marker, int)
        or attempt_marker < 0
    ):
        raise StateTamperError(
            "`attempt_started_at_monotonic` must be a non-negative integer"
        )
    if state.attempt_number > 0 and attempt_marker == 0:
        raise StateTamperError(
            "an active attempt must have a positive `attempt_started_at_monotonic`"
        )
    if attempt_marker > 0 and attempt_marker < phase_marker:
        raise StateTamperError(
            "an attempt can never precede the phase that owns it "
            "(`attempt_started_at_monotonic` must be >= "
            "`phase_started_at_monotonic`)"
        )
    if state.attempt_number == 0 and attempt_marker != 0:
        raise StateTamperError(
            "an attempt marker is zero whenever no attempt is active "
            "(`attempt_started_at_monotonic` must be 0 when `attempt_number` "
            "is 0)"
        )
    if state.current_phase in TERMINAL_PHASES and state.last_outcome != state.current_phase:
        raise StateTamperError(
            f"a terminal {state.current_phase!r} state must record "
            f"`last_outcome` {state.current_phase!r}"
        )
    if state.last_outcome is not None and state.last_outcome not in OUTCOMES:
        raise StateTamperError(
            f"`last_outcome` must be a trusted outcome enum value or null, "
            f"got {state.last_outcome!r}"
        )
    if state.last_outcome is None:
        if state.current_phase not in ("readiness", "planning"):
            raise StateTamperError(
                "`last_outcome` may be null only during the planning/readiness phase, "
                f"not {state.current_phase!r}"
            )
    else:
        allowed = PHASE_OUTCOMES.get(
            state.current_phase, frozenset({state.current_phase})
        )
        if state.last_outcome not in allowed:
            raise StateTamperError(
                f"`last_outcome` {state.last_outcome!r} is not a §13 outcome "
                f"of phase {state.current_phase!r}"
            )


def migrate_offline_state(data: object, *, readiness_required: bool = False) -> Dict[str, object]:
    """Explicitly migrate a legacy v1 document for fixture/offline tooling only.

    Production loading never calls this helper.  A legacy document cannot be
    upgraded into production readiness: callers requesting readiness are
    rejected and must start a fresh campaign with a new nonce.
    """
    if readiness_required:
        raise StateTamperError("legacy state cannot migrate into production readiness")
    if not isinstance(data, dict) or data.get("schema") != LEGACY_SCHEMA_NAME:
        raise StateTamperError("offline migration requires an exact factory-state/v1 document")
    migrated = dict(data)
    migrated["schema"] = SCHEMA_NAME
    if "readiness" not in migrated:
        migrated["readiness"] = empty_readiness()
    legacy_hook_fields = {
        "pre_round_hook_configuration_digest", "pre_round_hook_commit",
        "pre_round_hook_results_digest", "pre_round_hook_started_round",
        "pre_round_hook_completed_round",
    }
    present = legacy_hook_fields.intersection(migrated)
    if present and present != legacy_hook_fields:
        raise StateTamperError("pre-round hook state fields must be present as one complete set")
    if not present:
        current_round = migrated.get("current_round", 0)
        completed = current_round if type(current_round) is int and current_round > 0 else 0
        if migrated.get("current_phase") == "planning" and migrated.get("last_outcome") in ("pass", "findings", "blocked") and completed > 0:
            completed -= 1
        migrated.update({
            "pre_round_hook_configuration_digest": "0" * 64,
            "pre_round_hook_commit": "0" * 40,
            "pre_round_hook_results_digest": "0" * 64,
            "pre_round_hook_started_round": completed,
            "pre_round_hook_completed_round": completed,
        })
    parse_state(migrated)
    return migrated


def parse_state(data: object) -> FactoryState:
    """Build a validated ``FactoryState`` from a JSON object.

    Pure function of the data: it performs no I/O and raises
    ``StateTamperError`` for every documented tamper class (extra or missing
    fields, wrong schema, unknown phase, untrusted outcome, invalid digests,
    counter violations, task/attempt inconsistencies, terminal mismatch).
    """
    if not isinstance(data, dict):
        raise StateTamperError("control state must be a JSON object")
    # Runtime parsing is deliberately migration-free.  Legacy/offline callers
    # must opt in through ``migrate_offline_state``; production recovery can
    # therefore never synthesize a readiness authority at the old version.
    extra = sorted(set(data) - set(FIELD_NAMES))
    missing = sorted(set(FIELD_NAMES) - set(data))
    if extra or missing:
        raise StateTamperError(
            "control state must contain exactly the §11 field set"
            + (f" (extra: {extra})" if extra else "")
            + (f" (missing: {missing})" if missing else "")
        )
    role_prompt_digests = data.get("role_prompt_digests")
    if not isinstance(role_prompt_digests, dict):
        raise StateTamperError(
            "`role_prompt_digests` must be a JSON object"
        )
    state = FactoryState(
        schema=_expect_str(data, "schema"),
        repository_identity=_expect_str(
            data, "repository_identity", pattern=IDENTITY_RE
        ),
        branch=_expect_str(data, "branch"),
        campaign_id=_expect_str(data, "campaign_id"),
        rounds_requested=_expect_int(data, "rounds_requested"),
        current_round=_expect_int(data, "current_round"),
        current_phase=_expect_str(data, "current_phase"),
        specification_digest=_expect_str(
            data, "specification_digest", pattern=SHA256_RE
        ),
        plan_digest=_expect_str(data, "plan_digest", pattern=SHA256_RE),
        role_prompt_digests=dict(role_prompt_digests),
        audit_objectives_digest=_expect_str(
            data, "audit_objectives_digest", pattern=SHA256_RE
        ),
        pre_round_hook_configuration_digest=_expect_str(
            data, "pre_round_hook_configuration_digest", pattern=SHA256_RE
        ),
        pre_round_hook_commit=_expect_str(
            data, "pre_round_hook_commit", pattern=SHA40_RE
        ),
        pre_round_hook_results_digest=_expect_str(
            data, "pre_round_hook_results_digest", pattern=SHA256_RE
        ),
        pre_round_hook_started_round=_expect_int(data, "pre_round_hook_started_round"),
        pre_round_hook_completed_round=_expect_int(data, "pre_round_hook_completed_round"),
        phase_base_commit=_expect_str(
            data, "phase_base_commit", pattern=SHA40_RE
        ),
        selected_task_id=data.get("selected_task_id"),
        attempt_number=_expect_int(data, "attempt_number"),
        phase_started_at_monotonic=_expect_int(
            data, "phase_started_at_monotonic"
        ),
        attempt_started_at_monotonic=_expect_int(
            data, "attempt_started_at_monotonic"
        ),
        last_outcome=data.get("last_outcome"),
        readiness=dict(data.get("readiness", {})) if isinstance(data.get("readiness"), dict) else data.get("readiness"),
    )
    _validate_state(state)
    return state


def _checked_replace(state: FactoryState, **changes: object) -> FactoryState:
    """``dataclasses.replace`` that fails closed on a write-once change.

    Every transition helper goes through this gate, so a campaign-scoped
    binding can never be altered by a transition even in a caller-built
    model.
    """
    for name in BINDING_FIELDS:
        if name in changes and changes[name] != getattr(state, name):
            raise StateBindingError(
                f"write-once campaign field `{name}` may not change"
            )
    return replace(state, **changes)


def advance(
    state: FactoryState,
    outcome: str,
    *,
    now: Optional[int] = None,
    plan_digest: Optional[str] = None,
    phase_base_commit: Optional[str] = None,
) -> FactoryState:
    """Apply exactly one §11 transition and return the new trusted state.

    ``outcome`` is the trusted control-plane outcome of the current phase.
    The target phase is a pure function of the current state and outcome:

    * ``planning --planned--> implementation`` binds the round's committed
      plan: ``plan_digest`` and ``phase_base_commit`` are required there and
      are write-once until the next ``planning -> implementation`` edge;
    * ``audit`` resolves finality from ``rounds_requested``:
      ``current_round < rounds_requested`` advances to the next round's
      ``planning`` (``current_round`` increments); the final round ends the
      campaign in the terminal state named by the outcome
      (``pass -> success``, ``findings -> findings``, ``blocked -> blocked``).
      An interrupted audit (``interrupted``) and an untrusted audit
      (``infrastructure_failure``) are terminal fail-closed closes with no
      nonfinal edge: the round never advances and the campaign ends in the
      named terminal (Task 9 review B1);
    * every other row is the §11 table verbatim; a terminal state accepts no
      further transition.

    Attempt bookkeeping is reset on every phase change
    (``selected_task_id``/``attempt_number``/``attempt_started_at_monotonic``)
    and ``phase_started_at_monotonic`` is refreshed, so a reloaded state is
    always internally consistent.
    """
    state.validate()
    if state.current_phase == "readiness":
        expected_status = {"pass": "complete", "findings": "findings", "blocked": "human_blocked", "infrastructure_failure": "infrastructure_failure"}.get(outcome)
        if expected_status is None or state.readiness.get("status") != expected_status or state.readiness.get("terminal_outcome") != outcome or state.readiness.get("result_sha256") == "0" * 64:
            raise StateTransitionError("readiness transition requires a published exact-bound terminal result")
    if state.current_phase in TERMINAL_PHASES:
        raise StateTransitionError(
            f"a terminal {state.current_phase!r} state accepts no transition"
        )
    if outcome not in OUTCOMES:
        raise StateTransitionError(f"unknown trusted outcome {outcome!r}")
    if state.current_phase == "audit":
        if outcome in AUDIT_ABORT_TARGETS:
            # An interrupted audit and an untrusted audit are terminal
            # fail-closed closes with no nonfinal edge: the round never
            # advances and the campaign ends in the named terminal (Task 9
            # review B1).
            target = AUDIT_ABORT_TARGETS[outcome]
        elif outcome not in AUDIT_FINAL_TARGETS:
            raise StateTransitionError(
                f"no §11 audit transition with outcome {outcome!r}"
            )
        else:
            final = state.current_round >= state.rounds_requested
            target = AUDIT_FINAL_TARGETS[outcome] if final else "planning"
    else:
        target = TRANSITIONS.get((state.current_phase, outcome))
        if target is None:
            raise StateTransitionError(
                f"no §11 transition from {state.current_phase!r} with "
                f"outcome {outcome!r}"
            )
    if target == "implementation":
        if plan_digest is None or phase_base_commit is None:
            raise StateTransitionError(
                "planning -> implementation requires the bound plan_digest "
                "and phase_base_commit"
            )
        if not SHA256_RE.fullmatch(plan_digest):
            raise StateTamperError(
                "`plan_digest` must be a 64-character SHA-256 hex digest"
            )
        if not SHA40_RE.fullmatch(phase_base_commit):
            raise StateTamperError(
                "`phase_base_commit` must be a 40-character Git commit hash"
            )
        next_plan_digest, next_base = plan_digest, phase_base_commit
    else:
        if plan_digest is not None or phase_base_commit is not None:
            raise StateTransitionError(
                "plan_digest/phase_base_commit bind only on the "
                "planning -> implementation transition"
            )
        next_plan_digest, next_base = state.plan_digest, state.phase_base_commit
    if now is None:
        now = time.monotonic_ns()
    if isinstance(now, bool) or not isinstance(now, int) or now <= 0:
        raise StateTamperError(
            "`now` must be a positive monotonic marker (a zeroed `now=0` "
            "epoch marker is rejected as tamper)"
        )
    next_round = (
        1 if state.current_phase == "readiness" and target == "planning" else
        state.current_round + 1
        if state.current_phase == "audit" and target == "planning"
        else state.current_round
    )
    result = _checked_replace(
        state,
        current_phase=target,
        current_round=next_round,
        plan_digest=next_plan_digest,
        phase_base_commit=next_base,
        selected_task_id=None,
        attempt_number=0,
        attempt_started_at_monotonic=0,
        phase_started_at_monotonic=now,
        last_outcome=target if target in TERMINAL_PHASES else outcome,
    )
    result.validate()
    return result


def record_retry(state: FactoryState, outcome: str) -> FactoryState:
    """Record a retry outcome without claiming a §11 phase transition.

    Only documented retry outcomes of the current phase are accepted:
    ``planning`` may retry an ``interrupted`` attempt while its budget
    remains; ``implementation`` may retry ``task_progress``, ``task_failed``,
    or ``interrupted`` while the same task's attempt budget remains.  The
    phase, round, and counters are untouched; only ``last_outcome`` changes,
    and the next attempt is begun with :func:`begin_attempt`.
    """
    state.validate()
    allowed = RETRY_OUTCOMES.get(state.current_phase, ())
    if state.current_phase in TERMINAL_PHASES or outcome not in allowed:
        raise StateTransitionError(
            f"{outcome!r} is not a retry outcome of phase "
            f"{state.current_phase!r}"
        )
    return _checked_replace(state, last_outcome=outcome)


def begin_attempt(
    state: FactoryState, task_id: int, *, now: Optional[int] = None
) -> FactoryState:
    """Begin one implementation attempt of a selected plan task.

    Valid only in the implementation phase.  ``attempt_number`` is monotonic
    within the current task (increments for the same ``task_id``) and resets
    on a trusted task transition (a different ``task_id``), exactly per §11;
    ``attempt_started_at_monotonic`` restarts for timeout recovery.
    """
    state.validate()
    if state.current_phase != "implementation":
        raise StateTransitionError(
            "attempts begin only during the implementation phase"
        )
    if (
        isinstance(task_id, bool)
        or not isinstance(task_id, int)
        or task_id < 1
    ):
        raise StateTransitionError(
            "`task_id` must be a positive integer"
        )
    if now is None:
        now = time.monotonic_ns()
    if isinstance(now, bool) or not isinstance(now, int) or now <= 0:
        raise StateTamperError(
            "`now` must be a positive monotonic marker (a zeroed `now=0` "
            "epoch marker is rejected as tamper)"
        )
    attempt = (
        state.attempt_number + 1
        if state.selected_task_id == task_id
        else 1
    )
    result = _checked_replace(
        state,
        selected_task_id=task_id,
        attempt_number=attempt,
        attempt_started_at_monotonic=now,
    )
    result.validate()
    return result


def begin_pre_round_hooks(state: FactoryState) -> FactoryState:
    """Durably claim this round before any hook side effect can occur.

    Recovery never re-executes an ambiguous claimed round.  A caller that
    observes ``started == current_round > completed`` must terminate the
    campaign for operator review.
    """
    state.validate()
    if state.current_phase != "planning":
        raise StateTransitionError("pre-round hooks start only in planning")
    if state.pre_round_hook_completed_round == state.current_round:
        raise StateTransitionError("pre-round hooks already completed this round")
    expected_previous = state.current_round - 1
    if (
        state.pre_round_hook_started_round != expected_previous
        or state.pre_round_hook_completed_round != expected_previous
    ):
        raise StateTransitionError("pre-round hook cursor is ambiguous or rewound")
    result = _checked_replace(
        state, pre_round_hook_started_round=state.current_round
    )
    result.validate()
    return result


def complete_pre_round_hooks(
    state: FactoryState, chained_result_digest: str
) -> FactoryState:
    """Bind the canonical ordered result chain before the planner starts."""
    state.validate()
    if state.current_phase != "planning":
        raise StateTransitionError("pre-round hooks complete only in planning")
    if state.pre_round_hook_started_round != state.current_round:
        raise StateTransitionError("pre-round hooks were not claimed for this round")
    if state.pre_round_hook_completed_round != state.current_round - 1:
        raise StateTransitionError("pre-round hook completion cursor is not monotonic")
    if not isinstance(chained_result_digest, str) or not SHA256_RE.fullmatch(
        chained_result_digest
    ):
        raise StateTamperError("pre-round result chain must be a SHA-256 digest")
    result = _checked_replace(
        state,
        pre_round_hook_results_digest=chained_result_digest,
        pre_round_hook_completed_round=state.current_round,
    )
    result.validate()
    return result


def state_digest(state: FactoryState) -> str:
    """Deterministic SHA-256 of the canonical JSON encoding of the model.

    The canonical encoding is ``json.dumps(to_dict(), sort_keys=True,
    separators=(",", ":"))`` (the same bytes ``atomic_write_json`` writes
    without the trailing newline), so the digest is a deterministic function
    of the state model bytes and any semantic mutation changes it.
    """
    state.validate()
    canonical = json.dumps(
        state.to_dict(), sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


# ---------------------------------------------------------------------------
# Secure file I/O
# ---------------------------------------------------------------------------


def _validate_ledger_file(info: os.stat_result) -> None:
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.getuid()
        or info.st_nlink != 1
        or info.st_mode & 0o022
        or info.st_size > LEDGER_MAX
    ):
        raise StateDigestError("unsafe digest ledger")


def init_state(
    root,
    *,
    campaign_id: str,
    rounds_requested: int,
    specification_digest: str,
    plan_digest: str,
    role_prompt_digests: Mapping[str, str],
    audit_objectives_digest: str,
    phase_base_commit: str,
    pre_round_hook_configuration_digest: str = "0" * 64,
    pre_round_hook_commit: str = "0" * 40,
    readiness_required: bool = False,
    readiness_binding: Optional[Mapping[str, object]] = None,
    branch: Optional[str] = None,
    now: Optional[int] = None,
    identity: Optional[str] = None,
) -> FactoryState:
    """Create the initial ``factory-state/v1`` control state (round 1).

    Binds the write-once campaign fields and writes the file atomically
    through the established no-follow I/O.  Refuses to overwrite an existing
    control-state file: a campaign never silently clobbers prior lifecycle
    state.
    """
    root = _as_root(root)
    if branch is None:
        branch = live_branch(root)
    if identity is None:
        identity = repository_identity(root)
    if (
        isinstance(rounds_requested, bool)
        or not isinstance(rounds_requested, int)
        or rounds_requested < 1
    ):
        raise StateTamperError("`rounds_requested` must be a positive integer")
    if not isinstance(campaign_id, str) or not campaign_id:
        raise StateTamperError("`campaign_id` must be a non-empty string")
    for name, digest in (
        ("specification_digest", specification_digest),
        ("plan_digest", plan_digest),
        ("audit_objectives_digest", audit_objectives_digest),
        ("pre_round_hook_configuration_digest", pre_round_hook_configuration_digest),
    ):
        if not isinstance(digest, str) or not SHA256_RE.fullmatch(digest):
            raise StateTamperError(
                f"`{name}` must be a 64-character SHA-256 hex digest"
            )
    if not isinstance(phase_base_commit, str) or not SHA40_RE.fullmatch(phase_base_commit):
        raise StateTamperError(
            "`phase_base_commit` must be a 40-character Git commit hash"
        )
    if (
        not isinstance(role_prompt_digests, Mapping)
        or not role_prompt_digests
    ):
        raise StateTamperError(
            "`role_prompt_digests` must be a non-empty mapping"
        )
    for role, digest in role_prompt_digests.items():
        if (
            not isinstance(role, str)
            or not role
            or not isinstance(digest, str)
            or not SHA256_RE.fullmatch(digest)
        ):
            raise StateTamperError(
                "`role_prompt_digests` must map each role to a SHA-256 hex "
                "digest"
            )
    if now is None:
        now = time.monotonic_ns()
    if isinstance(now, bool) or not isinstance(now, int) or now <= 0:
        raise StateTamperError(
            "`now` must be a positive monotonic marker (a zeroed `now=0` "
            "epoch marker is rejected as tamper)"
        )
    state = FactoryState(
        schema=SCHEMA_NAME,
        repository_identity=identity,
        branch=branch,
        campaign_id=campaign_id,
        rounds_requested=rounds_requested,
        current_round=0 if readiness_required else 1,
        current_phase="readiness" if readiness_required else "planning",
        specification_digest=specification_digest,
        plan_digest=plan_digest,
        role_prompt_digests=dict(role_prompt_digests),
        audit_objectives_digest=audit_objectives_digest,
        pre_round_hook_configuration_digest=pre_round_hook_configuration_digest,
        pre_round_hook_commit=pre_round_hook_commit,
        pre_round_hook_results_digest="0" * 64,
        pre_round_hook_started_round=(1 if pre_round_hook_configuration_digest == "0" * 64 else 0),
        pre_round_hook_completed_round=(1 if pre_round_hook_configuration_digest == "0" * 64 else 0),
        phase_base_commit=phase_base_commit,
        selected_task_id=None,
        attempt_number=0,
        phase_started_at_monotonic=now,
        attempt_started_at_monotonic=0,
        last_outcome=None,
        readiness=(dict(readiness_binding) if readiness_binding is not None else empty_readiness(required=readiness_required)),
    )
    state.validate()
    # Deterministic crash-window/orphan recovery first (Task 19 S2): a torn
    # write left only validated orphaned temporaries (removed) or a quarantine
    # holding the last validated state (restored) — never a second authority.
    recover_state(root)
    if _state_file_exists(root):
        raise StateError(
            f"refusing to overwrite an existing control-state file "
            f"{root / '.factory-state' / STATE_FILE_NAME}"
        )
    try:
        has_prior_ledger = bool(_read_ledger(root))
    except StateDigestError as exc:
        if not os.path.exists(_fio.state_directory_path(root)):
            has_prior_ledger = False
        else:
            raise StateError(
                f"refusing to create a second campaign binding: the prior "
                f"digest ledger is unsafe: {exc}"
            ) from exc
    if has_prior_ledger:
        raise StateError(
            f"refusing to create a second campaign binding: the digest ledger "
            f"{root / '.factory-state' / DIGEST_LEDGER_NAME} already records "
            f"a prior campaign's phase digests"
        )
    try:
        # Atomic no-replace publication (Task 19 S1): the existence check and
        # the linkat publish happen inside one directory scope, so init can
        # never clobber existing state or a raced pathname.
        atomic_write_json(root, STATE_FILE_NAME, state.to_dict(), no_replace=True)
    except StateIOError as exc:
        raise StateError(
            f"refusing to overwrite an existing control-state file "
            f"{root / '.factory-state' / STATE_FILE_NAME}: {exc}"
        ) from exc
    return state


def _state_file_exists(root: Path) -> bool:
    """True when a control-state file already exists in the private directory.

    A fresh root whose ``.factory-state`` directory has not been created yet
    reports ``False`` (the campaign has no prior lifecycle state); an existing
    but unsafe directory still fails closed through ``state_dir``.
    """
    try:
        with _fio.state_dir(root, create=False) as directory_fd:
            try:
                os.stat(
                    STATE_FILE_NAME, dir_fd=directory_fd, follow_symlinks=False
                )
            except FileNotFoundError:
                return False
            return True
    except StateIOError:
        try:
            os.stat(_fio.state_directory_path(root), follow_symlinks=False)
        except FileNotFoundError:
            return False
        raise


def _validate_orphan(info: os.stat_result) -> None:
    """An orphaned writer artifact must still be a private owned marker.

    Recovery never deletes a file it cannot prove is an exact mode-0600
    same-UID single-link regular marker: a foreign, symlinked, hardlinked,
    group/other-readable, or oversized artifact fails closed instead of being
    destroyed.  The mode is checked exactly (``stat.S_IMODE(...) == 0o600``),
    not merely for absent group/other write bits, so a leaked world-readable
    orphan is never silently removed.
    """
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.getuid()
        or info.st_nlink != 1
        or stat.S_IMODE(info.st_mode) != 0o600
        or info.st_size > STATE_FILE_MAX
    ):
        raise StateError(
            "unsafe orphaned state artifact: refusing to remove a file that is "
            "not a private (exact 0600), owned, single-link regular marker"
        )


def owner_tamper_gate(root) -> Dict[str, object]:
    """Probe whether the kernel honors an ownership change on a state file.

    Task 19 S7: the owner-tamper conformance probe must never silently skip
    the ownership check.  This gate performs a real ``chown(2)`` on a probe
    file inside the private state directory (mode 0600, owned by the current
    user) and *declares* what the kernel actually allowed:

    * ``{"available": True, "reason": None}`` — the kernel accepted the
      ownership change (a privileged run); the caller may exercise a genuine
      ownership tamper and assert the state reader fails closed;
    * ``{"available": False, "reason": <detail>}`` — the kernel refused the
      change (typically an unprivileged run); the owner check is genuinely
      unavailable in this process, so the caller must NOT claim owner-tamper
      coverage.  This is an honest, declared unavailability with a
      fail-closed reason — never a silent skip and never a false claim of
      coverage.

    The probe owner is restored and the probe removed before returning, so
    the gate leaves no artifact.  Any name it creates (``.owner-probe-*``) is
    outside the recognized temporary/quarantine shapes and is therefore never
    touched by :func:`recover_state`.
    """
    root = _as_root(root)
    probe = f".owner-probe-{os.getpid()}"
    target_uid = 65534 if os.getuid() != 65534 else 65533
    try:
        with _fio.state_dir(root, create=True) as directory_fd:
            descriptor = os.open(
                probe,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
                | getattr(os, "O_CLOEXEC", 0),
                0o600,
                dir_fd=directory_fd,
            )
            try:
                os.write(descriptor, b"owner-tamper-probe\n")
            finally:
                os.close(descriptor)
            try:
                os.chown(
                    probe, target_uid, -1,
                    dir_fd=directory_fd, follow_symlinks=False,
                )
            except OSError as exc:
                os.unlink(probe, dir_fd=directory_fd)
                return {
                    "available": False,
                    "reason": (
                        "owner tamper unavailable: kernel refused chown(2): "
                        f"{exc.strerror or exc}"
                    ),
                }
            # The kernel honored the ownership change; restore our ownership
            # so the probe never leaves a foreign-owned artifact, then remove
            # it so the gate leaves the private directory clean.
            try:
                os.chown(
                    probe, os.getuid(), -1,
                    dir_fd=directory_fd, follow_symlinks=False,
                )
            except OSError:
                pass
            os.unlink(probe, dir_fd=directory_fd)
            os.fsync(directory_fd)
    except OSError as exc:
        return {
            "available": False,
            "reason": f"owner tamper unavailable: cannot probe ownership: {exc.strerror or exc}",
        }
    return {"available": True, "reason": None}


def _latest_ledger_digest(root: Path) -> Optional[str]:
    """Digest of the most recently recorded ledger entry, or ``None`` when no
    digest ledger exists.

    Task 19 S2: when a ledger exists, recovery must match the recovered state
    against the latest recorded state digest before restoring a quarantine, so
    a quarantined state that was tampered after it was recorded fails closed.
    A malformed or unsafe ledger fails closed through ``_read_ledger``, and a
    present-but-empty ledger is ambiguous torn evidence and likewise fails
    closed (hardening L4) instead of silently skipping the digest check.
    """
    ledger = _read_ledger(root)
    if not ledger:
        return None
    return next(reversed(ledger.values()))


def recover_state(
    root, *, _expected_uid: Optional[int] = None
) -> Dict[str, object]:
    """Deterministically recover crash-window and orphaned state artifacts.

    Task 19 S2: an atomic update can be torn by a crash at exactly two
    boundaries — leaving (a) an orphaned mode-0600 temporary that never
    linked, or (b) a quarantined copy of the last validated state after the
    canonical name was renamed but before the new state linked.  Recovery is a
    pure function of the private directory contents:

    * ``clean`` is reported *only* when the private directory is truly
      absent; an existing directory that is a symlink, a plain file,
      wrong-mode, or foreign-owned fails closed instead of being treated as
      clean;
    * ``existing-empty`` is reported when the private directory exists, is
      a valid private owned directory, and contains *nothing at all* — a
      valid present-empty directory (for example one left behind by an
      owner probe) is explicitly distinguished from a truly absent
      directory, so the caller is never told a fresh root has no lifecycle
      state when the directory is actually there; recovery creates or
      deletes nothing and leaves the empty directory untouched;
    * the canonical ``factory-loop.json`` is the single authority; when it
      exists it is re-validated and any validated orphaned temporaries and
      quarantines are removed (they are leftovers of a completed or aborted
      update);
    * when it is absent and exactly one quarantine remains, the quarantine
      holds the last validated state; the recovered state must match the
      latest recorded digest-ledger entry when a ledger exists (a
      present-but-empty ledger is ambiguous torn evidence and blocks the
      restore as fail-closed), the quarantine is restored atomically with
      no-replace, the canonical state is re-validated in place *before* the
      quarantine is deleted, and no second authority is created;
    * when it is absent and no quarantine remains, validated orphaned
      temporaries from a torn first write that never published are removed
      (no authoritative state ever existed, so nothing is lost);
    * more than one quarantine, an unvalidatable or foreign quarantine, an
      unsafe orphan, or a torn state fails closed for operator inspection —
      recovery never guesses and never deletes ambiguous or foreign
      artifacts.

    Any name outside the recognized state/ledger/temporary/quarantine shapes
    is preserved untouched.  Returns a machine-readable summary dict:
    ``status`` is ``clean`` (directory truly absent), ``existing-empty``
    (directory present and completely empty), or ``restored`` (a quarantine
    was restored); ``removed`` counts validated orphaned
    temporaries/quarantines deleted; ``restored`` is the restored canonical
    name or ``None``.

    ``_expected_uid`` is the underscore-private internal owner expectation
    threaded to the directory/file owner checks (default: the current user);
    a deterministic test may pass a wrong expected UID to exercise the exact
    owner-rejection branch of an unsafe existing directory without requiring
    ``chown``.  Production never passes it and the trusted CLI cannot set it.
    """
    root = _as_root(root)
    global _RACE_BEFORE_RESTORE_LINK, _RACE_AFTER_RESTORE_LINK
    # ``clean`` requires the private directory to be *truly absent*: a
    # symlinked, filed, world-writable, or foreign-owned existing directory
    # is unsafe and must fail closed rather than being reported clean (Task
    # 19 S2).  A dangling symlink still stats (mode S_IFLNK), so only a real
    # FileNotFoundError means the campaign has nothing to recover.
    try:
        os.stat(_fio.state_directory_path(root), follow_symlinks=False)
    except FileNotFoundError:
        return {"status": "clean", "removed": 0, "restored": None}
    try:
        with _fio.state_dir(
            root, create=False, _expected_uid=_expected_uid
        ) as directory_fd:
            names = sorted(os.listdir(directory_fd))
            temporaries = [
                name for name in names if TEMP_ORPHAN_RE.fullmatch(name)
            ]
            quarantines = [
                name for name in names if QUARANTINE_ORPHAN_RE.fullmatch(name)
            ]
            for name in temporaries + quarantines:
                info = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
                _validate_orphan(info)
            canonical_present = STATE_FILE_NAME in names
    except StateIOError as exc:
        raise StateError(
            f"refusing recovery into an unsafe existing state directory: {exc}"
        ) from exc

    if canonical_present:
        # The canonical file is the single authority; re-validate it so a
        # forged, moved, or torn state is never paired with recovery cleanup.
        load_state(root)
    else:
        if len(quarantines) > 1:
            raise StateError(
                "ambiguous crash recovery: multiple quarantined state copies; "
                "refusing to choose one without creating a second authority"
            )
        if len(quarantines) == 1:
            quarantine = quarantines[0]
            try:
                data = read_json(
                    root, quarantine, maximum=STATE_FILE_MAX,
                    # Only the narrowly scoped internal orphan-name shape is
                    # accepted for a quarantined state read; a quarantined
                    # marker name starts with a dot, so it cannot pass the
                    # public safe-name rule (``_name``) and must go through
                    # this exact shape validator (Task 19 S2).
                    name_validator=_fio._internal_orphan_name,
                    _expected_uid=_expected_uid,
                )
            except StateIOError as exc:
                raise StateError(
                    f"cannot read the quarantined state {quarantine!r}: {exc}"
                ) from exc
            recovered = parse_state(data)
            if recovered.repository_identity != repository_identity(root):
                raise StateError(
                    "quarantined state does not match the canonical root; "
                    "refusing to restore a moved or forged state"
                )
            latest = _latest_ledger_digest(root)
            if latest is not None and state_digest(recovered) != latest:
                raise StateError(
                    f"quarantined state digest does not match the latest "
                    f"digest-ledger entry ({latest}); refusing to restore a "
                    f"tampered state"
                )
            with _fio.state_dir(
                root, create=False, _expected_uid=_expected_uid
            ) as directory_fd:
                hook = _RACE_BEFORE_RESTORE_LINK
                if hook is not None:
                    _RACE_BEFORE_RESTORE_LINK = None
                    hook(root, directory_fd)
                try:
                    # No-replace restore: the canonical name must still be
                    # absent, so recovery can never overwrite a raced writer.
                    os.link(
                        quarantine, STATE_FILE_NAME,
                        src_dir_fd=directory_fd, dst_dir_fd=directory_fd,
                        follow_symlinks=False,
                    )
                except FileExistsError as exc:
                    raise StateError(
                        "state file appeared during recovery; rerun recovery "
                        "with the writer stopped"
                    ) from exc
                hook = _RACE_AFTER_RESTORE_LINK
                if hook is not None:
                    _RACE_AFTER_RESTORE_LINK = None
                    hook(root, directory_fd)
            # Re-validate the canonical state in place *before* deleting the
            # quarantine (Task 19 S2): the canonical and the quarantine are
            # one inode right now (nlink=2), so the read tolerates the extra
            # link and must reproduce the exact validated state.  A raced,
            # substituted, or forged canonical fails closed with the
            # quarantine preserved for operator inspection.
            try:
                raw = _fio.read_bytes(
                    root, STATE_FILE_NAME, maximum=STATE_FILE_MAX,
                    _expected_uid=_expected_uid, allow_linked=True,
                )
                reparsed = parse_state(json.loads(raw.decode("utf-8")))
            except (StateIOError, UnicodeError, json.JSONDecodeError) as exc:
                raise StateError(
                    f"cannot re-validate the restored state {STATE_FILE_NAME!r}: "
                    f"{exc}"
                ) from exc
            if reparsed.to_dict() != recovered.to_dict():
                raise StateError(
                    "restored canonical state differs from the validated "
                    "quarantined state; refusing to delete the quarantine"
                )
            with _fio.state_dir(
                root, create=False, _expected_uid=_expected_uid
            ) as directory_fd:
                canonical = os.stat(
                    STATE_FILE_NAME, dir_fd=directory_fd, follow_symlinks=False
                )
                quarantined = os.stat(
                    quarantine, dir_fd=directory_fd, follow_symlinks=False
                )
                if (canonical.st_dev, canonical.st_ino) != (
                    quarantined.st_dev, quarantined.st_ino
                ):
                    raise StateError(
                        "quarantine was substituted during restore; refusing "
                        "to delete it"
                    )
                os.unlink(quarantine, dir_fd=directory_fd)
                os.fsync(directory_fd)
    restored = STATE_FILE_NAME if not canonical_present and len(quarantines) == 1 else None
    restored = STATE_FILE_NAME if not canonical_present and len(quarantines) == 1 else None
    removed = 0
    with _fio.state_dir(
        root, create=False, _expected_uid=_expected_uid
    ) as directory_fd:
        leftovers = temporaries + (quarantines if canonical_present else [])
        for name in leftovers:
            info = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            _validate_orphan(info)
            os.unlink(name, dir_fd=directory_fd)
            removed += 1
        if leftovers:
            os.fsync(directory_fd)
    # ``existing-empty`` distinguishes a valid present-but-empty private
    # directory from a truly absent one (``clean``): recovery found no
    # lifecycle artifact to act on, but the directory exists, so a caller is
    # never told a fresh root has no lifecycle state when the directory is
    # actually there (for example one left behind by an owner probe).  A
    # present directory that contained artifacts reports ``clean`` after
    # recovery leaves a validated final state (leftovers already removed);
    # ``restored`` is reported only when a quarantine was linked into the
    # canonical name.
    if restored:
        status = "restored"
    elif not names:
        status = "existing-empty"
    else:
        status = "clean"
    return {
        "status": status,
        "removed": removed,
        "restored": restored,
    }


def write_state(root, state: FactoryState) -> None:
    """Atomically publish ``state`` through the established no-follow I/O.

    ``factory_state_io.atomic_write_json`` writes a mode-0600 temporary file
    in the validated ``.factory-state`` directory, ``fsync``s it, quarantines
    the previous file after an identity check, publishes with ``linkat`` (so
    a raced pathname is never silently replaced), and ``fsync``s the
    directory.
    """
    root = _as_root(root)
    state.validate()
    atomic_write_json(root, STATE_FILE_NAME, state.to_dict())


def _expect_binding(state: FactoryState, name: str, expected: object) -> None:
    if expected is None:
        return
    actual = getattr(state, name)
    if actual != expected:
        raise StateBindingError(
            f"state `{name}` {actual!r} does not match the campaign binding "
            f"{expected!r}"
        )


def load_state(
    root,
    *,
    expected_branch: Optional[str] = None,
    expected_campaign_id: Optional[str] = None,
    expected_rounds_requested: Optional[int] = None,
    expected_specification_digest: Optional[str] = None,
    expected_plan_digest: Optional[str] = None,
    expected_audit_objectives_digest: Optional[str] = None,
    expected_role_prompt_digests: Optional[Mapping[str, str]] = None,
    expected_pre_round_hook_configuration_digest: Optional[str] = None,
    expected_pre_round_hook_commit: Optional[str] = None,
    expected_readiness_required: Optional[bool] = None,
    _expected_uid: Optional[int] = None,
) -> FactoryState:
    """Securely reopen, validate, and bind the control-state file.

    Reuses ``factory_state_io.read_json`` (no-follow open, ownership/mode/
    link-count budget, size bound, read-identity checks), validates the §11
    field set and invariants, and then fails closed when the recorded
    ``repository_identity`` does not match the canonical root directory or
    when any expected campaign binding differs.

    ``_expected_uid`` is the underscore-private *internal* owner expectation
    that defaults to the current user (Task 19 S7): production never passes
    it, so the real owner check always compares real ``stat`` metadata
    against the current UID, and the trusted CLI cannot set it.  A
    deterministic always-runnable test may pass a wrong expected UID to
    exercise the exact owner-rejection branch with real stat metadata and
    without requiring ``chown``.
    """
    root = _as_root(root)
    try:
        data = read_json(
            root, STATE_FILE_NAME, maximum=STATE_FILE_MAX,
            _expected_uid=_expected_uid,
        )
    except StateIOError as exc:
        raise StateTamperError(str(exc)) from exc
    state = parse_state(data)
    if state.repository_identity != repository_identity(root):
        raise StateTamperError(
            "state `repository_identity` does not match the canonical root"
        )
    _expect_binding(state, "branch", expected_branch)
    _expect_binding(state, "campaign_id", expected_campaign_id)
    _expect_binding(state, "rounds_requested", expected_rounds_requested)
    _expect_binding(state, "specification_digest", expected_specification_digest)
    _expect_binding(state, "plan_digest", expected_plan_digest)
    _expect_binding(state, "audit_objectives_digest", expected_audit_objectives_digest)
    _expect_binding(state, "role_prompt_digests", expected_role_prompt_digests)
    _expect_binding(
        state, "pre_round_hook_configuration_digest",
        expected_pre_round_hook_configuration_digest,
    )
    _expect_binding(state, "pre_round_hook_commit", expected_pre_round_hook_commit)
    if expected_readiness_required is not None and state.readiness.get("required") is not expected_readiness_required:
        raise StateBindingError(
            "state readiness applicability differs from the immutable expected production policy"
        )
    return state


# ---------------------------------------------------------------------------
# Before/after untrusted-phase digest verification (§11)
# ---------------------------------------------------------------------------


def _append_ledger_line_fd(
    directory_fd: int,
    line: bytes,
    *,
    tag: str,
    expected_identity: Optional[Tuple[int, int]],
) -> None:
    """Append exactly one validated JSON line inside an open dirfd scope.

    The caller holds the single validated ``state_dir`` scope and already
    read the ledger through the same scope (:func:`_read_ledger_fd`) to
    reject duplicate tags; ``expected_identity`` is the (dev, inode) of the
    ledger observed by that read, or ``None`` when the ledger was absent.
    Every step re-validates identity against that expectation, so a ledger
    that was swapped, substituted, or created between the duplicate-tag
    check and the append fails closed (Task 19 hardening L4/L5): the stat
    before the append must match the read's identity, the opened descriptor
    must match it, the ledger is re-read to confirm the same tag is still
    absent on the very same inode (so a concurrent same-tag writer that
    landed between the duplicate-tag check and this append cannot produce a
    duplicate — at most one same-tag writer ever succeeds and the ledger
    stays a valid unique-tag sequence), and after the write the name must
    still resolve to the same inode with exactly
    ``before_size + len(line)`` bytes.
    """
    if not line.endswith(b"\n") or b"\n" in line[:-1] or b"\x00" in line:
        raise StateDigestError(
            "ledger line must be one newline-terminated JSON line"
        )
    try:
        try:
            name_before = os.stat(
                DIGEST_LEDGER_NAME, dir_fd=directory_fd, follow_symlinks=False
            )
        except FileNotFoundError:
            name_before = None
        name_identity = (
            (name_before.st_dev, name_before.st_ino)
            if name_before is not None
            else None
        )
        if name_identity != expected_identity:
            raise StateDigestError(
                "digest ledger was replaced between the duplicate-tag check "
                "and the append"
            )
        # Re-read the ledger through the same directory scope and confirm the
        # *same inode* still holds no record of this tag before we append.  A
        # concurrent writer that appended the same tag to this exact inode
        # between the caller's duplicate-tag check and now is caught here, so
        # exactly one same-tag writer can ever succeed and the resulting
        # ledger is never left with a repeated tag (Task 19 hardening L5).  A
        # swap to a different inode is also caught (the fresh identity differs
        # from the dup-checked expectation).
        recheck_ledger, recheck_identity = _read_ledger_fd(directory_fd)
        if recheck_identity != expected_identity:
            raise StateDigestError(
                "digest ledger was replaced between the duplicate-tag check "
                "and the append"
            )
        if tag in recheck_ledger:
            raise StateDigestError(f"digest ledger repeats phase tag {tag!r}")
        descriptor = os.open(
            DIGEST_LEDGER_NAME,
            os.O_WRONLY | os.O_APPEND | os.O_CREAT
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0),
            0o600,
            dir_fd=directory_fd,
        )
        try:
            os.fchmod(descriptor, 0o600)
            before = os.fstat(descriptor)
            _validate_ledger_file(before)
            if expected_identity is not None and (
                before.st_dev, before.st_ino
            ) != expected_identity:
                raise StateDigestError(
                    "digest ledger was replaced between the duplicate-tag "
                    "check and the append"
                )
            written = os.write(descriptor, line)
            after = os.fstat(descriptor)
            _validate_ledger_file(after)
            if written != len(line):
                raise StateDigestError("short digest-ledger append")
            if (
                (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino)
                or after.st_size != before.st_size + len(line)
            ):
                raise StateDigestError("digest ledger changed while appending")
            name_after = os.stat(
                DIGEST_LEDGER_NAME, dir_fd=directory_fd, follow_symlinks=False
            )
            if (name_after.st_dev, name_after.st_ino) != (
                after.st_dev, after.st_ino
            ):
                raise StateDigestError(
                    "digest ledger name replaced during the append"
                )
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        os.fsync(directory_fd)
    except (OSError, StateIOError) as exc:
        raise StateDigestError(f"cannot append to the digest ledger: {exc}") from exc


def _parse_ledger_lines(raw: bytes) -> Dict[str, str]:
    """Parse and validate the exact ``{"tag", "digest"}`` line set.

    An existing *zero-byte* ledger fails closed: the file is present but
    holds no record, which is ambiguous torn evidence of an interrupted
    first append — it is never silently treated as “no evidence” (Task 19
    hardening L4).
    """
    if raw == b"":
        raise StateDigestError(
            "digest ledger exists but is empty (a torn append artifact); "
            "refusing to treat it as no recorded evidence"
        )
    try:
        text = raw.decode("utf-8")
    except UnicodeError as exc:
        raise StateDigestError("digest ledger is not UTF-8") from exc
    ledger: Dict[str, str] = {}
    for number, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            raise StateDigestError(f"digest ledger line {number} is empty")
        try:
            record = json.loads(line)
        except json.JSONDecodeError as exc:
            raise StateDigestError(
                f"digest ledger line {number} is not JSON"
            ) from exc
        if (
            not isinstance(record, dict)
            or set(record) != {"tag", "digest"}
            or not isinstance(record.get("tag"), str)
            or not SAFE_TAG_RE.fullmatch(record["tag"])
            or not isinstance(record.get("digest"), str)
            or not SHA256_RE.fullmatch(record["digest"])
        ):
            raise StateDigestError(f"digest ledger line {number} is malformed")
        tag = record["tag"]
        if tag in ledger:
            raise StateDigestError(f"digest ledger repeats phase tag {tag!r}")
        ledger[tag] = record["digest"]
    return ledger


def _read_ledger_fd(
    directory_fd: int,
) -> Tuple[Dict[str, str], Optional[Tuple[int, int]]]:
    """Read and validate the ledger through an already-open directory fd.

    Returns ``(parsed entries, ledger (dev, inode) identity)`` where the
    identity is ``None`` when the ledger does not exist.  The read holds one
    descriptor and re-validates identity/size before and after, so a ledger
    replaced while being read fails closed; the returned identity lets the
    caller bind the append to the very ledger that was just checked.
    """
    try:
        descriptor = os.open(
            DIGEST_LEDGER_NAME,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0),
            dir_fd=directory_fd,
        )
    except FileNotFoundError:
        return {}, None
    except OSError as exc:
        raise StateDigestError(
            f"cannot safely open the digest ledger: {exc}"
        ) from exc
    try:
        before = os.fstat(descriptor)
        _validate_ledger_file(before)
        named = os.stat(
            DIGEST_LEDGER_NAME, dir_fd=directory_fd, follow_symlinks=False
        )
        identity = (before.st_dev, before.st_ino)
        if identity != (named.st_dev, named.st_ino):
            raise StateDigestError(
                "digest ledger changed while being opened"
            )
        chunks: List[bytes] = []
        remaining = LEDGER_MAX + 1
        while remaining:
            chunk = os.read(descriptor, min(65536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
        after = os.fstat(descriptor)
        named_after = os.stat(
            DIGEST_LEDGER_NAME, dir_fd=directory_fd, follow_symlinks=False
        )
        if (
            len(raw) > LEDGER_MAX
            or (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
            != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
            or (after.st_dev, after.st_ino) != (named_after.st_dev, named_after.st_ino)
        ):
            raise StateDigestError("digest ledger changed while reading")
        return _parse_ledger_lines(raw), identity
    finally:
        os.close(descriptor)


def _read_ledger(root: Path) -> Dict[str, str]:
    try:
        raw = read_bytes(
            root, DIGEST_LEDGER_NAME, maximum=LEDGER_MAX, missing_ok=True
        )
    except StateIOError as exc:
        raise StateDigestError(str(exc)) from exc
    if raw is None:
        return {}
    return _parse_ledger_lines(raw)


def read_phase_digest_ledger(root: Path) -> Dict[str, str]:
    """The phase-digest ledger through the authoritative strict state parser.

    ``tag -> recorded state digest`` for every phase tag the trusted control
    plane recorded, read through the hardened no-follow bounded reader and
    the exact strict line parser (Task 19 hardening L4: a present-but-empty
    ledger, a non-UTF-8 ledger, a malformed line, a repeated tag, or an
    unsafe marker all fail closed).  The Task 10 findings authority consumes
    the ledger only through this function, never through a private tolerant
    re-parse, so a tampered ledger can never be silently tolerated.
    """
    return _read_ledger(root)


def record_phase_digest(root, tag: str) -> str:
    """Record the current state digest before an untrusted phase.

    Appends one ``{"tag": ..., "digest": ...}`` line to the append-only
    evidence ledger ``.factory-state/state-digest-ledger.jsonl``; the ledger
    is evidence, never orchestration state, and a repeated tag fails closed.
    The duplicate-tag check and the append happen inside *one* validated
    directory scope over the same ledger identity (Task 19 hardening L3), so
    a ledger swapped or substituted between the check and the append — or
    while being appended — fails closed instead of silently recording into
    an unexpected file.  Returns the recorded digest.
    """
    root = _as_root(root)
    if not SAFE_TAG_RE.fullmatch(tag):
        raise StateDigestError(f"unsafe phase tag {tag!r}")
    global _LEDGER_RACE_AFTER_DUP_CHECK
    state = load_state(root)
    digest = state_digest(state)
    line = json.dumps(
        {"tag": tag, "digest": digest}, sort_keys=True, separators=(",", ":")
    ).encode("utf-8") + b"\n"
    try:
        with _fio.state_dir(root, create=True) as directory_fd:
            ledger, identity = _read_ledger_fd(directory_fd)
            if tag in ledger:
                raise StateDigestError(f"digest ledger repeats phase tag {tag!r}")
            hook = _LEDGER_RACE_AFTER_DUP_CHECK
            if hook is not None:
                _LEDGER_RACE_AFTER_DUP_CHECK = None
                hook(root, directory_fd)
            _append_ledger_line_fd(
                directory_fd, line, tag=tag, expected_identity=identity
            )
    except StateError:
        raise
    except (OSError, StateIOError) as exc:
        raise StateDigestError(f"cannot append to the digest ledger: {exc}") from exc
    return digest


def verify_phase_digest(root, tag: str) -> str:
    """Reopen, re-validate, and digest-match the state after an untrusted phase.

    Fails closed when the state changed since :func:`record_phase_digest`
    (forged field, rewound counter, phase/task drift, or any other semantic
    mutation) or when the ledger/record is missing, malformed, or repeated.
    """
    root = _as_root(root)
    if not SAFE_TAG_RE.fullmatch(tag):
        raise StateDigestError(f"unsafe phase tag {tag!r}")
    state = load_state(root)
    digest = state_digest(state)
    ledger = _read_ledger(root)
    recorded = ledger.get(tag)
    if recorded is None:
        raise StateDigestError(f"no recorded digest for phase {tag!r}")
    if recorded != digest:
        raise StateDigestError(
            f"state digest changed during the untrusted phase {tag!r}"
        )
    return digest


# ---------------------------------------------------------------------------
# Trusted control-plane CLI (never invoked by a model role)
# ---------------------------------------------------------------------------


def main(argv: Optional[List[str]] = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        prog="factory-state",
        description=(
            "Single mutable control-state authority (FACTORY-LOOP-SPEC §11, "
            "schema factory-state/v1). Trusted control-plane operations only."
        ),
    )
    parser.add_argument(
        "--root",
        metavar="ROOT",
        default=str(Path(__file__).resolve().parent.parent.parent),
        help="canonical repository root (default: this repository)",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    init_parser = sub.add_parser("init", help="create the initial planning state")
    init_parser.add_argument("--campaign-id", required=True)
    init_parser.add_argument("--rounds", type=int, required=True)
    init_parser.add_argument("--base-commit", required=True)
    init_parser.add_argument("--spec-digest", required=True)
    init_parser.add_argument("--plan-digest", required=True)
    init_parser.add_argument("--audit-digest", required=True)
    init_parser.add_argument("--role-digest", action="append", default=[], metavar="ROLE=HEX")
    init_parser.add_argument("--branch", default=None)

    sub.add_parser("show", help="print the canonical state JSON")
    sub.add_parser("digest", help="print the deterministic state digest")
    sub.add_parser(
        "recover",
        help=(
            "deterministic crash-window/orphan recovery (Task 19 S2): restore "
            "the last validated state from a torn write or remove validated "
            "orphaned writer artifacts"
        ),
    )

    # The subparser variables are distinctly named (``*_parser``) so a
    # parser object can never shadow the transition/retry helper of the same
    # name inside this function.
    advance_parser = sub.add_parser("advance", help="apply one §11 transition")
    advance_parser.add_argument("outcome", choices=sorted(OUTCOMES))
    advance_parser.add_argument("--plan-digest", default=None)
    advance_parser.add_argument("--base-commit", default=None)

    attempt_parser = sub.add_parser("begin-attempt", help="begin one implementation attempt")
    attempt_parser.add_argument("task_id", type=int)

    retry_parser = sub.add_parser("record-retry", help="record a retry outcome")
    retry_parser.add_argument("outcome", choices=sorted(OUTCOMES))

    record_parser = sub.add_parser("record-phase-digest", help="record the pre-phase digest")
    record_parser.add_argument("tag")

    verify_parser = sub.add_parser("verify-phase-digest", help="validate the post-phase digest")
    verify_parser.add_argument("tag")

    args = parser.parse_args(argv)
    root = Path(args.root)
    try:
        if args.command == "init":
            role_prompt_digests: Dict[str, str] = {}
            for item in args.role_digest:
                role, separator, digest = item.partition("=")
                if not separator or not role or not SHA256_RE.fullmatch(digest):
                    raise StateTamperError(
                        f"--role-digest expects ROLE=64-hex, got {item!r}"
                    )
                if role in role_prompt_digests:
                    raise StateTamperError(f"duplicate role prompt digest {role!r}")
                role_prompt_digests[role] = digest
            state = init_state(
                root,
                campaign_id=args.campaign_id,
                rounds_requested=args.rounds,
                specification_digest=args.spec_digest,
                plan_digest=args.plan_digest,
                role_prompt_digests=role_prompt_digests,
                audit_objectives_digest=args.audit_digest,
                phase_base_commit=args.base_commit,
                branch=args.branch,
            )
            print(f"initialized {state.campaign_id} round=1 phase=planning")
            print(f"digest={state_digest(state)}")
        elif args.command == "show":
            state = load_state(root)
            print(json.dumps(state.to_dict(), sort_keys=True, separators=(",", ":")))
        elif args.command == "digest":
            state = load_state(root)
            print(f"digest={state_digest(state)}")
        elif args.command == "recover":
            summary = recover_state(root)
            print(
                f"recovered status={summary['status']} "
                f"removed={summary['removed']} "
                f"restored={summary['restored'] or 'none'}"
            )
        elif args.command == "advance":
            state = advance(
                load_state(root),
                args.outcome,
                plan_digest=args.plan_digest,
                phase_base_commit=args.base_commit,
            )
            write_state(root, state)
            print(f"phase={state.current_phase} last_outcome={state.last_outcome}")
            print(f"digest={state_digest(state)}")
        elif args.command == "begin-attempt":
            state = begin_attempt(load_state(root), args.task_id)
            write_state(root, state)
            print(f"attempt={state.attempt_number}")
        elif args.command == "record-retry":
            state = record_retry(load_state(root), args.outcome)
            write_state(root, state)
            print(f"last_outcome={state.last_outcome}")
        elif args.command == "record-phase-digest":
            digest = record_phase_digest(root, args.tag)
            print(f"recorded {args.tag} digest={digest}")
        else:  # verify-phase-digest
            digest = verify_phase_digest(root, args.tag)
            print(f"verified {args.tag} digest={digest}")
    except (StateError, StateIOError, OSError) as exc:
        print(f"factory-state: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
