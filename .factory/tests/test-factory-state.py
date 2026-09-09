#!/usr/bin/env python3
"""Harness-owned conformance tests for the `factory-state/v1` control state (STATE-01).

This test lives under the hidden `.factory/tests/` namespace because the
specification (HIDE-01, §3) keeps harness-only tests out of the adopting
product's visible test tree. It is the deterministic verification for Task 4:

* the exact §11 field set, schema constant, and every structural invariant
  (identity, counters, terminal consistency, task/attempt bookkeeping, trusted
  outcome enum, digest shapes) fail closed against the committed adversarial
  `state-*.json` corpus;
* the §11 transition table is enforced edge-for-edge: legitimate transitions
  advance exactly as documented, retries and attempts obey their documented
  budgets, round finality and write-once bindings hold, and every illegal
  transition is rejected;
* the deterministic state digest is a function of the canonical model bytes
  and changes on any semantic mutation, and the before/after untrusted-phase
  ledger records it and fails closed on any drift;
* real atomic secure I/O is exercised in test-owned temporary Git
  repositories: mode/owner/link-count/pathname/identity tamper, forged or
  moved files, oversized or non-UTF-8 content, expected-binding mismatch,
  and counter rewind all fail closed, with the state file provably the only
  mutable lifecycle file (no global `/tmp` assertions);
* the trusted control-plane CLI prints exactly one machine-readable outcome
  and exits 0 only on a trusted transition (Task 4 CLI acceptance).
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
import unittest.mock as mock
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
LOOP = ROOT / ".factory" / "loop"
FIXTURES = ROOT / ".factory" / "tests" / "fixtures"
STATE_SCRIPT = LOOP / "state.py"

sys.path.insert(0, str(LOOP))
import state as state_module  # noqa: E402  (module object for internal race hooks)
from state import (  # noqa: E402
    AUDIT_ABORT_TARGETS,
    AUDIT_FINAL_TARGETS,
    BINDING_FIELDS,
    DIGEST_LEDGER_NAME,
    FIELD_NAMES,
    LEDGER_MAX,
    OUTCOMES,
    PHASES,
    PHASE_OUTCOMES,
    PHASE_VALUES,
    RETRY_OUTCOMES,
    SCHEMA_NAME,
    STATE_FILE_MAX,
    STATE_FILE_NAME,
    TERMINAL_PHASES,
    TRANSITIONS,
    FactoryState,
    StateBindingError,
    StateDigestError,
    StateError,
    StateIOError,
    StateTamperError,
    StateTransitionError,
    advance,
    atomic_write_json,
    begin_attempt,
    init_state,
    live_branch,
    load_state,
    owner_tamper_gate,
    parse_state,
    record_phase_digest,
    record_retry,
    recover_state,
    repository_identity,
    state_digest,
    verify_phase_digest,
    write_state,
)

# Canonical digest/commit values used by the fixtures and the runtime models.
SHA = "a" * 64
PLAN_SHA = "b" * 64
ROLE_SHA = "c" * 64
ROLE2_SHA = "d" * 64
AUDIT_SHA = "e" * 64
BASE_COMMIT = "f" * 40
MONOTONIC = 1700000000000000000
MONOTONIC2 = MONOTONIC + 1

# The committed corpus is listed verbatim so fixture-name drift fails closed.
VALID_FIXTURES = (
    "state-valid-initial.json",
    "state-valid-implementation.json",
    "state-valid-implementation-planned.json",
    "state-valid-planning-post-audit.json",
    "state-valid-retry-planning.json",
    "state-valid-verification.json",
    "state-valid-audit.json",
    "state-valid-final-round-audit.json",
    "state-valid-terminal-blocked.json",
    "state-valid-terminal-failed.json",
    "state-valid-terminal-findings.json",
    "state-valid-terminal-infrastructure-failure.json",
    "state-valid-terminal-interrupted.json",
    "state-valid-terminal-success.json",
)

# Defect fixture -> the exact fragment that proves the class was rejected.
TAMPER_FIXTURES = {
    "state-schema-wrong.json": "schema must be exactly",
    "state-schema-missing.json": "missing: ['schema']",
    "state-field-extra.json": "extra: ['model_memory']",
    "state-field-missing.json": "missing: ['current_phase']",
    "state-empty-object.json": "missing: [",
    "state-identity-malformed.json": "must match '^[0-9a-f]+:[0-9a-f]+$'",
    "state-identity-empty.json": "`repository_identity` must be a non-empty string",
    "state-branch-empty.json": "`branch` must be a non-empty string",
    "state-campaign-empty.json": "`campaign_id` must be a non-empty string",
    "state-rounds-requested-zero.json": "`rounds_requested` must be a positive integer",
    "state-rounds-requested-negative.json": "`rounds_requested` must be a positive integer",
    "state-rounds-requested-bool.json": "`rounds_requested` must be an integer",
    "state-current-round-zero.json": "round zero is reserved",
    "state-current-round-bool.json": "`current_round` must be an integer",
    "state-current-round-exceeds-requested.json": "`current_round` may not exceed",
    "state-phase-unknown.json": "`current_phase` must be one of",
    "state-attempt-number-negative.json": "`attempt_number` must be a non-negative integer",
    "state-attempt-number-bool.json": "`attempt_number` must be an integer",
    "state-phase-monotonic-negative.json": "positive monotonic marker",
    "state-phase-monotonic-zero.json": "positive monotonic marker",
    "state-attempt-monotonic-negative.json": "`attempt_started_at_monotonic` must be a non-negative integer",
    "state-attempt-before-phase.json": "can never precede",
    "state-outcome-phase-mismatch.json": "not a §13 outcome",
    "state-terminal-outcome-mismatch.json": "must record",
    "state-terminal-outcome-null.json": "must record",
    "state-task-without-attempt.json": "must have begun at least one attempt",
    "state-attempt-without-task.json": "`selected_task_id` must be present",
    "state-task-outside-implementation.json": "only during the implementation phase",
    "state-task-id-zero.json": "`selected_task_id` must be a positive integer",
    "state-task-id-bool.json": "`selected_task_id` must be a positive integer",
    "state-attempt-without-timestamp.json": "positive `attempt_started_at_monotonic`",
    "state-attempt-marker-without-attempt.json": "no attempt is active",
    "state-outcome-unknown.json": "trusted outcome enum",
    "state-outcome-number.json": "trusted outcome enum",
    "state-spec-digest-invalid.json": "`specification_digest` must match",
    "state-plan-digest-invalid.json": "`plan_digest` must match",
    "state-audit-digest-invalid.json": "`audit_objectives_digest` must match",
    "state-digest-uppercase.json": "`specification_digest` must match",
    "state-role-digest-invalid.json": "must map each role",
    "state-role-digest-empty-role.json": "must map each role",
    "state-role-digests-empty.json": "`role_prompt_digests` must be a non-empty mapping",
    "state-role-digests-not-object.json": "`role_prompt_digests` must be a JSON object",
    "state-phase-base-commit-invalid.json": "`phase_base_commit` must match",
    "state-phase-base-commit-uppercase.json": "`phase_base_commit` must match",
}

# Independent (S6) fixtures: the digest constants and the expected transition
# states are authored as fixed values in the corpus, never derived from the
# code path they exercise.
DIGEST_FIXTURES = (
    "state-digest-valid-initial.json",
    "state-digest-valid-implementation.json",
    "state-digest-valid-audit.json",
)

TRANSITION_FIXTURES = (
    "state-transition-planning-planned.json",
    "state-transition-planning-failed.json",
    "state-transition-implementation-completed.json",
    "state-transition-verification-pass.json",
    "state-transition-audit-nonfinal.json",
    "state-transition-audit-final.json",
)

UNSAFE_FIXTURES = (
    "state-unsafe-not-json.json",
    "state-unsafe-binary.json",
    "state-unsafe-oversized.json",
)

LEDGER_FIXTURES = (
    "state-ledger-malformed.jsonl",
    "state-ledger-repeated-tag.jsonl",
    "state-ledger-bad-tag.jsonl",
    "state-ledger-bad-digest.jsonl",
    "state-ledger-extra-key.jsonl",
    "state-ledger-empty-line.jsonl",
)

ALL_FIXTURES = sorted(
    VALID_FIXTURES
    + tuple(TAMPER_FIXTURES)
    + DIGEST_FIXTURES
    + TRANSITION_FIXTURES
    + UNSAFE_FIXTURES
    + LEDGER_FIXTURES
)

# The documented §11 advance edge set (audit finality is handled explicitly).
ADVANCE_EDGES = {
    ("planning", "planned"): "implementation",
    ("planning", "failed"): "failed",
    ("planning", "interrupted"): "interrupted",
    ("planning", "infrastructure_failure"): "infrastructure_failure",
    ("implementation", "task_completed"): "verification",
    ("implementation", "work_exhausted"): "verification",
    ("implementation", "blocked"): "verification",
    ("implementation", "task_failed"): "verification",
    ("implementation", "interrupted"): "interrupted",
    ("verification", "pass"): "audit",
    ("verification", "findings"): "audit",
    ("verification", "blocked"): "audit",
    ("verification", "software_verified_external_acceptance_blocked"): "audit",
    ("verification", "infrastructure_failure"): "infrastructure_failure",
    # Task 9 review B1: an interrupted audit and an untrusted audit are
    # terminal campaign ends with no nonfinal edge; the round never advances.
    ("audit", "interrupted"): "interrupted",
    ("audit", "infrastructure_failure"): "infrastructure_failure",
}
ALLOWED_ADVANCE_OUTCOMES = {
    "planning": ("planned", "failed", "interrupted", "infrastructure_failure"),
    "implementation": (
        "task_completed", "work_exhausted", "blocked", "task_failed",
        "interrupted",
    ),
    "verification": (
        "pass", "findings", "blocked", "infrastructure_failure",
        "software_verified_external_acceptance_blocked",
    ),
    "audit": ("pass", "findings", "blocked", "interrupted",
               "infrastructure_failure"),
}


def make_state(**overrides) -> FactoryState:
    """Build a parse-validated in-memory model with canonical defaults."""
    data = {
        "schema": SCHEMA_NAME,
        "repository_identity": "1a2b3c:4d5e6f",
        "branch": "boilerplate-develop",
        "campaign_id": "state-conformance",
        "rounds_requested": 2,
        "current_round": 1,
        "current_phase": "planning",
        "specification_digest": SHA,
        "plan_digest": PLAN_SHA,
        "role_prompt_digests": {"planner": ROLE_SHA},
        "audit_objectives_digest": AUDIT_SHA,
        "phase_base_commit": BASE_COMMIT,
        "selected_task_id": None,
        "attempt_number": 0,
        "phase_started_at_monotonic": MONOTONIC,
        "attempt_started_at_monotonic": 0,
        "last_outcome": None,
    }
    data.update(overrides)
    return parse_state(data)


def implementation_state() -> FactoryState:
    return make_state(
        current_phase="implementation",
        selected_task_id=4,
        attempt_number=1,
        attempt_started_at_monotonic=MONOTONIC,
        last_outcome="task_progress",
    )


class StateConformanceCase(unittest.TestCase):
    """Shared helpers: test-owned temporary repositories and seeding."""

    def new_repo(self, branch: str = "boilerplate-develop") -> Path:
        tmp = tempfile.TemporaryDirectory(prefix="state-conformance-")
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        subprocess.run(["git", "init", "-q", "-b", branch, str(root)], check=True)
        subprocess.run(
            [
                "git", "-C", str(root), "-c", "user.name=state-test",
                "-c", "user.email=state-test@example.invalid",
                "commit", "--allow-empty", "-q", "-m", "test repo root",
            ],
            check=True,
        )
        return root

    def _assert_private(self, path: Path) -> None:
        info = os.stat(path, follow_symlinks=False)
        self.assertTrue(stat.S_ISREG(info.st_mode))
        self.assertEqual(info.st_uid, os.getuid())
        self.assertEqual(info.st_nlink, 1)
        self.assertEqual(info.st_mode & 0o077, 0)

    def init_campaign(
        self,
        root: Path,
        *,
        rounds: int = 2,
        campaign_id: str = "state-conformance",
        branch: str = "boilerplate-develop",
    ) -> FactoryState:
        return init_state(
            root,
            campaign_id=campaign_id,
            rounds_requested=rounds,
            specification_digest=SHA,
            plan_digest=PLAN_SHA,
            role_prompt_digests={"planner": ROLE_SHA, "developer": ROLE2_SHA},
            audit_objectives_digest=AUDIT_SHA,
            phase_base_commit=BASE_COMMIT,
            branch=branch,
            now=MONOTONIC,
        )

    def seed(self, root: Path, name: str, data: bytes) -> None:
        """Write raw bytes into the private directory (mode 0600, no-follow)."""
        private = root / ".factory-state"
        private.mkdir(mode=0o700, exist_ok=True)
        descriptor = os.open(
            private / name,
            os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW,
            0o600,
        )
        try:
            os.write(descriptor, data)
        finally:
            os.close(descriptor)

    def cli(self, root: Path, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(STATE_SCRIPT), "--root", str(root), *args],
            capture_output=True,
            text=True,
        )

    def fixture(self, name: str) -> Path:
        path = FIXTURES / name
        self.assertTrue(path.is_file(), f"missing fixture {path}")
        return path

    def set_hook(self, hook_name: str, hook) -> None:
        """Install a module-private race hook and guarantee it is cleared.

        The hooks are one-shot (consumed and cleared immediately before the
        operation they perturb), so a fired hook leaves no residue; the
        cleanup here is defense in depth so a test that raises before the
        hook fires can never leak a stale hook into a later test.
        """
        setattr(state_module, hook_name, hook)
        self.addCleanup(lambda: setattr(state_module, hook_name, None))


class FixtureCorpusTest(StateConformanceCase):
    """The committed state-* corpus is exact and behaves as documented."""

    def test_corpus_is_exhaustive(self) -> None:
        committed = sorted(p.name for p in FIXTURES.glob("state-*"))
        self.assertEqual(committed, ALL_FIXTURES)

    def test_every_valid_fixture_parses_and_roundtrips(self) -> None:
        for name in VALID_FIXTURES:
            with self.subTest(fixture=name):
                data = json.loads(self.fixture(name).read_text("utf-8"))
                state = parse_state(data)
                self.assertEqual(parse_state(state.to_dict()), state)

    def test_every_tamper_fixture_fails_closed(self) -> None:
        for name, fragment in TAMPER_FIXTURES.items():
            with self.subTest(fixture=name, fragment=fragment):
                data = json.loads(self.fixture(name).read_text("utf-8"))
                with self.assertRaises(StateTamperError) as caught:
                    parse_state(data)
                self.assertIn(
                    fragment, str(caught.exception),
                    f"{name}: expected {fragment!r} in {str(caught.exception)!r}",
                )

    def test_unsafe_content_seeds_have_the_documented_shapes(self) -> None:
        not_json = self.fixture("state-unsafe-not-json.json").read_bytes()
        self.assertFalse(not_json.lstrip().startswith(b"{"))
        binary = self.fixture("state-unsafe-binary.json").read_bytes()
        self.assertTrue(binary.startswith(b"\xff\xfe"))
        oversized = self.fixture("state-unsafe-oversized.json")
        self.assertGreater(oversized.stat().st_size, STATE_FILE_MAX)

    def test_ledger_fixtures_are_jsonl(self) -> None:
        for name in LEDGER_FIXTURES:
            raw = self.fixture(name).read_text("utf-8")
            self.assertIn("\n", raw)
            self.assertFalse(raw.endswith("\n\n"))


class FieldSetTest(StateConformanceCase):
    """Exactly the §11 field set is accepted; no other field may appear."""

    def test_field_names_are_exactly_documented(self) -> None:
        self.assertEqual(
            FIELD_NAMES,
            (
                "schema", "repository_identity", "branch", "campaign_id",
                "rounds_requested", "current_round", "current_phase",
                "specification_digest", "plan_digest", "role_prompt_digests",
                "audit_objectives_digest", "phase_base_commit",
                "selected_task_id", "attempt_number",
                "phase_started_at_monotonic", "attempt_started_at_monotonic",
                "last_outcome",
            ),
        )
        self.assertEqual(len(FIELD_NAMES), 17)

    def test_extra_field_is_rejected(self) -> None:
        data = json.loads(self.fixture("state-field-extra.json").read_text("utf-8"))
        with self.assertRaises(StateTamperError) as caught:
            parse_state(data)
        self.assertIn("extra: ['model_memory']", str(caught.exception))

    def test_missing_field_is_rejected(self) -> None:
        data = json.loads(self.fixture("state-field-missing.json").read_text("utf-8"))
        with self.assertRaises(StateTamperError) as caught:
            parse_state(data)
        self.assertIn("missing: ['current_phase']", str(caught.exception))

    def test_empty_object_is_rejected(self) -> None:
        with self.assertRaises(StateTamperError) as caught:
            parse_state({})
        self.assertIn("missing", str(caught.exception))

    def test_role_prompt_digests_must_be_an_object(self) -> None:
        data = json.loads(
            self.fixture("state-role-digests-not-object.json").read_text("utf-8")
        )
        with self.assertRaises(StateTamperError) as caught:
            parse_state(data)
        self.assertIn("JSON object", str(caught.exception))


class SchemaTest(StateConformanceCase):
    """The schema constant is bound exactly once and never changes."""

    def test_wrong_schema_is_rejected(self) -> None:
        data = json.loads(self.fixture("state-schema-wrong.json").read_text("utf-8"))
        with self.assertRaises(StateTamperError) as caught:
            parse_state(data)
        self.assertIn("schema must be exactly", str(caught.exception))

    def test_missing_schema_is_rejected(self) -> None:
        data = json.loads(self.fixture("state-schema-missing.json").read_text("utf-8"))
        with self.assertRaises(StateTamperError) as caught:
            parse_state(data)
        self.assertIn("missing: ['schema']", str(caught.exception))


class IdentityTest(StateConformanceCase):
    """repository_identity is a strict dev:inode hex pair."""

    def test_malformed_identity_is_rejected(self) -> None:
        data = json.loads(
            self.fixture("state-identity-malformed.json").read_text("utf-8")
        )
        with self.assertRaises(StateTamperError) as caught:
            parse_state(data)
        self.assertIn("must match", str(caught.exception))

    def test_empty_identity_is_rejected(self) -> None:
        data = json.loads(self.fixture("state-identity-empty.json").read_text("utf-8"))
        with self.assertRaises(StateTamperError) as caught:
            parse_state(data)
        self.assertIn("non-empty", str(caught.exception))


class CounterTest(StateConformanceCase):
    """Round, attempt, and monotonic counters stay within their bounds."""

    def test_rounds_requested_invariants(self) -> None:
        for name in (
            "state-rounds-requested-zero.json",
            "state-rounds-requested-negative.json",
        ):
            with self.subTest(fixture=name):
                data = json.loads(self.fixture(name).read_text("utf-8"))
                with self.assertRaisesRegex(StateTamperError, "positive integer"):
                    parse_state(data)
        data = json.loads(
            self.fixture("state-rounds-requested-bool.json").read_text("utf-8")
        )
        with self.assertRaisesRegex(StateTamperError, "must be an integer"):
            parse_state(data)

    def test_current_round_invariants(self) -> None:
        with self.assertRaisesRegex(StateTamperError, "round zero is reserved"):
            parse_state(json.loads(
                self.fixture("state-current-round-zero.json").read_text("utf-8")
            ))
        with self.assertRaisesRegex(StateTamperError, "must be an integer"):
            parse_state(json.loads(
                self.fixture("state-current-round-bool.json").read_text("utf-8")
            ))
        with self.assertRaisesRegex(StateTamperError, "may not exceed"):
            parse_state(json.loads(
                self.fixture("state-current-round-exceeds-requested.json")
                .read_text("utf-8")
            ))

    def test_attempt_and_monotonic_invariants(self) -> None:
        with self.assertRaisesRegex(StateTamperError, "non-negative integer"):
            parse_state(json.loads(
                self.fixture("state-attempt-number-negative.json").read_text("utf-8")
            ))
        with self.assertRaisesRegex(StateTamperError, "must be an integer"):
            parse_state(json.loads(
                self.fixture("state-attempt-number-bool.json").read_text("utf-8")
            ))
        for name in (
            "state-phase-monotonic-negative.json",
            "state-phase-monotonic-zero.json",
        ):
            with self.subTest(fixture=name):
                with self.assertRaisesRegex(StateTamperError, "positive monotonic"):
                    parse_state(json.loads(
                        self.fixture(name).read_text("utf-8")
                    ))
        with self.assertRaisesRegex(StateTamperError, "non-negative integer"):
            parse_state(json.loads(
                self.fixture("state-attempt-monotonic-negative.json")
                .read_text("utf-8")
            ))

    def test_phase_outcome_enum_values_are_phase_scoped(self) -> None:
        """Task 19 S9: `last_outcome` must belong to the owning phase's §13 set."""
        mismatch = json.loads(
            self.fixture("state-outcome-phase-mismatch.json").read_text("utf-8")
        )
        with self.assertRaisesRegex(StateTamperError, "not a §13 outcome"):
            parse_state(mismatch)
        # The per-phase outcome sets are exact and documented. Planning also
        # carries the prior round's trusted audit outcome on a nonfinal edge.
        self.assertEqual(
            PHASE_OUTCOMES["planning"], frozenset(
                {"interrupted", "pass", "findings", "blocked"}
            )
        )
        self.assertEqual(
            PHASE_OUTCOMES["implementation"], frozenset(
                {"planned", "task_progress", "task_failed", "interrupted"}
            )
        )
        self.assertEqual(
            PHASE_OUTCOMES["verification"], frozenset(
                {"task_completed", "work_exhausted", "blocked", "task_failed"}
            )
        )
        self.assertEqual(
            PHASE_OUTCOMES["audit"], frozenset(
                {"pass", "findings", "blocked",
                 "software_verified_external_acceptance_blocked"}
            )
        )
        # `last_outcome` is null only during a fresh planning phase.
        null_verification = json.loads(
            self.fixture("state-valid-verification.json").read_text("utf-8")
        )
        null_verification["last_outcome"] = None
        with self.assertRaisesRegex(StateTamperError, "null only during the planning"):
            parse_state(null_verification)

    def test_phase_enum_is_exact(self) -> None:
        self.assertEqual(PHASES, ("planning", "implementation", "verification", "audit"))
        self.assertEqual(
            TERMINAL_PHASES,
            (
                "success", "findings", "blocked", "failed", "interrupted",
                "infrastructure_failure",
            ),
        )
        self.assertEqual(
            PHASE_VALUES,
            (
                "planning", "implementation", "verification", "audit",
                "success", "findings", "blocked", "failed", "interrupted",
                "infrastructure_failure",
            ),
        )


class TerminalStateTest(StateConformanceCase):
    """Terminal states record exactly their own phase as last_outcome."""

    def test_terminal_requires_matching_outcome(self) -> None:
        for name in (
            "state-terminal-outcome-mismatch.json",
            "state-terminal-outcome-null.json",
        ):
            with self.subTest(fixture=name):
                data = json.loads(self.fixture(name).read_text("utf-8"))
                with self.assertRaises(StateTamperError) as caught:
                    parse_state(data)
                self.assertIn("must record", str(caught.exception))

    def test_every_terminal_state_validates(self) -> None:
        for name in VALID_FIXTURES:
            if "terminal" not in name:
                continue
            with self.subTest(fixture=name):
                data = json.loads(self.fixture(name).read_text("utf-8"))
                state = parse_state(data)
                self.assertEqual(state.current_phase, state.last_outcome)

    def test_terminal_accepts_no_transition(self) -> None:
        for terminal in TERMINAL_PHASES:
            state = make_state(current_phase=terminal, last_outcome=terminal)
            for outcome in OUTCOMES:
                with self.subTest(terminal=terminal, outcome=outcome):
                    with self.assertRaises(StateTransitionError) as caught:
                        advance(state, outcome, now=MONOTONIC2)
                    self.assertIn("accepts no transition", str(caught.exception))


class TaskAttemptTest(StateConformanceCase):
    """Task selection and attempt counters stay internally consistent."""

    def test_task_requires_begun_attempt(self) -> None:
        data = json.loads(
            self.fixture("state-task-without-attempt.json").read_text("utf-8")
        )
        with self.assertRaisesRegex(StateTamperError, "must have begun"):
            parse_state(data)

    def test_attempt_requires_selected_task(self) -> None:
        data = json.loads(
            self.fixture("state-attempt-without-task.json").read_text("utf-8")
        )
        with self.assertRaisesRegex(StateTamperError, "must be present"):
            parse_state(data)

    def test_task_only_inside_implementation(self) -> None:
        data = json.loads(
            self.fixture("state-task-outside-implementation.json").read_text("utf-8")
        )
        with self.assertRaisesRegex(StateTamperError, "only during the implementation"):
            parse_state(data)

    def test_task_id_must_be_positive(self) -> None:
        for name in ("state-task-id-zero.json", "state-task-id-bool.json"):
            with self.subTest(fixture=name):
                data = json.loads(self.fixture(name).read_text("utf-8"))
                with self.assertRaisesRegex(
                    StateTamperError, "positive integer"
                ):
                    parse_state(data)

    def test_active_attempt_requires_timestamp(self) -> None:
        data = json.loads(
            self.fixture("state-attempt-without-timestamp.json").read_text("utf-8")
        )
        with self.assertRaisesRegex(StateTamperError, "attempt_started_at_monotonic"):
            parse_state(data)

    def test_attempt_marker_is_zero_without_an_active_attempt(self) -> None:
        """Task 19 S9 inverse marker invariant: no attempt => marker zero.

        A positive ``attempt_started_at_monotonic`` while ``attempt_number``
        is zero is a forged marker and fails closed with the exact invariant
        message (the committed ``state-attempt-marker-without-attempt.json``
        fixture is authored independently from this test's expectations).
        """
        data = json.loads(
            self.fixture("state-attempt-marker-without-attempt.json")
            .read_text("utf-8")
        )
        with self.assertRaisesRegex(StateTamperError, "no attempt is active"):
            parse_state(data)

    def test_valid_implementation_fixture_is_accepted(self) -> None:
        data = json.loads(
            self.fixture("state-valid-implementation.json").read_text("utf-8")
        )
        state = parse_state(data)
        self.assertEqual(state.selected_task_id, 4)
        self.assertEqual(state.attempt_number, 2)


class OutcomeTest(StateConformanceCase):
    """last_outcome is exactly the trusted enum, or null before first outcome."""

    def test_untrusted_outcomes_are_rejected(self) -> None:
        for name in ("state-outcome-unknown.json", "state-outcome-number.json"):
            with self.subTest(fixture=name):
                data = json.loads(self.fixture(name).read_text("utf-8"))
                with self.assertRaisesRegex(StateTamperError, "trusted outcome"):
                    parse_state(data)

    def test_outcome_enum_is_exact(self) -> None:
        self.assertEqual(
            OUTCOMES,
            (
                "planned", "failed", "interrupted",
                "task_completed", "task_progress", "task_failed",
                "work_exhausted", "blocked",
                "pass", "findings", "infrastructure_failure",
                "software_verified_external_acceptance_blocked",
                "success",
            ),
        )

    def test_null_is_valid_before_first_outcome(self) -> None:
        state = parse_state(json.loads(
            self.fixture("state-valid-initial.json").read_text("utf-8")
        ))
        self.assertIsNone(state.last_outcome)


class DigestDeterminismTest(StateConformanceCase):
    """state_digest is canonical bytes: deterministic, semantic, mutation-sensitive."""

    def test_digest_is_deterministic(self) -> None:
        data = json.loads(self.fixture("state-valid-initial.json").read_text("utf-8"))
        first = state_digest(parse_state(data))
        second = state_digest(parse_state(data))
        self.assertEqual(first, second)

    def test_digest_is_the_canonical_json_sha256(self) -> None:
        state = parse_state(json.loads(
            self.fixture("state-valid-initial.json").read_text("utf-8")
        ))
        canonical = json.dumps(
            state.to_dict(), sort_keys=True, separators=(",", ":"), ensure_ascii=True
        ).encode("utf-8")
        self.assertEqual(state_digest(state), hashlib.sha256(canonical).hexdigest())

    def test_whitespace_variance_does_not_change_the_digest(self) -> None:
        data = json.loads(self.fixture("state-valid-initial.json").read_text("utf-8"))
        pretty = json.dumps(data, indent=4, sort_keys=True)
        compact = json.dumps(data, sort_keys=True, separators=(",", ":"))
        self.assertEqual(
            state_digest(parse_state(json.loads(pretty))),
            state_digest(parse_state(json.loads(compact))),
        )

    def test_every_semantic_mutation_changes_the_digest(self) -> None:
        base = make_state()
        base_digest = state_digest(base)
        variants = {
            "branch": make_state(branch="other-branch"),
            "campaign_id": make_state(campaign_id="other-campaign"),
            "rounds_requested": make_state(rounds_requested=3),
            "current_round": make_state(current_round=2, rounds_requested=3),
            "current_phase": make_state(
                current_phase="verification", last_outcome="task_completed"
            ),
            "specification_digest": make_state(specification_digest="0" * 64),
            "plan_digest": make_state(plan_digest="0" * 64),
            "audit_objectives_digest": make_state(audit_objectives_digest="0" * 64),
            "role_prompt_digests": make_state(
                role_prompt_digests={"planner": "0" * 64}
            ),
            "phase_base_commit": make_state(phase_base_commit="0" * 40),
            "phase_started_at_monotonic": make_state(
                phase_started_at_monotonic=MONOTONIC + 7
            ),
            "phase_and_outcome": make_state(
                current_phase="audit", last_outcome="pass"
            ),
            "attempt_binding": make_state(
                current_phase="implementation", selected_task_id=4,
                attempt_number=1, attempt_started_at_monotonic=MONOTONIC,
                last_outcome="task_progress",
            ),
        }
        self.assertEqual(
            len({state_digest(v) for v in variants.values()}),
            len(variants),
            "each semantic mutation must produce a distinct digest",
        )
        for name, variant in variants.items():
            with self.subTest(mutation=name):
                self.assertNotEqual(state_digest(variant), base_digest)


class TransitionTableTest(StateConformanceCase):
    """The §11 transition table holds edge for edge."""

    def _source(self, phase: str) -> FactoryState:
        if phase == "planning":
            return make_state()
        if phase == "implementation":
            return implementation_state()
        if phase == "verification":
            return make_state(current_phase="verification", last_outcome="task_completed")
        if phase == "audit":
            return make_state(current_phase="audit", last_outcome="pass")
        raise AssertionError(phase)

    def test_every_s11_edge_advances_exactly(self) -> None:
        for (phase, outcome), target in ADVANCE_EDGES.items():
            with self.subTest(phase=phase, outcome=outcome):
                kwargs: dict = {"now": MONOTONIC2}
                if (phase, outcome) == ("planning", "planned"):
                    kwargs["plan_digest"] = "0" * 64
                    kwargs["phase_base_commit"] = "0" * 40
                source = self._source(phase)
                result = advance(source, outcome, **kwargs)
                self.assertEqual(result.current_phase, target)
                if target in TERMINAL_PHASES:
                    self.assertEqual(result.last_outcome, target)
                else:
                    self.assertEqual(result.last_outcome, outcome)
                self.assertNotEqual(result.phase_started_at_monotonic, MONOTONIC)

    def test_transition_table_matches_the_documented_edge_set(self) -> None:
        self.assertEqual(len(TRANSITIONS), len(ADVANCE_EDGES))
        for key, target in ADVANCE_EDGES.items():
            if key[0] != "audit":
                self.assertEqual(TRANSITIONS[key], target)

    def test_audit_finality_resolves_from_round_budget(self) -> None:
        nonfinal = make_state(
            current_phase="audit", last_outcome="pass", current_round=1,
            rounds_requested=2,
        )
        result = advance(nonfinal, "pass", now=MONOTONIC2)
        self.assertEqual(result.current_phase, "planning")
        self.assertEqual(result.current_round, 2)
        self.assertEqual(result.last_outcome, "pass")

        final = make_state(
            current_phase="audit", last_outcome="pass", current_round=1,
            rounds_requested=1,
        )
        for outcome, terminal in AUDIT_FINAL_TARGETS.items():
            with self.subTest(final_outcome=outcome):
                result = advance(final, outcome, now=MONOTONIC2)
                self.assertEqual(result.current_phase, terminal)
                self.assertEqual(result.last_outcome, terminal)
                self.assertEqual(result.current_round, 1)

    def test_audit_accepts_only_final_or_abort_outcomes(self) -> None:
        # Task 9 review B1: only the two final/abort outcome families close
        # an audit — ``pass | findings | blocked`` (round-finality resolved)
        # and the terminal aborts ``interrupted`` / ``infrastructure_failure``
        # (no nonfinal edge, the round never advances).  Every other trusted
        # outcome is not an audit edge and fails closed.
        audit = make_state(current_phase="audit", last_outcome="pass")
        for outcome in ("task_completed", "planned", "work_exhausted"):
            with self.subTest(outcome=outcome):
                with self.assertRaisesRegex(
                    StateTransitionError, "no §11 audit transition"
                ):
                    advance(audit, outcome)
        for outcome, terminal in AUDIT_ABORT_TARGETS.items():
            with self.subTest(abort_outcome=outcome):
                result = advance(audit, outcome, now=MONOTONIC2)
                self.assertEqual(result.current_phase, terminal)
                self.assertEqual(result.last_outcome, terminal)
                # An abort close never advances the round.
                self.assertEqual(result.current_round, audit.current_round)

    def test_audit_abort_edges_are_documented_and_terminal(self) -> None:
        # The two audit abort edges are part of the documented advance edge
        # set, terminal, and never advance a nonfinal round.
        self.assertEqual(
            ADVANCE_EDGES[("audit", "interrupted")], "interrupted")
        self.assertEqual(
            ADVANCE_EDGES[("audit", "infrastructure_failure")],
            "infrastructure_failure",
        )
        nonfinal = make_state(
            current_phase="audit", last_outcome="pass", current_round=1,
            rounds_requested=3,
        )
        for outcome in AUDIT_ABORT_TARGETS:
            result = advance(nonfinal, outcome, now=MONOTONIC2)
            self.assertIn(result.current_phase, TERMINAL_PHASES)
            self.assertEqual(result.current_round, 1)
        # A terminal abort state validates (the persisted form is coherent).
        for outcome, terminal in AUDIT_ABORT_TARGETS.items():
            result = advance(nonfinal, outcome, now=MONOTONIC2)
            parse_state(result.to_dict())
            self.assertEqual(result.to_dict()["last_outcome"], terminal)

    def test_illegal_transitions_are_rejected(self) -> None:
        for phase in ("planning", "implementation", "verification", "audit"):
            for outcome in OUTCOMES:
                if outcome in ALLOWED_ADVANCE_OUTCOMES[phase]:
                    continue
                with self.subTest(phase=phase, outcome=outcome):
                    with self.assertRaises(StateTransitionError) as caught:
                        advance(self._source(phase), outcome, now=MONOTONIC2)
                    self.assertIn("no §11", str(caught.exception))

    def test_plan_binding_is_required_and_single_use(self) -> None:
        planning = make_state()
        with self.assertRaisesRegex(
            StateTransitionError, "requires the bound plan_digest"
        ):
            advance(planning, "planned", now=1)
        bound = advance(
            planning, "planned", plan_digest="0" * 64,
            phase_base_commit="0" * 40, now=1,
        )
        self.assertEqual(bound.plan_digest, "0" * 64)
        # The next planning round must bind again, not carry the previous one.
        round_two = advance(
            make_state(), "planned", plan_digest="1" * 64,
            phase_base_commit="1" * 40, now=2,
        )
        self.assertEqual(round_two.plan_digest, "1" * 64)

    def test_plan_binding_rejected_outside_the_edge(self) -> None:
        verification = make_state(
            current_phase="verification", last_outcome="task_completed"
        )
        with self.assertRaisesRegex(
            StateTransitionError, "bind only on the"
        ):
            advance(verification, "pass", plan_digest="0" * 64, now=1)

    def test_now_must_be_a_positive_marker(self) -> None:
        kwargs = dict(plan_digest="0" * 64, phase_base_commit="0" * 40)
        with self.assertRaisesRegex(StateTamperError, "positive monotonic"):
            advance(make_state(), "planned", now=-1, **kwargs)
        with self.assertRaisesRegex(StateTamperError, "epoch marker"):
            advance(make_state(), "planned", now=0, **kwargs)

    def test_begin_attempt_rejects_now_zero_and_coupling(self) -> None:
        """S3: epoch-zero markers are tamper; S9: attempt >= owning phase."""
        state = advance(
            make_state(), "planned", plan_digest="0" * 64,
            phase_base_commit="0" * 40, now=100,
        )
        with self.assertRaisesRegex(StateTamperError, "epoch marker"):
            begin_attempt(state, 4, now=0)
        with self.assertRaisesRegex(StateTamperError, "can never precede"):
            begin_attempt(state, 4, now=1)
        began = begin_attempt(state, 4, now=100)
        self.assertEqual(began.attempt_started_at_monotonic, 100)

    def test_round_increments_only_on_audit_nonfinal(self) -> None:
        state = make_state()
        state = advance(state, "planned", plan_digest="0" * 64,
                        phase_base_commit="0" * 40, now=1)
        state = advance(state, "task_completed", now=2)
        state = advance(state, "pass", now=3)
        self.assertEqual(state.current_round, 1, "no increment before audit")
        state = advance(state, "pass", now=4)
        self.assertEqual(state.current_round, 2, "audit nonfinal increments once")
        state = advance(state, "failed", now=5)
        self.assertEqual(state.current_phase, "failed")

    def test_write_once_bindings_survive_a_full_campaign(self) -> None:
        state = make_state()
        bindings = {name: getattr(state, name) for name in BINDING_FIELDS}
        steps = (
            dict(outcome="planned", plan_digest="0" * 64,
                 phase_base_commit="0" * 40),
            dict(outcome="task_completed"),
            dict(outcome="pass"),
            dict(outcome="pass"),   # round 2 planning
            dict(outcome="planned", plan_digest="1" * 64,
                 phase_base_commit="1" * 40),
            dict(outcome="task_completed"),
            dict(outcome="pass"),
            dict(outcome="pass"),   # final -> success
        )
        for step in steps:
            kwargs = dict(step)
            outcome = kwargs.pop("outcome")
            state = advance(state, outcome, now=MONOTONIC2, **kwargs)
        self.assertEqual(state.current_phase, "success")
        for name, value in bindings.items():
            with self.subTest(binding=name):
                self.assertEqual(getattr(state, name), value)

    def test_advance_resets_task_bookkeeping_on_phase_change(self) -> None:
        state = advance(
            implementation_state(), "task_completed", now=MONOTONIC2
        )
        self.assertEqual(state.current_phase, "verification")
        self.assertIsNone(state.selected_task_id)
        self.assertEqual(state.attempt_number, 0)
        self.assertEqual(state.attempt_started_at_monotonic, 0)


class SoftwareVerifiedExternalAcceptanceBlockedTest(StateConformanceCase):
    """The verification outcome ``software_verified_external_acceptance_blocked``.

    Software fully verified while external release acceptance remains blocked:
    the outcome advances to the independent audit exactly like
    ``pass``/``findings``/``blocked``, but it can never produce campaign
    success — an audit ``pass`` entered from it resolves to the terminal
    ``blocked`` state in the final round and to the next round's ``planning``
    in a non-final round.  It never weakens round-zero readiness,
    infrastructure-failure fail-closed closes, or human authority.
    """

    OUTCOME = "software_verified_external_acceptance_blocked"

    def _verification(self) -> FactoryState:
        return make_state(
            current_phase="verification", last_outcome="task_completed"
        )

    def test_verification_advances_to_audit(self) -> None:
        state = advance(self._verification(), self.OUTCOME, now=MONOTONIC2)
        self.assertEqual(state.current_phase, "audit")
        self.assertEqual(state.last_outcome, self.OUTCOME)
        # The audit-phase state persists the verification outcome and
        # re-validates (the persisted form is coherent).
        parse_state(state.to_dict())

    def test_audit_pass_never_produces_success_in_final_round(self) -> None:
        final = make_state(
            current_phase="audit", last_outcome=self.OUTCOME,
            current_round=1, rounds_requested=1,
        )
        result = advance(final, "pass", now=MONOTONIC2)
        self.assertEqual(result.current_phase, "blocked")
        self.assertEqual(result.last_outcome, "blocked")
        self.assertNotEqual(result.current_phase, "success")

    def test_audit_pass_advances_nonfinal_round(self) -> None:
        nonfinal = make_state(
            current_phase="audit", last_outcome=self.OUTCOME,
            current_round=1, rounds_requested=2,
        )
        result = advance(nonfinal, "pass", now=MONOTONIC2)
        self.assertEqual(result.current_phase, "planning")
        self.assertEqual(result.current_round, 2)
        self.assertEqual(result.last_outcome, "pass")

    def test_audit_findings_and_blocked_keep_their_terminals(self) -> None:
        final = make_state(
            current_phase="audit", last_outcome=self.OUTCOME,
            current_round=1, rounds_requested=1,
        )
        for outcome, terminal in (("findings", "findings"), ("blocked", "blocked")):
            with self.subTest(audit_outcome=outcome):
                result = advance(final, outcome, now=MONOTONIC2)
                self.assertEqual(result.current_phase, terminal)
                self.assertEqual(result.last_outcome, terminal)

    def test_audit_abort_edges_stay_terminal(self) -> None:
        final = make_state(
            current_phase="audit", last_outcome=self.OUTCOME,
            current_round=1, rounds_requested=1,
        )
        for outcome, terminal in AUDIT_ABORT_TARGETS.items():
            with self.subTest(abort_outcome=outcome):
                result = advance(final, outcome, now=MONOTONIC2)
                self.assertEqual(result.current_phase, terminal)
                self.assertEqual(result.current_round, 1)

    def test_plain_audit_pass_still_produces_success(self) -> None:
        # Backward compatibility: an audit entered from a normal verification
        # ``pass`` still resolves to ``success`` in the final round.
        final = make_state(
            current_phase="audit", last_outcome="pass",
            current_round=1, rounds_requested=1,
        )
        result = advance(final, "pass", now=MONOTONIC2)
        self.assertEqual(result.current_phase, "success")

    def test_outcome_is_rejected_outside_verification(self) -> None:
        for phase in ("planning", "implementation", "audit"):
            with self.subTest(phase=phase):
                source = self._source(phase)
                with self.assertRaises(StateTransitionError) as caught:
                    advance(source, self.OUTCOME, now=MONOTONIC2)
                self.assertIn("no §11", str(caught.exception))

    def _source(self, phase: str) -> FactoryState:
        if phase == "planning":
            return make_state()
        if phase == "implementation":
            return implementation_state()
        if phase == "audit":
            return make_state(current_phase="audit", last_outcome="pass")
        raise AssertionError(phase)

    def test_legacy_v2_migration_remains_compatible(self) -> None:
        # A legacy factory-state/v2 document migrates to the canonical v1
        # field set; the new outcome is a v1-only extension and never appears
        # in a legacy document, so migration stays byte-compatible.
        legacy = {
            "schema": "factory-state/v2",
            "repository_identity": "1a2b3c:4d5e6f",
            "branch": "boilerplate-develop",
            "campaign_id": "state-conformance",
            "rounds_requested": 2,
            "current_round": 1,
            "current_phase": "planning",
            "specification_digest": SHA,
            "plan_digest": PLAN_SHA,
            "role_prompt_digests": {"planner": ROLE_SHA},
            "audit_objectives_digest": AUDIT_SHA,
            "pre_round_hook_configuration_digest": "0" * 64,
            "pre_round_hook_commit": "0" * 40,
            "pre_round_hook_results_digest": "0" * 64,
            "pre_round_hook_started_round": 0,
            "pre_round_hook_completed_round": 0,
            "phase_base_commit": BASE_COMMIT,
            "selected_task_id": None,
            "attempt_number": 0,
            "phase_started_at_monotonic": MONOTONIC,
            "attempt_started_at_monotonic": 0,
            "last_outcome": None,
        }
        migrated = state_module.migrate_offline_state(legacy)
        self.assertEqual(migrated["schema"], SCHEMA_NAME)
        self.assertNotIn("pre_round_hook_configuration_digest", migrated)
        canonical = {k: v for k, v in migrated.items() if k != "_sidecars"}
        parse_state(canonical)

    def test_legacy_v2_audit_state_still_parses_after_migration(self) -> None:
        # A legacy v2 document already in the audit phase migrates and the
        # canonical v1 parser accepts the result (backward compatibility).
        legacy = {
            "schema": "factory-state/v2",
            "repository_identity": "1a2b3c:4d5e6f",
            "branch": "boilerplate-develop",
            "campaign_id": "state-conformance",
            "rounds_requested": 1,
            "current_round": 1,
            "current_phase": "audit",
            "specification_digest": SHA,
            "plan_digest": PLAN_SHA,
            "role_prompt_digests": {"planner": ROLE_SHA},
            "audit_objectives_digest": AUDIT_SHA,
            "pre_round_hook_configuration_digest": "0" * 64,
            "pre_round_hook_commit": "0" * 40,
            "pre_round_hook_results_digest": "0" * 64,
            "pre_round_hook_started_round": 0,
            "pre_round_hook_completed_round": 0,
            "phase_base_commit": BASE_COMMIT,
            "selected_task_id": None,
            "attempt_number": 0,
            "phase_started_at_monotonic": MONOTONIC,
            "attempt_started_at_monotonic": 0,
            "last_outcome": "pass",
        }
        migrated = state_module.migrate_offline_state(legacy)
        canonical = {k: v for k, v in migrated.items() if k != "_sidecars"}
        state = parse_state(canonical)
        self.assertEqual(state.current_phase, "audit")
        self.assertEqual(state.last_outcome, "pass")

    def test_full_campaign_with_external_blocker_ends_blocked(self) -> None:
        # A complete campaign whose verification reports the new outcome can
        # never end in success: the final audit pass resolves to blocked.
        state = make_state(rounds_requested=1)
        state = advance(
            state, "planned", plan_digest="0" * 64,
            phase_base_commit="0" * 40, now=1,
        )
        state = advance(state, "task_completed", now=2)
        state = advance(state, self.OUTCOME, now=3)
        self.assertEqual(state.current_phase, "audit")
        state = advance(state, "pass", now=4)
        self.assertEqual(state.current_phase, "blocked")
        self.assertEqual(state.last_outcome, "blocked")
        self.assertNotEqual(state.current_phase, "success")


class RetryAndAttemptTest(StateConformanceCase):
    """Retries stay inside the documented budgets; attempts stay monotonic."""

    def test_record_retry_changes_only_last_outcome(self) -> None:
        before = implementation_state()
        after = record_retry(before, "task_failed")
        self.assertEqual(after.last_outcome, "task_failed")
        for name in FIELD_NAMES:
            if name == "last_outcome":
                continue
            with self.subTest(field=name):
                self.assertEqual(getattr(before, name), getattr(after, name))

    def test_retry_is_rejected_where_not_documented(self) -> None:
        cases = (
            (make_state(), "task_progress"),                  # planning
            (make_state(current_phase="verification",
                        last_outcome="task_completed"), "interrupted"),
            (make_state(current_phase="audit", last_outcome="pass"), "planned"),
            (implementation_state(), "pass"),
            (make_state(current_phase="success", last_outcome="success"),
             "interrupted"),
        )
        for state, outcome in cases:
            with self.subTest(phase=state.current_phase, outcome=outcome):
                with self.assertRaises(StateTransitionError):
                    record_retry(state, outcome)

    def test_retry_outcome_table_is_exact(self) -> None:
        self.assertEqual(RETRY_OUTCOMES["planning"], ("interrupted",))
        self.assertEqual(
            RETRY_OUTCOMES["implementation"],
            ("task_progress", "task_failed", "interrupted"),
        )

    def test_begin_attempt_increments_within_the_same_task(self) -> None:
        state = advance(
            make_state(), "planned", plan_digest="0" * 64,
            phase_base_commit="0" * 40, now=1,
        )
        first = begin_attempt(state, 4, now=10)
        second = begin_attempt(first, 4, now=20)
        third = begin_attempt(second, 4, now=30)
        self.assertEqual(
            (first.attempt_number, second.attempt_number, third.attempt_number),
            (1, 2, 3),
        )
        self.assertEqual(third.selected_task_id, 4)
        self.assertEqual(third.attempt_started_at_monotonic, 30)

    def test_begin_attempt_restarts_on_a_trusted_task_transition(self) -> None:
        state = begin_attempt(make_state(
            current_phase="implementation", last_outcome="task_progress",
            phase_started_at_monotonic=MONOTONIC,
        ), 4, now=MONOTONIC)
        state = begin_attempt(state, 4, now=MONOTONIC + 1)
        self.assertEqual(state.attempt_number, 2)
        switched = begin_attempt(state, 7, now=MONOTONIC + 2)
        self.assertEqual(switched.selected_task_id, 7)
        self.assertEqual(switched.attempt_number, 1)

    def test_begin_attempt_requires_implementation_phase(self) -> None:
        with self.assertRaisesRegex(StateTransitionError, "implementation phase"):
            begin_attempt(make_state(), 4)

    def test_begin_attempt_requires_positive_task_id(self) -> None:
        state = implementation_state()
        for task_id in (0, -1, True):
            with self.subTest(task_id=task_id):
                with self.assertRaises(StateTransitionError):
                    begin_attempt(state, task_id)

    def test_counters_never_move_backward(self) -> None:
        now = 0
        state = make_state()
        seen = []
        for outcome in ("planned", "task_completed", "pass", "pass"):
            now += 100
            state = advance(state, outcome, now=now, **(
                {"plan_digest": "0" * 64, "phase_base_commit": "0" * 40}
                if state.current_phase == "planning" and outcome == "planned"
                else {}
            ))
            seen.append(state.phase_started_at_monotonic)
        self.assertEqual(seen, sorted(seen))
        self.assertEqual(seen[-1], now)


class SecureIoTamperTest(StateConformanceCase):
    """Real atomic secure I/O: every metadata/content tamper fails closed."""

    def test_init_publishes_one_private_atomic_file(self) -> None:
        root = self.new_repo()
        # The committed initial fixture is byte-equivalent to a real init:
        # only the canonical root identity is substituted.
        data = json.loads(self.fixture("state-valid-initial.json").read_text("utf-8"))
        state = init_state(
            root,
            campaign_id=data["campaign_id"],
            rounds_requested=data["rounds_requested"],
            specification_digest=data["specification_digest"],
            plan_digest=data["plan_digest"],
            role_prompt_digests=data["role_prompt_digests"],
            audit_objectives_digest=data["audit_objectives_digest"],
            phase_base_commit=data["phase_base_commit"],
            branch=data["branch"],
            now=data["phase_started_at_monotonic"],
        )
        directory = root / ".factory-state"
        info = os.stat(directory, follow_symlinks=False)
        self.assertTrue(stat.S_ISDIR(info.st_mode))
        self.assertEqual(info.st_mode & 0o077, 0)
        self.assertEqual(sorted(p.name for p in directory.iterdir()), [STATE_FILE_NAME])
        state_file = directory / STATE_FILE_NAME
        self._assert_private(state_file)
        self.assertEqual(load_state(root), state)
        data["repository_identity"] = state.repository_identity
        self.assertEqual(state.to_dict(), parse_state(data).to_dict())

    def test_state_file_is_the_only_mutable_lifecycle_file(self) -> None:
        root = self.new_repo()
        self.init_campaign(root)
        directory = root / ".factory-state"
        self.assertEqual(sorted(p.name for p in directory.iterdir()), [STATE_FILE_NAME])
        record_phase_digest(root, "round-1-planning")
        self.assertEqual(
            sorted(p.name for p in directory.iterdir()),
            [STATE_FILE_NAME, DIGEST_LEDGER_NAME],
        )
        # A full campaign walk writes through the real atomic path and leaves
        # no temporary or quarantine artifact behind.
        state = load_state(root)
        steps = (
            dict(outcome="planned", plan_digest="0" * 64,
                 phase_base_commit="0" * 40),
            dict(outcome="task_completed"),
            dict(outcome="pass"),
            dict(outcome="pass"),  # round 2 planning
        )
        now = 0
        for step in steps:
            now += 1
            kwargs = dict(step)
            outcome = kwargs.pop("outcome")
            state = advance(state, outcome, now=now, **kwargs)
            write_state(root, state)
            self.assertFalse(
                [p for p in directory.iterdir() if p.name.startswith(".")],
                "atomic publication must leave no temporary inode",
            )
        # Re-publishing an unchanged model is still atomic and leaves no trash.
        for _ in range(3):
            write_state(root, load_state(root))
        self.assertEqual(
            sorted(p.name for p in directory.iterdir()),
            [STATE_FILE_NAME, DIGEST_LEDGER_NAME],
        )

    def test_init_refuses_to_overwrite_existing_state(self) -> None:
        root = self.new_repo()
        self.init_campaign(root)
        with self.assertRaisesRegex(StateError, "refusing to overwrite"):
            self.init_campaign(root)

    def test_mode_tamper_fails_closed(self) -> None:
        root = self.new_repo()
        self.init_campaign(root)
        state_file = root / ".factory-state" / STATE_FILE_NAME
        os.chmod(state_file, 0o666)
        with self.assertRaises(StateTamperError):
            load_state(root)

    def test_owner_tamper_fails_closed(self) -> None:
        """The owner probe runs and asserts the gate; never skips (Task 19 S7).

        :func:`owner_tamper_gate` performs a real ``chown(2)`` and declares
        what the kernel actually allows.  A privileged run can change file
        ownership and really exercises the owner-tamper fail-closed path; an
        unprivileged run cannot, and the gate reports the ownership check
        unavailable with a fail-closed reason.  Either way the probe RUNS and
        asserts the gate — it never silently skips and never falsely claims
        owner-tamper coverage when the kernel refuses the ownership change.
        """
        root = self.new_repo()
        self.init_campaign(root)
        gate = owner_tamper_gate(root)
        if not gate["available"]:
            # The kernel refused the ownership change: the owner check is
            # genuinely unavailable in this process.  Assert the gate declared
            # the unavailability with a fail-closed reason, and do NOT claim
            # owner-tamper coverage.
            self.assertTrue(
                gate["reason"], "an unavailable owner gate must declare a reason"
            )
            self.assertIn(
                "owner", gate["reason"].lower(),
                "an unavailable owner gate must name the owner check",
            )
            return
        self.assertIsNone(gate["reason"])
        # The kernel honors ownership changes: exercise the real owner tamper
        # and assert the state reader fails closed on the foreign owner.
        state_file = root / ".factory-state" / STATE_FILE_NAME
        target = 65534 if os.getuid() != 65534 else 65533
        os.chown(state_file, target, -1)
        with self.assertRaises(StateTamperError):
            load_state(root)

    def test_owner_rejection_is_always_exercised_with_wrong_expected_uid(
        self,
    ) -> None:
        """Task 19 S7: the owner-rejection branch always runs in this test.

        ``load_state``'s internal ``_expected_uid`` defaults to the current
        user in production; a wrong expected UID makes the real ``stat``
        owner metadata fail the exact production owner check with no
        ``chown`` required, so this test exercises the owner-rejection branch
        deterministically in any environment (root or unprivileged).  It
        complements — never replaces — the honest real-chown capability gate,
        and claims no real-system owner-tamper coverage.  The knob is
        underscore-private: the trusted CLI cannot set it.
        """
        root = self.new_repo()
        self.init_campaign(root)
        wrong = os.getuid() + 100000
        self.assertNotEqual(wrong, os.getuid())
        with self.assertRaises(StateTamperError):
            load_state(root, _expected_uid=wrong)
        # The campaign is untouched: default owner validation still loads.
        self.assertEqual(load_state(root).campaign_id, "state-conformance")

    def test_link_count_tamper_fails_closed(self) -> None:
        root = self.new_repo()
        self.init_campaign(root)
        directory = root / ".factory-state"
        os.link(directory / STATE_FILE_NAME, directory / "extra-hardlink")
        with self.assertRaises(StateTamperError):
            load_state(root)

    def test_symlinked_state_fails_closed(self) -> None:
        root = self.new_repo()
        self.init_campaign(root)
        directory = root / ".factory-state"
        (directory / STATE_FILE_NAME).unlink()
        os.symlink(STATE_FILE_NAME + ".real", directory / STATE_FILE_NAME)
        (directory / (STATE_FILE_NAME + ".real")).write_text("{}")
        with self.assertRaises(StateTamperError):
            load_state(root)

    def test_symlinked_directory_fails_closed(self) -> None:
        root = self.new_repo()
        self.init_campaign(root)
        private = root / ".factory-state"
        elsewhere = root / "elsewhere"
        elsewhere.mkdir()
        shutil.rmtree(private)
        os.symlink("elsewhere", private)
        with self.assertRaises(StateTamperError):
            load_state(root)
        # A campaign may never clobber through the symlink: init fails closed.
        # Recovery now refuses an unsafe existing state directory (Task 19
        # S2) before any write attempt, so the fail-closed error is the
        # recovery refusal rather than a deeper StateIOError.
        with self.assertRaisesRegex(StateError, "unsafe existing state directory"):
            self.init_campaign(root, campaign_id="clobber")

    def test_world_writable_directory_fails_closed(self) -> None:
        root = self.new_repo()
        self.init_campaign(root)
        os.chmod(root / ".factory-state", 0o755)
        with self.assertRaises(StateTamperError):
            load_state(root)

    def test_oversized_state_fails_closed(self) -> None:
        root = self.new_repo()
        self.init_campaign(root)
        self.seed(root, STATE_FILE_NAME,
                  self.fixture("state-unsafe-oversized.json").read_bytes())
        with self.assertRaises(StateTamperError):
            load_state(root)

    def test_non_json_state_fails_closed(self) -> None:
        root = self.new_repo()
        self.init_campaign(root)
        self.seed(root, STATE_FILE_NAME,
                  self.fixture("state-unsafe-not-json.json").read_bytes())
        with self.assertRaises(StateTamperError) as caught:
            load_state(root)
        self.assertIn("JSON", str(caught.exception))

    def test_invalid_utf8_state_fails_closed(self) -> None:
        root = self.new_repo()
        self.init_campaign(root)
        self.seed(root, STATE_FILE_NAME,
                  self.fixture("state-unsafe-binary.json").read_bytes())
        with self.assertRaises(StateTamperError):
            load_state(root)

    def test_state_moved_between_repositories_fails_closed(self) -> None:
        root_a = self.new_repo()
        root_b = self.new_repo()
        self.init_campaign(root_a)
        shutil.copytree(root_a / ".factory-state", root_b / ".factory-state")
        with self.assertRaisesRegex(StateTamperError, "repository_identity"):
            load_state(root_b)

    def test_expected_binding_mismatch_fails_closed(self) -> None:
        root = self.new_repo()
        self.init_campaign(root, rounds=2)
        wrong = {
            "expected_branch": "other-branch",
            "expected_campaign_id": "other-campaign",
            "expected_rounds_requested": 3,
            "expected_specification_digest": "0" * 64,
            "expected_plan_digest": "0" * 64,
            "expected_audit_objectives_digest": "0" * 64,
            "expected_role_prompt_digests": {"planner": "0" * 64},
        }
        for name, value in wrong.items():
            with self.subTest(binding=name):
                with self.assertRaises(StateBindingError):
                    load_state(root, **{name: value})
        # Matching expectations load cleanly.
        self.assertEqual(
            load_state(root, expected_campaign_id="state-conformance"),
            load_state(root),
        )

    def test_rewound_round_counter_is_detected(self) -> None:
        root = self.new_repo()
        self.init_campaign(root, rounds=2)
        state = load_state(root)
        state = advance(state, "planned", plan_digest="0" * 64,
                        phase_base_commit="0" * 40, now=1)
        state = advance(state, "task_completed", now=2)
        state = advance(state, "pass", now=3)
        state = advance(state, "pass", now=4)  # round 2 planning
        write_state(root, state)
        record_phase_digest(root, "round-2-planning")
        forged = parse_state({**state.to_dict(), "current_round": 1})
        write_state(root, forged)
        with self.assertRaises(StateDigestError) as caught:
            verify_phase_digest(root, "round-2-planning")
        self.assertIn("changed during", str(caught.exception))

    def test_forged_binding_is_detected_by_digest(self) -> None:
        root = self.new_repo()
        self.init_campaign(root)
        record_phase_digest(root, "round-1-planning")
        state = load_state(root)
        forged = parse_state({**state.to_dict(), "branch": "other-branch"})
        write_state(root, forged)
        with self.assertRaises(StateDigestError):
            verify_phase_digest(root, "round-1-planning")

    def test_write_state_rejects_invalid_model_before_any_io(self) -> None:
        root = self.new_repo()
        self.init_campaign(root)
        before = (root / ".factory-state" / STATE_FILE_NAME).read_bytes()
        from dataclasses import replace

        invalid = replace(load_state(root), rounds_requested=0)
        with self.assertRaises(StateTamperError):
            write_state(root, invalid)
        self.assertEqual(
            (root / ".factory-state" / STATE_FILE_NAME).read_bytes(), before
        )

    def test_live_branch_is_resolved_when_unspecified(self) -> None:
        root = self.new_repo()
        state = init_state(
            root,
            campaign_id="state-conformance",
            rounds_requested=2,
            specification_digest=SHA,
            plan_digest=PLAN_SHA,
            role_prompt_digests={"planner": ROLE_SHA},
            audit_objectives_digest=AUDIT_SHA,
            phase_base_commit=BASE_COMMIT,
            now=MONOTONIC,
        )
        self.assertEqual(state.branch, "boilerplate-develop")
        self.assertEqual(
            state.repository_identity, repository_identity(root)
        )

    def test_live_branch_git_call_is_finite_bounded(self) -> None:
        # Task 9 review MED: the trusted live-branch resolution is finite
        # bounded — the pinned Git executable may never wait forever.
        root = self.new_repo()
        recorded: list = []
        real_run = state_module._git.git_run

        def recording_run(argv, *, timeout=None, **kwargs):
            recorded.append(timeout)
            return real_run(argv, timeout=timeout, **kwargs)

        with mock.patch.object(
            state_module._git, "git_run", side_effect=recording_run,
        ):
            self.assertEqual(live_branch(root), "boilerplate-develop")
        self.assertEqual(len(recorded), 1)
        self.assertIsNotNone(recorded[0])
        self.assertGreater(recorded[0], 0)
        self.assertLessEqual(recorded[0], state_module._git.GIT_TIMEOUT)

    def test_live_branch_git_boundary_error_is_a_state_error(self) -> None:
        # The pinned-Git failure (timeout / missing binary / broken pipe) is
        # routed into the state contract; no bare GitBoundaryError escapes.
        root = self.new_repo()
        with mock.patch.object(
            state_module._git, "git_run",
            side_effect=state_module._git.GitBoundaryError("pinned Git hung"),
        ):
            with self.assertRaisesRegex(StateError, "pinned Git hung"):
                live_branch(root)


class LedgerTest(StateConformanceCase):
    """Before/after untrusted-phase digest ledger is append-only and exact."""

    def test_record_and_verify_roundtrip(self) -> None:
        root = self.new_repo()
        self.init_campaign(root)
        recorded = record_phase_digest(root, "round-1-planning")
        self.assertEqual(recorded, state_digest(load_state(root)))
        self.assertEqual(verify_phase_digest(root, "round-1-planning"), recorded)

    def test_verify_without_record_fails_closed(self) -> None:
        root = self.new_repo()
        self.init_campaign(root)
        with self.assertRaisesRegex(StateDigestError, "no recorded digest"):
            verify_phase_digest(root, "round-1-planning")

    def test_repeated_tag_fails_closed(self) -> None:
        root = self.new_repo()
        self.init_campaign(root)
        record_phase_digest(root, "round-1-planning")
        with self.assertRaisesRegex(StateDigestError, "repeats phase tag"):
            record_phase_digest(root, "round-1-planning")

    def test_unsafe_tag_fails_closed(self) -> None:
        root = self.new_repo()
        self.init_campaign(root)
        for tag in ("../escape", "has space", "-leading-dash", "tag\ninjected"):
            with self.subTest(tag=tag):
                with self.assertRaisesRegex(StateDigestError, "unsafe phase tag"):
                    record_phase_digest(root, tag)

    def test_semantic_drift_between_record_and_verify_fails(self) -> None:
        root = self.new_repo()
        self.init_campaign(root)
        record_phase_digest(root, "round-1-planning")
        state = load_state(root)
        forged = parse_state({**state.to_dict(), "last_outcome": "interrupted"})
        write_state(root, forged)
        with self.assertRaisesRegex(StateDigestError, "changed during"):
            verify_phase_digest(root, "round-1-planning")

    def test_every_ledger_fixture_fails_closed(self) -> None:
        for name in LEDGER_FIXTURES:
            with self.subTest(fixture=name):
                root = self.new_repo()
                self.init_campaign(root)
                self.seed(root, DIGEST_LEDGER_NAME,
                          self.fixture(name).read_bytes())
                with self.assertRaises(StateDigestError):
                    verify_phase_digest(root, "round-1-planning")

    def test_ledger_is_private_append_only_evidence(self) -> None:
        root = self.new_repo()
        self.init_campaign(root)
        record_phase_digest(root, "round-1-planning")
        ledger = root / ".factory-state" / DIGEST_LEDGER_NAME
        self._assert_private(ledger)
        lines = ledger.read_text("utf-8").splitlines()
        self.assertEqual(len(lines), 1)
        record = json.loads(lines[0])
        self.assertEqual(set(record), {"tag", "digest"})
        self.assertEqual(record["tag"], "round-1-planning")
        # The ledger is evidence, never orchestration state: the state file is
        # byte-identical to its model and the ledger adds no field to it.
        self.assertEqual(state_digest(load_state(root)), record["digest"])

    def test_oversized_ledger_fails_closed(self) -> None:
        root = self.new_repo()
        self.init_campaign(root)
        self.seed(root, DIGEST_LEDGER_NAME, b"x" * (LEDGER_MAX + 1))
        with self.assertRaises(StateDigestError):
            verify_phase_digest(root, "round-1-planning")

    def test_ledger_replacement_is_detected(self) -> None:
        root = self.new_repo()
        self.init_campaign(root)
        record_phase_digest(root, "round-1-planning")
        ledger = root / ".factory-state" / DIGEST_LEDGER_NAME
        os.link(ledger, ledger.with_name("second-link"))
        with self.assertRaises(StateDigestError):
            record_phase_digest(root, "round-2-planning")

    def test_concurrent_same_tag_duplicate_allows_at_most_one_success(
        self,
    ) -> None:
        """Task 19 hardening L5: when a concurrent writer lands the *same*
        tag on the same ledger inode between the duplicate-tag check and the
        append, the second writer fails closed — at most one same-tag writer
        can ever succeed — and the resulting ledger stays a valid unique-tag
        sequence (never left with a repeated tag).
        """
        root = self.new_repo()
        self.init_campaign(root)
        record_phase_digest(root, "round-1-planning")

        def concurrent_writer(_root, directory_fd):
            line = json.dumps(
                {"tag": "round-2-planning", "digest": "0" * 64},
                sort_keys=True, separators=(",", ":"),
            ).encode() + b"\n"
            fd = os.open(
                DIGEST_LEDGER_NAME,
                os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0),
                0o600,
                dir_fd=directory_fd,
            )
            os.write(fd, line)
            os.close(fd)

        self.set_hook("_LEDGER_RACE_AFTER_DUP_CHECK", concurrent_writer)
        with self.assertRaisesRegex(StateDigestError, "repeats phase tag"):
            record_phase_digest(root, "round-2-planning")
        ledger = (root / ".factory-state" / DIGEST_LEDGER_NAME).read_text("utf-8")
        tags = [json.loads(line)["tag"] for line in ledger.splitlines()]
        self.assertEqual(tags.count("round-2-planning"), 1)

    def test_ledger_inode_swap_before_append_fails_closed(self) -> None:
        """Task 19 hardening L4: swapping the ledger to a different inode
        between the duplicate-tag check and the append fails closed — the
        append is bound to the exact inode that was checked, never a
        substituted ledger.
        """
        root = self.new_repo()
        self.init_campaign(root)
        record_phase_digest(root, "round-1-planning")

        def swap(_root, directory_fd):
            os.unlink(DIGEST_LEDGER_NAME, dir_fd=directory_fd)
            fd = os.open(
                DIGEST_LEDGER_NAME,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
                dir_fd=directory_fd,
            )
            os.write(fd, b'{"tag":"round-2-planning","digest":"' + b"0" * 64 + b'"}\n')
            os.close(fd)

        self.set_hook("_LEDGER_RACE_AFTER_DUP_CHECK", swap)
        with self.assertRaisesRegex(
            StateDigestError, "was replaced between the duplicate-tag check"
        ):
            record_phase_digest(root, "round-2-planning")


class IndependentFixtureTest(StateConformanceCase):
    """S6: transition and digest fixtures are authored independently.

    The expected transition states and the digest constants below are fixed
    values committed in the corpus, written by hand from the documented §11
    table and the canonical digest encoding; they are never derived from the
    code path they exercise at test time, so a later drift in ``advance`` or
    ``state_digest`` fails these fixtures instead of being masked by a
    self-derived expectation.
    """

    def test_digest_fixtures_are_fixed_constants(self) -> None:
        for name in DIGEST_FIXTURES:
            with self.subTest(fixture=name):
                data = json.loads(self.fixture(name).read_text("utf-8"))
                self.assertEqual(
                    set(data), {"digest_sha256", "state"},
                    "digest fixtures carry exactly a state and its fixed digest",
                )
                self.assertRegex(data["digest_sha256"], r"^[0-9a-f]{64}$")
                state = parse_state(data["state"])
                self.assertEqual(state_digest(state), data["digest_sha256"])
                canonical = json.dumps(
                    state.to_dict(), sort_keys=True,
                    separators=(",", ":"), ensure_ascii=True,
                ).encode("utf-8")
                self.assertEqual(
                    hashlib.sha256(canonical).hexdigest(),
                    data["digest_sha256"],
                )

    def test_transition_fixtures_advance_exactly(self) -> None:
        for name in TRANSITION_FIXTURES:
            with self.subTest(fixture=name):
                data = json.loads(self.fixture(name).read_text("utf-8"))
                state = parse_state(data["input"])
                kwargs: dict = {"now": data["now"]}
                if "plan_digest" in data:
                    kwargs["plan_digest"] = data["plan_digest"]
                if "phase_base_commit" in data:
                    kwargs["phase_base_commit"] = data["phase_base_commit"]
                result = advance(state, data["outcome"], **kwargs)
                migrated_expected = parse_state(data["expected"]).to_dict()
                self.assertEqual(
                    result.to_dict(), migrated_expected,
                    f"{name}: advance did not produce the hand-authored state",
                )
                # The hand-authored legacy expected state migrates to a valid
                # hook-bound state without changing lifecycle semantics.
                self.assertEqual(parse_state(migrated_expected).to_dict(), migrated_expected)

    def test_digest_fixtures_cover_three_lifecycle_shapes(self) -> None:
        self.assertEqual(len(DIGEST_FIXTURES), 3)
        for name in DIGEST_FIXTURES:
            data = json.loads(self.fixture(name).read_text("utf-8"))
            parse_state(data["state"])


class InitNoReplaceTest(StateConformanceCase):
    """S1: init is atomic with no-replace semantics."""

    def test_init_never_clobbers_existing_state(self) -> None:
        root = self.new_repo()
        self.init_campaign(root)
        before = (root / ".factory-state" / STATE_FILE_NAME).read_bytes()
        with self.assertRaisesRegex(StateError, "refusing to overwrite"):
            self.init_campaign(root, campaign_id="other-campaign")
        after = (root / ".factory-state" / STATE_FILE_NAME).read_bytes()
        self.assertEqual(after, before)
        # The failed init leaves no temporary or quarantine artifact behind.
        directory = root / ".factory-state"
        self.assertEqual(
            sorted(p.name for p in directory.iterdir()), [STATE_FILE_NAME]
        )

    def test_init_refuses_a_prior_campaign_ledger_binding(self) -> None:
        root = self.new_repo()
        self.init_campaign(root)
        record_phase_digest(root, "round-1-planning")
        (root / ".factory-state" / STATE_FILE_NAME).unlink()
        with self.assertRaisesRegex(StateError, "second campaign binding"):
            self.init_campaign(root, campaign_id="other")

    def test_underlying_publication_is_no_replace(self) -> None:
        root = self.new_repo()
        directory = root / ".factory-state"
        directory.mkdir(mode=0o700)
        atomic_write_json(
            root, STATE_FILE_NAME, {"schema": SCHEMA_NAME}, no_replace=True
        )
        with self.assertRaises(StateIOError):
            atomic_write_json(
                root, STATE_FILE_NAME, {"schema": "forged"}, no_replace=True
            )
        self.assertEqual(
            json.loads((directory / STATE_FILE_NAME).read_text("utf-8")),
            {"schema": SCHEMA_NAME},
        )


class RecoveryTest(StateConformanceCase):
    """S2: deterministic crash-window and orphan recovery."""

    def _orphan(self, root: Path, suffix: str) -> Path:
        name = f".{STATE_FILE_NAME}.{suffix}"
        self.seed(root, name, b"torn write")
        return root / ".factory-state" / name

    def test_recover_removes_validated_orphan_temporary(self) -> None:
        root = self.new_repo()
        orphan = self._orphan(root, "0" * 32)
        summary = recover_state(root)
        self.assertEqual(summary["status"], "clean")
        self.assertEqual(summary["removed"], 1)
        self.assertIsNone(summary["restored"])
        self.assertFalse(orphan.exists())
        directory = root / ".factory-state"
        self.assertEqual(sorted(p.name for p in directory.iterdir()), [])

    def test_recover_restores_quarantined_state_without_data_loss(self) -> None:
        root = self.new_repo()
        self.init_campaign(root)
        state = load_state(root)
        quarantine = f".{STATE_FILE_NAME}.quarantine-" + "a" * 32
        (root / ".factory-state" / STATE_FILE_NAME).rename(
            root / ".factory-state" / quarantine
        )
        summary = recover_state(root)
        self.assertEqual(summary["status"], "restored")
        self.assertEqual(summary["restored"], STATE_FILE_NAME)
        self.assertEqual(load_state(root), state, "no durable state is lost")
        directory = root / ".factory-state"
        self.assertEqual(
            sorted(p.name for p in directory.iterdir()), [STATE_FILE_NAME]
        )

    def test_recover_keeps_canonical_and_cleans_leftovers(self) -> None:
        root = self.new_repo()
        self.init_campaign(root)
        before = load_state(root)
        orphan = self._orphan(root, "b" * 32)
        quarantine = f".{STATE_FILE_NAME}.quarantine-" + "c" * 32
        shutil.copy2(root / ".factory-state" / STATE_FILE_NAME,
                     root / ".factory-state" / quarantine)
        summary = recover_state(root)
        self.assertEqual(summary["status"], "clean")
        self.assertEqual(summary["removed"], 2)
        self.assertEqual(load_state(root), before)
        self.assertFalse(orphan.exists())
        self.assertFalse((root / ".factory-state" / quarantine).exists())

    def test_recover_fails_closed_on_multiple_quarantines(self) -> None:
        root = self.new_repo()
        self.init_campaign(root)
        q1 = f".{STATE_FILE_NAME}.quarantine-" + "d" * 32
        q2 = f".{STATE_FILE_NAME}.quarantine-" + "e" * 32
        (root / ".factory-state" / STATE_FILE_NAME).rename(
            root / ".factory-state" / q1
        )
        shutil.copy2(root / ".factory-state" / q1, root / ".factory-state" / q2)
        with self.assertRaisesRegex(StateError, "multiple quarantined"):
            recover_state(root)

    def test_recover_fails_closed_on_unsafe_orphan(self) -> None:
        root = self.new_repo()
        self.init_campaign(root)
        orphan = self._orphan(root, "f" * 32)
        os.chmod(orphan, 0o644)
        with self.assertRaisesRegex(StateError, "unsafe orphaned"):
            recover_state(root)
        self.assertTrue(orphan.exists())

    def test_recover_fails_closed_on_foreign_quarantine(self) -> None:
        root = self.new_repo()
        other = self.new_repo()
        self.init_campaign(other)
        quarantine = f".{STATE_FILE_NAME}.quarantine-" + "9" * 32
        self.seed(root, quarantine,
                  (other / ".factory-state" / STATE_FILE_NAME).read_bytes())
        with self.assertRaisesRegex(StateError, "does not match the canonical root"):
            recover_state(root)

    def test_recover_reports_clean_only_when_directory_is_truly_absent(self) -> None:
        """Task 19 S2: ``clean`` requires no private directory at all.

        The pre-existing fresh-root test proves a truly absent directory is
        clean; the following unsafe-existing-directory tests prove that an
        existing symlink, plain file, wrong-mode, or foreign-owned directory
        fails closed instead of being reported clean.
        """
        root = self.new_repo()
        self.init_campaign(root)
        private = root / ".factory-state"

        # A symlinked private directory (even a dangling one) fails closed.
        shutil.rmtree(private)
        os.symlink("elsewhere", private)
        with self.assertRaisesRegex(StateError, "unsafe existing state directory"):
            recover_state(root)

        # A plain file where the directory must be fails closed.
        os.unlink(private)
        private.write_text("not a directory", encoding="utf-8")
        with self.assertRaisesRegex(StateError, "unsafe existing state directory"):
            recover_state(root)

        # A world-accessible existing directory fails closed.
        private.unlink()
        private.mkdir(mode=0o755)
        with self.assertRaisesRegex(StateError, "unsafe existing state directory"):
            recover_state(root)

        # A foreign-owned directory fails closed through the internal owner
        # expectation (real stat metadata + wrong expected UID; no chown).
        with self.assertRaisesRegex(StateError, "unsafe existing state directory"):
            recover_state(root, _expected_uid=os.getuid() + 100000)

    def test_recover_restores_quarantine_matching_the_ledger_digest(self) -> None:
        """Task 19 S2: a ledger-bound restore must match the latest digest.

        The ledger records the state digest before the crash; the quarantined
        state is byte-identical to it, so recovery restores it, re-validates
        the canonical in place, and only then deletes the quarantine.
        """
        root = self.new_repo()
        self.init_campaign(root)
        state = load_state(root)
        record_phase_digest(root, "round-1-planning")
        ledger = (root / ".factory-state" / DIGEST_LEDGER_NAME).read_text("utf-8")
        latest = json.loads(ledger.splitlines()[-1])["digest"]
        self.assertEqual(latest, state_digest(state))
        quarantine = f".{STATE_FILE_NAME}.quarantine-" + "a" * 32
        (root / ".factory-state" / STATE_FILE_NAME).rename(
            root / ".factory-state" / quarantine
        )
        summary = recover_state(root)
        self.assertEqual(summary["status"], "restored")
        self.assertEqual(summary["restored"], STATE_FILE_NAME)
        restored = load_state(root)
        self.assertEqual(restored, state, "no durable state is lost")
        self.assertEqual(state_digest(restored), latest)
        directory = root / ".factory-state"
        self.assertEqual(
            sorted(p.name for p in directory.iterdir()),
            [STATE_FILE_NAME, DIGEST_LEDGER_NAME],
        )

    def test_recover_fails_closed_when_quarantine_digest_mismatches_ledger(
        self,
    ) -> None:
        """Task 19 S2 tamper test: a tampered quarantine is never restored.

        The quarantined state was recorded in the ledger before the crash; an
        attacker mutates the quarantined content afterwards, so the recovered
        digest no longer matches the latest recorded ledger entry and recovery
        fails closed, leaving the quarantine untouched for inspection.
        """
        root = self.new_repo()
        self.init_campaign(root)
        record_phase_digest(root, "round-1-planning")
        quarantine = f".{STATE_FILE_NAME}.quarantine-" + "c" * 32
        (root / ".factory-state" / STATE_FILE_NAME).rename(
            root / ".factory-state" / quarantine
        )
        # The tampered state is still a structurally valid §11 state whose
        # repository_identity matches the canonical root, so recovery reaches
        # the ledger digest check and fails closed exactly there.
        original = json.loads(
            (root / ".factory-state" / quarantine).read_text("utf-8")
        )
        forged = dict(original, campaign_id="tampered-campaign")
        self.seed(root, quarantine, json.dumps(forged, sort_keys=True).encode())
        with self.assertRaisesRegex(StateError, "does not match the latest"):
            recover_state(root)
        # Fail-closed: nothing restored, quarantine preserved, no canonical.
        directory = root / ".factory-state"
        self.assertFalse((directory / STATE_FILE_NAME).exists())
        self.assertTrue((directory / quarantine).exists())
        self.assertEqual(
            sorted(p.name for p in directory.iterdir()),
            [quarantine, DIGEST_LEDGER_NAME],
        )

    def test_recover_fails_closed_on_unsafe_ledger_during_restore(self) -> None:
        """Task 19 S2: an unreadable ledger blocks quarantine restore.

        When a ledger exists but cannot be validated, recovery cannot prove
        the quarantined state matches the latest recorded digest and fails
        closed, preserving the quarantine.
        """
        root = self.new_repo()
        self.init_campaign(root)
        record_phase_digest(root, "round-1-planning")
        quarantine = f".{STATE_FILE_NAME}.quarantine-" + "d" * 32
        (root / ".factory-state" / STATE_FILE_NAME).rename(
            root / ".factory-state" / quarantine
        )
        self.seed(root, DIGEST_LEDGER_NAME,
                  self.fixture("state-ledger-malformed.jsonl").read_bytes())
        with self.assertRaises(StateDigestError):
            recover_state(root)
        directory = root / ".factory-state"
        self.assertFalse((directory / STATE_FILE_NAME).exists())
        self.assertTrue((directory / quarantine).exists())

    def test_recover_preserves_unknown_artifacts_and_evidence(self) -> None:
        root = self.new_repo()
        self.init_campaign(root)
        record_phase_digest(root, "round-1-planning")
        orphan = self._orphan(root, "7" * 32)
        self.seed(root, "operator-note.txt", b"keep me")
        summary = recover_state(root)
        self.assertEqual(summary["status"], "clean")
        self.assertEqual(summary["removed"], 1)
        self.assertFalse(orphan.exists())
        directory = root / ".factory-state"
        self.assertEqual(
            sorted(p.name for p in directory.iterdir()),
            [STATE_FILE_NAME, "operator-note.txt", DIGEST_LEDGER_NAME],
        )
        self.assertEqual(load_state(root).campaign_id, "state-conformance")

    def test_recover_is_deterministic_and_idempotent(self) -> None:
        root = self.new_repo()
        self.init_campaign(root)
        before = load_state(root)
        self._orphan(root, "8" * 32)
        first = recover_state(root)
        second = recover_state(root)
        # The first run removed the observed validated orphan (removed=1); the
        # second run observes an already-final clean directory (removed=0).
        # Idempotency is compared on the stable final state/status, not on the
        # one-shot removed counter.
        self.assertEqual(first, {"status": "clean", "removed": 1, "restored": None})
        self.assertEqual(second, {"status": "clean", "removed": 0, "restored": None})
        self.assertEqual(first["status"], second["status"])
        self.assertEqual(first["restored"], second["restored"])
        self.assertEqual(
            load_state(root), before, "recovery must not change durable state"
        )

    def test_init_recovers_then_refuses_restored_campaign(self) -> None:
        root = self.new_repo()
        self.init_campaign(root)
        quarantine = f".{STATE_FILE_NAME}.quarantine-" + "5" * 32
        (root / ".factory-state" / STATE_FILE_NAME).rename(
            root / ".factory-state" / quarantine
        )
        with self.assertRaisesRegex(StateError, "refusing to overwrite"):
            self.init_campaign(root, campaign_id="clobber")
        self.assertEqual(load_state(root).campaign_id, "state-conformance")

    def test_recover_on_fresh_root_is_clean(self) -> None:
        root = self.new_repo()
        self.assertEqual(recover_state(root), {"status": "clean", "removed": 0, "restored": None})
        self.cli(root, "recover")
        result = self.cli(root, "recover")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("status=clean", result.stdout)

    def test_recover_reports_existing_empty_for_a_present_empty_directory(
        self,
    ) -> None:
        """Task 19 S2: a valid present-but-empty private directory is a
        distinct ``existing-empty`` outcome, never confused with a truly
        absent directory (``clean``). Recovery touches nothing and leaves the
        empty directory untouched.
        """
        root = self.new_repo()
        (root / ".factory-state").mkdir(mode=0o700)
        summary = recover_state(root)
        self.assertEqual(
            summary, {"status": "existing-empty", "removed": 0, "restored": None}
        )
        directory = root / ".factory-state"
        self.assertTrue(directory.is_dir())
        self.assertEqual(sorted(p.name for p in directory.iterdir()), [])
        # ``existing-empty`` is distinct from a truly absent directory.
        fresh = self.new_repo()
        self.assertEqual(
            recover_state(fresh),
            {"status": "clean", "removed": 0, "restored": None},
        )

    def test_recover_zero_byte_ledger_blocks_restore(self) -> None:
        """Task 19 hardening L4: a present-but-zero-byte ledger is ambiguous
        torn evidence of an interrupted first append and must block a
        quarantine restore, never being silently treated as “no evidence”.
        """
        root = self.new_repo()
        self.init_campaign(root)
        quarantine = f".{STATE_FILE_NAME}.quarantine-" + "3" * 32
        (root / ".factory-state" / STATE_FILE_NAME).rename(
            root / ".factory-state" / quarantine
        )
        self.seed(root, DIGEST_LEDGER_NAME, b"")
        with self.assertRaisesRegex(StateDigestError, "empty"):
            recover_state(root)
        directory = root / ".factory-state"
        self.assertFalse((directory / STATE_FILE_NAME).exists())
        self.assertTrue((directory / quarantine).exists())
        self.assertTrue((directory / DIGEST_LEDGER_NAME).exists())

    def test_recover_restores_quarantine_matching_the_latest_of_multi_entry_ledger(
        self,
    ) -> None:
        """Task 19 S2: with a multi-entry ledger, recovery binds the restore to
        the *latest* recorded digest, not the first. The quarantined state is
        byte-identical to the latest entry and is restored.
        """
        root = self.new_repo()
        self.init_campaign(root)
        record_phase_digest(root, "round-1-planning")  # first entry (planning)
        planned = advance(
            load_state(root), "planned", now=MONOTONIC + 1,
            plan_digest=PLAN_SHA, phase_base_commit=BASE_COMMIT,
        )
        write_state(root, planned)
        record_phase_digest(root, "round-1-implementation")  # latest (impl)
        ledger = (root / ".factory-state" / DIGEST_LEDGER_NAME).read_text("utf-8")
        self.assertEqual(len(ledger.splitlines()), 2)
        latest = json.loads(ledger.splitlines()[-1])["digest"]
        quarantine = f".{STATE_FILE_NAME}.quarantine-" + "4" * 32
        (root / ".factory-state" / STATE_FILE_NAME).rename(
            root / ".factory-state" / quarantine
        )
        summary = recover_state(root)
        self.assertEqual(summary["status"], "restored")
        restored = load_state(root)
        self.assertEqual(restored, planned)
        self.assertEqual(state_digest(restored), latest)

    def test_recover_rejects_quarantine_matching_an_older_ledger_entry(
        self,
    ) -> None:
        """Task 19 S2: a quarantined state whose digest matches an *earlier*
        ledger entry but not the latest fails closed — recovery must bind to
        the latest recorded digest and never restore a stale state that a
        later entry superseded.
        """
        root = self.new_repo()
        self.init_campaign(root)
        initial_bytes = (root / ".factory-state" / STATE_FILE_NAME).read_bytes()
        record_phase_digest(root, "round-1-planning")  # first (initial state)
        planned = advance(
            load_state(root), "planned", now=MONOTONIC + 1,
            plan_digest=PLAN_SHA, phase_base_commit=BASE_COMMIT,
        )
        write_state(root, planned)
        record_phase_digest(root, "round-1-implementation")  # latest
        quarantine = f".{STATE_FILE_NAME}.quarantine-" + "5" * 32
        # The canonical is absent (torn write) and the sole remaining
        # quarantine holds the *older* (initial) state whose digest matches
        # the first ledger entry, not the latest.
        (root / ".factory-state" / STATE_FILE_NAME).unlink()
        self.seed(root, quarantine, initial_bytes)
        with self.assertRaisesRegex(StateError, "does not match the latest"):
            recover_state(root)
        directory = root / ".factory-state"
        self.assertFalse((directory / STATE_FILE_NAME).exists())
        self.assertTrue((directory / quarantine).exists())
        self.assertTrue((directory / DIGEST_LEDGER_NAME).exists())


class RecoveryRaceHookTest(StateConformanceCase):
    """Task 19 S2: the deterministic race hooks force the exact restore
    fail-closed branches (raced canonical, substituted canonical, substituted
    quarantine) that a live crash cannot reproduce deterministically.
    """

    def _make_quarantine(self, root: Path) -> str:
        self.init_campaign(root)
        record_phase_digest(root, "round-1-planning")
        qname = f".{STATE_FILE_NAME}.quarantine-" + "1" * 32
        (root / ".factory-state" / STATE_FILE_NAME).rename(
            root / ".factory-state" / qname
        )
        return qname

    def test_raced_canonical_appears_fails_closed_preserving_quarantine(
        self,
    ) -> None:
        """A writer creates the canonical name between the quarantine read and
        recovery's no-replace ``linkat``: recovery must fail closed on the
        raced canonical without deleting the quarantine (recoverability).
        """
        root = self.new_repo()
        qname = self._make_quarantine(root)

        def raced_writer(_root, directory_fd):
            fd = os.open(
                STATE_FILE_NAME,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
                dir_fd=directory_fd,
            )
            os.write(fd, b"raced writer")
            os.close(fd)

        self.set_hook("_RACE_BEFORE_RESTORE_LINK", raced_writer)
        with self.assertRaisesRegex(StateError, "state file appeared during recovery"):
            recover_state(root)
        directory = root / ".factory-state"
        # Fail-closed: neither artifact is destroyed; the quarantine remains
        # for a later clean recovery (once the writer is stopped).
        self.assertTrue((directory / STATE_FILE_NAME).exists())
        self.assertTrue((directory / qname).exists())

    def test_canonical_substituted_after_link_differs_fails_closed(self) -> None:
        """After recovery links the canonical name, a hook substitutes a
        different valid state in its place: the in-place re-validation detects
        the mismatch and fails closed, preserving the quarantine.
        """
        root = self.new_repo()
        qname = self._make_quarantine(root)
        forged = json.loads((root / ".factory-state" / qname).read_text("utf-8"))
        forged = dict(forged, campaign_id="raced-campaign")
        raw = json.dumps(forged, sort_keys=True).encode()

        def substitute(_root, directory_fd):
            os.unlink(STATE_FILE_NAME, dir_fd=directory_fd)
            fd = os.open(
                STATE_FILE_NAME,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
                dir_fd=directory_fd,
            )
            os.write(fd, raw)
            os.close(fd)

        self.set_hook("_RACE_AFTER_RESTORE_LINK", substitute)
        with self.assertRaisesRegex(StateError, "differs from the validated"):
            recover_state(root)
        directory = root / ".factory-state"
        self.assertTrue((directory / qname).exists())

    def test_quarantine_inode_swap_after_link_fails_closed(self) -> None:
        """After recovery links the canonical name, the quarantine name is
        swapped to a brand-new inode: the canonical/quarantine identity check
        fails closed and the swap is never accepted, so a forgery cannot
        replace the durable copy before the quarantine is deleted.
        """
        root = self.new_repo()
        qname = self._make_quarantine(root)

        def swap(_root, directory_fd):
            os.unlink(qname, dir_fd=directory_fd)
            fd = os.open(
                qname,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
                dir_fd=directory_fd,
            )
            os.write(
                fd, (root / ".factory-state" / STATE_FILE_NAME).read_bytes()
            )
            os.close(fd)

        self.set_hook("_RACE_AFTER_RESTORE_LINK", swap)
        with self.assertRaisesRegex(StateError, "quarantine was substituted"):
            recover_state(root)
        directory = root / ".factory-state"
        self.assertTrue((directory / STATE_FILE_NAME).exists())
        self.assertTrue((directory / qname).exists())


class TrustedCliTest(StateConformanceCase):
    """The trusted control-plane CLI prints one outcome and fails closed."""

    def test_cli_init_show_digest(self) -> None:
        root = self.new_repo()
        result = self.cli(
            root, "init", "--campaign-id", "cli-campaign", "--rounds", "2",
            "--base-commit", BASE_COMMIT, "--spec-digest", SHA,
            "--plan-digest", PLAN_SHA, "--audit-digest", AUDIT_SHA,
            "--role-digest", f"planner={ROLE_SHA}", "--branch", "boilerplate-develop",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("initialized cli-campaign round=1 phase=planning", result.stdout)
        expected = state_digest(load_state(root))
        self.assertIn(f"digest={expected}", result.stdout)
        show = self.cli(root, "show")
        self.assertEqual(show.returncode, 0, show.stderr)
        self.assertEqual(
            show.stdout.strip(),
            json.dumps(load_state(root).to_dict(), sort_keys=True,
                       separators=(",", ":")),
        )
        digest = self.cli(root, "digest")
        self.assertEqual(digest.returncode, 0, digest.stderr)
        self.assertEqual(digest.stdout.strip(), f"digest={expected}")

    def test_cli_full_campaign(self) -> None:
        root = self.new_repo()
        self.assertEqual(
            self.cli(root, "init", "--campaign-id", "cli", "--rounds", "2",
                     "--base-commit", BASE_COMMIT, "--spec-digest", SHA,
                     "--plan-digest", PLAN_SHA, "--audit-digest", AUDIT_SHA,
                     "--role-digest", f"planner={ROLE_SHA}",
                     "--branch", "boilerplate-develop").returncode, 0)
        steps = (
            ("advance", "planned", "--plan-digest", PLAN_SHA,
             "--base-commit", BASE_COMMIT),
            ("begin-attempt", "4"),
            ("record-retry", "task_progress"),
            ("advance", "task_completed"),
            ("advance", "pass"),
            ("advance", "findings"),      # round 2 planning
            ("advance", "planned", "--plan-digest", PLAN_SHA,
             "--base-commit", BASE_COMMIT),
            ("advance", "task_completed"),
            ("advance", "pass"),
            ("advance", "pass"),          # final -> success
        )
        for step in steps:
            result = self.cli(root, *step)
            self.assertEqual(result.returncode, 0, f"{step}: {result.stderr}")
        final = self.cli(root, "show")
        state = load_state(root)
        self.assertEqual(state.current_phase, "success")
        self.assertEqual(state.last_outcome, "success")
        self.assertEqual(final.stdout.strip(),
                         json.dumps(state.to_dict(), sort_keys=True,
                                    separators=(",", ":")))

    def test_cli_advance_requires_plan_binding(self) -> None:
        root = self.new_repo()
        self.cli(root, "init", "--campaign-id", "cli", "--rounds", "1",
                 "--base-commit", BASE_COMMIT, "--spec-digest", SHA,
                 "--plan-digest", PLAN_SHA, "--audit-digest", AUDIT_SHA,
                 "--role-digest", f"planner={ROLE_SHA}",
                 "--branch", "boilerplate-develop")
        result = self.cli(root, "advance", "planned")
        self.assertEqual(result.returncode, 1)
        self.assertIn("requires the bound plan_digest", result.stderr)

    def test_cli_terminal_and_illegal_advances_fail_closed(self) -> None:
        root = self.new_repo()
        self.cli(root, "init", "--campaign-id", "cli", "--rounds", "1",
                 "--base-commit", BASE_COMMIT, "--spec-digest", SHA,
                 "--plan-digest", PLAN_SHA, "--audit-digest", AUDIT_SHA,
                 "--role-digest", f"planner={ROLE_SHA}",
                 "--branch", "boilerplate-develop")
        illegal = self.cli(root, "advance", "pass")
        self.assertEqual(illegal.returncode, 1)
        self.assertIn("no §11 transition", illegal.stderr)
        bound = self.cli(root, "advance", "planned", "--plan-digest", PLAN_SHA,
                         "--base-commit", BASE_COMMIT)
        self.assertEqual(bound.returncode, 0, bound.stderr)
        terminal = self.cli(root, "advance", "task_completed", "--plan-digest", "0" * 64,
                            "--base-commit", "0" * 40)
        self.assertEqual(terminal.returncode, 1)
        self.assertIn("bind only on the", terminal.stderr)

    def test_cli_unsafe_tag_and_bad_role_digest_fail_closed(self) -> None:
        root = self.new_repo()
        self.cli(root, "init", "--campaign-id", "cli", "--rounds", "1",
                 "--base-commit", BASE_COMMIT, "--spec-digest", SHA,
                 "--plan-digest", PLAN_SHA, "--audit-digest", AUDIT_SHA,
                 "--role-digest", f"planner={ROLE_SHA}",
                 "--branch", "boilerplate-develop")
        result = self.cli(root, "record-phase-digest", "../escape")
        self.assertEqual(result.returncode, 1)
        self.assertIn("unsafe phase tag", result.stderr)
        result = self.cli(root, "init", "--campaign-id", "cli", "--rounds", "1",
                          "--base-commit", BASE_COMMIT, "--spec-digest", SHA,
                          "--plan-digest", PLAN_SHA, "--audit-digest", AUDIT_SHA,
                          "--role-digest", "planner=zz", "--branch", "x")
        self.assertEqual(result.returncode, 1)
        self.assertIn("--role-digest expects ROLE=64-hex", result.stderr)

    def test_cli_audit_abort_is_terminal_and_refuses_rerun(self) -> None:
        # Task 9 review B1: an interrupted audit is persisted in the
        # authoritative control state as a terminal close — the round never
        # advances and a later transition is refused (the campaign is never
        # re-executed).
        root = self.new_repo()
        self.assertEqual(
            self.cli(root, "init", "--campaign-id", "cli", "--rounds", "2",
                     "--base-commit", BASE_COMMIT, "--spec-digest", SHA,
                     "--plan-digest", PLAN_SHA, "--audit-digest", AUDIT_SHA,
                     "--role-digest", f"planner={ROLE_SHA}",
                     "--branch", "boilerplate-develop").returncode, 0)
        for step in (
            ("advance", "planned", "--plan-digest", PLAN_SHA,
             "--base-commit", BASE_COMMIT),
            ("begin-attempt", "4"),
            ("advance", "task_completed"),
            ("advance", "pass"),
        ):
            result = self.cli(root, *step)
            self.assertEqual(result.returncode, 0, f"{step}: {result.stderr}")
        # Non-final round 1: an interrupted audit is a terminal close; the
        # round never advances to round 2 and the terminal is persisted.
        result = self.cli(root, "advance", "interrupted")
        self.assertEqual(result.returncode, 0, result.stderr)
        state = load_state(root)
        self.assertEqual(state.current_phase, "interrupted")
        self.assertEqual(state.last_outcome, "interrupted")
        self.assertEqual(state.current_round, 1)
        # The persisted terminal accepts no further transition (rerun refused).
        refused = self.cli(root, "advance", "pass")
        self.assertEqual(refused.returncode, 1)
        self.assertIn("accepts no transition", refused.stderr)
        show = self.cli(root, "show")
        self.assertEqual(show.returncode, 0, show.stderr)
        self.assertIn('"current_phase":"interrupted"', show.stdout)

    def test_cli_init_refuses_overwrite(self) -> None:
        root = self.new_repo()
        args = ("init", "--campaign-id", "cli", "--rounds", "1",
                "--base-commit", BASE_COMMIT, "--spec-digest", SHA,
                "--plan-digest", PLAN_SHA, "--audit-digest", AUDIT_SHA,
                "--role-digest", f"planner={ROLE_SHA}",
                "--branch", "boilerplate-develop")
        self.assertEqual(self.cli(root, *args).returncode, 0)
        result = self.cli(root, *args)
        self.assertEqual(result.returncode, 1)
        self.assertIn("refusing to overwrite", result.stderr)

    def test_cli_ledger_roundtrip_and_mutation_detection(self) -> None:
        root = self.new_repo()
        self.cli(root, "init", "--campaign-id", "cli", "--rounds", "1",
                 "--base-commit", BASE_COMMIT, "--spec-digest", SHA,
                 "--plan-digest", PLAN_SHA, "--audit-digest", AUDIT_SHA,
                 "--role-digest", f"planner={ROLE_SHA}",
                 "--branch", "boilerplate-develop")
        recorded = self.cli(root, "record-phase-digest", "round-1-planning")
        self.assertEqual(recorded.returncode, 0, recorded.stderr)
        verified = self.cli(root, "verify-phase-digest", "round-1-planning")
        self.assertEqual(verified.returncode, 0, verified.stderr)
        # A same-UID forgery through the trusted write path is then caught.
        state = load_state(root)
        write_state(root, parse_state({**state.to_dict(), "campaign_id": "forged"}))
        tampered = self.cli(root, "verify-phase-digest", "round-1-planning")
        self.assertEqual(tampered.returncode, 1)
        self.assertIn("changed during", tampered.stderr)


class PurityAndBoundaryTest(StateConformanceCase):
    """parse_state/state_digest are pure; the module respects its boundary."""

    def test_parse_and_digest_are_filesystem_independent(self) -> None:
        with tempfile.TemporaryDirectory(prefix="state-purity-") as empty:
            script = (
                "import json, sys; sys.path.insert(0, %r); "
                "from state import parse_state, state_digest; "
                "data = json.load(open(%r, encoding='utf-8')); "
                "print(state_digest(parse_state(data)))"
            ) % (
                str(LOOP),
                str(self.fixture("state-valid-initial.json")),
            )
            result = subprocess.run(
                [sys.executable, "-c", script],
                capture_output=True, text=True, cwd=empty,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            expected = state_digest(parse_state(json.loads(
                self.fixture("state-valid-initial.json").read_text("utf-8")
            )))
            self.assertEqual(result.stdout.strip(), expected)

    def test_on_disk_bytes_hash_to_the_same_digest(self) -> None:
        root = self.new_repo()
        self.init_campaign(root)
        raw = (root / ".factory-state" / STATE_FILE_NAME).read_bytes()
        self.assertEqual(raw[-1:], b"\n")
        self.assertEqual(
            hashlib.sha256(raw[:-1]).hexdigest(), state_digest(load_state(root))
        )

    def test_no_wall_clock_timestamp_field_is_accepted(self) -> None:
        data = json.loads(self.fixture("state-valid-initial.json").read_text("utf-8"))
        data["wall_clock"] = "2026-01-01T00:00:00Z"
        with self.assertRaisesRegex(StateTamperError, "extra"):
            parse_state(data)

    def test_ledger_never_becomes_orchestration_state(self) -> None:
        self.assertNotIn("current_phase", DIGEST_LEDGER_NAME)
        root = self.new_repo()
        self.init_campaign(root)
        record_phase_digest(root, "round-1-planning")
        ledger = (root / ".factory-state" / DIGEST_LEDGER_NAME).read_text("utf-8")
        self.assertIn('"tag"', ledger)
        self.assertIn('"digest"', ledger)
        self.assertNotIn('"current_phase"', ledger)

    def test_expected_uid_is_underscore_private_with_no_cli_bypass(self) -> None:
        """Task 19 S7: the internal owner expectation is underscore-private
        (``_expected_uid``) on the module's own functions and is never exposed
        as a trusted-CLI option, so no operator can weaken the real owner
        check through the control-plane command surface.
        """
        import inspect

        for func in (recover_state, load_state):
            with self.subTest(func=func.__name__):
                self.assertIn("_expected_uid", inspect.signature(func).parameters)
        # The CLI surface defines no expected-uid knob: every command rejects
        # it as an unrecognized argument (argparse exits 2).
        root = self.new_repo()
        self.init_campaign(root)
        for command in (("recover",), ("show",), ("digest",)):
            with self.subTest(command=command):
                result = self.cli(root, *command, "--expected-uid", "999")
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertIn("unrecognized arguments", result.stderr)
        # The real owner check still runs with the default (current) UID.
        self.assertEqual(load_state(root).campaign_id, "state-conformance")


if __name__ == "__main__":
    suite = unittest.defaultTestLoader.loadTestsFromModule(sys.modules[__name__])
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    raise SystemExit(0 if result.wasSuccessful() else 1)
