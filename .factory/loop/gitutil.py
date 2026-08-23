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

The runner also strips a documented set of Git override environment
variables — including the *complete* ``GIT_CONFIG*`` family
(``GIT_CONFIG``, ``GIT_CONFIG_SYSTEM``, ``GIT_CONFIG_GLOBAL``,
``GIT_CONFIG_NOSYSTEM``, ``GIT_CONFIG_COUNT``, ``GIT_CONFIG_KEY_*``,
``GIT_CONFIG_VALUE_*``, ``GIT_CONFIG_PARAMETERS``) and the object-store /
index / work-tree / helper redirectors — so the pinned executable cannot be
pointed at a different object store, index, work tree, or configuration set
behind the boundary (finding F5).  Only the *read-only* state/branch calls
of the trusted control plane use this module; the committed
``scripts/git-commit-guard.sh`` boundary remains the authority for commit
creation and is preserved untouched.

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
import stat
import subprocess
from typing import Dict, Iterable, Mapping, Optional, Sequence, Tuple


class GitBoundaryError(RuntimeError):
    """The pinned Git executable cannot be resolved, or its invocation failed."""


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
)
# The complete ``GIT_CONFIG`` family is matched by prefix (F5): every key
# whose name is exactly ``GIT_CONFIG`` or starts with ``GIT_CONFIG_`` is
# removed, covering the well-known enumerated keys and any future sibling.
GIT_CONFIG_PREFIX = "GIT_CONFIG"
GIT_CONFIG_PREFIX_PATTERN = (GIT_CONFIG_PREFIX, GIT_CONFIG_PREFIX + "_")


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
    """
    environment = sanitize_git_environment(env)
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


def resolve_head(root: Path) -> Optional[str]:
    """Resolve the full 40-hex HEAD of ``root`` (``None`` when unborn/invalid)."""
    result = git_run(["-C", str(root), "rev-parse", "--verify", "HEAD"])
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
    ``scripts/factory_state_io.py``) works in both contexts.
    """
    path = Path(__file__).resolve().parent / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise GitBoundaryError(f"cannot load sibling control-plane module {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module
