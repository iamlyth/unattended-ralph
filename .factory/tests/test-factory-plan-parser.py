#!/usr/bin/env python3
"""Harness-owned conformance tests for the `factory-plan/v1` parser (PLAN-01).

This test lives under the hidden `.factory/tests/` namespace because the
specification (HIDE-01, §3) keeps harness-only tests out of the adopting
product's visible test tree. It is the deterministic verification for Task 2:

* the parser and the existing plan validator agree on the canonical committed
  plan (`.factory/artifacts/implementation-plan.md`);
* the parser round-trips the canonical plan and every valid fixture
  byte-exactly (no semantic loss);
* the parsed model and JSON dump are deterministic functions of the plan
  bytes;
* every documented defect class is rejected by its exact fixture with the
  documented error class;
* the allowed status transition table is enforced as documented.
"""

from __future__ import annotations

import json
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
LOOP = ROOT / ".factory" / "loop"
FIXTURES = ROOT / ".factory" / "tests" / "fixtures"
CANONICAL_PLAN = ROOT / ".factory" / "artifacts" / "implementation-plan.md"
VALIDATOR = ROOT / "scripts" / "validate-implementation-plan.py"

sys.path.insert(0, str(LOOP))
from plan_parser import (  # noqa: E402
    ALLOWED_TRANSITIONS,
    FINAL_AUDIT_TITLE,
    INTERACTION_BOUNDARIES,
    LIFECYCLE_STATUSES,
    MATRIX_HEADER,
    Plan,
    PlanError,
    SCHEMA_NAME,
    TASK_STATUSES,
    is_allowed_transition,
    parse_plan,
)

# Every documented defect class, mapped to its exact fixture and the error
# fragment that proves the class was rejected (not some earlier grammar stop).
FIXTURE_EXPECTATIONS = {
    "plan-duplicate-heading.md": "duplicate canonical section",
    "plan-unknown-status.md": "invalid status",
    "plan-unknown-lifecycle-status.md": "front matter status",
    "plan-ambiguous-task-section.md": "malformed task heading",
    "plan-out-of-order-dependency.md": "must refer to earlier tasks",
    "plan-cyclic-dependency.md": "contains a cycle",
    "plan-noncontiguous-ids.md": "contiguous",
    "plan-duplicate-task-id.md": "duplicate task id",
    "plan-duplicate-task-title.md": "duplicate task title",
    "plan-duplicate-field.md": "duplicate field",
    "plan-missing-field.md": "requires exactly one",
    "plan-unknown-field.md": "unknown field",
    "plan-malformed-dependencies.md": "malformed dependencies",
    "plan-self-dependency.md": "cannot depend on itself",
    "plan-unknown-dependency.md": "unknown dependencies",
    "plan-two-in-progress.md": "at most one task",
    "plan-blocked-without-reference.md": "Blocked on",
    "plan-bad-priority.md": "priority must be",
    "plan-matrix-duplicate-id.md": "duplicate conformance requirement ID",
    "plan-matrix-bad-classification.md": "invalid classification",
    "plan-matrix-bad-header.md": "matrix header",
    "plan-matrix-unknown-task.md": "references an unknown task",
    "plan-missing-interactions.md": "requires canonical section",
    "plan-missing-interaction-boundary.md": "must cover",
    "plan-front-matter-missing.md": "missing",
    "plan-front-matter-duplicate-key.md": "duplicate key",
    "plan-front-matter-bad-sha.md": "40-character",
    "plan-front-matter-absolute-path.md": "repository-relative",
    "plan-unrecognized-heading.md": "unknown section heading",
    "plan-no-tasks.md": "no numbered tasks",
    "plan-no-title.md": "title",
}


class CanonicalPlanAgreementTest(unittest.TestCase):
    """The parser and the existing validator agree on the canonical plan."""

    def test_canonical_plan_parses(self) -> None:
        plan = Plan.from_file(CANONICAL_PLAN)
        self.assertEqual(plan.schema, SCHEMA_NAME)
        self.assertEqual(plan.status, "active")
        self.assertEqual(len(plan.tasks), 18)
        self.assertEqual(len(plan.matrix), 24)
        self.assertEqual(
            [entry.boundary for entry in plan.interactions],
            list(INTERACTION_BOUNDARIES),
        )
        # Lifecycle invariants hold on the committed plan.
        self.assertEqual(
            [task.status for task in plan.tasks].count("in_progress"), 0
        )
        final = [task for task in plan.tasks if task.title == FINAL_AUDIT_TITLE]
        self.assertEqual(len(final), 1)
        self.assertEqual(final[0].number, 18)
        self.assertEqual(set(final[0].dependencies), set(range(1, 18)))
        # Default priority derives from the task id for a stable sort.
        self.assertEqual([task.priority for task in plan.tasks], list(range(1, 19)))
        # Front matter binds the canonical specification.
        self.assertEqual(plan.spec_path, "docs/FACTORY-LOOP-SPEC.md")

    def test_existing_validator_agrees(self) -> None:
        result = subprocess.run(
            [
                sys.executable,
                str(VALIDATOR),
                "planning",
                str(CANONICAL_PLAN),
            ],
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_existing_validator_agrees_on_base_fixture(self) -> None:
        base = FIXTURES / "plan-valid-base.md"
        result = subprocess.run(
            [sys.executable, str(VALIDATOR), "planning", str(base)],
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)


class RoundTripAndDeterminismTest(unittest.TestCase):
    """parse -> serialize -> parse is byte-exact and deterministic."""

    def _assert_byte_exact_roundtrip(self, path: Path) -> None:
        raw = path.read_bytes()
        plan = parse_plan(raw.decode("utf-8"))
        serialized = plan.serialize()
        self.assertEqual(serialized.encode("utf-8"), raw)
        second = parse_plan(serialized)
        self.assertEqual(second.to_dict(), plan.to_dict())

    def test_canonical_plan_roundtrips_byte_exactly(self) -> None:
        self._assert_byte_exact_roundtrip(CANONICAL_PLAN)

    def test_base_fixture_roundtrips_byte_exactly(self) -> None:
        self._assert_byte_exact_roundtrip(FIXTURES / "plan-valid-base.md")

    def test_dump_is_deterministic_from_bytes(self) -> None:
        raw = CANONICAL_PLAN.read_bytes()
        first = parse_plan(raw.decode("utf-8")).dump_json()
        second = parse_plan(raw.decode("utf-8")).dump_json()
        self.assertEqual(first, second)
        # JSON is stable and key-sorted.
        self.assertEqual(first, json.dumps(
            json.loads(first), sort_keys=True, ensure_ascii=False, separators=(",", ":")
        ))

    def test_parse_result_is_stable(self) -> None:
        raw = CANONICAL_PLAN.read_bytes()
        one = parse_plan(raw.decode("utf-8")).to_dict()
        two = parse_plan(raw.decode("utf-8")).to_dict()
        self.assertEqual(one, two)

    def test_dump_model_shape(self) -> None:
        plan = parse_plan(CANONICAL_PLAN.read_text("utf-8"))
        model = json.loads(plan.dump_json())
        self.assertEqual(model["schema"], "factory-plan/v1")
        for key in ("spec_path", "spec_commit", "spec_blob", "base_commit", "status"):
            self.assertIn(key, model)
        for task in model["tasks"]:
            self.assertEqual(
                set(task),
                {"number", "title", "status", "priority", "dependencies",
                 "blocked_on", "fields"},
            )
            self.assertIn(task["status"], TASK_STATUSES)
            self.assertIsInstance(task["priority"], int)
            self.assertGreaterEqual(task["priority"], 1)
            for required in ("Status", "Dependencies", "Scope",
                             "Acceptance criteria", "Verification",
                             "Documentation impact"):
                self.assertIn(required, task["fields"])
        for row in model["matrix"]:
            self.assertEqual(
                set(row), {"requirement_id", "spec_sections", "classification",
                           "evidence", "tasks"}
            )
        self.assertEqual(
            {entry["boundary"] for entry in model["interactions"]},
            set(INTERACTION_BOUNDARIES),
        )


class FixtureRejectionTest(unittest.TestCase):
    """Every documented defect class has an exact fixture and is rejected."""

    def test_every_defect_fixture_is_rejected(self) -> None:
        for name, fragment in FIXTURE_EXPECTATIONS.items():
            path = FIXTURES / name
            with self.subTest(fixture=name, fragment=fragment):
                self.assertTrue(path.is_file(), f"missing fixture {path}")
                with self.assertRaises(PlanError) as caught:
                    parse_plan(path.read_text("utf-8"))
                message = str(caught.exception)
                self.assertIn(
                    fragment, message,
                    f"{name}: expected {fragment!r} in {message!r}",
                )

    def test_valid_base_is_accepted(self) -> None:
        plan = parse_plan((FIXTURES / "plan-valid-base.md").read_text("utf-8"))
        self.assertEqual(len(plan.tasks), 2)
        self.assertEqual(plan.tasks[0].priority, 3)
        self.assertEqual(plan.tasks[0].dependencies, [])
        self.assertEqual(plan.tasks[1].dependencies, [1])
        self.assertEqual(plan.tasks[1].title, FINAL_AUDIT_TITLE)


class TransitionTableTest(unittest.TestCase):
    """The documented allowed status transitions are enforced."""

    def test_allowed_transitions(self) -> None:
        allowed = {
            ("pending", "in_progress"),
            ("in_progress", "complete"),
            ("in_progress", "blocked"),
            ("in_progress", "pending"),
            ("blocked", "pending"),
        }
        for current in TASK_STATUSES:
            for next_status in TASK_STATUSES:
                expected = (current, next_status) in allowed
                self.assertEqual(
                    is_allowed_transition(current, next_status),
                    expected,
                    f"{current} -> {next_status}",
                )

    def test_documented_invariants(self) -> None:
        # complete is write-once: no transition may leave it.
        self.assertEqual(set(ALLOWED_TRANSITIONS["complete"]), set())
        # lifecycle statuses and task statuses are exactly the documented sets.
        self.assertEqual(tuple(LIFECYCLE_STATUSES), ("active", "complete"))
        self.assertEqual(
            tuple(TASK_STATUSES),
            ("pending", "in_progress", "complete", "blocked"),
        )
        self.assertEqual(
            MATRIX_HEADER,
            ("ID", "Spec §", "Classification", "Evidence", "Task"),
        )


if __name__ == "__main__":
    suite = unittest.defaultTestLoader.loadTestsFromModule(sys.modules[__name__])
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    raise SystemExit(0 if result.wasSuccessful() else 1)
