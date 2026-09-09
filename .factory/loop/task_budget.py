#!/usr/bin/env python3
"""Versioned strict generic task-resource-budget authority (Phase 2A).

The trusted control plane bounds one implementation task/session by
cumulative wall time, cumulative process CPU time, combined captured output
bytes, and live/descendant process count across every fresh implementation
attempt of the selected task, and keeps a per-command timeout as
defense-in-depth.  The budget replaces the prompt-only three-command
debugging cap: focused inspect/edit/test/fix cycles may continue while the
resource budgets remain.

Trust and data boundaries:

* **Versioned strict schema.**  The committed schema
  ``factory-task-budget/v1`` (``.factory/schemas/factory-task-budget-v1.schema.json``)
  is the contract: a closed field set (``additionalProperties: false``),
  bounded integers, and a closed ``schema`` enum.  The validator re-checks
  every bound in code, so a schema that drifts can never silently widen the
  boundary.
* **Duplicate-key rejection.**  Parsing uses an ``object_pairs_hook`` that
  rejects any repeated JSON object key, so a forged budget cannot hide a
  drifted limit behind a duplicate.
* **Explicit exhaustion reasons.**  Exhaustion is recorded with a closed
  enum reason (``wall_time``, ``cpu_time``, ``output_bytes``,
  ``live_processes``, ``per_command_timeout``, ``accounting_untrusted``);
  an unknown reason fails closed and exhaustion can never produce
  acceptance.
* **Cumulative ledger.**  The campaign keeps one per-task ledger
  (``factory-task-budget-ledger/v1``) under the ignored ``.factory-state/``
  namespace recording the cumulative wall time, CPU time, output bytes, and
  peak live process count consumed by the task's attempts.  The ledger is
  runtime state, never committed, never a task/memory authority.
* **Fail-closed accounting.**  Where the platform supports trusted process
  CPU accounting the launch broker counts it; if accounting cannot be
  trusted in production the campaign fails closed (``accounting_untrusted``)
  instead of silently running unbounded.
"""

from __future__ import annotations

import json
import os
import re
import stat
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Mapping, Optional

SCHEMA_NAME = "factory-task-budget/v1"
LEDGER_SCHEMA_NAME = "factory-task-budget-ledger/v1"
SCHEMA_FILE = "factory-task-budget-v1.schema.json"
MAX_BUDGET_BYTES = 64 * 1024
MAX_LEDGER_BYTES = 64 * 1024

# Closed exhaustion-reason enum (Phase 2A): an unknown reason fails closed.
EXHAUSTION_REASONS = (
    "wall_time",
    "cpu_time",
    "output_bytes",
    "live_processes",
    "per_command_timeout",
    "accounting_untrusted",
)

# Bounded-integer ceilings (Phase 2A): enforced by the validator in addition
# to the committed JSON schema.
MAX_WALL_TIME_SECONDS = 86400
MAX_CPU_TIME_SECONDS = 86400
MAX_OUTPUT_BYTES = 2**31 - 1
MAX_LIVE_PROCESSES = 1024
MAX_PER_COMMAND_TIMEOUT_SECONDS = 3600

SAFE_CAMPAIGN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


class TaskBudgetError(Exception):
    """A task-resource budget was forged, unsafe, or malformed."""


class TaskBudgetMalformedError(TaskBudgetError):
    """The budget violates the committed schema or a bounded-size rule."""


def _reject_duplicate_keys(pairs: List[tuple]) -> Dict[str, object]:
    """JSON object-pairs hook: reject any repeated object key."""
    result: Dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise TaskBudgetMalformedError(
                f"duplicate JSON object key: {key!r}"
            )
        result[key] = value
    return result


def _load_schema() -> Dict[str, object]:
    """Read the committed schema with a no-follow, identity-safe open."""
    here = Path(__file__).resolve().parents[1]  # .factory/
    path = here / "schemas" / SCHEMA_FILE
    try:
        descriptor = os.open(
            path,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0),
        )
    except OSError as exc:
        raise TaskBudgetError(
            f"cannot open the committed schema {path}: {exc}"
        ) from exc
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise TaskBudgetError(
                f"the committed schema {path} is not a regular file"
            )
        if info.st_uid != os.getuid() or info.st_mode & 0o022:
            raise TaskBudgetError(
                f"the committed schema {path} is not owned/private; the "
                "schema authority fails closed"
            )
        if info.st_size > 256 * 1024:
            raise TaskBudgetError(f"the committed schema {path} is oversized")
        data = os.read(descriptor, info.st_size + 1)
        if len(data) > 256 * 1024:
            raise TaskBudgetError(f"the committed schema {path} is oversized")
    finally:
        os.close(descriptor)
    try:
        schema = json.loads(data)
    except ValueError as exc:
        raise TaskBudgetError(f"the committed schema {path} is not JSON") from exc
    if not isinstance(schema, dict):
        raise TaskBudgetError(f"the committed schema {path} is not an object")
    return schema


_SCHEMA: Optional[Dict[str, object]] = None


def _schema() -> Dict[str, object]:
    global _SCHEMA
    if _SCHEMA is None:
        _SCHEMA = _load_schema()
    return _SCHEMA


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
    raise TaskBudgetMalformedError(
        f"value of type {type(value).__name__} is not JSON-serializable"
    )


def _check_instance(
    instance: object, schema: object, path: str, context: str
) -> None:
    """Validate ``instance`` against the JSON-Schema subset the committed
    schemas use (type/enum/minimum/maximum/properties/required/
    additionalProperties).  Any mismatch fails closed."""
    if not isinstance(schema, dict):
        return
    expected = schema.get("type")
    if expected is not None:
        types = expected if isinstance(expected, list) else [expected]
        if _json_type(instance) not in types:
            raise TaskBudgetMalformedError(
                f"{context} violation at {path or '(root)'}: expected "
                f"{expected!r}, got {_json_type(instance)!r}"
            )
    if "enum" in schema and instance not in schema["enum"]:
        raise TaskBudgetMalformedError(
            f"{context} violation at {path or '(root)'}: value {instance!r} "
            f"is not one of {schema['enum']!r}"
        )
    if isinstance(instance, (int, float)) and not isinstance(instance, bool):
        if "minimum" in schema and instance < schema["minimum"]:
            raise TaskBudgetMalformedError(
                f"{context} violation at {path or '(root)'}: value below minimum"
            )
        if "maximum" in schema and instance > schema["maximum"]:
            raise TaskBudgetMalformedError(
                f"{context} violation at {path or '(root)'}: value above maximum"
            )
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
                    raise TaskBudgetMalformedError(
                        f"{context} violation at {path or '(root)'}: missing "
                        f"required field {key!r}"
                    )
        if schema.get("additionalProperties") is False:
            declared = set((schema.get("properties") or {}).keys())
            for key in instance:
                if key not in declared:
                    raise TaskBudgetMalformedError(
                        f"{context} violation at {path or '(root)'}: extra "
                        f"field {key!r}"
                    )


def validate_budget(budget: Mapping[str, object]) -> None:
    """Validate one task-budget document against the committed schema.

    Enforces the exact closed field set, the closed ``schema`` enum, and the
    bounded integers; any mismatch fails closed.
    """
    if not isinstance(budget, dict):
        raise TaskBudgetMalformedError(
            "task-budget must be a JSON object"
        )
    _check_instance(budget, _schema(), "", "task-budget")
    # Semantic re-checks beyond the JSON schema (defense in depth): the
    # bounded-integer ceilings are enforced here too, so a schema that
    # drifts can never silently widen the boundary.
    for name, ceiling in (
        ("wall_time_seconds", MAX_WALL_TIME_SECONDS),
        ("cpu_time_seconds", MAX_CPU_TIME_SECONDS),
        ("output_bytes", MAX_OUTPUT_BYTES),
        ("max_live_processes", MAX_LIVE_PROCESSES),
        ("per_command_timeout_seconds", MAX_PER_COMMAND_TIMEOUT_SECONDS),
    ):
        value = budget.get(name)
        if (
            isinstance(value, bool)
            or not isinstance(value, int)
            or value < 1
            or value > ceiling
        ):
            raise TaskBudgetMalformedError(
                f"task-budget {name} must be an integer in [1, {ceiling}]"
            )


def budget_bytes(budget: Mapping[str, object]) -> bytes:
    """The deterministic canonical budget bytes (sorted JSON, no newline)."""
    validate_budget(budget)
    return json.dumps(
        dict(budget), sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")


def parse_budget(raw: bytes) -> Dict[str, object]:
    """Parse raw budget bytes with duplicate-key rejection and validate.

    A repeated JSON object key, an oversized document, non-UTF-8 bytes, or
    any schema/bound violation fails closed (Phase 2A).
    """
    if not isinstance(raw, bytes) or len(raw) > MAX_BUDGET_BYTES:
        raise TaskBudgetMalformedError(
            "task-budget document is oversized or not bytes"
        )
    try:
        text = raw.decode("utf-8")
    except UnicodeError as exc:
        raise TaskBudgetMalformedError(
            "task-budget document is not UTF-8"
        ) from exc
    try:
        data = json.loads(text, object_pairs_hook=_reject_duplicate_keys)
    except json.JSONDecodeError as exc:
        raise TaskBudgetMalformedError(
            f"task-budget document is not valid JSON: {exc}"
        ) from exc
    validate_budget(data)
    return data


# ---------------------------------------------------------------------------
# Cumulative per-task ledger
# ---------------------------------------------------------------------------


@dataclass
class BudgetLedger:
    """Cumulative resource usage of one selected task across its attempts.

    ``wall_time_seconds``/``cpu_time_seconds`` are cumulative floats,
    ``output_bytes`` is the cumulative combined captured byte count, and
    ``max_live_processes`` is the peak live/descendant process count
    observed.  ``exhausted_reason`` is the closed-enum reason of the first
    exhausted budget, or ``None`` while the task still has budget.  The
    ledger is runtime state under the ignored ``.factory-state/`` namespace,
    never committed and never a task/memory authority.
    """

    campaign_id: str
    task_id: int
    wall_time_seconds: float = 0.0
    cpu_time_seconds: float = 0.0
    output_bytes: int = 0
    max_live_processes: int = 0
    exhausted_reason: Optional[str] = None

    def to_dict(self) -> Dict[str, object]:
        return {
            "schema": LEDGER_SCHEMA_NAME,
            "campaign_id": self.campaign_id,
            "task_id": self.task_id,
            "wall_time_seconds": round(self.wall_time_seconds, 6),
            "cpu_time_seconds": round(self.cpu_time_seconds, 6),
            "output_bytes": self.output_bytes,
            "max_live_processes": self.max_live_processes,
            "exhausted_reason": self.exhausted_reason,
        }

    def record_attempt(
        self,
        *,
        wall_time_seconds: float,
        cpu_time_seconds: float,
        output_bytes: int,
        max_live_processes: int,
    ) -> None:
        """Accumulate one attempt's usage into the ledger (monotonic)."""
        for name, value in (
            ("wall_time_seconds", wall_time_seconds),
            ("cpu_time_seconds", cpu_time_seconds),
        ):
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or value < 0
            ):
                raise TaskBudgetError(
                    f"attempt {name} must be a non-negative number"
                )
        if (
            isinstance(output_bytes, bool)
            or not isinstance(output_bytes, int)
            or output_bytes < 0
        ):
            raise TaskBudgetError(
                "attempt output_bytes must be a non-negative integer"
            )
        if (
            isinstance(max_live_processes, bool)
            or not isinstance(max_live_processes, int)
            or max_live_processes < 0
        ):
            raise TaskBudgetError(
                "attempt max_live_processes must be a non-negative integer"
            )
        self.wall_time_seconds += float(wall_time_seconds)
        self.cpu_time_seconds += float(cpu_time_seconds)
        self.output_bytes += output_bytes
        self.max_live_processes = max(
            self.max_live_processes, max_live_processes
        )

    def mark_exhausted(self, reason: str) -> None:
        """Record the closed-enum exhaustion reason (first reason wins)."""
        if reason not in EXHAUSTION_REASONS:
            raise TaskBudgetError(
                f"exhaustion reason {reason!r} is not a closed enum value"
            )
        if self.exhausted_reason is None:
            self.exhausted_reason = reason


def exhausted_reason(
    ledger: BudgetLedger, budget: Mapping[str, object]
) -> Optional[str]:
    """The first exhausted budget reason, or ``None`` while budget remains.

    The closed-enum reasons are checked in a fixed precedence order
    (wall time, CPU time, output bytes, live processes); the per-command
    timeout is a defense-in-depth floor enforced by the launch broker and
    is reported by the broker, never derived here.
    """
    if ledger.exhausted_reason is not None:
        return ledger.exhausted_reason
    if ledger.wall_time_seconds >= float(budget["wall_time_seconds"]):
        return "wall_time"
    if ledger.cpu_time_seconds >= float(budget["cpu_time_seconds"]):
        return "cpu_time"
    if ledger.output_bytes >= int(budget["output_bytes"]):
        return "output_bytes"
    if ledger.max_live_processes >= int(budget["max_live_processes"]):
        return "live_processes"
    return None


def remaining_limits(
    ledger: BudgetLedger, budget: Mapping[str, object]
) -> Dict[str, float]:
    """The remaining per-attempt limits derived from the cumulative budget.

    Returns ``wall_time_seconds``, ``cpu_time_seconds``, ``output_bytes``,
    and ``max_live_processes`` remaining for the next attempt.  A consumed
    budget yields a zero remaining limit, which the launch broker treats as
    an immediate exhaustion (never an unbounded run).
    """
    return {
        "wall_time_seconds": max(
            0.0, float(budget["wall_time_seconds"]) - ledger.wall_time_seconds
        ),
        "cpu_time_seconds": max(
            0.0, float(budget["cpu_time_seconds"]) - ledger.cpu_time_seconds
        ),
        "output_bytes": max(
            0, int(budget["output_bytes"]) - ledger.output_bytes
        ),
        "max_live_processes": max(
            0, int(budget["max_live_processes"])
        ),
    }


def ledger_bytes(ledger: BudgetLedger) -> bytes:
    """The deterministic canonical ledger bytes (sorted JSON, no newline)."""
    return json.dumps(
        ledger.to_dict(), sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")


def parse_ledger(raw: bytes) -> BudgetLedger:
    """Parse raw ledger bytes with duplicate-key rejection and validate."""
    if not isinstance(raw, bytes) or len(raw) > MAX_LEDGER_BYTES:
        raise TaskBudgetMalformedError(
            "task-budget ledger is oversized or not bytes"
        )
    try:
        text = raw.decode("utf-8")
    except UnicodeError as exc:
        raise TaskBudgetMalformedError(
            "task-budget ledger is not UTF-8"
        ) from exc
    try:
        data = json.loads(text, object_pairs_hook=_reject_duplicate_keys)
    except json.JSONDecodeError as exc:
        raise TaskBudgetMalformedError(
            f"task-budget ledger is not valid JSON: {exc}"
        ) from exc
    if not isinstance(data, dict):
        raise TaskBudgetMalformedError("task-budget ledger must be an object")
    if data.get("schema") != LEDGER_SCHEMA_NAME:
        raise TaskBudgetMalformedError(
            "task-budget ledger has the wrong schema"
        )
    campaign_id = data.get("campaign_id")
    if not isinstance(campaign_id, str) or not SAFE_CAMPAIGN_ID_RE.fullmatch(
        campaign_id
    ):
        raise TaskBudgetMalformedError(
            "task-budget ledger campaign_id is invalid"
        )
    task_id = data.get("task_id")
    if (
        isinstance(task_id, bool)
        or not isinstance(task_id, int)
        or task_id < 1
    ):
        raise TaskBudgetMalformedError(
            "task-budget ledger task_id must be a positive integer"
        )
    for name in ("wall_time_seconds", "cpu_time_seconds"):
        value = data.get(name)
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or value < 0
        ):
            raise TaskBudgetMalformedError(
                f"task-budget ledger {name} must be a non-negative number"
            )
    for name in ("output_bytes", "max_live_processes"):
        value = data.get(name)
        if (
            isinstance(value, bool)
            or not isinstance(value, int)
            or value < 0
        ):
            raise TaskBudgetMalformedError(
                f"task-budget ledger {name} must be a non-negative integer"
            )
    reason = data.get("exhausted_reason")
    if reason is not None and reason not in EXHAUSTION_REASONS:
        raise TaskBudgetMalformedError(
            "task-budget ledger exhausted_reason is not a closed enum value"
        )
    return BudgetLedger(
        campaign_id=campaign_id,
        task_id=task_id,
        wall_time_seconds=float(data["wall_time_seconds"]),
        cpu_time_seconds=float(data["cpu_time_seconds"]),
        output_bytes=int(data["output_bytes"]),
        max_live_processes=int(data["max_live_processes"]),
        exhausted_reason=reason,
    )


def ledger_name(campaign_id: str, task_id: int) -> str:
    """The canonical per-task ledger artifact name under ``.factory-state/``."""
    if not SAFE_CAMPAIGN_ID_RE.fullmatch(campaign_id):
        raise TaskBudgetError("ledger campaign_id is invalid")
    if isinstance(task_id, bool) or not isinstance(task_id, int) or task_id < 1:
        raise TaskBudgetError("ledger task_id must be a positive integer")
    return f"task-budget-{campaign_id}-task-{task_id}.json"


def load_ledger(root: Path, campaign_id: str, task_id: int) -> BudgetLedger:
    """Load the per-task ledger, or a fresh zero ledger when none exists.

    A forged, malformed, or foreign ledger fails closed (never silently
    reset), so a tampered cumulative budget can never be hidden.
    """
    name = ledger_name(campaign_id, task_id)
    try:
        from . import factory_state_io as _io
    except ImportError:
        import factory_state_io as _io  # type: ignore[no-redef]
    try:
        raw = _io.read_bytes(
            root, name, maximum=MAX_LEDGER_BYTES, missing_ok=True
        )
    except _io.StateIOError as exc:
        raise TaskBudgetError(
            f"cannot safely read the task-budget ledger {name}: {exc}"
        ) from exc
    if raw is None:
        return BudgetLedger(campaign_id=campaign_id, task_id=task_id)
    try:
        return parse_ledger(raw)
    except TaskBudgetMalformedError as exc:
        raise TaskBudgetError(
            f"the task-budget ledger {name} is malformed: {exc}"
        ) from exc


def save_ledger(root: Path, ledger: BudgetLedger) -> None:
    """Atomically publish the per-task ledger (no-replace, byte-idempotent).

    A byte-exact re-publication across a crash window is accepted; any other
    pre-existing content fails closed, so a raced or forged ledger can never
    be silently replaced.
    """
    name = ledger_name(ledger.campaign_id, ledger.task_id)
    raw = ledger_bytes(ledger)
    try:
        from . import factory_state_io as _io
    except ImportError:
        import factory_state_io as _io  # type: ignore[no-redef]
    try:
        _io.atomic_write(root, name, raw, no_replace=True)
    except _io.StateIOError as exc:
        try:
            existing = _io.read_bytes(
                root, name, maximum=MAX_LEDGER_BYTES, missing_ok=False
            )
        except _io.StateIOError as read_exc:
            raise TaskBudgetError(
                f"cannot publish the task-budget ledger {name}: a marker "
                f"already exists and cannot be safely re-read ({read_exc})"
            ) from read_exc
        if existing != raw:
            raise TaskBudgetError(
                f"cannot publish the task-budget ledger {name}: a different "
                "marker already exists; a tampered or foreign ledger fails "
                "closed"
            ) from exc
