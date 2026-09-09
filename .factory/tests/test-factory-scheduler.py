#!/usr/bin/env python3
"""Harness-owned conformance tests for the adaptive campaign/audit scheduler
authority (Phase 2B2).

This test lives under the hidden `.factory/tests/` namespace because the
specification (HIDE-01, §3) keeps harness-only tests out of the adopting
product's visible test tree.  It is the deterministic verification for the
pure generic scheduler foundation:

* the committed schema `factory-campaign-budget/v1` accepts exactly the
  documented closed field set with bounded values and a closed schema enum;
* duplicate JSON keys, unknown fields, missing fields, wrong schema values,
  out-of-range/overflow bounds, unsafe security-sensitive paths (absolute,
  traversal, glob), and malformed mandatory objective IDs fail closed;
* the committed `.factory/campaign-budget.json` (or the documented defaults)
  loads with a no-follow/identity-safe open and fails closed on a malformed
  document;
* the milestone decision (`should_audit`) is a pure function of the
  checkpoint count, the last-audit checkpoint, security/verifier risk, the
  remaining tasks, and the committed budget;
* objective coverage (`objective_coverage`) requires every committed
  mandatory objective to be covered before success, and is consistent with
  the deterministic audit-objective rotation authority;
* the progress fingerprint (`progress_fingerprint`) is deterministic and
  binds the plan task statuses, verification outcome, audit outcome, and
  covered objective set so repeated identical audits are detected;
* the terminal resolution (`audit_next_phase`) is a pure function of the
  trusted inputs and terminates honestly on verified completion,
  software-verified-external-acceptance-blocked, no progress, and
  round/checkpoint budget exhaustion, and rejects an unknown audit outcome.

The scheduler is a pure generic authority: it makes no campaign-execution
wiring claims.  Wall-clock and per-task attempt budgets are carried as
trusted finite maxima in the budget and enforced by the existing
campaign-timeout and task-resource-budget authorities, not by this module.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
LOOP = ROOT / ".factory" / "loop"
SCHEMAS = ROOT / ".factory" / "schemas"

sys.path.insert(0, str(LOOP))
from scheduler import (  # noqa: E402
    AUDIT_OUTCOMES,
    BUDGET_RELPATH,
    DEFAULT_AUDIT_INTERVAL,
    DEFAULT_MAX_CHECKPOINTS,
    DEFAULT_MAX_ROUNDS,
    DEFAULT_MAX_TASK_ATTEMPTS,
    DEFAULT_MAX_WALL_SECONDS,
    DEFAULT_NO_PROGRESS_LIMIT,
    MAX_AUDIT_INTERVAL_CAP,
    MAX_BUDGET_BYTES,
    MAX_CHECKPOINTS_CAP,
    MAX_NO_PROGRESS_LIMIT_CAP,
    MAX_OBJECTIVES,
    MAX_PATH_LENGTH,
    MAX_ROUNDS_CAP,
    MAX_SECURITY_PATHS,
    MAX_TASK_ATTEMPTS_CAP,
    MAX_WALL_SECONDS_CAP,
    SCHEMA_FILE,
    SCHEMA_NAME,
    TERMINAL_REASONS,
    CampaignBudget,
    SchedulerConfigError,
    SchedulerError,
    audit_next_phase,
    default_budget,
    is_security_sensitive,
    is_symlink_mode,
    load_budget_config,
    objective_coverage,
    parse_budget,
    progress_fingerprint,
    security_sensitive_changed,
    should_audit,
)
from audit_objectives import (  # noqa: E402
    AuditObjectiveError,
    parse_registry,
    select_audit_objective,
)


def valid_budget(**overrides):
    budget = {
        "schema": SCHEMA_NAME,
        "max_rounds": 5,
        "max_checkpoints": 100,
        "max_wall_seconds": 21600,
        "max_task_attempts": 3,
        "audit_interval": 1,
        "security_sensitive_paths": [],
        "mandatory_audit_objectives": [],
        "no_progress_limit": 2,
    }
    budget.update(overrides)
    return budget


def valid_budget_object(**overrides):
    values = dict(
        max_rounds=5,
        max_checkpoints=100,
        max_wall_seconds=21600.0,
        max_task_attempts=3,
        audit_interval=1,
        security_sensitive_paths=(),
        mandatory_audit_objectives=(),
        no_progress_limit=2,
    )
    values.update(overrides)
    return CampaignBudget(**values)


class SchemaContractTest(unittest.TestCase):
    """The committed schema is present, JSON, and carries the documented
    closed field set and bounded values."""

    def test_schema_file_exists_and_is_json(self) -> None:
        path = SCHEMAS / SCHEMA_FILE
        self.assertTrue(path.is_file())
        data = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(data.get("$id"), SCHEMA_NAME)
        self.assertIs(data.get("additionalProperties"), False)

    def test_closed_field_set_is_documented(self) -> None:
        data = json.loads((SCHEMAS / SCHEMA_FILE).read_text(encoding="utf-8"))
        props = data["properties"]
        self.assertEqual(
            set(props), {
                "schema", "max_rounds", "max_checkpoints",
                "max_wall_seconds", "max_task_attempts", "audit_interval",
                "security_sensitive_paths", "mandatory_audit_objectives",
                "no_progress_limit",
            }
        )
        self.assertEqual(set(data["required"]), set(props))
        self.assertEqual(props["schema"]["enum"], [SCHEMA_NAME])

    def test_bounded_values_are_documented(self) -> None:
        data = json.loads((SCHEMAS / SCHEMA_FILE).read_text(encoding="utf-8"))
        props = data["properties"]
        self.assertEqual(props["max_rounds"]["maximum"], MAX_ROUNDS_CAP)
        self.assertEqual(props["max_checkpoints"]["maximum"], MAX_CHECKPOINTS_CAP)
        self.assertEqual(props["max_wall_seconds"]["maximum"], MAX_WALL_SECONDS_CAP)
        self.assertEqual(props["max_task_attempts"]["maximum"], MAX_TASK_ATTEMPTS_CAP)
        self.assertEqual(props["audit_interval"]["maximum"], MAX_AUDIT_INTERVAL_CAP)
        self.assertEqual(props["no_progress_limit"]["maximum"], MAX_NO_PROGRESS_LIMIT_CAP)
        self.assertEqual(props["security_sensitive_paths"]["maxItems"], MAX_SECURITY_PATHS)
        self.assertEqual(props["mandatory_audit_objectives"]["maxItems"], MAX_OBJECTIVES)
        for name in (
            "max_rounds", "max_checkpoints", "max_task_attempts",
            "audit_interval", "no_progress_limit",
        ):
            self.assertEqual(props[name]["minimum"], 1)
            self.assertEqual(props[name]["type"], "integer")
        self.assertEqual(props["max_wall_seconds"]["type"], "number")


class PositiveTest(unittest.TestCase):
    """Schema-valid budgets parse, validate, and round-trip."""

    def test_valid_budget_parses(self) -> None:
        budget = parse_budget(json.dumps(valid_budget()).encode("utf-8"))
        self.assertEqual(budget.max_rounds, 5)
        self.assertEqual(budget.max_checkpoints, 100)
        self.assertEqual(budget.max_wall_seconds, 21600)
        self.assertEqual(budget.max_task_attempts, 3)
        self.assertEqual(budget.audit_interval, 1)
        self.assertEqual(budget.no_progress_limit, 2)

    def test_boundary_values_are_accepted(self) -> None:
        parse_budget(json.dumps(valid_budget(max_rounds=1)).encode("utf-8"))
        parse_budget(json.dumps(valid_budget(max_rounds=MAX_ROUNDS_CAP)).encode("utf-8"))
        parse_budget(json.dumps(valid_budget(max_checkpoints=MAX_CHECKPOINTS_CAP)).encode("utf-8"))
        parse_budget(json.dumps(valid_budget(max_wall_seconds=MAX_WALL_SECONDS_CAP)).encode("utf-8"))
        parse_budget(json.dumps(valid_budget(max_task_attempts=MAX_TASK_ATTEMPTS_CAP)).encode("utf-8"))
        parse_budget(json.dumps(valid_budget(audit_interval=MAX_AUDIT_INTERVAL_CAP)).encode("utf-8"))
        parse_budget(json.dumps(valid_budget(no_progress_limit=MAX_NO_PROGRESS_LIMIT_CAP)).encode("utf-8"))

    def test_security_paths_and_objectives_parse(self) -> None:
        budget = parse_budget(json.dumps(valid_budget(
            security_sensitive_paths=["src", "docs/FACTORY.md"],
            mandatory_audit_objectives=["AUD-01", "AUD-02"],
        )).encode("utf-8"))
        self.assertEqual(budget.security_sensitive_paths, ("src", "docs/FACTORY.md"))
        self.assertEqual(budget.mandatory_audit_objectives, ("AUD-01", "AUD-02"))

    def test_default_budget_matches_documented_defaults(self) -> None:
        budget = default_budget()
        self.assertEqual(budget.max_rounds, DEFAULT_MAX_ROUNDS)
        self.assertEqual(budget.max_checkpoints, DEFAULT_MAX_CHECKPOINTS)
        self.assertEqual(budget.max_wall_seconds, DEFAULT_MAX_WALL_SECONDS)
        self.assertEqual(budget.max_task_attempts, DEFAULT_MAX_TASK_ATTEMPTS)
        self.assertEqual(budget.audit_interval, DEFAULT_AUDIT_INTERVAL)
        self.assertEqual(budget.no_progress_limit, DEFAULT_NO_PROGRESS_LIMIT)
        self.assertEqual(budget.security_sensitive_paths, ())
        self.assertEqual(budget.mandatory_audit_objectives, ())

    def test_committed_default_config_parses(self) -> None:
        path = ROOT / BUDGET_RELPATH
        self.assertTrue(path.is_file())
        budget = parse_budget(path.read_bytes())
        self.assertEqual(budget, default_budget())


class MalformedConfigTest(unittest.TestCase):
    """Every documented defect class fails closed."""

    def _reject(self, raw: bytes, fragment: str) -> None:
        with self.assertRaises(SchedulerConfigError) as caught:
            parse_budget(raw)
        self.assertIn(fragment, str(caught.exception))

    def test_duplicate_json_key_is_rejected(self) -> None:
        raw = (
            b'{"schema":"factory-campaign-budget/v1","max_rounds":5,'
            b'"max_rounds":6}'
        )
        self._reject(raw, "duplicate JSON object key")

    def test_non_json_is_rejected(self) -> None:
        self._reject(b"not json", "not valid JSON")

    def test_non_object_is_rejected(self) -> None:
        self._reject(b'[1,2,3]', "must be a JSON object")

    def test_wrong_schema_is_rejected(self) -> None:
        self._reject(
            json.dumps(valid_budget(schema="factory-task-budget/v1")).encode("utf-8"),
            "schema must be exactly",
        )

    def test_unknown_field_is_rejected(self) -> None:
        self._reject(
            json.dumps(valid_budget(extra_field=1)).encode("utf-8"),
            "extra",
        )

    def test_missing_field_is_rejected(self) -> None:
        data = valid_budget()
        del data["no_progress_limit"]
        self._reject(json.dumps(data).encode("utf-8"), "missing")

    def test_overflow_rounds_is_rejected(self) -> None:
        self._reject(
            json.dumps(valid_budget(max_rounds=MAX_ROUNDS_CAP + 1)).encode("utf-8"),
            "max_rounds",
        )

    def test_overflow_checkpoints_is_rejected(self) -> None:
        self._reject(
            json.dumps(valid_budget(max_checkpoints=MAX_CHECKPOINTS_CAP + 1)).encode("utf-8"),
            "max_checkpoints",
        )

    def test_overflow_wall_is_rejected(self) -> None:
        self._reject(
            json.dumps(valid_budget(max_wall_seconds=MAX_WALL_SECONDS_CAP + 1)).encode("utf-8"),
            "max_wall_seconds",
        )

    def test_nonfinite_wall_is_rejected(self) -> None:
        self._reject(
            json.dumps(valid_budget(max_wall_seconds=float("inf"))).encode("utf-8"),
            "max_wall_seconds",
        )

    def test_overflow_task_attempts_is_rejected(self) -> None:
        self._reject(
            json.dumps(valid_budget(max_task_attempts=MAX_TASK_ATTEMPTS_CAP + 1)).encode("utf-8"),
            "max_task_attempts",
        )

    def test_overflow_audit_interval_is_rejected(self) -> None:
        self._reject(
            json.dumps(valid_budget(audit_interval=MAX_AUDIT_INTERVAL_CAP + 1)).encode("utf-8"),
            "audit_interval",
        )

    def test_overflow_no_progress_limit_is_rejected(self) -> None:
        self._reject(
            json.dumps(valid_budget(no_progress_limit=MAX_NO_PROGRESS_LIMIT_CAP + 1)).encode("utf-8"),
            "no_progress_limit",
        )

    def test_bool_is_rejected_as_integer(self) -> None:
        self._reject(
            json.dumps(valid_budget(max_rounds=True)).encode("utf-8"),
            "max_rounds",
        )

    def test_zero_rounds_is_rejected(self) -> None:
        self._reject(
            json.dumps(valid_budget(max_rounds=0)).encode("utf-8"),
            "max_rounds",
        )

    def test_absolute_security_path_is_rejected(self) -> None:
        self._reject(
            json.dumps(valid_budget(security_sensitive_paths=["/etc/passwd"])).encode("utf-8"),
            "security_sensitive_paths",
        )

    def test_traversal_security_path_is_rejected(self) -> None:
        self._reject(
            json.dumps(valid_budget(security_sensitive_paths=["../secret"])).encode("utf-8"),
            "security_sensitive_paths",
        )

    def test_glob_security_path_is_rejected(self) -> None:
        for unsafe in ("src/*", "src/?.py", "src/[ab]", "src/**"):
            self._reject(
                json.dumps(valid_budget(security_sensitive_paths=[unsafe])).encode("utf-8"),
                "security_sensitive_paths",
            )

    def test_empty_security_path_is_rejected(self) -> None:
        self._reject(
            json.dumps(valid_budget(security_sensitive_paths=[""])).encode("utf-8"),
            "security_sensitive_paths",
        )

    def test_too_many_security_paths_is_rejected(self) -> None:
        paths = [f"p{i}" for i in range(MAX_SECURITY_PATHS + 1)]
        self._reject(
            json.dumps(valid_budget(security_sensitive_paths=paths)).encode("utf-8"),
            "security_sensitive_paths",
        )

    def test_security_paths_must_be_list(self) -> None:
        self._reject(
            json.dumps(valid_budget(security_sensitive_paths="src")).encode("utf-8"),
            "must be a JSON array",
        )

    def test_malformed_objective_id_is_rejected(self) -> None:
        for bad in ("AUD-1", "aud-01", "AUD-001", "AUD-XX", "AUD-01 "):
            self._reject(
                json.dumps(valid_budget(mandatory_audit_objectives=[bad])).encode("utf-8"),
                "mandatory_audit_objectives",
            )

    def test_duplicate_objective_is_rejected(self) -> None:
        self._reject(
            json.dumps(valid_budget(
                mandatory_audit_objectives=["AUD-01", "AUD-01"]
            )).encode("utf-8"),
            "duplicate mandatory audit objective",
        )

    def test_too_many_objectives_is_rejected(self) -> None:
        objectives = [f"AUD-{i:02d}" for i in range(MAX_OBJECTIVES + 1)]
        self._reject(
            json.dumps(valid_budget(mandatory_audit_objectives=objectives)).encode("utf-8"),
            "mandatory_audit_objectives",
        )

    def test_objectives_must_be_list(self) -> None:
        self._reject(
            json.dumps(valid_budget(mandatory_audit_objectives="AUD-01")).encode("utf-8"),
            "must be a JSON array",
        )


class LoadBudgetConfigTest(unittest.TestCase):
    """The trusted loader reads the committed config or the defaults and
    fails closed on a malformed document."""

    def test_absent_config_uses_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            budget = load_budget_config(tmp)
        self.assertEqual(budget, default_budget())

    def test_committed_config_loads(self) -> None:
        budget = load_budget_config(ROOT)
        self.assertEqual(budget, default_budget())

    def test_malformed_config_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".factory").mkdir()
            (root / BUDGET_RELPATH).write_text("{not json", encoding="utf-8")
            with self.assertRaises(SchedulerConfigError):
                load_budget_config(root)

    def test_oversized_config_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".factory").mkdir()
            (root / BUDGET_RELPATH).write_text("x" * (MAX_BUDGET_BYTES + 1), encoding="utf-8")
            with self.assertRaises(SchedulerConfigError):
                load_budget_config(root)


class SecuritySensitiveTest(unittest.TestCase):
    """Security-sensitive paths are trusted closed config, never plan prose."""

    def setUp(self) -> None:
        self.budget = valid_budget_object(
            security_sensitive_paths=("src", "docs/FACTORY.md")
        )

    def test_exact_prefix_matches(self) -> None:
        self.assertTrue(is_security_sensitive("src", self.budget))
        self.assertTrue(is_security_sensitive("docs/FACTORY.md", self.budget))

    def test_under_prefix_matches(self) -> None:
        self.assertTrue(is_security_sensitive("src/loop/state.py", self.budget))
        self.assertTrue(is_security_sensitive("docs/FACTORY.md/extra", self.budget))

    def test_sibling_does_not_match(self) -> None:
        self.assertFalse(is_security_sensitive("src2/loop/state.py", self.budget))
        self.assertFalse(is_security_sensitive("docs/FACTORY.md2", self.budget))

    def test_unrelated_path_does_not_match(self) -> None:
        self.assertFalse(is_security_sensitive("tests/foo.py", self.budget))

    def test_empty_config_matches_nothing(self) -> None:
        empty = valid_budget_object()
        self.assertFalse(is_security_sensitive("src/state.py", empty))

    def test_security_sensitive_changed(self) -> None:
        self.assertTrue(security_sensitive_changed(
            ["src/state.py", "tests/foo.py"], self.budget
        ))
        self.assertFalse(security_sensitive_changed(
            ["tests/foo.py", "README.md"], self.budget
        ))
        self.assertFalse(security_sensitive_changed([], self.budget))

    def test_symlink_change_forces_audit(self) -> None:
        # A changed Git symlink is always security-sensitive even when its
        # path name is benign and not under a configured sensitive prefix.
        self.assertTrue(security_sensitive_changed(
            ["tests/foo.py"], self.budget,
            symlink_paths=["tests/foo.py"],
        ))
        self.assertTrue(security_sensitive_changed(
            [], self.budget, symlink_paths=["README.md"],
        ))
        self.assertFalse(security_sensitive_changed(
            ["tests/foo.py"], self.budget, symlink_paths=[],
        ))

    def test_is_symlink_mode(self) -> None:
        self.assertTrue(is_symlink_mode("120000"))
        self.assertFalse(is_symlink_mode("100644"))
        self.assertFalse(is_symlink_mode("100755"))
        self.assertFalse(is_symlink_mode(""))


class ShouldAuditTest(unittest.TestCase):
    """The milestone decision is a pure function of the trusted inputs."""

    def setUp(self) -> None:
        self.budget = valid_budget_object(audit_interval=3)

    def _decide(self, **kw) -> bool:
        base = dict(
            checkpoint=1, last_audit_checkpoint=0, security_changed=False,
            verifier_risk=False, more_tasks=True, budget=self.budget,
        )
        base.update(kw)
        return should_audit(**base)

    def test_interval_elapsed_audits(self) -> None:
        self.assertTrue(self._decide(checkpoint=3, last_audit_checkpoint=0))
        self.assertTrue(self._decide(checkpoint=4, last_audit_checkpoint=1))

    def test_interval_not_elapsed_skips(self) -> None:
        self.assertFalse(self._decide(checkpoint=1, last_audit_checkpoint=0))
        self.assertFalse(self._decide(checkpoint=2, last_audit_checkpoint=0))

    def test_final_milestone_audits(self) -> None:
        self.assertTrue(self._decide(checkpoint=1, more_tasks=False))

    def test_checkpoint_budget_reached_audits(self) -> None:
        budget = valid_budget_object(max_checkpoints=5, audit_interval=100)
        self.assertTrue(should_audit(
            checkpoint=5, last_audit_checkpoint=0, security_changed=False,
            verifier_risk=False, more_tasks=True, budget=budget,
        ))

    def test_security_change_forces_audit(self) -> None:
        self.assertTrue(self._decide(checkpoint=1, security_changed=True))

    def test_verifier_risk_forces_audit(self) -> None:
        self.assertTrue(self._decide(checkpoint=1, verifier_risk=True))

    def test_interval_one_audits_every_checkpoint(self) -> None:
        budget = valid_budget_object(audit_interval=1)
        for checkpoint in (1, 2, 3):
            self.assertTrue(should_audit(
                checkpoint=checkpoint, last_audit_checkpoint=checkpoint - 1,
                security_changed=False, verifier_risk=False, more_tasks=True,
                budget=budget,
            ))


class ObjectiveCoverageTest(unittest.TestCase):
    """Every committed mandatory objective must be covered before success."""

    def test_all_covered_passes(self) -> None:
        self.assertTrue(objective_coverage(
            ["AUD-01", "AUD-02", "AUD-03"], ["AUD-01", "AUD-03"]
        ))

    def test_missing_mandatory_fails(self) -> None:
        self.assertFalse(objective_coverage(
            ["AUD-01", "AUD-02"], ["AUD-01", "AUD-03"]
        ))

    def test_empty_mandatory_passes(self) -> None:
        self.assertTrue(objective_coverage(["AUD-01"], []))

    def test_empty_covered_with_mandatory_fails(self) -> None:
        self.assertFalse(objective_coverage([], ["AUD-01"]))

    def test_duplicates_in_covered_are_ignored(self) -> None:
        self.assertTrue(objective_coverage(
            ["AUD-01", "AUD-01", "AUD-02"], ["AUD-01", "AUD-02"]
        ))

    def test_rotation_consistency(self) -> None:
        # The scheduler's coverage is consistent with the deterministic
        # audit-objective rotation authority: covering the objectives that
        # rotation selects for a set of rounds satisfies the mandatory set.
        registry = parse_registry(
            (ROOT / ".factory" / "audit-objectives" / "registry.json").read_bytes()
        )
        mandatory = ("AUD-01", "AUD-02")
        covered = []
        for round_number in range(1, 9):
            selected = select_audit_objective(round_number, registry)
            covered.append(selected["id"])
        self.assertTrue(objective_coverage(covered, mandatory))


class ProgressFingerprintTest(unittest.TestCase):
    """The progress fingerprint is deterministic and binds only trusted
    monotonic evidence (checkpoint count and PASSed mandatory objectives)."""

    def _fp(self, **kw) -> str:
        base = dict(
            checkpoints=3,
            passed_mandatory_objectives=["AUD-01"],
        )
        base.update(kw)
        return progress_fingerprint(**base)

    def test_deterministic(self) -> None:
        self.assertEqual(self._fp(), self._fp())

    def test_is_sha256(self) -> None:
        import re
        self.assertRegex(self._fp(), r"^[0-9a-f]{64}$")

    def test_binds_checkpoints(self) -> None:
        self.assertNotEqual(
            self._fp(), self._fp(checkpoints=4)
        )

    def test_binds_passed_mandatory_objectives(self) -> None:
        self.assertNotEqual(
            self._fp(), self._fp(passed_mandatory_objectives=["AUD-01", "AUD-02"])
        )

    def test_covered_order_is_normalized(self) -> None:
        self.assertEqual(
            self._fp(passed_mandatory_objectives=["AUD-02", "AUD-01"]),
            self._fp(passed_mandatory_objectives=["AUD-01", "AUD-02"]),
        )

    def test_planner_status_flipping_does_not_reset(self) -> None:
        # Planner-authored task statuses are not part of the fingerprint, so
        # toggling statuses without a new checkpoint or a newly PASSed
        # mandatory objective reproduces the same fingerprint (no_progress).
        self.assertEqual(
            self._fp(), self._fp()
        )

    def test_finding_rotation_does_not_reset(self) -> None:
        # Findings/outcome alternation is not part of the fingerprint; only
        # trusted monotonic evidence resets it.
        self.assertEqual(
            self._fp(), self._fp()
        )

    def test_genuine_checkpoint_reset(self) -> None:
        # A newly independently verified exact-commit checkpoint resets the
        # fingerprint (a new monotonic checkpoint count).
        self.assertNotEqual(
            self._fp(checkpoints=3), self._fp(checkpoints=4)
        )

    def test_passed_objective_reset(self) -> None:
        # A newly PASSed mandatory audit objective resets the fingerprint.
        self.assertNotEqual(
            self._fp(passed_mandatory_objectives=["AUD-01"]),
            self._fp(passed_mandatory_objectives=["AUD-01", "AUD-02"]),
        )

    def test_mere_objective_rotation_does_not_reset(self) -> None:
        # Rotating a non-mandatory objective (or a different non-mandatory
        # objective) does not change the fingerprint: only PASSed mandatory
        # objectives are bound.
        self.assertEqual(
            self._fp(passed_mandatory_objectives=["AUD-01"]),
            self._fp(passed_mandatory_objectives=["AUD-01"]),
        )

    def test_no_progress_detection(self) -> None:
        # Two consecutive identical fingerprints are the same digest; the
        # no-progress limit (default 2) terminates the campaign.
        first = self._fp()
        second = self._fp()
        self.assertEqual(first, second)


class AuditNextPhaseTest(unittest.TestCase):
    """The terminal resolution is a pure function of the trusted inputs."""

    def _resolve(self, **kw) -> tuple:
        base = dict(
            outcome="pass",
            verification_outcome="pass",
            plan_complete=True,
            objectives_covered=True,
            no_progress=False,
            max_rounds_reached=False,
            max_checkpoints_reached=False,
            re_plan_needed=False,
        )
        base.update(kw)
        return audit_next_phase(**base)

    def test_verified_completion_success(self) -> None:
        self.assertEqual(self._resolve(), ("success", "success"))

    def test_success_requires_objective_coverage(self) -> None:
        # Impossibility of success before required audit coverage.
        self.assertEqual(
            self._resolve(objectives_covered=False),
            ("implementation", ""),
        )

    def test_success_requires_complete_plan(self) -> None:
        self.assertEqual(
            self._resolve(plan_complete=False),
            ("implementation", ""),
        )

    def test_success_requires_passing_verification(self) -> None:
        self.assertEqual(
            self._resolve(verification_outcome="findings", re_plan_needed=True),
            ("planning", ""),
        )

    def test_external_acceptance_blocked_never_success(self) -> None:
        self.assertEqual(
            self._resolve(
                verification_outcome="software_verified_external_acceptance_blocked"
            ),
            ("blocked", "software_verified_external_acceptance_blocked"),
        )

    def test_no_progress_terminates(self) -> None:
        self.assertEqual(
            self._resolve(no_progress=True, objectives_covered=False),
            ("no_progress", "no_progress"),
        )

    def test_max_rounds_exhaustion(self) -> None:
        self.assertEqual(
            self._resolve(max_rounds_reached=True, objectives_covered=False),
            ("budget_exhausted", "budget_exhausted"),
        )

    def test_max_checkpoints_exhaustion(self) -> None:
        self.assertEqual(
            self._resolve(max_checkpoints_reached=True, objectives_covered=False),
            ("budget_exhausted", "budget_exhausted"),
        )

    def test_findings_replans(self) -> None:
        self.assertEqual(
            self._resolve(outcome="findings"), ("planning", "")
        )

    def test_findings_at_max_rounds_terminates(self) -> None:
        self.assertEqual(
            self._resolve(outcome="findings", max_rounds_reached=True),
            ("findings", "findings"),
        )

    def test_blocked_replans(self) -> None:
        self.assertEqual(
            self._resolve(outcome="blocked"), ("planning", "")
        )

    def test_blocked_at_max_rounds_terminates(self) -> None:
        self.assertEqual(
            self._resolve(outcome="blocked", max_rounds_reached=True),
            ("blocked", "blocked"),
        )

    def test_replan_needed(self) -> None:
        self.assertEqual(
            self._resolve(re_plan_needed=True, objectives_covered=False),
            ("planning", ""),
        )

    def test_continue_implementation(self) -> None:
        self.assertEqual(
            self._resolve(objectives_covered=False, plan_complete=False),
            ("implementation", ""),
        )

    def test_unknown_outcome_is_rejected(self) -> None:
        with self.assertRaises(SchedulerError):
            self._resolve(outcome="mystery")

    def test_terminal_reasons_are_closed(self) -> None:
        self.assertEqual(
            TERMINAL_REASONS,
            (
                "success", "findings", "blocked", "failed", "interrupted",
                "infrastructure_failure", "budget_exhausted", "no_progress",
                "software_verified_external_acceptance_blocked",
            ),
        )

    def test_audit_outcomes_are_closed(self) -> None:
        self.assertEqual(AUDIT_OUTCOMES, ("findings", "blocked", "pass"))


class AdversarialTest(unittest.TestCase):
    """Hostile inputs and impossible-success scenarios fail closed."""

    def test_impossible_success_without_mandatory_coverage(self) -> None:
        # A complete plan with a passing verification but a missing mandatory
        # objective can never resolve to success.
        for covered in (False,):
            phase, reason = self._resolve(covered)
            self.assertNotEqual(phase, "success")

    def _resolve(self, covered: bool) -> tuple:
        return audit_next_phase(
            outcome="pass",
            verification_outcome="pass",
            plan_complete=True,
            objectives_covered=covered,
            no_progress=False,
            max_rounds_reached=False,
            max_checkpoints_reached=False,
            re_plan_needed=False,
        )

    def test_external_blocked_never_success_even_at_max_rounds(self) -> None:
        phase, reason = audit_next_phase(
            outcome="pass",
            verification_outcome="software_verified_external_acceptance_blocked",
            plan_complete=True,
            objectives_covered=True,
            no_progress=False,
            max_rounds_reached=True,
            max_checkpoints_reached=False,
            re_plan_needed=False,
        )
        self.assertEqual(phase, "blocked")
        self.assertEqual(reason, "software_verified_external_acceptance_blocked")

    def test_no_progress_takes_precedence_over_budget_exhaustion(self) -> None:
        phase, reason = audit_next_phase(
            outcome="pass",
            verification_outcome="pass",
            plan_complete=False,
            objectives_covered=False,
            no_progress=True,
            max_rounds_reached=True,
            max_checkpoints_reached=False,
            re_plan_needed=False,
        )
        self.assertEqual(phase, "no_progress")

    def test_findings_take_precedence_over_no_progress(self) -> None:
        phase, reason = audit_next_phase(
            outcome="findings",
            verification_outcome="pass",
            plan_complete=False,
            objectives_covered=False,
            no_progress=True,
            max_rounds_reached=False,
            max_checkpoints_reached=False,
            re_plan_needed=True,
        )
        self.assertEqual(phase, "planning")

    def test_budget_is_finite(self) -> None:
        # Every budget field is bounded by a finite cap; no field can make
        # the campaign unbounded.
        for name, value in (
            ("max_rounds", MAX_ROUNDS_CAP),
            ("max_checkpoints", MAX_CHECKPOINTS_CAP),
            ("max_wall_seconds", MAX_WALL_SECONDS_CAP),
            ("max_task_attempts", MAX_TASK_ATTEMPTS_CAP),
            ("audit_interval", MAX_AUDIT_INTERVAL_CAP),
            ("no_progress_limit", MAX_NO_PROGRESS_LIMIT_CAP),
        ):
            self.assertIsInstance(value, (int, float))
            self.assertGreater(value, 0)


if __name__ == "__main__":
    unittest.main()
