#!/usr/bin/env python3
"""Deterministic plan-derived task selection for the trusted control plane.

This module implements the selection boundary of ``docs/FACTORY-LOOP-SPEC.md``
\uffa78 (requirement TASK-01) as a pure, stdlib-only function of an already
parsed ``factory-plan/v1`` plan model.  The trusted control plane (and no
model role) calls :func:`select_task` once per implementation attempt; the
model never chooses among multiple tasks.

Selection order, exactly as specified:

1. reject an invalid, stale, or ambiguously parsed plan.  Invalid plans are
   rejected by the committed parser (``plan_parser.py``); this module rejects
   a *stale* plan (the optional bound base commit differs from the front
   matter ``base_commit``) and an *ambiguous* plan (an ``in_progress`` task
   whose dependencies are not all ``complete``, which no valid lifecycle can
   have reached);
2. resume the sole ``in_progress`` task, if present;
3. otherwise consider ``pending`` tasks whose dependencies are all
   ``complete``;
4. sort by explicit numeric priority, then lexicographic task identifier
   (the decimal rendering of the task number, so ``"10" < "2"``);
5. select exactly one task;
6. if none are runnable, classify the implementation phase as
   ``work_exhausted`` when no ``pending`` or ``in_progress`` task remains, or
   ``blocked`` when unfinished tasks remain but none can run (every such task
   is blocked directly, or transitively through a blocked dependency).

Determinism and purity: selection performs no I/O at all.  It never reads a
runtime task ledger, the control-state file, the environment, or process
state; the outcome is a deterministic function of the plan model (plus the
optional bound base commit).  ``parse -> select`` therefore always reproduces
the same result for identical plan bytes, which is why the hidden harness
suite can assert the exact selection order, tie-breaks, single-task
guarantee, and empty-work classifications from fixture plans.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional

try:
    from .plan_parser import TASK_STATUSES, Plan, PlanError
except ImportError:  # flat-import mode used by the hidden harness test suite
    from plan_parser import TASK_STATUSES, Plan, PlanError  # type: ignore[no-redef]

# Valid selector outcomes: exactly one selected task, or one of the two
# empty-work phase classes (FACTORY-LOOP-SPEC \u00a78, \u00a713.2).
CLASSIFICATIONS = ("selected", "work_exhausted", "blocked")


class SelectorError(Exception):
    """Raised when the selector must reject a stale or ambiguous plan.

    Invalid plans are rejected earlier by ``PlanError`` at the parse
    boundary; ``SelectorError`` is reserved for failures the selection
    boundary itself owns: a stale front-matter base binding and an
    internally inconsistent ``in_progress`` task.
    """


@dataclass(frozen=True)
class Selection:
    """Deterministic outcome of one selection pass.

    ``classification`` is ``selected`` (exactly one ``task_id``), or the
    empty-work phase class ``work_exhausted`` / ``blocked`` with ``task_id``
    ``None``.  The dataclass is frozen and its fields are a pure function of
    the plan model, so equality and hashing are deterministic.
    """

    classification: str
    task_id: Optional[int] = None

    def __post_init__(self) -> None:
        if self.classification not in CLASSIFICATIONS:
            raise SelectorError(
                f"invalid selection classification {self.classification!r}"
            )
        if self.classification == "selected":
            if self.task_id is None:
                raise SelectorError("a selected task requires a task id")
        elif self.task_id is not None:
            # A blocked or work-exhausted phase never carries a task id; the
            # field is reserved for the single selected task.
            raise SelectorError(
                f"a `{self.classification}` classification must not carry a task id"
            )

    @property
    def selected(self) -> bool:
        """True when exactly one task was selected."""
        return self.classification == "selected"


def _validate_model(plan: Plan) -> None:
    """Defense in depth on the parsed plan model at the selection boundary.

    The parser already guarantees these invariants for parsed input; this
    pass keeps the selector independently safe for any caller-built ``Plan``
    and is what lets the selector *reject an ambiguously parsed plan* rather
    than silently mis-selecting.  It performs no I/O.

    It independently re-checks the full structural contract a valid
    ``factory-plan/v1`` model satisfies: a non-empty, uniquely numbered task
    list; every task status in the lifecycle set; dependencies that reference
    existing tasks only, never the task itself, and never in a cycle; a
    ``blocked`` task that names an exact unresolved reference; at most one
    ``in_progress`` task; and an ``in_progress`` task whose dependencies are
    all ``complete``.
    """
    if not plan.tasks:
        raise SelectorError("invalid plan: plan has no tasks")

    numbers: set = set()
    for task in plan.tasks:
        if task.number in numbers:
            raise SelectorError(f"invalid plan: duplicate task id {task.number}")
        numbers.add(task.number)
        if task.status not in TASK_STATUSES:
            raise SelectorError(
                f"invalid plan: task {task.number} has unknown status "
                f"{task.status!r}"
            )

    statuses = {task.number: task.status for task in plan.tasks}
    for task in plan.tasks:
        if task.number in task.dependencies:
            raise SelectorError(
                f"invalid plan: task {task.number} cannot depend on itself"
            )
        unknown = sorted(set(task.dependencies) - numbers)
        if unknown:
            raise SelectorError(
                f"invalid plan: task {task.number} references unknown "
                f"dependencies {unknown}"
            )
        if task.status == "blocked" and not task.blocked_on:
            raise SelectorError(
                f"invalid plan: blocked task {task.number} must name an exact "
                "unresolved reference"
            )

    # A valid plan's dependency graph is acyclic; reject any cycle a
    # caller-built model could smuggle in so selection never mis-classifies a
    # structurally invalid graph.
    visiting: set = set()
    visited: set = set()

    def visit(number: int) -> None:
        if number in visiting:
            raise SelectorError(
                "invalid plan: task dependency graph contains a cycle"
            )
        if number in visited:
            return
        visiting.add(number)
        task = next(task for task in plan.tasks if task.number == number)
        for dependency in task.dependencies:
            visit(dependency)
        visiting.discard(number)
        visited.add(number)

    for number in numbers:
        visit(number)

    in_progress: List[int] = [
        task.number for task in plan.tasks if task.status == "in_progress"
    ]
    if len(in_progress) > 1:
        raise SelectorError(
            "ambiguous plan: more than one task is `in_progress`: "
            f"{in_progress}"
        )
    # A lifecycle may never reach `in_progress` before every dependency is
    # `complete` (`pending -> in_progress` only after selection, which
    # requires complete dependencies).  An `in_progress` task with an
    # unsatisfied dependency is an ambiguous plan and fails closed here.
    for task in plan.tasks:
        if task.status == "in_progress":
            incomplete = sorted(
                dep for dep in task.dependencies if statuses[dep] != "complete"
            )
            if incomplete:
                raise SelectorError(
                    "ambiguous plan: in_progress task "
                    f"{task.number} has dependencies that are not complete: "
                    f"{incomplete}"
                )


def select_task(plan: Plan, *, bound_base_commit: Optional[str] = None) -> Selection:
    """Select exactly one plan task, or classify the empty-work phase.

    Pure function of ``plan`` (and, optionally, the bound ``base_commit`` of
    the committed plan blob as the ``state`` half of the contract): it reads
    no file, ledger, environment, or process state.

    Selection follows the FACTORY-LOOP-SPEC \u00a78 order exactly: reject a
    stale or ambiguous plan, resume the sole ``in_progress`` task, otherwise
    sort runnable ``pending`` tasks (dependencies complete) by explicit
    numeric priority then lexicographic task identifier, select exactly one,
    and classify empty work as ``work_exhausted`` or ``blocked``.
    """
    if bound_base_commit is not None and plan.base_commit != bound_base_commit:
        raise SelectorError(
            f"stale plan: base_commit {plan.base_commit} does not match the "
            f"bound base commit {bound_base_commit}"
        )
    _validate_model(plan)

    in_progress = [task for task in plan.tasks if task.status == "in_progress"]
    if in_progress:
        return Selection("selected", in_progress[0].number)

    statuses = {task.number: task.status for task in plan.tasks}
    runnable = [
        task
        for task in plan.tasks
        if task.status == "pending"
        and all(statuses[dep] == "complete" for dep in task.dependencies)
    ]
    if runnable:
        chosen = min(runnable, key=lambda task: (task.priority, str(task.number)))
        return Selection("selected", chosen.number)

    unfinished = [task for task in plan.tasks if task.status != "complete"]
    if unfinished:
        return Selection("blocked")
    return Selection("work_exhausted")


def main(argv: Optional[List[str]] = None) -> int:
    """Trusted control-plane entry point (never invoked by a model role)."""
    import argparse
    import sys
    from pathlib import Path

    parser = argparse.ArgumentParser(
        prog="factory-selector",
        description=(
            "Deterministic plan-derived task selection (FACTORY-LOOP-SPEC "
            "§8). Selects exactly one task or reports the empty-work phase "
            "class."
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_select = sub.add_parser(
        "select",
        help="parse the plan and print the deterministic selection",
    )
    p_select.add_argument("path", metavar="PLAN")
    p_select.add_argument(
        "--bound-base-commit",
        metavar="SHA",
        default=None,
        help="expected plan base_commit; a mismatch is rejected as stale",
    )

    # Only the `select` subcommand exists and subparsers are `required=True`,
    # so argparse guarantees `args.command == "select"` after parse_args; the
    # dedicated ``--bound-base-commit`` binding is the only other option.
    args = parser.parse_args(argv)
    assert args.command == "select"
    try:
        plan = Plan.from_file(Path(args.path))
    except PlanError as exc:
        print(f"factory-selector: {exc}", file=sys.stderr)
        return 1
    try:
        selection = select_task(
            plan, bound_base_commit=args.bound_base_commit
        )
    except SelectorError as exc:
        print(f"factory-selector: {exc}", file=sys.stderr)
        return 1
    if selection.selected:
        print(f"selected={selection.task_id}")
    else:
        print(f"classification={selection.classification}")
    return 0


if __name__ == "__main__":
    import sys

    sys.exit(main())
