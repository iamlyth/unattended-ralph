#!/usr/bin/env python3
"""Unit and adversarial coverage for ordered exact-commit pre-round hooks."""

from __future__ import annotations

import hashlib
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / ".factory" / "loop"))

import campaign as campaign_module  # noqa: E402
import pre_round  # noqa: E402
import state  # noqa: E402

SHA = "a" * 64
COMMIT = "b" * 40


def registry_bytes() -> bytes:
    return json.dumps({
        "schema": pre_round.REGISTRY_SCHEMA,
        "hooks": [
            {"id": "first", "implementation": "branch_guard", "enabled": True,
             "mandatory": True},
        ],
    }, separators=(",", ":")).encode()


def two_hook_runtime_registry() -> pre_round.Registry:
    """Exercise generic ordered execution without configuring quota policy."""
    return pre_round.Registry(
        (
            pre_round.Hook("first", "branch_guard", True, True),
            pre_round.Hook("second", "synthetic_test_guard", True, True),
        ),
        hashlib.sha256(b"runtime-test-registry").hexdigest(),
    )


class RegistryTest(unittest.TestCase):
    def test_committed_registry_contains_only_branch_guard(self) -> None:
        registry = pre_round.parse_registry(
            (ROOT / ".factory/pre-round-hooks.json").read_bytes()
        )
        self.assertEqual(
            [(hook.implementation, hook.enabled, hook.mandatory)
             for hook in registry.hooks],
            [("branch_guard", True, True)],
        )

    def test_committed_hook_executes(self) -> None:
        registry = pre_round.parse_registry(registry_bytes())
        calls = []
        results, success = pre_round.run_hooks(
            registry, implementation_digests={"branch_guard": SHA},
            execute=lambda hook: calls.append(hook.hook_id),
        )
        self.assertTrue(success)
        self.assertEqual(calls, ["first"])
        self.assertEqual([r.outcome for r in results], ["pass"])

    def test_single_hook_registry_remains_valid(self) -> None:
        raw = json.dumps({
            "schema": pre_round.REGISTRY_SCHEMA,
            "hooks": [{"id": "first", "implementation": "branch_guard",
                       "enabled": True, "mandatory": True}],
        }).encode()
        registry = pre_round.parse_registry(raw)
        calls = []
        results, success = pre_round.run_hooks(
            registry, implementation_digests={"branch_guard": SHA},
            execute=lambda hook: calls.append(hook.hook_id),
        )
        self.assertTrue(success)
        self.assertEqual(calls, ["first"])
        self.assertEqual(results[0].outcome, "pass")

    def test_enabled_hooks_run_in_declared_order(self) -> None:
        registry = two_hook_runtime_registry()
        calls = []
        _results, success = pre_round.run_hooks(
            registry,
            implementation_digests={"branch_guard": SHA, "synthetic_test_guard": SHA},
            execute=lambda hook: calls.append(hook.implementation),
        )
        self.assertTrue(success)
        self.assertEqual(calls, ["branch_guard", "synthetic_test_guard"])

    def test_mandatory_failure_blocks_later_hook(self) -> None:
        registry = two_hook_runtime_registry()
        calls = []
        def execute(hook):
            calls.append(hook.hook_id)
            raise RuntimeError("synthetic secret must not enter result")
        results, success = pre_round.run_hooks(
            registry,
            implementation_digests={"branch_guard": SHA, "synthetic_test_guard": SHA},
            execute=execute,
        )
        self.assertFalse(success)
        self.assertEqual(calls, ["first"])
        self.assertEqual([r.outcome for r in results], ["failed", "not_run"])
        payload = pre_round.result_bytes(
            campaign_id="campaign", round_number=1, commit=COMMIT,
            configuration_digest_value=SHA, results=results,
        )
        self.assertNotIn(b"synthetic secret", payload)

    def test_registry_rejects_extension_and_ambiguity(self) -> None:
        invalid = [
            b"\xef\xbb\xbf" + registry_bytes(),
            b'{"schema":"factory-pre-round-hooks/v1","schema":"x","hooks":[]}',
            json.dumps({"schema": pre_round.REGISTRY_SCHEMA, "hooks": [{
                "id": "x", "implementation": "branch_guard", "enabled": 1,
                "mandatory": True}]}).encode(),
            json.dumps({"schema": pre_round.REGISTRY_SCHEMA, "hooks": [{
                "id": "x", "implementation": "branch_guard", "enabled": True,
                "mandatory": False}]}).encode(),
            json.dumps({"schema": pre_round.REGISTRY_SCHEMA, "hooks": [{
                "id": "x", "implementation": "command", "enabled": True,
                "mandatory": True}]}).encode(),
        ]
        for raw in invalid:
            with self.subTest(raw=raw[:40]):
                with self.assertRaises(pre_round.PreRoundError):
                    pre_round.parse_registry(raw)

    def test_branch_binding_covers_lock_and_git_implementation(self) -> None:
        class Blobs:
            def __init__(self, altered: str = "") -> None:
                self.altered = altered

            def blob_at(self, commit: str, relpath: str) -> bytes:
                if commit != COMMIT:
                    raise AssertionError("binding read escaped the selected commit")
                data = (ROOT / relpath).read_bytes()
                return data + (b"\n# altered\n" if relpath == self.altered else b"")

        _registry, _digests, baseline = campaign_module._derive_pre_round_binding(
            ROOT, bound_commit=COMMIT, git=Blobs()
        )
        for relpath in (".factory/loop/lock.py", ".factory/loop/gitutil.py"):
            with self.subTest(relpath=relpath):
                _registry, _digests, changed = campaign_module._derive_pre_round_binding(
                    ROOT, bound_commit=COMMIT, git=Blobs(relpath)
                )
                self.assertNotEqual(changed, baseline)

    def test_config_and_result_chain_are_order_sensitive(self) -> None:
        registry = pre_round.parse_registry(registry_bytes())
        config = pre_round.configuration_digest(
            registry, {"branch_guard": SHA}, bound_commit=COMMIT,
        )
        results, _ = pre_round.run_hooks(
            registry, implementation_digests={"branch_guard": SHA},
            execute=lambda _hook: None,
        )
        payload = pre_round.result_bytes(
            campaign_id="campaign", round_number=1, commit=COMMIT,
            configuration_digest_value=config, results=results,
        )
        first = pre_round.chain_result_digest("0" * 64, payload)
        second = pre_round.chain_result_digest(first, payload)
        self.assertNotEqual(first, second)
        self.assertEqual(first, hashlib.sha256(bytes(32) + payload).hexdigest())


class StateCursorTest(unittest.TestCase):
    def initial(self):
        return state.FactoryState(
            schema=state.SCHEMA_NAME, repository_identity="1:2", branch="develop",
            campaign_id="campaign", rounds_requested=5, current_round=1,
            current_phase="planning", specification_digest=SHA, plan_digest=SHA,
            role_prompt_digests={"planner": SHA}, audit_objectives_digest=SHA,
            pre_round_hook_configuration_digest=SHA,
            pre_round_hook_commit=COMMIT,
            pre_round_hook_results_digest="0" * 64,
            pre_round_hook_started_round=0, pre_round_hook_completed_round=0,
            phase_base_commit=COMMIT, selected_task_id=None, attempt_number=0,
            phase_started_at_monotonic=1, attempt_started_at_monotonic=0,
            last_outcome=None,
        )

    def test_legacy_state_is_diagnostic_only_and_cannot_match_binding(self) -> None:
        legacy = self.initial().to_dict()
        for key in (
            "pre_round_hook_configuration_digest", "pre_round_hook_commit",
            "pre_round_hook_results_digest",
            "pre_round_hook_started_round", "pre_round_hook_completed_round",
        ):
            legacy.pop(key)
        migrated = state.parse_state(legacy)
        self.assertEqual(migrated.pre_round_hook_configuration_digest, "0" * 64)
        self.assertNotEqual(migrated.pre_round_hook_configuration_digest, SHA)

    def test_write_ahead_cursor_prevents_duplicate_execution(self) -> None:
        claimed = state.begin_pre_round_hooks(self.initial())
        self.assertEqual(claimed.pre_round_hook_started_round, 1)
        with self.assertRaises(state.StateTransitionError):
            state.begin_pre_round_hooks(claimed)
        completed = state.complete_pre_round_hooks(claimed, "d" * 64)
        self.assertEqual(completed.pre_round_hook_completed_round, 1)
        with self.assertRaises(state.StateTransitionError):
            state.begin_pre_round_hooks(completed)


if __name__ == "__main__":
    unittest.main(verbosity=2)
