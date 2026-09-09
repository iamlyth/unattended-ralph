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
VALIDATOR = ROOT / ".factory" / "tools" / "validate-implementation-plan.py"

sys.path.insert(0, str(LOOP))
import plan_parser  # noqa: E402
from plan_parser import (  # noqa: E402
    ALLOWED_TRANSITIONS,
    FINAL_AUDIT_TITLE,
    INTERACTION_BOUNDARIES,
    LIFECYCLE_STATUSES,
    MATRIX_HEADER,
    Plan,
    PlanError,
    SCHEMA_NAME,
    STABLE_REQUIREMENT_IDS,
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
    "plan-unknown-dependency.md": "references unknown dependencies",
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
    # Task 18 hardened-boundary fixtures: exact adversarial inputs for every
    # newly closed untrusted-plan acceptance gap.
    "plan-bom.md": "byte order mark",
    "plan-verified-empty-refs.md": "must reference a completed task",
    "plan-verified-pending.md": "not complete",
    "plan-verified-in-active-plan.md": "`active`",
    "plan-matrix-missing-id.md": "must cover every",
    "plan-matrix-extra-id.md": "outside the",
    "plan-lifecycle-inconsistent.md": "every task",
    "plan-dependency-range-oversize.md": "oversized dependency range",
    "plan-matrix-range-oversize.md": "oversized task range",
    "plan-range-overflow.md": "out-of-range dependency number",
    "plan-structured-field-continuation.md": "must not have continuation lines",
    "plan-empty-interaction.md": "must not be empty",
    "plan-front-matter-traversal-path.md": "`..`",
    "plan-final-audit-misplaced.md": "must be the last task",
    "plan-final-audit-missing-dependency.md": "must depend on every other task",
    "plan-matrix-complete-only-pending.md": "must own it",
    "plan-matrix-complete-nonverified.md": "references only completed tasks",
    "plan-empty-required-value.md": "has an empty",
    "plan-missing-title.md": "has no title",
    "plan-duplicate-title.md": "exactly one",
}

# Accepted fixtures that must parse, serialize byte-identically, and stay
# accepted by the legacy validator (parser/legacy-validator agreement).
# Accepted fixtures that must parse, serialize byte-identically, and stay
# accepted by the legacy validator (parser/legacy-validator agreement).
# plan-classification-blocked/not-applicable cover the Task 14 classification
# values added to the accepted enum (spec §22: `blocked` and narrowly
# justified spec-scoped `not_applicable` keep their fail-closed semantics).
ACCEPTED_FIXTURES = (
    FIXTURES / "plan-valid-base.md",
    FIXTURES / "plan-trailing-blank-line.md",
    FIXTURES / "plan-classification-blocked.md",
    FIXTURES / "plan-classification-not-applicable.md",
)


class CanonicalPlanAgreementTest(unittest.TestCase):
    """The parser and the existing validator agree on the canonical plan."""

    def test_canonical_plan_parses(self) -> None:
        plan = Plan.from_file(CANONICAL_PLAN)
        self.assertEqual(plan.schema, SCHEMA_NAME)
        self.assertEqual(plan.status, "active")
        self.assertEqual(len(plan.tasks), 36)
        self.assertEqual(len(plan.matrix), 27)
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
        self.assertEqual(final[0].number, 36)
        self.assertEqual(set(final[0].dependencies), set(range(1, 36)))
        # Default priority derives from the task id for a stable sort. Task 19
        # alone retains its explicit remediation priority; dependencies keep
        # audit-round tasks 20-24 finite and serialized; the redesign
        # foundation tasks 25-27 and the scheduler foundation task 30 precede
        # the final audit; the Phase 2C1 path-lease foundation is Task 32.
        self.assertEqual(
            [task.priority for task in plan.tasks],
            list(range(1, 19)) + [1] + list(range(20, 37)),
        )
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
                 "blocked_on", "write_scopes", "fields"},
            )
            self.assertIn(task["status"], TASK_STATUSES)
            self.assertIsInstance(task["priority"], int)
            self.assertGreaterEqual(task["priority"], 1)
            self.assertIsInstance(task["write_scopes"], list)
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

    def test_every_accepted_fixture_parses_and_roundtrips(self) -> None:
        for path in ACCEPTED_FIXTURES:
            with self.subTest(fixture=path.name):
                plan = parse_plan(path.read_text("utf-8"))
                self.assertEqual(
                    plan.serialize().encode("utf-8"), path.read_bytes()
                )

    def test_blocked_classification_parses(self) -> None:
        plan = parse_plan(
            (FIXTURES / "plan-classification-blocked.md").read_text("utf-8")
        )
        row = next(r for r in plan.matrix if r.requirement_id == "AUTH-01")
        self.assertEqual(row.classification, "blocked")
        self.assertEqual(row.tasks, [1])

    def test_not_applicable_classification_parses(self) -> None:
        plan = parse_plan(
            (FIXTURES / "plan-classification-not-applicable.md").read_text("utf-8")
        )
        row = next(r for r in plan.matrix if r.requirement_id == "AUTH-01")
        self.assertEqual(row.classification, "not_applicable")
        self.assertEqual(row.tasks, [1])

    def test_complete_plan_cannot_carry_blocked_row(self) -> None:
        # A `complete` lifecycle plan may never carry a blocked/not_applicable
        # row: every task is complete, so the non-verified row references only
        # completed tasks and is rejected (blocked/not_applicable fail
        # implementation completion).
        with self.assertRaises(PlanError) as caught:
            parse_plan(
                (FIXTURES / "plan-matrix-complete-nonverified.md").read_text("utf-8")
            )
        self.assertIn("references only completed tasks", str(caught.exception))


class BoundedRangeProbeTest(unittest.TestCase):
    """Repeated oversized-range probes stay within a fixed time/memory ceiling.

    Task 18 acceptance requires that attacker-sized dependency and matrix task
    ranges are rejected without being materialized, so that repeated probes of
    the range fixtures consume a bounded, endpoint-independent slice of CPU and
    RSS. The parser bounds every range endpoint to the parsed task count before
    expansion, so a probe loop must stay far below an endpoint-proportional
    ceiling.
    """

    RANGE_FIXTURES = (
        FIXTURES / "plan-dependency-range-oversize.md",
        FIXTURES / "plan-matrix-range-oversize.md",
        FIXTURES / "plan-range-overflow.md",
    )
    ITERATIONS = 200
    # Generous fixed ceilings: the parser rejects each probe in microseconds,
    # so a batch of 600 parses stays well under one CPU second and consumes a
    # negligible amount of peak RSS regardless of the endpoint magnitude.
    CPU_CEILING_SECONDS = 10.0
    RSS_CEILING_KIB = 32 * 1024

    def test_oversized_range_probes_stay_bounded(self) -> None:
        import resource
        import time

        rss_before = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        start = time.process_time()
        for _ in range(self.ITERATIONS):
            for fixture in self.RANGE_FIXTURES:
                with self.assertRaises(PlanError):
                    parse_plan(fixture.read_text("utf-8"))
        elapsed = time.process_time() - start
        rss_after = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        self.assertLess(
            elapsed, self.CPU_CEILING_SECONDS,
            f"{self.ITERATIONS} range probes took {elapsed:.3f}s",
        )
        self.assertLess(
            rss_after - rss_before, self.RSS_CEILING_KIB,
            "range probes grew peak RSS by "
            f"{rss_after - rss_before} KiB",
        )


class BoundedPriorityTest(unittest.TestCase):
    """M2: an oversized Priority digit string is bounded before int() and
    raises a PlanError, never an uncaught ValueError traceback.

    Python 3.11+ caps int-string conversion (``sys.set_int_max_str_digits``
    default 4300), so an attacker-sized digit string would otherwise raise
    an uncaught ``ValueError``.  The parser bounds the digit length first and
    raises a bounded ``PlanError``.
    """

    BASE = (FIXTURES / "plan-valid-base.md").read_text("utf-8")

    def _with_priority(self, value: str) -> str:
        return self.BASE.replace("- Priority: 3", f"- Priority: {value}")

    def test_oversized_priority_raises_bounded_plan_error(self) -> None:
        huge = "9" * (plan_parser.MAX_PRIORITY_DIGITS + 1)
        with self.assertRaises(PlanError) as caught:
            parse_plan(self._with_priority(huge))
        self.assertIn("priority must be", str(caught.exception))
        self.assertIn("at most", str(caught.exception))

    def test_max_digit_priority_is_accepted(self) -> None:
        ok = "9" * plan_parser.MAX_PRIORITY_DIGITS
        plan = parse_plan(self._with_priority(ok))
        self.assertEqual(plan.tasks[0].priority, int(ok))


class WriteScopesFieldTest(unittest.TestCase):
    """The optional closed-format `Write scopes:` request field (Phase 2C1).

    The planner request grants nothing by itself (the trusted policy
    intersection decides); the parser only enforces the closed format,
    duplicate rejection, and byte-exact legacy compatibility.
    """

    BASE = (FIXTURES / "plan-valid-base.md").read_text("utf-8")

    def _with_field(self, value: str) -> str:
        return self.BASE.replace(
            "- Status: pending\n- Dependencies: None\n- Priority: 3",
            f"- Status: pending\n- Dependencies: None\n- Priority: 3\n- Write scopes: {value}",
        )

    def test_absent_field_is_legacy_compatible(self) -> None:
        plan = parse_plan(self.BASE)
        self.assertEqual(plan.tasks[0].write_scopes, [])
        self.assertEqual(plan.serialize(), self.BASE)

    def test_request_list_parses_and_roundtrips(self) -> None:
        text = self._with_field("scripts, nix")
        plan = parse_plan(text)
        self.assertEqual(plan.tasks[0].write_scopes, ["scripts", "nix"])
        self.assertEqual(plan.serialize(), text)
        model = json.loads(plan.dump_json())
        self.assertEqual(model["tasks"][0]["write_scopes"], ["scripts", "nix"])

    def test_none_means_no_request(self) -> None:
        plan = parse_plan(self._with_field("None"))
        self.assertEqual(plan.tasks[0].write_scopes, [])

    def test_duplicate_scope_is_rejected(self) -> None:
        with self.assertRaises(PlanError) as caught:
            parse_plan(self._with_field("scripts, scripts"))
        self.assertIn("duplicate write scope", str(caught.exception))

    def test_invalid_scope_id_is_rejected(self) -> None:
        for bad in ("Scripts", "scripts/", "-scripts", "scripts..", "a b"):
            with self.subTest(bad=bad):
                with self.assertRaises(PlanError) as caught:
                    parse_plan(self._with_field(bad))
                self.assertIn("invalid write scope", str(caught.exception))

    def test_empty_item_is_rejected(self) -> None:
        with self.assertRaises(PlanError) as caught:
            parse_plan(self._with_field("scripts, "))
        self.assertIn("malformed", str(caught.exception))

    def test_continuation_line_is_rejected(self) -> None:
        text = self._with_field("scripts").replace(
            "- Write scopes: scripts",
            "- Write scopes: scripts\n  nix",
        )
        with self.assertRaises(PlanError) as caught:
            parse_plan(text)
        self.assertIn("continuation", str(caught.exception))


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
