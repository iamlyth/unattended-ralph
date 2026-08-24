#!/usr/bin/env python3
"""Harness-owned adversarial tests for the root-descriptor lock and Git writer
boundary (LOCK-01, GIT-01, PROC-01).

This test lives under the hidden ``.factory/tests/`` namespace because the
specification (HIDE-01, §3) keeps harness-only tests out of the adopting
product's visible test tree.  It is the deterministic verification for
Task 5, reconciled against the security-review findings F1-F5 and F8-F10:

* the lock is an exclusive ``flock`` on the *already-open canonical root
  directory descriptor* (``O_DIRECTORY|O_NOFOLLOW|O_CLOEXEC``), there is no
  replaceable lock-file pathname, and concurrent launcher probes prove
  exactly one writer;
* F2: identity/branch/spec/plan bindings are *mandatory* at acquisition and
  validated while the lock is held; every Git read is bound to the locked
  descriptor's inode (reads run with ``-C /proc/self/fd/<anchor>``), so a
  canonical-path rebind cannot redirect a single read, and pathname drift
  fails closed before and after every read;
* F1: the full descendant closure is snapshotted from the
  ``/proc/<pid>/stat`` parent table with a *bounded* maximum (an over-bound
  closure fails closed instead of silently truncating), and
  :func:`live_scope` re-enumerates only the still-live members of a
  captured scope (the detector-scope surface Task 6 supervision consumes);
* F3: a bounded child timeout terminates and reaps the child's *entire* new
  process group (TERM, full bounded grace, unconditional KILL, bounded
  group-gone verification, reaped leader, bounded pipe collection/close) and
  surfaces as :class:`RootLockTimeoutError` — including the adversarial
  case where the leader exits on TERM while a TERM-ignoring grandchild that
  holds the pipe ends survives;
* F4: Git selection never consults the caller ``PATH`` — the pinned
  executable is a fixed absolute candidate (FHS or immutable root-owned Nix
  store, validated by pattern + ownership + writability), and a caller-owned
  or writable candidate fails closed;
* F5: the *complete* ``GIT_CONFIG*`` family (including
  ``GIT_CONFIG_PARAMETERS``/``GIT_CONFIG_COUNT``/``GIT_CONFIG_KEY_*``/
  ``GIT_CONFIG_VALUE_*``) is stripped from every trusted Git invocation;
* F8: ``pass_fds`` aliases of the root lock OFD/inode (the lock descriptor
  itself, a ``dup``, or a separately opened root descriptor) are rejected
  before exec;
* F9: every lock/authority failure routes through the unified
  :class:`RootLockError` exception contract — including the wrapped
  pinned-Git import resolution, a nonzero child exit under ``check=True``
  (:class:`RootLockCommandError`), and the ``flock`` ``OSError`` wrap;
* F10: the child environment has every ``FACTORY_LOOP_LOCK_*`` key and every
  legacy ``FACTORY_LOCK_*`` key stripped by prefix;
* the captured descendant scope records each PID's starttime and parent PID
  (:class:`CapturedProcess`) so :func:`live_scope` excludes a PID reused by
  an unrelated process and never returns it as a live descendant;
* a double-fork/``setsid`` descendant that survives bounded termination is
  detected and blocks recovery, and an escaped child that keeps the lock's
  open file description alive blocks reacquisition until it is killed.
"""

from __future__ import annotations

import ctypes
import fcntl
import hashlib
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
import unittest.mock

ROOT = Path(__file__).resolve().parents[2]
LOOP = ROOT / ".factory" / "loop"

sys.path.insert(0, str(LOOP))
import gitutil  # noqa: E402
import lock as lock_module  # noqa: E402
import state as state_module  # noqa: E402
from lock import (  # noqa: E402
    ENV_KEYS,
    ENV_PREFIX,
    LEGACY_ENV_PREFIX,
    CapturedProcess,
    EscapedDescendantError,
    PlanBinding,
    RootLock,
    RootLockBindingError,
    RootLockCommandError,
    RootLockError,
    RootLockHeldError,
    RootLockTimeoutError,
    RootLockUnsafeError,
    SpecBinding,
    acquire_root_lock,
    assert_no_escaped_descendants,
    capture_descendants,
    detect_escaped_descendants,
    live_scope,
    probe_root_lock,
    stripped_child_env,
)
from state import live_branch, repository_identity  # noqa: E402

GIT = gitutil.GIT_EXECUTABLE


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
        raise AssertionError((command, result.returncode, result.stdout, result.stderr))
    return result


class LockConformanceCase(unittest.TestCase):
    """Shared helpers: test-owned temporary Git repositories."""

    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory(prefix="factory-loop-lock-")
        self.addCleanup(self._temporary.cleanup)

    @property
    def tmp(self) -> Path:
        return Path(self._temporary.name)

    def make_repo(self, branch: str = "develop", *, files: bool = True) -> Path:
        root = Path(tempfile.mkdtemp(prefix="factory-loop-lock-repo.", dir=self.tmp))
        self.addCleanup(shutil.rmtree, root, True)
        run([GIT, "init", "-q", "-b", branch, str(root)], root)
        if files:
            (root / "tracked.txt").write_text("tracked\n", encoding="utf-8")
            (root / "docs").mkdir()
            (root / "docs" / "SPEC.md").write_text(
                "canonical spec bytes\n", encoding="utf-8"
            )
            (root / ".factory").mkdir()
            (root / ".factory" / "artifacts").mkdir()
            (root / ".factory" / "artifacts" / "implementation-plan.md").write_text(
                "canonical plan bytes\n", encoding="utf-8"
            )
            run([GIT, "add", "."], root)
            run(
                [
                    GIT, "-C", str(root), "-c", "user.name=lock-test",
                    "-c", "user.email=lock-test@example.invalid",
                    "commit", "-qm", "base",
                ],
                root,
            )
        return root

    def spec_binding(self, root: Path) -> SpecBinding:
        commit = run([GIT, "-C", str(root), "rev-parse", "HEAD"]).stdout.strip()
        blob = run(
            [GIT, "-C", str(root), "rev-parse", "HEAD:docs/SPEC.md"]
        ).stdout.strip()
        return SpecBinding(path="docs/SPEC.md", commit=commit, blob=blob)

    def plan_binding(self, root: Path) -> PlanBinding:
        commit = run([GIT, "-C", str(root), "rev-parse", "HEAD"]).stdout.strip()
        raw = (root / ".factory" / "artifacts" / "implementation-plan.md").read_bytes()
        return PlanBinding(
            path=".factory/artifacts/implementation-plan.md",
            base_commit=commit,
            digest=hashlib.sha256(raw).hexdigest(),
        )

    def full_binding(self, root: Path):
        """The mandatory identity/branch/spec/plan acquisition binding set (F2)."""
        return (
            repository_identity(root),
            live_branch(root),
            self.spec_binding(root),
            self.plan_binding(root),
        )

    def acquire(self, root: Path, **overrides):
        """Acquire the lock with the full mandatory binding set (F2)."""
        identity, branch, spec, plan = self.full_binding(root)
        if "expected_identity" in overrides:
            identity = overrides.pop("expected_identity")
        if "expected_branch" in overrides:
            branch = overrides.pop("expected_branch")
        if "spec" in overrides:
            spec = overrides.pop("spec")
        if "plan" in overrides:
            plan = overrides.pop("plan")
        if overrides:
            raise AssertionError(f"unexpected override keys: {sorted(overrides)}")
        return acquire_root_lock(
            root,
            expected_identity=identity,
            expected_branch=branch,
            spec=spec,
            plan=plan,
        )

    def wait_for_marker(self, marker: Path, timeout: float = 10.0) -> str:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if marker.exists():
                pid = marker.read_text(encoding="utf-8").strip()
                if pid:
                    return pid
            time.sleep(0.02)
        raise AssertionError(f"marker never appeared: {marker}")

    def _pid_alive(self, pid: int) -> bool:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True

    @staticmethod
    def _pids(scope) -> frozenset[int]:
        """PID set of a captured scope (records or bare PIDs)."""
        return frozenset(
            record.pid if isinstance(record, CapturedProcess) else record
            for record in scope
        )

    @staticmethod
    def _record_for(scope, pid: int) -> CapturedProcess | None:
        for record in scope:
            if isinstance(record, CapturedProcess) and record.pid == pid:
                return record
        return None


class RootDescriptorLockCase(LockConformanceCase):
    """Exclusive flock on the canonical root directory descriptor."""

    def test_exactly_one_writer_holds_the_lock(self) -> None:
        root = self.make_repo()
        self.assertTrue(probe_root_lock(root))
        lock = self.acquire(root)
        try:
            self.assertFalse(probe_root_lock(root), "second writer must contend")
            with self.assertRaises(RootLockHeldError):
                self.acquire(root)
        finally:
            lock.release()
        self.assertTrue(probe_root_lock(root), "release must free the boundary")
        with self.acquire(root):
            pass

    def test_no_replaceable_lock_pathname_is_created(self) -> None:
        root = self.make_repo()
        before = sorted(p.name for p in root.iterdir())
        with self.acquire(root):
            pass
        after = sorted(p.name for p in root.iterdir())
        self.assertEqual(before, after, "locking must not create a lock file")
        self.assertFalse((root / ".factory-lock").exists())

    def test_root_must_be_a_real_directory_and_no_follow(self) -> None:
        root = self.make_repo()
        identity, branch, spec, plan = self.full_binding(root)
        plain = root / "tracked.txt"
        with self.assertRaises(RootLockUnsafeError):
            acquire_root_lock(
                plain,
                expected_identity=identity, expected_branch=branch,
                spec=spec, plan=plan,
            )
        link = self.tmp / "root-link"
        link.symlink_to(root)
        with self.assertRaises(RootLockUnsafeError):
            acquire_root_lock(
                link,
                expected_identity=identity, expected_branch=branch,
                spec=spec, plan=plan,
            )
        link.unlink()

    def test_independent_descriptor_cannot_unlock_the_holder(self) -> None:
        root = self.make_repo()
        with self.acquire(root) as lock:
            separate = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                # LOCK_UN on an unrelated open file description is a no-op and
                # cannot disturb the holder's description.
                fcntl.flock(separate, fcntl.LOCK_UN)
                with self.assertRaises(BlockingIOError):
                    fcntl.flock(separate, fcntl.LOCK_EX | fcntl.LOCK_NB)
                self.assertFalse(probe_root_lock(root))
                self.assertEqual(lock.identity, repository_identity(root))
            finally:
                os.close(separate)
        self.assertTrue(probe_root_lock(root))

    def test_concurrent_launcher_probes_exactly_one_writer(self) -> None:
        root = self.make_repo()
        identity, branch, spec, plan = self.full_binding(root)
        binding = (
            f"expected_identity={identity!r}, expected_branch={branch!r}, "
            f"spec={spec!r}, plan={plan!r}"
        )
        contenders = 4
        child = (
            "import sys, time\n"
            "sys.path.insert(0, %r)\n"
            "from lock import acquire_root_lock, RootLockHeldError\n"
            "from lock import SpecBinding, PlanBinding\n"
            "try:\n"
            "    with acquire_root_lock(sys.argv[1], %s):\n"
            "        time.sleep(2)\n"
            "        sys.exit(0)\n"
            "except RootLockHeldError:\n"
            "    sys.exit(1)\n" % (str(LOOP), binding)
        )
        processes = [
            subprocess.Popen(
                [sys.executable, "-c", child, str(root)],
                cwd=self.tmp,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            for _ in range(contenders)
        ]
        exits = []
        for process in processes:
            process.communicate(timeout=30)
            exits.append(process.returncode)
        self.assertEqual(
            exits.count(0), 1, f"exactly one writer must win, got exits {exits}"
        )


class MandatoryBindings(LockConformanceCase):
    """F2: mandatory identity/branch/spec/plan bindings fail closed."""

    def test_acquisition_requires_all_four_bindings(self) -> None:
        root = self.make_repo()
        identity, branch, spec, plan = self.full_binding(root)
        for kwargs in (
            {},
            {"expected_identity": identity},
            {"expected_identity": identity, "expected_branch": branch},
            {
                "expected_identity": identity,
                "expected_branch": branch,
                "spec": spec,
            },
        ):
            with self.subTest(kwargs=kwargs):
                with self.assertRaisesRegex(RootLockBindingError, "mandatory"):
                    acquire_root_lock(root, **kwargs)
                self.assertTrue(probe_root_lock(root), "no lock may be granted")

    def test_repository_identity_matches_the_state_authority(self) -> None:
        root = self.make_repo()
        with self.acquire(root) as lock:
            self.assertEqual(lock.identity, repository_identity(root))
        with self.assertRaises(RootLockBindingError):
            self.acquire(root, expected_identity="0:0")
        other = self.make_repo()
        other_identity = repository_identity(other)
        with self.assertRaises(RootLockBindingError):
            self.acquire(root, expected_identity=other_identity)

    def test_required_branch_binding(self) -> None:
        root = self.make_repo(branch="develop")
        with self.acquire(root, expected_branch="develop"):
            pass
        with self.assertRaises(RootLockBindingError):
            self.acquire(root, expected_branch="main")
        self.assertEqual(live_branch(root), "develop")

    def test_spec_binding_matches_and_mismatch_fails_closed(self) -> None:
        root = self.make_repo()
        binding = self.spec_binding(root)
        with self.acquire(root, spec=binding):
            pass
        wrong_blob = SpecBinding(path=binding.path, commit=binding.commit, blob="0" * 40)
        with self.assertRaisesRegex(RootLockBindingError, "spec blob binding mismatch"):
            self.acquire(root, spec=wrong_blob)
        wrong_commit = SpecBinding(
            path=binding.path, commit="1" * 40, blob=binding.blob
        )
        with self.assertRaisesRegex(RootLockBindingError, "spec commit"):
            self.acquire(root, spec=wrong_commit)
        missing_path = SpecBinding(
            path="docs/NOT-THERE.md", commit=binding.commit, blob=binding.blob
        )
        with self.assertRaisesRegex(RootLockBindingError, "not committed"):
            self.acquire(root, spec=missing_path)

    def test_plan_binding_matches_and_mismatch_fails_closed(self) -> None:
        root = self.make_repo()
        binding = self.plan_binding(root)
        with self.acquire(root, plan=binding):
            pass
        wrong_digest = PlanBinding(
            path=binding.path, base_commit=binding.base_commit, digest="0" * 64
        )
        with self.assertRaisesRegex(RootLockBindingError, "plan digest binding mismatch"):
            self.acquire(root, plan=wrong_digest)
        wrong_head = PlanBinding(
            path=binding.path, base_commit="0" * 40, digest=binding.digest
        )
        with self.assertRaisesRegex(RootLockBindingError, "base commit"):
            self.acquire(root, plan=wrong_head)
        missing_path = PlanBinding(
            path=".factory/artifacts/absent.md",
            base_commit=binding.base_commit,
            digest=binding.digest,
        )
        with self.assertRaisesRegex(RootLockBindingError, "not committed"):
            self.acquire(root, plan=missing_path)
        # A checkout moved forward past the bound base fails closed.
        run(
            [
                GIT, "-C", str(root), "-c", "user.name=lock-test",
                "-c", "user.email=lock-test@example.invalid",
                "commit", "--allow-empty", "-qm", "forward",
            ]
        )
        with self.assertRaisesRegex(RootLockBindingError, "does not match the plan base"):
            self.acquire(root, plan=binding)

    def test_traversal_and_unsafe_binding_paths_fail_closed(self) -> None:
        root = self.make_repo()
        for path in ("", "/etc/passwd", "../outside.md", "a/../b.md", ".", "a//b"):
            with self.subTest(path=path):
                spec = SpecBinding(path=path, commit="0" * 40, blob="0" * 40)
                with self.assertRaises(RootLockBindingError):
                    self.acquire(root, spec=spec)

    def test_git_reads_are_anchored_to_the_locked_inode(self) -> None:
        """F2: a canonical-path rebind never redirects a single read.

        While the lock is held the canonical pathname is renamed away and a
        symlink to a *different* repository is installed.  Re-validation must
        fail closed on the pathname/inode drift, and an explicitly anchored
        Git read must still resolve the *locked* repository, never the
        substituted one.
        """
        root = self.make_repo(branch="develop")
        evil = self.make_repo(branch="evil")
        with self.acquire(root) as lock:
            moved = self.tmp / "root.moved"
            os.rename(root, moved)
            try:
                os.symlink(evil, root)
                with self.assertRaisesRegex(RootLockError, "no longer names"):
                    lock.validate_bindings()
                # The anchored descriptor still resolves the locked inode.
                anchor = lock._anchor_descriptor()
                try:
                    result = gitutil.git_run(
                        ["-C", f"/proc/self/fd/{anchor}",
                         "rev-parse", "--abbrev-ref", "HEAD"],
                        pass_fds=[anchor],
                    )
                    self.assertEqual(result.stdout.strip(), "develop")
                    self.assertNotEqual(result.stdout.strip(), "evil")
                finally:
                    os.close(anchor)
            finally:
                os.unlink(root)
                os.rename(moved, root)
        self.assertTrue(probe_root_lock(root))


class ChildBoundary(LockConformanceCase):
    """Leaves inherit no lock descriptor, no lock metadata, and a new session."""

    def test_untrusted_leaf_inherits_no_lock_descriptor_and_no_lock_env(self) -> None:
        root = self.make_repo()
        inspect = root / "inspect.py"
        inspect.write_text(
            """import os, sys
root = sys.argv[1]
keys = %r
assert all(key not in os.environ for key in keys), os.environ
root_info = os.stat(root, follow_symlinks=False)
for item in os.listdir('/proc/self/fd'):
    try:
        info = os.stat('/proc/self/fd/' + item)
    except OSError:
        continue
    assert (info.st_dev, info.st_ino) != (root_info.st_dev, root_info.st_ino), item
# A new process session was started for the child.
assert os.getsid(0) == os.getpid()
assert os.getsid(0) != os.getsid(os.getppid())
print('untrusted-clean')
"""
            % (ENV_KEYS,),
            encoding="utf-8",
        )
        with self.acquire(root) as lock:
            result = lock.spawn_child(
                [sys.executable, str(inspect), str(root)],
                cwd=self.tmp,
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("untrusted-clean", result.stdout)

    def test_bounded_capture_flooded_stdout_stays_bounded_during_read(self) -> None:
        """Task 11 review: gate stdout is bounded *during* the read, not
        after communicate — a child flooding its pipes can never blow up
        the holder's memory, and the retained text is the bounded head."""
        root = self.make_repo()
        flood = root / "flood.py"
        flood.write_text(
            "import sys\n"
            "for index in range(400000):\n"
            "    sys.stdout.write(f'flood-{index:06d} ' * 8 + '\\n')\n"
            "sys.stderr.write('stderr marker 0815\\n')\n"
            "sys.exit(0)\n",
            encoding="utf-8",
        )
        with self.acquire(root) as lock:
            result = lock.spawn_child(
                [sys.executable, str(flood)],
                cwd=self.tmp,
                stdout_limit=8192,
                stderr_limit=8192,
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertLessEqual(len(result.stdout), 8192)
        self.assertLessEqual(len(result.stderr), 8192)
        self.assertIn("stderr marker 0815", result.stderr)
        # The retained stdout is the bounded head (what the deterministic
        # gates consume); the flood that follows is drained, never retained.
        self.assertIn("flood-000000 ", result.stdout)
        self.assertNotIn("flood-399999 ", result.stdout)

    def test_boundary_refuses_to_pass_the_lock_descriptor(self) -> None:
        root = self.make_repo()
        with self.acquire(root) as lock:
            with self.assertRaises(RootLockUnsafeError):
                lock.spawn_child([sys.executable, "-c", "pass"], pass_fds=[lock.fd])

    def test_standard_descriptor_pass_fds_is_rejected_explicitly(self) -> None:
        """Task 22 residual: pass_fds <= 2 is refused, never silently dropped.

        A standard descriptor (stdin/stdout/stderr) can never be a retained
        helper descriptor — ``close_fds=True`` always preserves the standard
        streams — so a caller that believed the descriptor reached the child
        would be wrong about the executed boundary.  The boundary therefore
        rejects 0/1/2 explicitly instead of filtering them out.
        """
        root = self.make_repo()
        with self.acquire(root) as lock:
            for fd in (0, 1, 2):
                with self.assertRaisesRegex(
                    RootLockUnsafeError, "standard descriptor"
                ):
                    lock.spawn_child(
                        [sys.executable, "-c", "pass"], pass_fds=[fd]
                    )

    def test_pass_fds_alias_of_the_lock_inode_is_rejected(self) -> None:
        """F8: a dup (same OFD) or a separate open (same inode) is refused."""
        root = self.make_repo()
        with self.acquire(root) as lock:
            dup = os.dup(lock.fd)
            try:
                with self.assertRaisesRegex(RootLockUnsafeError, "aliases"):
                    lock.spawn_child(
                        [sys.executable, "-c", "pass"], pass_fds=[dup]
                    )
            finally:
                os.close(dup)
            separate = os.open(
                root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
            )
            try:
                with self.assertRaisesRegex(RootLockUnsafeError, "aliases"):
                    lock.spawn_child(
                        [sys.executable, "-c", "pass"], pass_fds=[separate]
                    )
            finally:
                os.close(separate)
            # An unrelated descriptor is *not* an alias and still passes.
            unrelated = os.open(self.tmp / "unrelated.txt", os.O_CREAT | os.O_RDWR, 0o600)
            try:
                probe = root / "fd.py"
                probe.write_text(
                    "import os, sys; print('got', sys.argv[1])\n", encoding="utf-8"
                )
                result = lock.spawn_child(
                    [sys.executable, str(probe), str(unrelated)],
                    cwd=self.tmp, pass_fds=[unrelated],
                )
                self.assertEqual(result.returncode, 0, result.stderr)
            finally:
                os.close(unrelated)

    def test_child_environment_is_stripped_of_all_lock_metadata(self) -> None:
        root = self.make_repo()
        with self.acquire(root) as lock:
            metadata = lock.lock_metadata()
            self.assertEqual(set(metadata), set(ENV_KEYS))
            stripped = stripped_child_env(metadata)
            self.assertEqual(stripped, {})
            for key in ENV_KEYS:
                self.assertNotIn(key, stripped_child_env())
            exported = dict(os.environ)
            exported.update(metadata)
            self.assertEqual(
                set(stripped_child_env(exported)) & set(ENV_KEYS), set()
            )

    def test_legacy_lock_env_keys_are_stripped_by_prefix_too(self) -> None:
        """F10: the whole prefix, including the legacy FACTORY_LOCK_* family."""
        legacy_keys = (
            "FACTORY_LOCK_HELD",
            "FACTORY_LOCK_FD",
            "FACTORY_LOCK_ID",
            "FACTORY_LOCK_ROOT",
            "FACTORY_LOCK_UNKNOWN_FUTURE",
        )
        environment = dict(os.environ)
        for key in legacy_keys:
            environment[key] = "1"
        environment["FACTORY_LOOP_LOCK_LEGACY_TOO"] = "1"
        stripped = stripped_child_env(environment)
        for key in legacy_keys + ("FACTORY_LOOP_LOCK_LEGACY_TOO",):
            self.assertNotIn(key, stripped)
        root = self.make_repo()
        with self.acquire(root) as lock:
            probe = root / "env.py"
            probe.write_text(
                "import os\n"
                "for key in %r:\n"
                "    assert key not in os.environ, key\n"
                "print('legacy-clean')\n" % (legacy_keys,),
                encoding="utf-8",
            )
            result = lock.spawn_child(
                [sys.executable, str(probe)], cwd=self.tmp, env=environment
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("legacy-clean", result.stdout)

    def test_child_starts_in_a_new_session(self) -> None:
        root = self.make_repo()
        probe = root / "session.py"
        probe.write_text("import os; print(os.getsid(0))\n", encoding="utf-8")
        with self.acquire(root) as lock:
            result = lock.spawn_child([sys.executable, str(probe)], cwd=self.tmp)
        child_sid = int(result.stdout.strip())
        self.assertNotEqual(child_sid, os.getsid(0))
        self.assertNotEqual(child_sid, os.getpid())

    def test_bounded_timeout_kills_and_reaps_the_whole_process_group(self) -> None:
        """F3: a bounded timeout TERM/KILLs and reaps the child's entire group."""
        root = self.make_repo()
        marker = self.tmp / "group-child.pid"
        script = root / "sleepy-model.py"
        script.write_text(
            """import os, sys, time
root, marker = sys.argv[1], sys.argv[2]
pid = os.fork()
if pid == 0:
    # Grandchild stays in the child's new session/process group.
    with open(marker, 'w', encoding='utf-8') as stream:
        stream.write(str(os.getpid()))
    time.sleep(300)
    os._exit(0)
os.waitpid(pid, 0)
time.sleep(300)
""",
            encoding="utf-8",
        )
        with self.acquire(root) as lock:
            started = time.monotonic()
            with self.assertRaises(RootLockTimeoutError):
                lock.spawn_child(
                    [sys.executable, str(script), str(root), str(marker)],
                    cwd=self.tmp,
                    timeout=1.0,
                    kill_grace=0.2,
                )
            elapsed = time.monotonic() - started
            self.assertLess(elapsed, 10.0, "bounded termination must stay bounded")
            grandchild = None
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                if marker.exists():
                    grandchild = int(self.wait_for_marker(marker, timeout=1))
                    break
                time.sleep(0.02)
            time.sleep(0.4)
            if grandchild is not None:
                self.assertFalse(
                    self._pid_alive(grandchild),
                    f"grandchild {grandchild} survived the group timeout",
                )

    def test_timeout_failure_is_part_of_the_unified_exception_contract(self) -> None:
        """F9: every lock failure routes through RootLockError subclasses."""
        root = self.make_repo()
        with self.acquire(root) as lock:
            self.assertIsInstance(
                RootLockHeldError("x"), RootLockError
            )
            for exc in (
                RootLockBindingError("x"),
                RootLockUnsafeError("x"),
                RootLockTimeoutError("x"),
                RootLockCommandError(1, ["x"]),
                EscapedDescendantError("x"),
            ):
                self.assertIsInstance(exc, RootLockError)
            # A Git-boundary failure behind the lock surfaces as the lock
            # contract, never as a raw GitBoundaryError.
            lock.release()
            with self.assertRaises(RootLockError):
                lock.spawn_child([sys.executable, "-c", "pass"])

    def test_leader_exits_on_term_but_termingnoring_grandchild_holds_pipes_stays_bounded(self) -> None:
        """F3: a TERM-ignoring grandchild that holds the pipes cannot stall.

        The leader exits on TERM (a well-behaved model) while a grandchild
        that inherits the stdout/stderr write ends ignores SIGTERM and keeps
        writing.  The leader's exit is never taken as 'the group is gone':
        after the full bounded grace the group is KILLed unconditionally,
        the held pipe ends are force-closed, and the bounded run returns
        with no live process-group member left.
        """
        root = self.make_repo()
        marker = self.tmp / "termingnore-grandchild.pid"
        script = root / "term-ignoring-model.py"
        script.write_text(
            """import os, signal, sys, time
root, marker = sys.argv[1], sys.argv[2]

def _on_term(signum, frame):
    # The leader exits on TERM exactly as a well-behaved model would.
    os._exit(0)

signal.signal(signal.SIGTERM, _on_term)
pid = os.fork()
if pid == 0:
    # Grandchild ignores TERM and holds the stdout/stderr write ends.
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    with open(marker, 'w', encoding='utf-8') as stream:
        stream.write(str(os.getpid()))
    deadline = time.monotonic() + 300
    while time.monotonic() < deadline:
        print('grandchild-holds-pipe', flush=True)
        time.sleep(0.05)
    os._exit(0)
os.waitpid(pid, 0)
time.sleep(300)
""",
            encoding="utf-8",
        )
        grandchild = None
        with self.acquire(root) as lock:
            started = time.monotonic()
            with self.assertRaises(RootLockTimeoutError):
                lock.spawn_child(
                    [sys.executable, str(script), str(root), str(marker)],
                    cwd=self.tmp,
                    timeout=1.0,
                    kill_grace=0.2,
                )
            elapsed = time.monotonic() - started
            self.assertLess(
                elapsed, 10.0,
                "a TERM-ignoring pipe-holding grandchild must not stall the "
                f"bounded run (took {elapsed:.2f}s)",
            )
            grandchild = int(self.wait_for_marker(marker, timeout=5))
            time.sleep(0.4)
            self.assertFalse(
                self._pid_alive(grandchild),
                f"TERM-ignoring grandchild {grandchild} survived the group "
                "timeout",
            )

    def test_nonzero_child_exit_under_check_true_surfaces_as_command_error(self) -> None:
        """F9: a nonzero child exit under ``check=True`` is RootLockCommandError."""
        root = self.make_repo()
        with self.acquire(root) as lock:
            with self.assertRaises(RootLockCommandError) as ctx:
                lock.spawn_child(
                    [sys.executable, "-c", "import sys; sys.exit(7)"],
                    cwd=self.tmp,
                    check=True,
                )
            self.assertEqual(ctx.exception.returncode, 7)
            self.assertEqual(ctx.exception.argv[-1], "import sys; sys.exit(7)")
            # Without ``check=True`` the nonzero exit is reported, not raised.
            result = lock.spawn_child(
                [sys.executable, "-c", "import sys; sys.exit(3)"], cwd=self.tmp
            )
            self.assertEqual(result.returncode, 3)

    def test_flock_oserror_is_wrapped_into_the_lock_contract(self) -> None:
        """F9: an OSError from the exclusive flock surfaces as RootLockUnsafeError."""
        root = self.make_repo()
        identity, branch, spec, plan = self.full_binding(root)

        def _boom(fd, op):
            raise OSError("simulated flock failure")

        with unittest.mock.patch.object(lock_module.fcntl, "flock", _boom):
            with self.assertRaisesRegex(RootLockUnsafeError, "flock"):
                acquire_root_lock(
                    root,
                    expected_identity=identity,
                    expected_branch=branch,
                    spec=spec,
                    plan=plan,
                )
        self.assertTrue(probe_root_lock(root), "the failed acquisition grants no lock")

    def test_git_boundary_failure_is_wrapped_into_the_lock_contract(self) -> None:
        """F9: a GitBoundaryError behind the lock surfaces as RootLockUnsafeError."""
        root = self.make_repo()
        with self.acquire(root) as lock:
            with unittest.mock.patch.object(
                lock_module,
                "git_run",
                side_effect=lock_module.GitBoundaryError("boom"),
            ):
                with self.assertRaisesRegex(RootLockUnsafeError, "pinned Git"):
                    lock.validate_bindings()


class PinnedGit(LockConformanceCase):
    """F4/F5: Git selection and environment hardening."""

    def _evil_git(self, marker: Path) -> dict:
        evil = self.tmp / "evil-bin"
        evil.mkdir()
        git_shim = evil / "git"
        git_shim.write_text(
            "#!/usr/bin/env bash\n"
            f"echo substituted >> {marker}\n"
            "echo evil-branch\n"
            "exit 0\n",
            encoding="utf-8",
        )
        git_shim.chmod(0o755)
        return {"PATH": f"{evil}:{os.environ.get('PATH', '')}"}

    def test_live_branch_uses_the_pinned_absolute_git(self) -> None:
        root = self.make_repo(branch="develop")
        marker = self.tmp / "substituted.marker"
        environment = self._evil_git(marker)
        saved = os.environ["PATH"]
        os.environ["PATH"] = environment["PATH"]
        try:
            branch = live_branch(root)
        finally:
            os.environ["PATH"] = saved
        self.assertEqual(branch, "develop")
        self.assertFalse(marker.exists(), "the evil `git` shim must never run")

    def test_resolve_head_is_finite_bounded(self) -> None:
        # Task 9 review MED: every trusted state/branch Git invocation is
        # finite bounded — resolve_head may never wait forever.
        root = self.make_repo()
        recorded: list = []
        real_run = gitutil.git_run

        def recording_run(argv, *, timeout=None, **kwargs):
            recorded.append(timeout)
            return real_run(argv, timeout=timeout, **kwargs)

        with unittest.mock.patch.object(
            gitutil, "git_run", side_effect=recording_run,
        ):
            head = gitutil.resolve_head(root)
        self.assertEqual(len(head), 40)
        self.assertEqual(len(recorded), 1)
        self.assertIsNotNone(recorded[0])
        self.assertGreater(recorded[0], 0)
        self.assertLessEqual(recorded[0], gitutil.GIT_TIMEOUT)

    def test_lock_binding_git_calls_use_the_pinned_absolute_git(self) -> None:
        root = self.make_repo()
        marker = self.tmp / "substituted.marker"
        environment = self._evil_git(marker)
        saved = os.environ["PATH"]
        os.environ["PATH"] = environment["PATH"]
        try:
            with self.acquire(root) as lock:
                self.assertEqual(lock.identity, repository_identity(root))
        finally:
            os.environ["PATH"] = saved
        self.assertFalse(marker.exists(), "the evil `git` shim must never run")

    def test_git_selection_never_consults_the_caller_path(self) -> None:
        """F4: resolution is a pure function of fixed absolute locations."""
        evil = self.tmp / "evil-bin"
        evil.mkdir()
        fake = evil / "git"
        fake.write_text("#!/bin/sh\necho evil\n", encoding="utf-8")
        fake.chmod(0o755)
        poisoned = {"PATH": f"{evil}:{os.environ.get('PATH', '')}"}
        saved = os.environ["PATH"]
        os.environ["PATH"] = poisoned["PATH"]
        try:
            resolved = gitutil.resolve_git_executable()
        finally:
            os.environ["PATH"] = saved
        self.assertTrue(resolved.startswith("/"), resolved)
        self.assertNotEqual(Path(resolved).parent, evil)
        self.assertFalse(
            str(resolved).startswith(str(evil)), f"PATH-derived git selected: {resolved}"
        )
        self.assertEqual(
            resolved, gitutil.resolve_git_executable(), "resolution is deterministic"
        )

    def test_immutable_nix_store_candidate_is_supported(self) -> None:
        """F4: an immutable root-owned Nix-store absolute git is accepted safely."""
        if not gitutil.NIX_STORE_PATH_RE.fullmatch(gitutil.GIT_EXECUTABLE):
            self.skipTest("the pinned Git is not a Nix-store path")
        self.assertEqual(gitutil.GIT_EXECUTABLE, gitutil.resolve_git_executable())
        # The binary and its store components are foreign-owned and not
        # group/other-writable; the caller cannot modify them.
        info = os.stat(gitutil.GIT_EXECUTABLE)
        self.assertNotEqual(info.st_uid, os.getuid())
        self.assertEqual(info.st_mode & 0o022, 0)

    def test_callable_owned_or_writable_candidate_fails_closed(self) -> None:
        """F4: a caller-owned or group/other-writable candidate is rejected."""
        owned = self.tmp / "git"
        owned.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        owned.chmod(0o755)
        with self.assertRaises(gitutil.GitBoundaryError):
            gitutil._validate_candidate(str(owned))
        writable = self.tmp / "git-writable"
        writable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        writable.chmod(0o666)
        with self.assertRaises(gitutil.GitBoundaryError):
            gitutil._validate_candidate(str(writable))
        # The pinned executable itself is never replaced by these.
        self.assertEqual(gitutil.GIT_EXECUTABLE, gitutil.resolve_git_executable())

    def test_git_config_family_is_stripped_completely(self) -> None:
        """F5: the complete GIT_CONFIG family (incl. parameters) is stripped."""
        poisoned = {
            "PATH": "/usr/bin",
            "GIT_CONFIG": "global",
            "GIT_CONFIG_SYSTEM": "/etc/evil-gitconfig",
            "GIT_CONFIG_GLOBAL": "/home/evil/.gitconfig",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_COUNT": "2",
            "GIT_CONFIG_KEY_0": "core.hooksPath",
            "GIT_CONFIG_VALUE_0": "/evil/hooks",
            "GIT_CONFIG_PARAMETERS": "'core.hooksPath=/evil/hooks'",
            "GIT_DIR": "/evil/objects",
            "GIT_WORK_TREE": "/evil",
            "GIT_INDEX_FILE": "/evil/index",
            "KEEP_ME": "kept",
        }
        sanitized = gitutil.sanitize_git_environment(poisoned)
        self.assertEqual(sanitized, {"PATH": "/usr/bin", "KEEP_ME": "kept"})
        for key in poisoned:
            if key in ("PATH", "KEEP_ME"):
                continue
            self.assertNotIn(key, sanitized)
        # The strip is prefix-based: any future GIT_CONFIG_* sibling is gone.
        future = {"GIT_CONFIG_FUTURE_VARIANT": "1", "PATH": "/usr/bin"}
        self.assertNotIn(
            "GIT_CONFIG_FUTURE_VARIANT",
            gitutil.sanitize_git_environment(future),
        )
        # A real invocation never sees the family either.
        root = self.make_repo()
        poisoned_env = dict(os.environ)
        poisoned_env.update(
            {
                "GIT_CONFIG_COUNT": "1",
                "GIT_CONFIG_KEY_0": "core.bare",
                "GIT_CONFIG_VALUE_0": "true",
            }
        )
        result = gitutil.git_run(
            ["-C", str(root), "rev-parse", "--abbrev-ref", "HEAD"],
            env=poisoned_env,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "develop")


class EscapedDescendants(LockConformanceCase):
    """F1: full bounded ancestry; escapes are detected and block recovery."""

    def test_full_bounded_ancestry_is_bounded_and_fails_closed(self) -> None:
        root = self.make_repo()
        marker = self.tmp / "child.pid"
        sleeper = root / "sleeper.py"
        sleeper.write_text(
            "import os, sys, time\n"
            f"with open({str(marker)!r}, 'w') as s: s.write(str(os.getpid()))\n"
            "time.sleep(300)\n",
            encoding="utf-8",
        )
        child = subprocess.Popen(
            [sys.executable, str(sleeper)], cwd=self.tmp, start_new_session=True
        )
        try:
            child_pid = int(self.wait_for_marker(marker))
            # The full closure of our own process includes the child, and
            # every captured PID records its starttime and parent identity.
            captured = capture_descendants(os.getpid())
            captured_pids = self._pids(captured)
            self.assertIn(os.getpid(), captured_pids)
            self.assertIn(child_pid, captured_pids)
            for pid in captured_pids:
                record = self._record_for(captured, pid)
                self.assertIsNotNone(record, f"no record for captured pid {pid}")
                self.assertGreater(record.starttime, 0)
                self.assertGreaterEqual(record.parent, 1)
            # A closure that would exceed the bound fails closed instead of
            # silently truncating (the root pid's own tree is > 1 here).
            with self.assertRaises(RootLockUnsafeError):
                capture_descendants(os.getpid(), maximum=1)
            # An invalid bound fails closed too.
            with self.assertRaises(RootLockUnsafeError):
                capture_descendants(os.getpid(), maximum=0)
            with self.assertRaises(RootLockUnsafeError):
                capture_descendants(0)
            # live_scope re-enumerates only the still-live captured members.
            self.assertIn(child_pid, live_scope(captured))
            os.kill(child_pid, signal.SIGKILL)
            child.wait(timeout=10)
            time.sleep(0.2)
            self.assertNotIn(child_pid, live_scope(captured))
        finally:
            if child.poll() is None:
                child.kill()
                child.wait(timeout=10)

    def test_stat_comm_containing_parenthesis_is_parsed(self) -> None:
        """F1: /proc/<pid>/stat parsing uses the last `)` (comm may contain it)."""
        original = Path("/proc/self/comm").read_text(encoding="ascii").strip()
        libc = ctypes.CDLL(None, use_errno=True)
        probe_name = ctypes.c_char_p(b"we)ird)comm")
        self.assertEqual(libc.prctl(15, probe_name, 0, 0, 0), 0)  # PR_SET_NAME
        try:
            self.assertEqual(lock_module._ppid_of(os.getpid()), os.getppid())
            self.assertIn("we)ird)comm", Path("/proc/self/comm").read_text(encoding="ascii"))
        finally:
            libc.prctl(15, ctypes.c_char_p(original.encode("ascii")[:15]), 0, 0, 0)

    def test_double_fork_setsid_escape_is_detected_and_blocks_recovery(self) -> None:
        root = self.make_repo()
        marker = self.tmp / "escaped.pid"
        script = root / "escaped-model.py"
        script.write_text(
            """import os, sys, time
root, marker = sys.argv[1], sys.argv[2]
pid = os.fork()
if pid == 0:
    os.setsid()
    os.chdir(root)
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    with open(marker, 'w', encoding='utf-8') as stream:
        stream.write(str(os.getpid()))
    time.sleep(300)
    os._exit(0)
time.sleep(300)
""",
            encoding="utf-8",
        )
        grandchild = None
        model = subprocess.Popen(
            [sys.executable, str(script), str(root), str(marker)],
            cwd=self.tmp, start_new_session=True,
        )
        try:
            grandchild = int(self.wait_for_marker(marker))
            captured = capture_descendants(model.pid)
            captured_pids = self._pids(captured)
            self.assertIn(model.pid, captured_pids)
            self.assertIn(grandchild, captured_pids)
            os.killpg(model.pid, signal.SIGTERM)
            self.assertEqual(model.wait(timeout=10), -signal.SIGTERM)
            escaped, reason = detect_escaped_descendants(
                root, model_pid=model.pid, captured=captured
            )
            self.assertIn(grandchild, escaped)
            self.assertTrue(reason)
            with self.assertRaises(EscapedDescendantError):
                assert_no_escaped_descendants(
                    root, model_pid=model.pid, captured=captured
                )
        finally:
            if grandchild is not None:
                try:
                    os.kill(grandchild, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            if model.poll() is None:
                try:
                    os.killpg(model.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                model.wait(timeout=10)
            time.sleep(0.3)
        escaped, reason = detect_escaped_descendants(root)
        self.assertEqual(escaped, frozenset(), reason)

    def test_escaped_child_holding_the_lock_fd_blocks_reacquisition(self) -> None:
        root = self.make_repo()
        marker = self.tmp / "holder.pid"
        lock = self.acquire(root)
        holder = os.fork()
        if holder == 0:
            os.setsid()
            with open(marker, "w", encoding="utf-8") as stream:
                stream.write(str(os.getpid()))
            time.sleep(300)
            os._exit(0)
        holder_pid = int(self.wait_for_marker(marker))
        try:
            # The holder's descriptor is dropped *without* LOCK_UN; the
            # escaped child's fork dup keeps the open file description — and
            # therefore the flock — alive: reacquisition must fail closed.
            os.close(lock.fd)
            lock._fd = -1
            with self.assertRaises(RootLockHeldError):
                self.acquire(root)
            escaped, reason = detect_escaped_descendants(root)
            self.assertIn(holder_pid, escaped, reason)
        finally:
            os.kill(holder_pid, signal.SIGKILL)
            os.waitpid(holder_pid, 0)
        self.assertTrue(probe_root_lock(root), "killing the escape frees the boundary")
        with self.acquire(root):
            pass

    def test_handle_holder_outside_a_snapshot_is_detected(self) -> None:
        root = self.make_repo()
        marker = self.tmp / "handle.pid"
        holder = None
        pid = os.fork()
        if pid == 0:
            os.setsid()
            descriptor = os.open(
                root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
            )
            with open(marker, "w", encoding="utf-8") as stream:
                stream.write(str(os.getpid()))
            time.sleep(300)
            os._exit(0)
        try:
            holder = int(self.wait_for_marker(marker))
            escaped, reason = detect_escaped_descendants(root)
            self.assertIn(holder, escaped, reason)
            self.assertIn("repository-root or lock inode handle", reason)
        finally:
            if holder is not None:
                os.kill(holder, signal.SIGKILL)
                os.waitpid(holder, 0)
        escaped, reason = detect_escaped_descendants(root)
        self.assertEqual(escaped, frozenset(), reason)

    def test_self_ancestry_walks_the_full_multi_level_chain(self) -> None:
        """``_self_ancestry`` trusts every level of a multi-level chain.

        A three-level fork chain is built; the deepest leaf reports its
        trusted ancestor set.  Every level of the chain — the leaf's own
        pid, each intermediate, the model root, and the test process — must
        appear, proving the walk follows the per-pid parent table (not a
        single ``os.getppid``) up to PID 1.
        """
        root = self.make_repo()
        report = self.tmp / "self-ancestry.report"
        ready = self.tmp / "self-ancestry.ready"
        go = self.tmp / "self-ancestry.go"
        script = root / "self-ancestry.py"
        script.write_text(
            "import os, sys, time\n"
            "sys.path.insert(0, %r)\n"
            "import lock as lock_module\n"
            "depth = int(sys.argv[1])\n"
            "ready = sys.argv[2]\n"
            "go = sys.argv[3]\n"
            "report = sys.argv[4]\n"
            "def descend(d):\n"
            "    if d <= 0:\n"
            "        with open(ready, 'w') as f: f.write(str(os.getpid()))\n"
            "        deadline = time.monotonic() + 60\n"
            "        while not os.path.exists(go) and time.monotonic() < deadline:\n"
            "            time.sleep(0.02)\n"
            "        trusted = lock_module._self_ancestry()\n"
            "        with open(report, 'w') as f:\n"
            "            f.write(','.join(map(str, sorted(trusted))))\n"
            "        return\n"
            "    pid = os.fork()\n"
            "    if pid == 0:\n"
            "        descend(d - 1)\n"
            "        os._exit(0)\n"
            "    os.waitpid(pid, 0)\n"
            "descend(depth)\n"
            "print('done')\n" % (str(LOOP),),
            encoding="utf-8",
        )
        model = subprocess.Popen(
            [sys.executable, str(script), "3", str(ready), str(go), str(report)],
            cwd=self.tmp,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        chain = None
        try:
            self.wait_for_marker(ready)
            # The whole chain is alive here (the leaf is held at the go
            # gate), so the full descendant closure can be captured.
            chain = self._pids(capture_descendants(model.pid))
            go.write_text("go", encoding="utf-8")
            model.wait(timeout=30)
            report_text = self.wait_for_marker(report)
            leaf_trusted = {int(item) for item in report_text.split(",")}
            self.assertIn(os.getpid(), leaf_trusted, "test process must be trusted")
            self.assertIn(model.pid, leaf_trusted, "model root must be trusted")
            for pid in chain:
                self.assertIn(
                    pid, leaf_trusted,
                    f"chain level {pid} missing from _self_ancestry",
                )
        finally:
            go.write_text("go", encoding="utf-8")
            if model.poll() is None:
                model.kill()
                model.wait(timeout=10)
            for stream in (model.stdout, model.stderr):
                if stream is not None:
                    try:
                        stream.close()
                    except OSError:
                        pass

    def test_pid_starttime_reuse_is_excluded_by_live_scope(self) -> None:
        """CapturedProcess pins starttime: a reused/mismatched PID is excluded.

        A live captured descendant is reported by ``live_scope``; a
        :class:`CapturedProcess` at the same PID whose recorded starttime no
        longer matches the live process — the PID-reuse case — is excluded,
        and the underlying identity check fails closed on the mismatch.  PID
        reuse can therefore never widen the live-scope set with an unrelated
        process.
        """
        root = self.make_repo()
        marker = self.tmp / "reuse-sleeper.pid"
        sleeper = root / "reuse-sleeper.py"
        sleeper.write_text(
            "import os, sys, time\n"
            f"with open({str(marker)!r}, 'w') as s: s.write(str(os.getpid()))\n"
            "time.sleep(300)\n",
            encoding="utf-8",
        )
        child = subprocess.Popen([sys.executable, str(sleeper)], cwd=self.tmp)
        try:
            child_pid = int(self.wait_for_marker(marker))
            captured = capture_descendants(os.getpid())
            record = self._record_for(captured, child_pid)
            self.assertIsNotNone(record, f"no record captured for pid {child_pid}")
            self.assertGreater(record.starttime, 0)
            # The live child carrying its recorded starttime is live.
            self.assertIn(child_pid, live_scope(captured))
            self.assertTrue(
                lock_module._is_live_with_identity(child_pid, record.starttime)
            )
            # A PID reused by an unrelated process carries a different
            # starttime: the captured scope must exclude it, fail-closed.
            reused = frozenset(
                [
                    CapturedProcess(
                        pid=child_pid,
                        starttime=record.starttime + 1,
                        parent=record.parent,
                    )
                ]
            )
            self.assertNotIn(child_pid, live_scope(reused))
            self.assertFalse(
                lock_module._is_live_with_identity(child_pid, record.starttime + 1)
            )
        finally:
            if child.poll() is None:
                child.kill()
                child.wait(timeout=10)


class PinnedGroupTermination(LockConformanceCase):
    """Identity-pinned per-member group termination (F3, F4, Task 23).

    Bounded group termination never signals by a numeric group id
    (``killpg``): the numeric id is only a ``/proc`` scan key, every member
    is signaled by its own PID while its starttime identity matches, new
    descendants forked during the grace are captured by the repeated scan,
    and a foreign group that reuses the released numeric id is never
    signaled — deterministically simulated here with foreign groups and
    stale pins.
    """

    def _foreign_sleeper(self, marker: Path) -> subprocess.Popen:
        """A foreign process in its own session/group, writing its PID."""
        script = self.tmp / "foreign.py"
        script.write_text(
            "import os, sys, time\n"
            f"with open({str(marker)!r}, 'w') as s: s.write(str(os.getpid()))\n"
            "time.sleep(300)\n",
            encoding="utf-8",
        )
        return subprocess.Popen(
            [sys.executable, str(script)], cwd=self.tmp, start_new_session=True
        )

    def test_foreign_group_with_unpinned_numeric_id_is_never_signaled(self) -> None:
        """The PGID-reuse case, deterministic: a foreign group holds a numeric
        id for which nothing (or only stale identities) is pinned.  The
        pinned supervisor must never signal it."""
        marker = self.tmp / "foreign-unpinned.pid"
        foreign = self._foreign_sleeper(marker)
        try:
            foreign_pid = int(self.wait_for_marker(marker))
            self.assertEqual(foreign_pid, foreign.pid)
            self.assertEqual(foreign_pid, os.getpgid(foreign_pid))
            # Nothing pinned for this numeric id: refuse to signal (the
            # unpinned-id refusal is what makes the post-reap reuse safe).
            result = lock_module.terminate_pinned_group(
                foreign_pid, grace=0.1, reap_bound=1.0
            )
            self.assertEqual(result, {})
            self.assertTrue(
                self._pid_alive(foreign_pid),
                "a foreign group must survive an unpinned numeric id",
            )
            # Stale pins (identities from a long-gone group): the live
            # starttime mismatch refuses every per-PID signal.
            stale = {foreign_pid: lock_module._starttime_of(foreign_pid) + 1}
            result = lock_module.terminate_pinned_group(
                foreign_pid, grace=0.1, reap_bound=1.0, pinned=stale
            )
            self.assertEqual(result, {})
            self.assertTrue(
                self._pid_alive(foreign_pid),
                "a foreign member must survive stale identity pins",
            )
        finally:
            if foreign.poll() is None:
                foreign.kill()
                foreign.wait(timeout=10)

    def test_fork_during_termination_is_captured_and_cleaned(self) -> None:
        """A member that forks a new descendant into the group while the TERM
        grace is pending is captured by the repeated ``/proc`` scan and
        killed; no owned member survives the bounded termination."""
        marker = self.tmp / "leader.pid"
        child_marker = self.tmp / "forked-child.pid"
        script = self.tmp / "fork-during-term.py"
        script.write_text(
            "import os, signal, sys, time\n"
            "marker, child_marker = sys.argv[1], sys.argv[2]\n"
            "def handler(signum, frame):\n"
            "    pid = os.fork()\n"
            "    if pid == 0:\n"
            "        with open(child_marker, 'w') as f: f.write(str(os.getpid()))\n"
            "        time.sleep(300)\n"
            "        os._exit(0)\n"
            "signal.signal(signal.SIGTERM, handler)\n"
            "with open(marker, 'w') as f: f.write(str(os.getpid()))\n"
            "while True:\n"
            "    time.sleep(0.05)\n",
            encoding="utf-8",
        )
        leader = subprocess.Popen(
            [sys.executable, str(script), str(marker), str(child_marker)],
            cwd=self.tmp,
            start_new_session=True,
        )
        forked: int | None = None
        try:
            leader_pid = int(self.wait_for_marker(marker))
            self.assertEqual(leader_pid, leader.pid)
            lock_module.terminate_pinned_group(
                leader_pid, grace=1.0, reap_bound=5.0
            )
            leader.wait(timeout=10)
            self.assertFalse(self._pid_alive(leader_pid))
            self.assertFalse(
                lock_module._pgid_has_live_members(leader_pid),
                "a fork-during-termination member must be cleaned",
            )
            if child_marker.exists():
                forked = int(child_marker.read_text(encoding="utf-8").strip())
                # A SIGKILLed descendant may linger as an unreaped zombie
                # (reparented to init); liveness must exclude zombies exactly
                # like the supervision identity check.
                self.assertFalse(
                    lock_module._is_live_with_identity(
                        forked, lock_module._starttime_of(forked) or 0
                    ),
                    "the fork-during-termination descendant survived",
                )
        finally:
            if leader.poll() is None:
                leader.kill()
                leader.wait(timeout=10)
            if forked is not None:
                try:
                    os.kill(forked, signal.SIGKILL)
                except ProcessLookupError:
                    pass

    def test_terminate_pinned_group_clean_exit_reaps_group(self) -> None:
        """A normal TERM-cooperating group is terminated and verified gone."""
        marker = self.tmp / "coop.pid"
        script = self.tmp / "coop.py"
        script.write_text(
            "import os, signal, sys, time\n"
            "def handler(signum, frame): raise SystemExit(0)\n"
            "signal.signal(signal.SIGTERM, handler)\n"
            f"with open({str(marker)!r}, 'w') as s: s.write(str(os.getpid()))\n"
            "while True: time.sleep(0.05)\n",
            encoding="utf-8",
        )
        leader = subprocess.Popen(
            [sys.executable, str(script)], cwd=self.tmp, start_new_session=True
        )
        try:
            leader_pid = int(self.wait_for_marker(marker))
            lock_module.terminate_pinned_group(leader_pid, grace=1.0, reap_bound=5.0)
            leader.wait(timeout=10)
            self.assertFalse(self._pid_alive(leader_pid))
            self.assertFalse(lock_module._pgid_has_live_members(leader_pid))
        finally:
            if leader.poll() is None:
                leader.kill()
                leader.wait(timeout=10)


if __name__ == "__main__":
    unittest.main(verbosity=2)
