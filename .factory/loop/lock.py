#!/usr/bin/env python3
"""Root-descriptor lock and Git writer boundary (LOCK-01, GIT-01, PROC-01).

This module implements the writer-boundary authority of FACTORY-LOOP-SPEC
§12 (Task 5): exactly one trusted writer at a time, enforced by an exclusive
Linux ``flock`` on the *already-open canonical Git top-level directory
descriptor* itself (``O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC``) — never on a
replaceable lock-file pathname.

Guarantees provided here (the trusted control plane wires them at launch):

* :func:`acquire_root_lock` requires the *mandatory* canonical repository
  identity, required branch, and bound specification/plan commitments
  (:class:`SpecBinding` / :class:`PlanBinding`) at acquisition (finding F2):
  an acquisition without all four fails closed before any lock is granted,
  and every mismatch is validated *while the lock is held*, so a moved,
  rebased, or re-bound checkout fails closed before any launch.  A second
  concurrent launcher fails immediately with :class:`RootLockHeldError`;
  there is exactly one writer.
* Every Git read of the trusted holder (live branch, spec commit/blob, plan
  base/HEAD/blob) is *bound to the locked descriptor's inode* (F2): the
  pinned Git executable (``gitutil.GIT_EXECUTABLE``, never a PATH-derived
  ``git``) runs with ``-C /proc/self/fd/<anchor>`` where the anchor is a
  freshly opened descriptor resolved *through the lock descriptor itself*
  (``/proc/self/fd/<lock fd>``), so a rename/rebind of the canonical
  pathname cannot redirect a single Git read, and the canonical pathname is
  additionally re-verified against the locked (device, inode) before and
  after every read — pathname drift fails closed even when the reads stayed
  on the locked inode.
* The lock descriptor is ``O_CLOEXEC``, and :meth:`RootLock.spawn_child`
  runs every child with ``close_fds`` (the descriptor is never in
  ``pass_fds``), a *stripped* environment, and always in a **new process
  session**, so an untrusted leaf inherits neither the lock descriptor nor
  any lock metadata and cannot signal or observe the holder through a shared
  session.
* ``pass_fds`` aliases of the root lock inode are rejected before exec
  (F8): the lock descriptor itself, a ``dup`` of it (same open file
  description), and any separately opened descriptor naming the locked
  inode all fail closed — an untrusted leaf can never receive a handle that
  aliases the root.
* ``spawn_child(executable=…)`` (Task 12 MED1) separates the path the
  kernel opens from the bound ``argv``: the trusted holder runs the
  retained verifier descriptor as ``executable=/proc/self/fd/<fd>`` (with
  the same read-only descriptor in ``pass_fds``) so the child executes the
  bound inode at exec time.  The bound command argv is passed to the kernel
  verbatim, so every argument after the script path is preserved; for a
  shebang script the kernel's shebang dispatch passes the descriptor path
  ``/proc/self/fd/<fd>`` as the script argument, so the script's ``$0`` is
  the fd path — never the canonical repository path (F1).  The inherited
  verifier fd is intentionally the only extra descriptor the child
  receives (accepted read-only inheritance, F2); the root lock descriptor
  and every other trusted-holder descriptor are never inherited.
* A bounded child timeout kills and reaps the child's **entire new process
  group** (F3): TERM per-PID to the identity-pinned leader and every member
  of the group (the numeric group id is used only as a ``/proc`` scan key,
  never as a ``killpg`` target, so a group id reused by a foreign group
  after the leader is reaped can never signal that foreign group), then the
  *full* bounded grace is always observed (the leader exiting on TERM is
  never taken as “the group is gone” — a TERM-ignoring descendant that
  holds the pipe ends survives in the group), then KILL per-PID to every
  identity-matching member — including a descendant forked during the
  grace, which the repeated scan captures while a pinned member still
  lives — then a bounded verification that no live member survives, then a
  reaping wait for the leader; the trailing pipe collection is itself
  bounded and the pipe ends are force-closed when a stubborn member held
  them open, so a bounded spawn can never hang the holder and a descendant
  that stays in the group cannot outlive the bounded run.  The failure
  surfaces as
  :class:`RootLockTimeoutError` under the unified exception contract.
* Because ``flock`` locks are bound to the *open file description*, a
  separately opened repository descriptor can never unlock the holder:
  an attacker who re-opens the root and calls ``flock(..., LOCK_UN)`` only
  affects that unrelated description.
* :func:`capture_descendants` snapshots the *full bounded* descendant
  closure of a model process from the ``/proc/<pid>/stat`` parent table
  (F1) — bounded, and failing closed with :class:`RootLockUnsafeError` when
  the closure would exceed the bound rather than silently returning a stale
  snapshot.  Every captured PID records its **starttime and parent PID**
  from the same snapshot (:class:`CapturedProcess`), so the captured scope
  is reuse-safe: :func:`live_scope` re-enumerates only the still-live
  members of a captured scope whose recorded identity (start time) still
  matches the live process — a PID reused by an unrelated process is
  excluded and never returned as a live descendant (the detector-scope
  surface Task 6 supervision consumes so descendant accounting is never
  stale), and :func:`detect_escaped_descendants` /
  :func:`assert_no_escaped_descendants` fail closed when a double-fork or
  ``setsid`` descendant survives bounded termination or when any untrusted
  process still holds the repository-root/lock inode handle.  Recovery
  therefore cannot proceed — and the writer boundary cannot be reacquired —
  while an escaped descendant survives.
* Every lock/authority failure is routed through one unified fail-closed
  exception contract (F9): the public surface raises only subclasses of
  :class:`RootLockError` (``RootLockHeldError``, ``RootLockUnsafeError``,
  ``RootLockBindingError``, ``RootLockTimeoutError``,
  ``RootLockCommandError``, ``EscapedDescendantError``); the
  ``GitBoundaryError`` of the pinned Git runner — including the pinned-Git
  import-time resolution — is wrapped into the lock contract at the module
  edge, a nonzero child exit under ``check=True`` surfaces as
  :class:`RootLockCommandError`, and an ``OSError`` from the exclusive
  ``flock`` (other than the contended ``BlockingIOError``, which is
  :class:`RootLockHeldError`) surfaces as :class:`RootLockUnsafeError`.
* The child environment has every ``FACTORY_LOOP_LOCK_*`` key removed by
  prefix and every *legacy* ``FACTORY_LOCK_*`` key removed as well (F10), so
  a leaf of the new loop sees neither the new metadata nor the metadata of
  the previous lifecycle machinery.

All Git reads go through the PATH-pinned absolute executable in
``gitutil.py`` (GIT-01); an attacker-controlled ``PATH`` cannot substitute a
different ``git``.  The committed ``.factory/tools/git-commit-guard.sh`` commit
boundary is preserved and never bypassed by this module.
"""

from __future__ import annotations

import fcntl
import hashlib
import importlib.util
import os
from dataclasses import dataclass
from pathlib import Path
import re
import signal
import stat
import subprocess
import sys
import threading
import time
from typing import Dict, Iterable, Iterator, List, Mapping, Optional, Sequence, Tuple

# Lock metadata keys carried in the trusted holder's environment (and in the
# environment of any pre-exec helper that must *assert* the lock before
# exec).  Every key with the ``ENV_PREFIX`` prefix — plus every legacy
# ``FACTORY_LOCK_*`` key of the previous machinery — is stripped from every
# child environment before exec (F10); an untrusted leaf sees none of them.
ENV_PREFIX = "FACTORY_LOOP_LOCK_"
LEGACY_ENV_PREFIX = "FACTORY_LOCK_"
ENV_KEYS = (
    "FACTORY_LOOP_LOCK_HELD",
    "FACTORY_LOOP_LOCK_FD",
    "FACTORY_LOOP_LOCK_ID",
    "FACTORY_LOOP_LOCK_ROOT",
    "FACTORY_LOOP_LOCK_BRANCH",
)

HEX40_RE = re.compile(r"^[0-9a-f]{40}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")

_DIRECTORY_FLAGS = (
    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
)

# Bounded timeout machinery (F3): the grace between TERM and KILL of the
# child's whole new process group, the bound on verifying the group is gone
# and reaping the leader after the final KILL, and the bound on collecting
# the trailing pipe output.  All are bounded so a stubborn group — including
# a leader that exits on TERM while a TERM-ignoring descendant keeps the
# pipe write ends open — can never stall the holder indefinitely.
DEFAULT_KILL_GRACE = 1.0
REAP_TIMEOUT = 2.0
PIPE_COLLECT_TIMEOUT = 2.0

# Per-stream capture bound for :meth:`RootLock.spawn_child` (Task 11 review):
# the child's stdout/stderr are read *during* the run, bounded per stream, so
# a gate or acceptance command that floods its pipes can never blow up the
# holder's memory.  A stream that exceeds the bound is truncated; the child is
# never blocked (the reader keeps draining and discarding) and the returncode
# stays authoritative.
DEFAULT_CAPTURE_LIMIT = 1 << 20


class RootLockError(RuntimeError):
    """Base class for every fail-closed root-descriptor lock failure (F9)."""


class RootLockHeldError(RootLockError):
    """Another trusted writer already holds the exclusive root-descriptor lock."""


class RootLockUnsafeError(RootLockError):
    """The canonical root, lock descriptor, or child boundary is unsafe."""


class RootLockBindingError(RootLockError):
    """A mandatory repository/branch/spec/plan binding does not match or is missing."""


class RootLockTimeoutError(RootLockError):
    """A bounded child run timed out and its process group was terminated."""


class RootLockCommandError(RootLockError):
    """A child-boundary command exited nonzero while ``check=True`` (F9)."""

    def __init__(
        self,
        returncode: int,
        argv: Sequence[str],
        output: Optional[str] = None,
        stderr: Optional[str] = None,
    ) -> None:
        self.returncode = returncode
        self.argv = list(argv)
        self.output = output
        self.stderr = stderr
        super().__init__(f"child boundary {argv!r} exited with {returncode}")


class EscapedDescendantError(RootLockError):
    """A double-fork/setsid descendant survives; recovery must fail closed."""


@dataclass(frozen=True)
class _CaptureResult:
    """One bounded stream capture: the retained text, total bytes, truncation flag.

    ``text`` holds at most the configured per-stream limit (the *first*
    characters of the stream, matching the head-slice the deterministic
    gates consume), ``total`` counts every character the child wrote to the
    pipe, and ``truncated`` is True when the stream exceeded the limit (the
    extra output was drained and discarded, never retained, so the child is
    never blocked on a full pipe and the holder's memory stays bounded —
    Task 11 review).
    """

    text: str = ""
    total: int = 0
    truncated: bool = False


def _capture_stream_bounded(
    stream: object,
    limit: int,
    results: Dict[str, _CaptureResult],
    name: str,
) -> None:
    """Drain one text pipe, retaining at most ``limit`` chars (task seam).

    Runs in a daemon reader thread per stream.  The reader reads in bounded
    chunks, appends up to ``limit`` characters, then keeps draining and
    discarding so a flood never blocks the child on a full pipe and never
    grows the retained memory.  ``results[name]`` is published exactly once.
    Any read error (EOF, closed pipe, decoding failure) ends the reader;
    the exit status of the child is authoritative regardless.
    """
    captured = []
    total = 0
    kept = 0
    truncated = False
    try:
        while True:
            chunk = stream.read(65536)  # type: ignore[attr-defined]
            if not chunk:
                break
            total += len(chunk)
            if kept < limit:
                take = min(limit - kept, len(chunk))
                captured.append(chunk[:take])
                kept += take
                if take < len(chunk):
                    truncated = True
            else:
                truncated = True
    except (OSError, ValueError):
        pass
    finally:
        try:
            stream.close()  # type: ignore[attr-defined]
        except OSError:
            pass
    results[name] = _CaptureResult("".join(captured), total, truncated)


def require_linux_primitives() -> None:
    """Fail closed when any no-follow/dirfd/flock primitive is unavailable."""
    required = ("O_NOFOLLOW", "O_DIRECTORY", "O_CLOEXEC")
    missing = [name for name in required if not hasattr(os, name)]
    if (
        sys.platform != "linux"
        or missing
        or not Path("/proc/self/fd").is_dir()
        or not hasattr(fcntl, "flock")
    ):
        detail = ", ".join(missing) if missing else "flock/proc"
        raise RootLockUnsafeError(
            f"required Linux no-follow/flock primitives are unavailable: {detail}"
        )


def _load_gitutil() -> object:
    """Load the pinned-Git runner by committed file path (see ``gitutil``).

    The import-time resolution failure of the pinned Git executable
    (``GitBoundaryError``) is routed into the unified lock contract as
    :class:`RootLockUnsafeError` (F9), so no caller of this module ever sees
    a bare ``GitBoundaryError`` escape the boundary.
    """
    path = Path(__file__).resolve().parent / "gitutil.py"
    spec = importlib.util.spec_from_file_location("gitutil", path)
    if spec is None or spec.loader is None:
        raise RootLockUnsafeError(f"cannot load PATH-pinned Git runner at {path}")
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except Exception as exc:
        raise RootLockUnsafeError(
            f"cannot initialize the PATH-pinned Git runner at {path}: {exc}"
        ) from exc
    return module


_git = _load_gitutil()
git_run = _git.git_run
git_bytes = _git.git_bytes
GitBoundaryError = _git.GitBoundaryError


def stripped_child_env(env: Optional[Mapping[str, str]] = None) -> Dict[str, str]:
    """Return ``env`` (default ``os.environ``) with every lock-metadata key removed.

    The strip is *prefix-based* (F10): every ``FACTORY_LOOP_LOCK_*`` key of
    the current holder *and* every legacy ``FACTORY_LOCK_*`` key of the
    previous lifecycle machinery is removed, so no child of the trusted
    holder ever sees the lock descriptor, its identity, the canonical root,
    or any historical alias of that metadata (FACTORY-LOOP-SPEC §12).
    """
    environment = dict(os.environ if env is None else env)
    for key in list(environment):
        if key.startswith(ENV_PREFIX) or key.startswith(LEGACY_ENV_PREFIX):
            environment.pop(key, None)
    return environment


def lock_environment(
    root: Path, identity: str, branch: str, fd: Optional[int] = None
) -> Dict[str, str]:
    """Trusted holder bookkeeping about the held lock (stripped before exec)."""
    return {
        "FACTORY_LOOP_LOCK_HELD": "1",
        "FACTORY_LOOP_LOCK_FD": "" if fd is None else str(fd),
        "FACTORY_LOOP_LOCK_ID": identity,
        "FACTORY_LOOP_LOCK_ROOT": str(root),
        "FACTORY_LOOP_LOCK_BRANCH": branch,
    }


def _safe_repo_relative(path: str) -> None:
    """Reject absolute, empty, dot-segment, and ``..`` binding paths."""
    if not path or path.startswith("/"):
        raise RootLockBindingError(
            "binding path must be non-empty and repository-relative"
        )
    if any(segment in ("", ".", "..") for segment in path.split("/")):
        raise RootLockBindingError(
            "binding path must not contain empty, `.`, or `..` segments"
        )
    if len(path) > 1024:
        raise RootLockBindingError("binding path exceeds 1024 bytes")


@dataclass(frozen=True)
class SpecBinding:
    """Bound canonical specification (repository-relative path, commit, blob).

    The values come from the committed ``factory-plan/v1`` front matter
    (``spec_path``/``spec_commit``/``spec_blob``) and are verified against
    the canonical repository *while the lock is held*, through the pinned
    Git executable anchored to the locked inode.
    """

    path: str
    commit: str
    blob: str

    def validate(self) -> None:
        _safe_repo_relative(self.path)
        if not HEX40_RE.fullmatch(self.commit):
            raise RootLockBindingError(
                "spec commit must be a 40-character Git commit hash"
            )
        if not HEX40_RE.fullmatch(self.blob):
            raise RootLockBindingError(
                "spec blob must be a 40-character Git blob hash"
            )


@dataclass(frozen=True)
class PlanBinding:
    """Bound canonical plan (path, base commit, SHA-256 digest of plan bytes)."""

    path: str
    base_commit: str
    digest: str

    def validate(self) -> None:
        _safe_repo_relative(self.path)
        if not HEX40_RE.fullmatch(self.base_commit):
            raise RootLockBindingError(
                "plan base commit must be a 40-character Git commit hash"
            )
        if not SHA256_RE.fullmatch(self.digest):
            raise RootLockBindingError(
                "plan digest must be a 64-character lowercase SHA-256 hex digest"
            )


def probe_root_lock(root: Path) -> bool:
    """Report whether a fresh canonical descriptor can take the lock.

    ``True`` means the writer boundary is free; ``False`` means another open
    description currently holds the exclusive flock.  The probe opens its
    own descriptor (``O_DIRECTORY|O_NOFOLLOW|O_CLOEXEC``) and immediately
    releases any lock it takes, so it never interferes with a holder.
    """
    require_linux_primitives()
    root = Path(root).absolute()
    try:
        descriptor = os.open(root, _DIRECTORY_FLAGS)
    except OSError as exc:
        raise RootLockUnsafeError(
            f"cannot open the canonical root no-follow: {exc}"
        ) from exc
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISDIR(info.st_mode):
            raise RootLockUnsafeError(f"canonical root is not a directory: {root}")
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return False
        except OSError as exc:
            raise RootLockUnsafeError(
                f"cannot probe the exclusive root-descriptor flock: {exc}"
            ) from exc
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        return True
    finally:
        os.close(descriptor)


class RootLock:
    """Exclusive root-descriptor lock with mandatory pre-launch bindings.

    Acquisition opens the canonical Git top-level directory with
    ``O_DIRECTORY|O_NOFOLLOW|O_CLOEXEC`` and takes an exclusive non-blocking
    ``flock`` on that open description.  A second concurrent launcher probe
    (fresh descriptor) therefore fails with :class:`RootLockHeldError`
    immediately: exactly one writer.

    The acquisition is *mandatory* (F2): canonical repository identity, the
    required branch, and the bound specification/plan commitments must all
    be provided, and are validated while the lock is held.  Every Git read
    is anchored to the locked descriptor inode (a fresh descriptor opened
    through ``/proc/self/fd/<lock fd>``), so a canonical-path rebind can
    never redirect a read, and the pathname is re-verified against the
    locked (device, inode) before and after each read: pathname drift fails
    closed even though the reads stayed on the locked inode.
    """

    def __init__(
        self,
        root: Path,
        *,
        expected_identity: Optional[str] = None,
        expected_branch: Optional[str] = None,
        spec: Optional[SpecBinding] = None,
        plan: Optional[PlanBinding] = None,
    ) -> None:
        require_linux_primitives()
        for name, value in (
            ("expected_identity", expected_identity),
            ("expected_branch", expected_branch),
            ("spec", spec),
            ("plan", plan),
        ):
            if value is None:
                raise RootLockBindingError(
                    f"the {name} binding is mandatory at lock acquisition; "
                    "refusing to acquire the writer boundary without it (F2)"
                )
        self._root = Path(root).absolute()
        self._expected_identity = expected_identity
        self._expected_branch = expected_branch
        self._spec = spec
        self._plan = plan
        self._fd = -1
        self._identity: Optional[str] = None
        self._branch: Optional[str] = None
        self._branch_resolved = False
        self._closed = False
        self._locked = False
        try:
            descriptor = os.open(self._root, _DIRECTORY_FLAGS)
        except OSError as exc:
            raise RootLockUnsafeError(
                f"cannot open the canonical root no-follow: {exc}"
            ) from exc
        try:
            info = os.fstat(descriptor)
            if not stat.S_ISDIR(info.st_mode):
                raise RootLockUnsafeError(
                    f"canonical root is not a directory: {self._root}"
                )
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise RootLockHeldError(
                    f"another writer holds the canonical root-descriptor lock: "
                    f"{self._root}"
                ) from exc
            except OSError as exc:
                raise RootLockUnsafeError(
                    f"cannot take the exclusive root-descriptor flock on "
                    f"{self._root}: {exc}"
                ) from exc
            self._fd = descriptor
            self._locked = True
            self._identity = f"{info.st_dev:x}:{info.st_ino:x}"
            try:
                # Bind the descriptor to its canonical path (device+inode
                # match) and validate every mandatory binding while the
                # lock is held (F2).
                self._assert_anchored()
                self.validate_bindings()
            except BaseException:
                self.release()
                raise
        except BaseException:
            if descriptor >= 0 and not self._closed:
                os.close(descriptor)
            raise

    # -- identity and environment -------------------------------------------

    @property
    def root(self) -> Path:
        return self._root

    @property
    def fd(self) -> int:
        if self._closed:
            raise RootLockUnsafeError("the root-descriptor lock is released")
        return self._fd

    @property
    def identity(self) -> str:
        if self._identity is None:
            raise RootLockUnsafeError("the root-descriptor lock is released")
        return self._identity

    @property
    def branch(self) -> str:
        """Live branch of the canonical root, resolved through the pinned Git.

        Resolution goes through the descriptor-anchored pinned Git
        executable and is cached; a checkout that is not a Git repository (or
        whose HEAD is unborn) fails closed.
        """
        if not self._branch_resolved:
            result = self._git_run(["rev-parse", "--abbrev-ref", "HEAD"])
            if result.returncode != 0:
                raise RootLockUnsafeError(
                    f"cannot resolve the live Git branch of {self._root}"
                )
            branch = result.stdout.strip()
            if not branch or branch == "HEAD":
                raise RootLockUnsafeError(
                    f"live Git branch of {self._root} is not a named branch"
                )
            self._branch = branch
            self._branch_resolved = True
        if self._branch is None:
            raise RootLockUnsafeError("live branch was not resolved")
        return self._branch

    def validate_live_branch(
        self, expected: str, *, timeout: Optional[float] = _git.GIT_TIMEOUT,
    ) -> str:
        """Re-read the named branch through the held descriptor authority.

        The caller may shorten the default bound to the remaining campaign
        deadline; an unbounded branch hook is never permitted.
        """
        self._require_locked()
        if (
            not isinstance(timeout, (int, float))
            or isinstance(timeout, bool)
            or timeout <= 0
            or timeout != timeout
            or timeout == float("inf")
        ):
            raise RootLockUnsafeError("live branch timeout must be finite and positive")
        result = self._git_run(
            ["rev-parse", "--abbrev-ref", "HEAD"], timeout=float(timeout)
        )
        if result.returncode != 0:
            raise RootLockUnsafeError("cannot revalidate the live Git branch")
        branch = result.stdout.strip()
        if not branch or branch == "HEAD" or branch != expected:
            raise RootLockBindingError(
                f"live branch {branch!r} does not match required branch {expected!r}"
            )
        return branch

    def lock_metadata(self, *, fd: bool = True) -> Dict[str, str]:
        """The trusted holder's lock-metadata environment (stripped in children)."""
        return lock_environment(
            self._root, self.identity, self.branch,
            fd=self._fd if fd else None,
        )

    # -- descriptor-anchored Git reads (F2) -----------------------------------

    def _require_locked(self) -> None:
        if self._closed or not self._locked:
            raise RootLockUnsafeError(
                "operation requires a held root-descriptor lock"
            )

    def _assert_anchored(self) -> None:
        """Fail closed unless the canonical pathname still names the locked inode.

        The descriptor is the authority; the pathname must *also* name it,
        so a canonical-path rebind (rename + symlink swap) is detected even
        though the anchored Git reads themselves can never be redirected.
        """
        self._require_locked()
        locked = os.fstat(self._fd)
        try:
            info = os.stat(self._root, follow_symlinks=False)
        except OSError as exc:
            raise RootLockUnsafeError(
                f"canonical root pathname {self._root} no longer resolves: {exc}"
            ) from exc
        if (info.st_dev, info.st_ino) != (locked.st_dev, locked.st_ino):
            raise RootLockUnsafeError(
                f"canonical root pathname {self._root} no longer names the "
                f"locked descriptor inode ({locked.st_dev:x}:{locked.st_ino:x})"
            )

    def _anchor_descriptor(self) -> int:
        """Open a fresh descriptor resolved *through* the locked descriptor.

        ``/proc/self/fd/<lock fd>`` pins the locked inode, so opening it
        yields a new open file description bound to the same inode that a
        canonical-path rebind cannot redirect.  Git runs with this anchor as
        its ``-C`` target; the locked open file description itself never
        leaves this process (F8).
        """
        self._require_locked()
        try:
            return os.open(
                f"/proc/self/fd/{self._fd}",
                os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_CLOEXEC", 0),
            )
        except OSError as exc:
            raise RootLockUnsafeError(
                f"cannot anchor a Git descriptor to the locked inode: {exc}"
            ) from exc

    def _git_run(self, argv: Sequence[str], *, timeout: Optional[float] = None):
        """Run the pinned Git anchored to the locked inode (text output)."""
        self._assert_anchored()
        anchor = self._anchor_descriptor()
        try:
            result = git_run(
                ["-C", f"/proc/self/fd/{anchor}", *argv],
                timeout=timeout,
                pass_fds=[anchor],
            )
        except GitBoundaryError as exc:
            raise RootLockUnsafeError(
                f"pinned Git invocation failed behind the lock: {exc}"
            ) from exc
        finally:
            os.close(anchor)
        self._assert_anchored()
        return result

    def _git_bytes(self, argv: Sequence[str], *, timeout: Optional[float] = None):
        """Run the pinned Git anchored to the locked inode (byte output)."""
        self._assert_anchored()
        anchor = self._anchor_descriptor()
        try:
            result = git_bytes(
                ["-C", f"/proc/self/fd/{anchor}", *argv],
                timeout=timeout,
                pass_fds=[anchor],
            )
        except GitBoundaryError as exc:
            raise RootLockUnsafeError(
                f"pinned Git invocation failed: {exc}"
            ) from exc
        finally:
            os.close(anchor)
        self._assert_anchored()
        return result

    # -- binding validation -----------------------------------------------------

    def validate_bindings(self) -> None:
        """Validate every mandatory binding while the lock is held (F2).

        Repository identity, the required branch, and the spec/plan
        commitments are all mandatory at acquisition and are matched against
        the *anchored* repository; any mismatch raises
        :class:`RootLockBindingError` and the lock stays held (the caller
        releases it).  Because every Git read is descriptor-anchored, a
        canonical-path rebind can neither satisfy nor redirect a binding.
        """
        self._require_locked()
        if self._expected_identity != self.identity:
            raise RootLockBindingError(
                f"repository identity {self.identity!r} does not match the "
                f"required identity {self._expected_identity!r}"
            )
        branch = self.branch
        if branch != self._expected_branch:
            raise RootLockBindingError(
                f"live branch {branch!r} does not match the required branch "
                f"{self._expected_branch!r}"
            )
        self._validate_spec_binding(self._spec)
        self._validate_plan_binding(self._plan)

    def _validate_spec_binding(self, spec: SpecBinding) -> None:
        spec.validate()
        commit = f"{spec.commit}^{{commit}}"
        check = self._git_run(["cat-file", "-e", commit])
        if check.returncode != 0:
            raise RootLockBindingError(
                f"spec commit {spec.commit!r} is not an object in the "
                f"canonical repository"
            )
        blob = self._git_run(["rev-parse", f"{spec.commit}:{spec.path}"])
        if blob.returncode != 0:
            raise RootLockBindingError(
                f"spec path {spec.path!r} is not committed at {spec.commit}"
            )
        resolved = blob.stdout.strip()
        if resolved != spec.blob:
            raise RootLockBindingError(
                f"spec blob binding mismatch: committed {resolved!r} != "
                f"required {spec.blob!r} at {spec.commit}:{spec.path}"
            )

    def _validate_plan_binding(self, plan: PlanBinding) -> None:
        plan.validate()
        check = self._git_run(["cat-file", "-e", f"{plan.base_commit}^{{commit}}"])
        if check.returncode != 0:
            raise RootLockBindingError(
                f"plan base commit {plan.base_commit!r} is not an object in "
                f"the canonical repository"
            )
        head = self._git_run(["rev-parse", "--verify", "HEAD"])
        if head.returncode != 0:
            raise RootLockBindingError("cannot resolve HEAD of the canonical repository")
        if head.stdout.strip() != plan.base_commit:
            raise RootLockBindingError(
                f"checkout HEAD {head.stdout.strip()!r} does not match the "
                f"plan base commit {plan.base_commit!r}"
            )
        blob = self._git_run(["rev-parse", f"{plan.base_commit}:{plan.path}"])
        if blob.returncode != 0:
            raise RootLockBindingError(
                f"plan path {plan.path!r} is not committed at {plan.base_commit}"
            )
        raw = self._git_bytes(["cat-file", "blob", blob.stdout.strip()])
        if raw.returncode != 0:
            raise RootLockBindingError("cannot read the committed plan blob")
        digest = hashlib.sha256(raw.stdout).hexdigest()
        if digest != plan.digest:
            raise RootLockBindingError(
                f"plan digest binding mismatch: committed {digest!r} != "
                f"required {plan.digest!r}"
            )

    # -- child boundary --------------------------------------------------------

    def _assert_no_root_aliases(self, pass_fds: Sequence[int]) -> None:
        """Reject any passed fd that aliases the root lock OFD/inode (F8).

        The check covers the lock descriptor itself, a ``dup`` of it (the
        same open file description), and any separately opened descriptor
        naming the locked inode: each is compared by (device, inode) against
        the locked root, and each is refused before exec.
        """
        self._require_locked()
        locked = os.fstat(self._fd)
        for fd in tuple(pass_fds):
            if not isinstance(fd, int) or fd < 0:
                raise RootLockUnsafeError(
                    f"pass_fds entry {fd!r} is not a valid descriptor number"
                )
            if fd == self._fd:
                raise RootLockUnsafeError(
                    "refusing to pass the canonical lock descriptor into a child"
                )
            try:
                info = os.stat(f"/proc/self/fd/{fd}")
            except OSError as exc:
                raise RootLockUnsafeError(
                    f"pass_fds entry {fd} is not an open descriptor: {exc}"
                ) from exc
            if (info.st_dev, info.st_ino) == (locked.st_dev, locked.st_ino):
                raise RootLockUnsafeError(
                    f"refusing to pass fd {fd}: it aliases the canonical "
                    f"root lock inode ({locked.st_dev:x}:{locked.st_ino:x})"
                )

    def _kill_process_group(self, process, grace: float) -> None:
        """TERM then KILL the child's whole new process group and reap it (F3).

        The child was started in a new session/group, so its group is
        identified by the leader PID.  Termination is **identity-pinned per
        member** (see :func:`terminate_pinned_group`): the numeric group id
        is used only as a ``/proc`` scan key and is **never** passed to
        ``killpg``, so a numeric id reused by a foreign group after the
        leader is reaped can never signal that foreign group.  TERM is
        delivered per-PID to the pinned leader and every identity-matching
        member and the *full* bounded grace is always observed before the
        KILL — the leader exiting on TERM is never taken as “the group is
        gone”, because a descendant that ignores TERM and keeps the pipe
        write ends open survives in the group.  After the grace every
        identity-matching member (including a descendant forked during the
        grace) is KILLed per-PID, the group is then verified to have no live
        pinned member within a bounded window, and the leader is reaped with
        a bounded wait.  A group that cannot be drained within the bounds
        raises :class:`RootLockUnsafeError` instead of ever hanging the
        holder.
        """
        pid = process.pid
        terminate_pinned_group(pid, grace=grace, reap_bound=REAP_TIMEOUT)
        # Bounded reap of the group leader (poll reaps; a live leader raises).
        if process.poll() is None:
            try:
                process.wait(timeout=REAP_TIMEOUT)
            except subprocess.TimeoutExpired as exc:
                raise RootLockUnsafeError(
                    "the bounded process-group leader was not reaped within "
                    f"the {REAP_TIMEOUT:.1f}s bound"
                ) from exc

    def spawn_child(
        self,
        argv: Sequence[str],
        *,
        cwd: Optional[Path] = None,
        env: Optional[Mapping[str, str]] = None,
        pass_fds: Sequence[int] = (),
        executable: Optional[str] = None,
        timeout: Optional[float] = None,
        kill_grace: float = DEFAULT_KILL_GRACE,
        check: bool = False,
        stdout_limit: int = DEFAULT_CAPTURE_LIMIT,
        stderr_limit: int = DEFAULT_CAPTURE_LIMIT,
    ) -> subprocess.CompletedProcess[str]:
        """Run ``argv`` behind the lock boundary.

        The child always starts in a **new process session** and its own
        process group; it inherits none of the lock descriptor
        (``close_fds=True``; the descriptor is never in ``pass_fds`` and no
        passed/inherited fd may alias the root lock inode — F8), and it
        receives an environment with every ``FACTORY_LOOP_LOCK_*`` and
        legacy ``FACTORY_LOCK_*`` key stripped (F10).  A bounded ``timeout``
        terminates and reaps the child's *entire* new process group (TERM,
        full bounded grace, KILL, bounded group-gone verification, reaped
        leader) and surfaces as :class:`RootLockTimeoutError`; the trailing
        pipe collection after the group termination is itself bounded and
        the pipe ends are force-closed when a stubborn member held them
        open, so a bounded spawn can never hang the holder.  An untrusted
        leaf therefore can neither inherit the lock nor unlock it, cannot
        see lock metadata, and cannot outlive a bounded run
        (FACTORY-LOOP-SPEC §12, §9).

        **Bounded during-read capture** (Task 11 review): the child's
        stdout/stderr are drained by bounded reader threads *while the run
        proceeds*, never collected wholesale after ``communicate``.  Each
        stream is capped to its own ``*_limit`` (default
        :data:`DEFAULT_CAPTURE_LIMIT`), the reader keeps draining the pipe
        past the cap so the child is never blocked on a full pipe, the
        captured text is the most recent tail within the cap, and the
        stream's total byte count and truncated flag are reported through
        :data:`_CaptureResult`.  A child that floods its pipes therefore
        cannot grow the holder's memory without bound.
        """
        if self._closed or not self._locked:
            raise RootLockUnsafeError(
                "cannot spawn a child behind a released root-descriptor lock"
            )
        self._assert_anchored()
        self._assert_no_root_aliases(pass_fds)
        # Task 22 residual: a standard descriptor (0/1/2) can never be a
        # retained helper descriptor — ``close_fds=True`` always preserves
        # the standard streams, so a ``pass_fds`` entry <= 2 is always a
        # mistake.  Reject it explicitly instead of silently dropping it
        # (a caller that believes the descriptor reached the child would be
        # wrong about the executed boundary).
        for fd in tuple(pass_fds):
            if fd <= 2:
                raise RootLockUnsafeError(
                    f"refusing to pass the standard descriptor {fd} into a "
                    "child; pass_fds must name a retained helper descriptor "
                    "> 2 (close_fds=True always preserves the standard "
                    "streams)"
                )
        environment = stripped_child_env(env)
        try:
            process = subprocess.Popen(
                argv,
                executable=executable,
                cwd=str(cwd or self._root),
                env=environment,
                start_new_session=True,
                close_fds=True,
                pass_fds=tuple(pass_fds),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
        except (OSError, ValueError) as exc:
            raise RootLockUnsafeError(
                f"cannot spawn child boundary {argv!r}: {exc}"
            ) from exc
        captures: Dict[str, _CaptureResult] = {}
        readers = []
        for name, stream, limit in (
            ("stdout", process.stdout, stdout_limit),
            ("stderr", process.stderr, stderr_limit),
        ):
            if stream is None:
                continue
            thread = threading.Thread(
                target=_capture_stream_bounded,
                args=(stream, limit, captures, name),
                name=f"factory-lock-{name}",
                daemon=True,
            )
            thread.start()
            readers.append(thread)
        timed_out = False
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            if process.poll() is not None and all(
                not reader.is_alive() for reader in readers
            ):
                break
            if deadline is not None and time.monotonic() >= deadline:
                timed_out = True
                break
            time.sleep(0.02)
        if timed_out:
            self._kill_process_group(process, kill_grace)
        # Bounded trailing pipe collection: a group member that ignored the
        # group termination may have held the pipe write ends open, so the
        # reader joins are themselves bounded and the pipe ends are
        # force-closed afterwards — a bounded spawn can never hang the
        # holder on a pipe held by a survivor.
        for reader in readers:
            reader.join(timeout=PIPE_COLLECT_TIMEOUT)
        for stream in (process.stdout, process.stderr):
            if stream is not None:
                try:
                    stream.close()
                except OSError:
                    pass
        for reader in readers:
            reader.join(timeout=PIPE_COLLECT_TIMEOUT)
        if timed_out:
            raise RootLockTimeoutError(
                f"child boundary {argv!r} exceeded its bounded timeout "
                f"({timeout}s); the whole process group was terminated "
                "and reaped"
            )
        stdout = captures.get("stdout", _CaptureResult("", 0, False))
        stderr = captures.get("stderr", _CaptureResult("", 0, False))
        if check and process.returncode != 0:
            raise RootLockCommandError(
                process.returncode, argv, output=stdout.text, stderr=stderr.text
            )
        return subprocess.CompletedProcess(
            argv, process.returncode, stdout.text, stderr.text
        )

    # -- release ---------------------------------------------------------------

    def release(self) -> None:
        """Unlock and close the root-descriptor lock (idempotent)."""
        if self._locked:
            try:
                fcntl.flock(self._fd, fcntl.LOCK_UN)
            except (OSError, ValueError):
                pass
            self._locked = False
        if not self._closed and self._fd >= 0:
            try:
                os.close(self._fd)
            except OSError:
                pass
            self._closed = True
            self._fd = -1

    def __enter__(self) -> "RootLock":
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.release()

    def __del__(self) -> None:
        try:
            self.release()
        except Exception:
            pass


def acquire_root_lock(
    root: Path,
    *,
    expected_identity: Optional[str] = None,
    expected_branch: Optional[str] = None,
    spec: Optional[SpecBinding] = None,
    plan: Optional[PlanBinding] = None,
) -> RootLock:
    """Acquire the exclusive root-descriptor lock with the mandatory bindings.

    All four acquisition bindings are mandatory (F2): canonical repository
    identity, required branch, and the bound specification/plan commitments
    from the committed ``factory-plan/v1`` front matter.  Concurrent
    launcher probes: only one acquisition succeeds; every other probe fails
    immediately with :class:`RootLockHeldError`.  Bindings are validated
    while the lock is held, so a stale, rebased, or rebranded checkout fails
    closed before launch.
    """
    return RootLock(
        root,
        expected_identity=expected_identity,
        expected_branch=expected_branch,
        spec=spec,
        plan=plan,
    )


# -- escaped-descendant detection (PROC-01) -------------------------------------

def _iter_pids() -> Iterator[int]:
    try:
        entries = os.listdir("/proc")
    except OSError:
        return
    for entry in entries:
        if entry.isdigit():
            yield int(entry)


def _proc_stat_fields(pid: int) -> Optional[List[str]]:
    """Tokens of ``/proc/<pid>/stat`` after the closing ``)`` of the comm.

    The comm field is the last token before the closing parenthesis, so a
    process name that itself contains ``)`` is handled by searching the
    *last* ``)``.  Field indices of the returned list (0-based) map to the
    ``proc(5)`` stat fields as: 0 → state (3), 1 → ppid (4), 2 → pgrp (5),
    and 19 → starttime (22).  ``None`` when the process is gone or the stat
    is malformed.
    """
    try:
        with open(
            f"/proc/{pid}/stat", "r", encoding="ascii", errors="replace"
        ) as stream:
            raw = stream.read()
    except OSError:
        return None
    end = raw.rfind(")")
    if end < 0:
        return None
    fields = raw[end + 1:].split()
    return fields or None


def _ppid_of(pid: int) -> Optional[int]:
    """Parent PID of ``pid`` from ``/proc/<pid>/stat`` (``None`` when gone)."""
    fields = _proc_stat_fields(pid)
    if fields is None or len(fields) < 2:
        return None
    try:
        return int(fields[1])
    except ValueError:
        return None


@dataclass(frozen=True)
class CapturedProcess:
    """One captured descendant: PID plus the identity that pins it.

    ``starttime`` is the process start time from ``/proc/<pid>/stat``
    (field 22, in clock ticks since boot) and ``parent`` is the parent PID
    observed in the same snapshot.  Together with the PID they identify a
    *specific* process across PID reuse: :func:`live_scope` and the escape
    detector require the live process at the captured PID to still carry
    the captured starttime, so a PID reused by an unrelated process is
    never reported as a live descendant of the captured scope.
    """

    pid: int
    starttime: int
    parent: int


def _is_live_with_identity(pid: int, starttime: int) -> bool:
    """True when ``pid`` is alive, not a zombie, and still has ``starttime``.

    The existence, state, and starttime checks are read from the *same*
    ``/proc/<pid>/stat`` snapshot, so a between-read PID reuse race cannot
    make an unrelated process look like the captured descendant.
    """
    fields = _proc_stat_fields(pid)
    if fields is None or len(fields) < 20 or fields[0] == "Z":
        return False
    try:
        return int(fields[19]) == starttime
    except ValueError:
        return False


def _pgid_has_live_members(pgid: int) -> bool:
    """True when any live (non-zombie) process still belongs to ``pgid``.

    The check reads the pgrp (field 5, token 2 after the comm) of every
    process from its own ``/proc/<pid>/stat`` entry, so a group counts as
    gone only when no non-zombie member remains — a zombie holds no pipe
    ends, no descriptors, and no lock reference and is reaped at the kernel
    level.  This is a read-only probe: the numeric ``pgid`` is used only to
    scan ``/proc`` and is never used as a signal target.
    """
    for pid in _iter_pids():
        fields = _proc_stat_fields(pid)
        if fields is None or len(fields) < 3 or fields[0] == "Z":
            continue
        try:
            if int(fields[2]) == pgid:
                return True
        except ValueError:
            continue
    return False


def _starttime_of(pid: int) -> Optional[int]:
    """Starttime (proc stat field 22) of ``pid``, or ``None`` when unknown.

    The starttime is the process identity that survives PID reuse: a PID
    recycled by an unrelated process carries a different starttime.
    """
    fields = _proc_stat_fields(pid)
    if fields is None or len(fields) < 20:
        return None
    try:
        return int(fields[19])
    except ValueError:
        return None


def _pgid_members_identity(pgid: int) -> Dict[int, int]:
    """Every live non-zombie member of ``pgid``: ``{pid: starttime}``.

    This is the **only** way a bounded supervisor enumerates group members
    for signaling: the numeric group id is a ``/proc`` scan key and is never
    passed to ``killpg``.  A group whose leader was reaped and whose numeric
    id was then reused by a foreign group enumerates the foreign group's
    members here, but those members carry starttimes that never match the
    bounded run's pinned identities, so they are never signaled.
    """
    members: Dict[int, int] = {}
    for pid in _iter_pids():
        fields = _proc_stat_fields(pid)
        if fields is None or len(fields) < 3 or fields[0] == "Z":
            continue
        try:
            if int(fields[2]) != pgid:
                continue
        except ValueError:
            continue
        starttime = _starttime_of(pid)
        if starttime is not None:
            members[pid] = starttime
    return members


def _signal_pid_pinned(pid: int, starttime: int, signum: int) -> bool:
    """Signal one exact process through a starttime-revalidated pidfd.

    A ``/proc`` identity check followed by ``os.kill(pid, ...)`` still has a
    PID-reuse window.  Opening a pidfd first pins the kernel process object;
    the starttime is then re-read and must still match before delivery through
    ``pidfd_send_signal``.  If pidfd signaling is unavailable for a live
    identity, cleanup fails closed instead of falling back to a numeric PID.
    Returns ``True`` only when the signal was delivered to the pinned object.
    """
    if not _is_live_with_identity(pid, starttime):
        return False
    pidfd_open = getattr(os, "pidfd_open", None)
    pidfd_send_signal = getattr(signal, "pidfd_send_signal", None)
    if not callable(pidfd_open) or not callable(pidfd_send_signal):
        raise RootLockUnsafeError(
            "identity-safe pidfd signaling is unavailable for live bounded "
            f"process {pid}; refusing numeric os.kill"
        )
    try:
        descriptor = pidfd_open(pid, 0)
    except ProcessLookupError:
        return False
    except OSError as exc:
        raise RootLockUnsafeError(
            f"cannot pidfd-pin bounded process {pid}: {exc}"
        ) from exc
    try:
        # The numeric PID may have been recycled before pidfd_open.  The pidfd
        # now pins whichever object was opened, so authorize delivery only
        # after the current /proc identity still matches the original pin.
        if not _is_live_with_identity(pid, starttime):
            return False
        try:
            pidfd_send_signal(descriptor, signum, None, 0)
        except ProcessLookupError:
            return False
        except OSError as exc:
            raise RootLockUnsafeError(
                f"cannot pidfd-signal bounded process {pid}: {exc}"
            ) from exc
        return True
    finally:
        try:
            os.close(descriptor)
        except OSError:
            pass


def _signal_group_pinned(
    pgid: int, signum: int, pinned: Mapping[int, int]
) -> Dict[int, int]:
    """Signal the identity-pinned members of ``pgid`` per-PID (leader first).

    The live group is enumerated by ``/proc`` scan; a member is signaled by
    its own PID only when its live starttime exactly matches the pinned
    identity (the leader — the PID that names the group — first).  A member
    whose PID was reused, or whose identity was never pinned, is never
    signaled.  Returns the members actually signaled.
    """
    signaled: Dict[int, int] = {}
    members = _pgid_members_identity(pgid)
    order = sorted(members)
    if pgid in order:
        order.remove(pgid)
        order.insert(0, pgid)
    for pid in order:
        pinned_st = pinned.get(pid)
        if pinned_st is None or members.get(pid) != pinned_st:
            continue
        if _signal_pid_pinned(pid, pinned_st, signum):
            signaled[pid] = pinned_st
    return signaled


def _ancestry_reaches(pid: int, candidates: Iterable[int], depth: int = 32) -> bool:
    """True when ``pid``'s ppid chain reaches any ``candidates`` PID.

    Used to classify a member that appeared in the group only after
    termination began: its parent chain (still visible in ``/proc`` while
    the parent is un-reaped) must reach a known pinned member of the bounded
    run, so a fork-during-termination descendant is recognized as owned while
    a foreign process that merely reuses the released numeric group id is
    not.  ``pid`` itself is never its own ancestor — a member whose own PID
    merely matches a stale pin (the PGID/PID reuse case) is never classified
    as owned by that coincidence.
    """
    trusted = set(candidates)
    trusted.discard(pid)
    seen: set[int] = set()
    current = pid
    for _ in range(depth):
        if current in trusted:
            return True
        if current in seen or current <= 1:
            return False
        seen.add(current)
        parent = _ppid_of(current)
        if parent is None:
            return False
        current = parent
    return False


def terminate_pinned_group(
    pgid: int,
    *,
    grace: float = DEFAULT_KILL_GRACE,
    reap_bound: float = REAP_TIMEOUT,
    pinned: Optional[Mapping[int, int]] = None,
    extra: Optional[Mapping[int, int]] = None,
) -> Dict[int, int]:
    """TERM -> full bounded grace -> KILL -> verified-gone for a pinned set.

    The numeric ``pgid`` is used **only** as a ``/proc`` scan key — it is
    **never** passed to ``killpg``, so a numeric id that a foreign group
    reused after the bounded leader was reaped can never signal that foreign
    group (F4).  ``pinned`` maps every known member PID to its starttime
    identity (default: a fresh scan that pins the leader and every live
    member); ``extra`` maps identity-pinned PIDs outside the group
    (``setsid``/double-fork escapes) that are signaled per-PID alongside it.

    SIGTERM is delivered per-PID to the pinned leader, every identity-
    matching member, and every ``extra`` PID; the full bounded grace is
    always observed (the leader exiting on TERM is never taken as the group
    being gone); then SIGKILL is delivered per-PID to every identity-
    matching member and ``extra`` PID.  A member forked during the grace is
    captured by the repeated scan and killed while the original group is
    still provably ours (at least one pinned member still lives, so the
    numeric pgid cannot have been reused by a foreign group) or while its
    ppid ancestry still reaches a pinned member; once no pinned member
    survives the numeric id is never used again and an ambiguous member
    (fork-during-grace whose parent was reaped, or a foreign group that
    reused the released id) is never signaled.  Baseline and foreign
    processes are never touched.  Returns the final live pinned accounting
    (empty when every owned member is gone).
    """
    if pinned is None:
        pinned = _pgid_members_identity(pgid)
    extra = extra or {}
    if not pinned and not extra:
        # Nothing was ever pinned for this numeric id: refusing to signal an
        # unpinned group id (a foreign group that reused the number is never
        # touched).  An ``extra`` (escaped) member is always identity-pinned
        # and is never skipped by the group's state.
        return {}
    _signal_group_pinned(pgid, signal.SIGTERM, pinned)
    for pid, starttime in extra.items():
        _signal_pid_pinned(pid, starttime, signal.SIGTERM)
    # The full bounded grace is always observed before the KILL: the leader
    # exiting on TERM is never taken as the group being gone.  During the
    # grace the scan keeps capturing fork-during-termination descendants
    # into the pinned set while a pinned member still lives (the pgid is
    # provably ours, so the new member cannot be foreign).
    deadline = time.monotonic() + grace
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        if not _pgid_has_live_members(pgid):
            break
        for member_pid, member_start in _pgid_members_identity(pgid).items():
            if pinned.get(member_pid) == member_start:
                continue
            if any(
                _is_live_with_identity(pid, start)
                for pid, start in pinned.items()
            ):
                pinned = {**pinned, member_pid: member_start}
        time.sleep(min(0.02, remaining))
    deadline = time.monotonic() + reap_bound
    while True:
        current = _pgid_members_identity(pgid)
        live_pinned = {
            pid: start for pid, start in pinned.items()
            if _is_live_with_identity(pid, start)
        }
        if not live_pinned:
            # No pinned member survives: the bounded group is gone and the
            # numeric id is released.  A member enumerated now is either a
            # fork-during-grace descendant whose parent was reaped or a
            # foreign group that reused the released id — never signal an
            # unpinned group id (F4); classify by ancestry only.
            for member_pid, member_start in current.items():
                if _ancestry_reaches(member_pid, pinned):
                    _signal_pid_pinned(member_pid, member_start, signal.SIGKILL)
                    pinned = {**pinned, member_pid: member_start}
        else:
            # At least one pinned member still lives, so this pgid cannot
            # have been reused by a foreign group: every enumerated member —
            # including a descendant forked during the grace — is ours and
            # is killed by its own identity.
            for member_pid, member_start in current.items():
                _signal_pid_pinned(member_pid, member_start, signal.SIGKILL)
                pinned = {**pinned, member_pid: member_start}
        # Every identity-pinned ``extra`` PID (a ``setsid``/double-fork
        # escape outside the group) is KILLed in both branches: the group's
        # liveness never gates the escape cleanup.
        for pid, start in extra.items():
            _signal_pid_pinned(pid, start, signal.SIGKILL)
        if not _pgid_has_live_members(pgid) and not any(
            _is_live_with_identity(pid, start)
            for pid, start in pinned.items()
        ) and not any(
            _is_live_with_identity(pid, start)
            for pid, start in extra.items()
        ):
            return {}
        if time.monotonic() >= deadline:
            live = sorted(
                pid for pid, start in pinned.items()
                if _is_live_with_identity(pid, start)
            ) + sorted(
                pid for pid, start in extra.items()
                if _is_live_with_identity(pid, start)
            )
            raise RootLockUnsafeError(
                "the bounded process group did not die within the KILL "
                f"reap window ({reap_bound:.1f}s); live group members "
                f"survive: {live}"
            )
        time.sleep(0.02)


def capture_descendants(
    root_pid: int, *, maximum: int = 512
) -> frozenset[CapturedProcess]:
    """Snapshot the full bounded descendant closure of ``root_pid`` (F1).

    The snapshot reads the parent table from ``/proc/<pid>/stat`` for every
    live process and follows the transitive descendant closure of
    ``root_pid``, so a double-forked grandchild — even one that later
    reparents to PID 1 after ``setsid`` — is captured by its ancestry while
    the model root is still alive.  The closure is *bounded*: when it would
    exceed ``maximum`` the snapshot is *not* silently truncated — it fails
    closed with :class:`RootLockUnsafeError` so no caller can mistake a
    partial accounting for a complete one.

    Every captured PID records its **starttime and parent PID** from the
    same snapshot (:class:`CapturedProcess`), making the captured scope
    reuse-safe: a later caller (:func:`live_scope`, the escape detector)
    can tell a PID reused by an unrelated process from the actual captured
    descendant.  Callers take this snapshot before signaling termination
    and pass it to :func:`detect_escaped_descendants`.
    """
    if (
        isinstance(root_pid, bool)
        or not isinstance(root_pid, int)
        or root_pid < 1
    ):
        raise RootLockUnsafeError("`root_pid` must be a positive PID")
    if isinstance(maximum, bool) or not isinstance(maximum, int) or maximum < 1:
        raise RootLockUnsafeError("`maximum` must be a positive integer bound")
    snapshot: Dict[int, Tuple[int, int]] = {}
    for pid in _iter_pids():
        fields = _proc_stat_fields(pid)
        if fields is None or len(fields) < 20:
            continue
        try:
            snapshot[pid] = (int(fields[1]), int(fields[19]))
        except ValueError:
            continue
    if root_pid not in snapshot:
        raise RootLockUnsafeError(
            f"cannot capture the descendant scope of {root_pid}: the "
            "process is not visible in /proc"
        )
    seen: set[int] = {root_pid}
    frontier: List[int] = [root_pid]
    while frontier:
        parent = frontier.pop()
        for pid, (ppid, _) in snapshot.items():
            if ppid == parent and pid not in seen:
                seen.add(pid)
                frontier.append(pid)
                if len(seen) > maximum:
                    raise RootLockUnsafeError(
                        f"descendant scope of {root_pid} exceeds the bounded "
                        f"maximum {maximum}; refusing an incomplete snapshot"
                    )
    return frozenset(
        CapturedProcess(pid=pid, starttime=snapshot[pid][1], parent=snapshot[pid][0])
        for pid in seen
    )


def live_scope(captured: Iterable) -> frozenset[int]:
    """Re-enumerate only the still-live, identity-matching scope members.

    This is the detector-scope surface that Task 6 supervision consumes: it
    snapshots once (:func:`capture_descendants`) and then refreshes *only
    the role's own captured descendants* (never a fresh whole-system scan)
    so descendant accounting is never stale.  Each captured
    :class:`CapturedProcess` pins its PID to the starttime recorded in the
    snapshot; a captured PID counts as live only while ``/proc/<pid>``
    exists, is not a zombie, *and* still carries the captured starttime.  A
    PID that was reused by an unrelated process after the captured one
    exited therefore fails the identity check and is **excluded** — the
    live scope never returns an unrelated PID and PID reuse can never widen
    the set.  (A bare integer entry — a legacy caller — is treated with the
    pre-identity exists-and-not-zombie check.)
    """
    live: set[int] = set()
    for entry in captured:
        if isinstance(entry, CapturedProcess):
            if _is_live_with_identity(entry.pid, entry.starttime):
                live.add(entry.pid)
            continue
        if isinstance(entry, bool) or not isinstance(entry, int) or entry < 1:
            continue
        if os.path.exists(f"/proc/{entry}") and not _is_zombie(entry):
            live.add(entry)
    return frozenset(live)


def _self_ancestry() -> frozenset[int]:
    """PIDs of this process and its full trusted ancestor chain (never flagged).

    The walk reads the parent of *each* PID from its own
    ``/proc/<pid>/stat`` entry — never a constant ``os.getppid()``, which
    can only ever report the parent of this process — so the entire trusted
    chain above the control plane is walked, bounded, up to PID 1.  A PID
    whose proc entry vanished mid-walk, a malformed stat, a self-parent, or
    a parent already in the chain ends the walk at the last verified
    ancestor instead of trusting an unrelated PID.
    """
    trusted: set[int] = set()
    pid = os.getpid()
    guard = 0
    while pid > 1 and guard < 64:
        trusted.add(pid)
        parent = _ppid_of(pid)
        if parent is None or parent <= 1 or parent == pid or parent in trusted:
            break
        pid = parent
        guard += 1
    trusted.add(1)
    return frozenset(trusted)


def _holds_root_handle(pid: int, root: Path, root_dev: int, root_ino: int) -> bool:
    """True when ``pid`` keeps ``root`` as its cwd or holds an fd to it.

    ``/proc/<pid>/cwd`` and ``/proc/<pid>/fd/N`` stat directly on the
    target inode, so both an inherited directory descriptor (including a
    dup of the lock descriptor) and a model workspace handle are detected
    by the same (dev, ino) comparison.
    """
    try:
        cwd_info = os.stat(f"/proc/{pid}/cwd")
    except OSError:
        cwd_info = None
    if (
        cwd_info is not None
        and (cwd_info.st_dev, cwd_info.st_ino) == (root_dev, root_ino)
    ):
        return True
    try:
        descriptors = os.listdir(f"/proc/{pid}/fd")
    except OSError:
        return False
    for entry in descriptors:
        try:
            info = os.stat(f"/proc/{pid}/fd/{entry}")
        except OSError:
            continue
        if (info.st_dev, info.st_ino) == (root_dev, root_ino):
            return True
    return False


def _is_zombie(pid: int) -> bool:
    """True when ``pid`` is a zombie (state ``Z`` in ``/proc/<pid>/stat``).

    A zombie holds no file descriptors, no cwd, and no lock reference; it is
    fully reaped at the kernel level and only awaits its parent's ``wait``.
    It therefore never counts as a surviving escaped descendant (an escape
    that still holds the repository root/lock inode is caught by the handle
    scan, which cannot see zombie fds).
    """
    fields = _proc_stat_fields(pid)
    return fields is not None and bool(fields) and fields[0] == "Z"


def _captured_pids(captured: Iterable) -> Dict[int, Optional[int]]:
    """Map captured PID to its recorded starttime (``None`` for bare PIDs).

    Accepts both :class:`CapturedProcess` records (starttime known) and
    bare integer PIDs (a legacy caller), so callers that received a scope
    from :func:`capture_descendants` keep reuse-safe identity checks while
    callers that constructed a scope by hand still work.
    """
    result: Dict[int, Optional[int]] = {}
    for entry in captured:
        if isinstance(entry, CapturedProcess):
            result[entry.pid] = entry.starttime
            continue
        if isinstance(entry, bool) or not isinstance(entry, int) or entry < 1:
            continue
        result[entry] = None
    return result


def detect_escaped_descendants(
    root: Path,
    *,
    model_pid: Optional[int] = None,
    captured: Optional[Iterable] = None,
    trusted_pids: Optional[Iterable[int]] = None,
    trusted_identities: Optional[Mapping[int, int]] = None,
) -> Tuple[frozenset[int], str]:
    """Detect escaped double-fork/``setsid`` descendants that survive.

    Returns ``(escaped_pids, reason)``; an empty set means no escape.

    * ``captured`` is the pre-termination descendant snapshot of the model
      process (see :func:`capture_descendants`); any captured PID that still
      exists — and, for an identity-pinned :class:`CapturedProcess`, still
      carries the recorded starttime, so a PID reused by an unrelated
      process is never counted as an escaped descendant — after bounded
      termination is an escaped descendant and recovery must fail closed.
    * Independently, every PID outside the trusted set that still retains
      the canonical root as cwd or an open root/lock inode handle is
      flagged: an escaped child that does not hold the root is undetectable
      by handle scan but *is* captured in the tree snapshot, and a
      handle-holder that slipped outside the snapshot is caught here.
    * ``trusted_pids`` defaults to this process and its ancestor chain (the
      control-plane's own tree). ``trusted_identities`` is the reuse-safe
      exclusion surface for a supervision baseline: a PID is excluded only
      while its current starttime still equals the snapshotted starttime.
    """
    root = Path(root).absolute()
    try:
        root_info = os.stat(root, follow_symlinks=False)
    except OSError as exc:
        raise RootLockUnsafeError(
            f"cannot stat the canonical root for escape detection: {exc}"
        ) from exc
    if not stat.S_ISDIR(root_info.st_mode):
        raise RootLockUnsafeError(
            f"canonical root is not a directory: {root}"
        )
    if captured is None and model_pid is not None:
        captured = capture_descendants(model_pid)
    if captured is None:
        captured = ()
    trusted = frozenset(trusted_pids or ()) | _self_ancestry()
    identity_baseline = {
        int(pid): int(starttime)
        for pid, starttime in (trusted_identities or {}).items()
        if (
            not isinstance(pid, bool) and isinstance(pid, int) and pid > 0
            and not isinstance(starttime, bool)
            and isinstance(starttime, int) and starttime > 0
        )
    }

    def is_trusted(pid: int) -> bool:
        if pid in trusted:
            return True
        starttime = identity_baseline.get(pid)
        return starttime is not None and _is_live_with_identity(pid, starttime)

    escaped: set[int] = set()
    reasons: List[str] = []
    for pid, starttime in sorted(_captured_pids(captured).items()):
        if is_trusted(pid):
            continue
        if starttime is not None:
            still_live = _is_live_with_identity(pid, starttime)
        else:
            still_live = os.path.exists(f"/proc/{pid}") and not _is_zombie(pid)
        if still_live:
            escaped.add(pid)
            reasons.append(
                f"pid {pid} of the model process tree survives termination"
            )
    for pid in sorted(_iter_pids()):
        if pid in escaped or pid == os.getpid():
            continue
        if is_trusted(pid):
            continue
        if _holds_root_handle(pid, root, root_info.st_dev, root_info.st_ino):
            # Re-check an exact baseline after the potentially long fd scan.
            # A baseline process that exited and had its numeric PID reused
            # while /proc was inspected must not exempt the replacement.
            if is_trusted(pid):
                continue
            escaped.add(pid)
            reasons.append(
                f"pid {pid} retains a repository-root or lock inode handle"
            )
    if not escaped:
        return (frozenset(), "")
    return (frozenset(escaped), "; ".join(reasons))


def assert_no_escaped_descendants(
    root: Path,
    *,
    model_pid: Optional[int] = None,
    captured: Optional[frozenset[int]] = None,
    trusted_pids: Optional[Iterable[int]] = None,
    trusted_identities: Optional[Mapping[int, int]] = None,
) -> None:
    """Fail closed when an escaped descendant survives (PROC-01, §12).

    The supervisor calls this *after* bounded termination and reaping, and
    must not reacquire the writer boundary — and must not resume recovery —
    while it raises :class:`EscapedDescendantError`.
    """
    escaped, reason = detect_escaped_descendants(
        root, model_pid=model_pid, captured=captured, trusted_pids=trusted_pids,
        trusted_identities=trusted_identities,
    )
    if escaped:
        raise EscapedDescendantError(
            f"escaped model descendants survive; recovery is blocked: {reason}"
        )
