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
  probes prove real confinement at the production boundary; a raw
  ``clone(CLONE_UNTRACED)`` backend is denied, clone3 is denied with the safe
  ENOSYS fallback where the kernel supports it, and bounded return leaves no
  survivor or later workspace
  mutation;
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
import ctypes
import dataclasses
import errno
import fcntl
import hashlib
import importlib.util
import inspect
import io
import json
import os
from pathlib import Path
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
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

PY = os.path.realpath(sys.executable)
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
import ctypes, json, os, subprocess, sys
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
out["no_new_privs"] = ctypes.CDLL(None).prctl(39, 0, 0, 0, 0)
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
        # Task 11: every fixture repo commits the exact model-side Pi guard
        # extension so the launch authority can verify and always load it
        # through ``--extension`` in the child argv.
        shutil.copy2(
            ROOT / "scripts" / "pi-factory-guard-extension.mjs",
            ws / "scripts" / "pi-factory-guard-extension.mjs",
        )
        (ws / "scripts" / "pi-cli-shims").mkdir()
        shutil.copy2(
            ROOT / "scripts" / "pi-cli-shims" / "git",
            ws / "scripts" / "pi-cli-shims" / "git",
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
        # If the adopting project commits a Nix shell, keep the fixture's
        # toolchain closure bound to that exact expression. Generic projects
        # are not required to provide ``shell.nix``.
        if (ROOT / "shell.nix").is_file():
            shutil.copy2(ROOT / "shell.nix", ws / "shell.nix")
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
        for module in ("confine_launcher.py", "usage.py", "usage_fetch.py"):
            shutil.copy2(LOOP / module, loop_dir / module)
        factory_tests = factory / "tests"
        factory_tests.mkdir()
        (factory_tests / "__init__.py").write_text("# verifier fixture\n", encoding="utf-8")
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

    def _run_confine_launcher(
        self,
        spec: object,
        spec_path: Path,
        command: list[str],
        *,
        exec_fd_placeholders: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[bytes]:
        """Run with production descriptor transport plus an optional fd contract.

        A descriptor-sensitive probe must not enumerate ``/proc/self/fd`` from
        inside Landlock merely to discover the protected executable table.
        When placeholders are requested, this test fixture passes one owned
        high-numbered ``/dev/null`` descriptor to make the broker's table base
        deterministic, derives the exact production-approved path ordering,
        and substitutes each requested path's exact protected slot into the
        child argv.  The extra descriptor controls only allocation geometry;
        the production launcher still opens, protects, and brokers every exec
        descriptor normally.
        """
        descriptors: tuple[int, ...] = ()
        contract_fd: int | None = None
        try:
            if isinstance(spec, dict):
                opened: list[int] = []
                try:
                    for rule in spec.get("rules", []):
                        descriptor = wc._open_path_anchor(
                            str(rule["path"]), "test confinement rule"
                        )
                        if wc._descriptor_identity(descriptor) != rule["identity"]:
                            os.close(descriptor)
                            raise wc.ConfinementError("test rule identity mismatch")
                        opened.append(descriptor)
                    descriptors = tuple(opened)
                except (KeyError, TypeError, wc.ConfinementError):
                    for descriptor in opened:
                        os.close(descriptor)
                    descriptors = ()
            inherited = descriptors
            if exec_fd_placeholders:
                self.assertIsInstance(spec, dict)
                approved = confine_launcher._approved_exec_targets(spec, descriptors)
                try:
                    approved_paths = sorted(
                        path for path, _descriptor in approved.values()
                    )
                finally:
                    for _path, descriptor in approved.values():
                        os.close(descriptor)
                source_fd = os.open(
                    "/dev/null", os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
                )
                try:
                    # Leave room for every approved source descriptor that
                    # the fresh launcher opens before reserving its protected
                    # table. A fixed +64 leaked the previous executable-set
                    # size into this fixture and became order-dependent when
                    # the exact project-shell closure admitted more tools.
                    contract_fd = fcntl.fcntl(
                        source_fd,
                        fcntl.F_DUPFD,
                        max([127, *descriptors]) + len(approved_paths) + 64,
                    )
                finally:
                    os.close(source_fd)
                # ``contract_fd`` is the highest inherited/open descriptor.
                # The production table starts 32 slots above that exact number.
                slots = {
                    path: contract_fd + 32 + index
                    for index, path in enumerate(approved_paths)
                }
                for placeholder, path in exec_fd_placeholders.items():
                    self.assertIn(path, slots, f"no approved exec slot for {path}")
                    command = [
                        argument.replace(placeholder, str(slots[path]))
                        for argument in command
                    ]
                inherited = (*descriptors, contract_fd)
            fd_text = ",".join(str(fd) for fd in descriptors) or "999999"
            return subprocess.run(
                [
                    PY, str(LOOP / "confine_launcher.py"),
                    "--spec-file", str(spec_path),
                    "--rule-fds", fd_text,
                    "--", *command,
                ],
                pass_fds=inherited,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=str(self.workspace),
                timeout=60,
            )
        finally:
            if contract_fd is not None:
                try:
                    os.close(contract_fd)
                except OSError:
                    pass
            for descriptor in descriptors:
                try:
                    os.close(descriptor)
                except OSError:
                    pass

    def run_confined(
        self,
        role: str,
        targets: list,
        *,
        spec: dict | None = None,
        exec_fd_placeholders: dict[str, str] | None = None,
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
        proc = self._run_confine_launcher(
            spec,
            spec_path,
            [PY, str(self.workspace / "probe.py"), json.dumps(targets)],
            exec_fd_placeholders=exec_fd_placeholders,
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

    def test_tester_auditor_read_factory_implementation_without_write(self) -> None:
        ws = str(self.workspace)
        for role in ("tester", "auditor"):
            with self.subTest(role=role):
                result = self.run_confined(role, _probe_targets(
                    f"read:{ws}/.factory/loop/confine_launcher.py",
                    f"read:{ws}/.factory/tests/__init__.py",
                    f"write:{ws}/.factory/loop/confine_launcher.py",
                    f"write:{ws}/.factory/tests/__init__.py",
                ))
                self.assertProbe(
                    result, "read",
                    f"{ws}/.factory/loop/confine_launcher.py", "ok",
                )
                self.assertProbe(
                    result, "read", f"{ws}/.factory/tests/__init__.py", "ok",
                )
                self.assertProbe(
                    result, "write",
                    f"{ws}/.factory/loop/confine_launcher.py", "PermissionError",
                )
                self.assertProbe(
                    result, "write", f"{ws}/.factory/tests/__init__.py",
                    "PermissionError",
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

    def test_unicode_symlink_escape_target_denied(self) -> None:
        """Unicode path names do not weaken no-follow containment checks."""
        escape = self.workspace / "src" / "Stéphanie"
        os.symlink(str(self.operator_store), escape)
        targets = [{"label": "unicode-symlink", "path": str(escape), "op": "read"}]
        free = self.run_unconfined(targets)["read:unicode-symlink"]
        self.assertEqual(free, "ok")
        confined = self.run_confined("developer", targets)["read:unicode-symlink"]
        self.assertIn("permission", confined.lower())

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

    def test_absolute_copied_loader_and_interpreted_git_execution_denied(self) -> None:
        """Only exact approved executable inodes cross the kernel boundary.

        Direct Git, copied Git/helper ELF in a model-writable directory, an
        explicit dynamic-loader invocation, generated/sourced script, and an
        interpreter subprocess are denied. Ordinary approved executables still
        work with the loader readable-only.
        """
        real_git = os.path.realpath(shutil.which("git") or "")
        real_helper = os.path.realpath(sys.executable)
        self.assertTrue(real_git and os.path.isfile(real_git))
        self.assertTrue(real_helper and os.path.isfile(real_helper))
        loader = ""
        with open("/proc/self/maps", encoding="utf-8") as stream:
            for line in stream:
                mapped = line.rsplit(" ", 1)[-1].strip()
                if ("ld-linux" in mapped or "ld-musl" in mapped) and os.path.isfile(mapped):
                    loader = os.path.realpath(mapped)
                    break
        self.assertTrue(loader, "cannot locate the executing dynamic loader")
        copied_git = self.workspace / "src" / "copied-git"
        copied_helper = self.workspace / "src" / "copied-helper"
        shutil.copy2(real_git, copied_git)
        shutil.copy2(real_helper, copied_helper)
        copied_git.chmod(0o700)
        copied_helper.chmod(0o700)
        generated = self.workspace / "src" / "generated-git.sh"
        generated.write_text(
            "#!/bin/sh\n" + real_git + " --version\n", encoding="utf-8"
        )
        generated.chmod(0o700)
        commands = [
            {"op": "cmd", "cmd": [real_git, "--version"], "label": "absolute-git"},
            {"op": "cmd", "cmd": [str(copied_git), "--version"], "label": "copied-git"},
            {"op": "cmd", "cmd": [str(copied_helper), "--version"], "label": "copied-helper"},
            {"op": "cmd", "cmd": [loader, real_git, "--version"], "label": "loader-git"},
            {"op": "cmd", "cmd": ["sh", str(generated)], "label": "generated-script"},
            {"op": "cmd", "cmd": ["sh", "-c", '. "$1"', "sh", str(generated)],
             "label": "sourced-script"},
            {"op": "cmd", "cmd": [PY, "-c",
             "import subprocess,sys;sys.exit(subprocess.run(sys.argv[1:]).returncode)",
             real_git, "--version"], "label": "interpreter-subprocess"},
        ]
        free = self.run_unconfined(commands)
        for command in commands:
            self.assertEqual(free[f"cmd:{command['label']}"]["returncode"], 0)
        confined = self.run_confined("developer", commands)
        approved = self.run_confined("developer", [
            {"op": "cmd", "cmd": [PY, "-c", "pass"], "label": "approved-python"},
        ])
        self.assertEqual(
            approved["cmd:approved-python"]["returncode"], 0,
            approved["cmd:approved-python"],
        )
        for command in commands:
            result = confined[f"cmd:{command['label']}"]
            self.assertTrue(
                result.get("returncode", 0) != 0 or result.get("error"),
                f"real Git executed through {command['label']}: {result}",
            )
        self.assertEqual(confined["no_new_privs"], 1)

    def test_high_word_fd_aliases_cannot_replace_protected_exec_slot(self) -> None:
        """Kernel-low-32-bit fd aliases cannot retarget an approved exec.

        The production launcher reserves the approved Python inode in its
        retained exec table.  Five raw syscalls then name that exact slot as
        ``(1 << 32) | slot``.  Linux converts fd/range parameters to 32 bits,
        so a filter that compared the full seccomp register would miss these
        aliases.  Each attack tries to replace/close/CLOEXEC-mark the slot
        with the dynamic loader, then requests approved Python with argv that
        makes only the loader execute a copied workspace ELF.  The protected
        inode must remain unchanged and the copied ELF marker must never run.
        """
        loader = ""
        with open("/proc/self/maps", encoding="utf-8") as stream:
            for line in stream:
                mapped = line.rsplit(" ", 1)[-1].strip()
                if (
                    ("ld-linux" in mapped or "ld-musl" in mapped)
                    and os.path.isfile(mapped)
                ):
                    loader = os.path.realpath(mapped)
                    break
        self.assertTrue(loader, "cannot locate the executing dynamic loader")
        copied_python = self.workspace / "src" / "high-fd-copy"
        shutil.copy2(PY, copied_python)
        copied_python.chmod(0o700)
        marker_prefix = self.workspace / "src" / ".factory-test-output" / "high-fd"
        probe = r'''
import ctypes, errno, fcntl, json, os, platform, sys
slot_text, approved, loader, copied, marker_prefix = sys.argv[1:]
slot = int(slot_text)
machine = platform.machine().lower()
if machine in ("x86_64", "amd64"):
    numbers = {"close": 3, "fcntl": 72, "dup2": 33, "dup3": 292,
               "close_range": 436, "execve": 59}
elif machine in ("aarch64", "arm64"):
    numbers = {"close": 57, "fcntl": 25, "dup3": 24,
               "close_range": 436, "execve": 221}
else:
    raise SystemExit(80)
libc = ctypes.CDLL(None, use_errno=True)
identity = os.stat(approved)
try:
    original = os.fstat(slot)
except OSError:
    raise SystemExit(81)
if (original.st_dev, original.st_ino) != (identity.st_dev, identity.st_ino):
    raise SystemExit(81)
operations = ["close", "fcntl", "dup3", "close_range"]
if "dup2" in numbers:
    operations.insert(2, "dup2")

def syscall(number, *arguments):
    ctypes.set_errno(0)
    result = libc.syscall(
        ctypes.c_long(number),
        *(ctypes.c_ulonglong(value) for value in arguments),
    )
    return int(result), ctypes.get_errno()

reports = []
for kind in operations:
    read_fd, write_fd = os.pipe()
    pid = os.fork()
    if pid == 0:
        os.close(read_fd)
        loader_fd = os.open(
            loader,
            getattr(os, "O_PATH", os.O_RDONLY) | getattr(os, "O_CLOEXEC", 0),
        )
        high_alias = (1 << 32) | slot
        refill = None
        if kind == "close":
            result, error = syscall(numbers[kind], high_alias)
            if result == 0:
                refill, _ = syscall(
                    numbers["fcntl"], loader_fd, fcntl.F_DUPFD, slot
                )
        elif kind == "fcntl":
            result, error = syscall(
                numbers[kind], high_alias, fcntl.F_SETFD, fcntl.FD_CLOEXEC
            )
        elif kind == "dup2":
            result, error = syscall(numbers[kind], loader_fd, high_alias)
        elif kind == "dup3":
            result, error = syscall(numbers[kind], loader_fd, high_alias, 0)
        else:
            result, error = syscall(numbers[kind], high_alias, high_alias, 0)
            if result == 0:
                refill, _ = syscall(
                    numbers["fcntl"], loader_fd, fcntl.F_DUPFD, slot
                )
        try:
            current = os.fstat(slot)
            same_inode = (
                current.st_dev, current.st_ino
            ) == (original.st_dev, original.st_ino)
        except OSError:
            same_inode = False
        report = {
            "kind": kind, "result": result, "errno": error,
            "refill": refill, "same_inode": same_inode,
        }
        os.write(write_fd, (json.dumps(report) + "\n").encode())
        os.close(write_fd)
        marker = marker_prefix + "-" + kind
        code = ("open(" + repr(marker) + ", 'w').write('EXECUTED')").encode()
        argv = (ctypes.c_char_p * 5)(
            loader.encode(), copied.encode(), b"-c", code, None
        )
        envp = (ctypes.c_char_p * 1)(None)
        syscall(
            numbers["execve"],
            ctypes.cast(ctypes.c_char_p(approved.encode()), ctypes.c_void_p).value,
            ctypes.cast(argv, ctypes.c_void_p).value,
            ctypes.cast(envp, ctypes.c_void_p).value,
        )
        os._exit(82)
    os.close(write_fd)
    payload = b""
    while True:
        chunk = os.read(read_fd, 4096)
        if not chunk:
            break
        payload += chunk
    os.close(read_fd)
    os.waitpid(pid, 0)
    reports.append(json.loads(payload))

expected = set(operations)
if {report["kind"] for report in reports} != expected:
    raise SystemExit(83)
for report in reports:
    if not report["same_inode"]:
        raise SystemExit(84)
    if report["kind"] == "close_range":
        if report["result"] != 0 or report["refill"] == slot:
            raise SystemExit(85)
    elif report["result"] != -1 or report["errno"] != errno.EACCES:
        raise SystemExit(86)
for kind in operations:
    if os.path.exists(marker_prefix + "-" + kind):
        raise SystemExit(87)
print("HIGH_FD_SLOTS_PROTECTED")
'''
        command = [{
            "op": "cmd",
            "cmd": [
                PY, "-c", probe, "@factory-test-python-exec-fd@", PY,
                loader, str(copied_python), str(marker_prefix),
            ],
            "label": "high-word-fd-aliases",
        }]
        confined = self.run_confined(
            "developer",
            command,
            exec_fd_placeholders={"@factory-test-python-exec-fd@": PY},
        )["cmd:high-word-fd-aliases"]
        self.assertEqual(confined.get("returncode"), 0, confined)
        self.assertIn("HIGH_FD_SLOTS_PROTECTED", confined.get("stdout", ""))
        for kind in ("close", "fcntl", "dup2", "dup3", "close_range"):
            self.assertFalse(
                Path(f"{marker_prefix}-{kind}").exists(),
                f"the copied workspace ELF executed after {kind}",
            )

    def test_concurrent_symlink_swap_never_authorizes_exec(self) -> None:
        """A pathname race cannot borrow an approved inode then execute a copy."""
        approved_true = os.path.realpath(shutil.which("true") or "")
        copied = self.workspace / "src" / "race-copy"
        shutil.copy2(sys.executable, copied)
        copied.chmod(0o700)
        link = self.workspace / "src" / "race-exec"
        race = r'''
import os, subprocess, sys, threading
link, approved, copied = sys.argv[1:]
try:
    os.unlink(link)
except FileNotFoundError:
    pass
os.symlink(approved, link)
stop = False
def swap():
    index = 0
    while not stop:
        temporary = link + ".next"
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        os.symlink(approved if index & 1 else copied, temporary)
        os.replace(temporary, link)
        index += 1
thread = threading.Thread(target=swap)
thread.start()
succeeded = False
try:
    for _ in range(200):
        try:
            result = subprocess.run([link, "--version"], stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL, timeout=5)
            succeeded = succeeded or result.returncode == 0
        except (OSError, subprocess.SubprocessError):
            pass
finally:
    stop = True
    thread.join()
sys.exit(1 if succeeded else 0)
'''
        command = [{
            "op": "cmd", "cmd": [PY, "-c", race, str(link), approved_true, str(copied)],
            "label": "symlink-swap",
        }]
        free = self.run_unconfined(command)["cmd:symlink-swap"]
        self.assertNotEqual(free["returncode"], 0, free)
        confined = self.run_confined("developer", command)["cmd:symlink-swap"]
        self.assertEqual(confined.get("returncode"), 0, confined)

    def test_shared_buffer_approved_path_loader_race_never_executes_copy(self) -> None:
        """A second thread cannot retarget a validated exec buffer to ld-linux.

        The approved selector is the immutable Python inode, but its argv makes
        Python reject the copied ELF.  The alternate selector is the dynamic
        loader, which would run that copied ELF successfully.  Thus exit 0 is
        an unambiguous loader-race escape.  Unconfined execution observes the
        escape; under confinement every attempt is denied or executes the
        descriptor-bound Python inode and no attempt exits 0.
        """
        loader = ""
        with open("/proc/self/maps", encoding="utf-8") as stream:
            for line in stream:
                mapped = line.rsplit(" ", 1)[-1].strip()
                if ("ld-linux" in mapped or "ld-musl" in mapped) and os.path.isfile(mapped):
                    loader = os.path.realpath(mapped)
                    break
        self.assertTrue(loader, "cannot locate the executing dynamic loader")
        copied_python = self.workspace / "src" / "shared-buffer-copy"
        shutil.copy2(PY, copied_python)
        copied_python.chmod(0o700)
        probe = r'''
import ctypes, os, platform, sys, threading
approved, loader, copied = sys.argv[1:]
libc = ctypes.CDLL(None, use_errno=True)
execve_nr = 59 if platform.machine().lower() in ("x86_64", "amd64") else 221
size = max(len(approved), len(loader)) + 2

def one_attempt():
    pid = os.fork()
    if pid == 0:
        buffer = ctypes.create_string_buffer(size)
        address = ctypes.addressof(buffer)
        stop = False
        ready = threading.Event()
        def write(value):
            raw = value.encode() + b"\0"
            ctypes.memmove(address, raw, len(raw))
        def mutate():
            nonlocal stop
            ready.set()
            while not stop:
                write(approved)
                write(loader)
        write(approved)
        thread = threading.Thread(target=mutate)
        thread.start(); ready.wait()
        argv = (ctypes.c_char_p * 5)(
            loader.encode(), copied.encode(), b"-c", b"pass", None
        )
        envp = (ctypes.c_char_p * 1)(None)
        libc.syscall(execve_nr, ctypes.c_void_p(address), argv, envp)
        stop = True; thread.join()
        os._exit(93)
    _pid, status = os.waitpid(pid, 0)
    return os.WIFEXITED(status) and os.WEXITSTATUS(status) == 0

escaped = any(one_attempt() for _ in range(40))
sys.exit(1 if escaped else 0)
'''
        command = [{
            "op": "cmd",
            "cmd": [PY, "-c", probe, PY, loader, str(copied_python)],
            "label": "shared-buffer-loader-race",
        }]
        free = self.run_unconfined(command)["cmd:shared-buffer-loader-race"]
        self.assertNotEqual(free.get("returncode"), 0, free)
        confined = self.run_confined("developer", command)[
            "cmd:shared-buffer-loader-race"
        ]
        self.assertEqual(confined.get("returncode"), 0, confined)

    def test_relative_procfd_and_execveat_forms_denied(self) -> None:
        """Only canonical absolute execve is in policy; execveat is all-deny."""
        approved_true = os.path.realpath(shutil.which("true") or "")
        probe = r'''
import ctypes, errno, os, platform, sys
path = sys.argv[1]
libc = ctypes.CDLL(None, use_errno=True)
execveat_nr = 322 if platform.machine().lower() in ("x86_64", "amd64") else 281
AT_FDCWD, AT_EMPTY_PATH = -100, 0x1000
argv = (ctypes.c_char_p * 2)(path.encode(), None)
envp = (ctypes.c_char_p * 1)(None)
def wait_ok(pid):
    _pid, status = os.waitpid(pid, 0)
    return os.WIFEXITED(status) and os.WEXITSTATUS(status) == 0
def denied_execve(value, cwd=None):
    pid = os.fork()
    if pid == 0:
        try:
            if cwd is not None:
                os.chdir(cwd)
            os.execve(value, [value], {})
        except OSError as exc:
            os._exit(0 if exc.errno in (errno.EACCES, errno.EPERM) else 90)
        os._exit(91)
    return wait_ok(pid)
def denied_execveat(dirfd, value, flags):
    pid = os.fork()
    if pid == 0:
        ctypes.set_errno(0)
        result = libc.syscall(execveat_nr, dirfd, ctypes.c_char_p(value),
                              argv, envp, flags)
        error = ctypes.get_errno()
        os._exit(0 if result == -1 and error in (errno.EACCES, errno.EPERM) else 92)
    return wait_ok(pid)
fd = os.open(path, getattr(os, "O_PATH", os.O_RDONLY))
dirfd = os.open(os.path.dirname(path), getattr(os, "O_PATH", os.O_RDONLY))
checks = [
    denied_execve("./" + os.path.basename(path), os.path.dirname(path)),
    denied_execve("/proc/self/fd/%d" % fd),
    denied_execveat(AT_FDCWD, path.encode(), 0),
    denied_execveat(dirfd, os.path.basename(path).encode(), 0),
    denied_execveat(fd, b"", AT_EMPTY_PATH),
]
os.close(dirfd); os.close(fd)
sys.exit(0 if all(checks) else 1)
'''
        result = self.run_confined("developer", [{
            "op": "cmd", "cmd": [PY, "-c", probe, approved_true],
            "label": "unsafe-exec-forms",
        }])["cmd:unsafe-exec-forms"]
        self.assertEqual(result.get("returncode"), 0, result)

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
        git_show = result["cmd:git-show"]
        self.assertTrue(
            git_show.get("returncode", 0) != 0 or git_show.get("error"),
            f"git show read repository history under confinement: {git_show}",
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
    @unittest.skipUnless(
        confine_launcher.platform.machine().lower() in ("x86_64", "amd64"),
        "compat int 0x80/x32 probes are x86_64-specific",
    )
    def test_seccomp_kills_compat_int80_and_x32_before_dispatch(self) -> None:
        """The BPF architecture prologue kills both x86 alternate ABIs."""
        import mmap

        # mov eax, __NR_getpid; int 0x80/syscall; ret. If either call returns,
        # the child exits 88 and the regression fails; correct filters SIGSYS.
        probes = {
            "int80": b"\xb8\x14\x00\x00\x00\xcd\x80\xc3",
            "x32": b"\xb8\x27\x00\x00\x40\x0f\x05\xc3",
        }
        for label, machine_code in probes.items():
            with self.subTest(label=label):
                pid = os.fork()
                if pid == 0:
                    region = mmap.mmap(
                        -1, mmap.PAGESIZE,
                        prot=mmap.PROT_READ | mmap.PROT_WRITE | mmap.PROT_EXEC,
                    )
                    region.write(machine_code)
                    address = ctypes.addressof(ctypes.c_char.from_buffer(region))
                    function = ctypes.CFUNCTYPE(ctypes.c_long)(address)
                    confine_launcher._set_no_new_privs()
                    confine_launcher._install_exec_trace_filter()
                    function()
                    os._exit(88)
                _waited, status = os.waitpid(pid, 0)
                self.assertTrue(os.WIFSIGNALED(status), status)
                self.assertEqual(os.WTERMSIG(status), signal.SIGSYS)

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
        return self._run_confine_launcher(
            spec, spec_path, [PY, "-c", "print('EXECUTED')"]
        )

    def _valid_spec(self) -> dict:
        binding = self.binding()
        home = wc.sanitized_home_directory()
        self.addCleanup(shutil.rmtree, home, ignore_errors=True)
        return wc.confinement_spec(binding, sanitized_home=home)

    def test_valid_spec_executes(self) -> None:
        spec_path = self.diag / "ok-spec.json"
        spec = self._valid_spec()
        spec_path.write_text(
            json.dumps(spec, sort_keys=True, separators=(",", ":")),
            encoding="utf-8",
        )
        proc = self._run_confine_launcher(
            spec, spec_path, [PY, "-c", "print('EXECUTED')"]
        )
        self.assertEqual(proc.returncode, 0, proc.stderr.decode()[-1000:])
        self.assertIn(b"EXECUTED", proc.stdout)

    def test_ordinary_threads_and_process_creation_remain_traced(self) -> None:
        """clone3 denial still permits libc's ordinary legacy-clone fallback."""
        spec_path = self.diag / "ordinary-clone-spec.json"
        spec = self._valid_spec()
        spec_path.write_text(
            json.dumps(spec, sort_keys=True, separators=(",", ":")),
            encoding="utf-8",
        )
        script = (
            "import os,threading\n"
            "seen=[]\n"
            "thread=threading.Thread(target=lambda: seen.append('thread'))\n"
            "thread.start();thread.join()\n"
            "pid=os.fork()\n"
            "if pid == 0: os._exit(0)\n"
            "waited,status=os.waitpid(pid,0)\n"
            "assert seen == ['thread'] and waited == pid and os.WIFEXITED(status)\n"
            "print('ORDINARY_CLONE_OK')\n"
        )
        proc = self._run_confine_launcher(spec, spec_path, [PY, "-c", script])
        self.assertEqual(proc.returncode, 0, proc.stderr.decode()[-1000:])
        self.assertIn(b"ORDINARY_CLONE_OK", proc.stdout)

    def test_released_target_pgid_is_never_used_for_signal_forwarding(self) -> None:
        """A reused target PGID is excluded after ptrace releases identity."""
        group_calls: list[tuple[int, int]] = []
        identity_calls: list[tuple[int, int]] = []
        target = 500
        pinned_tracees = {501, 502}
        foreign_reuser = 700
        with mock.patch.object(
            confine_launcher.os, "killpg",
            side_effect=lambda pgid, signum: group_calls.append((pgid, signum)),
        ), mock.patch.object(
            confine_launcher.os, "kill",
            side_effect=lambda pid, signum: identity_calls.append((pid, signum)),
        ):
            confine_launcher._forward_broker_signal(
                target, pinned_tracees, True, signal.SIGTERM
            )
            confine_launcher._forward_broker_signal(
                target, pinned_tracees, False, signal.SIGHUP
            )

        self.assertEqual(group_calls, [(target, signal.SIGTERM)])
        self.assertEqual(
            identity_calls,
            [(501, signal.SIGHUP), (502, signal.SIGHUP)],
        )
        self.assertNotIn(
            (target, signal.SIGHUP), group_calls,
            "the released numeric target/PGID was signaled after reuse",
        )
        self.assertNotIn(
            foreign_reuser, {pid for pid, _signum in identity_calls},
            "a foreign member of the reused group was not ptrace-pinned",
        )

    def test_broker_bounds_and_reaps_setsid_descendant_after_target_exit(self) -> None:
        """A tracee that outlives the target cannot hold the launcher open."""
        spec_path = self.diag / "descendant-spec.json"
        spec = self._valid_spec()
        spec_path.write_text(
            json.dumps(spec, sort_keys=True, separators=(",", ":")),
            encoding="utf-8",
        )
        marker = self.workspace / "src" / ".factory-test-output" / "broker-child.pid"
        script = (
            "import os,signal,subprocess,sys\n"
            "sink=open('src/.factory-test-output/broker-child.log','wb')\n"
            "child=subprocess.Popen([sys.executable,'-c',"
            "'import os,signal,time;signal.signal(signal.SIGTERM,signal.SIG_IGN);'"
            "'os.setsid();time.sleep(300)'],stdin=sink,stdout=sink,stderr=sink)\n"
            f"open({str(marker)!r},'w').write(str(child.pid))\n"
            "sink.close()\n"
        )
        started = time.monotonic()
        pid: int | None = None
        try:
            proc = self._run_confine_launcher(
                spec, spec_path, [PY, "-c", script]
            )
            elapsed = time.monotonic() - started
            self.assertEqual(proc.returncode, 0, proc.stderr.decode()[-1000:])
            self.assertLess(
                elapsed, 10.0,
                "the ptrace broker waited for an escaped descendant instead "
                "of bounded-terminating it",
            )
            self.assertTrue(marker.is_file(), "the descendant probe never started")
            pid = int(marker.read_text(encoding="utf-8"))
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline and os.path.exists(f"/proc/{pid}"):
                time.sleep(0.02)
            self.assertFalse(
                os.path.exists(f"/proc/{pid}"),
                f"the ptrace broker did not reap escaped descendant {pid}",
            )
        finally:
            if pid is None and marker.is_file():
                pid = int(marker.read_text(encoding="utf-8"))
            if pid is not None:
                try:
                    os.kill(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass

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

    def test_file_substitution_cannot_change_retained_anchor(self) -> None:
        """Same-name regular-file replacement never becomes the granted inode."""
        binding = self.binding(role="planner")
        home = wc.sanitized_home_directory()
        self.addCleanup(shutil.rmtree, home, ignore_errors=True)
        descriptors: list[int] = []
        spec = wc.confinement_spec(
            binding, sanitized_home=home, _rule_descriptors=descriptors
        )
        self.addCleanup(lambda: [os.close(fd) for fd in descriptors])
        target = self.workspace / "plan.md"
        target.unlink()
        target.write_text("substituted\n", encoding="utf-8")
        with self.assertRaises(wc.ConfinementError) as caught:
            wc.validate_rule_anchors(spec, descriptors)
        self.assertIn("nlink", str(caught.exception))

    def test_symlink_substitution_cannot_change_retained_anchor(self) -> None:
        """A final-component symlink swap is never followed after validation."""
        binding = self.binding(role="planner")
        home = wc.sanitized_home_directory()
        self.addCleanup(shutil.rmtree, home, ignore_errors=True)
        descriptors: list[int] = []
        spec = wc.confinement_spec(
            binding, sanitized_home=home, _rule_descriptors=descriptors
        )
        self.addCleanup(lambda: [os.close(fd) for fd in descriptors])
        target = self.workspace / "plan.md"
        target.unlink()
        target.symlink_to(self.workspace / ".factory-state" / "factory-loop.json")
        with self.assertRaises(wc.ConfinementError) as caught:
            wc.validate_rule_anchors(spec, descriptors)
        self.assertIn("nlink", str(caught.exception))
        with self.assertRaises(wc.ConfinementError):
            wc._open_path_anchor(str(target))

    def test_directory_substitution_cannot_change_retained_anchor(self) -> None:
        """A same-name directory replacement never inherits the broad grant."""
        binding = self.binding(role="developer")
        home = wc.sanitized_home_directory()
        self.addCleanup(shutil.rmtree, home, ignore_errors=True)
        descriptors: list[int] = []
        spec = wc.confinement_spec(
            binding, sanitized_home=home, _rule_descriptors=descriptors
        )
        self.addCleanup(lambda: [os.close(fd) for fd in descriptors])
        target = self.workspace / "src"
        original = self.workspace / "src.original"
        target.rename(original)
        target.mkdir()
        wc.validate_rule_anchors(spec, descriptors)
        identities = [rule["identity"] for rule in spec["rules"]
                      if rule["path"] == str(target)]
        self.assertTrue(identities)
        self.assertTrue(all(identity != wc._path_identity(str(target))
                            for identity in identities))

    def test_post_anchor_path_swap_never_reopened_by_child(self) -> None:
        """Inherited descriptors, not swapped pathnames, feed Landlock."""
        binding = self.binding(role="planner")
        home = wc.sanitized_home_directory()
        self.addCleanup(shutil.rmtree, home, ignore_errors=True)
        retained: list[int] = []
        spec = wc.confinement_spec(
            binding, sanitized_home=home, _rule_descriptors=retained
        )
        descriptors = tuple(retained)
        self.addCleanup(lambda: [os.close(fd) for fd in descriptors])
        target = self.workspace / "plan.md"
        target.rename(self.workspace / "plan.original")
        target.symlink_to(self.workspace / ".factory-state" / "factory-loop.json")
        spec_path = self.diag / "anchored-swap.json"
        spec_path.write_text(
            json.dumps(spec, sort_keys=True, separators=(",", ":")),
            encoding="utf-8",
        )
        proc = subprocess.run(
            [
                PY, str(LOOP / "confine_launcher.py"),
                "--spec-file", str(spec_path),
                "--rule-fds", ",".join(str(fd) for fd in descriptors),
                "--", PY, str(self.workspace / "probe.py"),
                json.dumps(_probe_targets(f"read:{target}")),
            ],
            pass_fds=descriptors,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=str(self.workspace),
            timeout=60,
        )
        # The launcher succeeded using the old retained inode, while opening
        # the swapped pathname/denied target remained denied to the model.
        self.assertEqual(proc.returncode, 0, proc.stderr.decode()[-1000:])
        line = next(line for line in proc.stdout.decode().splitlines()
                    if line.startswith("FACTORY_CONFINEMENT_PROBE "))
        result = json.loads(line.split(" ", 1)[1])
        self.assertEqual(result[f"read:{target}"], "PermissionError")

    def test_allowlisted_hardlink_rejected_without_forbidden_scan(self) -> None:
        """Single-link allowlist checks never enumerate denied namespaces."""
        binding = self.binding(role="planner")
        home = wc.sanitized_home_directory()
        self.addCleanup(shutil.rmtree, home, ignore_errors=True)
        # A normal spec build must not walk .ralph/.factory-state (or call
        # os.walk at all); denied namespaces remain opaque.
        with mock.patch.object(
            wc.os, "walk", side_effect=AssertionError("forbidden enumeration")
        ):
            wc.confinement_spec(binding, sanitized_home=home)
        allowed = self.workspace / ".factory" / "bugs" / "open.md"
        denied = self.workspace / ".factory-state" / "factory-loop.json"
        allowed.unlink()
        os.link(denied, allowed)
        with self.assertRaises(wc.ConfinementError) as caught:
            wc.confinement_spec(binding, sanitized_home=home)
        self.assertIn("link count", str(caught.exception))

    def test_authorize_accepts_no_caller_confinement_spec(self) -> None:
        """A caller cannot widen confinement because no spec input exists."""
        parameters = inspect.signature(launch.authorize_launch).parameters
        self.assertNotIn("_confinement_spec", parameters)
        self.assertNotIn("confinement_spec", parameters)

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
                    # Tester/auditor receive read-only loop/test source for
                    # executable verifier and audit inspection. Prompts and
                    # runtime/legacy state remain denied to every role; planner
                    # and developer also cannot read control-plane source.
                    denied_paths = [
                        ".factory/prompts", ".factory/state", ".factory/ralph",
                    ]
                    if role not in ("tester", "auditor"):
                        denied_paths.extend([".factory/loop", ".factory/tests"])
                    for denied in denied_paths:
                        self.assertFalse(
                            str(relative).startswith(denied),
                            f"rule {path} grants a control-plane source "
                            f"({denied}) for role {role}",
                        )
                if role in ("tester", "auditor"):
                    rule_paths = {str(Path(r["path"]).absolute())
                                  for r in spec["rules"]}
                    self.assertIn(str(self.workspace / ".factory" / "loop"), rule_paths)
                    self.assertIn(str(self.workspace / ".factory" / "tests"), rule_paths)
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
        """Only an exact immutable external backend receives EXECUTE."""
        external = self.diag / "toolchain" / "bin"
        external.mkdir(parents=True)
        mutable = external / "synthetic-backend"
        mutable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        os.chmod(mutable, 0o700)
        home = wc.sanitized_home_directory()
        self.addCleanup(shutil.rmtree, home, ignore_errors=True)
        with self.assertRaisesRegex(wc.ConfinementError, "not an immutable"):
            wc.confinement_spec(
                self.binding(backend=mutable), sanitized_home=home
            )
        trusted = Path(PY)
        spec = wc.confinement_spec(
            self.binding(backend=trusted), sanitized_home=home
        )
        rule_paths = {str(Path(r["path"]).absolute()) for r in spec["rules"]}
        self.assertIn(str(trusted), rule_paths)
        self.assertNotIn(str(trusted.parent), rule_paths)

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
        self.assertFalse(hasattr(proof, "synthetic"))
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
            _mint=wc._PROOF_MINT_SECRET,
        )
        with self.assertRaises(wc.ConfinementError):
            wc.validate_proof(forged, binding, confinement_spec=spec)

    def test_installed_authority_has_no_synthetic_proof_surface(self) -> None:
        """Only real proof minting exists in the production authority."""
        self.assertFalse(hasattr(wc, "_mint_synthetic_proof"))
        self.assertNotIn("synthetic", inspect.signature(wc.prove_confinement).parameters)
        binding = self.binding()
        home = wc.sanitized_home_directory()
        self.addCleanup(shutil.rmtree, home, ignore_errors=True)
        proof = wc.prove_confinement(
            binding,
            confinement_spec=wc.confinement_spec(binding, sanitized_home=home),
        )
        self.assertFalse(hasattr(proof, "synthetic"))

    def test_landlock_primitive_is_real(self) -> None:
        self.assertTrue(wc.confinement_primitive_available())
        self.assertGreaterEqual(wc._landlock_abi(), 1)
        wc.require_confinement_primitive()  # full nnp/add-rule/restrict sequence

    def test_probe_sequence_closes_descriptors(self) -> None:
        """The primitive probe applies nnp + add_rule + restrict and closes FDs."""
        read_fd, write_fd = os.pipe()
        pid = os.fork()
        if pid == 0:
            os.close(read_fd)
            try:
                before = len(os.listdir("/proc/self/fd"))
                wc._probe_apply_ruleset(
                    wc._handled_access_bits(wc._landlock_abi())
                )
                after = len(os.listdir("/proc/self/fd"))
                os.write(write_fd, f"{before}:{after}".encode("ascii"))
                os._exit(0)
            except BaseException:
                os._exit(1)
        os.close(write_fd)
        payload = os.read(read_fd, 100).decode("ascii")
        os.close(read_fd)
        _, status = os.waitpid(pid, 0)
        self.assertTrue(os.WIFEXITED(status))
        self.assertEqual(os.WEXITSTATUS(status), 0)
        before_text, after_text = payload.split(":")
        self.assertEqual(int(after_text), int(before_text))


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

    def _authorize(self, binding, **kwargs):
        authority = launch.authorize_launch(
            binding,
            role_prompt=(self.workspace / "role.md").read_bytes(),
            agents=(self.workspace / "AGENTS.md").read_bytes(),
            spec=(self.workspace / "spec.md").read_bytes(),
            plan=(self.workspace / "plan.md").read_bytes(),
            **kwargs,
        )

        def cleanup() -> None:
            for descriptor in (
                *getattr(authority, "_confinement_rule_fds", ()),
                getattr(authority, "_prompt_fd", -1),
                getattr(authority, "_auth_fd", -1),
            ):
                if descriptor is not None and descriptor >= 0:
                    try:
                        os.close(descriptor)
                    except OSError:
                        pass
            for path in (
                getattr(authority, "_exec_dir", None),
                getattr(authority, "_session_dir", None),
                getattr(authority, "_sanitized_home", None),
            ):
                if path is not None:
                    shutil.rmtree(path, ignore_errors=True)

        self.addCleanup(cleanup)
        return authority

    def test_authorize_with_real_spec_mints_real_proof(self) -> None:
        binding = self.binding(role="planner")
        authority = self._authorize(binding)
        self.assertIsInstance(authority, launch.LaunchAuthority)
        self.assertIsNotNone(authority._confinement_spec)
        proof = authority._confinement_proof
        self.assertIsNotNone(proof)
        self.assertFalse(hasattr(proof, "synthetic"))
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
        staged_git = authority._exec_dir / launch.STAGED_GIT_SHIM_NAME
        self.assertTrue(staged_git.is_file())
        self.assertEqual(staged_git.stat().st_mode & 0o111, 0)
        self.assertFalse(
            any(rule["path"] == "/nix/store"
                for rule in authority._confinement_spec["rules"]),
            "the broad Nix store root must never enter the model read view",
        )
        closure_rules = [
            rule for rule in authority._confinement_spec["rules"]
            if str(rule["path"]).startswith("/nix/store/")
            and wc.ACCESS_EXECUTE not in rule["access"]
        ]
        self.assertTrue(closure_rules, "the exact immutable toolchain closure is absent")
        for rule in closure_rules:
            root = str(rule["path"])
            self.assertRegex(root, wc.NIX_STORE_ROOT_RE)
            self.assertIn(wc.ACCESS_READ, rule["access"])
            self.assertNotIn(wc.ACCESS_WRITE, rule["access"])
        executable_rules = {
            rule["path"]
            for rule in authority._confinement_spec["rules"]
            if wc.ACCESS_EXECUTE in rule["access"]
        }
        self.assertNotIn(str(authority._exec_dir), executable_rules)
        self.assertFalse(
            any(Path(path).is_relative_to(authority._exec_dir) for path in executable_rules),
            "caller-owned staging paths must never receive Landlock EXECUTE",
        )
        for executable in executable_rules:
            self.assertTrue(
                Path(executable).is_file(),
                f"EXECUTE was granted to a directory instead of an exact inode: {executable}",
            )
        real_git = os.path.realpath(shutil.which("git") or "")
        self.assertNotIn(real_git, executable_rules)
        with open("/proc/self/maps", encoding="utf-8") as stream:
            loaders = {
                os.path.realpath(line.rsplit(" ", 1)[-1].strip())
                for line in stream
                if ("ld-linux" in line or "ld-musl" in line)
                and os.path.isfile(line.rsplit(" ", 1)[-1].strip())
            }
        self.assertTrue(loaders)
        self.assertTrue(loaders.issubset(executable_rules))
        approved_identities = confine_launcher._approved_exec_identities(
            authority._confinement_spec
        )
        for loader in loaders:
            info = os.stat(loader)
            self.assertNotIn((info.st_dev, info.st_ino), approved_identities)

    def test_nix_closure_query_rejects_path_escape_and_environment_injection(self) -> None:
        seed = wc._nix_store_root(PY)
        self.assertIsNotNone(seed)
        assert seed is not None
        observed: dict = {}

        def escaped(argv, **kwargs):
            observed.update(kwargs)
            return subprocess.CompletedProcess(
                argv, 0, f"{seed}\n/tmp/attacker-closure\n".encode(), b""
            )

        trusted_nix_store = shutil.which("nix-store")
        self.assertIsNotNone(trusted_nix_store)
        hostile = dict(os.environ)
        hostile.update({
            "TOKEN": "must-not-leak", "NIX_CONFIG": "extra-access-tokens = leak",
            "GIT_CONFIG_COUNT": "1",
        })
        with mock.patch.dict(os.environ, hostile, clear=True), \
             mock.patch.object(wc.shutil, "which", return_value=trusted_nix_store), \
             mock.patch.object(wc.subprocess, "run", side_effect=escaped):
            with self.assertRaises(wc.ConfinementError):
                wc._toolchain_closure_paths([PY])
        child_env = observed["env"]
        self.assertEqual(child_env["HOME"], "/")
        self.assertNotIn("TOKEN", child_env)
        self.assertNotIn("NIX_CONFIG", child_env)
        self.assertNotIn("GIT_CONFIG_COUNT", child_env)

    def test_mutable_path_cannot_substitute_nix_closure_authority(self) -> None:
        fake = self.diag / "nix-store"
        fake.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        fake.chmod(0o755)
        with mock.patch.dict(os.environ, {"PATH": str(self.diag)}, clear=False):
            with self.assertRaises(wc.ConfinementUnavailable):
                wc._toolchain_closure_paths([PY])

    def test_caller_sanitized_home_keyword_is_rejected(self) -> None:
        binding = self.binding(role="planner")
        with self.assertRaises(TypeError):
            self._authorize(binding, _sanitized_home=self.diag)

    def test_ollama_authorization_has_real_proof_without_quota_channel(self) -> None:
        """Ollama launch keeps real confinement but opens no quota channel."""
        binding = self.binding(role="planner", provider="ollama")
        self.assertFalse(hasattr(launch, "usage_guard"))
        authority = self._authorize(binding)
        self.assertFalse(hasattr(authority._confinement_proof, "synthetic"))
        self.assertEqual(
            {c.to_tuple() for c in authority._confinement_proof.credential_channels},
            {("env_store", usage._default_env_file())},
        )

    def test_caller_proof_keyword_is_rejected(self) -> None:
        """No installed caller can inject any proof, synthetic or otherwise."""
        binding = self.binding(role="planner", provider="ollama")
        with self.assertRaises(TypeError):
            self._authorize(binding, _confinement_proof=object())

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
        """A sibling launch's private directories are never granted to another.

        The prompt is not among them: it is an anonymous sealed memfd with no
        pathname or Landlock rule.
        """
        binding = self.binding(role="planner")
        # Mint authority A with its internally created home and real proof.
        authority_a = self._authorize(binding)
        home_a = authority_a._sanitized_home
        self.assertFalse(hasattr(authority_a._confinement_proof, "synthetic"))
        self.assertFalse(hasattr(authority_a, "_prompt_path"))
        with self.assertRaises(OSError):
            os.pwrite(authority_a._prompt_fd, b"tamper", 0)
        sibling_dirs = [
            authority_a._exec_dir,
            authority_a._session_dir,
            home_a,
        ]
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
            {"op": "read", "path": str(authority_a._exec_dir / "pi2-secure-exec.py"),
             "label": "sibling-staging"},
            {"op": "write", "path": str(authority_a._session_dir / "probe"),
             "label": "sibling-session"},
            {"op": "write", "path": str(home_a / "probe"),
             "label": "sibling-home"},
        ]
        free = self.run_unconfined(targets)
        for key in ("read:sibling-staging",
                    "write:sibling-session", "write:sibling-home"):
            self.assertEqual(
                free[key], "ok",
                f"sibling path control {key} is not accessible without "
                "confinement (vacuous)",
            )
        # B's confined child cannot touch A's private paths.
        result = self.run_confined("developer", targets, spec=spec_b)
        for key in ("read:sibling-staging",
                    "write:sibling-session", "write:sibling-home"):
            self.assertEqual(
                result.get(key), "PermissionError",
                f"launch B reached sibling launch A's private path {key}",
            )

    def test_every_provider_and_direct_api_requires_real_confinement(self) -> None:
        """Every programmatic provider receives internally minted real confinement."""
        for provider in ("synthetic", "ollama"):
            with self.subTest(provider=provider):
                binding = self.binding(role="planner", provider=provider)
                authority = self._authorize(binding)
                self.assertFalse(hasattr(authority._confinement_proof, "synthetic"))
                self.assertTrue(authority._confinement_rule_fds)

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
                    hasattr(proof, "synthetic"),
                    f"provider {provider} exposed a synthetic-proof marker",
                )
                # Re-validate against the exact channels the invocation
                # consumed (the strict channel equality the launch path uses).
                wc.validate_proof(
                    proof, binding,
                    confinement_spec=authority._confinement_spec,
                    cookie_file=None,
                )

    def test_authorize_failure_cleans_private_dirs_and_prompt_fd(self) -> None:
        """Authorization failure removes dirs and closes the sealed prompt."""
        binding = self.binding(role="planner")
        tracked: dict = {}
        for name, prefix in (
            ("exec_dir", "factory-loop-exec-"),
            ("session_dir", "factory-loop-session-"),
        ):
            path = Path(tempfile.mkdtemp(prefix=prefix, dir="/tmp"))
            os.chmod(path, 0o700)
            tracked[name] = path
            self.addCleanup(shutil.rmtree, path, ignore_errors=True)
        home = wc.sanitized_home_directory()
        self.addCleanup(shutil.rmtree, home, ignore_errors=True)
        prompt_fd = launch._sealed_prompt_memfd(b"prompt")
        # Exercise cleanup with a proof-mint failure after every private
        # directory and the anonymous prompt channel have been created.
        with mock.patch.object(launch, "_exec_staging_dir",
                               return_value=tracked["exec_dir"]), \
             mock.patch.object(launch, "_sealed_prompt_memfd",
                               return_value=prompt_fd), \
             mock.patch.object(launch, "_session_directory",
                               return_value=tracked["session_dir"]), \
             mock.patch.object(wc, "sanitized_home_directory", return_value=home), \
             mock.patch.object(
                 wc, "prove_confinement",
                 side_effect=wc.ConfinementError("proof-mint failure"),
             ):
            with self.assertRaises(launch.InvocationError) as caught:
                launch.authorize_launch(
                    binding,
                    role_prompt=(self.workspace / "role.md").read_bytes(),
                    agents=(self.workspace / "AGENTS.md").read_bytes(),
                    spec=(self.workspace / "spec.md").read_bytes(),
                    plan=(self.workspace / "plan.md").read_bytes(),
                )
            self.assertIn("confinement", str(caught.exception).lower())
        for path in (*tracked.values(), home):
            self.assertFalse(
                path.exists(),
                f"private directory {path} survived a failed authorization",
            )
        with self.assertRaises(OSError):
            os.fstat(prompt_fd)

    def test_production_denies_untraced_clone_and_clone3_without_late_mutation(
        self,
    ) -> None:
        """Raw clone escape primitives are denied on the full launch path.

        The committed Python backend is a small raw-syscall helper. If
        CLONE_UNTRACED were ever continued, its untraced child would close the
        launch pipes, outlive the backend, and mutate a workspace sentinel
        after the launch returned. The production seccomp/ptrace path must
        instead return EACCES within a bound; clone3 returns the deliberate
        ENOSYS denial on kernels that implement it because pointer-backed flags
        cannot be authorized race-free (and libc can safely fall back to clone).
        """
        machine = confine_launcher.platform.machine().lower()
        syscalls = confine_launcher._brokered_scalar_syscalls()
        self.assertIn(machine, ("x86_64", "amd64", "aarch64", "arm64"))

        # Non-destructive unconfined support probe: a null, zero-sized
        # clone_args never creates a process. ENOSYS alone means unsupported.
        libc = ctypes.CDLL(None, use_errno=True)
        libc.syscall.restype = ctypes.c_long
        ctypes.set_errno(0)
        probe_result = libc.syscall(
            ctypes.c_long(syscalls["clone3"]), ctypes.c_void_p(), ctypes.c_size_t(0)
        )
        clone3_supported = not (
            probe_result == -1 and ctypes.get_errno() == errno.ENOSYS
        )

        backend = self.workspace / "backend.py"
        result_path = (
            self.workspace / "src" / ".factory-test-output" / "raw-clone.json"
        )
        survivor_path = result_path.with_name("raw-clone-survivor.pid")
        late_path = result_path.with_name("raw-clone-late.marker")
        sentinel = result_path.with_name("raw-clone-sentinel.txt")
        sentinel.write_text("stable\n", encoding="utf-8")
        backend.write_text(
            "#!/usr/bin/env python3\n"
            "import ctypes, errno, json, os, platform, signal, time\n"
            f"result_path = {str(result_path)!r}\n"
            f"survivor_path = {str(survivor_path)!r}\n"
            f"late_path = {str(late_path)!r}\n"
            f"sentinel = {str(sentinel)!r}\n"
            "numbers = {'x86_64': (56, 435), 'amd64': (56, 435), "
            "'aarch64': (220, 435), 'arm64': (220, 435)}\n"
            "clone_nr, clone3_nr = numbers[platform.machine().lower()]\n"
            "libc = ctypes.CDLL(None, use_errno=True)\n"
            "libc.syscall.restype = ctypes.c_long\n"
            "ctypes.set_errno(0)\n"
            "clone_result = libc.syscall(ctypes.c_long(clone_nr), "
            "ctypes.c_ulonglong(0x00800000 | signal.SIGCHLD), "
            "ctypes.c_void_p(), ctypes.c_void_p(), ctypes.c_void_p(), "
            "ctypes.c_void_p())\n"
            "clone_errno = ctypes.get_errno() if clone_result == -1 else 0\n"
            "if clone_result == 0:\n"
            "    for descriptor in (0, 1, 2):\n"
            "        try: os.close(descriptor)\n"
            "        except OSError: pass\n"
            "    time.sleep(1.0)\n"
            "    with open(sentinel, 'a', encoding='utf-8') as stream:\n"
            "        stream.write('MUTATED\\n')\n"
            "    with open(late_path, 'w', encoding='utf-8') as stream:\n"
            "        stream.write(str(os.getpid()))\n"
            "    os._exit(0)\n"
            "if clone_result > 0:\n"
            "    with open(survivor_path, 'w', encoding='utf-8') as stream:\n"
            "        stream.write(str(clone_result))\n"
            "ctypes.set_errno(0)\n"
            "clone3_result = libc.syscall(ctypes.c_long(clone3_nr), "
            "ctypes.c_void_p(), ctypes.c_size_t(0))\n"
            "clone3_errno = ctypes.get_errno() if clone3_result == -1 else 0\n"
            "payload = {'clone_result': clone_result, "
            "'clone_errno': clone_errno, 'clone3_result': clone3_result, "
            "'clone3_errno': clone3_errno}\n"
            "with open(result_path, 'w', encoding='utf-8') as stream:\n"
            "    json.dump(payload, stream, sort_keys=True)\n"
            "print('RAW_CLONE_RESULT ' + json.dumps(payload, sort_keys=True))\n",
            encoding="utf-8",
        )
        os.chmod(backend, 0o700)
        _git("add", "backend.py", "src/.factory-test-output/raw-clone-sentinel.txt",
             cwd=self.workspace)
        _git("commit", "-qm", "add raw clone confinement helper", cwd=self.workspace)
        self.head = _git("rev-parse", "HEAD", cwd=self.workspace).stdout.strip()
        binding = self.binding()
        plan = self.workspace / "plan.md"
        _, excerpt_digest = launch.derive_task_excerpt(plan.read_bytes(), 1)
        argv = [
            "launch", "--root", str(self.workspace), "--role", "developer",
            "--model", "synthetic-model", "--provider", "synthetic",
            "--backend", str(backend), "--role-prompt",
            str(self.workspace / "role.md"), "--role-prompt-digest", sha256(b"role\n"),
            "--prompt-set-digest", sha256(b"set"), "--policy",
            str(self.workspace / "AGENTS.md"), "--policy-digest", sha256(b"agents\n"),
            "--spec", str(self.workspace / "spec.md"), "--spec-digest",
            sha256(b"spec\n"), "--plan", str(plan), "--plan-digest",
            sha256(plan.read_bytes()), "--bound-commit", self.head,
            "--allowed-tools", "read,bash", "--runtime-limit", "30",
            "--inactivity-limit", "20", "--task-id", "1",
            "--task-excerpt-digest", excerpt_digest,
        ]
        out = io.StringIO()
        err = io.StringIO()
        survivor_pid: int | None = None
        started = time.monotonic()
        try:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                status = launch.main(argv)
            elapsed = time.monotonic() - started
            self.assertEqual(status, launch.EXIT_COMPLETED, err.getvalue()[-2000:])
            self.assertLess(elapsed, 10.0, "raw clone denial exceeded its bound")
            self.assertTrue(result_path.is_file(), "the raw helper produced no result")
            result = json.loads(result_path.read_text(encoding="utf-8"))
            self.assertEqual(result["clone_result"], -1, result)
            self.assertEqual(result["clone_errno"], errno.EACCES, result)
            if clone3_supported:
                self.assertEqual(result["clone3_result"], -1, result)
                self.assertEqual(result["clone3_errno"], errno.ENOSYS, result)
            if survivor_path.is_file():
                survivor_pid = int(survivor_path.read_text(encoding="utf-8"))
            self.assertIsNone(
                survivor_pid,
                f"CLONE_UNTRACED unexpectedly created survivor {survivor_pid}",
            )
            # Wait beyond the hostile child's programmed mutation point. The
            # launch has already returned, so any surviving escape would now
            # alter both the sentinel and the late marker.
            time.sleep(1.2)
            self.assertEqual(sentinel.read_bytes(), b"stable\n")
            self.assertFalse(late_path.exists(), "workspace mutated after return")
        finally:
            if survivor_pid is None and survivor_path.is_file():
                survivor_pid = int(survivor_path.read_text(encoding="utf-8"))
            if survivor_pid is not None:
                try:
                    os.kill(survivor_pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                try:
                    os.waitpid(survivor_pid, 0)
                except ChildProcessError:
                    pass

    def test_confined_leaf_executes_exact_project_toolchain(self) -> None:
        """The real broker executes the generic project tool closure, not 126.

        This runs below the full Landlock+seccomp launch path. The confined
        process must start the adopting project's immutable generic Python/Nix
        toolchain; product-specific graphics tools are not required. A mere
        command lookup or simulated marker is insufficient, and the broad
        /nix/store root remains absent from the spec.
        """
        backend = self.workspace / "backend.py"
        marker = self.workspace / "src" / ".factory-test-output" / "toolchain.txt"
        backend.write_text(
            "#!/usr/bin/env python3\n"
            "import os, pathlib, subprocess, sys\n"
            "sys.stdin.buffer.read()\n"
            "root = os.environ['FACTORY_LOOP_LAUNCH_WORKSPACE']\n"
            "commands = [['nix-shell','--version'], ['python3','-c','print(123)']]\n"
            "runs = [subprocess.run(item, cwd=root, capture_output=True, text=True, timeout=30) for item in commands]\n"
            "result = type('Result', (), {'returncode': next((r.returncode for r in runs if r.returncode), 0), "
            "'stdout': ''.join(r.stdout for r in runs), 'stderr': ''.join(r.stderr for r in runs)})()\n"
            f"path = pathlib.Path({str(marker)!r})\n"
            "path.parent.mkdir(parents=True, exist_ok=True)\n"
            "path.write_text(f'{result.returncode}\\nSTDOUT:\\n{result.stdout}\\nSTDERR:\\n{result.stderr}', encoding='utf-8')\n"
            "raise SystemExit(result.returncode)\n",
            encoding="utf-8",
        )
        os.chmod(backend, 0o700)
        _git("add", "backend.py", cwd=self.workspace)
        _git("commit", "-qm", "add exact toolchain probe", cwd=self.workspace)
        self.head = _git("rev-parse", "HEAD", cwd=self.workspace).stdout.strip()
        plan = self.workspace / "plan.md"
        _, excerpt_digest = launch.derive_task_excerpt(plan.read_bytes(), 1)
        argv = [
            "launch", "--root", str(self.workspace), "--role", "developer",
            "--model", "synthetic-model", "--provider", "synthetic",
            "--backend", str(backend), "--role-prompt",
            str(self.workspace / "role.md"), "--role-prompt-digest", sha256(b"role\n"),
            "--prompt-set-digest", sha256(b"set"), "--policy",
            str(self.workspace / "AGENTS.md"), "--policy-digest", sha256(b"agents\n"),
            "--spec", str(self.workspace / "spec.md"), "--spec-digest", sha256(b"spec\n"),
            "--plan", str(plan), "--plan-digest", sha256(plan.read_bytes()),
            "--bound-commit", self.head, "--allowed-tools", "read,bash",
            "--runtime-limit", "180", "--inactivity-limit", "150", "--task-id", "1",
            "--task-excerpt-digest", excerpt_digest,
        ]
        out = io.StringIO()
        err = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            status = launch.main(argv)
        payload = marker.read_text(encoding="utf-8") if marker.exists() else "missing marker"
        self.assertEqual(
            status, launch.EXIT_COMPLETED,
            (err.getvalue() + "\n" + out.getvalue() + "\n" + payload)[-6000:],
        )
        self.assertTrue(payload.startswith("0\n"), payload[-2000:])
        self.assertIn("123", payload)
        self.assertIn("nix-shell", payload.lower())

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
            backend=Path(sys.executable), workspace=ROOT,
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
            self.assertEqual(set(rule), {"path", "access", "identity"})
            self.assertEqual(
                set(rule["identity"]), {"dev", "ino", "type", "uid", "nlink"}
            )
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

        The production API constructs and mints confinement internally and
        exposes no proof/spec/home or usage-transport test seam.
        """
        parameters = inspect.signature(launch.authorize_launch).parameters
        for forbidden in (
            "_confinement_spec", "confinement_spec", "_confinement_proof",
            "confinement_proof", "_sanitized_home", "_usage_guard_html_file",
            "_usage_guard_allow_loopback", "synthetic",
        ):
            self.assertNotIn(forbidden, parameters)


if __name__ == "__main__":
    unittest.main(verbosity=2)
