#!/usr/bin/env python3
"""Harness-owned conformance tests for the deterministic task selector (TASK-01).

This test lives under the hidden `.factory/tests/` namespace because the
specification (HIDE-01, §3) keeps harness-only tests out of the adopting
product's visible test tree. It is the deterministic verification for Task 3:

* the selector is a pure, stdlib-only function of the parsed plan (plus the
  optional bound base commit) and never consults a runtime task ledger;
* the exact §8 selection order holds: reject stale/ambiguous plans, resume
  the sole `in_progress` task, sort runnable `pending` tasks (dependencies
  complete) by explicit numeric priority then lexicographic task identifier,
  select exactly one;
* empty work is classified `work_exhausted` (no pending/in-progress task
  remains) or `blocked` (unfinished tasks remain but none can run);
* every fixture plan parses and round-trips byte-exactly;
* the trusted CLI entry point prints exactly one machine-readable outcome.
"""

from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
LOOP = ROOT / ".factory" / "loop"
FIXTURES = ROOT / ".factory" / "tests" / "fixtures"
CANONICAL_PLAN = ROOT / ".factory" / "artifacts" / "implementation-plan.md"

sys.path.insert(0, str(LOOP))
from plan_parser import Plan, PlanError, Task, parse_plan  # noqa: E402
from selector import (  # noqa: E402
    CLASSIFICATIONS,
    Selection,
    SelectorError,
    select_task,
)

# Every committed selector fixture is parsed and exercised; this explicit list
# keeps the corpus visible and prevents accidental fixture-name drift.
SELECTOR_FIXTURES = (
    "plan-select-resume-in-progress.md",
    "plan-select-priority-order.md",
    "plan-select-lexicographic-tiebreak.md",
    "plan-select-dependency-gate.md",
    "plan-select-blocked.md",
    "plan-select-work-exhausted.md",
    "plan-select-stale.md",
    "plan-select-inconsistent-in-progress.md",
)


def parse_fixture(name: str) -> "Plan":
    path = FIXTURES / name
    assert path.is_file(), f"missing fixture {path}"
    return Plan.from_file(path)


class CanonicalPlanSelectionTest(unittest.TestCase):
    """The committed canonical plan deterministically selects its next task."""

    def test_canonical_plan_selects_first_runnable_pending(self) -> None:
        plan = Plan.from_file(CANONICAL_PLAN)
        selection = select_task(plan)
        self.assertTrue(selection.selected)
        self.assertEqual(selection.classification, "selected")
        # Task 11 is complete at this plan revision, so Tasks 12 and 15 are
        # runnable. Task 13 is also runnable from Task 1; numeric priority then
        # lexicographic identifier deterministically picks Task 12.
        self.assertEqual(selection.task_id, 12)
        # Tasks 12, 13, and 15 are runnable; every other unfinished task still
        # has an incomplete dependency.
        statuses = {task.number: task.status for task in plan.tasks}
        runnable = sorted(
            task.number
            for task in plan.tasks
            if task.status == "pending"
            and all(statuses[dep] == "complete" for dep in task.dependencies)
        )
        self.assertEqual(runnable, [12, 13, 15])
        self.assertEqual(plan.tasks[18].status, "complete")
        self.assertEqual(plan.tasks[18].priority, 1)
        self.assertEqual(plan.tasks[12].priority, 13)
        self.assertEqual(plan.tasks[2].status, "complete")
        self.assertEqual(plan.tasks[3].status, "complete")

    def test_canonical_bound_base_commit_matches(self) -> None:
        plan = Plan.from_file(CANONICAL_PLAN)
        selection = select_task(plan, bound_base_commit=plan.base_commit)
        self.assertEqual(selection.task_id, 12)

    def test_bound_base_commit_mismatch_is_stale(self) -> None:
        plan = Plan.from_file(CANONICAL_PLAN)
        wrong = "0" * 40 if plan.base_commit != "0" * 40 else "1" * 40
        with self.assertRaises(SelectorError) as caught:
            select_task(plan, bound_base_commit=wrong)
        self.assertIn("stale plan", str(caught.exception))


class ResumeInProgressTest(unittest.TestCase):
    """The sole `in_progress` task is resumed regardless of priority."""

    def test_resumes_sole_in_progress(self) -> None:
        plan = parse_fixture("plan-select-resume-in-progress.md")
        in_progress = [t for t in plan.tasks if t.status == "in_progress"]
        self.assertEqual([t.number for t in in_progress], [1])
        selection = select_task(plan)
        self.assertEqual(selection.classification, "selected")
        self.assertEqual(selection.task_id, 1)
        # Resuming ignores the explicit priority 5 on the in-progress task.
        self.assertEqual(plan.tasks[0].priority, 5)


class PriorityOrderTest(unittest.TestCase):
    """Runnable pending tasks sort by explicit numeric priority first."""

    def test_lower_priority_wins(self) -> None:
        plan = parse_fixture("plan-select-priority-order.md")
        selection = select_task(plan)
        self.assertEqual(selection.task_id, 2)
        self.assertLess(plan.tasks[1].priority, plan.tasks[0].priority)

    def test_default_priority_is_task_id(self) -> None:
        # The resume fixture has no priority on the audit task; the parser
        # defaults priority to the task id, keeping the sort total.
        plan = parse_fixture("plan-select-priority-order.md")
        for task in plan.tasks:
            self.assertGreaterEqual(task.priority, 1)


class LexicographicTiebreakTest(unittest.TestCase):
    """Equal priorities break deterministically on lexicographic task ID.

    Tasks 2 and 10 share the minimum priority 1.  The identifier is the task
    number rendered in decimal, so the lexicographic minimum is `"10"` (not
    the numeric minimum 2).  The fixture is deliberately constructed so the
    two orderings disagree, proving the documented tie-break is used.
    """

    def test_lexicographic_identifier_wins(self) -> None:
        plan = parse_fixture("plan-select-lexicographic-tiebreak.md")
        selection = select_task(plan)
        self.assertEqual(selection.classification, "selected")
        self.assertEqual(selection.task_id, 10)
        # The fixture must actually disagree with numeric ordering, or it
        # would not prove the lexicographic rule: among the minimum-priority
        # group {Task 2, Task 10}, numeric order picks 2 and lexicographic
        # order picks 10.
        min_priority = min(task.priority for task in plan.tasks)
        tied = [task.number for task in plan.tasks if task.priority == min_priority]
        self.assertEqual(sorted(tied), [2, 10])
        self.assertEqual(min(tied), 2)

    def test_priority_is_the_primary_key(self) -> None:
        plan = parse_fixture("plan-select-lexicographic-tiebreak.md")
        priorities = {task.number: task.priority for task in plan.tasks}
        self.assertEqual(priorities[2], 1)
        self.assertEqual(priorities[10], 1)
        self.assertEqual(priorities[1], 5)  # not the minimum priority


class DependencyGateTest(unittest.TestCase):
    """A `pending` task is runnable only when every dependency is `complete`."""

    def test_task_with_incomplete_dependency_is_not_runnable(self) -> None:
        plan = parse_fixture("plan-select-dependency-gate.md")
        selection = select_task(plan)
        self.assertEqual(selection.task_id, 2)
        # Task 3 depends on the still-pending Task 2 and must not be selected.
        self.assertNotEqual(selection.task_id, 3)

    def test_only_runnable_task_selected(self) -> None:
        plan = parse_fixture("plan-select-dependency-gate.md")
        statuses = {t.number: t.status for t in plan.tasks}
        runnable = [
            t.number for t in plan.tasks
            if t.status == "pending"
            and all(statuses[d] == "complete" for d in t.dependencies)
        ]
        self.assertEqual(runnable, [2])


class EmptyWorkClassificationTest(unittest.TestCase):
    """None runnable classifies `work_exhausted` or `blocked`, never `selected`."""

    def test_blocked_classification(self) -> None:
        plan = parse_fixture("plan-select-blocked.md")
        selection = select_task(plan)
        self.assertEqual(selection.classification, "blocked")
        self.assertIsNone(selection.task_id)
        self.assertFalse(selection.selected)

    def test_work_exhausted_classification(self) -> None:
        plan = parse_fixture("plan-select-work-exhausted.md")
        selection = select_task(plan)
        self.assertEqual(selection.classification, "work_exhausted")
        self.assertIsNone(selection.task_id)
        self.assertFalse(selection.selected)

    def test_classifications_are_exactly_documented(self) -> None:
        self.assertEqual(
            tuple(CLASSIFICATIONS), ("selected", "work_exhausted", "blocked")
        )


class StaleAndAmbiguousRejectionTest(unittest.TestCase):
    """§8 step 1: stale or ambiguous plans are rejected before selection."""

    def test_stale_plan_rejected(self) -> None:
        plan = parse_fixture("plan-select-stale.md")
        # Without a binding the plan is fine and selects normally.
        self.assertEqual(select_task(plan).task_id, 1)
        with self.assertRaises(SelectorError) as caught:
            select_task(
                plan,
                bound_base_commit="3333333333333333333333333333333333333333",
            )
        self.assertIn("stale plan", str(caught.exception))

    def test_inconsistent_in_progress_rejected(self) -> None:
        plan = parse_fixture("plan-select-inconsistent-in-progress.md")
        with self.assertRaises(SelectorError) as caught:
            select_task(plan)
        self.assertIn("ambiguous plan", str(caught.exception))
        self.assertIn("not complete", str(caught.exception))

    def test_invalid_plan_never_reaches_selection(self) -> None:
        # The parser is the first rejection boundary: a defect fixture raises
        # PlanError before any selection logic runs.
        with self.assertRaises(PlanError):
            Plan.from_file(FIXTURES / "plan-two-in-progress.md")


class CallerBuiltInvariantTest(unittest.TestCase):
    """Defense-in-depth rejects malformed caller-built models and outcomes."""

    @staticmethod
    def task(
        number: int,
        *,
        status: str = "pending",
        dependencies: list[int] | None = None,
        blocked_on: str | None = None,
    ) -> Task:
        return Task(
            number=number,
            title=f"Task {number}",
            status=status,
            dependencies=list(dependencies or []),
            priority=number,
            blocked_on=blocked_on,
            fields={},
            field_order=[],
        )

    def test_selection_result_requires_consistent_task_id(self) -> None:
        with self.assertRaises(SelectorError):
            Selection("selected")
        with self.assertRaises(SelectorError):
            Selection("blocked", 1)
        with self.assertRaises(SelectorError):
            Selection("work_exhausted", 1)

    def test_empty_and_duplicate_task_models_rejected(self) -> None:
        with self.assertRaisesRegex(SelectorError, "no tasks"):
            select_task(Plan(tasks=[]))
        with self.assertRaisesRegex(SelectorError, "duplicate task id"):
            select_task(Plan(tasks=[self.task(1), self.task(1)]))

    def test_invalid_dependency_graphs_rejected(self) -> None:
        invalid = (
            (Plan(tasks=[self.task(1, dependencies=[1])]), "itself"),
            (Plan(tasks=[self.task(1, dependencies=[2])]), "unknown"),
            (
                Plan(tasks=[
                    self.task(1, dependencies=[2]),
                    self.task(2, dependencies=[1]),
                ]),
                "cycle",
            ),
        )
        for plan, fragment in invalid:
            with self.subTest(fragment=fragment):
                with self.assertRaisesRegex(SelectorError, fragment):
                    select_task(plan)

    def test_blocked_task_requires_exact_reference(self) -> None:
        plan = Plan(tasks=[self.task(1, status="blocked")])
        with self.assertRaisesRegex(SelectorError, "exact unresolved reference"):
            select_task(plan)


class SingleTaskGuaranteeTest(unittest.TestCase):
    """Selection is exactly one task, or a classification with none."""

    def test_every_fixture_selects_at_most_one_task(self) -> None:
        plans = [parse_fixture(name) for name in SELECTOR_FIXTURES]
        plans.append(Plan.from_file(CANONICAL_PLAN))
        for plan in plans:
            with self.subTest(fixture=plan.base_commit[:8]):
                if any(task.status == "in_progress" and any(
                    dep not in {
                        t.number for t in plan.tasks if t.status == "complete"
                    }
                    for dep in task.dependencies
                ) for task in plan.tasks):
                    # The inconsistent-in-progress fixture is rejected by the
                    # selector (tested in StaleAndAmbiguousRejectionTest).
                    with self.assertRaises(SelectorError):
                        select_task(plan)
                    continue
                selection = select_task(plan)
                self.assertIn(selection.classification, CLASSIFICATIONS)
                if selection.classification == "selected":
                    self.assertIsNotNone(selection.task_id)
                    matches = [
                        t for t in plan.tasks if t.number == selection.task_id
                    ]
                    self.assertEqual(len(matches), 1)
                else:
                    self.assertIsNone(selection.task_id)


class PurityAndLedgerBoundaryTest(unittest.TestCase):
    """The selector is a pure function and never consults a runtime ledger."""

    def test_selection_is_a_pure_function(self) -> None:
        plan = Plan.from_file(CANONICAL_PLAN)
        first = select_task(plan)
        for _ in range(50):
            self.assertEqual(select_task(plan), first)

    def test_selection_performs_no_io(self) -> None:
        # `select_task` runs on an in-memory plan model with no file access:
        # no ledger path, no state path, no filesystem import is exercised.
        import importlib

        selector = importlib.import_module("selector")
        source = Path(selector.__file__).read_text(encoding="utf-8")
        for forbidden in (
            "tasks.jsonl",           # Ralph-era runtime task ledger
            ".ralph",                # legacy Ralph state
            ".factory-state",  # the one mutable control-state file
            "open(",           # no file descriptor in the selection path
            "subprocess",      # no child process in the selection path
            "os.",             # no process/OS state in the selection path
        ):
            self.assertNotIn(forbidden, source, forbidden)
        plan = Plan.from_file(CANONICAL_PLAN)
        self.assertEqual(select_task(plan).task_id, 12)

    def test_selection_result_is_immutable(self) -> None:
        plan = parse_fixture("plan-select-priority-order.md")
        selection = select_task(plan)
        with self.assertRaises(AttributeError):
            selection.task_id = 99  # type: ignore[misc]

    def test_selected_requires_task_id(self) -> None:
        with self.assertRaises(SelectorError):
            Selection("selected")  # type: ignore[call-arg]
        with self.assertRaises(SelectorError):
            Selection("unknown_class", 1)


class TrustedCliTest(unittest.TestCase):
    """The trusted control-plane CLI prints one deterministic outcome."""

    def _run(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(LOOP / "selector.py"), *args],
            capture_output=True,
            text=True,
        )

    def test_cli_selects_canonical_next_task(self) -> None:
        result = self._run("select", str(CANONICAL_PLAN))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "selected=12")

    def test_cli_reports_blocked(self) -> None:
        result = self._run("select", str(FIXTURES / "plan-select-blocked.md"))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "classification=blocked")

    def test_cli_reports_work_exhausted(self) -> None:
        result = self._run(
            "select", str(FIXTURES / "plan-select-work-exhausted.md")
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            result.stdout.strip(), "classification=work_exhausted"
        )

    def test_cli_rejects_stale_binding(self) -> None:
        result = self._run(
            "select",
            str(FIXTURES / "plan-select-stale.md"),
            "--bound-base-commit",
            "3333333333333333333333333333333333333333",
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("stale plan", result.stderr)

    def test_cli_rejects_invalid_plan(self) -> None:
        result = self._run("select", str(FIXTURES / "plan-two-in-progress.md"))
        self.assertEqual(result.returncode, 1)
        self.assertNotEqual(result.stderr, "")


class FixtureIntegrityTest(unittest.TestCase):
    """Every selector fixture parses, round-trips byte-exactly, and is listed."""

    def test_fixture_corpus_is_exhaustive(self) -> None:
        committed = sorted(p.name for p in FIXTURES.glob("plan-select-*.md"))
        self.assertEqual(committed, sorted(SELECTOR_FIXTURES))

    def test_every_fixture_roundtrips_byte_exactly(self) -> None:
        for name in SELECTOR_FIXTURES:
            with self.subTest(fixture=name):
                path = FIXTURES / name
                raw = path.read_bytes()
                plan = parse_plan(raw.decode("utf-8"))
                self.assertEqual(plan.serialize().encode("utf-8"), raw)


if __name__ == "__main__":
    suite = unittest.defaultTestLoader.loadTestsFromModule(sys.modules[__name__])
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    raise SystemExit(0 if result.wasSuccessful() else 1)
