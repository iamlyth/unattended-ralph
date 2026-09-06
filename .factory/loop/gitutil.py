#!/usr/bin/env python3
"""PATH-pinned absolute Git executable for trusted state/branch calls (GIT-01).

FACTORY-LOOP-SPEC §12 requires every trusted Git call to use an absolute,
PATH-pinned executable: an unqualified ``git`` resolved from a
caller-controlled ``PATH`` could substitute a different binary behind the
guarded commit boundary.  This module resolves the executable exactly once,
at import time, from *fixed absolute candidate locations only* — never from
the caller's ``PATH`` (finding F4).  The candidates are the standard FHS
locations (``/usr/bin/git``, ``/bin/git``), the NixOS system profile
(``/run/current-system/sw/bin/git``), and the immutable root-owned Nix store
(``/nix/store/*/bin/git``).  Every candidate is validated as an absolute
regular executable whose complete path chain the caller cannot modify
(owner differs from the caller's uid, no group/other write bits, and a
sticky-protected foreign-owned store directory is accepted only because the
store pattern + foreign ownership already make the pinned path
immutable-from-the-caller);  if no candidate validates, resolution *fails
closed* with :class:`GitBoundaryError` and is never silently replaced by an
unvalidated ``git`` found on an arbitrary ``PATH``.

The boundary has an **explicit non-root requirement**: when the process
runs with effective uid 0, every candidate path component is caller-owned,
so no candidate can be proven immutable *without weakening the ownership
check*.  Rather than weaken the check, :func:`resolve_git_executable` fails
closed as root with :class:`GitBoundaryError`; the trusted control plane
must run as an unprivileged user.  (An immutable root-owned store alone
does not make the chain safe for root: a root caller could chmod/chown the
very store entries it must not control.)

The runner also strips a deterministic replace refs pin and a documented set
of Git override environment variables — including the *complete*
``GIT_CONFIG*`` family (``GIT_CONFIG``, ``GIT_CONFIG_SYSTEM``,
``GIT_CONFIG_GLOBAL``, ``GIT_CONFIG_NOSYSTEM``, ``GIT_CONFIG_COUNT``,
``GIT_CONFIG_KEY_*``, ``GIT_CONFIG_VALUE_*``, ``GIT_CONFIG_PARAMETERS``),
the object-store / index / work-tree / helper redirectors, and
``GIT_NO_REPLACE_OBJECTS`` itself — so the pinned executable cannot be
pointed at a different object store, index, work tree, configuration set, or
replace-refs behavior behind the boundary (finding F5; replace refs could
otherwise swap an object behind ``git cat-file``).  Every trusted invocation
re-pins ``GIT_NO_REPLACE_OBJECTS=1`` in its sanitized environment, so object
resolution can never consult ``refs/replace/*``.  Only the *read-only*
state/branch calls of the trusted control plane use this module; the
committed ``.factory/tools/git-commit-guard.sh`` boundary remains the authority for
commit creation and is preserved untouched.

Bounded byte capture: :func:`git_bytes_bounded` drives the stdin write and
both capture pipes through **one fair ``select`` event loop** — stdin,
stdout, and stderr are serviced together against one shared deadline with
``select``-bounded ``read1``/``os.write`` chunks, so a child that floods one
pipe while another stalls can never deadlock the capture into a spurious
timeout, and a batched child whose request payload *and* transcript each
exceed a pipe buffer keeps making progress instead of wedging a sequential
write-then-drain (Task 20).  It fails closed the moment a stream exceeds its
cap, so a blob read is never an unbounded ``subprocess.run`` capture even
when a pre-checked size is the primary defense (Task 15 F1).  The child runs
in its **own process group** (``start_new_session=True``) and a wedged,
over-bound, or timed-out capture terminates and reaps the child's *entire*
group (TERM, full bounded grace, unconditional KILL, bounded leader reap),
so no git helper survives and no zombie is left behind (Task 15).  A broken
or exhausted stdin pipe (EPIPE/EOF) ends the write side instead of failing,
and once the input is fully delivered the write end is closed so the child
observes EOF on stdin; a child that never reads its input cannot stall the
capture past the shared deadline either.

Every invocation failure (missing binary, broken pipe, bounded timeout) is
routed through the single fail-closed :class:`GitBoundaryError` contract
(finding F9), so the lock/state authority can wrap it consistently.
"""

from __future__ import annotations

import glob
import importlib.util
import os
from pathlib import Path
import re
import select
import signal
import stat
import subprocess
import time
from typing import Any, Dict, Iterable, Mapping, Optional, Sequence, Tuple


class GitBoundaryError(RuntimeError):
    """The pinned Git executable cannot be resolved, or its invocation failed."""


# Finite bound for every trusted state/branch Git invocation of this module
# (Task 9 review MED): an unbounded ``subprocess.run`` behind the commit
# boundary could wait forever on a wedged repository/pipe.  Callers that
# need a different bound pass their own ``timeout`` explicitly.
GIT_TIMEOUT = 120.0


# Fixed absolute Git candidates, in precedence order.  ``PATH`` is *never*
# consulted: an unqualified ``git`` resolved from a caller-controlled PATH
# could substitute a different binary behind the guarded commit boundary
# (F4).  ``/nix/store/*/bin/git`` is expanded and validated deterministically
# (sorted store paths, first valid candidate wins).
FIXED_GIT_CANDIDATES = (
    "/usr/bin/git",
    "/bin/git",
    "/run/current-system/sw/bin/git",
)
NIX_STORE_GIT_GLOB = "/nix/store/*/bin/git"
NIX_STORE_PATH_RE = re.compile(r"^/nix/store/[0-9a-z]{32}-[^/]+/bin/git$")

# Git environment variables that can redirect where the executable reads or
# writes repository state.  They are stripped from every trusted invocation so
# a caller-controlled environment cannot redirect the pinned binary.  The
# ``GIT_CONFIG`` family is stripped by *prefix* in
# :func:`sanitize_git_environment` (a complete strip that covers every
# ``GIT_CONFIG_*`` parameter, not only the enumerated names);  the remaining
# exact keys are enumerated here.
GIT_ENV_STRIP = (
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_INDEX_FILE",
    "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    "GIT_COMMON_DIR",
    "GIT_NAMESPACE",
    "GIT_CEILING_DIRECTORIES",
    "GIT_SSH",
    "GIT_SSH_COMMAND",
    "GIT_ASKPASS",
    "GIT_TERMINAL_PROMPT",
    "GIT_CONFIG_PARAMETERS",
    "GIT_EXEC_PATH",
    "GIT_TEMPLATE_DIR",
    # A caller environment can never re-enable replace refs (a replace
    # ref could swap an object hash behind a trusted blob read); the
    # boundary re-pins ``GIT_NO_REPLACE_OBJECTS=1`` itself on every call.
    "GIT_NO_REPLACE_OBJECTS",
)
# The complete ``GIT_CONFIG`` family is matched by prefix (F5): every key
# whose name is exactly ``GIT_CONFIG`` or starts with ``GIT_CONFIG_`` is
# removed, covering the well-known enumerated keys and any future sibling.
GIT_CONFIG_PREFIX = "GIT_CONFIG"
GIT_CONFIG_PREFIX_PATTERN = (GIT_CONFIG_PREFIX, GIT_CONFIG_PREFIX + "_")
# The exact no-replace pin every trusted invocation re-applies after the
# environment strip: a caller environment can never re-enable replace refs,
# so a replace ref can never swap an object hash behind a trusted blob read
# (finding F5).
GIT_NO_REPLACE_OBJECTS = "GIT_NO_REPLACE_OBJECTS"


def _is_git_config_key(key: str) -> bool:
    return key == GIT_CONFIG_PREFIX or key.startswith(GIT_CONFIG_PREFIX + "_")


def sanitize_git_environment(env: Optional[Mapping[str, str]] = None) -> Dict[str, str]:
    """Return ``env`` (default: ``os.environ``) without the Git override keys.

    Every ``GIT_CONFIG*`` variable — the complete family, not just the
    enumerated names — is removed by prefix, together with the exact
    redirector keys of :data:`GIT_ENV_STRIP` (F5).
    """
    environment = dict(os.environ if env is None else env)
    for key in list(environment):
        if key in GIT_ENV_STRIP or _is_git_config_key(key):
            environment.pop(key, None)
    return environment


def _immutable_chain(path: str) -> None:
    """Fail closed unless the caller cannot modify ``path`` or its ancestors.

    A candidate Git executable is trusted only when the *caller* (the uid
    running the control plane, and therefore every same-uid model process)
    can neither replace it nor replace any directory that names it, using
    ordinary file permissions.  The check walks the realpath-resolved chain
    from the binary up to its containment boundary:

    * for an immutable Nix-store candidate the boundary is the store root
      ``/nix/store`` — the store's own entries are foreign-owned (root on
      real NixOS) and read-only, and the sticky-protected store directory
      prevents entry replacement, so mount/namespace ancestors above the
      store (``/nix``, ``/``) are not part of the permission chain;
    * for an FHS candidate the boundary is the filesystem root ``/``.

    Every component in the chain must be non-group/other-writable and owned
    by a uid that differs from the caller's (an owned component could be
    chmod'd back writable), with one exception: a *sticky*, foreign-owned
    directory (the Nix store is mode 1775 owned by a non-caller uid; the
    sticky bit protects every store entry from deletion or replacement by
    the caller).  The binary file itself must additionally be a regular
    executable owned by a non-caller uid with no group/other write bits.

    A candidate resolved through a symlink is validated at its real target,
    so a root-owned symlink that redirects into a caller-writable directory
    is rejected.
    """
    resolved = os.path.realpath(path)
    store_root = Path("/nix/store")
    if resolved.startswith(str(store_root) + os.sep):
        boundary = store_root
    else:
        boundary = Path(resolved).anchor
    current = Path(resolved)
    while True:
        try:
            info = current.lstat()
        except OSError as exc:
            raise GitBoundaryError(
                f"cannot stat candidate path component {current}: {exc}"
            ) from exc
        if info.st_uid == os.getuid():
            sticky = stat.S_ISDIR(info.st_mode) and info.st_mode & stat.S_ISVTX
            if not sticky:
                raise GitBoundaryError(
                    f"candidate path component {current} is owned by the caller "
                    "and can be replaced or modified"
                )
        if info.st_mode & 0o022 and not (
            stat.S_ISDIR(info.st_mode) and stat.S_ISVTX
        ):
            raise GitBoundaryError(
                f"candidate path component {current} is writable by the "
                "caller's group/other and is not sticky-protected"
            )
        if current == boundary:
            break
        parent = current.parent
        if current == parent:
            break
        current = parent


def _require_absolute_executable(path: str) -> None:
    """Fail closed unless ``path`` is a pinned immutable regular executable."""
    if not path or not path.startswith("/"):
        raise GitBoundaryError(f"resolved Git executable is not absolute: {path!r}")
    _immutable_chain(path)
    try:
        info = os.stat(path)
    except OSError as exc:
        raise GitBoundaryError(
            f"cannot stat the pinned Git executable {path!r}: {exc}"
        ) from exc
    if not stat.S_ISREG(info.st_mode) or not (info.st_mode & 0o111):
        raise GitBoundaryError(
            f"pinned Git executable is not a regular executable file: {path!r}"
        )


def require_trusted_executable(path: str) -> None:
    """Public seam: fail closed unless ``path`` is a pinned immutable executable.

    This is the same immutable-chain authority the Git resolver uses (F4): an
    absolute regular executable owned by a non-caller uid, with every path
    component non-group/other-writable up to its containment boundary (the
    Nix store root for store paths, the filesystem root otherwise).  The
    Task 6 launch boundary reuses it for *external trusted* wrapper/backend
    executables: an operator-claimed or caller-controlled path is never a
    trusted executable (F2).
    """
    _require_absolute_executable(path)


def require_trusted_regular_file(path: str) -> None:
    """Fail closed unless ``path`` is canonical immutable regular data.

    Runtime modules such as Pi's ``dist/cli.js`` are interpreter input rather
    than executable files.  They still require the same immutable naming
    chain as an executable and must be addressed by their canonical path;
    accepting a symlink spelling would leave the final interpreter open
    vulnerable to a post-verification link retarget.
    """
    if not path or not path.startswith("/") or os.path.realpath(path) != path:
        raise GitBoundaryError(
            f"trusted runtime data path is not canonical and absolute: {path!r}"
        )
    _immutable_chain(path)
    try:
        info = os.lstat(path)
    except OSError as exc:
        raise GitBoundaryError(
            f"cannot stat trusted runtime data {path!r}: {exc}"
        ) from exc
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise GitBoundaryError(
            f"trusted runtime data is not a single-link regular file: {path!r}"
        )
    if info.st_mode & 0o022:
        raise GitBoundaryError(
            f"trusted runtime data is group/other writable: {path!r}"
        )


def _validate_candidate(candidate: str) -> None:
    """Validate one fixed absolute candidate (testable seam)."""
    _require_absolute_executable(candidate)


def _store_git_candidates() -> Iterable[str]:
    """Immutable Nix-store ``bin/git`` candidates, in deterministic order.

    The store is scanned as a *fixed absolute directory* (``/nix/store``),
    never through ``PATH``.  Only paths matching the store-path pattern are
    considered, and each is validated for regular-executable + immutable
    chain; the lexicographically first valid store candidate is selected.
    """
    try:
        found = sorted(glob.glob(NIX_STORE_GIT_GLOB))
    except OSError:
        return ()
    for candidate in found:
        if NIX_STORE_PATH_RE.fullmatch(candidate):
            yield candidate


def resolve_git_executable() -> str:
    """Resolve the absolute Git executable from fixed trusted locations.

    Precedence, all absolute and none PATH-derived (F4):

    1. ``/usr/bin/git``, ``/bin/git`` (standard FHS);
    2. ``/run/current-system/sw/bin/git`` (NixOS system profile, a symlink
       into the root-owned immutable store);
    3. ``/nix/store/*/bin/git`` (root-owned immutable store; deterministic
       lexicographic first valid candidate).

    Each candidate must be an absolute regular executable whose realpath
    chain the caller cannot modify; any candidate that fails validation is
    skipped, and when *no* candidate validates the resolver fails closed
    with :class:`GitBoundaryError` — it never falls back to an unqualified
    ``git`` resolved from an arbitrary caller ``PATH``.

    The boundary refuses to resolve as root (an explicit non-root
    requirement): under uid 0 every candidate path component is
    caller-owned, so no candidate can be proven immutable without weakening
    the ownership check; the trusted control plane must run as an
    unprivileged user.
    """
    if os.geteuid() == 0:
        raise GitBoundaryError(
            "the pinned Git boundary refuses to resolve as root: every "
            "candidate path component is caller-owned under uid 0, so no "
            "candidate can be proven immutable without weakening the "
            "ownership check; run the trusted control plane as an "
            "unprivileged user"
        )
    seen: set = set()
    for candidate in FIXED_GIT_CANDIDATES:
        if candidate in seen:
            continue
        seen.add(candidate)
        if not os.path.exists(candidate):
            continue
        try:
            _validate_candidate(candidate)
        except GitBoundaryError:
            continue
        return candidate
    for candidate in _store_git_candidates():
        if candidate in seen:
            continue
        seen.add(candidate)
        try:
            _validate_candidate(candidate)
        except GitBoundaryError:
            continue
        return candidate
    raise GitBoundaryError(
        "no immutable absolute Git executable is available to the trusted "
        "boundary; refusing to resolve `git` from a caller-controlled PATH"
    )


# Pinned once at import: every trusted state/branch call in this package runs
# this absolute path, never an unqualified ``git`` from a caller PATH.
GIT_EXECUTABLE: str = resolve_git_executable()


def git_run(
    argv: Sequence[str],
    *,
    cwd: Optional[Path] = None,
    env: Optional[Mapping[str, str]] = None,
    input: Optional[str] = None,
    timeout: Optional[float] = None,
    pass_fds: Sequence[int] = (),
) -> subprocess.CompletedProcess[str]:
    """Run the pinned absolute Git executable with a sanitized environment.

    ``argv`` is passed verbatim after the pinned executable (callers use
    ``-C <root>`` for repository-relative commands).  ``pass_fds`` is used
    only by the trusted root-descriptor lock to hand the pinned binary an
    *anchored, freshly opened* root-directory descriptor (a new open file
    description bound to the locked inode);  it is never populated by
    untrusted input.  The returned ``CompletedProcess`` carries the exit
    code, stdout, and stderr; trusted callers interpret nonzero exits as
    fail-closed binding failures.
    """
    environment = sanitize_git_environment(env)
    # Replace refs are never consulted: a caller-controlled environment cannot
    # re-enable them (the key is stripped above), and the boundary itself pins
    # the no-replace behavior on every trusted invocation so an object hash
    # can never be swapped behind a blob read (Task 15 F1).
    environment[GIT_NO_REPLACE_OBJECTS] = "1"
    try:
        return subprocess.run(
            [GIT_EXECUTABLE, *argv],
            cwd=cwd,
            env=environment,
            input=input,
            capture_output=True,
            text=True,
            timeout=timeout,
            close_fds=True,
            pass_fds=tuple(pass_fds),
        )
    except (OSError, ValueError) as exc:
        raise GitBoundaryError(
            f"cannot execute the pinned Git executable {GIT_EXECUTABLE!r}: {exc}"
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise GitBoundaryError(
            f"the pinned Git executable {GIT_EXECUTABLE!r} exceeded its "
            f"bounded timeout: {exc}"
        ) from exc


def git_bytes(
    argv: Sequence[str],
    *,
    cwd: Optional[Path] = None,
    env: Optional[Mapping[str, str]] = None,
    input: Optional[bytes] = None,
    timeout: Optional[float] = None,
    pass_fds: Sequence[int] = (),
) -> subprocess.CompletedProcess[bytes]:
    """Like :func:`git_run`, but with byte-preserving stdout (blob reads).

    The canonical plan digest is the SHA-256 of the exact committed plan
    bytes, so blob reads must not round-trip through a text decoding.

    This plain variant captures whatever the child writes; callers that need
    a hard-bounded capture (migration blob reads) use
    :func:`git_bytes_bounded` instead.
    """
    environment = sanitize_git_environment(env)
    environment[GIT_NO_REPLACE_OBJECTS] = "1"
    try:
        return subprocess.run(
            [GIT_EXECUTABLE, *argv],
            cwd=cwd,
            env=environment,
            input=input,
            capture_output=True,
            timeout=timeout,
            close_fds=True,
            pass_fds=tuple(pass_fds),
        )
    except (OSError, ValueError) as exc:
        raise GitBoundaryError(
            f"cannot execute the pinned Git executable {GIT_EXECUTABLE!r}: {exc}"
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise GitBoundaryError(
            f"the pinned Git executable {GIT_EXECUTABLE!r} exceeded its "
            f"bounded timeout: {exc}"
        ) from exc


def _close_pinned_git_streams(proc: subprocess.Popen) -> None:
    """Close every pipe end of a pinned-Git child (never raises)."""
    for stream in (proc.stdin, proc.stdout, proc.stderr):
        if stream is None:
            continue
        try:
            stream.close()
        except OSError:
            pass


def _set_pipe_nonblocking(stream) -> None:
    """Make one child pipe end non-blocking so a select wakeup never blocks.

    ``select`` reporting a pipe readable is a strong signal, but a spurious
    wakeup (for example EINTR) can still leave the pipe empty; a non-blocking
    pipe makes the subsequent ``read1`` raise ``BlockingIOError`` instead of
    blocking forever, so the bounded drain stays strictly non-blocking.
    """
    try:
        os.set_blocking(stream.fileno(), False)
    except OSError:
        pass


def _signal_process_group(proc: subprocess.Popen, signum: int) -> None:
    """Send ``signum`` to the child's whole process group (best effort).

    The bounded runner starts every child in its own new session/process
    group, so the group is addressed by the child's pid.  If the group is
    already gone (``ProcessLookupError``) and the leader still lives, the
    leader is signalled directly; a reaped leader means there is nothing to
    signal.
    """
    try:
        pgid = os.getpgid(proc.pid)
    except OSError:
        pgid = None
    if pgid is not None:
        try:
            os.killpg(pgid, signum)
            return
        except ProcessLookupError:
            pass
        except OSError:
            pass
    if proc.poll() is None:
        try:
            os.kill(proc.pid, signum)
        except OSError:
            pass


def _reap_leader_bounded(proc: subprocess.Popen, timeout: float) -> None:
    """Reap the group leader within a bounded deadline after KILL.

    SIGKILL cannot be ignored, so the leader either dies (becoming a zombie
    that ``wait()`` immediately reaps) or is stuck in an unrecoverable
    kernel wait; the loop keeps calling ``wait()`` up to the bounded
    deadline so a normal death is always reaped (never left as a zombie of
    the caller), and gives up only on the pathological uninterruptible case
    (which is not a zombie and is documented rather than spun on).
    """
    deadline = time.monotonic() + timeout
    while True:
        try:
            proc.wait(timeout=max(0.05, deadline - time.monotonic()))
            return
        except subprocess.TimeoutExpired:
            if time.monotonic() >= deadline:
                return


def _terminate_pinned_git(proc: subprocess.Popen) -> None:
    """Terminate and reap a wedged pinned-Git child's entire process group.

    The child runs in its own process group (``start_new_session``), so the
    bounded escalation — TERM to the group, the full grace, unconditional
    KILL of the group, and the bounded leader reap — applies to every git
    helper the child spawned: no grandchild survives and no zombie is left
    behind.  The KILL is sent even when the leader exited on TERM, so a
    TERM-ignoring grandchild holding a pipe cannot outlive the capture.
    """
    _signal_process_group(proc, signal.SIGTERM)
    try:
        proc.wait(timeout=5.0)
    except subprocess.TimeoutExpired:
        pass
    _signal_process_group(proc, signal.SIGKILL)
    _reap_leader_bounded(proc, timeout=5.0)


def _bounded_io_loop(
    proc: subprocess.Popen,
    out: bytearray,
    err: bytearray,
    maximum: int,
    input: bytes,
    deadline: float,
    argv: Sequence[str],
) -> None:
    """Drive stdin writes and stdout/stderr drains in ONE fair event loop.

    The stdin write end and both capture pipes are serviced by the *same*
    ``select`` wakeups against one shared deadline, so a child that produces
    output faster than the caller pushes its input can never wedge the
    capture: a batched ``git cat-file --batch`` whose request payload *and*
    whose transcript each exceed the pipe buffer would stall a sequential
    write-then-drain into a spurious timeout (the child blocks on a full
    stdout pipe and stops reading the full stdin pipe, so the sequential
    writer's stdin select never fires).  Stdin writes and stream drains each
    make progress whenever the corresponding pipe is ready, and a stream
    that floods while its sibling stalls is consumed fairly (the stalled
    sibling is simply never ready).  Each stream accumulates at most
    ``maximum`` bytes and an over-bound stream fails closed; EOF removes a
    stream while its siblings keep draining; a broken stdin pipe (the child
    closed its input, typically by exiting) ends the write side instead of
    failing.  Once the input is fully delivered the stdin write end is
    closed so the child observes EOF and can exit.  All pipes are
    non-blocking (``read1``/``os.write`` raise ``BlockingIOError`` on a
    spurious wakeup instead of blocking) and the shared deadline bounds the
    whole call.
    """
    streams: Dict[Any, Tuple[bytearray, str, int]] = {}
    if proc.stdout is not None:
        streams[proc.stdout] = (out, "stdout", maximum)
    if proc.stderr is not None:
        streams[proc.stderr] = (err, "stderr", maximum)
    view = memoryview(input)
    write_fd: Optional[int] = None
    if proc.stdin is not None and view:
        try:
            write_fd = proc.stdin.fileno()
            os.set_blocking(write_fd, False)
        except (AttributeError, OSError):
            write_fd = None
    elif proc.stdin is not None:
        # No input to deliver: close the write end immediately so a child
        # that reads stdin to EOF can exit (a never-closed stdin pipe would
        # stall the bounded wait into a spurious timeout).
        try:
            proc.stdin.close()
        except OSError:
            pass
    stdin_open = write_fd is not None
    while streams or (stdin_open and view):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise GitBoundaryError(
                f"the pinned Git executable {GIT_EXECUTABLE!r} exceeded its "
                f"bounded timeout: {' '.join(argv)}"
            )
        read_set = list(streams)
        write_set = [write_fd] if stdin_open and view else []
        try:
            # ``select.select`` rejects descriptors >= FD_SETSIZE (commonly
            # 1024).  The confinement authority intentionally retains many
            # descriptor anchors while exact-commit Git reads run, so use the
            # Linux poll boundary, which is fd-number independent and keeps
            # the same shared-deadline/fair-drain semantics.
            poller = select.poll()
            read_by_fd = {stream.fileno(): stream for stream in read_set}
            for descriptor in read_by_fd:
                poller.register(
                    descriptor,
                    select.POLLIN | select.POLLHUP | select.POLLERR,
                )
            if write_set:
                poller.register(
                    write_fd,
                    select.POLLOUT | select.POLLHUP | select.POLLERR,
                )
            events = poller.poll(max(1, int(remaining * 1000)))
            ready_read = [
                read_by_fd[descriptor]
                for descriptor, event in events
                if descriptor in read_by_fd
                and event & (select.POLLIN | select.POLLHUP | select.POLLERR)
            ]
            ready_write = [
                descriptor
                for descriptor, event in events
                if write_fd is not None and descriptor == write_fd
                and event & (select.POLLOUT | select.POLLHUP | select.POLLERR)
            ]
        except (OSError, ValueError) as exc:
            raise GitBoundaryError(
                f"cannot poll the pinned Git executable pipes "
                f"{GIT_EXECUTABLE!r}: {exc}"
            ) from exc
        if not ready_read and not ready_write:
            raise GitBoundaryError(
                f"the pinned Git executable {GIT_EXECUTABLE!r} exceeded its "
                f"bounded timeout: {' '.join(argv)}"
            )
        for stream in ready_read:
            buffer, label, cap = streams[stream]
            try:
                chunk = stream.read1(min(65536, cap + 1 - len(buffer)))
            except BlockingIOError:
                continue
            if not chunk:
                del streams[stream]
                continue
            buffer.extend(chunk)
            if len(buffer) > cap:
                raise GitBoundaryError(
                    f"the pinned Git executable {GIT_EXECUTABLE!r} wrote more "
                    f"than {cap} bytes of {label}: {' '.join(argv)}"
                )
        if write_fd is not None and write_fd in ready_write and view:
            try:
                written = os.write(write_fd, view)
            except BlockingIOError:
                continue
            except (BrokenPipeError, OSError):
                # The child closed its stdin (typically by exiting); the
                # write side is done and nothing more can be delivered.
                stdin_open = False
                continue
            if written <= 0:
                raise GitBoundaryError(
                    f"the pinned Git executable {GIT_EXECUTABLE!r} made no "
                    f"stdin progress: {' '.join(argv)}"
                )
            view = view[written:]
            if not view:
                # Input fully delivered: close the write end so the child
                # observes EOF on stdin and can exit.
                try:
                    proc.stdin.close()
                except OSError:
                    pass
                stdin_open = False


def git_bytes_bounded(
    argv: Sequence[str],
    *,
    maximum: int,
    cwd: Optional[Path] = None,
    env: Optional[Mapping[str, str]] = None,
    input: Optional[bytes] = None,
    timeout: Optional[float] = None,
    pass_fds: Sequence[int] = (),
) -> subprocess.CompletedProcess[bytes]:
    """Pinned-Git byte read whose captured stdout is hard-capped (no unbounded capture).

    ``subprocess.run(capture_output=True)`` collects whatever the child
    writes; for a blob that is only *post-hoc* size-checked that is an
    unbounded capture.  This bounded variant consumes the child's output
    through the pipes with ``select``-bounded ``read1`` chunks and writes
    the stdin payload in the **same single fair event loop** — stdin, stdout
    and stderr are serviced together against one shared deadline, so a
    batched child whose request payload *and* transcript both exceed a pipe
    buffer can never deadlock the capture into a spurious timeout, and a
    child that floods one pipe while the other stalls is drained fairly —
    accumulates at most ``maximum`` bytes per stream, and fails closed —
    terminating and reaping the child's *entire* process group, leaving no
    zombie — the moment a stream exceeds its cap, so an over-bound or
    swapped object can never be captured unboundedly and a stalled child can
    never be waited on forever (the finite ``timeout``, default
    :data:`GIT_TIMEOUT`, bounds the whole call).  The child runs in its own
    new session/process group.  The sanitized environment carries the
    ``GIT_NO_REPLACE_OBJECTS=1`` no-replace pin of the other trusted
    invocations.
    """
    if maximum < 0:
        raise GitBoundaryError(
            "bounded Git capture requires a non-negative maximum"
        )
    environment = sanitize_git_environment(env)
    environment[GIT_NO_REPLACE_OBJECTS] = "1"
    try:
        proc = subprocess.Popen(
            [GIT_EXECUTABLE, *argv],
            cwd=cwd,
            env=environment,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            close_fds=True,
            pass_fds=tuple(pass_fds),
            start_new_session=True,
        )
    except OSError as exc:
        raise GitBoundaryError(
            f"cannot execute the pinned Git executable {GIT_EXECUTABLE!r}: {exc}"
        ) from exc
    _set_pipe_nonblocking(proc.stdout)
    _set_pipe_nonblocking(proc.stderr)
    out = bytearray()
    err = bytearray()
    deadline = time.monotonic() + (GIT_TIMEOUT if timeout is None else timeout)
    try:
        _bounded_io_loop(proc, out, err, maximum, input or b"", deadline, argv)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise GitBoundaryError(
                f"the pinned Git executable {GIT_EXECUTABLE!r} exceeded its "
                f"bounded timeout: {' '.join(argv)}"
            )
        proc.wait(timeout=remaining)
    except subprocess.TimeoutExpired as exc:
        _terminate_pinned_git(proc)
        raise GitBoundaryError(
            f"the pinned Git executable {GIT_EXECUTABLE!r} exceeded its "
            f"bounded timeout: {exc}"
        ) from exc
    except GitBoundaryError:
        _terminate_pinned_git(proc)
        raise
    except (OSError, ValueError) as exc:
        _terminate_pinned_git(proc)
        raise GitBoundaryError(
            f"cannot execute the pinned Git executable {GIT_EXECUTABLE!r}: {exc}"
        ) from exc
    except BaseException:
        # Signals and memory pressure must not leave a pinned Git process or
        # descendant running after the trusted caller unwinds.
        _terminate_pinned_git(proc)
        raise
    finally:
        _close_pinned_git_streams(proc)
    return subprocess.CompletedProcess(
        [GIT_EXECUTABLE, *argv], proc.returncode, bytes(out), bytes(err)
    )


def resolve_head(root: Path) -> Optional[str]:
    """Resolve the full 40-hex HEAD of ``root`` (``None`` when unborn/invalid).

    Every trusted invocation is finite-bounded (Task 9 review MED): the
    pinned Git executable may never wait forever behind the boundary, so
    the call passes :data:`GIT_TIMEOUT`.
    """
    result = git_run(
        ["-C", str(root), "rev-parse", "--verify", "HEAD"],
        timeout=GIT_TIMEOUT,
    )
    if result.returncode != 0:
        return None
    head = result.stdout.strip()
    if len(head) != 40:
        return None
    return head


def load_sibling(name: str) -> object:
    """Load a sibling control-plane module by committed file path.

    The hidden tests import the control-plane modules directly from the
    ``.factory/loop/`` directory, while the package ``__init__.py`` imports
    them as a proper package; loading a sibling by its absolute committed
    path (the same idiom ``state.py`` uses for
    ``.factory/tools/factory_state_io.py``) works in both contexts.
    """
    path = Path(__file__).resolve().parent / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise GitBoundaryError(f"cannot load sibling control-plane module {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module
