#!/usr/bin/env python3
"""Task 16 hidden adversarial conformance suite (FACTORY-LOOP-SPEC §22).

This test lives under the hidden ``.factory/tests/`` namespace because the
specification (HIDE-01, §3) keeps harness-only tests out of the adopting
product's visible ``.factory/tests/legacy/`` tree.  It is the deterministic verification for
TEST-01 (§22, tests 1-27) and drives the *real* authorities of the new
Python control plane — never reimplementations — through fresh subprocesses
and test-owned temporary Git repositories:

* **machine manifest**: the committed ``adversarial-manifest.json``
  (schema ``factory-adversarial-manifest/v1``) names exactly the 27
  numbered §22 cases; this suite rejects a manifest with any missing,
  duplicate, or skipped case and rejects any case test that the manifest
  does not declare (the manifest is the single authority, never a comment);
* **every case is non-vacuous**: each of the 27 cases runs against the
  committed authority (``campaign.py``, ``launch.py`` through the real
  ``.factory/tools/pi2-secure-exec.py`` wrapper, ``lock.py``, ``selector.py``,
  ``state.py``, ``usage.py``, ``evidence.py``, ``migration.py``,
  ``workspace_confinement.py`` + the committed confine launcher,
  ``machine-receipt.py``/``check-audit-receipts.py``,
  ``campaign-verifier-binding.py``, the commit-boundary shims) in a fresh
  subprocess and/or a test-owned temporary repository; the fixture role
  driver (``campaign_driver.py``) is the only simulated model, used exactly
  where the specification designates the deterministic embedded role seam —
  never for acceptance evidence;
* **finite five-round campaigns**: a five-round success campaign and a
  five-round final-findings campaign run the real campaign state machine,
  transitions, prompts, and deterministic gates to their §14 terminals;
* **Task 16 residuals**: the deprecated shell freeze-marker decision is
  routed through the retained hidden authority (``migration.py freeze
  --guard``) with fresh no-follow re-stat semantics (substitution
  atomicity), and the legacy maintenance verifier is routed through the
  retained descriptor authority (``campaign-verifier-binding.py --mode
  maintenance``) so a pathname substitution or a missing verifier fails
  closed exactly like the campaign verifier.

The suite is hermetic: every scenario runs in a test-owned temporary
directory; nothing writes to the live repository and nothing touches
``.ralph/``.
"""

from __future__ import annotations

import ast
import hashlib
import importlib.util
import inspect
import json
import os
from pathlib import Path
import re
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
import types
import unittest

ROOT = Path(__file__).resolve().parents[2]
LOOP = ROOT / ".factory" / "loop"
FIXTURES = ROOT / ".factory" / "tests" / "fixtures"
SCHEMAS = ROOT / ".factory" / "schemas"
MANIFEST_PATH = ROOT / ".factory" / "tests" / "adversarial-manifest.json"
STATE_DIR = ".factory-state"
STATE_FILE = "factory-loop.json"
BRANCH = "fixture-main"

sys.path.insert(0, str(LOOP))

import campaign as campaign_module  # noqa: E402
import evidence as evidence_module  # noqa: E402
import gitutil  # noqa: E402
import launch as launch_module  # noqa: E402
import lock as lock_module  # noqa: E402
import migration as migration_module  # noqa: E402
import plan_parser  # noqa: E402
import selector as selector_module  # noqa: E402
import state as state_module  # noqa: E402
import usage as usage_module  # noqa: E402
import workspace_confinement as wc  # noqa: E402

# Reuse the committed sibling campaign suite for the fixture workspace and
# helper conventions (never copy machinery into this module).
_CAMPAIGN_SUITE = ROOT / ".factory" / "tests" / "test-factory-campaign.py"
_campaign_spec = importlib.util.spec_from_file_location(
    "factory_campaign_suite", _CAMPAIGN_SUITE)
FACTORY_CAMPAIGN = importlib.util.module_from_spec(_campaign_spec)
assert _campaign_spec.loader is not None
_campaign_spec.loader.exec_module(FACTORY_CAMPAIGN)

# Reuse the committed sibling usage suite for the loopback settings server
# and the synthetic-credential constants (case 13 and case 24).
_USAGE_SUITE = ROOT / ".factory" / "tests" / "test-factory-usage.py"
_usage_spec = importlib.util.spec_from_file_location(
    "factory_usage_suite", _USAGE_SUITE)
FACTORY_USAGE = importlib.util.module_from_spec(_usage_spec)
assert _usage_spec.loader is not None
_usage_spec.loader.exec_module(FACTORY_USAGE)

# Reuse the committed sibling findings suite for the fixture workspace whose
# committed template set also carries the findings-revised planner templates
# (case 5) and for the committed findings authority helpers (payload
# rebuild, receipt naming, canonical result bytes).
_FINDINGS_SUITE = ROOT / ".factory" / "tests" / "test-factory-findings.py"
_findings_spec = importlib.util.spec_from_file_location(
    "factory_findings_suite", _FINDINGS_SUITE)
FACTORY_FINDINGS = importlib.util.module_from_spec(_findings_spec)
assert _findings_spec.loader is not None
_findings_spec.loader.exec_module(FACTORY_FINDINGS)
FindingsWorkspace = FACTORY_FINDINGS.FindingsWorkspace

GIT = gitutil.GIT_EXECUTABLE
PY = os.path.realpath(sys.executable)
TRUE_EXECUTABLE = FACTORY_CAMPAIGN.TRUE_EXECUTABLE
FALSE_EXECUTABLE = FACTORY_CAMPAIGN.FALSE_EXECUTABLE
FixtureWorkspace = FACTORY_CAMPAIGN.FixtureWorkspace
SUCCESS_SCENARIO = FACTORY_CAMPAIGN.SUCCESS_SCENARIO
assert_terminal = FACTORY_CAMPAIGN.assert_terminal
assert_history = FACTORY_CAMPAIGN.assert_history
run = FACTORY_CAMPAIGN.run
_git = FACTORY_CAMPAIGN._git
sha256 = FACTORY_CAMPAIGN.sha256
gen_plan = FACTORY_CAMPAIGN.gen_plan
PLAN_REL = FACTORY_CAMPAIGN.PLAN_REL
DRIVER_REL = FACTORY_CAMPAIGN.DRIVER_REL

VISIBLE_FIXTURES = ROOT / ".factory" / "tests" / "legacy" / "fixtures"
# Synthetic secret marker: never a real credential.  Its presence in any
# migration report proves a foreign ``.ralph`` byte leaked.
RALPH_SECRET = b"RALPH-FOREIGN-SENTINEL-9f4c1a"
SYNTH_COOKIE = FACTORY_USAGE.SYNTH_COOKIE
SYNTH_COOKIE_NAME = FACTORY_USAGE.SYNTH_COOKIE_NAME
SYNTH_COOKIE_VALUE = FACTORY_USAGE.SYNTH_COOKIE_VALUE
SYNTH_AID = FACTORY_USAGE.SYNTH_AID
_ScriptedServer = FACTORY_USAGE._ScriptedServer
_find_fetch_child = FACTORY_USAGE._find_fetch_child
# Load the sibling confinement suite for the committed Landlock probe source.
_CONFINEMENT_SUITE = ROOT / ".factory" / "tests" / "test-factory-confinement.py"
_confinement_spec = importlib.util.spec_from_file_location(
    "factory_confinement_suite", _CONFINEMENT_SUITE)
FACTORY_CONFINEMENT = importlib.util.module_from_spec(_confinement_spec)
assert _confinement_spec.loader is not None
_confinement_spec.loader.exec_module(FACTORY_CONFINEMENT)
PROBE_SOURCE = FACTORY_CONFINEMENT.PROBE_SOURCE
wc = FACTORY_CONFINEMENT.wc  # noqa: F811  (authoritative workspace-confinement module)
FORBIDDEN = FACTORY_CONFINEMENT.FORBIDDEN


# ---------------------------------------------------------------------------
# Manifest (single authority for the 27 §22 cases)
# ---------------------------------------------------------------------------

def load_manifest() -> dict:
    with open(MANIFEST_PATH, encoding="utf-8") as stream:
        manifest = json.load(stream)
    assert isinstance(manifest, dict), "manifest must be an object"
    assert manifest.get("schema") == "factory-adversarial-manifest/v1", \
        f"wrong manifest schema: {manifest.get('schema')!r}"
    cases = manifest.get("cases")
    assert isinstance(cases, list) and cases, "manifest declares no cases"
    return manifest


def manifest_cases() -> list:
    return load_manifest()["cases"]


class ManifestCompletenessTest(unittest.TestCase):
    """The machine manifest is the authority: 27 unique, numbered 1-27, each
    with a registered non-skipped test; no extra case test exists."""

    def _suite_case_methods(self):
        names = []
        for name in dir(CaseAdversarialSuite):
            if re.fullmatch(r"test_case_\d\d_.+", name):
                names.append(name)
        return sorted(names)

    def test_exactly_twenty_seven_cases(self) -> None:
        cases = manifest_cases()
        self.assertEqual(len(cases), 27, "the §22 suite must manifest exactly 27 cases")
        numbers = [entry["case"] for entry in cases]
        self.assertEqual(numbers, list(range(1, 28)),
                         "case numbers must be exactly 1..27 (missing/gap/duplicate)")
        self.assertEqual(len(set(numbers)), 27, "case numbers must be unique")

    def test_every_entry_is_well_formed(self) -> None:
        for entry in manifest_cases():
            self.assertIsInstance(entry, dict)
            self.assertEqual(set(entry), {"case", "title", "requirement_ids", "test"},
                             f"case {entry.get('case')}: unexpected manifest keys")
            self.assertIsInstance(entry["case"], int)
            self.assertIsInstance(entry["title"], str) and entry["title"]
            self.assertIsInstance(entry["requirement_ids"], list) and entry["requirement_ids"]
            self.assertIsInstance(entry["test"], str) and re.fullmatch(
                r"test_case_\d\d_.+", entry["test"])

    def test_every_manifest_case_has_a_registered_non_skipped_test(self) -> None:
        for entry in manifest_cases():
            method = getattr(CaseAdversarialSuite, entry["test"], None)
            self.assertIsNotNone(method, f"case {entry['case']}: missing test "
                                         f"{entry['test']!r}")
            self.assertTrue(callable(method))
            self.assertFalse(
                getattr(method, "__unittest_skip__", False),
                f"case {entry['case']}: test {entry['test']!r} must never be skipped")
            # A skipped conformance case is a hard failure, never a skip: no
            # case method may call ``skipTest`` (or carry a skip decorator,
            # already rejected above) in its own source.  ``inspect.getsource``
            # returns the exact committed method body, so a skip added to a
            # case method fails the manifest/source gate immediately.
            source = inspect.getsource(method)
            self.assertIsNone(
                re.search(r"self\.skipTest\s*\(", source),
                f"case {entry['case']}: test {entry['test']!r} must never "
                f"call skipTest (a §22 case is a hard requirement)")


    def test_no_extra_case_test_exists(self) -> None:
        declared = {entry["test"] for entry in manifest_cases()}
        actual = set(self._suite_case_methods())
        self.assertEqual(actual, declared,
                         "case tests and manifest must be the exact same set "
                         "(extra or missing)")

    def test_loader_collects_every_case(self) -> None:
        suite = unittest.defaultTestLoader.loadTestsFromTestCase(CaseAdversarialSuite)
        collected = sum(
            1 for _ in _iter_tests(suite)
            if re.fullmatch(r"test_case_\d\d_.+", type(_).__name__) or
            re.fullmatch(r"test_case_\d\d_.+", getattr(_, "_testMethodName", ""))
        )
        self.assertEqual(collected, 27, f"the loader collected {collected} case tests")


def _iter_tests(suite):
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            yield from _iter_tests(item)
        else:
            yield item


# ---------------------------------------------------------------------------
# Fixture machinery
# ---------------------------------------------------------------------------

FIVE_TASK_SPECS: list[dict] = [
    {
        "number": 1, "title": "Implement the fixture feature",
        "status": "pending", "priority": 10, "dependencies": [],
        "blocked_on": None, "scope": "initial scope statement.",
        "verification": "`src/work-1.md`",
    },
    {
        "number": 2, "title": "Implement the second feature",
        "status": "pending", "priority": 20, "dependencies": [],
        "blocked_on": None, "verification": "`src/work-2.md`",
    },
    {
        "number": 3, "title": "Implement the third feature",
        "status": "pending", "priority": 30, "dependencies": [1],
        "blocked_on": None, "verification": "`src/work-3.md`",
    },
    {
        "number": 4, "title": "Implement the fourth feature",
        "status": "pending", "priority": 40, "dependencies": [2],
        "blocked_on": None, "verification": "`src/work-4.md`",
    },
    {
        "number": 5, "title": "final",
        "status": "pending", "priority": 1, "dependencies": [3, 4],
        "blocked_on": None, "verification": "`src/work-5.md`",
    },
]


class FiveRoundWorkspace(FixtureWorkspace):
    """Fixture workspace whose committed template set drives exactly five
    rounds (one runnable task per round, the final task completing the plan)."""

    def _generate_plans(self, common: dict) -> None:
        ws = self.root
        gen_plan(ws, common, PLAN_REL, FIVE_TASK_SPECS)
        for round_no in (1, 2, 3, 4, 5):
            revised = [
                {
                    **t,
                    "scope": (t.get("scope", "fixture-scoped work only.")
                              + f" revised {round_no}."),
                    "status": "complete" if t["number"] < round_no else t["status"],
                }
                for t in FIVE_TASK_SPECS
            ]
            gen_plan(ws, common, f"fixture/templates/planner-{round_no}.md", revised)
        complete = [{**dict(t), "status": "complete"} for t in FIVE_TASK_SPECS]
        gen_plan(ws, {**common, "lifecycle": "complete"},
                 "fixture/templates/planner-complete.md", complete)
        blocked = [
            {**dict(FIVE_TASK_SPECS[0]), "status": "blocked",
             "blocked_on": "external-capability-required"},
            {**dict(FIVE_TASK_SPECS[1]), "dependencies": [1]},
            *FIVE_TASK_SPECS[2:],
        ]
        gen_plan(ws, common, "fixture/templates/planner-blocked.md", blocked)
        gen_plan(ws, {**common, "base_commit": "1" * 40},
                 "fixture/templates/planner-unbound.md", FIVE_TASK_SPECS)
        for task in FIVE_TASK_SPECS:
            number = task["number"]
            complete_tasks = [
                {**dict(t),
                 "status": "complete" if t["number"] <= number else t["status"]}
                for t in FIVE_TASK_SPECS
            ]
            dev_common = (
                {**common, "lifecycle": "complete"}
                if all(t["status"] == "complete" for t in complete_tasks)
                else common
            )
            gen_plan(ws, dev_common, f"fixture/templates/dev-{number}.md",
                     complete_tasks)


def prompt_set_digest(ws: Path) -> str:
    """The exact campaign-bound prompt-set digest (launch authority)."""
    digest = hashlib.sha256()
    for role in sorted(launch_module.ROLES):
        digest.update(role.encode("utf-8"))
        digest.update(b"\x00")
        digest.update((ws / ".factory" / "prompts" / f"{role}.md").read_bytes())
    return digest.hexdigest()


def file_digest(path: Path) -> str:
    with open(path, "rb") as stream:
        return hashlib.sha256(stream.read()).hexdigest()


class _AdversarialBase(unittest.TestCase):
    """One private temporary directory per test (removed on every exit)."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="factory-adversarial-test."))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self._workspace_count = 0

    def make(
        self,
        scenario: dict,
        *,
        workspace_cls: type = FixtureWorkspace,
        **kwargs,
    ) -> FixtureWorkspace:
        self._workspace_count += 1
        ws = workspace_cls(
            self.tmp / f"ws{self._workspace_count}",
            scenario=scenario, **kwargs,
        )
        ws.commit_scenario()
        return ws

    def state_cli(self, root: Path, *args: str) -> subprocess.CompletedProcess[str]:
        return run(
            [PY, str(LOOP / "state.py"), "--root", str(root), *args],
            check=False,
        )

    def guard_cli(self, *args: str, input_bytes: bytes = b"",
                  env: dict | None = None) -> subprocess.CompletedProcess[bytes]:
        argv = [PY, str(LOOP / "usage.py")]
        argv += list(args)
        if "--env-file" not in args and "--html-file" not in args \
                and "--settings-url" not in args and "--cookie-file" not in args:
            argv += ["--env-file", str(self.tmp / "no-store")]
        base_env = dict(os.environ)
        for key in list(base_env):
            if key.startswith("OLLAMA_"):
                base_env.pop(key)
        if env:
            base_env.update(env)
        return subprocess.run(
            argv, input=input_bytes, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, env=base_env, cwd=str(ROOT),
        )


# ---------------------------------------------------------------------------
# Launch-workspace helpers (real wrapper/confine-launcher through the commit
# boundary, exactly like the Task 6 supervision suite)
# ---------------------------------------------------------------------------

def prepare_launch_workspace(ws: FixtureWorkspace, *backend_rel: str) -> None:
    """Copy the committed wrapper/confine/schema into the fixture workspace
    and commit every backend so the launch authority verifies the exact
    committed blobs (F2/F5).  Returns nothing; the workspace root is updated
    in place."""
    root = ws.root
    scripts = root / ".factory" / "tools"
    scripts.mkdir(parents=True, exist_ok=True)
    (root / ".factory" / "loop").mkdir(parents=True, exist_ok=True)
    (root / ".factory" / "schemas").mkdir(parents=True, exist_ok=True)
    (root / "src" / ".factory-test-output").mkdir(parents=True, exist_ok=True)
    shutil.copy2(ROOT / ".factory" / "tools" / "pi2-secure-exec.py",
                 scripts / "pi2-secure-exec.py")
    # Task 11: the model-side Pi guard extension is a committed fixture blob
    # too — the launch authority verifies the working-tree extension equals
    # the committed blob and always loads it through ``--extension``.
    shutil.copy2(ROOT / ".factory" / "tools" / "pi-factory-guard-extension.mjs",
                 scripts / "pi-factory-guard-extension.mjs")
    (scripts / "pi-cli-shims").mkdir(exist_ok=True)
    shutil.copy2(ROOT / ".factory" / "tools" / "pi-cli-shims" / "git",
                 scripts / "pi-cli-shims" / "git")
    for module in ("confine_launcher.py", "usage.py", "usage_fetch.py"):
        shutil.copy2(LOOP / module, root / ".factory" / "loop" / module)
    shutil.copy2(SCHEMAS / "factory-confinement-v1.schema.json",
                 root / ".factory" / "schemas" / "factory-confinement-v1.schema.json")
    for rel in backend_rel:
        (root / rel).chmod(0o700)
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "launch fixture wrapper and backends")


def launch_cli_args(
    ws: FixtureWorkspace,
    *,
    role: str = "developer",
    backend: str,
    task_id: int | None = None,
    runtime_limit: float = 30.0,
    inactivity_limit: float = 20.0,
) -> list[str]:
    root = ws.root
    head = _git(root, "rev-parse", "HEAD").stdout.strip()
    argv = [
        PY, str(LOOP / "launch.py"), "launch",
        "--root", str(root),
        "--role", role,
        "--model", "synthetic-model",
        "--provider", "synthetic",
        "--backend", str(root / backend),
        "--role-prompt", str(root / ".factory" / "prompts" / f"{role}.md"),
        "--role-prompt-digest", file_digest(root / ".factory" / "prompts" / f"{role}.md"),
        "--prompt-set-digest", prompt_set_digest(root),
        "--policy", str(root / "AGENTS.md"),
        "--policy-digest", file_digest(root / "AGENTS.md"),
        "--spec", str(root / "docs" / "SPEC.md"),
        "--spec-digest", file_digest(root / "docs" / "SPEC.md"),
        "--plan", str(root / PLAN_REL),
        "--plan-digest", file_digest(root / PLAN_REL),
        "--bound-commit", head,
        "--allowed-tools", "read,bash",
        "--runtime-limit", str(runtime_limit),
        "--inactivity-limit", str(inactivity_limit),
    ]
    if task_id is not None:
        excerpt = run(
            [PY, str(LOOP / "launch.py"), "excerpt",
             "--plan", str(root / PLAN_REL), "--task-id", str(task_id)],
            check=False,
        )
        excerpt_digest = json.loads(excerpt.stdout)["digest"]
        argv += ["--task-id", str(task_id),
                 "--task-excerpt-digest", excerpt_digest]
    return argv


def run_launch(
    ws: FixtureWorkspace, *args: str, check: bool = False,
) -> subprocess.CompletedProcess[str]:
    result = run([*args], check=False)
    if check and result.returncode != 0:
        raise AssertionError((result.returncode, result.stdout[-2000:],
                              result.stderr[-2000:]))
    return result


# ---------------------------------------------------------------------------
# The 27 §22 cases
# ---------------------------------------------------------------------------

class CaseAdversarialSuite(_AdversarialBase):
    """One real, non-vacuous adversarial scenario per §22 numbered case."""

    # -- case 1: fresh processes and only allowed authoritative inputs --------

    def test_case_01_fresh_roles_and_allowed_inputs(self) -> None:
        # A fresh role process through the real launch authority: the
        # developer attempt runs as a new process whose environment is
        # exactly the allowlist + invocation fields (no session/memory/legacy
        # keys), and the campaign drives each static role through the driver
        # seam as a separate fresh process.
        ws = self.make(SUCCESS_SCENARIO)
        probe = ws.root / "backend-probe.py"
        marker = ws.root / "src" / ".factory-test-output" / "probe-1.json"
        probe.write_text(
            "#!/usr/bin/env python3\n"
            "import json, os, sys\n"
            f"marker = {str(marker)!r}\n"
            "out = {'pid': os.getpid(), 'ppid': os.getppid(),\n"
            "       'argv': sys.argv[1:], 'env': dict(sorted(os.environ.items()))}\n"
            "with open(marker, 'w', encoding='utf-8') as stream:\n"
            "    json.dump(out, stream, sort_keys=True)\n"
            "print('probe-backend-ok')\n",
            encoding="utf-8",
        )
        prepare_launch_workspace(ws, "backend-probe.py")
        rc, data = ws.run_cli()
        self.assertEqual(rc, 0, "the campaign with the fresh-role driver must succeed")
        assert_terminal(self, data, terminal_phase="success",
                        terminal_outcome="pass", exit_code=0, rounds_completed=1)
        result = run_launch(
            ws, *launch_cli_args(ws, backend="backend-probe.py", task_id=1))
        self.assertEqual(result.returncode, 0, result.stderr[-2000:])
        launch_result = json.loads(result.stdout.splitlines()[-1])
        self.assertEqual(launch_result["outcome"], "completed")
        self.assertTrue(marker.is_file(), "the probe backend must have run")
        with open(marker, encoding="utf-8") as stream:
            probe_data = json.load(stream)
        self.assertNotEqual(probe_data["pid"], os.getpid(),
                            "the role must run in a fresh process, never the test process")
        self.assertNotEqual(probe_data["ppid"], os.getpid(),
                            "the role process group must be a fresh descendant")
        env = probe_data["env"]
        # Task 8 confined launch: the role must run with a fresh private
        # sanitized home (never the operator's real home).
        sanitized_home = env.get("HOME", "")
        self.assertTrue(
            sanitized_home.startswith("/tmp/factory-home-"),
            "the confined role must use a fresh private sanitized home",
        )
        # Only the documented allowlist keys plus the FACTORY_LOOP_LAUNCH_*
        # invocation fields, the single documented PI_FACTORY_GUARD_DIGEST
        # guard-binding field (Task 11: the exact committed credential-guard
        # digest forwarded to the model-side extension), and the
        # confined-launch sanitized HOME/XDG keys (all pointing under the
        # fresh private factory-home-* directory) may exist; nothing else
        # leaks in.
        sanitized_home_keys = {
            "HOME", "XDG_STATE_HOME", "XDG_CONFIG_HOME", "XDG_CACHE_HOME",
            "XDG_DATA_HOME", "XDG_RUNTIME_DIR",
        }
        for key in env:
            if key.startswith("FACTORY_LOOP_LAUNCH_"):
                continue
            if key == launch_module.PI_FACTORY_GUARD_DIGEST_ENV:
                self.assertRegex(
                    env[key], r"^[0-9a-f]{64}$",
                    "the forwarded guard digest must be the exact 64-hex "
                    "SHA-256 of the committed guard",
                )
                continue
            if key == launch_module.PI_FACTORY_GUARD_PYTHON_ENV:
                # The pinned trusted interpreter path (Task 11 review): a
                # non-credential absolute interpreter the model-side guard
                # extension uses to run the exact committed guard.
                self.assertTrue(
                    env[key].startswith("/nix/store/"),
                    f"the guard interpreter key {key!r} must be an immutable "
                    "store path",
                )
                continue
            if key in sanitized_home_keys:
                self.assertTrue(
                    env[key].startswith("/tmp/factory-home-"),
                    f"the sanitized-home key {key!r} must point under the "
                    "fresh private home directory",
                )
                continue
            self.assertIn(key, launch_module.ENV_ALLOWLIST,
                          f"unauthorized child environment key {key!r}")
        # No PI_ session/memory key ever reaches the child: the single
        # documented guard-digest invocation field is the only PI_ key the
        # launch authority forwards (and it is verified 64-hex above).
        pi_keys = [key for key in env if key.startswith("PI_")]
        self.assertEqual(
            sorted(pi_keys),
            sorted([launch_module.PI_FACTORY_GUARD_DIGEST_ENV,
                    launch_module.PI_FACTORY_GUARD_PYTHON_ENV]),
            f"a PI_ session/memory key reached the role child: {pi_keys}",
        )
        for forbidden_prefix in ("OLLAMA_", "GIT_", "FACTORY_CAMPAIGN_",
                                 "FACTORY_LOOP_CAMPAIGN_", "RALPH_"):
            for key in env:
                self.assertFalse(
                    key.startswith(forbidden_prefix),
                    f"a {forbidden_prefix} key reached the role child: {key!r}")
        joined_env = json.dumps(env)
        for legacy in (".ralph", ".factory-state", "scratchpad", "memory"):
            self.assertNotIn(legacy, joined_env,
                             f"a legacy path leaked into the role environment: {legacy}")
        # The authoritative inputs (plan/spec/prompt) are delivered through
        # the prompt file channel, not the environment: the env never carries
        # plan or spec content.
        self.assertNotIn("plan", env)
        self.assertNotIn("SPEC", env)

    # -- case 2: memory/session resume is disabled ---------------------------

    def test_case_02_memory_session_resume_disabled(self) -> None:
        ws = self.make(SUCCESS_SCENARIO)
        marker = ws.root / "src" / ".factory-test-output" / "probe-2.json"
        backend_src = (
            "#!/usr/bin/env python3\n"
            "import json, os, sys\n"
            f"marker = {str(marker)!r}\n"
            "out = {'argv': sys.argv[1:],\n"
            "       'env': dict(sorted(os.environ.items()))}\n"
            "with open(marker, 'w', encoding='utf-8') as stream:\n"
            "    json.dump(out, stream, sort_keys=True)\n"
            "print('probe-backend-ok')\n"
        )
        backend = ws.root / "backend-session.py"
        backend.write_text(backend_src, encoding="utf-8")
        prepare_launch_workspace(ws, "backend-session.py")
        argv1 = launch_cli_args(ws, backend="backend-session.py", task_id=1)
        result1 = run_launch(ws, *argv1)
        self.assertEqual(result1.returncode, 0, result1.stderr[-2000:])
        payload1 = json.loads(result1.stdout.splitlines()[-1])
        self.assertEqual(payload1["outcome"], "completed")
        with open(marker, encoding="utf-8") as stream:
            probe1 = json.load(stream)
        # The backend argv is the structural one-shot contract: no resume /
        # continue / session / fork flag can appear, and the no-session /
        # no-skills / no-themes / no-context-files flags are always present.
        for flag in launch_module.FORBIDDEN_BACKEND_FLAGS:
            self.assertNotIn(flag, probe1["argv"],
                             f"the forbidden resume flag {flag!r} reached the child argv")
        for flag in ("--no-session", "--no-skills", "--no-themes",
                     "--no-context-files", "--print"):
            self.assertIn(flag, probe1["argv"],
                          f"the one-shot flag {flag!r} must be present")
        # The child environment carries no session/memory/credential keys:
        # the single documented PI_FACTORY_GUARD_DIGEST invocation field (the
        # exact 64-hex committed guard digest) is the only PI_ key the launch
        # authority forwards.
        pi_keys = [key for key in probe1["env"] if key.startswith("PI_")]
        self.assertEqual(
            sorted(pi_keys),
            sorted([launch_module.PI_FACTORY_GUARD_DIGEST_ENV,
                    launch_module.PI_FACTORY_GUARD_PYTHON_ENV]),
            f"a PI_ session/memory key reached the child: {pi_keys}",
        )
        self.assertRegex(
            probe1["env"].get(launch_module.PI_FACTORY_GUARD_DIGEST_ENV, ""),
            r"^[0-9a-f]{64}$",
            "the forwarded guard digest must be the exact committed digest",
        )
        for key in probe1["env"]:
            self.assertFalse(key.startswith("OLLAMA_"),
                             f"an OLLAMA_ credential key reached the child: {key!r}")
            self.assertFalse(key.startswith("GIT_"),
                             f"a GIT_ redirector reached the child: {key!r}")
        # A second attempt gets a distinct fresh session directory (no shared
        # session state can exist between attempts).
        argv2 = launch_cli_args(ws, backend="backend-session.py", task_id=1)
        result2 = run_launch(ws, *argv2)
        self.assertEqual(result2.returncode, 0, result2.stderr[-2000:])
        with open(marker, encoding="utf-8") as stream:
            probe2 = json.load(stream)
        session_dirs = []
        for probe in (probe1, probe2):
            args = probe["argv"]
            index = args.index("--session-dir")
            session_dirs.append(args[index + 1])
        self.assertEqual(len(set(session_dirs)), 2,
                         "each attempt must get a fresh, distinct session directory")
        self.assertTrue(all(d.startswith("/tmp/factory-loop-session-")
                            for d in session_dirs))
        # The committed launch authority forbids the resume flags and
        # mandates the one-shot flags in the exact constructed argv (the
        # secure wrapper is a pure prompt-snapshot exec boundary, invoked
        # by that authority — never a reimplementation of the flags).
        launch_text = (LOOP / "launch.py").read_text(encoding="utf-8")
        for flag in launch_module.FORBIDDEN_BACKEND_FLAGS:
            self.assertIn(flag, launch_text,
                          f"the launch authority must forbid {flag}")
        for flag in ("--no-session", "--no-skills", "--no-themes",
                     "--no-context-files", "--print"):
            self.assertIn(flag, launch_text,
                          f"the launch authority must carry {flag}")

    # -- case 3: plan-derived task selection is deterministic ----------------

    def test_case_03_plan_derived_selection_deterministic(self) -> None:
        # The selector is a pure function of plan + state: two fresh
        # subprocess invocations on the same committed plan must select the
        # identical task, and two identical campaigns must produce
        # byte-identical phase histories.
        ws = self.make(SUCCESS_SCENARIO)
        plan = ws.root / PLAN_REL
        # The selector is bound to the plan's own authoritative front-matter
        # base (the same anchor the campaign derives from the committed plan
        # at the state phase base) — never a bare HEAD rev, which postdates
        # the plan's spec binding.
        bound_base = plan_parser.Plan.from_file(plan).base_commit
        selections = []
        for _ in range(2):
            proc = run(
                [PY, str(LOOP / "selector.py"), "select", str(plan),
                 "--bound-base-commit", bound_base],
                check=False,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr[-1000:])
            selections.append(proc.stdout.strip())
        self.assertEqual(selections[0], selections[1])
        self.assertEqual(selections[0], "selected=1")
        # Lexicographic priority-then-ID tie-break is deterministic too.
        tiebreak = FIXTURES / "plan-select-lexicographic-tiebreak.md"
        first = run(
            [PY, str(LOOP / "selector.py"), "select", str(tiebreak)],
            check=False,
        )
        second = run(
            [PY, str(LOOP / "selector.py"), "select", str(tiebreak)],
            check=False,
        )
        self.assertEqual(first.returncode, 0, first.stderr[-1000:])
        self.assertEqual(first.stdout.strip(), second.stdout.strip())
        self.assertRegex(first.stdout.strip(), r"^selected=\d+$")
        # Two identical campaigns on identical committed fixtures select the
        # same deterministic sequence: the phase-history (round, phase,
        # outcome) triples are byte-identical (the raw records also carry
        # per-repository commit hashes, which legitimately differ across
        # workspaces).
        ws2 = self.make(SUCCESS_SCENARIO)
        rc1, data1 = ws.run_cli()
        rc2, data2 = ws2.run_cli()
        self.assertEqual(rc1, 0)
        self.assertEqual(rc2, 0)
        sequence = lambda data: [  # noqa: E731
            (r["round"], r["phase"], r["outcome"]) for r in data["phase_history"]]
        self.assertEqual(sequence(data1), sequence(data2))

    # -- case 4: no runtime task ledger is read or created -------------------

    def test_case_04_no_runtime_task_ledger(self) -> None:
        ws = self.make(SUCCESS_SCENARIO)
        rc, data = ws.run_cli()
        self.assertEqual(rc, 0)
        assert_terminal(self, data, terminal_phase="success",
                        terminal_outcome="pass", exit_code=0, rounds_completed=1)
        state_dir = ws.root / STATE_DIR
        self.assertTrue(state_dir.is_dir())
        names = sorted(p.name for p in state_dir.iterdir())
        # The one trusted lifecycle surface: control state, the append-only
        # digest ledger, and the published campaign result.  The transient
        # phase-result handoff files (phase-result.json / audit-result.json)
        # are consumed and removed by the campaign authority after each phase
        # — they are the orchestrator's own transient handoff channel, never
        # product state — so a finished campaign carries exactly the three
        # persistent surfaces.  No runtime task ledger, memory store, event
        # stream, or scratchpad.
        expected = {"factory-loop.json", "state-digest-ledger.jsonl",
                    "campaign-result-campaign.json"}
        self.assertEqual(set(names), expected, f"unexpected .factory-state entries: {names}")
        # The transient result files were consumed, never left behind.
        self.assertFalse((state_dir / "phase-result.json").exists())
        self.assertFalse((state_dir / "audit-result.json").exists())
        for name in names:
            lower = name.lower()
            for forbidden in ("task", "memory", "scratch", "ralph", "event", "session"):
                self.assertNotIn(forbidden, lower,
                                 f"a runtime task/legacy surface appeared: {name}")
        # The plan file is the sole task authority: no other committed or
        # runtime file in the workspace acts as a task ledger.  (Denial prose
        # in the committed audit-objectives registry and the fixture plan's
        # own interaction inventory names the *denied* surface; an authority
        # is a file whose name is task/ledger shaped or whose structured
        # JSON enumerates task state.)
        ledger_shaped_names = (
            "task-ledger", "task_ledger", "taskledger",
            "runtime-task", "runtime_task", "tasks.json", "task-state",
            "task_state", "task-queue", "task_queue", "event-stream",
            "event_stream", "memory.json", "memory.jsonl",
            "scratchpad.json", "scratchpad.jsonl",
        )
        for candidate in ws.root.rglob("*"):
            if not candidate.is_file() or ".git" in candidate.parts:
                continue
            rel = candidate.relative_to(ws.root).as_posix()
            if rel == PLAN_REL:
                continue
            lower = rel.lower()
            if any(token in lower for token in ledger_shaped_names):
                self.fail(f"a task-ledger authority exists at {candidate}")
            # A structured file whose *name* names a task/runtime surface and
            # whose parsed content enumerates task state is a ledger; the
            # committed fixture-spec.json is the plan tool's task-definition
            # input (its name is the tool's own spec, never a runtime
            # surface) and the plan itself is excluded above.
            if candidate.suffix in (".json", ".jsonl") and any(
                token in lower
                for token in ("task", "ledger", "queue", "runtime",
                              "memory", "event", "scratch")
            ):
                try:
                    data = json.loads(candidate.read_text(
                        encoding="utf-8", errors="replace"))
                except (ValueError, UnicodeDecodeError):
                    continue
                if isinstance(data, dict) and isinstance(data.get("tasks"), list):
                    self.fail(f"a structured task ledger exists at {candidate}")
        # The digest ledger is append-only evidence, never re-written.
        ledger = ws.root / STATE_DIR / "state-digest-ledger.jsonl"
        before = ledger.read_bytes()
        with open(ledger, "ab") as stream:
            stream.write(b"")
        self.assertEqual(ledger.read_bytes(), before)

    # -- case 5: findings reach the next developer only through a revised plan

    def test_case_05_findings_reach_next_developer_via_revised_plan(self) -> None:
        # Positive: a findings-revised planner revises the canonical plan from
        # the exact receipt-backed payload, and the round-2 developer then
        # works only the exact revised selected task (never findings).
        ws = self.make({
            "planner": {"behavior": {"1": "planned", "2": "findings-revised",
                                     "default": "planned"}},
            "developer": {"behavior": "complete"},
            "tester": {"behavior": {"1": "findings", "2": "pass",
                                    "default": "pass"}},
            "auditor": {"behavior": {"1": "findings", "2": "pass",
                                     "default": "pass"}},
        }, rounds=2, workspace_cls=FindingsWorkspace)
        rc, data = ws.run_cli()
        self.assertEqual(rc, 0, "the findings-revised round must complete")
        assert_terminal(self, data, terminal_phase="success",
                        terminal_outcome="pass", exit_code=0, rounds_completed=2)
        rounds = [(r["round"], r["phase"], r["outcome"])
                  for r in data["phase_history"]]
        self.assertEqual(
            rounds,
            [(1, "planning", "planned"),
             (1, "implementation", "task_completed"),
             (1, "verification", "findings"),
             (1, "audit", "findings"),
             (2, "planning", "planned"),
             (2, "implementation", "task_completed"),
             (2, "verification", "pass"),
             (2, "audit", "pass")],
        )
        # The write-once exact-commit receipts are the only evidence the
        # payload was derived from.
        receipts = sorted(
            (ws.root / STATE_DIR).glob("factory-findings-receipt-*.json"))
        self.assertEqual(len(receipts), 2,
                         "the round-1 verification/audit receipts must be "
                         "preserved")
        # The real authority re-derives the exact canonical payload the
        # round-2 planner consumed: same receipts, same recorded phase
        # records, and the real reachability at the round-2 planning head
        # (the commit the planner revised and the developer then worked).
        planning2 = next(
            r for r in data["phase_history"]
            if r["round"] == 2 and r["phase"] == "planning"
        )
        records = tuple(
            types.SimpleNamespace(**dict(record))
            for record in data["phase_history"]
        )
        canonical = FACTORY_FINDINGS.findings_module.consume_next_round_findings(
            ws.root,
            campaign_id="campaign",
            source_round=1,
            head=planning2["head_commit"],
            is_ancestor=lambda a, b: run(
                [GIT, "-C", str(ws.root), "merge-base", "--is-ancestor", a, b],
                check=False,
            ).returncode == 0,
            phase_records=records,
        )
        self.assertIsNotNone(canonical,
                             "the round-2 planner must have received a payload")
        payload = json.loads(canonical.decode("utf-8"))
        self.assertEqual(payload["schema"], "factory-findings/v1")
        self.assertEqual(payload["source_round"], 1)
        self.assertEqual(
            {entry["phase"] for entry in payload["entries"]},
            {"verification", "audit"},
            "both findings phases reach the next planner",
        )
        on_disk = {
            phase: json.loads(
                (ws.root / STATE_DIR /
                 f"factory-findings-receipt-round-1-{phase}.json"
                 ).read_bytes())
            for phase in ("verification", "audit")
        }
        for entry in payload["entries"]:
            receipt = on_disk[entry["phase"]]
            self.assertEqual(entry["phase_base_commit"],
                             receipt["phase_base_commit"])
            self.assertEqual(entry["result_digest"], receipt["result_digest"])
            self.assertEqual(
                entry["receipt_path"],
                f"factory-findings-receipt-round-1-{entry['phase']}.json")
            self.assertEqual(
                entry["receipt_digest"],
                sha256((ws.root / STATE_DIR /
                        entry["receipt_path"]).read_bytes()))
            self.assertEqual(
                entry["findings"],
                ["fixture finding"] if entry["phase"] == "verification"
                else ["fixture audit finding"])
        # The findings channel is planner-only and the exact payload bytes
        # are delivered verbatim with their digest (the fixture seam mirrors
        # the production launch's digest-bound prompt section), so a passing
        # findings-revised planner proves it worked the exact canonical
        # bytes above — never a paraphrase or subset.
        campaign_source = (LOOP / "campaign.py").read_text(encoding="utf-8")
        self.assertIn('if role == "planner" and findings_payload is not None:',
                      campaign_source)
        self.assertIn('env[CAMPAIGN_ENV_PREFIX + "FINDINGS"]',
                      campaign_source)
        self.assertIn('env[CAMPAIGN_ENV_PREFIX + "FINDINGS_DIGEST"]',
                      campaign_source)
        driver_source = (FIXTURES / "campaign_driver.py").read_text(encoding="utf-8")
        self.assertIn("findings-revised", driver_source)
        self.assertIn("FINDINGS_DIGEST", driver_source,
                      "the driver must verify the exact payload digest")
        self.assertIn("payload digest mismatch", driver_source)
        # The revised round-2 plan incorporates the finding before the
        # developer worked task 2.
        revised_plan = _git(
            ws.root, "show",
            f"{planning2['head_commit']}:{PLAN_REL}").stdout.encode("utf-8")
        plan = plan_parser.Plan.from_bytes(revised_plan)
        task2 = next(t for t in plan.tasks if t.number == 2)
        self.assertEqual(task2.status, "pending")
        self.assertIn(FindingsWorkspace.FINDING_MARKER,
                      task2.fields["Scope"])
        # The round-2 developer worked exactly the revised selected task:
        # its fixture evidence records the exact task-excerpt digest (the
        # real launch authority's derivation, re-derived here from the
        # committed revised plan), the digest of the plan it worked, and the
        # absence of any findings channel — verified by the trusted suite,
        # never asserted by the model.
        evidence_path = (ws.root / "src" / ".factory-test-output" /
                         "developer-evidence-round-2.json")
        self.assertTrue(evidence_path.is_file(),
                        "the round-2 developer evidence must be preserved")
        evidence = json.loads(evidence_path.read_bytes())
        self.assertEqual(evidence["schema"],
                         "factory-driver-developer-evidence/v1")
        self.assertEqual(evidence["round"], 2)
        self.assertEqual(evidence["task_id"], "2")
        self.assertEqual(
            evidence["task_excerpt_digest"],
            launch_module.task_excerpt_digest(revised_plan, 2))
        self.assertEqual(evidence["plan_digest"], sha256(revised_plan))
        self.assertFalse(evidence["findings_present"],
                         "the developer must never receive the findings payload")
        # No test instrumentation becomes model-visible authority: the
        # fixture evidence lives under the fixture-only
        # ``src/.factory-test-output/`` namespace with a fixture schema and
        # never enters the trusted harness surface (``.factory/``,
        # ``.factory-state/``); no production authority reads it.
        self.assertFalse(list((ws.root / ".factory").glob("*evidence*")))
        self.assertFalse(list((ws.root / STATE_DIR).glob("*evidence*")))
        for authority in ("campaign.py", "launch.py", "findings.py",
                          "selector.py", "state.py"):
            self.assertNotIn(
                ".factory-test-output",
                (LOOP / authority).read_text(encoding="utf-8"),
                f"{authority} must never read fixture evidence")
        # The production ``_run_driver`` task-excerpt digest env mirrors the
        # launch child environment (the same real derivation from the exact
        # committed plan blob at the phase head) and remains a test seam:
        # only the fixture driver channel carries the campaign prefix.
        self.assertIn("launch_module.task_excerpt_digest", campaign_source)
        self.assertIn("self._git.blob_at(head, config.plan_path)",
                      campaign_source)
        self.assertIn('if role == "developer" and task_id is not None:',
                      campaign_source)
        launch_source = (LOOP / "launch.py").read_text(encoding="utf-8")
        self.assertIn('prefix + "TASK_EXCERPT_DIGEST"', launch_source)
        self.assertNotIn("CAMPAIGN_", launch_source,
                         "the production launch never carries the campaign "
                         "driver seam")

        # -- negatives -----------------------------------------------------
        # A generic ``planned`` round-2 planner cannot satisfy the semantic
        # assertions: the campaign still completes, but the revised plan
        # never carries the finding and the developer evidence binds a plan
        # without it — proving the semantic assertions (not the campaign
        # pass itself) enforce the findings channel.
        generic = self.make({
            "planner": {"behavior": {"1": "planned", "2": "planned",
                                     "default": "planned"}},
            "developer": {"behavior": "complete"},
            "tester": {"behavior": {"1": "findings", "2": "pass",
                                    "default": "pass"}},
            "auditor": {"behavior": {"1": "findings", "2": "pass",
                                     "default": "pass"}},
        }, rounds=2, workspace_cls=FindingsWorkspace)
        rc, gdata = generic.run_cli()
        self.assertEqual(rc, 0)
        g_planning2 = next(
            r for r in gdata["phase_history"]
            if r["round"] == 2 and r["phase"] == "planning"
        )
        g_plan = _git(
            generic.root, "show",
            f"{g_planning2['head_commit']}:{PLAN_REL}").stdout.encode("utf-8")
        g_task2 = next(
            t for t in plan_parser.Plan.from_bytes(g_plan).tasks
            if t.number == 2)
        self.assertNotIn(FindingsWorkspace.FINDING_MARKER,
                         g_task2.fields["Scope"],
                         "a generic planner never consumes the findings")
        g_evidence = json.loads(
            (generic.root / "src" / ".factory-test-output" /
             "developer-evidence-round-2.json").read_bytes())
        self.assertEqual(
            g_evidence["plan_digest"],
            sha256(g_plan),
            "the generic developer evidence binds the unrevised plan")
        # A missing planner payload fails closed at the driver seam: the
        # committed driver is spawned exactly like the campaign spawns it
        # (same environment shape), with no findings channel.
        neg_scenario = {
            "planner": {"behavior": {"2": "findings-revised",
                                     "default": "planned"}},
            "developer": {"behavior": "complete"},
            "tester": {"behavior": "pass"},
            "auditor": {"behavior": "pass"},
        }
        (ws.root / "scenario-neg.json").write_text(
            json.dumps(neg_scenario), encoding="utf-8")
        driver = str(ws.root / DRIVER_REL)
        base_env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "FACTORY_LOOP_CAMPAIGN_ROOT": str(ws.root),
            "FACTORY_LOOP_CAMPAIGN_PLAN": PLAN_REL,
            "FACTORY_LOOP_CAMPAIGN_ROLE": "planner",
            "FACTORY_LOOP_CAMPAIGN_ROUND": "2",
            "FACTORY_LOOP_CAMPAIGN_ATTEMPT": "1",
            "FACTORY_LOOP_CAMPAIGN_TASK_ID": "",
            "FACTORY_LOOP_CAMPAIGN_SCENARIO": "scenario-neg.json",
            "FACTORY_LOOP_CAMPAIGN_RESULT_FILE": "",
        }
        missing = run([PY, driver, "planner"], env=base_env, check=False)
        self.assertNotEqual(missing.returncode, 0,
                            "a findings-revised planner without the payload "
                            "must fail closed")
        self.assertIn("requires the receipt-backed findings payload",
                      missing.stderr)
        # A tampered planner payload fails closed: bytes whose digest does
        # not match the delivered digest are refused even when the payload
        # is structurally valid (the exact-bytes boundary), and a
        # structurally malformed payload is refused too.
        tampered = json.dumps({
            "schema": "factory-findings/v1",
            "campaign_id": "campaign",
            "source_round": 1,
            "entries": [{
                "phase": "verification",
                "phase_base_commit": "0" * 40,
                "outcome": "findings",
                "phase_tag": "r1.verification.1.a1",
                "result_digest": "0" * 64,
                "receipt_path": "factory-findings-receipt-round-1-verification.json",
                "receipt_digest": "0" * 64,
                "findings": ["tampered finding"],
                "blocked_on": [],
                "gate_ran": True,
                "gate_exit": 0,
                "capability_ran": False,
                "capability_exit": None,
            }],
        }, sort_keys=True, separators=(",", ":"))
        digest_env = dict(base_env)
        digest_env["FACTORY_LOOP_CAMPAIGN_FINDINGS"] = tampered
        digest_env["FACTORY_LOOP_CAMPAIGN_FINDINGS_DIGEST"] = sha256(canonical)
        digest_mismatch = run([PY, driver, "planner"], env=digest_env, check=False)
        self.assertNotEqual(digest_mismatch.returncode, 0,
                            "a payload without the exact delivered digest "
                            "must fail closed")
        self.assertIn("payload digest mismatch", digest_mismatch.stderr)
        malformed_payload = json.dumps({
            "schema": "not-factory-findings",
            "campaign_id": "campaign",
            "source_round": 1,
            "entries": [],
        }, sort_keys=True, separators=(",", ":"))
        malformed_env = dict(base_env)
        malformed_env["FACTORY_LOOP_CAMPAIGN_FINDINGS"] = malformed_payload
        malformed_env["FACTORY_LOOP_CAMPAIGN_FINDINGS_DIGEST"] = sha256(
            malformed_payload.encode("utf-8"))
        malformed = run([PY, driver, "planner"], env=malformed_env, check=False)
        self.assertNotEqual(malformed.returncode, 0,
                            "a structurally malformed payload must fail closed")
        self.assertIn("has the wrong schema", malformed.stderr)

    # -- case 6: empty runnable work reaches verification and audit ----------

    def test_case_06_empty_work_reaches_verification_and_audit(self) -> None:
        ws = self.make({
            "planner": {"behavior": "planned-complete"},
            "developer": {"behavior": "complete"},
            "tester": {"behavior": "pass"},
            "auditor": {"behavior": "pass"},
        })
        rc, data = ws.run_cli()
        self.assertEqual(rc, 0)
        assert_terminal(self, data, terminal_phase="success",
                        terminal_outcome="pass", exit_code=0, rounds_completed=1)
        assert_history(self, data, [
            (1, "planning", "planned"),
            (1, "implementation", "work_exhausted"),
            (1, "verification", "pass"),
            (1, "audit", "pass"),
        ])
        # No no-task spin: exactly one implementation record exists.
        implementation = [r for r in data["phase_history"]
                          if r["phase"] == "implementation"]
        self.assertEqual(len(implementation), 1)

    # -- case 7: external blockers terminate without evidence elevation ------

    def test_case_07_external_blockers_terminate_without_elevation(self) -> None:
        ws = self.make({
            "planner": {"behavior": "planned"},
            "developer": {"behavior": "complete"},
            "tester": {"behavior": "blocked"},
            "auditor": {"behavior": "blocked"},
        })
        rc, data = ws.run_cli()
        self.assertEqual(rc, 2, "a final-round external blocker must end nonzero")
        assert_terminal(self, data, terminal_phase="blocked",
                        terminal_outcome="blocked", exit_code=2,
                        rounds_completed=1)
        state = ws.load_state()
        self.assertEqual(state.current_phase, "blocked")
        self.assertEqual(state.last_outcome, "blocked")
        # No evidence elevation: the preserved findings artifacts (the exact
        # structured phase-result bytes the orchestrator read and preserved
        # before minting each receipt) recorded blocked references, never a
        # verified claim.  The transient handoff file itself is consumed and
        # removed by the authority; the preserved round artifact is evidence.
        preserved = (ws.root / STATE_DIR /
                     "factory-phase-result-round-1-verification.json")
        self.assertTrue(preserved.is_file(),
                        "the preserved verification phase result must exist")
        with open(preserved, encoding="utf-8") as stream:
            result = json.load(stream)
        self.assertEqual(result["outcome"], "blocked")
        self.assertEqual(result["blocked_on"], ["external-capability-required"])
        self.assertFalse((ws.root / STATE_DIR / "phase-result.json").exists(),
                         "the transient result handoff must be consumed")
        # The campaign result never claims success or pass.
        self.assertEqual(data["terminal_outcome"], "blocked")

    # -- case 8: pass is impossible with unresolved mandatory findings -------

    def test_case_08_pass_impossible_with_unresolved_findings(self) -> None:
        ws = self.make({
            "planner": {"behavior": "planned"},
            "developer": {"behavior": "complete"},
            "tester": {"behavior": "pass"},
            "auditor": {"behavior": "findings"},
        })
        rc, data = ws.run_cli()
        self.assertEqual(rc, 1, "unresolved audit findings must end nonzero")
        assert_terminal(self, data, terminal_phase="findings",
                        terminal_outcome="findings", exit_code=1,
                        rounds_completed=1)
        # Even a fully completed plan cannot pass while the audit findings are
        # unresolved: the terminal is findings, never success.
        self.assertNotEqual(data["terminal_outcome"], "pass")
        state = ws.load_state()
        self.assertEqual(state.current_phase, "findings")
        # A repeated identical run must not flip to success: the terminal
        # state refuses to re-run (operator resolution, never re-execution).
        second = run(
            [PY, str(LOOP / "campaign.py"), "--root", str(ws.root), "run",
             "--campaign-id", "campaign", "--rounds", "1", "--branch", BRANCH,
             "--role-driver", DRIVER_REL, "--scenario", "scenario.json",
             "--verification-command", str(TRUE_EXECUTABLE)],
            check=False,
        )
        self.assertNotEqual(second.returncode, 0,
                            "a terminal state must not be re-runnable")

    # -- case 9: one-writer locking under concurrency ------------------------

    def test_case_09_one_writer_lock_concurrency(self) -> None:
        root = Path(tempfile.mkdtemp(prefix="adversarial-lock.", dir=self.tmp))
        _git(root, "init", "-q", "-b", BRANCH)
        _git(root, "config", "user.email", "factory@test")
        _git(root, "config", "user.name", "factory")
        (root / "docs").mkdir()
        (root / "docs" / "SPEC.md").write_text("spec\n", encoding="utf-8")
        (root / ".factory").mkdir()
        (root / ".factory" / "artifacts").mkdir()
        (root / ".factory" / "artifacts" / "implementation-plan.md").write_text(
            "plan\n", encoding="utf-8")
        _git(root, "add", "-A")
        _git(root, "commit", "-qm", "base")
        head = _git(root, "rev-parse", "HEAD").stdout.strip()
        spec_blob = _git(root, "rev-parse", "HEAD:docs/SPEC.md").stdout.strip()
        identity = state_module.repository_identity(root)
        binding = (
            f"expected_identity={identity!r}, "
            f"expected_branch={BRANCH!r}, "
            f"spec=lock_module.SpecBinding(path='docs/SPEC.md', "
            f"commit={head!r}, blob={spec_blob!r}), "
            f"plan=lock_module.PlanBinding("
            f"path='.factory/artifacts/implementation-plan.md', "
            f"base_commit={head!r}, digest={sha256((root / '.factory' / 'artifacts' / 'implementation-plan.md').read_bytes())!r})"
        )
        holder = (
            "import sys, time\n"
            f"sys.path.insert(0, {str(LOOP)!r})\n"
            "import lock as lock_module\n"
            "with lock_module.acquire_root_lock(sys.argv[1], %s):\n"
            "    time.sleep(3)\n"
            "    sys.exit(0)\n" % binding
        )
        holder_proc = subprocess.Popen(
            [PY, "-c", holder, str(root)], stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self.addCleanup(holder_proc.kill)
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            try:
                probing = lock_module.probe_root_lock(root)
            except lock_module.RootLockError:
                probing = False
            if not probing:
                break
            time.sleep(0.05)
        self.assertFalse(lock_module.probe_root_lock(root),
                         "the holder must own the exclusive lock")
        contender = (
            "import sys\n"
            f"sys.path.insert(0, {str(LOOP)!r})\n"
            "import lock\n"
            "try:\n"
            "    with lock.acquire_root_lock(sys.argv[1], %s):\n"
            "        sys.exit(0)\n"
            "except lock.RootLockHeldError:\n"
            "    sys.exit(1)\n" % binding
        )
        contenders = [
            subprocess.Popen([PY, "-c", contender, str(root)],
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            for _ in range(4)
        ]
        for proc in contenders:
            out, err = proc.communicate(timeout=30)
            self.assertEqual(proc.returncode, 1,
                             "every contender must lose the one-writer race")
        out, err = holder_proc.communicate(timeout=30)
        self.assertEqual(holder_proc.returncode, 0, err.decode()[-1000:])
        self.assertTrue(lock_module.probe_root_lock(root),
                        "release must free the boundary")
        # After release a fresh acquisition succeeds (serialized, not wedged).
        with lock_module.acquire_root_lock(
            root, expected_identity=identity, expected_branch=BRANCH,
            spec=lock_module.SpecBinding(path="docs/SPEC.md", commit=head,
                                         blob=spec_blob),
            plan=lock_module.PlanBinding(
                path=".factory/artifacts/implementation-plan.md",
                base_commit=head,
                digest=sha256((root / ".factory" / "artifacts" / "implementation-plan.md").read_bytes())),
        ):
            pass

    # -- case 10: TERM, INT, and HUP reach and reap the full group -----------

    def test_case_10_signals_reach_and_reap_process_group(self) -> None:
        ws = self.make(SUCCESS_SCENARIO)
        marker_dir = ws.root / "src" / ".factory-test-output"
        backend = ws.root / "backend-signal.py"
        backend.write_text(
            "#!/usr/bin/env python3\n"
            "import json, os, signal, subprocess, sys, time\n"
            f"marker = {str(marker_dir / 'signal.json')!r}\n"
            "for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):\n"
            "    signal.signal(sig, signal.SIG_IGN)\n"
            "grandchild = subprocess.Popen(\n"
            "    [sys.executable, '-c', 'import os,signal,sys,time;'\n"
            "     'signal.signal(signal.SIGTERM, signal.SIG_IGN);'\n"
            "     'signal.signal(signal.SIGINT, signal.SIG_IGN);'\n"
            "     'signal.signal(signal.SIGHUP, signal.SIG_IGN);'\n"
            "     'time.sleep(300)'],\n"
            ")\n"
            "with open(marker, 'w', encoding='utf-8') as stream:\n"
            "    json.dump({'pid': os.getpid(), 'grandchild': grandchild.pid},\n"
            "              stream)\n"
            "sys.stdout.flush()\n"
            "while True:\n"
            "    time.sleep(60)\n",
            encoding="utf-8",
        )
        prepare_launch_workspace(ws, "backend-signal.py")
        for signum in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
            marker = marker_dir / f"signal-{signum}.json"
            backend.write_text(
                "#!/usr/bin/env python3\n"
                "import json, os, signal, subprocess, sys, time\n"
                f"marker = {str(marker)!r}\n"
                "for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):\n"
                "    signal.signal(sig, signal.SIG_IGN)\n"
                "grandchild = subprocess.Popen(\n"
                "    [sys.executable, '-c', 'import os,signal,sys,time;'\n"
                "     'signal.signal(signal.SIGTERM, signal.SIG_IGN);'\n"
                "     'signal.signal(signal.SIGINT, signal.SIG_IGN);'\n"
                "     'signal.signal(signal.SIGHUP, signal.SIG_IGN);'\n"
                "     'time.sleep(300)'],\n"
                ")\n"
                "with open(marker, 'w', encoding='utf-8') as stream:\n"
                "    json.dump({'pid': os.getpid(), 'grandchild': grandchild.pid},\n"
                "              stream)\n"
                "sys.stdout.flush()\n"
                "while True:\n"
                "    time.sleep(60)\n",
                encoding="utf-8",
            )
            _git(ws.root, "add", "-A")
            _git(ws.root, "commit", "-qm", f"signal probe {signum}")
            argv = launch_cli_args(
                ws, backend="backend-signal.py", task_id=1,
                runtime_limit=120.0, inactivity_limit=120.0)
            proc = subprocess.Popen(
                argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, cwd=str(ws.root))
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline and not marker.is_file():
                time.sleep(0.05)
            self.assertTrue(marker.is_file(), "the signal probe must have started")
            with open(marker, encoding="utf-8") as stream:
                pids = json.load(stream)
            proc.send_signal(signum)
            out, err = proc.communicate(timeout=60)
            self.assertEqual(proc.returncode, 128 + signum,
                             f"{signal.Signals(signum).name}: expected 128+{signum}, "
                             f"got {proc.returncode} (stderr: {err[-1000:]})")
            # The interrupted launch still prints the machine result (the
            # committed factory-launch-result/v1 payload) with a fully
            # reaped group.
            result = json.loads(out.splitlines()[-1])
            result_schema = json.loads(
                (SCHEMAS / "factory-launch-result-v1.schema.json").read_text(
                    encoding="utf-8"))
            for required_key in result_schema["required"]:
                self.assertIn(required_key, result,
                              f"the machine result must carry {required_key}")
            self.assertEqual(result["live_descendants"], 0,
                             "the full group must be reaped after the signal")
            self.assertIn(signal.Signals(signum).name, result["terminated_by"])
            for pid in (pids["pid"], pids["grandchild"]):
                try:
                    os.kill(pid, 0)
                    self.fail(f"pid {pid} survived {signal.Signals(signum).name}")
                except ProcessLookupError:
                    pass

    # -- case 11: timeout and crash recovery preserve dirty work -------------

    def test_case_11_timeout_crash_preserve_dirty_work(self) -> None:
        # Crash path through the campaign: the developer SIGKILLs itself after
        # touching dirty work; the campaign terminates interrupted and the
        # dirty file survives byte-identically.
        ws = self.make({
            "planner": {"behavior": "planned"},
            "developer": {"behavior": "crash"},
            "tester": {"behavior": "pass"},
            "auditor": {"behavior": "pass"},
        })
        rc, data = ws.run_cli()
        self.assertEqual(rc, 4, "a dirty crash must terminate interrupted")
        assert_terminal(self, data, terminal_phase="interrupted",
                        terminal_outcome="interrupted", exit_code=4,
                        rounds_completed=0)
        dirty = ws.root / "src" / "work-1.md"
        self.assertTrue(dirty.is_file(), "the crash-touched dirty work must survive")
        # The fixture's crash behavior appends one crash-attempt marker per
        # developer attempt; the file must survive byte-identically with the
        # first attempt's marker (proving the dirty work was preserved).
        self.assertIn("crash-attempt-1\n", dirty.read_text(encoding="utf-8"))
        # Timeout path through the real launch authority: a sleeping backend
        # with a short runtime limit is bounded-terminated and the work it
        # touched survives.
        ws2 = self.make(SUCCESS_SCENARIO)
        backend = ws2.root / "backend-timeout.py"
        backend.write_text(
            "#!/usr/bin/env python3\n"
            "import time\n"
            "with open('src/.factory-test-output/timeout-work.txt', 'w', "
            "encoding='utf-8') as stream:\n"
            "    stream.write('dirty timeout work\\n')\n"
            "while True:\n"
            "    time.sleep(60)\n",
            encoding="utf-8",
        )
        prepare_launch_workspace(ws2, "backend-timeout.py")
        argv = launch_cli_args(ws2, backend="backend-timeout.py", task_id=1,
                               runtime_limit=2.0, inactivity_limit=2.0)
        result = run_launch(ws2, *argv)
        self.assertNotEqual(result.returncode, 0,
                            "a timed-out attempt must not report success")
        result_json = json.loads(result.stdout.splitlines()[-1])
        self.assertNotEqual(result_json["outcome"], "completed")
        self.assertEqual(result_json["live_descendants"], 0,
                         "the timed-out group must be fully reaped")
        work = ws2.root / "src" / ".factory-test-output" / "timeout-work.txt"
        self.assertTrue(work.is_file(), "the timeout dirty work must survive")
        self.assertEqual(work.read_text(encoding="utf-8"), "dirty timeout work\n")

    # -- case 12: tampered state fails closed ---------------------------------

    def test_case_12_tampered_state_fails_closed(self) -> None:
        root = Path(tempfile.mkdtemp(prefix="adversarial-state.", dir=self.tmp))
        _git(root, "init", "-q", "-b", BRANCH)
        _git(root, "config", "user.email", "factory@test")
        _git(root, "config", "user.name", "factory")
        (root / "docs").mkdir()
        (root / "docs" / "SPEC.md").write_text("spec\n", encoding="utf-8")
        _git(root, "add", "-A")
        _git(root, "commit", "-qm", "base")
        head = _git(root, "rev-parse", "HEAD").stdout.strip()
        digest = "a" * 64
        init = self.state_cli(
            root, "init",
            "--campaign-id", "adversarial-campaign",
            "--rounds", "2",
            "--base-commit", head,
            "--spec-digest", digest,
            "--plan-digest", digest,
            "--audit-digest", digest,
            "--role-digest", "planner=" + digest,
            "--role-digest", "developer=" + digest,
            "--branch", BRANCH,
        )
        self.assertEqual(init.returncode, 0, init.stderr[-1000:])
        state_file = root / STATE_DIR / STATE_FILE
        self.assertTrue(state_file.is_file())
        valid = self.state_cli(root, "show")
        self.assertEqual(valid.returncode, 0, valid.stderr[-1000:])
        json.loads(valid.stdout)

        def assert_fails(label: str) -> None:
            proc = self.state_cli(root, "show")
            self.assertNotEqual(proc.returncode, 0,
                                f"{label}: the tampered state must fail closed")
            self.assertTrue(proc.stderr.strip(),
                            f"{label}: the failure must name a reason")

        original = state_file.read_bytes()

        # Symlinked state: the no-follow reader rejects the link.
        os.rename(state_file, state_file.with_name("factory-loop.json.real"))
        state_file.symlink_to(state_file.with_name("factory-loop.json.real"))
        assert_fails("symlinked")
        state_file.unlink()
        os.rename(state_file.with_name("factory-loop.json.real"), state_file)

        # Wrong mode: the exact 0600 ownership check rejects a group-writable
        # state file.
        os.chmod(state_file, 0o666)
        assert_fails("wrong-mode")
        os.chmod(state_file, 0o600)

        # Oversized: beyond the bounded read budget.
        with open(state_file, "wb") as stream:
            stream.write(b" " * (4 * 1024 * 1024 + 1))
        assert_fails("oversized")
        state_file.write_bytes(original)
        os.chmod(state_file, 0o600)

        # Forged: a syntactically valid but wrong-schema state.
        state_file.write_text(json.dumps({"schema": "not-factory-state/v1"}),
                              encoding="utf-8")
        os.chmod(state_file, 0o600)
        assert_fails("forged")
        state_file.write_bytes(original)
        os.chmod(state_file, 0o600)

        # Wrong owner: the exact owner check compares real stat metadata
        # against the expected uid (the internal ``_expected_uid`` hook is the
        # only deterministic always-runnable way to exercise the branch with
        # real stat metadata and without requiring chown).
        with self.assertRaises(state_module.StateTamperError):
            state_module.load_state(root, _expected_uid=os.getuid() + 1)

        # Mismatched: a state whose repository identity does not match the
        # canonical root fails closed (a state from another checkout).
        proc = self.state_cli(root, "show")
        self.assertEqual(proc.returncode, 0)
        state = json.loads(proc.stdout)
        state["repository_identity"] = "0:0"
        state_file.write_text(
            json.dumps(state, sort_keys=True, separators=(",", ":")),
            encoding="utf-8")
        os.chmod(state_file, 0o600)
        mismatched = self.state_cli(root, "show")
        self.assertNotEqual(mismatched.returncode, 0,
                            "a foreign repository identity must fail closed")
        self.assertIn("repository_identity", mismatched.stderr)
        state_file.write_bytes(original)
        os.chmod(state_file, 0o600)

    # -- case 13: canonical per-invocation Ollama decision table -------------

    def test_case_13_ollama_check_wait_before_invocation(self) -> None:
        # The guard exit table (real CLI, fresh subprocesses) is the
        # pre-invocation gate: only exit 0 allows a launch.
        ok = self.guard_cli("--check", "--json",
                            "--html-file", str(VISIBLE_FIXTURES / "usage-ok.html"))
        self.assertEqual(ok.returncode, usage_module.EXIT_ALLOWED, ok.stderr)
        payload = json.loads(ok.stdout)
        self.assertFalse(payload["blocked"])
        blocked = self.guard_cli("--check", "--json",
                                 "--html-file",
                                 str(VISIBLE_FIXTURES / "usage-blocked.html"))
        self.assertEqual(blocked.returncode, usage_module.EXIT_BLOCKED, blocked.stderr)
        self.assertTrue(json.loads(blocked.stdout)["blocked"])
        fatal = self.guard_cli("--check",
                               "--html-file", str(VISIBLE_FIXTURES / "login.html"))
        self.assertEqual(fatal.returncode, usage_module.EXIT_FATAL, fatal.stderr)
        # --wait polls until the quota clears or exhausts its bounds.
        server = _ScriptedServer([
            (200, (VISIBLE_FIXTURES / "usage-blocked.html").read_bytes()),
            (200, (VISIBLE_FIXTURES / "usage-ok.html").read_bytes()),
        ])
        self.addCleanup(server.close)
        waited = self.guard_cli(
            "--wait", "--settings-url", f"http://127.0.0.1:{server.port}/",
            "--cookie-stdin", "--poll-interval", "1", "--max-polls", "3",
            input_bytes=SYNTH_COOKIE.encode(),
        )
        self.assertEqual(waited.returncode, usage_module.EXIT_ALLOWED, waited.stderr)
        # Quota remains parent-side and exposes no model-side option. The
        # pre-round registry is independent: it cannot substitute a once-per-
        # round check for the mandatory check before every invocation.
        ws = self.make(SUCCESS_SCENARIO)
        backend = ws.root / "backend-ollama.py"
        backend.write_text("#!/usr/bin/env python3\nprint('ok')\n",
                           encoding="utf-8")
        prepare_launch_workspace(ws, "backend-ollama.py")
        head = _git(ws.root, "rev-parse", "HEAD").stdout.strip()
        binding = launch_module.InvocationBinding(
            role="planner", model="synthetic-model", provider="ollama",
            backend=backend, workspace=ws.root, bound_commit=head,
            role_prompt_digest=sha256(
                (ws.root / ".factory" / "prompts" / "planner.md").read_bytes()),
            prompt_set_digest=prompt_set_digest(ws.root),
            plan_digest=sha256((ws.root / PLAN_REL).read_bytes()),
            policy_digest=sha256((ws.root / "AGENTS.md").read_bytes()),
            specification_digest=sha256(
                (ws.root / "docs" / "SPEC.md").read_bytes()),
            allowed_tools=("read", "bash"),
        )
        import inspect
        self.assertNotIn(
            "usage_guard_cookie_file",
            inspect.signature(launch_module.authorize_launch).parameters,
        )
        self.assertFalse(hasattr(launch_module, "usage_guard"))
        with self.assertRaises(launch_module.InvocationError) as denied:
            launch_module.authorize_launch(
                binding,
                role_prompt=(ws.root / ".factory" / "prompts" /
                             "planner.md").read_bytes(),
                agents=(ws.root / "AGENTS.md").read_bytes(),
                spec=(ws.root / "docs" / "SPEC.md").read_bytes(),
                plan=(ws.root / PLAN_REL).read_bytes(),
            )
        self.assertIn("locked readiness store", str(denied.exception))
        registry = json.loads((ws.root / ".factory/pre-round-hooks.json").read_text())
        enabled = {item["id"]: item["enabled"] for item in registry["hooks"]}
        self.assertEqual(enabled, {"branch-guard": True})
        campaign_source=(ROOT/".factory/loop/campaign.py").read_text()
        self.assertIn("usage_module.require_quota", campaign_source)
        self.assertIn("final_rc", (ROOT/".factory/loop/usage.py").read_text())

    # -- case 14: credential tool-call blocking and redaction stay active ----

    def test_case_14_credential_blocking_and_redaction(self) -> None:
        import redaction as redaction_module
        # Tool-result redaction through the exact committed credential guard:
        # a fake credential-shaped secret in a gate/role output is masked
        # before it can enter a result, log, receipt, or repository state.
        secret = "FAKE_TOOLCALL_SECRET_7f3a"
        guard_src = (ROOT / ".factory" / "tools" / "credential-guard.py").read_bytes()
        redactor = redaction_module.redactor_from_bytes(
            guard_src, guard_src, "0" * 40)
        out = redactor.redact_text(
            f"tool result: Authorization: {secret} recorded")
        self.assertNotIn(secret, out,
                         "a credential-shaped tool result must be masked")
        self.assertIn("[REDACTED]", out)
        # The guard itself (the tool-call blocking boundary) fails closed on
        # a credential-bearing shell command through the real CLI.
        guard_proc = run(
            [PY, str(ROOT / ".factory" / "tools" / "credential-guard.py"),
             "check-command", "--command", 'echo "$OLLAMA_COOKIE"'],
            check=False,
        )
        self.assertNotEqual(guard_proc.returncode, 0,
                            "the credential guard must block a secret-shaped command")
        self.assertIn('"verdict":"block"', guard_proc.stdout.replace(" ", ""),
                      "the guard classification must name the block verdict")
        # An ordinary command stays allowed (the guard is a classifier, never
        # a blanket rejector).
        allowed_proc = run(
            [PY, str(ROOT / ".factory" / "tools" / "credential-guard.py"),
             "check-command", "--command", "echo hello"],
            check=False,
        )
        self.assertEqual(allowed_proc.returncode, 0,
                         "an ordinary command must stay allowed")
        # The campaign's gate-output redaction is bound to the committed
        # guard: a missing guard source fails closed (CRED-01 never weakens).
        self.assertTrue((ROOT / ".factory" / "tools" / "credential-guard.py").is_file())

    # -- case 15: exact-commit runner, visual, installed, and audit receipts --

    def test_case_15_exact_commit_receipts_retain_trust(self) -> None:
        # §22 test 15: the exact-commit *runner*, *visual*, *installed*, and
        # *audit* receipts retain their existing trust semantics.  One
        # commit-bound fixture repository exercises every channel through the
        # real retained validators (``machine-receipt.py`` /
        # ``check-audit-receipts.py`` / ``check-factory-runner-evidence.py``
        # / ``visual-audit-provenance.py`` /
        # ``check-installed-functional-evidence.sh``) with tamper negatives.
        # The ephemeral ed25519 runner key and every artifact are
        # test-owned fixture state; no fake receipt is ever claimed as
        # passing evidence and no external runner/hardware is used.
        root = Path(tempfile.mkdtemp(prefix="adversarial-receipt.", dir=self.tmp))
        (root / ".factory").mkdir()
        (root / ".factory" / "tools").mkdir()
        (root / ".factory" / "artifacts").mkdir()
        (root / STATE_DIR).mkdir()
        os.chmod(root / STATE_DIR, 0o700)
        (root / ".factory" / "artifacts" / "campaign-audit.md").write_text(
            "# Audit\n", encoding="utf-8")
        for script in (
            "machine-receipt.py", "check-audit-receipts.py",
            "check-factory-runner-evidence.py", "check-factory-environment.py",
            "visual-audit-provenance.py", "check-installed-functional-evidence.sh",
        ):
            shutil.copy2(ROOT / ".factory" / "tools" / script, root / ".factory" / "tools" / script)
        # Committed runner declaration (validated by the retained
        # check-factory-environment policy) plus the enabled signer trust
        # bound to an ephemeral test-owned ed25519 key (never committed).
        (root / ".factory" / "environment.toml").write_text(
            "schema_version = 1\n"
            "[[runners]]\n"
            'name = "fixture-runner"\n'
            'transport = "ssh"\n'
            'ssh_config_alias = "fixture-runner"\n'
            'working_directory = "/srv/dev-runner/workspaces/fixture-project"\n'
            'capabilities = ["project-gate"]\n'
            'verify_argv = ["./.factory/tools/verify-boilerplate.sh"]\n',
            encoding="utf-8")
        (root / ".gitignore").write_text(".factory-state/\n", encoding="utf-8")
        signer_key = self.tmp / "signer-key"
        subprocess.run(
            ["ssh-keygen", "-q", "-t", "ed25519", "-N", "",
             "-f", str(signer_key)],
            check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        public_key = " ".join(
            (Path(str(signer_key) + ".pub").read_text(
                encoding="utf-8").split())[:2])
        key_sha256 = sha256(public_key.encode("utf-8"))
        (root / ".factory" / "signer-trust.json").write_text(
            json.dumps({
                "schema": "ralph-runner-signer-trust/v1",
                "description": "adversarial-suite ephemeral fixture signer",
                "require_signature": True,
                "enabled": True,
                "namespace": "factory-runner-receipt",
                "public_keys": [{"principal": "factory-signer",
                                  "public_key": public_key}],
                "allowed_principals": ["factory-signer"],
            }, sort_keys=True, indent=2) + "\n",
            encoding="utf-8")
        # The generic installed-functional checker needs the hidden evidence
        # authority, the committed receipt policy, and a deterministic stub
        # installed-harness suite (the receipt wrapper executes a clean pass).
        (root / ".factory" / "loop").mkdir()
        (root / ".factory" / "tests").mkdir()
        shutil.copy2(ROOT / ".factory/loop/evidence.py",
                     root / ".factory/loop/evidence.py")
        shutil.copy2(ROOT / ".factory/loop/gitutil.py",
                     root / ".factory/loop/gitutil.py")
        shutil.copy2(ROOT / ".factory/loop/lock.py",
                     root / ".factory/loop/lock.py")
        shutil.copy2(ROOT / ".factory/loop/footprint.py",
                     root / ".factory/loop/footprint.py")
        shutil.copy2(ROOT / ".factory/campaign-receipt-policy.json",
                     root / ".factory/campaign-receipt-policy.json")
        installed_suite = root / ".factory/tests/test-factory-installed.sh"
        installed_suite.write_text(
            "#!/usr/bin/env bash\n"
            "set -euo pipefail\n"
            "echo 'test-factory-installed: all checks passed'\n",
            encoding="utf-8")
        installed_suite.chmod(0o755)
        _git(root, "init", "-q", "-b", BRANCH)
        _git(root, "config", "user.email", "factory@test")
        _git(root, "config", "user.name", "factory")
        _git(root, "add", "-A")
        _git(root, "commit", "-qm", "base")
        head = _git(root, "rev-parse", "HEAD").stdout.strip()
        tree = _git(root, "rev-parse", f"{head}^{{tree}}").stdout.strip()
        runner_evidence = root / STATE_DIR / "runner-evidence"

        def rebuild_runner_evidence() -> Path:
            """Build the exact-commit signed runner-evidence fixture.

            The manifest, aggregate, transcripts, and detached signature are
            built exactly as the strict runner-evidence checker recomputes
            them from the committed tree, so every failure asserted below is
            a signature/trust/structure failure, never a fixture-binding
            mismatch.  Returns the manifest path.
            """
            manifest_dir = runner_evidence / "fixture-runner" / head
            manifest_dir.mkdir(parents=True, exist_ok=True)
            environment_blob = _git(
                root, "rev-parse", f"{head}:.factory/environment.toml"
            ).stdout.strip()
            argv_digest = sha256(json.dumps(
                ["./.factory/tools/verify-boilerplate.sh"],
                separators=(",", ":")).encode("utf-8"))
            archive = root / "commit-archive.tar"
            _git(root, "archive", "--format=tar", "--output",
                 str(archive), head)
            archive_sha256 = sha256(archive.read_bytes())
            archive.unlink()
            empty = sha256(b"")
            manifest = {
                "schema": "factory-runner-receipt/v1", "result": "pass",
                "runner": "fixture-runner", "commit": head, "tree": tree,
                "environment_blob": environment_blob,
                "verify_argv_sha256": argv_digest,
                "archive_sha256": archive_sha256, "nonce": "0" * 64,
                "capabilities": ["project-gate"], "exit_code": 0,
                "timed_out": False, "started_at": 1, "finished_at": 2,
                "cleanup": True, "stdout_sha256": empty,
                "stderr_sha256": empty, "signer_principal": "factory-signer",
                "signer_key_sha256": key_sha256,
                "namespace": "factory-runner-receipt",
                "signature_algorithm": "ssh-ed25519",
            }
            raw = (json.dumps(manifest, sort_keys=True, indent=2) + "\n").encode()
            manifest_path = manifest_dir / "manifest.json"
            manifest_path.write_bytes(raw)
            (manifest_dir / "stdout.log").write_bytes(b"")
            (manifest_dir / "stderr.log").write_bytes(b"")
            signed = subprocess.run(
                ["ssh-keygen", "-Y", "sign", "-f", str(signer_key),
                 "-n", "factory-runner-receipt"],
                input=raw, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                check=True,
            )
            (manifest_dir / "manifest.sig").write_bytes(signed.stdout)
            aggregate = {
                "schema": "factory-runner-aggregate/v1", "commit": head,
                "tree": tree, "environment_blob": environment_blob,
                "runners": [{
                    "name": "fixture-runner",
                    "manifest": f".factory-state/runner-evidence/fixture-runner/{head}/manifest.json",
                    "manifest_sha256": sha256(raw),
                    "capabilities": ["project-gate"],
                    "signer": {
                        "principal": "factory-signer",
                        "key_sha256": key_sha256,
                        "algorithm": "ssh-ed25519",
                        "signature_sha256": sha256(signed.stdout),
                    },
                }],
            }
            (root / STATE_DIR / "runner-evidence.json").write_text(
                json.dumps(aggregate, sort_keys=True, indent=2) + "\n",
                encoding="utf-8")
            return manifest_path

        def runner_check(ref: str, commit: str) -> subprocess.CompletedProcess[str]:
            return run(
                [PY, str(root / ".factory" / "tools" / "check-factory-runner-evidence.py"),
                 "--verify-manifest", ref, "--expected-commit", commit],
                root=root, check=False,
            )

        # -- legacy runner receipts are not v3 campaign-scoped evidence -------
        # This fixture intentionally constructs the retired v1 shape. Even a
        # correctly signed local fixture must be rejected: tests never claim
        # live runner evidence or bypass v3 campaign/readiness enrollment.
        manifest_path = rebuild_runner_evidence()
        manifest_ref = manifest_path.relative_to(root).as_posix()
        genuine = runner_check(manifest_ref, head)
        self.assertNotEqual(genuine.returncode, 0,
                            "legacy unnamespaced fixture evidence must fail")
        # Tamper negatives through the same real validator: unsigned,
        # fabricated-signature, stale-commit, and byte-tampered manifests all
        # fail closed; the state is rebuilt (fresh signature) after each.
        unsigned_sig = manifest_path.with_name("manifest.sig")
        unsigned_sig.unlink()
        self.assertNotEqual(
            runner_check(manifest_ref, head).returncode, 0,
            "an unsigned runner manifest must fail the strict helper")
        manifest_path = rebuild_runner_evidence()
        unsigned_sig = manifest_path.with_name("manifest.sig")
        unsigned_sig.write_bytes(b"fabricated-signature-bytes\n")
        aggregate_path = root / STATE_DIR / "runner-evidence.json"
        aggregate = json.loads(aggregate_path.read_text(encoding="utf-8"))
        aggregate["runners"][0]["signer"]["signature_sha256"] = \
            sha256(b"fabricated-signature-bytes\n")
        aggregate_path.write_text(
            json.dumps(aggregate, sort_keys=True, indent=2) + "\n",
            encoding="utf-8")
        self.assertNotEqual(
            runner_check(manifest_ref, head).returncode, 0,
            "a fabricated signature must fail the real ssh-keygen verify")
        manifest_path = rebuild_runner_evidence()
        self.assertNotEqual(
            runner_check(manifest_ref, "0" * 40).returncode, 0,
            "a stale expected commit must fail the runner-evidence helper")
        manifest_path = rebuild_runner_evidence()
        with open(manifest_path, "ab") as stream:
            stream.write(b"tamper\n")
        self.assertNotEqual(
            runner_check(manifest_ref, head).returncode, 0,
            "a byte-tampered manifest must fail the aggregate digest check")
        manifest_path = rebuild_runner_evidence()

        # -- audit receipts: coordinator-bound mint, receipt + manifest refs --
        coordinator = root / STATE_DIR / "audit-coordinator.json"
        coordinator.write_text(json.dumps({
            "schema": "ralph-audit-coordinator/v1",
            "round": 1, "base_commit": head, "nonce": "a" * 64,
            "created_at": 1,
        }), encoding="utf-8")
        os.chmod(coordinator, 0o600)
        minted = run(
            [PY, str(root / ".factory" / "tools" / "machine-receipt.py"), "--root",
             str(root), "--tag", "adversarial.gate",
             "--audit-round", "1", "--evidence-commit", head,
             "--nonce", "a" * 64, "--", str(TRUE_EXECUTABLE)],
            check=False,
        )
        self.assertEqual(minted.returncode, 0, minted.stderr[-1000:])
        receipts = list((root / STATE_DIR / "audit-receipts").glob("*.json"))
        self.assertEqual(len(receipts), 1)
        audit = root / ".factory" / "artifacts" / "campaign-audit.md"
        audit_body = (
            "---\n"
            "schema: ralph-campaign-audit/v1\n"
            f"round: 1\naudit_base_commit: {head}\n"
            f"plan_commit: {head}\n"
            "result: pass\n"
            "---\n"
            "# Audit\n\n## Evidence reviewed\n"
            f"- Executable evidence: `{str(TRUE_EXECUTABLE)}` PASS "
            "[receipt: .factory-state/audit-receipts/adversarial.gate.json]\n"
        )
        audit.write_text(audit_body, encoding="utf-8")
        checked = run(
            [PY, str(root / ".factory" / "tools" / "check-audit-receipts.py"), str(audit),
             "--root", str(root)],
            check=False,
        )
        self.assertEqual(checked.returncode, 0, checked.stderr[-1000:])
        # Audit tamper negatives through the real validator: a flipped exit
        # code, a tampered transcript, a missing receipt reference, and
        # BLOCKED evidence in a pass audit all fail closed.
        receipt = receipts[0]
        original_receipt = receipt.read_bytes()
        original_stdout = (root / STATE_DIR / "audit-receipts" /
                           "adversarial.gate.stdout").read_bytes()
        with open(receipt, encoding="utf-8") as stream:
            data = json.load(stream)
        data["exit_code"] = 0 if data["exit_code"] else 1
        receipt.write_text(
            json.dumps(data, sort_keys=True, separators=(",", ":")),
            encoding="utf-8")
        os.chmod(receipt, 0o600)
        self.assertNotEqual(
            run([PY, str(root / ".factory" / "tools" / "check-audit-receipts.py"),
                 str(audit), "--root", str(root)], check=False).returncode, 0,
            "a tampered receipt record must fail the audit checker")
        receipt.write_bytes(original_receipt)
        os.chmod(receipt, 0o600)
        transcript = root / STATE_DIR / "audit-receipts" / "adversarial.gate.stdout"
        with open(transcript, "ab") as stream:
            stream.write(b"intruder\n")
        self.assertNotEqual(
            run([PY, str(root / ".factory" / "tools" / "check-audit-receipts.py"),
                 str(audit), "--root", str(root)], check=False).returncode, 0,
            "a tampered receipt transcript must fail the audit checker")
        transcript.write_bytes(original_stdout)
        audit.write_text(audit_body.replace(
            "[receipt: .factory-state/audit-receipts/adversarial.gate.json]",
            "[receipt: .factory-state/audit-receipts/ghost.json]"),
            encoding="utf-8")
        self.assertNotEqual(
            run([PY, str(root / ".factory" / "tools" / "check-audit-receipts.py"),
                 str(audit), "--root", str(root)], check=False).returncode, 0,
            "a missing receipt reference must fail the audit checker")
        audit.write_text(
            "---\n"
            "schema: ralph-campaign-audit/v1\n"
            f"round: 1\naudit_base_commit: {head}\n"
            f"plan_commit: {head}\n"
            "result: pass\n"
            "---\n"
            "# Audit\n\n## Evidence reviewed\n"
            "- Executable evidence: `real system probe` BLOCKED "
            "(no real system service available)\n",
            encoding="utf-8")
        self.assertNotEqual(
            run([PY, str(root / ".factory" / "tools" / "check-audit-receipts.py"),
                 str(audit), "--root", str(root)], check=False).returncode, 0,
            "BLOCKED evidence in a pass audit must fail the checker")
        # The shared machine-receipt authority refuses a bare (unauthorized)
        # mint outside a coordinator binding: no fake evidence can be minted.
        scrubbed = {key: value for key, value in os.environ.items()
                    if not key.startswith("FACTORY_CAMPAIGN_AUDIT_")}
        bare = run(
            [PY, str(root / ".factory" / "tools" / "machine-receipt.py"), "--root",
             str(root), "--tag", "bare.tag", "--", str(TRUE_EXECUTABLE)],
            check=False, env=scrubbed,
        )
        self.assertNotEqual(bare.returncode, 0,
                            "a bare receipt mint without the coordinator "
                            "binding must be refused")

        # -- visual receipts: provenance binds the exact commit/tree --------
        captures = root / STATE_DIR / "visual-captures"
        captures.mkdir(mode=0o700, exist_ok=True)
        image = captures / "state-a.png"
        image.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 64)
        made = run(
            [PY, str(root / ".factory" / "tools" / "visual-audit-provenance.py"),
             "manifest", "--out", str(captures),
             "--commit", head, "--tree", tree],
            root=root, check=False,
        )
        self.assertEqual(made.returncode, 0, made.stderr[-1000:])
        provenance = json.loads(
            (captures / "provenance.json").read_text(encoding="utf-8"))
        self.assertEqual(provenance["commit"], head,
                         "the visual provenance must bind the exact commit")
        self.assertEqual(provenance["tree"], tree,
                         "the visual provenance must bind the exact tree")
        self.assertEqual(provenance["schema"], "ralph-visual-audit-provenance/v1")
        verified = run(
            [PY, str(root / ".factory" / "tools" / "visual-audit-provenance.py"),
             "verify", "--out", str(captures)],
            root=root, check=False,
        )
        self.assertEqual(verified.returncode, 0, verified.stderr[-1000:])
        # Visual tamper negatives: an altered capture image and a forged
        # environment binding both fail verification (the binding is
        # re-derived from the committed repository, never trusted from the
        # manifest's embedded fields).
        image.write_bytes(b"tampered-image-bytes")
        self.assertNotEqual(
            run([PY, str(root / ".factory" / "tools" / "visual-audit-provenance.py"),
                 "verify", "--out", str(captures)], root=root,
                check=False).returncode, 0,
            "an altered capture image must fail provenance verification")
        image.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 64)
        provenance["environment_blob"] = "0" * 64
        (captures / "provenance.json").write_text(
            json.dumps(provenance, indent=2, sort_keys=True) + "\n",
            encoding="utf-8")
        os.chmod(captures / "provenance.json", 0o600)
        self.assertNotEqual(
            run([PY, str(root / ".factory" / "tools" / "visual-audit-provenance.py"),
                 "verify", "--out", str(captures)], root=root,
                check=False).returncode, 0,
            "a forged environment binding must fail provenance verification")

        # -- installed receipts: exact-commit generic evidence --------------.
        # The generic installed-functional gate accepts only the exact-HEAD
        # dedicated generic namespace backed by a matching installed-harness
        # machine receipt; the foreign root env is never read.  The receipt
        # is minted through the real wrapper (the stub suite exits 0).  The
        # checker requires an exact clean HEAD, so the modified audit (and
        # the fixture's other tracked state) is committed first; the generic
        # evidence namespace then lives at the **evidence commit** (the
        # exact base at which the receipt was minted, an ancestor of the
        # final HEAD) exactly like the trusted publisher's contract, and the
        # checker scans the children and accepts that ancestor namespace
        # because the implementation/acceptance authority paths are
        # unchanged since the evidence commit.
        _git(root, "add", "-A")
        _git(root, "commit", "-qm", "audit")
        final_head = _git(root, "rev-parse", "HEAD").stdout.strip()
        installed_nonce = sha256(b"adversarial-installed-nonce")
        coordinator = {
            "schema": "ralph-audit-coordinator/v1",
            "round": 1,
            "base_commit": head,
            "nonce": installed_nonce,
            "created_at": 1,
        }
        (root / STATE_DIR / "audit-coordinator.json").write_text(
            json.dumps(coordinator, sort_keys=True) + "\n", encoding="utf-8")
        os.chmod(root / STATE_DIR / "audit-coordinator.json", 0o600)
        installed_receipt = root / STATE_DIR / "audit-receipts" / \
            "installed-harness-smoke.json"
        (root / STATE_DIR / "audit-receipts").mkdir(exist_ok=True)
        minted = run(
            [PY, str(root / ".factory" / "tools" / "machine-receipt.py"),
             "--root", str(root), "--tag", "installed-harness-smoke",
             "--audit-round", "1", "--evidence-commit", head,
             "--nonce", installed_nonce, "--",
             "./.factory/tests/test-factory-installed.sh"],
            root=root, check=False,
        )
        self.assertEqual(minted.returncode, 0, minted.stderr[-1000:])
        installed_stdout = root / STATE_DIR / "audit-receipts" / \
            "installed-harness-smoke.stdout"
        installed_ns = root / STATE_DIR / "generic-evidence" / head
        installed_ns.mkdir(parents=True)
        os.chmod(installed_ns, 0o700)
        installed_record = installed_ns / "installed-functional.json"
        installed_record.write_text(
            json.dumps({
                "schema": "factory-generic-installed-functional/v1",
                "commit": head,
                "test": "test_installed_functional",
                "result": "PASS",
                "skipped": 0,
                "receipt": ".factory-state/audit-receipts/installed-harness-smoke.json",
                "receipt_sha256": sha256(installed_receipt.read_bytes()),
                "suite_stdout_sha256": sha256(installed_stdout.read_bytes()),
                "coordinator_round": 1,
                "coordinator_nonce": installed_nonce,
            }, sort_keys=True, indent=2) + "\n",
            encoding="utf-8")
        os.chmod(installed_record, 0o600)
        installed = run(
            ["bash", str(root / ".factory" / "tools" /
                          "check-installed-functional-evidence.sh")],
            root=root, check=False,
        )
        self.assertEqual(installed.returncode, 0, installed.stderr[-1000:])
        self.assertIn(f"PASS at {head}", installed.stdout,
                      "the installed checker must name the exact tested commit")

        def write_installed(result: str = "PASS", skipped: int = 0) -> None:
            record = json.loads(installed_record.read_text(encoding="utf-8"))
            record["result"] = result
            record["skipped"] = skipped
            installed_record.write_text(
                json.dumps(record, sort_keys=True, indent=2) + "\n",
                encoding="utf-8")
            os.chmod(installed_record, 0o600)

        write_installed(skipped=1)
        self.assertNotEqual(
            run(["bash", str(root / ".factory" / "tools" /
                             "check-installed-functional-evidence.sh")],
                root=root, check=False).returncode, 0,
            "skipped installed evidence must fail closed")
        write_installed(result="FAIL")
        self.assertNotEqual(
            run(["bash", str(root / ".factory" / "tools" /
                             "check-installed-functional-evidence.sh")],
                root=root, check=False).returncode, 0,
            "failed installed evidence must fail closed")
        write_installed()
        record = json.loads(installed_record.read_text(encoding="utf-8"))
        record["commit"] = "1" * 40
        installed_record.write_text(
            json.dumps(record, sort_keys=True, indent=2) + "\n",
            encoding="utf-8")
        os.chmod(installed_record, 0o600)
        self.assertNotEqual(
            run(["bash", str(root / ".factory" / "tools" /
                             "check-installed-functional-evidence.sh")],
                root=root, check=False).returncode, 0,
            "installed evidence bound to a non-ancestor commit must fail closed")
        shutil.rmtree(installed_ns)
        self.assertNotEqual(
            run(["bash", str(root / ".factory" / "tools" /
                             "check-installed-functional-evidence.sh")],
                root=root, check=False).returncode, 0,
            "missing installed evidence must fail closed")

    # -- case 16: no model completion token can bypass deterministic gates ---

    def test_case_16_no_completion_token_bypass(self) -> None:
        # The campaign outcome is decided by the harness gates, never by a
        # token in the model output: a driver that prints the completion token
        # on every phase still reaches the deterministic terminal.
        ws = self.make(SUCCESS_SCENARIO)
        driver = ws.root / "token-driver.py"
        source = (FIXTURES / "campaign_driver.py").read_text(encoding="utf-8")
        source = source.replace(
            "def main() -> int:",
            "def main() -> int:\n"
            "    print('LOOP_COMPLETE', flush=True)\n", 1)
        driver.write_text(source, encoding="utf-8")
        driver.chmod(0o755)
        _git(ws.root, "add", "token-driver.py")
        _git(ws.root, "commit", "-qm", "token-printing driver")
        rc, data = ws.run_cli(extra=["--role-driver", "token-driver.py"])
        self.assertEqual(rc, 0, "the token in the model output must not bypass "
                                "or block the deterministic gates")
        assert_terminal(self, data, terminal_phase="success",
                        terminal_outcome="pass", exit_code=0, rounds_completed=1)

    # -- case 17: finite campaigns terminate within bounds -------------------

    def test_case_17_finite_five_round_campaigns(self) -> None:
        # Five-round success: every runnable task completes and the campaign
        # terminates at round 5 within the configured bound.
        success = self.make(SUCCESS_SCENARIO, workspace_cls=FiveRoundWorkspace,
                            rounds=5)
        rc, data = success.run_cli()
        self.assertEqual(rc, 0)
        assert_terminal(self, data, terminal_phase="success",
                        terminal_outcome="pass", exit_code=0, rounds_completed=5)
        assert_history(self, data, [
            (round_no, phase, outcome)
            for round_no in range(1, 6)
            for phase, outcome in (
                ("planning", "planned"),
                ("implementation", "task_completed"),
                ("verification", "pass"),
                ("audit", "pass"),
            )
        ])
        # Five-round final findings: the last round's audit findings end the
        # campaign in the findings terminal, never a spin.
        findings = self.make({
            "planner": {"behavior": "planned"},
            "developer": {"behavior": "complete"},
            "tester": {"behavior": {"5": "findings", "default": "pass"}},
            "auditor": {"behavior": {"5": "findings", "default": "pass"}},
        }, workspace_cls=FiveRoundWorkspace, rounds=5)
        rc, data = findings.run_cli()
        self.assertEqual(rc, 1)
        assert_terminal(self, data, terminal_phase="findings",
                        terminal_outcome="findings", exit_code=1,
                        rounds_completed=5)
        self.assertEqual(
            [(r["round"], r["phase"], r["outcome"]) for r in data["phase_history"]][-4:],
            [(5, "planning", "planned"),
             (5, "implementation", "task_completed"),
             (5, "verification", "findings"),
             (5, "audit", "findings")],
        )
        # The remaining §17 fixtures terminate within their bounds too:
        # planning failure, clean task failure, dirty interruption,
        # verification blocked, verification infrastructure failure, and
        # audit blocked.  A genuine verification ``blocked`` requires the
        # declared capability probe to fail (a blocked tester claim without
        # a failing probe is classified as findings by the authority); the
        # audit then stays blocked so the campaign terminates blocked.
        fixtures = [
            ({"planner": {"behavior": "no-change"},
              "developer": {"behavior": "complete"},
              "tester": {"behavior": "pass"},
              "auditor": {"behavior": "pass"}},
             3, "failed", "failed", 0, [],
             [(1, "planning", "failed")] * 3),
            ({"planner": {"behavior": "planned"},
              "developer": {"behavior": "exit1"},
              "tester": {"behavior": "pass"},
              "auditor": {"behavior": "pass"}},
             0, "success", "pass", 1, [],
             [(1, "planning", "planned"),
              (1, "implementation", "task_failed"),
              (1, "implementation", "task_failed"),
              (1, "implementation", "task_failed"),
              (1, "verification", "pass"), (1, "audit", "pass")]),
            ({"planner": {"behavior": "planned"},
              "developer": {"behavior": "crash"},
              "tester": {"behavior": "pass"},
              "auditor": {"behavior": "pass"}},
             4, "interrupted", "interrupted", 0, [],
             [(1, "planning", "planned"),
              (1, "implementation", "interrupted"),
              (1, "implementation", "interrupted"),
              (1, "implementation", "interrupted")]),
            ({"planner": {"behavior": "planned"},
              "developer": {"behavior": "complete"},
              "tester": {"behavior": "blocked"},
              "auditor": {"behavior": "blocked"}},
             2, "blocked", "blocked", 1,
             ["--capability-command", str(FALSE_EXECUTABLE)],
             [(1, "planning", "planned"),
              (1, "implementation", "task_completed"),
              (1, "verification", "blocked"), (1, "audit", "blocked")]),
            ({"planner": {"behavior": "planned"},
              "developer": {"behavior": "complete"},
              "tester": {"behavior": "dirty"},
              "auditor": {"behavior": "pass"}},
             5, "infrastructure_failure", "infrastructure_failure", 0, [],
             [(1, "planning", "planned"),
              (1, "implementation", "task_completed"),
              (1, "verification", "infrastructure_failure")]),
            ({"planner": {"behavior": "planned"},
              "developer": {"behavior": "complete"},
              "tester": {"behavior": "pass"},
              "auditor": {"behavior": "blocked"}},
             2, "blocked", "blocked", 1, [],
             [(1, "planning", "planned"),
              (1, "implementation", "task_completed"),
              (1, "verification", "pass"), (1, "audit", "blocked")]),
        ]
        for scenario, exit_code, phase, outcome, rounds_completed, extra, \
                history in fixtures:
            ws = self.make(scenario)
            rc, data = ws.run_cli(extra=extra)
            self.assertEqual(rc, exit_code,
                             f"{outcome}: exit {rc}, expected {exit_code}")
            assert_terminal(self, data, terminal_phase=phase,
                            terminal_outcome=outcome, exit_code=exit_code,
                            rounds_completed=rounds_completed)
            # The exact production transition sequence is asserted, not just
            # the terminal: retries are recorded per attempt (three failed
            # planning attempts, three clean-exhaustion task failures, three
            # interrupted implementation attempts), the developer's clean
            # task failure still advances to verification, a genuine
            # verification ``blocked`` advances to audit and the audit stays
            # blocked, and a dirty tester is an untrusted verification that
            # terminates ``infrastructure_failure`` without audit.
            assert_history(self, data, history)

    # -- case 18: migration preserves authority without Ralph imports --------

    def test_case_18_migration_preserves_without_ralph_imports(self) -> None:
        # A fixture repository with a real committed plan, commits, and dirty
        # work: migration derives a snapshot that preserves the plan/commit/
        # dirty work/evidence/blockers and never imports Ralph state.
        root = Path(tempfile.mkdtemp(prefix="adversarial-migration.", dir=self.tmp))
        (root / "docs").mkdir()
        (root / ".factory").mkdir()
        (root / ".factory" / "artifacts").mkdir()
        (root / "src").mkdir()
        (root / "docs" / "SPEC.md").write_text("SPEC\n", encoding="utf-8")
        (root / ".factory" / "ralph-freeze").write_text("# frozen\n",
                                                        encoding="utf-8")
        _git(root, "init", "-q", "-b", BRANCH)
        _git(root, "config", "user.email", "factory@test")
        _git(root, "config", "user.name", "factory")
        _git(root, "add", "-A")
        _git(root, "commit", "-qm", "base")
        head = _git(root, "rev-parse", "HEAD").stdout.strip()
        spec_blob = _git(root, "rev-parse", "HEAD:docs/SPEC.md").stdout.strip()
        # The canonical plan is generated through the committed plan tool so
        # the fixture carries a real factory-plan/v1 (front matter on line 1).
        gen_plan(root, {
            "spec_path": "docs/SPEC.md",
            "spec_commit": head,
            "spec_blob": spec_blob,
            "base_commit": head,
            "lifecycle": "active",
        }, ".factory/artifacts/implementation-plan.md",
        FACTORY_CAMPAIGN.TASK_SPECS)
        # A blocked facts entry stays an external blocker in the snapshot.
        blocked = root / ".factory" / "artifacts" / "blocked-facts.json"
        blocked.write_text(json.dumps({
            "schema": "ralph-blocked-facts/v1",
            "facts": [{"id": "BLK-1", "title": "fixture blocker",
                        "status": "open", "requirements": ["MIG-01"]}],
        }), encoding="utf-8")
        _git(root, "add", "-A")
        _git(root, "commit", "-qm", "evidence")
        evidence_commit = _git(root, "rev-parse", "HEAD").stdout.strip()
        # Dirty work survives as an *uncommitted* path record (never
        # discarded, never read as a task ledger).
        dirty = root / "src" / "dirty-work.md"
        dirty.write_text("preserved dirty work\n", encoding="utf-8")
        derived = run(
            [PY, str(LOOP / "migration.py"), "--root", str(root), "derive"],
            check=False,
        )
        self.assertEqual(derived.returncode, 0, derived.stderr[-1000:])
        payload = json.loads(derived.stdout)
        self.assertEqual(payload.get("schema"), "factory-migration/v1")
        # The active plan and Git commits are preserved by reference.
        self.assertEqual(payload["head_commit"], evidence_commit)
        self.assertEqual(payload["base_commit"], head)
        self.assertEqual(payload["plan_path"],
                         ".factory/artifacts/implementation-plan.md")
        # The tracked freeze marker keeps legacy launches frozen through the
        # retained authority (exit 0 = frozen).
        frozen = run(
            [PY, str(LOOP / "migration.py"), "--root", str(root),
             "freeze", "--guard"],
            check=False,
        )
        self.assertEqual(frozen.returncode, 0,
                         "the tracked marker must freeze legacy launches")
        # Dirty work survives as a path record (never discarded, never read
        # as a task ledger).
        dirty_paths = [entry["path"] for entry in payload["dirty"]]
        self.assertIn("src/dirty-work.md", dirty_paths)
        # The structured external blocker is preserved, never elevated to a
        # verified claim.
        self.assertTrue(
            any(b.get("id") == "BLK-1" and b.get("status") == "open"
                for b in payload["blockers"]),
            "the external blocker must survive in the snapshot")
        # The migration authority imports no Ralph runtime surface: the
        # legacy `.ralph/` namespace is not read, and no Ralph import exists
        # in the hidden control plane.
        self.assertEqual(
            sorted(path.name for path in (LOOP / "migration.py").parent.glob("*.py")),
            sorted(path.name for path in (LOOP / "migration.py").parent.glob("*.py")),
        )
        migration_source = (LOOP / "migration.py").read_text(encoding="utf-8")
        for token in ("ralph emit", "ralph_event", "RalphMemory", "completion_token"):
            self.assertNotIn(token, migration_source)
        self.assertNotIn("import ralph", migration_source)
        # The freeze-guard substitution atomicity (Task 16 residual): the
        # shell launchers route the freeze decision through the retained
        # authority's fresh no-follow re-stat — a marker substitution between
        # guard invocations changes the decision with no stale cache, and an
        # unsafe marker fails closed (exit 2) instead of silently unfreezing.
        fixture = Path(tempfile.mkdtemp(prefix="adversarial-freeze.", dir=self.tmp))
        (fixture / ".factory" / "loop").mkdir(parents=True)
        for module in ("migration", "gitutil", "plan_parser", "state", "factory_state_io"):
            shutil.copy2(LOOP / f"{module}.py", fixture / ".factory" / "loop" / f"{module}.py")
        marker = fixture / ".factory" / "ralph-freeze"
        guard = [PY, str(fixture / ".factory" / "loop" / "migration.py"),
                 "--root", str(fixture), "freeze", "--guard"]

        def guard_rc() -> int:
            proc = run(guard, check=False)
            return proc.returncode

        self.assertEqual(guard_rc(), 1, "missing marker -> not frozen (exit 1)")
        marker.write_text("# frozen\n", encoding="utf-8")
        self.assertEqual(guard_rc(), 0, "regular marker -> frozen (exit 0)")
        # Substitution: swap the regular marker for a symlink. The guard's
        # single no-follow re-stat fails closed; no cached decision survives.
        target = fixture / "marker-target"
        target.write_text("t\n", encoding="utf-8")
        marker.unlink()
        marker.symlink_to(target)
        self.assertEqual(guard_rc(), 2,
                         "a symlink marker must fail closed (exit 2), never "
                         "silently unfreeze")
        marker.unlink()
        target.unlink()
        marker.write_text("# frozen\n", encoding="utf-8")
        self.assertEqual(guard_rc(), 0, "restored regular marker -> frozen again")
        # A FIFO marker also fails closed (exit 2).
        marker.unlink()
        os.mkfifo(marker)
        self.assertEqual(guard_rc(), 2, "a FIFO marker must fail closed (exit 2)")
        marker.unlink()

    # -- case 19: the leaf inherits no lock descriptor/environment ----------

    def test_case_19_leaf_inherits_no_lock_descriptor(self) -> None:
        root = Path(tempfile.mkdtemp(prefix="adversarial-leaf.", dir=self.tmp))
        _git(root, "init", "-q", "-b", BRANCH)
        _git(root, "config", "user.email", "factory@test")
        _git(root, "config", "user.name", "factory")
        (root / "docs").mkdir()
        (root / "docs" / "SPEC.md").write_text("spec\n", encoding="utf-8")
        (root / ".factory").mkdir()
        (root / ".factory" / "artifacts").mkdir()
        (root / ".factory" / "artifacts" / "implementation-plan.md").write_text(
            "plan\n", encoding="utf-8")
        _git(root, "add", "-A")
        _git(root, "commit", "-qm", "base")
        head = _git(root, "rev-parse", "HEAD").stdout.strip()
        spec_blob = _git(root, "rev-parse", "HEAD:docs/SPEC.md").stdout.strip()
        identity = state_module.repository_identity(root)
        with lock_module.acquire_root_lock(
            root, expected_identity=identity, expected_branch=BRANCH,
            spec=lock_module.SpecBinding(path="docs/SPEC.md", commit=head,
                                         blob=spec_blob),
            plan=lock_module.PlanBinding(
                path=".factory/artifacts/implementation-plan.md",
                base_commit=head,
                digest=sha256((root / ".factory" / "artifacts" / "implementation-plan.md").read_bytes())),
        ) as lock:
            child_out = root / "leaf.json"
            leaf = (
                "import json, os, sys\n"
                "out = {'env': dict(sorted(os.environ.items())), 'fds': []}\n"
                "for fd in range(0, 256):\n"
                "    try:\n"
                "        os.fstat(fd)\n"
                "        out['fds'].append(fd)\n"
                "    except OSError:\n"
                "        pass\n"
                "with open(sys.argv[1], 'w', encoding='utf-8') as stream:\n"
                "    json.dump(out, stream, sort_keys=True)\n"
            )
            proc = run([PY, "-c", leaf, str(child_out)], check=False)
            self.assertEqual(proc.returncode, 0, proc.stderr[-1000:])
            with open(child_out, encoding="utf-8") as stream:
                child = json.load(stream)
            # No lock descriptor leaks into the leaf, and no lock metadata
            # key reaches its environment.
            self.assertEqual(child["fds"], [0, 1, 2],
                             f"the leaf inherited unexpected descriptors: "
                             f"{child['fds']}")
            for key in child["env"]:
                self.assertFalse(key.startswith("FACTORY_LOCK"),
                                 f"lock metadata leaked as {key!r}")
            # A separately opened repository descriptor cannot unlock the
            # holder (flock semantics: LOCK_UN on an unrelated open file
            # description is a no-op for the holder's description).
            separate = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                import fcntl
                fcntl.flock(separate, fcntl.LOCK_UN)
                self.assertFalse(lock_module.probe_root_lock(root),
                                 "a foreign descriptor must not release the lock")
                with self.assertRaises(BlockingIOError):
                    fcntl.flock(separate, fcntl.LOCK_EX | fcntl.LOCK_NB)
            finally:
                os.close(separate)
            self.assertEqual(lock.identity, identity)

    # -- case 20: escaped descendants are terminated/detected ----------------

    def test_case_20_escaped_descendant_terminated(self) -> None:
        ws = self.make(SUCCESS_SCENARIO)
        marker = ws.root / "src" / ".factory-test-output" / "escape.json"
        backend = ws.root / "backend-escape.py"
        backend.write_text(
            "#!/usr/bin/env python3\n"
            "import json, os, subprocess, sys\n"
            f"marker = {str(marker)!r}\n"
            "sink = open('src/.factory-test-output/grandchild-sink.txt', 'wb')\n"
            "grandchild = subprocess.Popen(\n"
            "    [sys.executable, '-c', 'import os,signal,sys,time;'\n"
            "     'signal.signal(signal.SIGTERM, signal.SIG_IGN);'\n"
            "     'signal.signal(signal.SIGINT, signal.SIG_IGN);'\n"
            "     'signal.signal(signal.SIGHUP, signal.SIG_IGN);'\n"
            "     'os.setsid(); time.sleep(300)'],\n"
            "    stdin=sink, stdout=sink, stderr=sink,\n"
            ")\n"
            "sink.close()\n"
            "with open(marker, 'w', encoding='utf-8') as stream:\n"
            "    json.dump({'pid': os.getpid(), 'grandchild': grandchild.pid},\n"
            "              stream)\n"
            "sys.stdout.flush()\n",
            encoding="utf-8",
        )
        prepare_launch_workspace(ws, "backend-escape.py")
        argv = launch_cli_args(ws, backend="backend-escape.py", task_id=1,
                               runtime_limit=120.0, inactivity_limit=120.0)
        proc = subprocess.Popen(argv, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True,
                                cwd=str(ws.root))
        pids: dict[str, int] = {}
        started = time.monotonic()
        try:
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline and not marker.is_file():
                time.sleep(0.05)
            self.assertTrue(marker.is_file(), "the escape probe must have started")
            with open(marker, encoding="utf-8") as stream:
                pids = json.load(stream)
            out, err = proc.communicate(timeout=60)
            elapsed = time.monotonic() - started
            # The ptrace broker detects a tracee that outlived its target,
            # bounded-kills/reaps it, closes its pipe lifecycle, and sends an
            # unspoofable report to the outer launch supervisor.  Recovery
            # still fails closed even though no escaped process is leaked.
            self.assertEqual(proc.returncode, launch_module.EXIT_SUPERVISION,
                             f"escaped-descendant fail-closed expected, got "
                             f"{proc.returncode} (stderr: {err[-1000:]})")
            self.assertIn("escaped", err)
            self.assertLess(
                elapsed, 15.0,
                "escaped-descendant cleanup exceeded its internal bounded "
                "termination/reap window",
            )
            deadline = time.monotonic() + 5
            alive: list[int] = []
            while time.monotonic() < deadline:
                alive = []
                for pid in (pids["grandchild"], pids["pid"]):
                    try:
                        os.kill(pid, 0)
                        alive.append(pid)
                    except ProcessLookupError:
                        pass
                if not alive:
                    break
                time.sleep(0.05)
            self.assertEqual(
                alive, [],
                f"the broker failed to terminate/reap escaped pids {alive}",
            )
        finally:
            # Failure-path hygiene belongs to the test: never leak its outer
            # Popen streams or any probe process while reporting a regression.
            if proc.poll() is None:
                try:
                    proc.kill()
                    proc.wait(timeout=5)
                except (ProcessLookupError, subprocess.TimeoutExpired):
                    pass
            for stream in (proc.stdout, proc.stderr):
                if stream is not None and not stream.closed:
                    stream.close()
            for pid in pids.values():
                try:
                    os.killpg(pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass
                try:
                    os.kill(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
        self.assertTrue(proc.stdout is None or proc.stdout.closed)
        self.assertTrue(proc.stderr is None or proc.stderr.closed)

    # -- case 21: the task excerpt is byte-bound to the committed plan -------

    def test_case_21_task_excerpt_byte_bound(self) -> None:
        ws = self.make(SUCCESS_SCENARIO)
        plan = ws.root / PLAN_REL
        excerpt1 = run(
            [PY, str(LOOP / "launch.py"), "excerpt", "--plan", str(plan),
             "--task-id", "1"],
            check=False,
        )
        self.assertEqual(excerpt1.returncode, 0, excerpt1.stderr[-1000:])
        payload1 = json.loads(excerpt1.stdout)
        self.assertEqual(payload1["task_id"], 1)
        digest1 = payload1["digest"]
        # Byte substitution in the plan changes the delivered excerpt digest.
        tampered = ws.root / "tampered-plan.md"
        tampered.write_bytes(plan.read_bytes().replace(b"initial scope",
                                                       b"substituted scope"))
        excerpt2 = run(
            [PY, str(LOOP / "launch.py"), "excerpt", "--plan",
             str(tampered), "--task-id", "1"],
            check=False,
        )
        self.assertEqual(excerpt2.returncode, 0, excerpt2.stderr[-1000:])
        self.assertNotEqual(json.loads(excerpt2.stdout)["digest"], digest1,
                            "a byte substitution must change the excerpt digest")
        # A paraphrase (different bytes, same wording) also changes the
        # digest: the binding is byte-exact, never semantic.
        paraphrased = ws.root / "paraphrased-plan.md"
        paraphrased.write_bytes(plan.read_bytes().replace(b"initial scope",
                                                          b"scope stated initially"))
        excerpt3 = run(
            [PY, str(LOOP / "launch.py"), "excerpt", "--plan",
             str(paraphrased), "--task-id", "1"],
            check=False,
        )
        self.assertNotEqual(json.loads(excerpt3.stdout)["digest"], digest1,
                            "a paraphrase must change the excerpt digest")
        # The developer launch fails closed when the delivered excerpt digest
        # does not match the committed plan's section (a real committed
        # backend so the failure is the digest mismatch, never a missing file).
        backend = ws.root / "backend-excerpt.py"
        backend.write_text("#!/usr/bin/env python3\nprint('ok')\n",
                           encoding="utf-8")
        prepare_launch_workspace(ws, "backend-excerpt.py")
        wrong = launch_cli_args(ws, backend="backend-excerpt.py", task_id=1)
        wrong.append("--task-excerpt-digest")
        wrong.append("0" * 64)
        result = run_launch(ws, *wrong)
        self.assertNotEqual(result.returncode, 0,
                            "a wrong task-excerpt digest must fail closed")
        self.assertIn("task-excerpt", result.stderr)

    # -- case 22: authoritative reads allowed; legacy paths denied -----------

    def test_case_22_authoritative_reads_legacy_paths_denied(self) -> None:
        # §22 test 22 is a hard conformance requirement, never a skip: the
        # manifest/source checker rejects any skipTest in a case method.  An
        # unavailable Landlock primitive is therefore a hard FAILURE here
        # (the production confine authority fails closed the same way), not
        # a skipped case.  The probe runs in a fresh child exactly as the
        # production authority probes it.
        self.assertTrue(
            wc.confinement_primitive_available(),
            "case 22 requires the real Landlock LSM; an unavailable primitive "
            "is a hard §22 conformance failure of this case (never a skip; "
            "the production confine authority fails closed without it)",
        )
        workspace = self.tmp / "confined-workspace"
        workspace.mkdir()
        (workspace / "scripts").mkdir()
        (workspace / "src").mkdir()
        (workspace / "src" / ".factory-test-output").mkdir()
        (workspace / "docs").mkdir()
        (workspace / ".factory").mkdir()
        (workspace / ".factory" / "artifacts").mkdir()
        (workspace / ".ralph").mkdir()
        (workspace / ".factory-state").mkdir()
        (workspace / ".pi").mkdir()
        (workspace / "docs" / "SPEC.md").write_text("SPEC\n", encoding="utf-8")
        (workspace / ".factory" / "artifacts" / "implementation-plan.md").write_text(
            "plan\n", encoding="utf-8")
        (workspace / ".ralph" / "secret.txt").write_text("forbidden\n",
                                                         encoding="utf-8")
        (workspace / ".factory-state" / "factory-loop.json").write_text(
            "{}", encoding="utf-8")
        (workspace / ".pi" / "memory.txt").write_text("forbidden\n",
                                                      encoding="utf-8")
        store = workspace / ".ollama-usage-env"
        store.write_text("export OLLAMA_COOKIE='x'\n", encoding="utf-8")
        os.chmod(store, 0o600)
        backend = workspace / "backend.py"
        backend.write_text("#!/usr/bin/env python3\nprint('ok')\n",
                           encoding="utf-8")
        os.chmod(backend, 0o700)
        probe = workspace / "probe.py"
        probe.write_text(PROBE_SOURCE, encoding="utf-8")
        os.chmod(probe, 0o700)
        _git(workspace, "init", "-q", "-b", BRANCH)
        _git(workspace, "config", "user.email", "factory@test")
        _git(workspace, "config", "user.name", "factory")
        _git(workspace, "add", "-A")
        _git(workspace, "commit", "-qm", "fixture")
        head = _git(workspace, "rev-parse", "HEAD").stdout.strip()
        binding = launch_module.InvocationBinding(
            role="developer", model="synthetic-model", provider="synthetic",
            backend=backend, workspace=workspace, bound_commit=head,
            role_prompt_digest=sha256(b"role"),
            prompt_set_digest=sha256(b"set"),
            plan_digest=sha256((workspace / ".factory" / "artifacts" /
                                "implementation-plan.md").read_bytes()),
            policy_digest=sha256(b"policy"),
            specification_digest=sha256((workspace / "docs" / "SPEC.md").read_bytes()),
            allowed_tools=("read", "bash"),
        )
        home = wc.sanitized_home_directory()
        self.addCleanup(shutil.rmtree, home, ignore_errors=True)
        rule_descriptors: list[int] = []
        spec = wc.confinement_spec(
            binding, sanitized_home=home,
            _rule_descriptors=rule_descriptors,
        )
        self.addCleanup(lambda: [os.close(fd) for fd in rule_descriptors])
        wc.validate_confinement_spec(spec, binding)
        spec_path = self.tmp / "confinement-spec.json"
        spec_path.write_text(
            json.dumps(spec, sort_keys=True, separators=(",", ":")),
            encoding="utf-8")

        def probe_read(targets: list, confined: bool) -> dict:
            argv = [PY, str(workspace / "probe.py"), json.dumps(targets)]
            if confined:
                argv = [PY, str(LOOP / "confine_launcher.py"),
                        "--spec-file", str(spec_path),
                        "--rule-fds", ",".join(
                            str(fd) for fd in rule_descriptors),
                        "--", *argv]
            proc = subprocess.run(
                argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                cwd=str(workspace), timeout=60,
                pass_fds=tuple(rule_descriptors) if confined else (),
            )
            self.assertEqual(proc.returncode, 0, proc.stderr.decode()[-2000:])
            line = next(
                text for text in proc.stdout.decode("utf-8", "replace").splitlines()
                if text.startswith("FACTORY_CONFINEMENT_PROBE "))
            return json.loads(line.split(" ", 1)[1])

        targets = [
            {"op": "read", "path": ".ralph/secret.txt", "label": "ralph"},
            {"op": "read", "path": ".factory-state/factory-loop.json",
             "label": "factory-state"},
            {"op": "read", "path": ".pi/memory.txt", "label": "pi-memory"},
            {"op": "read", "path": ".ollama-usage-env", "label": "env-store"},
            {"op": "read", "path": "docs/SPEC.md", "label": "spec"},
            {"op": "read", "path": ".factory/artifacts/implementation-plan.md",
             "label": "plan"},
        ]
        # The denial control: unconfined, every read succeeds (files exist).
        unconfined = probe_read(targets, confined=False)
        for label in ("ralph", "factory-state", "pi-memory", "env-store",
                      "spec", "plan"):
            self.assertEqual(unconfined.get(f"read:{label}"), "ok",
                             f"unconfined read of {label} must succeed")
        confined = probe_read(targets, confined=True)
        for label in ("ralph", "factory-state", "pi-memory", "env-store"):
            self.assertEqual(confined.get(f"read:{label}"), "PermissionError",
                             f"the confined role must be denied {label}")
        for label in ("spec", "plan"):
            self.assertEqual(confined.get(f"read:{label}"), "ok",
                             f"the confined role must read the authoritative {label}")

    # -- case 23: mid-phase mutation / rewind / binding mismatch fails closed -

    def test_case_23_mid_phase_mutation_fails_closed(self) -> None:
        root = Path(tempfile.mkdtemp(prefix="adversarial-mutation.", dir=self.tmp))
        _git(root, "init", "-q", "-b", BRANCH)
        _git(root, "config", "user.email", "factory@test")
        _git(root, "config", "user.name", "factory")
        (root / "docs").mkdir()
        (root / "docs" / "SPEC.md").write_text("spec\n", encoding="utf-8")
        _git(root, "add", "-A")
        _git(root, "commit", "-qm", "base")
        head = _git(root, "rev-parse", "HEAD").stdout.strip()
        digest = "b" * 64
        self.assertEqual(self.state_cli(
            root, "init", "--campaign-id", "mutation-campaign",
            "--rounds", "2", "--base-commit", head,
            "--spec-digest", digest, "--plan-digest", digest,
            "--audit-digest", digest,
            "--role-digest", "planner=" + digest,
            "--branch", BRANCH).returncode, 0)
        state_file = root / STATE_DIR / STATE_FILE
        # Mid-phase mutation: the before/after digest ledger rejects any
        # state change during an untrusted phase.
        recorded = self.state_cli(root, "record-phase-digest", "phase-1")
        self.assertEqual(recorded.returncode, 0, recorded.stderr[-1000:])
        with open(state_file, encoding="utf-8") as stream:
            state = json.load(stream)
        state["specification_digest"] = "c" * 64  # a mid-phase rewrite
        state_file.write_text(
            json.dumps(state, sort_keys=True, separators=(",", ":")),
            encoding="utf-8")
        os.chmod(state_file, 0o600)
        verify = self.state_cli(root, "verify-phase-digest", "phase-1")
        self.assertNotEqual(verify.returncode, 0,
                            "a mid-phase mutation must fail the digest gate")
        self.assertIn("digest changed", verify.stderr)
        # Counter rewind: rewinding the round counter is a digest change the
        # gate rejects.  The state is first advanced to a valid later round
        # (rounds_requested=2) and its digest recorded; the rewind to round 1
        # is then a semantic mutation the gate must refuse.
        state = json.loads(state_file.read_text(encoding="utf-8"))
        state["current_round"] = 2
        state_file.write_text(
            json.dumps(state, sort_keys=True, separators=(",", ":")),
            encoding="utf-8")
        os.chmod(state_file, 0o600)
        self.assertEqual(
            self.state_cli(root, "record-phase-digest", "phase-2").returncode, 0)
        state = json.loads(state_file.read_text(encoding="utf-8"))
        state["current_round"] = 1  # rewind
        state_file.write_text(
            json.dumps(state, sort_keys=True, separators=(",", ":")),
            encoding="utf-8")
        os.chmod(state_file, 0o600)
        self.assertNotEqual(
            self.state_cli(root, "verify-phase-digest", "phase-2").returncode, 0,
            "a counter rewind must fail the digest gate")
        # Campaign-option / branch mismatch fails closed at the campaign
        # boundary: a campaign bound to a different campaign id or rounds
        # count cannot resume the recorded state.
        ws = self.make(SUCCESS_SCENARIO)
        rc, _ = ws.run_cli()
        self.assertEqual(rc, 0)
        mismatch = run(
            [PY, str(LOOP / "campaign.py"), "--root", str(ws.root), "run",
             "--campaign-id", "campaign", "--rounds", "3", "--branch", BRANCH,
             "--role-driver", DRIVER_REL, "--scenario", "scenario.json",
             "--verification-command", str(TRUE_EXECUTABLE)],
            check=False,
        )
        self.assertNotEqual(mismatch.returncode, 0,
                            "a rounds-requested mismatch must fail closed")
        branch_mismatch = run(
            [PY, str(LOOP / "campaign.py"), "--root", str(ws.root), "run",
             "--campaign-id", "campaign", "--rounds", "1", "--branch", "other",
             "--role-driver", DRIVER_REL, "--scenario", "scenario.json",
             "--verification-command", str(TRUE_EXECUTABLE)],
            check=False,
        )
        self.assertNotEqual(branch_mismatch.returncode, 0,
                            "a branch mismatch must fail closed")
        # Specification change fails closed: a committed spec change makes the
        # plan's spec binding stale before any role launches.
        (ws.root / "docs" / "SPEC.md").write_text("CHANGED SPEC\n", encoding="utf-8")
        _git(ws.root, "add", "-A")
        _git(ws.root, "commit", "-qm", "spec change")
        spec_change = run(
            [PY, str(LOOP / "campaign.py"), "--root", str(ws.root), "run",
             "--campaign-id", "campaign", "--rounds", "1", "--branch", BRANCH,
             "--role-driver", DRIVER_REL, "--scenario", "scenario.json",
             "--verification-command", str(TRUE_EXECUTABLE)],
            check=False,
        )
        self.assertNotEqual(spec_change.returncode, 0,
                            "a specification change must fail closed")

    # -- case 24: synthetic Ollama credentials in neither argv nor env -------

    def test_case_24_synthetic_ollama_credentials_absent(self) -> None:
        server = _ScriptedServer(
            [(200, (VISIBLE_FIXTURES / "usage-ok.html").read_bytes())],
            hold=True,
        )
        self.addCleanup(server.close)
        proc = subprocess.Popen(
            [PY, str(LOOP / "usage.py"), "--check",
             "--settings-url", f"http://127.0.0.1:{server.port}/",
             "--cookie-stdin"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, cwd=str(ROOT),
        )
        proc.stdin.write(SYNTH_COOKIE.encode())
        proc.stdin.close()
        self.assertTrue(server.request_seen.wait(30), "fetch never started")
        child = None
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            child = _find_fetch_child(proc.pid)
            if child is not None:
                break
            time.sleep(0.02)
        self.assertIsNotNone(child, "the live fetch child was never observed")
        cmdline = Path(f"/proc/{child}/cmdline").read_bytes()
        environ = Path(f"/proc/{child}/environ").read_bytes()
        for token in (SYNTH_COOKIE_NAME, SYNTH_COOKIE_VALUE, SYNTH_AID,
                      "OLLAMA_COOKIE"):
            self.assertNotIn(token.encode(), cmdline,
                             f"{token!r} leaked into the fetch child argv")
            self.assertNotIn(token.encode(), environ,
                             f"{token!r} leaked into the fetch child environment")
        for entry in environ.split(b"\x00"):
            self.assertFalse(entry.startswith(b"OLLAMA_"),
                             f"an OLLAMA_* key reached the fetch child: {entry!r}")
        # The guard's own argv/env is clean too.
        guard_cmdline = Path(f"/proc/{proc.pid}/cmdline").read_bytes()
        guard_environ = Path(f"/proc/{proc.pid}/environ").read_bytes()
        for token in (SYNTH_COOKIE_NAME, SYNTH_COOKIE_VALUE, SYNTH_AID):
            self.assertNotIn(token.encode(), guard_cmdline)
            self.assertNotIn(token.encode(), guard_environ)
        server.release()
        out, err = proc.communicate(timeout=20)
        self.assertEqual(proc.returncode, usage_module.EXIT_ALLOWED, err.decode())
        self.assertNotIn(SYNTH_COOKIE_VALUE.encode(), out)
        self.assertNotIn(SYNTH_COOKIE_VALUE.encode(), err)
        # --wait is equally clean.
        server2 = _ScriptedServer(
            [(200, (VISIBLE_FIXTURES / "usage-ok.html").read_bytes())],
            hold=True,
        )
        self.addCleanup(server2.close)
        proc2 = subprocess.Popen(
            [PY, str(LOOP / "usage.py"), "--wait",
             "--settings-url", f"http://127.0.0.1:{server2.port}/",
             "--cookie-stdin", "--poll-interval", "30", "--max-polls", "3"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, cwd=str(ROOT),
        )
        proc2.stdin.write(SYNTH_COOKIE.encode())
        proc2.stdin.close()
        self.assertTrue(server2.request_seen.wait(30), "wait fetch never started")
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            child = _find_fetch_child(proc2.pid)
            if child is not None:
                break
            time.sleep(0.02)
        self.assertIsNotNone(child)
        cmdline = Path(f"/proc/{child}/cmdline").read_bytes()
        environ = Path(f"/proc/{child}/environ").read_bytes()
        for token in (SYNTH_COOKIE_NAME, SYNTH_COOKIE_VALUE, SYNTH_AID):
            self.assertNotIn(token.encode(), cmdline)
            self.assertNotIn(token.encode(), environ)
        # Signal the guard *while* the fetch is still held: the scoped
        # handler terminates and reaps the in-flight credential-holding
        # fetch child and exits 128+SIGTERM (never leaving the child).
        proc2.send_signal(signal.SIGTERM)
        server2.release()
        out2, err2 = proc2.communicate(timeout=20)
        self.assertEqual(proc2.returncode, 128 + signal.SIGTERM)
        self.assertNotIn(SYNTH_COOKIE_VALUE.encode(), out2 + err2)

    # -- case 25: the immutable verifier descriptor rejects pathname swap ----

    def test_case_25_immutable_verifier_descriptor(self) -> None:
        # The retained Python verifier authority (campaign-verifier-binding.py)
        # binds the configured campaign verifier (verification.campaign_command
        # from .factory/config.toml) to its committed blob/identity.  The
        # helper is opened before the verifier step and executed through the
        # retained descriptor, so a workspace pathname or byte substitution
        # can never substitute the verifier that runs (EVID-01 §19).
        root = Path(tempfile.mkdtemp(prefix="adversarial-verifier.", dir=self.tmp))
        (root / ".factory").mkdir()
        (root / ".factory" / "tools").mkdir()
        shutil.copy2(ROOT / ".factory" / "tools" / "campaign-verifier-binding.py",
                     root / ".factory" / "tools" / "campaign-verifier-binding.py")
        verifier = root / ".factory" / "tools" / "verify-project.sh"
        verifier.write_text(
            "#!/usr/bin/env bash\necho ORIGINAL-VERIFIER-RAN\n"
            "exit 0\n", encoding="utf-8")
        verifier.chmod(0o755)
        (root / ".factory" / "config.toml").write_text(
            '[verification]\n'
            'campaign_command = ["./.factory/tools/verify-project.sh"]\n',
            encoding="utf-8")
        (root / ".factory" / "verifier-acceptance.json").write_text(
            json.dumps({"schema": "ralph-verifier-acceptance/v1",
                        "gates": [{"name": "test-one.sh", "args": []}]}),
            encoding="utf-8")
        _git(root, "init", "-q", "-b", BRANCH)
        _git(root, "config", "user.email", "factory@test")
        _git(root, "config", "user.name", "factory")
        _git(root, "add", "-A")
        _git(root, "commit", "-qm", "base")
        helper = root / ".factory" / "tools" / "campaign-verifier-binding.py"

        def helper_run(*args: str, pass_fds: tuple = ()) -> subprocess.CompletedProcess[str]:
            return subprocess.run(
                [*args], cwd=str(root), text=True, capture_output=True,
                pass_fds=pass_fds,
            )

        # The campaign verifier binding (EVID-01 §19): the campaign command
        # is bound to its committed blob/identity through the retained
        # Python authority.
        bound = helper_run(str(helper))
        self.assertEqual(bound.returncode, 0, bound.stderr[-1000:])
        binding = json.loads(bound.stdout)
        self.assertEqual(binding["binding"]["schema"], "campaign-verifier-binding/v1")
        self.assertEqual(binding["binding"]["executable"], ".factory/tools/verify-project.sh")
        self.assertEqual(binding["binding"]["argv"],
                         ["./.factory/tools/verify-project.sh"])
        digest = binding["sha256"]
        # The bound inode executes through the retained descriptor; a
        # pathname substitution of the helper can never run the substitute.
        fd = os.open(helper, os.O_RDONLY)
        try:
            os.set_inheritable(fd, True)
            helper.rename(root / ".factory" / "tools" / "campaign-verifier-binding.py.orig")
            substitute = root / ".factory" / "tools" / "campaign-verifier-binding.py"
            substitute.write_text(
                "#!/usr/bin/env python3\n"
                "print('SUBSTITUTE-HELPER-RAN')\n", encoding="utf-8")
            substitute.chmod(0o755)
            repeated = helper_run(
                f"/proc/self/fd/{fd}", pass_fds=(fd,))
            self.assertEqual(repeated.returncode, 0, repeated.stderr[-1000:])
            self.assertNotIn("SUBSTITUTE-HELPER-RAN", repeated.stdout,
                             "the substituted helper must never run")
            verifier_digest = json.loads(repeated.stdout)["sha256"]
            self.assertEqual(verifier_digest, digest)
            # The campaign verifier runs through the retained descriptor
            # authority with the exact bound digest.
            executed = helper_run(
                f"/proc/self/fd/{fd}",
                "--expected-digest", digest, "--exec", pass_fds=(fd,))
            self.assertEqual(executed.returncode, 0, executed.stderr[-1000:])
            self.assertIn("ORIGINAL-VERIFIER-RAN", executed.stdout)
            self.assertNotIn("SUBSTITUTE", executed.stdout)
        finally:
            os.close(fd)
            # Hand the workspace back to the original authority: the helper
            # was moved aside for the retained-descriptor proof, so the
            # substitution swap is reverted before any pathname invocation
            # of the helper in the remaining checks.
            (root / ".factory" / "tools" / "campaign-verifier-binding.py.orig").rename(helper)
        # A verifier byte substitution fails closed before execution.
        verifier.write_text(
            "#!/usr/bin/env bash\necho SUBSTITUTE-VERIFIER-RAN\nexit 7\n",
            encoding="utf-8")
        rejected = helper_run(str(helper),
                              "--expected-digest", digest, "--exec")
        self.assertNotEqual(rejected.returncode, 0,
                            "a substituted campaign verifier must fail closed")
        self.assertNotIn("SUBSTITUTE-VERIFIER-RAN", rejected.stdout)
        run(["git", "-C", str(root), "restore", "--", ".factory/tools/verify-project.sh"],
            check=True)
        # No missing verifier: a config without campaign_command fails
        # closed with the exact diagnostic, and a nonexistent configured
        # verifier fails closed too.
        config = root / ".factory" / "config.toml"
        config.write_text('[verification]\n', encoding="utf-8")
        missing = helper_run(str(helper))
        self.assertNotEqual(missing.returncode, 0)
        self.assertIn("verification.campaign_command must be a non-empty argv array",
                      missing.stderr)
        config.write_text(
            '[verification]\ncampaign_command = ["./.factory/tools/does-not-exist.sh"]\n',
            encoding="utf-8")
        run(["git", "-C", str(root), "add", "-A"], check=True)
        run(["git", "-C", str(root), "commit", "-qm", "config update"], check=True)
        gone = helper_run(str(helper))
        self.assertNotEqual(gone.returncode, 0,
                            "a nonexistent campaign verifier must fail closed")
        self.assertNotEqual(gone.stderr.strip(), "")

    # -- case 26: Git commit-boundary bypass attempts remain rejected ---------

    def test_case_26_git_commit_boundary_bypass_rejected(self) -> None:
        # The fail-closed pre-commit boundary plus the shim argv boundary:
        # bypass flags and hooksPath redirects are rejected before Git runs.
        shim = ROOT / ".factory" / "tools" / "pi-cli-shims" / "git"
        self.assertTrue(shim.is_file())
        for bypass in ("--no-verify", "-n"):
            proc = run(
                ["bash", str(shim), "commit", bypass, "-m", "x"],
                check=False,
            )
            self.assertNotEqual(proc.returncode, 0,
                                f"the shim must reject commit {bypass}")
            self.assertIn("bypass", proc.stderr)
        for config_flag in ("-c", "--config"):
            proc = run(
                ["bash", str(shim), config_flag, "core.hooksPath=/tmp/x",
                 "commit", "-m", "x"],
                check=False,
            )
            self.assertNotEqual(proc.returncode, 0,
                                f"the shim must reject {config_flag} hooksPath")
        # The commit boundary refuses to run as root (no candidate can be
        # proven immutable), and the retained git shim enforces the
        # fail-closed pre-commit boundary installation for every
        # commit-creation path (the deprecated ralph launchers that once
        # installed it are removed).
        guard_text = (ROOT / ".factory" / "tools" / "git-commit-guard.sh").read_text(
            encoding="utf-8")
        self.assertIn("pre-commit", guard_text)
        shim_text = (ROOT / ".factory" / "tools" / "pi-cli-shims" / "git").read_text(
            encoding="utf-8")
        self.assertIn("install-git-commit-guard.sh", shim_text,
                      "the git shim must install the commit boundary")
        self.assertIn("pre-commit", shim_text)

    # -- case 27: no Ralph lifecycle dependency in the new implementation ----

    def test_case_27_no_ralph_lifecycle_dependency(self) -> None:
        # §22 test 27 scans the COMPLETE production hidden control plane —
        # every module in .factory/loop/, with no blanket exclusion
        # (campaign.py and migration.py are scanned exactly like every other
        # module) — plus every retained production script of the new path.
        # A Ralph lifecycle token is permitted only where the file is the
        # deprecation forwarder that names the deprecated surface solely to
        # freeze, detect, remove, or reject it (the explicit allowlist
        # below); every allowlisted mention must sit in a non-dependence
        # context (never an import, invocation, environment read, exec, or
        # attribute access), and the separate mechanical scan below proves
        # there is no runtime authority dependence at all.  The deprecated
        # lifecycle tools themselves (ralph-event-boundary.py,
        # ralph-completion-gate.sh, ralph-campaign-state.py,
        # ralph-final-state.py, ralph_lock.py, ralph-lock-recover.py,
        # ralph-supervision*.py, ralph-recover.sh, ralph-verifier-migrate.sh,
        # ralph-context-summary.py, check-context-summary.py,
        # repair-scratchpad-handoffs.py, pi-ralph-emit-extension.mjs,
        # pi-cli-shims/ralph, pi2-ollama.sh) are the deprecated surface
        # itself, not new implementation, and are excluded only from this
        # "new implementation" scan — the new path's non-dependence on them
        # is proven below.
        lifecycle_tokens = (
            "ralph emit", "ralph_emit", "completion token",
            "completion_token", "ralphmemory", "ralph_event",
            "ralph event stream", "ralph event", "ralph events",
            "ralph lifecycle", "ralph memory", "ralph ledger",
            "ralph shim", "ralph runtime task", "ralph task",
            "ralph completion",
        )
        # Explicit allowlist: the exact lifecycle tokens the deprecation
        # forwarders legitimately name (and nothing else).  Every allowlisted
        # occurrence is verified below to be forwarder prose in a
        # non-dependence context.
        forwarder_allowlist = {
            # migration.py is the migration/deprecation authority: it names
            # the deprecated completion-token surface only to declare that it
            # never participates in the migrated state.
            "migration.py": ("completion token",),
            # check-scratchpad.sh is the retained gate that rejects reserved
            # lifecycle completion tokens in the scratchpad.
            "check-scratchpad.sh": ("completion token",),
            # verify-boilerplate.sh asserts the role prompts deny the
            # reserved completion token (the ``emit the completion token``
            # phrase the prompts require for scratchpad prose).
            "verify-boilerplate.sh": ("completion token",),
        }
        # The deprecated lifecycle layer: the frozen launchers (which the
        # migration authority must name to freeze) and the deeper lifecycle
        # tools (which no new-path module may reference at all).
        frozen_launchers = {
            "ralph-run.sh", "ralph-plan.sh", "ralph-campaign.sh",
            "ralph-audit.sh", "ralph-maintenance-plan.sh",
            "ralph-maintenance-run.sh", "ralph-recover.sh",
        }
        legacy_lifecycle_tools = {
            "ralph-completion-gate.sh", "ralph-supervision.sh",
            "ralph-verifier-migrate.sh",
            "ralph-campaign-state.py", "ralph-context-summary.py",
            "ralph-event-boundary.py", "ralph-final-state.py",
            "ralph-lock-recover.py", "ralph-supervision-migrate.py",
            "ralph_lock.py", "pi-ralph-emit-extension.mjs", "pi2-ollama.sh",
            "repair-scratchpad-handoffs.py", "check-context-summary.py",
        }
        loop_modules = sorted(LOOP.glob("*.py"))
        scanned_scripts: list[Path] = []
        for candidate in sorted((ROOT / "scripts").glob("*")):
            if (candidate.is_file()
                    and candidate.name not in frozen_launchers
                    and candidate.name not in legacy_lifecycle_tools):
                scanned_scripts.append(candidate)
        for candidate in sorted((ROOT / ".factory" / "tools" / "pi-cli-shims").glob("*")):
            if candidate.is_file() and candidate.name != "ralph":
                scanned_scripts.append(candidate)
        self.assertTrue(scanned_scripts, "the script scan must cover scripts")
        for source in loop_modules + scanned_scripts:
            if source.parent.name == "loop":
                label = source.name
            else:
                label = source.relative_to(ROOT).as_posix()
            text = source.read_text(encoding="utf-8")
            lowered = text.lower()
            for token in lifecycle_tokens:
                if token not in lowered:
                    continue
                allowed = forwarder_allowlist.get(source.name, ())
                self.assertIn(
                    token, allowed,
                    f"{label} depends on a Ralph lifecycle surface: {token!r}",
                )
                # Forwarder proof: every occurrence of the allowlisted token
                # must be in a non-dependence context — never an import,
                # a module/binary invocation, an environment read, an exec,
                # or a ``ralph.`` attribute access.
                for number, line in enumerate(text.splitlines(), 1):
                    if token not in line.lower():
                        continue
                    self.assertFalse(
                        re.search(r"(^|\s)(import|from)\s", line),
                        f"{label}:{number} must not import a Ralph surface: "
                        f"{line.strip()!r}",
                    )
                    self.assertFalse(
                        re.search(r"\bralph\s*[.(]", line.lower()),
                        f"{label}:{number} must not invoke a Ralph surface: "
                        f"{line.strip()!r}",
                    )
                    for dependence in ("subprocess", "Popen", "os.exec",
                                       "os.system", "os.environ", "getenv",
                                       "command -v", "$RALPH_BIN"):
                        self.assertNotIn(
                            dependence, line,
                            f"{label}:{number} must not read or execute a "
                            f"Ralph surface: {line.strip()!r}",
                        )
        # No runtime authority dependence (the mechanical proof): the
        # complete control plane never imports a Ralph module, never accesses
        # a ``ralph.*`` attribute, never executes a ralph binary, never reads
        # a Ralph lifecycle environment surface, and never references the
        # deprecated lifecycle tools by exact file name.
        for source in loop_modules:
            text = source.read_text(encoding="utf-8")
            label = source.name
            for token in ("import ralph", "from ralph", "ralph emit",
                          "ralph_emit", "ralph_event", "RALPH_BIN",
                          "command -v ralph", "pi-ralph-emit-extension",
                          "pi-cli-shims/ralph"):
                self.assertNotIn(token, text,
                                 f"{label} has a Ralph runtime dependence: "
                                 f"{token!r}")
            tree = ast.parse(text)
            for node in ast.walk(tree):
                if isinstance(node, (ast.Import, ast.ImportFrom)):
                    for alias in node.names:
                        self.assertFalse(
                            alias.name == "ralph"
                            or alias.name.startswith("ralph."),
                            f"{label} imports the Ralph authority at line "
                            f"{node.lineno}",
                        )
                if (isinstance(node, ast.Attribute)
                        and isinstance(node.value, ast.Name)
                        and node.value.id == "ralph"):
                    self.fail(f"{label} accesses a Ralph attribute at line "
                              f"{node.lineno}")
            if source.name == "migration.py":
                # The migration authority is the single deprecation
                # forwarder: it may name the frozen launchers it freezes, but
                # never the deeper lifecycle tools.
                tools = legacy_lifecycle_tools
            else:
                tools = frozen_launchers | legacy_lifecycle_tools
            for tool in tools:
                self.assertNotIn(tool, text,
                                 f"{label} references the deprecated "
                                 f"lifecycle tool {tool!r}")
        # The migration authority names the deprecated surface only to freeze
        # and detect it — it never imports or reads any Ralph runtime surface.
        migration_text = (LOOP / "migration.py").read_text(encoding="utf-8")
        for import_token in ("from ralph", "import ralph", "ralph.emit",
                             "ralph.plan", "ralph.audit", "ralph.memory",
                             "ralph_event", "ralph_emit"):
            self.assertNotIn(import_token, migration_text)
        self.assertNotIn("completion_token", migration_text)
        # The campaign driver seam is the only model simulation and it is
        # explicitly the designated fixture role driver.
        driver = (FIXTURES / "campaign_driver.py").read_text(encoding="utf-8")
        self.assertIn("embedded/fixture role seam", driver)
        # The new launch authority invokes the committed secure wrapper
        # directly — never the deprecated ralph shim or the emit extension.
        launch_text = (LOOP / "launch.py").read_text(encoding="utf-8")
        for token in ("pi-cli-shims/ralph", "pi-ralph-emit-extension",
                      "ralph emit", "ralph_emit"):
            self.assertNotIn(token, launch_text,
                             f"launch.py must not depend on {token!r}")
        self.assertIn("pi2-secure-exec.py", launch_text)
        # The retained production gates never invoke the deprecated emit
        # extension or the ralph emit protocol (an artifact inventory that
        # merely requires the frozen shim to *exist* is retention, not an
        # invocation, and is covered by the required-artifacts gate).
        for script in (".factory/tools/verify-boilerplate.sh", ".factory/tools/final-gate.sh",
                       ".factory/tools/campaign-verifier-binding.py",
                       ".factory/tools/check-installed-functional-evidence.sh",
                       ".factory/tools/visual-audit-provenance.py"):
            text = (ROOT / script).read_text(encoding="utf-8")
            for token in ("pi-ralph-emit-extension", "ralph emit",
                          "ralph-event"):
                self.assertNotIn(token, text,
                                 f"{script} must not depend on {token!r}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
