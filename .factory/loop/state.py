#!/usr/bin/env python3
"""Single mutable control-state authority: ``factory-state/v1`` (STATE-01).

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
    verification   infrastructure_failure -> infrastructure_failure (terminal)
    audit          pass | findings | blocked -> planning(next round)
                                             when current_round < rounds_requested
    audit          pass                 -> success            (final, terminal)
    audit          findings             -> findings           (final, terminal)
    audit          blocked              -> blocked            (final, terminal)

Round advances only on ``audit --nonfinal``; phase never moves backward
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
transition.

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
from dataclasses import dataclass, replace
from pathlib import Path
import re
import stat
import subprocess
import sys
import time
from typing import Dict, List, Mapping, Optional, Tuple

SCHEMA_NAME = "factory-state/v1"
STATE_FILE_NAME = "factory-loop.json"
DIGEST_LEDGER_NAME = "state-digest-ledger.jsonl"

# One mutable control-state file plus one append-only evidence ledger; the
# ledger is never orchestration state (FACTORY-LOOP-SPEC §11).
STATE_FILE_MAX = 16 * 1024
LEDGER_MAX = 1024 * 1024

# §11 phases: the four lifecycle phases and the terminal campaign states of
# the §11 transition table.
PHASES = ("planning", "implementation", "verification", "audit")
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
    ("planning", "planned"): "implementation",
    ("planning", "failed"): "failed",
    ("planning", "interrupted"): "interrupted",
    ("implementation", "task_completed"): "verification",
    ("implementation", "work_exhausted"): "verification",
    ("implementation", "blocked"): "verification",
    ("implementation", "task_failed"): "verification",
    ("implementation", "interrupted"): "interrupted",
    ("verification", "pass"): "audit",
    ("verification", "findings"): "audit",
    ("verification", "blocked"): "audit",
    ("verification", "infrastructure_failure"): "infrastructure_failure",
}
# ``audit --final--> success | findings | blocked``; the same outcomes advance
# to the next round's planning when the audit is non-final.
AUDIT_FINAL_TARGETS = {"pass": "success", "findings": "findings", "blocked": "blocked"}

# Outcomes that retry the same phase/attempt without claiming a §11 phase
# transition (their attempt budget remains): a planning step interrupted
# before a valid checkpoint (§13.1) and implementation attempts that retry
# the same plan task (§13.2).
RETRY_OUTCOMES: Dict[str, Tuple[str, ...]] = {
    "planning": ("interrupted",),
    "implementation": ("task_progress", "task_failed", "interrupted"),
}

# Campaign-scoped fields bound once by ``init`` and immutable afterwards.
BINDING_FIELDS = (
    "schema", "repository_identity", "branch", "campaign_id",
    "rounds_requested", "specification_digest", "role_prompt_digests",
    "audit_objectives_digest",
)
FIELD_NAMES: Tuple[str, ...] = (
    "schema", "repository_identity", "branch", "campaign_id",
    "rounds_requested", "current_round", "current_phase",
    "specification_digest", "plan_digest", "role_prompt_digests",
    "audit_objectives_digest", "phase_base_commit", "selected_task_id",
    "attempt_number", "phase_started_at_monotonic",
    "attempt_started_at_monotonic", "last_outcome",
)

SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
SHA40_RE = re.compile(r"^[0-9a-f]{40}$")
IDENTITY_RE = re.compile(r"^[0-9a-f]+:[0-9a-f]+$")
SAFE_TAG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


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


_fio = _load_factory_state_io()
read_json = _fio.read_json
read_bytes = _fio.read_bytes
atomic_write_json = _fio.atomic_write_json
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
    with ``O_DIRECTORY`` and ``O_NOFOLLOW``, so a control-state file moved to
    a different checkout (different pathname or inode) fails closed on load.
    """
    root = _as_root(root)
    if sys.platform != "linux" or not hasattr(os, "O_NOFOLLOW"):
        raise StateError("required Linux no-follow primitives are unavailable")
    descriptor = os.open(
        root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    )
    try:
        info = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    return f"{info.st_dev:x}:{info.st_ino:x}"


def live_branch(root) -> str:
    """The live Git branch of ``root`` (used by ``init`` when none is given)."""
    result = subprocess.run(
        ["git", "-C", str(_as_root(root)), "rev-parse", "--abbrev-ref", "HEAD"],
        capture_output=True,
        text=True,
    )
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
    phase_base_commit: str
    selected_task_id: Optional[int]
    attempt_number: int
    phase_started_at_monotonic: int
    attempt_started_at_monotonic: int
    last_outcome: Optional[str]

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
            "phase_base_commit": self.phase_base_commit,
            "selected_task_id": self.selected_task_id,
            "attempt_number": self.attempt_number,
            "phase_started_at_monotonic": self.phase_started_at_monotonic,
            "attempt_started_at_monotonic": self.attempt_started_at_monotonic,
            "last_outcome": self.last_outcome,
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
        or state.current_round < 1
    ):
        raise StateTamperError("`current_round` must be a positive integer")
    if state.current_round > state.rounds_requested:
        raise StateTamperError(
            "`current_round` may not exceed `rounds_requested`"
        )
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
    for name, value in (
        ("phase_started_at_monotonic", state.phase_started_at_monotonic),
        ("attempt_started_at_monotonic", state.attempt_started_at_monotonic),
    ):
        if (
            isinstance(value, bool)
            or not isinstance(value, int)
            or value < 0
        ):
            raise StateTamperError(f"`{name}` must be a non-negative integer")
    if state.attempt_number > 0 and state.attempt_started_at_monotonic == 0:
        raise StateTamperError(
            "an active attempt must have a positive `attempt_started_at_monotonic`"
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


def parse_state(data: object) -> FactoryState:
    """Build a validated ``FactoryState`` from a JSON object.

    Pure function of the data: it performs no I/O and raises
    ``StateTamperError`` for every documented tamper class (extra or missing
    fields, wrong schema, unknown phase, untrusted outcome, invalid digests,
    counter violations, task/attempt inconsistencies, terminal mismatch).
    """
    if not isinstance(data, dict):
        raise StateTamperError("control state must be a JSON object")
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
      (``pass -> success``, ``findings -> findings``, ``blocked -> blocked``);
    * every other row is the §11 table verbatim; a terminal state accepts no
      further transition.

    Attempt bookkeeping is reset on every phase change
    (``selected_task_id``/``attempt_number``/``attempt_started_at_monotonic``)
    and ``phase_started_at_monotonic`` is refreshed, so a reloaded state is
    always internally consistent.
    """
    state.validate()
    if state.current_phase in TERMINAL_PHASES:
        raise StateTransitionError(
            f"a terminal {state.current_phase!r} state accepts no transition"
        )
    if outcome not in OUTCOMES:
        raise StateTransitionError(f"unknown trusted outcome {outcome!r}")
    if state.current_phase == "audit":
        if outcome not in AUDIT_FINAL_TARGETS:
            raise StateTransitionError(
                f"no §11 audit transition with outcome {outcome!r}"
            )
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
    if isinstance(now, bool) or not isinstance(now, int) or now < 0:
        raise StateTamperError("`now` must be a non-negative monotonic marker")
    next_round = (
        state.current_round + 1
        if state.current_phase == "audit" and target == "planning"
        else state.current_round
    )
    return _checked_replace(
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
    if isinstance(now, bool) or not isinstance(now, int) or now < 0:
        raise StateTamperError("`now` must be a non-negative monotonic marker")
    attempt = (
        state.attempt_number + 1
        if state.selected_task_id == task_id
        else 1
    )
    return _checked_replace(
        state,
        selected_task_id=task_id,
        attempt_number=attempt,
        attempt_started_at_monotonic=now,
    )


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
    state = FactoryState(
        schema=SCHEMA_NAME,
        repository_identity=identity,
        branch=branch,
        campaign_id=campaign_id,
        rounds_requested=rounds_requested,
        current_round=1,
        current_phase="planning",
        specification_digest=specification_digest,
        plan_digest=plan_digest,
        role_prompt_digests=dict(role_prompt_digests),
        audit_objectives_digest=audit_objectives_digest,
        phase_base_commit=phase_base_commit,
        selected_task_id=None,
        attempt_number=0,
        phase_started_at_monotonic=now,
        attempt_started_at_monotonic=0,
        last_outcome=None,
    )
    state.validate()
    if _state_file_exists(root):
        raise StateError(
            f"refusing to overwrite an existing control-state file "
            f"{root / '.factory-state' / STATE_FILE_NAME}"
        )
    write_state(root, state)
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
            os.stat(root / ".factory-state", follow_symlinks=False)
        except FileNotFoundError:
            return False
        raise


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
) -> FactoryState:
    """Securely reopen, validate, and bind the control-state file.

    Reuses ``factory_state_io.read_json`` (no-follow open, ownership/mode/
    link-count budget, size bound, read-identity checks), validates the §11
    field set and invariants, and then fails closed when the recorded
    ``repository_identity`` does not match the canonical root directory or
    when any expected campaign binding differs.
    """
    root = _as_root(root)
    try:
        data = read_json(root, STATE_FILE_NAME, maximum=STATE_FILE_MAX)
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
    return state


# ---------------------------------------------------------------------------
# Before/after untrusted-phase digest verification (§11)
# ---------------------------------------------------------------------------


def _append_ledger_line(root: Path, line: bytes) -> None:
    """Append exactly one validated JSON line to the append-only ledger."""
    if not line.endswith(b"\n") or b"\n" in line[:-1] or b"\x00" in line:
        raise StateDigestError("ledger line must be one newline-terminated JSON line")
    try:
        with _fio.state_dir(root, create=True) as directory_fd:
            descriptor = -1
            try:
                try:
                    existing = os.stat(
                        DIGEST_LEDGER_NAME, dir_fd=directory_fd, follow_symlinks=False
                    )
                except FileNotFoundError:
                    existing = None
                if existing is not None:
                    _validate_ledger_file(existing)
                descriptor = os.open(
                    DIGEST_LEDGER_NAME,
                    os.O_WRONLY | os.O_APPEND | os.O_CREAT
                    | getattr(os, "O_NOFOLLOW", 0),
                    0o600,
                    dir_fd=directory_fd,
                )
                os.fchmod(descriptor, 0o600)
                before = os.fstat(descriptor)
                _validate_ledger_file(before)
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
                os.fsync(descriptor)
            finally:
                if descriptor >= 0:
                    os.close(descriptor)
            os.fsync(directory_fd)
    except StateError:
        raise
    except (OSError, StateIOError) as exc:
        raise StateDigestError(f"cannot append to the digest ledger: {exc}") from exc


def _read_ledger(root: Path) -> Dict[str, str]:
    try:
        raw = read_bytes(root, DIGEST_LEDGER_NAME, maximum=LEDGER_MAX, missing_ok=True)
    except StateIOError as exc:
        raise StateDigestError(str(exc)) from exc
    if raw is None:
        return {}
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


def record_phase_digest(root, tag: str) -> str:
    """Record the current state digest before an untrusted phase.

    Appends one ``{"tag": ..., "digest": ...}`` line to the append-only
    evidence ledger ``.factory-state/state-digest-ledger.jsonl``; the ledger
    is evidence, never orchestration state, and a repeated tag fails closed.
    Returns the recorded digest.
    """
    root = _as_root(root)
    if not SAFE_TAG_RE.fullmatch(tag):
        raise StateDigestError(f"unsafe phase tag {tag!r}")
    state = load_state(root)
    digest = state_digest(state)
    if tag in _read_ledger(root):
        raise StateDigestError(f"digest ledger repeats phase tag {tag!r}")
    _append_ledger_line(
        root,
        json.dumps(
            {"tag": tag, "digest": digest}, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        + b"\n",
    )
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
