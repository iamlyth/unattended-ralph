#!/usr/bin/env python3
"""Hidden campaign suite for the trusted phase/campaign orchestrator
(PHASE-01, COMPLETE-01, GIT-01; FACTORY-LOOP-SPEC §11-§15, §17; Task 9).

This test lives under the hidden ``.factory/tests/`` namespace because the
specification (HIDE-01, §3) keeps harness-only tests out of the adopting
product's visible test tree.  It drives ``.factory/loop/campaign.py`` — the
trusted phase/campaign control plane — through the explicit deterministic
embedded role seam (``.factory/tests/fixtures/campaign_driver.py``) on
committed synthetic fixture repositories:

* **finite terminal outcomes (§13/§14)**: fixture campaigns for every
  terminal — ``success``, ``findings``, ``blocked`` (with exact
  unavailable-evidence references), ``failed`` (planning-attempt
  exhaustion), ``interrupted`` (planning interruption and dirty
  implementation-attempt exhaustion), and ``infrastructure_failure``
  (untrusted verifier/auditor) — each terminating within the configured
  round/attempt bounds with the exact §14 exit code and round count;
* **empty work does not spin (§13.2/§14)**: ``work_exhausted`` and
  ``blocked`` selections still reach verification and audit, and a
  multi-task campaign terminates after the last runnable task;
* **crash reconciliation (§17)**: a trusted commit that landed before its
  transition was recorded is reconciled deterministically from Git + plan +
  state on the next run (planning and implementation), an ambiguous
  recovery fails closed, and dirty work is preserved — never reset,
  discarded, or silently overwritten;
* **descriptor-anchored Git authority (GIT-01, §12)**: every campaign
  commit is created by the orchestrator identity, the committed scope of
  every campaign commit is exactly the allowlisted role work, and the
  model/fixture role never runs Git;
* **one trusted lifecycle surface (§11)**: the campaign touches exactly the
  control-state file, the append-only digest ledger, and the published
  campaign result under ``.factory-state/`` — no runtime task ledger,
  memory store, or event stream is ever created;
* **fail-closed control-plane errors**: write-once campaign bindings, a
  terminal control state, malformed phase-result files, unbound plans,
  scope violations, and unknown configurations all fail closed;
* **schema and CLI**: the published campaign result and the structured
  phase results conform to their committed schemas; the CLI help, exit
  codes, and config derivation are exercised end to end.

The suite reuses the established fixture patterns of the other
``test-factory-*.py`` modules: real committed Git repositories, the pinned
absolute Git executable, and the committed role-driver seam (never evidence
of real model acceptance or real confinement).
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
import unittest.mock

ROOT = Path(__file__).resolve().parents[2]
LOOP = ROOT / ".factory" / "loop"
FIXTURES = ROOT / ".factory" / "tests" / "fixtures"
SCHEMAS = ROOT / ".factory" / "schemas"
STATE_DIR = ".factory-state"

sys.path.insert(0, str(LOOP))
import audit_objectives as audit_objectives_module  # noqa: E402
import campaign as campaign_module  # noqa: E402
import gitutil  # noqa: E402
import launch as launch_module  # noqa: E402
import state as state_module  # noqa: E402

GIT = gitutil.GIT_EXECUTABLE

# Deterministic gate executables for the fixture campaigns.  ``/bin/false``
# and ``/bin/true`` do not exist under the Nix store layout, so the tests
# bind ``--verification-command`` / ``--capability-command`` to the store
# ``bin/true`` / ``bin/false`` entrypoints following the established pattern
# of the hidden suite.  The paths are intentionally *not* resolved through
# symlinks: the store ``bin/true`` is a symlink to the multi-call
# ``coreutils`` binary, which dispatches on argv[0], so the symlink path must
# be exec'd verbatim.  Task 9 review MED: verification requires an explicit
# deterministic verification command — the tester JSON alone never gates —
# so every fixture campaign configures the ``true`` gate and an absent gate
# fails the campaign closed (``infrastructure_failure``).
TRUE_EXECUTABLE = Path(shutil.which("true"))
FALSE_EXECUTABLE = Path(shutil.which("false"))

REQUIREMENT_REGISTRY = SCHEMAS / "factory-plan-v1.requirements.json"
PLAN_TOOL = FIXTURES / "fixture_plan_tool.py"
DRIVER_REL = ".factory/tests/fixtures/campaign_driver.py"
PLAN_REL = ".factory/artifacts/implementation-plan.md"
BRANCH = "fixture-main"


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def run(
    command: list[str],
    root: Path | None = None,
    *,
    check: bool = True,
    env: dict | None = None,
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        command, cwd=root, text=True, capture_output=True, env=env
    )
    if check and result.returncode:
        raise AssertionError(
            (command, result.returncode, result.stdout[-2000:], result.stderr[-2000:])
        )
    return result


def _git(workspace: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return run([GIT, "-C", str(workspace), *args], check=True)


def gen_plan(ws: Path, common: dict, out_rel: str, tasks: list[dict]) -> None:
    """Generate one committed-plan template through the committed tool."""
    spec_path = ws / "fixture-spec.json"
    spec_path.write_text(json.dumps({**common, "tasks": tasks}), encoding="utf-8")
    run(
        [
            sys.executable, str(PLAN_TOOL),
            "--spec", str(spec_path),
            "--registry", str(REQUIREMENT_REGISTRY),
            "--out", str(ws / out_rel),
        ],
        root=ws,
    )


# The standard fixture task set: two implementation tasks plus the final
# audit task (which must be last and depend on every other task).
TASK_SPECS: list[dict] = [
    {
        "number": 1, "title": "Implement the fixture feature",
        "status": "pending", "priority": 10, "dependencies": [],
        "blocked_on": None, "scope": "initial scope statement.",
        "verification": "`src/work-1.md`",
    },
    {
        "number": 2, "title": "Implement the second feature",
        "status": "pending", "priority": 20, "dependencies": [],
        "blocked_on": None,
        "verification": "`src/work-2.md`",
    },
    {
        "number": 3, "title": "final", "status": "pending",
        "priority": 1, "dependencies": [1, 2], "blocked_on": None,
        "verification": "`src/work-3.md`",
    },
]


class FixtureWorkspace:
    """One committed synthetic repository for campaign scenarios.

    A real Git repository whose committed blobs bind every authoritative byte
    the campaign reads (spec, plan, prompts, registry, driver), so the
    lock/binding authority and the embedded role driver re-derive exactly
    what the campaign config declared.  The role driver is copied from the
    committed fixture module so the worktree copy equals the committed blob
    the campaign verifies.
    """

    def __init__(
        self,
        tmp: Path,
        *,
        scenario: dict,
        rounds: int = 1,
        planning_attempts: int = 3,
        implementation_attempts: int = 3,
        phase_result: str = f"{STATE_DIR}/phase-result.json",
        audit_result: str = f"{STATE_DIR}/audit-result.json",
    ) -> None:
        self.root = tmp / "workspace"
        self.scenario = scenario
        self.rounds = rounds
        self.planning_attempts = planning_attempts
        self.implementation_attempts = implementation_attempts
        self.phase_result = phase_result
        self.audit_result = audit_result
        self.scenario_commit: str | None = None
        self.build()

    # -- fixture construction -------------------------------------------------

    def build(self) -> None:
        ws = self.root
        ws.mkdir(parents=True)
        for rel in (
            "docs",
            "scripts",
            ".factory/prompts",
            ".factory/audit-objectives",
            ".factory/artifacts",
            ".factory/tests/fixtures",
            "fixture/templates",
            "src",
        ):
            (ws / rel).mkdir(parents=True)
        # Task 11: every fixture repository commits the exact credential
        # guard — deterministic gate output is redacted through the exact
        # committed guard before it can enter a result, log, receipt, or
        # repository state.
        shutil.copy2(
            ROOT / "scripts" / "credential-guard.py",
            ws / "scripts" / "credential-guard.py",
        )
        (ws / "AGENTS.md").write_text(
            "AGENTS.md operational policy\n", encoding="utf-8")
        (ws / "docs" / "SPEC.md").write_text(
            "PRODUCT SPEC FIXTURE\n", encoding="utf-8")
        for role in ("planner", "developer", "tester", "auditor"):
            (ws / ".factory" / "prompts" / f"{role}.md").write_text(
                f"# {role} role prompt\n", encoding="utf-8")
        shutil.copy2(
            ROOT / ".factory" / "audit-objectives" / "registry.json",
            ws / ".factory" / "audit-objectives" / "registry.json",
        )
        _git(ws, "init", "-q", "-b", BRANCH)
        _git(ws, "config", "user.email", "fixture@test")
        _git(ws, "config", "user.name", "fixture")
        _git(ws, "add", "-A")
        _git(ws, "commit", "-qm", "fixture base")
        base_head = _git(ws, "rev-parse", "HEAD").stdout.strip()
        spec_blob = _git(ws, "rev-parse", "HEAD:docs/SPEC.md").stdout.strip()
        common = {
            "spec_path": "docs/SPEC.md",
            "spec_commit": base_head,
            "spec_blob": spec_blob,
            "base_commit": base_head,
            "lifecycle": "active",
        }
        self._generate_plans(common)
        driver_dst = ws / DRIVER_REL
        shutil.copy2(FIXTURES / "campaign_driver.py", driver_dst)
        os.chmod(driver_dst, 0o755)
        _git(ws, "add", "-A")
        _git(ws, "commit", "-qm", "fixture plan and driver")

    def _generate_plans(self, common: dict) -> None:
        ws = self.root
        gen_plan(ws, common, ".factory/artifacts/implementation-plan.md",
                 TASK_SPECS)
        # The planner template of round N preserves the tasks the previous
        # rounds already completed (a real planner revises the plan from the
        # committed plan state), so a multi-round campaign works through
        # every runnable task instead of re-selecting completed work.
        for round_no in (1, 2, 3):
            revised = [
                {
                    **t,
                    "scope": (t.get("scope", "fixture-scoped work only.")
                              + f" revised {round_no}."),
                    "status": "complete" if t["number"] < round_no else t["status"],
                }
                for t in TASK_SPECS
            ]
            gen_plan(ws, common, f"fixture/templates/planner-{round_no}.md",
                     revised)
        complete = [{**dict(t), "status": "complete"} for t in TASK_SPECS]
        gen_plan(ws, {**common, "lifecycle": "complete"},
                 "fixture/templates/planner-complete.md", complete)
        blocked = [
            {**dict(TASK_SPECS[0]), "status": "blocked",
             "blocked_on": "external-capability-required"},
            {**dict(TASK_SPECS[1]), "dependencies": [1]},
            TASK_SPECS[2],
        ]
        gen_plan(ws, common, "fixture/templates/planner-blocked.md", blocked)
        # An unbound template: every binding matches except the cycle base.
        gen_plan(ws, {**common, "base_commit": "1" * 40},
                 "fixture/templates/planner-unbound.md", TASK_SPECS)
        for task in TASK_SPECS:
            number = task["number"]
            # The developer's completion template of task N keeps every
            # earlier task complete (it revises the already-committed plan
            # state), never regressing completed work; the final task's plan
            # therefore carries every task complete and a `complete`
            # lifecycle (the parser rejects an `active` plan whose
            # non-verified matrix rows reference only completed tasks).
            complete_tasks = [
                {**dict(t),
                 "status": "complete" if t["number"] <= number else t["status"]}
                for t in TASK_SPECS
            ]
            dev_common = (
                {**common, "lifecycle": "complete"}
                if all(t["status"] == "complete" for t in complete_tasks)
                else common
            )
            gen_plan(ws, dev_common, f"fixture/templates/dev-{number}.md",
                     complete_tasks)
            # A valid plan may reach ``in_progress`` only when every
            # dependency is complete (§8), so progress templates exist for
            # tasks without incomplete dependencies (the fixture's task 1).
            if not task["dependencies"]:
                progress_tasks = [
                    {**dict(t),
                     "status": "in_progress" if t["number"] == number else t["status"]}
                    for t in TASK_SPECS
                ]
                gen_plan(ws, common,
                         f"fixture/templates/dev-{number}-progress.md",
                         progress_tasks)

    def commit_scenario(self, scenario: dict | None = None) -> None:
        """Write and commit the scenario JSON (never dirty role work)."""
        if scenario is not None:
            self.scenario = scenario
        (self.root / "scenario.json").write_text(
            json.dumps(self.scenario), encoding="utf-8")
        _git(self.root, "add", "scenario.json")
        _git(self.root, "commit", "-qm", "scenario fixture")
        self.scenario_commit = _git(
            self.root, "rev-parse", "HEAD").stdout.strip()

    # -- invocation ------------------------------------------------------------

    def config_kwargs(self) -> dict:
        return {
            "campaign_id": "campaign",
            "rounds": self.rounds,
            "branch": BRANCH,
            "plan_path": PLAN_REL,
            "planning_attempts": self.planning_attempts,
            "implementation_attempts": self.implementation_attempts,
            "provider": "synthetic",
            "model": "fixture-model",
            "backend": "",
            "role_driver": DRIVER_REL,
            "scenario_path": "scenario.json",
            "acceptance_command": [],
            # Task 9 review MED: every fixture campaign configures an
            # explicit deterministic verification command; the tester's
            # structured result alone is never a gate.
            "verification_command": [str(TRUE_EXECUTABLE)],
            "capability_command": [],
            "phase_result_path": self.phase_result,
            "audit_result_path": self.audit_result,
            "role_timeout": 60.0,
            "gate_timeout": 60.0,
        }

    def run_cli(
        self,
        campaign_id: str = "campaign",
        extra: list[str] | None = None,
    ) -> tuple[int, dict | None]:
        """Run the campaign CLI in a subprocess; parse the JSON result.

        Every fixture campaign configures the deterministic verification
        gate (Task 9 review MED); a test that overrides it passes its own
        ``--verification-command`` in ``extra`` and the default is not
        added a second time.
        """
        argv = [
            sys.executable, str(LOOP / "campaign.py"),
            "--root", str(self.root),
            "run",
            "--campaign-id", campaign_id,
            "--rounds", str(self.rounds),
            "--branch", BRANCH,
            "--role-driver", DRIVER_REL,
            "--scenario", "scenario.json",
            "--phase-result", self.phase_result,
            "--audit-result", self.audit_result,
            "--planning-attempts", str(self.planning_attempts),
            "--implementation-attempts", str(self.implementation_attempts),
        ]
        if extra:
            argv.extend(extra)
        if not any(token.startswith("--verification-command")
                   for token in argv):
            argv += ["--verification-command", str(TRUE_EXECUTABLE)]
        result = run(argv, root=ROOT, check=False)
        data = None
        try:
            data = json.loads(result.stdout)
        except ValueError:
            pass
        return result.returncode, data

    def derive_config(self, campaign_id: str = "campaign"):
        return campaign_module.derive_campaign_config(
            self.root, **{**self.config_kwargs(), "campaign_id": campaign_id}
        )

    # -- state helpers ----------------------------------------------------------

    def state_file(self) -> Path:
        return self.root / STATE_DIR / state_module.STATE_FILE_NAME

    def ledger_file(self) -> Path:
        return self.root / STATE_DIR / state_module.DIGEST_LEDGER_NAME

    def result_file(self, campaign_id: str = "campaign") -> Path:
        return self.root / STATE_DIR / f"campaign-result-{campaign_id}.json"

    def load_state(self):
        return state_module.load_state(
            self.root,
            expected_branch=BRANCH,
            expected_campaign_id="campaign",
            expected_rounds_requested=self.rounds,
        )


SUCCESS_SCENARIO = {
    "planner": {"behavior": "planned"},
    "developer": {"behavior": "complete"},
    "tester": {"behavior": "pass"},
    "auditor": {"behavior": "pass"},
}


class _CampaignBase(unittest.TestCase):
    """Shared helpers: one fresh fixture workspace per test."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="factory-campaign-test."))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self._workspace_count = 0

    def make(self, scenario: dict, **kwargs) -> FixtureWorkspace:
        # A test may build more than one workspace (e.g. a second scenario
        # in the same test); every workspace gets its own directory.
        self._workspace_count += 1
        ws = FixtureWorkspace(
            self.tmp / f"ws{self._workspace_count}",
            scenario=scenario, **kwargs,
        )
        ws.commit_scenario()
        return ws

    def _crash_at_plan(
        self,
        ws: FixtureWorkspace,
        when: callable,
    ):
        """Run the campaign with a state-write that simulates a crash.

        Returns the pre-run config.  The trusted transition commit lands but
        the state write is lost, leaving the state file at the previous
        trusted write — the §17 recovery window.
        """
        original = state_module.write_state
        crashed = {"raised": False}

        def crashing_write(root, state):
            if not crashed["raised"] and when(state):
                crashed["raised"] = True
                raise state_module.StateError(
                    "simulated crash: state write lost")
            return original(root, state)

        config = ws.derive_config()
        with unittest.mock.patch.object(
            campaign_module.state_module, "write_state",
            side_effect=crashing_write,
        ):
            with self.assertRaises(state_module.StateError):
                campaign_module.Campaign(config).run()
        self.assertTrue(crashed["raised"])
        return config


def assert_terminal(
    test: unittest.TestCase,
    data: dict,
    *,
    terminal_phase: str,
    terminal_outcome: str,
    exit_code: int,
    rounds_completed: int,
) -> None:
    test.assertEqual(data["terminal_phase"], terminal_phase)
    test.assertEqual(data["terminal_outcome"], terminal_outcome)
    test.assertEqual(data["exit_code"], exit_code)
    test.assertEqual(data["rounds_completed"], rounds_completed)
    test.assertEqual(data["schema"], "factory-campaign-result/v1")
    test.assertEqual(data["campaign_id"], "campaign")
    test.assertEqual(len(data["head_commit"]), 40)


def assert_history(
    test: unittest.TestCase,
    data: dict,
    expected: list[tuple[int, str, str]],
) -> None:
    """Assert the phase-history ``(round, phase, outcome)`` sequence exactly."""
    got = [
        (record["round"], record["phase"], record["outcome"])
        for record in data["phase_history"]
    ]
    test.assertEqual(got, expected)


class CampaignTerminals(_CampaignBase):
    """§14 finite terminal classification fixtures."""

    def test_success_campaign(self) -> None:
        ws = self.make(SUCCESS_SCENARIO)
        rc, data = ws.run_cli()
        self.assertEqual(rc, 0)
        assert_terminal(self, data, terminal_phase="success",
                        terminal_outcome="pass", exit_code=0,
                        rounds_completed=1)
        assert_history(self, data, [
            (1, "planning", "planned"),
            (1, "implementation", "task_completed"),
            (1, "verification", "pass"),
            (1, "audit", "pass"),
        ])
        state = ws.load_state()
        self.assertEqual(state.current_phase, "success")
        self.assertEqual(state.last_outcome, "success")

    def test_final_findings(self) -> None:
        ws = self.make({
            "planner": {"behavior": "planned"},
            "developer": {"behavior": "complete"},
            "tester": {"behavior": "findings"},
            "auditor": {"behavior": "findings"},
        })
        rc, data = ws.run_cli()
        self.assertEqual(rc, 1)
        assert_terminal(self, data, terminal_phase="findings",
                        terminal_outcome="findings", exit_code=1,
                        rounds_completed=1)
        assert_history(self, data, [
            (1, "planning", "planned"),
            (1, "implementation", "task_completed"),
            (1, "verification", "findings"),
            (1, "audit", "findings"),
        ])

    def test_final_audit_blocked_exact_references(self) -> None:
        ws = self.make({
            "planner": {"behavior": "planned"},
            "developer": {"behavior": "complete"},
            "tester": {"behavior": "pass"},
            "auditor": {"behavior": "blocked"},
        })
        rc, data = ws.run_cli()
        self.assertEqual(rc, 2)
        assert_terminal(self, data, terminal_phase="blocked",
                        terminal_outcome="blocked", exit_code=2,
                        rounds_completed=1)

    def test_audit_findings_take_precedence_over_blocked(self) -> None:
        # §14: when both categories exist, findings take precedence.  The
        # auditor writes findings *and* exact blocked references.
        ws = self.make({
            "planner": {"behavior": "planned"},
            "developer": {"behavior": "complete"},
            "tester": {"behavior": "pass"},
            "auditor": {
                "behavior": "findings",
                "blocked_on": ["external-human-authority"],
            },
        })
        rc, data = ws.run_cli()
        self.assertEqual(rc, 1)
        assert_terminal(self, data, terminal_phase="findings",
                        terminal_outcome="findings", exit_code=1,
                        rounds_completed=1)

    def test_planning_attempt_exhaustion_terminates_failed(self) -> None:
        ws = self.make({
            "planner": {"behavior": "no-change"},
            "developer": {"behavior": "complete"},
            "tester": {"behavior": "pass"},
            "auditor": {"behavior": "pass"},
        })
        rc, data = ws.run_cli()
        self.assertEqual(rc, 3)
        assert_terminal(self, data, terminal_phase="failed",
                        terminal_outcome="failed", exit_code=3,
                        rounds_completed=0)
        history = data["phase_history"]
        self.assertEqual(len(history), 3)
        self.assertTrue(all(
            record["phase"] == "planning" and record["outcome"] == "failed"
            for record in history))

    def test_planning_interruption_terminates_interrupted(self) -> None:
        ws = self.make({
            "planner": {"behavior": "crash"},
            "developer": {"behavior": "complete"},
            "tester": {"behavior": "pass"},
            "auditor": {"behavior": "pass"},
        })
        rc, data = ws.run_cli()
        self.assertEqual(rc, 4)
        assert_terminal(self, data, terminal_phase="interrupted",
                        terminal_outcome="interrupted", exit_code=4,
                        rounds_completed=0)

    def test_dirty_implementation_exhaustion_terminates_interrupted(self) -> None:
        # §13.2: budget expiry with dirty work terminates interrupted;
        # verification does not run.
        ws = self.make({
            "planner": {"behavior": "planned"},
            "developer": {"behavior": "crash"},
            "tester": {"behavior": "pass"},
            "auditor": {"behavior": "pass"},
        })
        rc, data = ws.run_cli()
        self.assertEqual(rc, 4)
        assert_terminal(self, data, terminal_phase="interrupted",
                        terminal_outcome="interrupted", exit_code=4,
                        rounds_completed=0)
        self.assertTrue(any(r["phase"] == "implementation"
                            for r in data["phase_history"]))
        self.assertTrue(all(r["phase"] != "verification"
                            for r in data["phase_history"]))

    def test_untrusted_verifier_terminates_infrastructure_failure(self) -> None:
        ws = self.make({
            "planner": {"behavior": "planned"},
            "developer": {"behavior": "complete"},
            "tester": {"behavior": "dirty"},
            "auditor": {"behavior": "pass"},
        })
        rc, data = ws.run_cli()
        self.assertEqual(rc, 5)
        assert_terminal(self, data, terminal_phase="infrastructure_failure",
                        terminal_outcome="infrastructure_failure",
                        exit_code=5, rounds_completed=0)

    def test_untrusted_auditor_terminates_infrastructure_failure(self) -> None:
        ws = self.make({
            "planner": {"behavior": "planned"},
            "developer": {"behavior": "complete"},
            "tester": {"behavior": "pass"},
            "auditor": {"behavior": "dirty"},
        })
        rc, data = ws.run_cli()
        self.assertEqual(rc, 5)
        assert_terminal(self, data, terminal_phase="infrastructure_failure",
                        terminal_outcome="infrastructure_failure",
                        exit_code=5, rounds_completed=0)

    def test_interrupted_audit_terminates_interrupted(self) -> None:
        ws = self.make({
            "planner": {"behavior": "planned"},
            "developer": {"behavior": "complete"},
            "tester": {"behavior": "pass"},
            "auditor": {"behavior": "crash"},
        })
        rc, data = ws.run_cli()
        self.assertEqual(rc, 4)
        assert_terminal(self, data, terminal_phase="interrupted",
                        terminal_outcome="interrupted", exit_code=4,
                        rounds_completed=0)
        assert_history(self, data, [
            (1, "planning", "planned"),
            (1, "implementation", "task_completed"),
            (1, "verification", "pass"),
            (1, "audit", "interrupted"),
        ])

    def test_work_exhausted_reaches_verification_and_audit(self) -> None:
        # §13.2/§14: empty runnable work reaches verification and audit
        # instead of spinning.
        ws = self.make({
            "planner": {"behavior": "planned-complete"},
            "developer": {"behavior": "complete"},
            "tester": {"behavior": "pass"},
            "auditor": {"behavior": "pass"},
        })
        rc, data = ws.run_cli()
        self.assertEqual(rc, 0)
        assert_terminal(self, data, terminal_phase="success",
                        terminal_outcome="pass", exit_code=0,
                        rounds_completed=1)
        assert_history(self, data, [
            (1, "planning", "planned"),
            (1, "implementation", "work_exhausted"),
            (1, "verification", "pass"),
            (1, "audit", "pass"),
        ])

    def test_blocked_plan_reaches_verification_and_audit(self) -> None:
        # §8/§13.2: a plan whose unfinished tasks are all explicitly blocked
        # on unavailable references classifies implementation as blocked and
        # still runs verification/audit.
        ws = self.make({
            "planner": {"behavior": "planned-blocked"},
            "developer": {"behavior": "complete"},
            "tester": {"behavior": "pass"},
            "auditor": {"behavior": "blocked"},
        })
        rc, data = ws.run_cli()
        self.assertEqual(rc, 2)
        assert_terminal(self, data, terminal_phase="blocked",
                        terminal_outcome="blocked", exit_code=2,
                        rounds_completed=1)
        assert_history(self, data, [
            (1, "planning", "planned"),
            (1, "implementation", "blocked"),
            (1, "verification", "pass"),
            (1, "audit", "blocked"),
        ])


class CampaignRecovery(_CampaignBase):
    """§17 crash reconciliation derived from Git + plan + state."""

    def test_planning_commit_before_transition_is_reconciled(self) -> None:
        ws = self.make(SUCCESS_SCENARIO)
        config = self._crash_at_plan(
            ws,
            lambda state: (
                state.current_phase == "implementation"
                and state.last_outcome == "planned"
            ),
        )
        # The planning commit landed; the state file still records the
        # planning phase at the old base — the recovery window.
        self.assertNotEqual(
            _git(ws.root, "rev-parse", "HEAD").stdout.strip(),
            ws.load_state().phase_base_commit,
        )
        self.assertEqual(ws.load_state().current_phase, "planning")
        # A fresh run reconciles the committed plan and continues to success.
        result = campaign_module.Campaign(config).run()
        self.assertEqual(result.terminal_phase, "success")
        self.assertEqual(result.terminal_outcome, "pass")

    def test_completion_commit_before_transition_is_reconciled(self) -> None:
        ws = self.make(SUCCESS_SCENARIO)
        config = self._crash_at_plan(
            ws,
            lambda state: (
                state.current_phase == "verification"
                and state.last_outcome == "task_completed"
            ),
        )
        # The developer's completion commit landed; the state file still says
        # implementation with the selected task.
        state = state_module.load_state(ws.root, expected_branch=BRANCH)
        self.assertEqual(state.current_phase, "implementation")
        self.assertEqual(state.selected_task_id, 1)
        # Re-run: recovery verifies the committed completion and advances.
        result = campaign_module.Campaign(config).run()
        self.assertEqual(result.terminal_phase, "success")
        outcomes = [r.outcome for r in result.phase_history]
        self.assertIn("task_completed", outcomes)

    def test_ambiguous_recovery_fails_closed(self) -> None:
        # A HEAD advanced past the phase base with a foreign commit during
        # planning fails closed instead of guessing.
        ws = self.make(SUCCESS_SCENARIO)
        config = self._crash_at_plan(
            ws,
            lambda state: (
                state.current_phase == "implementation"
                and state.last_outcome == "planned"
            ),
        )
        (ws.root / "foreign.txt").write_text("not role work\n", encoding="utf-8")
        _git(ws.root, "add", "foreign.txt")
        _git(ws.root, "commit", "-qm", "foreign scope")
        with self.assertRaises(campaign_module.CampaignRecoveryError):
            campaign_module.Campaign(config).run()


class EmptyWorkAndFindings(_CampaignBase):
    """§14/§16: findings and empty work flow through verification/audit."""

    def test_multi_task_campaign_completes_every_runnable_task(self) -> None:
        # §14: each round runs planning -> implementation -> verification ->
        # audit, and the planner revision of round N preserves the tasks the
        # earlier rounds completed.  The campaign therefore works through
        # every runnable task — one per round — and the final round's last
        # selection classifies as work_exhausted before verification/audit.
        ws = self.make(SUCCESS_SCENARIO, rounds=3)
        rc, data = ws.run_cli()
        self.assertEqual(rc, 0)
        self.assertEqual(data["rounds_completed"], 3)
        assert_history(self, data, [
            (1, "planning", "planned"),
            (1, "implementation", "task_completed"),
            (1, "verification", "pass"),
            (1, "audit", "pass"),
            (2, "planning", "planned"),
            (2, "implementation", "task_completed"),
            (2, "verification", "pass"),
            (2, "audit", "pass"),
            (3, "planning", "planned"),
            (3, "implementation", "task_completed"),
            (3, "verification", "pass"),
            (3, "audit", "pass"),
        ])

    def test_findings_reach_next_round_via_revised_plan(self) -> None:
        # Round 1 verification+audit findings; round 2's planner revises the
        # plan and the campaign completes.
        ws = self.make({
            "planner": {"behavior": "planned"},
            "developer": {"behavior": "complete"},
            "tester": {"behavior": {"1": "findings", "2": "pass",
                                     "default": "pass"}},
            "auditor": {"behavior": {"1": "findings", "2": "pass",
                                     "default": "pass"}},
        }, rounds=2)
        rc, data = ws.run_cli()
        self.assertEqual(rc, 0)
        assert_terminal(self, data, terminal_phase="success",
                        terminal_outcome="pass", exit_code=0,
                        rounds_completed=2)
        rounds = [r["round"] for r in data["phase_history"]]
        self.assertEqual(rounds, [1, 1, 1, 1, 2, 2, 2, 2])

    def test_planning_retries_within_budget_then_succeeds(self) -> None:
        # A planner that fails once (exit 1) then produces a plan on the
        # second attempt stays within the configured budget.
        ws = self.make({
            "planner": {"behavior": {"1.1": "exit1", "1.2": "planned",
                                     "default": "planned"}},
            "developer": {"behavior": "complete"},
            "tester": {"behavior": "pass"},
            "auditor": {"behavior": "pass"},
        })
        rc, data = ws.run_cli()
        self.assertEqual(rc, 0)
        assert_history(self, data, [
            (1, "planning", "failed"),
            (1, "planning", "planned"),
            (1, "implementation", "task_completed"),
            (1, "verification", "pass"),
            (1, "audit", "pass"),
        ])

    def test_clean_task_failure_reaches_verification(self) -> None:
        # A reproducible deterministic developer failure (nonzero exit with no
        # work) exhausts the attempt budget cleanly and then proceeds to
        # verification/audit at the last coherent commit (§13.3).
        ws = self.make({
            "planner": {"behavior": "planned"},
            "developer": {"behavior": "exit1"},
            "tester": {"behavior": "pass"},
            "auditor": {"behavior": "pass"},
        })
        rc, data = ws.run_cli()
        self.assertEqual(rc, 0)
        assert_history(self, data, [
            (1, "planning", "planned"),
            (1, "implementation", "task_failed"),
            (1, "implementation", "task_failed"),
            (1, "implementation", "task_failed"),
            (1, "verification", "pass"),
            (1, "audit", "pass"),
        ])

    def test_invalid_developer_plan_fails_closed(self) -> None:
        ws = self.make({
            "planner": {"behavior": "planned"},
            "developer": {"behavior": "invalid"},
            "tester": {"behavior": "pass"},
            "auditor": {"behavior": "pass"},
        })
        rc, data = ws.run_cli()
        self.assertEqual(rc, 0)
        self.assertEqual(
            [r["outcome"] for r in data["phase_history"][1:4]],
            ["task_failed", "task_failed", "task_failed"],
        )

    def test_acceptance_gate_rejects_missing_verification_file(self) -> None:
        # The default acceptance gate requires every verification reference
        # of the committed task to exist; a missing file fails the task.
        ws = self.make({
            "planner": {"behavior": "planned"},
            "developer": {"behavior": "complete-no-file"},
            "tester": {"behavior": "pass"},
            "auditor": {"behavior": "pass"},
        })
        rc, data = ws.run_cli()
        self.assertEqual(rc, 0)
        self.assertEqual(data["phase_history"][1]["outcome"], "task_failed")

    def test_task_progress_retries_then_completes(self) -> None:
        ws = self.make({
            "planner": {"behavior": "planned"},
            "developer": {"behavior": {"1.1": "progress", "1.2": "complete",
                                       "default": "complete"}},
            "tester": {"behavior": "pass"},
            "auditor": {"behavior": "pass"},
        })
        rc, data = ws.run_cli()
        self.assertEqual(rc, 0)
        assert_history(self, data, [
            (1, "planning", "planned"),
            (1, "implementation", "task_progress"),
            (1, "implementation", "task_completed"),
            (1, "verification", "pass"),
            (1, "audit", "pass"),
        ])

    def test_verification_gate_failure_is_findings(self) -> None:
        ws = self.make({
            "planner": {"behavior": "planned"},
            "developer": {"behavior": "complete"},
            "tester": {"behavior": "pass"},
            "auditor": {"behavior": "findings"},
        })
        rc, data = ws.run_cli(
            extra=["--verification-command", str(FALSE_EXECUTABLE)])
        self.assertEqual(rc, 1)
        self.assertEqual(data["phase_history"][2]["phase"], "verification")
        self.assertEqual(data["phase_history"][2]["outcome"], "findings")

    def test_absent_verification_command_fails_closed(self) -> None:
        # Task 9 review MED: verification requires an explicit deterministic
        # verification command — the tester's structured result alone never
        # gates.  A campaign without a verification command fails closed as
        # infrastructure_failure even when the tester reports pass.
        ws = self.make(SUCCESS_SCENARIO)
        config = dataclasses.replace(
            ws.derive_config(), verification_command=())
        result = campaign_module.Campaign(config).run()
        self.assertEqual(result.terminal_phase, "infrastructure_failure")
        outcomes = [(r.phase, r.outcome) for r in result.phase_history]
        self.assertIn(("verification", "infrastructure_failure"), outcomes)
        self.assertNotIn(("audit", "pass"), outcomes)

    def test_verification_blocked_requires_declared_capability(self) -> None:
        # A tester blocked result with exact references becomes a genuine
        # verification blocked only when the declared capability probe fails;
        # without a capability command the blocked claim is a finding.
        ws = self.make({
            "planner": {"behavior": "planned"},
            "developer": {"behavior": "complete"},
            "tester": {"behavior": "blocked"},
            "auditor": {"behavior": "blocked"},
        })
        rc, data = ws.run_cli(
            extra=["--capability-command", str(FALSE_EXECUTABLE)])
        self.assertEqual(rc, 2)
        self.assertEqual(data["phase_history"][2]["outcome"], "blocked")
        self.assertEqual(data["phase_history"][3]["outcome"], "blocked")

        ws2 = self.make({
            "planner": {"behavior": "planned"},
            "developer": {"behavior": "complete"},
            "tester": {"behavior": "blocked"},
            "auditor": {"behavior": "findings"},
        })
        rc2, data2 = ws2.run_cli()
        self.assertEqual(rc2, 1)
        self.assertEqual(data2["phase_history"][2]["outcome"], "findings")


class ScopeAndGit(_CampaignBase):
    """§12 scope authority, commit boundary, and dirty preservation."""

    def test_planner_scope_violation_terminates_failed(self) -> None:
        ws = self.make({
            "planner": {"behavior": "scope"},
            "developer": {"behavior": "complete"},
            "tester": {"behavior": "pass"},
            "auditor": {"behavior": "pass"},
        })
        rc, data = ws.run_cli()
        self.assertEqual(rc, 3)
        self.assertEqual(data["terminal_phase"], "failed")
        # The foreign dirty file is preserved, never reset.
        self.assertTrue((ws.root / "src" / "planner-touched.py").exists())

    def test_developer_scope_violation_dirty_exhaustion_interrupts(self) -> None:
        ws = self.make({
            "planner": {"behavior": "planned"},
            "developer": {"behavior": "scope"},
            "tester": {"behavior": "pass"},
            "auditor": {"behavior": "pass"},
        })
        rc, data = ws.run_cli()
        self.assertEqual(rc, 4)
        self.assertEqual(data["terminal_phase"], "interrupted")
        # The harness-state violation is preserved, never removed.
        self.assertTrue((ws.root / ".factory" / "config.toml").exists())

    def test_developer_cannot_commit_trusted_policy_surface(self) -> None:
        # Task 9 review HIGH: an untrusted developer that tampers with the
        # trusted policy/harness surface (operational policy, harness docs,
        # legacy security scripts) is a deterministic scope violation: the
        # dirty work is preserved but the orchestrator never commits any of
        # it, and the campaign fails closed on budget exhaustion.
        ws = self.make({
            "planner": {"behavior": "planned"},
            "developer": {"behavior": "scope-policy"},
            "tester": {"behavior": "pass"},
            "auditor": {"behavior": "pass"},
        })
        rc, data = ws.run_cli()
        self.assertEqual(rc, 4)
        self.assertEqual(data["terminal_phase"], "interrupted")
        # Every tampered policy/harness path stays dirty and preserved.
        for rel in ("AGENTS.md", "docs/FACTORY.md", "scripts/guard.sh"):
            self.assertTrue((ws.root / rel).exists())
        self.assertIn(
            "dirty fixture work",
            (ws.root / "AGENTS.md").read_text(encoding="utf-8"),
        )
        # No campaign commit after the scenario touches the policy surface.
        self.assertIsNotNone(ws.scenario_commit)
        heads = _git(ws.root, "log", f"{ws.scenario_commit}..HEAD",
                     "--format=%H").stdout.splitlines()
        for entry in heads:
            files = _git(ws.root, "diff-tree", "--no-commit-id",
                         "--name-only", "-r", entry).stdout.splitlines()
            for path in files:
                self.assertNotIn(
                    path,
                    ("AGENTS.md", "docs/FACTORY.md", "scripts/guard.sh"),
                    f"campaign commit touched trusted policy path {path!r}",
                )

    def test_dirty_work_is_preserved(self) -> None:
        # §17: dirty work is never reset, discarded, or silently overwritten.
        ws = self.make({
            "planner": {"behavior": "planned"},
            "developer": {"behavior": "crash"},
            "tester": {"behavior": "pass"},
            "auditor": {"behavior": "pass"},
        })
        rc, data = ws.run_cli()
        self.assertEqual(rc, 4)
        work = ws.root / "src" / "work-1.md"
        self.assertTrue(work.exists())
        self.assertIn("dirty fixture work", work.read_text(encoding="utf-8"))

    def test_every_campaign_commit_is_orchestrator_authored(self) -> None:
        ws = self.make(SUCCESS_SCENARIO)
        rc, _ = ws.run_cli()
        self.assertEqual(rc, 0)
        self.assertIsNotNone(ws.scenario_commit)
        # Only the commits the orchestrator created during the campaign are
        # in scope; the fixture's own setup commits carry the fixture
        # identity and predate the scenario fixture.
        log = _git(ws.root, "log", f"{ws.scenario_commit}..HEAD",
                   "--format=%an <%ae>").stdout.splitlines()
        self.assertTrue(log)
        for entry in log:
            self.assertEqual(
                entry, "factory-campaign <factory-campaign@localhost>",
                msg=f"unexpected commit author {entry!r}")

    def test_committed_scope_is_exactly_the_allowed_paths(self) -> None:
        ws = self.make(SUCCESS_SCENARIO)
        rc, _ = ws.run_cli()
        self.assertEqual(rc, 0)
        self.assertIsNotNone(ws.scenario_commit)
        heads = _git(ws.root, "log", f"{ws.scenario_commit}..HEAD",
                     "--format=%H").stdout.splitlines()
        self.assertTrue(heads)
        for entry in heads:
            files = _git(ws.root, "diff-tree", "--no-commit-id",
                         "--name-only", "-r", entry).stdout.splitlines()
            for path in files:
                self.assertFalse(
                    path.startswith(".git/"),
                    f"campaign commit touched {path!r}")
                self.assertTrue(
                    path == PLAN_REL or not path.startswith(".factory/"),
                    f"campaign commit touched harness path {path!r}")

    def test_no_foreign_commit_during_campaign(self) -> None:
        # The only commits after the scenario fixture are orchestrator
        # commits; the fixture role never runs Git (the driver performs no
        # git operation at all).
        ws = self.make(SUCCESS_SCENARIO)
        rc, _ = ws.run_cli()
        self.assertEqual(rc, 0)
        messages = _git(ws.root, "log", "--format=%s").stdout.splitlines()
        for message in messages:
            self.assertTrue(
                message.startswith("factory-campaign: ")
                or message in ("fixture base", "fixture plan and driver",
                               "scenario fixture"),
                msg=f"unexpected commit {message!r}")


class LifecycleAndCli(_CampaignBase):
    """§11 one-lifecycle surface, committed schema, and CLI behavior."""

    def test_no_runtime_ledger_is_created(self) -> None:
        ws = self.make(SUCCESS_SCENARIO)
        rc, _ = ws.run_cli()
        self.assertEqual(rc, 0)
        names = sorted(path.name for path in (ws.root / STATE_DIR).iterdir())
        self.assertEqual(names, [
            "campaign-result-campaign.json",
            "factory-loop.json",
            "state-digest-ledger.jsonl",
        ])

    def test_published_result_conforms_to_committed_schema(self) -> None:
        ws = self.make(SUCCESS_SCENARIO)
        rc, data = ws.run_cli()
        self.assertEqual(rc, 0)
        published = json.loads(
            ws.result_file().read_text(encoding="utf-8"))
        campaign_module.validate_campaign_result(published)
        # The published bytes equal the CLI's printed result (single writer).
        self.assertEqual(published, data)

    def test_control_state_is_exactly_the_section11_field_set(self) -> None:
        ws = self.make(SUCCESS_SCENARIO)
        rc, _ = ws.run_cli()
        self.assertEqual(rc, 0)
        state = ws.load_state()
        keys = sorted(state.to_dict())
        self.assertEqual(keys, sorted(state_module.FIELD_NAMES))

    def test_write_once_campaign_binding_fails_closed(self) -> None:
        ws = self.make(SUCCESS_SCENARIO)
        rc, _ = ws.run_cli()
        self.assertEqual(rc, 0)
        # A second campaign with a different id must not clobber the binding.
        rc2, data2 = ws.run_cli(campaign_id="other")
        self.assertEqual(rc2, 6)
        self.assertIsNone(data2)

    def test_terminal_state_refuses_to_rerun(self) -> None:
        ws = self.make(SUCCESS_SCENARIO)
        rc, _ = ws.run_cli()
        self.assertEqual(rc, 0)
        config = ws.derive_config()
        with self.assertRaises(campaign_module.CampaignPhaseError):
            campaign_module.Campaign(config).run()

    def test_cli_rejects_unknown_provider(self) -> None:
        ws = self.make(SUCCESS_SCENARIO)
        result = run(
            [sys.executable, str(LOOP / "campaign.py"),
             "--root", str(ws.root), "run",
             "--campaign-id", "x", "--rounds", "1",
             "--branch", BRANCH, "--provider", "bogus"],
            root=ROOT, check=False)
        self.assertEqual(result.returncode, 6)
        self.assertIn("factory-campaign:", result.stderr)

    def test_cli_main_catches_git_boundary_error(self) -> None:
        # Task 9 review MED: a pinned-Git failure (timeout, missing binary,
        # broken pipe) during config derivation is a clean fail-closed
        # control-plane error, never an unhandled GitBoundaryError traceback.
        ws = self.make(SUCCESS_SCENARIO)
        import contextlib
        import io
        with unittest.mock.patch.object(
            campaign_module.gitutil, "git_run",
            side_effect=campaign_module.gitutil.GitBoundaryError(
                "pinned Git hung"),
        ):
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                rc = campaign_module.main([
                    "--root", str(ws.root), "run",
                    "--campaign-id", "x", "--rounds", "1",
                    "--branch", BRANCH,
                ])
        self.assertEqual(rc, 6)
        self.assertIn("pinned Git hung", stderr.getvalue())

    def test_cli_rejects_role_driver_with_ollama_provider(self) -> None:
        ws = self.make(SUCCESS_SCENARIO)
        result = run(
            [sys.executable, str(LOOP / "campaign.py"),
             "--root", str(ws.root), "run",
             "--campaign-id", "x", "--rounds", "1",
             "--branch", BRANCH, "--provider", "ollama",
             "--role-driver", DRIVER_REL],
            root=ROOT, check=False)
        self.assertEqual(result.returncode, 6)
        self.assertIn("fixture surface", result.stderr)

    def test_cli_help_and_show(self) -> None:
        result = run(
            [sys.executable, str(LOOP / "campaign.py"), "--help"],
            root=ROOT, check=False)
        self.assertEqual(result.returncode, 0)
        self.assertIn("factory-campaign", result.stdout)
        # show on a repository without control state fails closed.
        empty = self.tmp / "empty"
        empty.mkdir()
        result = run(
            [sys.executable, str(LOOP / "campaign.py"),
             "--root", str(empty), "show"],
            root=ROOT, check=False)
        self.assertEqual(result.returncode, 6)

    def test_malformed_phase_result_fails_closed(self) -> None:
        ws = self.make(SUCCESS_SCENARIO)
        bad = ws.root / STATE_DIR / "bad-result.json"
        bad.parent.mkdir(parents=True, exist_ok=True)
        bad.write_text(
            '{"schema": "factory-phase-result/v1", "outcome": "bogus"}',
            encoding="utf-8")
        with self.assertRaises(campaign_module.CampaignResultError):
            campaign_module.read_phase_result(
                ws.root, f"{STATE_DIR}/bad-result.json", "fixture")

    def test_result_model_validation_fails_closed(self) -> None:
        result = campaign_module.CampaignResult(
            campaign_id="campaign", rounds_requested=1, rounds_completed=0,
            terminal_phase="success", terminal_outcome="pass",
            head_commit="0" * 40,
            phase_history=(
                campaign_module.PhaseRecord(
                    round=1, phase="audit", attempt=1, outcome="bogus",
                    head_commit="0" * 40, plan_digest="0" * 64,
                ),
            ),
        )
        with self.assertRaises(campaign_module.CampaignResultError):
            result.validate()

    def test_config_validation_fails_closed(self) -> None:
        ws = self.make(SUCCESS_SCENARIO)
        config = ws.derive_config()
        with self.assertRaises(campaign_module.CampaignConfigError):
            dataclasses.replace(config, rounds_requested=0)
        with self.assertRaises(campaign_module.CampaignConfigError):
            dataclasses.replace(config, planning_attempts=0)
        with self.assertRaises(campaign_module.CampaignConfigError):
            dataclasses.replace(config, provider="bogus")

    def test_unbound_plan_is_rejected_at_planning(self) -> None:
        # A planner revision that changes the bound base commit is unbound and
        # fails the planning phase (PLAN-01 acceptance boundary).
        ws = self.make({
            "planner": {"behavior": "planned-unbound"},
            "developer": {"behavior": "complete"},
            "tester": {"behavior": "pass"},
            "auditor": {"behavior": "pass"},
        })
        rc, data = ws.run_cli()
        self.assertEqual(rc, 3)
        self.assertEqual(data["terminal_phase"], "failed")


class ClassificationUnits(_CampaignBase):
    """§13 classification is a pure function of trusted inputs."""

    def test_planning_classification(self) -> None:
        planned = campaign_module.RoleOutcome("planner", 0)
        self.assertEqual(campaign_module.classify_planning(
            role=planned, plan_changed=True, plan_valid=True, scope_ok=True),
            "planned")
        self.assertEqual(campaign_module.classify_planning(
            role=planned, plan_changed=False, plan_valid=True, scope_ok=True),
            "failed")
        self.assertEqual(campaign_module.classify_planning(
            role=planned, plan_changed=True, plan_valid=False, scope_ok=True),
            "failed")
        interrupted = campaign_module.RoleOutcome(
            "planner", -9, interrupted=True, signal="SIGKILL")
        self.assertEqual(campaign_module.classify_planning(
            role=interrupted, plan_changed=False, plan_valid=True,
            scope_ok=True), "interrupted")

    def test_implementation_classification(self) -> None:
        ok = campaign_module.RoleOutcome("developer", 0)
        base = dict(role=ok, plan_valid=True, task_complete=False,
                    acceptance_pass=False, had_changes=True, scope_ok=True)
        self.assertEqual(campaign_module.classify_implementation(
            **{**base, "task_complete": True, "acceptance_pass": True,
               "had_changes": True}), "task_completed")
        self.assertEqual(campaign_module.classify_implementation(
            **{**base, "task_complete": True, "acceptance_pass": False,
               "had_changes": True}), "task_failed")
        self.assertEqual(campaign_module.classify_implementation(**base),
                         "task_progress")
        self.assertEqual(campaign_module.classify_implementation(
            **{**base, "had_changes": False}), "task_failed")
        self.assertEqual(campaign_module.classify_implementation(
            **{**base, "scope_ok": False}), "task_failed")

    def test_verification_classification(self) -> None:
        ok = campaign_module.RoleOutcome("tester", 0)
        base = dict(role=ok, scope_ok=True, gate_ran=True, gate_exit=0,
                    tester_result_valid=True, tester_result_outcome="pass",
                    blocked_refs=(), capability_available=True)
        self.assertEqual(campaign_module.classify_verification(**base), "pass")
        self.assertEqual(campaign_module.classify_verification(
            **{**base, "gate_exit": 1}), "findings")
        self.assertEqual(campaign_module.classify_verification(
            **{**base, "tester_result_outcome": "findings"}), "findings")
        # Task 9 review MED: an absent deterministic verification command is
        # an unrun gate — the tester JSON alone never gates.
        self.assertEqual(campaign_module.classify_verification(
            **{**base, "gate_ran": False}), "infrastructure_failure")
        self.assertEqual(campaign_module.classify_verification(
            **{**base, "gate_ran": False,
               "tester_result_outcome": "findings"}),
            "infrastructure_failure")
        self.assertEqual(campaign_module.classify_verification(
            **{**base, "tester_result_outcome": "blocked",
               "blocked_refs": ["ext"], "capability_available": False}),
            "blocked")
        self.assertEqual(campaign_module.classify_verification(
            **{**base, "tester_result_outcome": "blocked",
               "blocked_refs": ["ext"], "capability_available": True}),
            "findings")
        self.assertEqual(campaign_module.classify_verification(
            **{**base, "tester_result_valid": False}),
            "infrastructure_failure")

    def test_audit_classification(self) -> None:
        ok = campaign_module.RoleOutcome("auditor", 0)
        self.assertEqual(campaign_module.classify_audit(
            role=ok, scope_ok=True, result_valid=True, outcome="pass",
            findings=(), blocked_refs=()), "pass")
        self.assertEqual(campaign_module.classify_audit(
            role=ok, scope_ok=True, result_valid=True, outcome="pass",
            findings=("f",), blocked_refs=()), "findings")
        self.assertEqual(campaign_module.classify_audit(
            role=ok, scope_ok=True, result_valid=True, outcome="pass",
            findings=(), blocked_refs=("b",)), "blocked")
        self.assertEqual(campaign_module.classify_audit(
            role=ok, scope_ok=True, result_valid=True, outcome="pass",
            findings=("f",), blocked_refs=("b",)), "findings")
        interrupted = campaign_module.RoleOutcome(
            "auditor", -9, interrupted=True, signal="SIGKILL")
        self.assertEqual(campaign_module.classify_audit(
            role=interrupted, scope_ok=True, result_valid=True,
            outcome=None, findings=(), blocked_refs=()), "interrupted")


class ReviewHardening(_CampaignBase):
    """Task 9 review remediation adversarial tests (B1/B2/M1/M2/L1/L2/L3/L4)."""

    # -- B1: persisted audit terminal + rerun refusal -------------------------

    def test_interrupted_audit_is_persisted_and_refuses_rerun(self) -> None:
        # An interrupted audit ends the campaign and is persisted in the
        # authoritative control state as the terminal `interrupted` phase
        # (the round never advances); a later run refuses to re-execute it.
        ws = self.make({
            "planner": {"behavior": "planned"},
            "developer": {"behavior": "complete"},
            "tester": {"behavior": "pass"},
            "auditor": {"behavior": "crash"},
        })
        rc, data = ws.run_cli()
        self.assertEqual(rc, 4)
        self.assertEqual(data["terminal_phase"], "interrupted")
        state = ws.load_state()
        self.assertEqual(state.current_phase, "interrupted")
        self.assertEqual(state.last_outcome, "interrupted")
        self.assertEqual(state.current_round, 1)
        head_before = _git(ws.root, "rev-parse", "HEAD").stdout.strip()
        with self.assertRaises(campaign_module.CampaignPhaseError) as cm:
            campaign_module.Campaign(ws.derive_config()).run()
        self.assertIn("already terminal", str(cm.exception))
        # The refused rerun creates no commit and never re-executes the audit.
        self.assertEqual(
            _git(ws.root, "rev-parse", "HEAD").stdout.strip(), head_before)
        rc2, data2 = ws.run_cli()
        self.assertEqual(rc2, 6)
        self.assertIsNone(data2)

    def test_untrusted_audit_is_persisted_and_refuses_rerun(self) -> None:
        # An untrusted audit (infrastructure_failure) is likewise a terminal
        # fail-closed close persisted in the control state.
        ws = self.make({
            "planner": {"behavior": "planned"},
            "developer": {"behavior": "complete"},
            "tester": {"behavior": "pass"},
            "auditor": {"behavior": "dirty"},
        })
        rc, data = ws.run_cli()
        self.assertEqual(rc, 5)
        self.assertEqual(data["terminal_phase"], "infrastructure_failure")
        state = ws.load_state()
        self.assertEqual(state.current_phase, "infrastructure_failure")
        self.assertEqual(state.last_outcome, "infrastructure_failure")
        self.assertEqual(state.current_round, 1)
        with self.assertRaises(campaign_module.CampaignPhaseError) as cm:
            campaign_module.Campaign(ws.derive_config()).run()
        self.assertIn("already terminal", str(cm.exception))

    # -- B2: production (non-driver) launch re-derives exact bytes -----------

    def _production_config(self, ws: FixtureWorkspace):
        # The real (non-driver) launch path requires a committed absolute
        # backend; the fixture driver file is an existing committed regular
        # file, and authorize_launch is mocked so no process is spawned.
        return dataclasses.replace(
            ws.derive_config(), backend=str(ws.root / DRIVER_REL))

    def test_production_developer_launch_derives_exact_committed_task_bytes(
        self,
    ) -> None:
        # B2: when the hidden suite does not supply the excerpt, the
        # production launch re-derives the developer's task bytes from the
        # committed plan blob at the bound commit and binds their exact
        # digest — never a self-claimed or operator-supplied byte set.
        ws = self.make(SUCCESS_SCENARIO)
        config = self._production_config(ws)
        head = _git(ws.root, "rev-parse", "HEAD").stdout.strip()
        plan_blob = campaign_module._blob_at(ws.root, config.plan_path)
        expected_excerpt, expected_digest = launch_module.derive_task_excerpt(
            plan_blob, 1)
        captured: dict = {}

        def _authorize(binding, **kwargs):
            captured["task_excerpt"] = kwargs["task_excerpt"]
            captured["task_excerpt_digest"] = binding.task_excerpt_digest
            captured["plan_bytes"] = kwargs["plan"]
            captured["plan_digest"] = binding.plan_digest
            captured["bound_commit"] = binding.bound_commit
            return object()

        class _Supervisor:
            def __init__(self, binding):
                self.binding = binding

            def run(self, authority):
                return type("_Result", (), {
                    "outcome": "exited", "returncode": 0,
                })()

        with unittest.mock.patch.object(
            campaign_module.launch_module, "authorize_launch",
            side_effect=_authorize,
        ), unittest.mock.patch.object(
            campaign_module.launch_module, "LaunchSupervision",
            side_effect=_Supervisor,
        ):
            outcome = campaign_module.launch_role_attempt(
                config, role="developer", head=head, task_id=1,
                _confinement_proof=object(),
            )
        self.assertEqual(outcome.exit_status, 0)
        self.assertFalse(outcome.interrupted)
        self.assertEqual(captured["task_excerpt"], expected_excerpt)
        self.assertEqual(captured["task_excerpt_digest"], expected_digest)
        self.assertEqual(captured["task_excerpt_digest"], sha256(expected_excerpt))
        self.assertEqual(captured["plan_bytes"], plan_blob)
        self.assertEqual(captured["plan_digest"], sha256(plan_blob))
        self.assertEqual(captured["bound_commit"], head)

    def test_production_auditor_derives_exact_committed_objective_bytes(
        self,
    ) -> None:
        # B2: the auditor objective is re-derived from the committed
        # audit-objective registry with the deterministic §6.4 selection and
        # the exact deterministic JSON encoding the registry CLI prints.
        ws = self.make(SUCCESS_SCENARIO)
        config = self._production_config(ws)
        head = _git(ws.root, "rev-parse", "HEAD").stdout.strip()
        registry = campaign_module._blob_at(
            ws.root, ".factory/audit-objectives/registry.json")
        document = audit_objectives_module.parse_registry(registry)
        objective = audit_objectives_module.select_audit_objective(2, document)
        expected = json.dumps(
            dict(objective), sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        captured: dict = {}

        def _authorize(binding, **kwargs):
            captured["audit_objective"] = kwargs["audit_objective"]
            captured["audit_objective_digest"] = binding.audit_objective_digest
            return object()

        class _Supervisor:
            def __init__(self, binding):
                self.binding = binding

            def run(self, authority):
                return type("_Result", (), {
                    "outcome": "exited", "returncode": 0,
                })()

        with unittest.mock.patch.object(
            campaign_module.launch_module, "authorize_launch",
            side_effect=_authorize,
        ), unittest.mock.patch.object(
            campaign_module.launch_module, "LaunchSupervision",
            side_effect=_Supervisor,
        ):
            outcome = campaign_module.launch_role_attempt(
                config, role="auditor", head=head, round_number=2,
                _confinement_proof=object(),
            )
        self.assertEqual(outcome.exit_status, 0)
        self.assertEqual(captured["audit_objective"], expected)
        self.assertEqual(captured["audit_objective_digest"], sha256(expected))

    def test_production_launch_invocation_error_is_clean_campaign_error(
        self,
    ) -> None:
        # B2: every InvocationError of the production launch (a refused
        # launch) is a clean, documented CampaignPhaseError — never an
        # unhandled traceback.
        ws = self.make(SUCCESS_SCENARIO)
        config = self._production_config(ws)
        head = _git(ws.root, "rev-parse", "HEAD").stdout.strip()
        with unittest.mock.patch.object(
            campaign_module.launch_module, "authorize_launch",
            side_effect=launch_module.InvocationError("guard refusal"),
        ):
            with self.assertRaises(campaign_module.CampaignPhaseError) as cm:
                campaign_module.launch_role_attempt(
                    config, role="developer", head=head, task_id=1,
                    _confinement_proof=object(),
                )
        self.assertIn("guard refusal", str(cm.exception))
        # A malformed invocation binding is a clean campaign error too.
        with unittest.mock.patch.object(
            campaign_module.launch_module, "verify_invocation",
            side_effect=launch_module.InvocationError("unbound bytes"),
        ):
            with self.assertRaises(campaign_module.CampaignPhaseError) as cm2:
                campaign_module.launch_role_attempt(
                    config, role="developer", head=head, task_id=1,
                    _confinement_proof=object(),
                )
        self.assertIn("unbound bytes", str(cm2.exception))
        # A task id absent from the committed plan refuses the launch cleanly.
        with self.assertRaises(campaign_module.CampaignPhaseError) as cm3:
            campaign_module.launch_role_attempt(
                config, role="developer", head=head, task_id=999,
                _confinement_proof=object(),
            )
        self.assertIn("no Task 999", str(cm3.exception))

    # -- M1: the acceptance gate validates the exact newly validated plan ----

    def test_acceptance_gate_validates_the_exact_new_plan_not_the_stale_head(
        self,
    ) -> None:
        # M1: the default gate operates on the exact newly validated (worktree)
        # plan argument, never the stale pre-commit head plan.  A newly
        # validated plan whose verification reference differs from the
        # committed plan must be honored — a reference missing from the new
        # plan fails the gate even though the committed plan's reference
        # exists.
        ws = self.make(SUCCESS_SCENARIO)
        campaign = campaign_module.Campaign(ws.derive_config())
        text = (ws.root / PLAN_REL).read_text(encoding="utf-8")
        committed = campaign_module.plan_parser.Plan.from_bytes(text.encode())
        committed_task = next(t for t in committed.tasks if t.number == 1)
        self.assertIn("src/work-1.md", committed_task.fields["Verification"])
        (ws.root / "src" / "work-1.md").write_text("work", encoding="utf-8")
        ok, _ = campaign._acceptance_gate(1, committed)
        self.assertTrue(ok)
        # The exact newly validated plan changes the reference to a file the
        # worktree does not have: the gate must consult the new plan and fail,
        # even though the committed plan's reference exists.
        revised = text.replace(
            "- Verification: `src/work-1.md`",
            "- Verification: `src/work-missing.md`",
            1,
        )
        work_plan = campaign_module.plan_parser.Plan.from_bytes(
            revised.encode())
        ok, detail = campaign._acceptance_gate(1, work_plan)
        self.assertFalse(ok)
        self.assertIn("missing verification references", detail)
        self.assertIn("src/work-missing.md", detail)
        # The committed (stale head) plan still passes on its own, proving the
        # failure came from validating the exact newly validated plan.
        ok, _ = campaign._acceptance_gate(1, committed)
        self.assertTrue(ok)

    # -- M2: the selector is bound to the authoritative plan base ------------

    def test_selector_is_bound_to_the_authoritative_plan_base(self) -> None:
        # M2: a committed plan whose front-matter base_commit drifted from
        # the authoritative base anchored at the state's phase base fails
        # closed at implementation selection/recovery instead of selecting a
        # task from a stale plan.
        ws = self.make(SUCCESS_SCENARIO)
        self._crash_at_plan(
            ws,
            lambda state: (
                state.current_phase == "verification"
                and state.last_outcome == "task_completed"
            ),
        )
        plan_path = ws.root / PLAN_REL
        text = plan_path.read_text(encoding="utf-8")
        tampered = re.sub(
            r"^base_commit: [0-9a-f]{40}$",
            "base_commit: " + "1" * 40,
            text, count=1, flags=re.M,
        )
        plan_path.write_text(tampered, encoding="utf-8")
        _git(ws.root, "add", PLAN_REL)
        _git(ws.root, "commit", "-qm", "tampered stale plan")
        with self.assertRaises(campaign_module.CampaignRecoveryError) as cm:
            campaign_module.Campaign(ws.derive_config()).run()
        self.assertIn("stale", str(cm.exception))

    def test_selector_call_uses_the_authoritative_base_binding(self) -> None:
        # M2 wiring: _step_implementation passes the authoritative base derived
        # from the state's phase base (never the plan's self-declared base) to
        # the selector, and a stale/ambiguous selection fails the step closed.
        ws = self.make(SUCCESS_SCENARIO)
        config = ws.derive_config()
        campaign = campaign_module.Campaign(config)
        campaign._acquire()
        try:
            head = campaign._git.head()
            plan_blob = campaign._git.blob_at(head, config.plan_path)
            state = state_module.init_state(
                ws.root,
                campaign_id=config.campaign_id,
                rounds_requested=config.rounds_requested,
                specification_digest=config.specification_digest,
                plan_digest=config.plan_digest,
                role_prompt_digests=dict(config.role_prompt_digests),
                audit_objectives_digest=config.audit_objectives_digest,
                phase_base_commit=config.phase_base_commit,
                branch=config.branch,
            )
            state = state_module.advance(
                state, "planned",
                plan_digest=campaign_module.plan_sha256(plan_blob),
                phase_base_commit=head,
            )
            self.assertEqual(state.current_phase, "implementation")
            authoritative = campaign._authoritative_plan_base(state)
            recorded: dict = {}

            def _fake_select(plan, *, bound_base_commit=None):
                recorded["bound"] = bound_base_commit
                recorded["plan_base"] = plan.base_commit
                raise campaign_module.selector_module.SelectorError(
                    "stale plan")

            with unittest.mock.patch.object(
                campaign_module.selector_module, "select_task",
                side_effect=_fake_select,
            ):
                with self.assertRaises(campaign_module.CampaignRecoveryError) as cm:
                    campaign._step_implementation(state)
            self.assertIn("stale or ambiguous", str(cm.exception))
        finally:
            campaign._lock.release()
        self.assertEqual(recorded["bound"], authoritative)
        self.assertEqual(recorded["bound"], recorded["plan_base"])

    # -- L1: status entries never guess how to stage a move/gitlink ---------

    def test_status_rejects_rename_and_copy_records(self) -> None:
        ws = self.make(SUCCESS_SCENARIO)
        campaign = campaign_module.Campaign(ws.derive_config())
        (ws.root / "src" / "tracked.txt").write_text("x", encoding="utf-8")
        _git(ws.root, "add", "src/tracked.txt")
        _git(ws.root, "commit", "-qm", "tracked fixture file")
        campaign._acquire()
        try:
            git = campaign._git
            _git(ws.root, "mv", "src/tracked.txt", "src/renamed.txt")
            with self.assertRaises(campaign_module.CampaignGitError) as cm:
                git.status_entries()
            self.assertIn("rename/copy", str(cm.exception))
            _git(ws.root, "reset", "--hard", "-q")
            # A copy record is rejected the same way (crafted porcelain bytes;
            # with `-z` a real rename/copy is `XY old\0new\0`).
            class _Bytes:
                def __init__(self, payload):
                    self.stdout = payload
                    self.returncode = 0

            with unittest.mock.patch.object(
                type(git), "_bytes",
                return_value=_Bytes(b"C  src/old\x00src/new\x00"),
            ):
                with self.assertRaises(campaign_module.CampaignGitError) as cm2:
                    git.status_entries()
            self.assertIn("rename/copy", str(cm2.exception))
        finally:
            campaign._lock.release()

    def test_status_rejects_directory_marker_and_gitlink(self) -> None:
        ws = self.make(SUCCESS_SCENARIO)
        campaign = campaign_module.Campaign(ws.derive_config())
        campaign._acquire()
        try:
            git = campaign._git
            # A whole-directory untracked marker (untracked nested repository)
            # is never an explicit file path.
            (ws.root / "nested").mkdir()
            _git(ws.root, "init", "-q", str(ws.root / "nested"))
            with self.assertRaises(campaign_module.CampaignGitError) as cm:
                git.status_entries()
            self.assertIn("whole-directory", str(cm.exception))
            shutil.rmtree(ws.root / "nested")
            # A tracked gitlink (submodule pointer, mode 160000) among the
            # dirty paths is rejected: the orchestrator never stages a
            # submodule pointer.
            head = _git(ws.root, "rev-parse", "HEAD").stdout.strip()
            _git(ws.root, "update-index", "--add", "--cacheinfo",
                 "160000,%s,sub" % head)
            _git(ws.root, "commit", "-qm", "add gitlink")
            _git(ws.root, "update-index", "--cacheinfo",
                 "160000,%s,sub" % ws.scenario_commit)
            with self.assertRaises(campaign_module.CampaignGitError) as cm2:
                git.status_entries()
            self.assertIn("gitlink", str(cm2.exception))
        finally:
            campaign._lock.release()

    def test_status_rejects_quoted_and_malformed_entries(self) -> None:
        # Defensive parser contract: porcelain bytes that would be ambiguous
        # (a quoted path, a malformed record) are rejected deterministically.
        ws = self.make(SUCCESS_SCENARIO)
        campaign = campaign_module.Campaign(ws.derive_config())
        campaign._acquire()
        try:
            git = campaign._git

            class _Bytes:
                def __init__(self, payload):
                    self.stdout = payload
                    self.returncode = 0

            for payload in (
                b'?? "quoted path"\x00',
                b"??\x00",
                b"?? \x00",
            ):
                with self.assertRaises(campaign_module.CampaignGitError):
                    with unittest.mock.patch.object(
                        type(git), "_bytes", return_value=_Bytes(payload),
                    ):
                        git.status_entries()
        finally:
            campaign._lock.release()

    # -- L2: verification references never escape the repository ------------

    def test_unsafe_verification_references_are_rejected(self) -> None:
        ws = self.make(SUCCESS_SCENARIO)
        campaign = campaign_module.Campaign(ws.derive_config())
        plan = campaign_module.plan_parser.Plan.from_bytes(
            (ws.root / PLAN_REL).read_bytes())
        task = next(t for t in plan.tasks if t.number == 1)
        (ws.root / "src" / "work-1.md").write_text("x", encoding="utf-8")
        task.fields["Verification"] = "`src/work-1.md`"
        ok, _ = campaign._acceptance_gate(1, plan)
        self.assertTrue(ok)
        for unsafe in (
            "`/etc/passwd`",
            "`../escape.md`",
            "`src/../escape.md`",
            "`src//work.md`",
            "`src/./work.md`",
            "`src/work file.md`",
        ):
            task.fields["Verification"] = unsafe
            ok, detail = campaign._acceptance_gate(1, plan)
            self.assertFalse(ok)
            self.assertIn("unsafe verification reference", detail)
        # Task 9 review LOW: a whitespace-bearing reference is rejected as
        # unsafe rather than silently skipped (it cannot name one exact
        # repository-relative path).
        task.fields["Verification"] = "`src/work file.md`"
        ok, detail = campaign._acceptance_gate(1, plan)
        self.assertFalse(ok)
        self.assertIn("unsafe verification reference", detail)
        self.assertIn("src/work file.md", detail)
        for unsafe in (
            "/etc/passwd", "../escape.md", "a/../b", "a//b", "a/./b",
            "a\\b", "a\x00b", "",
        ):
            self.assertIsNotNone(
                campaign_module._unsafe_repo_relative(unsafe)[0], unsafe)
        self.assertEqual(
            campaign_module._unsafe_repo_relative("src/work-1.md"),
            (None, None))

    # -- HIGH: the untrusted developer never writes/commits trusted policy --

    def test_scope_violation_denies_trusted_policy_surface(self) -> None:
        # The orchestrator scope authority shares the confinement's trusted
        # policy/harness deny set: AGENTS.md, the hidden CI/forge tooling,
        # shell.nix, the factory configs, the harness docs, and the legacy
        # scripts/ security surface are never writable by any phase, while
        # genuine product entries stay writable.
        denied = [
            "AGENTS.md", ".gitignore", ".github/workflows/x.yml",
            ".forgejo/ISSUE_TEMPLATE/bug_report.md", "shell.nix",
            ".factory/config.toml", ".factory/environment.toml",
            "docs/FACTORY.md", "docs/OPERATIONS.md",
            "docs/FACTORY-LOOP-SPEC.md", "scripts/verify-project.sh",
            "scripts/git-commit-guard.sh", "scripts/pi2-secure-exec.py",
        ]
        allowed = [
            "src/main.py", "tests/test-main.py", "data/fixture.bin",
            "README.md", "docs/product-notes.md", "LICENSE",
        ]
        for phase in ("planning", "implementation", "verification", "audit"):
            for path in denied:
                with self.subTest(phase=phase, path=path):
                    violation = campaign_module.scope_violation(
                        [path], phase=phase,
                        plan_path=PLAN_REL,
                        spec_path="docs/SPEC.md",
                        allow_paths=[".factory-state/phase-result.json"],
                    )
                    self.assertIsNotNone(violation)
                    self.assertIn("trusted policy/harness surface", violation)
        for path in allowed:
            self.assertIsNone(campaign_module.scope_violation(
                [path], phase="implementation",
                plan_path=PLAN_REL, spec_path="docs/SPEC.md",
            ))
        # The canonical specification is still denied by the spec rule (a
        # genuine product entry, not the policy surface).
        violation = campaign_module.scope_violation(
            ["docs/SPEC.md"], phase="implementation",
            plan_path=PLAN_REL, spec_path="docs/SPEC.md",
        )
        self.assertIsNotNone(violation)
        self.assertIn("canonical specification", violation)

    def test_missing_role_digest_is_clean_campaign_error(self) -> None:
        # LOW: a committed role-prompt digest absent from the config is a
        # clean CampaignPhaseError (fail closed), never an unhandled KeyError.
        ws = self.make(SUCCESS_SCENARIO)
        config = self._production_config(ws)
        config = dataclasses.replace(
            config,
            role_prompt_digests={
                role: digest
                for role, digest in config.role_prompt_digests.items()
                if role != "tester"
            },
        )
        head = _git(ws.root, "rev-parse", "HEAD").stdout.strip()
        with unittest.mock.patch.object(
            campaign_module.launch_module, "authorize_launch",
            side_effect=launch_module.InvocationError("unreachable"),
        ):
            with self.assertRaises(campaign_module.CampaignPhaseError) as cm:
                campaign_module.launch_role_attempt(
                    config, role="tester", head=head,
                    _confinement_proof=object(),
                )
        self.assertIn("no digest for 'tester'", str(cm.exception))

    # -- L3: classify_implementation honors the §13 exit status -------------- 

    def test_implementation_classification_honors_exit_status(self) -> None:
        ok = campaign_module.RoleOutcome("developer", 0)
        base = dict(role=ok, plan_valid=True, task_complete=True,
                    acceptance_pass=True, had_changes=True, scope_ok=True)
        self.assertEqual(campaign_module.classify_implementation(**base),
                         "task_completed")
        failed = campaign_module.RoleOutcome("developer", 1)
        self.assertEqual(campaign_module.classify_implementation(
            **{**base, "role": failed}), "task_failed")
        self.assertEqual(campaign_module.classify_implementation(
            **{**base, "role": failed, "task_complete": False,
               "acceptance_pass": False, "had_changes": True}), "task_failed")
        doc = campaign_module.classify_implementation.__doc__ or ""
        self.assertIn("nonzero machine-readable", doc)

    def test_complete_work_with_nonzero_exit_retries_then_completes(self) -> None:
        # L3 integration: a developer whose complete work is accompanied by a
        # nonzero exit status is a deterministic task_failed on that attempt;
        # the coherent work is preserved and the retry commits it as the
        # completion.
        ws = self.make({
            "planner": {"behavior": "planned"},
            "developer": {"behavior": "complete-exit1"},
            "tester": {"behavior": "pass"},
            "auditor": {"behavior": "pass"},
        })
        rc, data = ws.run_cli()
        self.assertEqual(rc, 0)
        assert_history(self, data, [
            (1, "planning", "planned"),
            (1, "implementation", "task_failed"),
            (1, "implementation", "task_completed"),
            (1, "verification", "pass"),
            (1, "audit", "pass"),
        ])
        # The coherent work survives both attempts.
        work = ws.root / "src" / "work-1.md"
        self.assertTrue(work.exists())

    # -- L4: every trusted Git call is finite-bounded ------------------------

    def test_every_trusted_git_call_is_finite_bounded(self) -> None:
        ws = self.make(SUCCESS_SCENARIO)
        campaign = campaign_module.Campaign(ws.derive_config())
        campaign._acquire()
        try:
            real_run = campaign._lock._git_run
            real_bytes = campaign._lock._git_bytes
            recorded: list = []

            def recording_run(argv, *, timeout=None):
                recorded.append((list(argv), timeout))
                return real_run(argv, timeout=timeout)

            def recording_bytes(argv, *, timeout=None):
                recorded.append((list(argv), timeout))
                return real_bytes(argv, timeout=timeout)

            with unittest.mock.patch.object(
                campaign._lock, "_git_run", side_effect=recording_run,
            ), unittest.mock.patch.object(
                campaign._lock, "_git_bytes", side_effect=recording_bytes,
            ):
                git = campaign._git
                git.head()
                git.is_ancestor("HEAD", "HEAD")
                git.status_entries()
                git.diff_paths("HEAD")
                git.history(3)
                git.commit_count()
                git.plan_at(git.head())
            self.assertTrue(recorded)
            for argv, timeout in recorded:
                self.assertIsNotNone(timeout, f"unbounded Git call {argv}")
                self.assertGreater(timeout, 0)
                self.assertLessEqual(timeout, campaign_module.GIT_TIMEOUT)
        finally:
            campaign._lock.release()
        # The module-level trusted Git reads are bounded the same way.
        recorded2: list = []
        real_git_run = campaign_module.gitutil.git_run
        real_git_bytes = campaign_module.gitutil.git_bytes

        def recording_git_run(argv, *, timeout=None, **kwargs):
            recorded2.append(timeout)
            return real_git_run(argv, timeout=timeout, **kwargs)

        def recording_git_bytes(argv, *, timeout=None, **kwargs):
            recorded2.append(timeout)
            return real_git_bytes(argv, timeout=timeout, **kwargs)

        with unittest.mock.patch.object(
            campaign_module.gitutil, "git_run",
            side_effect=recording_git_run,
        ), unittest.mock.patch.object(
            campaign_module.gitutil, "git_bytes",
            side_effect=recording_git_bytes,
        ):
            campaign_module._live_head(ws.root)
            campaign_module._blob_at(ws.root, PLAN_REL)
            campaign_module._audit_objective_bytes(ws.root, 1)
        self.assertTrue(recorded2)
        for timeout in recorded2:
            self.assertIsNotNone(timeout)
            self.assertGreater(timeout, 0)
            self.assertLessEqual(timeout, campaign_module.GIT_TIMEOUT)


if __name__ == "__main__":
    unittest.main()
