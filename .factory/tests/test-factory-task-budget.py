#!/usr/bin/env python3
"""Harness-owned conformance tests for the versioned strict task-resource
budget authority (Phase 2A).

This test lives under the hidden `.factory/tests/` namespace because the
specification (HIDE-01, §3) keeps harness-only tests out of the adopting
product's visible test tree.  It is the deterministic verification for the
task-resource-budget foundation:

* the committed schema `factory-task-budget/v1` accepts exactly the
  documented closed field set with bounded integers and a closed schema
  enum;
* duplicate JSON keys, oversized documents, unknown schema values, extra
  fields, missing required fields, and out-of-range bounds fail closed;
* the canonical budget bytes are deterministic and round-trip through
  parse -> bytes -> parse without semantic loss;
* the trusted config loader reads the committed `.factory/task-budget.json`
  (or the documented defaults) with a no-follow/identity-safe open and
  fails closed on a malformed/foreign/oversized config;
* the cumulative per-task ledger (`factory-task-budget-ledger/v1`) records
  usage monotonically, rejects negative/foreign usage, derives remaining
  limits, reports every closed exhaustion reason, and publishes with
  no-replace byte-idempotent tamper handling;
* `validate_commit_context` (verifier-failure EVID-02) rejects a commit
  mismatch, an unresolvable commit, and a resolver failure.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
LOOP = ROOT / ".factory" / "loop"
SCHEMAS = ROOT / ".factory" / "schemas"

sys.path.insert(0, str(LOOP))
from task_budget import (  # noqa: E402
    BUDGET_CONFIG_FILE,
    DEFAULT_TASK_BUDGET,
    EXHAUSTION_REASONS,
    LEDGER_SCHEMA_NAME,
    MAX_BUDGET_BYTES,
    MAX_CPU_TIME_SECONDS,
    MAX_LEDGER_BYTES,
    MAX_LIVE_PROCESSES,
    MAX_OUTPUT_BYTES,
    MAX_PER_COMMAND_TIMEOUT_SECONDS,
    MAX_WALL_TIME_SECONDS,
    SCHEMA_FILE,
    SCHEMA_NAME,
    BudgetLedger,
    TaskBudgetError,
    TaskBudgetMalformedError,
    budget_bytes,
    exhausted_reason,
    ledger_bytes,
    load_budget_config,
    load_ledger,
    parse_budget,
    parse_ledger,
    remaining_limits,
    save_ledger,
    validate_budget,
)
from verifier_failure import (  # noqa: E402
    VerifierFailureError,
    VerifierFailureMalformedError,
    build_artifact,
    validate_commit_context,
)

CAMPAIGN = "task-budget-conformance"


def valid_budget(**overrides):
    budget = {
        "schema": SCHEMA_NAME,
        "wall_time_seconds": 3600,
        "cpu_time_seconds": 1800,
        "output_bytes": 64 * 1024 * 1024,
        "max_live_processes": 256,
        "per_command_timeout_seconds": 300,
    }
    budget.update(overrides)
    return budget


def valid_ledger(**overrides):
    ledger = {
        "schema": LEDGER_SCHEMA_NAME,
        "campaign_id": CAMPAIGN,
        "task_id": 1,
        "wall_time_seconds": 0.0,
        "cpu_time_seconds": 0.0,
        "output_bytes": 0,
        "max_live_processes": 0,
        "exhausted_reason": None,
    }
    ledger.update(overrides)
    return ledger


class SchemaContractTest(unittest.TestCase):
    """The committed schema is present, JSON, and carries the documented
    closed field set and bounded integers."""

    def test_schema_file_exists_and_is_json(self) -> None:
        path = SCHEMAS / SCHEMA_FILE
        self.assertTrue(path.is_file())
        data = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(data.get("$id"), SCHEMA_NAME)
        self.assertIs(data.get("additionalProperties"), False)

    def test_closed_field_set_is_documented(self) -> None:
        path = SCHEMAS / SCHEMA_FILE
        data = json.loads(path.read_text(encoding="utf-8"))
        props = data["properties"]
        self.assertEqual(
            set(props), {
                "schema", "wall_time_seconds", "cpu_time_seconds",
                "output_bytes", "max_live_processes",
                "per_command_timeout_seconds",
            }
        )
        self.assertEqual(
            set(data["required"]), set(props)
        )
        self.assertEqual(props["schema"]["enum"], [SCHEMA_NAME])

    def test_bounded_integers_are_documented(self) -> None:
        path = SCHEMAS / SCHEMA_FILE
        data = json.loads(path.read_text(encoding="utf-8"))
        props = data["properties"]
        self.assertEqual(props["wall_time_seconds"]["maximum"], MAX_WALL_TIME_SECONDS)
        self.assertEqual(props["cpu_time_seconds"]["maximum"], MAX_CPU_TIME_SECONDS)
        self.assertEqual(props["output_bytes"]["maximum"], MAX_OUTPUT_BYTES)
        self.assertEqual(props["max_live_processes"]["maximum"], MAX_LIVE_PROCESSES)
        self.assertEqual(
            props["per_command_timeout_seconds"]["maximum"],
            MAX_PER_COMMAND_TIMEOUT_SECONDS,
        )
        for name in (
            "wall_time_seconds", "cpu_time_seconds", "output_bytes",
            "max_live_processes", "per_command_timeout_seconds",
        ):
            self.assertEqual(props[name]["minimum"], 1)
            self.assertEqual(props[name]["type"], "integer")


class PositiveTest(unittest.TestCase):
    """Schema-valid budgets parse, validate, and round-trip."""

    def test_valid_budget_validates(self) -> None:
        validate_budget(valid_budget())

    def test_valid_budget_roundtrips_bytes(self) -> None:
        budget = valid_budget()
        raw = budget_bytes(budget)
        reparsed = parse_budget(raw)
        self.assertEqual(reparsed, budget)

    def test_deterministic_bytes(self) -> None:
        first = budget_bytes(valid_budget())
        second = budget_bytes(valid_budget())
        self.assertEqual(first, second)
        # Canonical bytes are sorted JSON with no newline.
        self.assertNotIn(b"\n", first)
        self.assertEqual(
            first, json.dumps(
                valid_budget(), sort_keys=True, separators=(",", ":")
            ).encode("utf-8")
        )

    def test_boundary_values_are_accepted(self) -> None:
        validate_budget(valid_budget(wall_time_seconds=1))
        validate_budget(valid_budget(wall_time_seconds=MAX_WALL_TIME_SECONDS))
        validate_budget(valid_budget(cpu_time_seconds=MAX_CPU_TIME_SECONDS))
        validate_budget(valid_budget(output_bytes=MAX_OUTPUT_BYTES))
        validate_budget(valid_budget(max_live_processes=MAX_LIVE_PROCESSES))
        validate_budget(
            valid_budget(per_command_timeout_seconds=MAX_PER_COMMAND_TIMEOUT_SECONDS)
        )


class MalformedInputTest(unittest.TestCase):
    """Every documented defect class fails closed."""

    def _reject(self, budget, fragment: str) -> None:
        with self.assertRaises(TaskBudgetMalformedError) as caught:
            validate_budget(budget)
        self.assertIn(fragment, str(caught.exception))

    def test_duplicate_json_key_is_rejected(self) -> None:
        raw = (
            b'{"schema":"factory-task-budget/v1","schema":"x",'
            b'"wall_time_seconds":3600,"cpu_time_seconds":1800,'
            b'"output_bytes":1,"max_live_processes":1,'
            b'"per_command_timeout_seconds":1}'
        )
        with self.assertRaises(TaskBudgetMalformedError) as caught:
            parse_budget(raw)
        self.assertIn("duplicate JSON object key", str(caught.exception))

    def test_oversized_document_is_rejected(self) -> None:
        with self.assertRaises(TaskBudgetMalformedError) as caught:
            parse_budget(b" " * (MAX_BUDGET_BYTES + 1))
        self.assertIn("oversized", str(caught.exception))

    def test_non_bytes_is_rejected(self) -> None:
        with self.assertRaises(TaskBudgetMalformedError):
            parse_budget("not bytes")  # type: ignore[arg-type]

    def test_non_utf8_is_rejected(self) -> None:
        with self.assertRaises(TaskBudgetMalformedError) as caught:
            parse_budget(b"\xff\xfe\x00")
        self.assertIn("not UTF-8", str(caught.exception))

    def test_not_json_is_rejected(self) -> None:
        with self.assertRaises(TaskBudgetMalformedError) as caught:
            parse_budget(b"not json")
        self.assertIn("not valid JSON", str(caught.exception))

    def test_unknown_schema_is_rejected(self) -> None:
        self._reject(valid_budget(schema="factory-task-budget/v2"), "schema")

    def test_extra_field_is_rejected(self) -> None:
        self._reject(valid_budget(extra="x"), "extra field")

    def test_missing_required_field_is_rejected(self) -> None:
        budget = valid_budget()
        budget.pop("output_bytes")
        self._reject(budget, "missing required field")

    def test_non_object_is_rejected(self) -> None:
        with self.assertRaises(TaskBudgetMalformedError):
            validate_budget(["not", "an", "object"])

    def test_bool_integer_is_rejected(self) -> None:
        self._reject(valid_budget(wall_time_seconds=True), "wall_time_seconds")

    def test_zero_and_negative_bounds_are_rejected(self) -> None:
        self._reject(valid_budget(wall_time_seconds=0), "wall_time_seconds")
        self._reject(valid_budget(cpu_time_seconds=-1), "cpu_time_seconds")
        self._reject(valid_budget(output_bytes=0), "output_bytes")
        self._reject(valid_budget(max_live_processes=0), "max_live_processes")
        self._reject(
            valid_budget(per_command_timeout_seconds=0),
            "per_command_timeout_seconds",
        )

    def test_overflow_bounds_are_rejected(self) -> None:
        self._reject(
            valid_budget(wall_time_seconds=MAX_WALL_TIME_SECONDS + 1),
            "wall_time_seconds",
        )
        self._reject(
            valid_budget(cpu_time_seconds=MAX_CPU_TIME_SECONDS + 1),
            "cpu_time_seconds",
        )
        self._reject(
            valid_budget(output_bytes=MAX_OUTPUT_BYTES + 1), "output_bytes"
        )
        self._reject(
            valid_budget(max_live_processes=MAX_LIVE_PROCESSES + 1),
            "max_live_processes",
        )
        self._reject(
            valid_budget(per_command_timeout_seconds=MAX_PER_COMMAND_TIMEOUT_SECONDS + 1),
            "per_command_timeout_seconds",
        )

    def test_float_bound_is_rejected(self) -> None:
        self._reject(valid_budget(wall_time_seconds=1.5), "wall_time_seconds")


class ConfigLoadingTest(unittest.TestCase):
    """The trusted config loader reads the committed config or defaults."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="task-budget-config."))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def test_absent_config_uses_documented_defaults(self) -> None:
        budget = load_budget_config(self.tmp)
        self.assertEqual(budget, DEFAULT_TASK_BUDGET)
        validate_budget(budget)

    def test_committed_config_is_loaded_and_validated(self) -> None:
        factory = self.tmp / ".factory"
        factory.mkdir()
        (factory / BUDGET_CONFIG_FILE).write_text(
            json.dumps(valid_budget(wall_time_seconds=120)), encoding="utf-8"
        )
        budget = load_budget_config(self.tmp)
        self.assertEqual(budget["wall_time_seconds"], 120)

    def test_malformed_config_fails_closed(self) -> None:
        factory = self.tmp / ".factory"
        factory.mkdir()
        (factory / BUDGET_CONFIG_FILE).write_text(
            json.dumps(valid_budget(wall_time_seconds=0)), encoding="utf-8"
        )
        with self.assertRaises(TaskBudgetError) as caught:
            load_budget_config(self.tmp)
        self.assertIn("malformed", str(caught.exception))

    def test_foreign_config_fails_closed(self) -> None:
        factory = self.tmp / ".factory"
        factory.mkdir()
        (factory / BUDGET_CONFIG_FILE).write_text(
            json.dumps(valid_budget(schema="factory-task-budget/v2")),
            encoding="utf-8",
        )
        with self.assertRaises(TaskBudgetError):
            load_budget_config(self.tmp)

    def test_oversized_config_fails_closed(self) -> None:
        factory = self.tmp / ".factory"
        factory.mkdir()
        (factory / BUDGET_CONFIG_FILE).write_bytes(b" " * (MAX_BUDGET_BYTES + 1))
        with self.assertRaises(TaskBudgetError) as caught:
            load_budget_config(self.tmp)
        self.assertIn("oversized", str(caught.exception))

    def test_symlink_config_fails_closed(self) -> None:
        factory = self.tmp / ".factory"
        factory.mkdir()
        target = self.tmp / "elsewhere.json"
        target.write_text(json.dumps(valid_budget()), encoding="utf-8")
        (factory / BUDGET_CONFIG_FILE).symlink_to(target)
        with self.assertRaises(TaskBudgetError):
            load_budget_config(self.tmp)


class LedgerAccountingTest(unittest.TestCase):
    """The cumulative ledger records usage monotonically and rejects abuse."""

    def test_fresh_ledger_has_zero_usage(self) -> None:
        ledger = BudgetLedger(campaign_id=CAMPAIGN, task_id=1)
        self.assertEqual(ledger.wall_time_seconds, 0.0)
        self.assertEqual(ledger.cpu_time_seconds, 0.0)
        self.assertEqual(ledger.output_bytes, 0)
        self.assertEqual(ledger.max_live_processes, 0)
        self.assertIsNone(ledger.exhausted_reason)

    def test_record_attempt_accumulates_monotonically(self) -> None:
        ledger = BudgetLedger(campaign_id=CAMPAIGN, task_id=1)
        ledger.record_attempt(
            wall_time_seconds=10.5, cpu_time_seconds=2.25,
            output_bytes=1000, max_live_processes=4,
        )
        ledger.record_attempt(
            wall_time_seconds=5.0, cpu_time_seconds=1.0,
            output_bytes=500, max_live_processes=2,
        )
        self.assertEqual(ledger.wall_time_seconds, 15.5)
        self.assertEqual(ledger.cpu_time_seconds, 3.25)
        self.assertEqual(ledger.output_bytes, 1500)
        # The live-process dimension records the peak, never a sum.
        self.assertEqual(ledger.max_live_processes, 4)

    def test_record_attempt_rejects_negative_usage(self) -> None:
        ledger = BudgetLedger(campaign_id=CAMPAIGN, task_id=1)
        base = {
            "wall_time_seconds": 0.0, "cpu_time_seconds": 0.0,
            "output_bytes": 0, "max_live_processes": 0,
        }
        for name, value in (
            ("wall_time_seconds", -1.0),
            ("cpu_time_seconds", -1.0),
            ("output_bytes", -1),
            ("max_live_processes", -1),
            ("wall_time_seconds", True),
            ("output_bytes", True),
            ("max_live_processes", 1.5),
        ):
            with self.assertRaises(TaskBudgetError):
                ledger.record_attempt(**{**base, name: value})

    def test_mark_exhausted_first_reason_wins(self) -> None:
        ledger = BudgetLedger(campaign_id=CAMPAIGN, task_id=1)
        ledger.mark_exhausted("wall_time")
        ledger.mark_exhausted("cpu_time")
        self.assertEqual(ledger.exhausted_reason, "wall_time")

    def test_mark_exhausted_rejects_unknown_reason(self) -> None:
        ledger = BudgetLedger(campaign_id=CAMPAIGN, task_id=1)
        with self.assertRaises(TaskBudgetError):
            ledger.mark_exhausted("mystery")

    def test_every_exhaustion_reason_is_closed(self) -> None:
        self.assertEqual(
            EXHAUSTION_REASONS,
            (
                "wall_time", "cpu_time", "output_bytes", "live_processes",
                "per_command_timeout", "accounting_untrusted",
            ),
        )
        for reason in EXHAUSTION_REASONS:
            ledger = BudgetLedger(campaign_id=CAMPAIGN, task_id=1)
            ledger.mark_exhausted(reason)
            self.assertEqual(ledger.exhausted_reason, reason)

    def test_exhausted_reason_derivation_precedence(self) -> None:
        budget = valid_budget(
            wall_time_seconds=100, cpu_time_seconds=50,
            output_bytes=1000, max_live_processes=10,
        )
        ledger = BudgetLedger(campaign_id=CAMPAIGN, task_id=1)
        self.assertIsNone(exhausted_reason(ledger, budget))
        ledger.record_attempt(
            wall_time_seconds=100.0, cpu_time_seconds=1.0,
            output_bytes=1, max_live_processes=1,
        )
        self.assertEqual(exhausted_reason(ledger, budget), "wall_time")
        ledger = BudgetLedger(campaign_id=CAMPAIGN, task_id=1)
        ledger.record_attempt(
            wall_time_seconds=1.0, cpu_time_seconds=50.0,
            output_bytes=1, max_live_processes=1,
        )
        self.assertEqual(exhausted_reason(ledger, budget), "cpu_time")
        ledger = BudgetLedger(campaign_id=CAMPAIGN, task_id=1)
        ledger.record_attempt(
            wall_time_seconds=1.0, cpu_time_seconds=1.0,
            output_bytes=1000, max_live_processes=1,
        )
        self.assertEqual(exhausted_reason(ledger, budget), "output_bytes")
        ledger = BudgetLedger(campaign_id=CAMPAIGN, task_id=1)
        ledger.record_attempt(
            wall_time_seconds=1.0, cpu_time_seconds=1.0,
            output_bytes=1, max_live_processes=10,
        )
        self.assertEqual(exhausted_reason(ledger, budget), "live_processes")
        # A recorded exhaustion reason wins over the derived precedence.
        ledger.mark_exhausted("accounting_untrusted")
        self.assertEqual(exhausted_reason(ledger, budget), "accounting_untrusted")

    def test_remaining_limits_derive_and_floor_at_zero(self) -> None:
        budget = valid_budget(
            wall_time_seconds=100, cpu_time_seconds=50,
            output_bytes=1000, max_live_processes=10,
        )
        ledger = BudgetLedger(campaign_id=CAMPAIGN, task_id=1)
        remaining = remaining_limits(ledger, budget)
        self.assertEqual(remaining["wall_time_seconds"], 100.0)
        self.assertEqual(remaining["cpu_time_seconds"], 50.0)
        self.assertEqual(remaining["output_bytes"], 1000)
        self.assertEqual(remaining["max_live_processes"], 10)
        ledger.record_attempt(
            wall_time_seconds=150.0, cpu_time_seconds=60.0,
            output_bytes=2000, max_live_processes=3,
        )
        remaining = remaining_limits(ledger, budget)
        self.assertEqual(remaining["wall_time_seconds"], 0.0)
        self.assertEqual(remaining["cpu_time_seconds"], 0.0)
        self.assertEqual(remaining["output_bytes"], 0)
        # The live-process dimension is a per-attempt peak bound: the full
        # budget remains for the next attempt.
        self.assertEqual(remaining["max_live_processes"], 10)

    def test_ledger_roundtrips_bytes(self) -> None:
        ledger = BudgetLedger(campaign_id=CAMPAIGN, task_id=1)
        ledger.record_attempt(
            wall_time_seconds=10.0, cpu_time_seconds=2.0,
            output_bytes=100, max_live_processes=3,
        )
        raw = ledger_bytes(ledger)
        reparsed = parse_ledger(raw)
        self.assertEqual(reparsed.to_dict(), ledger.to_dict())

    def test_parse_ledger_rejects_duplicate_keys(self) -> None:
        raw = (
            b'{"schema":"factory-task-budget-ledger/v1",'
            b'"schema":"x","campaign_id":"c","task_id":1,'
            b'"wall_time_seconds":0,"cpu_time_seconds":0,'
            b'"output_bytes":0,"max_live_processes":0,'
            b'"exhausted_reason":null}'
        )
        with self.assertRaises(TaskBudgetMalformedError) as caught:
            parse_ledger(raw)
        self.assertIn("duplicate JSON object key", str(caught.exception))

    def test_parse_ledger_rejects_foreign_or_malformed(self) -> None:
        with self.assertRaises(TaskBudgetMalformedError):
            parse_ledger(b"{}")
        with self.assertRaises(TaskBudgetMalformedError):
            parse_ledger(ledger_bytes(
                BudgetLedger(campaign_id="../escape", task_id=1)
            ))
        with self.assertRaises(TaskBudgetMalformedError):
            parse_ledger(ledger_bytes(
                BudgetLedger(campaign_id=CAMPAIGN, task_id=0)
            ))
        with self.assertRaises(TaskBudgetMalformedError):
            parse_ledger(ledger_bytes(
                BudgetLedger(campaign_id=CAMPAIGN, task_id=1)
            ).replace(b'"exhausted_reason":null',
                      b'"exhausted_reason":"mystery"'))
        with self.assertRaises(TaskBudgetMalformedError):
            parse_ledger(b" " * (MAX_LEDGER_BYTES + 1))


class LedgerPersistenceTest(unittest.TestCase):
    """The ledger publishes with no-replace byte-idempotent tamper handling."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="task-budget-ledger."))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        private = self.tmp / ".factory-state"
        private.mkdir(mode=0o700)

    def test_save_then_load_roundtrips(self) -> None:
        ledger = BudgetLedger(campaign_id=CAMPAIGN, task_id=1)
        ledger.record_attempt(
            wall_time_seconds=10.0, cpu_time_seconds=2.0,
            output_bytes=100, max_live_processes=3,
        )
        save_ledger(self.tmp, ledger)
        loaded = load_ledger(self.tmp, CAMPAIGN, 1)
        self.assertEqual(loaded.to_dict(), ledger.to_dict())

    def test_missing_ledger_loads_fresh_zero(self) -> None:
        ledger = load_ledger(self.tmp, CAMPAIGN, 1)
        self.assertEqual(ledger.wall_time_seconds, 0.0)
        self.assertIsNone(ledger.exhausted_reason)

    def test_byte_exact_republication_is_accepted(self) -> None:
        ledger = BudgetLedger(campaign_id=CAMPAIGN, task_id=1)
        save_ledger(self.tmp, ledger)
        # A byte-exact re-publication across a crash window is accepted.
        save_ledger(self.tmp, ledger)

    def test_monotonic_update_is_accepted(self) -> None:
        first = BudgetLedger(campaign_id=CAMPAIGN, task_id=1)
        first.record_attempt(
            wall_time_seconds=1.0, cpu_time_seconds=0.5,
            output_bytes=100, max_live_processes=2,
        )
        save_ledger(self.tmp, first)
        # The trusted supervisor's next attempt accumulates monotonically.
        updated = load_ledger(self.tmp, CAMPAIGN, 1)
        updated.record_attempt(
            wall_time_seconds=2.0, cpu_time_seconds=1.0,
            output_bytes=200, max_live_processes=3,
        )
        save_ledger(self.tmp, updated)
        loaded = load_ledger(self.tmp, CAMPAIGN, 1)
        self.assertEqual(loaded.wall_time_seconds, 3.0)
        self.assertEqual(loaded.cpu_time_seconds, 1.5)
        self.assertEqual(loaded.output_bytes, 300)
        self.assertEqual(loaded.max_live_processes, 3)

    def test_non_monotonic_update_fails_closed(self) -> None:
        first = BudgetLedger(campaign_id=CAMPAIGN, task_id=1)
        first.record_attempt(
            wall_time_seconds=10.0, cpu_time_seconds=0.0,
            output_bytes=0, max_live_processes=0,
        )
        save_ledger(self.tmp, first)
        # A forged ledger that *decreases* a cumulative dimension is a
        # tampered ledger and fails closed (never silently replaced).
        tampered = BudgetLedger(campaign_id=CAMPAIGN, task_id=1)
        tampered.record_attempt(
            wall_time_seconds=1.0, cpu_time_seconds=0.0,
            output_bytes=0, max_live_processes=0,
        )
        with self.assertRaises(TaskBudgetError) as caught:
            save_ledger(self.tmp, tampered)
        self.assertIn("not a monotonic predecessor", str(caught.exception))
        # The original ledger is untouched.
        loaded = load_ledger(self.tmp, CAMPAIGN, 1)
        self.assertEqual(loaded.wall_time_seconds, 10.0)

    def test_exhaustion_reason_is_first_wins_across_updates(self) -> None:
        first = BudgetLedger(campaign_id=CAMPAIGN, task_id=1)
        first.record_attempt(
            wall_time_seconds=1.0, cpu_time_seconds=0.0,
            output_bytes=0, max_live_processes=0,
        )
        first.mark_exhausted("wall_time")
        save_ledger(self.tmp, first)
        # A later update cannot change the recorded exhaustion reason.
        forged = load_ledger(self.tmp, CAMPAIGN, 1)
        forged.exhausted_reason = "cpu_time"
        with self.assertRaises(TaskBudgetError):
            save_ledger(self.tmp, forged)
        # A monotonic update that preserves the reason is accepted.
        preserved = load_ledger(self.tmp, CAMPAIGN, 1)
        preserved.record_attempt(
            wall_time_seconds=1.0, cpu_time_seconds=0.0,
            output_bytes=0, max_live_processes=0,
        )
        save_ledger(self.tmp, preserved)
        self.assertEqual(
            load_ledger(self.tmp, CAMPAIGN, 1).exhausted_reason, "wall_time"
        )

    def test_malformed_existing_ledger_fails_closed(self) -> None:
        from task_budget import ledger_name
        (self.tmp / ".factory-state" / ledger_name(CAMPAIGN, 1)).write_bytes(
            b"not a ledger"
        )
        with self.assertRaises(TaskBudgetError) as caught:
            save_ledger(self.tmp, BudgetLedger(campaign_id=CAMPAIGN, task_id=1))
        self.assertIn("malformed", str(caught.exception))

    def test_foreign_ledger_fails_closed(self) -> None:
        # A ledger file at the expected name that records a different
        # campaign/task is foreign and fails closed (never silently reset).
        foreign = BudgetLedger(campaign_id="other-campaign", task_id=1)
        from task_budget import ledger_name
        (self.tmp / ".factory-state" / ledger_name(CAMPAIGN, 1)).write_bytes(
            ledger_bytes(foreign)
        )
        with self.assertRaises(TaskBudgetError):
            load_ledger(self.tmp, CAMPAIGN, 1)
        with self.assertRaises(TaskBudgetError):
            save_ledger(self.tmp, BudgetLedger(campaign_id=CAMPAIGN, task_id=1))

    def test_ledger_name_is_campaign_and_task_bound(self) -> None:
        from task_budget import ledger_name
        self.assertEqual(
            ledger_name(CAMPAIGN, 1),
            f"task-budget-{CAMPAIGN}-task-1.json",
        )
        with self.assertRaises(TaskBudgetError):
            ledger_name("../escape", 1)
        with self.assertRaises(TaskBudgetError):
            ledger_name(CAMPAIGN, 0)


class CommitContextTest(unittest.TestCase):
    """validate_commit_context rejects mismatch/unresolvable/resolver failure."""

    COMMIT = "a" * 40

    def _artifact(self, **overrides):
        artifact = build_artifact(
            campaign_id=CAMPAIGN,
            phase="verification",
            commit=self.COMMIT,
            command=["./.factory/tools/verify-boilerplate.sh"],
            exit_status=1,
            expected="exit 0",
            observed="exit 1",
            environment_classification="clean",
            capability_classification="available",
            rerun_scope="targeted",
        )
        artifact.update(overrides)
        return artifact

    def test_matching_resolvable_commit_passes(self) -> None:
        validate_commit_context(
            self._artifact(), expected_commit=self.COMMIT,
            root=ROOT, resolver=lambda commit: True,
        )

    def test_commit_mismatch_fails_closed(self) -> None:
        with self.assertRaises(VerifierFailureMalformedError) as caught:
            validate_commit_context(
                self._artifact(commit="b" * 40),
                expected_commit=self.COMMIT, root=ROOT,
                resolver=lambda commit: True,
            )
        self.assertIn("does not match the expected bound commit", str(caught.exception))

    def test_unresolvable_commit_fails_closed(self) -> None:
        with self.assertRaises(VerifierFailureMalformedError) as caught:
            validate_commit_context(
                self._artifact(), expected_commit=self.COMMIT, root=ROOT,
                resolver=lambda commit: False,
            )
        self.assertIn("not resolvable", str(caught.exception))

    def test_resolver_failure_fails_closed(self) -> None:
        def broken(commit: str) -> bool:
            raise OSError("pinned git unavailable")

        with self.assertRaises(VerifierFailureError) as caught:
            validate_commit_context(
                self._artifact(), expected_commit=self.COMMIT, root=ROOT,
                resolver=broken,
            )
        self.assertIn("cannot probe repository resolvability", str(caught.exception))

    def test_malformed_expected_commit_fails_closed(self) -> None:
        with self.assertRaises(VerifierFailureMalformedError):
            validate_commit_context(
                self._artifact(), expected_commit="HEAD", root=ROOT,
                resolver=lambda commit: True,
            )

    def test_missing_resolver_requires_directory_root(self) -> None:
        with self.assertRaises(VerifierFailureError):
            validate_commit_context(
                self._artifact(), expected_commit=self.COMMIT,
                root=self.tmp_missing(),
            )

    def tmp_missing(self) -> Path:
        path = Path(tempfile.mkdtemp(prefix="commit-context."))
        shutil.rmtree(path)
        return path


if __name__ == "__main__":
    unittest.main()
