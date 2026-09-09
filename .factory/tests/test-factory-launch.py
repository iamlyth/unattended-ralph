#!/usr/bin/env python3
"""Harness-owned adversarial tests for fresh-context execution, the exact
invocation contract, and supervision (CTX-01, TASK-02, LOCK-01, PROC-01).

This test lives under the hidden ``.factory/tests/`` namespace because the
specification (HIDE-01, §3) keeps harness-only tests out of the adopting
product's visible test tree.  It is the deterministic verification for
Task 6, exercising ``.factory/loop/launch.py`` through the real
``.factory/tools/pi2-secure-exec.py`` wrapper (invoked, never reimplemented) with
synthetic backends:

* **invocation / task-excerpt exact byte binding (TASK-02, §9/§20)**: the
  excerpt re-derived from the committed plan is the *verbatim byte slice* of
  the ``## Task N:`` section (including its trailing blank lines), its digest
  is recorded in the binding, and any substituted, paraphrased, oversized, or
  foreign input fails closed before launch;
* **prompt input allowlist / no history (CTX-01, §5.1/§9)**: the composed
  prompt carries exactly the static role prompt, AGENTS.md, specification,
  plan, audit objective (auditor only), and the exact task excerpt
  (developer only) — never session, memory, scratchpad, or historical
  content — and is byte-deterministic;
* **argv/env secret stripping (§18)**: the child environment is rebuilt from
  the allowlist plus ``FACTORY_LOOP_LAUNCH_*`` fields — synthetic
  credentials, ``OLLAMA_*``/``PI_*`` session variables, lock metadata, and
  ``GIT_CONFIG*`` redirectors placed in the parent never reach the leaf
  (asserted both on the constructed environment and on the live child's
  ``/proc/<pid>/environ`` and recorded argv);
* **fresh no-resume process (CTX-01)**: a live child starts its own process
  session (session id == pid), the argv carries the structural one-shot/no-
  resume flags and no forbidden resume flag, and the environment carries the
  ``FRESH``/``NO_RESUME``/``NO_MEMORY`` markers;
* **lock-fd non-inheritance (LOCK-01)**: the supervisor spawns with
  ``close_fds``, no ``pass_fds``, and a built environment, and the
  per-launch invariant scan verifies the child carries no root-inode
  descriptor and no lock metadata;
* **process-group TERM/INT/HUP → KILL (PROC-01, §9/§12)**: a TERM-trapping
  leader receives the supervisor's TERM; a TERM/INT/HUP-ignoring leader and
  a pipe-holding descendant in the group are killed after the bounded grace,
  the group is verified gone, and the leader is reaped;
* **dedicated broker / descendant scope / PID reuse (F6/F7)**: the capture
  is reuse-safe, a crash before snapshot preserves dirty work, and the fresh
  childless ptrace/subreaper broker reaps double-forked orphans and
  bounded-SIGKILLs surviving ``setsid`` escapes. A pre-existing coordinator
  child that forks and exits during launch keeps both its unrelated worker
  and wait status;
* **bounded output/timeouts (§9)**: per-stream digests and bounded tails,
  runtime and inactivity limits terminate the run, and results are bounded
  and carry no credentials (no argv/environment ever appears in a result).
"""

from __future__ import annotations

import contextlib
import dataclasses
import errno
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import signal
import stat
import selectors
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import unittest.mock

ROOT = Path(__file__).resolve().parents[2]
LOOP = ROOT / ".factory" / "loop"

sys.path.insert(0, str(LOOP))
import gitutil  # noqa: E402
import launch  # noqa: E402
import lock as lock_module  # noqa: E402
import task_budget  # noqa: E402
import verifier_failure  # noqa: E402
from launch import (  # noqa: E402
    DEFAULT_ALLOWED_TOOLS,
    DEFAULT_INACTIVITY_LIMIT,
    DEFAULT_RUNTIME_LIMIT,
    ENV_ALLOWLIST,
    INVOCATION_ENV_PREFIX,
    InvocationBinding,
    InvocationError,
    LaunchResult,
    LaunchSupervision,
    OUTPUT_DIGEST_CAP,
    OUTPUT_TAIL_CAP,
    PROMPT_INPUT_MAX,
    PROMPT_MAX_BYTES,
    ResultSchemaError,
    ROLES,
    SECURE_WRAPPER,
    StreamResult,
    SupervisionError,
    SupervisionSignalInterrupt,
    TERMINATION_SIGNALS,
    child_argv,
    child_environment,
    compose_prompt,
    derive_task_excerpt,
    PI_FACTORY_GUARD_DIGEST_ENV,
    secure_wrapper_path,
    task_excerpt_bytes,
    task_excerpt_digest,
    validate_launch_result,
    verify_child_env,
    verify_invocation,
    verify_task_excerpt,
)
from lock import (  # noqa: E402
    CapturedProcess,
    EscapedDescendantError,
    RootLockUnsafeError,
    capture_descendants,
    live_scope,
)

GIT = gitutil.GIT_EXECUTABLE
FIXTURE_PLAN = ROOT / ".factory" / "tests" / "fixtures" / "plan-valid-base.md"
REAL_WRAPPER = ROOT / SECURE_WRAPPER
WRAPPER_BASENAME = Path(SECURE_WRAPPER).name
SYNTHETIC_SECRET = "synth-cookie-value-9f3a1c7b"

# Every committed fixture tracked in the ``_Base.setUp`` workspace at the
# pinned head.  A supervision attempt must never alter, delete, or overwrite
# any of these authoritative blobs, and the supervisor adds only its own
# ``behavior.json`` driver (pre-commit harness control) plus any dirty work
# the model wrote before it crashed.
COMMITTED_FIXTURE_NAMES = frozenset(
    {
        ".git",
        "AGENTS.md",       # operational policy
        "backend.py",      # model backend
        "plan.md",         # implementation plan
        "role.md",         # role prompt
        "scripts",         # secure wrapper
        "spec.md",         # product specification
    }
)

# A self-contained synthetic model backend.  It parses the exact backend argv
# produced by ``child_argv``, reads the prompt snapshot from stdin (the secure
# wrapper dups the sealed memfd onto stdin), and switches on a ``behavior.json``
# file in its working directory (the canonical workspace), so the harness
# drives supervision scenarios without adding any environment or argv knob.
BACKEND_SOURCE = r'''#!/usr/bin/env python3
import argparse, hashlib, json, os, signal, sys, time

_p = argparse.ArgumentParser(add_help=False)
_p.add_argument("--provider")
_p.add_argument("--model")
_p.add_argument("--print", action="store_true")
_p.add_argument("--no-session", action="store_true")
_p.add_argument("--session-dir")
_p.add_argument("--no-skills", action="store_true")
_p.add_argument("--no-themes", action="store_true")
_p.add_argument("--no-context-files", action="store_true")
_p.add_argument("--tools")
_args, _extra = _p.parse_known_args(sys.argv[1:])

_WS = os.environ.get("FACTORY_LOOP_LAUNCH_WORKSPACE", ".")
_MARKERS = os.path.join(_WS, "src", ".factory-test-output")

def marker(name, value):
    os.makedirs(_MARKERS, exist_ok=True)
    with open(os.path.join(_MARKERS, name), "w") as f:
        f.write(str(value))

def behavior():
    try:
        with open(os.path.join(_WS, "behavior.json")) as f:
            return json.load(f)
    except OSError:
        return {}

def main():
    cfg = behavior()
    mode = cfg.get("mode", "record")
    if mode == "record":
        data = sys.stdin.buffer.read()
        marker("prompt.digest", hashlib.sha256(data).hexdigest())
        marker("argv.json", json.dumps(sys.argv[1:]))
        marker("env.json", json.dumps(sorted(dict(os.environ).items())))
        marker("sid.json", json.dumps({
            "pid": os.getpid(), "sid": os.getsid(0), "ppid": os.getppid(),
            "cwd": os.getcwd(),
        }))
        sys.stdout.write("hello from model backend\n")
        sys.stderr.write("model stderr line\n")
        sys.stdout.flush(); sys.stderr.flush()
        return 0
    if mode == "spew":
        total = int(cfg.get("bytes", 300000))
        chunk = b"x" * 65536
        written = 0
        while written < total:
            take = min(len(chunk), total - written)
            sys.stdout.buffer.write(chunk[:take])
            written += take
        sys.stderr.buffer.write(b"err-" * 100)
        sys.stdout.flush(); sys.stderr.flush()
        return 0
    if mode == "burn-cpu":
        # Deterministic CPU-budget fixture: busy-loop on one core for the
        # configured wall seconds so the trusted supervisor's /proc
        # utime+stime sampling observes real CPU consumption.
        end = time.monotonic() + int(cfg.get("seconds", 30))
        while time.monotonic() < end:
            pass
        return 0
    if mode == "burn-short-lived":
        # Adversarial CPU-accounting fixture: repeatedly fork short-lived
        # CPU burners that each consume ``burn_seconds`` of CPU and exit
        # before the next 1s live sample, so a sampling-only accounting
        # would under-count them.  The trusted supervisor's authoritative
        # RUSAGE_CHILDREN delta must include every reaped burner.
        burners = int(cfg.get("burners", 4))
        rounds = int(cfg.get("rounds", 3))
        burn_seconds = float(cfg.get("burn_seconds", 0.2))
        for _ in range(rounds):
            pids = []
            for _ in range(burners):
                pid = os.fork()
                if pid == 0:
                    end = time.monotonic() + burn_seconds
                    while time.monotonic() < end:
                        pass
                    os._exit(0)
                pids.append(pid)
            for pid in pids:
                os.waitpid(pid, 0)
        return 0
    if mode == "fork-many":
        # Deterministic live-process-budget fixture: fork ``count``
        # pipe-holding sleeping descendants so the trusted supervisor's
        # descendant closure exceeds the configured live-process budget.
        count = int(cfg.get("count", 4))
        children = []
        for _ in range(count):
            pid = os.fork()
            if pid == 0:
                time.sleep(300)
                os._exit(9)
            children.append(pid)
        marker("children", json.dumps(children))
        sys.stdout.flush(); sys.stderr.flush()
        time.sleep(300)
        return 0
    if mode == "exit":
        os._exit(0)
    if mode == "crash":
        os.kill(os.getpid(), signal.SIGKILL)
    if mode == "dirty-crash":
        marker("dirty-work.txt", "half-written work")
        os.kill(os.getpid(), signal.SIGKILL)
    if mode == "sleep":
        time.sleep(300)
        return 0
    if mode == "trap-term":
        def on_term(sig, frame):
            marker("term.marker", "SIGTERM")
            os._exit(0)
        signal.signal(signal.SIGTERM, on_term)
        marker("pid", os.getpid())
        time.sleep(300)
        return 0
    if mode == "ignore-term":
        for s in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
            signal.signal(s, signal.SIG_IGN)
        marker("pid", os.getpid())
        # Heartbeat so the runtime bound (not inactivity) ends the run: the
        # leader ignores TERM/INT/HUP and must be KILLed with the whole group.
        end = time.monotonic() + 120
        while time.monotonic() < end:
            sys.stdout.write(".")
            sys.stdout.flush()
            time.sleep(0.02)
        return 0
    if mode == "spawn-holder":
        for s in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
            signal.signal(s, signal.SIG_IGN)
        pid = os.fork()
        if pid == 0:
            time.sleep(300)
            os._exit(9)
        marker("child.pid", pid)
        sys.stdout.flush(); sys.stderr.flush()
        os._exit(0)
    if mode == "exit-with-holder":
        # Deterministic F4 poll-reap race fixture: the leader forks a
        # pipe-holding TERM/INT/HUP-ignoring descendant and then exits
        # *immediately*, so it is a zombie (or gone) before the invariant/
        # snapshot read-back finishes while the descendant keeps the pipes
        # open.  A ``Popen.poll()`` anywhere before termination would reap
        # the leader and free the PID (and the process-group id) ahead of
        # the F4 identity check; the supervisor must instead pin the
        # unreaped zombie's starttime and KILL the whole group.
        pid = os.fork()
        if pid == 0:
            for s in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
                signal.signal(s, signal.SIG_IGN)
            marker("holder.pid", os.getpid())
            time.sleep(300)
            os._exit(9)
        marker("pid", os.getpid())
        sys.stdout.flush(); sys.stderr.flush()
        os._exit(0)
    if mode == "double-fork-exit":
        pid = os.fork()
        if pid == 0:
            pid2 = os.fork()
            if pid2 > 0:
                os._exit(0)
            marker("orphan.pid", os.getpid())
            os._exit(0)
        for _ in range(500):
            if os.path.exists(os.path.join(_WS, "orphan.pid")):
                break
            time.sleep(0.01)
        os._exit(0)
    if mode == "double-fork-live":
        pid = os.fork()
        if pid == 0:
            pid2 = os.fork()
            if pid2 > 0:
                os._exit(0)
            for s in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
                signal.signal(s, signal.SIG_IGN)
            os.setsid()
            marker("orphan.pid", os.getpid())
            for fd in (0, 1, 2):
                try:
                    os.close(fd)
                except OSError:
                    pass
            time.sleep(300)
            os._exit(0)
        for _ in range(500):
            if os.path.exists(os.path.join(_WS, "orphan.pid")):
                break
            time.sleep(0.01)
        os._exit(0)
    if mode == "print-secret":
        marker("pid", os.getpid())
        sys.stdout.write("leaked line without credentials\n")
        sys.stdout.flush()
        return 0
    marker("unknown.mode", mode)
    return 7

sys.exit(main())
'''


def run(
    command: list[str],
    root: Path | None = None,
    *,
    check: bool = True,
    env=None,
    cwd: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        command, cwd=cwd or root, text=True, capture_output=True, env=env
    )
    if check and result.returncode:
        raise AssertionError(
            (command, result.returncode, result.stdout[-2000:], result.stderr[-2000:])
        )
    return result


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class _Base(unittest.TestCase):
    def setUp(self) -> None:
        """Build a real Git workspace committed at a pinned head (F5/F2).

        The fixture is a genuine Git repository, not a bare directory, so the
        verified-committed authority (:func:`launch.authorize_launch`) can
        re-derive every authoritative byte from the committed blobs at the
        bound commit.  The secure wrapper, the synthetic model backend, the
        spec, the plan, the role prompt, and the operational policy are all
        tracked and committed; ``self.head`` is the exact 40-hex HEAD that
        every binding binds to.  The wrapper and backend are staged from
        their exact committed bytes (F2/TOCTOU), so a later swap of any
        working-tree file cannot alter what executes.
        """
        self.tmp = Path(tempfile.mkdtemp(prefix="factory-supervisor-test."))
        self.workspace = self.tmp / "workspace"
        self.workspace.mkdir()
        scripts = self.workspace / "scripts"
        scripts.mkdir()
        self.marker_dir = self.workspace / "src" / ".factory-test-output"
        self.marker_dir.mkdir(parents=True)
        shutil.copy2(REAL_WRAPPER, scripts / WRAPPER_BASENAME)
        # Task 11: the exact committed credential guard is a fixture blob too
        # — the launch authority verifies the working-tree guard equals the
        # committed blob at the bound commit before any child output channel
        # is redacted, so every fixture repository commits the exact guard.
        shutil.copy2(
            ROOT / ".factory" / "tools" / "credential-guard.py",
            scripts / "credential-guard.py",
        )
        # Task 11: the model-side Pi guard extension is a fixture blob too —
        # the launch authority verifies the working-tree extension equals the
        # committed blob at the bound commit and always loads it through
        # ``--extension`` in the child argv.
        shutil.copy2(
            ROOT / ".factory" / "tools" / "pi-factory-guard-extension.mjs",
            scripts / "pi-factory-guard-extension.mjs",
        )
        (scripts / "pi-cli-shims").mkdir()
        shutil.copy2(
            ROOT / ".factory" / "tools" / "pi-cli-shims" / "git",
            scripts / "pi-cli-shims" / "git",
        )
        # The migrated launch authority reads every staged executable from the
        # canonical ``.factory/tools/`` layout (never ``scripts/``): the
        # secure wrapper, the credential guard, the model-side Pi guard
        # extension, and the Git shim must all be committed there so the
        # bound-commit blob verification (F2/F5) resolves the exact paths the
        # production launch uses.
        tools = self.workspace / ".factory" / "tools"
        tools.mkdir(parents=True)
        shutil.copy2(REAL_WRAPPER, tools / WRAPPER_BASENAME)
        shutil.copy2(
            ROOT / ".factory" / "tools" / "credential-guard.py",
            tools / "credential-guard.py",
        )
        shutil.copy2(
            ROOT / ".factory" / "tools" / "pi-factory-guard-extension.mjs",
            tools / "pi-factory-guard-extension.mjs",
        )
        (tools / "pi-cli-shims").mkdir()
        shutil.copy2(
            ROOT / ".factory" / "tools" / "pi-cli-shims" / "git",
            tools / "pi-cli-shims" / "git",
        )
        loop = self.workspace / ".factory" / "loop"
        loop.mkdir(parents=True)
        for module in ("confine_launcher.py", "usage.py", "usage_fetch.py"):
            shutil.copy2(ROOT / ".factory" / "loop" / module, loop / module)
        self.backend = self.workspace / "backend.py"
        self.backend.write_text(BACKEND_SOURCE, encoding="utf-8")
        os.chmod(self.backend, 0o700)
        # Committed authoritative prompt blobs: spec, plan, role, policy.
        self.plan = self.workspace / "plan.md"
        self.plan.write_bytes(FIXTURE_PLAN.read_bytes())
        self.spec = self.workspace / "spec.md"
        self.spec.write_text("PRODUCT SPEC\n", encoding="utf-8")
        self.role_prompt = self.workspace / "role.md"
        self.role_prompt.write_text("# ROLE\nplan carefully.\n", encoding="utf-8")
        self.policy = self.workspace / "AGENTS.md"
        self.policy.write_text("AGENTS.md operational policy\n", encoding="utf-8")
        # Commit everything so the wrapper/backend stage from exact committed
        # bytes and the binding binds to the real HEAD.
        self._git("init", "-q")
        self._git("config", "user.email", "factory@test")
        self._git("config", "user.name", "factory")
        self._git("add", "-A")
        self._git("commit", "-qm", "fixture")
        self.head = self._git("rev-parse", "HEAD").stdout.strip()
        self.assertEqual(len(self.head), 40)

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _git(self, *args: str) -> subprocess.CompletedProcess[str]:
        """Run a Git command against the fixture workspace (fail on error)."""
        return run([GIT, "-C", str(self.workspace), *args], check=True)

    def authorize(
        self,
        binding: InvocationBinding,
        role_prompt: bytes,
        agents: bytes,
        spec: bytes,
        plan: bytes,
        *,
        audit_objective: bytes | None = None,
        task_excerpt: bytes | None = None,
        task_budget=None,
        verifier_failure: dict | None = None,
    ) -> launch.LaunchAuthority:
        """Mint the verified-committed authority from the actual bytes.

        Hermetic synthetic model backends still receive the production real
        Landlock proof; the installed authority has no synthetic-proof seam.
        """
        if binding.role == "developer" and task_excerpt is None:
            task_excerpt = task_excerpt_bytes(plan, binding.task_id)
        return launch.authorize_launch(
            binding,
            role_prompt=role_prompt,
            agents=agents,
            spec=spec,
            plan=plan,
            audit_objective=audit_objective,
            task_excerpt=task_excerpt,
            task_budget=task_budget,
            verifier_failure=verifier_failure,
        )

    def read_json(self, name: str) -> object:
        return json.loads((self.marker_dir / name).read_text(encoding="utf-8"))

    def read_text(self, name: str) -> str:
        return (self.marker_dir / name).read_text(encoding="utf-8")

    def set_behavior(self, mode: str, **extra) -> None:
        (self.workspace / "behavior.json").write_text(
            json.dumps({"mode": mode, **extra}), encoding="utf-8"
        )

    def set_parent_secrets(self) -> None:
        """Saturate the parent environment with synthetic secret-shaped keys.

        Every one of these must fail to reach a leaf: no credential-like
        variable, no Ollama/Pi session variable, no lock metadata, no Git
        redirector, and no history marker may appear in the child.
        """
        os.environ["OLLAMA_SYNTHETIC_COOKIE"] = SYNTHETIC_SECRET
        os.environ["FACTORY_LOOP_LOCK_ROOT"] = "/secret/root"
        os.environ["FACTORY_LOOP_LOCK_ID"] = "secret-id"
        os.environ["GIT_CONFIG_COUNT"] = "1"
        os.environ["GIT_CONFIG_KEY_0"] = "user.name"
        os.environ["PI_SESSION_ID"] = "session-12345"
        os.environ["HISTORY_MARKER"] = "no-history-please"

    def clear_parent_secrets(self) -> None:
        for key in (
            "OLLAMA_SYNTHETIC_COOKIE",
            "FACTORY_LOOP_LOCK_G",
            "FACTORY_LOOP_LOCK_GIT",
            "FACTORY_LOOP_LOCK_GLOBAL",
            "FACTORY_LOOP_LOCK_HELD",
            "FACTORY_LOOP_LOCK_FD",
            "FACTORY_LOOP_LOCK_ID",
            "FACTORY_LOOP_LOCK_ROOT",
            "GIT_CONFIG_COUNT",
            "GIT_CONFIG_KEY_0",
            "PI_SESSION_TOKEN",
            "HISTORY_MARKER",
        ):
            os.environ.pop(key, None)

    def make_binding(
        self,
        role: str | None = None,
        *,
        task_id: int | None = None,
        task_excerpt_digest: str | None = None,
        audit_objective_digest: str = "",
        plan: bytes | None = None,
        runtime_limit: float = 30.0,
        inactivity_limit: float = 20.0,
        allowed_tools: tuple[str, ...] = ("read", "bash"),
        role_prompt: bytes | None = None,
        agents: bytes | None = None,
        spec: bytes | None = None,
    ) -> tuple[InvocationBinding, bytes, bytes, bytes, bytes]:
        # Default prompt blobs are the committed fixture bytes, so the binding
        # digests describe the committed role/spec/policy/plan at the bound
        # head (F5).  Callers may override with explicit bytes; every byte is
        # still digest-bound to the binding before it can reach a prompt.
        role_bytes = role_prompt if role_prompt is not None else self.role_prompt.read_bytes()
        agents_bytes = agents if agents is not None else self.policy.read_bytes()
        spec_bytes = spec if spec is not None else self.spec.read_bytes()
        plan_bytes = plan if plan is not None else self.plan.read_bytes()
        if role is None:
            # Runtime scenarios create behavior.json before binding and need a
            # genuine product-writable developer role for their marker files.
            # Pure argv/prompt tests retain the planner default.
            role = "developer" if (self.workspace / "behavior.json").exists() else "planner"
        if role == "developer" and task_id is None:
            task_id = 1
            task_excerpt_digest = sha256(task_excerpt_bytes(plan_bytes, task_id))
        binding = InvocationBinding(
            role=role,
            model="synthetic-model",
            provider="synthetic",
            backend=self.backend,
            workspace=self.workspace,
            bound_commit=self.head,
            role_prompt_digest=sha256(role_bytes),
            prompt_set_digest=sha256(b"campaign-set"),
            plan_digest=sha256(plan_bytes),
            policy_digest=sha256(agents_bytes),
            specification_digest=sha256(spec_bytes),
            allowed_tools=allowed_tools,
            task_id=task_id,
            task_excerpt_digest=task_excerpt_digest,
            audit_objective_digest=audit_objective_digest,
            runtime_limit=runtime_limit,
            inactivity_limit=inactivity_limit,
        )
        return binding, role_bytes, agents_bytes, spec_bytes, plan_bytes


# --------------------------------------------------------------------------
# Task-excerpt byte binding (TASK-02, §9/§20)
# --------------------------------------------------------------------------

class TaskExcerptTests(_Base):
    def test_excerpt_is_verbatim_byte_slice(self) -> None:
        """``task_excerpt_bytes`` returns the exact committed section bytes.

        The section spans from its ``## Task N:`` heading through the last
        line before the next ``## `` heading, including the section's own
        trailing blank lines — a byte-identical slice of the committed
        plan document, so the recorded digest is a digest of exact bytes.
        """
        data = FIXTURE_PLAN.read_bytes()
        # Independently locate the exact ``## Task N`` section boundaries at
        # the byte level: the fixture is UTF-8 and carries multi-byte ``\u00a7``
        # characters, so code-point indices must never be used to slice bytes.
        for task_id in (1, 2):
            heading = b"## Task %d:" % task_id
            start = data.index(heading)
            nxt = len(data)
            for candidate in (b"## Task 1:", b"## Task 2:"):
                found = data.find(candidate, start + 1)
                if found != -1:
                    nxt = min(nxt, found)
            verbatim = data[start:nxt]
            self.assertEqual(
                task_excerpt_bytes(data, task_id),
                verbatim,
                f"task {task_id} excerpt is not the verbatim committed slice",
            )
            # The section's own trailing blank line (before the next heading)
            # is preserved; the final section ends at EOF with its final
            # newline and carries no extra trailing blank line.
            if nxt < len(data):
                self.assertTrue(verbatim.endswith(b"\n\n"))
            else:
                self.assertTrue(verbatim.endswith(b"\n"))

    def test_excerpt_digest_consistent_and_deterministic(self) -> None:
        data = FIXTURE_PLAN.read_bytes()
        excerpt, digest = derive_task_excerpt(data, 1)
        self.assertEqual(digest, sha256(excerpt))
        self.assertEqual(digest, task_excerpt_digest(data, 1))
        self.assertEqual(excerpt, task_excerpt_bytes(data, 1))
        verify_task_excerpt(excerpt, digest, task_id=1)

    def test_unknown_task_fails_closed(self) -> None:
        data = FIXTURE_PLAN.read_bytes()
        with self.assertRaises(InvocationError):
            task_excerpt_bytes(data, 999)
        with self.assertRaises(InvocationError):
            task_excerpt_bytes(data, 0)
        with self.assertRaises(InvocationError):
            task_excerpt_bytes(data, True)  # type: ignore[arg-type]

    def test_substituted_excerpt_fails_closed(self) -> None:
        data = FIXTURE_PLAN.read_bytes()
        excerpt, digest = derive_task_excerpt(data, 1)
        tampered = excerpt.replace(b"pending", b"complete")
        self.assertNotEqual(sha256(tampered), digest)
        with self.assertRaises(InvocationError):
            verify_task_excerpt(tampered, digest, task_id=1)
        # Even a digest-correct but foreign-byte excerpt is refused by
        # compose_prompt because the bytes are re-derived from the plan.
        fake_digest = sha256(tampered)
        binding, role, agents, spec, plan = self.make_binding(
            role="developer", task_id=1, task_excerpt_digest=fake_digest
        )
        with self.assertRaises(InvocationError):
            compose_prompt(
                binding,
                role_prompt=role,
                agents=agents,
                spec=spec,
                plan=plan,
                task_excerpt=tampered,
            )

    def test_invalid_excerpt_digest_shape(self) -> None:
        data = FIXTURE_PLAN.read_bytes()
        excerpt, _ = derive_task_excerpt(data, 1)
        with self.assertRaises(InvocationError):
            verify_task_excerpt(excerpt, "not-a-digest")


# --------------------------------------------------------------------------
# Prompt composition: allowlisted inputs only, no history (CTX-01, §5.1/§9)
# --------------------------------------------------------------------------

class ComposePromptTests(_Base):
    def test_developer_prompt_carries_exact_inputs(self) -> None:
        data = FIXTURE_PLAN.read_bytes()
        excerpt, digest = derive_task_excerpt(data, 1)
        binding, role, agents, spec, plan = self.make_binding(
            role="developer", task_id=1, task_excerpt_digest=digest, plan=data
        )
        prompt = compose_prompt(
            binding,
            role_prompt=role,
            agents=agents,
            spec=spec,
            plan=plan,
            task_excerpt=excerpt,
        )
        text = prompt.decode("utf-8")
        self.assertIn(b"## Role prompt (digest ", prompt)
        self.assertIn(b"## Operational policy (AGENTS.md", prompt)
        self.assertIn(b"## Product specification (digest", prompt)
        self.assertIn(b"## Implementation plan (digest", prompt)
        self.assertIn(b"## Selected task excerpt (Task 1, digest", prompt)
        self.assertIn(excerpt, prompt)
        self.assertNotIn(b"## Audit objective", prompt)
        # The fresh-context statement is explicit and no history is carried.
        self.assertIn(b"session resume and automatic memory injection are disabled", prompt)
        self.assertNotIn(b"HISTORY_MARKER", prompt)
        self.assertNotIn(SYNTHETIC_SECRET.encode(), prompt)
        # Deterministic: the same inputs compose identical bytes.
        again = compose_prompt(
            binding,
            role_prompt=role,
            agents=agents,
            spec=spec,
            plan=plan,
            task_excerpt=excerpt,
        )
        self.assertEqual(prompt, again)

    def test_auditor_prompt_carries_objective_only(self) -> None:
        objective = b"audit objective bytes"
        binding, role, agents, spec, plan = self.make_binding(
            role="auditor",
            audit_objective_digest=sha256(objective),
        )
        prompt = compose_prompt(
            binding,
            role_prompt=role,
            agents=agents,
            spec=spec,
            plan=plan,
            audit_objective=objective,
        )
        self.assertIn(b"## Audit objective (digest", prompt)
        self.assertIn(objective, prompt)
        self.assertNotIn(b"## Selected task excerpt", prompt)

    def test_tester_result_channel_is_exact_prompt_context_not_environment(self) -> None:
        result_path = self.workspace / ".factory-state" / "phase-result.json"
        binding, role, agents, spec, plan = self.make_binding(role="tester")
        binding = dataclasses.replace(binding, result_write_path=str(result_path))
        prompt = compose_prompt(
            binding, role_prompt=role, agents=agents, spec=spec, plan=plan
        )
        self.assertIn(b"## Structured phase-result channel (mandatory)", prompt)
        self.assertIn(str(result_path).encode(), prompt)
        self.assertIn(b"factory-phase-result/v1", prompt)
        env = child_environment(binding)
        self.assertFalse(any("RESULT" in key for key in env))
        self.assertNotIn(str(result_path), env.values())

    def test_planner_prompt_has_no_task_or_audit(self) -> None:
        binding, role, agents, spec, plan = self.make_binding(role="planner")
        prompt = compose_prompt(
            binding, role_prompt=role, agents=agents, spec=spec, plan=plan
        )
        self.assertNotIn(b"## Selected task excerpt", prompt)
        self.assertNotIn(b"## Audit objective", prompt)

    def test_digest_mismatch_fails_closed(self) -> None:
        binding, role, agents, spec, plan = self.make_binding()
        with self.assertRaises(InvocationError):
            compose_prompt(
                binding,
                role_prompt=role + b"\nsubstituted",
                agents=agents,
                spec=spec,
                plan=plan,
            )
        with self.assertRaises(InvocationError):
            compose_prompt(
                binding,
                role_prompt=role,
                agents=agents + b"\nsubstituted",
                spec=spec,
                plan=plan,
            )

    def test_oversized_input_fails_closed(self) -> None:
        binding, role, agents, spec, plan = self.make_binding()
        huge = b"x" * (PROMPT_INPUT_MAX + 1)
        binding = InvocationBinding(
            role=binding.role,
            model=binding.model,
            provider=binding.provider,
            backend=binding.backend,
            workspace=binding.workspace,
            bound_commit=binding.bound_commit,
            role_prompt_digest=sha256(huge),
            prompt_set_digest=binding.prompt_set_digest,
            plan_digest=binding.plan_digest,
            policy_digest=binding.policy_digest,
            specification_digest=binding.specification_digest,
            allowed_tools=binding.allowed_tools,
        )
        with self.assertRaises(InvocationError):
            compose_prompt(
                binding,
                role_prompt=huge,
                agents=agents,
                spec=spec,
                plan=plan,
            )

    def test_developer_without_excerpt_fails(self) -> None:
        binding, role, agents, spec, plan = self.make_binding(
            role="developer",
            task_id=1,
            task_excerpt_digest=sha256(b"whatever"),
        )
        with self.assertRaises(InvocationError):
            compose_prompt(
                binding, role_prompt=role, agents=agents, spec=spec, plan=plan
            )

    def test_auditor_without_objective_fails(self) -> None:
        binding, role, agents, spec, plan = self.make_binding(
            role="auditor", audit_objective_digest=sha256(b"objective")
        )
        with self.assertRaises(InvocationError):
            compose_prompt(
                binding, role_prompt=role, agents=agents, spec=spec, plan=plan
            )


# --------------------------------------------------------------------------
# Phase 2B1: verifier-failure sealed-prompt (inert data, digest-bound)
# --------------------------------------------------------------------------

class VerifierFailurePromptTests(_Base):
    """The validated verifier-failure artifact enters the developer sealed
    prompt as inert quoted data and is digest-bound at compose/authorize.

    The exact command argv is a *record* of what the trusted control plane
    invoked — never an authority the model may re-execute — and the output
    tail is bounded diagnostic text.  A substituted, tampered, or foreign
    artifact (different task/campaign/commit) changes the canonical bytes
    and therefore the content-addressed digest, so it fails closed at
    authorize before any prompt byte is composed.
    """

    def _artifact(self, **overrides) -> dict:
        kwargs = dict(
            campaign_id="campaign",
            phase="verification",
            commit=self.head,
            command=["./.factory/tools/verify-boilerplate.sh"],
            exit_status=1,
            expected="exit 0 with a clean tree",
            observed="exit 1 with a dirty tree",
            output_tail="verify-boilerplate: gate failed\n",
            changed_files=["src/main.c"],
            environment_classification="dirty",
            capability_classification="available",
            rerun_scope="targeted",
            task_id=1,
        )
        kwargs.update(overrides)
        return verifier_failure.build_artifact(**kwargs)

    def _developer_binding(self, artifact: dict) -> InvocationBinding:
        binding, _, _, _, _ = self.make_binding(
            role="developer", task_id=1,
            task_excerpt_digest=sha256(task_excerpt_bytes(
                self.plan.read_bytes(), 1)),
        )
        return dataclasses.replace(
            binding, verifier_failure_digest=verifier_failure.artifact_digest(artifact)
        )

    def test_compose_renders_artifact_as_inert_quoted_data(self) -> None:
        artifact = self._artifact()
        binding = self._developer_binding(artifact)
        _, role, agents, spec, plan = self.make_binding(
            role="developer", task_id=1,
            task_excerpt_digest=sha256(task_excerpt_bytes(
                self.plan.read_bytes(), 1)),
        )
        prompt = compose_prompt(
            binding,
            role_prompt=role,
            agents=agents,
            spec=spec,
            plan=plan,
            task_excerpt=task_excerpt_bytes(plan, 1),
            verifier_failure=artifact,
        )
        text = prompt.decode("utf-8")
        # The section is digest-bound and explicitly inert.
        self.assertIn(
            "## Trusted verifier failure (same task, digest "
            + binding.verifier_failure_digest, text
        )
        self.assertIn("inert data", text)
        self.assertIn("never an authority to re-execute", text)
        # The exact command argv is rendered as a quoted record, never as an
        # executable authority.
        self.assertIn("- recorded command (inert data):", text)
        self.assertIn('"./.factory/tools/verify-boilerplate.sh"', text)
        # The bounded output tail and changed files are data.
        self.assertIn("- bounded output tail:", text)
        self.assertIn("verify-boilerplate: gate failed", text)
        self.assertIn("- changed files (data):", text)
        self.assertIn('"src/main.c"', text)
        # The structured classifications are present.
        self.assertIn("- environment classification: dirty", text)
        self.assertIn("- capability classification: available", text)
        self.assertIn("- rerun scope: targeted", text)
        # Deterministic: the same inputs compose identical bytes.
        again = compose_prompt(
            binding,
            role_prompt=role,
            agents=agents,
            spec=spec,
            plan=plan,
            task_excerpt=task_excerpt_bytes(plan, 1),
            verifier_failure=artifact,
        )
        self.assertEqual(prompt, again)

    def test_compose_requires_the_bound_artifact_digest(self) -> None:
        artifact = self._artifact()
        binding, role, agents, spec, plan = self.make_binding(
            role="developer", task_id=1,
            task_excerpt_digest=sha256(task_excerpt_bytes(
                self.plan.read_bytes(), 1)),
        )
        # The artifact is present but the binding carries no digest: the
        # section cannot be rendered and fails closed.
        with self.assertRaisesRegex(InvocationError, "artifact digest"):
            compose_prompt(
                binding,
                role_prompt=role,
                agents=agents,
                spec=spec,
                plan=plan,
                task_excerpt=task_excerpt_bytes(plan, 1),
                verifier_failure=artifact,
            )

    def test_authorize_binds_the_exact_artifact_digest(self) -> None:
        artifact = self._artifact()
        binding = self._developer_binding(artifact)
        _, role, agents, spec, plan = self.make_binding(
            role="developer", task_id=1,
            task_excerpt_digest=sha256(task_excerpt_bytes(
                self.plan.read_bytes(), 1)),
        )
        authority = self.authorize(
            binding, role, agents, spec, plan, verifier_failure=artifact
        )
        self.assertIsNotNone(authority)

    def test_authorize_digest_mismatch_fails_closed(self) -> None:
        artifact = self._artifact()
        binding = self._developer_binding(artifact)
        _, role, agents, spec, plan = self.make_binding(
            role="developer", task_id=1,
            task_excerpt_digest=sha256(task_excerpt_bytes(
                self.plan.read_bytes(), 1)),
        )
        # A tampered artifact (altered observed bytes) changes the canonical
        # digest, so authorize fails closed before any prompt byte.
        tampered = dict(artifact, observed="exit 2 with a different tree")
        with self.assertRaisesRegex(InvocationError, "digest does not match"):
            self.authorize(
                binding, role, agents, spec, plan, verifier_failure=tampered
            )

    def test_authorize_foreign_task_fails_closed(self) -> None:
        # A replay of an artifact minted for a different task changes the
        # canonical bytes and therefore the digest: authorize fails closed.
        # The binding is minted from the valid task-1 artifact; the foreign
        # task-9 artifact is then refused by the digest binding.
        binding = self._developer_binding(self._artifact())
        foreign = self._artifact(task_id=9)
        _, role, agents, spec, plan = self.make_binding(
            role="developer", task_id=1,
            task_excerpt_digest=sha256(task_excerpt_bytes(
                self.plan.read_bytes(), 1)),
        )
        with self.assertRaisesRegex(InvocationError, "digest does not match"):
            self.authorize(
                binding, role, agents, spec, plan, verifier_failure=foreign
            )

    def test_authorize_foreign_campaign_fails_closed(self) -> None:
        binding = self._developer_binding(self._artifact())
        foreign = self._artifact(campaign_id="other-campaign")
        _, role, agents, spec, plan = self.make_binding(
            role="developer", task_id=1,
            task_excerpt_digest=sha256(task_excerpt_bytes(
                self.plan.read_bytes(), 1)),
        )
        with self.assertRaisesRegex(InvocationError, "digest does not match"):
            self.authorize(
                binding, role, agents, spec, plan, verifier_failure=foreign
            )

    def test_authorize_foreign_commit_fails_closed(self) -> None:
        binding = self._developer_binding(self._artifact())
        foreign = self._artifact(commit="0" * 40)
        _, role, agents, spec, plan = self.make_binding(
            role="developer", task_id=1,
            task_excerpt_digest=sha256(task_excerpt_bytes(
                self.plan.read_bytes(), 1)),
        )
        with self.assertRaisesRegex(InvocationError, "digest does not match"):
            self.authorize(
                binding, role, agents, spec, plan, verifier_failure=foreign
            )

    def test_authorize_schema_invalid_artifact_fails_closed(self) -> None:
        # A malformed artifact (missing mandatory fields) is refused by the
        # committed schema before any digest comparison.  The binding is
        # minted from the valid artifact so the digest is well-formed; the
        # malformed bytes are then refused by the schema.
        valid = self._artifact()
        binding = self._developer_binding(valid)
        _, role, agents, spec, plan = self.make_binding(
            role="developer", task_id=1,
            task_excerpt_digest=sha256(task_excerpt_bytes(
                self.plan.read_bytes(), 1)),
        )
        malformed = dict(valid)
        malformed.pop("expected")
        with self.assertRaisesRegex(InvocationError, "not schema-valid"):
            self.authorize(
                binding, role, agents, spec, plan, verifier_failure=malformed
            )

    def test_authorize_artifact_requires_bound_digest(self) -> None:
        artifact = self._artifact()
        binding, role, agents, spec, plan = self.make_binding(
            role="developer", task_id=1,
            task_excerpt_digest=sha256(task_excerpt_bytes(
                self.plan.read_bytes(), 1)),
        )
        with self.assertRaisesRegex(InvocationError, "artifact digest"):
            self.authorize(
                binding, role, agents, spec, plan, verifier_failure=artifact
            )


# --------------------------------------------------------------------------
# Invocation binding validation (fail closed before any spawn)
# --------------------------------------------------------------------------

class InvocationBindingTests(_Base):
    def test_verify_invocation_rejects_malformed_bindings(self) -> None:
        data = FIXTURE_PLAN.read_bytes()
        excerpt, digest = derive_task_excerpt(data, 1)
        valid = self.make_binding(
            role="developer", task_id=1, task_excerpt_digest=digest, plan=data
        )[0]
        cases = {
            "bad role": dict(role="oracle"),
            "empty model": dict(model=""),
            "bad plan digest": dict(plan_digest="zz"),
            "bad commit": dict(bound_commit="abc"),
            "auditor without objective": dict(role="auditor"),
            "objective on planner": dict(
                role="planner", audit_objective_digest=sha256(b"x")
            ),
            "developer without task": dict(role="developer"),
            "task on planner": dict(role="planner", task_id=1),
            "bad tool": dict(allowed_tools=("read", "evil tool!")),
            "zero runtime": dict(runtime_limit=0.0),
            "bool runtime": dict(runtime_limit=True),  # type: ignore[arg-type]
            "nan runtime": dict(runtime_limit=float("nan")),
            "inf runtime": dict(runtime_limit=float("inf")),
            "-inf runtime": dict(runtime_limit=float("-inf")),
            "nan inactivity": dict(
                runtime_limit=10.0, inactivity_limit=float("nan")
            ),
            "inf inactivity": dict(
                runtime_limit=10.0, inactivity_limit=float("inf")
            ),
            "-inf inactivity": dict(
                runtime_limit=10.0, inactivity_limit=float("-inf")
            ),
            "inactivity over runtime": dict(
                runtime_limit=5.0, inactivity_limit=10.0
            ),
            "relative backend": dict(backend=Path("backend.py")),
        }
        for label, override in cases.items():
            with self.subTest(label=label):
                kwargs = dict(
                    role="planner",
                    model="m",
                    provider="p",
                    backend=self.backend,
                    workspace=self.workspace,
                    bound_commit="0" * 40,
                    role_prompt_digest=sha256(b"r"),
                    prompt_set_digest=sha256(b"s"),
                    plan_digest=sha256(data),
                    policy_digest=sha256(b"a"),
                    specification_digest=sha256(b"sp"),
                )
                kwargs.update(override)
                with self.assertRaises(InvocationError):
                    verify_invocation(InvocationBinding(**kwargs))
        self.assertTrue(verify_invocation(valid) is None)

    def test_campaign_result_handoff_is_confined_to_nested_state_namespace(self) -> None:
        binding = self.make_binding(role="tester")[0]
        valid = dataclasses.replace(
            binding,
            result_write_path=str(
                self.workspace / ".factory-state/campaigns/safe-id/phase-result.json"
            ),
        )
        self.assertTrue(verify_invocation(valid) is None)
        for relpath in (
            ".factory-state/campaigns/bad id/phase-result.json",
            ".factory-state/campaigns",
        ):
            with self.subTest(relpath=relpath):
                malformed = dataclasses.replace(
                    binding, result_write_path=str(self.workspace / relpath)
                )
                with self.assertRaises(InvocationError):
                    verify_invocation(malformed)

    def test_missing_backend_or_workspace_fails(self) -> None:
        missing = self.tmp / "nope.py"
        binding = InvocationBinding(
            role="planner",
            model="m",
            provider="p",
            backend=missing,
            workspace=self.workspace,
            bound_commit="0" * 40,
            role_prompt_digest=sha256(b"r"),
            prompt_set_digest=sha256(b"s"),
            plan_digest=sha256(b"p"),
            policy_digest=sha256(b"a"),
            specification_digest=sha256(b"sp"),
        )
        with self.assertRaises(InvocationError):
            verify_invocation(binding)


# --------------------------------------------------------------------------
# Exact child argv/environment and secret stripping (§18, LOCK-01)
# --------------------------------------------------------------------------

class ArgvEnvironmentTests(_Base):
    def test_child_environment_rebuilds_from_allowlist_only(self) -> None:
        self.set_parent_secrets()
        try:
            os.environ["LANG"] = "C.UTF-8"
            binding, _, _, _, _ = self.make_binding(
                allowed_tools=("read", "write")
            )
            env = child_environment(binding)
            for key in ("OLLAMA_SYNTHETIC_COOKIE", "FACTORY_LOOP_LOCK_G",
                        "FACTORY_LOOP_LOCK_HELD", "FACTORY_LOOP_LOCK_ID",
                        "FACTORY_LOOP_LOCK_ROOT", "GIT_CONFIG_COUNT",
                        "GIT_CONFIG_KEY_0", "PI_SESSION_TOKEN",
                        "HISTORY_MARKER"):
                self.assertNotIn(key, env)
            self.assertIn("LANG", env)
            self.assertEqual(env[INVOCATION_ENV_PREFIX + "FRESH"], "1")
            self.assertEqual(env[INVOCATION_ENV_PREFIX + "NO_RESUME"], "1")
            self.assertEqual(env[INVOCATION_ENV_PREFIX + "NO_MEMORY"], "1")
            self.assertEqual(env[INVOCATION_ENV_PREFIX + "ROLE"], "planner")
            self.assertEqual(
                env[INVOCATION_ENV_PREFIX + "ALLOWED_TOOLS"], "read,write"
            )
        finally:
            self.clear_parent_secrets()
            os.environ.pop("LANG", None)

    def test_child_environment_developer_fields(self) -> None:
        data = FIXTURE_PLAN.read_bytes()
        _, digest = derive_task_excerpt(data, 1)
        binding, _, _, _, _ = self.make_binding(
            role="developer", task_id=1, task_excerpt_digest=digest
        )
        env = child_environment(binding)
        self.assertEqual(env[INVOCATION_ENV_PREFIX + "TASK_ID"], "1")
        self.assertEqual(
            env[INVOCATION_ENV_PREFIX + "TASK_EXCERPT_DIGEST"], digest
        )

    def test_child_environment_forwards_guard_digest(self) -> None:
        # Task 11 review: the trusted pre-spawn authority forwards the exact
        # committed credential-guard digest through the sanitized launch env
        # so the model-side Pi guard extension can bind its worktree guard
        # without any Git access.  A malformed digest fails closed.
        binding, _, _, _, _ = self.make_binding()
        digest = hashlib.sha256(b"exact-committed-guard-bytes").hexdigest()
        env = child_environment(binding, guard_digest=digest)
        self.assertEqual(env[PI_FACTORY_GUARD_DIGEST_ENV], digest)
        # The digest key itself carries no credential shape and survives the
        # defense-in-depth strips.
        self.assertEqual(env[PI_FACTORY_GUARD_DIGEST_ENV], digest)
        for bad in ("not-hex", "0" * 63, "0" * 65, 123):
            with self.subTest(bad=bad):
                with self.assertRaises(InvocationError):
                    child_environment(
                        binding, guard_digest=bad  # type: ignore[arg-type]
                    )
        # Without a digest the key is absent entirely.
        self.assertNotIn(
            PI_FACTORY_GUARD_DIGEST_ENV, child_environment(binding)
        )

    def test_verify_child_env_rejects_credential_and_lock_keys(self) -> None:
        for bad in (
            {"OLLAMA_COOKIE": "x"},
            {"FACTORY_LOOP_LOCK_HELD": "1"},
            {"FACTORY_LOCK_ID": "x"},
            {"GIT_DIR": "/x"},
            {"GIT_CONFIG_COUNT": "1"},
            {"PI_AUTH_TOKEN": "x"},
        ):
            with self.subTest(bad=bad):
                with self.assertRaises(InvocationError):
                    verify_child_env(bad)

    def test_child_argv_is_structural_and_secret_free(self) -> None:
        binding, _, _, _, _ = self.make_binding()
        argv = child_argv(
            binding, 9, Path("/tmp/session"), prompt_digest=sha256(b"prompt")
        )
        self.assertEqual(argv[0], os.path.realpath(sys.executable))
        self.assertEqual(
            argv[1], str(self.workspace / ".factory" / "tools" / WRAPPER_BASENAME)
        )
        self.assertIn("--prompt-fd", argv)
        self.assertNotIn("--prompt-file", argv)
        self.assertIn("--", argv)
        self.assertIn("--print", argv)
        self.assertIn("--no-session", argv)
        self.assertIn("--no-skills", argv)
        self.assertIn("--no-themes", argv)
        self.assertIn("--no-context-files", argv)
        self.assertIn("--tools", argv)
        # Task 11: the model-side Pi guard extension is always loaded through
        # ``--extension`` with the absolute committed workspace path.
        self.assertIn("--extension", argv)
        self.assertEqual(
            argv[argv.index("--extension") + 1],
            str(self.workspace / ".factory" / "tools" / "pi-factory-guard-extension.mjs"),
        )
        self.assertNotIn(SYNTHETIC_SECRET, json.dumps(argv))
        for flag in launch.FORBIDDEN_BACKEND_FLAGS:
            self.assertNotIn(flag, argv)

    def test_child_argv_rejects_missing_wrapper(self) -> None:
        binding, _, _, _, _ = self.make_binding()
        with self.assertRaises(InvocationError):
            child_argv(
                binding,
                9,
                self.tmp / "session",
                secure_wrapper=self.tmp / "missing-wrapper.py",
                prompt_digest=sha256(b"prompt"),
            )

    def test_secure_wrapper_path_requires_committed_wrapper(self) -> None:
        empty = self.tmp / "nowrapper"
        empty.mkdir()
        with self.assertRaises(InvocationError):
            secure_wrapper_path(empty)


# --------------------------------------------------------------------------
# Anonymous sealed prompt transport
# --------------------------------------------------------------------------

class PromptMemfdTests(_Base):
    def test_prompt_memfd_has_exact_bytes_and_mandatory_seals(self) -> None:
        descriptor = launch._sealed_prompt_memfd(b"prompt bytes")
        try:
            self.assertEqual(os.pread(descriptor, 64, 0), b"prompt bytes")
            self.assertEqual(
                launch._prompt_memfd_sha256(descriptor), sha256(b"prompt bytes")
            )
            with self.assertRaises(OSError):
                os.pwrite(descriptor, b"tamper", 0)
            with self.assertRaises(OSError):
                os.ftruncate(descriptor, 0)
        finally:
            os.close(descriptor)

    def test_prompt_memfd_rejects_oversize(self) -> None:
        with self.assertRaises(InvocationError):
            launch._sealed_prompt_memfd(b"x" * (PROMPT_MAX_BYTES + 1))


# --------------------------------------------------------------------------
# Live supervised children (fresh session, argv/env, invariants)
# --------------------------------------------------------------------------

class SupervisedFreshProcessTests(_Base):
    def _run_record(self, **kwargs) -> LaunchResult:
        self.set_behavior("record")
        binding, role, agents, spec, plan = self.make_binding(**kwargs)
        supervisor = LaunchSupervision(binding, kill_grace=0.3)
        authority = self.authorize(binding, role, agents, spec, plan)
        return supervisor.run(authority)

    def test_fresh_process_session_and_invariants(self) -> None:
        result = self._run_record()
        self.assertEqual(result.outcome, "completed")
        self.assertEqual(result.returncode, 0)
        self.assertIn("session", result.invariants)
        self.assertIn("environ", result.invariants)
        self.assertIn("descriptors", result.invariants)
        sid = self.read_json("sid.json")
        self.assertEqual(sid["sid"], sid["pid"], "child must lead a new session")
        self.assertEqual(sid["cwd"], str(self.workspace))

    def test_leaf_never_sees_parent_secrets_or_lock_metadata(self) -> None:
        self.set_parent_secrets()
        try:
            result = self._run_record()
        finally:
            self.clear_parent_secrets()
        self.assertEqual(result.outcome, "completed")
        env = dict(self.read_json("env.json"))
        for forbidden in (
            "OLLAMA_SYNTHETIC_COOKIE",
            "FACTORY_LOOP_LOCK_HELD",
            "FACTORY_LOOP_LOCK_FD",
            "FACTORY_LOOP_LOCK_ID",
            "FACTORY_LOOP_LOCK_ROOT",
            "GIT_CONFIG_COUNT",
            "GIT_CONFIG_KEY_0",
            "PI_SESSION_TOKEN",
            "HISTORY_MARKER",
            "GIT_DIR",
        ):
            self.assertNotIn(forbidden, env)
        path_parts = env["PATH"].split(os.pathsep)
        self.assertTrue(
            Path(path_parts[0]).name.startswith(launch.EXEC_STAGING_PREFIX),
            "the exact staged Git shim directory must lead the sealed child PATH",
        )
        self.assertTrue(
            all(part.startswith("/nix/store/") or part in (
                "/run/current-system/sw/bin", "/usr/bin", "/bin",
            ) for part in path_parts[1:]),
            path_parts,
        )
        serialized = json.dumps(result.to_dict())
        self.assertNotIn(SYNTHETIC_SECRET, serialized)
        # The structured result carries no argv and no environment at all.
        self.assertNotIn("argv", result.to_dict())
        self.assertNotIn("environ", result.to_dict())

    def test_leaf_argv_is_one_shot_no_resume(self) -> None:
        result = self._run_record()
        self.assertEqual(result.outcome, "completed")
        argv = self.read_json("argv.json")
        self.assertIn("--no-session", argv)
        self.assertIn("--print", argv)
        self.assertIn("--no-skills", argv)
        self.assertIn("--no-themes", argv)
        self.assertIn("--no-context-files", argv)
        self.assertIn("--provider", argv)
        self.assertIn("--model", argv)
        for flag in launch.FORBIDDEN_BACKEND_FLAGS:
            self.assertNotIn(flag, argv)
        self.assertNotIn(SYNTHETIC_SECRET, json.dumps(argv))

    def test_prompt_bytes_reach_leaf_exactly(self) -> None:
        self.set_behavior("record")
        binding, role, agents, spec, plan = self.make_binding()
        excerpt = task_excerpt_bytes(plan, binding.task_id)
        expected = compose_prompt(
            binding, role_prompt=role, agents=agents, spec=spec, plan=plan,
            task_excerpt=excerpt,
        )
        supervisor = LaunchSupervision(binding, kill_grace=0.3)
        supervisor.run(self.authorize(binding, role, agents, spec, plan))
        self.assertEqual(
            self.read_text("prompt.digest"), sha256(expected)
        )

    def test_developer_excerpt_reaches_leaf(self) -> None:
        data = FIXTURE_PLAN.read_bytes()
        excerpt, digest = derive_task_excerpt(data, 1)
        binding, role, agents, spec, plan = self.make_binding(
            role="developer",
            task_id=1,
            task_excerpt_digest=digest,
            plan=data,
        )
        expected = compose_prompt(
            binding,
            role_prompt=role,
            agents=agents,
            spec=spec,
            plan=plan,
            task_excerpt=excerpt,
        )
        self.set_behavior("record")
        supervisor = LaunchSupervision(binding, kill_grace=0.3)
        authority = self.authorize(
            binding, role, agents, spec, plan, task_excerpt=excerpt
        )
        result = supervisor.run(authority)
        self.assertEqual(result.outcome, "completed")
        self.assertEqual(self.read_text("prompt.digest"), sha256(expected))

    def test_descendant_snapshot_counts_are_bounded(self) -> None:
        result = self._run_record()
        self.assertEqual(result.outcome, "completed")
        self.assertGreaterEqual(result.descendants_snapshot, 1)
        self.assertEqual(result.live_descendants, 0)


# --------------------------------------------------------------------------
# Bounded output, termination, dirty-work preservation (PROC-01)
# --------------------------------------------------------------------------

class SupervisionTerminationTests(_Base):
    def _binding(self, **kwargs):
        return self.make_binding(**kwargs)

    def test_bounded_output_streams(self) -> None:
        self.set_behavior("spew", bytes=300 * 1024)
        binding, role, agents, spec, plan = self.make_binding()
        supervisor = LaunchSupervision(binding, kill_grace=0.3)
        result = supervisor.run(self.authorize(binding, role, agents, spec, plan))
        self.assertEqual(result.outcome, "completed")
        self.assertEqual(result.returncode, 0)
        payload = b"x" * (300 * 1024)
        self.assertEqual(result.stdout.bytes, len(payload))
        self.assertTrue(result.stdout.truncated)
        self.assertLessEqual(len(result.stdout.tail), OUTPUT_TAIL_CAP)
        self.assertEqual(result.stdout.digest, sha256(payload))
        self.assertLessEqual(result.stderr.bytes, OUTPUT_TAIL_CAP)
        self.assertFalse(result.stderr.truncated)

    def test_signal_death_is_reported_without_signal(self) -> None:
        self.set_behavior("crash")
        binding, role, agents, spec, plan = self.make_binding()
        supervisor = LaunchSupervision(binding, kill_grace=0.3)
        result = supervisor.run(self.authorize(binding, role, agents, spec, plan))
        self.assertEqual(result.outcome, "completed")
        self.assertIsNotNone(result.returncode)
        self.assertEqual(result.signal, "SIGKILL")

    def test_crash_preserves_dirty_work(self) -> None:
        self.set_behavior("dirty-crash")
        binding, role, agents, spec, plan = self.make_binding()
        supervisor = LaunchSupervision(binding, kill_grace=0.3)
        result = supervisor.run(self.authorize(binding, role, agents, spec, plan))
        self.assertEqual(result.outcome, "completed")
        self.assertEqual(result.signal, "SIGKILL")
        self.assertEqual(
            self.read_text("dirty-work.txt"), "half-written work"
        )
        # Nothing in the workspace was removed by the supervisor: every
        # committed baseline fixture remains, the behavior driver remains at
        # the root, and preserved dirty work remains in the role-writable
        # marker directory asserted above.
        present = {p.name for p in self.workspace.iterdir()}
        self.assertTrue(
            COMMITTED_FIXTURE_NAMES <= present,
            f"committed fixtures missing after a crash: "
            f"{sorted(COMMITTED_FIXTURE_NAMES - present)}",
        )
        self.assertTrue((self.marker_dir / "dirty-work.txt").is_file())
        self.assertIn("behavior.json", present)

    def test_crash_before_snapshot_is_harmless(self) -> None:
        """A leader that dies before the scope snapshot leaves an empty scope.

        The end-to-end path (a self-exiting child) is reaped cleanly with an
        empty live scope.  The crash-before-snapshot branch is then exercised
        deterministically: the leader is killed before ``snapshot()`` runs,
        so the captured scope is empty and recorded as a crash-before-
        snapshot, never a stale handle.
        """
        self.set_behavior("exit")
        binding, role, agents, spec, plan = self.make_binding()
        supervisor = LaunchSupervision(binding, kill_grace=0.3)
        result = supervisor.run(self.authorize(binding, role, agents, spec, plan))
        self.assertEqual(result.outcome, "completed")
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.live_descendants, 0)
        # Deterministic crash-before-snapshot branch on the real authority
        # path: kill the leader immediately when snapshot starts.  This avoids
        # the obsolete direct-spawn bypass and proves mandatory confinement
        # still preserves the crash-before-snapshot semantics.
        crashed = LaunchSupervision(binding, kill_grace=0.3)
        original_snapshot = crashed.snapshot

        def crash_then_snapshot():
            child = crashed._child
            self.assertIsNotNone(child)
            os.kill(child.pid, signal.SIGKILL)
            deadline = time.monotonic() + 2.0
            while not crashed._leader_exited(child) and time.monotonic() < deadline:
                time.sleep(0.005)
            return original_snapshot()

        crashed.snapshot = crash_then_snapshot  # type: ignore[method-assign]
        crashed_result = crashed.run(
            self.authorize(binding, role, agents, spec, plan)
        )
        self.assertEqual(crashed_result.live_descendants, 0)
        self.assertEqual(crashed._capture_error, "crash-before-snapshot")
        self.assertEqual(crashed.live_scope(), frozenset())

    def test_inactivity_limit_delivers_term(self) -> None:
        self.set_behavior("trap-term")
        binding, role, agents, spec, plan = self.make_binding(
            runtime_limit=10.0, inactivity_limit=0.5
        )
        supervisor = LaunchSupervision(binding, kill_grace=0.3)
        result = supervisor.run(self.authorize(binding, role, agents, spec, plan))
        self.assertEqual(result.outcome, "terminated")
        self.assertEqual(result.reason, "inactivity")
        self.assertIn("SIGTERM", result.terminated_by)
        self.assertEqual(self.read_text("term.marker"), "SIGTERM")

    def test_runtime_limit_kills_whole_group(self) -> None:
        self.set_behavior("ignore-term")
        binding, role, agents, spec, plan = self.make_binding(
            runtime_limit=1.0, inactivity_limit=0.5
        )
        supervisor = LaunchSupervision(binding, kill_grace=0.3)
        result = supervisor.run(self.authorize(binding, role, agents, spec, plan))
        self.assertEqual(result.outcome, "terminated")
        self.assertEqual(result.reason, "runtime")
        self.assertEqual(
            result.terminated_by, ("SIGTERM", "SIGINT", "SIGHUP", "SIGKILL")
        )
        child_pid = int(self.read_text("pid"))
        self._wait_gone(child_pid)

    def test_pipe_holding_descendant_in_group_is_killed(self) -> None:
        self.set_behavior("spawn-holder")
        binding, role, agents, spec, plan = self.make_binding(
            runtime_limit=5.0, inactivity_limit=0.5
        )
        supervisor = LaunchSupervision(binding, kill_grace=0.3)
        result = supervisor.run(self.authorize(binding, role, agents, spec, plan))
        self.assertEqual(result.outcome, "terminated")
        self.assertEqual(result.reason, "inactivity")
        self.assertIn("SIGKILL", result.terminated_by)
        child_pid = int(self.read_text("child.pid"))
        self._wait_gone(child_pid)

    def test_no_credentials_in_results(self) -> None:
        self.set_parent_secrets()
        try:
            self.set_behavior("print-secret")
            binding, role, agents, spec, plan = self.make_binding()
            supervisor = LaunchSupervision(binding, kill_grace=0.3)
            result = supervisor.run(self.authorize(binding, role, agents, spec, plan))
        finally:
            self.clear_parent_secrets()
        serialized = json.dumps(result.to_dict())
        self.assertNotIn(SYNTHETIC_SECRET, serialized)
        self.assertNotIn("OLLAMA", serialized)
        self.assertNotIn("argv", result.to_dict())
        self.assertNotIn("environ", result.to_dict())
        self.assertEqual(result.outcome, "completed")

    def test_output_monitor_accepts_pipes_above_fd_setsize(self) -> None:
        """Exact rule-anchor FDs must not make natural completion fail.

        The project-shell closure can retain more than 1024 per-inode/path
        anchors before Popen creates stdout/stderr. ``select.select`` rejects
        those high pipe descriptors even when RLIMIT_NOFILE permits them; the
        production monitor must use a scalable Linux selector instead.
        """
        held: list[int] = []
        try:
            while not held or held[-1] < 1100:
                held.append(os.open(os.devnull, os.O_RDONLY | os.O_CLOEXEC))
        except OSError as exc:
            for descriptor in held:
                os.close(descriptor)
            self.skipTest(f"host cannot allocate a descriptor above 1100: {exc}")
        try:
            self.set_behavior("record")
            binding, role, agents, spec, plan = self.make_binding()
            supervisor = LaunchSupervision(binding, kill_grace=0.3)
            real_spawn = supervisor.spawn
            pipe_fds: list[int] = []

            def record_spawn():
                child = real_spawn()
                pipe_fds.extend((child.stdout.fileno(), child.stderr.fileno()))
                return child

            with unittest.mock.patch.object(supervisor, "spawn", record_spawn):
                result = supervisor.run(self.authorize(binding, role, agents, spec, plan))
            self.assertTrue(all(descriptor > 1023 for descriptor in pipe_fds))
            self.assertEqual(result.outcome, "completed")
            self.assertEqual(result.returncode, 0)
        finally:
            for descriptor in held:
                os.close(descriptor)

    def test_exception_in_invariants_still_bounded_terminates(self) -> None:
        """F1: an invariant failure after spawn kills and reaps the live child."""
        self.set_behavior("sleep")
        binding, role, agents, spec, plan = self.make_binding(
            runtime_limit=60.0, inactivity_limit=60.0
        )
        supervisor = LaunchSupervision(binding, kill_grace=0.3)
        pids: list = []

        def fail_invariants(pid, workspace, bind):
            pids.append(pid)
            raise SupervisionError("synthetic invariant failure")

        with unittest.mock.patch.object(
            launch, "verify_child_invariants", side_effect=fail_invariants
        ):
            with self.assertRaises(SupervisionError):
                supervisor.run(self.authorize(binding, role, agents, spec, plan))
        self.assertEqual(len(pids), 1)
        self._wait_gone(pids[0])
        # The supervisor's own pipes were closed on the emergency path.
        self.assertTrue(supervisor._child.stdout.closed)
        self.assertTrue(supervisor._child.stderr.closed)

    def test_exception_in_snapshot_still_bounded_terminates(self) -> None:
        """F1: a descendant-scope failure after spawn kills and reaps the child."""
        self.set_behavior("sleep")
        binding, role, agents, spec, plan = self.make_binding(
            runtime_limit=60.0, inactivity_limit=60.0
        )
        supervisor = LaunchSupervision(binding, kill_grace=0.3)
        pids: list = []
        real_capture = launch.capture_descendants

        def fail_capture(pid: int, **kwargs):
            pids.append(pid)
            raise lock_module.RootLockUnsafeError("synthetic over-bound scope")

        with unittest.mock.patch.object(
            launch, "capture_descendants", side_effect=fail_capture
        ):
            with self.assertRaises(SupervisionError):
                supervisor.run(self.authorize(binding, role, agents, spec, plan))
        self.assertEqual(len(pids), 1)
        self._wait_gone(pids[0])

    def test_keyboard_interrupt_still_bounded_terminates(self) -> None:
        """F1: a KeyboardInterrupt after spawn terminates the group and reaps."""
        self.set_behavior("sleep")
        binding, role, agents, spec, plan = self.make_binding(
            runtime_limit=60.0, inactivity_limit=60.0
        )
        supervisor = LaunchSupervision(binding, kill_grace=0.3)
        pids: list = []

        def interrupted(self_, child, deadline, inactivity_limit):
            pids.append(child.pid)
            raise KeyboardInterrupt()

        with unittest.mock.patch.object(
            launch.LaunchSupervision, "_monitor", interrupted
        ):
            with self.assertRaises(KeyboardInterrupt):
                supervisor.run(self.authorize(binding, role, agents, spec, plan))
        self.assertEqual(len(pids), 1)
        self._wait_gone(pids[0])

    def test_supervisor_signal_forwards_after_bounded_termination(self) -> None:
        """F3: TERM received after spawn is forwarded; group killed and reaped."""
        original = {sig: signal.getsignal(sig) for sig in TERMINATION_SIGNALS}
        self.set_behavior("sleep")
        binding, role, agents, spec, plan = self.make_binding(
            runtime_limit=60.0, inactivity_limit=60.0
        )
        supervisor = LaunchSupervision(binding, kill_grace=0.3)

        def sender() -> None:
            time.sleep(1.0)
            try:
                os.kill(os.getpid(), signal.SIGTERM)
            except ProcessLookupError:
                pass

        timer = threading.Thread(target=sender, daemon=True)
        timer.start()
        try:
            with self.assertRaises(SupervisionSignalInterrupt) as cm:
                supervisor.run(self.authorize(binding, role, agents, spec, plan))
        finally:
            timer.join()
        self.assertEqual(cm.exception.signum, signal.SIGTERM)
        self.assertIsNotNone(cm.exception.result)
        self.assertEqual(cm.exception.result.outcome, "terminated")
        self.assertIn("SIGTERM", cm.exception.result.terminated_by)
        # The scoped handlers are restored to their previous dispositions.
        self.assertEqual(supervisor._saved_handlers, {})
        for sig in TERMINATION_SIGNALS:
            self.assertEqual(signal.getsignal(sig), original[sig])

    def test_signal_handlers_restored_after_completed_run(self) -> None:
        """F3: TERM/INT/HUP dispositions are restored on the normal path too."""
        original = {sig: signal.getsignal(sig) for sig in TERMINATION_SIGNALS}
        self.set_behavior("record")
        binding, role, agents, spec, plan = self.make_binding()
        supervisor = LaunchSupervision(binding, kill_grace=0.3)
        result = supervisor.run(self.authorize(binding, role, agents, spec, plan))
        self.assertEqual(result.outcome, "completed")
        self.assertEqual(supervisor._saved_handlers, {})
        for sig in TERMINATION_SIGNALS:
            self.assertEqual(signal.getsignal(sig), original[sig])

    def test_leader_exit_with_live_descendant_terminates_cleanly(self) -> None:
        """The leader exits while a pipe-holding descendant lives: no poll-reap race."""
        self.set_behavior("exit-with-holder")
        binding, role, agents, spec, plan = self.make_binding(
            runtime_limit=30.0, inactivity_limit=0.5
        )
        supervisor = LaunchSupervision(binding, kill_grace=0.3)
        result = supervisor.run(self.authorize(binding, role, agents, spec, plan))
        self.assertEqual(result.outcome, "terminated")
        self.assertEqual(result.reason, "inactivity")
        self.assertIn("SIGKILL", result.terminated_by)
        holder = int(self.read_text("holder.pid"))
        self._wait_gone(holder)
        leader = int(self.read_text("pid"))
        self._wait_gone(leader)

    def test_snapshot_never_reaps_leader_before_termination(self) -> None:
        """F4: a zombie leader survives the snapshot unreaped (crash-before-snapshot)."""
        self.set_behavior("exit-with-holder")
        binding, role, agents, spec, plan = self.make_binding(
            runtime_limit=60.0, inactivity_limit=60.0
        )
        supervisor = LaunchSupervision(binding, kill_grace=0.3)
        supervisor.prompt_fd = launch._sealed_prompt_memfd(b"prompt")
        supervisor._prompt_digest = sha256(b"prompt")
        supervisor.session_dir = launch._session_directory()
        child = supervisor.spawn()
        starttime = supervisor._leader_starttime
        self.assertIsNotNone(starttime)
        try:
            deadline = time.monotonic() + 5.0
            while time.monotonic() < deadline:
                fields = lock_module._proc_stat_fields(child.pid)
                if fields and fields[0] == "Z":
                    break
                time.sleep(0.01)
            else:
                self.fail("the leader never became a zombie")
            captured = supervisor.snapshot()
            self.assertEqual(captured, frozenset())
            self.assertEqual(supervisor._capture_error, "crash-before-snapshot")
            # The zombie keeps its pinned identity: snapshot() never reaped it,
            # so the process-group id was never freed before termination.
            fields = lock_module._proc_stat_fields(child.pid)
            self.assertIsNotNone(fields)
            self.assertEqual(fields[0], "Z")
            self.assertEqual(int(fields[19]), starttime)
            delivered = supervisor._terminate(child, "inactivity")
            self.assertEqual(
                delivered, ("SIGTERM", "SIGINT", "SIGHUP", "SIGKILL")
            )
            holder = int(self.read_text("holder.pid"))
            self._wait_gone(holder)
            self.assertIsNotNone(child.returncode)
        finally:
            supervisor._cleanup()

    def test_killpg_gated_on_reused_pid_identity(self) -> None:
        """F4: an identity loss before a group signal is never signaled."""
        self.set_behavior("sleep")
        binding, role, agents, spec, plan = self.make_binding(
            runtime_limit=60.0, inactivity_limit=60.0
        )
        supervisor = LaunchSupervision(binding, kill_grace=0.3)
        supervisor.prompt_fd = launch._sealed_prompt_memfd(b"prompt")
        supervisor._prompt_digest = sha256(b"prompt")
        supervisor.session_dir = launch._session_directory()
        child = supervisor.spawn()
        killpg_calls: list = []
        try:
            with unittest.mock.patch.object(
                launch, "_pid_matches_identity", return_value=False
            ), unittest.mock.patch(
                "os.killpg", side_effect=lambda pgid, sig: killpg_calls.append(pgid)
            ):
                with self.assertRaises(SupervisionError):
                    supervisor._terminate(child, "runtime")
        finally:
            if child.poll() is None:
                child.kill()
                child.wait(timeout=10)
            supervisor._cleanup()
        self.assertEqual(
            killpg_calls, [], "no group signal may be delivered when the identity is lost"
        )

    def test_killpg_before_kill_is_gated_on_identity(self) -> None:
        """F4: the SIGKILL escalation is refused when the identity is lost."""
        self.set_behavior("ignore-term")
        binding, role, agents, spec, plan = self.make_binding(
            runtime_limit=60.0, inactivity_limit=60.0
        )
        supervisor = LaunchSupervision(binding, kill_grace=0.05)
        supervisor.prompt_fd = launch._sealed_prompt_memfd(b"prompt")
        supervisor._prompt_digest = sha256(b"prompt")
        supervisor.session_dir = launch._session_directory()
        child = supervisor.spawn()
        try:
            identity = iter([True, True, True, False])
            with unittest.mock.patch.object(
                launch,
                "_pid_matches_identity",
                side_effect=lambda *_: next(identity),
            ):
                with self.assertRaises(SupervisionError):
                    supervisor._terminate(child, "runtime")
        finally:
            if child.poll() is None:
                child.kill()
                child.wait(timeout=10)
            supervisor._cleanup()

    def _wait(self, pid: int, timeout: float = 5.0) -> None:
        """Wait until ``pid`` is gone from ``/proc``."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not os.path.exists(f"/proc/{pid}"):
                return
            time.sleep(0.02)
        self.fail(f"pid {pid} still present in /proc")

    def _wait_gone(self, pid: int, timeout: float = 5.0) -> None:
        """Wait until ``pid`` is gone or a zombie (nothing live remains)."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            fields = lock_module._proc_stat_fields(pid)
            if fields is None or fields[0] == "Z":
                return
            time.sleep(0.02)
        self.fail(f"pid {pid} still live in /proc")

    def test_emergency_terminate_after_leader_exit_kills_holder(self) -> None:
        """F1: emergency cleanup kills the group even when the leader already exited.

        The leader exits immediately while a TERM/INT/HUP-ignoring holder
        keeps the pipes open, then a BaseException escapes the post-spawn
        body. The emergency path must still bounded-terminate the full group
        through the *unreaped zombie's* pinned identity and never skip the
        group kill merely because its leader is gone.
        """
        self.set_behavior("exit-with-holder")
        binding, role, agents, spec, plan = self.make_binding(
            runtime_limit=60.0, inactivity_limit=60.0
        )
        supervisor = LaunchSupervision(binding, kill_grace=0.3)
        seen: list = []

        def exploded(self_, child, deadline, inactivity_limit):
            seen.append(child.pid)
            # Give the exit-with-holder leader time to write both markers
            # (``pid`` and ``holder.pid``) before the emergency raise: the
            # TERM-ignoring holder must already exist in the group so the
            # emergency group kill is genuinely exercised after leader exit.
            end = time.monotonic() + 5.0
            while time.monotonic() < end:
                if (self.marker_dir / "pid").exists() and (
                    self.marker_dir / "holder.pid"
                ).exists():
                    break
                time.sleep(0.01)
            raise RuntimeError("synthetic BaseException after leader exit")

        with unittest.mock.patch.object(
            launch.LaunchSupervision, "_monitor", exploded
        ):
            with self.assertRaises(RuntimeError):
                supervisor.run(self.authorize(binding, role, agents, spec, plan))
        self.assertEqual(len(seen), 1)
        leader = int(self.read_text("pid"))
        holder = int(self.read_text("holder.pid"))
        # The group kill is never skipped because the leader is gone: both the
        # TERM-ignoring holder and the (already-exited) leader are gone.
        self._wait_gone(holder)
        self._wait_gone(leader)

    def test_pending_term_during_spawn_mask_no_orphan(self) -> None:
        """A TERM received during the Popen mask window is pending, never lost.

        TERM/INT/HUP are blocked on the launch thread from before the child is
        spawned until its identity is recorded. A TERM delivered in that window
        becomes pending and is delivered immediately after the mask restores,
        so the monitor takes the bounded terminate-then-reap path and no
        unrecorded child is ever left running.
        """
        self.set_behavior("sleep")
        binding, role, agents, spec, plan = self.make_binding(
            runtime_limit=60.0, inactivity_limit=60.0
        )
        supervisor = LaunchSupervision(binding, kill_grace=0.3)
        parent_pid = os.getpid()
        real_sigmask = signal.pthread_sigmask
        injected: list = []
        original = {sig: signal.getsignal(sig) for sig in TERMINATION_SIGNALS}

        def injecting_sigmask(how, mask):
            if (
                how == signal.SIG_SETMASK
                and os.getpid() == parent_pid
                and not injected
            ):
                # While the mask is still blocked (the spawn window, before the
                # restore completes), deliver SIGTERM: it becomes pending and is
                # delivered immediately after the restore, when the child's
                # identity is already recorded.
                injected.append(1)
                os.kill(os.getpid(), signal.SIGTERM)
            return real_sigmask(how, mask)

        try:
            with unittest.mock.patch.object(
                signal, "pthread_sigmask", injecting_sigmask
            ):
                with self.assertRaises(launch.SupervisionSignalInterrupt) as cm:
                    supervisor.run(self.authorize(binding, role, agents, spec, plan))
        finally:
            for sig in TERMINATION_SIGNALS:
                signal.signal(sig, original[sig])
        self.assertEqual(len(injected), 1)
        self.assertEqual(cm.exception.signum, signal.SIGTERM)
        self.assertEqual(cm.exception.result.outcome, "terminated")
        self.assertIn("SIGTERM", cm.exception.result.terminated_by)
        child_pid = supervisor._child.pid
        self._wait_gone(child_pid)

    def test_off_main_thread_install_and_run_rejected(self) -> None:
        """An off-main-thread launch/install fails loudly and spawns nothing."""
        self.set_behavior("sleep")
        binding, role, agents, spec, plan = self.make_binding()
        supervisor = LaunchSupervision(binding, kill_grace=0.3)
        authority = self.authorize(binding, role, agents, spec, plan)
        install_errors: list = []
        run_errors: list = []

        def off_main() -> None:
            try:
                supervisor.install_signal_handlers()
            except BaseException as exc:  # noqa: BLE001
                install_errors.append(exc)
            try:
                supervisor.run(authority)
            except BaseException as exc:  # noqa: BLE001
                run_errors.append(exc)

        thread = threading.Thread(target=off_main, daemon=True)
        thread.start()
        thread.join(timeout=15)
        self.assertFalse(thread.is_alive(), "the off-main attempt hung")
        self.assertEqual(len(install_errors), 1)
        self.assertIsInstance(install_errors[0], launch.SupervisionError)
        self.assertEqual(len(run_errors), 1)
        self.assertIsInstance(run_errors[0], launch.SupervisionError)
        self.assertIn("off the main thread", str(run_errors[0]))
        self.assertIsNone(
            supervisor._child, "an off-main launch must never spawn a child"
        )
        # The minted token staged executables for the (rejected) attempt; the
        # harness removes that staging dir so no private /tmp material leaks.
        try:
            shutil.rmtree(authority._exec_dir)
        except OSError:
            pass


# --------------------------------------------------------------------------
# Phase 2A task-resource budget enforcement (wall/CPU/output/live, cumulative
# ledger, accounting-untrusted fail-closed, cleanup, non-success)
# --------------------------------------------------------------------------

class TaskBudgetEnforcementTests(_Base):
    """The trusted supervisor enforces the cumulative per-task budget.

    The budget document is the closed ``factory-task-budget/v1`` config the
    campaign loads (the supervisor trusts the campaign-validated config, so
    the fixtures use sub-minimum values purely to keep the tests bounded and
    fast); the ledger is the cumulative ``factory-task-budget-ledger/v1``
    state bound to (campaign, selected task) under ``.factory-state/``.
    """

    CAMPAIGN = "budget-test"

    def _budget(self, **overrides):
        budget = {
            "schema": "factory-task-budget/v1",
            "wall_time_seconds": 3600,
            "cpu_time_seconds": 1800,
            "output_bytes": 64 * 1024 * 1024,
            "max_live_processes": 256,
            "per_command_timeout_seconds": 300,
        }
        budget.update(overrides)
        return budget

    def _ledger(self, campaign: str | None = None):
        return task_budget.BudgetLedger(
            campaign_id=campaign or self.CAMPAIGN, task_id=1
        )

    def _state_dir(self) -> None:
        (self.workspace / ".factory-state").mkdir(mode=0o700, exist_ok=True)

    def _run(self, mode: str, budget, ledger=None, **behavior):
        self.set_behavior(mode, **behavior)
        binding, role, agents, spec, plan = self.make_binding(
            runtime_limit=30.0, inactivity_limit=20.0
        )
        supervisor = LaunchSupervision(
            binding, kill_grace=0.3, budget=budget, ledger=ledger
        )
        result = supervisor.run(
            self.authorize(binding, role, agents, spec, plan, task_budget=budget)
        )
        return supervisor, result

    def test_wall_budget_terminates_and_records_ledger(self) -> None:
        self._state_dir()
        ledger = self._ledger()
        budget = self._budget(wall_time_seconds=1)
        supervisor, result = self._run("sleep", budget, ledger)
        self.assertEqual(result.outcome, "terminated")
        self.assertEqual(result.reason, "budget:wall_time")
        self.assertIn("SIGTERM", result.terminated_by)
        # The cumulative ledger records the attempt and the exhaustion reason.
        saved = task_budget.load_ledger(self.workspace, self.CAMPAIGN, 1)
        self.assertGreaterEqual(saved.wall_time_seconds, 1.0)
        self.assertEqual(saved.exhausted_reason, "wall_time")
        # The group is fully cleaned up.
        self.assertEqual(supervisor.live_scope(), frozenset())

    def test_cpu_budget_terminates_and_records_ledger(self) -> None:
        self._state_dir()
        ledger = self._ledger()
        budget = self._budget(cpu_time_seconds=1)
        with unittest.mock.patch.object(launch, "BUDGET_SAMPLE_INTERVAL", 0.05):
            supervisor, result = self._run("burn-cpu", budget, ledger)
        self.assertEqual(result.outcome, "terminated")
        self.assertEqual(result.reason, "budget:cpu_time")
        saved = task_budget.load_ledger(self.workspace, self.CAMPAIGN, 1)
        self.assertGreaterEqual(saved.cpu_time_seconds, 1.0)
        self.assertEqual(saved.exhausted_reason, "cpu_time")
        self.assertEqual(supervisor.live_scope(), frozenset())

    def test_short_lived_burners_are_counted_in_ledger(self) -> None:
        # Adversarial CPU accounting: short-lived burners exit between the
        # 1s live samples, so a sampling-only accounting would under-count
        # them.  The authoritative RUSAGE_CHILDREN delta (baseline pinned
        # before spawn, read after the broker is reaped) must include every
        # reaped burner in the cumulative ledger.
        self._state_dir()
        ledger = self._ledger()
        budget = self._budget()  # generous: the attempt completes
        supervisor, result = self._run(
            "burn-short-lived", budget, ledger,
            burners=4, rounds=3, burn_seconds=0.2,
        )
        self.assertEqual(result.outcome, "completed")
        saved = task_budget.load_ledger(self.workspace, self.CAMPAIGN, 1)
        # 4 burners x 3 rounds x 0.2s = 2.4s of CPU that never appears in a
        # live sample; the ledger must carry the reaped total.
        self.assertGreaterEqual(saved.cpu_time_seconds, 1.5)
        self.assertEqual(supervisor.live_scope(), frozenset())

    def test_capture_overflow_preserves_last_known_cpu(self) -> None:
        # Finding 2: on descendant-capture overflow the live-process budget
        # is exhausted (the bound is reported, never an incomplete snapshot
        # mistaken for a small tree) while the last-known CPU is preserved
        # — never a trusted zero.
        self._state_dir()
        ledger = self._ledger()
        budget = self._budget(max_live_processes=2)
        real_capture = launch.capture_descendants
        calls = {"n": 0}

        def flaky(root_pid, *, maximum=launch.BUDGET_CAPTURE_MINIMUM):
            calls["n"] += 1
            if calls["n"] > 2:  # snapshot + first sample succeed
                raise launch.RootLockUnsafeError("simulated capture overflow")
            return real_capture(root_pid, maximum=maximum)

        with unittest.mock.patch.object(
            launch, "BUDGET_SAMPLE_INTERVAL", 0.05
        ), unittest.mock.patch.object(
            launch, "capture_descendants", side_effect=flaky
        ):
            supervisor, result = self._run("burn-cpu", budget, ledger, seconds=3)
        self.assertEqual(result.outcome, "terminated")
        self.assertEqual(result.reason, "budget:live_processes")
        # The last-known CPU from the first successful sample is preserved
        # (never a trusted zero) and the bound exhausted the live budget.
        self.assertGreater(supervisor._budget_usage.cpu_time_seconds, 0.0)
        saved = task_budget.load_ledger(self.workspace, self.CAMPAIGN, 1)
        self.assertGreaterEqual(saved.max_live_processes, 2)
        self.assertEqual(saved.exhausted_reason, "live_processes")
        self.assertEqual(supervisor.live_scope(), frozenset())

    def test_accounting_failure_fails_closed_untrusted(self) -> None:
        # Finding 1/2: when exact cumulative CPU accounting cannot be
        # established, the attempt fails closed as accounting_untrusted and
        # the last-known bounded usage is preserved — never a trusted zero.
        self._state_dir()
        ledger = self._ledger()
        budget = self._budget()
        with unittest.mock.patch.object(
            launch, "BUDGET_SAMPLE_INTERVAL", 0.05
        ), unittest.mock.patch.object(
            launch.LaunchSupervision,
            "_authoritative_cpu_seconds",
            return_value=None,
        ):
            supervisor, result = self._run("burn-cpu", budget, ledger, seconds=2)
        self.assertEqual(result.outcome, "completed")
        saved = task_budget.load_ledger(self.workspace, self.CAMPAIGN, 1)
        self.assertEqual(saved.exhausted_reason, "accounting_untrusted")
        # The last-known live sample (real burned CPU) is preserved.
        self.assertGreaterEqual(saved.cpu_time_seconds, 1.0)
        self.assertEqual(supervisor.live_scope(), frozenset())

    def test_rusage_delta_isolates_sequential_attempts(self) -> None:
        # The per-attempt RUSAGE_CHILDREN baseline is pinned before each
        # spawn, so a later attempt's delta never re-counts an earlier
        # attempt's CPU (the campaign launches sequential roles through the
        # same supervisor process).
        self._state_dir()
        budget = self._budget()
        with unittest.mock.patch.object(launch, "BUDGET_SAMPLE_INTERVAL", 0.05):
            _, result1 = self._run("burn-cpu", budget, self._ledger(), seconds=1)
            self.assertEqual(result1.outcome, "completed")
            saved1 = task_budget.load_ledger(self.workspace, self.CAMPAIGN, 1)
            # Attempt 2 reloads the cumulative ledger and burns almost no
            # CPU; its own delta must be small.
            _, result2 = self._run(
                "record", budget,
                task_budget.load_ledger(self.workspace, self.CAMPAIGN, 1),
            )
            self.assertEqual(result2.outcome, "completed")
            saved2 = task_budget.load_ledger(self.workspace, self.CAMPAIGN, 1)
        # If the baseline were not per-attempt, attempt 2's delta would have
        # re-counted attempt 1's ~1s of CPU.
        self.assertLess(
            saved2.cpu_time_seconds - saved1.cpu_time_seconds, 0.5
        )

    def test_output_budget_terminates_on_flood(self) -> None:
        self._state_dir()
        ledger = self._ledger()
        budget = self._budget(output_bytes=1)
        supervisor, result = self._run("spew", budget, ledger, bytes=300 * 1024)
        self.assertEqual(result.outcome, "terminated")
        self.assertEqual(result.reason, "budget:output_bytes")
        saved = task_budget.load_ledger(self.workspace, self.CAMPAIGN, 1)
        self.assertGreaterEqual(saved.output_bytes, 1)
        self.assertEqual(saved.exhausted_reason, "output_bytes")
        self.assertEqual(supervisor.live_scope(), frozenset())

    def test_live_process_budget_terminates_and_cleans_descendants(self) -> None:
        self._state_dir()
        ledger = self._ledger()
        budget = self._budget(max_live_processes=2)
        with unittest.mock.patch.object(launch, "BUDGET_SAMPLE_INTERVAL", 0.05):
            supervisor, result = self._run("fork-many", budget, ledger, count=4)
        self.assertEqual(result.outcome, "terminated")
        self.assertEqual(result.reason, "budget:live_processes")
        # The measured descendant closure exceeded the budget and every
        # measured member is gone after the bounded termination (no escaped
        # descendant survives).
        self.assertGreaterEqual(len(supervisor._budget_captured), 2)
        self.assertEqual(
            live_scope(supervisor._budget_captured), frozenset()
        )
        self.assertEqual(supervisor.live_scope(), frozenset())
        saved = task_budget.load_ledger(self.workspace, self.CAMPAIGN, 1)
        self.assertGreaterEqual(saved.max_live_processes, 2)
        self.assertEqual(saved.exhausted_reason, "live_processes")

    def test_exhaustion_is_never_success(self) -> None:
        # A budget-terminated attempt is a non-success outcome: never
        # ``completed`` and never a zero returncode, so exhaustion can never
        # be mistaken for acceptance.
        self._state_dir()
        for index, (mode, budget) in enumerate((
            ("sleep", self._budget(wall_time_seconds=1)),
            ("burn-cpu", self._budget(cpu_time_seconds=1)),
            ("spew", self._budget(output_bytes=1)),
            ("fork-many", self._budget(max_live_processes=2)),
        )):
            with self.subTest(mode=mode):
                campaign = f"budget-test-{index}"
                with unittest.mock.patch.object(
                    launch, "BUDGET_SAMPLE_INTERVAL", 0.05
                ):
                    _, result = self._run(
                        mode, budget, self._ledger(campaign)
                    )
                self.assertEqual(result.outcome, "terminated")
                self.assertNotEqual(result.returncode, 0)
                self.assertTrue(result.reason.startswith("budget:"))

    def test_cumulative_ledger_stops_second_attempt_before_spawn(self) -> None:
        # Attempt 1 consumes the output budget; the reloaded cumulative
        # ledger then refuses attempt 2 in the preflight, before any spawn.
        self._state_dir()
        budget = self._budget(output_bytes=1)
        _, result = self._run("spew", budget, self._ledger(), bytes=300 * 1024)
        self.assertEqual(result.reason, "budget:output_bytes")
        saved = task_budget.load_ledger(self.workspace, self.CAMPAIGN, 1)
        self.assertEqual(saved.exhausted_reason, "output_bytes")
        # Attempt 2: the same budget with the reloaded ledger is refused.
        self.set_behavior("sleep")
        binding, role, agents, spec, plan = self.make_binding(
            runtime_limit=30.0, inactivity_limit=20.0
        )
        supervisor = LaunchSupervision(
            binding, kill_grace=0.3, budget=budget, ledger=saved
        )
        with self.assertRaises(launch.BudgetExhaustedError) as caught:
            supervisor.run(
                self.authorize(binding, role, agents, spec, plan, task_budget=budget)
            )
        self.assertIn("budget exhausted", str(caught.exception))
        self.assertIsNone(supervisor._child, "no spawn for an exhausted task")

    def test_accounting_untrusted_fails_closed(self) -> None:
        # Unreadable /proc accounting fails closed as accounting_untrusted
        # instead of silently under-counting.
        self._state_dir()
        ledger = self._ledger()
        budget = self._budget()
        self.set_behavior("sleep")
        binding, role, agents, spec, plan = self.make_binding(
            runtime_limit=30.0, inactivity_limit=20.0
        )
        supervisor = LaunchSupervision(
            binding, kill_grace=0.3, budget=budget, ledger=ledger
        )

        def broken_measure(self_):
            raise launch.BudgetExhaustedError("accounting untrusted")

        with unittest.mock.patch.object(
            launch.LaunchSupervision, "_measure_tree", broken_measure
        ), unittest.mock.patch.object(launch, "BUDGET_SAMPLE_INTERVAL", 0.05):
            result = supervisor.run(
                self.authorize(binding, role, agents, spec, plan, task_budget=budget)
            )
        self.assertEqual(result.outcome, "terminated")
        self.assertEqual(result.reason, "budget:accounting_untrusted")
        saved = task_budget.load_ledger(self.workspace, self.CAMPAIGN, 1)
        self.assertEqual(saved.exhausted_reason, "accounting_untrusted")
        self.assertEqual(supervisor.live_scope(), frozenset())

    def test_no_budget_means_no_ledger(self) -> None:
        # Without a budget/ledger the supervisor enforces nothing and writes
        # no ledger artifact.
        self._state_dir()
        self.set_behavior("record")
        binding, role, agents, spec, plan = self.make_binding()
        supervisor = LaunchSupervision(binding, kill_grace=0.3)
        result = supervisor.run(self.authorize(binding, role, agents, spec, plan))
        self.assertEqual(result.outcome, "completed")
        self.assertEqual(result.returncode, 0)
        self.assertFalse(
            (self.workspace / ".factory-state" / "task-budget-budget-test-task-1.json").exists()
        )

    def test_ledger_records_usage_monotonically_across_attempts(self) -> None:
        # Two bounded attempts against the same ledger accumulate usage; the
        # live-process dimension records the peak, never a sum.
        self._state_dir()
        budget = self._budget(output_bytes=64 * 1024 * 1024)
        ledger = self._ledger()
        _, first = self._run("record", budget, ledger)
        self.assertEqual(first.outcome, "completed")
        saved = task_budget.load_ledger(self.workspace, self.CAMPAIGN, 1)
        first_wall = saved.wall_time_seconds
        self.assertGreater(first_wall, 0.0)
        # Second attempt with the reloaded ledger accumulates.
        _, second = self._run("record", budget, saved)
        self.assertEqual(second.outcome, "completed")
        saved2 = task_budget.load_ledger(self.workspace, self.CAMPAIGN, 1)
        self.assertGreaterEqual(
            saved2.wall_time_seconds, first_wall
        )
        self.assertIsNone(saved2.exhausted_reason)


# --------------------------------------------------------------------------
# Descendant scope / subreaper / reparent / PID reuse (F6/F7)
# --------------------------------------------------------------------------

class DescendantScopeTests(_Base):
    def test_orphan_that_dies_is_always_reaped(self) -> None:
        self.set_behavior("double-fork-exit")
        binding, role, agents, spec, plan = self.make_binding()
        supervisor = LaunchSupervision(binding, kill_grace=0.3)
        result = supervisor.run(self.authorize(binding, role, agents, spec, plan))
        self.assertEqual(result.outcome, "completed")
        orphan = int(self.read_text("orphan.pid"))
        self._wait_gone_time(orphan)
        self.assertFalse(
            os.path.exists(f"/proc/{orphan}"),
            "the orphaned double-fork descendant must be reaped, not a zombie",
        )

    def test_surviving_escaped_descendant_is_killed_reaped_then_fails(self) -> None:
        self.set_behavior("double-fork-live")
        binding, role, agents, spec, plan = self.make_binding()
        supervisor = LaunchSupervision(binding, kill_grace=0.3)
        marker = self.marker_dir / "orphan.pid"
        pid: int | None = None
        try:
            with self.assertRaises(EscapedDescendantError) as caught:
                supervisor.run(self.authorize(binding, role, agents, spec, plan))
            self.assertIn("reaped", str(caught.exception))
            self.assertTrue(marker.is_file(), "the escaped helper never started")
            pid = int(marker.read_text(encoding="utf-8"))
            self._wait_gone_time(pid)
            self.assertFalse(
                os.path.exists(f"/proc/{pid}"),
                "finalization returned before the owned escape was reaped",
            )
            with self.assertRaises(ChildProcessError):
                os.waitpid(pid, os.WNOHANG)
        finally:
            # Failure-path hygiene only: a regressed implementation must not
            # leave the test's deliberately escaped process behind.
            if pid is None and marker.is_file():
                pid = int(marker.read_text(encoding="utf-8"))
            if pid is not None:
                try:
                    os.kill(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                try:
                    os.waitpid(pid, 0)
                except ChildProcessError:
                    pass

    def test_preexisting_child_fork_exit_does_not_widen_broker_ownership(self) -> None:
        """An unrelated mid-attempt fork keeps its worker and exit status.

        The pre-existing coordinator child forks only after the confined
        command is live, then exits 37.  Its worker is not in the fresh
        executable broker's ancestry, so launch must neither signal it nor
        consume the parent's wait status.
        """
        trigger = self.marker_dir / "pid"
        worker_pidfile = self.marker_dir / "unrelated-worker.pid"
        worker_status = self.marker_dir / "unrelated-worker.status"
        helper = self.tmp / "unrelated-forker.py"
        helper.write_text(
            "import os, pathlib, sys, time\n"
            "trigger, pidfile, status = map(pathlib.Path, sys.argv[1:])\n"
            "deadline = time.monotonic() + 20\n"
            "while not trigger.exists() and time.monotonic() < deadline:\n"
            "    time.sleep(0.01)\n"
            "if not trigger.exists(): os._exit(91)\n"
            "worker = os.fork()\n"
            "if worker:\n"
            "    pidfile.write_text(str(worker), encoding='ascii')\n"
            "    os._exit(37)\n"
            "os.setsid()\n"
            "time.sleep(0.15)\n"
            "status.write_text('untouched', encoding='ascii')\n"
            "time.sleep(120)\n",
            encoding="utf-8",
        )
        preexisting = subprocess.Popen([
            sys.executable, str(helper), str(trigger), str(worker_pidfile),
            str(worker_status),
        ])
        worker_pid: int | None = None
        try:
            self.set_behavior("trap-term")
            binding, role, agents, spec, plan = self.make_binding(
                runtime_limit=30.0, inactivity_limit=0.8
            )
            supervisor = LaunchSupervision(binding, kill_grace=0.3)
            result = supervisor.run(
                self.authorize(binding, role, agents, spec, plan)
            )
            self.assertEqual(result.outcome, "terminated")
            self.assertEqual(preexisting.wait(timeout=5), 37)
            deadline = time.monotonic() + 5.0
            while time.monotonic() < deadline and not worker_status.is_file():
                time.sleep(0.02)
            self.assertTrue(worker_pidfile.is_file(), "unrelated worker never forked")
            worker_pid = int(worker_pidfile.read_text(encoding="ascii"))
            self.assertEqual(worker_status.read_text(encoding="ascii"), "untouched")
            os.kill(worker_pid, 0)
            self.assertFalse(hasattr(supervisor, "_pre_existing_children"))
            self.assertFalse(hasattr(supervisor, "_owned_descendant_identities"))
        finally:
            if worker_pid is None and worker_pidfile.is_file():
                worker_pid = int(worker_pidfile.read_text(encoding="ascii"))
            if worker_pid is not None:
                try:
                    os.kill(worker_pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            if preexisting.poll() is None:
                preexisting.kill()
                preexisting.wait(timeout=5)

    def test_live_escape_fails_closed_without_pidfd_signaling(self) -> None:
        """Numeric ``os.kill`` is never a fallback for an owned escape."""
        with unittest.mock.patch.object(
            launch, "_is_live_with_identity", return_value=True
        ), unittest.mock.patch.object(os, "pidfd_open", None):
            with self.assertRaisesRegex(
                launch.SupervisionError, "pidfd signaling is unavailable"
            ):
                LaunchSupervision._kill_pinned_identity(424242, 101)

    def test_pidfd_identity_is_revalidated_after_open(self) -> None:
        """A PID recycled before pidfd_open is never signaled through its pidfd."""
        descriptor = os.open("/dev/null", os.O_RDONLY)
        send = unittest.mock.Mock()
        with unittest.mock.patch.object(
            launch, "_is_live_with_identity", side_effect=[True, False]
        ), unittest.mock.patch.object(
            os, "pidfd_open", return_value=descriptor
        ), unittest.mock.patch.object(
            signal, "pidfd_send_signal", send, create=True
        ):
            self.assertFalse(
                LaunchSupervision._kill_pinned_identity(424243, 102)
            )
        send.assert_not_called()

    def test_live_scope_excludes_pid_reuse(self) -> None:
        """A reused PID with a different start time is never a live descendant."""
        marker = self.workspace / "sleeper.pid"
        sleeper = self.workspace / "sleeper.py"
        sleeper.write_text(
            "import os, time\n"
            f"open({str(marker)!r}, 'w').write(str(os.getpid()))\n"
            "time.sleep(120)\n",
            encoding="utf-8",
        )
        child = subprocess.Popen(
            [sys.executable, str(sleeper)], cwd=self.workspace
        )
        try:
            deadline = time.monotonic() + 5
            while not marker.exists() and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertTrue(marker.exists())
            pid = int(marker.read_text(encoding="utf-8"))
            captured = capture_descendants(os.getpid())
            record = next(
                (c for c in captured if c.pid == pid), None
            )
            self.assertIsNotNone(record, f"no captured record for pid {pid}")
            self.assertIn(pid, live_scope(captured))
            reused = frozenset(
                [
                    CapturedProcess(
                        pid=pid,
                        starttime=record.starttime + 1,
                        parent=record.parent,
                    )
                ]
            )
            self.assertNotIn(pid, live_scope(reused))
        finally:
            if child.poll() is None:
                child.kill()
                child.wait(timeout=10)

    def test_capture_scope_bounds_fail_closed(self) -> None:
        child = os.fork()
        if child == 0:
            time.sleep(120)
            os._exit(0)
        try:
            with self.assertRaises(RootLockUnsafeError):
                capture_descendants(os.getpid(), maximum=1)
        finally:
            try:
                os.kill(child, signal.SIGKILL)
            except ProcessLookupError:
                pass
            try:
                os.waitpid(child, 0)
            except ChildProcessError:
                pass

    def test_preexisting_child_exit_status_is_not_reaped(self) -> None:
        """F6/F7: the outer coordinator waits only for its broker child."""
        sleeper = self.workspace / "preexisting.py"
        marker = self.workspace / "preexisting.pid"
        sleeper.write_text(
            "import os, time\n"
            f"open({str(marker)!r}, 'w').write(str(os.getpid()))\n"
            "time.sleep(120)\n",
            encoding="utf-8",
        )
        pre = subprocess.Popen(
            [sys.executable, str(sleeper)], cwd=self.workspace
        )
        self.set_behavior("sleep")
        binding, role, agents, spec, plan = self.make_binding(
            runtime_limit=60.0, inactivity_limit=2.0
        )
        supervisor = LaunchSupervision(binding, kill_grace=0.3)
        try:
            deadline = time.monotonic() + 5
            while not marker.exists() and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertTrue(marker.exists())

            def stop_preexisting() -> None:
                time.sleep(0.8)
                try:
                    pre.terminate()
                except ProcessLookupError:
                    pass

            timer = threading.Thread(target=stop_preexisting, daemon=True)
            timer.start()
            try:
                result = supervisor.run(self.authorize(binding, role, agents, spec, plan))
            finally:
                timer.join()
            self.assertEqual(result.outcome, "terminated")
            # The pre-existing child exited mid-attempt: it is a zombie of
            # *this* process because the supervisor never claimed its status.
            deadline = time.monotonic() + 5.0
            while time.monotonic() < deadline:
                fields = lock_module._proc_stat_fields(pre.pid)
                if fields and fields[0] == "Z":
                    break
                time.sleep(0.02)
            else:
                self.fail(
                    "the pre-existing child was never left as a zombie of "
                    "its owner (its exit status was reaped by the supervisor?)"
                )
            got, status = os.waitpid(pre.pid, os.WNOHANG)
            self.assertEqual(got, pre.pid)
            self.assertNotEqual(status & 0xFF, 0)
        finally:
            if pre.poll() is None:
                pre.kill()
                pre.wait(timeout=10)

    def _wait_gone_time(self, pid: int) -> None:
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            if not os.path.exists(f"/proc/{pid}"):
                return
            time.sleep(0.02)
        self.fail(f"pid {pid} still visible in /proc")


# --------------------------------------------------------------------------
# Trusted operator CLI (machine-readable exit status)
# --------------------------------------------------------------------------

class CliTests(_Base):
    def make_repo(self) -> tuple[Path, str, Path]:
        repo = self.tmp / "repo"
        repo.mkdir()
        (repo / "src" / ".factory-test-output").mkdir(parents=True)
        # The migrated launch authority reads every staged executable from the
        # canonical ``.factory/tools/`` layout: the secure wrapper, the
        # credential guard, the model-side Pi guard extension, and the Git
        # shim are committed there so the bound-commit blob verification
        # (F2/F5) resolves the exact production paths.
        tools = repo / ".factory" / "tools"
        tools.mkdir(parents=True)
        shutil.copy2(REAL_WRAPPER, tools / WRAPPER_BASENAME)
        # Task 11: commit the exact credential guard into every fixture repo
        # (the launch redacts every child output channel through the exact
        # committed guard before any result is produced).
        shutil.copy2(
            ROOT / ".factory" / "tools" / "credential-guard.py",
            tools / "credential-guard.py",
        )
        # Task 11: commit the exact model-side Pi guard extension into every
        # fixture repo (the launch always loads it through ``--extension``).
        shutil.copy2(
            ROOT / ".factory" / "tools" / "pi-factory-guard-extension.mjs",
            tools / "pi-factory-guard-extension.mjs",
        )
        (tools / "pi-cli-shims").mkdir()
        shutil.copy2(
            ROOT / ".factory" / "tools" / "pi-cli-shims" / "git",
            tools / "pi-cli-shims" / "git",
        )
        # Task 8 confined launch: the fixture repo commits the exact
        # confine-launcher blob (F2/F5) so the production CLI can stage it
        # from the bound commit, plus the committed confinement schema doc
        # the specification is documented against.
        loop_dir = repo / ".factory" / "loop"
        loop_dir.mkdir(parents=True)
        for module in ("confine_launcher.py", "usage.py", "usage_fetch.py"):
            shutil.copy2(
                ROOT / ".factory" / "loop" / module,
                loop_dir / module,
            )
        schemas_dir = repo / ".factory" / "schemas"
        schemas_dir.mkdir(parents=True)
        shutil.copy2(
            ROOT / ".factory" / "schemas" / "factory-confinement-v1.schema.json",
            schemas_dir / "factory-confinement-v1.schema.json",
        )
        run([GIT, "-C", str(repo), "init", "-q"])
        run([GIT, "-C", str(repo), "config", "user.email", "factory@test"])
        run([GIT, "-C", str(repo), "config", "user.name", "factory"])
        backend = repo / "backend.py"
        backend.write_text(BACKEND_SOURCE, encoding="utf-8")
        os.chmod(backend, 0o700)
        plan_path = repo / "plan.md"
        shutil.copy2(FIXTURE_PLAN, plan_path)
        (repo / "spec.md").write_text("spec\n", encoding="utf-8")
        (repo / "policy.md").write_text("policy\n", encoding="utf-8")
        (repo / "role.md").write_text("role\n", encoding="utf-8")
        run([GIT, "-C", str(repo), "add", "."])
        run([GIT, "-C", str(repo), "commit", "-qm", "init"])
        head = run(
            [GIT, "-C", str(repo), "rev-parse", "HEAD"]
        ).stdout.strip()
        return repo, head, plan_path

    def test_excerpt_cli_is_machine_readable(self) -> None:
        plan_path = FIXTURE_PLAN
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            status = launch.main(["excerpt", "--plan", str(plan_path), "--task-id", "1"])
        self.assertEqual(status, 0)
        payload = json.loads(out.getvalue())
        self.assertEqual(payload["task_id"], 1)
        self.assertEqual(
            payload["digest"], task_excerpt_digest(plan_path.read_bytes(), 1)
        )

    def test_launch_cli_completes_with_json(self) -> None:
        repo, head, plan_path = self.make_repo()
        _, excerpt_digest = derive_task_excerpt(plan_path.read_bytes(), 1)
        (repo / "behavior.json").write_text(
            json.dumps({"mode": "record"}), encoding="utf-8"
        )
        argv = [
            "launch",
            "--root", str(repo),
            "--role", "developer",
            "--model", "synthetic-model",
            "--provider", "synthetic",
            "--backend", str(repo / "backend.py"),
            "--role-prompt", str(repo / "role.md"),
            "--role-prompt-digest", sha256(b"role\n"),
            "--prompt-set-digest", sha256(b"set"),
            "--policy", str(repo / "policy.md"),
            "--policy-digest", sha256(b"policy\n"),
            "--spec", str(repo / "spec.md"),
            "--spec-digest", sha256(b"spec\n"),
            "--plan", str(plan_path),
            "--plan-digest", sha256(plan_path.read_bytes()),
            "--bound-commit", head,
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
        self.assertEqual(status, 0, err.getvalue() + "\nSTDOUT:" + out.getvalue())
        payload = json.loads(out.getvalue())
        self.assertEqual(payload["outcome"], "completed")
        self.assertEqual(payload["returncode"], 0)
        self.assertEqual(payload["role"], "developer")
        launch.validate_launch_result(payload)

    def _argv(
        self, repo: Path, head: str, plan: Path, **override: object
    ) -> list:
        """The canonical launch CLI argv; ``override`` swaps option values."""
        argv = [
            "launch",
            "--root", str(repo),
            "--role", "developer",
            "--model", "synthetic-model",
            "--provider", "synthetic",
            "--backend", str(repo / "backend.py"),
            "--role-prompt", str(repo / "role.md"),
            "--role-prompt-digest", sha256(b"role\n"),
            "--prompt-set-digest", sha256(b"set"),
            "--policy", str(repo / "policy.md"),
            "--policy-digest", sha256(b"policy\n"),
            "--spec", str(repo / "spec.md"),
            "--spec-digest", sha256(b"spec\n"),
            "--plan", str(plan),
            "--plan-digest", sha256(plan.read_bytes()),
            "--bound-commit", head,
            "--allowed-tools", "read,bash",
            "--runtime-limit", "30",
            "--inactivity-limit", "20",
            "--task-id", "1",
            "--task-excerpt-digest", derive_task_excerpt(
                plan.read_bytes(), 1
            )[1],
        ]
        built: list = []
        index = 0
        while index < len(argv):
            option = argv[index]
            if option in override:
                value = override[option]
                if value is not None:
                    built.append(option)
                    built.append(value)
                index += 2
            else:
                built.append(option)
                index += 1
        return built

    def test_tampered_workspace_backend_rejected_before_exec(self) -> None:
        """F2: a workspace backend that diverges from the bound commit is refused."""
        repo, head, plan = self.make_repo()
        (repo / "backend.py").write_text("tampered backend\n", encoding="utf-8")
        argv = self._argv(repo, head, plan)
        out = io.StringIO()
        err = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            status = launch.main(argv)
        self.assertEqual(status, launch.EXIT_INVOCATION)
        self.assertEqual(out.getvalue(), "")
        self.assertIn("backend", err.getvalue())
        self.assertIn("committed blob", err.getvalue())

    def test_tampered_workspace_wrapper_rejected_before_exec(self) -> None:
        """F2: the secure wrapper must run from its exact bound-commit bytes."""
        repo, head, plan = self.make_repo()
        wrapper = repo / ".factory" / "tools" / WRAPPER_BASENAME
        original = wrapper.read_bytes()
        wrapper.write_bytes(original + b"\n# tampered\n")
        argv = self._argv(repo, head, plan)
        out = io.StringIO()
        err = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            status = launch.main(argv)
        self.assertEqual(status, launch.EXIT_INVOCATION)
        self.assertIn("wrapper", err.getvalue())

    def test_cli_rejects_symlinked_role_prompt(self) -> None:
        """F5: a symlinked authoritative blob is refused (no-follow anchored reads)."""
        repo, head, plan = self.make_repo()
        link = repo / "role-link.md"
        os.symlink(repo / "role.md", link)
        argv = self._argv(repo, head, plan, **{"--role-prompt": str(link)})
        out = io.StringIO()
        err = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            status = launch.main(argv)
        self.assertEqual(status, launch.EXIT_INVOCATION)
        self.assertIn("no-follow", err.getvalue())

    def test_cli_rejects_operator_claimed_digest_substitution(self) -> None:
        """F5: an operator-claimed digest is never authoritative."""
        repo, head, plan = self.make_repo()
        argv = self._argv(repo, head, plan, **{"--plan-digest": "f" * 64})
        out = io.StringIO()
        err = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            status = launch.main(argv)
        self.assertEqual(status, launch.EXIT_INVOCATION)
        self.assertIn("claimed", err.getvalue())

    def test_module_entrypoint_is_machine_reachable(self) -> None:
        """The CLI is reachable via ``python -m factory.loop.launch``.

        The hidden namespace is imported under the public package name by
        the external-prefix alias mechanism (``factory`` -> the canonical
        ``.factory/`` directory on ``PYTHONPATH``); no visible bare script
        exposes the launcher.
        """
        alias_dir = Path(tempfile.mkdtemp(prefix="factory-entrypoint."))
        try:
            os.symlink(ROOT / ".factory", alias_dir / "factory")
            env = dict(os.environ)
            env["PYTHONPATH"] = str(alias_dir)
            proc = subprocess.run(
                [sys.executable, "-m", "factory.loop.launch", "--help"],
                capture_output=True,
                text=True,
                env=env,
                cwd=str(ROOT),
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn("factory-launch", proc.stdout)
        finally:
            shutil.rmtree(alias_dir, ignore_errors=True)
        # No visible bare ``.factory/tools/`` wrapper may expose the launcher.
        self.assertEqual(
            list((ROOT / "scripts").glob("*launch*")),
            [],
            "a visible .factory/tools/ wrapper must never expose the launcher",
        )

    def test_launch_cli_rejects_mismatched_excerpt_digest(self) -> None:
        repo, head, plan = self.make_repo()
        argv = [
            "launch",
            "--root", str(repo),
            "--role", "developer",
            "--model", "synthetic-model",
            "--provider", "synthetic",
            "--backend", str(repo / "backend.py"),
            "--role-prompt", str(repo / "role.md"),
            "--role-prompt-digest", sha256(b"role\n"),
            "--prompt-set-digest", sha256(b"set"),
            "--policy", str(repo / "policy.md"),
            "--policy-digest", sha256(b"policy\n"),
            "--spec", str(repo / "spec.md"),
            "--spec-digest", sha256(b"spec\n"),
            "--plan", str(plan),
            "--plan-digest", sha256(plan.read_bytes()),
            "--bound-commit", head,
            "--allowed-tools", "read,write",
            "--runtime-limit", "30",
            "--inactivity-limit", "20",
            "--task-id", "1",
            "--task-excerpt-digest", "0" * 64,
        ]
        out = io.StringIO()
        err = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            status = launch.main(argv)
        self.assertEqual(status, launch.EXIT_INVOCATION)
        self.assertEqual(out.getvalue(), "")


# --------------------------------------------------------------------------
# Committed machine-result schema (factory-launch-result/v1)
# --------------------------------------------------------------------------

class ResultSchemaTests(_Base):
    def _result(self) -> LaunchResult:
        return LaunchResult(
            role="planner",
            model="m",
            provider="p",
            outcome="completed",
            returncode=0,
            signal=None,
            reason=None,
            terminated_by=(),
            elapsed=0.1,
            stdout=StreamResult(
                bytes=3, digest="0" * 64, tail="abc", truncated=False
            ),
            stderr=StreamResult(
                bytes=0, digest="0" * 64, tail="", truncated=False
            ),
            descendants_snapshot=1,
            live_descendants=0,
            invariants=("session", "environ", "descriptors"),
        )

    def test_result_schema_accepts_complete_result(self) -> None:
        launch.validate_launch_result(self._result())

    def test_result_schema_rejects_extra_field(self) -> None:
        payload = self._result().to_dict()
        payload["extra"] = "x"
        with self.assertRaises(launch.ResultSchemaError):
            launch.validate_launch_result(payload)

    def test_result_schema_rejects_missing_field(self) -> None:
        payload = self._result().to_dict()
        del payload["outcome"]
        with self.assertRaises(launch.ResultSchemaError):
            launch.validate_launch_result(payload)

    def test_result_schema_rejects_bad_enum(self) -> None:
        payload = self._result().to_dict()
        payload["outcome"] = "bogus"
        with self.assertRaises(launch.ResultSchemaError):
            launch.validate_launch_result(payload)

    def test_result_schema_rejects_bad_digest(self) -> None:
        payload = self._result().to_dict()
        payload["stdout"]["digest"] = "zzz"
        with self.assertRaises(launch.ResultSchemaError):
            launch.validate_launch_result(payload)


# ---------------------------------------------------------------------------
# Fd-anchored no-follow bounded blob reads (F5)
# ---------------------------------------------------------------------------

class BlobAnchorTests(_Base):
    def test_blob_anchor_rejects_symlink(self) -> None:
        target = self.tmp / "real.txt"
        target.write_text("data", encoding="utf-8")
        link = self.tmp / "link.txt"
        os.symlink(target, link)
        with self.assertRaises(launch.InvocationError):
            launch._read_blob_anchored(str(link), "blob", 1024)

    def test_blob_anchor_rejects_non_regular_file(self) -> None:
        # A character device can never be an authoritative committed blob.
        with self.assertRaises(launch.InvocationError):
            launch._read_blob_anchored("/dev/null", "blob", 1024)

    def test_blob_anchor_rejects_oversize(self) -> None:
        path = self.tmp / "big.txt"
        path.write_bytes(b"x" * 2048)
        with self.assertRaises(launch.InvocationError):
            launch._read_blob_anchored(str(path), "blob", 1024)


# ---------------------------------------------------------------------------
# Unforgeable verified-committed authority token (F2/F5)
# ---------------------------------------------------------------------------

class AuthorityTokenTests(_Base):
    def test_direct_run_without_authority_rejected(self) -> None:
        """A programmatic launch without a minted token is refused (F2/F5)."""
        self.set_behavior("sleep")
        binding, role, agents, spec, plan = self.make_binding()
        supervisor = LaunchSupervision(binding, kill_grace=0.3)
        with self.assertRaises(launch.SupervisionError):
            supervisor.run(None)
        with self.assertRaises(launch.SupervisionError):
            supervisor.run("an-operator-claimed-token")
        self.assertIsNone(
            supervisor._child, "no child may be spawned without a verified token"
        )

    def test_forged_authority_rejected(self) -> None:
        """A token whose mint marker is absent/forged is refused loudly."""
        self.set_behavior("sleep")
        binding, role, agents, spec, plan = self.make_binding()
        supervisor = LaunchSupervision(binding, kill_grace=0.3)
        # ``object.__new__`` bypasses the guard constructor, so this mimics a
        # token forged from operator claims that never passed ``_MINT_SECRET``.
        forged = object.__new__(launch.LaunchAuthority)
        forged._mint = object()
        forged._binding = None
        with self.assertRaises(launch.SupervisionError) as cm:
            supervisor.run(forged)
        self.assertIn("forged", str(cm.exception))
        self.assertIsNone(supervisor._child)

    def test_authorize_has_no_caller_proof_transport(self) -> None:
        """Installed callers cannot supply any confinement proof object."""
        binding, role, agents, spec, plan = self.make_binding()
        with self.assertRaises(TypeError):
            launch.authorize_launch(
                binding,
                role_prompt=role,
                agents=agents,
                spec=spec,
                plan=plan,
                _confinement_proof=object(),
            )

    def test_token_not_serializable_or_smuggled(self) -> None:
        """The token cannot be serialized and carries no smuggled attribute dict."""
        binding, role, agents, spec, plan = self.make_binding()
        authority = self.authorize(binding, role, agents, spec, plan)
        with self.assertRaises(TypeError):
            json.dumps(authority)
        # The token is slots-only: it exposes no ``__dict__`` an attacker could
        # fill with operator claims.
        self.assertFalse(hasattr(authority, "__dict__"))
        self.assertFalse(hasattr(authority, "binding"))

    def test_staging_modes_and_cleanup(self) -> None:
        """Staged scripts are non-executable mode-0400 data, then removed."""
        binding, role, agents, spec, plan = self.make_binding()
        authority = self.authorize(binding, role, agents, spec, plan)
        exec_dir = authority._exec_dir
        self.assertTrue(exec_dir.is_dir())
        self.assertEqual(exec_dir.stat().st_mode & 0o777, 0o700)
        self.assertGreaterEqual(len(authority._staged_digests), 2, "wrapper+backend")
        for path_text, digest in authority._staged_digests.items():
            path = Path(path_text)
            self.assertTrue(path.is_file())
            self.assertEqual(path.stat().st_mode & 0o777, 0o400)
            self.assertEqual(path.stat().st_nlink, 1)
            self.assertEqual(launch._file_sha256(path), digest)
        supervisor = LaunchSupervision(binding, kill_grace=0.3)
        result = supervisor.run(authority)
        self.assertEqual(result.outcome, "completed")
        self.assertFalse(
            exec_dir.exists(), "the private staging directory must be cleaned up"
        )

    def test_workspace_copied_elf_is_never_staged_for_execution(self) -> None:
        """A committed copied ELF must use the immutable external boundary."""
        copied_elf = self.workspace / "copied-backend"
        shutil.copy2(os.path.realpath(sys.executable), copied_elf)
        copied_elf.chmod(0o700)
        self._git("add", "copied-backend")
        self._git("commit", "-qm", "copied ELF negative")
        new_head = self._git("rev-parse", "HEAD").stdout.strip()
        binding, role, agents, spec, plan = self.make_binding()
        binding = dataclasses.replace(
            binding, backend=copied_elf, bound_commit=new_head
        )
        with self.assertRaisesRegex(InvocationError, "copied workspace ELF"):
            self.authorize(binding, role, agents, spec, plan)

    def test_mutate_workspace_after_authorize_staged_bytes_execute(self) -> None:
        """TOCTOU: a post-authorize workspace swap cannot change what executes."""
        self.set_behavior("record")
        binding, role, agents, spec, plan = self.make_binding()
        supervisor = LaunchSupervision(binding, kill_grace=0.3)
        authority = self.authorize(binding, role, agents, spec, plan)
        staged_backend_digest = authority._binding.backend
        # Mutate both the workspace wrapper and backend after the token minted
        # the exact committed bytes into the private staging directory.
        self.backend.write_text("#!/bin/sh\necho swapped\n", encoding="utf-8")
        wrapper = self.workspace / ".factory" / "tools" / WRAPPER_BASENAME
        wrapper.write_text("# swapped wrapper\n", encoding="utf-8")
        (self.workspace / launch.PI_FACTORY_GUARD_EXTENSION).write_text(
            "throw new Error('swapped extension');\n", encoding="utf-8"
        )
        (self.workspace / launch.PI_GIT_SHIM).write_text(
            "#!/bin/sh\nexit 99\n", encoding="utf-8"
        )
        result = supervisor.run(authority)
        self.assertEqual(result.outcome, "completed")
        self.assertEqual(result.returncode, 0)
        # The *committed* backend executed (it wrote its marker in record mode);
        # the swapped working-tree bytes never ran.
        self.assertTrue(
            (self.marker_dir / "prompt.digest").exists(),
            "the staged committed backend must have executed, not the swap",
        )
        argv = self.read_json("argv.json")
        staged_extension = authority._guard_extension
        self.assertEqual(
            argv[argv.index("--extension") + 1], str(staged_extension)
        )
        self.assertTrue(staged_extension.is_relative_to(authority._exec_dir))
        self.assertNotEqual(
            staged_extension,
            self.workspace / launch.PI_FACTORY_GUARD_EXTENSION,
            "the mutable worktree extension must never be executed",
        )

    def test_prompt_memfd_is_sealed_before_supervision(self) -> None:
        """No pathname/race window exists and the authority memfd is immutable."""
        self.set_behavior("record")
        binding, role, agents, spec, plan = self.make_binding()
        authority = self.authorize(binding, role, agents, spec, plan)
        self.assertFalse(hasattr(authority, "_prompt_path"))
        with self.assertRaises(OSError):
            os.pwrite(authority._prompt_fd, b"SUBSTITUTED PROMPT\n", 0)
        with self.assertRaises(OSError):
            os.ftruncate(authority._prompt_fd, 0)
        supervisor = LaunchSupervision(binding, kill_grace=0.3)
        result = supervisor.run(authority)
        self.assertEqual(result.returncode, 0)
        self.assertTrue((self.marker_dir / "prompt.digest").exists())

    def test_auth_fd_parent_copy_closes_and_resets_immediately_after_spawn(self) -> None:
        """Authority transfer leaves no parent-readable auth descriptor."""
        self.assertTrue(hasattr(os, "memfd_create"), "Linux memfd is mandatory")
        self.set_behavior("trap-term")
        binding, role, agents, spec, plan = self.make_binding(
            runtime_limit=5.0, inactivity_limit=0.5
        )
        authority = self.authorize(binding, role, agents, spec, plan)
        auth_fd = os.memfd_create("factory-parent-lifecycle", 0)
        os.write(auth_fd, b"synthetic-auth")
        authority._auth_fd = auth_fd
        identity = os.fstat(auth_fd)
        supervisor = LaunchSupervision(binding, kill_grace=0.3)
        real_invariants = launch.verify_child_invariants
        observed_live = []

        def assert_live_parent_and_broker_closed(pid, workspace, child_binding):
            # This callback runs immediately after Popen while the confinement
            # broker/target are live, not after run() cleanup.
            self.assertEqual(supervisor._auth_fd, -1)
            with self.assertRaises(OSError) as caught:
                os.fstat(auth_fd)
            self.assertEqual(caught.exception.errno, errno.EBADF)
            # Target-side readiness barrier: the backend has exec'd, written
            # its PID, and is sleeping before any simulated tool call.
            target_marker = self.marker_dir / "pid"
            deadline = time.monotonic() + 2.0
            while not target_marker.exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertTrue(target_marker.exists(), "target readiness marker missing")
            target_pid = int(target_marker.read_text(encoding="utf-8"))
            broker_fields = launch._proc_stat_fields(pid)
            target_fields = launch._proc_stat_fields(target_pid)
            self.assertIsNotNone(broker_fields)
            self.assertIsNotNone(target_fields)
            self.assertNotEqual(broker_fields[0], "Z")
            self.assertNotEqual(target_fields[0], "Z")

            auth_identity = (identity.st_dev, identity.st_ino)
            deadline = time.monotonic() + 2.0
            while True:
                broker_identities = set()
                for name in os.listdir(f"/proc/{pid}/fd"):
                    try:
                        info = os.stat(f"/proc/{pid}/fd/{name}")
                    except OSError:
                        continue
                    broker_identities.add((info.st_dev, info.st_ino))
                if auth_identity not in broker_identities or time.monotonic() >= deadline:
                    break
                time.sleep(0.01)
            self.assertNotIn(
                auth_identity, broker_identities,
                "the long-lived confinement broker retained the auth memfd",
            )
            observed_live.append(pid)
            return real_invariants(pid, workspace, child_binding)

        with unittest.mock.patch.object(
            launch, "verify_child_invariants", side_effect=assert_live_parent_and_broker_closed
        ):
            result = supervisor.run(authority)
        self.assertEqual(result.outcome, "terminated")
        self.assertTrue(observed_live)
        self.assertEqual(authority._auth_fd, -1)
        self.assertEqual(supervisor._auth_fd, -1)

    def test_spawn_failure_closes_auth_fd_after_authority_transfer(self) -> None:
        """Popen failure is covered by outer cleanup, including auth."""
        self.assertTrue(hasattr(os, "memfd_create"), "Linux memfd is mandatory")
        binding, role, agents, spec, plan = self.make_binding()
        authority = self.authorize(binding, role, agents, spec, plan)
        auth_fd = os.memfd_create("factory-parent-failure", 0)
        authority._auth_fd = auth_fd
        supervisor = LaunchSupervision(binding, kill_grace=0.3)
        with unittest.mock.patch.object(
            launch.subprocess, "Popen", side_effect=OSError(errno.EMFILE, "synthetic")
        ):
            with self.assertRaises(launch.LaunchError):
                supervisor.run(authority)
        self.assertEqual(authority._auth_fd, -1)
        self.assertEqual(supervisor._auth_fd, -1)
        with self.assertRaises(OSError) as caught:
            os.fstat(auth_fd)
        self.assertEqual(caught.exception.errno, errno.EBADF)

    def test_prompt_preflight_failure_closes_fds_and_private_dirs(self) -> None:
        """Outer run cleanup covers composition failure after descriptor transfer."""
        binding, role, agents, spec, plan = self.make_binding()
        authority = self.authorize(binding, role, agents, spec, plan)
        descriptors = [*authority._confinement_rule_fds, authority._prompt_fd]
        directories = list(launch._authority_private_directories(authority))
        supervisor = LaunchSupervision(binding, kill_grace=0.3)
        with unittest.mock.patch.object(
            launch, "compose_prompt", side_effect=InvocationError("prompt preflight")
        ), unittest.mock.patch.object(launch.subprocess, "Popen") as popen:
            with self.assertRaises(InvocationError):
                supervisor.run(authority)
        popen.assert_not_called()
        for descriptor in descriptors:
            with self.assertRaises(OSError):
                os.fstat(descriptor)
        for directory in directories:
            self.assertFalse(Path(directory).exists(), directory)

    def test_redactor_preflight_failure_closes_fds_and_private_dirs(self) -> None:
        """Outer run cleanup covers redactor failure before lifecycle try."""
        binding, role, agents, spec, plan = self.make_binding()
        authority = self.authorize(binding, role, agents, spec, plan)
        descriptors = [*authority._confinement_rule_fds, authority._prompt_fd]
        directories = list(launch._authority_private_directories(authority))
        supervisor = LaunchSupervision(binding, kill_grace=0.3)
        with unittest.mock.patch.object(
            launch.output_redaction,
            "redactor_for",
            side_effect=launch.output_redaction.OutputRedactionError("preflight"),
        ), unittest.mock.patch.object(launch.subprocess, "Popen") as popen:
            with self.assertRaises(SupervisionError):
                supervisor.run(authority)
        popen.assert_not_called()
        for descriptor in descriptors:
            with self.assertRaises(OSError):
                os.fstat(descriptor)
        for directory in directories:
            self.assertFalse(Path(directory).exists(), directory)

    def test_mutable_external_symlink_rejected(self) -> None:
        """A workspace symlink to a mutable external path never qualifies (F2)."""
        external = self.tmp / "external"
        external.mkdir()
        ext_backend = external / "backend.py"
        ext_backend.write_text(BACKEND_SOURCE, encoding="utf-8")
        os.chmod(ext_backend, 0o700)
        os.unlink(self.backend)
        os.symlink(str(ext_backend), self.backend)
        binding, role, agents, spec, plan = self.make_binding()
        with self.assertRaises(launch.InvocationError):
            self.authorize(binding, role, agents, spec, plan)

    def test_mutable_external_dir_rejected(self) -> None:
        """The immutable-chain authority rejects a caller-owned mutable dir."""
        mutable = self.tmp / "mutable-dir"
        mutable.mkdir()
        script = mutable / "tool.py"
        script.write_text("#!/usr/bin/env python3\nprint('x')\n", encoding="utf-8")
        os.chmod(script, 0o500)
        with self.assertRaises(gitutil.GitBoundaryError):
            gitutil.require_trusted_executable(str(script))


class CredentialReturnTests(_Base):
    """Focused credential-return pipe consume/publish/cleanup unit tests.

    The trusted parent provisions one private pipe per openai-codex launch;
    the model-side extension returns the detached credential bytes there at
    session shutdown.  These tests exercise the parent-side consume contract:
    exactly one JSON document, a 1 MiB cap, EOF ordering after the child is
    reaped, and fd/thread cleanup on every path.
    """

    def _openai_supervisor(self) -> launch.LaunchSupervision:
        """Build a supervisor bound to an openai-codex provider."""
        binding, role, agents, spec, plan = self.make_binding()
        codex = launch.InvocationBinding(
            role=binding.role,
            model=binding.model,
            provider="openai-codex",
            backend=binding.backend,
            workspace=binding.workspace,
            bound_commit=binding.bound_commit,
            role_prompt_digest=binding.role_prompt_digest,
            prompt_set_digest=binding.prompt_set_digest,
            plan_digest=binding.plan_digest,
            policy_digest=binding.policy_digest,
            specification_digest=binding.specification_digest,
            allowed_tools=binding.allowed_tools,
            task_id=binding.task_id,
            task_excerpt_digest=binding.task_excerpt_digest,
            audit_objective_digest=binding.audit_objective_digest,
            runtime_limit=binding.runtime_limit,
            inactivity_limit=binding.inactivity_limit,
        )
        return launch.LaunchSupervision(codex, kill_grace=0.3)

    def _valid_document(self) -> bytes:
        return json.dumps({
            "openai-codex": {
                "type": "oauth", "access": "SYNTHETIC-ACCESS-VALUE",
                "refresh": "SYNTHETIC-REFRESH-VALUE",
                "accountId": "synthetic-account",
                "expires": int(time.time() * 1000) + 3_600_000,
            },
        }).encode("utf-8")

    def test_valid_single_document_returned(self) -> None:
        supervisor = self._openai_supervisor()
        valid = self._valid_document()
        supervisor._credential_return_data = bytearray(valid)
        supervisor._credential_return_eof = True
        returned = supervisor._consume_credential_return()
        self.assertEqual(returned, valid)
        self.assertIsNone(supervisor._credential_return_data)

    def test_missing_returns_none(self) -> None:
        supervisor = self._openai_supervisor()
        supervisor._credential_return_data = None
        supervisor._credential_return_eof = True
        self.assertIsNone(supervisor._consume_credential_return())

    def test_malformed_raises(self) -> None:
        supervisor = self._openai_supervisor()
        supervisor._credential_return_data = bytearray(b"{not-json")
        supervisor._credential_return_eof = True
        with self.assertRaises(launch.SupervisionError):
            supervisor._consume_credential_return()

    def test_oversize_raises(self) -> None:
        supervisor = self._openai_supervisor()
        supervisor._credential_return_oversize = True
        supervisor._credential_return_data = bytearray()
        supervisor._credential_return_eof = True
        with self.assertRaises(launch.SupervisionError):
            supervisor._consume_credential_return()

    def test_concatenated_documents_raise(self) -> None:
        supervisor = self._openai_supervisor()
        supervisor._credential_return_data = bytearray(
            self._valid_document() + self._valid_document()
        )
        supervisor._credential_return_eof = True
        with self.assertRaises(launch.SupervisionError):
            supervisor._consume_credential_return()

    def test_ordering_after_reap_and_cleanup(self) -> None:
        """The reader thread reaches EOF only after the write end is closed
        (the child is reaped); cleanup joins the thread and closes both ends."""
        supervisor = self._openai_supervisor()
        supervisor._provision_credential_return_pipe()
        self.assertGreaterEqual(supervisor._credential_return_fd, 0)
        self.assertGreaterEqual(supervisor._credential_return_write_fd, 0)
        document = self._valid_document()
        # The model writes the returned credential and closes its write end
        # (the child process tree is fully terminated).
        os.write(supervisor._credential_return_write_fd, document)
        os.close(supervisor._credential_return_write_fd)
        supervisor._credential_return_write_fd = -1
        supervisor._credential_return_thread = threading.Thread(
            target=supervisor._drain_credential_return, daemon=True
        )
        supervisor._credential_return_thread.start()
        returned = supervisor._consume_credential_return()
        self.assertEqual(returned, document)
        self.assertTrue(supervisor._credential_return_eof)
        # Cleanup is idempotent and closes the read end / joins the thread.
        supervisor._cleanup()
        supervisor._cleanup()
        self.assertEqual(supervisor._credential_return_fd, -1)
        self.assertEqual(supervisor._credential_return_write_fd, -1)
        self.assertIsNone(supervisor._credential_return_thread)

    def test_long_session_beyond_old_drain_window_still_consumes(self) -> None:
        """Regression: the reader starts at spawn and must drain for an
        arbitrarily long bounded campaign runtime, so a session longer than the
        old 30s spawn-relative drain window must still reach EOF and consume.
        The selector is mocked to report no events for 70 polls (each 0.5s =>
        35s, beyond the removed 30s deadline) before the credential becomes
        readable, without actually sleeping."""
        supervisor = self._openai_supervisor()
        supervisor._provision_credential_return_pipe()
        document = self._valid_document()
        # The model writes the returned credential and closes its write end at
        # session shutdown; the reader only notices it after a long idle poll.
        os.write(supervisor._credential_return_write_fd, document)
        os.close(supervisor._credential_return_write_fd)
        supervisor._credential_return_write_fd = -1

        class _LongIdleSelector:
            def __init__(self) -> None:
                self._calls = 0
                self._closed = False

            def register(self, fileobj, events):
                pass

            def select(self, timeout=None):
                self._calls += 1
                # 70 idle polls * 0.5s = 35s, beyond the old 30s deadline.
                if self._calls <= 70:
                    return []
                return [(None, selectors.EVENT_READ)]

            def close(self):
                self._closed = True

        fake = _LongIdleSelector()
        with unittest.mock.patch.object(
            launch.selectors, "DefaultSelector", return_value=fake
        ):
            supervisor._credential_return_thread = threading.Thread(
                target=supervisor._drain_credential_return, daemon=True
            )
            supervisor._credential_return_thread.start()
            returned = supervisor._consume_credential_return()
        self.assertEqual(returned, document)
        self.assertTrue(supervisor._credential_return_eof)
        self.assertTrue(fake._closed)
        supervisor._cleanup()
        self.assertEqual(supervisor._credential_return_fd, -1)
        self.assertIsNone(supervisor._credential_return_thread)

    def test_selector_register_failure_fails_closed_without_hang(self) -> None:
        """A selector that cannot register the read end must fail closed (no
        EOF, no consumed bytes) and return promptly rather than busy-looping
        on an empty selector or hanging the drain thread."""
        supervisor = self._openai_supervisor()
        supervisor._provision_credential_return_pipe()
        self.assertGreaterEqual(supervisor._credential_return_fd, 0)

        class _RegisterFailingSelector:
            def __init__(self) -> None:
                self._closed = False

            def register(self, fileobj, events):
                raise OSError("synthetic register failure")

            def select(self, timeout=None):
                raise AssertionError("select must never be reached")

            def close(self):
                self._closed = True

        fake = _RegisterFailingSelector()
        with unittest.mock.patch.object(
            launch.selectors, "DefaultSelector", return_value=fake
        ):
            supervisor._credential_return_thread = threading.Thread(
                target=supervisor._drain_credential_return, daemon=True
            )
            supervisor._credential_return_thread.start()
            supervisor._credential_return_thread.join(timeout=5.0)
        self.assertFalse(supervisor._credential_return_thread.is_alive())
        self.assertFalse(supervisor._credential_return_eof)
        self.assertEqual(supervisor._credential_return_data, bytearray())
        self.assertFalse(supervisor._credential_return_oversize)
        self.assertTrue(fake._closed)
        supervisor._cleanup()
        self.assertEqual(supervisor._credential_return_fd, -1)
        self.assertIsNone(supervisor._credential_return_thread)

    # -- subprocess-level return-channel tests -------------------------------
    # A real child process (the model) inherits the return write end, seals it
    # exactly like the extension (CLOEXEC duplicate, original closed), spawns
    # a descendant that attempts to hold/write the descriptor, returns a valid
    # credential document, and exits.  The parent drains through the real
    # reader thread, consumes only after EOF (the child is reaped), and
    # atomically persists the returned bytes.

    RETURN_CHILD_SOURCE = r'''
import os, subprocess, sys

fd = int(sys.argv[1])
marker = sys.argv[2]
descendant = sys.argv[3]
document_path = sys.argv[4]
# Seal exactly like the extension: duplicate through /proc/self/fd with
# O_WRONLY|O_CLOEXEC, close the original inheritable descriptor.
dup = os.open(f"/proc/self/fd/{fd}", os.O_WRONLY | os.O_CLOEXEC)
os.close(fd)
# A descendant spawned by the model must not inherit the sealed descriptor
# (CLOEXEC) and therefore cannot write it.
result = subprocess.run(
    [sys.executable, descendant, str(dup)], capture_output=True, timeout=20
)
with open(marker, "w") as stream:
    stream.write(f"rc={result.returncode}")
if result.returncode != 0:
    os._exit(9)
with open(document_path, "rb") as stream:
    document = stream.read()
os.write(dup, document)
os.close(dup)
os._exit(0)
'''

    RETURN_DESCENDANT_SOURCE = r'''
import os, sys

fd = int(sys.argv[1])
try:
    os.write(fd, b"x")
except OSError as error:
    if error.errno == 9:  # EBADF: the sealed descriptor was not inherited
        os._exit(0)
    os._exit(3)
os._exit(2)
'''

    def test_subprocess_descendant_cannot_inherit_and_persist_after_completion(self) -> None:
        """A real child seals the return fd; its descendant cannot inherit or
        write it, and the completed valid return is atomically persisted only
        after the child (broker/leader) is reaped and EOF is reached."""
        supervisor = self._openai_supervisor()
        supervisor._provision_credential_return_pipe()
        write_fd = supervisor._credential_return_write_fd
        child_script = self.tmp / "return-child.py"
        child_script.write_text(self.RETURN_CHILD_SOURCE, encoding="utf-8")
        descendant_script = self.tmp / "return-descendant.py"
        descendant_script.write_text(
            self.RETURN_DESCENDANT_SOURCE, encoding="utf-8"
        )
        marker = self.tmp / "descendant.marker"
        document = self._valid_document()
        document_path = self.tmp / "return-document.json"
        document_path.write_bytes(document)
        child = subprocess.Popen(
            [sys.executable, str(child_script), str(write_fd),
             str(marker), str(descendant_script), str(document_path)],
            pass_fds=(write_fd,),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        # The parent must not keep a writable copy: close it immediately so
        # EOF arrives exactly when the child closes its sealed copy.
        os.close(write_fd)
        supervisor._credential_return_write_fd = -1
        supervisor._credential_return_thread = threading.Thread(
            target=supervisor._drain_credential_return, daemon=True
        )
        supervisor._credential_return_thread.start()
        _, err = child.communicate(timeout=30)
        self.assertEqual(child.returncode, 0, err)
        # The descendant could not inherit or write the sealed return fd.
        self.assertEqual(marker.read_text(encoding="utf-8").strip(), "rc=0")
        # The completed valid return is consumed only after EOF (the child is
        # reaped) and atomically persisted to the private home.
        returned = supervisor._consume_credential_return()
        self.assertEqual(returned, document)
        home = self.tmp / "sanitized-home"
        home.mkdir()
        supervisor._sanitized_home = home
        supervisor._publish_credential_return_to_private_home(returned)
        persisted = home / ".pi" / "agent2" / "auth.json"
        self.assertEqual(persisted.read_bytes(), returned)
        self.assertEqual(persisted.stat().st_mode & 0o777, 0o600)
        self.assertEqual(persisted.stat().st_nlink, 1)
        supervisor._cleanup()

    def test_timeout_cleanup_zeroes_and_fails_closed(self) -> None:
        """A write end that never closes (no EOF) fails the consume closed
        within the bounded join window, zeroes the buffer, and cleanup
        stops/joins/closes without deadlock."""
        supervisor = self._openai_supervisor()
        supervisor._provision_credential_return_pipe()
        write_fd = supervisor._credential_return_write_fd
        holder = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            pass_fds=(write_fd,),
        )
        os.close(write_fd)
        supervisor._credential_return_write_fd = -1
        supervisor._credential_return_thread = threading.Thread(
            target=supervisor._drain_credential_return, daemon=True
        )
        supervisor._credential_return_thread.start()
        with self.assertRaises(launch.SupervisionError):
            supervisor._consume_credential_return()
        self.assertIsNone(supervisor._credential_return_data)
        self.assertFalse(supervisor._credential_return_eof)
        supervisor._cleanup()
        holder.kill()
        holder.wait(timeout=10)
        self.assertEqual(supervisor._credential_return_fd, -1)
        self.assertEqual(supervisor._credential_return_write_fd, -1)
        self.assertIsNone(supervisor._credential_return_thread)

    # -- publish adversarial tests -------------------------------------------
    # The publish path is hardened against an untrusted filesystem state
    # between the operator home and ``.pi/agent2``: every directory component
    # is pinned once with O_DIRECTORY|O_NOFOLLOW and fstat-bound to the lstat
    # expectation taken just before the open; only dirfd-relative temp
    # creation, rename, and cleanup are used afterwards, so pathname swaps
    # cannot redirect the credential.

    def test_publish_fails_closed_when_agent_dir_swapped_before_pin(self) -> None:
        """A swap of the ``agent2`` path between the lstat expectation and the
        pinned ``O_DIRECTORY|O_NOFOLLOW`` open is detected by the fstat
        dev/ino/mode/uid bind and fails closed: neither the attacker directory
        nor the displaced original receives the credential."""
        home = self.tmp / "sanitized-home"
        agent_dir = home / ".pi" / "agent2"
        agent_dir.mkdir(parents=True)
        supervisor = self._openai_supervisor()
        supervisor._sanitized_home = home
        returned = self._valid_document()
        attacker = self.tmp / "attacker-agent-dir"
        real_open = os.open
        swapped = False

        def staged_open(path, flags, mode=0o777, *, dir_fd=None):
            nonlocal swapped
            if path == "agent2" and dir_fd is not None and not swapped:
                swapped = True
                agent_dir.rename(self.tmp / "agent2.original")
                attacker.mkdir()
                (attacker / "attacker-marker").write_text("owned")
            return real_open(path, flags, mode, dir_fd=dir_fd)

        with unittest.mock.patch("os.open", side_effect=staged_open):
            with self.assertRaises(launch.SupervisionError):
                supervisor._publish_credential_return_to_private_home(returned)
        self.assertTrue(swapped)
        # Bytes never reached the attacker directory or the displaced original.
        self.assertFalse(attacker.joinpath("auth.json").exists())
        self.assertFalse(
            (self.tmp / "agent2.original" / "auth.json").exists()
        )
        self.assertEqual(attacker.joinpath("attacker-marker").read_text(), "owned")

    def test_publish_swap_after_dirfd_pin_stays_anchored_in_original(self) -> None:
        """Swapping the ``.pi/agent2`` pathname after the directory
        descriptors are pinned cannot redirect the credential: the rename is
        anchored to the pinned descriptor, so the bytes land only in the
        displaced original home and never in the attacker-created directory."""
        home = self.tmp / "sanitized-home"
        agent_dir = home / ".pi" / "agent2"
        agent_dir.mkdir(parents=True)
        supervisor = self._openai_supervisor()
        supervisor._sanitized_home = home
        returned = self._valid_document()
        real_open = os.open
        swapped = False

        def staged_open(path, flags, mode=0o777, *, dir_fd=None):
            nonlocal swapped
            if (dir_fd is not None and (flags & os.O_CREAT)
                    and (flags & os.O_EXCL)
                    and path.startswith(".auth-return-") and not swapped):
                # The pinned agent-directory descriptor is already held;
                # divert the pathname the attacker controls.
                swapped = True
                agent_dir.rename(home / ".pi" / "agent2.anchor")
                attacker = home / ".pi" / "agent2"
                attacker.mkdir()
                (attacker / "attacker-marker").write_text("owned")
            return real_open(path, flags, mode, dir_fd=dir_fd)

        with unittest.mock.patch("os.open", side_effect=staged_open):
            supervisor._publish_credential_return_to_private_home(returned)
        self.assertTrue(swapped)
        anchor = home / ".pi" / "agent2.anchor"
        published = anchor / "auth.json"
        self.assertEqual(published.read_bytes(), returned)
        self.assertEqual(published.stat().st_mode & 0o777, 0o600)
        self.assertEqual(published.stat().st_nlink, 1)
        attacker = home / ".pi" / "agent2"
        self.assertFalse(attacker.joinpath("auth.json").exists())
        self.assertEqual(attacker.joinpath("attacker-marker").read_text(), "owned")
        # No temp litter remains in the anchored directory.
        self.assertEqual(list(anchor.glob(".auth-return-*")), [])

    def test_publish_fails_closed_on_symlink_components(self) -> None:
        """A symlinked ``agent2`` or ``.pi`` component fails closed before any
        write and never resolves through the link."""
        outsider = self.tmp / "outsider"
        outsider.mkdir()
        for component in ("agent2", ".pi"):
            with self.subTest(component=component):
                home = self.tmp / f"symlink-home-{component}"
                home.mkdir()
                if component == "agent2":
                    (home / ".pi").mkdir()
                    (home / ".pi" / "agent2").symlink_to(
                        outsider, target_is_directory=True
                    )
                else:
                    (home / ".pi").symlink_to(outsider, target_is_directory=True)
                supervisor = self._openai_supervisor()
                supervisor._sanitized_home = home
                with self.assertRaises(launch.SupervisionError):
                    supervisor._publish_credential_return_to_private_home(
                        self._valid_document()
                    )
                self.assertFalse(outsider.joinpath("auth.json").exists())
                self.assertFalse(
                    (home / ".pi" / "agent2" / "auth.json").exists()
                )

    def test_publish_replaces_hardlinked_target_without_writing_through(self) -> None:
        """A pre-existing ``auth.json`` hardlinked to a victim file is
        atomically replaced by a fresh single-link mode-0600 file; the victim
        inode is untouched and the temp precondition (nlink/mode) holds."""
        home = self.tmp / "sanitized-home"
        agent_dir = home / ".pi" / "agent2"
        agent_dir.mkdir(parents=True)
        victim = self.tmp / "victim-auth.json"
        victim.write_bytes(b"victim-bytes")
        os.link(victim, agent_dir / "auth.json")
        supervisor = self._openai_supervisor()
        supervisor._sanitized_home = home
        returned = self._valid_document()
        supervisor._publish_credential_return_to_private_home(returned)
        published = agent_dir / "auth.json"
        self.assertEqual(published.read_bytes(), returned)
        self.assertEqual(published.stat().st_mode & 0o777, 0o600)
        self.assertEqual(published.stat().st_nlink, 1)
        self.assertEqual(victim.read_bytes(), b"victim-bytes")
        self.assertEqual(list(agent_dir.glob(".auth-return-*")), [])

    def test_publish_fails_closed_when_component_is_not_a_directory(self) -> None:
        """A regular file in the component chain fails the directory
        precondition closed with no writes anywhere."""
        for component in ("pi-file", "agent2-file"):
            with self.subTest(component=component):
                home = self.tmp / f"file-home-{component}"
                if component == "pi-file":
                    home.mkdir()
                    (home / ".pi").write_bytes(b"not a directory")
                else:
                    (home / ".pi").mkdir(parents=True)
                    (home / ".pi" / "agent2").write_bytes(b"not a directory")
                supervisor = self._openai_supervisor()
                supervisor._sanitized_home = home
                with self.assertRaises(launch.SupervisionError):
                    supervisor._publish_credential_return_to_private_home(
                        self._valid_document()
                    )
                self.assertFalse(
                    (home / ".pi" / "agent2" / "auth.json").exists()
                )


if __name__ == "__main__":
    unittest.main(verbosity=2)
