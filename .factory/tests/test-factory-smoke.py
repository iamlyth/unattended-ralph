#!/usr/bin/env python3
"""Hidden evidence-smoke lane suite (Task 22).

Drives the real production ``campaign.py run --evidence-smoke`` control plane
through the trusted operator command (``.factory/smoke/evidence_smoke.py``)
against committed synthetic fixture repositories shaped like the canonical
repository (25-task plan, Task 22 the sole runnable pending task, foreign
pre-existing ``.factory-state`` bytes pre-planted).  The suite proves, in a
temporary clone-like workspace and never in the live repository:

* the exact one-round phase history, terminal ``success``, and honest
  round limit (the final audit task stays pending — the round is not
  acceptance);
* the state file and digest ledger contracts (write-once bindings, digests,
  phase tags, private mode-0600 state);
* the canonical byte-bound planner revision (the committed plan plus the
  fixed marker) and the single committed evidence artifact;
* the deterministic seam and gate blob binding, fail-closed refusal of an
  arbitrary driver / dirty tree / wrong branch / wrong commit / non-synthetic
  provider;
* byte-for-byte preservation of pre-existing ``.factory-state`` entries
  (digest, mode, mtime) and a clean tree afterwards; no surviving process.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
import unittest
import unittest.mock
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
LOOP = ROOT / ".factory" / "loop"
SMOKE = ROOT / ".factory" / "smoke"
FIXTURES = ROOT / ".factory" / "tests" / "fixtures"
SCHEMAS = ROOT / ".factory" / "schemas"

sys.path.insert(0, str(LOOP))
import gitutil as gitutil_module  # noqa: E402
import plan_parser as plan_parser_module  # noqa: E402
import state as state_module  # noqa: E402
sys.path.insert(0, str(SMOKE))
import evidence_smoke_common as common  # noqa: E402
import evidence_smoke as smoke_module  # noqa: E402, PLC0415
sys.path.insert(0, str(LOOP))
import campaign as campaign_module  # noqa: E402, PLC0415
import evidence as evidence_module  # noqa: E402, PLC0415
import dataclasses  # noqa: E402

GIT = gitutil_module.GIT_EXECUTABLE
BRANCH = "fixture-main"
PLAN_REL = ".factory/artifacts/implementation-plan.md"
EVIDENCE_REL = ".factory/artifacts/campaign-smoke-evidence.json"
STATE_DIR = ".factory-state"


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def run(command, cwd=None, *, check=True, timeout=120, env=None):
    result = subprocess.run(
        command, cwd=cwd, text=True, capture_output=True, env=env, timeout=timeout
    )
    if check and result.returncode:
        raise AssertionError(
            (command, result.returncode, result.stdout[-2000:], result.stderr[-2000:])
        )
    return result


def _git(workspace: Path, *args: str, check: bool = True):
    return run([GIT, "-C", str(workspace), *args], check=check)


def _git_blob(workspace: Path, commit: str, relpath: str) -> bytes:
    resolved = _git(workspace, "rev-parse", f"{commit}:{relpath}").stdout.strip()
    return _git(workspace, "cat-file", "blob", resolved).stdout.encode("utf-8")


def _no_survivors(campaign_id: str, baseline: set[int] | None = None) -> None:
    """Fail unless every marker-bearing process is a foreign pre-run leaf.

    Only processes that were NOT present before the run are campaign leaves;
    a foreign orphan that already carried the marker is deliberately
    untouched and excluded from the leak report.
    """
    baseline = baseline or set()
    for pid in common.marker_processes(campaign_id):
        if pid in baseline:
            continue
        raise AssertionError(
            f"a process survives the smoke round: {pid}"
        )


# The fixture task shape mirrors the canonical plan's pending set: tasks 1-20
# complete, task 21 blocked with an exact external reference, Task 22 the
# sole runnable pending task, 23/24 pending behind it, 25 the final audit.
FIXTURE_TASKS: list[dict] = []
for number in range(1, 26):
    if number <= 20:
        status, blocked_on, deps = "complete", None, []
    elif number == 21:
        status, blocked_on, deps = "blocked", "external-human-runner-authority", [20]
    elif number == 22:
        status, blocked_on, deps = "pending", None, [20]
    elif number == 23:
        status, blocked_on, deps = "pending", None, [20, 22]
    elif number == 24:
        status, blocked_on, deps = "pending", None, [20, 22, 23]
    else:
        status, blocked_on, deps = "pending", None, []
    FIXTURE_TASKS.append(
        {
            "number": number,
            "title": f"Fixture task {number}",
            "status": status,
            "priority": 10,
            "dependencies": deps,
            "blocked_on": blocked_on,
            "scope": "fixture-scoped work only.",
            "verification": f"`src/work-{number}.md`",
        }
    )


class SmokeWorkspace:
    """A committed fixture repository shaped like the canonical repository.

    A real Git repository on ``BRANCH`` carrying the full hidden loop
    package, the canonical spec/prompts/registry/schemas, the designated
    smoke seam, gate, and operator command, a 25-task canonical plan with
    Task 22 pending, and (optionally) pre-existing foreign ``.factory-state``
    bytes.  The smoke round runs against this committed workspace exactly as
    the live repository would.
    """

    def __init__(self, tmp: Path, *, branch: str = BRANCH, foreign: bool = True):
        self.root = tmp / "smoke-ws"
        self.branch = branch
        self.foreign_snapshot: dict = {}
        self.head = ""
        self.plan_bytes = b""
        self.build()
        if foreign:
            self.plant_foreign_files()

    # -- fixture construction ------------------------------------------------

    def build(self) -> None:
        ws = self.root
        ws.mkdir(parents=True)
        for rel in (
            "docs",
            "scripts",
            "src",
            ".factory/prompts",
            ".factory/audit-objectives",
            ".factory/artifacts",
            ".factory/schemas",
            ".factory/smoke",
            ".factory/loop",
        ):
            (ws / rel).mkdir(parents=True)
        # The complete hidden loop package (the campaign runs as a script).
        for module in sorted(LOOP.glob("*.py")):
            shutil.copy2(module, ws / ".factory" / "loop" / module.name)
        for name in (
            "factory-plan-v1.requirements.json",
            "factory-campaign-result-v1.schema.json",
            "factory-phase-result-v1.schema.json",
        ):
            shutil.copy2(SCHEMAS / name, ws / ".factory" / "schemas" / name)
        shutil.copy2(
            ROOT / ".factory" / "audit-objectives" / "registry.json",
            ws / ".factory" / "audit-objectives" / "registry.json",
        )
        for role in ("planner", "developer", "tester", "auditor"):
            (ws / ".factory" / "prompts" / f"{role}.md").write_text(
                f"# {role} fixture role prompt\n", encoding="utf-8"
            )
        # The docs-complete fixture set: the canonical documentation gate
        # (scripts/check-docs-sync.sh) reads exactly these five documents and
        # must pass every machine-checked claim against them, so a retained
        # descriptor execution with a pinned FACTORY_VERIFIER_ROOT proves the
        # full gate on the fixture.  No fenced command blocks and no removed
        # authority tokens, so the fixture cannot trip the claim checks.
        (ws / "README.md").write_text(
            "# Fixture repository\n\n"
            "Canonical specification: docs/FACTORY-LOOP-SPEC.md.\n\n"
            "Prerequisites: Landlock, ssh-keygen.\n",
            encoding="utf-8",
        )
        (ws / "docs" / "FACTORY.md").write_text(
            "# Fixture factory\n\n"
            "## Goal and non-goals\n"
            "The canonical plan is `.factory/artifacts/implementation-plan.md`.\n\n"
            "## Control state\n"
            "Single control-state file `.factory-state/factory-loop.json`.\n\n"
            "## Roles\n"
            "Role prompts: `.factory/prompts/planner.md`, "
            "`.factory/prompts/developer.md`, `.factory/prompts/tester.md`, "
            "`.factory/prompts/auditor.md`.\n\n"
            "## CLI\n"
            "Campaign CLI: `factory.loop.campaign`. Launch CLI: "
            "`factory.loop.launch`.\n"
            "Plan parser authority: `.factory/loop/plan_parser.py`.\n"
            "Deterministic selector: `.factory/loop/selector.py`.\n\n"
            "## Prerequisites\n"
            "Requires `/proc`; the freeze marker is `.factory/ralph-freeze` "
            "and the recovery-only override is `FACTORY_RALPH_FREEZE_OVERRIDE`.\n\n"
            "## Documentation\n"
            "Bug workflow: `docs/BUG_WORKFLOW.md`.\n\n"
            "## Outcomes\n"
            "A campaign terminates in exactly one finite outcome: success, "
            "findings, blocked, failed, interrupted, infrastructure_failure.\n\n"
            "## Release\n"
            "The canonical release update target is "
            "`docs/FACTORY-LOOP-SPEC.md`.\n",
            encoding="utf-8",
        )
        (ws / "docs" / "OPERATIONS.md").write_text(
            "# Fixture operations\n\n"
            "A fresh clone has no lifecycle state: the lifecycle marker is "
            "missing.\n"
            "The findings payload schema is `factory-findings/v1` and the "
            "planner-only findings flow is documented in "
            "`docs/BUG_WORKFLOW.md`.\n",
            encoding="utf-8",
        )
        (ws / "docs" / "BUG_WORKFLOW.md").write_text(
            "# Fixture bug workflow\n\n"
            "The next round's fresh planner only handles findings; the "
            "findings payload is `factory-findings/v1`.\n"
            "The frozen legacy launcher `ralph-maintenance-run.sh` stays "
            "frozen legacy.\n"
            "Ledger: `.factory/bugs/open.md` and `.factory/bugs/closed.md`.\n"
            "The adopting-product supplied `scripts/verify-project.sh` is "
            "the project verifier.\n",
            encoding="utf-8",
        )
        (ws / "AGENTS.md").write_text(
            "AGENTS.md fixture operational policy: run "
            "scripts/verify-boilerplate.sh and scripts/check-docs-sync.sh; "
            "the adversarial suite is .factory/tests/test-factory-adversarial.sh.\n",
            encoding="utf-8",
        )
        (ws / "scripts" / "verify-boilerplate.sh").write_text(
            "#!/usr/bin/env bash\n"
            "set -euo pipefail\n"
            "# Fixture generic verifier stub: the documentation gate is part "
            "of the generic verifier.\n"
            "docs_gate=scripts/check-docs-sync.sh\n"
            "echo 'fixture verifier stub'\n",
            encoding="utf-8",
        )
        os.chmod(ws / "scripts" / "verify-boilerplate.sh", 0o755)
        shutil.copy2(
            ROOT / "docs/FACTORY-LOOP-SPEC.md", ws / "docs/FACTORY-LOOP-SPEC.md"
        )
        for script in (
            "factory_state_io.py",
            "credential-guard.py",
            "check-plan-freshness.sh",
            "check-generic-leakage.sh",
            "check-docs-sync.sh",
        ):
            shutil.copy2(ROOT / "scripts" / script, ws / "scripts" / script)
        # The fixture installs the *real* tracked Git commit boundary: the
        # exact `git-commit-guard.sh` and its installer are committed and the
        # six launcher hooks are installed, so every campaign commit (and
        # every fixture commit) runs through the production guard.
        shutil.copy2(
            ROOT / "scripts" / "git-commit-guard.sh",
            ws / "scripts" / "git-commit-guard.sh",
        )
        shutil.copy2(
            ROOT / "scripts" / "install-git-commit-guard.sh",
            ws / "scripts" / "install-git-commit-guard.sh",
        )
        os.chmod(ws / "scripts" / "git-commit-guard.sh", 0o755)
        shutil.copy2(
            ROOT / ".factory" / "generic-leak-allowlist",
            ws / ".factory" / "generic-leak-allowlist",
        )
        # The plan contract's policy authorities (freshness-scope files the
        # post-round documentation gates require).
        for policy in ("campaign-receipt-policy.json", "requirement-policy.json",
                       "capability-contracts.json"):
            shutil.copy2(
                ROOT / ".factory" / policy,
                ws / ".factory" / policy,
            )
        (ws / ".gitignore").write_text(
            ".factory-state/\n__pycache__/\n*.pyc\n", encoding="utf-8"
        )
        (ws / ".factory" / "config.toml").write_text(
            "[project]\n"
            'spec = "docs/FACTORY-LOOP-SPEC.md"\n'
            'plan = ".factory/artifacts/implementation-plan.md"\n'
            f'development_branch = "{self.branch}"\n'
            'release_branch = "main"\n',
            encoding="utf-8",
        )
        for name in (
            "evidence_smoke_common.py",
            "evidence_smoke_driver.py",
            "evidence_smoke_gate.py",
            "evidence_smoke.py",
        ):
            shutil.copy2(SMOKE / name, ws / ".factory" / "smoke" / name)
        for name in (
            "evidence_smoke_driver.py",
            "evidence_smoke_gate.py",
            "evidence_smoke.py",
        ):
            # The designated seam, gate, and operator command are tracked
            # executables (100755) exactly like the production harness.
            os.chmod(ws / ".factory" / "smoke" / name, 0o755)
        _git(ws, "init", "-q", "-b", self.branch)
        _git(ws, "config", "user.email", "fixture@test")
        _git(ws, "config", "user.name", "fixture")
        # Install the real commit-guard hooks before any fixture commit, so
        # the fixture provably runs every commit (including the campaign's)
        # behind the production Git boundary.
        run(
            ["bash", str(ws / "scripts" / "install-git-commit-guard.sh")],
            cwd=str(ws),
        )
        _git(ws, "add", "-A")
        _git(ws, "commit", "-qm", "smoke fixture base")
        base_head = _git(ws, "rev-parse", "HEAD").stdout.strip()
        spec_blob = _git(
            ws, "rev-parse", "HEAD:docs/FACTORY-LOOP-SPEC.md"
        ).stdout.strip()
        common_front = {
            "spec_path": "docs/FACTORY-LOOP-SPEC.md",
            "spec_commit": base_head,
            "spec_blob": spec_blob,
            "base_commit": base_head,
            "lifecycle": "active",
        }
        spec_path = ws / "fixture-spec.json"
        spec_path.write_text(
            json.dumps({**common_front, "tasks": FIXTURE_TASKS}), encoding="utf-8"
        )
        run(
            [
                sys.executable, str(FIXTURES / "fixture_plan_tool.py"),
                "--spec", str(spec_path),
                "--registry", str(SCHEMAS / "factory-plan-v1.requirements.json"),
                "--out", str(ws / PLAN_REL),
            ],
            cwd=str(ws),
        )
        spec_path.unlink()
        _git(ws, "add", "-A")
        _git(ws, "commit", "-qm", "smoke fixture plan and seam")
        self.head = _git(ws, "rev-parse", "HEAD").stdout.strip()
        self.plan_bytes = (ws / PLAN_REL).read_bytes()

    def plant_foreign_files(self) -> dict:
        """Pre-plant foreign ``.factory-state`` bytes (never to be touched)."""
        state_dir = self.root / STATE_DIR
        state_dir.mkdir(mode=0o700)
        files = {
            "installed-functional-evidence.env": (
                "FOREIGN_ADOPTING_COMMIT=61356a0f00000000000000000000000000000000\n"
            ),
            "campaign-resume-foreign.log": "foreign runtime bytes\n" * 512,
            "nested/foreign.json": '{"schema": "foreign/artifact-v1"}\n',
        }
        snapshot = {}
        for rel, data in files.items():
            path = state_dir / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data.encode("utf-8"))
            info = path.stat()
            snapshot[rel] = {
                "sha256": sha256(path.read_bytes()),
                "mode": stat.S_IMODE(info.st_mode),
                "mtime_ns": info.st_mtime_ns,
            }
        self.foreign_snapshot = snapshot
        return snapshot

    def verify_foreign_preserved(self) -> None:
        for rel, expected in self.foreign_snapshot.items():
            path = self.root / STATE_DIR / rel
            if not path.is_file():
                raise AssertionError(f"pre-existing entry deleted: {rel}")
            info = path.stat()
            current = {
                "sha256": sha256(path.read_bytes()),
                "mode": stat.S_IMODE(info.st_mode),
                "mtime_ns": info.st_mtime_ns,
            }
            if current != expected:
                raise AssertionError(f"pre-existing entry changed: {rel}")

    # -- invocation ----------------------------------------------------------

    def run_smoke(
        self,
        *,
        expect_commit: str | None = None,
        extra: list[str] | None = None,
        check: bool = True,
        env: dict | None = None,
    ):
        if expect_commit is None:
            expect_commit = self.head
        command = [
            sys.executable,
            str(self.root / ".factory" / "smoke" / "evidence_smoke.py"),
            "--root", str(self.root),
            "run",
            "--branch", self.branch,
            "--expect-commit", expect_commit,
        ]
        if extra:
            command += extra
        result = run(
            command, cwd=str(self.root), check=False, timeout=900, env=env
        )
        summary = None
        try:
            if result.stdout.strip():
                summary = json.loads(result.stdout)
        except ValueError:
            summary = None
        if check and result.returncode != 0:
            raise AssertionError(
                (result.returncode, result.stdout[-2000:], result.stderr[-2000:])
            )
        return result, summary

    def state(self, campaign_id: str):
        return state_module.load_state(
            self.root,
            expected_branch=self.branch,
            expected_campaign_id=campaign_id,
            expected_rounds_requested=1,
        )


class _SmokeBase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="factory-smoke-test."))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def make(self, *, branch: str = BRANCH, foreign: bool = True) -> SmokeWorkspace:
        return SmokeWorkspace(self.tmp / "ws", branch=branch, foreign=foreign)


class EvidenceSmokeUnit(_SmokeBase):
    """Pure deterministic transformations of canonical pre-smoke bytes."""

    @staticmethod
    def _pre_smoke_plan() -> bytes:
        """Derive the pre-round fixture whether or not live smoke has run."""
        text = (ROOT / PLAN_REL).read_text(encoding="utf-8")
        text = text.replace(common.SMOKE_MARKER + "\n", "", 1)
        heading = "## Task 22: Live campaign and control-state instantiation"
        start = text.index(heading)
        end = text.index("\n## Task 23:", start)
        block = text[start:end]
        block = block.replace("- Status: complete", "- Status: pending", 1)
        return (text[:start] + block + text[end:]).encode("utf-8")

    def test_marker_revision_is_canonical_and_byte_bound(self) -> None:
        plan = self._pre_smoke_plan()
        revision = common.plan_with_smoke_marker(plan)
        parsed = plan_parser_module.Plan.from_bytes(revision)
        # Byte-exact round-trip and deterministic bytes.
        self.assertEqual(parsed.serialize().encode("utf-8"), revision)
        self.assertEqual(common.plan_with_smoke_marker(plan), revision)
        task22 = next(t for t in parsed.tasks if t.number == 22)
        self.assertEqual(task22.status, "pending")
        # The planner revision is a pure byte extension: one marker line.
        self.assertIn(common.SMOKE_MARKER, revision.decode("utf-8"))
        self.assertNotEqual(revision, plan)

    def test_developer_revision_completes_exactly_one_task(self) -> None:
        plan = self._pre_smoke_plan()
        marker = common.plan_with_smoke_marker(plan)
        completed = common.plan_with_task_complete(marker, 22)
        parsed = plan_parser_module.Plan.from_bytes(completed)
        self.assertEqual(parsed.serialize().encode("utf-8"), completed)
        task22 = next(t for t in parsed.tasks if t.number == 22)
        self.assertEqual(task22.status, "complete")
        task25 = next(t for t in parsed.tasks if t.number == 25)
        self.assertEqual(task25.status, "pending")
        # The selector picks exactly the evidence task from the planner revision.
        import selector as selector_module  # noqa: PLC0415

        selection = selector_module.select_task(
            plan_parser_module.Plan.from_bytes(marker),
            bound_base_commit=parsed.base_commit,
        )
        self.assertTrue(selection.selected)
        self.assertEqual(selection.task_id, 22)

    def test_evidence_artifact_schema_validation(self) -> None:
        payload = common.smoke_evidence_bytes(
            campaign_id="evidence-smoke-deadbeef",
            bound_commit="0" * 40,
            task_id=22,
            round_no=1,
            attempt=1,
            task_excerpt_digest="0" * 64,
            plan_digest_worked="1" * 64,
            findings_present=False,
        )
        parsed = common.validate_smoke_evidence(
            payload, task_id=22, campaign_id="evidence-smoke-deadbeef"
        )
        self.assertEqual(parsed["schema"], common.SCHEMA_NAME)
        self.assertEqual(parsed["seam"], "evidence-smoke")
        with self.assertRaises(common.EvidenceSmokeError):
            common.validate_smoke_evidence(payload, task_id=23)


class EvidenceSmokeRound(_SmokeBase):
    """The trusted operator command drives one full real campaign round."""

    def test_full_round_success_with_foreign_state_preserved(self) -> None:
        ws = self.make()
        campaign_id = common.smoke_campaign_id(ws.head)
        result, summary = ws.run_smoke(expect_commit=ws.head)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(summary["ok"], True)
        self.assertEqual(summary["campaign_id"], campaign_id)
        self.assertEqual(summary["terminal_phase"], "success")
        self.assertEqual(summary["rounds_completed"], 1)
        # Exact one-round phase history.
        history = [
            (record["round"], record["phase"], record["outcome"])
            for record in summary["phase_history"]
        ]
        self.assertEqual(history, common.expected_phase_history())
        # State fields: write-once bindings, digests, terminal.
        state = ws.state(campaign_id)
        self.assertEqual(state.current_phase, "success")
        self.assertEqual(state.last_outcome, "success")
        self.assertEqual(state.rounds_requested, 1)
        self.assertEqual(state.current_round, 1)
        self.assertEqual(state.branch, BRANCH)
        self.assertEqual(state.selected_task_id, None)
        self.assertEqual(state.attempt_number, 0)
        planning_head = summary["phase_history"][0]["head_commit"]
        self.assertEqual(
            state.plan_digest, sha256(common.plan_with_smoke_marker(ws.plan_bytes))
        )
        self.assertEqual(state.phase_base_commit, planning_head)
        # State digest re-derivation: every digest must equal the exact
        # committed blob at the final head — the specification, the audit
        # objective registry, and all four role prompts are re-derived from
        # Git, never from operator claims or the mutable worktree.
        final_head = summary["head_commit"]
        self.assertEqual(
            state.specification_digest,
            sha256(_git_blob(ws.root, final_head, "docs/FACTORY-LOOP-SPEC.md")),
        )
        self.assertEqual(
            state.audit_objectives_digest,
            sha256(
                _git_blob(
                    ws.root, final_head, ".factory/audit-objectives/registry.json"
                )
            ),
        )
        for role in common.ROLE_NAMES:
            self.assertEqual(
                state.role_prompt_digests[role],
                sha256(
                    _git_blob(
                        ws.root, final_head, f".factory/prompts/{role}.md"
                    )
                ),
            )
        self.assertRegex(state.specification_digest, r"^[0-9a-f]{64}$")
        self.assertRegex(state.audit_objectives_digest, r"^[0-9a-f]{64}$")
        for digest in state.role_prompt_digests.values():
            self.assertRegex(digest, r"^[0-9a-f]{64}$")
        # Private state file and phase digest ledger (exact round-1 tags).
        state_path = ws.root / STATE_DIR / state_module.STATE_FILE_NAME
        info = state_path.stat()
        self.assertEqual(stat.S_IMODE(info.st_mode) & 0o777, 0o600)
        ledger = state_module.read_phase_digest_ledger(ws.root)
        self.assertGreaterEqual(len(ledger), 4)
        for tag, digest in ledger.items():
            self.assertRegex(digest, r"^[0-9a-f]{64}$")
            self.assertTrue(str(tag).startswith("r1."))

        # The planner committed the canonical byte-bound revision.
        planner_blob = _git_blob(ws.root, planning_head, PLAN_REL)
        self.assertEqual(
            planner_blob, common.plan_with_smoke_marker(ws.plan_bytes)
        )
        # The committed plan at the final head: Task 22 complete, final audit
        # still pending (round limit, not acceptance), bindings unchanged.
        final_head = summary["head_commit"]
        final_plan = plan_parser_module.Plan.from_bytes(
            _git_blob(ws.root, final_head, PLAN_REL)
        )
        task22 = next(t for t in final_plan.tasks if t.number == 22)
        self.assertEqual(task22.status, "complete")
        task25 = next(t for t in final_plan.tasks if t.number == 25)
        self.assertEqual(task25.status, "pending")
        base = plan_parser_module.Plan.from_bytes(ws.plan_bytes)
        self.assertEqual(final_plan.spec_path, base.spec_path)
        self.assertEqual(final_plan.spec_commit, base.spec_commit)
        self.assertEqual(final_plan.spec_blob, base.spec_blob)
        self.assertEqual(final_plan.base_commit, base.base_commit)

        # The single tracked evidence artifact is committed and well-formed.
        listing = _git(ws.root, "ls-files", "--", EVIDENCE_REL).stdout.strip()
        self.assertEqual(listing, EVIDENCE_REL)
        evidence = _git_blob(ws.root, final_head, EVIDENCE_REL)
        payload = common.validate_smoke_evidence(
            evidence, task_id=22, campaign_id=campaign_id
        )
        # The developer evidence binds the exact commit the developer worked
        # from (the planner's committed revision), never a paraphrase.
        self.assertEqual(payload["bound_commit"], planning_head)
        self.assertEqual(
            payload["plan_digest_worked"],
            sha256(common.plan_with_smoke_marker(ws.plan_bytes)),
        )
        self.assertRegex(str(payload["task_excerpt_digest"]), r"^[0-9a-f]{64}$")
        self.assertEqual(payload["findings_present"], False)

        # Foreign .factory-state bytes preserved; tree clean; no survivor.
        ws.verify_foreign_preserved()
        status = _git(ws.root, "status", "--porcelain")
        self.assertEqual(status.stdout, "")
        _no_survivors(campaign_id)

    def test_rejects_dirty_worktree(self) -> None:
        ws = self.make(foreign=False)
        (ws.root / "src" / "dirty.md").write_text("dirty\n", encoding="utf-8")
        result, _summary = ws.run_smoke(check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("worktree is not clean", result.stderr)

    def test_rejects_wrong_branch(self) -> None:
        ws = self.make(branch=BRANCH, foreign=False)
        result, _summary = ws.run_smoke(extra=["--branch", "another-main"], check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("branch", result.stderr)

    def test_rejects_wrong_commit(self) -> None:
        ws = self.make(foreign=False)
        result, _summary = ws.run_smoke(
            expect_commit="0" * 40, check=False
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("mandatory exact commit", result.stderr)

    def test_missing_expect_commit_is_rejected(self) -> None:
        ws = self.make(foreign=False)
        command = [
            sys.executable,
            str(ws.root / ".factory" / "smoke" / "evidence_smoke.py"),
            "--root", str(ws.root),
            "run",
            "--branch", BRANCH,
        ]
        result = run(command, cwd=str(ws.root), check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("--expect-commit", result.stderr)

    def test_rejects_modified_driver(self) -> None:
        ws = self.make(foreign=False)
        driver = ws.root / common.DESIGNATED_DRIVER_REL
        driver.write_bytes(driver.read_bytes() + b"\n# tampered\n")
        result, _summary = ws.run_smoke(check=False)
        # A substituted worktree seam is a dirty tree: the operator fails
        # closed before the campaign can ever run the substituted bytes.
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("worktree is not clean", result.stderr)

    def test_campaign_cli_refuses_arbitrary_driver(self) -> None:
        ws = self.make(foreign=False)
        command = [
            sys.executable, str(ws.root / ".factory/loop/campaign.py"),
            "--root", str(ws.root),
            "run",
            "--campaign-id", "evidence-smoke-test",
            "--rounds", "1",
            "--branch", BRANCH,
            "--provider", "synthetic",
            "--role-driver", ".factory/tests/fixtures/campaign_driver.py",
            "--developer-evidence-path", EVIDENCE_REL,
            "--evidence-smoke",
        ]
        result = run(command, cwd=str(ws.root), check=False)
        self.assertEqual(result.returncode, 6)
        self.assertIn("arbitrary role candidate", result.stderr)

    def test_campaign_cli_refuses_non_synthetic_provider(self) -> None:
        ws = self.make(foreign=False)
        command = [
            sys.executable, str(ws.root / ".factory/loop/campaign.py"),
            "--root", str(ws.root),
            "run",
            "--campaign-id", "evidence-smoke-test",
            "--rounds", "1",
            "--branch", BRANCH,
            "--provider", "ollama",
            "--role-driver", common.DESIGNATED_DRIVER_REL,
            "--evidence-smoke",
        ]
        result = run(command, cwd=str(ws.root), check=False)
        self.assertEqual(result.returncode, 6)
        self.assertIn("synthetic", result.stderr)

    def test_campaign_cli_refuses_unknown_seam_label(self) -> None:
        ws = self.make(foreign=False)
        command = [
            sys.executable, str(ws.root / ".factory/loop/campaign.py"),
            "--root", str(ws.root),
            "run",
            "--campaign-id", "ordinary-campaign",
            "--rounds", "1",
            "--branch", BRANCH,
            "--provider", "synthetic",
            "--role-driver", common.DESIGNATED_DRIVER_REL,
            "--evidence-smoke",
        ]
        result = run(command, cwd=str(ws.root), check=False)
        self.assertEqual(result.returncode, 6)
        self.assertIn("seam label", result.stderr)

    def test_state_fails_closed_when_state_already_exists(self) -> None:
        ws = self.make(foreign=False)
        state_dir = ws.root / STATE_DIR
        state_dir.mkdir(mode=0o700)
        (state_dir / state_module.STATE_FILE_NAME).write_text(
            json.dumps({"schema": "factory-state/v1", "forged": True}),
            encoding="utf-8",
        )
        result, _summary = ws.run_smoke(check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("factory-loop.json", result.stderr)


class EvidenceSmokeAdversarial(_SmokeBase):
    """Fail-closed adversarial fixtures of the evidence-smoke lane."""

    def test_fixture_tracks_executables_100755(self) -> None:
        ws = self.make(foreign=False)
        for rel in (
            common.DESIGNATED_DRIVER_REL,
            common.GATE_REL,
            ".factory/smoke/evidence_smoke.py",
            "scripts/git-commit-guard.sh",
            "scripts/install-git-commit-guard.sh",
        ):
            entry = _git(ws.root, "ls-files", "-s", "--", rel).stdout.strip()
            self.assertTrue(entry.startswith("100755"), (rel, entry))

    def test_fixture_installs_real_git_hooks(self) -> None:
        ws = self.make(foreign=False)
        for hook in (
            "pre-commit", "prepare-commit-msg", "pre-merge-commit",
            "applypatch-msg", "pre-applypatch", "commit-msg",
        ):
            hook_path = ws.root / ".git" / "hooks" / hook
            self.assertTrue(hook_path.is_file(), hook)
            self.assertTrue(os.access(hook_path, os.X_OK), hook)
            self.assertFalse(hook_path.is_symlink(), hook)
        check = run(
            [
                "bash", str(ws.root / "scripts" / "install-git-commit-guard.sh"),
                "--check",
            ],
            cwd=str(ws.root),
            check=False,
        )
        self.assertEqual(check.returncode, 0, check.stderr)

    def test_recovery_orphan_collision_rejected(self) -> None:
        ws = self.make(foreign=False)
        state_dir = ws.root / STATE_DIR
        state_dir.mkdir(mode=0o700)
        (state_dir / ".factory-loop.json.0123456789abcdef0123456789abcdef").write_text(
            "torn", encoding="utf-8"
        )
        result, _summary = ws.run_smoke(check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("recovery-orphan", result.stderr)

    def test_result_collision_rejected(self) -> None:
        ws = self.make(foreign=False)
        state_dir = ws.root / STATE_DIR
        state_dir.mkdir(mode=0o700)
        (state_dir / "evidence-smoke-phase-result.json").write_text(
            json.dumps({"schema": "factory-phase-result/v1", "outcome": "pass"}),
            encoding="utf-8",
        )
        result, _summary = ws.run_smoke(check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("collides", result.stderr)
        self.assertIn("no-replace", result.stderr)

    def test_ledger_collision_rejected(self) -> None:
        ws = self.make(foreign=False)
        state_dir = ws.root / STATE_DIR
        state_dir.mkdir(mode=0o700)
        (state_dir / "state-digest-ledger.jsonl").write_text(
            '{"tag": "foreign", "digest": "0" * 64}\n', encoding="utf-8"
        )
        result, _summary = ws.run_smoke(check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("digest ledger", result.stderr)

    def test_foreign_orphan_collision_untouched(self) -> None:
        ws = self.make()
        campaign_id = common.smoke_campaign_id(ws.head)
        marker = f"foreign-leaf-{campaign_id}"
        foreign = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(300)", marker],
            start_new_session=True,
            close_fds=True,
        )
        self.addCleanup(
            lambda: (foreign.kill(), foreign.wait())
            if foreign.poll() is None else None
        )
        baseline = set(common.marker_processes(campaign_id))
        self.assertIn(foreign.pid, baseline)
        result, summary = ws.run_smoke(expect_commit=ws.head)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(summary["ok"], True)
        # The foreign orphan (a pre-run marker collision) is deliberately
        # untouched: still alive, and the survivor scan excludes it.
        self.assertIsNone(foreign.poll())
        _no_survivors(campaign_id, baseline)
        foreign.kill()
        foreign.wait()

    def test_process_leak_token_detects_actual_leaf(self) -> None:
        ws = self.make()
        campaign_id = common.smoke_campaign_id(ws.head)
        baseline = set(common.marker_processes(campaign_id))
        result, _summary = ws.run_smoke(expect_commit=ws.head)
        self.assertEqual(result.returncode, 0, result.stderr)
        _no_survivors(campaign_id, baseline)
        # A leaked marker-bearing leaf spawned after the round is detected by
        # the token-based survivor scan (a real, non-zombie process).
        leaked = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(60)", campaign_id],
            start_new_session=True,
            close_fds=True,
        )
        self.addCleanup(
            lambda: (leaked.kill(), leaked.wait())
            if leaked.poll() is None else None
        )
        found = common.marker_processes(campaign_id)
        self.assertIn(leaked.pid, found)
        with self.assertRaises(AssertionError):
            _no_survivors(campaign_id)
        leaked.kill()
        leaked.wait()
        _no_survivors(campaign_id, baseline | set(found))

    def test_malicious_env_and_path_never_substitute(self) -> None:
        ws = self.make()
        evil = self.tmp / "evil-bin"
        evil.mkdir()
        marker_dir = self.tmp / "evil-markers"
        marker_dir.mkdir()
        # The poisoned tools the smoke lane's own channels would resolve from
        # PATH if they were not pinned/scrubbed.  `bash`/`git`/`realpath`/
        # `dirname` are deliberately not faked: the committed Git-hook
        # boundary resolves them from PATH, and this test targets the smoke
        # lane's own execution channels (driver, gates, docs gates), never
        # the Git commit chain.
        for tool in ("python3", "grep", "sed", "cat", "head",
                     "tail", "sort", "tr", "wc", "cut", "find", "cmp",
                     "printf"):
            trap = evil / tool
            trap.write_text(
                "#!/bin/sh\n"
                f"echo poisoned > {marker_dir / tool}\n"
                "exit 1\n",
                encoding="utf-8",
            )
            os.chmod(trap, 0o755)
        malicious = {
            **os.environ,
            "PATH": str(evil) + os.pathsep + os.environ.get("PATH", ""),
        }
        result, summary = ws.run_smoke(
            expect_commit=ws.head, env=malicious
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(summary["ok"], True)
        markers = sorted(marker_dir.iterdir())
        self.assertEqual(markers, [], "a poisoned tool executed")

    def test_ledger_state_evidence_tamper_detected(self) -> None:
        ws = self.make()
        campaign_id = common.smoke_campaign_id(ws.head)
        result, summary = ws.run_smoke(expect_commit=ws.head)
        self.assertEqual(result.returncode, 0, result.stderr)
        # The exact ledger tag set passes before the tamper.
        ledger = state_module.read_phase_digest_ledger(ws.root)
        common.validate_round_ledger(ledger)
        # Ledger tamper 1: a forged well-formed entry adds a foreign tag, so
        # the exact four-phase ledger contract fails closed.
        ledger_path = ws.root / STATE_DIR / "state-digest-ledger.jsonl"
        with ledger_path.open("a", encoding="utf-8") as stream:
            stream.write(
                json.dumps(
                    {"tag": "r1.forged.a9", "digest": "0" * 64},
                    sort_keys=True, separators=(",", ":"),
                )
                + "\n"
            )
        ledger = state_module.read_phase_digest_ledger(ws.root)
        self.assertIn("r1.forged.a9", ledger)
        with self.assertRaises(common.EvidenceSmokeError):
            common.validate_round_ledger(ledger)
        # Ledger tamper 2: a malformed digest line makes the strict ledger
        # reader fail closed on the whole ledger.
        with ledger_path.open("a", encoding="utf-8") as stream:
            stream.write('{"tag": "r1.planning.1.a1", "digest": "zz"}\n')
        with self.assertRaises(state_module.StateDigestError):
            state_module.read_phase_digest_ledger(ws.root)
        # State tamper: a terminal-success state whose `last_outcome` was
        # forged fails closed at load time (the structural invariant), and a
        # tampered plan_digest changes the canonical state digest so the
        # recorded summary digest can never match.
        state_path = ws.root / STATE_DIR / state_module.STATE_FILE_NAME
        state_dict = json.loads(state_path.read_text(encoding="utf-8"))
        state_dict["last_outcome"] = "failed"
        state_path.write_text(
            json.dumps(state_dict, sort_keys=True), encoding="utf-8"
        )
        with self.assertRaises(state_module.StateTamperError):
            state_module.load_state(ws.root)
        state_dict = json.loads(state_path.read_text(encoding="utf-8"))
        state_dict["last_outcome"] = "success"
        state_dict["plan_digest"] = "1" * 64
        state_path.write_text(
            json.dumps(state_dict, sort_keys=True), encoding="utf-8"
        )
        tampered_state = state_module.load_state(ws.root)
        self.assertNotEqual(
            state_module.state_digest(tampered_state), summary["state_digest"]
        )
        # Evidence tamper: a modified artifact payload is rejected by the
        # schema validator (bound_commit substitution fails closed).
        evidence = _git_blob(ws.root, summary["head_commit"], EVIDENCE_REL)
        payload = json.loads(evidence.decode("utf-8"))
        payload["bound_commit"] = "not-a-commit"
        forged = json.dumps(
            payload, sort_keys=True, separators=(",", ":")
        ).encode("utf-8") + b"\n"
        with self.assertRaises(common.EvidenceSmokeError):
            common.validate_smoke_evidence(forged, task_id=22, campaign_id=campaign_id)
        # A well-formed bound_commit that names the wrong phase head is caught
        # by the operator's cross-binding (bound_commit == planner head).
        payload = json.loads(evidence.decode("utf-8"))
        self.assertEqual(
            payload["bound_commit"], summary["phase_history"][0]["head_commit"]
        )
        self.assertNotEqual(payload["bound_commit"], "0" * 40)

    def test_toctou_driver_swap_never_executes(self) -> None:
        ws = self.make()
        trap = ws.root / "substitute-executed.marker"
        config = _smoke_campaign_config(ws)
        real_revalidate = evidence_module.revalidate_verifier
        swapped = {"done": False}

        def substituted(root, binding, *, git=None, current_commit=None, held=None):
            if not swapped["done"] and not binding.external:
                swapped["done"] = True
                path = Path(root) / binding.executable[2:]
                path.write_text(
                    "#!/usr/bin/env python3\n"
                    "import pathlib\n"
                    f"pathlib.Path({str(trap)!r}).write_text('executed')\n",
                    encoding="utf-8",
                )
                os.chmod(path, 0o755)
            return real_revalidate(
                root, binding, git=git, current_commit=current_commit, held=held
            )

        with unittest.mock.patch.object(
            evidence_module, "revalidate_verifier", side_effect=substituted
        ):
            with self.assertRaises(campaign_module.CampaignError):
                campaign_module.Campaign(config).run()
        # The substituted driver bytes never executed.
        self.assertFalse(trap.exists())

    def test_signal_group_kill_and_escaped_child_detection(self) -> None:
        marker = "evidence-smoke-signal-test"
        campaign_id = common.smoke_campaign_id("0" * 40)
        # The leader ignores TERM and spawns a same-session member that also
        # ignores TERM: the bounded group kill drains the whole group with
        # the unconditional KILL.
        leader_code = (
            "import signal, subprocess, sys, time\n"
            "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
            "subprocess.Popen([sys.executable, '-c', 'import signal,time; '"
            "'signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)'])\n"
            "time.sleep(300)\n"
        )
        leader = subprocess.Popen(
            [sys.executable, "-c", leader_code, marker],
            start_new_session=True, close_fds=True,
        )
        self.addCleanup(lambda: leader.kill() if leader.poll() is None else None)
        # The whole group is drained: TERM is ignored, so the unconditional
        # KILL reaches every same-group member and the group is reaped.
        smoke_module._kill_campaign_group(leader, grace=1.0)
        self.assertIsNotNone(leader.poll())
        self.assertFalse(smoke_module._pgid_has_live_members(leader.pid))
        leader.wait()
        # An escaped child in its own NEW session survives the group kill; the
        # marker scan detects the actual leaf.
        escaped = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(60)", campaign_id],
            start_new_session=True, close_fds=True,
        )
        self.addCleanup(lambda: escaped.kill() if escaped.poll() is None else None)
        self.assertIn(escaped.pid, common.marker_processes(campaign_id))
        escaped.kill()
        escaped.wait()
        self.assertNotIn(escaped.pid, common.marker_processes(campaign_id))


class PinnedGateBoundary(_SmokeBase):
    """The pinned gate supervisor bounds a flooding child and reaps its group."""

    def test_flooding_gate_bounded_capture_and_group_reap(self) -> None:
        """A flooding gate is overcap-terminated: bounded, reaped, no survivors.

        ``_run_pinned_gate`` drains stdout/stderr with bounded reader
        threads (fair, capped) instead of unbounded ``communicate``: a gate
        that floods both pipes far beyond the capture cap while a
        TERM-ignoring same-session member holds the write ends is detected
        by overcap, the whole group is TERM/KILLed and reaped, and the
        operator fails closed — bounded memory, no survivors, no hang.
        """
        leader_pid_file = self.tmp / "flood-leader.pid"
        code = (
            "import os, signal, subprocess, sys, time\n"
            f"open({str(leader_pid_file)!r}, 'w').write(str(os.getpid()))\n"
            "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
            "subprocess.Popen([sys.executable, '-c', 'import signal,time; "
            "signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)'])\n"
            "payload = 'x' * 65536\n"
            "for _ in range(64):\n"
            "    sys.stdout.write(payload)\n"
            "    sys.stderr.write(payload)\n"
            "    sys.stdout.flush()\n"
            "    sys.stderr.flush()\n"
            "time.sleep(300)\n"
        )
        env = {
            "HOME": os.environ.get("HOME", ""),
            "PATH": os.environ.get("PATH", ""),
        }
        started = time.monotonic()
        with self.assertRaises(smoke_module.EvidenceSmokeError) as ctx:
            smoke_module._run_pinned_gate(
                [sys.executable, "-c", code], env=env, root=self.tmp,
                timeout=30.0, capture_limit=4096,
            )
        message = str(ctx.exception)
        self.assertIn("capture cap", message)
        self.assertIn("terminated and reaped", message)
        # Prompt overcap detection: the flood exceeds the cap within a
        # second, so the group termination starts long before the deadline.
        self.assertLess(time.monotonic() - started, 10.0)
        leader = int(leader_pid_file.read_text(encoding="utf-8"))
        # No survivor in the gate's process group, and the leader was reaped
        # (bounded wait), never left as a corpse.
        self.assertFalse(smoke_module._pgid_has_live_members(leader))
        with self.assertRaises(ProcessLookupError):
            os.kill(leader, 0)


class DocsGateSync(_SmokeBase):
    """The canonical documentation gate honors a validated pinned root (fd exec).

    The gate child executes the committed script through a retained
    descriptor (`/proc/self/fd/<fd>`), so `BASH_SOURCE[0]` names the fd
    path, never the canonical repository path; the trusted parent pins the
    canonical root as FACTORY_VERIFIER_ROOT exactly like the sibling
    documentation gates and verify-boilerplate.sh.  An unsafe/non-root
    marker is rejected before any repository operation.
    """

    def _docs_via_fd(self, root: Path, *, verifier_root: str | None):
        script = root / "scripts" / "check-docs-sync.sh"
        descriptor = os.open(
            script,
            os.O_RDONLY
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0),
        )
        try:
            env = dict(os.environ)
            env.pop("FACTORY_VERIFIER_ROOT", None)
            if verifier_root is not None:
                env["FACTORY_VERIFIER_ROOT"] = str(verifier_root)
            result = subprocess.run(
                [smoke_module._pinned_bash(), f"/proc/self/fd/{descriptor}"],
                cwd=str(root), env=env, pass_fds=(descriptor,),
                capture_output=True, text=True, timeout=240,
            )
        finally:
            os.close(descriptor)
        return result

    def test_docs_gate_fd_invocation_honors_pinned_root(self) -> None:
        """The full documentation gate passes through a retained descriptor."""
        ws = self.make(foreign=False)
        result = self._docs_via_fd(ws.root, verifier_root=str(ws.root))
        self.assertEqual(result.returncode, 0, result.stderr[-2000:])
        self.assertIn("documentation change gate passed", result.stdout)

    def test_docs_gate_rejects_unsafe_nonroot_marker(self) -> None:
        """A substituted/non-root marker fails closed before any operation."""
        ws = self.make(foreign=False)
        bad_candidates = (
            ws.root / "scripts",            # inside the repo: not the root
            self.tmp / "not-a-repo",        # no repository markers at all
        )
        for bad in bad_candidates:
            bad.mkdir(parents=True, exist_ok=True)
            result = self._docs_via_fd(ws.root, verifier_root=str(bad))
            self.assertNotEqual(result.returncode, 0)
            self.assertIn(
                "cannot resolve the canonical repository root", result.stderr
            )
            self.assertNotIn(
                "documentation change gate passed", result.stdout
            )

    def test_docs_gate_fd_exec_without_pinned_root_fails_closed(self) -> None:
        """Without the pinned root the fd path cannot name the repository."""
        ws = self.make(foreign=False)
        result = self._docs_via_fd(ws.root, verifier_root=None)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(
            "cannot resolve the canonical repository root", result.stderr
        )


def _smoke_campaign_config(ws):
    """The campaign config driving the smoke fixture (in-process)."""
    campaign_id = common.smoke_campaign_id(ws.head)
    gate = [
        "./" + common.GATE_REL, "--root", str(ws.root),
        "--evidence", EVIDENCE_REL, "--task", "22",
        "--campaign-id", campaign_id, "--mode",
    ]
    return campaign_module.derive_campaign_config(
        ws.root,
        campaign_id=campaign_id,
        rounds=1,
        branch=BRANCH,
        plan_path=PLAN_REL,
        planning_attempts=3,
        implementation_attempts=3,
        provider="synthetic",
        model="fixture-model",
        backend="",
        role_driver=common.DESIGNATED_DRIVER_REL,
        developer_evidence_path=EVIDENCE_REL,
        scenario_path="",
        acceptance_command=tuple(gate + ["acceptance"]),
        verification_command=tuple(gate + ["verify"]),
        capability_command=(),
        phase_result_path=common.PHASE_RESULT_REL,
        audit_result_path=common.AUDIT_RESULT_REL,
        role_timeout=900.0,
        gate_timeout=1800.0,
    )


if __name__ == "__main__":
    unittest.main()
