#!/usr/bin/env python3
"""Harness-owned adversarial tests for model workspace/tool confinement (Task 8).

This suite lives under the hidden ``.factory/tests/`` namespace (HIDE-01 keeps
harness-only tests out of the adopting product's visible ``tests/`` tree) and
is the deterministic verification for Task 8 (role prompts, prompt-set
binding, audit-objective registry selection, and real model workspace
confinement).  Nothing here is simulated: the confinement is applied through
the real Linux Landlock LSM by the exact committed confine-launcher blob
``.factory/loop/confine_launcher.py`` executed as a subprocess, and the
production authority ``.factory/loop/workspace_confinement.py`` is exercised
end to end.

Coverage:

* **real Landlock subprocess matrix** (CTX-02, §5.1/§18): for every role the
  confine launcher is executed against a real specification built from a real
  committed fixture workspace (outside the system-allowlisted roots), and the
  confined probe child proves — under real Landlock enforcement — that
  ``.ralph/``, ``.factory-state/``, the operator ``.ollama-usage-env`` store,
  the control-plane launcher blob, and every path outside the allowlist are
  denied by default, that the per-role read allowlists work, and that the
  per-role write allowlists are honored (planner writes only the plan; the
  developer writes product entries; the tester writes build trees only; the
  auditor writes nothing).  A control probe runs the same reads *without*
  confinement to prove the denial is not vacuous;
* **symlink escapes** (Task 8 review, finding 1): a symlink in any top-level
  allowlist component fails the specification construction closed, and a
  nested symlink inside an allowlisted directory whose resolved target
  escapes the allowed namespace is denied by the *effective* confinement
  (with an unconfined readable control);
* **no repository history / no ``.git`` reads** (finding 2): ``.git/config``,
  ``.git/HEAD``, and reflog reads are denied and ``git show HEAD`` cannot
  read repository history inside the confined child (with an unconfined
  working ``git show`` control);
* **no ``/proc`` grant** (finding 3): self/other ``environ``, ``cmdline``,
  ``maps``, and ``fd`` reads under ``/proc`` are denied by the effective
  confinement (with an unconfined readable control);
* **exact per-launch private paths, no sibling access** (finding 4): the
  model receives no broad ``/tmp`` grant, and a sibling launch's private
  home/staging/prompt/session paths are denied (with an unconfined
  readable/writable control);
* **mandatory real confinement for every provider and every public
  authorize API** (finding 5): a direct programmatic authorize with neither
  the real specification nor the private synthetic seam fails closed for
  every provider, and the programmatic path with the real specification
  mints a real (never synthetic) proof;
* **private-directory cleanup on authorization failure** (finding 6): any
  failed authorization removes all four per-launch private directories
  (exec staging, prompt, session, sanitized home);
* **narrow documented system-path allowlist** (finding 7): a ``/run/...``
  Unix socket and the host-config file ``/etc/hostname`` are denied under
  the effective confinement, each with an unconfined non-vacuous control;
* **sanitized HOME/XDG**: the confined child sees a fresh mode-0700 private
  home under the shared temporary directory and cannot reach the operator's
  real home;
* **fail-closed launcher**: malformed, foreign-schema, unknown-right,
  symlinked, or oversized confinement specifications fail closed before exec;
* **confinement specification determinism**: the ``factory-confinement/v1``
  specification is a pure function of the binding + committed workspace, the
  forbidden namespaces never appear in any rule, and ``validate_confinement_spec``
  fails closed on any tamper;
* **real confinement proof**: ``prove_confinement`` (the Task 8 production
  authority) requires the real Landlock primitive, binds the exact bound
  commit/workspace/provider, the exact executing guard-source bytes
  (``usage.py`` / ``usage_fetch.py``), the exact confinement-specification
  digest, and every effective Task 7 credential channel — the default
  operator env store, an explicitly specified cookie file, and stdin-provided
  credential provenance — while rejecting a channel scoped inside the
  workspace or granted by the confinement allowlist, a foreign/forged proof,
  and the Task 7 synthetic seam (a synthetic proof is never evidence and can
  never satisfy the real authority or the public launch surface);
* **production launch integration**: ``authorize_launch`` with the real
  specification mints a real proof (no model can start without it),
  ``LaunchSupervision`` re-validates the proof immediately before exec, and
  the full ``python -m factory.loop.launch`` CLI runs the model child through
  the staged confine launcher so the leaf's own environment and forbidden-path
  probes prove real confinement at the production boundary;
* **Task 10 exact-file result handoff** (REQ 4): the confined tester/auditor
  holds exact read/write access to exactly its configured transient
  phase/audit result file — never the ``.factory-state/`` breadth.  Real
  kernel probes prove the pre-created exact file is writable/readable while
  every sibling (create, read, write) stays denied, with an unconfined
  control proving the same fixture is accessible without confinement; the
  extra-write validation rejects symlink, missing, non-regular, and foreign
  extra paths; the handoff digest is the SHA-256 of the exact transient
  bytes; and the campaign launch authority grants the result channel only to
  the role that owns it (tester -> phase result, auditor -> audit result,
  planner/developer -> none);
* **role-prompt set and audit-objective registry**: the four committed static
  role prompts have fixed bytes and deterministic per-role/prompt-set digests,
  and the committed audit-objective registry is parsed strictly and selects
  deterministically per round (no randomness, no runtime state);
* **exported API / docs schema**: ``launch`` exports the confine-launcher and
  confinement-spec constants, the committed ``factory-confinement/v1`` schema
  document exists and covers the real specification surface, and the confine
  launcher's access-bit table agrees with the production authority's.

The whole suite runs hermetically: synthetic tokens and loopback fixtures
only, no real credential is ever read, and every temporary fixture workspace
is removed.
"""

from __future__ import annotations

import contextlib
import dataclasses
import hashlib
import importlib.util
import inspect
import io
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
import unittest.mock as mock

ROOT = Path(__file__).resolve().parents[2]
LOOP = ROOT / ".factory" / "loop"
FIXTURES = ROOT / ".factory" / "tests" / "fixtures"
PROMPTS = ROOT / ".factory" / "prompts"
SCHEMAS = ROOT / ".factory" / "schemas"

sys.path.insert(0, str(LOOP))
import audit_objectives  # noqa: E402
import campaign as campaign_module  # noqa: E402
import confine_launcher  # noqa: E402
import confinement  # noqa: E402  (Task 7 private synthetic seam)
import launch  # noqa: E402
import promptset  # noqa: E402
import usage  # noqa: E402
import workspace_confinement as wc  # noqa: E402

# Load the sibling hidden campaign suite so the committed fixture workspace
# and helper conventions are reused, never copied (the Task 10 role-scoped
# result-channel tests drive the production launch authority through the
# same committed fixture the campaign suite uses).
_CAMPAIGN_SUITE = ROOT / ".factory" / "tests" / "test-factory-campaign.py"
_campaign_spec = importlib.util.spec_from_file_location(
    "factory_campaign_suite", _CAMPAIGN_SUITE)
FACTORY_CAMPAIGN = importlib.util.module_from_spec(_campaign_spec)
assert _campaign_spec.loader is not None
_campaign_spec.loader.exec_module(FACTORY_CAMPAIGN)

PY = sys.executable
GIT = "git"
REAL_WRAPPER = ROOT / launch.SECURE_WRAPPER
WRAPPER_BASENAME = Path(launch.SECURE_WRAPPER).name

# The confined child must never be able to reach the operator's real
# credential store even when the environment names it (deny-by-default; the
# store is never allowlisted).
_REAL_CREDENTIAL_STORE_ENV = "OLLAMA_USAGE_ENV_FILE"

# Namespaces that must never be allowlisted for any role.
FORBIDDEN = wc.FORBIDDEN_WORKSPACE_TOP


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _git(*args: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [GIT, "-C", str(cwd), *args],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


# The confinement probe: a committed workspace script executed *inside* the
# Landlock-confined child.  It reports, as one JSON line on stdout, the
# outcome of every requested probe and the effective HOME/XDG environment,
# so the harness asserts real kernel-enforced outcomes.  Supported ops:
#
# * ``read`` / ``write`` — open the path (``rb`` / ``ab``);
# * ``stat`` — ``os.stat`` the path (used for socket/device-node paths that
#   cannot be opened for reading even unconfined);
# * ``listdir`` — ``os.listdir`` the path (the ``/proc/<pid>/fd`` directory
#   listing needs READ_DIR and is denied under confinement even though a
#   ``/proc/<pid>/fd/N`` symlink dereference would alias an inherited
#   descriptor);
# * ``cmd`` — run ``target["cmd"]`` and record ``returncode``/output
#   (used for the ``git show`` history-read denial probe);
#
# A ``label`` key names the result key deterministically (``op:label``)
# instead of the absolute path, and the ``@self@`` / ``@parent@`` / ``@fd@`` /
# ``@maps@`` / ``@cmdline@`` tokens substitute live ``/proc/<pid>/...``
# paths so self/other/fd credential reads are probed with the child's own
# identity.
PROBE_SOURCE = r'''#!/usr/bin/env python3
import json, os, subprocess, sys
targets = json.loads(sys.argv[1])
out = {}
self_pid = os.getpid()
substitutions = {
    "@self@": "/proc/%d/environ" % self_pid,
    "@cmdline@": "/proc/%d/cmdline" % self_pid,
    "@maps@": "/proc/%d/maps" % self_pid,
    "@fd@": "/proc/%d/fd" % self_pid,
    "@parent@": "/proc/%d/environ" % os.getppid(),
}
for target in targets:
    op = target["op"]
    raw_path = target.get("path")
    path = raw_path
    if path is not None:
        for token, value in substitutions.items():
            path = path.replace(token, value)
        path = os.path.abspath(path)
    label = target.get("label") or path or op
    if op == "read":
        try:
            with open(path, "rb") as stream:
                stream.read(1)
            out["read:%s" % label] = "ok"
        except Exception as exc:
            out["read:%s" % label] = type(exc).__name__
    elif op == "write":
        try:
            with open(path, "ab") as stream:
                stream.write(b"x")
            out["write:%s" % label] = "ok"
        except Exception as exc:
            out["write:%s" % label] = type(exc).__name__
    elif op == "stat":
        try:
            os.stat(path)
            out["stat:%s" % label] = "ok"
        except Exception as exc:
            out["stat:%s" % label] = type(exc).__name__
    elif op == "listdir":
        try:
            os.listdir(path)
            out["listdir:%s" % label] = "ok"
        except Exception as exc:
            out["listdir:%s" % label] = type(exc).__name__
    elif op == "cmd":
        try:
            proc = subprocess.run(
                target["cmd"], stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, timeout=20,
            )
            out["cmd:%s" % label] = {
                "returncode": proc.returncode,
                "stdout": proc.stdout.decode("utf-8", "replace")[:200],
                "stderr": proc.stderr.decode("utf-8", "replace")[:200],
            }
        except Exception as exc:
            out["cmd:%s" % label] = {"error": type(exc).__name__}
out["env"] = {
    key: os.environ.get(key, "")
    for key in ("HOME", "XDG_CONFIG_HOME", "XDG_CACHE_HOME",
                "XDG_DATA_HOME", "XDG_STATE_HOME", "XDG_RUNTIME_DIR")
}
print("FACTORY_CONFINEMENT_PROBE " + json.dumps(out, sort_keys=True))
sys.stdout.flush()
'''


def _probe_targets(*specs: str) -> list:
    """``"read:path"`` / ``"write:path"`` strings -> probe target list."""
    targets = []
    for spec in specs:
        op, path = spec.split(":", 1)
        targets.append({"op": op, "path": path})
    return targets


class _Base(unittest.TestCase):
    """A real committed fixture workspace outside the system-allowlisted roots.

    The workspace must live *outside* ``/tmp`` and ``/nix/store`` (both are
    system-allowlisted and would otherwise make every deny assertion vacuous),
    so it is created under a ROOT-relative private directory and removed at
    teardown.
    """

    def setUp(self) -> None:
        self.diag = Path(tempfile.mkdtemp(prefix="factory-confinement.", dir=str(ROOT)))
        self.addCleanup(shutil.rmtree, self.diag, ignore_errors=True)
        self.workspace = self.diag / "workspace"
        self.workspace.mkdir()
        self._populate_workspace()
        _git("init", "-q", cwd=self.workspace)
        _git("config", "user.email", "factory@test", cwd=self.workspace)
        _git("config", "user.name", "factory", cwd=self.workspace)
        _git("add", "-A", cwd=self.workspace)
        _git("commit", "-qm", "fixture", cwd=self.workspace)
        self.head = _git("rev-parse", "HEAD", cwd=self.workspace).stdout.strip()
        self.assertEqual(len(self.head), 40)
        # An operator credential store *outside* the workspace: the default
        # env-store channel the guard consumes must be bindable by the proof
        # while never being allowlisted.
        self.operator_store = self.diag / "operator" / "ollama-usage-env"
        self.operator_store.parent.mkdir()
        self.operator_store.write_text("export OLLAMA_COOKIE='n=v'\n", encoding="utf-8")
        os.chmod(self.operator_store, 0o600)

    def _populate_workspace(self) -> None:
        ws = self.workspace
        (ws / "scripts").mkdir()
        shutil.copy2(REAL_WRAPPER, ws / "scripts" / WRAPPER_BASENAME)
        # Task 11: every fixture repo commits the exact credential guard so
        # the launch authority can verify and bind the guard source before
        # any child output channel is redacted.
        shutil.copy2(
            ROOT / "scripts" / "credential-guard.py",
            ws / "scripts" / "credential-guard.py",
        )
        (ws / "src").mkdir()
        (ws / "src" / "main.py").write_text("def main(): pass\n", encoding="utf-8")
        # An *existing* developer-allowlisted test artifact directory: the
        # developer write allowlist covers ``src``, so the confined leaf can
        # honestly write markers here and the harness reads them back from
        # the real filesystem (never from self-reported stdout alone).
        (ws / "src" / ".factory-test-output").mkdir()
        (ws / "src" / ".factory-test-output" / "README").write_text(
            "harness marker dir\n", encoding="utf-8"
        )
        (ws / "build-check").mkdir()
        (ws / "build-check" / "placeholder").write_text("b\n", encoding="utf-8")
        (ws / "plan.md").write_bytes((FIXTURES / "plan-valid-base.md").read_bytes())
        (ws / "spec.md").write_text("spec\n", encoding="utf-8")
        (ws / "role.md").write_text("role\n", encoding="utf-8")
        (ws / "AGENTS.md").write_text("agents\n", encoding="utf-8")
        backend = ws / "backend.py"
        backend.write_text("#!/usr/bin/env python3\nprint('ok')\n", encoding="utf-8")
        os.chmod(backend, 0o700)
        probe = ws / "probe.py"
        probe.write_text(PROBE_SOURCE, encoding="utf-8")
        os.chmod(probe, 0o700)
        # Forbidden namespaces, committed so the denial is asserted against
        # real bound-commit workspace content (never against missing files).
        (ws / ".ralph").mkdir()
        (ws / ".ralph" / "secret.txt").write_text("forbidden\n", encoding="utf-8")
        (ws / ".factory-state").mkdir()
        (ws / ".factory-state" / "factory-loop.json").write_text(
            "{}", encoding="utf-8"
        )
        (ws / ".pi").mkdir()
        (ws / ".pi" / "memory.txt").write_text("forbidden\n", encoding="utf-8")
        store = ws / ".ollama-usage-env"
        store.write_text("export OLLAMA_COOKIE='x'\n", encoding="utf-8")
        os.chmod(store, 0o600)
        # Allowlisted ``.factory/`` inputs (base reads + planner schema reads).
        factory = ws / ".factory"
        factory.mkdir()
        (factory / "config.toml").write_text("[x]\n", encoding="utf-8")
        (factory / "environment.toml").write_text("schema_version = 1\n", encoding="utf-8")
        (factory / "artifacts").mkdir()
        (factory / "artifacts" / "implementation-plan.md").write_text(
            "plan\n", encoding="utf-8"
        )
        (factory / "artifacts" / "blocked-facts.json").write_text(
            "{}", encoding="utf-8"
        )
        (factory / "bugs").mkdir()
        (factory / "bugs" / "open.md").write_text("bug\n", encoding="utf-8")
        (factory / "schemas").mkdir()
        (factory / "schemas" / "factory-confinement-v1.schema.json").write_bytes(
            (SCHEMAS / "factory-confinement-v1.schema.json").read_bytes()
        )
        # The exact committed confine-launcher blob (F2) plus prompt files.
        loop_dir = factory / "loop"
        loop_dir.mkdir()
        shutil.copy2(LOOP / "confine_launcher.py", loop_dir / "confine_launcher.py")
        prompts_dir = factory / "prompts"
        prompts_dir.mkdir()
        for role in promptset.ROLES:
            shutil.copy2(PROMPTS / f"{role}.md", prompts_dir / f"{role}.md")

    def _git_ls_files(self, path: str) -> str:
        """The tracked paths under the canonical repo (may be WIP-untracked)."""
        return subprocess.run(
            [GIT, "-C", str(ROOT), "ls-files", path],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        ).stdout

    def binding(self, role: str = "developer", *, provider: str = "synthetic",
                backend: Path | None = None) -> launch.InvocationBinding:
        return launch.InvocationBinding(
            role=role,
            model="synthetic-model",
            provider=provider,
            backend=backend or (self.workspace / "backend.py"),
            workspace=self.workspace,
            bound_commit=self.head,
            role_prompt_digest=sha256((self.workspace / "role.md").read_bytes()),
            prompt_set_digest=sha256(b"campaign-set"),
            plan_digest=sha256((self.workspace / "plan.md").read_bytes()),
            policy_digest=sha256((self.workspace / "AGENTS.md").read_bytes()),
            specification_digest=sha256((self.workspace / "spec.md").read_bytes()),
        )

    def confinement_spec(self, role: str = "developer", **kwargs) -> dict:
        binding = self.binding(role=role, **{k: v for k, v in kwargs.items() if k == "provider"})
        home = wc.sanitized_home_directory()
        self.addCleanup(shutil.rmtree, home, ignore_errors=True)
        spec = wc.confinement_spec(binding, sanitized_home=home)
        wc.validate_confinement_spec(spec, binding)
        return spec

    def run_confined(
        self,
        role: str,
        targets: list,
        *,
        spec: dict | None = None,
    ) -> dict:
        """Exec the real confine launcher against a real spec; probe the child."""
        binding = self.binding(role=role)
        if spec is None:
            home = wc.sanitized_home_directory()
            self.addCleanup(shutil.rmtree, home, ignore_errors=True)
            spec = wc.confinement_spec(binding, sanitized_home=home)
            wc.validate_confinement_spec(spec, binding)
        spec_path = self.diag / "spec.json"
        spec_path.write_text(
            json.dumps(spec, sort_keys=True, separators=(",", ":")),
            encoding="utf-8",
        )
        proc = subprocess.run(
            [
                PY, str(LOOP / "confine_launcher.py"),
                "--spec-file", str(spec_path),
                "--", PY, str(self.workspace / "probe.py"),
                json.dumps(targets),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=str(self.workspace),
            timeout=60,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr.decode()[-2000:])
        line = next(
            (
                text
                for text in proc.stdout.decode("utf-8", "replace").splitlines()
                if text.startswith("FACTORY_CONFINEMENT_PROBE ")
            ),
            None,
        )
        self.assertIsNotNone(line, "the confined probe produced no result line")
        return json.loads(line.split(" ", 1)[1])

    def run_unconfined(self, targets: list) -> dict:
        """The same probe *without* confinement (the denial control)."""
        proc = subprocess.run(
            [PY, str(self.workspace / "probe.py"), json.dumps(targets)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=str(self.workspace),
            timeout=60,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr.decode()[-2000:])
        line = next(
            text
            for text in proc.stdout.decode("utf-8", "replace").splitlines()
            if text.startswith("FACTORY_CONFINEMENT_PROBE ")
        )
        return json.loads(line.split(" ", 1)[1])

    def assertProbe(self, result: dict, op: str, path: str, outcome: str) -> None:
        self.assertEqual(
            result.get(f"{op}:{str(Path(path).absolute())}"),
            outcome,
            f"{op} {path} under confinement: expected {outcome}, got "
            f"{result.get(f'{op}:{str(Path(path).absolute())}')!r}",
        )


# ---------------------------------------------------------------------------
# Real Landlock subprocess matrix (CTX-02, §5.1/§18)
# ---------------------------------------------------------------------------

class LandlockSubprocessMatrixTests(_Base):
    @classmethod
    def setUpClass(cls) -> None:
        if not wc.confinement_primitive_available():
            raise unittest.SkipTest(
                "the Landlock LSM is unavailable on this host; the real "
                "confinement matrix cannot run (fail closed, never simulated)"
            )

    def test_deny_by_default_is_not_vacuous(self) -> None:
        """The same forbidden reads succeed without confinement.

        The denial the confined child observes must come from Landlock, not
        from a missing file: every forbidden path is committed and readable
        by the probe without confinement.
        """
        ws = str(self.workspace)
        targets = _probe_targets(
            f"read:{ws}/.ralph/secret.txt",
            f"read:{ws}/.factory-state/factory-loop.json",
            f"read:{ws}/.ollama-usage-env",
            f"read:{ws}/.factory/loop/confine_launcher.py",
        )
        free = self.run_unconfined(targets)
        for target in targets:
            self.assertEqual(free[f"read:{target['path']}"], "ok",
                             "the fixture path is not readable without "
                             "confinement (vacuous control)")

    def test_developer_denied_by_default(self) -> None:
        ws = str(self.workspace)
        result = self.run_confined(
            "developer",
            _probe_targets(
                f"read:{ws}/.ralph/secret.txt",
                f"read:{ws}/.factory-state/factory-loop.json",
                f"read:{ws}/.pi/memory.txt",
                f"read:{ws}/.ollama-usage-env",
                f"read:{ws}/.factory/loop/confine_launcher.py",
                f"read:{ws}/.factory/prompts/developer.md",
                f"read:{ws}/.factory/tests/__init__.py",
            ),
        )
        for path in (
            f"{ws}/.ralph/secret.txt",
            f"{ws}/.factory-state/factory-loop.json",
            f"{ws}/.pi/memory.txt",
            f"{ws}/.ollama-usage-env",
        ):
            self.assertProbe(result, "read", path, "PermissionError")
        for path in (
            f"{ws}/.factory/loop/confine_launcher.py",
            f"{ws}/.factory/prompts/developer.md",
            f"{ws}/.factory/tests/__init__.py",
        ):
            self.assertNotEqual(
                result.get(f"read:{path}"), "ok",
                f"unallowlisted path {path} was readable under confinement",
            )

    def test_role_read_allowlists_work(self) -> None:
        ws = str(self.workspace)
        for role in ("planner", "developer", "tester", "auditor"):
            with self.subTest(role=role):
                result = self.run_confined(
                    role,
                    _probe_targets(
                        f"read:{ws}/plan.md",
                        f"read:{ws}/spec.md",
                        f"read:{ws}/role.md",
                        f"read:{ws}/AGENTS.md",
                        f"read:{ws}/src/main.py",
                        f"read:{ws}/.factory/config.toml",
                        f"read:{ws}/.factory/artifacts/implementation-plan.md",
                        f"read:{ws}/.factory/bugs/open.md",
                    ),
                )
                for path in (
                    f"{ws}/plan.md", f"{ws}/spec.md", f"{ws}/role.md",
                    f"{ws}/AGENTS.md", f"{ws}/src/main.py",
                    f"{ws}/.factory/config.toml",
                    f"{ws}/.factory/artifacts/implementation-plan.md",
                    f"{ws}/.factory/bugs/open.md",
                ):
                    self.assertProbe(result, "read", path, "ok")

    def test_planner_write_allowlist_is_plan_only(self) -> None:
        ws = str(self.workspace)
        result = self.run_confined(
            "planner",
            _probe_targets(
                f"write:{ws}/.factory/artifacts/implementation-plan.md",
                f"write:{ws}/spec.md",
                f"write:{ws}/src/new.py",
                f"write:{ws}/.ralph/new.txt",
                f"write:{ws}/.factory-state/new.json",
            ),
        )
        self.assertProbe(result, "write",
                         f"{ws}/.factory/artifacts/implementation-plan.md", "ok")
        for path in (f"{ws}/spec.md", f"{ws}/src/new.py",
                     f"{ws}/.ralph/new.txt", f"{ws}/.factory-state/new.json"):
            self.assertNotEqual(
                result.get(f"write:{path}"), "ok",
                f"the planner wrote {path} outside the plan allowlist",
            )

    def test_developer_write_allowlist(self) -> None:
        ws = str(self.workspace)
        result = self.run_confined(
            "developer",
            _probe_targets(
                f"write:{ws}/.factory/artifacts/implementation-plan.md",
                f"write:{ws}/spec.md",
                f"write:{ws}/src/new.py",
                f"write:{ws}/build-check/new.o",
                f"write:{ws}/.ralph/new.txt",
                f"write:{ws}/.ollama-usage-env",
            ),
        )
        for path in (
            f"{ws}/.factory/artifacts/implementation-plan.md",
            f"{ws}/spec.md",
            f"{ws}/src/new.py",
            f"{ws}/build-check/new.o",
        ):
            self.assertProbe(result, "write", path, "ok")
        for path in (f"{ws}/.ralph/new.txt", f"{ws}/.ollama-usage-env"):
            self.assertNotEqual(
                result.get(f"write:{path}"), "ok",
                f"the developer wrote {path} outside the write allowlist",
            )

    def test_tester_write_allowlist_is_build_trees_only(self) -> None:
        ws = str(self.workspace)
        result = self.run_confined(
            "tester",
            _probe_targets(
                f"write:{ws}/build-check/new.o",
                f"write:{ws}/src/new.py",
                f"write:{ws}/.factory/artifacts/implementation-plan.md",
            ),
        )
        self.assertProbe(result, "write", f"{ws}/build-check/new.o", "ok")
        for path in (f"{ws}/src/new.py",
                     f"{ws}/.factory/artifacts/implementation-plan.md"):
            self.assertNotEqual(
                result.get(f"write:{path}"), "ok",
                f"the tester wrote {path} outside the build-tree allowlist",
            )

    def test_auditor_is_read_only(self) -> None:
        ws = str(self.workspace)
        result = self.run_confined(
            "auditor",
            _probe_targets(
                f"write:{ws}/plan.md",
                f"write:{ws}/spec.md",
                f"write:{ws}/src/new.py",
                f"write:{ws}/build-check/new.o",
            ),
        )
        for path in (f"{ws}/plan.md", f"{ws}/spec.md",
                     f"{ws}/src/new.py", f"{ws}/build-check/new.o"):
            self.assertNotEqual(
                result.get(f"write:{path}"), "ok",
                f"the auditor wrote {path} (must be read-only)",
            )

    def test_sanitized_home_environment_and_real_home_denied(self) -> None:
        ws = str(self.workspace)
        real_home = str(Path.home())
        result = self.run_confined(
            "developer",
            _probe_targets(f"read:{real_home}"),
        )
        env = result["env"]
        for key in ("HOME", "XDG_CONFIG_HOME", "XDG_CACHE_HOME",
                    "XDG_DATA_HOME", "XDG_STATE_HOME", "XDG_RUNTIME_DIR"):
            self.assertTrue(
                env[key].startswith("/tmp/factory-home-"),
                f"{key} was not sanitized: {env[key]!r}",
            )
            self.assertFalse(env[key].startswith(real_home),
                             f"{key} leaked the operator's real home")
        self.assertNotEqual(
            result.get(f"read:{real_home}"), "ok",
            "the confined child reached the operator's real home",
        )

    def test_confined_child_cannot_reach_host_credentials(self) -> None:
        """A credential-shaped store named by the environment stays unreachable."""
        ws = str(self.workspace)
        store = self.operator_store
        # The env store is an *existing* operator file outside the workspace;
        # it is never in the confinement allowlist, so the confined child
        # cannot read it even though the parent environment names it.
        targets = _probe_targets(f"read:{store}")
        self.assertEqual(
            self.run_unconfined(targets)[f"read:{store}"], "ok",
            "the operator store control is not readable (vacuous)",
        )
        result = self.run_confined("developer", targets)
        self.assertNotEqual(
            result.get(f"read:{store}"), "ok",
            "the confined child read the operator credential store",
        )

    def test_nested_symlink_escape_target_denied(self) -> None:
        """A nested symlink whose resolved target escapes the workspace is
        denied by the *effective* confinement (finding 1, resolved-target
        containment).

        The symlink sits inside the allowlisted ``src`` directory but points
        at the operator credential store outside the workspace; Landlock
        resolves the target and denies the open because the resolved path is
        not granted.  The same path is readable without confinement, so the
        denial is not vacuous.
        """
        escape = self.workspace / "src" / "escape-link"
        os.symlink(str(self.operator_store), escape)
        targets = _probe_targets(f"read:{escape}")
        self.assertEqual(
            self.run_unconfined(targets)[f"read:{escape}"], "ok",
            "the nested-symlink control is not readable without confinement",
        )
        result = self.run_confined("developer", targets)
        self.assertProbe(result, "read", str(escape), "PermissionError")

    def test_git_and_git_show_denied(self) -> None:
        """The model holds no ``.git`` read and cannot read repository history
        (finding 2): ``.git`` objects/config/reflog are denied and ``git show
        HEAD`` cannot run inside the confined child.  Without confinement the
        same paths are readable and ``git show`` succeeds, so both denials
        are non-vacuous.
        """
        ws = str(self.workspace)
        targets = _probe_targets(
            f"read:{ws}/.git/config",
            f"read:{ws}/.git/HEAD",
            f"read:{ws}/.git/logs/HEAD",
        ) + [
            {"op": "cmd", "cmd": ["git", "show", "HEAD"],
             "label": "git-show"},
        ]
        free = self.run_unconfined(targets)
        for target in targets[:3]:
            self.assertEqual(
                free[f"read:{target['path']}"], "ok",
                f"the .git fixture path {target['path']} is not readable "
                "without confinement (vacuous control)",
            )
        self.assertEqual(
            free["cmd:git-show"]["returncode"], 0,
            f"git show failed without confinement: {free['cmd:git-show']}",
        )
        result = self.run_confined("developer", targets)
        for target in targets[:3]:
            self.assertProbe(result, "read", target["path"], "PermissionError")
        self.assertNotEqual(
            result["cmd:git-show"]["returncode"], 0,
            f"git show read repository history under confinement: "
            f"{result['cmd:git-show']}",
        )

    def test_proc_credential_reads_denied(self) -> None:
        """``/proc`` is not granted (finding 3): self/other ``environ``,
        ``cmdline``, ``maps``, and ``fd`` reads are denied under the
        effective confinement.  The same reads succeed without confinement
        (the probe's own identity), so the denial is not vacuous.
        """
        targets = [
            {"op": "read", "path": "@self@", "label": "proc:self"},
            {"op": "read", "path": "@parent@", "label": "proc:other"},
            {"op": "listdir", "path": "@fd@", "label": "proc:fd"},
            {"op": "read", "path": "@maps@", "label": "proc:maps"},
            {"op": "read", "path": "@cmdline@", "label": "proc:cmdline"},
        ]
        free = self.run_unconfined(targets)
        for key in ("proc:self", "proc:other", "proc:fd", "proc:maps",
                    "proc:cmdline"):
            self.assertEqual(
                free[f"read:{key}"] if key != "proc:fd" else free["listdir:proc:fd"],
                "ok",
                f"the /proc probe {key} is not readable without confinement "
                "(vacuous control)",
            )
        result = self.run_confined("developer", targets)
        for key in ("proc:self", "proc:other", "proc:fd", "proc:maps",
                    "proc:cmdline"):
            self.assertEqual(
                result.get(f"read:{key}") if key != "proc:fd"
                else result.get("listdir:proc:fd"),
                "PermissionError",
                f"/proc credential read {key} succeeded under confinement",
            )

    def test_run_socket_denied_under_effective_confinement(self) -> None:
        """A ``/run/...`` Unix socket is denied under the effective
        confinement (finding 7): the model cannot reach sockets/state under
        the system runtime namespace.

        A Unix socket cannot be opened for reading even without confinement
        (ENXIO), so the non-vacuous control is: without confinement the
        socket is ``stat``-able (and the unconfined open fails with the
        socket-open error, never a Landlock denial); with confinement both
        the ``stat`` and the ``open`` are denied with ``PermissionError``.
        """
        import socket as socket_module

        runtime_dir = os.environ.get("XDG_RUNTIME_DIR") or (
            f"/run/user/{os.getuid()}"
        )
        if not os.path.isdir(runtime_dir) or not os.access(runtime_dir, os.W_OK):
            self.skipTest(
                f"no writable /run runtime directory on this host ({runtime_dir})"
            )
        sock_path = os.path.join(runtime_dir, "factory-confinement-probe.sock")
        self.addCleanup(
            lambda: os.path.exists(sock_path) and os.unlink(sock_path)
        )
        sock = socket_module.socket(socket_module.AF_UNIX)
        try:
            sock.bind(sock_path)
        finally:
            sock.close()
        self.assertTrue(stat.S_ISSOCK(os.stat(sock_path).st_mode))
        targets = [
            {"op": "stat", "path": sock_path, "label": "run-socket:stat"},
            {"op": "read", "path": sock_path, "label": "run-socket:read"},
        ]
        free = self.run_unconfined(targets)
        self.assertEqual(
            free["stat:run-socket:stat"], "ok",
            "the /run socket is not visible without confinement (vacuous)",
        )
        self.assertEqual(
            free["read:run-socket:read"], "OSError",
            "the unconfined socket-open control did not observe the socket-open "
            f"semantics: {free['read:run-socket:read']!r}",
        )
        # ``stat`` is not a Landlock-restricted access (metadata only), so
        # the denial is proven through the *open*: the confined child cannot
        # reach the socket (EACCES) whereas the same open without confinement
        # reaches the socket and fails with the socket-open error (ENXIO).
        result = self.run_confined("developer", targets)
        self.assertEqual(
            result.get("read:run-socket:read"), "PermissionError",
            "the confined child opened a /run socket",
        )

    def test_etc_hostname_denied_under_effective_confinement(self) -> None:
        """Host config under ``/etc`` outside the narrow documented allowlist
        is denied (finding 7): ``/etc/hostname`` is world-readable without
        confinement but denied under the effective confinement.
        """
        hostname = "/etc/hostname"
        self.assertTrue(
            os.path.isfile(hostname),
            "the host-config control path is missing on this host",
        )
        targets = _probe_targets(f"read:{hostname}")
        self.assertEqual(
            self.run_unconfined(targets)[f"read:{hostname}"], "ok",
            "the /etc/hostname control is not readable without confinement "
            "(vacuous)",
        )
        result = self.run_confined("developer", targets)
        self.assertProbe(result, "read", hostname, "PermissionError")


# ---------------------------------------------------------------------------
# Confine-launcher fail-closed behavior
# ---------------------------------------------------------------------------

class ConfineLauncherFailClosedTests(_Base):
    def _run_launcher(self, spec: object, *, as_symlink: bool = False,
                      raw: bytes | None = None) -> subprocess.CompletedProcess[bytes]:
        spec_path = self.diag / "bad-spec.json"
        if raw is not None:
            spec_path.write_bytes(raw)
        else:
            spec_path.write_text(
                json.dumps(spec, sort_keys=True, separators=(",", ":")),
                encoding="utf-8",
            )
        if as_symlink:
            target = self.diag / "real-spec.json"
            target.write_bytes(spec_path.read_bytes())
            spec_path.unlink()
            os.symlink(target, spec_path)
        return subprocess.run(
            [
                PY, str(LOOP / "confine_launcher.py"),
                "--spec-file", str(spec_path),
                "--", PY, "-c", "print('EXECUTED')",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=str(self.workspace),
            timeout=60,
        )

    def _valid_spec(self) -> dict:
        binding = self.binding()
        home = wc.sanitized_home_directory()
        self.addCleanup(shutil.rmtree, home, ignore_errors=True)
        return wc.confinement_spec(binding, sanitized_home=home)

    def test_valid_spec_executes(self) -> None:
        spec_path = self.diag / "ok-spec.json"
        spec_path.write_text(
            json.dumps(self._valid_spec(), sort_keys=True, separators=(",", ":")),
            encoding="utf-8",
        )
        proc = subprocess.run(
            [
                PY, str(LOOP / "confine_launcher.py"),
                "--spec-file", str(spec_path),
                "--", PY, "-c", "print('EXECUTED')",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=str(self.workspace),
            timeout=60,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr.decode()[-1000:])
        self.assertIn(b"EXECUTED", proc.stdout)

    def test_wrong_schema_fails_closed(self) -> None:
        spec = self._valid_spec()
        spec["schema"] = "factory-confinement/v0"
        proc = self._run_launcher(spec)
        self.assertNotEqual(proc.returncode, 0)
        self.assertNotIn(b"EXECUTED", proc.stdout)
        self.assertIn(b"schema", proc.stderr)

    def test_unknown_right_fails_closed(self) -> None:
        spec = self._valid_spec()
        spec["rules"][0]["access"] = ["read", "teleport"]
        proc = self._run_launcher(spec)
        self.assertNotEqual(proc.returncode, 0)
        self.assertNotIn(b"EXECUTED", proc.stdout)
        self.assertIn(b"unknown right", proc.stderr)

    def test_no_rules_fails_closed(self) -> None:
        spec = self._valid_spec()
        spec["rules"] = []
        proc = self._run_launcher(spec)
        self.assertNotEqual(proc.returncode, 0)
        self.assertNotIn(b"EXECUTED", proc.stdout)

    def test_symlinked_spec_fails_closed(self) -> None:
        proc = self._run_launcher(self._valid_spec(), as_symlink=True)
        self.assertNotEqual(proc.returncode, 0)
        self.assertNotIn(b"EXECUTED", proc.stdout)
        self.assertIn(b"spec", proc.stderr)

    def test_oversized_spec_fails_closed(self) -> None:
        proc = self._run_launcher(None, raw=b"x" * (confine_launcher.MAX_SPEC_BYTES + 1))
        self.assertNotEqual(proc.returncode, 0)
        self.assertNotIn(b"EXECUTED", proc.stdout)

    def test_non_json_spec_fails_closed(self) -> None:
        proc = self._run_launcher(None, raw=b"not json")
        self.assertNotEqual(proc.returncode, 0)
        self.assertNotIn(b"EXECUTED", proc.stdout)

    def test_missing_usage_fails_closed(self) -> None:
        proc = subprocess.run(
            [PY, str(LOOP / "confine_launcher.py")],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=str(self.workspace),
            timeout=60,
        )
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn(b"usage", proc.stderr)

    def test_access_bit_tables_agree_with_authority(self) -> None:
        """The child launcher and the production authority share the bit table."""
        self.assertEqual(confine_launcher.RIGHT_BITS, wc.RIGHT_BITS)
        self.assertEqual(
            confine_launcher.CONFINEMENT_SCHEMA, wc.CONFINEMENT_SCHEMA
        )
        self.assertEqual(
            confine_launcher.CONFINEMENT_SCHEMA,
            launch.CONFINEMENT_SPEC_SCHEMA,
        )


# ---------------------------------------------------------------------------
# Confinement specification determinism and validation
# ---------------------------------------------------------------------------

class ConfinementSpecTests(_Base):
    def test_spec_is_deterministic_pure_function(self) -> None:
        binding = self.binding()
        home = wc.sanitized_home_directory()
        self.addCleanup(shutil.rmtree, home, ignore_errors=True)
        # The specification is a pure function of (binding, committed
        # workspace, sanitized home): the same home yields identical bytes.
        first = wc.confinement_spec(binding, sanitized_home=home)
        second = wc.confinement_spec(binding, sanitized_home=home)
        self.assertEqual(first, second)
        self.assertEqual(wc.spec_digest(first), wc.spec_digest(second))
        # The digest is the SHA-256 of the canonical JSON serialization.
        canonical = json.dumps(
            first, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        self.assertEqual(wc.spec_digest(first), sha256(canonical))
        # A different sanitized home (different attempt) binds a different
        # spec and digest, so the proof always binds the exact home applied.
        other_home = wc.sanitized_home_directory()
        self.addCleanup(shutil.rmtree, other_home, ignore_errors=True)
        other = wc.confinement_spec(binding, sanitized_home=other_home)
        self.assertNotEqual(wc.spec_digest(first), wc.spec_digest(other))
        self.assertNotEqual(first["home"], other["home"])

    def test_forbidden_namespaces_never_appear_in_rules(self) -> None:
        workspace_abs = self.workspace.absolute()
        for role in ("planner", "developer", "tester", "auditor"):
            with self.subTest(role=role):
                spec = self.confinement_spec(role)
                for rule in spec["rules"]:
                    path = Path(str(rule["path"])).absolute()
                    if path == workspace_abs or not path.is_relative_to(workspace_abs):
                        continue  # system/tool rules (e.g. /tmp) are not workspace
                    relative = path.relative_to(workspace_abs)
                    # ``.factory`` is deliberately allowlisted for the exact
                    # plan/policy/evidence sidecars below; every *other*
                    # forbidden namespace must never appear in any rule.
                    for forbidden in FORBIDDEN - {".factory"}:
                        self.assertFalse(
                            forbidden in relative.parts,
                            f"rule {path} grants a forbidden namespace "
                            f"({forbidden}) for role {role}",
                        )
                    # Only the documented ``.factory/`` inputs are granted:
                    # the control-plane source, harness tests, prompts, and
                    # runtime state are never allowlisted.
                    for denied in (".factory/loop", ".factory/tests",
                                   ".factory/prompts", ".factory/state",
                                   ".factory/ralph"):
                        self.assertFalse(
                            str(relative).startswith(denied),
                            f"rule {path} grants a control-plane source "
                            f"({denied}) for role {role}",
                        )
                # The plan/spec/product entries and allowlisted factory
                # inputs are present for every role.
                rule_paths = {str(Path(r["path"]).absolute())
                              for r in spec["rules"]}
                for expected in (
                    self.workspace / "plan.md",
                    self.workspace / "spec.md",
                    self.workspace / ".factory" / "config.toml",
                    self.workspace / ".factory" / "artifacts" / "implementation-plan.md",
                ):
                    self.assertIn(str(expected), rule_paths)

    def test_role_write_allowlist_contents(self) -> None:
        plan = str((self.workspace / ".factory" / "artifacts" / "implementation-plan.md").absolute())
        spec = self.confinement_spec("planner")
        workspace_abs = self.workspace.absolute()
        write_paths = {
            str(Path(r["path"]).absolute())
            for r in spec["rules"] if "write" in r["access"]
            and Path(str(r["path"])).absolute().is_relative_to(workspace_abs)
        }
        self.assertEqual(write_paths, {plan})

    def test_developer_write_allowlist_covers_product_entries_and_plan(self) -> None:
        spec = self.confinement_spec("developer")
        write_paths = {
            str(Path(r["path"]).absolute())
            for r in spec["rules"] if "write" in r["access"]
        }
        for expected in (
            self.workspace / "src",
            self.workspace / "plan.md",
            self.workspace / ".factory" / "artifacts" / "implementation-plan.md",
        ):
            self.assertIn(str(expected.absolute()), write_paths)
        self.assertNotIn(
            str((self.workspace / ".ralph").absolute()), write_paths
        )

    def test_tester_write_allowlist_is_build_trees(self) -> None:
        spec = self.confinement_spec("tester")
        write_paths = {
            str(Path(r["path"]).absolute())
            for r in spec["rules"] if "write" in r["access"]
        }
        self.assertIn(str((self.workspace / "build-check").absolute()), write_paths)
        self.assertNotIn(str((self.workspace / "src").absolute()), write_paths)
        self.assertNotIn(str((self.workspace / "plan.md").absolute()), write_paths)

    def test_auditor_has_no_write_allowlist(self) -> None:
        spec = self.confinement_spec("auditor")
        workspace_abs = self.workspace.absolute()
        for rule in spec["rules"]:
            path = Path(str(rule["path"])).absolute()
            if path == workspace_abs or not path.is_relative_to(workspace_abs):
                continue  # system/tool rules (e.g. /tmp) are outside the workspace
            self.assertNotIn("write", rule["access"])
        # The auditor also never gains a workspace write rule through the
        # developer/tester write machinery.
        self.assertFalse(
            any(
                "write" in rule["access"]
                and Path(str(rule["path"])).absolute().is_relative_to(workspace_abs)
                for rule in spec["rules"]
            )
        )

    def test_validate_spec_fails_closed_on_tamper(self) -> None:
        binding = self.binding()
        home = wc.sanitized_home_directory()
        self.addCleanup(shutil.rmtree, home, ignore_errors=True)
        spec = wc.confinement_spec(binding, sanitized_home=home)
        cases = {
            "wrong schema": dict(schema="other/v1"),
            "wrong version": dict(version=2),
            "wrong role": dict(role="oracle"),
            "wrong commit": dict(bound_commit="1" * 40),
            "wrong workspace": dict(workspace=str(self.diag / "elsewhere")),
            "unknown right": dict(
                rules=[{"path": "/x", "access": ["teleport"]}]
            ),
            "no rules": dict(rules=[]),
            "missing path": dict(rules=[{"path": "", "access": ["read"]}]),
        }
        for label, mutation in cases.items():
            with self.subTest(label=label):
                tampered = json.loads(json.dumps(spec))
                tampered.update(mutation)
                with self.assertRaises(wc.ConfinementError):
                    wc.validate_confinement_spec(tampered, binding)

    def test_sanitized_home_is_private_and_xdg_mapped(self) -> None:
        home = wc.sanitized_home_directory()
        self.addCleanup(shutil.rmtree, home, ignore_errors=True)
        self.assertEqual(home.stat().st_mode & 0o777, 0o700)
        for sub in (".config", ".cache", ".local/share", "run"):
            self.assertTrue((home / sub).is_dir())
        env = wc.home_environment(home)
        self.assertEqual(env["HOME"], str(home))
        self.assertEqual(env["XDG_CONFIG_HOME"], str(home / ".config"))
        self.assertEqual(env["XDG_RUNTIME_DIR"], str(home / "run"))

    def test_absent_workspace_entry_is_not_allowlisted(self) -> None:
        # A committed entry removed from the working tree is denied by
        # default: no rule can open a nonexistent path.
        binding = self.binding()
        (self.workspace / "spec.md").unlink()
        home = wc.sanitized_home_directory()
        self.addCleanup(shutil.rmtree, home, ignore_errors=True)
        spec = wc.confinement_spec(binding, sanitized_home=home)
        rule_paths = {str(Path(r["path"]).absolute()) for r in spec["rules"]}
        self.assertNotIn(
            str((self.workspace / "spec.md").absolute()), rule_paths
        )

    def test_external_backend_is_executable_and_readable(self) -> None:
        """An external backend's file + parents are allowlisted read+execute."""
        external = self.diag / "toolchain" / "bin"
        external.mkdir(parents=True)
        tool = external / "synthetic-backend"
        tool.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        os.chmod(tool, 0o700)
        binding = self.binding(backend=tool)
        home = wc.sanitized_home_directory()
        self.addCleanup(shutil.rmtree, home, ignore_errors=True)
        spec = wc.confinement_spec(binding, sanitized_home=home)
        rule_paths = {str(Path(r["path"]).absolute()) for r in spec["rules"]}
        self.assertIn(str(tool.absolute()), rule_paths)
        self.assertIn(str(external.absolute()), rule_paths)

    def test_top_level_symlink_allowlist_escape_fails_closed(self) -> None:
        """A symlink in any top-level allowlist component fails closed
        (finding 1): a workspace symlink whose resolved target points at a
        forbidden path can never become an allowlist entry.

        The symlink is a non-hidden top-level entry, so the allowlist
        construction must reject it outright rather than following it to its
        resolved target (the operator credential store outside the workspace).
        """
        link = self.workspace / "escape-link"
        os.symlink(str(self.operator_store), link)
        binding = self.binding()
        home = wc.sanitized_home_directory()
        self.addCleanup(shutil.rmtree, home, ignore_errors=True)
        with self.assertRaises(wc.ConfinementError) as caught:
            wc.confinement_spec(binding, sanitized_home=home)
        self.assertIn("symlink", str(caught.exception).lower())
        # The same construction path also rejects a top-level symlink whose
        # target stays *inside* the workspace: no symlink component is ever
        # acceptable in an allowlist entry.
        inside = self.workspace / "src" / "main.py"
        os.unlink(link)
        os.symlink(str(inside), link)
        with self.assertRaises(wc.ConfinementError):
            wc.confinement_spec(binding, sanitized_home=home)


class Task10ResultHandoffConfinementTests(_Base):
    """Task 10 production result channels are exact-file Landlock grants."""

    @classmethod
    def setUpClass(cls) -> None:
        if not wc.confinement_primitive_available():
            raise unittest.SkipTest(
                "real Landlock is required for exact result-channel evidence"
            )

    def test_tester_and_auditor_write_only_their_exact_result_file(self) -> None:
        for role, filename in (
            ("tester", "phase-result.json"),
            ("auditor", "audit-result.json"),
        ):
            with self.subTest(role=role):
                state_dir = self.workspace / ".factory-state"
                exact = state_dir / filename
                sibling = state_dir / f"{role}-sibling.json"
                exact.write_bytes(b"{}")
                sibling.write_bytes(b"secret")
                os.chmod(exact, 0o600)
                os.chmod(sibling, 0o600)

                targets = [
                    {"op": "write", "path": str(exact)},
                    {"op": "read", "path": str(sibling)},
                    {"op": "write", "path": str(sibling)},
                    {"op": "write", "path": str(state_dir / "new.json")},
                ]
                control = self.run_unconfined(targets)
                for target in targets:
                    self.assertProbe(control, target["op"], target["path"], "ok")
                (state_dir / "new.json").unlink()
                exact.write_bytes(b"{}")

                binding = self.binding(role=role)
                home = wc.sanitized_home_directory()
                self.addCleanup(shutil.rmtree, home, ignore_errors=True)
                spec = wc.confinement_spec(
                    binding,
                    sanitized_home=home,
                    extra_write=(str(exact),),
                )
                wc.validate_confinement_spec(spec, binding)
                confined = self.run_confined(role, targets, spec=spec)
                self.assertProbe(confined, "write", str(exact), "ok")
                self.assertProbe(
                    confined, "read", str(sibling), "PermissionError"
                )
                self.assertProbe(
                    confined, "write", str(sibling), "PermissionError"
                )
                self.assertProbe(
                    confined,
                    "write",
                    str(state_dir / "new.json"),
                    "PermissionError",
                )
                self.assertEqual(exact.read_bytes(), b"{}x")
                self.assertFalse((state_dir / "new.json").exists())

    def test_extra_write_rejects_missing_directory_symlink_and_foreign(self) -> None:
        binding = self.binding(role="tester")
        home = wc.sanitized_home_directory()
        self.addCleanup(shutil.rmtree, home, ignore_errors=True)
        state_dir = self.workspace / ".factory-state"
        target = state_dir / "target.json"
        target.write_bytes(b"{}")
        link = state_dir / "link.json"
        link.symlink_to(target)
        foreign = self.diag / "foreign.json"
        foreign.write_bytes(b"{}")
        for invalid in (
            state_dir / "missing.json",
            state_dir,
            link,
            foreign,
        ):
            with self.subTest(path=str(invalid)):
                with self.assertRaises(wc.ConfinementError):
                    wc.confinement_spec(
                        binding,
                        sanitized_home=home,
                        extra_write=(str(invalid),),
                    )


# ---------------------------------------------------------------------------
# The real confinement proof (Task 7 review obligations 1/2/3, jointly Task 8)
# ---------------------------------------------------------------------------

class ConfinementProofTests(_Base):
    @classmethod
    def setUpClass(cls) -> None:
        if not wc.confinement_primitive_available():
            raise unittest.SkipTest(
                "the Landlock LSM is unavailable on this host; real proofs "
                "cannot be minted (fail closed, never simulated)"
            )

    def test_proof_binds_invocation_and_spec_digest(self) -> None:
        binding = self.binding()
        home = wc.sanitized_home_directory()
        self.addCleanup(shutil.rmtree, home, ignore_errors=True)
        spec = wc.confinement_spec(binding, sanitized_home=home)
        proof = wc.prove_confinement(binding, confinement_spec=spec)
        self.assertFalse(proof.synthetic)
        self.assertEqual(proof.bound_commit, binding.bound_commit)
        self.assertEqual(proof.workspace, str(self.workspace.absolute()))
        self.assertEqual(proof.provider, binding.provider)
        self.assertEqual(proof.confinement_spec_digest, wc.spec_digest(spec))
        wc.validate_proof(proof, binding, confinement_spec=spec)

    def test_proof_binds_exact_executing_guard_source_bytes(self) -> None:
        """The proof binds the SHA-256 of the executing usage guard modules.

        The digests must equal the SHA-256 of the exact ``usage.py`` /
        ``usage_fetch.py`` bytes that will execute with the model child — an
        operator-claimed or caller-supplied guard source is never accepted.
        """
        binding = self.binding()
        home = wc.sanitized_home_directory()
        self.addCleanup(shutil.rmtree, home, ignore_errors=True)
        spec = wc.confinement_spec(binding, sanitized_home=home)
        proof = wc.prove_confinement(binding, confinement_spec=spec)
        expected = tuple(
            sha256((LOOP / module).read_bytes())
            for module in wc.GUARD_SOURCE_MODULES
        )
        self.assertEqual(proof.guard_source_digests, expected)
        self.assertEqual(
            proof.guard_source_digests, wc._executing_guard_source_digests()
        )

    def test_proof_binds_every_effective_credential_channel(self) -> None:
        """Default env store + explicit cookie file + stdin provenance."""
        binding = self.binding()
        home = wc.sanitized_home_directory()
        self.addCleanup(shutil.rmtree, home, ignore_errors=True)
        spec = wc.confinement_spec(binding, sanitized_home=home)
        cookie = self.diag / "cookie.txt"
        cookie.write_text("n=v\n", encoding="utf-8")
        os.chmod(cookie, 0o600)
        proof = wc.prove_confinement(
            binding,
            confinement_spec=spec,
            cookie_file=str(cookie),
            cookie_stdin=True,
            env_store=str(self.operator_store),
        )
        channels = {c.to_tuple() for c in proof.credential_channels}
        self.assertIn(("env_store", str(self.operator_store)), channels)
        self.assertIn(("cookie_file", str(cookie)), channels)
        self.assertIn(("stdin", None), channels)
        self.assertEqual(len(channels), 3)
        wc.validate_proof(
            proof, binding, confinement_spec=spec,
            cookie_file=str(cookie), cookie_stdin=True,
            env_store=str(self.operator_store),
        )

    def test_default_env_store_channel_is_always_bound(self) -> None:
        """The guard's default operator env store is a consumed channel."""
        binding = self.binding()
        home = wc.sanitized_home_directory()
        self.addCleanup(shutil.rmtree, home, ignore_errors=True)
        spec = wc.confinement_spec(binding, sanitized_home=home)
        with contextlib.ExitStack() as stack:
            stack.enter_context(
                mock.patch.dict(
                    os.environ,
                    {_REAL_CREDENTIAL_STORE_ENV: str(self.operator_store)},
                    clear=False,
                )
            )
            proof = wc.prove_confinement(binding, confinement_spec=spec)
        self.assertEqual(
            proof.credential_channels,
            (wc.CredentialChannel("env_store", str(self.operator_store)),),
        )

    def test_strict_channel_equality_enforced(self) -> None:
        binding = self.binding()
        home = wc.sanitized_home_directory()
        self.addCleanup(shutil.rmtree, home, ignore_errors=True)
        spec = wc.confinement_spec(binding, sanitized_home=home)
        cookie = self.diag / "cookie.txt"
        cookie.write_text("n=v\n", encoding="utf-8")
        os.chmod(cookie, 0o600)
        proof = wc.prove_confinement(
            binding, confinement_spec=spec, cookie_file=str(cookie)
        )
        # The launch path supplies the guard options; a proof bound to a
        # different channel set than the invocation consumes fails closed.
        with self.assertRaises(wc.ConfinementError):
            wc.validate_proof(proof, binding, confinement_spec=spec)
        wc.validate_proof(
            proof, binding, confinement_spec=spec, cookie_file=str(cookie)
        )

    def test_channel_inside_workspace_fails_closed(self) -> None:
        binding = self.binding()
        home = wc.sanitized_home_directory()
        self.addCleanup(shutil.rmtree, home, ignore_errors=True)
        spec = wc.confinement_spec(binding, sanitized_home=home)
        inside = self.workspace / ".ollama-usage-env"
        with self.assertRaises(wc.ConfinementError):
            wc.prove_confinement(
                binding, confinement_spec=spec, env_store=str(inside)
            )

    def test_channel_covered_by_allowlist_fails_closed(self) -> None:
        """A cookie inside an allowlisted product directory cannot be proven
        inaccessible to model tools and therefore fails closed."""
        binding = self.binding()
        home = wc.sanitized_home_directory()
        self.addCleanup(shutil.rmtree, home, ignore_errors=True)
        spec = wc.confinement_spec(binding, sanitized_home=home)
        covered = self.workspace / "src" / "cookie.txt"
        covered.write_text("n=v\n", encoding="utf-8")
        os.chmod(covered, 0o600)
        with self.assertRaises(wc.ConfinementError):
            wc.prove_confinement(
                binding, confinement_spec=spec, cookie_file=str(covered)
            )

    def test_proof_cannot_be_forged_from_operator_claims(self) -> None:
        with self.assertRaises(wc.ConfinementError):
            wc.ConfinementProof(
                bound_commit=self.head,
                workspace=str(self.workspace),
                provider="synthetic",
                guard_source_digests=("0" * 64, "1" * 64),
                credential_channels=(),
                confinement_spec_digest="0" * 64,
                synthetic=False,
                _mint=object(),
            )

    def test_validate_rejects_wrong_commit_workspace_provider(self) -> None:
        binding = self.binding()
        home = wc.sanitized_home_directory()
        self.addCleanup(shutil.rmtree, home, ignore_errors=True)
        spec = wc.confinement_spec(binding, sanitized_home=home)
        proof = wc.prove_confinement(binding, confinement_spec=spec)
        other = launch.InvocationBinding(
            role=binding.role, model=binding.model, provider=binding.provider,
            backend=binding.backend, workspace=binding.workspace,
            bound_commit="1" * 40, role_prompt_digest=binding.role_prompt_digest,
            prompt_set_digest=binding.prompt_set_digest,
            plan_digest=binding.plan_digest, policy_digest=binding.policy_digest,
            specification_digest=binding.specification_digest,
        )
        with self.assertRaises(wc.ConfinementError):
            wc.validate_proof(proof, other, confinement_spec=spec)

    def test_guard_source_digest_substitution_rejected(self) -> None:
        """A caller-supplied guard-source digest can never match execution."""
        binding = self.binding()
        home = wc.sanitized_home_directory()
        self.addCleanup(shutil.rmtree, home, ignore_errors=True)
        spec = wc.confinement_spec(binding, sanitized_home=home)
        proof = wc.prove_confinement(binding, confinement_spec=spec)
        forged = wc.ConfinementProof(
            bound_commit=proof.bound_commit,
            workspace=proof.workspace,
            provider=proof.provider,
            guard_source_digests=("0" * 64, "0" * 64),
            credential_channels=proof.credential_channels,
            confinement_spec_digest=proof.confinement_spec_digest,
            synthetic=False,
            _mint=wc._PROOF_MINT_SECRET,
        )
        with self.assertRaises(wc.ConfinementError):
            wc.validate_proof(forged, binding, confinement_spec=spec)

    def test_synthetic_proof_never_satisfies_real_authority(self) -> None:
        """The Task 7 synthetic seam is never evidence of real confinement.

        A synthetic proof token (``confinement._mint_synthetic_proof``) is a
        different token type that the real authority rejects outright, and
        even the real type minted synthetically never binds a specification
        digest and so can never satisfy a production launch.
        """
        binding = self.binding()
        home = wc.sanitized_home_directory()
        self.addCleanup(shutil.rmtree, home, ignore_errors=True)
        spec = wc.confinement_spec(binding, sanitized_home=home)
        synthetic = confinement._mint_synthetic_proof(binding)
        with self.assertRaises(wc.ConfinementError):
            wc.validate_proof(synthetic, binding, confinement_spec=spec)
        # The real authority's own private seam likewise can never satisfy a
        # production re-validation against the exact specification: a
        # synthetic proof carries no Landlock claim and no bound digest.
        with self.assertRaises(wc.ConfinementError):
            wc.validate_proof(
                wc._mint_synthetic_proof(binding), binding,
                confinement_spec=spec,
            )
        # The production authority never mints a synthetic proof.
        self.assertFalse(
            wc.prove_confinement(binding, confinement_spec=spec).synthetic
        )

    def test_landlock_primitive_is_real(self) -> None:
        self.assertTrue(wc.confinement_primitive_available())
        self.assertGreaterEqual(wc._landlock_abi(), 1)
        wc.require_confinement_primitive()  # must not raise


# ---------------------------------------------------------------------------
# Production launch integration (real spec -> real proof -> confined exec)
# ---------------------------------------------------------------------------

class ProductionLaunchConfinementTests(_Base):
    @classmethod
    def setUpClass(cls) -> None:
        if not wc.confinement_primitive_available():
            raise unittest.SkipTest(
                "the Landlock LSM is unavailable on this host; the production "
                "confinement launch path cannot run (fail closed)"
            )

    def _authorize(self, binding, *, spec=None, home=None, **kwargs):
        if home is None:
            home = wc.sanitized_home_directory()
            self.addCleanup(shutil.rmtree, home, ignore_errors=True)
        if spec is None:
            spec = wc.confinement_spec(binding, sanitized_home=home)
        return launch.authorize_launch(
            binding,
            role_prompt=(self.workspace / "role.md").read_bytes(),
            agents=(self.workspace / "AGENTS.md").read_bytes(),
            spec=(self.workspace / "spec.md").read_bytes(),
            plan=(self.workspace / "plan.md").read_bytes(),
            _confinement_spec=spec,
            _sanitized_home=home,
            **kwargs,
        )

    def test_authorize_with_real_spec_mints_real_proof(self) -> None:
        binding = self.binding(role="planner")
        authority = self._authorize(binding)
        self.assertIsInstance(authority, launch.LaunchAuthority)
        self.assertIsNotNone(authority._confinement_spec)
        proof = authority._confinement_proof
        self.assertIsNotNone(proof)
        self.assertFalse(proof.synthetic)
        self.assertEqual(
            proof.confinement_spec_digest,
            wc.spec_digest(authority._confinement_spec),
        )
        self.assertIsNotNone(authority._confined_launcher)
        self.assertTrue(authority._confined_launcher.is_file())
        self.assertEqual(
            sha256(authority._confined_launcher.read_bytes()),
            sha256((LOOP / "confine_launcher.py").read_bytes()),
        )

    def test_spec_without_sanitized_home_fails_closed(self) -> None:
        binding = self.binding(role="planner")
        home = wc.sanitized_home_directory()
        self.addCleanup(shutil.rmtree, home, ignore_errors=True)
        spec = wc.confinement_spec(binding, sanitized_home=home)
        with self.assertRaises(launch.InvocationError) as caught:
            launch.authorize_launch(
                binding,
                role_prompt=b"r", agents=b"a", spec=b"sp", plan=b"pl",
                _confinement_spec=spec,
            )
        self.assertIn("sanitized home", str(caught.exception))

    def test_ollama_authorization_never_runs_quota(self) -> None:
        """Ollama still requires a real proof but launch runs no quota policy."""
        binding = self.binding(role="planner", provider="ollama")
        with mock.patch.object(launch.usage_guard, "require_quota") as quota:
            authority = self._authorize(binding)
        quota.assert_not_called()
        self.assertFalse(authority._confinement_proof.synthetic)

    def test_ollama_without_real_confinement_fails_closed(self) -> None:
        """A caller cannot claim a spec without its real proof (fail closed)."""
        binding = self.binding(role="planner", provider="ollama")
        with self.assertRaises(launch.InvocationError) as caught:
            launch.authorize_launch(
                binding,
                role_prompt=b"r", agents=b"a", spec=b"sp", plan=b"pl",
            )
        self.assertIn("confinement", str(caught.exception).lower())

    def test_supervisor_revalidates_proof_before_exec(self) -> None:
        """A token whose proof binds a different spec is refused at run time."""
        binding = self.binding(role="planner")
        authority = self._authorize(binding)
        # Tamper the token's spec *after* the mint: the proof digest no
        # longer matches what the supervisor would apply, so run() must fail
        # closed before any spawn.
        authority._confinement_spec["role"] = "auditor"
        supervisor = launch.LaunchSupervision(binding, kill_grace=0.3)
        with self.assertRaises(launch.SupervisionError) as caught:
            supervisor.run(authority)
        self.assertIn("confinement", str(caught.exception).lower())
        self.assertIsNone(supervisor._child, "no child may be spawned")

    def test_sibling_launch_private_paths_denied(self) -> None:
        """A sibling launch's private directories are never granted to another
        launch (finding 4): launch A's exec-staging, prompt, session, and
        sanitized-home paths are denied to launch B's confined child, even
        though both live under the shared temporary directory.
        """
        binding = self.binding(role="planner")
        # Mint authority A with its own sanitized home and a real proof.
        home_a = wc.sanitized_home_directory()
        self.addCleanup(shutil.rmtree, home_a, ignore_errors=True)
        spec_a = wc.confinement_spec(binding, sanitized_home=home_a)
        authority_a = launch.authorize_launch(
            binding,
            role_prompt=(self.workspace / "role.md").read_bytes(),
            agents=(self.workspace / "AGENTS.md").read_bytes(),
            spec=(self.workspace / "spec.md").read_bytes(),
            plan=(self.workspace / "plan.md").read_bytes(),
            _confinement_spec=spec_a,
            _sanitized_home=home_a,
        )
        self.assertFalse(authority_a._confinement_proof.synthetic)
        sibling_dirs = [
            authority_a._exec_dir,
            Path(authority_a._prompt_path).parent,
            authority_a._session_dir,
            home_a,
        ]
        for path in sibling_dirs:
            self.addCleanup(shutil.rmtree, path, ignore_errors=True)
        # B gets its own private home and spec; B's allowlist never grants A's
        # paths, and there is no broad /tmp grant (finding 4).
        home_b = wc.sanitized_home_directory()
        self.addCleanup(shutil.rmtree, home_b, ignore_errors=True)
        spec_b = wc.confinement_spec(binding, sanitized_home=home_b)
        rule_paths = {Path(str(rule["path"])).absolute()
                      for rule in spec_b["rules"]}
        for path in sibling_dirs:
            self.assertNotIn(
                path.absolute(), rule_paths,
                f"sibling launch A's private path {path} leaked into B's spec",
            )
        self.assertNotIn(Path("/tmp"), rule_paths,
                         "the model received a broad /tmp grant")
        # Unconfined control: A's private paths are real and accessible.
        targets = [
            {"op": "read", "path": str(authority_a._prompt_path),
             "label": "sibling-prompt"},
            {"op": "read", "path": str(authority_a._exec_dir / "pi2-secure-exec.py"),
             "label": "sibling-staging"},
            {"op": "write", "path": str(authority_a._session_dir / "probe"),
             "label": "sibling-session"},
            {"op": "write", "path": str(home_a / "probe"),
             "label": "sibling-home"},
        ]
        free = self.run_unconfined(targets)
        for key in ("read:sibling-prompt", "read:sibling-staging",
                    "write:sibling-session", "write:sibling-home"):
            self.assertEqual(
                free[key], "ok",
                f"sibling path control {key} is not accessible without "
                "confinement (vacuous)",
            )
        # B's confined child cannot touch A's private paths.
        result = self.run_confined("developer", targets, spec=spec_b)
        for key in ("read:sibling-prompt", "read:sibling-staging",
                    "write:sibling-session", "write:sibling-home"):
            self.assertEqual(
                result.get(key), "PermissionError",
                f"launch B reached sibling launch A's private path {key}",
            )

    def test_every_provider_and_direct_api_requires_real_confinement(self) -> None:
        """Real confinement is mandatory for every provider and every public
        authorize API, CLI or programmatic (finding 5): a direct programmatic
        authorize carrying neither the real specification nor the explicit
        private synthetic seam fails closed for every provider.
        """
        for provider in ("synthetic", "ollama"):
            with self.subTest(provider=provider):
                binding = self.binding(role="planner", provider=provider)
                with self.assertRaises(launch.InvocationError) as caught:
                    launch.authorize_launch(
                        binding,
                        role_prompt=b"r", agents=b"a", spec=b"sp", plan=b"pl",
                    )
                self.assertIn(
                    "confinement", str(caught.exception).lower(),
                    f"provider {provider} authorized without confinement",
                )
        # A real specification without the sanitized home also fails closed.
        binding = self.binding(role="planner")
        home = wc.sanitized_home_directory()
        self.addCleanup(shutil.rmtree, home, ignore_errors=True)
        spec = wc.confinement_spec(binding, sanitized_home=home)
        with self.assertRaises(launch.InvocationError) as caught:
            launch.authorize_launch(
                binding,
                role_prompt=b"r", agents=b"a", spec=b"sp", plan=b"pl",
                _confinement_spec=spec,
            )
        self.assertIn("sanitized home", str(caught.exception).lower())

    def test_direct_api_with_real_spec_mints_real_proof(self) -> None:
        """The programmatic (non-CLI) authorize API applies real confinement
        and mints a real (never synthetic) proof for every provider — the
        confinement is not CLI-only (finding 5).
        """
        for provider in ("synthetic", "ollama"):
            with self.subTest(provider=provider):
                binding = self.binding(role="planner", provider=provider)
                authority = self._authorize(binding)
                self.assertIsInstance(authority, launch.LaunchAuthority)
                proof = authority._confinement_proof
                self.assertIsNotNone(
                    proof, f"provider {provider} got no confinement proof"
                )
                self.assertFalse(
                    proof.synthetic,
                    f"provider {provider} was satisfied by a synthetic proof",
                )
                # Re-validate against the exact channels the invocation
                # consumed (the strict channel equality the launch path uses).
                wc.validate_proof(
                    proof, binding,
                    confinement_spec=authority._confinement_spec,
                )

    def test_authorize_failure_cleans_four_private_dirs(self) -> None:
        """Any authorization failure removes every per-launch private
        directory the mint created (finding 6): the exec-staging directory,
        the prompt directory, the session directory, and the sanitized home
        — so no private or credential material survives a failed
        authorization.
        """
        binding = self.binding(role="planner")
        tracked: dict = {}
        for name, prefix in (
            ("exec_dir", "factory-loop-exec-"),
            ("prompt_dir", "factory-loop-launch-"),
            ("session_dir", "factory-loop-session-"),
        ):
            path = Path(tempfile.mkdtemp(prefix=prefix, dir="/tmp"))
            os.chmod(path, 0o700)
            tracked[name] = path
            self.addCleanup(shutil.rmtree, path, ignore_errors=True)
        home = wc.sanitized_home_directory()
        self.addCleanup(shutil.rmtree, home, ignore_errors=True)
        good = wc.confinement_spec(binding, sanitized_home=home)
        # A spec that cannot bind the invocation fails the mint *after* the
        # private paths exist (staging, prompt, session, home).
        tampered = dict(good, workspace=str(self.diag / "elsewhere"))
        with mock.patch.object(launch, "_exec_staging_dir",
                               return_value=tracked["exec_dir"]), \
             mock.patch.object(launch, "_prompt_directory",
                               return_value=tracked["prompt_dir"]), \
             mock.patch.object(launch, "_session_directory",
                               return_value=tracked["session_dir"]):
            with self.assertRaises(launch.InvocationError) as caught:
                launch.authorize_launch(
                    binding,
                    role_prompt=(self.workspace / "role.md").read_bytes(),
                    agents=(self.workspace / "AGENTS.md").read_bytes(),
                    spec=(self.workspace / "spec.md").read_bytes(),
                    plan=(self.workspace / "plan.md").read_bytes(),
                    _confinement_spec=tampered,
                    _sanitized_home=home,
                )
            self.assertIn("confinement", str(caught.exception).lower())
        for path in (*tracked.values(), home):
            self.assertFalse(
                path.exists(),
                f"private directory {path} survived a failed authorization",
            )

    def test_cli_runs_leaf_through_staged_confine_launcher(self) -> None:
        """The full CLI runs the model child through the confine launcher.

        The synthetic model backend writes its evidence honestly: marker files
        under the *existing developer-allowlisted* test artifact directory
        ``src/.factory-test-output/`` (covered by the developer write
        allowlist), recording its effective environment and its forbidden-path
        probe results.  The harness reads those markers back from the real
        filesystem after the run — never from a self-reported stdout claim
        alone — so the CLI evidence stays a real confinement proof (the CLI
        mints the real Landlock proof internally; no synthetic proof is ever
        passed to the production path).
        """
        backend = self.workspace / "backend.py"
        backend.write_text(
            "#!/usr/bin/env python3\n"
            "import json, os, sys\n"
            "sys.stdin.buffer.read()\n"
            "ws = os.environ.get('FACTORY_LOOP_LAUNCH_WORKSPACE', '.')\n"
            "out_dir = os.path.join(ws, 'src', '.factory-test-output')\n"
            "env = {'HOME': os.environ.get('HOME', ''), "
            "'XDG_CONFIG_HOME': os.environ.get('XDG_CONFIG_HOME', '')}\n"
            "probe = {}\n"
            "for rel in ('.ralph/secret.txt', '.factory-state/factory-loop.json', "
            "'.ollama-usage-env'):\n"
            "    try:\n"
            "        with open(os.path.join(ws, rel), 'rb') as f:\n"
            "            f.read(1)\n"
            "        probe[rel] = 'ok'\n"
            "    except OSError as exc:\n"
            "        probe[rel] = type(exc).__name__\n"
            "# The leaf's own write proof: a marker under the developer-"
            "writable artifact dir.  If the write is denied under confinement,"
            "no marker appears and the harness fails the test.\n"
            "try:\n"
            "    os.makedirs(out_dir, exist_ok=True)\n"
            "    with open(os.path.join(out_dir, 'leaf.marker'), 'w', "
            "encoding='utf-8') as f:\n"
            "        json.dump({'env': env, 'probe': probe}, f, sort_keys=True)\n"
            "    marker = 'ok'\n"
            "except OSError as exc:\n"
            "    marker = type(exc).__name__\n"
            "print('FACTORY_CONFINEMENT_PROBE ' + "
            "json.dumps({'marker': marker, 'env': env, 'probe': probe}, "
            "sort_keys=True))\n"
            "sys.stdout.flush()\n",
            encoding="utf-8",
        )
        os.chmod(backend, 0o700)
        _git("add", "backend.py", cwd=self.workspace)
        _git("commit", "-qm", "probe backend", cwd=self.workspace)
        self.head = _git("rev-parse", "HEAD", cwd=self.workspace).stdout.strip()
        binding = self.binding()
        plan = self.workspace / "plan.md"
        _, excerpt_digest = launch.derive_task_excerpt(plan.read_bytes(), 1)
        argv = [
            "launch",
            "--root", str(self.workspace),
            "--role", "developer",
            "--model", "synthetic-model",
            "--provider", "synthetic",
            "--backend", str(backend),
            "--role-prompt", str(self.workspace / "role.md"),
            "--role-prompt-digest", sha256(b"role\n"),
            "--prompt-set-digest", sha256(b"set"),
            "--policy", str(self.workspace / "AGENTS.md"),
            "--policy-digest", sha256(b"agents\n"),
            "--spec", str(self.workspace / "spec.md"),
            "--spec-digest", sha256(b"spec\n"),
            "--plan", str(plan),
            "--plan-digest", sha256(plan.read_bytes()),
            "--bound-commit", self.head,
            "--allowed-tools", "read,bash",
            "--runtime-limit", "30",
            "--inactivity-limit", "20",
            "--task-id", "1",
            "--task-excerpt-digest", excerpt_digest,
        ]
        out = io.StringIO()
        err = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            status = launch.main(argv)
        self.assertEqual(status, 0, err.getvalue()[-2000:])
        payload = json.loads(out.getvalue())
        self.assertEqual(payload["outcome"], "completed")
        self.assertEqual(payload["returncode"], 0)
        # The honest evidence: markers written by the leaf under the
        # developer-allowlisted artifact dir, read back from the real
        # filesystem (never asserted from the leaf's own stdout alone).
        marker_path = (
            self.workspace / "src" / ".factory-test-output" / "leaf.marker"
        )
        self.assertTrue(
            marker_path.is_file(),
            "the leaf wrote no marker under the allowlisted artifact dir; "
            "the write allowlist did not hold under real confinement",
        )
        marker = json.loads(marker_path.read_text(encoding="utf-8"))
        self.assertTrue(
            marker["env"]["HOME"].startswith("/tmp/factory-home-"),
            f"the leaf ran with an unsanitized HOME: {marker['env']['HOME']!r}",
        )
        self.assertEqual(marker["env"]["XDG_CONFIG_HOME"],
                         marker["env"]["HOME"] + "/.config")
        for rel in (".ralph/secret.txt", ".factory-state/factory-loop.json",
                    ".ollama-usage-env"):
            self.assertNotEqual(
                marker["probe"].get(rel), "ok",
                f"the production leaf read {rel} under real confinement",
            )
            self.assertEqual(
                marker["probe"].get(rel), "PermissionError",
                f"the production leaf was not denied {rel} with the expected "
                f"Landlock error: {marker['probe'].get(rel)!r}",
            )
        # The supervisor result corroborates the same outcome through the
        # machine-readable channel (the marker is the primary evidence).
        tail = payload["stdout"]["tail"]
        line = next(
            (text for text in tail.splitlines()
             if text.startswith("FACTORY_CONFINEMENT_PROBE ")),
            None,
        )
        self.assertIsNotNone(
            line,
            "the production leaf produced no confinement-probe line",
        )
        probe = json.loads(line.split(" ", 1)[1])
        self.assertEqual(probe["marker"], "ok")
        for rel in (".ralph/secret.txt", ".factory-state/factory-loop.json",
                    ".ollama-usage-env"):
            self.assertEqual(probe["probe"].get(rel), "PermissionError")


# ---------------------------------------------------------------------------
# Role-prompt set: fixed committed bytes and deterministic digests
# ---------------------------------------------------------------------------

class RolePromptSetTests(unittest.TestCase):
    def test_four_static_roles_with_distinct_prompts(self) -> None:
        self.assertEqual(promptset.ROLES, ("planner", "developer", "tester", "auditor"))
        prompts = {role: promptset.read_role_prompt(role) for role in promptset.ROLES}
        self.assertEqual(set(prompts), set(promptset.ROLES))
        # Distinct roles must carry distinct prompt bytes.
        self.assertEqual(len({sha256(data) for data in prompts.values()}), 4)

    def test_prompt_files_are_fixed_bytes_at_canonical_path(self) -> None:
        for role in promptset.ROLES:
            with self.subTest(role=role):
                path = PROMPTS / f"{role}.md"
                self.assertTrue(path.is_file(), f"missing role prompt {path}")
                self.assertNotIn(b"\x00", path.read_bytes())
                self.assertGreater(len(path.read_bytes()), 100)
                # The digest is the SHA-256 of the exact canonical bytes.
                self.assertEqual(
                    promptset.role_prompt_digest(role), sha256(path.read_bytes())
                )

    def test_prompt_set_digest_is_deterministic_and_binds_all_four(self) -> None:
        first = promptset.prompt_set_digest()
        second = promptset.prompt_set_digest()
        self.assertEqual(first, second)
        digest = hashlib.sha256()
        for role in sorted(promptset.ROLES):
            digest.update(role.encode("utf-8"))
            digest.update(b"\x00")
            digest.update(promptset.read_role_prompt(role))
            digest.update(b"\x00")
        self.assertEqual(first, digest.hexdigest())
        # The prompt-set digest depends on every role prompt: substituting a
        # single byte changes it.
        real_read = promptset.read_role_prompt
        with mock.patch.object(
            promptset, "read_role_prompt",
            side_effect=lambda role: (
                real_read(role) if role != "developer"
                else real_read(role) + b"X"
            ),
        ):
            self.assertNotEqual(promptset.prompt_set_digest(), first)

    def test_read_role_prompt_fails_closed_on_unsafe_prompt(self) -> None:
        fake = self._fake_prompts_dir()
        # A symlinked final component is refused (no-follow anchored reads).
        with mock.patch.object(
            promptset, "prompts_directory", return_value=fake,
        ):
            with self.assertRaises(promptset.PromptSetError):
                promptset.read_role_prompt("planner")
            # An oversized prompt is refused.
            (fake / "tester.md").write_bytes(
                b"x" * (promptset.MAX_PROMPT_BYTES + 1)
            )
            with self.assertRaises(promptset.PromptSetError):
                promptset.read_role_prompt("tester")

    def _fake_prompts_dir(self) -> Path:
        fake = Path(tempfile.mkdtemp(prefix="factory-prompts-fake."))
        self.addCleanup(shutil.rmtree, fake, ignore_errors=True)
        (fake / "developer.md").write_text("prompt\n", encoding="utf-8")
        (fake / "tester.md").write_text("prompt\n", encoding="utf-8")
        os.symlink(fake / "developer.md", fake / "planner.md")
        return fake

    def test_unknown_role_fails_closed(self) -> None:
        with self.assertRaises(promptset.PromptSetError):
            promptset.role_prompt_file("oracle")
        with self.assertRaises(promptset.PromptSetError):
            promptset.role_prompt_digest("oracle")

    def test_prompt_cli_is_machine_readable(self) -> None:
        out = io.StringIO()
        err = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            status = promptset._cli(["role-digest", "--role", "planner"])
        self.assertEqual(status, 0, err.getvalue())
        self.assertEqual(
            out.getvalue().strip(),
            promptset.role_prompt_digest("planner"),
        )


# ---------------------------------------------------------------------------
# Audit-objective registry: fixed bytes and deterministic selection (§6.4)
# ---------------------------------------------------------------------------

class AuditObjectiveRegistryTests(unittest.TestCase):
    def test_registry_is_fixed_bytes_and_parses(self) -> None:
        path = ROOT / ".factory" / "audit-objectives" / "registry.json"
        self.assertTrue(path.is_file())
        data, document = audit_objectives.load_registry(ROOT)
        objectives = audit_objectives.objectives_list(document)
        self.assertEqual(
            [entry["id"] for entry in objectives],
            [f"AUD-{i:02d}" for i in range(1, len(objectives) + 1)],
        )
        self.assertEqual(
            audit_objectives.registry_digest(data), sha256(path.read_bytes())
        )

    def test_selection_is_deterministic_per_round(self) -> None:
        _, document = audit_objectives.load_registry(ROOT)
        count = len(audit_objectives.objectives_list(document))
        # Round 1 -> AUD-01; round N -> AUD-N; round N+1 wraps to AUD-01.
        self.assertEqual(
            audit_objectives.select_audit_objective(1, document)["id"], "AUD-01"
        )
        self.assertEqual(
            audit_objectives.select_audit_objective(count, document)["id"],
            f"AUD-{count:02d}",
        )
        self.assertEqual(
            audit_objectives.select_audit_objective(count + 1, document)["id"],
            "AUD-01",
        )
        for round_number in (1, 7, count, count + 1):
            self.assertEqual(
                audit_objectives.select_audit_objective(round_number, document),
                audit_objectives.select_audit_objective(round_number, document),
            )

    def test_non_positive_round_fails_closed(self) -> None:
        _, document = audit_objectives.load_registry(ROOT)
        for bad in (0, -1, True, 1.5):
            with self.subTest(bad=bad):
                with self.assertRaises(audit_objectives.AuditObjectiveError):
                    audit_objectives.select_audit_objective(bad, document)  # type: ignore[arg-type]

    def test_malformed_registries_fail_closed(self) -> None:
        base = json.loads(
            (ROOT / ".factory" / "audit-objectives" / "registry.json").read_bytes()
        )
        cases = {
            "wrong schema": dict(schema="other/v1"),
            "duplicate id": dict(objectives=[
                dict(id="AUD-01", title="a", objective="x"),
                dict(id="AUD-01", title="b", objective="y"),
            ]),
            "non-sequential": dict(objectives=[
                dict(id="AUD-01", title="a", objective="x"),
                dict(id="AUD-03", title="b", objective="y"),
            ]),
            "extra field": dict(objectives=[
                dict(id="AUD-01", title="a", objective="x", extra=1),
            ]),
            "bad id": dict(objectives=[
                dict(id="AUD-1", title="a", objective="x"),
            ]),
            "empty objective": dict(objectives=[
                dict(id="AUD-01", title="a", objective=" "),
            ]),
        }
        for label, mutation in cases.items():
            with self.subTest(label=label):
                payload = json.loads(json.dumps(base))
                payload.update(mutation)
                with self.assertRaises(audit_objectives.AuditObjectiveError):
                    audit_objectives.parse_registry(
                        json.dumps(payload).encode("utf-8")
                    )

    def test_read_registry_is_bounded_no_follow(self) -> None:
        fake = Path(tempfile.mkdtemp(prefix="factory-audit-registry."))
        self.addCleanup(shutil.rmtree, fake, ignore_errors=True)
        registry = fake / "registry.json"
        registry.write_text("{}", encoding="utf-8")
        os.symlink(registry, fake / "link.json")
        with self.assertRaises(audit_objectives.AuditObjectiveError):
            audit_objectives.read_registry_bytes(fake / "link.json")

    def test_registry_cli_selection_is_deterministic(self) -> None:
        out = io.StringIO()
        err = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            status = audit_objectives._cli(["select", "--round", "1"])
        self.assertEqual(status, 0, err.getvalue())
        self.assertEqual(json.loads(out.getvalue())["id"], "AUD-01")


# ---------------------------------------------------------------------------
# Exported API and docs schema
# ---------------------------------------------------------------------------

class ApiAndSchemaTests(unittest.TestCase):
    def test_launch_exports_confinement_constants(self) -> None:
        for name in ("CONFINE_LAUNCHER", "CONFINEMENT_SPEC_SCHEMA"):
            self.assertIn(name, launch.__all__)
            self.assertTrue(getattr(launch, name))
        self.assertEqual(launch.CONFINE_LAUNCHER, ".factory/loop/confine_launcher.py")
        self.assertEqual(launch.CONFINEMENT_SPEC_SCHEMA, "factory-confinement/v1")

    def test_confinement_schema_doc_covers_real_spec_surface(self) -> None:
        path = SCHEMAS / "factory-confinement-v1.schema.json"
        self.assertTrue(path.is_file())
        document = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(document["$id"], "factory-confinement/v1")
        self.assertEqual(document["type"], "object")
        required = set(document["required"])
        self.assertTrue(
            {"schema", "version", "role", "provider", "workspace",
             "bound_commit", "backend", "home", "env", "rules"} <= required
        )
        # A real per-role specification's surface is exactly the schema's.
        # (Built against the committed fixture workspace via a lightweight
        # binding; only the schema's property surface is asserted here.)
        properties = set(document["properties"])
        for key in ("schema", "version", "role", "provider", "workspace",
                    "bound_commit", "backend", "home", "env", "rules"):
            self.assertIn(key, properties)
        self.assertEqual(
            set(document["properties"]["rules"]["items"]["properties"]["access"]
                ["items"]["enum"]),
            {"read", "write", "execute"},
        )
        # The real specification surface is a subset of the documented schema:
        # every key the authority emits is documented (no undocumented drift).
        binding = launch.InvocationBinding(
            role="planner", model="m", provider="synthetic",
            backend=Path("/bin/true"), workspace=ROOT,
            bound_commit="0" * 40,
            role_prompt_digest=sha256(b"r"), prompt_set_digest=sha256(b"s"),
            plan_digest=sha256(b"p"), policy_digest=sha256(b"a"),
            specification_digest=sha256(b"sp"),
        )
        import tempfile as _tempfile

        home = wc.sanitized_home_directory()
        self.addCleanup(shutil.rmtree, home, ignore_errors=True)
        spec = wc.confinement_spec(binding, sanitized_home=home)
        self.assertTrue(set(spec) <= properties)
        for rule in spec["rules"]:
            self.assertTrue(set(rule) <= {"path", "access"})
            self.assertTrue(
                set(rule["access"]) <= {"read", "write", "execute"}
            )

    def test_audit_objectives_schema_doc_is_present(self) -> None:
        path = SCHEMAS / "audit-objectives-v1.schema.json"
        self.assertTrue(path.is_file())
        document = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(document["schema"], "audit-objectives/v1")

    def test_public_launch_surface_has_no_synthetic_proof_option(self) -> None:
        """The public CLI cannot be satisfied by a synthetic proof.

        ``_confinement_proof``/``_usage_guard_html_file``/``_usage_guard_allow_loopback``
        are private authority seams; the public launch surface carries only
        the real-confinement spec path and no synthetic-proof opt-in.
        """
        parameters = inspect.signature(launch.authorize_launch).parameters
        self.assertIn("_confinement_spec", parameters)
        self.assertNotIn("confinement_spec", parameters)
        self.assertNotIn("confinement_proof", parameters)
        self.assertNotIn("synthetic", parameters)


if __name__ == "__main__":
    unittest.main(verbosity=2)
