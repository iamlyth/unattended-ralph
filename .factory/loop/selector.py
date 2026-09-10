"""Deterministic task selector for the Ralph factory.

Picks exactly one task per implementation attempt per spec §6.
The model never chooses among tasks.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class SelectionResult:
    status: str  # "selected", "work_exhausted", "blocked"
    task_id: int | None = None
    reason: str | None = None


def select(tasks: list, available_capabilities: set[str] | None = None) -> SelectionResult:
    """
    Deterministically select one task from the plan.

    Selection rules (in priority order):
    1. Task must be pending or in_progress
    2. All dependencies must be completed
    3. If task requires a runner capability, it must be available
    4. Among eligible tasks, select the lowest-numbered one
    5. If no task is eligible, return work_exhausted or blocked
    """
    if available_capabilities is None:
        available_capabilities = set()

    # Build a lookup for status checks
    task_map = {t.id: t for t in tasks}

    # Check for any pending/in_progress tasks at all
    unfinished = [t for t in tasks if t.status in ("pending", "in_progress")]
    if not unfinished:
        return SelectionResult(
            status="work_exhausted",
            reason="No pending or in_progress tasks remain",
        )

    # Find eligible tasks
    eligible: list = []
    blocked_reasons: list[str] = []

    for task in sorted(unfinished, key=lambda t: t.id):
        # Check dependencies
        deps_met = True
        for dep_id in task.dependencies:
            dep = task_map.get(dep_id)
            if dep is None or dep.status != "completed":
                deps_met = False
                blocked_reasons.append(
                    f"Task {task.id} blocked: dependency {dep_id} not completed"
                )
                break

        if not deps_met:
            continue

        # Check runner capability
        if task.runner:
            if task.runner not in available_capabilities:
                blocked_reasons.append(
                    f"Task {task.id} blocked: runner capability '{task.runner}' unavailable"
                )
                continue

        eligible.append(task)

    if not eligible:
        # There are unfinished tasks but none can proceed
        reason = "; ".join(blocked_reasons[:3]) if blocked_reasons else "All unfinished tasks blocked"
        return SelectionResult(status="blocked", reason=reason)

    # Select lowest-numbered eligible task
    selected = min(eligible, key=lambda t: t.id)
    return SelectionResult(status="selected", task_id=selected.id)