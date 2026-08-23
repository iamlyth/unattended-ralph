#!/usr/bin/env python3
"""Harness-owned adversarial tests for the hidden Ollama usage guard (Task 7).

QUOTA-01 and QUOTA-02 (§10, §22 tests 13 and 24) are verified here under the
hidden ``.factory/tests/`` namespace (HIDE-01 keeps harness-only tests out of
the adopting product's visible ``tests/`` tree).  Everything runs against
synthetic tokens only — no real credential, no external network: the network
path is exercised exclusively through a loopback ``127.0.0.1`` HTTP server
serving the committed fixture pages.

Coverage:

* **parse contract**: ``usage-ok.html`` / ``usage-blocked.html`` /
  ``login.html`` and the new malformed/partial/secret-hint fixtures drive the
  retained parse path with the exact exit table (0 allowed / 1 quota / 2
  fatal / 3 transient) and the machine ``--json`` payload;
* **fail-closed pre-fetch gate**: a *network* guard with no valid private
  cookie is fatal (exit 2) before any fetch, so a missing cookie can never
  masquerade as a transient failure and drift into an unbounded ``--wait``;
* **hermetic stores**: every guard invocation in this suite points ``--env-file``
  at a store that does not exist (or passes an explicit synthetic store), so
  the operator's real ``<root>/.ollama-usage-env`` is never read and every
  poll/wait bound is explicit and tiny; the whole suite runs under an outer
  60s bound with no survivor processes;
* **§10 decision table**: ``require_quota`` (the launch-authority hook)
  enforces check → wait → final-check and blocks on every blocking exit,
  including the final-check-must-exit-0 rule;
* **wait behavior**: bounds (max polls / max wait), quota clearing, and
  clean TERM/INT/HUP aborts with no survivor;
* **argv/environ/proc scan (§22 test 24)**: a live in-flight fetch child's
  ``/proc/<pid>/cmdline`` and ``/proc/<pid>/environ`` never carry the
  synthetic cookie name, value, or any ``OLLAMA_*`` key — during both
  ``--check`` and ``--wait``;
* **transport strictness**: bounded no-follow mode/owner/link checks for the
  cookie store and the ``.ollama-usage-env`` store; bounded stdin; no cookie
  in argv or environment by construction;
* **malformed responses** fail closed (parse/auth/internal); **timeout and
  retry** classify as transient and retry in wait mode;
* **secret-shaped output redaction**: credential tokens embedded in a page
  hint are ``[redacted]`` from stdout/stderr/JSON and never appear in any
  captured output;
* **cleanup**: no owned temporary material is left behind and no fetch child
  survives any outcome;
* **launch-authority integration**: an ``ollama``-provider invocation passes
  the guard before the authority is minted (ok proceeds, blocked/fatal fail
  closed as :class:`InvocationError`), and a non-Ollama provider never
  triggers the guard.
"""

from __future__ import annotations

import contextlib
import hashlib
import http.server
import inspect
import io
import json
import os
from pathlib import Path
import shutil
import signal
import socket
import socketserver
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import unittest.mock as mock

ROOT = Path(__file__).resolve().parents[2]
LOOP = ROOT / ".factory" / "loop"
FIXTURES = ROOT / ".factory" / "tests" / "fixtures"
VISIBLE_FIXTURES = ROOT / "tests" / "fixtures"

sys.path.insert(0, str(LOOP))
import confinement  # noqa: E402
import usage  # noqa: E402
import usage_fetch  # noqa: E402
import launch  # noqa: E402

PY = sys.executable
GUARD = LOOP / "usage.py"

# An immutable, reachable trusted executable for the provider/backend fixtures.
# ``/bin/true`` does not exist under the Nix store layout, so the registry and
# confinement tests bind the backend to the resolved ``true`` executable (an
# immutable store path) rather than weakening ``verify_invocation``'s
# reachability/regular-file check to accommodate a nonexistent path.
TRUSTED_EXECUTABLE = Path(shutil.which("true")).resolve()

SYNTH_COOKIE_NAME = "__Secure-session"
SYNTH_COOKIE_VALUE = "synth-cookie-value-9f3a1c7b"
SYNTH_AID = "synth-aid-77aa99"
SYNTH_COOKIE = f"{SYNTH_COOKIE_NAME}={SYNTH_COOKIE_VALUE}; aid={SYNTH_AID}"


@contextlib.contextmanager
def _scrubbed_ollama_env():
    """Run one in-process guard call without any ambient OLLAMA_* key.

    The suite must never depend on (or read) the operator's real cookie
    store or ambient credentials, so every in-process ``require_quota``
    call that intends a missing-cookie outcome removes every OLLAMA_* key
    from ``os.environ`` for its duration.
    """
    with mock.patch.dict(os.environ, {}, clear=False):
        for key in [k for k in os.environ if k.startswith("OLLAMA_")]:
            del os.environ[key]
        yield


# ---------------------------------------------------------------------------
# Hermetic loopback server (the only "network" in the suite)
# ---------------------------------------------------------------------------

class _ScriptedServer:
    """A loopback HTTP server that serves a scripted response sequence.

    ``script`` is a list of ``(status, body)`` or ``(status, body, headers)``
    entries served in order (the last entry repeats); ``headers`` is a
    mapping of extra response headers (for example ``Location`` on a
    redirect).  ``hold`` makes each request wait for :meth:`release`; an
    integer ``hold`` waits only on that 1-indexed request, so a held
    in-flight fetch child can be scanned or signalled deterministically.
    """

    def __init__(self, script, *, hold: bool | int = False) -> None:
        self.script = script
        self.hold = hold
        self.index = 0
        self.requests = 0
        self.release_event = threading.Event()
        self.request_seen = threading.Event()
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(16)
        self.port = self._sock.getsockname()[1]
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        while True:
            try:
                conn, _addr = self._sock.accept()
            except OSError:
                return
            try:
                data = b""
                while b"\r\n\r\n" not in data:
                    chunk = conn.recv(4096)
                    if not chunk:
                        break
                    data += chunk
                self.requests += 1
                held = self.hold is True or (
                    isinstance(self.hold, int) and self.requests == self.hold
                )
                if held:
                    self.request_seen.set()
                    if not self.release_event.wait(60):
                        conn.close()
                        continue
                status, body = self.script[min(self.index, len(self.script) - 1)][:2]
                extra_headers = (
                    self.script[min(self.index, len(self.script) - 1)][2]
                    if len(self.script[min(self.index, len(self.script) - 1)]) > 2
                    else {}
                )
                self.index += 1
                self.request_seen.set()
                head = b"HTTP/1.1 " + str(status).encode() + b" OK\r\n"
                for name, value in extra_headers.items():
                    head += name.encode() + b": " + str(value).encode() + b"\r\n"
                head += (
                    b"Content-Length: " + str(len(body)).encode() + b"\r\n"
                    b"Connection: close\r\n\r\n"
                )
                conn.sendall(head + body)
            except OSError:
                pass
            finally:
                conn.close()

    def release(self) -> None:
        self.release_event.set()

    def close(self) -> None:
        self.release_event.set()
        self._sock.close()


class _Base(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="factory-usage-test."))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        # ``no-store`` is a hermetic env-store path that never exists: the
        # suite must never read the operator's real ``<root>/.ollama-usage-env``
        # (the guard's default store) even when a guard call forgets to pass
        # its own store.
        self.no_store = str(self.tmp / "no-store")

    def run_guard(
        self,
        *args,
        input_bytes: bytes = b"",
        cookie_file: Path | None = None,
        env: dict | None = None,
    ) -> subprocess.CompletedProcess[bytes]:
        argv = [PY, str(usage_path())]
        argv += [str(a) for a in args]
        # Never read the operator's real default store: when the caller does
        # not supply its own ``--env-file``, point the guard at a store that
        # does not exist so every test run is hermetic.
        if "--env-file" not in [str(a) for a in args]:
            argv += ["--env-file", self.no_store]
        base_env = dict(os.environ)
        base_env.pop("OLLAMA_COOKIE", None)
        for key in list(base_env):
            if key.startswith("OLLAMA_"):
                base_env.pop(key)
        if env:
            base_env.update(env)
        result = subprocess.run(
            argv,
            input=input_bytes,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=base_env,
            cwd=str(ROOT),
        )
        return result

    def assertNoLiveFetchChildren(self) -> None:
        """No synthetic fetch child survives any guard outcome."""
        for pid in os.listdir("/proc"):
            if not pid.isdigit():
                continue
            try:
                cmdline = Path(f"/proc/{pid}/cmdline").read_bytes()
            except OSError:
                continue
            if b"usage_fetch.py" not in cmdline:
                continue
            self.fail(f"surviving fetch child pid {pid}")

    def make_cookie_file(self, cookie: str = SYNTH_COOKIE, mode: int = 0o600) -> Path:
        path = self.tmp / "cookie.txt"
        path.write_text(cookie, encoding="utf-8")
        os.chmod(path, mode)
        return path


def usage_path() -> Path:
    return LOOP / "usage.py"


# ---------------------------------------------------------------------------
# Retained parse contract and exit table
# ---------------------------------------------------------------------------

class ParseContractTests(_Base):
    def _fixture(self, name: str) -> str:
        return str(VISIBLE_FIXTURES / name)

    def test_check_ok_fixture(self) -> None:
        proc = self.run_guard(
            "--check", "--json", "--html-file", self._fixture("usage-ok.html")
        )
        self.assertEqual(proc.returncode, usage.EXIT_ALLOWED)
        payload = json.loads(proc.stdout)
        self.assertEqual(
            set(payload),
            {"session_percent", "weekly_percent", "threshold_percent",
             "blocked", "reset_hint"},
        )
        self.assertFalse(payload["blocked"])
        self.assertEqual(payload["session_percent"], 12.5)
        self.assertEqual(payload["weekly_percent"], 44.0)
        self.assertEqual(payload["threshold_percent"], 80.0)
        self.assertIsInstance(payload["reset_hint"], str)

    def test_blocked_fixture_exit_one(self) -> None:
        proc = self.run_guard(
            "--check", "--json", "--html-file", self._fixture("usage-blocked.html")
        )
        self.assertEqual(proc.returncode, usage.EXIT_BLOCKED)
        payload = json.loads(proc.stdout)
        self.assertTrue(payload["blocked"])

    def test_login_fixture_exit_two(self) -> None:
        proc = self.run_guard(
            "--check", "--html-file", self._fixture("login.html")
        )
        self.assertEqual(proc.returncode, usage.EXIT_FATAL)
        self.assertIn(b"authentication expired", proc.stderr)

    def test_malformed_fixture_exit_two(self) -> None:
        """A page with the Settings title but unparseable usage fields is fatal.

        Non-vacuous: the committed malformed fixture drives the retained parse
        path (not a missing-file path), so the exit and the exact diagnostic
        are asserted.
        """
        proc = self.run_guard(
            "--check", "--html-file", str(FIXTURES / "usage-malformed.html")
        )
        self.assertEqual(proc.returncode, usage.EXIT_FATAL)
        self.assertIn(b"settings page format changed", proc.stderr)

    def test_partial_fixture_exit_two(self) -> None:
        """A page missing one of the two usage fields is fatal.

        Non-vacuous: the committed partial fixture reaches the retained parse
        path and the diagnostic identifies the missing field, rather than
        failing vacuously on a missing file.
        """
        proc = self.run_guard(
            "--check", "--html-file", str(FIXTURES / "usage-partial.html")
        )
        self.assertEqual(proc.returncode, usage.EXIT_FATAL)
        self.assertIn(b"settings page format changed", proc.stderr)
        self.assertIn(b"session=''", proc.stderr)

    def test_missing_cookie_network_mode_exit_two(self) -> None:
        proc = self.run_guard("--check", "--settings-url", "http://127.0.0.1:1/")
        self.assertEqual(proc.returncode, usage.EXIT_FATAL)
        self.assertIn(b"OLLAMA_COOKIE is missing", proc.stderr)

    def test_json_with_wait_rejected(self) -> None:
        proc = self.run_guard(
            "--json", "--wait", "--html-file", self._fixture("usage-ok.html")
        )
        self.assertEqual(proc.returncode, usage.EXIT_FATAL)

    def test_html_file_mode_needs_no_cookie(self) -> None:
        proc = self.run_guard(
            "--check", "--html-file", self._fixture("usage-ok.html")
        )
        self.assertEqual(proc.returncode, usage.EXIT_ALLOWED)

    def test_classify_html_ported_regexes(self) -> None:
        ok = (VISIBLE_FIXTURES / "usage-ok.html").read_text(encoding="utf-8")
        self.assertEqual(usage.classify_html(ok)[0:3], ("usage", "12.5", "44.0"))
        blocked = (VISIBLE_FIXTURES / "usage-blocked.html").read_text(
            encoding="utf-8"
        )
        self.assertEqual(
            usage.classify_html(blocked)[0:3], ("usage", "82.0", "45.0")
        )
        login = (VISIBLE_FIXTURES / "login.html").read_text(encoding="utf-8")
        self.assertEqual(usage.classify_html(login)[0], "auth")


# ---------------------------------------------------------------------------
# The §10 decision table (the launch-authority hook)
# ---------------------------------------------------------------------------

class DecisionTableTests(_Base):
    def test_allowed_proceeds(self) -> None:
        usage.require_quota(
            html_file=str(VISIBLE_FIXTURES / "usage-ok.html"),
            cookie_file=str(self.make_cookie_file()),
        )

    def test_blocked_blocks_launch(self) -> None:
        with self.assertRaises(usage.UsageQuotaBlocked):
            usage.require_quota(
                html_file=str(VISIBLE_FIXTURES / "usage-blocked.html"),
                cookie_file=str(self.make_cookie_file()),
                max_polls=1,
                poll_interval=0,
            )

    def test_fatal_blocks_launch(self) -> None:
        with self.assertRaises(usage.UsageQuotaBlocked):
            usage.require_quota(
                html_file=str(VISIBLE_FIXTURES / "login.html"),
                cookie_file=str(self.make_cookie_file()),
            )

    def test_missing_cookie_blocks_launch(self) -> None:
        """A network guard with no private cookie is fatal *before* fetch.

        Regression: a missing cookie previously fell through to the network
        fetch, classified the loopback failure as transient (exit 3), and
        then ``--wait`` slept the default 300s poll interval, timing the
        suite out.  The guard must fail closed before any fetch, so the
        in-flight probe server must never observe a request.
        """
        server = _ScriptedServer([(200, b"unreachable")], hold=True)
        self.addCleanup(server.close)
        with _scrubbed_ollama_env():
            with self.assertRaises(usage.UsageQuotaBlocked):
                usage.require_quota(
                    settings_url=f"http://127.0.0.1:{server.port}/",
                    env_file=self.no_store,
                )
        self.assertFalse(
            server.request_seen.is_set(),
            "the usage guard fetched despite having no private cookie",
        )
        self.assertNoLiveFetchChildren()

    def test_transient_then_allowed_waits_and_proceeds(self) -> None:
        server = _ScriptedServer(
            [
                (500, b"down"),
                (200, (VISIBLE_FIXTURES / "usage-ok.html").read_bytes()),
            ],
            hold=False,
        )
        self.addCleanup(server.close)
        usage.require_quota(
            settings_url=f"http://127.0.0.1:{server.port}/",
            cookie_file=str(self.make_cookie_file()),
            poll_interval=0,
            max_polls=5,
        )

    def test_final_check_must_exit_zero(self) -> None:
        # wait succeeds on request 2 (ok), then the *final* --check (request
        # 3) is blocked: the invocation must be refused (QUOTA-01).
        server = _ScriptedServer(
            [
                (500, b"down"),
                (200, (VISIBLE_FIXTURES / "usage-ok.html").read_bytes()),
                (200, (VISIBLE_FIXTURES / "usage-blocked.html").read_bytes()),
            ],
            hold=False,
        )
        self.addCleanup(server.close)
        with self.assertRaises(usage.UsageQuotaBlocked) as caught:
            usage.require_quota(
                settings_url=f"http://127.0.0.1:{server.port}/",
                cookie_file=str(self.make_cookie_file()),
                poll_interval=0,
                max_polls=5,
            )
        self.assertIn("final ollama usage --check", str(caught.exception))

    def test_wait_bounds_exhausted_blocks(self) -> None:
        server = _ScriptedServer([(200, b"page without usage fields")], hold=False)
        self.addCleanup(server.close)
        with self.assertRaises(usage.UsageQuotaBlocked):
            usage.require_quota(
                settings_url=f"http://127.0.0.1:{server.port}/",
                cookie_file=str(self.make_cookie_file()),
                poll_interval=0,
                max_polls=2,
            )


# ---------------------------------------------------------------------------
# --wait behavior: bounds and signal cleanup
# ---------------------------------------------------------------------------

class WaitBehaviorTests(_Base):
    def test_wait_returns_one_after_max_polls_blocked(self) -> None:
        proc = self.run_guard(
            "--wait",
            "--html-file",
            str(VISIBLE_FIXTURES / "usage-blocked.html"),
            "--max-polls",
            "1",
            "--poll-interval",
            "0",
        )
        self.assertEqual(proc.returncode, usage.EXIT_BLOCKED)
        self.assertIn(b"maximum poll count reached", proc.stderr)

    def test_wait_returns_zero_after_quota_clears(self) -> None:
        server = _ScriptedServer(
            [
                (200, (VISIBLE_FIXTURES / "usage-blocked.html").read_bytes()),
                (200, (VISIBLE_FIXTURES / "usage-ok.html").read_bytes()),
            ],
            hold=False,
        )
        self.addCleanup(server.close)
        proc = self.run_guard(
            "--wait",
            "--settings-url",
            f"http://127.0.0.1:{server.port}/",
            "--cookie-stdin",
            "--max-polls",
            "3",
            "--poll-interval",
            "0",
            input_bytes=SYNTH_COOKIE.encode(),
        )
        self.assertEqual(proc.returncode, usage.EXIT_ALLOWED)
        self.assertIn(b"quota available again", proc.stdout)

    def _assert_signal_cleanup(self, signum: int, expected_rc: int) -> None:
        proc = subprocess.Popen(
            [
                PY, str(usage_path()),
                "--wait",
                "--html-file",
                str(VISIBLE_FIXTURES / "usage-blocked.html"),
                "--poll-interval",
                "30",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=str(ROOT),
        )
        try:
            # First check completes quickly; then the guard sleeps 30s.
            time.sleep(1.0)
            proc.send_signal(signum)
            _out, err = proc.communicate(timeout=15)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.communicate()
        self.assertEqual(proc.returncode, expected_rc)
        self.assertNotIn(b"Traceback", err)
        self.assertNoLiveFetchChildren()

    def test_wait_terminates_cleanly_on_sigterm(self) -> None:
        self._assert_signal_cleanup(signal.SIGTERM, 128 + signal.SIGTERM)

    def test_wait_terminates_cleanly_on_sigint(self) -> None:
        self._assert_signal_cleanup(signal.SIGINT, 128 + signal.SIGINT)

    def test_wait_terminates_cleanly_on_sighup(self) -> None:
        self._assert_signal_cleanup(signal.SIGHUP, 128 + signal.SIGHUP)

    def test_signal_during_inflight_fetch_reaps_child(self) -> None:
        """A signal while the fetch child is mid-flight still reaps it.

        The live in-flight fetch child is *observed* in /proc first, then the
        guard is SIGINT'd: it must terminate and boundedly reap the child and
        exit ``128 + SIGINT`` on its own (no outer timeout may kill it, and
        the held server is released deterministically).
        """
        server = _ScriptedServer([(200, b"x")], hold=True)
        self.addCleanup(server.close)
        proc = subprocess.Popen(
            [
                PY, str(usage_path()),
                "--check",
                "--settings-url",
                f"http://127.0.0.1:{server.port}/",
                "--cookie-stdin",
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=str(ROOT),
        )
        proc.stdin.write(SYNTH_COOKIE.encode())
        proc.stdin.close()
        self.assertTrue(server.request_seen.wait(15), "fetch never started")
        # Observe the live credential-holding fetch child before signalling.
        child = None
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            child = _find_fetch_child(proc.pid)
            if child is not None:
                break
            time.sleep(0.02)
        self.assertIsNotNone(child, "live fetch child was never observed")
        proc.send_signal(signal.SIGINT)
        out, err = proc.communicate(timeout=15)
        self.assertEqual(proc.returncode, 128 + signal.SIGINT)
        self.assertNoLiveFetchChildren()
        self.assertNotIn(b"Traceback", err)
        server.release()


# ---------------------------------------------------------------------------
# argv/environ/proc scan (§22 test 24)
# ---------------------------------------------------------------------------

def _children_of(parent_pid: int) -> list[int]:
    """Live direct children of ``parent_pid`` (bounded /proc scan)."""
    children = []
    for pid in os.listdir("/proc"):
        if not pid.isdigit():
            continue
        try:
            with open(f"/proc/{pid}/stat", "rb") as stream:
                fields = stream.read().decode("utf-8", "replace").split()
            if int(fields[3]) == parent_pid:
                children.append(int(pid))
        except (OSError, IndexError, ValueError):
            continue
    return children


def _find_fetch_child(parent_pid: int) -> int | None:
    for pid in _children_of(parent_pid):
        try:
            cmdline = Path(f"/proc/{pid}/cmdline").read_bytes()
        except OSError:
            continue
        if b"usage_fetch.py" in cmdline:
            return pid
    return None


class ProcScanTests(_Base):
    def _assert_live_child_is_clean(self, parent_pid: int) -> int:
        """Find the live fetch child and fail on any credential leak."""
        child = None
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            child = _find_fetch_child(parent_pid)
            if child is not None:
                break
            time.sleep(0.02)
        self.assertIsNotNone(child, "live fetch child was never observed")
        cmdline = Path(f"/proc/{child}/cmdline").read_bytes()
        environ = Path(f"/proc/{child}/environ").read_bytes()
        for token in (
            SYNTH_COOKIE_NAME,
            SYNTH_COOKIE_VALUE,
            SYNTH_AID,
            "OLLAMA_COOKIE",
        ):
            self.assertNotIn(
                token.encode(), cmdline,
                f"synthetic credential {token!r} leaked into the fetch "
                "child's argv",
            )
            self.assertNotIn(
                token.encode(), environ,
                f"synthetic credential {token!r} leaked into the fetch "
                "child's environment",
            )
        for entry in environ.split(b"\x00"):
            self.assertFalse(
                entry.startswith(b"OLLAMA_"),
                f"an OLLAMA_* key reached the fetch child: {entry!r}",
            )
        return child

    def test_check_live_child_argv_environ_clean(self) -> None:
        """A live in-flight fetch child during ``--check`` leaks nothing."""
        server = _ScriptedServer(
            [(200, (VISIBLE_FIXTURES / "usage-ok.html").read_bytes())],
            hold=True,
        )
        self.addCleanup(server.close)
        proc = subprocess.Popen(
            [
                PY, str(usage_path()),
                "--check",
                "--settings-url",
                f"http://127.0.0.1:{server.port}/",
                "--cookie-stdin",
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=str(ROOT),
        )
        proc.stdin.write(SYNTH_COOKIE.encode())
        proc.stdin.close()
        self.assertTrue(server.request_seen.wait(15), "fetch never started")
        child = self._assert_live_child_is_clean(proc.pid)
        # The guard process itself must also be clean: the cookie only ever
        # entered through the private stdin pipe.
        guard_cmdline = Path(f"/proc/{proc.pid}/cmdline").read_bytes()
        guard_environ = Path(f"/proc/{proc.pid}/environ").read_bytes()
        for token in (SYNTH_COOKIE_NAME, SYNTH_COOKIE_VALUE, SYNTH_AID):
            self.assertNotIn(token.encode(), guard_cmdline)
            self.assertNotIn(token.encode(), guard_environ)
        server.release()
        out, err = proc.communicate(timeout=20)
        self.assertEqual(proc.returncode, usage.EXIT_ALLOWED, err.decode())
        self.assertNotIn(SYNTH_COOKIE_VALUE.encode(), out)
        self.assertNotIn(SYNTH_COOKIE_VALUE.encode(), err)

    def test_wait_live_child_argv_environ_clean(self) -> None:
        """A live in-flight fetch child during ``--wait`` leaks nothing."""
        server = _ScriptedServer(
            [(200, (VISIBLE_FIXTURES / "usage-blocked.html").read_bytes())],
            hold=True,
        )
        self.addCleanup(server.close)
        proc = subprocess.Popen(
            [
                PY, str(usage_path()),
                "--wait",
                "--settings-url",
                f"http://127.0.0.1:{server.port}/",
                "--cookie-stdin",
                "--poll-interval",
                "30",
                "--max-polls",
                "3",
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=str(ROOT),
        )
        proc.stdin.write(SYNTH_COOKIE.encode())
        proc.stdin.close()
        self.assertTrue(server.request_seen.wait(30), "fetch never started")
        self._assert_live_child_is_clean(proc.pid)
        server.release()
        # Let the guard finish its first blocked check and enter the 30s
        # sleep, then terminate it cleanly.
        time.sleep(0.5)
        proc.send_signal(signal.SIGTERM)
        out, err = proc.communicate(timeout=20)
        self.assertEqual(proc.returncode, 128 + signal.SIGTERM)
        self.assertNotIn(SYNTH_COOKIE_VALUE.encode(), out + err)
        self.assertNoLiveFetchChildren()

    def test_fetch_child_environment_is_allowlist_only(self) -> None:
        """The fetch child's env is built, never inherited (QUOTA-02).

        Ambient credential-shaped keys — including ``OLLAMA_COOKIE`` itself —
        are **scrubbed, never fatal** (Task 7 review, obligation 8): the
        parent environment is never a cookie source and never spawns a child
        with a credential.  The built environment contains only allowlist
        keys plus ``PYTHONPATH``/``PYTHONHASHSEED``, so no credential-shaped
        key can survive into ``/proc/<pid>/environ``.
        """
        env = usage.fetch_child_env(
            {
                **dict(os.environ),
                "OLLAMA_SYNTHETIC_COOKIE": SYNTH_COOKIE_VALUE,
                "OLLAMA_COOKIE": SYNTH_COOKIE,
                "PI_SESSION_TOKEN": "session-12345",
                "MY_API_KEY": "key-12345",
            }
        )
        for key in env:
            self.assertNotIn("OLLAMA_", key.upper())
            self.assertNotIn("COOKIE", key.upper())
            self.assertNotIn("TOKEN", key.upper())
            self.assertNotIn("API_KEY", key.upper())
        self.assertIn("PYTHONPATH", env)
        self.assertEqual(set(env) - {"PYTHONPATH", "PYTHONHASHSEED"},
                         set(usage.FETCH_ENV_ALLOWLIST) & set(os.environ))
        # Ambient credentials never raise: scrubbing is the contract, and
        # only a key that would *survive into the built environment* is a
        # hard error (none of the allowlist keys is credential-shaped).
        usage.fetch_child_env(
            {"OLLAMA_COOKIE": SYNTH_COOKIE, "MY_API_KEY": "key-12345"}
        )

    def test_fetch_child_argv_is_structural(self) -> None:
        argv = usage.fetch_child_argv("https://ollama.com/settings")
        for token in (SYNTH_COOKIE_NAME, SYNTH_COOKIE_VALUE, SYNTH_AID,
                      "OLLAMA_COOKIE"):
            self.assertNotIn(token, " ".join(argv))
        self.assertTrue(argv[0].startswith("/"))


# ---------------------------------------------------------------------------
# Bounded no-follow mode/owner/link transport checks
# ---------------------------------------------------------------------------

class TransportStrictnessTests(_Base):
    def test_cookie_file_group_writable_rejected(self) -> None:
        path = self.make_cookie_file(mode=0o664)
        proc = self.run_guard(
            "--check", "--html-file",
            str(VISIBLE_FIXTURES / "usage-ok.html"),
            "--cookie-file", str(path),
        )
        self.assertEqual(proc.returncode, usage.EXIT_FATAL)
        self.assertIn(b"must be mode 0600", proc.stderr)

    def test_cookie_file_symlink_rejected(self) -> None:
        target = self.make_cookie_file()
        link = self.tmp / "cookie-link"
        os.symlink(target, link)
        proc = self.run_guard(
            "--check", "--html-file",
            str(VISIBLE_FIXTURES / "usage-ok.html"),
            "--cookie-file", str(link),
        )
        self.assertEqual(proc.returncode, usage.EXIT_FATAL)

    def test_cookie_file_oversize_rejected(self) -> None:
        path = self.tmp / "big-cookie"
        path.write_text("x" * (usage.MAX_COOKIE_BYTES + 1), encoding="utf-8")
        os.chmod(path, 0o600)
        proc = self.run_guard(
            "--check", "--html-file",
            str(VISIBLE_FIXTURES / "usage-ok.html"),
            "--cookie-file", str(path),
        )
        self.assertEqual(proc.returncode, usage.EXIT_FATAL)

    def test_cookie_file_missing_rejected(self) -> None:
        proc = self.run_guard(
            "--check", "--html-file",
            str(VISIBLE_FIXTURES / "usage-ok.html"),
            "--cookie-file", str(self.tmp / "missing"),
        )
        self.assertEqual(proc.returncode, usage.EXIT_FATAL)

    def test_cookie_file_wrong_owner_rejected_when_privileged(self) -> None:
        if os.geteuid() != 0:
            self.skipTest("owner tamper requires root")
        path = self.make_cookie_file()
        os.chown(path, 0, 0)  # root-owned while the test user is not root
        proc = self.run_guard(
            "--check", "--html-file",
            str(VISIBLE_FIXTURES / "usage-ok.html"),
            "--cookie-file", str(path),
        )
        self.assertEqual(proc.returncode, usage.EXIT_FATAL)

    def test_cookie_stdin_oversize_rejected(self) -> None:
        proc = self.run_guard(
            "--check", "--html-file",
            str(VISIBLE_FIXTURES / "usage-ok.html"),
            "--cookie-stdin",
            input_bytes=b"x" * (usage.MAX_COOKIE_BYTES + 1),
        )
        self.assertEqual(proc.returncode, usage.EXIT_FATAL)

    def test_env_store_group_writable_rejected(self) -> None:
        store = self.tmp / ".ollama-usage-env"
        store.write_text(
            "export OLLAMA_COOKIE='%s'\n" % SYNTH_COOKIE, encoding="utf-8"
        )
        os.chmod(store, 0o664)
        proc = self.run_guard(
            "--check", "--html-file",
            str(VISIBLE_FIXTURES / "usage-ok.html"),
            "--env-file", str(store),
        )
        self.assertEqual(proc.returncode, usage.EXIT_FATAL)
        self.assertIn(b"must be mode 0600", proc.stderr)

    def test_env_store_parse_and_cookie(self) -> None:
        store = self.tmp / ".ollama-usage-env"
        store.write_text(
            "export OLLAMA_COOKIE='%s'\nexport OLLAMA_THRESHOLD='70'\n"
            % SYNTH_COOKIE,
            encoding="utf-8",
        )
        os.chmod(store, 0o600)
        proc = self.run_guard(
            "--check", "--json", "--html-file",
            str(VISIBLE_FIXTURES / "usage-blocked.html"),
            "--env-file", str(store),
        )
        self.assertEqual(proc.returncode, usage.EXIT_BLOCKED)
        payload = json.loads(proc.stdout)
        self.assertEqual(payload["threshold_percent"], 70.0)
        self.assertTrue(payload["blocked"])
        # The cookie value must never appear in any output even though it
        # was read from the store.
        self.assertNotIn(SYNTH_COOKIE_VALUE.encode(), proc.stdout + proc.stderr)

    def test_env_store_malformed_value_fails_closed(self) -> None:
        store = self.tmp / ".ollama-usage-env"
        store.write_text("export OLLAMA_COOKIE='unterminated\n", encoding="utf-8")
        os.chmod(store, 0o600)
        proc = self.run_guard(
            "--check", "--html-file",
            str(VISIBLE_FIXTURES / "usage-ok.html"),
            "--env-file", str(store),
        )
        self.assertEqual(proc.returncode, usage.EXIT_FATAL)


# ---------------------------------------------------------------------------
# Secret-shaped output redaction
# ---------------------------------------------------------------------------

class RedactionTests(_Base):
    def test_secret_shaped_hint_is_redacted(self) -> None:
        proc = self.run_guard(
            "--check", "--json",
            "--html-file", str(FIXTURES / "usage-secret-hint.html"),
            "--cookie-stdin",
            input_bytes=SYNTH_COOKIE.encode(),
        )
        self.assertEqual(proc.returncode, usage.EXIT_BLOCKED)
        combined = proc.stdout + proc.stderr
        for token in (SYNTH_COOKIE_NAME, SYNTH_COOKIE_VALUE, SYNTH_AID):
            self.assertNotIn(token.encode(), combined)
        payload = json.loads(proc.stdout)
        self.assertIn("[redacted]", payload["reset_hint"])

    def test_cookie_never_in_any_output_channel(self) -> None:
        server = _ScriptedServer(
            [(200, (VISIBLE_FIXTURES / "usage-ok.html").read_bytes())],
            hold=False,
        )
        self.addCleanup(server.close)
        proc = self.run_guard(
            "--check", "--json",
            "--settings-url", f"http://127.0.0.1:{server.port}/",
            "--cookie-stdin",
            input_bytes=SYNTH_COOKIE.encode(),
        )
        self.assertEqual(proc.returncode, usage.EXIT_ALLOWED)
        combined = proc.stdout + proc.stderr
        for token in (SYNTH_COOKIE_NAME, SYNTH_COOKIE_VALUE, SYNTH_AID):
            self.assertNotIn(token.encode(), combined)


# ---------------------------------------------------------------------------
# Cleanup: no owned temp material, no survivor
# ---------------------------------------------------------------------------

class CleanupTests(_Base):
    def test_no_owned_temporary_material(self) -> None:
        before = set(os.listdir("/tmp"))
        server = _ScriptedServer(
            [
                (200, (VISIBLE_FIXTURES / "usage-blocked.html").read_bytes()),
                (200, (VISIBLE_FIXTURES / "usage-ok.html").read_bytes()),
            ],
            hold=False,
        )
        self.addCleanup(server.close)
        proc = self.run_guard(
            "--wait",
            "--settings-url", f"http://127.0.0.1:{server.port}/",
            "--cookie-stdin",
            "--poll-interval", "0",
            "--max-polls", "3",
            input_bytes=SYNTH_COOKIE.encode(),
        )
        self.assertEqual(proc.returncode, usage.EXIT_ALLOWED)
        after = set(os.listdir("/tmp"))
        new_entries = after - before
        for entry in new_entries:
            self.assertFalse(
                entry.startswith(("factory-usage-", "ollama-guard-")),
                f"guard-owned temporary material left behind: {entry}",
            )

    def test_zeroization_of_cookie_buffer(self) -> None:
        cookie = bytearray(SYNTH_COOKIE.encode())
        credentials = usage.Credentials(
            cookie=cookie,
            threshold=80.0,
            poll_interval=0,
            max_wait=0,
            max_polls=0,
            settings_url="http://127.0.0.1:1/",
        )
        credentials.zeroize()
        self.assertEqual(bytes(cookie), b"")

    def test_redaction_tokens_cover_pairs_names_values(self) -> None:
        tokens = usage.redaction_tokens(SYNTH_COOKIE.encode())
        self.assertIn(SYNTH_COOKIE_VALUE, tokens)
        self.assertIn(SYNTH_COOKIE_NAME, tokens)
        self.assertIn(SYNTH_AID, tokens)
        self.assertIn(f"{SYNTH_COOKIE_NAME}={SYNTH_COOKIE_VALUE}", tokens)


# ---------------------------------------------------------------------------
# Launch-authority integration (QUOTA-01; Task 6 authority without Task 8+)
# ---------------------------------------------------------------------------

GIT = None


def _work(args, cwd, check=True):
    global GIT
    if GIT is None:
        from gitutil import GIT_EXECUTABLE
        GIT = GIT_EXECUTABLE
    return subprocess.run(
        [GIT, "-C", str(cwd), *args], check=check,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )


class LaunchIntegrationTests(_Base):
    """The guard is part of the launch authority for ``ollama`` providers."""

    def setUp(self) -> None:
        super().setUp()
        self.workspace = self.tmp / "workspace"
        self.workspace.mkdir()
        scripts = self.workspace / "scripts"
        scripts.mkdir()
        shutil.copy2(ROOT / "scripts" / "pi2-secure-exec.py",
                     scripts / "pi2-secure-exec.py")
        backend = self.workspace / "backend.py"
        backend.write_text("#!/usr/bin/env python3\nprint('ok')\n", encoding="utf-8")
        os.chmod(backend, 0o700)
        plan = self.workspace / "plan.md"
        plan.write_bytes((FIXTURES / "plan-valid-base.md").read_bytes())
        spec = self.workspace / "spec.md"
        spec.write_text("SPEC\n", encoding="utf-8")
        role = self.workspace / "role.md"
        role.write_text("# ROLE\n", encoding="utf-8")
        policy = self.workspace / "AGENTS.md"
        policy.write_text("POLICY\n", encoding="utf-8")
        _work(["init", "-q"], self.workspace)
        _work(["config", "user.email", "factory@test"], self.workspace)
        _work(["config", "user.name", "factory"], self.workspace)
        _work(["add", "-A"], self.workspace)
        _work(["commit", "-qm", "fixture"], self.workspace)
        self.head = _work(["rev-parse", "HEAD"], self.workspace).stdout.decode().strip()
        self.assertEqual(len(self.head), 40)

    def _binding(self, provider: str) -> launch.InvocationBinding:
        def digest(data: bytes) -> str:
            return hashlib.sha256(data).hexdigest()

        role_bytes = (self.workspace / "role.md").read_bytes()
        policy_bytes = (self.workspace / "AGENTS.md").read_bytes()
        spec_bytes = (self.workspace / "spec.md").read_bytes()
        plan_bytes = (self.workspace / "plan.md").read_bytes()
        return launch.InvocationBinding(
            role="planner",
            model="synthetic-model",
            provider=provider,
            backend=self.workspace / "backend.py",
            workspace=self.workspace,
            bound_commit=self.head,
            role_prompt_digest=digest(role_bytes),
            prompt_set_digest=digest(b"campaign-set"),
            plan_digest=digest(plan_bytes),
            policy_digest=digest(policy_bytes),
            specification_digest=digest(spec_bytes),
            allowed_tools=("read", "bash"),
        )

    def _authorize(self, binding, **guard_kwargs) -> launch.LaunchAuthority:
        role_bytes = (self.workspace / "role.md").read_bytes()
        policy_bytes = (self.workspace / "AGENTS.md").read_bytes()
        spec_bytes = (self.workspace / "spec.md").read_bytes()
        plan_bytes = (self.workspace / "plan.md").read_bytes()
        return launch.authorize_launch(
            binding,
            role_prompt=role_bytes,
            agents=policy_bytes,
            spec=spec_bytes,
            plan=plan_bytes,
            **guard_kwargs,
        )

    def _proof(self, binding, **kwargs) -> "object":
        """The private synthetic Task 8 confinement proof (test-only seam)."""
        return confinement._mint_synthetic_proof(binding, **kwargs)

    def test_ollama_provider_ok_proceeds(self) -> None:
        """An ollama launch proceeds only behind a synthetic confinement proof.

        The private synthetic proof is required (the Task 8 production
        authority is unavailable), and the guard runs against the committed
        fixture through the hidden ``_usage_guard_html_file`` seam — the
        production API/CLI surface has no ``html-file`` option.
        """
        cookie = self.make_cookie_file()
        authority = self._authorize(
            self._binding("ollama"),
            _confinement_proof=self._proof(self._binding("ollama")),
            _usage_guard_html_file=str(VISIBLE_FIXTURES / "usage-ok.html"),
            usage_guard_cookie_file=str(cookie),
        )
        self.assertIsInstance(authority, launch.LaunchAuthority)

    def test_ollama_provider_blocked_fails_closed(self) -> None:
        cookie = self.make_cookie_file()
        with self.assertRaises(launch.InvocationError) as caught:
            self._authorize(
                self._binding("ollama"),
                _confinement_proof=self._proof(self._binding("ollama")),
                _usage_guard_html_file=str(VISIBLE_FIXTURES / "usage-blocked.html"),
                usage_guard_cookie_file=str(cookie),
                usage_guard_max_polls=1,
                usage_guard_poll_interval=0,
            )
        self.assertIn("ollama usage guard", str(caught.exception))

    def test_ollama_provider_fatal_fails_closed(self) -> None:
        cookie = self.make_cookie_file()
        with self.assertRaises(launch.InvocationError):
            self._authorize(
                self._binding("ollama"),
                _confinement_proof=self._proof(self._binding("ollama")),
                _usage_guard_html_file=str(VISIBLE_FIXTURES / "login.html"),
                usage_guard_cookie_file=str(cookie),
            )

    def test_non_ollama_provider_never_runs_the_guard(self) -> None:
        binding = self._binding("synthetic")
        authority = self._authorize(
            binding, _confinement_proof=self._proof(binding)
        )
        self.assertIsInstance(authority, launch.LaunchAuthority)

    def test_guard_cannot_be_bypassed_for_ollama(self) -> None:
        # No cookie store and a network settings URL: the guard must fail
        # closed (fatal missing-cookie, before any fetch) even though the
        # caller asked for a completely open invocation.  The ambient
        # environment is scrubbed so no real credential can ever satisfy
        # the guard inside this test.
        with _scrubbed_ollama_env():
            with self.assertRaises(launch.InvocationError):
                self._authorize(
                    self._binding("ollama"),
                    _confinement_proof=self._proof(self._binding("ollama")),
                    usage_guard_settings_url="http://127.0.0.1:1/",
                )

    def test_production_launch_rejects_loopback_settings_before_fetch(self) -> None:
        """A production launch rejects http:// loopback settings before any fetch.

        The ordinary launch path (the private loopback seam off) fails closed
        on a ``127.0.0.1``/``localhost`` ``http://`` settings URL in the launch
        authority *before* the guard runs, so an in-flight loopback server
        never observes a request (Task 7 review, obligation 14 residual).
        """
        server = _ScriptedServer([(200, b"unreachable")], hold=True)
        self.addCleanup(server.close)
        with _scrubbed_ollama_env():
            with self.assertRaises(launch.InvocationError) as caught:
                self._authorize(
                    self._binding("ollama"),
                    _confinement_proof=self._proof(self._binding("ollama")),
                    usage_guard_settings_url=f"http://127.0.0.1:{server.port}/",
                )
        self.assertIn("loopback", str(caught.exception))
        self.assertFalse(
            server.request_seen.is_set(),
            "a production launch fetched despite rejecting loopback settings",
        )

    def test_loopback_seam_without_proof_fails_closed(self) -> None:
        """The private loopback seam cannot be enabled without a proof.

        Setting ``_usage_guard_allow_loopback`` without a valid synthetic
        confinement proof fails closed before any transport/guard logic: the
        seam enables a transport the ordinary production launch rejects, so it
        is gated on a proof exactly like the Task 8 authority.
        """
        with self.assertRaises(launch.InvocationError) as caught:
            self._authorize(
                self._binding("ollama"),
                _usage_guard_allow_loopback=True,
                usage_guard_settings_url="http://127.0.0.1:1/",
            )
        self.assertIn("confinement proof", str(caught.exception))

    def test_loopback_seam_with_synthetic_proof_proceeds(self) -> None:
        """With a valid synthetic proof the hidden suite can use loopback.

        The private seam plus a validated synthetic confinement proof lets the
        hermetic suite exercise the loopback http transport end-to-end: the
        guard fetches the committed fixture page from a loopback server and
        the authority is minted.
        """
        server = _ScriptedServer(
            [(200, (VISIBLE_FIXTURES / "usage-ok.html").read_bytes())],
            hold=False,
        )
        self.addCleanup(server.close)
        authority = self._authorize(
            self._binding("ollama"),
            _confinement_proof=self._proof(self._binding("ollama")),
            _usage_guard_allow_loopback=True,
            usage_guard_settings_url=f"http://127.0.0.1:{server.port}/",
            usage_guard_cookie_file=str(self.make_cookie_file()),
        )
        self.assertIsInstance(authority, launch.LaunchAuthority)


# ---------------------------------------------------------------------------
# Strict known-provider registry and per-policy gating (obligation 5)
# ---------------------------------------------------------------------------

class ProviderRegistryTests(_Base):
    def test_unknown_provider_fails_closed(self) -> None:
        """An unknown/caller-claimed provider can never bypass the guard."""
        for provider in ("openai", "anthropic", "unknown", ""):
            with self.subTest(provider=provider):
                binding = launch.InvocationBinding(
                    role="planner",
                    model="m",
                    provider=provider,
                    backend=TRUSTED_EXECUTABLE,
                    workspace=self.tmp,
                    bound_commit="0" * 40,
                    role_prompt_digest=hashlib.sha256(b"r").hexdigest(),
                    prompt_set_digest=hashlib.sha256(b"s").hexdigest(),
                    plan_digest=hashlib.sha256(b"p").hexdigest(),
                    policy_digest=hashlib.sha256(b"a").hexdigest(),
                    specification_digest=hashlib.sha256(b"sp").hexdigest(),
                )
                with self.assertRaises(launch.InvocationError):
                    launch.verify_invocation(binding)

    def test_known_providers_accepted_case_insensitively(self) -> None:
        for provider in ("ollama", "OLLAMA", "synthetic"):
            binding = launch.InvocationBinding(
                role="planner",
                model="m",
                provider=provider,
                backend=TRUSTED_EXECUTABLE,
                workspace=self.tmp,
                bound_commit="0" * 40,
                role_prompt_digest=hashlib.sha256(b"r").hexdigest(),
                prompt_set_digest=hashlib.sha256(b"s").hexdigest(),
                plan_digest=hashlib.sha256(b"p").hexdigest(),
                policy_digest=hashlib.sha256(b"a").hexdigest(),
                specification_digest=hashlib.sha256(b"sp").hexdigest(),
            )
            self.assertIsNone(launch.verify_invocation(binding))

    def test_ollama_is_the_only_guard_required_provider(self) -> None:
        self.assertEqual(launch.PROVIDER_GUARD_REQUIRED, frozenset({"ollama"}))
        self.assertIn("ollama", launch.SUPPORTED_PROVIDERS)
        self.assertIn("synthetic", launch.SUPPORTED_PROVIDERS)


# ---------------------------------------------------------------------------
# Task 8 confinement proof seam (obligations 2, 3, and store location)
# ---------------------------------------------------------------------------

class ConfinementProofTests(_Base):
    def _binding(self, provider: str = "ollama") -> launch.InvocationBinding:
        return launch.InvocationBinding(
            role="planner",
            model="m",
            provider=provider,
            backend=TRUSTED_EXECUTABLE,
            workspace=self.tmp,
            bound_commit="0" * 40,
            role_prompt_digest=hashlib.sha256(b"r").hexdigest(),
            prompt_set_digest=hashlib.sha256(b"s").hexdigest(),
            plan_digest=hashlib.sha256(b"p").hexdigest(),
            policy_digest=hashlib.sha256(b"a").hexdigest(),
            specification_digest=hashlib.sha256(b"sp").hexdigest(),
        )

    def test_production_prove_confinement_unavailable(self) -> None:
        """The Task 8 production authority is absent: every call fails closed."""
        with self.assertRaises(confinement.ConfinementUnavailable):
            confinement.prove_confinement(self._binding())

    def test_ollama_launch_without_proof_fails_closed(self) -> None:
        """No production Ollama launch proceeds without a confinement proof."""
        workspace = self.tmp / "workspace"
        workspace.mkdir()
        scripts = workspace / "scripts"
        scripts.mkdir()
        shutil.copy2(ROOT / "scripts" / "pi2-secure-exec.py",
                     scripts / "pi2-secure-exec.py")
        backend = workspace / "backend.py"
        backend.write_text("#!/usr/bin/env python3\nprint('ok')\n", encoding="utf-8")
        os.chmod(backend, 0o700)
        (workspace / "plan.md").write_bytes(
            (FIXTURES / "plan-valid-base.md").read_bytes()
        )
        (workspace / "spec.md").write_text("SPEC\n", encoding="utf-8")
        (workspace / "role.md").write_text("# ROLE\n", encoding="utf-8")
        (workspace / "AGENTS.md").write_text("POLICY\n", encoding="utf-8")
        _work(["init", "-q"], workspace)
        _work(["config", "user.email", "factory@test"], workspace)
        _work(["config", "user.name", "factory"], workspace)
        _work(["add", "-A"], workspace)
        _work(["commit", "-qm", "fixture"], workspace)
        head = _work(["rev-parse", "HEAD"], workspace).stdout.decode().strip()

        def digest(data: bytes) -> str:
            return hashlib.sha256(data).hexdigest()

        binding = launch.InvocationBinding(
            role="planner",
            model="m",
            provider="ollama",
            backend=backend,
            workspace=workspace,
            bound_commit=head,
            role_prompt_digest=digest((workspace / "role.md").read_bytes()),
            prompt_set_digest=digest(b"set"),
            plan_digest=digest((workspace / "plan.md").read_bytes()),
            policy_digest=digest((workspace / "AGENTS.md").read_bytes()),
            specification_digest=digest((workspace / "spec.md").read_bytes()),
        )
        with _scrubbed_ollama_env():
            with self.assertRaises(launch.InvocationError) as caught:
                launch.authorize_launch(
                    binding,
                    role_prompt=(workspace / "role.md").read_bytes(),
                    agents=(workspace / "AGENTS.md").read_bytes(),
                    spec=(workspace / "spec.md").read_bytes(),
                    plan=(workspace / "plan.md").read_bytes(),
                )
        self.assertIn("confinement", str(caught.exception).lower())
        self.assertIn("Task 8", str(caught.exception))

    def test_synthetic_proof_binds_exact_invocation(self) -> None:
        binding = self._binding()
        proof = confinement._mint_synthetic_proof(binding)
        confinement.validate_proof(proof, binding)
        self.assertEqual(proof.bound_commit, binding.bound_commit)
        self.assertEqual(proof.provider, binding.provider)
        self.assertEqual(proof.credential_stores, (usage._default_env_file(),))

    def test_synthetic_proof_wrong_commit_rejected(self) -> None:
        proof = confinement._mint_synthetic_proof(self._binding())
        other = self._binding()
        other = launch.InvocationBinding(
            role=other.role, model=other.model, provider=other.provider,
            backend=other.backend, workspace=other.workspace,
            bound_commit="1" * 40, role_prompt_digest=other.role_prompt_digest,
            prompt_set_digest=other.prompt_set_digest, plan_digest=other.plan_digest,
            policy_digest=other.policy_digest,
            specification_digest=other.specification_digest,
        )
        with self.assertRaises(confinement.ConfinementError):
            confinement.validate_proof(proof, other)

    def test_synthetic_proof_wrong_workspace_rejected(self) -> None:
        proof = confinement._mint_synthetic_proof(self._binding())
        other = self._binding()
        other = launch.InvocationBinding(
            role=other.role, model=other.model, provider=other.provider,
            backend=other.backend, workspace=self.tmp / "elsewhere",
            bound_commit=other.bound_commit,
            role_prompt_digest=other.role_prompt_digest,
            prompt_set_digest=other.prompt_set_digest, plan_digest=other.plan_digest,
            policy_digest=other.policy_digest,
            specification_digest=other.specification_digest,
        )
        with self.assertRaises(confinement.ConfinementError):
            confinement.validate_proof(proof, other)

    def test_synthetic_proof_wrong_provider_rejected(self) -> None:
        proof = confinement._mint_synthetic_proof(self._binding())
        other = self._binding(provider="synthetic")
        with self.assertRaises(confinement.ConfinementError):
            confinement.validate_proof(proof, other)

    def test_synthetic_proof_wrong_guard_source_digest_rejected(self) -> None:
        """A caller-supplied guard-source digest never matches the executing bytes."""
        proof = confinement._mint_synthetic_proof(
            self._binding(),
            guard_source_digests=["0" * 64, "1" * 64],
        )
        with self.assertRaises(confinement.ConfinementError):
            confinement.validate_proof(proof, self._binding())

    def test_proof_cannot_be_forged_from_operator_claims(self) -> None:
        with self.assertRaises(confinement.ConfinementError):
            confinement.ConfinementProof(
                bound_commit="0" * 40,
                workspace=str(self.tmp),
                provider="ollama",
                guard_source_digests=("0" * 64, "1" * 64),
                credential_stores=(str(self.tmp / "store"),),
                _mint=object(),
            )

    def test_synthetic_proof_rejects_in_workspace_store(self) -> None:
        inside = self.tmp / "workspace" / ".ollama-usage-env"
        with self.assertRaises(confinement.ConfinementError):
            confinement._mint_synthetic_proof(
                self._binding(), credential_stores=[str(inside)]
            )

    def test_synthetic_proof_is_never_evidence(self) -> None:
        """The capability evidence checker must never accept a synthetic proof."""
        proof = confinement._mint_synthetic_proof(self._binding())
        # The private mint marker is not the production authority's marker and
        # the proof object carries no capability-claim surface: it is a
        # skeleton token whose only producer is the hidden suite.
        self.assertFalse(hasattr(proof, "evidence"))
        self.assertTrue(proof._mint is not None)


# ---------------------------------------------------------------------------
# Default external operator store (obligation 1)
# ---------------------------------------------------------------------------

class ExternalStoreTests(_Base):
    def test_default_env_file_outside_workspace(self) -> None:
        workspace = self.tmp / "workspace"
        workspace.mkdir()
        with mock.patch.dict(
            os.environ,
            {"XDG_CONFIG_HOME": str(self.tmp / "config")},
            clear=False,
        ):
            default = usage._default_env_file()
        self.assertFalse(Path(default).is_relative_to(workspace))
        self.assertEqual(
            Path(default), self.tmp / "config" / "unattended-ralph" / "ollama-usage-env"
        )
        # An explicit operator override is honored verbatim.
        with mock.patch.dict(
            os.environ,
            {"OLLAMA_USAGE_ENV_FILE": str(self.tmp / "external-store")},
            clear=False,
        ):
            self.assertEqual(
                usage._default_env_file(), str(self.tmp / "external-store")
            )

    def test_default_env_file_never_in_repo_or_workspace(self) -> None:
        """The canonical default is the operator config home, never a repo path."""
        workspace = self.tmp / "workspace"
        workspace.mkdir()
        with mock.patch.dict(os.environ, {}, clear=False):
            for key in [k for k in os.environ if k in ("XDG_CONFIG_HOME", "HOME")]:
                del os.environ[key]
            os.environ["HOME"] = str(self.tmp / "home")
            default = usage._default_env_file()
        self.assertNotIn("/workspace", default)
        self.assertFalse(Path(default).is_relative_to(workspace))
        self.assertTrue(str(default).startswith(str(self.tmp)))

    def test_store_inside_workspace_fails_closed(self) -> None:
        workspace = self.tmp / "workspace"
        workspace.mkdir()
        inside = workspace / ".ollama-usage-env"
        with self.assertRaises(usage.UsageConfigError):
            usage.assert_store_outside_workspace(str(inside), workspace)
        root = self.tmp / "repo"
        root.mkdir()
        with self.assertRaises(usage.UsageConfigError):
            usage.assert_store_outside_workspace(str(root / ".ollama-usage-env"), root)

    def test_store_outside_workspace_accepted(self) -> None:
        workspace = self.tmp / "workspace"
        workspace.mkdir()
        outside = self.tmp / "external" / "ollama-usage-env"
        usage.assert_store_outside_workspace(str(outside), workspace)

    def test_env_store_symlink_rejected(self) -> None:
        target = self.tmp / "cookie.txt"
        target.write_text(
            "export OLLAMA_COOKIE='%s'\n" % SYNTH_COOKIE, encoding="utf-8"
        )
        os.chmod(target, 0o600)
        link = self.tmp / "store-link"
        os.symlink(target, link)
        proc = self.run_guard(
            "--check", "--html-file",
            str(VISIBLE_FIXTURES / "usage-ok.html"),
            "--env-file", str(link),
        )
        self.assertEqual(proc.returncode, usage.EXIT_FATAL)


# ---------------------------------------------------------------------------
# HTTPS-only with the explicit loopback seam (obligation 9)
# ---------------------------------------------------------------------------

class HttpsLoopbackTests(_Base):
    def test_https_settings_url_accepted(self) -> None:
        usage._validate_settings_url("https://ollama.com/settings")

    def test_http_nonloopback_rejected(self) -> None:
        with self.assertRaises(usage.UsageConfigError):
            usage._validate_settings_url("http://example.com/settings")
        with self.assertRaises(usage.UsageConfigError):
            usage._validate_settings_url("http://10.0.0.1/settings")
        with self.assertRaises(usage.UsageConfigError):
            usage._validate_settings_url("ftp://ollama.com/settings")

    def test_http_loopback_seam_accepted(self) -> None:
        usage._validate_settings_url("http://127.0.0.1:8080/")
        usage._validate_settings_url("http://localhost/settings")

    def test_cli_rejects_http_nonloopback_url(self) -> None:
        proc = self.run_guard(
            "--check",
            "--settings-url", "http://example.com/settings",
            "--cookie-stdin",
            input_bytes=SYNTH_COOKIE.encode(),
        )
        self.assertEqual(proc.returncode, usage.EXIT_FATAL)
        self.assertIn(b"not HTTPS", proc.stderr)

    def test_fetch_child_rejects_http_nonloopback(self) -> None:
        child = LOOP / "usage_fetch.py"
        proc = subprocess.run(
            [PY, str(child), "--settings-url", "http://example.com/",
             "--cookie-max", "65536"],
            input=SYNTH_COOKIE.encode(),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=str(ROOT),
        )
        self.assertEqual(proc.returncode, usage.EXIT_FATAL)


# ---------------------------------------------------------------------------
# Zero-poll unbounded busy-loop rejection (obligation 10)
# ---------------------------------------------------------------------------

class ZeroPollTests(_Base):
    def test_zero_poll_without_bound_rejected(self) -> None:
        with self.assertRaises(usage.UsageConfigError):
            usage.acquire_credentials(
                poll_interval=0, max_wait=0, max_polls=0,
                html_file=str(VISIBLE_FIXTURES / "usage-ok.html"),
            )

    def test_zero_poll_with_finite_bound_accepted(self) -> None:
        credentials = usage.acquire_credentials(
            poll_interval=0, max_wait=0, max_polls=1,
            html_file=str(VISIBLE_FIXTURES / "usage-ok.html"),
        )
        self.assertEqual(credentials.poll_interval, 0)
        self.assertEqual(credentials.max_polls, 1)

    def test_cli_zero_poll_without_bound_rejected(self) -> None:
        proc = self.run_guard(
            "--wait", "--html-file",
            str(VISIBLE_FIXTURES / "usage-ok.html"),
            "--poll-interval", "0",
            "--max-wait", "0",
            "--max-polls", "0",
        )
        self.assertEqual(proc.returncode, usage.EXIT_FATAL)
        self.assertIn(b"zero poll interval", proc.stderr)

    def test_cli_zero_poll_with_max_polls_ok(self) -> None:
        proc = self.run_guard(
            "--wait", "--html-file",
            str(VISIBLE_FIXTURES / "usage-blocked.html"),
            "--poll-interval", "0",
            "--max-polls", "1",
        )
        self.assertEqual(proc.returncode, usage.EXIT_BLOCKED)
        self.assertIn(b"maximum poll count reached", proc.stderr)


# ---------------------------------------------------------------------------
# CRLF / embedded-control legacy cookie rejection (obligation 11)
# ---------------------------------------------------------------------------

class CrlfCookieTests(_Base):
    def test_cookie_with_crlf_rejected(self) -> None:
        with self.assertRaises(usage.UsageConfigError):
            usage._validate_cookie(
                b"name=value\r\nSet-Cookie: evil=1; path=/"
            )
        with self.assertRaises(usage.UsageConfigError):
            usage._validate_cookie(b"name=value\nX-Inject: 1")

    def test_cookie_with_control_byte_rejected(self) -> None:
        with self.assertRaises(usage.UsageConfigError):
            usage._validate_cookie(b"name=value\x07")
        with self.assertRaises(usage.UsageConfigError):
            usage._validate_cookie(b"name=\x00value")

    def test_cli_cookie_stdin_crlf_rejected(self) -> None:
        proc = self.run_guard(
            "--check", "--html-file",
            str(VISIBLE_FIXTURES / "usage-ok.html"),
            "--cookie-stdin",
            input_bytes=b"name=value\r\nSet-Cookie: evil=1",
        )
        self.assertEqual(proc.returncode, usage.EXIT_FATAL)
        self.assertIn(b"control character", proc.stderr)

    def test_cookie_file_crlf_rejected(self) -> None:
        path = self.tmp / "crlf-cookie"
        path.write_bytes(b"name=value\r\nSet-Cookie: evil=1")
        os.chmod(path, 0o600)
        proc = self.run_guard(
            "--check", "--html-file",
            str(VISIBLE_FIXTURES / "usage-ok.html"),
            "--cookie-file", str(path),
        )
        self.assertEqual(proc.returncode, usage.EXIT_FATAL)

    def test_fetch_child_never_sends_crlf_cookie(self) -> None:
        server = _ScriptedServer(
            [(200, (VISIBLE_FIXTURES / "usage-ok.html").read_bytes())]
        )
        self.addCleanup(server.close)
        proc = self.run_guard(
            "--check",
            "--settings-url", f"http://127.0.0.1:{server.port}/",
            "--cookie-stdin",
            input_bytes=b"name=value\r\nX-Inject: 1",
        )
        self.assertEqual(proc.returncode, usage.EXIT_FATAL)
        self.assertFalse(
            server.request_seen.is_set(),
            "a CRLF cookie must never reach an HTTP request",
        )


# ---------------------------------------------------------------------------
# Bounded same-origin redirects; 401/403/3xx fatal (obligation 6)
# ---------------------------------------------------------------------------

class RedirectTests(_Base):
    def _check(self, server: _ScriptedServer) -> subprocess.CompletedProcess[bytes]:
        return self.run_guard(
            "--check",
            "--settings-url", f"http://127.0.0.1:{server.port}/",
            "--cookie-stdin",
            input_bytes=SYNTH_COOKIE.encode(),
        )

    def test_same_origin_redirect_chain_succeeds(self) -> None:
        ok = (VISIBLE_FIXTURES / "usage-ok.html").read_bytes()
        server = _ScriptedServer(
            [
                (302, b"", {"Location": "/hop1"}),
                (301, b"", {"Location": "/hop2"}),
                (307, b"", {"Location": "/hop3"}),
                (308, b"", {"Location": "/hop4"}),
                (303, b"", {"Location": "/final"}),
                (200, ok),
            ]
        )
        self.addCleanup(server.close)
        proc = self._check(server)
        self.assertEqual(proc.returncode, usage.EXIT_ALLOWED)

    def test_cross_origin_redirect_fatal(self) -> None:
        server = _ScriptedServer(
            [(302, b"", {"Location": "http://localhost:1/"})]
        )
        self.addCleanup(server.close)
        proc = self._check(server)
        self.assertEqual(proc.returncode, usage.EXIT_FATAL)

    def test_cross_scheme_redirect_fatal(self) -> None:
        server = _ScriptedServer(
            [(302, b"", {"Location": f"https://127.0.0.1:{1}/"})]
        )
        self.addCleanup(server.close)
        proc = self._check(server)
        self.assertEqual(proc.returncode, usage.EXIT_FATAL)

    def test_redirect_without_location_fatal(self) -> None:
        server = _ScriptedServer([(302, b"")])
        self.addCleanup(server.close)
        proc = self._check(server)
        self.assertEqual(proc.returncode, usage.EXIT_FATAL)

    def test_redirect_hop_exhaustion_fatal(self) -> None:
        """More than ``MAX_REDIRECT_HOPS`` same-origin hops is fatal."""
        server = _ScriptedServer(
            [(302, b"", {"Location": "/loop"})]
            * (usage_fetch.MAX_REDIRECT_HOPS + 2)
        )
        self.addCleanup(server.close)
        proc = self._check(server)
        self.assertEqual(proc.returncode, usage.EXIT_FATAL)

    def test_401_fatal_not_transient(self) -> None:
        server = _ScriptedServer([(401, b"unauthorized")])
        self.addCleanup(server.close)
        proc = self._check(server)
        self.assertEqual(proc.returncode, usage.EXIT_FATAL)

    def test_403_fatal_not_transient(self) -> None:
        server = _ScriptedServer([(403, b"forbidden")])
        self.addCleanup(server.close)
        proc = self._check(server)
        self.assertEqual(proc.returncode, usage.EXIT_FATAL)

    def test_500_transient_retryable(self) -> None:
        server = _ScriptedServer([(500, b"down")])
        self.addCleanup(server.close)
        proc = self._check(server)
        self.assertEqual(proc.returncode, usage.EXIT_TRANSIENT)

    def test_final_3xx_without_follow_fatal(self) -> None:
        """A 3xx that is not a followable redirect is an unresolvable redirect."""
        server = _ScriptedServer([(304, b"")])
        self.addCleanup(server.close)
        proc = self._check(server)
        self.assertEqual(proc.returncode, usage.EXIT_FATAL)


# ---------------------------------------------------------------------------
# Retained case-insensitive / multiline parse path (obligation 7)
# ---------------------------------------------------------------------------

class CaseMultilineParseTests(_Base):
    def _classify(self, page: str) -> tuple:
        return usage.classify_html(page)

    def test_case_insensitive_aria_labels(self) -> None:
        page = (
            '<!doctype html><title>USAGE · SETTINGS</title>\n'
            '<div aria-label="session usage 12.5% used"></div>\n'
            '<div aria-label="WEEKLY USAGE 44.0% used"></div>\n'
            "<p>Session resets in 2 hours</p>\n"
        )
        self.assertEqual(self._classify(page)[0:3], ("usage", "12.5", "44.0"))

    def test_multiline_html_still_parses(self) -> None:
        """Attributes split across lines parse with the retained ``re.I | re.S``."""
        page = (
            "<!doctype html>\n"
            "<html>\n<head><title>Usage · Settings</title></head>\n"
            "<body>\n"
            '  <div\n    aria-label="Session usage 12.5%\n used"></div>\n'
            '  <div aria-label="Weekly\n usage 44.0% used"></div>\n'
            "  <p>Session\n resets in 2 hours</p>\n"
            "</body></html>\n"
        )
        self.assertEqual(self._classify(page)[0:3], ("usage", "12.5", "44.0"))

    def test_auth_heuristic_ignores_usage_settings_title(self) -> None:
        """A login page that still carries the settings title is not auth."""
        page = '<!doctype html><title>Usage · Settings</title><h1>Sign in</h1>\n'
        self.assertEqual(self._classify(page)[0], "parse")

    def test_malformed_fixture_reaches_parse_path_not_multiline_capture(self) -> None:
        """A NaN field is never rescued by the sibling element's numeric value."""
        raw = (FIXTURES / "usage-malformed.html").read_text(encoding="utf-8")
        self.assertEqual(self._classify(raw)[0], "parse")
        self.assertEqual(self._classify(raw)[1], "")
        self.assertEqual(self._classify(raw)[2], "")


# ---------------------------------------------------------------------------
# Ambient OLLAMA_COOKIE is scrubbed, never a crash (obligation 8)
# ---------------------------------------------------------------------------

class AmbientScrubTests(_Base):
    def test_ambient_cookie_scrubbed_from_fetch_env(self) -> None:
        env = usage.fetch_child_env(
            {**dict(os.environ), "OLLAMA_COOKIE": SYNTH_COOKIE}
        )
        for key in env:
            self.assertNotIn("OLLAMA_", key.upper())
            self.assertNotIn("COOKIE", key.upper())

    def test_ambient_cookie_is_not_a_source(self) -> None:
        """Ambient OLLAMA_COOKIE alone cannot satisfy a network check."""
        server = _ScriptedServer([(200, b"unreachable")], hold=True)
        self.addCleanup(server.close)
        proc = self.run_guard(
            "--check",
            "--settings-url", f"http://127.0.0.1:{server.port}/",
            env={"OLLAMA_COOKIE": SYNTH_COOKIE},
        )
        self.assertEqual(proc.returncode, usage.EXIT_FATAL)
        self.assertFalse(
            server.request_seen.is_set(),
            "ambient OLLAMA_COOKIE must never drive a network fetch",
        )

    def test_private_cookie_file_wins_over_ambient(self) -> None:
        """Ambient credentials do not disturb a private cookie-file run."""
        cookie = self.make_cookie_file()
        proc = self.run_guard(
            "--check", "--json",
            "--html-file", str(VISIBLE_FIXTURES / "usage-ok.html"),
            "--cookie-file", str(cookie),
            env={"OLLAMA_COOKIE": SYNTH_COOKIE, "MY_API_KEY": "x"},
        )
        self.assertEqual(proc.returncode, usage.EXIT_ALLOWED)
        combined = proc.stdout + proc.stderr
        self.assertNotIn(SYNTH_COOKIE_VALUE.encode(), combined)

    def test_require_quota_scrubs_ambient_without_error(self) -> None:
        """The launch hook continues (fails closed only on a missing private cookie)."""
        with mock.patch.dict(
            os.environ, {"OLLAMA_COOKIE": SYNTH_COOKIE}, clear=False
        ):
            usage.require_quota(
                html_file=str(VISIBLE_FIXTURES / "usage-ok.html"),
                cookie_file=str(self.make_cookie_file()),
            )


# ---------------------------------------------------------------------------
# Signals during the initial and final checks (obligation 12)
# ---------------------------------------------------------------------------

class SignalRequireQuotaTests(_Base):
    def _run_require_quota(self, url: str, cookie_file: str) -> subprocess.Popen[bytes]:
        script = (
            "import sys; sys.path.insert(0, %r)\n"
            "import usage\n"
            "try:\n"
            "    usage.require_quota(settings_url=sys.argv[1], "
            "cookie_file=sys.argv[2], poll_interval=0, max_polls=10)\n"
            "except usage.WaitInterrupted as exc:\n"
            "    sys.exit(128 + exc.signum)\n"
        ) % str(LOOP)
        return subprocess.Popen(
            [PY, "-c", script, url, cookie_file],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=str(ROOT),
        )

    def _assert_signal_abort(self, proc, signum: int, server) -> None:
        self.assertTrue(server.request_seen.wait(20), "fetch never started")
        proc.send_signal(signum)
        out, err = proc.communicate(timeout=20)
        self.assertEqual(proc.returncode, 128 + signum, err.decode())
        self.assertNotIn(b"Traceback", err)
        self.assertNoLiveFetchChildren()
        server.release()

    def test_sigint_during_initial_check_reaps_and_aborts(self) -> None:
        server = _ScriptedServer([(200, b"x")], hold=True)
        self.addCleanup(server.close)
        proc = self._run_require_quota(
            f"http://127.0.0.1:{server.port}/", str(self.make_cookie_file())
        )
        self._assert_signal_abort(proc, signal.SIGINT, server)

    def test_sigterm_during_final_check_reaps_and_aborts(self) -> None:
        """A signal while the *final* --check (after --wait) is in flight aborts."""
        ok = (VISIBLE_FIXTURES / "usage-ok.html").read_bytes()
        blocked = (VISIBLE_FIXTURES / "usage-blocked.html").read_bytes()
        server = _ScriptedServer(
            [(200, blocked), (200, ok), (200, b"x")], hold=3
        )
        self.addCleanup(server.close)
        proc = self._run_require_quota(
            f"http://127.0.0.1:{server.port}/", str(self.make_cookie_file())
        )
        self._assert_signal_abort(proc, signal.SIGTERM, server)

    def test_sighup_during_wait_sleep_aborts_cleanly(self) -> None:
        proc = subprocess.Popen(
            [
                PY, str(usage_path()),
                "--wait", "--html-file",
                str(VISIBLE_FIXTURES / "usage-blocked.html"),
                "--poll-interval", "30",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=str(ROOT),
        )
        try:
            time.sleep(1.0)
            proc.send_signal(signal.SIGHUP)
            out, err = proc.communicate(timeout=15)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.communicate()
        self.assertEqual(proc.returncode, 128 + signal.SIGHUP)
        self.assertNotIn(b"Traceback", err)
        self.assertNoLiveFetchChildren()


# ---------------------------------------------------------------------------
# Catch-all: every exception is a documented exit with no traceback
# ---------------------------------------------------------------------------

class CatchAllNoTracebackTests(_Base):
    def test_internal_exception_in_check_maps_to_fatal(self) -> None:
        out = io.StringIO()
        err = io.StringIO()
        with mock.patch.object(
            usage, "check_once", side_effect=RuntimeError("synthetic boom")
        ), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            status = usage.main(["--check", "--html-file", "unused"])
        self.assertEqual(status, usage.EXIT_FATAL)
        self.assertIn("internal failure", err.getvalue())
        self.assertNotIn("Traceback", err.getvalue())

    def test_internal_exception_in_wait_maps_to_fatal(self) -> None:
        out = io.StringIO()
        err = io.StringIO()
        with mock.patch.object(
            usage, "wait", side_effect=RuntimeError("synthetic wait boom")
        ), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            status = usage.main(["--wait", "--html-file", "unused"])
        self.assertEqual(status, usage.EXIT_FATAL)
        self.assertNotIn("Traceback", err.getvalue())


# ---------------------------------------------------------------------------
# html-file is absent from the production launch API/CLI (obligation 4)
# ---------------------------------------------------------------------------

class ProductionSurfaceTests(_Base):
    def test_authorize_launch_public_surface_has_no_html_file(self) -> None:
        parameters = inspect.signature(launch.authorize_launch).parameters
        self.assertNotIn("usage_guard_html_file", parameters)
        # The hidden suite reaches the fixture path only through the private
        # seam, exactly like the private confinement-proof seam.
        self.assertIn("_usage_guard_html_file", parameters)

    def test_launch_cli_help_has_no_html_file(self) -> None:
        out = io.StringIO()
        err = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            with self.assertRaises(SystemExit) as caught:
                launch.main(["launch", "--help"])
        self.assertEqual(caught.exception.code, 0)
        help_text = out.getvalue()
        self.assertNotIn("--usage-guard-html-file", help_text)
        self.assertIn("--usage-guard-cookie-file", help_text)

    def test_guard_cli_html_file_is_diagnostics_only(self) -> None:
        """The *guard* CLI keeps its diagnostics fixture, the launch surface does not."""
        proc = self.run_guard(
            "--check", "--html-file", str(VISIBLE_FIXTURES / "usage-ok.html")
        )
        self.assertEqual(proc.returncode, usage.EXIT_ALLOWED)

    def test_launch_cli_has_no_loopback_optin(self) -> None:
        """The production launch CLI/argparse exposes no loopback opt-in.

        ``_usage_guard_allow_loopback`` is a private authority seam absent
        from the CLI (Task 7 review, obligations 9 and 14): the help text must
        not advertise it, and argparse must refuse the flag outright on an
        otherwise-valid command line rather than silently accepting it.
        """
        out = io.StringIO()
        err = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            with self.assertRaises(SystemExit) as caught:
                launch.main(["launch", "--help"])
        self.assertEqual(caught.exception.code, 0)
        help_text = out.getvalue()
        self.assertNotIn("--usage-guard-allow-loopback", help_text)
        self.assertNotIn("loopback", help_text)
        self.assertNotIn("_usage_guard_allow_loopback", help_text)
        # argparse itself rejects the flag on a fully valid launch command
        # (usage error, exit 2) — never accepted as a recognized option.
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            with self.assertRaises(SystemExit) as caught:
                launch.main([
                    "launch", "--root", "/tmp/x", "--role", "planner",
                    "--model", "m", "--provider", "ollama",
                    "--backend", "/bin/true", "--role-prompt", "/tmp/r",
                    "--prompt-set-digest", "a", "--policy", "/tmp/p",
                    "--spec", "/tmp/s", "--plan", "/tmp/pl",
                    "--bound-commit", "0" * 40, "--allowed-tools", "read",
                    "--usage-guard-allow-loopback",
                ])
        self.assertEqual(caught.exception.code, 2)
        self.assertIn("usage-guard-allow-loopback", err.getvalue())
        self.assertIn("unrecognized arguments", err.getvalue())


if __name__ == "__main__":
    unittest.main(verbosity=2)
