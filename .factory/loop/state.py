"""Minimal control state for the Ralph factory.

One JSON file: .factory-state/factory-loop.json
Orchestrator-only; roles never see this file.
Per spec §10.
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from pathlib import Path
import json
import os
import tempfile

SCHEMA = "factory-state/v1"
VALID_PHASES = {"planning", "implementation", "verification", "audit", "repair", "terminal"}
VALID_OUTCOMES = {
    "success", "findings", "blocked", "failed",
    "interrupted", "infrastructure_failure", None,
}


@dataclass
class State:
    schema: str = SCHEMA
    campaign_id: str = ""
    current_round: int = 1
    current_phase: str = "planning"
    selected_task_id: int | None = None
    attempt_number: int = 1
    repair_count: int = 0
    last_outcome: str | None = None
    terminal_outcome: str | None = None
    rounds_completed: int = 0
    phase_history: list[dict] = field(default_factory=list)


def load(state_path: str | Path) -> State:
    """Load state from JSON file. Returns default State if file doesn't exist."""
    path = Path(state_path)
    if not path.exists():
        return State()
    data = json.loads(path.read_text(encoding="utf-8"))
    # Construct State from dict, ignoring unknown keys for forward-compat
    known = {f for f in State.__dataclass_fields__}
    filtered = {k: v for k, v in data.items() if k in known}
    return State(**filtered)


def save(state_path: str | Path, state: State) -> None:
    """Save state to JSON file atomically (write temp + rename)."""
    path = Path(state_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = asdict(state)
    # Use atomic write: temp file in same directory, then rename
    fd, tmp = tempfile.mkstemp(
        dir=str(path.parent), prefix=".factory-state-", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, sort_keys=False)
            f.write("\n")
        os.replace(tmp, str(path))
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def validate(state: State) -> None:
    """Validate state invariants. Raises ValueError on violation."""
    if state.schema != SCHEMA:
        raise ValueError(f"Invalid schema: {state.schema!r} (expected {SCHEMA!r})")

    if state.current_phase not in VALID_PHASES:
        raise ValueError(
            f"Invalid phase: {state.current_phase!r} "
            f"(valid: {VALID_PHASES})"
        )

    if state.last_outcome not in VALID_OUTCOMES:
        raise ValueError(
            f"Invalid last_outcome: {state.last_outcome!r} "
            f"(valid: {VALID_OUTCOMES})"
        )

    if state.terminal_outcome not in VALID_OUTCOMES:
        raise ValueError(
            f"Invalid terminal_outcome: {state.terminal_outcome!r} "
            f"(valid: {VALID_OUTCOMES})"
        )

    if state.current_round < 1:
        raise ValueError(
            f"current_round must be >= 1, got {state.current_round}"
        )

    if state.attempt_number < 1:
        raise ValueError(
            f"attempt_number must be >= 1, got {state.attempt_number}"
        )

    if state.rounds_completed < 0:
        raise ValueError(
            f"rounds_completed must be >= 0, got {state.rounds_completed}"
        )

    if state.terminal_outcome is not None and state.current_phase != "terminal":
        raise ValueError(
            f"terminal_outcome set but phase is {state.current_phase!r} "
            f"(must be 'terminal')"
        )