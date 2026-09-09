#!/usr/bin/env python3
"""Generic adaptive campaign/audit scheduler authority (Phase 2B2).

This module implements the trusted, deterministic scheduler decisions of the
adaptive campaign loop on top of the committed campaign budget
(``factory-campaign-budget/v1``, ``.factory/campaign-budget.json``) and the
scheduler-extension fields of the canonical control state
(``factory-state/v1``).  It is a pure-function authority: every decision is a
deterministic function of the budget, the plan state, the control state, and
the trusted phase outcomes — never of model prose, wall-clock time, or
runtime randomness.

The scheduler replaces the exact/fixed-round ceremony with a configured
campaign budget:

* ``max_rounds`` — the maximum number of planning/implementation/
  verification/audit cycles (the existing ``--rounds`` option is a maximum
  budget, never an exact count; there is no exactly-five rejection);
* ``max_checkpoints`` — the maximum number of coherent task checkpoints
  (committed task completions) the campaign may produce;
* ``max_wall_seconds`` — the wall-clock budget (the existing
  ``--campaign-timeout``);
* ``max_task_attempts`` — the per-task attempt budget (the existing
  ``--implementation-attempts``);
* ``audit_interval`` — the coherent-checkpoint interval at which the
  independent tester/auditor run (milestone-boundary roles, not mandatory
  after every patch);
* ``security_sensitive_paths`` — trusted closed-config path prefixes that
  force an independent audit when a checkpoint touches them (never model
  prose);
* ``mandatory_audit_objectives`` — the audit objective IDs that must all be
  covered before release success, regardless of checkpoint count;
* ``no_progress_limit`` — the number of consecutive audits that reproduce
  the same progress fingerprint before the campaign terminates honestly as
  ``no_progress``.

The campaign continues only while meaningful progress is possible and
terminates deterministically on verified completion,
``software_verified_external_acceptance_blocked``, a persistent
external/infrastructure blocker, repeated/no meaningful progress,
task/campaign budget exhaustion, or interruption.  The terminal reason is a
closed enum (``TERMINAL_REASONS``) recorded in the control state and the
published campaign result so operators can distinguish success, findings,
infrastructure failure, interrupted, budget exhausted/no progress, and
software-verified-external-acceptance-blocked without weakening the existing
bounded public reasons or the exact-commit evidence chain.

The scheduler never selects audit authority from task/plan prose: the
mandatory objective set and the security-sensitive path prefixes come only
from the committed closed config, and the audit objective itself is selected
deterministically from the round number by the committed registry authority
(``audit_objectives.py``).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Mapping, Optional, Sequence, Tuple

SCHEMA_NAME = "factory-campaign-budget/v1"
BUDGET_RELPATH = ".factory/campaign-budget.json"
SCHEMA_FILE = "factory-campaign-budget-v1.schema.json"

MAX_BUDGET_BYTES = 64 * 1024
MAX_OBJECTIVES = 64
MAX_SECURITY_PATHS = 64
MAX_PATH_LENGTH = 512

# Closed terminal-reason enum (result/state metadata; STATE-01 scheduler
# extension).  ``terminal_reason`` is exactly one of these values (or empty
# while the campaign is live); it is set by the trusted harness, never by
# model output.
TERMINAL_REASONS = (
    "success", "findings", "blocked", "failed", "interrupted",
    "infrastructure_failure", "budget_exhausted", "no_progress",
    "software_verified_external_acceptance_blocked",
)

# Closed audit-outcome enum accepted by the terminal-resolution authority
# (``audit_next_phase``).  An unknown outcome is a fail-closed scheduler
# error, never silently treated as a pass.
AUDIT_OUTCOMES = ("findings", "blocked", "pass")

# Documented defaults used when the committed budget config is absent.  The
# defaults preserve the pre-Phase-2B2 behavior: an audit after every
# checkpoint (``audit_interval = 1``), no security-sensitive path forcing, no
# mandatory objective coverage requirement, and a bounded no-progress
# termination after two identical audits.
DEFAULT_MAX_ROUNDS = 5
DEFAULT_MAX_CHECKPOINTS = 100
DEFAULT_MAX_WALL_SECONDS = 21600.0
DEFAULT_MAX_TASK_ATTEMPTS = 3
DEFAULT_AUDIT_INTERVAL = 1
DEFAULT_NO_PROGRESS_LIMIT = 2

# Hard caps (migration/cap bounds): a committed budget may never exceed
# these finite upper bounds, so a malformed or hostile config cannot make the
# campaign unbounded.
MAX_ROUNDS_CAP = 1000
MAX_CHECKPOINTS_CAP = 10000
MAX_WALL_SECONDS_CAP = 86400.0
MAX_TASK_ATTEMPTS_CAP = 100
MAX_AUDIT_INTERVAL_CAP = 10000
MAX_NO_PROGRESS_LIMIT_CAP = 100

OBJECTIVE_ID_RE = re.compile(r"^AUD-[0-9]{2}$")
SAFE_PATH_RE = re.compile(r"^[A-Za-z0-9._/+-]+$")


class SchedulerError(Exception):
    """Base class for every fail-closed scheduler failure."""


class SchedulerConfigError(SchedulerError):
    """The committed campaign budget is malformed, unbounded, or unbound."""


@dataclass(frozen=True)
class CampaignBudget:
    """The trusted campaign budget (bounds, intervals, and closed config).

    ``max_rounds`` is the maximum number of planning/implementation/
    verification/audit cycles (the existing ``--rounds`` option is a maximum
    budget, never an exact count).  ``max_checkpoints`` bounds the coherent
    task checkpoints; ``max_wall_seconds`` bounds the wall clock;
    ``max_task_attempts`` bounds the per-task attempts.  ``audit_interval``
    is the coherent-checkpoint interval at which the independent
    tester/auditor run; ``security_sensitive_paths`` are trusted closed-config
    path prefixes that force an independent audit when a checkpoint touches
    them; ``mandatory_audit_objectives`` are the objective IDs that must all
    be covered before release success; ``no_progress_limit`` bounds the
    consecutive identical-audit streak before honest ``no_progress``
    termination.
    """

    max_rounds: int
    max_checkpoints: int
    max_wall_seconds: float
    max_task_attempts: int
    audit_interval: int
    security_sensitive_paths: Tuple[str, ...]
    mandatory_audit_objectives: Tuple[str, ...]
    no_progress_limit: int

    def __post_init__(self) -> None:
        self.validate()

    def validate(self) -> None:
        for name, value, minimum, maximum in (
            ("max_rounds", self.max_rounds, 1, MAX_ROUNDS_CAP),
            ("max_checkpoints", self.max_checkpoints, 1, MAX_CHECKPOINTS_CAP),
            ("max_task_attempts", self.max_task_attempts, 1, MAX_TASK_ATTEMPTS_CAP),
            ("audit_interval", self.audit_interval, 1, MAX_AUDIT_INTERVAL_CAP),
            ("no_progress_limit", self.no_progress_limit, 1, MAX_NO_PROGRESS_LIMIT_CAP),
        ):
            if (
                isinstance(value, bool)
                or not isinstance(value, int)
                or value < minimum
                or value > maximum
            ):
                raise SchedulerConfigError(
                    f"`{name}` must be an integer within "
                    f"[{minimum}, {maximum}], got {value!r}"
                )
        wall = self.max_wall_seconds
        if (
            isinstance(wall, bool)
            or not isinstance(wall, (int, float))
            or wall <= 0
            or float(wall) != float(wall)
            or float(wall) == float("inf")
            or wall > MAX_WALL_SECONDS_CAP
        ):
            raise SchedulerConfigError(
                f"`max_wall_seconds` must be a finite positive number at most "
                f"{MAX_WALL_SECONDS_CAP:g}, got {wall!r}"
            )
        if len(self.security_sensitive_paths) > MAX_SECURITY_PATHS:
            raise SchedulerConfigError(
                f"`security_sensitive_paths` may carry at most "
                f"{MAX_SECURITY_PATHS} entries"
            )
        for path in self.security_sensitive_paths:
            if (
                not isinstance(path, str)
                or not path
                or len(path) > MAX_PATH_LENGTH
                or not SAFE_PATH_RE.fullmatch(path)
                or path.startswith("/")
                or any(segment in ("", ".", "..") for segment in path.split("/"))
            ):
                raise SchedulerConfigError(
                    f"`security_sensitive_paths` entry {path!r} is not a safe "
                    "repository-relative path prefix"
                )
        if len(self.mandatory_audit_objectives) > MAX_OBJECTIVES:
            raise SchedulerConfigError(
                f"`mandatory_audit_objectives` may carry at most "
                f"{MAX_OBJECTIVES} entries"
            )
        seen: List[str] = []
        for objective_id in self.mandatory_audit_objectives:
            if (
                not isinstance(objective_id, str)
                or not OBJECTIVE_ID_RE.fullmatch(objective_id)
            ):
                raise SchedulerConfigError(
                    f"`mandatory_audit_objectives` entry {objective_id!r} must "
                    "match AUD-<NN>"
                )
            if objective_id in seen:
                raise SchedulerConfigError(
                    f"duplicate mandatory audit objective {objective_id!r}"
                )
            seen.append(objective_id)


def _reject_duplicate_keys(pairs: List[tuple]) -> Dict[str, object]:
    """JSON object-pairs hook: reject any repeated object key."""
    result: Dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise SchedulerConfigError(
                f"duplicate JSON object key in the campaign budget: {key!r}"
            )
        result[key] = value
    return result


def parse_budget(data: bytes) -> CampaignBudget:
    """Deterministically parse and validate the committed budget document.

    Rejects: a non-JSON document, a wrong schema name, an unknown field, a
    missing field, a duplicate key, a value outside the documented caps, an
    unsafe security-sensitive path, or a malformed mandatory objective ID.
    The parsed budget is a deterministic function of the document bytes.
    """
    try:
        document = json.loads(
            data.decode("utf-8"), object_pairs_hook=_reject_duplicate_keys
        )
    except (UnicodeDecodeError, ValueError) as exc:
        raise SchedulerConfigError(
            f"the campaign budget is not valid JSON: {exc}"
        ) from exc
    if not isinstance(document, dict):
        raise SchedulerConfigError("the campaign budget must be a JSON object")
    if document.get("schema") != SCHEMA_NAME:
        raise SchedulerConfigError(
            f"the campaign budget schema must be exactly {SCHEMA_NAME!r}, got "
            f"{document.get('schema')!r}"
        )
    expected = {
        "schema", "max_rounds", "max_checkpoints", "max_wall_seconds",
        "max_task_attempts", "audit_interval", "security_sensitive_paths",
        "mandatory_audit_objectives", "no_progress_limit",
    }
    extra = sorted(set(document) - expected)
    missing = sorted(expected - set(document))
    if extra or missing:
        raise SchedulerConfigError(
            "the campaign budget must carry exactly the documented field set"
            + (f" (extra: {extra})" if extra else "")
            + (f" (missing: {missing})" if missing else "")
        )
    security_paths = document.get("security_sensitive_paths")
    if not isinstance(security_paths, list):
        raise SchedulerConfigError(
            "`security_sensitive_paths` must be a JSON array"
        )
    objectives = document.get("mandatory_audit_objectives")
    if not isinstance(objectives, list):
        raise SchedulerConfigError(
            "`mandatory_audit_objectives` must be a JSON array"
        )
    return CampaignBudget(
        max_rounds=document["max_rounds"],
        max_checkpoints=document["max_checkpoints"],
        max_wall_seconds=document["max_wall_seconds"],
        max_task_attempts=document["max_task_attempts"],
        audit_interval=document["audit_interval"],
        security_sensitive_paths=tuple(security_paths),
        mandatory_audit_objectives=tuple(objectives),
        no_progress_limit=document["no_progress_limit"],
    )


def default_budget() -> CampaignBudget:
    """The documented default budget (used when the committed config is absent).

    The defaults preserve the pre-Phase-2B2 behavior: an audit after every
    checkpoint, no security-sensitive path forcing, no mandatory objective
    coverage requirement, and a bounded no-progress termination after two
    identical audits.
    """
    return CampaignBudget(
        max_rounds=DEFAULT_MAX_ROUNDS,
        max_checkpoints=DEFAULT_MAX_CHECKPOINTS,
        max_wall_seconds=DEFAULT_MAX_WALL_SECONDS,
        max_task_attempts=DEFAULT_MAX_TASK_ATTEMPTS,
        audit_interval=DEFAULT_AUDIT_INTERVAL,
        security_sensitive_paths=(),
        mandatory_audit_objectives=(),
        no_progress_limit=DEFAULT_NO_PROGRESS_LIMIT,
    )


def load_budget_config(root: object) -> CampaignBudget:
    """Load the committed campaign budget or the documented defaults.

    The committed document is read with a bounded no-follow read and parsed
    through :func:`parse_budget`; a malformed, unsafe, or unbounded document
    fails closed as a campaign error (never a silent fallback to defaults).
    An absent document uses the documented defaults.
    """
    root = Path(str(root)).absolute()
    path = root / BUDGET_RELPATH
    flags = (
        os.O_RDONLY
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )
    try:
        descriptor = os.open(str(path), flags)
    except FileNotFoundError:
        return default_budget()
    except OSError as exc:
        raise SchedulerConfigError(
            f"cannot open the campaign budget {path}: {exc}"
        ) from exc
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise SchedulerConfigError(
                f"the campaign budget {path} is not a regular file"
            )
        if info.st_size > MAX_BUDGET_BYTES:
            raise SchedulerConfigError(
                f"the campaign budget {path} exceeds the "
                f"{MAX_BUDGET_BYTES}-byte bound"
            )
        before = (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)
        raw = bytearray()
        while True:
            chunk = os.read(descriptor, 65536)
            if not chunk:
                break
            raw.extend(chunk)
            if len(raw) > MAX_BUDGET_BYTES:
                raise SchedulerConfigError(
                    f"the campaign budget {path} exceeds the bound"
                )
        after = os.fstat(descriptor)
        if (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns) != before:
            raise SchedulerConfigError(
                f"the campaign budget {path} changed while being read"
            )
    finally:
        os.close(descriptor)
    return parse_budget(bytes(raw))


def is_security_sensitive(path: str, budget: CampaignBudget) -> bool:
    """True when ``path`` matches a configured security-sensitive prefix.

    The prefixes come only from the trusted closed config (never model
    prose).  A path matches when it equals a prefix or lives under it; the
    comparison is exact repository-relative path-prefix matching.
    """
    if not budget.security_sensitive_paths:
        return False
    for prefix in budget.security_sensitive_paths:
        if path == prefix or path.startswith(prefix + "/"):
            return True
    return False


def security_sensitive_changed(
    paths: Sequence[str], budget: CampaignBudget
) -> bool:
    """True when any changed path is security-sensitive."""
    return any(is_security_sensitive(path, budget) for path in paths)


def should_audit(
    *,
    checkpoint: int,
    last_audit_checkpoint: int,
    security_changed: bool,
    verifier_risk: bool,
    more_tasks: bool,
    budget: CampaignBudget,
) -> bool:
    """The milestone decision: run the independent tester/auditor now?

    The independent tester/auditor are milestone-boundary roles, not
    mandatory after every patch.  The trusted deterministic verifier still
    runs at each candidate exact commit (the acceptance gate); the
    independent review runs at a milestone:

    * the final milestone — no more runnable tasks remain (the last
      checkpoint before final acceptance);
    * the checkpoint budget is reached (``max_checkpoints``);
    * a security/trust-sensitive path changed in this checkpoint;
    * the verifier risk classification requests it;
    * the configured coherent-checkpoint interval (``audit_interval``) has
      elapsed since the last audit.

    ``checkpoint`` is the 1-based coherent-checkpoint count after the current
    task completion; ``last_audit_checkpoint`` is the checkpoint count at the
    last independent audit (0 before the first).  The decision is a pure
    function of the budget and the trusted inputs.
    """
    if not more_tasks:
        return True
    if checkpoint >= budget.max_checkpoints:
        return True
    if security_changed:
        return True
    if verifier_risk:
        return True
    if checkpoint - last_audit_checkpoint >= budget.audit_interval:
        return True
    return False


def objective_coverage(
    covered: Sequence[str], mandatory: Sequence[str]
) -> bool:
    """True when every configured mandatory objective is covered.

    ``covered`` is the sorted set of objective IDs the independent audits
    have covered so far; ``mandatory`` is the committed closed-config set.
    Coverage is a pure function of the two sets; a mandatory objective that
    was never audited can never be silently skipped.
    """
    covered_set = set(covered)
    return all(objective_id in covered_set for objective_id in mandatory)


def progress_fingerprint(
    *,
    task_statuses: str,
    verification_outcome: str,
    audit_outcome: str,
    covered_objectives: Sequence[str],
) -> str:
    """Deterministic monotonic progress fingerprint of one audit boundary.

    The fingerprint binds the plan task statuses, the verification outcome,
    the audit outcome, and the covered objective set.  Two consecutive audits
    that reproduce the same fingerprint made no meaningful progress; the
    campaign terminates honestly as ``no_progress`` after
    ``no_progress_limit`` identical fingerprints.  ``task_statuses`` is the
    deterministic ``"<id>:<status>,..."`` string derived from the committed
    plan by the trusted campaign (never model prose).
    """
    payload = "|".join(
        (
            task_statuses,
            verification_outcome,
            audit_outcome,
            ",".join(sorted(covered_objectives)),
        )
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def audit_next_phase(
    *,
    outcome: str,
    verification_outcome: str,
    plan_complete: bool,
    objectives_covered: bool,
    no_progress: bool,
    max_rounds_reached: bool,
    max_checkpoints_reached: bool,
    re_plan_needed: bool,
) -> Tuple[str, str]:
    """Resolve one audit outcome to the next phase and terminal reason.

    Returns ``(next_phase, terminal_reason)`` where ``terminal_reason`` is
    empty for a non-terminal next phase.  The resolution is a pure function
    of the trusted inputs:

    * ``findings`` — re-plan to incorporate the findings; at the maximum
      round budget the campaign terminates ``findings``;
    * ``blocked`` — re-plan to keep the blockers explicit; at the maximum
      round budget the campaign terminates ``blocked``;
    * ``pass`` with a ``software_verified_external_acceptance_blocked``
      verification — software is fully verified while external release
      acceptance remains blocked; the campaign terminates ``blocked`` with
      that reason (never success);
    * ``pass`` with a complete plan, every mandatory objective covered, and
      a passing verification — verified completion; the campaign terminates
      ``success`` (early, before the maximum round budget);
    * ``pass`` with a reproduced no-progress fingerprint — the campaign
      terminates ``no_progress``;
    * ``pass`` at the maximum round/checkpoint budget without verified
      completion — the campaign terminates ``budget_exhausted``;
    * ``pass`` with re-planning needed (verification findings/blocked, or no
      runnable task with an incomplete plan) — the campaign re-plans;
    * ``pass`` otherwise — the campaign continues implementation without
      running the planner merely because a round starts.
    """
    if outcome not in AUDIT_OUTCOMES:
        raise SchedulerError(
            f"unknown audit outcome {outcome!r}; expected one of "
            f"{', '.join(AUDIT_OUTCOMES)}"
        )
    if outcome == "findings":
        if max_rounds_reached:
            return "findings", "findings"
        return "planning", ""
    if outcome == "blocked":
        if max_rounds_reached:
            return "blocked", "blocked"
        return "planning", ""
    # outcome == "pass"
    if verification_outcome == "software_verified_external_acceptance_blocked":
        return "blocked", "software_verified_external_acceptance_blocked"
    if plan_complete and objectives_covered and verification_outcome == "pass":
        return "success", "success"
    if no_progress:
        return "no_progress", "no_progress"
    if max_rounds_reached or max_checkpoints_reached:
        return "budget_exhausted", "budget_exhausted"
    if re_plan_needed:
        return "planning", ""
    return "implementation", ""


def _cli(argv: Sequence[str]) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        prog="factory-scheduler",
        description=(
            "Adaptive campaign/audit scheduler authority (budget validation, "
            "milestone decisions, objective coverage, terminal reasons)."
        ),
    )
    parser.add_argument(
        "--root",
        metavar="ROOT",
        default=str(Path(__file__).resolve().parent.parent.parent),
        help="canonical repository root (default: this repository)",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    p_show = sub.add_parser("show", help="print the effective campaign budget")
    p_show.add_argument("--json", action="store_true", help="print JSON")
    p_validate = sub.add_parser("validate", help="validate the committed budget")
    args = parser.parse_args(argv)
    try:
        budget = load_budget_config(Path(args.root))
    except SchedulerError as exc:
        print(f"factory-scheduler: {exc}", file=sys.stderr)
        return 2
    if args.command == "validate":
        print(
            f"budget max_rounds={budget.max_rounds} "
            f"max_checkpoints={budget.max_checkpoints} "
            f"audit_interval={budget.audit_interval} "
            f"no_progress_limit={budget.no_progress_limit}"
        )
        return 0
    if args.json:
        print(json.dumps({
            "schema": SCHEMA_NAME,
            "max_rounds": budget.max_rounds,
            "max_checkpoints": budget.max_checkpoints,
            "max_wall_seconds": budget.max_wall_seconds,
            "max_task_attempts": budget.max_task_attempts,
            "audit_interval": budget.audit_interval,
            "security_sensitive_paths": list(budget.security_sensitive_paths),
            "mandatory_audit_objectives": list(budget.mandatory_audit_objectives),
            "no_progress_limit": budget.no_progress_limit,
        }, sort_keys=True, separators=(",", ":")))
        return 0
    print(
        f"max_rounds={budget.max_rounds} max_checkpoints={budget.max_checkpoints} "
        f"max_wall_seconds={budget.max_wall_seconds:g} "
        f"max_task_attempts={budget.max_task_attempts} "
        f"audit_interval={budget.audit_interval} "
        f"security_sensitive_paths={','.join(budget.security_sensitive_paths) or '-'} "
        f"mandatory_audit_objectives={','.join(budget.mandatory_audit_objectives) or '-'} "
        f"no_progress_limit={budget.no_progress_limit}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(_cli(sys.argv[1:]))
