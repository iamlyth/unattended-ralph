#!/usr/bin/env python3
"""Phase/campaign orchestrator — Task 9 (PHASE-01, COMPLETE-01, GIT-01).

This module implements the trusted phase/campaign control plane of
``docs/FACTORY-LOOP-SPEC.md`` §13/§14/§15 on top of the already committed
building blocks: the deterministic plan parser (``plan_parser.py``), the
trusted selector (``selector.py``), the ``factory-state/v1`` control-state
authority (``state.py``), the root-descriptor lock and Git writer boundary
(``lock.py``/``gitutil.py``), the fresh-context launch/supervision authority
(``launch.py``), pre-round policy hooks (``pre_round.py``), and the workspace
confinement authority (``workspace_confinement.py``).  It is the only module
that owns the *campaign loop*; every Git operation, repository-history read,
staging step, and guarded commit of a campaign is performed here through the
descriptor-anchored authority and never by a model tool.

Responsibilities (Task 9 scope):

* drive the exact phase machine ``planning -> implementation ->
  verification -> audit`` with the §11 transition table, §13 outcome
  classification, §14 finite round semantics, and the §15 predicate ladder
  kept distinct (task completion, work exhaustion, verification pass, audit
  pass, product acceptance, and campaign success never collapse);
* enforce finite round and attempt bounds with the exact terminal
  classification: ``success | findings | blocked | failed | interrupted |
  infrastructure_failure``; ``work_exhausted``/``blocked`` always reach
  verification and audit and never spin on empty work;
* own the trusted, descriptor-anchored Git/history/status/commit authority:
  every commit, history read, staging step, and dirty-scope verification is
  performed behind the root-descriptor lock through the pinned absolute Git
  executable (never through a model tool, never through a caller ``PATH``,
  and with every ``GIT_CONFIG*``/redirector environment stripped); dirty
  work is never reset, discarded, or silently overwritten;
* crash recovery is derived from Git + plan + state: a trusted commit that
  landed before its transition was recorded is reconciled deterministically,
  an ``in_progress`` task resumes from current code and Git diff, and an
  ambiguous live process, changed identity/branch, stale spec binding,
  rewound counter, or invalid transition fails closed for operator
  inspection;
* one machine-readable campaign result (``factory-campaign-result/v1``) is
  produced and published under the ignored ``.factory-state/`` evidence
  namespace.  The control state, the digest ledger, and the published result
  are the only lifecycle files the orchestrator touches; no runtime task
  ledger, Orchestrator event stream, memory store, or context summary is
  ever created.

The untrusted model process is the only *untrusted* surface of a phase.  Its
model-completion markers and prose are never control protocol; the
machine-readable process exit status is one deterministic §13 signal
(FACTORY-LOOP-SPEC §13:
the trusted harness derives outcomes from plan state, Git state, exit
status, and deterministic gates).  The orchestrator derives every phase
outcome from the plan state, the Git state, the role's exit status,
deterministic gate commands, and the §11 transition table.

The ``--role-driver`` CLI option is an explicit test-only deterministic
fixture seam: it requires a committed scenario (or the dedicated evidence
smoke lane), uses the synthetic provider, and is never a production fallback
or evidence of real model acceptance/confinement. Production launches require
explicit provider/model/backend and always go through the launch authority
(:func:`launch_role_attempt`), which retains Task 8 real Landlock confinement.
The fixed §10 decision table runs immediately before every model invocation.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import stat
import sys
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Dict, List, Mapping, Optional, Sequence, Tuple

try:  # package import (the hidden `.factory/loop/` package)
    from . import audit_objectives as audit_objectives_module
    from . import evidence as evidence_module
    from . import findings as findings_module
    from . import gitutil
    from . import installer as installer_module
    from . import lock as lock_module
    from . import plan_parser
    from . import pre_round as pre_round_module
    from . import selector as selector_module
    from . import state as state_module
    from . import launch as launch_module
    from . import workspace_confinement as confinement_authority
    from . import redaction as output_redaction
    from . import readiness as readiness_module
    from . import sidecars as sidecars_module
    from . import usage as usage_module
except ImportError:  # flat import used by the hidden `.factory/tests/` suite
    import audit_objectives as audit_objectives_module  # type: ignore[no-redef]
    import evidence as evidence_module  # type: ignore[no-redef]
    import findings as findings_module  # type: ignore[no-redef]
    import gitutil  # type: ignore[no-redef]
    import installer as installer_module  # type: ignore[no-redef]
    import lock as lock_module  # type: ignore[no-redef]
    import plan_parser  # type: ignore[no-redef]
    import pre_round as pre_round_module  # type: ignore[no-redef]
    import selector as selector_module  # type: ignore[no-redef]
    import state as state_module  # type: ignore[no-redef]
    import launch as launch_module  # type: ignore[no-redef]
    import workspace_confinement as confinement_authority  # type: ignore[no-redef]
    import redaction as output_redaction  # type: ignore[no-redef]
    import readiness as readiness_module  # type: ignore[no-redef]
    import sidecars as sidecars_module  # type: ignore[no-redef]
    import usage as usage_module  # type: ignore[no-redef]

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SCHEMA_NAME = "factory-campaign-result/v1"
RESULT_SCHEMA_FILE = "factory-campaign-result-v1.schema.json"
PHASE_RESULT_SCHEMA_NAME = "factory-phase-result/v1"
PHASE_RESULT_SCHEMA_FILE = "factory-phase-result-v1.schema.json"

PHASES = ("planning", "implementation", "verification", "audit")
TERMINAL_PHASES = (
    "success", "readiness_complete", "findings", "blocked", "failed",
    "interrupted", "infrastructure_failure",
)

# §13 phase-outcome classification sets (subsets of the trusted outcome enum).
# ``audit`` additionally classifies the two terminal fail-closed closes — an
# interrupted audit (``interrupted``) and an untrusted audit
# (``infrastructure_failure``) — which are persisted in the authoritative
# control state through dedicated §11 terminal edges (Task 9 review B1), so
# the campaign never re-executes them.
PLANNING_OUTCOMES = ("planned", "failed", "interrupted", "infrastructure_failure")
IMPLEMENTATION_OUTCOMES = (
    "task_completed", "task_progress", "task_failed",
    "interrupted", "work_exhausted", "blocked",
)
VERIFICATION_OUTCOMES = (
    "pass", "findings", "blocked", "infrastructure_failure",
    "software_verified_external_acceptance_blocked",
)
AUDIT_OUTCOMES = (
    "pass", "findings", "blocked",
    "interrupted", "infrastructure_failure",
)

PHASE_OUTCOME_SETS: Mapping[str, frozenset] = {
    "planning": frozenset(PLANNING_OUTCOMES),
    "implementation": frozenset(IMPLEMENTATION_OUTCOMES),
    "verification": frozenset(VERIFICATION_OUTCOMES),
    "audit": frozenset(AUDIT_OUTCOMES),
}

# Machine-readable result exit codes (§14 terminal classification).
EXIT_SUCCESS = 0
EXIT_FINDINGS = 1
EXIT_BLOCKED = 2
EXIT_FAILED = 3
EXIT_INTERRUPTED = 4
EXIT_INFRASTRUCTURE_FAILURE = 5
# Fail-closed control-plane code (never a campaign outcome).
EXIT_ERROR = 6

TERMINAL_EXIT_CODES: Mapping[str, int] = {
    "success": EXIT_SUCCESS,
    "readiness_complete": EXIT_SUCCESS,
    "findings": EXIT_FINDINGS,
    "blocked": EXIT_BLOCKED,
    "failed": EXIT_FAILED,
    "interrupted": EXIT_INTERRUPTED,
    "infrastructure_failure": EXIT_INFRASTRUCTURE_FAILURE,
}

# The campaign phase context passed to an embedded/fixture role driver.
CAMPAIGN_ENV_PREFIX = "FACTORY_LOOP_CAMPAIGN_"

# The fail-closed evidence-smoke lane (Task 22): an explicit campaign mode
# that drives exactly one full planning -> implementation -> verification ->
# audit round with the designated committed smoke seam and never an
# arbitrary role candidate.  The seam is private harness methodology
# evidence (label prefix ``evidence-smoke-``), never a real model or human
# outcome.
EVIDENCE_SMOKE_DRIVER_REL = ".factory/smoke/evidence_smoke_driver.py"
EVIDENCE_SMOKE_ID_PREFIX = "evidence-smoke-"
EVIDENCE_SMOKE_EVIDENCE_PREFIX = ".factory/artifacts/"
_CONTROL_SOURCE_ROOT = Path(__file__).resolve().parents[2]
_SOURCE_FIXTURE_SEAMS_AVAILABLE = (
    (_CONTROL_SOURCE_ROOT / ".git").is_dir()
    and (_CONTROL_SOURCE_ROOT / ".factory" / "tests").is_dir()
)

# A torn ``factory-loop.json`` writer leftover (``state`` authority's
# ``TEMP_ORPHAN_RE`` contract): the evidence-smoke preflight rejects any such
# recovery orphan before the state recovery can reconcile it.
CAMPAIGN_STATE_ROOT_REL = ".factory-state"
CAMPAIGN_STATE_PARENT_NAME = "campaigns"
CAMPAIGN_STATE_PARENT_REL = f"{CAMPAIGN_STATE_ROOT_REL}/{CAMPAIGN_STATE_PARENT_NAME}"
CAMPAIGN_PHASE_RESULT_NAME = "factory-phase-result.json"
CAMPAIGN_AUDIT_RESULT_NAME = "factory-audit-result.json"
RUNNER_ACQUISITION_NAME = "runner-acquisition.json"
RUNNER_ACQUISITION_SCHEMA = "factory-runner-acquisition/v1"
RUNNER_COMMAND = ("./.factory/tools/run-factory-runners.py",)
RUNNER_CHECKER_COMMAND = ("./.factory/tools/check-factory-runner-evidence.py",)
RUNNER_TRANSPORT_EXIT = 20
RUNNER_FINDINGS_EXIT = 21
RUNNER_INTEGRITY_EXIT = 22
INSTALL_MANIFEST_MAX = 64 * 1024 * 1024

# The orchestration layer's own commit identity: every campaign commit is
# provably orchestrator-created (the model never runs Git).
COMMIT_AUTHOR_NAME = "factory-campaign"
COMMIT_AUTHOR_EMAIL = "factory-campaign@localhost"

PLAN_BLOB_MAX = 4 * 1024 * 1024
MAX_RESULT_FILE = 256 * 1024
STATUS_PORCELAIN_MAX = 4 * 1024 * 1024
DEFAULT_ROLE_TIMEOUT = 900.0
DEFAULT_GATE_TIMEOUT = 1800.0
DEFAULT_RUNNER_TIMEOUT = 7800.0
MAX_RUNNER_TIMEOUT = 10800.0
DEFAULT_CAMPAIGN_TIMEOUT = 21600.0
MAX_CAMPAIGN_TIMEOUT = 86400.0
INSTALLED_EVIDENCE_OVERRIDE = "FACTORY_INSTALLED_FUNCTIONAL_EVIDENCE_PATH"
SKIP_OUTPUT_RE = re.compile(r"(?i)(?:^|[^a-z])(?:skip(?:ped)?|not[ -]?run)(?:[^a-z]|$)")
# Finite bound for every trusted Git call of the orchestrator (Task 9
# review L4): no trusted Git invocation may wait forever behind the lock.
GIT_TIMEOUT = 120.0

# Deterministic gates and acceptance commands run with a *stripped
# allowlisted environment* — never the full parent environment — so a
# credential in the operator's environment can never reach a gate child
# (Task 11).  The allowlist mirrors the fresh-child launch allowlist
# (``launch.ENV_ALLOWLIST``); every lock key and Git redirector is stripped
# as defense in depth and any surviving credential-shaped key fails closed.
GATE_ENV_ALLOWLIST = (
    "PATH", "HOME", "USER", "LOGNAME", "LANG", "LC_ALL", "LC_CTYPE",
    "LC_COLLATE", "LC_MESSAGES", "LC_MONETARY", "LC_NUMERIC", "LC_TIME",
    "TERM", "TZ", "SHELL", "XDG_RUNTIME_DIR", "XDG_CONFIG_HOME",
    "XDG_CACHE_HOME", "XDG_DATA_HOME", "NO_COLOR", "CLICOLOR",
    "CLICOLOR_FORCE", "NIX_PATH", "NIX_REMOTE",
)

# Gate output is bounded to this many bytes before redaction (the same
# bound the previous full-capture path applied).
GATE_DETAIL_MAX = 4000

SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
SHA40_RE = re.compile(r"^[0-9a-f]{40}$")
SAFE_CAMPAIGN_ID_RE = re.compile(r"^[a-z0-9](?:[a-z0-9._-]{0,62}[a-z0-9])?$")

# The trusted scope authority: no untrusted role may ever modify Git history,
# the mutable control state, legacy runtime namespaces, model tool stores, or
# the canonical specification.
FORBIDDEN_SCOPE_PREFIXES = (
    ".git",
    ".ralph",
    ".factory-state",
    ".pi",
    "$tmp",
    ".ollama-usage-env",
)


class CampaignError(Exception):
    """Base class for every fail-closed campaign failure."""


class CampaignConfigError(CampaignError):
    """The campaign configuration is malformed, unbounded, or unbound."""


class CampaignBindingError(CampaignError):
    """A repository/branch/spec/plan/state binding does not match."""


class CampaignGitError(CampaignError):
    """A trusted Git operation failed or produced an unsafe result."""


class CampaignRecoveryError(CampaignError):
    """Git/plan/state recovery is ambiguous; fail closed for operator review."""


class CampaignResultError(CampaignError):
    """The campaign result or a phase-result artifact violates its schema."""


class CampaignPhaseError(CampaignError):
    """A phase step is invoked outside its transition-table contract."""


class CampaignFindingsError(CampaignError):
    """Fail-closed findings-flow error (Task 10, §16, FIND-01).

    Raised when the receipt-backed findings of a verification/audit phase
    cannot be minted or consumed for the next planner: malformed, stale,
    synthetic, foreign, or receipt-only findings claims, or a receipt that
    the hardened no-follow bounded reader rejects, all fail the campaign
    closed before the next planner launches.
    """


# ---------------------------------------------------------------------------
# Machine models
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RoleOutcome:
    """The machine-readable outcome of one untrusted role process.

    ``exit_status`` is the process exit code; ``interrupted`` is true when
    the role ended on a bounded signal/timeout (never the model's choice);
    ``signal`` names the terminating signal when known. ``diagnostic`` is one
    bounded redacted process-tail line for operator diagnosis only. The
    orchestrator never interprets it or any other role prose as control
    protocol — only this enum surface, the plan state, the Git state, and
    deterministic gates decide a phase outcome.
    """

    role: str
    exit_status: int
    interrupted: bool = False
    signal: Optional[str] = None
    diagnostic: str = ""


@dataclass(frozen=True)
class PhaseRecord:
    """One trusted phase-step record of the campaign result history.

    ``result_digest`` is the SHA-256 of the exact structured phase-result
    bytes the orchestrator consumed (verification/audit only; empty for
    every other phase).  Task 10 §16: it is the digest the orchestrator
    mints into the findings receipt, so the next-round findings authority
    can re-bind every receipt to the exact result bytes this run read — a
    tampered receipt whose digest contradicts the recorded run fails
    closed.
    """

    round: int
    phase: str
    attempt: int
    outcome: str
    head_commit: str
    plan_digest: str
    detail: str = ""
    result_digest: str = ""

    def to_dict(self) -> Dict[str, object]:
        return {
            "round": self.round,
            "phase": self.phase,
            "attempt": self.attempt,
            "outcome": self.outcome,
            "head_commit": self.head_commit,
            "plan_digest": self.plan_digest,
            "detail": self.detail,
            "result_digest": self.result_digest,
        }


@dataclass(frozen=True)
class CampaignResult:
    """Machine-readable terminal result (``factory-campaign-result/v1``)."""

    campaign_id: str
    rounds_requested: int
    rounds_completed: int
    terminal_phase: str
    terminal_outcome: str
    head_commit: str
    phase_history: Tuple[PhaseRecord, ...] = ()

    def to_dict(self) -> Dict[str, object]:
        return {
            "schema": SCHEMA_NAME,
            "campaign_id": self.campaign_id,
            "rounds_requested": self.rounds_requested,
            "rounds_completed": self.rounds_completed,
            "terminal_phase": self.terminal_phase,
            "terminal_outcome": self.terminal_outcome,
            "head_commit": self.head_commit,
            "exit_code": TERMINAL_EXIT_CODES[self.terminal_phase],
            "phase_history": [record.to_dict() for record in self.phase_history],
        }

    def validate(self) -> None:
        if self.terminal_phase not in TERMINAL_PHASES:
            raise CampaignResultError(
                f"terminal_phase {self.terminal_phase!r} is not a terminal phase"
            )
        if self.terminal_phase not in TERMINAL_EXIT_CODES:
            raise CampaignResultError("terminal phase has no documented exit code")
        if not SHA40_RE.fullmatch(self.head_commit):
            raise CampaignResultError(
                "head_commit must be a 40-character lowercase Git commit hash"
            )
        if not SAFE_CAMPAIGN_ID_RE.fullmatch(self.campaign_id):
            raise CampaignResultError(
                "campaign_id must match the safe campaign-id pattern"
            )
        if (
            isinstance(self.rounds_requested, bool)
            or not isinstance(self.rounds_requested, int)
            or self.rounds_requested < 1
        ):
            raise CampaignResultError("rounds_requested must be a positive integer")
        if (
            isinstance(self.rounds_completed, bool)
            or not isinstance(self.rounds_completed, int)
            or not (0 <= self.rounds_completed <= self.rounds_requested)
        ):
            raise CampaignResultError(
                "rounds_completed must be between zero and rounds_requested"
            )
        if self.terminal_phase == "readiness_complete":
            if self.terminal_outcome != "readiness_complete" or self.rounds_completed != 0 or self.phase_history:
                raise CampaignResultError("readiness-only cannot impersonate campaign success")
        if self.terminal_phase == "success":
            if self.rounds_completed != self.rounds_requested:
                raise CampaignResultError("campaign success requires every requested round")
            completed_audits = {record.round for record in self.phase_history if record.phase == "audit"}
            if completed_audits != set(range(1, self.rounds_requested + 1)):
                raise CampaignResultError("campaign success requires complete passing phase history")
            if any(
                record.phase == "verification"
                and record.outcome
                == "software_verified_external_acceptance_blocked"
                for record in self.phase_history
            ):
                raise CampaignResultError(
                    "campaign success is impossible when verification history "
                    "contains software_verified_external_acceptance_blocked"
                )
        for record in self.phase_history:
            if record.round < 1 or record.round > self.rounds_requested:
                raise CampaignResultError(
                    f"phase record round {record.round} exceeds the round budget"
                )
            if record.phase not in PHASE_OUTCOME_SETS:
                raise CampaignResultError(f"unknown phase {record.phase!r}")
            if record.outcome not in PHASE_OUTCOME_SETS[record.phase]:
                raise CampaignResultError(
                    f"outcome {record.outcome!r} is not an outcome of phase "
                    f"{record.phase!r}"
                )
            if not SHA40_RE.fullmatch(record.head_commit):
                raise CampaignResultError(
                    "phase record head_commit must be a 40-hex commit hash"
                )
            if not SHA256_RE.fullmatch(record.plan_digest):
                raise CampaignResultError(
                    "phase record plan_digest must be a 64-hex SHA-256 digest"
                )
            if record.result_digest and not SHA256_RE.fullmatch(
                record.result_digest
            ):
                raise CampaignResultError(
                    "phase record result_digest must be a 64-hex SHA-256 "
                    "digest or empty"
                )
            if record.attempt < 1:
                raise CampaignResultError("phase record attempt must be positive")


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CampaignConfig:
    """The trusted campaign contract (bounds, bindings, and role seams).

    ``root`` is the canonical repository; ``spec_path``/``spec_commit``/
    ``spec_blob`` come from the committed plan front matter and are
    re-verified against the locked repository; ``plan_path`` is the
    repository-relative canonical plan; ``phase_base_commit`` is the
    campaign's initial bound base (the commit the first planning phase works
    from).  ``planning_attempts`` and ``implementation_attempts`` bound each
    round/task; the campaign never exceeds them. Production requires explicit
    provider/model/backend. Production providers are fixed by the launch
    authority (currently ``ollama`` and ``openai-codex``); ``synthetic`` is
    explicit and test-only;
    ``role_driver`` is the explicit deterministic embedded/fixture role seam
    (a committed repository-relative executable) or ``None`` for the real
    launch path.
    """

    root: Path
    campaign_id: str
    rounds_requested: int
    branch: str
    spec_path: str
    spec_commit: str
    spec_blob: str
    plan_path: str
    phase_base_commit: str
    planning_attempts: int
    implementation_attempts: int
    specification_digest: str
    plan_digest: str
    role_prompt_digests: Mapping[str, str]
    prompt_set_digest: str
    audit_objectives_digest: str
    pre_round_registry: pre_round_module.Registry
    pre_round_implementation_digests: Mapping[str, str]
    pre_round_hook_configuration_digest: str
    pre_round_hook_commit: str
    provider: str = ""
    model: str = ""
    backend: str = ""
    role_driver: Optional[str] = None
    developer_evidence_path: str = ""
    scenario_path: str = ""
    acceptance_command: Tuple[str, ...] = ()
    verification_command: Tuple[str, ...] = ()
    capability_command: Tuple[str, ...] = ()
    runner_command: Tuple[str, ...] = ()
    phase_result_path: str = ""
    audit_result_path: str = ""
    state_namespace: str = ""
    accepted_commit: str = ""
    install_manifest: str = ""
    role_timeout: float = DEFAULT_ROLE_TIMEOUT
    gate_timeout: float = DEFAULT_GATE_TIMEOUT
    runner_timeout: float = DEFAULT_RUNNER_TIMEOUT
    campaign_timeout: float = DEFAULT_CAMPAIGN_TIMEOUT
    runtime_limit: float = launch_module.DEFAULT_RUNTIME_LIMIT
    inactivity_limit: float = launch_module.DEFAULT_INACTIVITY_LIMIT
    readiness_only: bool = False

    def __post_init__(self) -> None:
        # Fail closed at construction: an invalid campaign contract can never
        # reach a phase step.  ``derive_campaign_config`` builds only valid
        # contracts; a caller-built or operator-built model is rejected here
        # before any Git/state/role authority is touched.
        self.validate()

    def validate(self) -> None:
        if not SAFE_CAMPAIGN_ID_RE.fullmatch(self.campaign_id):
            raise CampaignConfigError(
                "campaign_id must be canonical lowercase and match "
                "`[a-z0-9](?:[a-z0-9._-]{0,62}[a-z0-9])?`"
            )
        if (
            isinstance(self.rounds_requested, bool)
            or not isinstance(self.rounds_requested, int)
            or self.rounds_requested < 1
        ):
            raise CampaignConfigError("rounds_requested must be a positive integer")
        if (
            isinstance(self.planning_attempts, bool)
            or not isinstance(self.planning_attempts, int)
            or self.planning_attempts < 1
        ):
            raise CampaignConfigError("planning_attempts must be a positive integer")
        if (
            isinstance(self.implementation_attempts, bool)
            or not isinstance(self.implementation_attempts, int)
            or self.implementation_attempts < 1
        ):
            raise CampaignConfigError(
                "implementation_attempts must be a positive integer"
            )
        for name, value in (
            ("branch", self.branch),
            ("spec_path", self.spec_path),
            ("spec_commit", self.spec_commit),
            ("spec_blob", self.spec_blob),
            ("plan_path", self.plan_path),
            ("phase_base_commit", self.phase_base_commit),
        ):
            if not isinstance(value, str) or not value:
                raise CampaignConfigError(f"`{name}` must be a non-empty string")
        if not SHA40_RE.fullmatch(self.spec_commit) or not SHA40_RE.fullmatch(
            self.spec_blob
        ):
            raise CampaignConfigError(
                "spec_commit and spec_blob must be 40-hex Git object IDs"
            )
        if not SHA40_RE.fullmatch(self.phase_base_commit):
            raise CampaignConfigError(
                "phase_base_commit must be a 40-hex Git commit hash"
            )
        for name, value in (
            ("specification_digest", self.specification_digest),
            ("plan_digest", self.plan_digest),
            ("audit_objectives_digest", self.audit_objectives_digest),
            ("prompt_set_digest", self.prompt_set_digest),
            ("pre_round_hook_configuration_digest", self.pre_round_hook_configuration_digest),
        ):
            if not isinstance(value, str) or not SHA256_RE.fullmatch(value):
                raise CampaignConfigError(
                    f"`{name}` must be a 64-hex SHA-256 digest"
                )
        if not isinstance(self.pre_round_registry, pre_round_module.Registry):
            raise CampaignConfigError("pre-round registry must be parsed by the trusted authority")
        try:
            derived_hook_digest = pre_round_module.configuration_digest(
                self.pre_round_registry,
                self.pre_round_implementation_digests,
                bound_commit=self.pre_round_hook_commit,
            )
        except pre_round_module.PreRoundError as exc:
            raise CampaignConfigError(f"invalid pre-round hook binding: {exc}") from exc
        if derived_hook_digest != self.pre_round_hook_configuration_digest:
            raise CampaignConfigError("pre-round hook configuration digest mismatch")
        if not SHA40_RE.fullmatch(self.pre_round_hook_commit):
            raise CampaignConfigError("pre-round hook commit must be exact SHA-1")
        if (
            not isinstance(self.role_prompt_digests, Mapping)
            or not self.role_prompt_digests
        ):
            raise CampaignConfigError(
                "role_prompt_digests must be a non-empty mapping"
            )
        for role, digest in self.role_prompt_digests.items():
            if (
                not isinstance(role, str)
                or not isinstance(digest, str)
                or not SHA256_RE.fullmatch(digest)
            ):
                raise CampaignConfigError(
                    "role_prompt_digests must map each role to a 64-hex digest"
                )
        if type(self.readiness_only) is not bool:
            raise CampaignConfigError("readiness_only must be boolean")
        if (not isinstance(self.provider, str) or not self.provider) and not self.readiness_only:
            raise CampaignConfigError("production campaign provider is mandatory")
        if (not isinstance(self.model, str) or not self.model) and not self.readiness_only:
            raise CampaignConfigError("production campaign model is mandatory")
        if not isinstance(self.backend, str):
            raise CampaignConfigError("campaign backend must be a string")
        provider = self.provider.lower()
        if provider and provider not in launch_module.SUPPORTED_PROVIDERS:
            raise CampaignConfigError(
                "unknown provider; the fixed launch-provider policy fails closed"
            )
        if provider != "synthetic" and self.role_driver is not None:
            raise CampaignConfigError(
                "the embedded role-driver seam is the explicit fixture surface; "
                "a production provider must use the real launch path"
            )
        if self.role_driver is None and not self.backend and not self.readiness_only:
            raise CampaignConfigError(
                "production campaign backend is mandatory when no test-only role driver is used"
            )
        if not self.verification_command and not self.readiness_only:
            raise CampaignConfigError(
                "campaign requires an explicit non-empty verification_command "
                "before any role can launch (fixtures must supply their safe verifier)"
            )
        for name in (
            "verification_command", "acceptance_command", "capability_command",
            "runner_command",
        ):
            command = getattr(self, name)
            if any(not isinstance(item, str) or not item for item in command):
                raise CampaignConfigError(
                    f"{name} must be an argv of non-empty strings"
                )
        if self.role_driver is None:
            if self.runner_command and tuple(self.runner_command) != RUNNER_COMMAND:
                raise CampaignConfigError(
                    "when configured, production runner_command must be exactly "
                    "./.factory/tools/run-factory-runners.py with no arguments or shell"
                )
            if not self.acceptance_command and not self.readiness_only:
                raise CampaignConfigError(
                    "production campaign requires an explicit non-empty acceptance_command"
                )
            for name in ("acceptance_command", "capability_command"):
                command = getattr(self, name)
                if command and not command[0].startswith("./"):
                    raise CampaignConfigError(
                        f"production {name} must use a canonical repository-relative ./path"
                    )
            expected_namespace = (
                f"{CAMPAIGN_STATE_PARENT_REL}/{self.campaign_id}"
            )
            if self.state_namespace != expected_namespace:
                raise CampaignConfigError(
                    "production campaign state must use the fresh dedicated "
                    f"namespace {expected_namespace!r}"
                )
            if not SHA40_RE.fullmatch(self.accepted_commit):
                raise CampaignConfigError(
                    "production campaign requires an exact accepted_commit binding"
                )
            if self.pre_round_hook_commit != self.accepted_commit:
                raise CampaignConfigError(
                    "production hook commit must equal the accepted commit"
                )
            if not self.install_manifest or not Path(self.install_manifest).is_absolute():
                raise CampaignConfigError(
                    "production campaign requires an absolute verified install manifest path"
                )
        if self.role_driver is not None:
            if (
                not _SOURCE_FIXTURE_SEAMS_AVAILABLE
                and self.role_driver != EVIDENCE_SMOKE_DRIVER_REL
            ):
                raise CampaignConfigError(
                    "the general fixture role-driver seam is unavailable to "
                    "installed production callers"
                )
            if provider != "synthetic":
                raise CampaignConfigError(
                    "the test-only role-driver seam requires the synthetic provider"
                )
            if not self.role_driver or self.role_driver.startswith("/"):
                raise CampaignConfigError(
                    "the role driver must be a repository-relative committed "
                    "path, never an absolute or operator path"
                )
            if any(
                segment in ("", ".", "..")
                for segment in self.role_driver.split("/")
            ):
                raise CampaignConfigError(
                    "the role driver path must not contain empty, `.`, or `..` "
                    "segments"
                )
        if self.developer_evidence_path:
            unsafe_evidence, reason = _unsafe_repo_relative(
                self.developer_evidence_path
            )
            if unsafe_evidence:
                raise CampaignConfigError(
                    f"developer evidence path is not a safe repository-relative "
                    f"path: {reason}"
                )
            if not self.developer_evidence_path.startswith(
                EVIDENCE_SMOKE_EVIDENCE_PREFIX
            ):
                raise CampaignConfigError(
                    "developer evidence must live under the bounded harness-owned "
                    f"`{EVIDENCE_SMOKE_EVIDENCE_PREFIX}` namespace"
                )
            if self.developer_evidence_path == self.plan_path:
                raise CampaignConfigError(
                    "developer evidence must not collide with the canonical plan"
                )
        for name in ("scenario_path", "phase_result_path", "audit_result_path"):
            value = getattr(self, name)
            if value and not _safe_result_relpath(value):
                raise CampaignConfigError(
                    f"{name} must be a safe repository-relative path"
                )
        for name in ("phase_result_path", "audit_result_path"):
            value = getattr(self, name)
            expected_prefix = (
                self.state_namespace + "/"
                if self.state_namespace
                else ".factory-state/"
            )
            if not value or not value.startswith(expected_prefix):
                raise CampaignConfigError(
                    f"{name} is mandatory and must be an exact path under "
                    f"{expected_prefix!r}"
                )
        if self.phase_result_path == self.audit_result_path:
            raise CampaignConfigError(
                "tester and auditor structured result paths must be distinct"
            )
        for name in (
            "role_timeout", "gate_timeout", "runner_timeout", "campaign_timeout",
            "runtime_limit", "inactivity_limit",
        ):
            value = getattr(self, name)
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or value <= 0
                or float(value) != float(value)
                or float(value) == float("inf")
            ):
                raise CampaignConfigError(
                    f"`{name}` must be a finite positive number of seconds"
                )
        if self.runner_timeout > MAX_RUNNER_TIMEOUT:
            raise CampaignConfigError(
                f"runner_timeout must not exceed {MAX_RUNNER_TIMEOUT:g} seconds"
            )
        if self.campaign_timeout > MAX_CAMPAIGN_TIMEOUT:
            raise CampaignConfigError(
                f"campaign_timeout must not exceed {MAX_CAMPAIGN_TIMEOUT:g} seconds"
            )


# ---------------------------------------------------------------------------
# Trusted descriptor-anchored Git authority
# ---------------------------------------------------------------------------


class TrustedGit:
    """The descriptor-anchored Git/history/status/commit authority.

    Every Git operation of a campaign runs under the root-descriptor lock
    through the pinned absolute Git executable (``gitutil.GIT_EXECUTABLE``)
    anchored to the locked inode (``lock.RootLock``), so a canonical-path
    rebind can never redirect a read or a commit and a caller-controlled
    ``PATH`` or ``GIT_CONFIG*`` environment can never substitute a different
    binary.  The model never executes Git; the orchestrator stages and
    commits exactly the allowlisted dirty scope it verified.
    """

    def __init__(self, lock: object, plan_path: str) -> None:
        if lock is None:
            raise CampaignGitError(
                "the trusted Git authority requires the root-descriptor lock"
            )
        self._lock = lock
        self._plan_path = plan_path

    def _run(self, argv: Sequence[str], *, timeout: Optional[float] = None):
        # Task 9 review L4: every trusted Git call is bounded by a finite
        # timeout; an unbounded trusted Git wait behind the lock is never
        # permitted.
        return self._lock._git_run(list(argv), timeout=timeout or GIT_TIMEOUT)

    def _bytes(self, argv: Sequence[str], *, timeout: Optional[float] = None):
        return self._lock._git_bytes(
            list(argv), timeout=timeout or GIT_TIMEOUT
        )

    def head(self) -> str:
        result = self._run(["rev-parse", "--verify", "HEAD"])
        if result.returncode != 0:
            raise CampaignGitError("cannot resolve HEAD of the canonical repository")
        value = result.stdout.strip()
        if not SHA40_RE.fullmatch(value):
            raise CampaignGitError("resolved HEAD is not a 40-hex commit hash")
        return value

    def object_id(self, revision: str) -> str:
        """Resolve one commit-bound object id through the locked Git authority."""
        result = self._run(["rev-parse", "--verify", revision])
        value = result.stdout.strip()
        if result.returncode != 0 or not SHA40_RE.fullmatch(value):
            raise CampaignGitError(f"cannot resolve exact Git object {revision!r}")
        return value

    def is_ancestor(self, ancestor: str, descendant: str) -> bool:
        result = self._run(["merge-base", "--is-ancestor", ancestor, descendant])
        if result.returncode not in (0, 1):
            raise CampaignGitError(f"cannot test ancestry {ancestor}..{descendant}")
        return result.returncode == 0

    def status_entries(self) -> List[Tuple[str, str]]:
        """``(status_flags, repository-relative path)`` porcelain pairs.

        Untracked entries are enumerated per file (``--untracked-files=all``),
        never as a whole-directory ``?? dir/`` marker, so the exact dirty
        scope the orchestrator stages and commits is always a set of concrete
        repository-relative file paths; a directory summary would stage files
        the caller never explicitly allowlisted and break the exact staged
        scope equality in :meth:`commit`.

        Task 9 review L1: rename/copy entries (``R``/``C`` index flags),
        whole-directory untracked markers (a path ending in ``/``, which is
        how Git summarizes an untracked nested repository), and any tracked
        gitlink (submodule) path are rejected explicitly — the orchestrator
        never guesses how to stage a move or a submodule pointer, so each
        dirty path is always one explicit create/modify/delete of a regular
        committed file.  Quoted (ambiguous) porcelain output is rejected the
        same way.
        """
        result = self._bytes(["status", "--porcelain", "-z", "--untracked-files=all"])
        if result.returncode != 0:
            raise CampaignGitError("cannot read the Git status")
        raw = result.stdout
        if len(raw) > STATUS_PORCELAIN_MAX:
            raise CampaignGitError("the Git status is oversized")
        entries: List[Tuple[str, str]] = []
        index = 0
        while index < len(raw):
            end = raw.find(b"\x00", index)
            if end < 0:
                raise CampaignGitError("malformed porcelain status stream")
            entry = raw[index:end]
            if len(entry) < 4:
                raise CampaignGitError("malformed porcelain status entry")
            flags = entry[0:2]
            path_bytes = entry[3:]
            if flags[0:1] in (b"R", b"C"):
                # A rename/copy record is ``XY old\0new\0``: the second
                # NUL-terminated path is the rename/copy target.  The
                # orchestrator never interprets a move implicitly.
                raise CampaignGitError(
                    "rename/copy status entries are not explicit scope paths; "
                    f"refusing ambiguous porcelain output ({entry!r})"
                )
            if path_bytes.endswith(b"/"):
                # A whole-directory untracked marker (e.g. an untracked
                # nested repository) is never an explicit file path.
                raise CampaignGitError(
                    "a whole-directory status marker is not an explicit file "
                    f"path; refusing to interpret {path_bytes!r}"
                )
            if path_bytes[:1] == b'"':
                raise CampaignGitError(
                    "a dirty path requires quoting; refusing to interpret "
                    "ambiguous porcelain output"
                )
            decoded = path_bytes.decode("utf-8", "replace")
            if any(
                ord(char) < 0x20 or ord(char) == 0x7F for char in decoded
            ):
                # Task 11 (Task 9 residual): a control-character path is
                # never explicit trusted commit scope — the orchestrator
                # cannot stage, name, or reason about a filename with a
                # control byte deterministically, and the exact staged-scope
                # equality in ``commit`` must never match an ambiguous byte
                # sequence.  Reject it at the porcelain layer before any
                # allowlist/commit decision.
                raise CampaignGitError(
                    "a dirty path contains control characters; refusing to "
                    "interpret unsafe porcelain output"
                )
            entries.append(
                (flags.decode("ascii", "replace"), decoded)
            )
            index = end + 1
        if entries:
            gitlinks = self._gitlinks([path for _, path in entries])
            if gitlinks:
                raise CampaignGitError(
                    "dirty paths include tracked gitlink/submodule "
                    f"path(s) {sorted(gitlinks)!r}; the orchestrator never "
                    "stages a submodule pointer"
                )
        return entries

    def _gitlinks(self, paths: Sequence[str]) -> List[str]:
        """Tracked gitlink (submodule, mode 160000) paths among ``paths``.

        A gitlink is repository state owned by the parent repository's
        submodule pointer, never explicit role file work; staging or
        committing one would move a foreign repository's pointer behind the
        guarded boundary (Task 9 review L1).
        """
        if not paths:
            return []
        result = self._bytes(["ls-files", "-s", "-z", "--", *paths])
        if result.returncode != 0:
            raise CampaignGitError("cannot enumerate tracked path modes")
        raw = result.stdout
        gitlinks: List[str] = []
        index = 0
        while index < len(raw):
            end = raw.find(b"\x00", index)
            if end < 0:
                raise CampaignGitError("malformed ls-files stream")
            record = raw[index:end]
            if len(record) < 7:
                raise CampaignGitError("malformed ls-files record")
            mode = record[:6]
            tab = record.find(b"\t")
            if tab < 0:
                raise CampaignGitError("malformed ls-files record")
            path = record[tab + 1:].decode("utf-8", "replace")
            if mode == b"160000":
                gitlinks.append(path)
            index = end + 1
        return gitlinks

    def status_paths(self) -> List[str]:
        """Repository-relative dirty paths (modified/added/untracked)."""
        return [path for _, path in self.status_entries()]

    def role_dirty_paths(self) -> List[str]:
        """Dirty paths attributable to an untrusted role.

        The orchestrator's own runtime namespaces (the mutable control
        state, digest ledger, and published results under ``.factory-state``)
        are created by the trusted control plane and are never role work;
        untracked entries there are
        skipped.  Any *tracked* modification under those namespaces can only
        have been made by an untrusted role and still surfaces as a
        violation.
        """
        paths: List[str] = []
        for flags, path in self.status_entries():
            if flags == "??" and _is_harness_runtime(path):
                continue
            paths.append(path)
        return paths

    def dirty(self) -> bool:
        return bool(self.role_dirty_paths())

    def raw_dirty(self) -> bool:
        return bool(self.status_paths())

    def diff_paths(self, base: str) -> List[str]:
        """Repository-relative paths changed between ``base`` and HEAD."""
        result = self._bytes(["diff", "--name-only", "-z", base, "HEAD"])
        if result.returncode != 0:
            raise CampaignGitError(f"cannot diff {base}..HEAD")
        raw = result.stdout
        if not raw:
            return []
        return [item.decode("utf-8", "replace") for item in raw.split(b"\x00") if item]

    def blob_at(self, commit: str, relpath: str) -> bytes:
        """The exact blob bytes of ``<commit>:<relpath>`` (bounded)."""
        resolved = self._run(["rev-parse", f"{commit}:{relpath}"])
        if resolved.returncode != 0:
            raise CampaignGitError(f"path {relpath!r} is not tracked at {commit}")
        blob = resolved.stdout.strip()
        if not SHA40_RE.fullmatch(blob):
            raise CampaignGitError(f"path {relpath!r} at {commit} does not resolve")
        raw = self._bytes(["cat-file", "blob", blob])
        if raw.returncode != 0:
            raise CampaignGitError(f"cannot read blob {blob}")
        if len(raw.stdout) > PLAN_BLOB_MAX:
            raise CampaignGitError(f"blob {blob} exceeds the {PLAN_BLOB_MAX}-byte bound")
        return raw.stdout

    def plan_at(self, commit: str):
        """Parsed committed plan at ``commit`` (fail-closed on any violation)."""
        try:
            return plan_parser.Plan.from_bytes(self.blob_at(commit, self._plan_path))
        except plan_parser.PlanError as exc:
            raise CampaignGitError(
                f"the committed plan at {commit} does not parse: {exc}"
            ) from exc

    def restore(self, paths: Sequence[str]) -> None:
        """Restore repository-relative worktree paths from the committed HEAD.

        The orchestrator restores exactly the regenerable plan path (never
        product work) when an untrusted role left an unparsable/unbound plan
        in the worktree, so a deterministic task failure proceeds to
        verification at the last coherent committed state.  The command runs
        through the pinned anchored executable and never commits.
        """
        paths = [path for path in paths if path]
        if not paths:
            return
        result = self._run(["checkout", "HEAD", "--", *paths])
        if result.returncode != 0:
            raise CampaignGitError(
                f"cannot restore {paths!r}: {result.stderr[-2000:]}"
            )

    def commit(self, paths: Sequence[str], message: str) -> str:
        """Stage and commit exactly ``paths`` with the orchestrator identity.

        The command runs through the pinned anchored executable with the
        sanitized environment (``GIT_CONFIG*`` and redirect overrides
        stripped) and never passes ``--no-verify``, hook-path overrides, or
        worktree options; the committed Git commit guard stays authoritative.
        The fixed author identity makes every campaign commit provably
        orchestrator-created.  Returns the new HEAD.
        """
        paths = [path for path in paths if path]
        if not paths:
            raise CampaignGitError("refusing to commit an empty path set")
        add = self._run(["add", "--", *paths])
        if add.returncode != 0:
            raise CampaignGitError(f"cannot stage {paths!r}: {add.stderr[-2000:]}")
        staged = self._bytes(["diff", "--cached", "--name-only", "-z"])
        if staged.returncode != 0:
            raise CampaignGitError("cannot read the staged path set")
        staged_names = {
            item.decode("utf-8", "replace")
            for item in staged.stdout.split(b"\x00")
            if item
        }
        if not staged_names:
            raise CampaignGitError(
                "the attempt produced no staged changes; an empty commit "
                "manufactures metadata-only progress"
            )
        if set(staged_names) != set(paths):
            raise CampaignGitError(
                f"staged scope {sorted(staged_names)} does not equal the allowed "
                f"scope {sorted(paths)}; refusing a foreign commit"
            )
        commit_result = self._run(
            [
                "-c", f"user.name={COMMIT_AUTHOR_NAME}",
                "-c", f"user.email={COMMIT_AUTHOR_EMAIL}",
                "commit", "-m", message,
            ]
        )
        if commit_result.returncode != 0:
            raise CampaignGitError(
                f"orchestrator commit rejected: {commit_result.stderr[-2000:]}"
            )
        new_head = self.head()
        if not SHA40_RE.fullmatch(new_head):
            raise CampaignGitError("commit did not advance HEAD deterministically")
        return new_head

    def history(self, limit: int) -> List[str]:
        """Most recent ``limit`` commit hashes (newest first)."""
        if limit < 1 or limit > 4096:
            raise CampaignGitError("history bound must be within 1..4096")
        result = self._run(["log", "--format=%H", f"--max-count={limit}"])
        if result.returncode != 0:
            raise CampaignGitError("cannot read the committed Git history")
        values = [line for line in result.stdout.splitlines() if line]
        for value in values:
            if not SHA40_RE.fullmatch(value):
                raise CampaignGitError("history contains a malformed commit hash")
        return values

    def commit_count(self) -> int:
        result = self._run(["rev-list", "--count", "HEAD"])
        if result.returncode != 0:
            raise CampaignGitError("cannot count the committed history")
        try:
            return int(result.stdout.strip())
        except ValueError as exc:
            raise CampaignGitError("commit count is not an integer") from exc


# ---------------------------------------------------------------------------
# Deterministic scope validation
# ---------------------------------------------------------------------------


def scope_violation(
    paths: Sequence[str],
    *,
    phase: str,
    plan_path: str,
    spec_path: str,
    allow_paths: Sequence[str] = (),
) -> Optional[str]:
    """First scope violation among ``paths`` for ``phase``, or ``None``.

    No untrusted role may touch the Git history, runtime state, the
    canonical spec, the plan of a phase that does not own it, or any
    forbidden namespace.  The planner may change exactly the plan path; the
    developer may change product entries, tests, and the plan; the tester
    and auditor are strictly read-only except for their designated
    structured-result path (``allow_paths``).  A violation is a
    deterministic fail-closed finding (``task_failed`` /
    ``infrastructure_failure``).
    """
    allowed = set(allow_paths)
    for path in paths:
        if path in allowed:
            continue
        if path == ".factory":
            # Task 11 (Task 9 residual): a *bare* ``.factory`` dirty path is
            # never role work — the hidden harness namespace root may not be
            # created, renamed, or committed by any role (a bare entry would
            # shadow or replace the committed control-plane surface).  Even
            # though Landlock already prevents model creation, the trusted
            # commit-scope layer rejects it as defense in depth.
            return (
                f"{path!r} is the hidden harness namespace root; no role may "
                "create or commit a bare .factory entry"
            )
        for prefix in FORBIDDEN_SCOPE_PREFIXES:
            if path == prefix or path.startswith(prefix + "/"):
                return f"{path!r} is inside the forbidden {prefix!r} namespace"
        # Task 9 review HIGH: the trusted policy/harness surface is shared
        # with the confinement write allowlists
        # (``workspace_confinement.TRUSTED_POLICY_RELPATHS``) — AGENTS.md,
        # the hidden CI/forge tooling, shell.nix, the factory configs, the
        # harness docs, and the legacy ``.factory/tools/`` security surface.  An
        # untrusted role must never write *or commit* these paths, so the
        # orchestrator refuses them exactly like the model workspace does.
        if confinement_authority.is_trusted_policy_path(path):
            return (
                f"{path!r} is trusted policy/harness surface (AGENTS.md, "
                "hidden CI/forge tooling, factory configs, harness docs, "
                "legacy scripts); no role may write or commit it"
            )
        if path == spec_path:
            return f"{path!r} is the canonical specification; no role may edit it"
        if phase == "planning":
            if path != plan_path:
                return (
                    f"{path!r} is outside the planner's sole writable path "
                    f"{plan_path!r}"
                )
        elif phase == "implementation":
            if path.startswith(".factory/") and path != plan_path:
                return (
                    f"{path!r} is harness state outside the developer's plan "
                    f"path {plan_path!r}"
                )
        else:  # verification and audit are strictly read-only
            return f"{path!r} was modified by the read-only {phase!r} phase"
    return None


def plan_sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _unsafe_repo_relative(path: str) -> Tuple[Optional[str], Optional[str]]:
    """``(unsafe_render, reason)`` for a path that is not repo-relative-safe.

    A safe repository-relative path is non-empty, not absolute, free of
    empty/``.``/``..`` segments, and free of backslashes and NUL/control
    characters (Task 9 review L2).  ``(None, None)`` when the path is safe.
    """
    if not path or path.startswith("/"):
        return (path or "<empty>", "absolute or empty")
    if any(segment in ("", ".", "..") for segment in path.split("/")):
        return (path, "contains an empty, `.`, or `..` segment")
    if "\\" in path or any(ch in path for ch in "\x00\n\r"):
        return (path, "contains a backslash or control character")
    return (None, None)


HARNESS_RUNTIME_PREFIXES = (
    ".factory-state", ".ralph", ".pi", "$tmp"
)


def _is_harness_runtime(path: str) -> bool:
    """True when ``path`` lives in a namespace owned by the trusted harness."""
    for prefix in HARNESS_RUNTIME_PREFIXES:
        if path == prefix or path.startswith(prefix + "/"):
            return True
    return False


def _safe_result_relpath(relpath: str) -> bool:
    """True when ``relpath`` is a safe repository-relative transient path.

    The transient phase-result handoff channel must stay inside the
    repository: no absolute path, no dot-segment/``..`` traversal, no empty
    path, and no control characters.  The symlink-component check is done by
    the caller against the real repository root (a symlink in any parent
    component redirects the transient write outside the repository and fails
    closed; REQ 4).
    """
    if not relpath or relpath.startswith("/"):
        return False
    path = Path(relpath)
    if path.is_absolute():
        return False
    if not path.parts or any(part in ("", ".", "..") for part in path.parts):
        return False
    for part in path.parts:
        if any(ord(char) < 0x20 for char in part):
            return False
    return True


def sanitized_gate_environment(
    environ: Optional[Mapping[str, str]] = None,
) -> Dict[str, str]:
    """The stripped allowlisted environment deterministic gates receive.

    Deterministic gates and acceptance commands never receive the full
    parent environment (Task 11): only the documented benign keys of
    :data:`GATE_ENV_ALLOWLIST` are copied, every lock metadata key and Git
    redirector is stripped as defense in depth, and any surviving
    credential-shaped key fails closed — a credential in the operator's
    environment can never reach a gate child.
    """
    parent = os.environ if environ is None else environ
    environment: Dict[str, str] = {}
    for key in GATE_ENV_ALLOWLIST:
        if key in parent:
            environment[key] = parent[key]
    nix_remote = environment.get("NIX_REMOTE")
    if nix_remote is not None and nix_remote != "daemon":
        raise CampaignError("deterministic gates require NIX_REMOTE=daemon")
    nix_path = environment.get("NIX_PATH")
    if nix_path is not None:
        match = re.fullmatch(r"nixpkgs=(/nix/store/[0-9a-z]{32}-[^:]+)", nix_path)
        if match is None:
            raise CampaignError("refusing a non-canonical deterministic gate NIX_PATH")
        source = match.group(1)
        try:
            info = os.stat(source, follow_symlinks=False)
        except OSError as exc:
            raise CampaignError("cannot validate deterministic gate NIX_PATH") from exc
        if (
            not stat.S_ISDIR(info.st_mode)
            or info.st_uid == os.getuid()
            or info.st_mode & 0o022
            or os.path.realpath(source) != source
        ):
            raise CampaignError("deterministic gate NIX_PATH is not immutable store data")
    environment = lock_module.stripped_child_env(environment)
    environment = gitutil.sanitize_git_environment(environment)
    for key in environment:
        upper = key.upper()
        if any(
            token in upper
            for token in ("COOKIE", "TOKEN", "PASSWORD", "PASSWD", "API_KEY",
                          "SECRET", "CREDENTIAL", "PRIVATE_KEY", "AUTH")
        ):
            raise CampaignError(
                f"refusing to spawn a deterministic gate with credential-shaped "
                f"environment key {key!r}"
            )
    return environment


def _bounded_read(root: Path, relpath: str, label: str, maximum: int) -> bytes:
    """Bounded no-follow read of a worktree file."""
    path = Path(root) / relpath
    flags = (
        os.O_RDONLY
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise CampaignError(f"cannot open {label} {path}: {exc}") from exc
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise CampaignError(f"{label} {path} is not a regular file")
        if before.st_size > maximum:
            raise CampaignError(f"{label} {path} exceeds the {maximum}-byte bound")
        raw = bytearray()
        while True:
            chunk = os.read(descriptor, 65536)
            if not chunk:
                break
            raw.extend(chunk)
            if len(raw) > maximum:
                raise CampaignError(f"{label} {path} exceeds the bound")
        after = os.fstat(descriptor)
        if (
            before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns
        ) != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns):
            raise CampaignError(f"{label} {path} changed while being read")
        return bytes(raw)
    finally:
        os.close(descriptor)


def validate_plan_worktree(
    data: bytes,
    *,
    spec_path: str,
    spec_commit: str,
    spec_blob: str,
    base_commit: str,
) -> Tuple[bool, Optional[str]]:
    """``(valid, reason)``: the worktree plan parses and keeps the binding.

    A plan that changes the specification path/commit/blob binding or the
    cycle base commit is rejected as unbound — the plan contract (PLAN-01)
    is part of the acceptance boundary and free-form prose cannot alter it.
    """
    try:
        plan = plan_parser.Plan.from_bytes(data)
    except plan_parser.PlanError as exc:
        return False, f"worktree plan does not parse: {exc}"
    if plan.spec_path != spec_path:
        return False, f"plan spec_path {plan.spec_path!r} != bound {spec_path!r}"
    if plan.spec_commit != spec_commit:
        return False, f"plan spec_commit {plan.spec_commit!r} != bound {spec_commit!r}"
    if plan.spec_blob != spec_blob:
        return False, f"plan spec_blob {plan.spec_blob!r} != bound {spec_blob!r}"
    if plan.base_commit != base_commit:
        return False, f"plan base_commit {plan.base_commit!r} != bound {base_commit!r}"
    return True, None


# ---------------------------------------------------------------------------
# Deterministic outcome classification (§13; pure functions of trusted inputs)
# ---------------------------------------------------------------------------


def classify_planning(
    *,
    role: RoleOutcome,
    plan_changed: bool,
    plan_valid: bool,
    scope_ok: bool,
) -> str:
    """Classify one planning attempt (planned/failed/interrupted).

    ``planned`` requires a changed, valid, spec-bound plan within the
    planner scope; ``interrupted`` records a bounded process interruption;
    everything else is a deterministic planning failure retried until the
    planning budget exhausts (then terminal ``failed``).
    """
    if role.interrupted:
        return "interrupted"
    if role.exit_status != 0:
        return "failed"
    if not plan_changed:
        return "failed"
    if not plan_valid:
        return "failed"
    if not scope_ok:
        return "failed"
    return "planned"


def classify_implementation(
    *,
    role: RoleOutcome,
    plan_valid: bool,
    task_complete: bool,
    acceptance_pass: bool,
    had_changes: bool,
    scope_ok: bool,
) -> str:
    """Pure classification of one developer attempt (§13.2).

    ``task_completed`` requires a coherent, committed, acceptance-passing
    completion of the selected task; ``task_progress`` preserves committed
    work toward an ``in_progress`` task; a bounded process interruption is
    ``interrupted``; every deterministic failure — a nonzero machine-readable
    exit status (the §13 model-process-failed signal), an invalid plan, a
    rejected completion claim, no usable work, or a scope violation — is
    ``task_failed`` (Task 9 review L3).
    """
    if role.interrupted or role.exit_status < 0:
        return "interrupted"
    if role.exit_status != 0:
        # §13: the harness derives outcomes from the machine-readable exit
        # status as well as plan/Git state and gates; a nonzero developer
        # exit is a deterministic model-process failure and is never
        # accepted as a completion even when the worktree looks complete
        # (the orchestrator preserves the coherent work and retries the
        # same task while the attempt budget remains).
        return "task_failed"
    if not scope_ok:
        return "task_failed"
    if not plan_valid:
        return "task_failed"
    if task_complete:
        if acceptance_pass and had_changes:
            return "task_completed"
        # The completion claim failed its deterministic acceptance gate or
        # produced no coherent commit: it is never accepted.
        return "task_failed"
    if had_changes:
        return "task_progress"
    return "task_failed"


def classify_verification(
    *,
    role: RoleOutcome,
    scope_ok: bool,
    gate_ran: bool,
    gate_exit: int,
    tester_result_valid: bool,
    tester_result_outcome: Optional[str],
    findings: Sequence[str],
    blocked_refs: Sequence[str],
    capability_available: bool,
    capability_ran: bool = True,
    capability_exit: int = 0,
    gate_skipped: bool = False,
    capability_skipped: bool = False,
) -> str:
    """Pure verification classification (§13.3).

    ``infrastructure_failure`` when the verifier itself cannot be trusted
    (interrupted verifier, read-only violation, missing/invalid structured
    result, unrun gate); ``pass`` when both the deterministic gate and the
    tester pass; ``findings`` when a deterministic check fails or a finding
    is reported; ``blocked`` only when a required declared capability
    cannot execute and the tester cited exact unavailable references;
    ``software_verified_external_acceptance_blocked`` when the deterministic
    gate passed, no finding remains, the declared capability is available,
    and the tester cited exact blocked references — software is fully
    verified while external release acceptance (human approval, real-system
    evidence, or an unavailable external release authority) remains blocked.
    The new outcome advances to the independent audit and can never produce
    campaign success (an audit ``pass`` entered from it resolves to the
    terminal ``blocked`` state in the final round).

    ``gate_ran`` reflects an **explicit deterministic verification
    command** executed by the trusted orchestrator (Task 9 review MED):
    the tester's structured result alone is never a gate, and an absent
    verification command fails the campaign closed as
    ``infrastructure_failure``.
    """
    if role.interrupted or role.exit_status != 0:
        return "infrastructure_failure"
    if not scope_ok:
        return "infrastructure_failure"
    if not gate_ran:
        return "infrastructure_failure"
    # 126/127 and the supervisor's negative timeout/binding statuses mean the
    # fixed verifier/toolchain did not execute. They are infrastructure, never
    # product findings, capability blockers, or fake skips. A genuine verifier
    # exit 1 remains a software/acceptance finding.
    if gate_exit < 0 or gate_exit in (126, 127):
        return "infrastructure_failure"
    if capability_ran and (
        capability_exit < 0 or capability_exit in (126, 127)
    ):
        return "infrastructure_failure"
    if not tester_result_valid:
        return "infrastructure_failure"
    # A trusted command that reports a skip did run but did not exercise
    # acceptance. It is a finding only after the tester handoff is valid; a
    # missing/malformed handoff remains infrastructure and can never reach
    # findings redaction with a null result object.
    if gate_skipped:
        return "findings"
    if capability_skipped:
        # A skipped capability probe never supports pass. It is an honest
        # external blocker only when the tester also supplied exact blocker
        # references and no higher-precedence finding; every other composition
        # is a verification finding.
        if (
            tester_result_outcome == "blocked" and blocked_refs
            and not findings and gate_exit == 0
        ):
            return "blocked"
        return "findings"
    if gate_exit != 0:
        return "findings"
    if not capability_available or (capability_ran and capability_exit != 0):
        # An unavailable/failed declared capability can never disappear behind
        # a tester's optimistic pass. Exact blockers stay blocked only when
        # the tester supplied their machine-readable references; otherwise the
        # failed acquisition/check is an honest verification finding.
        if tester_result_outcome == "blocked" and blocked_refs and not findings:
            return "blocked"
        return "findings"
    # Findings always win over blockers and over the role's claimed outcome.
    # A contradictory pass is rejected by the conditional schema before this
    # classifier, while a blocked result carrying findings remains findings.
    if findings:
        return "findings"
    if tester_result_outcome == "blocked":
        if not blocked_refs:
            return "infrastructure_failure"
        # A capability probe that executed and returned an ordinary nonzero
        # status is an honest unavailable external capability. A skipped probe
        # is never passing evidence but remains an explicit blocker rather than
        # being confused with command-not-found/permission infrastructure.
        if not capability_available:
            return "blocked"
        # The deterministic gate passed, no finding remains, and the declared
        # capability is available: the tester's exact blocked references are
        # external release acceptance (human approval, real-system evidence,
        # or an unavailable external release authority), not a verification
        # failure.  Software is fully verified while external acceptance
        # remains blocked; the outcome advances to the independent audit and
        # can never produce campaign success.
        return "software_verified_external_acceptance_blocked"
    if tester_result_outcome == "findings":
        return "findings"
    return "pass"


def classify_audit(
    *,
    role: RoleOutcome,
    scope_ok: bool,
    result_valid: bool,
    outcome: Optional[str],
    findings: Sequence[str],
    blocked_refs: Sequence[str],
) -> str:
    """Classify one audit attempt (§13.4).

    An interrupted or untrusted audit fails the campaign closed
    (``interrupted``/``infrastructure_failure``); a valid structured result
    classifies ``pass``/``findings``/``blocked`` with the §14 precedence
    rule (any finding makes it ``findings`` even when external blockers
    exist).
    """
    if role.interrupted:
        return "interrupted"
    if role.exit_status != 0:
        return "infrastructure_failure"
    if not scope_ok:
        return "infrastructure_failure"
    if not result_valid:
        return "infrastructure_failure"
    if findings:
        return "findings"
    if blocked_refs:
        return "blocked"
    if outcome == "pass":
        return "pass"
    return "findings"


# ---------------------------------------------------------------------------
# Machine-result schema validation
# ---------------------------------------------------------------------------


def _load_schema(name: str) -> Dict[str, object]:
    here = Path(__file__).resolve().parents[1]  # .factory/
    path = here / "schemas" / name
    try:
        descriptor = os.open(
            path,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0),
        )
    except OSError as exc:
        raise CampaignResultError(f"cannot open the committed schema {path}: {exc}") from exc
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise CampaignResultError(
                f"the committed schema {path} is not a regular file"
            )
        if info.st_uid != os.getuid() or info.st_mode & 0o022:
            raise CampaignResultError(
                f"the committed schema {path} is not owned/private; the "
                "schema authority fails closed"
            )
        if info.st_size > 256 * 1024:
            raise CampaignResultError(f"the committed schema {path} is oversized")
        data = os.read(descriptor, info.st_size + 1)
        if len(data) > 256 * 1024:
            raise CampaignResultError(f"the committed schema {path} is oversized")
    finally:
        os.close(descriptor)
    try:
        schema = json.loads(data)
    except ValueError as exc:
        raise CampaignResultError(f"the committed schema {path} is not JSON") from exc
    if not isinstance(schema, dict):
        raise CampaignResultError(f"the committed schema {path} is not an object")
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
    raise CampaignResultError(
        f"value of type {type(value).__name__} is not JSON-serializable"
    )


def _schema_check(instance: object, schema: object, path: str) -> None:
    """Validate against the JSON-Schema subset the committed schemas use."""
    if not isinstance(schema, dict):
        return
    expected = schema.get("type")
    if expected is not None:
        types = expected if isinstance(expected, list) else [expected]
        if _json_type(instance) not in types:
            raise CampaignResultError(
                f"phase-result violation at {path or '(root)'}: expected "
                f"{expected!r}, got {_json_type(instance)!r}"
            )
    if "enum" in schema and instance not in schema["enum"]:
        raise CampaignResultError(
            f"phase-result violation at {path or '(root)'}: value {instance!r} "
            f"is not one of {schema['enum']!r}"
        )
    if "const" in schema and instance != schema["const"]:
        raise CampaignResultError(
            f"phase-result violation at {path or '(root)'}: value {instance!r} "
            f"does not equal {schema['const']!r}"
        )
    if isinstance(instance, str):
        if "minLength" in schema and len(instance) < schema["minLength"]:
            raise CampaignResultError(
                f"phase-result violation at {path or '(root)'}: string below the minimum"
            )
        if "pattern" in schema and re.fullmatch(schema["pattern"], instance) is None:
            raise CampaignResultError(
                f"phase-result violation at {path or '(root)'}: pattern mismatch"
            )
    if isinstance(instance, (int, float)) and not isinstance(instance, bool):
        if "minimum" in schema and instance < schema["minimum"]:
            raise CampaignResultError(
                f"phase-result violation at {path or '(root)'}: value below minimum"
            )
    if isinstance(instance, dict):
        properties = schema.get("properties", {})
        for key in schema.get("required", []):
            if key not in instance:
                raise CampaignResultError(
                    f"phase-result violation at {path or '(root)'}: missing {key!r}"
                )
        for key, subschema in properties.items():
            if key in instance:
                _schema_check(instance[key], subschema, f"{path}.{key}")
        if schema.get("additionalProperties") is False:
            for key in instance:
                if key not in properties:
                    raise CampaignResultError(
                        f"phase-result violation at {path or '(root)'}: unexpected "
                        f"property {key!r}"
                    )
    if isinstance(instance, list):
        if "minItems" in schema and len(instance) < schema["minItems"]:
            raise CampaignResultError(
                f"phase-result violation at {path or '(root)'}: array below the minimum"
            )
        if "maxItems" in schema and len(instance) > schema["maxItems"]:
            raise CampaignResultError(
                f"phase-result violation at {path or '(root)'}: array above the maximum"
            )
        items = schema.get("items")
        if items is not None:
            for index, item in enumerate(instance):
                _schema_check(item, items, f"{path}[{index}]")
    for subschema in schema.get("allOf", []):
        _schema_check(instance, subschema, path)
    conditional = schema.get("if")
    if conditional is not None:
        try:
            _schema_check(instance, conditional, path)
            matched = True
        except CampaignResultError:
            matched = False
        branch = schema.get("then") if matched else schema.get("else")
        if branch is not None:
            _schema_check(instance, branch, path)


_RESULT_SCHEMA: Optional[Dict[str, object]] = None


def validate_campaign_result(result: object) -> None:
    """Fail closed unless the campaign result conforms to the committed schema."""
    global _RESULT_SCHEMA
    if _RESULT_SCHEMA is None:
        _RESULT_SCHEMA = _load_schema(RESULT_SCHEMA_FILE)
    instance = result.to_dict() if isinstance(result, CampaignResult) else result
    _schema_check(instance, _RESULT_SCHEMA, "")


_PHASE_RESULT_SCHEMA: Optional[Dict[str, object]] = None


def phase_result_schema() -> Dict[str, object]:
    global _PHASE_RESULT_SCHEMA
    if _PHASE_RESULT_SCHEMA is None:
        _PHASE_RESULT_SCHEMA = _load_schema(PHASE_RESULT_SCHEMA_FILE)
    return _PHASE_RESULT_SCHEMA


def _reject_duplicate_keys(pairs: List[tuple]) -> Dict[str, object]:
    """JSON object-pairs hook: reject any repeated object key.

    A duplicate key silently overwrites its predecessor under a plain
    ``dict`` decode and can hide a drifted authority; the live phase-result
    gate rejects it instead (EVID-02 duplicate-key rejection).
    """
    result: Dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise CampaignResultError(
                f"duplicate JSON object key in phase result: {key!r}"
            )
        result[key] = value
    return result


def read_phase_result(
    root: Path, relpath: str, label: str
) -> Optional[Tuple[Dict[str, object], str, bytes]]:
    """Bounded no-follow read + schema validation of one role result file.

    Returns ``(data, raw_digest, raw_bytes)`` where ``raw_digest`` is the
    SHA-256 of the exact result bytes the orchestrator read, or ``None``
    when the result file does not exist or is empty (a pre-created transient
    handoff the role never filled carries no structured result).  Any
    unsafe, oversized, malformed, or non-conforming file raises
    :class:`CampaignResultError` — the untrusted phase's structured output
    is only ever interpreted through this gate, and the digest is what the
    Task 10 findings receipt binds.  The raw bytes are what the Task 10
    authority preserves as the exact trusted content a findings receipt
    authenticates (REQ 3).

    **Secure removal on every path (Task 23):** the transient handoff file
    is removed in a ``finally`` — on a successful parse, on an empty file,
    and on every parse/schema/oversize/error path — so a malformed or
    secret-laden raw result can never persist on disk.  The unlink is
    *secure*: the pathname is removed only when it still names the exact
    inode that was opened and read (a role rewrite, a symlink substitution,
    or any swap between the read and the cleanup is never deleted).
    """
    if not relpath:
        return None
    path = Path(root) / relpath
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        descriptor = os.open(path, flags)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise CampaignResultError(f"cannot open the {label} result file {path}: {exc}") from exc
    opened_identity: Optional[Tuple[int, int]] = None

    def secure_unlink() -> None:
        """Remove the handoff pathname only when it still names the read inode."""
        if opened_identity is None:
            return
        try:
            named = os.lstat(path)
        except OSError:
            return
        if (named.st_dev, named.st_ino) != opened_identity:
            # The pathname no longer names the file that was read (the role
            # rewrote it or an attacker substituted it); never delete a
            # different file.
            return
        try:
            os.unlink(path)
        except OSError:
            pass

    try:
        try:
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode):
                raise CampaignResultError(f"{label} result {path} is not a regular file")
            opened_identity = (info.st_dev, info.st_ino)
            if info.st_size > MAX_RESULT_FILE:
                raise CampaignResultError(f"{label} result {path} is oversized")
            before = (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)
            raw = bytearray()
            while True:
                chunk = os.read(descriptor, 65536)
                if not chunk:
                    break
                raw.extend(chunk)
                if len(raw) > MAX_RESULT_FILE:
                    raise CampaignResultError(f"{label} result {path} is oversized")
            after = os.fstat(descriptor)
            if before != (
                after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns
            ):
                raise CampaignResultError(f"{label} result {path} changed while being read")
        finally:
            os.close(descriptor)
        raw_bytes = bytes(raw)
        if not raw_bytes:
            # A pre-created transient handoff file the role never filled is
            # exactly the same as an absent result (no structured output);
            # it is removed like every other consumed handoff.
            return None
        try:
            data = json.loads(
                raw_bytes.decode("utf-8"),
                object_pairs_hook=_reject_duplicate_keys,
            )
        except (ValueError, UnicodeDecodeError) as exc:
            raise CampaignResultError(f"{label} result {path} is not valid JSON: {exc}") from exc
        if not isinstance(data, dict):
            raise CampaignResultError(f"{label} result {path} is not an object")
        _schema_check(data, phase_result_schema(), "")
        raw_digest = plan_sha256(raw_bytes)
        return data, raw_digest, raw_bytes
    finally:
        # Every path — parse, schema, oversize, empty, or success — removes
        # the transient raw result bytes; a malformed secret-laden result
        # leaves no bytes and no path.
        secure_unlink()


# ---------------------------------------------------------------------------
# Production launch integration (launch/usage/confinement)
# ---------------------------------------------------------------------------


def _blob_at(root: Path, relpath: str, *, revision: str = "HEAD") -> bytes:
    result = gitutil.git_run(
        ["-C", str(root), "rev-parse", f"{revision}:{relpath}"],
        timeout=GIT_TIMEOUT,
    )
    if result.returncode != 0:
        raise CampaignError(f"blob {relpath!r} is not tracked at HEAD")
    raw = gitutil.git_bytes(
        ["-C", str(root), "cat-file", "blob", result.stdout.strip()],
        timeout=GIT_TIMEOUT,
    )
    if raw.returncode != 0:
        raise CampaignError(f"cannot read blob {relpath!r}")
    if len(raw.stdout) > PLAN_BLOB_MAX:
        raise CampaignError(f"blob {relpath!r} is oversized")
    return raw.stdout


def _derive_pre_round_binding(
    root: Path,
    *,
    bound_commit: str,
    git: Optional["TrustedGit"] = None,
) -> Tuple[pre_round_module.Registry, Mapping[str, str], str]:
    """Derive one single-commit registry binding, descriptor-anchored when locked."""
    if not SHA40_RE.fullmatch(bound_commit):
        raise CampaignBindingError("pre-round binding requires an exact commit")
    def blob(relpath: str) -> bytes:
        if git is not None:
            return git.blob_at(bound_commit, relpath)
        return _blob_at(root, relpath, revision=bound_commit)
    registry = pre_round_module.parse_registry(
        blob(".factory/pre-round-hooks.json")
    )
    source = blob(".factory/loop/pre_round.py")
    available = {
        "branch_guard": plan_sha256(
            source + b"\x00" + blob(".factory/loop/campaign.py")
            + b"\x00" + blob(".factory/loop/state.py")
            + b"\x00" + blob(".factory/loop/lock.py")
            + b"\x00" + blob(".factory/loop/gitutil.py")
        ),
    }
    names = {hook.implementation for hook in registry.hooks}
    implementation_digests = {
        name: available[name] for name in available if name in names
    }
    digest = pre_round_module.configuration_digest(
        registry, implementation_digests, bound_commit=bound_commit
    )
    return registry, implementation_digests, digest


def _live_head(root: Path) -> str:
    result = gitutil.git_run(
        ["-C", str(root), "rev-parse", "--verify", "HEAD"],
        timeout=GIT_TIMEOUT,
    )
    if result.returncode != 0:
        raise CampaignGitError(f"cannot resolve HEAD of {root}")
    value = result.stdout.strip()
    if len(value) != 40:
        raise CampaignGitError("HEAD is not a 40-hex commit hash")
    return value


def _audit_objective_bytes(root: Path, round_number: int) -> bytes:
    """Deterministic per-round audit-objective bytes from the committed registry.

    The registry blob is read from the committed state at HEAD and validated
    (``audit-objectives/v1``); the objective for the round is selected
    deterministically (``index = (round - 1) % len``, §6.4) and serialized
    with the exact deterministic JSON encoding the registry CLI prints, so
    the same registry and round always produce the same bytes and digest
    (Task 9 review B2).
    """
    registry = _blob_at(root, ".factory/audit-objectives/registry.json")
    document = audit_objectives_module.parse_registry(registry)
    selected = audit_objectives_module.select_audit_objective(
        round_number, document
    )
    return json.dumps(
        dict(selected), sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def launch_role_attempt(
    config: CampaignConfig,
    *,
    role: str,
    head: str,
    task_id: Optional[int] = None,
    round_number: int = 1,
    task_excerpt: Optional[bytes] = None,
    audit_objective: Optional[bytes] = None,
    findings_payload: Optional[bytes] = None,
    _authorization_store: Optional[object] = None,
    _authorization_token: str = "",
    _authorization_claims: Optional[Mapping[str, object]] = None,
) -> RoleOutcome:
    """Run one fresh role attempt through the committed launch authority.

    Reads every authoritative blob from the committed state at ``head``
    (role prompt, operational policy, spec, plan, and the developer task
    excerpt / auditor objective), binds the digests, and mints the
    unforgeable verified-committed token (:func:`launch.authorize_launch`),
    which applies the Task 8 real Landlock confinement. The fixed quota
    decision runs in the trusted campaign parent before every invocation.

    Task 9 review B2: the developer's task bytes are re-derived from the
    committed plan blob at ``head`` (:func:`launch.derive_task_excerpt`) and
    the auditor's objective bytes are re-derived from the committed
    audit-objective registry (:func:`_audit_objective_bytes`) when the hidden
    suite has not supplied them explicitly; the exact bytes and digests are
    passed to :func:`launch.authorize_launch`, which fails closed on any
    substitution, paraphrase, or digest drift.  Any :class:`InvocationError`
    (a refused launch: unbound bytes, unknown provider, guard/quota refusal,
    unavailable confinement) is caught cleanly and raised as a documented
    :class:`CampaignPhaseError`, so a refused launch is a clean fail-closed
    control-plane outcome and never an unhandled traceback.

    Task 10 §16: ``findings_payload`` is the deterministic receipt-backed
    findings payload of the previous round, delivered only to the planner
    role as a digest-bound input (never to the developer, tester, or
    auditor, and never read by the deterministic selector).

    Confinement is not caller-configurable: ``authorize_launch`` internally
    creates the private home, canonical descriptor-anchored specification,
    and real proof for every role/provider.
    """
    root = Path(config.root).absolute()
    if config.backend:
        backend = Path(config.backend)
    else:
        raise CampaignConfigError(
            "a real launch requires the committed model backend (`backend`)"
        )
    result_write_path = ""
    if role == "tester":
        if not config.phase_result_path:
            raise CampaignConfigError(
                "tester launch requires the exact structured phase-result path"
            )
        result_write_path = str((root / config.phase_result_path).absolute())
    elif role == "auditor":
        if not config.audit_result_path:
            raise CampaignConfigError(
                "auditor launch requires the exact structured audit-result path"
            )
        result_write_path = str((root / config.audit_result_path).absolute())
    plan_blob = _blob_at(root, config.plan_path)
    try:
        if role not in config.role_prompt_digests:
            # Task 9 review LOW: a missing committed role-prompt digest is a
            # clean fail-closed control-plane error (a launch can never bind
            # an unverifiable role prompt), never an unhandled KeyError.
            raise CampaignPhaseError(
                f"the committed role-prompt digests carry no digest for "
                f"{role!r}; a launch cannot bind an unverifiable role prompt"
            )
        if role == "developer":
            if task_excerpt is None:
                task_excerpt, _ = launch_module.derive_task_excerpt(
                    plan_blob, task_id
                )
        elif role == "auditor":
            if audit_objective is None:
                audit_objective = _audit_objective_bytes(root, round_number)
        binding = launch_module.InvocationBinding(
            role=role,
            model=config.model,
            provider=config.provider,
            backend=backend,
            workspace=root,
            bound_commit=head,
            role_prompt_digest=config.role_prompt_digests[role],
            prompt_set_digest=config.prompt_set_digest,
            plan_digest=plan_sha256(plan_blob),
            policy_digest=plan_sha256(_blob_at(root, "AGENTS.md")),
            specification_digest=config.specification_digest,
            allowed_tools=launch_module.DEFAULT_ALLOWED_TOOLS[role],
            task_id=task_id,
            task_excerpt_digest=(
                plan_sha256(task_excerpt) if task_excerpt is not None else None
            ),
            audit_objective_digest=(
                plan_sha256(audit_objective) if audit_objective is not None else ""
            ),
            findings_digest=(
                plan_sha256(findings_payload)
                if findings_payload is not None
                else ""
            ),
            result_write_path=result_write_path,
            runtime_limit=config.runtime_limit,
            inactivity_limit=config.inactivity_limit,
        )
        launch_module.verify_invocation(binding)
        authority = launch_module.authorize_launch(
            binding,
            role_prompt=_blob_at(root, f".factory/prompts/{role}.md"),
            agents=_blob_at(root, "AGENTS.md"),
            spec=_blob_at(root, config.spec_path),
            plan=plan_blob,
            audit_objective=audit_objective,
            task_excerpt=task_excerpt,
            findings=findings_payload,
            _authorization_store=_authorization_store,
            _authorization_token=_authorization_token,
            _authorization_claims=_authorization_claims,
        )
    except launch_module.InvocationError as exc:
        # Task 9 review B2: every refused launch — unbound or missing bytes,
        # an unknown provider, a guard/quota refusal, an unavailable
        # confinement, or a malformed invocation binding — is a clean
        # fail-closed campaign outcome (CampaignPhaseError), never an
        # unhandled InvocationError traceback.
        raise CampaignPhaseError(f"launch refused for {role}: {exc}") from exc
    supervisor = launch_module.LaunchSupervision(binding)
    try:
        result = supervisor.run(authority)
    except launch_module.InvocationError as exc:
        # A bounded supervision fail-closed (invariant, reap, or interrupted
        # group) is a clean campaign fail-closed error, never a traceback.
        raise CampaignPhaseError(
            f"supervision fail-closed for {role}: {exc}"
        ) from exc
    stderr = getattr(getattr(result, "stderr", None), "tail", "") or ""
    stdout = getattr(getattr(result, "stdout", None), "tail", "") or ""
    reason = getattr(result, "reason", "") or ""
    # Stream tails have already crossed LaunchSupervision's exact-commit
    # redaction boundary. Preserve only one bounded single-line diagnostic so
    # an infrastructure exit is actionable without treating model prose as
    # control protocol or publishing raw process output.
    diagnostic = " ".join((reason or stderr or stdout).split())[:512]
    if result.outcome == "terminated" or (
        result.returncode is not None and result.returncode < 0
    ):
        return RoleOutcome(
            role, result.returncode, interrupted=True, signal=result.signal,
            diagnostic=diagnostic,
        )
    return RoleOutcome(
        role, result.returncode or 0, interrupted=False, diagnostic=diagnostic
    )


# ---------------------------------------------------------------------------
# Phase steps
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Step:
    """One trusted phase-step result."""

    record: PhaseRecord
    state: Optional[state_module.FactoryState] = None
    terminal: Optional[str] = None
    retry: bool = False


class Campaign:
    """The trusted phase/campaign orchestrator (one writer, serialized).

    ``run()`` acquires the exclusive root-descriptor lock with the mandatory
    identity/branch/spec/plan bindings and drives the §11 state machine to a
    §14 terminal.  Every Git operation goes through :class:`TrustedGit`
    behind the lock; every untrusted phase is guarded by the before/after
    state-digest ledger; every transition is written through the established
    no-follow control-state authority.  ``role_runner`` is the phase driver
    seam (the embedded fixture driver when ``config.role_driver`` is set,
    otherwise the real launch authority).
    """

    def __init__(
        self,
        config: CampaignConfig,
        *,
        role_runner: Optional[object] = None,
    ) -> None:
        config.validate()
        self._config = config
        self._root = Path(config.root).absolute()
        self._lock: Optional[lock_module.RootLock] = None
        self._git: Optional[TrustedGit] = None
        self._role_runner = role_runner
        self._planning_attempts_used = 0
        self._rounds_completed = 0
        self._deadline: Optional[float] = None
        # Task 11: the verified credential-guard redactor for deterministic
        # gate output, built lazily on the first gate run (fail closed when
        # the exact committed guard cannot be verified).
        self._redactor: Optional[object] = None
        # Task 12: the deterministic verifier bound (committed blob, secure
        # identity, retained inode descriptor) *before* the untrusted phase;
        # every gate execution re-validates it and fails closed on any
        # pathname/content/committed-tree substitution.
        self._held_verifier: Optional[evidence_module.HeldVerifier] = None
        # Production capability acceptance and coordinator-owned runner
        # acquisition are independently exact-commit bound; neither falls
        # back to the verification descriptor or a workspace-resolved command.
        self._held_capability: Optional[evidence_module.HeldVerifier] = None
        self._held_runner: Optional[evidence_module.HeldVerifier] = None
        self._held_runner_checker: Optional[evidence_module.HeldVerifier] = None
        # Task 22 (B1): the role driver is bound to its exact committed
        # blob/identity/inode descriptor *before* planning and every role
        # execution re-validates it, then executes the pinned interpreter
        # with the descriptor path (/proc/self/fd/<fd>) through pass_fds —
        # no pathname exec, so a substitution after binding can never
        # substitute the executed bytes.
        self._held_driver: Optional[evidence_module.HeldVerifier] = None
        # Task 22 (B2): the acceptance gate is bound to its exact committed
        # descriptor *before* planning and both acceptance and verification
        # modes execute through the same retained-fd authority — no pathname
        # asymmetry between the two gate lanes.
        self._held_acceptance: Optional[evidence_module.HeldVerifier] = None
        # Task 10 §16: the phase records of THIS run, consumed by the findings
        # authority to bind every previous-round receipt to a phase that
        # actually ran and classified findings/blocked.
        self._records: List[PhaseRecord] = []
        self._launch_store: Optional[readiness_module.AuthorizationStore] = None
        self._readiness_document: Optional[Dict[str, object]] = None

    # -- acquisition ------------------------------------------------------------

    def _acquire(self) -> None:
        spec = lock_module.SpecBinding(
            self._config.spec_path, self._config.spec_commit, self._config.spec_blob
        )
        spec.validate()
        head = _live_head(self._root)
        plan = lock_module.PlanBinding(
            self._config.plan_path,
            head,
            plan_sha256(_blob_at(self._root, self._config.plan_path)),
        )
        plan.validate()
        self._lock = lock_module.RootLock(
            self._root,
            expected_identity=state_module.repository_identity(self._root),
            expected_branch=self._config.branch,
            spec=spec,
            plan=plan,
        )
        try:
            self._git = TrustedGit(self._lock, self._config.plan_path)
            # Programmatic callers are not a registry authority. Re-read and
            # re-derive every hook binding from the locked exact commit before
            # state initialization or any hook/planner execution.
            try:
                live_head = self._git.head()
                bound_commit = self._config.pre_round_hook_commit
                if not self._git.is_ancestor(bound_commit, live_head):
                    raise CampaignBindingError(
                        "pre-round hook commit is not an ancestor of live HEAD"
                    )
                registry, implementation_digests, digest = _derive_pre_round_binding(
                    self._root, bound_commit=bound_commit, git=self._git
                )
            except pre_round_module.PreRoundError as exc:
                raise CampaignBindingError(
                    f"cannot bind pre-round registry: {exc}"
                ) from exc
            if (
                registry != self._config.pre_round_registry
                or dict(implementation_digests)
                != dict(self._config.pre_round_implementation_digests)
                or digest != self._config.pre_round_hook_configuration_digest
            ):
                raise CampaignBindingError(
                    "caller pre-round registry differs from the locked exact commit"
                )
            self._bind_held_authorities()
        except BaseException:
            self._lock.release()
            self._lock = None
            self._git = None
            raise

    def _bind_held_authorities(self) -> None:
        """Bind the driver and acceptance gate to their committed descriptors.

        Task 22 (B1/B2): the role driver and the acceptance gate are opened
        ``O_RDONLY|O_NOFOLLOW|O_CLOEXEC`` and bound (committed blob, secure
        identity, retained inode) *before* any untrusted phase — the planner
        runs only after both are held, so a pathname/content substitution by
        any untrusted role fails closed instead of executing substituted
        bytes.  A script that cannot be bound fails the campaign closed at
        startup (never a silent pathname fallback).
        """
        config = self._config
        if config.role_driver:
            canonical = "./" + config.role_driver
            try:
                binding = evidence_module.bind_verifier(
                    self._root, (canonical,),
                    commit=self._git.head(), git=self._git,
                )
                self._held_driver = evidence_module.HeldVerifier(
                    self._root, binding
                )
            except evidence_module.VerifierBindingError as exc:
                raise CampaignBindingError(
                    f"the role driver {config.role_driver!r} cannot be bound "
                    f"before the campaign start: {exc}"
                ) from exc
        if config.verification_command:
            command = tuple(config.verification_command)
            try:
                binding = evidence_module.bind_verifier(
                    self._root, command,
                    commit=self._git.head(), git=self._git,
                )
                self._held_verifier = evidence_module.HeldVerifier(
                    self._root, binding
                )
            except evidence_module.VerifierBindingError as exc:
                raise CampaignBindingError(
                    "the explicit verification command cannot be bound to "
                    f"the exact pre-planning commit: {exc}"
                ) from exc
        for label, command_value, attribute in (
            ("capability", config.capability_command, "_held_capability"),
            ("acceptance", config.acceptance_command, "_held_acceptance"),
            ("runner acquisition", config.runner_command, "_held_runner"),
            (
                "runner evidence checker",
                RUNNER_CHECKER_COMMAND if config.runner_command else (),
                "_held_runner_checker",
            ),
        ):
            command = tuple(command_value)
            if command and command[0].startswith("./"):
                try:
                    binding = evidence_module.bind_verifier(
                        self._root, command,
                        commit=self._git.head(), git=self._git,
                    )
                    setattr(
                        self, attribute,
                        evidence_module.HeldVerifier(self._root, binding),
                    )
                except evidence_module.VerifierBindingError as exc:
                    raise CampaignConfigError(
                        f"the {label} gate cannot be bound before the plan "
                        f"start: {exc}"
                    ) from exc

    def _remaining_time(self, label: str) -> float:
        """Return the finite remaining campaign wall-clock budget."""
        if self._deadline is None:
            return self._config.campaign_timeout
        remaining = self._deadline - time.monotonic()
        if remaining <= 0:
            raise CampaignPhaseError(
                f"campaign wall-clock deadline expired before {label}"
            )
        return remaining

    def _gate_environment(self) -> Dict[str, str]:
        environment = {
            **sanitized_gate_environment(),
            "FACTORY_VERIFIER_ROOT": str(self._root),
        }
        if self._config.runner_command:
            environment["FACTORY_CAMPAIGN_ID"] = self._config.campaign_id
            environment["FACTORY_READINESS_NONCE"] = self._runner_readiness_nonce()
        if self._config.state_namespace:
            evidence_rel = (
                f"{self._config.state_namespace}/installed-functional-evidence.env"
            )
            environment[INSTALLED_EVIDENCE_OVERRIDE] = str(
                (self._root / evidence_rel).absolute()
            )
        return environment

    def _spawn_held_script(
        self, held: evidence_module.HeldVerifier, tail: Sequence[str],
    ) -> Tuple[Sequence[str], Optional[str], Sequence[int]]:
        """One fd-pinned execution through a pinned interpreter.

        Re-validates the retained descriptor (owner/mode/link-count/inode,
        byte digest, pathname identity, committed blob at the current head)
        and then executes the *pinned interpreter* with the descriptor path
        ``/proc/self/fd/<fd>`` as the script argument and exactly that
        descriptor in ``pass_fds`` — no ``PATH``-resolved shebang, so a
        malicious ``PATH`` or a pathname swap can never substitute the
        executed bytes (B1/B2 pinned-interpreter contract).
        """
        raw = evidence_module.revalidate_verifier(
            self._root, held.binding, git=self._git,
            current_commit=self._git.head(), held=held,
        )
        first_line = raw.split(b"\n", 1)[0].strip()
        if not (first_line.startswith(b"#!") and b"python" in first_line.lower()):
            raise CampaignBindingError(
                "the bound script is not a pinned-interpreter Python script; "
                "refusing an ambiguous interpreter dispatch"
            )
        return (
            [sys.executable, f"/proc/self/fd/{held.fd}", *tail],
            None,
            (held.fd,),
        )

    # -- control-state recovery (Git + plan + state) -----------------------------

    def _state_directory(self) -> Path:
        if self._config.state_namespace:
            return self._root / self._config.state_namespace
        return self._root / ".factory-state"

    def _state_file_exists(self) -> bool:
        return (self._state_directory() / state_module.STATE_FILE_NAME).exists()

    def _load_or_init_state(self):
        """Load or initialize the control state; return ``(state, recovered)``.

        ``recovered`` is the phase-history record of a trusted transition
        that was reconciled from Git after a crash (a committed plan or task
        completion whose state write was lost); the campaign records it in
        the phase history exactly like a live phase step.  ``None`` when no
        recovery occurred.
        """
        state_module.recover_state(self._root)
        if self._state_file_exists():
            state = state_module.load_state(
                self._root,
                expected_branch=self._config.branch,
                expected_campaign_id=self._config.campaign_id,
                expected_rounds_requested=self._config.rounds_requested,
                expected_specification_digest=self._config.specification_digest,
                expected_audit_objectives_digest=self._config.audit_objectives_digest,
                expected_role_prompt_digests=dict(self._config.role_prompt_digests),
            )
            return self._reconcile_head(state)
        state = state_module.init_state(
            self._root,
            campaign_id=self._config.campaign_id,
            rounds_requested=self._config.rounds_requested,
            specification_digest=self._config.specification_digest,
            plan_digest=self._config.plan_digest,
            role_prompt_digests=dict(self._config.role_prompt_digests),
            audit_objectives_digest=self._config.audit_objectives_digest,
            phase_base_commit=self._config.phase_base_commit,
            branch=self._config.branch,
        )
        # Publish the coordinator-owned pre-round hook sidecar (never a
        # canonical state field) bound to this campaign.
        sidecars_module.write_pre_round(
            self._root,
            sidecars_module.empty_pre_round_hooks(
                campaign_id=self._config.campaign_id,
                configuration_digest=self._config.pre_round_hook_configuration_digest,
                commit=self._config.pre_round_hook_commit,
            ),
        )
        return state, None

    def _reconcile_head(self, state: state_module.FactoryState):
        """Recover from a crash between a trusted commit and the state write.

        Returns ``(state, recovered_phase_record_or_None)``.  A terminal
        control state is returned untouched: it accepts no transition and the
        run loop refuses to re-run it (an operator resolution, never a
        re-execution).  Otherwise the state file is authoritative for
        bindings, but a trusted commit
        that landed before its ``advance`` was written is recovered
        deterministically from Git + plan + state:

        * ``planning`` with a committed plan blob at HEAD (only the plan path
          changed since the phase base): bind the new plan digest/base and
          advance ``planned``;
        * ``implementation`` with the selected task complete at HEAD's plan
          and a committed, acceptance-passing change: advance
          ``task_completed``;
        * ``implementation`` with committed in-progress work: resume the same
          task without discarding it.

        Any other Git/state divergence — HEAD behind the base, a foreign
        commit, an unparsable committed plan, a changed binding, or an
        ambiguous selected task — fails closed for operator inspection.
        """
        git = self._git
        if state.current_phase in TERMINAL_PHASES:
            return state, None
        head = git.head()
        base = state.phase_base_commit
        if head == base:
            return state, None
        if not git.is_ancestor(base, head):
            raise CampaignRecoveryError(
                f"HEAD {head} is not a descendant of the phase base {base}; "
                "the checkout was rewound or rebased and fails closed"
            )
        if state.current_phase == "planning":
            return self._reconcile_planning(state, base, head)
        if state.current_phase == "implementation":
            return self._reconcile_implementation(state, base, head)
        if state.current_phase in ("verification", "audit"):
            # Task 10 review (REQ 1): the read-only verification/audit phases
            # never commit, so HEAD advanced past the phase base only through
            # this round's own trusted planning/implementation commits.  A
            # crash in the receipt-mint window (receipt and preserved result
            # published, state advance lost) leaves exactly this state; the
            # rerun re-validates the committed scope and completes the
            # transition from the already-published trusted artifacts
            # WITHOUT re-running the untrusted role, so it never wedges on
            # its own previously published receipt.
            return self._reconcile_verification_audit(state, base, head)
        raise CampaignRecoveryError(
            f"HEAD {head} advanced past the phase base {base} during the "
            f"{state.current_phase!r} phase with no recorded transition"
        )

    def _reconcile_verification_audit(
        self, state: state_module.FactoryState, base: str, head: str
    ):
        """Resume a crashed verification/audit phase (REQ 1 crash window).

        The phase itself never commits, so ``base -> head`` must contain
        exactly implementation-scope work (the round's own trusted task
        commits plus the canonical plan), with a valid, correctly anchored
        committed plan at ``head`` — the same checks the implementation
        recovery applies.  Any foreign scope, stale plan, or unparsable plan
        fails closed for operator inspection.

        Task 10 REQ 1: when this phase already published its findings
        receipt **and** preserved phase-result artifact (a crash after the
        mint, before the state transition), the transition is completed
        from those trusted artifacts without re-running the untrusted
        role — the phase already ran, its exact result bytes were read,
        preserved, and receipted, and a re-execution could only produce a
        changed result that the byte-exact no-replace preserve would then
        reject (a wedge).  When no receipt was published the phase is
        re-run at ``head`` exactly as before (the state digest ledger
        re-validates the recorded pre-phase digest; the byte-idempotent
        preserve/mint accept a rerun that reproduces the same result).
        """
        git = self._git
        try:
            plan = plan_parser.Plan.from_bytes(
                git.blob_at(head, self._config.plan_path)
            )
        except plan_parser.PlanError as exc:
            raise CampaignRecoveryError(
                f"the committed plan at {head} does not parse: {exc}"
            ) from exc
        if plan.base_commit != self._authoritative_plan_base(state):
            raise CampaignRecoveryError(
                f"the committed plan at {head} is stale: its base_commit "
                f"{plan.base_commit!r} differs from the authoritative plan "
                f"base at the state phase base"
            )
        changed = git.diff_paths(base)
        violation = scope_violation(
            changed, phase="implementation",
            plan_path=self._config.plan_path, spec_path=self._config.spec_path,
            allow_paths=self._implementation_allow_paths(),
        )
        if violation:
            raise CampaignRecoveryError(
                f"the committed scope during the {state.current_phase!r} "
                f"phase is invalid: {violation}"
            )
        recovered = self._reconcile_published_findings(state, head)
        if recovered is not None:
            return recovered
        return state, None

    def _reconcile_published_findings(
        self, state: state_module.FactoryState, head: str
    ):
        """Complete a crashed verification/audit transition from the receipt.

        Called after the committed-scope validation when the state file
        still records the ``verification``/``audit`` phase but the round's
        findings receipt and preserved phase-result were already published
        (the crash was in the mint window, after the receipt, before the
        state advance was written).  Every binding is re-validated —
        campaign, round, phase, the exact phase-base commit (``head``), the
        outcome, the phase tag recorded in the state digest ledger, and the
        preserved result bytes digest — and a valid, intact pair completes
        the §11 transition deterministically.  Any torn, tampered, foreign,
        or missing-pair artifact fails closed for operator inspection;
        ``None`` when no receipt was published (the crash predates the
        mint and the phase re-runs normally).
        """
        phase = state.current_phase
        round_no = state.current_round
        try:
            receipt_data = findings_module.read_receipt(
                self._root, round_no, phase
            )
            preserved = findings_module.read_preserved_phase_result(
                self._root, round_no, phase
            )
        except findings_module.FindingsError as exc:
            raise CampaignRecoveryError(
                f"cannot reconcile the round {round_no} {phase} findings "
                f"artifacts: {exc}"
            ) from exc
        if receipt_data is None:
            if preserved is not None:
                raise CampaignRecoveryError(
                    f"round {round_no} {phase} has a preserved phase-result "
                    "without its findings receipt; a torn mint fails closed "
                    "for operator inspection"
                )
            return None
        receipt, _raw_digest = receipt_data
        if str(receipt["campaign_id"]) != self._config.campaign_id:
            raise CampaignRecoveryError(
                f"the round {round_no} {phase} findings receipt belongs to "
                f"campaign {receipt['campaign_id']!r}, not "
                f"{self._config.campaign_id!r}; a foreign receipt fails "
                "closed during recovery"
            )
        if int(receipt["round"]) != round_no or str(receipt["phase"]) != phase:
            raise CampaignRecoveryError(
                f"the round {round_no} {phase} findings receipt is bound to "
                f"round {receipt['round']} {receipt['phase']}; a stale "
                "receipt fails closed during recovery"
            )
        if str(receipt["phase_base_commit"]) != head:
            raise CampaignRecoveryError(
                f"the round {round_no} {phase} findings receipt binds the "
                f"phase base {receipt['phase_base_commit']} but the phase "
                f"ran at {head}; a forged receipt fails closed during "
                "recovery"
            )
        outcome = str(receipt["outcome"])
        if outcome not in ("findings", "blocked"):
            raise CampaignRecoveryError(
                f"the round {round_no} {phase} findings receipt claims "
                f"outcome {outcome!r}; only findings|blocked may carry a "
                "receipt"
            )
        try:
            ledger = state_module.read_phase_digest_ledger(self._root)
        except state_module.StateError as exc:
            raise CampaignRecoveryError(
                f"cannot read the state digest ledger during recovery: {exc}"
            ) from exc
        tag = str(receipt["phase_tag"])
        if tag not in ledger:
            raise CampaignRecoveryError(
                f"the round {round_no} {phase} findings receipt binds the "
                f"phase tag {tag!r} that the state digest ledger never "
                "recorded; a synthetic receipt fails closed during recovery"
            )
        if preserved is None:
            raise CampaignRecoveryError(
                f"the round {round_no} {phase} findings receipt has no "
                "preserved phase-result artifact; a torn or tampered mint "
                "fails closed during recovery"
            )
        result_data, result_digest = preserved
        if str(receipt["result_digest"]) != result_digest:
            raise CampaignRecoveryError(
                f"the round {round_no} {phase} findings receipt binds "
                f"result_digest {receipt['result_digest']} but the preserved "
                f"phase-result bytes digest to {result_digest}; a tampered "
                "receipt or artifact fails closed during recovery"
            )
        try:
            findings_module.validate_receipt_content(
                receipt, result_data,
                findings_module.receipt_name(round_no, phase),
            )
        except findings_module.FindingsError as exc:
            # A schema-valid but contradictory receipt (findings/blocked
            # references or outcome rewritten after the mint) fails closed
            # during recovery as a clean operator-facing campaign error,
            # never a raw findings-authority exception.
            raise CampaignRecoveryError(
                f"the round {round_no} {phase} findings receipt content "
                f"contradicts the preserved phase-result bytes: {exc}"
            ) from exc
        state2 = state_module.advance(state, outcome)
        state_module.write_state(self._root, state2)
        if state2.current_phase == "planning":
            self._rounds_completed = state2.current_round - 1
            self._planning_attempts_used = 0
        elif state2.current_phase in TERMINAL_PHASES:
            self._rounds_completed = state2.current_round
        return state2, self._record(
            state, 1, outcome,
            "reconciled from the published findings receipt",
            result_digest=result_digest,
        )

    def _reconcile_planning(
        self, state: state_module.FactoryState, base: str, head: str
    ) -> state_module.FactoryState:
        git = self._git
        changed = git.diff_paths(base)
        if changed != [self._config.plan_path]:
            raise CampaignRecoveryError(
                f"HEAD {head} advanced past the planning base {base} with a "
                f"non-plan commit scope {changed}"
            )
        plan_data = git.blob_at(head, self._config.plan_path)
        # The plan binding is the committed plan at the phase base (the same
        # anchor ``_step_planning`` validates the worktree against); the
        # recovered plan must keep that spec/base binding exactly.
        base_plan = git.plan_at(base)
        valid, reason = validate_plan_worktree(
            plan_data,
            spec_path=self._config.spec_path,
            spec_commit=self._config.spec_commit,
            spec_blob=self._config.spec_blob,
            base_commit=base_plan.base_commit,
        )
        if not valid:
            raise CampaignRecoveryError(
                f"the recovered committed plan at {head} is invalid: {reason}"
            )
        self._planning_attempts_used = 0
        state2 = state_module.advance(
            state, "planned",
            plan_digest=plan_sha256(plan_data),
            phase_base_commit=head,
        )
        return state2, self._record(state, 1, "planned", "")

    def _reconcile_implementation(
        self, state: state_module.FactoryState, base: str, head: str
    ) -> state_module.FactoryState:
        git = self._git
        task_id = state.selected_task_id
        if task_id is None:
            raise CampaignRecoveryError(
                "HEAD advanced during implementation but no task was selected"
            )
        try:
            plan = plan_parser.Plan.from_bytes(git.blob_at(head, self._config.plan_path))
        except plan_parser.PlanError as exc:
            raise CampaignRecoveryError(
                f"the committed plan at {head} does not parse: {exc}"
            ) from exc
        # Task 9 review M2: the committed plan must keep the authoritative
        # plan base anchored at the state's phase base — a plan whose front
        # matter base drifted (stale) fails closed before any completion
        # claim is accepted.
        if plan.base_commit != self._authoritative_plan_base(state):
            raise CampaignRecoveryError(
                f"the committed plan at {head} is stale: its base_commit "
                f"{plan.base_commit!r} differs from the authoritative plan "
                f"base at the state phase base"
            )
        task = next((t for t in plan.tasks if t.number == task_id), None)
        if task is None:
            raise CampaignRecoveryError(
                f"the selected task {task_id} is absent from the committed plan"
            )
        changed = git.diff_paths(base)
        violation = scope_violation(
            changed, phase="implementation",
            plan_path=self._config.plan_path, spec_path=self._config.spec_path,
            allow_paths=self._implementation_allow_paths(),
        )
        if violation:
            raise CampaignRecoveryError(
                f"the committed implementation scope is invalid: {violation}"
            )
        if task.status == "complete":
            ok, detail = self._acceptance_gate(task_id, plan)
            if not ok:
                raise CampaignRecoveryError(
                    f"the committed completion of task {task_id} does not pass "
                    f"its acceptance gate: {detail}"
                )
            state2 = state_module.advance(state, "task_completed")
            return state2, self._record(state, 1, "task_completed", "")
        if task.status == "in_progress" and changed:
            return state, None
        raise CampaignRecoveryError(
            f"HEAD advanced during implementation of task {task_id} with status "
            f"{task.status!r} and a non-progress scope"
        )

    # -- before/after untrusted-phase digest ledger ------------------------------

    def _phase_tag(self, state: state_module.FactoryState, seq: int) -> str:
        # The attempt sequence resets to 1 whenever the selector moves to a
        # new task within the same implementation phase; the tag therefore
        # anchors the selected task so two tasks can never reuse the same
        # before/after digest-ledger tag.
        task = (
            f".t{state.selected_task_id}"
            if state.selected_task_id is not None
            else ""
        )
        return (
            f"r{state.current_round}.{state.current_phase}{task}."
            f"{state.phase_started_at_monotonic}.a{seq}"
        )

    def _begin_untrusted(self, state: state_module.FactoryState, seq: int) -> str:
        tag = self._phase_tag(state, seq)
        try:
            state_module.verify_phase_digest(self._root, tag)
        except state_module.StateDigestError as exc:
            if "no recorded digest" in str(exc):
                state_module.record_phase_digest(self._root, tag)
                return tag
            raise
        return tag

    def _end_untrusted(self, tag: str) -> None:
        state_module.verify_phase_digest(self._root, tag)

    # -- role execution -----------------------------------------------------------

    def _run_role(
        self,
        role: str,
        state: state_module.FactoryState,
        head: str,
        *,
        task_id: Optional[int] = None,
        attempt: int = 1,
        findings_payload: Optional[bytes] = None,
    ) -> RoleOutcome:
        if self._role_runner is not None:
            return self._role_runner(role, state, head, task_id, attempt)
        if self._config.role_driver:
            return self._run_driver(
                role, state, head,
                task_id=task_id, attempt=attempt,
                findings_payload=findings_payload,
            )
        remaining = self._remaining_time(f"{role} launch")
        runtime_budget = min(float(self._config.runtime_limit), remaining)
        bounded = replace(
            self._config,
            runtime_limit=runtime_budget,
            inactivity_limit=min(
                float(self._config.inactivity_limit), runtime_budget
            ),
        )
        # A cache is never role authority. Reopen the accepted policy at each
        # mint and ensure campaign progress is an authorized descendant.
        policy_raw = self._git.blob_at(
            self._config.accepted_commit, readiness_module.POLICY_PATH
        )
        policy, _ = readiness_module.load_policy(self._root, policy_raw)
        if policy["production_authority"]["enrolled"] is not True:
            raise CampaignPhaseError("human_block: no production authority is enrolled")
        if not self._git.is_ancestor(self._config.accepted_commit, head):
            raise CampaignPhaseError(
                "launch current commit is not an authorized descendant of the accepted readiness commit"
            )
        # QUOTA-01 is per invocation, never per round.  The trusted parent runs
        # the fixed internal decision table immediately before every real model
        # process: check; conditional bounded wait; final check.  No quota
        # option, credential, result, or callable crosses into model argv,
        # environment, prompt, or tools.
        try:
            usage_module.require_quota(
                max_wait=max(1, int(self._remaining_time(f"{role} quota wait"))),
                max_polls=max(1, int(self._remaining_time(f"{role} quota wait"))) + 1,
            )
        except usage_module.WaitInterrupted as exc:
            return RoleOutcome(role, 128 + exc.signum, interrupted=True,
                               signal=exc.signum,
                               diagnostic="quota wait interrupted before model launch")
        except usage_module.UsageGuardError as exc:
            return RoleOutcome(role, usage_module.EXIT_FATAL,
                               diagnostic=("quota gate refused model launch: "
                                           + str(exc))[:512])
        if self._launch_store is None:
            raise CampaignPhaseError("readiness did not mint a campaign launch authority")
        plan_blob = self._git.blob_at(head, self._config.plan_path)
        claims = {
            "readiness_nonce": self._runner_readiness_nonce(),
            "phase": state.current_phase,
            "role": role,
            "task": task_id,
            "attempt": attempt,
            "round": state.current_round,
            "prompt_set_digest": bounded.prompt_set_digest,
            "role_prompt_digest": bounded.role_prompt_digests[role],
            "tools": list(launch_module.DEFAULT_ALLOWED_TOOLS[role]),
            "provider": bounded.provider,
            "model": bounded.model,
            "backend": str(Path(bounded.backend).absolute()),
            "runtime": runtime_budget,
            "current_commit": head,
            "accepted_commit": bounded.accepted_commit,
            "current_tree": self._git.text(["show", "-s", "--format=%T", head]).strip(),
            "accepted_tree": self._git.text(["show", "-s", "--format=%T", bounded.accepted_commit]).strip(),
            "plan_digest": plan_sha256(plan_blob),
            "task_excerpt_digest": (
                plan_sha256(launch_module.task_excerpt_bytes(plan_blob, task_id))
                if role == "developer" else ""
            ),
        }
        token = self._launch_store.mint(claims)
        return launch_role_attempt(
            bounded,
            role=role,
            head=head,
            task_id=task_id,
            round_number=state.current_round,
            findings_payload=findings_payload,
            _authorization_store=self._launch_store,
            _authorization_token=token,
            _authorization_claims=claims,
        )

    def _run_driver(
        self, role, state, head, *, task_id, attempt, findings_payload=None
    ) -> RoleOutcome:
        config = self._config
        driver_rel = config.role_driver
        if self._held_driver is None:
            raise CampaignBindingError(
                f"the role driver {driver_rel!r} was not bound before the "
                "campaign start; a driver cannot execute unbound"
            )
        # Task 22 (B1): no pathname exec.  The driver descriptor is bound to
        # its exact committed blob/identity/inode before planning; every role
        # execution re-validates it (owner/mode/link-count/inode, byte
        # digest, pathname identity, and the committed blob at the phase
        # head) and the child executes the pinned interpreter with the
        # descriptor path through ``pass_fds``.  The marker-bearing
        # campaign id rides in the driver argv so a leaked driver is a
        # detectable survivor (F process contract).
        try:
            driver_argv, driver_executable, driver_pass_fds = (
                self._spawn_held_script(self._held_driver, [role, config.campaign_id])
            )
        except evidence_module.VerifierBindingError as exc:
            raise CampaignBindingError(
                f"the role driver {driver_rel!r} binding failed closed: {exc}"
            ) from exc
        env = sanitized_gate_environment()
        env[CAMPAIGN_ENV_PREFIX + "ROOT"] = str(self._root)
        env[CAMPAIGN_ENV_PREFIX + "PLAN"] = config.plan_path
        env[CAMPAIGN_ENV_PREFIX + "ROLE"] = role
        env[CAMPAIGN_ENV_PREFIX + "ROUND"] = str(state.current_round)
        env[CAMPAIGN_ENV_PREFIX + "ATTEMPT"] = str(attempt)
        env[CAMPAIGN_ENV_PREFIX + "TASK_ID"] = str(task_id) if task_id is not None else ""
        env[CAMPAIGN_ENV_PREFIX + "BOUND_COMMIT"] = head
        env[CAMPAIGN_ENV_PREFIX + "SCENARIO"] = config.scenario_path
        # Task 22: the designated evidence-smoke seam receives the campaign
        # id (the private seam label) and the exact bound developer evidence
        # path so it can write the single tracked evidence artifact under
        # ``.factory/artifacts/`` deterministically; every other role driver
        # simply ignores them.
        env[CAMPAIGN_ENV_PREFIX + "CAMPAIGN_ID"] = config.campaign_id
        env[CAMPAIGN_ENV_PREFIX + "DEVELOPER_EVIDENCE"] = (
            config.developer_evidence_path
        )
        # Task 16 §22.5: the developer driver role receives the exact
        # task-excerpt digest of the committed plan at the phase head,
        # derived by the real launch authority exactly like the production
        # child environment (``launch.py``); the driver fails closed when the
        # digest is absent, so a campaign proves the developer worked the
        # exact revised selected-task bytes and nothing else (no findings
        # payload, no receipts).  The excerpt is re-derived from the exact
        # committed plan blob at ``head`` — never from the mutable worktree —
        # so a substituted or paraphrased task fails closed.
        if role == "developer" and task_id is not None:
            try:
                plan_blob = self._git.blob_at(head, config.plan_path)
                excerpt_digest = launch_module.task_excerpt_digest(
                    plan_blob, task_id
                )
            except launch_module.InvocationError as exc:
                raise CampaignPhaseError(
                    f"cannot derive the developer task-excerpt digest: {exc}"
                ) from exc
            env[CAMPAIGN_ENV_PREFIX + "TASK_EXCERPT_DIGEST"] = excerpt_digest
        # Task 10 §16: the planner role receives the deterministic
        # receipt-backed findings payload of the previous round as its only
        # findings channel (the fixture seam mirrors the digest-bound
        # prompt section of the production launch).  Other roles carry none.
        # Task 16 §22.5: the exact payload digest is delivered alongside
        # the verbatim payload bytes, exactly like the production launch
        # binds ``findings_digest`` to the payload and refuses substituted
        # prompt bytes (:func:`launch.compose_prompt`); the deterministic
        # driver fails closed unless ``sha256(FINDINGS bytes)`` equals the
        # delivered digest, so a substituted or tampered payload can never
        # satisfy the findings-revised planner.
        if role == "planner" and findings_payload is not None:
            if len(findings_payload) > 64 * 1024:
                raise CampaignFindingsError(
                    "the findings payload exceeds the driver-channel bound"
                )
            env[CAMPAIGN_ENV_PREFIX + "FINDINGS"] = findings_payload.decode(
                "utf-8"
            )
            env[CAMPAIGN_ENV_PREFIX + "FINDINGS_DIGEST"] = plan_sha256(
                findings_payload
            )
        # The structured-result handoff is role-specific: the tester may only
        # write the verification result path and the auditor only the audit
        # result path; the other roles carry no result channel at all.
        env[CAMPAIGN_ENV_PREFIX + "RESULT_FILE"] = (
            config.phase_result_path
            if role == "tester"
            else config.audit_result_path if role == "auditor" else ""
        )
        try:
            result = self._lock.spawn_child(
                driver_argv,
                executable=driver_executable,
                pass_fds=driver_pass_fds or (),
                env=env,
                timeout=config.role_timeout,
            )
        except lock_module.RootLockTimeoutError:
            return RoleOutcome(role, -1, interrupted=True, signal="SIGTERM")
        except lock_module.RootLockUnsafeError as exc:
            raise CampaignPhaseError(f"unsafe role boundary: {exc}") from exc
        except lock_module.EscapedDescendantError as exc:
            raise CampaignPhaseError(f"escaped descendant during {role}: {exc}") from exc
        if result.returncode < 0:
            return RoleOutcome(
                role, result.returncode, interrupted=True,
                signal=f"SIG{-result.returncode}",
            )
        return RoleOutcome(role, result.returncode)

    def _publish_findings(
        self,
        state: state_module.FactoryState,
        *,
        phase: str,
        phase_tag: str,
        phase_base_commit: str,
        outcome: str,
        result_digest: str,
        findings: Sequence[str],
        blocked_on: Sequence[str],
        gate_ran: bool,
        gate_exit: Optional[int],
        capability_ran: bool,
        capability_exit: Optional[int],
    ) -> None:
        """Mint one orchestrator-owned findings receipt (Task 10, §16).

        Runs only after the trusted phase classification produced
        ``findings`` or ``blocked`` and after the before/after digest-ledger
        validation of the untrusted phase passed.  The receipt binds the
        exact commit the phase ran at (``phase_base_commit``), the digest of
        the exact structured result bytes the orchestrator read, the phase
        tag recorded in the state digest ledger, and the deterministic-gate
        evidence.  Publication is write-only no-replace evidence under the
        ignored ``.factory-state/`` namespace; a pre-planted or forged
        receipt fails the mint closed.
        """
        try:
            findings_module.publish_receipt(
                self._root,
                findings_module.build_receipt(
                    campaign_id=self._config.campaign_id,
                    round_number=state.current_round,
                    phase=phase,
                    phase_tag=phase_tag,
                    phase_base_commit=phase_base_commit,
                    outcome=outcome,
                    result_digest=result_digest,
                    findings=findings,
                    blocked_on=blocked_on,
                    gate_ran=gate_ran,
                    gate_exit=gate_exit,
                    capability_ran=capability_ran,
                    capability_exit=capability_exit,
                ),
            )
        except findings_module.FindingsError as exc:
            raise CampaignFindingsError(
                f"cannot publish the round {state.current_round} {phase} "
                f"findings receipt: {exc}"
            ) from exc

    def _prepare_phase_result_file(self, relpath: str, label: str) -> None:
        """Pre-create the transient phase-result handoff file (Task 10, REQ 4).

        The read-only verification/audit phases run under real Landlock
        confinement whose write grant covers exactly the configured result
        file — never the ``.factory-state/`` breadth.  Landlock cannot grant
        the creation of a not-yet-existing file through an exact-file rule,
        so the trusted orchestrator pre-creates the mode-0600 transient file
        before the role launches; the confined role then holds exact
        read/write rights on that file and can truncate/rewrite it, while
        every sibling in ``.factory-state/`` stays denied.  An empty file the
        role never fills is treated as no structured result by
        :func:`read_phase_result`.

        The creation is dirfd/no-follow parent-safe: every parent component
        is opened with ``O_DIRECTORY|O_NOFOLLOW`` through ``openat`` and a
        symlink in any component fails closed, so a raced or planted
        symlink can never redirect the transient write outside the
        repository (Phase 2A hardening).
        """
        if not relpath:
            return
        path = Path(self._root) / relpath
        if not _safe_result_relpath(relpath):
            raise CampaignResultError(
                f"unsafe {label} result path {relpath!r}; the transient "
                "result channel must stay inside the repository with no "
                "traversal and no absolute path"
            )
        parts = Path(relpath).parts
        parent_fd = os.open(
            str(self._root),
            os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0),
        )
        try:
            for part in parts[:-1]:
                try:
                    parent_fd = os.open(
                        part,
                        os.O_RDONLY | os.O_DIRECTORY
                        | getattr(os, "O_NOFOLLOW", 0)
                        | getattr(os, "O_CLOEXEC", 0),
                        dir_fd=parent_fd,
                    )
                except OSError as exc:
                    raise CampaignResultError(
                        f"cannot open the {label} result parent component "
                        f"{part!r}: {exc}; the transient result channel "
                        "fails closed"
                    ) from exc
                try:
                    info = os.fstat(parent_fd)
                except OSError as exc:
                    raise CampaignResultError(
                        f"cannot stat the {label} result parent {part!r}: {exc}"
                    ) from exc
                if not stat.S_ISDIR(info.st_mode):
                    raise CampaignResultError(
                        f"the {label} result parent {part!r} is not a "
                        "directory; the transient result channel fails closed"
                    )
            flags = (
                os.O_WRONLY | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
                | getattr(os, "O_CLOEXEC", 0)
            )
            try:
                descriptor = os.open(parts[-1], flags, 0o600, dir_fd=parent_fd)
            except OSError as exc:
                raise CampaignResultError(
                    f"cannot pre-create the {label} result file {path}: {exc}"
                ) from exc
            try:
                info = os.fstat(descriptor)
                if not stat.S_ISREG(info.st_mode):
                    raise CampaignResultError(
                        f"the {label} result file {path} is not a regular file"
                    )
                if info.st_uid != os.getuid() or info.st_mode & 0o022:
                    raise CampaignResultError(
                        f"the {label} result file {path} is not owned/private; "
                        "the transient result channel fails closed"
                    )
                # A stale transient file from a crashed attempt is truncated so
                # the fresh role starts from an empty handoff channel.
                os.ftruncate(descriptor, 0)
            finally:
                os.close(descriptor)
        finally:
            os.close(parent_fd)

    def _preserve_phase_result(
        self, state: state_module.FactoryState, phase: str, raw_bytes: bytes
    ) -> None:
        """Preserve the exact structured phase-result bytes as evidence.

        Task 10 review (REQ 3): before the findings receipt of a
        findings/``blocked`` verification/audit phase is minted, the exact
        bytes of the structured result the orchestrator read are preserved
        under the ignored ``.factory-state/`` namespace (deterministic
        name ``factory-phase-result-round-{r}-{phase}.json``, atomic
        no-replace with byte-exact crash idempotency), so the next-round
        findings authority authenticates every receipt against the exact
        trusted content — ``result_digest`` and parsed ``findings``/
        ``blocked_on`` — never a self-digest only.
        """
        try:
            findings_module.preserve_phase_result(
                self._root,
                state.current_round, phase, raw_bytes,
            )
        except findings_module.FindingsError as exc:
            raise CampaignFindingsError(
                f"cannot preserve the round {state.current_round} {phase} "
                f"phase-result bytes: {exc}"
            ) from exc

    # -- deterministic gates -------------------------------------------------------

    def _authoritative_plan_base(
        self, state: state_module.FactoryState
    ) -> str:
        """The authoritative plan front-matter base for the current phase.

        The plan front-matter ``base_commit`` never changes during a
        campaign; its authoritative anchor is the committed plan at the
        state's ``phase_base_commit`` (the commit the current phase started
        from, which predates every later role commit of the phase).  The
        implementation selector and the implementation recovery validate the
        current plan against this anchor, so a stale plan whose front matter
        drifted is rejected deterministically (Task 9 review M2).
        """
        try:
            return self._git.plan_at(state.phase_base_commit).base_commit
        except CampaignGitError as exc:
            raise CampaignRecoveryError(
                f"the committed plan at the state phase base "
                f"{state.phase_base_commit} cannot be resolved: {exc}"
            ) from exc

    def _acceptance_gate(
        self, task_id: int, plan: plan_parser.Plan
    ) -> Tuple[bool, str]:
        """Deterministic acceptance gate for the selected task.

        A configured ``acceptance_command`` is executed behind the lock
        (bounded, new session, **stripped allowlisted environment** — Task
        11: a credential in the operator's environment can never reach a
        gate child); its exit status is the gate.  Any captured failure
        output is bounded and redacted through the exact committed
        credential guard before it can enter a result, log, receipt, or
        repository state. Production reserves that command for final product
        acceptance and uses the exact verification command as the coherent
        per-task contract. Fixtures without an acceptance command also run
        their mandatory verifier, then supplement it by checking every
        single-token ``- Verification:`` repository path in the **exact newly
        validated plan**. Shell command prose (including whitespace) belongs
        to the trusted verifier and is never interpreted as a pathname. The
        gate never reads the stale pre-commit plan (Task 9 review M1).

        Task 9 review L2: a token interpreted as a path must be a safe
        repository-relative path — an absolute path, an empty/dot-segment
        path, a ``..`` traversal, or any other path that would resolve
        outside the repository fails closed instead of being probed.
        """
        config = self._config
        if config.role_driver is None:
            # Production reserves --acceptance-command for the final product
            # acceptance boundary.  A selected task is accepted by the same
            # exact-commit deterministic verifier contract that will inspect
            # the completed work in the verification phase; planner command
            # prose is never reinterpreted as a pathname.
            ran, exit_code, detail, skipped = self._run_gate(
                config.verification_command, "task acceptance"
            )
            if not ran:
                return False, detail or "task acceptance verifier did not run"
            if exit_code != 0:
                return False, detail or "task acceptance verifier failed"
            if skipped:
                return False, "task acceptance verifier reported a skip"
            return True, "task acceptance verifier passed with no skips"
        if config.acceptance_command:
            command = tuple(config.acceptance_command)
            spawn_argv = list(command)
            spawn_executable = None
            spawn_pass_fds: Tuple[int, ...] = ()
            if (
                self._held_acceptance is not None
                and command == tuple(self._held_acceptance.binding.command)
            ):
                # Task 22 (B2): the acceptance gate executes through the
                # retained-fd authority — the descriptor bound to the exact
                # committed blob before planning is re-validated immediately
                # before execution and the child runs the pinned interpreter
                # with the descriptor path, so acceptance and verification
                # modes share the identical authority (no pathname
                # asymmetry) and a substitution can never execute.
                try:
                    spawn_argv, spawn_executable, spawn_pass_fds = (
                        self._spawn_held_script(
                            self._held_acceptance, list(command)[1:]
                        )
                    )
                    spawn_pass_fds = tuple(spawn_pass_fds)
                except evidence_module.VerifierBindingError as exc:
                    return False, f"acceptance gate binding failed closed: {exc}"
            else:
                spawn_argv = list(command)
            try:
                result = self._lock.spawn_child(
                    spawn_argv,
                    executable=spawn_executable,
                    pass_fds=spawn_pass_fds or (),
                    env=self._gate_environment(),
                    timeout=min(
                        config.gate_timeout,
                        self._remaining_time("fixture acceptance gate"),
                    ),
                )
            except lock_module.RootLockTimeoutError:
                return False, "acceptance gate timed out"
            if result.returncode == 0:
                return True, "acceptance gate passed"
            return False, self._redact_gate_detail(
                result.stdout, result.stderr
            ) or "acceptance gate failed"
        # Fixture/default contracts still run their mandatory deterministic
        # verifier. Repository-path references are a supplementary fixture
        # assertion below; shell command prose is not reinterpreted as a path.
        if self._lock is not None:
            ran, exit_code, detail, skipped = self._run_gate(
                config.verification_command, "task acceptance"
            )
            if not ran or exit_code != 0 or skipped:
                return False, detail or "task acceptance verifier failed or skipped"
        task = next((t for t in plan.tasks if t.number == task_id), None)
        if task is None:
            return False, f"task {task_id} is absent from the validated plan"
        verification = task.fields.get("Verification", "")
        missing: List[str] = []
        unsafe: List[str] = []
        for line in verification.splitlines():
            token = line.strip().strip("`")
            if not token:
                # An empty line contributes no reference and is skipped.
                continue
            if any(ch in token for ch in " \t"):
                # Planner Verification fields are command prose by contract.
                # The trusted verifier above owns command execution; never
                # misclassify valid shell argv/prose as a repository pathname.
                continue
            unsafe_path, reason = _unsafe_repo_relative(token)
            if unsafe_path:
                unsafe.append(unsafe_path)
                continue
            if not (self._root / token).exists():
                missing.append(token)
        if unsafe:
            return False, (
                "unsafe verification reference(s) must be single-token "
                "repository-relative regular paths: "
                f"{', '.join(sorted(unsafe))}"
            )
        if missing:
            return False, f"missing verification references: {', '.join(missing)}"
        return True, "all verification references exist"

    def _runner_bindings(self) -> Tuple[str, str, str]:
        """Re-prove the clean exact Git/environment identity for acquisition."""
        if self._git.role_dirty_paths():
            raise CampaignBindingError(
                "runner acquisition requires a clean tracked/untracked product tree"
            )
        head = self._git.head()
        tree = self._git.object_id(f"{head}^{{tree}}")
        environment_blob = self._git.object_id(
            f"{head}:.factory/environment.toml"
        )
        return head, tree, environment_blob

    def _read_runner_acquisition(self) -> Optional[Dict[str, object]]:
        state_directory = self._state_directory()
        if not state_directory.exists() and not state_directory.is_symlink():
            return None
        try:
            value = state_module.read_json(
                self._root, RUNNER_ACQUISITION_NAME,
                maximum=MAX_RESULT_FILE, missing_ok=True,
            )
        except state_module.StateIOError as exc:
            raise CampaignBindingError(
                f"runner acquisition metadata is unsafe or partial: {exc}"
            ) from exc
        if value is None:
            return None
        expected = {
            "schema", "attempt", "status", "head", "tree",
            "environment_blob", "command_sha256", "command",
            "aggregate_sha256", "checker_exit", "runner_exit",
            "diagnostic",
        }
        command_sha = plan_sha256(
            json.dumps(list(RUNNER_COMMAND), separators=(",", ":")).encode()
        )
        if (
            not isinstance(value, dict) or set(value) != expected
            or value.get("schema") != RUNNER_ACQUISITION_SCHEMA
            or type(value.get("attempt")) is not int or value["attempt"] < 1
            or value.get("status") not in {
                "acquiring", "complete", "transport_failure",
                "findings", "integrity_failure",
            }
            or value.get("command") != list(RUNNER_COMMAND)
            or value.get("command_sha256") != command_sha
            or not all(
                SHA40_RE.fullmatch(str(value.get(name, "")))
                for name in ("head", "tree", "environment_blob")
            )
            or not isinstance(value.get("aggregate_sha256"), str)
            or value.get("aggregate_sha256")
            and not SHA256_RE.fullmatch(str(value["aggregate_sha256"]))
            or type(value.get("checker_exit")) is not int
            or type(value.get("runner_exit")) is not int
            or not isinstance(value.get("diagnostic"), str)
            or (
                value.get("status") == "complete"
                and (
                    not SHA256_RE.fullmatch(str(value.get("aggregate_sha256", "")))
                    or value.get("checker_exit") != 0
                    or value.get("runner_exit") != 0
                )
            )
        ):
            raise CampaignBindingError(
                "runner acquisition metadata schema/binding is invalid"
            )
        return value

    def _write_runner_acquisition(
        self, *, attempt: int, status: str, head: str, tree: str,
        environment_blob: str, aggregate_sha256: str = "",
        checker_exit: int = -1, runner_exit: int = -1,
        diagnostic: str = "",
    ) -> None:
        command_sha = plan_sha256(
            json.dumps(list(RUNNER_COMMAND), separators=(",", ":")).encode()
        )
        state_module.atomic_write_json(self._root, RUNNER_ACQUISITION_NAME, {
            "schema": RUNNER_ACQUISITION_SCHEMA,
            "attempt": attempt,
            "status": status,
            "head": head,
            "tree": tree,
            "environment_blob": environment_blob,
            "command_sha256": command_sha,
            "command": list(RUNNER_COMMAND),
            "aggregate_sha256": aggregate_sha256,
            "checker_exit": checker_exit,
            "runner_exit": runner_exit,
            # Fixed coordinator classifications only: child output, hostnames,
            # transport bytes, environment values, and credentials never enter
            # durable acquisition state or campaign findings.
            "diagnostic": diagnostic,
        })

    def _runner_readiness_nonce(self) -> str:
        """Stable recovery scope, fresh across campaign/acceptance authority."""
        if self._config.role_driver is not None and not self._config.accepted_commit:
            return plan_sha256(self._config.campaign_id.encode() + b"\0fixture")
        policy = self._git.blob_at(self._config.accepted_commit, readiness_module.POLICY_PATH)
        return plan_sha256(
            self._config.campaign_id.encode() + b"\0"
            + self._config.accepted_commit.encode() + b"\0" + policy
        )

    def _spawn_runner_authority(
        self, held: evidence_module.HeldVerifier, tail: Sequence[str],
        timeout: float,
    ):
        try:
            argv, executable, pass_fds = self._spawn_held_script(held, tail)
        except evidence_module.VerifierBindingError as exc:
            raise CampaignBindingError(
                f"runner authority binding failed closed: {exc}"
            ) from exc
        return self._lock.spawn_child(
            argv, executable=executable, pass_fds=pass_fds,
            env=self._gate_environment(),
            timeout=min(timeout, self._remaining_time("runner evidence acquisition")),
            stdout_limit=GATE_DETAIL_MAX, stderr_limit=GATE_DETAIL_MAX,
        )

    def _check_runner_aggregate(self, head: str) -> Tuple[int, str]:
        """Run the strong signed aggregate checker and return its exact digest."""
        if self._held_runner_checker is None:
            return -1, ""
        try:
            tail = ["--expected-commit", head]
            if self._config.role_driver is None:
                tail += ["--expected-campaign-id", self._config.campaign_id,
                         "--expected-readiness-nonce", self._runner_readiness_nonce()]
            tail.append("--print-digest")
            result = self._spawn_runner_authority(
                self._held_runner_checker, tuple(tail),
                min(self._config.gate_timeout, self._config.runner_timeout),
            )
        except (lock_module.RootLockTimeoutError, CampaignBindingError):
            return -1, ""
        digest = (result.stdout or "").strip()
        if result.returncode != 0 or not SHA256_RE.fullmatch(digest):
            return result.returncode, ""
        return 0, digest

    def _ensure_runner_evidence(self) -> Tuple[bool, int, str]:
        """Acquire fresh signed runner evidence under the trusted campaign lock.

        Reuse is permitted only for this campaign's unambiguous completed
        acquisition at the unchanged clean HEAD/tree/environment and only
        after the strong checker revalidates the exact aggregate. Any changed
        HEAD, interrupted/acquiring marker, transport failure, or stale
        aggregate causes a new bounded acquisition; current-head tampering
        after a completed acquisition is an integrity failure, never silently
        overwritten.
        """
        if not self._config.runner_command:
            return False, 0, ""
        if (
            tuple(self._config.runner_command) != RUNNER_COMMAND
            or self._held_runner is None
            or self._held_runner_checker is None
        ):
            return False, -1, "runner acquisition authority is not exactly bound"
        try:
            head, tree, environment_blob = self._runner_bindings()
            prior = self._read_runner_acquisition()
        except (CampaignBindingError, CampaignGitError):
            return False, -1, "runner acquisition prerequisite failed integrity validation"
        same = bool(prior) and all(
            prior.get(name) == value for name, value in (
                ("head", head), ("tree", tree),
                ("environment_blob", environment_blob),
            )
        )
        if same and prior.get("status") == "complete":
            checker_exit, digest = self._check_runner_aggregate(head)
            if checker_exit == 0 and digest == prior.get("aggregate_sha256"):
                return True, 0, "runner evidence reused after exact validation"
            self._write_runner_acquisition(
                attempt=int(prior["attempt"]), status="integrity_failure",
                head=head, tree=tree, environment_blob=environment_blob,
                checker_exit=checker_exit, runner_exit=RUNNER_INTEGRITY_EXIT,
                diagnostic="completed runner aggregate failed integrity validation",
            )
            return True, -1, "completed runner aggregate failed integrity validation"
        attempt = int(prior["attempt"]) + 1 if prior else 1
        self._write_runner_acquisition(
            attempt=attempt, status="acquiring", head=head, tree=tree,
            environment_blob=environment_blob,
            diagnostic="runner acquisition started",
        )
        # The metadata publication is not authority by itself. Revalidate the
        # command inode/bytes and clean Git bindings again in the final
        # pre-exec window, while retaining the sole campaign/root lock.
        try:
            binding_unchanged = (
                self._runner_bindings() == (head, tree, environment_blob)
            )
        except (CampaignBindingError, CampaignGitError):
            binding_unchanged = False
        if not binding_unchanged:
            self._write_runner_acquisition(
                attempt=attempt, status="integrity_failure", head=head,
                tree=tree, environment_blob=environment_blob,
                diagnostic="Git binding changed before runner invocation",
            )
            return False, -1, "Git binding changed before runner invocation"
        try:
            result = self._spawn_runner_authority(
                self._held_runner, (), self._config.runner_timeout,
            )
            runner_exit = result.returncode
        except lock_module.RootLockTimeoutError:
            runner_exit = RUNNER_TRANSPORT_EXIT
        except CampaignBindingError:
            runner_exit = RUNNER_INTEGRITY_EXIT
        checker_exit, digest = self._check_runner_aggregate(head)
        if runner_exit == 0 and checker_exit == 0:
            self._write_runner_acquisition(
                attempt=attempt, status="complete", head=head, tree=tree,
                environment_blob=environment_blob, aggregate_sha256=digest,
                checker_exit=0, runner_exit=0,
                diagnostic="runner acquisition and strong validation passed",
            )
            return True, 0, "runner evidence acquired and strongly validated"
        if runner_exit in (RUNNER_TRANSPORT_EXIT, RUNNER_FINDINGS_EXIT):
            status = (
                "transport_failure" if runner_exit == RUNNER_TRANSPORT_EXIT
                else "findings"
            )
            diagnostic = (
                "runner transport unavailable"
                if status == "transport_failure"
                else "declared runner verification did not pass"
            )
            self._write_runner_acquisition(
                attempt=attempt, status=status, head=head, tree=tree,
                environment_blob=environment_blob, checker_exit=checker_exit,
                runner_exit=runner_exit, diagnostic=diagnostic,
            )
            return True, runner_exit, diagnostic
        self._write_runner_acquisition(
            attempt=attempt, status="integrity_failure", head=head, tree=tree,
            environment_blob=environment_blob, checker_exit=checker_exit,
            runner_exit=runner_exit,
            diagnostic="runner protocol or aggregate integrity failure",
        )
        return True, -1, "runner protocol or aggregate integrity failure"

    def _run_gate(
        self, command: Sequence[str], label: str
    ) -> Tuple[bool, int, str, bool]:
        if not command:
            return False, 0, "", False
        held = {
            "capability": self._held_capability,
            "final capability": self._held_capability,
            "final acceptance": self._held_acceptance,
        }.get(label, self._held_verifier)
        spawn_argv = list(command)
        spawn_executable = None
        spawn_pass_fds: Tuple[int, ...] = ()
        if held is not None and tuple(held.binding.command) == tuple(command):
            # Task 12 §19 (MED1): the verifier entrypoint was opened and bound
            # to its committed blob/identity/inode *before* the untrusted
            # phase.  Immediately before every execution the retained
            # descriptor is re-validated (inode identity, owner/mode/
            # link-count, byte digest, committed blob at the current head);
            # then the child executes ``/proc/self/fd/<fd>`` (the retained
            # read-only descriptor, passed through ``pass_fds`` while the root
            # lock and every other holder descriptor are never passed) with
            # the bound command argv passed to the kernel verbatim, so a
            # pathname substitution in the final revalidate→exec race still
            # executes the exact bound inode and any substitution fails
            # closed instead of executing substituted bytes.  The kernel's
            # shebang dispatch replaces the script argument with the fd path
            # (a script sees ``$0 = /proc/self/fd/<fd>`` — never the
            # canonical path — while every argument after it is preserved;
            # F1), and the child intentionally inherits exactly that one
            # read-only verifier descriptor (accepted inheritance; F2).
            try:
                raw = evidence_module.revalidate_verifier(
                    self._root, held.binding, git=self._git,
                    current_commit=self._git.head(), held=held,
                )
                first_line = raw.split(b"\n", 1)[0].strip()
                if first_line.startswith(b"#!") and b"python" in first_line.lower():
                    # Task 22 (B2): Python gate scripts execute through the
                    # pinned interpreter with the descriptor path — no
                    # ``PATH``-resolved shebang, so a malicious PATH or a
                    # pathname substitution can never substitute the bytes.
                    spawn_argv, spawn_executable, spawn_pass_fds = (
                        self._spawn_held_script(held, list(command)[1:])
                    )
                else:
                    # Non-Python scripts keep the kernel-shebang dispatch on
                    # the retained descriptor (existing Task 12 contract).
                    spawn_argv, spawn_executable, spawn_pass_fds = held.spawn(
                        list(command)
                    )
                spawn_pass_fds = tuple(spawn_pass_fds)
            except evidence_module.VerifierBindingError as exc:
                # The gate must report that it did NOT run: the substituted
                # verifier was never executed, so the campaign classifies
                # the verification as infrastructure_failure (an untrusted
                # verifier can never yield pass/findings evidence).
                return False, -1, f"{label} verifier binding failed closed: {exc}", False
        try:
            result = self._lock.spawn_child(
                spawn_argv,
                executable=spawn_executable,
                pass_fds=spawn_pass_fds,
                env=self._gate_environment(),
                timeout=min(
                    self._config.gate_timeout,
                    self._remaining_time(f"{label} gate"),
                ),
            )
        except lock_module.RootLockTimeoutError:
            return True, -1, f"{label} gate timed out", False
        combined = (result.stdout or "") + (result.stderr or "")
        skipped = bool(SKIP_OUTPUT_RE.search(combined))
        return True, result.returncode, self._redact_gate_detail(
            result.stdout, result.stderr
        ), skipped

    def _exact_commit_redactor(self) -> object:
        """The verified exact-commit credential-guard redactor (fail closed).

        The redactor is bound to the exact committed ``.factory/tools/credential-
        guard.py`` blob at the current head.  An unverifiable guard fails
        the campaign closed: no findings/blocked content and no preserved
        structured result may be stored, delivered to the next planner, or
        logged while redaction is unavailable (Task 23/F).
        """
        if self._redactor is None:
            try:
                worktree = output_redaction.read_worktree_guard_source(
                    self._root
                )
                committed = self._git.blob_at(
                    self._git.head(),
                    output_redaction.REDACTION_GUARD_RELPATH,
                )
                self._redactor = output_redaction.redactor_from_bytes(
                    worktree, committed, self._git.head()
                )
            except (output_redaction.OutputRedactionError, CampaignGitError) as exc:
                raise CampaignFindingsError(
                    "findings/phase-result redaction is unavailable because "
                    f"the credential guard cannot be verified: {exc}; no "
                    "findings content or preserved structured result may be "
                    "stored or delivered (Task 23/F)"
                ) from exc
        return self._redactor

    def _redact_findings_content(
        self, result_data: Dict[str, object], phase: str
    ) -> Tuple[Sequence[str], Sequence[str], bytes]:
        """Redact findings/blocked_on and the preserved structured result
        bytes BEFORE any durable storage or next-planner delivery (Task 23/F).

        Every free-text finding and blocked reference is masked through the
        exact-commit Redactor, and the preserved ``factory-phase-result/v1``
        bytes are rebuilt from the redacted content (sorted canonical JSON),
        so a raw secret candidate can never persist in the receipt, the
        preserved artifact, the control state, a log, or the next planner
        prompt.  An individual redaction failure carries the fixed
        ``[REDACTION FAILED]`` marker (never the raw bytes); a redactor that
        cannot be verified fails the campaign closed.  Returns
        ``(redacted_findings, redacted_blocked_on, redacted_bytes)``; the
        caller binds the receipt/state digest to the redacted bytes.
        """
        redactor = self._exact_commit_redactor()

        def masked(items: Sequence[str]) -> List[str]:
            result: List[str] = []
            for item in items:
                try:
                    text = redactor.redact_text(str(item))  # type: ignore[attr-defined]
                except output_redaction.OutputRedactionError:
                    text = output_redaction.REDACTION_FAILED
                if not text.strip():
                    # The phase-result schema requires non-empty findings/
                    # blocked strings; a mask that collapsed to nothing is
                    # replaced by the semantic marker, never the raw bytes.
                    text = output_redaction.REDACTION_FAILED
                result.append(text)
            return result

        findings = masked(list(result_data.get("findings", [])))
        blocked_on = masked(list(result_data.get("blocked_on", [])))
        # The redacted rebuild preserves the original field structure: an
        # optional field that the role never wrote stays absent, so a result
        # with no credential-shaped content redacts to byte-identical bytes
        # and the digest binding is unchanged (the redaction only ever masks
        # credential-shaped values).
        redacted = dict(result_data)
        if "findings" in result_data:
            redacted["findings"] = findings
        if "blocked_on" in result_data:
            redacted["blocked_on"] = blocked_on
        redacted_bytes = json.dumps(
            redacted, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        return findings, blocked_on, redacted_bytes

    def _redact_gate_detail(
        self, stdout: Optional[str], stderr: Optional[str]
    ) -> str:
        """Bound, then mask one deterministic gate's output through the guard.

        The retained gate detail is bounded first (never feeding the guard
        unbounded input) and then redacted **through the exact committed
        credential guard** (Task 11) before it can enter a result, log,
        receipt, or repository state.  The redactor is verified and bound to
        ``(root, head)`` on the first gate run and fails closed when the
        exact committed guard cannot be verified; a redaction failure never
        exposes raw gate output and carries the fixed
        ``[REDACTION FAILED]`` marker instead.
        """
        if self._redactor is None:
            try:
                worktree = output_redaction.read_worktree_guard_source(
                    self._root
                )
                committed = self._git.blob_at(
                    self._git.head(),
                    output_redaction.REDACTION_GUARD_RELPATH,
                )
                self._redactor = output_redaction.redactor_from_bytes(
                    worktree, committed, self._git.head()
                )
            except (output_redaction.OutputRedactionError, CampaignGitError) as exc:
                raise CampaignError(
                    "deterministic gate output redaction is unavailable "
                    f"because the credential guard cannot be verified: {exc}; "
                    "no gate output may reach results, logs, receipts, or "
                    "repository state (Task 11)"
                ) from exc
        combined = (stdout or "") + (stderr or "")
        combined = combined[:GATE_DETAIL_MAX]
        try:
            return self._redactor.redact_text(combined)  # type: ignore[union-attr]
        except output_redaction.OutputRedactionError:
            # Fail closed: a redaction failure never exposes the raw gate
            # output; it carries the fixed marker instead.
            return output_redaction.REDACTION_FAILED

    def _implementation_allow_paths(self) -> List[str]:
        """The designated developer evidence path, when bound.

        The evidence-smoke lane (Task 22) lets the deterministic developer
        seam create exactly one bounded harness-owned tracked evidence
        artifact under ``.factory/artifacts/``; the campaign config binds that
        exact repository-relative path and it alone is allowed in the
        implementation scope on top of the canonical plan path.  Every other
        ``.factory/`` path stays role-forbidden.
        """
        if self._config.developer_evidence_path:
            return [self._config.developer_evidence_path]
        return []

    def _preservable_dirty_paths(self) -> List[str]:
        """Dirty paths that are preserved product work.

        The developer's plan path is the regenerable harness document the
        planner re-derives every round; an unparsable/unbound plan is never
        committed product work and never forces a dirty interruption.  Every
        other dirty path (product code, tests, evidence, a scope violation)
        is preservable work that stays untouched and interrupts on budget
        exhaustion.
        """
        return [
            path
            for path in self._git.role_dirty_paths()
            if path != self._config.plan_path
        ]

    def _restore_plan_worktree(self) -> None:
        """Restore the regenerable plan path to the committed HEAD.

        Called only when the budget exhausted cleanly with an unparsable or
        unbound developer plan and no preservable work: the invalid plan is
        never committed, the next verification/audit is read-only and must
        see the last coherent committed plan, and the planner re-derives the
        plan in a later round.
        """
        self._git.restore([self._config.plan_path])

    # -- records -------------------------------------------------------------------

    def _record(
        self, state: state_module.FactoryState, attempt: int, outcome: str,
        detail: str, *, result_digest: str = "",
    ) -> PhaseRecord:
        head = self._git.head() if self._git is not None else "0" * 40
        try:
            digest = plan_sha256(self._git.blob_at(head, self._config.plan_path))
        except CampaignGitError:
            digest = "0" * 64
        if result_digest and not SHA256_RE.fullmatch(result_digest):
            raise CampaignError("phase result digest must be 64-hex or empty")
        return PhaseRecord(
            round=state.current_round,
            phase=state.current_phase,
            attempt=attempt,
            outcome=outcome,
            head_commit=head,
            plan_digest=digest,
            detail=detail,
            result_digest=result_digest,
        )

    # -- phase steps ----------------------------------------------------------------

    def _step(self, state: state_module.FactoryState):
        phase = state.current_phase
        if phase == "planning":
            return self._step_planning(state)
        if phase == "implementation":
            return self._step_implementation(state)
        if phase == "verification":
            return self._step_verification(state)
        if phase == "audit":
            return self._step_audit(state)
        raise CampaignPhaseError(f"unknown live phase {phase!r}")

    def _run_pre_round_hooks(
        self, state: state_module.FactoryState
    ) -> Tuple[state_module.FactoryState, Optional[_Step]]:
        """Run the exact ordered registry once before this round's planner.

        The start cursor is durably written to the coordinator-owned pre-round
        hook sidecar (never a canonical state field) before execution.  A
        restart that observes an uncompleted start is ambiguous and terminates
        as an infrastructure failure; it never repeats a possibly
        side-effecting hook.  Completed results are chained into the sidecar.
        """
        sidecar = sidecars_module.read_pre_round(
            self._root, expected_campaign_id=self._config.campaign_id
        )
        if sidecar.completed_round == state.current_round:
            return state, None
        if sidecar.started_round == state.current_round:
            state2 = state_module.advance(state, "infrastructure_failure")
            state_module.write_state(self._root, state2)
            record = self._record(
                state, 1, "infrastructure_failure",
                "pre-round hook execution was interrupted after its durable start; refusing to rerun",
            )
            return state2, _Step(record, state=state2, terminal="infrastructure_failure")

        claimed = sidecars_module.begin_pre_round_hooks(
            sidecar, current_round=state.current_round
        )
        sidecars_module.write_pre_round(self._root, claimed)
        head = self._git.head()
        dirty_before = tuple(self._git.role_dirty_paths())

        def execute(hook: pre_round_module.Hook) -> None:
            if hook.implementation == "branch_guard":
                try:
                    self._lock.validate_live_branch(
                        self._config.branch,
                        timeout=min(
                            gitutil.GIT_TIMEOUT,
                            self._remaining_time("pre-round branch guard"),
                        ),
                    )
                except lock_module.RootLockError as exc:
                    raise pre_round_module.PreRoundError(
                        "descriptor-anchored branch guard failed"
                    ) from exc
                return
            raise pre_round_module.PreRoundError(
                f"unknown fixed hook implementation {hook.implementation!r}"
            )

        results, success = pre_round_module.run_hooks(
            self._config.pre_round_registry,
            implementation_digests=self._config.pre_round_implementation_digests,
            execute=execute,
        )
        postcondition = "pass"
        if self._git.head() != head or tuple(self._git.role_dirty_paths()) != dirty_before:
            success = False
            postcondition = "failed"
        payload = pre_round_module.result_bytes(
            campaign_id=self._config.campaign_id,
            round_number=state.current_round,
            commit=head,
            configuration_digest_value=(
                self._config.pre_round_hook_configuration_digest
            ),
            results=results,
            postcondition_outcome=postcondition,
        )
        chained = pre_round_module.chain_result_digest(
            claimed.results_digest, payload
        )
        completed = sidecars_module.complete_pre_round_hooks(
            claimed, chained, current_round=state.current_round
        )
        if not success:
            # One atomic publication binds both the failed typed result and
            # terminal outcome.  Until it lands, the sidecar remains an
            # ambiguous started round and recovery refuses to rerun it.
            terminal = state_module.advance(state, "infrastructure_failure")
            state_module.write_state(self._root, terminal)
            record = self._record(
                state, 1, "infrastructure_failure",
                "a mandatory pre-round hook failed; the planner was not launched",
                result_digest=hashlib.sha256(payload).hexdigest(),
            )
            return terminal, _Step(
                record, state=terminal, terminal="infrastructure_failure"
            )
        sidecars_module.write_pre_round(self._root, completed)
        return state, None

    def _step_planning(self, state: state_module.FactoryState) -> _Step:
        state, hook_terminal = self._run_pre_round_hooks(state)
        if hook_terminal is not None:
            return hook_terminal
        seq = self._planning_attempts_used
        tag = self._begin_untrusted(state, seq)
        head = self._git.head()
        attempt = seq + 1
        # Task 10 §16: the next planner is the only role that may receive
        # the receipt-backed findings of the previous round, as a
        # deterministic digest-bound payload.  The findings authority
        # validates every receipt (malformed/stale/synthetic/foreign/
        # receipt-only claims fail closed) and returns ``None`` when no
        # findings flowed (round 1, or a round whose verification/audit
        # passed).  The deterministic selector never reads this payload.
        findings_payload = None
        if state.current_round > 1:
            try:
                findings_payload = findings_module.consume_next_round_findings(
                    self._root,
                    campaign_id=self._config.campaign_id,
                    source_round=state.current_round - 1,
                    head=head,
                    is_ancestor=self._git.is_ancestor,
                    phase_records=tuple(self._records),
                )
            except findings_module.FindingsError as exc:
                raise CampaignFindingsError(
                    f"cannot consume the previous round's findings for the "
                    f"next planner: {exc}"
                ) from exc
        role = self._run_role(
            "planner", state, head, attempt=attempt,
            findings_payload=findings_payload,
        )
        plan_worktree = _bounded_read(self._root, self._config.plan_path, "plan", PLAN_BLOB_MAX)
        plan_committed = self._git.blob_at(head, self._config.plan_path)
        plan_changed = plan_worktree != plan_committed
        try:
            committed_plan = plan_parser.Plan.from_bytes(plan_committed)
            base_commit = committed_plan.base_commit
        except plan_parser.PlanError as exc:
            raise CampaignGitError(
                f"the committed plan at {head} does not parse: {exc}"
            ) from exc
        valid, reason = validate_plan_worktree(
            plan_worktree,
            spec_path=self._config.spec_path,
            spec_commit=self._config.spec_commit,
            spec_blob=self._config.spec_blob,
            base_commit=base_commit,
        )
        dirty = self._git.role_dirty_paths()
        violation = scope_violation(
            dirty, phase="planning",
            plan_path=self._config.plan_path, spec_path=self._config.spec_path,
        )
        outcome = classify_planning(
            role=role, plan_changed=plan_changed,
            plan_valid=valid, scope_ok=violation is None,
        )
        self._end_untrusted(tag)
        if outcome == "planned":
            new_head = self._git.commit(
                [self._config.plan_path],
                f"factory-campaign: planning round {state.current_round}",
            )
            plan_digest = plan_sha256(self._git.blob_at(new_head, self._config.plan_path))
            state2 = state_module.advance(
                state, "planned",
                plan_digest=plan_digest,
                phase_base_commit=new_head,
            )
            state_module.write_state(self._root, state2)
            self._planning_attempts_used = 0
            return _Step(self._record(state, attempt, "planned", ""), state=state2)
        # A planner retry is a fresh attempt against the committed canonical
        # plan, never against another attempt's partial/invalid worktree bytes.
        # Planner output is regenerable ledger state (not product work), so
        # every non-planned outcome restores it before retry or terminal exit.
        if plan_changed:
            self._restore_plan_worktree()
        detail = reason or (
            f"planner exit={role.exit_status}"
            + (" (interrupted)" if role.interrupted else "")
            + (f" scope={violation}" if violation else "")
            + (f" diagnostic={role.diagnostic}" if role.diagnostic else "")
        )
        if outcome == "interrupted":
            if attempt < self._config.planning_attempts:
                state2 = state_module.record_retry(state, "interrupted")
                state_module.write_state(self._root, state2)
                self._planning_attempts_used = attempt
                return _Step(self._record(state, attempt, "interrupted", detail),
                             state=state2, retry=True)
            state2 = state_module.advance(state, "interrupted")
            state_module.write_state(self._root, state2)
            return _Step(self._record(state, attempt, "interrupted", detail), state=state2)
        if attempt >= self._config.planning_attempts:
            state2 = state_module.advance(state, "failed")
            state_module.write_state(self._root, state2)
            return _Step(self._record(state, attempt, "failed", detail), state=state2)
        self._planning_attempts_used = attempt
        return _Step(
            self._record(state, attempt, "failed", detail),
            state=state,
            retry=True,
        )

    def _step_implementation(self, state: state_module.FactoryState) -> _Step:
        head = self._git.head()
        plan = self._git.plan_at(head)
        # Task 9 review M2: the selector is bound to the authoritative plan
        # base anchored at the state's phase base (never the plan's own
        # self-declared base), so a stale committed plan fails closed before
        # any task is selected.
        try:
            selection = selector_module.select_task(
                plan, bound_base_commit=self._authoritative_plan_base(state)
            )
        except selector_module.SelectorError as exc:
            raise CampaignRecoveryError(
                f"the committed plan at HEAD is stale or ambiguous: {exc}"
            ) from exc
        if not selection.selected:
            outcome = (
                "work_exhausted" if selection.classification == "work_exhausted"
                else "blocked"
            )
            state2 = state_module.advance(state, outcome)
            state_module.write_state(self._root, state2)
            return _Step(self._record(state, 1, outcome, ""), state=state2)
        task_id = selection.task_id
        # ``begin_attempt`` increments the attempt counter for a retry of the
        # same task and resets it on a trusted task transition (§11); the
        # attempt sequence is therefore monotonic and durable.
        state = state_module.begin_attempt(state, task_id)
        state_module.write_state(self._root, state)
        attempt = state.attempt_number
        seq = attempt
        tag = self._begin_untrusted(state, seq)
        # Resume path: a crashed attempt left dirty work.  Commit coherent
        # work as a progress checkpoint (preserving it) before launching;
        # incoherent work (unparsable plan, foreign scope) is a deterministic
        # task failure and stays dirty.
        dirty = self._git.role_dirty_paths()
        if dirty:
            plan_worktree = _bounded_read(
                self._root, self._config.plan_path, "plan", PLAN_BLOB_MAX
            )
            valid, reason = validate_plan_worktree(
                plan_worktree,
                spec_path=self._config.spec_path,
                spec_commit=self._config.spec_commit,
                spec_blob=self._config.spec_blob,
                base_commit=plan.base_commit,
            )
            violation = scope_violation(
                dirty, phase="implementation",
                plan_path=self._config.plan_path, spec_path=self._config.spec_path,
                allow_paths=self._implementation_allow_paths(),
            )
            if valid and violation is None:
                work_plan = plan_parser.Plan.from_bytes(plan_worktree)
                resumed_task = next(
                    (t for t in work_plan.tasks if t.number == task_id), None
                )
                # A crashed attempt may have left the selected task already
                # complete in the dirty work.  The resume commit is then the
                # task's coherent completion commit (never discarded, §17);
                # otherwise it is an in-progress checkpoint and the fresh
                # role attempt continues the work.
                resumed_complete = bool(
                    resumed_task is not None
                    and resumed_task.status == "complete"
                )
                head = self._git.commit(
                    [path for path in dirty if not scope_violation(
                        [path], phase="implementation",
                        plan_path=self._config.plan_path,
                        spec_path=self._config.spec_path,
                        allow_paths=self._implementation_allow_paths(),
                    )],
                    (
                        f"factory-campaign: task {task_id} complete"
                        if resumed_complete
                        else f"factory-campaign: resume task {task_id} progress"
                    ),
                )
                plan = self._git.plan_at(head)
                if resumed_complete:
                    acceptance_pass, acceptance_detail = self._acceptance_gate(
                        task_id, plan
                    )
                    self._end_untrusted(tag)
                    if acceptance_pass:
                        state2 = state_module.advance(state, "task_completed")
                        state_module.write_state(self._root, state2)
                        return _Step(
                            self._record(state, attempt, "task_completed", ""),
                            state=state2,
                        )
                    return self._implementation_outcome(
                        state, attempt, "task_failed",
                        detail=acceptance_detail
                        or "resumed completion failed its acceptance gate",
                        dirty_work=False,
                    )
            else:
                self._end_untrusted(tag)
                return self._implementation_outcome(
                    state, attempt, "task_failed",
                    detail=reason or violation or "unrecoverable dirty work",
                    dirty_work=bool(self._preservable_dirty_paths()),
                )
        head = self._git.head()
        role = self._run_role(
            "developer", state, head, task_id=task_id, attempt=attempt
        )
        plan_worktree = _bounded_read(
            self._root, self._config.plan_path, "plan", PLAN_BLOB_MAX
        )
        valid, reason = validate_plan_worktree(
            plan_worktree,
            spec_path=self._config.spec_path,
            spec_commit=self._config.spec_commit,
            spec_blob=self._config.spec_blob,
            base_commit=plan.base_commit,
        )
        dirty = self._git.role_dirty_paths()
        violation = scope_violation(
            dirty, phase="implementation",
            plan_path=self._config.plan_path, spec_path=self._config.spec_path,
            allow_paths=self._implementation_allow_paths(),
        )
        scope_ok = violation is None
        had_changes = bool(dirty)
        task_complete = False
        if valid:
            work_plan = plan_parser.Plan.from_bytes(plan_worktree)
            task = next((t for t in work_plan.tasks if t.number == task_id), None)
            task_complete = bool(task is not None and task.status == "complete")
        acceptance_pass = False
        if task_complete:
            # M1: the gate validates the exact newly validated worktree plan
            # that is about to be committed, never the stale pre-commit head.
            acceptance_pass, _ = self._acceptance_gate(task_id, work_plan)
        outcome = classify_implementation(
            role=role,
            plan_valid=valid,
            task_complete=task_complete,
            acceptance_pass=acceptance_pass,
            had_changes=had_changes,
            scope_ok=scope_ok,
        )
        self._end_untrusted(tag)
        if outcome == "task_completed":
            allowed = [
                path for path in dirty
                if not scope_violation(
                    [path], phase="implementation",
                    plan_path=self._config.plan_path,
                    spec_path=self._config.spec_path,
                    allow_paths=self._implementation_allow_paths(),
                )
            ]
            new_head = self._git.commit(
                allowed, f"factory-campaign: task {task_id} complete"
            )
            state2 = state_module.advance(state, "task_completed")
            state_module.write_state(self._root, state2)
            return _Step(self._record(state, attempt, "task_completed", ""), state=state2)
        if outcome == "task_progress":
            allowed = [
                path for path in dirty
                if not scope_violation(
                    [path], phase="implementation",
                    plan_path=self._config.plan_path,
                    spec_path=self._config.spec_path,
                    allow_paths=self._implementation_allow_paths(),
                )
            ]
            if allowed:
                self._git.commit(
                    allowed, f"factory-campaign: progress task {task_id}"
                )
            return self._implementation_outcome(
                state, attempt, "task_progress", "", dirty_work=False
            )
        return self._implementation_outcome(
            state, attempt, outcome,
            reason or (
                f"developer exit={role.exit_status}"
                + (f" diagnostic={role.diagnostic}" if role.diagnostic else "")
            ),
            dirty_work=bool(self._preservable_dirty_paths()),
        )

    def _implementation_outcome(
        self,
        state: state_module.FactoryState,
        attempt: int,
        outcome: str,
        detail: str,
        *,
        dirty_work: bool,
    ) -> _Step:
        budget_exhausted = attempt >= self._config.implementation_attempts
        if outcome == "interrupted":
            if budget_exhausted:
                if dirty_work or self._preservable_dirty_paths():
                    state2 = state_module.advance(state, "interrupted")
                    state_module.write_state(self._root, state2)
                    return _Step(
                        self._record(state, attempt, "interrupted", detail), state=state2
                    )
                # A bounded model timeout with no uncommitted work is a clean
                # exhausted attempt, not an operator interruption. Preserve
                # campaign liveness by recording task failure and proceeding
                # to independent verification/audit at the coherent HEAD.
                state2 = state_module.advance(state, "task_failed")
                state_module.write_state(self._root, state2)
                return _Step(
                    self._record(state, attempt, "task_failed", detail), state=state2
                )
            state2 = state_module.record_retry(state, "interrupted")
            state_module.write_state(self._root, state2)
            return _Step(
                self._record(state, attempt, "interrupted", detail),
                state=state2, retry=True,
            )
        # task_failed / task_progress
        if budget_exhausted:
            if dirty_work or self._preservable_dirty_paths():
                # §13.2: budget expiry with dirty work terminates interrupted
                # (dirty work preserved); verification does not run.
                state2 = state_module.advance(state, "interrupted")
                state_module.write_state(self._root, state2)
                return _Step(
                    self._record(state, attempt, "interrupted", detail), state=state2
                )
            # Clean deterministic exhaustion: record a finding and proceed to
            # verification at the last coherent committed state.  A broken
            # (unparsable/unbound) plan left in the worktree is regenerable
            # harness state, never preserved product work; restore it so the
            # read-only verification/audit phases see the committed plan.
            if self._git.role_dirty_paths():
                self._restore_plan_worktree()
            state2 = state_module.advance(state, "task_failed")
            state_module.write_state(self._root, state2)
            return _Step(
                self._record(state, attempt, "task_failed", detail), state=state2
            )
        state2 = state_module.record_retry(state, outcome)
        state_module.write_state(self._root, state2)
        return _Step(
            self._record(state, attempt, outcome, detail),
            state=state2, retry=True,
        )

    def _step_verification(self, state: state_module.FactoryState) -> _Step:
        head = self._git.head()
        # The verifier was opened and exact-commit bound during campaign
        # acquisition, before the planner or any other role could launch.
        # Every execution below revalidates that retained descriptor against
        # the current descendant commit; planner/developer commits may not
        # change the verifier bytes or pathname.
        if self._held_verifier is None:
            raise CampaignBindingError(
                "the explicit verification command is not held at the "
                "pre-planning exact-commit boundary"
            )
        # A successful tester process can still omit its mandatory handoff.
        # Retry that one infrastructure-only case once in a fresh role process
        # before running the expensive trusted gates. Scope violations and
        # nonzero/interrupted roles do not retry; absent or malformed JSON gets
        # one fresh serialization attempt. Each attempt has its own state-digest tag and exact empty
        # pre-created channel; no prior prose/session is carried forward.
        attempt = 1
        while True:
            tag = self._begin_untrusted(state, attempt)
            self._prepare_phase_result_file(
                self._config.phase_result_path, "verification"
            )
            role = self._run_role("tester", state, head, attempt=attempt)
            dirty = self._git.role_dirty_paths()
            allow_paths = (
                [self._config.phase_result_path]
                if self._config.phase_result_path else []
            )
            violation = scope_violation(
                dirty, phase="verification",
                plan_path=self._config.plan_path, spec_path=self._config.spec_path,
                allow_paths=allow_paths,
            )
            result_error: Optional[CampaignResultError] = None
            try:
                result = read_phase_result(
                    self._root, self._config.phase_result_path, "verification"
                )
            except CampaignResultError as exc:
                # The secure reader already removed the malformed channel.
                # One fresh retry may replace model serialization corruption;
                # a second malformed result remains a fail-closed campaign
                # error and is never interpreted or preserved as evidence.
                result = None
                result_error = exc
            self._end_untrusted(tag)
            if result_error is not None and attempt >= 2:
                raise result_error
            if (
                result is not None or attempt >= 2 or violation is not None
                or role.interrupted or role.exit_status != 0
            ):
                break
            attempt += 1
        gate_ran, gate_exit, gate_detail, verification_skipped = self._run_gate(
            self._config.verification_command, "verification"
        )
        if verification_skipped and gate_exit == 0:
            gate_exit = 1
            gate_detail = gate_detail or "verification gate reported a skip"
        result_data = result[0] if result is not None else None
        result_digest = result[1] if result is not None else ""
        result_bytes = result[2] if result is not None else b""
        result_valid = bool(result_data is not None)
        result_outcome = result_data.get("outcome") if result_data else None
        findings = list(result_data.get("findings", [])) if result_data else []
        blocked_refs = (
            list(result_data.get("blocked_on", [])) if result_data else []
        )
        acquisition_ran, acquisition_exit, acquisition_detail = (
            self._ensure_runner_evidence()
        )
        if self._config.capability_command:
            capability_ran, capability_exit, capability_detail, capability_skipped = self._run_gate(
                self._config.capability_command, "capability"
            )
        else:
            capability_ran, capability_exit = True, 0
            capability_detail, capability_skipped = "no capabilities required", False
        if acquisition_exit != 0:
            capability_ran = acquisition_ran
            capability_exit = acquisition_exit
            capability_detail = acquisition_detail
        capability_available = (
            self._config.role_driver is not None
            and not self._config.capability_command
        ) or (capability_ran and capability_exit == 0)
        if capability_skipped:
            capability_available = False
        outcome = classify_verification(
            role=role,
            scope_ok=violation is None,
            gate_ran=gate_ran,
            gate_exit=gate_exit,
            tester_result_valid=result_valid,
            tester_result_outcome=result_outcome,
            findings=findings,
            blocked_refs=blocked_refs,
            capability_available=capability_available,
            capability_ran=capability_ran,
            capability_exit=capability_exit,
            gate_skipped=verification_skipped,
            capability_skipped=capability_skipped,
        )
        if (
            outcome == "findings" and result_data is not None
            and result_data.get("outcome") == "pass" and not findings
        ):
            # Deterministic verifier/acquisition failures override an
            # optimistic tester pass. Mint one fixed, non-child-derived
            # finding so the preserved structured handoff remains schema-
            # coherent and cannot leak transport diagnostics.
            result_data = dict(result_data)
            result_data["outcome"] = "findings"
            result_data["findings"] = [
                "trusted verification or runner capability evidence did not pass"
            ]
            findings = list(result_data["findings"])
        if outcome in (
            "findings", "blocked",
            "software_verified_external_acceptance_blocked",
        ):
            # Task 10 §16: verification findings/blocked become next-round
            # planner input through an orchestrator-minted receipt that binds
            # the exact phase-base commit, the exact structured result digest,
            # the phase tag, and the deterministic-gate evidence.  The
            # software-verified-external-acceptance-blocked outcome preserves
            # the exact structured phase-result bytes (the tester's blocked
            # references are evidence) but publishes no findings receipt: the
            # external blockers are already explicit in the plan and the
            # outcome advances to the independent audit.
            #
            # Task 23 (F): the exact-commit Redactor masks every free-text
            # finding and blocked reference and rebuilds the preserved
            # structured result bytes from the redacted content **before**
            # any durable storage (the preserved artifact, the receipt, the
            # control state) and before the next planner can receive them; a
            # redactor that cannot be verified fails the campaign closed, and
            # an individual redaction failure carries the fixed marker, never
            # the raw bytes.  The receipt/state digest binds the redacted
            # preserved bytes.
            findings_red, blocked_red, redacted_bytes = (
                self._redact_findings_content(result_data, "verification")
            )
            redacted_digest = plan_sha256(redacted_bytes)
            self._preserve_phase_result(state, "verification", redacted_bytes)
            if outcome in ("findings", "blocked"):
                self._publish_findings(
                    state, phase="verification", phase_tag=tag,
                    phase_base_commit=head, outcome=outcome,
                    result_digest=redacted_digest,
                    findings=findings_red,
                    blocked_on=blocked_red,
                    gate_ran=gate_ran, gate_exit=gate_exit,
                    capability_ran=capability_ran,
                    capability_exit=capability_exit,
                )
            record_result_digest = redacted_digest
        else:
            # A clean verification phase has no findings/blocked content to
            # redact: the phase record still binds the exact structured
            # phase-result bytes the orchestrator consumed (or the honest
            # zero marker when no result file was produced).
            record_result_digest = result_digest or ("0" * 64)
        detail = (
            violation
            or (role.diagnostic if role.exit_status != 0 else "")
            or gate_detail
            or ""
        )
        state2 = state_module.advance(state, outcome)
        state_module.write_state(self._root, state2)
        return _Step(
            self._record(state, attempt, outcome, detail,
                         result_digest=record_result_digest),
            state=state2,
        )

    def _step_audit(self, state: state_module.FactoryState) -> _Step:
        head = self._git.head()
        # One fresh retry absorbs a transient/malformed/missing auditor process
        # without weakening the read-only scope. A dirty audit never retries;
        # the second failure remains the terminal fail-closed audit outcome.
        attempt = 1
        while True:
            tag = self._begin_untrusted(state, attempt)
            self._prepare_phase_result_file(
                self._config.audit_result_path, "audit"
            )
            role = self._run_role("auditor", state, head, attempt=attempt)
            dirty = self._git.role_dirty_paths()
            allow_paths = (
                [self._config.audit_result_path]
                if self._config.audit_result_path else []
            )
            violation = scope_violation(
                dirty, phase="audit",
                plan_path=self._config.plan_path, spec_path=self._config.spec_path,
                allow_paths=allow_paths,
            )
            result_error: Optional[CampaignResultError] = None
            try:
                result = read_phase_result(
                    self._root, self._config.audit_result_path, "audit"
                )
            except CampaignResultError as exc:
                result = None
                result_error = exc
            self._end_untrusted(tag)
            trusted_handoff = (
                result is not None and role.exit_status == 0
                and not role.interrupted and violation is None
            )
            if trusted_handoff or attempt >= 2 or violation is not None:
                if result_error is not None and attempt >= 2:
                    raise result_error
                break
            attempt += 1
        result_data = result[0] if result is not None else None
        result_digest = result[1] if result is not None else ""
        result_bytes = result[2] if result is not None else b""
        result_valid = bool(result_data is not None)
        final_gate_detail = ""
        if (
            result_valid
            and role.exit_status == 0
            and violation is None
            and result_data.get("outcome") == "pass"
            and state.current_round == self._config.rounds_requested
            and self._config.role_driver is None
        ):
            # Auditor JSON is never sufficient for campaign success.  At the
            # only transition that could yield success, execute both exact-
            # commit-bound production commands and reject nonzero, unrun, or
            # skip-marked output.  Deterministic failures become findings;
            # unavailable facts/capabilities therefore cannot be elevated.
            acquisition_ran, acquisition_exit, acquisition_detail = (
                self._ensure_runner_evidence()
            )
            if self._config.capability_command:
                cap_ran, cap_exit, cap_detail, cap_skipped = self._run_gate(
                    self._config.capability_command, "final capability"
                )
            else:
                cap_ran, cap_exit = True, 0
                cap_detail, cap_skipped = "no capabilities required", False
            if acquisition_exit != 0:
                cap_ran = acquisition_ran
                cap_exit = acquisition_exit
                cap_detail = acquisition_detail
            acc_ran, acc_exit, acc_detail, acc_skipped = self._run_gate(
                self._config.acceptance_command, "final acceptance"
            )
            failures: List[str] = []
            if not cap_ran or cap_exit != 0 or cap_skipped:
                failures.append("final capability/evidence command did not pass without skips")
            if not acc_ran or acc_exit != 0 or acc_skipped:
                failures.append("final project acceptance command did not pass without skips")
            if failures:
                result_data = dict(result_data)
                result_data["outcome"] = "findings"
                result_data["findings"] = [
                    *list(result_data.get("findings", [])), *failures
                ]
                final_gate_detail = "; ".join(failures)
            if acquisition_exit < 0:
                # A command/checker binding or signed-protocol integrity
                # failure is control-plane infrastructure, not an ordinary
                # capability finding. Preserve the structured terminal class.
                role = replace(
                    role, exit_status=-1,
                    diagnostic="runner evidence integrity failure",
                )
        outcome = classify_audit(
            role=role,
            scope_ok=violation is None,
            result_valid=result_valid,
            outcome=result_data.get("outcome") if result_data else None,
            findings=list(result_data.get("findings", [])) if result_data else [],
            blocked_refs=list(result_data.get("blocked_on", [])) if result_data else [],
        )
        if outcome in ("findings", "blocked"):
            # Task 10 §16: audit findings/blocked become next-round planner
            # input through an orchestrator-minted receipt; a non-final
            # blocked advances to the next planner exactly like findings with
            # the blocker explicit in the plan, and a final-round receipt is
            # preserved as evidence.
            #
            # Task 23 (F): the exact-commit Redactor masks every free-text
            # finding and blocked reference and rebuilds the preserved
            # structured result bytes from the redacted content **before**
            # any durable storage and before the next planner can receive
            # them (fail closed on an unverifiable redactor; the fixed marker
            # on an individual redaction failure).
            findings_red, blocked_red, redacted_bytes = (
                self._redact_findings_content(result_data, "audit")
            )
            redacted_digest = plan_sha256(redacted_bytes)
            self._preserve_phase_result(state, "audit", redacted_bytes)
            self._publish_findings(
                state, phase="audit", phase_tag=tag,
                phase_base_commit=head, outcome=outcome,
                result_digest=redacted_digest,
                findings=findings_red,
                blocked_on=blocked_red,
                gate_ran=False, gate_exit=None,
                capability_ran=False, capability_exit=None,
            )
            record_result_digest = redacted_digest
        else:
            # A clean audit phase has no findings/blocked content to
            # redact: the phase record still binds the exact structured
            # audit-result bytes the orchestrator consumed (or the honest
            # zero marker when no result file was produced).
            record_result_digest = result_digest or ("0" * 64)
        if outcome in ("interrupted", "infrastructure_failure"):
            # Task 9 review B1: an interrupted audit and an untrusted audit
            # are terminal fail-closed closes with no nonfinal edge; the
            # outcome is persisted in the authoritative control state
            # through the dedicated §11 terminal edge so a later run refuses
            # to re-execute it (the round never advances).
            state2 = state_module.advance(state, outcome)
            state_module.write_state(self._root, state2)
            return _Step(
                self._record(state, attempt, outcome, violation or "audit untrusted"),
                state=state2, terminal=outcome,
            )
        state2 = state_module.advance(state, outcome)
        state_module.write_state(self._root, state2)
        if state2.current_phase == "planning":
            self._rounds_completed = state2.current_round - 1
            self._planning_attempts_used = 0
        elif state2.current_phase in TERMINAL_PHASES:
            # The final round completed: its audit ended the campaign, so the
            # completed-round counter reaches the current round.
            self._rounds_completed = state2.current_round
        audit_detail = (
            role.diagnostic if role.exit_status != 0 else final_gate_detail
        )
        return _Step(
            self._record(state, attempt, outcome, audit_detail,
                         result_digest=record_result_digest),
            state=state2,
        )

    # -- round-zero readiness -------------------------------------------------

    def _run_readiness(self) -> str:
        """Run the complete fixed readiness registry before any planning role.

        Round-zero readiness is a coordinator-owned sidecar concern: the
        attempt/cursor/status and every bound result digest are published to
        the strict ``factory-readiness-state/v1`` sidecar (never a canonical
        state phase or outcome).  A readiness-only campaign additionally
        publishes the separate ``factory-readiness-result/v2`` document.
        """
        accepted = self._config.accepted_commit
        current = self._git.head()
        policy_raw = self._git.blob_at(accepted, readiness_module.POLICY_PATH)
        policy, _policy_sha = readiness_module.load_policy(self._root, policy_raw)
        required = policy["production_authority"]["enrolled"] is True
        nonce = self._runner_readiness_nonce()
        tree = self._git.text(["show", "-s", "--format=%T", accepted]).strip()
        environment_blob = self._git.text(
            ["rev-parse", f"{accepted}:.factory/environment.toml"]
        ).strip()
        # The production install manifest is a readiness binding; read it once
        # up front so the strict sidecar binding never changes mid-campaign.
        try:
            manifest_raw, _ = evidence_module.secure_read_bytes(
                Path(self._config.install_manifest), maximum=INSTALL_MANIFEST_MAX,
                what="production install manifest",
            )
        except (OSError, evidence_module.VerifierBindingError):
            manifest_raw = None
        # Read or initialize the strict campaign-bound readiness sidecar.
        try:
            sidecar = sidecars_module.read_readiness(
                self._root, expected_campaign_id=self._config.campaign_id)
        except sidecars_module.SidecarError:
            sidecar = None
        if sidecar is None:
            readiness = {
                "required": required, "nonce": nonce, "attempt": 0, "cursor": 0,
                "status": "pending" if required else "not_required",
                "accepted_commit": accepted, "tree": tree,
                "environment_blob": environment_blob,
                "specification_sha256": self._config.specification_digest,
                "plan_sha256": self._config.plan_digest,
                "conformance_sha256": "0" * 64,
                "policy_sha256": plan_sha256(policy_raw),
                "contracts_sha256": plan_sha256(
                    self._git.blob_at(accepted, ".factory/capability-contracts.json")),
                "install_manifest_sha256": (
                    plan_sha256(manifest_raw) if manifest_raw is not None else "0" * 64),
                "command_authority_sha256": "0" * 64,
                "human_authority_sha256": "0" * 64,
                "trust_authority_sha256": "0" * 64,
                "aggregate_sha256": "0" * 64,
                "capability_result_sha256": "0" * 64,
                "core_result_sha256": "0" * 64,
                "conformance_result_sha256": "0" * 64,
                "human_result_sha256": "0" * 64,
                "result_sha256": "0" * 64,
                "terminal_outcome": "pending" if required else "not_required",
            }
            sidecar = sidecars_module.ReadinessState(
                schema=sidecars_module.READINESS_SCHEMA,
                campaign_id=self._config.campaign_id,
                readiness=readiness,
            )
        else:
            readiness = dict(sidecar.readiness)
            # Re-bind the immutable campaign binding; a mismatch fails closed.
            readiness.update({
                "required": required, "nonce": nonce,
                "accepted_commit": accepted, "tree": tree,
                "environment_blob": environment_blob,
                "specification_sha256": self._config.specification_digest,
                "plan_sha256": self._config.plan_digest,
                "policy_sha256": plan_sha256(policy_raw),
                "contracts_sha256": plan_sha256(
                    self._git.blob_at(accepted, ".factory/capability-contracts.json")),
                "install_manifest_sha256": (
                    plan_sha256(manifest_raw) if manifest_raw is not None else "0" * 64),
            })

        def publish() -> None:
            nonlocal sidecar
            sidecar = sidecars_module.update_readiness(sidecar, readiness)
            sidecars_module.write_readiness(self._root, sidecar)

        if not required:
            readiness.update({"status": "not_required",
                              "terminal_outcome": "not_required"})
            publish()
            return "human_block"
        readiness.update({"status": "pending", "cursor": 1,
                          "terminal_outcome": "pending"})
        publish()
        aggregate_path = (self._root / ".factory-state" / "runner-evidence" /
                          self._config.campaign_id / nonce / "aggregate.json")
        gate_results: Dict[str, Dict[str, object]] = {}
        gate_ids = list(dict.fromkeys(
            list(policy["conformance_gate_ids"])
            + list(policy["core_gate_ids"])
            + ["conformance-implementation"]
        ))
        aggregate_digest = readiness_module.ZERO
        try:
            runner_argv = list(readiness_module.gate_argv("runner-aggregate")) + [
                "--expected-commit", accepted,
                "--expected-campaign-id", self._config.campaign_id,
                "--expected-readiness-nonce", nonce,
            ]
            runner = self._lock.spawn_child(
                runner_argv, env=self._gate_environment(),
                timeout=min(self._config.gate_timeout,
                            self._remaining_time("readiness runner aggregate")),
            )
            if runner.returncode != 0:
                readiness.update({"status": "findings", "cursor": 2,
                                  "terminal_outcome": "findings"})
                publish()
                return "findings"
            aggregate_raw, _ = evidence_module.secure_read_bytes(
                aggregate_path, maximum=4 * 1024 * 1024,
                what="readiness runner aggregate",
            )
            aggregate = json.loads(aggregate_raw.decode("utf-8"))
            aggregate_digest = readiness_module.validate_aggregate(
                aggregate, policy, accepted_commit=accepted, tree=tree,
                environment_blob=environment_blob,
                campaign_id=self._config.campaign_id,
                readiness_nonce=nonce,
            )
            for gate_id in gate_ids:
                argv = list(readiness_module.gate_argv(gate_id))
                completed = self._lock.spawn_child(
                    argv, env=self._gate_environment(),
                    timeout=min(self._config.gate_timeout,
                                self._remaining_time(f"readiness {gate_id}")),
                )
                transcript = ((completed.stdout or "") + "\0" +
                              (completed.stderr or "")).encode("utf-8", "replace")
                gate_results[gate_id] = {
                    "ran": True, "exit": completed.returncode,
                    "digest": hashlib.sha256(transcript).hexdigest(),
                }
        except (OSError, ValueError, evidence_module.VerifierBindingError,
                readiness_module.ReadinessError, lock_module.RootLockError):
            readiness.update({"status": "infrastructure_failure", "cursor": 2,
                              "terminal_outcome": "infrastructure_failure"})
            publish()
            return "infrastructure_failure"
        readiness.update({"status": "acquiring", "cursor": 3,
                          "terminal_outcome": "pending"})
        publish()
        # Human approval is deliberately validated separately from machine
        # gates.  A required but unavailable authority remains blocked.
        human_digest = readiness_module.digest({"required": False})
        human = policy.get("human_approval")
        if human is not None:
            try:
                approval = readiness_module.read_dirfd_file(
                    self._root, human["approval_path"], maximum=256*1024)
                signature = readiness_module.read_dirfd_file(
                    self._root, human["signature_path"], maximum=64*1024)
                trust = readiness_module.read_external_trust(human["trust_path"])
                human_digest = readiness_module.validate_human_authority(
                    policy, approval, trust, signature_raw=signature,
                    accepted_commit=accepted, accepted_tree=tree,
                    blob_at=self._git.blob_at,
                )
            except readiness_module.HumanAuthorityBlocked:
                readiness.update({"status": "human_blocked", "cursor": 4,
                                  "terminal_outcome": "blocked"})
                publish()
                return "human_block"
            except readiness_module.ReadinessError:
                readiness.update({"status": "infrastructure_failure", "cursor": 4,
                                  "terminal_outcome": "infrastructure_failure"})
                publish()
                return "infrastructure_failure"
        status, result_digests = readiness_module.evaluate(
            policy, aggregate_sha256=aggregate_digest,
            gate_results=gate_results, human_sha256=human_digest,
        )
        if status == "complete":
            if manifest_raw is None:
                readiness.update({"status": "infrastructure_failure", "cursor": 5,
                                  "terminal_outcome": "infrastructure_failure"})
                publish()
                return "infrastructure_failure"
            current_tree=self._git.text(["show","-s","--format=%T",current]).strip()
            bindings=readiness_module.readiness_bindings(
                accepted_commit=accepted, accepted_tree=tree,
                current_commit=current, current_tree=current_tree,
                config_sha256=plan_sha256(self._git.blob_at(accepted,".factory/config.toml")),
                environment_sha256=plan_sha256(self._git.blob_at(accepted,".factory/environment.toml")),
                specification_sha256=self._config.specification_digest,
                plan_sha256=self._config.plan_digest,
                contracts_sha256=plan_sha256(self._git.blob_at(accepted,".factory/capability-contracts.json")),
                policy_sha256=plan_sha256(policy_raw),
                trust_sha256=human_digest,
                install_manifest_sha256=plan_sha256(manifest_raw),
            )
            self._readiness_document=readiness_module.result_document(
                campaign_id=self._config.campaign_id, nonce=nonce,
                status=status, bindings=bindings, results=result_digests,
            )
            readiness.update({
                "status": "complete", "cursor": 6,
                "aggregate_sha256": result_digests["aggregate"],
                "capability_result_sha256": result_digests.get(
                    "capability-evidence", result_digests["aggregate"]),
                "core_result_sha256": result_digests.get(
                    "boilerplate-verification", result_digests["aggregate"]),
                "conformance_result_sha256": result_digests.get(
                    "conformance-implementation", result_digests["aggregate"]),
                "human_result_sha256": result_digests["human"],
                "result_sha256": readiness_module.digest(self._readiness_document),
                "terminal_outcome": "pass",
            })
            publish()
        else:
            outcome = {"findings": "findings", "human_block": "blocked",
                       "infrastructure_failure": "infrastructure_failure"}[status]
            readiness.update({"status": status, "cursor": 5,
                              "terminal_outcome": outcome})
            publish()
        return status

    def _readiness_terminal(self, status: str) -> CampaignResult:
        phase = {"complete":"readiness_complete", "findings":"findings",
                 "human_block":"blocked",
                 "infrastructure_failure":"infrastructure_failure"}[status]
        outcome = "readiness_complete" if status == "complete" else (
            "blocked" if status == "human_block" else status)
        result = CampaignResult(
            campaign_id=self._config.campaign_id,
            rounds_requested=self._config.rounds_requested,
            rounds_completed=0, terminal_phase=phase,
            terminal_outcome=outcome, head_commit=self._git.head(),
            phase_history=(),
        )
        result.validate(); validate_campaign_result(result)
        if self._readiness_document is not None:
            state_module.atomic_write_json(
                self._root, "readiness-result.json", self._readiness_document)
        self._publish_result(result)
        return result

    # -- campaign loop ---------------------------------------------------------

    def run(self) -> CampaignResult:
        """Run inside this campaign's sole lifecycle/evidence namespace."""
        self._deadline = time.monotonic() + self._config.campaign_timeout
        if self._config.role_driver is None:
            # Programmatic callers cannot bypass the CLI preflight.  Re-prove
            # clean HEAD, accepted commit, installed bytes/manifest, explicit
            # verifier, provider, and the already-reserved private namespace
            # immediately before acquisition and any role launch.
            _production_preflight(
                self._root, self._config,
                self._config.verification_command,
                acceptance_command=self._config.acceptance_command,
                capability_command=self._config.capability_command,
                runner_command=self._config.runner_command,
                reserve_namespace=False,
            )
        if self._config.state_namespace:
            with state_module.campaign_state_directory(self._state_directory()):
                return self._run_in_state_namespace()
        # Direct in-process fixture authorities retain their isolated legacy
        # ``.factory-state`` path; production can never reach this branch.
        return self._run_in_state_namespace()

    def _run_in_state_namespace(self) -> CampaignResult:
        self._acquire()
        try:
            if self._config.role_driver is None:
                readiness_status = self._run_readiness()
                if readiness_status != "complete" or self._config.readiness_only:
                    return self._readiness_terminal(readiness_status)
                if self._readiness_document is None:
                    raise CampaignPhaseError("complete readiness document was not published")
                state_module.atomic_write_json(
                    self._root, "readiness-result.json", self._readiness_document)
                self._launch_store = readiness_module._open_locked_authorization_store(
                    self._root, self._config.state_namespace,
                    self._config.campaign_id, self._runner_readiness_nonce(),
                    self._lock.fd, self._readiness_document,
                )
            state, recovered = self._load_or_init_state()
            history: List[PhaseRecord] = []
            if recovered is not None:
                history.append(recovered)
            self._records = list(history)
            terminal_outcome: Optional[str] = None
            terminal_phase: str = state.current_phase
            if state.current_phase in TERMINAL_PHASES:
                # Task 10 REQ 1: a crash-window reconciliation may complete a
                # final audit directly into a terminal (its findings receipt
                # was published before the transition write was lost).  The
                # recovered record carries the terminal outcome; a terminal
                # state loaded without a recovered record is an operator
                # resolution, never a re-run.
                if recovered is None:
                    raise CampaignPhaseError(
                        "the loaded control state is already terminal; the "
                        "campaign must be resolved by the operator, not "
                        "re-run"
                    )
                terminal_outcome = recovered.outcome
            else:
                while state.current_phase not in TERMINAL_PHASES:
                    step = self._step(state)
                    history.append(step.record)
                    self._records.append(step.record)
                    if step.terminal is not None:
                        terminal_outcome = step.record.outcome
                        terminal_phase = step.terminal
                        break
                    if step.state is not None:
                        state = step.state
                        if state.current_phase in TERMINAL_PHASES:
                            terminal_outcome = step.record.outcome
                            terminal_phase = state.current_phase
                            break
                    # A retry keeps the same live phase for the next attempt; an
                    # advanced live phase (planning -> implementation, ...) is the
                    # same trusted state machine.  Both resume the loop.
                    if step.retry or step.state is not None:
                        continue
                    raise CampaignPhaseError(
                        f"step outcome {step.record.outcome!r} neither retried "
                        "nor advanced the campaign"
                    )
            head = self._git.head()
            result = CampaignResult(
                campaign_id=self._config.campaign_id,
                rounds_requested=self._config.rounds_requested,
                rounds_completed=self._rounds_completed,
                terminal_phase=terminal_phase,
                terminal_outcome=terminal_outcome,
                head_commit=head,
                phase_history=tuple(history),
            )
            result.validate()
            validate_campaign_result(result)
            self._publish_result(result)
            return result
        finally:
            if self._launch_store is not None:
                self._launch_store.close()
                self._launch_store = None
            if self._held_verifier is not None:
                self._held_verifier.close()
                self._held_verifier = None
            if self._held_driver is not None:
                self._held_driver.close()
                self._held_driver = None
            if self._held_capability is not None:
                self._held_capability.close()
                self._held_capability = None
            if self._held_runner is not None:
                self._held_runner.close()
                self._held_runner = None
            if self._held_runner_checker is not None:
                self._held_runner_checker.close()
                self._held_runner_checker = None
            if self._held_acceptance is not None:
                self._held_acceptance.close()
                self._held_acceptance = None
            if self._lock is not None:
                self._lock.release()

    def _publish_result(self, result: CampaignResult) -> None:
        """Publish the machine result under the ignored evidence namespace.

        Publication is write-once/no-replace and byte-idempotent: a
        byte-exact re-publication across a crash window is accepted, and any
        other pre-existing content (a forged, tampered, or foreign result)
        fails closed instead of being silently replaced (Phase 2A
        hardening).
        """
        name = f"campaign-result-{self._config.campaign_id}.json"
        raw = json.dumps(
            result.to_dict(), sort_keys=True, separators=(",", ":")
        ).encode("utf-8") + b"\n"
        try:
            state_module.atomic_write(self._root, name, raw, no_replace=True)
        except state_module.StateIOError as exc:
            try:
                existing = state_module.read_bytes(
                    self._root, name, maximum=MAX_RESULT_FILE, missing_ok=False
                )
            except state_module.StateIOError as read_exc:
                raise CampaignResultError(
                    f"cannot publish the campaign result {name}: a marker "
                    f"already exists and cannot be safely re-read ({read_exc})"
                ) from read_exc
            if existing != raw:
                raise CampaignResultError(
                    f"cannot publish the campaign result {name}: a different "
                    "marker already exists; a tampered or foreign campaign "
                    "result fails closed"
                ) from exc


# ---------------------------------------------------------------------------
# Binding derivation (CLI)
# ---------------------------------------------------------------------------


def derive_campaign_config(
    root: Path,
    *,
    campaign_id: str,
    rounds: int,
    branch: str,
    plan_path: str,
    planning_attempts: int,
    implementation_attempts: int,
    provider: str,
    model: str,
    backend: str,
    role_driver: Optional[str],
    developer_evidence_path: str = "",
    scenario_path: str,
    acceptance_command: Sequence[str],
    verification_command: Sequence[str],
    capability_command: Sequence[str],
    phase_result_path: str,
    audit_result_path: str,
    role_timeout: float,
    gate_timeout: float,
    runner_command: Sequence[str] = (),
    runner_timeout: float = DEFAULT_RUNNER_TIMEOUT,
    campaign_timeout: float = DEFAULT_CAMPAIGN_TIMEOUT,
    state_namespace: str = "",
    accepted_commit: str = "",
    install_manifest: str = "",
    readiness_only: bool = False,
) -> CampaignConfig:
    """Derive every binding from the committed state at the current HEAD.

    The spec path/commit/blob and the cycle base come from the committed
    canonical plan's front matter; every digest (spec, plan, prompt set, and
    per-role prompts) is re-derived from the committed blobs at the bound
    HEAD, never from operator claims.
    """
    root = Path(root).absolute()
    head = _live_head(root)
    plan_data = _blob_at(root, plan_path)
    try:
        plan = plan_parser.Plan.from_bytes(plan_data)
    except plan_parser.PlanError as exc:
        raise CampaignConfigError(f"the canonical plan does not parse: {exc}") from exc
    spec_path = plan.spec_path
    spec_commit = plan.spec_commit
    spec_blob = plan.spec_blob
    spec_data = _blob(root, spec_path)
    prompt_digests: Dict[str, str] = {}
    for role in launch_module.ROLES:
        prompt_digests[role] = plan_sha256(
            _blob(root, f".factory/prompts/{role}.md")
        )
    prompt_set = hashlib.sha256()
    for role in sorted(launch_module.ROLES):
        prompt_set.update(role.encode("utf-8"))
        prompt_set.update(b"\x00")
        prompt_set.update(_blob(root, f".factory/prompts/{role}.md"))
        prompt_set.update(b"\x00")
    audit_digest = plan_sha256(_blob(root, ".factory/audit-objectives/registry.json"))
    registry, implementation_digests, hook_configuration_digest = (
        _derive_pre_round_binding(root, bound_commit=head)
    )
    return CampaignConfig(
        root=root,
        campaign_id=campaign_id,
        rounds_requested=rounds,
        branch=branch,
        spec_path=spec_path,
        spec_commit=spec_commit,
        spec_blob=spec_blob,
        plan_path=plan_path,
        phase_base_commit=head,
        planning_attempts=planning_attempts,
        implementation_attempts=implementation_attempts,
        specification_digest=plan_sha256(spec_data),
        plan_digest=plan_sha256(plan_data),
        role_prompt_digests=prompt_digests,
        prompt_set_digest=prompt_set.hexdigest(),
        audit_objectives_digest=audit_digest,
        pre_round_registry=registry,
        pre_round_implementation_digests=implementation_digests,
        pre_round_hook_configuration_digest=hook_configuration_digest,
        pre_round_hook_commit=head,
        provider=provider,
        model=model,
        backend=backend,
        role_driver=role_driver,
        developer_evidence_path=developer_evidence_path,
        scenario_path=scenario_path,
        acceptance_command=tuple(acceptance_command),
        verification_command=tuple(verification_command),
        capability_command=tuple(capability_command),
        runner_command=tuple(runner_command),
        phase_result_path=phase_result_path,
        audit_result_path=audit_result_path,
        state_namespace=state_namespace,
        accepted_commit=accepted_commit,
        install_manifest=install_manifest,
        readiness_only=readiness_only,
        role_timeout=role_timeout,
        gate_timeout=gate_timeout,
        runner_timeout=runner_timeout,
        campaign_timeout=campaign_timeout,
    )


def _blob(root: Path, relpath: str) -> bytes:
    return _blob_at(root, relpath)


# ---------------------------------------------------------------------------
# Trusted control-plane CLI
# ---------------------------------------------------------------------------


def main(argv: Optional[Sequence[str]] = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        prog="factory-campaign",
        description=(
            "Trusted finite campaign orchestrator with exact-commit ordered "
            "pre-round hooks before every planner (FACTORY-LOOP-SPEC §10-§15) "
            "and the fixed §10 quota decision before every model invocation. "
            "Never invoked by a model role."
        ),
    )
    parser.add_argument(
        "--root",
        metavar="ROOT",
        default=str(Path(__file__).resolve().parent.parent.parent),
        help="canonical repository root (default: this repository)",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_run = sub.add_parser(
        "run", help="run ordered pre-round hooks and phases to a §14 terminal"
    )
    p_run.add_argument("--campaign-id", required=True)
    p_run.add_argument("--rounds", type=int, required=True)
    p_run.add_argument("--branch", required=True)
    p_run.add_argument("--plan-path", default=".factory/artifacts/implementation-plan.md")
    p_run.add_argument("--planning-attempts", type=int, default=3)
    p_run.add_argument("--implementation-attempts", type=int, default=3)
    p_run.add_argument("--provider", default=None)
    p_run.add_argument("--model", default=None)
    p_run.add_argument("--backend", default=None)
    p_run.add_argument(
        "--accepted-commit", default="", metavar="SHA40",
        help="exact accepted clean commit the production campaign must bind",
    )
    p_run.add_argument(
        "--install-manifest", default="", metavar="ABSOLUTE_FILE",
        help=(
            "absolute production install manifest; the executing installed "
            "control plane must verify byte-exactly at --accepted-commit"
        ),
    )
    p_run.add_argument("--role-driver", default=None, metavar="RELPATH")
    p_run.add_argument(
        "--developer-evidence-path",
        default="",
        metavar="RELPATH",
        help=(  # Task 22 evidence-smoke lane
            "the exact bounded repository-relative evidence artifact the "
            "developer role may create under `.factory/artifacts/`"
        ),
    )
    p_run.add_argument(
        "--evidence-smoke",
        action="store_true",
        help=(  # Task 22 evidence-smoke lane
            "fail-closed evidence-smoke mode: exactly one full phase round "
            "with the designated committed smoke seam, a clean tree at the "
            "exact branch/commit, and no arbitrary role candidate"
        ),
    )
    p_run.add_argument(
        "--evidence-bound-commit",
        default="",
        metavar="SHA40",
        help=(  # Task 22 evidence-smoke lane
            "the exact bound commit the evidence-smoke run must observe; a "
            "different HEAD fails closed"
        ),
    )
    p_run.add_argument("--scenario", default=None, metavar="RELPATH")
    p_run.add_argument(
        "--phase-result", default=None, metavar="RELPATH",
    )
    p_run.add_argument(
        "--audit-result", default=None, metavar="RELPATH",
    )
    p_run.add_argument("--acceptance-command", action="append", default=[])
    p_run.add_argument("--verification-command", action="append", default=[])
    p_run.add_argument("--capability-command", action="append", default=[])
    p_run.add_argument(
        "--runner-command", action="append", default=[],
        help=(
            "exact coordinator-owned runner acquisition argv; production "
            "requires ./.factory/tools/run-factory-runners.py with no shell/arguments"
        ),
    )
    p_run.add_argument(
        "--preflight-only", action="store_true", help=argparse.SUPPRESS,
    )
    p_run.add_argument(
        "--readiness-only", action="store_true",
        help="run mandatory round-zero readiness only; never report campaign success",
    )
    p_run.add_argument("--role-timeout", type=float, default=DEFAULT_ROLE_TIMEOUT)
    p_run.add_argument("--gate-timeout", type=float, default=DEFAULT_GATE_TIMEOUT)
    p_run.add_argument(
        "--runner-timeout", type=float, default=DEFAULT_RUNNER_TIMEOUT,
        help=f"bounded runner acquisition timeout (maximum {MAX_RUNNER_TIMEOUT:g}s)",
    )
    p_run.add_argument(
        "--campaign-timeout", type=float, default=None, metavar="SECONDS",
        help=(
            "required bounded production wall-clock deadline (maximum "
            f"{MAX_CAMPAIGN_TIMEOUT:g}s), including quota waits"
        ),
    )

    args = parser.parse_args(argv)
    root = Path(args.root)
    if args.command == "run":
        try:
            verification_command = _flatten(args.verification_command)
            acceptance_command = _flatten(args.acceptance_command)
            capability_command = _flatten(args.capability_command)
            runner_command = _flatten(args.runner_command)
            if not args.role_driver and not args.readiness_only:
                missing_commands = [
                    option for option, command in (
                        ("--verification-command", verification_command),
                        ("--acceptance-command", acceptance_command),
                    ) if not command
                ]
                if missing_commands:
                    raise CampaignConfigError(
                        "production campaign requires explicit non-empty "
                        + ", ".join(missing_commands)
                        + "; preflight stops before every role"
                    )
                if args.campaign_timeout is None:
                    raise CampaignConfigError(
                        "production campaign requires --campaign-timeout so "
                        "quota waits and the full campaign are wall-clock bounded"
                    )
                if (
                    args.runner_timeout <= 0
                    or args.runner_timeout > MAX_RUNNER_TIMEOUT
                    or args.runner_timeout != args.runner_timeout
                    or args.runner_timeout == float("inf")
                ):
                    raise CampaignConfigError(
                        f"--runner-timeout must be finite, positive, and at "
                        f"most {MAX_RUNNER_TIMEOUT:g} seconds"
                    )
                if (
                    args.campaign_timeout <= 0
                    or args.campaign_timeout > MAX_CAMPAIGN_TIMEOUT
                    or args.campaign_timeout != args.campaign_timeout
                    or args.campaign_timeout == float("inf")
                ):
                    raise CampaignConfigError(
                        f"--campaign-timeout must be finite, positive, and at "
                        f"most {MAX_CAMPAIGN_TIMEOUT:g} seconds"
                    )
            state_namespace = ""
            if args.role_driver:
                # The embedded driver is a test-only fixture lane.  It is
                # never a production fallback and must be made unmistakable
                # by either the dedicated evidence-smoke mode or a committed
                # scenario file.  Production installed callers get no
                # synthetic defaults from this branch.
                if not args.evidence_smoke and not args.scenario:
                    raise CampaignConfigError(
                        "--role-driver is test-only and requires --scenario "
                        "or the dedicated --evidence-smoke lane"
                    )
                args.provider = args.provider or "synthetic"
                args.model = args.model or "fixture-model"
                args.backend = args.backend or ""
            else:
                missing = [
                    name for name, value in (
                        ("--provider", args.provider),
                        ("--model", args.model),
                        ("--backend", args.backend),
                    ) if not value
                ]
                if missing and not args.readiness_only:
                    raise CampaignConfigError(
                        "production campaign launch requires explicit "
                        + ", ".join(missing)
                    )
                if args.readiness_only:
                    args.provider = args.provider or "synthetic"
                    args.model = args.model or "readiness-only"
                    args.backend = args.backend or ""
            if args.evidence_smoke:
                _evidence_smoke_preflight(root, args)
            elif not args.role_driver:
                # Neutral production is explicitly blocked before install,
                # runner, gate, or model execution. Configuration errors above
                # remain distinguishable from missing human/project authority.
                policy, policy_sha = readiness_module.load_policy(root)
                if policy["production_authority"]["enrolled"] is not True:
                    print(json.dumps({
                        "schema":"factory-campaign-result/v1", "campaign_id":args.campaign_id,
                        "rounds_requested":args.rounds, "rounds_completed":0,
                        "terminal_phase":"blocked", "terminal_outcome":"blocked",
                        "head_commit":_live_head(root), "exit_code":EXIT_BLOCKED,
                        "phase_history":[],
                    }, sort_keys=True, separators=(",", ":")))
                    return EXIT_BLOCKED
                state_namespace = _production_preflight(
                    root, args, verification_command,
                    acceptance_command=acceptance_command,
                    capability_command=capability_command,
                    runner_command=runner_command,
                )
            if args.preflight_only:
                if args.role_driver or not state_namespace:
                    raise CampaignConfigError(
                        "--preflight-only is available only to production installed-copy tests"
                    )
                print(json.dumps({
                    "schema": "factory-production-preflight/v1",
                    "campaign_id": args.campaign_id,
                    "accepted_commit": args.accepted_commit,
                    "installed_root": str(_CONTROL_SOURCE_ROOT),
                    "state_namespace": state_namespace,
                }, sort_keys=True, separators=(",", ":")))
                return EXIT_SUCCESS
            phase_result = args.phase_result or (
                f"{state_namespace}/{CAMPAIGN_PHASE_RESULT_NAME}"
                if state_namespace
                else ".factory-state/factory-phase-result.json"
            )
            audit_result = args.audit_result or (
                f"{state_namespace}/{CAMPAIGN_AUDIT_RESULT_NAME}"
                if state_namespace
                else ".factory-state/factory-audit-result.json"
            )
            config = derive_campaign_config(
                root,
                campaign_id=args.campaign_id,
                rounds=args.rounds,
                branch=args.branch,
                plan_path=args.plan_path,
                planning_attempts=args.planning_attempts,
                implementation_attempts=args.implementation_attempts,
                provider=args.provider,
                model=args.model,
                backend=args.backend,
                role_driver=args.role_driver,
                developer_evidence_path=args.developer_evidence_path,
                scenario_path=args.scenario or "",
                acceptance_command=acceptance_command,
                verification_command=verification_command,
                capability_command=capability_command,
                runner_command=runner_command,
                phase_result_path=phase_result,
                audit_result_path=audit_result,
                role_timeout=args.role_timeout,
                gate_timeout=args.gate_timeout,
                runner_timeout=args.runner_timeout,
                campaign_timeout=(
                    args.campaign_timeout
                    if args.campaign_timeout is not None
                    else DEFAULT_CAMPAIGN_TIMEOUT
                ),
                state_namespace=state_namespace,
                accepted_commit=args.accepted_commit,
                install_manifest=args.install_manifest,
                readiness_only=args.readiness_only,
            )
        except (CampaignError, gitutil.GitBoundaryError) as exc:
            print(f"factory-campaign: {exc}", file=sys.stderr)
            return EXIT_ERROR
        try:
            result = Campaign(config).run()
        except (CampaignError, state_module.StateError, lock_module.RootLockError,
                gitutil.GitBoundaryError) as exc:
            print(f"factory-campaign: {exc}", file=sys.stderr)
            return EXIT_ERROR
        print(json.dumps(result.to_dict(), sort_keys=True, separators=(",", ":")))
        return TERMINAL_EXIT_CODES[result.terminal_phase]
    raise AssertionError("argparse accepted an unknown campaign command")


def _secure_install_manifest(path_text: str) -> Dict[str, object]:
    """Read one external production install manifest without following links."""
    path = Path(path_text)
    if not path.is_absolute():
        raise CampaignConfigError("--install-manifest must be an absolute path")
    try:
        raw, info = evidence_module.secure_read_bytes(
            path, maximum=INSTALL_MANIFEST_MAX, what="production install manifest"
        )
    except evidence_module.VerifierBindingError as exc:
        raise CampaignConfigError(str(exc)) from exc
    if stat.S_IMODE(info.st_mode) != 0o600:
        raise CampaignConfigError(
            "production install manifest must be a current-user-owned mode-0600 file"
        )
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeError, ValueError) as exc:
        raise CampaignConfigError(
            f"production install manifest is not valid JSON: {exc}"
        ) from exc
    if not isinstance(data, dict):
        raise CampaignConfigError("production install manifest must be an object")
    return data


def _private_directory_error(info: os.stat_result, label: str) -> Optional[str]:
    """Return the exact private-directory contract violation, if any."""
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        return f"{label} must be a real directory (never a symlink)"
    if info.st_uid != os.getuid():
        return f"{label} must be owned by the invoking user"
    if stat.S_IMODE(info.st_mode) != 0o700:
        return f"{label} must have mode 0700"
    return None


def _open_exact_private_directory(
    parent_fd: int, name: str, label: str, flags: int,
) -> int:
    """lstat and open one exact component without listing its parent."""
    try:
        info = os.lstat(name, dir_fd=parent_fd)
    except OSError as exc:
        raise CampaignConfigError(f"cannot validate exact {label}: {exc}") from exc
    error = _private_directory_error(info, label)
    if error:
        raise CampaignConfigError(error)
    try:
        descriptor = os.open(name, flags, dir_fd=parent_fd)
    except OSError as exc:
        raise CampaignConfigError(f"cannot open exact {label}: {exc}") from exc
    opened = os.fstat(descriptor)
    if (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
        os.close(descriptor)
        raise CampaignConfigError(f"{label} changed while opening")
    error = _private_directory_error(opened, label)
    if error:
        os.close(descriptor)
        raise CampaignConfigError(error)
    return descriptor


def _campaign_namespace_descriptors(
    root: Path, campaign_id: str, *, create: bool,
) -> Tuple[int, int, int, int]:
    """Open the exact state/campaign hierarchy, optionally creating the leaf.

    This authority never lists or reads ``.factory-state`` content.  It
    lstat's only the exact state root and fixed ``campaigns`` component, then
    creates only a missing fixed parent and one fresh safe campaign child via
    ``mkdirat``.  Existing foreign entries are never opened, renamed, removed,
    overwritten, or metadata-mutated.
    """
    if not SAFE_CAMPAIGN_ID_RE.fullmatch(campaign_id):
        raise CampaignConfigError("campaign id is unsafe for state namespace")
    flags = (
        os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )
    root_fd = state_fd = parent_fd = leaf_fd = -1
    try:
        try:
            root_fd = os.open(Path(root).absolute(), flags)
        except OSError as exc:
            raise CampaignConfigError(
                f"cannot open canonical campaign repository root: {exc}"
            ) from exc
        state_fd = _open_exact_private_directory(
            root_fd, CAMPAIGN_STATE_ROOT_REL, CAMPAIGN_STATE_ROOT_REL, flags
        )
        try:
            parent_info = os.lstat(CAMPAIGN_STATE_PARENT_NAME, dir_fd=state_fd)
        except FileNotFoundError:
            if not create:
                raise CampaignConfigError(
                    "prepared production campaign namespace parent is unavailable"
                )
            try:
                os.mkdir(CAMPAIGN_STATE_PARENT_NAME, 0o700, dir_fd=state_fd)
                os.fsync(state_fd)
            except FileExistsError as exc:
                raise CampaignConfigError(
                    "campaign namespace parent collision while creating exact "
                    f"{CAMPAIGN_STATE_PARENT_REL!r}"
                ) from exc
            except OSError as exc:
                raise CampaignConfigError(
                    f"cannot create exact {CAMPAIGN_STATE_PARENT_REL}: {exc}"
                ) from exc
        except OSError as exc:
            raise CampaignConfigError(
                f"cannot validate exact {CAMPAIGN_STATE_PARENT_REL}: {exc}"
            ) from exc
        parent_fd = _open_exact_private_directory(
            state_fd, CAMPAIGN_STATE_PARENT_NAME, CAMPAIGN_STATE_PARENT_REL, flags
        )
        if create:
            try:
                os.mkdir(campaign_id, 0o700, dir_fd=parent_fd)
                os.fsync(parent_fd)
            except FileExistsError as exc:
                raise CampaignConfigError(
                    f"campaign state namespace collision for {campaign_id!r}; "
                    "choose a new unique campaign id (existing bytes were untouched)"
                ) from exc
            except OSError as exc:
                raise CampaignConfigError(
                    f"cannot create fresh campaign state namespace {campaign_id!r}: {exc}"
                ) from exc
        leaf_fd = _open_exact_private_directory(
            parent_fd, campaign_id,
            f"campaign state namespace {CAMPAIGN_STATE_PARENT_REL}/{campaign_id}",
            flags,
        )
        return root_fd, state_fd, parent_fd, leaf_fd
    except Exception:
        for descriptor in (leaf_fd, parent_fd, state_fd, root_fd):
            if descriptor >= 0:
                os.close(descriptor)
        raise


def _reserve_campaign_namespace(root: Path, campaign_id: str) -> str:
    """Atomically reserve one private per-campaign namespace, no collision."""
    descriptors = _campaign_namespace_descriptors(root, campaign_id, create=True)
    for descriptor in reversed(descriptors):
        os.close(descriptor)
    return f"{CAMPAIGN_STATE_PARENT_REL}/{campaign_id}"


def _validate_reserved_campaign_namespace(root: Path, campaign_id: str) -> str:
    """Revalidate only the exact prepared hierarchy, without enumeration."""
    descriptors = _campaign_namespace_descriptors(root, campaign_id, create=False)
    for descriptor in reversed(descriptors):
        os.close(descriptor)
    return f"{CAMPAIGN_STATE_PARENT_REL}/{campaign_id}"


def _configured_development_branch(root: Path) -> str:
    """Read the sole production branch authority from committed config.toml."""
    import tomllib
    raw = readiness_module.read_dirfd_file(root, ".factory/config.toml", maximum=64 * 1024)
    try:
        value = tomllib.loads(raw.decode("utf-8"))
        branch = value["project"]["development_branch"]
    except (UnicodeError, ValueError, KeyError, TypeError) as exc:
        raise CampaignConfigError("configured development_branch is missing or malformed") from exc
    if not isinstance(branch, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,127}", branch) or branch == "main":
        raise CampaignConfigError("configured development_branch is unsafe or names main")
    return branch


def _production_preflight(
    root: Path,
    args: object,
    verification_command: Sequence[str],
    *,
    acceptance_command: Sequence[str] = (),
    capability_command: Sequence[str] = (),
    runner_command: Sequence[str] = (),
    reserve_namespace: bool = True,
) -> str:
    """Validate a normal production campaign before any role launch.

    Production is accepted only from the exact installed bytes certified by
    a production manifest for the clean repository HEAD.  This closes the
    planner contamination window: a pre-existing diff, stale install, source
    invocation, wrong branch, missing explicit verifier, or reused campaign
    namespace fails before :class:`Campaign` acquires or starts a role.
    """
    root = Path(root).absolute()
    readiness_only = bool(getattr(args, "readiness_only", False))
    for label, command, required in (
        ("verification", verification_command, not readiness_only),
        ("capability", capability_command, False),
        ("acceptance", acceptance_command, not readiness_only),
        ("runner acquisition", runner_command, False),
    ):
        if not command:
            if required:
                raise CampaignConfigError(
                    f"production campaign requires an explicit non-empty {label} command"
                )
            continue
        if not command[0].startswith("./"):
            raise CampaignConfigError(
                f"production {label} command must use a canonical repository-relative ./path"
            )
    if runner_command and tuple(runner_command) != RUNNER_COMMAND:
        raise CampaignConfigError(
            "when configured, production runner acquisition must be exactly "
            "./.factory/tools/run-factory-runners.py with no shell or arguments"
        )
    provider = str(getattr(args, "provider", "")).lower()
    if (not readiness_only and
            (provider not in launch_module.SUPPORTED_PROVIDERS or provider == "synthetic")):
        raise CampaignConfigError(
            "normal production campaigns require a fixed real-model provider; "
            "synthetic is confined to explicit fixture/role-driver lanes"
        )
    accepted = getattr(args, "accepted_commit", "")
    if not isinstance(accepted, str) or not SHA40_RE.fullmatch(accepted):
        raise CampaignConfigError(
            "production campaign requires --accepted-commit with a strict 40-hex commit"
        )
    head = _live_head(root)
    if head != accepted:
        raise CampaignConfigError(
            f"HEAD {head} does not equal accepted production commit {accepted}"
        )
    branch = gitutil.git_run(
        ["-C", str(root), "rev-parse", "--abbrev-ref", "HEAD"],
        timeout=GIT_TIMEOUT,
    )
    required_branch = _configured_development_branch(root)
    caller_branch = getattr(args, "branch", "")
    if caller_branch != required_branch or required_branch == "main":
        raise CampaignConfigError(
            "--branch must equal [project].development_branch and main is never a development branch"
        )
    if branch.returncode != 0 or branch.stdout.strip() != required_branch:
        raise CampaignConfigError(
            f"production branch {branch.stdout.strip()!r} does not equal "
            f"configured development branch {required_branch!r}"
        )
    status = gitutil.git_run(
        ["-C", str(root), "status", "--porcelain", "-z", "--untracked-files=all"],
        timeout=GIT_TIMEOUT,
    )
    if status.returncode != 0 or status.stdout:
        raise CampaignConfigError(
            "production campaign requires a clean Git tree before planning; "
            "the planner may never absorb pre-existing changes"
        )
    manifest_path = getattr(args, "install_manifest", "")
    manifest = _secure_install_manifest(manifest_path)
    if (
        manifest.get("schema") != installer_module.INSTALL_MANIFEST_SCHEMA
        or manifest.get("installation_mode") != "production"
        or manifest.get("acceptance_eligible") is not True
        or manifest.get("commit") != accepted
        or Path(str(manifest.get("root", ""))).absolute() != root
        or Path(str(manifest.get("prefix", ""))).absolute() != _CONTROL_SOURCE_ROOT
    ):
        raise CampaignConfigError(
            "production install manifest does not bind this executing installed "
            "control plane, repository, and accepted commit"
        )
    try:
        commands = [command for command in (verification_command, acceptance_command) if command]
        if capability_command:
            commands.append(capability_command)
        if runner_command:
            commands.extend((runner_command, RUNNER_CHECKER_COMMAND))
        for command in commands:
            held = evidence_module.HeldVerifier(
                root,
                evidence_module.bind_verifier(
                    root, tuple(command), commit=head
                ),
            )
            held.close()
        errors = installer_module.verify_staged(root, _CONTROL_SOURCE_ROOT, manifest)
    except (installer_module.InstallerError,
            evidence_module.VerifierBindingError) as exc:
        raise CampaignConfigError(
            f"exact-commit installed control-plane/verifier verification failed: {exc}"
        ) from exc
    if errors:
        raise CampaignConfigError(
            "exact-commit installed control-plane verification failed: "
            + "; ".join(errors[:8])
        )
    campaign_id = getattr(args, "campaign_id", "")
    if reserve_namespace:
        return _reserve_campaign_namespace(root, campaign_id)
    expected = f"{CAMPAIGN_STATE_PARENT_REL}/{campaign_id}"
    if getattr(args, "state_namespace", expected) != expected:
        raise CampaignConfigError(
            f"production campaign state namespace must be {expected!r}"
        )
    return _validate_reserved_campaign_namespace(root, campaign_id)


def _evidence_smoke_preflight(root: Path, args) -> None:
    """Fail closed before the evidence-smoke round drives the repository.

    Task 22: the ``--evidence-smoke`` mode is the explicit trusted smoke
    surface.  It refuses any arbitrary role candidate (only the designated
    committed seam), a non-synthetic provider, a multi-round campaign, a
    campaign id without the private seam label, an absent developer
    evidence binding, an absent result channel, a dirty worktree, a wrong
    branch, or a HEAD that differs from the bound commit.  The designated
    seam must be the exact committed blob at HEAD.
    """
    if args.provider.lower() != "synthetic":
        raise CampaignConfigError(
            "the evidence-smoke lane never invokes an external model; "
            "`--provider` must be `synthetic`"
        )
    if args.backend:
        raise CampaignConfigError(
            "the evidence-smoke lane binds no model backend"
        )
    if args.rounds != 1:
        raise CampaignConfigError(
            "the evidence-smoke lane is exactly one full phase cycle "
            "(`--rounds 1`)"
        )
    if args.role_driver != EVIDENCE_SMOKE_DRIVER_REL:
        raise CampaignConfigError(
            "the evidence-smoke lane refuses an arbitrary role candidate; "
            f"only the designated committed seam {EVIDENCE_SMOKE_DRIVER_REL!r} "
            "is accepted"
        )
    if not args.campaign_id.startswith(EVIDENCE_SMOKE_ID_PREFIX):
        raise CampaignConfigError(
            "the evidence-smoke campaign id must carry the private seam label "
            f"`{EVIDENCE_SMOKE_ID_PREFIX}`"
        )
    if not args.developer_evidence_path:
        raise CampaignConfigError(
            "the evidence-smoke lane requires the designated developer "
            "evidence artifact binding"
        )
    if not args.phase_result or not args.audit_result:
        raise CampaignConfigError(
            "the evidence-smoke lane requires both structured result channels"
        )
    if args.plan_path != ".factory/artifacts/implementation-plan.md":
        raise CampaignConfigError(
            "the evidence-smoke lane drives the canonical plan only"
        )
    if not args.evidence_bound_commit:
        raise CampaignConfigError(
            "the evidence-smoke lane requires the exact bound commit "
            "(`--evidence-bound-commit`); a smoke round never runs against "
            "an unverified HEAD"
        )
    if not SHA40_RE.fullmatch(args.evidence_bound_commit):
        raise CampaignConfigError(
            "the evidence bound commit must be a 40-hex Git commit hash"
        )
    head = _live_head(root)
    if head != args.evidence_bound_commit:
        raise CampaignConfigError(
            f"HEAD {head} does not match the bound exact commit "
            f"{args.evidence_bound_commit}; the evidence-smoke round fails "
            "closed on a non-exact commit"
        )
    branch = gitutil.git_run(
        ["-C", str(root), "rev-parse", "--abbrev-ref", "HEAD"],
        timeout=GIT_TIMEOUT,
    )
    if branch.returncode != 0 or branch.stdout.strip() != args.branch:
        raise CampaignConfigError(
            f"branch {branch.stdout.strip()!r} does not match the required "
            f"branch {args.branch!r}"
        )
    status = gitutil.git_run(
        ["-C", str(root), "status", "--porcelain", "-z", "--untracked-files=all"],
        timeout=GIT_TIMEOUT,
    )
    if status.returncode != 0 or status.stdout:
        raise CampaignConfigError(
            "the worktree is not clean; the evidence-smoke round requires a "
            "clean tree at the exact bound commit"
        )
    # The smoke lane checks only its explicit outputs.  It never enumerates
    # or attempts recovery from unrelated runtime-state content.
    for rel, label in (
        (args.phase_result, "phase result"),
        (args.audit_result, "audit result"),
        (args.developer_evidence_path, "evidence artifact"),
    ):
        if not rel:
            continue
        if (root / rel).exists():
            raise CampaignConfigError(
                f"an existing {label} collides with the evidence round's "
                f"no-replace output: {rel}"
            )
    committed = _blob_at(root, args.role_driver)
    worktree = _bounded_read(
        root, args.role_driver, "role driver", MAX_RESULT_FILE * 4
    )
    if committed != worktree:
        raise CampaignConfigError(
            "the designated smoke seam is not the exact committed blob at "
            "HEAD; a substituted seam fails closed"
        )


def _flatten(groups: Sequence[Sequence[str]]) -> List[str]:
    """Flatten one layer of ``--command`` argument groups.

    ``argparse`` with ``action="append"`` yields a list where each element is
    one command token (``["/bin/false"]``) or, when the operator passes a
    multi-token command whose tokens carry leading dashes (for example the
    evidence-smoke gate ``--root ... --mode acceptance``), a JSON array of
    tokens (``'["./gate.py", "--mode", "acceptance"]'``).  A plain token
    is preserved exactly — never split into characters — so a command can
    never be mangled into per-character argv, and a JSON-array element is
    unrolled into its exact tokens.
    """
    flattened: List[str] = []
    for group in groups:
        if isinstance(group, str):
            try:
                parsed = json.loads(group)
            except ValueError:
                parsed = None
            if (
                isinstance(parsed, list)
                and parsed
                and all(isinstance(item, str) and item for item in parsed)
            ):
                flattened.extend(parsed)
            else:
                flattened.append(group)
        else:
            flattened.extend(str(item) for item in group)
    return flattened


if __name__ == "__main__":
    sys.exit(main())
