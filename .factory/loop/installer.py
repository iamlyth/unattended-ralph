#!/usr/bin/env python3
"""Trusted harness installer/stager for installed-tier evidence (Task 20).

FACTORY-LOOP-SPEC §19 (EVID-01) and §3 (HIDE-01) require a clean
exact-commit installation of the generic harness into test-owned external
and hidden prefixes, an exact physical-file inventory, and production CLIs
executed from the installed copy.  This module is the trusted
installer/stager behind that tier:

* it stages **committed harness content** — every file tracked under the
  hidden ``.factory/`` and ``.pi/`` namespaces at the exact bound commit —
  into a fresh, absolute, never-symlinked prefix outside the resolved
  repository;
* committed content is staged from the exact committed blob bytes (a
  batched ``git cat-file --batch`` through the pinned Git boundary, hard
  capped by the fair bounded capture) and re-proven blob-exact afterwards
  with a batched ``git hash-object --no-filters --stdin-paths``, so an
  installed file can never drift from the bound commit and no content
  filter can rewrite the hashed bytes;
* Task-20-era additions that are not yet part of the bound commit are
  staged from the working tree only when they appear on the **exact
  reviewer allowlist** (``PENDING_ALLOWLIST`` — the known Task-20
  authorities) and live under the installed surface (``.factory/``,
  ``.pi/``); any other pending path under the surface fails closed, and a
  secret/credential-looking path is never staged; they are recorded in the
  manifest with ``pending: true`` so a reviewer sees exactly which
  installed bytes are newer than the bound commit.  A visible-``scripts/``
  worktree change that is not a declared shared authority or entrypoint is
  never staged;
* the declared shared authorities and operator entrypoints must live under
  the allowlisted first segments (``.factory``/``.pi``/``scripts``), must
  be non-secret names, and are staged **exactly once** — they are excluded
  from the bulk committed staging so a clean committed install can never
  double-stage an entrypoint;
* executable modes are preserved (``100755 -> 0755``, ``100644 -> 0644``),
  every directory under the prefix is private mode 0700, and any symlink,
  gitlink, special inode, device node, unsafe path, group/other-writable
  mode, secret/credential-shaped path (committed content included), or
  pre-existing prefix entry fails closed;
* the prefix is **descriptor-anchored**: it is created and opened with
  ``O_DIRECTORY|O_NOFOLLOW|O_CLOEXEC``, its ``(st_dev, st_ino)`` identity
  is recorded from the anchored fd and verified against the named path and
  the resolved containment *before* the private-0700 mode is pinned with
  ``fchmod``; every staged directory/file is created through ``openat``
  dirfd chains with ``O_NOFOLLOW`` and never through a pathname write, and
  the anchor identity/containment are revalidated before every stage and
  before the manifest verify; on any failure after the prefix is created,
  the created prefix is rolled back identity-safely (the exact directory
  inode recorded at creation is removed, never a replaced or foreign path);
* every malformed batched-Git transcript (a non-numeric ``cat-file`` size,
  truncated record framing, non-UTF-8 text, a malformed ``hash-object``
  oid) fails closed as an :class:`InstallerError`, never as a raw parser
  exception;
* the manifest (``factory-install-manifest/v1``) records per file: path,
  mode, size, sha256, the exact committed blob id (committed content), the
  pending flag, and the exact root/prefix the installer bound;
  :func:`verify_staged` re-walks the installed prefix and proves the exact
  physical set, the exact modes, entrypoint executability, directory
  privacy, the manifest prefix/root identity, the resolved prefix
  containment, the digests, and the blob equality.

The installer performs no network access, never calls a model or runner,
never writes to the repository, and runs only against test-owned prefixes.
The installed copy itself is never ``real_system``/``human``/product
evidence: every installed-tier gate is minted as a machine receipt bound to
the exact commit and the audit coordinator in the fixture authority.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import sys
import tempfile
from typing import Dict, List, Optional, Sequence, Set, Tuple

try:  # package-import mode (the hidden control-plane package)
    from . import footprint
    from . import gitutil
except ImportError:  # flat-import mode used by the hidden `.factory/tests/` suite
    import footprint  # type: ignore[no-redef]
    import gitutil  # type: ignore[no-redef]

INSTALL_MANIFEST_SCHEMA = "factory-install-manifest/v1"
INSTALLER_VERSION = "factory-installer/v1"
SHA1 = re.compile(r"^[0-9a-f]{40}$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")

# The installed surface: the hidden namespaces plus the shared ``scripts/``
# authorities the hidden control plane loads by their established absolute
# path (``scripts/factory_state_io.py``) and the operator entrypoints.
INSTALLED_SURFACE = (".factory", ".pi", "scripts")

# The committed shared authority the hidden control plane imports at runtime
# and the trusted operator entrypoints of the installed copy.
DEFAULT_SHARED: Tuple[str, ...] = ("scripts/factory_state_io.py",)
DEFAULT_ENTRYPOINTS: Tuple[str, ...] = (
    ".factory/bin/factory-launch",
    "scripts/machine-receipt.py",
)

# Declared shared authorities and operator entrypoints may only live under
# these first segments: the hidden namespaces plus the shared ``scripts/``
# surface.  A declared path anywhere else would smuggle a foreign file into
# the installed copy.
ALLOWED_FIRST_SEGMENTS: frozenset = frozenset({".factory", ".pi", "scripts"})

# The exact Task-20-era pending authorities that reviewer-mode staging may
# take from the working tree while they are not yet part of the bound
# commit.  Anything else pending under the installed surface fails closed:
# a reviewer must never be surprised by an arbitrary ``.factory``/``.pi``
# file staged from the working tree.  ``test-factory-migration.py`` is
# listed only while it carries the Task-20 gitutil bounded-capture review
# authority (the GitBytesBoundedTests regressions) as an uncommitted
# worktree change, and ``implementation-plan.md`` is listed because the
# installed copy carries the plan ledger (the installed gates read it) and
# the WIP ledger update is itself a Task-20 authority: every pending
# installed-surface path must be either allowlisted or committed, so these
# keep the WIP install buildable until the Task-20 commit lands, after
# which the pending set is empty and the allowlist is inert.
PENDING_ALLOWLIST: frozenset = frozenset({
    ".factory/loop/installer.py",
    ".factory/loop/footprint.py",
    ".factory/loop/gitutil.py",
    ".factory/bin/factory-launch",
    ".factory/tests/test-factory-installed.py",
    ".factory/tests/test-factory-installed.sh",
    ".factory/tests/test-factory-migration.py",
    ".factory/artifacts/implementation-plan.md",
})

# Secret/credential-shaped path detection.  A path is never staged when
# its basename looks like an actual credential artifact: a dot-env file, a
# key/keystore suffix, an exact credential artifact name (``id_rsa``,
# ``authorized_keys``, ``api_key``, ...), an exact credential word
# (``password``, ``token``, ``secret``, ...), or a delimiter compound that
# carries two or more credential words (``secret_token``).  Descriptive
# names that merely mention secrets — for example the committed redaction
# fixture ``usage-secret-hint.html`` — are not credential artifacts and are
# not flagged (a ``secret-hint`` fixture must keep installing).
_SECRET_NAME_WORDS: frozenset = frozenset({
    "secret", "token", "password", "passwd", "credential",
    "credentials", "keyring", "cookie", "oauth", "bearer",
    "preshared", "client", "session", "signing", "access", "api",
    "private", "authorized",
})
_SECRET_NAME_ARTIFACTS: frozenset = frozenset({
    "id_rsa", "id_ed25519", "id_ecdsa", "id_dsa", "authorized_keys",
    "private_key", "secret_key", "api_key", "apikey", "access_key",
    "client_secret", "session_key", "signing_key", "preshared_key",
})
_SECRET_NAME_SUFFIXES: Tuple[str, ...] = (".pem", ".key", ".pgp", ".asc",
                                          ".p12", ".pfx", ".kdbx")

# Bounds: one staged blob, the whole staged manifest, and every pinned-Git
# transcript.
BLOB_MAX = 64 * 1024 * 1024
MANIFEST_MAX = 64 * 1024 * 1024
GIT_TIMEOUT = 120.0

# The only installed-file modes the installer may produce.
_ALLOWED_MODES = (0o644, 0o755)
# Every installed directory (prefix included) is private.
_DIR_MODE = 0o700


class InstallerError(RuntimeError):
    """A fail-closed error of the trusted installer/stager."""


# ---------------------------------------------------------------------------
# Pinned-Git helpers (fair, bounded, fail-closed)
# ---------------------------------------------------------------------------


def _git_bytes_bounded(
    root: Path,
    argv: Sequence[str],
    *,
    maximum: int,
    input: Optional[bytes] = None,
) -> bytes:
    """Run the pinned Git boundary with a hard-capped, fairly drained capture.

    The bounded capture bounds both stdout and stderr against one shared
    deadline, so a pathological or swapped object can never be captured
    unboundedly and a stalled child can never be waited on forever; any
    boundary failure (over-bound stream, timeout, missing executable) is
    translated into a fail-closed :class:`InstallerError`.
    """
    try:
        result = gitutil.git_bytes_bounded(
            argv, cwd=root, maximum=maximum, input=input, timeout=GIT_TIMEOUT
        )
    except gitutil.GitBoundaryError as exc:
        raise InstallerError(
            f"pinned git {' '.join(argv)} failed: {exc}"
        ) from exc
    if result.returncode != 0:
        raise InstallerError(
            f"pinned git {' '.join(argv)} failed: "
            f"{result.stderr.strip() or result.stdout.strip()}"
        )
    return result.stdout


def _git_text_bounded(root: Path, argv: Sequence[str], *, maximum: int) -> str:
    try:
        return _git_bytes_bounded(root, argv, maximum=maximum).decode("utf-8")
    except UnicodeDecodeError as exc:
        raise InstallerError(
            f"pinned git {' '.join(argv)} output is not UTF-8: {exc}"
        ) from exc


def _committed_blobs(root: Path, oids: Sequence[str]) -> Dict[str, bytes]:
    """Read many committed blobs in one batched, bounded ``cat-file`` call."""
    oids = sorted(set(oids))
    if not oids:
        return {}
    payload = b"".join(oid.encode("ascii") + b"\n" for oid in oids)
    out = _git_bytes_bounded(
        root,
        ["cat-file", "--batch"],
        maximum=MANIFEST_MAX,
        input=payload,
    )
    blobs: Dict[str, bytes] = {}
    pos = 0
    length = len(out)
    while pos < length:
        try:
            nl = out.index(b"\n", pos)
        except ValueError as exc:
            raise InstallerError(
                "malformed cat-file --batch transcript (missing newline)"
            ) from exc
        header = out[pos:nl].decode("ascii", "replace").split()
        pos = nl + 1
        if len(header) != 3 or header[1] != "blob":
            raise InstallerError(
                f"unexpected cat-file --batch header: {header!r}"
            )
        oid, size_text = header[0], header[2]
        try:
            size = int(size_text)
        except ValueError as exc:
            raise InstallerError(
                f"invalid cat-file --batch size {size_text!r}"
            ) from exc
        if size > BLOB_MAX:
            raise InstallerError(f"committed blob exceeds the bound: {oid}")
        end = pos + size
        if end > length:
            raise InstallerError(
                f"cat-file --batch transcript is truncated for {oid}"
            )
        raw = out[pos:end]
        pos = end
        if pos < length:
            if out[pos] != 0x0A:
                raise InstallerError(
                    f"malformed cat-file --batch record for {oid}"
                )
            pos += 1
        blobs[oid] = raw
    if set(blobs) != set(oids):
        missing = sorted(set(oids) - set(blobs))
        raise InstallerError(f"cannot read committed blobs: {missing[:5]}")
    return blobs


def _ls_tree_surface(root: Path, commit: str) -> Dict[str, Tuple[str, str]]:
    """``{relpath: (mode, oid)}`` for every blob under the installed surface."""
    out = _git_text_bounded(
        root,
        ["ls-tree", "-r", "-z", commit, "--", *INSTALLED_SURFACE],
        maximum=MANIFEST_MAX,
    )
    entries: Dict[str, Tuple[str, str]] = {}
    for record in out.split("\0"):
        if not record:
            continue
        meta, sep, relpath = record.partition("\t")
        if not sep:
            raise InstallerError(f"cannot parse ls-tree record: {record!r}")
        parts = meta.split()
        if len(parts) != 3:
            raise InstallerError(f"cannot parse ls-tree meta: {meta!r}")
        mode, typ, oid = parts
        if typ != "blob":
            raise InstallerError(
                f"installed-surface entry is not a blob: {relpath!r}"
            )
        if mode in ("120000", "160000"):
            raise InstallerError(
                f"installed-surface entry is a symlink/gitlink: {relpath!r}"
            )
        # A secret/credential-looking path is never staged, *committed
        # content included*: the committed tree is not an exemption from
        # the no-secret installed-copy invariant (defense in depth).
        if _is_secret_name(relpath):
            raise InstallerError(
                f"committed installed-surface path is a secret name: "
                f"{relpath!r}"
            )
        entries[relpath] = (mode, oid)
    return entries


def _pending_paths(root: Path, commit: str) -> Set[str]:
    """Paths under the installed surface whose worktree bytes differ from
    the bound commit (modified tracked files plus untracked non-ignored
    files), i.e. the Task-20-era additions the manifest flags ``pending``.
    """
    modified = _git_text_bounded(
        root,
        ["diff", "--name-only", "-z", commit, "--", *INSTALLED_SURFACE],
        maximum=MANIFEST_MAX,
    )
    untracked = _git_text_bounded(
        root,
        ["ls-files", "--others", "--exclude-standard", "-z", "--",
         *INSTALLED_SURFACE],
        maximum=MANIFEST_MAX,
    )
    pending = {path for path in modified.split("\0") if path}
    pending.update(path for path in untracked.split("\0") if path)
    return pending


# ---------------------------------------------------------------------------
# Path policy
# ---------------------------------------------------------------------------


def _is_secret_name(relpath: str) -> bool:
    """True when ``relpath`` names a secret/credential artifact.

    Secret/credential material must never reach an installed copy, so a
    dot-env file, a private-key/keystore suffix, an exact credential
    artifact/word, or a delimiter compound carrying two or more credential
    words fails closed even when the path is otherwise allowlisted.  A
    committed path is subject to the same rule (defense in depth: a
    committed tree is not an exemption).
    """
    for segment in relpath.split("/"):
        lowered = segment.lower()
        if lowered.startswith(".") and lowered.endswith(".env"):
            return True
        if lowered.endswith(_SECRET_NAME_SUFFIXES):
            return True
        stem = lowered.split(".", 1)[0]
        if stem in _SECRET_NAME_ARTIFACTS:
            return True
        tokens = set(re.split(r"[-_.]+", lowered))
        if stem in _SECRET_NAME_WORDS:
            return True
        if len(tokens & _SECRET_NAME_WORDS) >= 2:
            return True
    return False


def _validate_declared(rel: str) -> None:
    """A shared authority/entrypoint must be a safe, allowlisted, non-secret
    installed-surface path."""
    if footprint.safe_relpath(rel) is None:
        raise InstallerError(f"declared path is unsafe: {rel!r}")
    first = rel.split("/", 1)[0]
    if first not in ALLOWED_FIRST_SEGMENTS:
        raise InstallerError(
            f"declared path escapes the installed surface: {rel!r}"
        )
    if _is_secret_name(rel):
        raise InstallerError(f"declared path is a secret name: {rel!r}")


def _validate_pending(rel: str) -> None:
    """Reviewer-mode pending staging takes only the exact allowlisted
    Task-20 authorities; a stray or secret-looking worktree file under the
    installed surface fails closed."""
    if rel not in PENDING_ALLOWLIST:
        raise InstallerError(
            f"pending installed-surface path is not allowlisted for "
            f"reviewer staging: {rel!r}"
        )
    if _is_secret_name(rel):
        raise InstallerError(f"pending path is a secret name: {rel!r}")


# ---------------------------------------------------------------------------
# Staging
# ---------------------------------------------------------------------------


def _read_worktree_no_follow(path: Path, size: int) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        descriptor = os.open(str(path.absolute()), flags)
    except OSError as exc:
        raise InstallerError(f"cannot open {path}: {exc}") from exc
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_size != size:
            raise InstallerError(f"source changed while staging: {path}")
        chunks: List[bytes] = []
        remaining = size
        while remaining:
            chunk = os.read(descriptor, min(65536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        if remaining:
            raise InstallerError(f"source shrank while staging: {path}")
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def _mode_from_tree(mode: str, relpath: str) -> int:
    if mode == "100755":
        return 0o755
    if mode == "100644":
        return 0o644
    raise InstallerError(f"unsupported tracked mode for {relpath!r}: {mode}")


def _open_dir_nofollow(path: str, *, dir_fd: Optional[int] = None) -> int:
    """Open one directory component with O_DIRECTORY|O_NOFOLLOW|O_CLOEXEC.

    The component is resolved relative to ``dir_fd`` (openat): a symlink
    planted anywhere along an installed path fails closed instead of being
    followed, and the descriptor never leaks across an exec.
    """
    flags = (
        os.O_RDONLY
        | os.O_DIRECTORY
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )
    try:
        return os.open(path, flags, dir_fd=dir_fd)
    except OSError as exc:
        raise InstallerError(
            f"cannot open install directory component {path!r}: {exc}"
        ) from exc


def _revalidate_root(
    prefix: Path, root_fd: int, identity: Tuple[int, int], root: Path
) -> None:
    """Re-prove the anchored prefix identity and containment.

    Before every staged write and before the post-staging manifest verify,
    the anchored directory fd must still be the recorded inode, the named
    path must still resolve to that same inode (a symlink or bind-mount
    swap of the path is detected even though the fd keeps pointing at the
    original directory), and the resolved path must still be outside the
    resolved repository.  A swapped prefix therefore fails closed *before*
    any repository write can happen through it.
    """
    try:
        anchored = os.fstat(root_fd)
    except OSError as exc:
        raise InstallerError(
            f"cannot fstat the installed prefix anchor: {exc}"
        ) from exc
    if (anchored.st_dev, anchored.st_ino) != identity:
        raise InstallerError(
            "installed prefix anchor was replaced under the installer"
        )
    try:
        named = os.stat(prefix, follow_symlinks=False)
    except OSError as exc:
        raise InstallerError(
            f"installed prefix path vanished under the installer: {exc}"
        ) from exc
    if not stat.S_ISDIR(named.st_mode) or (
        named.st_dev, named.st_ino
    ) != identity:
        raise InstallerError(
            "installed prefix path was swapped (bind-mount/symlink)"
        )
    try:
        Path(os.path.realpath(prefix)).relative_to(Path(os.path.realpath(root)))
    except ValueError:
        return
    raise InstallerError(
        "installed prefix resolves inside the repository under the installer"
    )


def _create_prefix(root: Path, prefix: Path) -> Tuple[int, Tuple[int, int]]:
    """Create the fresh prefix and anchor it by an open directory fd.

    The prefix is created, opened with O_DIRECTORY|O_NOFOLLOW|O_CLOEXEC,
    and its ``(st_dev, st_ino)`` identity is recorded from the anchored
    descriptor *before* the mode is pinned; the named path must resolve to
    the same identity and the resolved path must stay outside the resolved
    repository, and only then is the mode pinned to private 0700 with
    ``fchmod`` on the fd (never a pathname chmod).  Returns
    ``(dirfd, identity)``; the caller owns the fd and must close it.
    """
    try:
        prefix.mkdir(mode=_DIR_MODE, parents=True)
    except OSError as exc:
        raise InstallerError(
            f"cannot create the installed prefix: {exc}"
        ) from exc
    fd = _open_dir_nofollow(str(prefix))
    try:
        info = os.fstat(fd)
        if not stat.S_ISDIR(info.st_mode):
            raise InstallerError(
                f"created prefix is not a directory: {prefix}"
            )
        identity = (info.st_dev, info.st_ino)
        try:
            named = os.stat(prefix, follow_symlinks=False)
        except OSError as exc:
            raise InstallerError(
                f"cannot stat the created prefix path: {exc}"
            ) from exc
        if not stat.S_ISDIR(named.st_mode) or (
            named.st_dev, named.st_ino
        ) != identity:
            raise InstallerError(
                f"created prefix path was swapped: {prefix}"
            )
        try:
            Path(os.path.realpath(prefix)).relative_to(Path(os.path.realpath(root)))
        except ValueError:
            pass
        else:
            raise InstallerError(
                f"prefix must be outside the repository: {prefix}"
            )
        os.fchmod(fd, _DIR_MODE)
        return fd, identity
    except BaseException:
        os.close(fd)
        raise


def _stage_bytes(
    prefix: Path,
    root_fd: int,
    root_identity: Tuple[int, int],
    root: Path,
    relpath: str,
    data: bytes,
    mode: int,
) -> None:
    """Stage one file under the anchored prefix with openat fd chains.

    The path is resolved component-by-component with O_NOFOLLOW directory
    opens anchored at the prefix dirfd: a symlink planted anywhere along the
    path fails closed, every created directory is private 0700 (created and
    pinned through the fd, never a pathname chmod), and the final file is
    created with O_EXCL|O_NOFOLLOW (a pre-existing or symlinked final name
    fails closed) and written through that fd.  The root anchor identity
    and containment are revalidated before the write, so a prefix path
    swapped to a bind-mount/symlink into the repository fails before any
    write can land through it.  No pathname write is ever performed: every
    creation/open/stat/chmod is dirfd-anchored.
    """
    if footprint.safe_relpath(relpath) is None:
        raise InstallerError(f"unsafe install path: {relpath!r}")
    _revalidate_root(prefix, root_fd, root_identity, root)
    parts = relpath.split("/")
    parent_fd = root_fd
    opened: List[int] = []
    try:
        for part in parts[:-1]:
            try:
                child = _open_dir_nofollow(part, dir_fd=parent_fd)
            except InstallerError as exc:
                # Missing ancestor: create it private-0700 through the fd
                # and open it anchored (a raced symlink then fails closed).
                try:
                    os.mkdir(part, _DIR_MODE, dir_fd=parent_fd)
                except FileExistsError:
                    pass
                except OSError as create_exc:
                    raise InstallerError(
                        f"cannot create install directory for {relpath!r}: "
                        f"{create_exc}"
                    ) from create_exc
                child = _open_dir_nofollow(part, dir_fd=parent_fd)
            info = os.fstat(child)
            if not stat.S_ISDIR(info.st_mode):
                os.close(child)
                raise InstallerError(
                    f"install path component is not a directory: {relpath!r}"
                )
            if stat.S_IMODE(info.st_mode) != _DIR_MODE:
                # Pin the privacy invariant through the fd (never a
                # pathname chmod), independent of the caller's umask.
                os.fchmod(child, _DIR_MODE)
            opened.append(child)
            parent_fd = child
        final = parts[-1]
        try:
            os.stat(final, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            pass
        except OSError as exc:
            raise InstallerError(
                f"cannot inspect install path {relpath!r}: {exc}"
            ) from exc
        else:
            raise InstallerError(
                f"install path already exists (fresh prefix only): {relpath!r}"
            )
        create_flags = (
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0)
        )
        try:
            fd = os.open(final, create_flags, 0o600, dir_fd=parent_fd)
        except OSError as exc:
            raise InstallerError(
                f"cannot create install file {relpath!r}: {exc}"
            ) from exc
        try:
            view = memoryview(data)
            while view:
                written = os.write(fd, view)
                if written <= 0:
                    raise InstallerError(
                        f"cannot write install file {relpath!r}"
                    )
                view = view[written:]
            os.fsync(fd)
            os.fchmod(fd, mode)
        finally:
            os.close(fd)
        # Verify the staged file through the fd chain.
        info = os.stat(final, dir_fd=parent_fd, follow_symlinks=False)
        if not stat.S_ISREG(info.st_mode) or info.st_size != len(data):
            raise InstallerError(
                f"staged file is not the expected regular file: {relpath!r}"
            )
        if stat.S_IMODE(info.st_mode) != mode:
            raise InstallerError(
                f"staged file mode is wrong: {relpath!r} "
                f"({oct(stat.S_IMODE(info.st_mode))} != {oct(mode)})"
            )
    finally:
        for fd in reversed(opened):
            os.close(fd)


def _stage_pending(
    root: Path,
    prefix: Path,
    root_fd: int,
    root_identity: Tuple[int, int],
    relpath: str,
) -> Dict[str, object]:
    source = root / relpath
    try:
        info = os.lstat(source)
    except OSError as exc:
        raise InstallerError(f"cannot stat pending source {relpath!r}: {exc}") from exc
    if stat.S_ISLNK(info.st_mode):
        raise InstallerError(f"pending source is a symlink: {relpath!r}")
    if not stat.S_ISREG(info.st_mode):
        raise InstallerError(
            f"pending source is not a regular file: {relpath!r} "
            f"(mode {oct(stat.S_IMODE(info.st_mode))})"
        )
    if info.st_size > BLOB_MAX:
        raise InstallerError(f"pending source exceeds the bound: {relpath!r}")
    mode = stat.S_IMODE(info.st_mode)
    if mode not in _ALLOWED_MODES:
        raise InstallerError(
            f"pending source mode is not allowed: {relpath!r} "
            f"({oct(mode)}; only 0644/0755)"
        )
    data = _read_worktree_no_follow(source, info.st_size)
    _stage_bytes(prefix, root_fd, root_identity, root, relpath, data, mode)
    return {
        "path": relpath,
        "mode": format(mode, "04o"),
        "size": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
        "blob": "",
        "pending": True,
    }


def _rollback_prefix(prefix: Path, identity: Optional[Tuple[int, int]]) -> None:
    """Remove the prefix directory created by this call (identity-safe).

    Only the exact directory identity (``(st_dev, st_ino)``) recorded at
    creation time is removed.  If the path was replaced by a symlink or a
    different directory — a race or an adversarial swap — the removal
    fails closed and leaves the foreign path untouched.
    """
    if identity is None:
        return
    try:
        current = os.lstat(prefix)
    except OSError:
        return
    if (current.st_dev, current.st_ino) != identity:
        return
    if not stat.S_ISDIR(current.st_mode) or stat.S_ISLNK(current.st_mode):
        return
    shutil.rmtree(str(prefix))


def install_harness(
    root: Path,
    prefix: Path,
    commit: str,
    *,
    shared: Sequence[str] = DEFAULT_SHARED,
    entrypoints: Sequence[str] = DEFAULT_ENTRYPOINTS,
    manifest_out: Optional[Path] = None,
) -> Dict[str, object]:
    """Stage the committed harness into a fresh test-owned prefix.

    Returns the manifest dict (and writes it to ``manifest_out`` when
    given).  Fails closed on any unsafe path, symlink, special inode,
    group/other-writable mode, pre-existing prefix entry, or prefix that is
    not absolute/outside the resolved repository; a declared shared
    authority/entrypoint outside the allowlisted first segments, a
    secret-named path, or a pending worktree file outside the exact
    reviewer allowlist also fails closed.  The created prefix is rolled
    back identity-safely on any failure after its creation.
    """
    root = Path(root).absolute()
    prefix = Path(prefix).absolute()
    if not SHA1.fullmatch(commit):
        raise InstallerError("the bound commit must be a strict 40-hex SHA")
    if not os.path.isdir(root):
        raise InstallerError(f"root is not a directory: {root}")
    _git_text_bounded(
        root, ["cat-file", "-e", f"{commit}^{{commit}}"], maximum=1024 * 1024
    )
    if not str(prefix).startswith("/"):
        raise InstallerError(f"prefix must be absolute: {prefix}")
    if os.path.islink(prefix) or prefix.exists():
        raise InstallerError(
            f"prefix must be a fresh, never-symlinked path: {prefix}"
        )
    try:
        Path(os.path.realpath(prefix)).relative_to(Path(os.path.realpath(root)))
    except ValueError:
        pass
    else:
        raise InstallerError(f"prefix must be outside the repository: {prefix}")
    declared = set(shared) | set(entrypoints)
    for rel in declared:
        _validate_declared(rel)

    prefix_created = False
    created_identity: Optional[Tuple[int, int]] = None
    root_fd = -1
    try:
        root_fd, created_identity = _create_prefix(root, prefix)
        prefix_created = True

        committed = _ls_tree_surface(root, commit)
        pending = _pending_paths(root, commit)

        manifest_files: List[Dict[str, object]] = []

        # 1. Committed (non-pending) harness content under the hidden
        #    namespaces, *excluding* the declared shared authorities and
        #    operator entrypoints so a clean committed install can never
        #    double-stage them (they are staged exactly once below).
        committed_only = {
            rel: (mode, oid)
            for rel, (mode, oid) in committed.items()
            if rel not in pending
            and (rel.startswith(".factory/") or rel.startswith(".pi/"))
            and rel not in declared
        }
        blobs = _committed_blobs(
            root, [oid for _, oid in committed_only.values()]
        )
        for rel, (mode, oid) in sorted(committed_only.items()):
            data = blobs.get(oid)
            if data is None:
                raise InstallerError(f"cannot read committed blob for {rel!r}")
            fs_mode = _mode_from_tree(mode, rel)
            _stage_bytes(prefix, root_fd, created_identity, root, rel, data, fs_mode)
            manifest_files.append(
                {
                    "path": rel,
                    "mode": format(fs_mode, "04o"),
                    "size": len(data),
                    "sha256": hashlib.sha256(data).hexdigest(),
                    "blob": oid,
                    "pending": False,
                }
            )

        # 2. Pending harness content under the hidden namespaces
        #    (Task-20-era additions): staged from the worktree only when on
        #    the exact reviewer allowlist, and explicitly flagged.  A
        #    visible-``scripts/`` worktree change that is not a declared
        #    authority is never an installed authority and is never staged.
        for rel in sorted(pending):
            if rel in declared:
                # Staged below as a shared authority or operator entrypoint.
                continue
            if rel.startswith("scripts/"):
                continue
            if rel.startswith(".factory/") or rel.startswith(".pi/"):
                _validate_pending(rel)
                manifest_files.append(
                    _stage_pending(root, prefix, root_fd, created_identity, rel)
                )

        # 3. Shared authorities and operator entrypoints (exactly once).
        shared_records: List[Dict[str, object]] = []
        entrypoint_records: List[Dict[str, object]] = []
        for rel in shared:
            if rel in pending:
                shared_records.append(
                    _stage_pending(root, prefix, root_fd, created_identity, rel)
                )
            elif rel in committed:
                mode, oid = committed[rel]
                data = _committed_blobs(root, [oid]).get(oid)
                if data is None:
                    raise InstallerError(f"cannot read committed blob for {rel!r}")
                fs_mode = _mode_from_tree(mode, rel)
                _stage_bytes(prefix, root_fd, created_identity, root, rel, data, fs_mode)
                shared_records.append(
                    {
                        "path": rel,
                        "mode": format(fs_mode, "04o"),
                        "size": len(data),
                        "sha256": hashlib.sha256(data).hexdigest(),
                        "blob": oid,
                        "pending": False,
                    }
                )
            else:
                raise InstallerError(f"shared authority is not installable: {rel!r}")
        for rel in entrypoints:
            if rel in pending:
                entrypoint_records.append(
                    _stage_pending(root, prefix, root_fd, created_identity, rel)
                )
            elif rel in committed:
                mode, oid = committed[rel]
                data = _committed_blobs(root, [oid]).get(oid)
                if data is None:
                    raise InstallerError(f"cannot read committed blob for {rel!r}")
                fs_mode = _mode_from_tree(mode, rel)
                _stage_bytes(prefix, root_fd, created_identity, root, rel, data, fs_mode)
                entrypoint_records.append(
                    {
                        "path": rel,
                        "mode": format(fs_mode, "04o"),
                        "size": len(data),
                        "sha256": hashlib.sha256(data).hexdigest(),
                        "blob": oid,
                        "pending": False,
                    }
                )
            else:
                raise InstallerError(f"entrypoint is not installable: {rel!r}")

        manifest: Dict[str, object] = {
            "schema": INSTALL_MANIFEST_SCHEMA,
            "installer": INSTALLER_VERSION,
            "commit": commit,
            "root": str(root),
            "prefix": str(prefix),
            "files": sorted(manifest_files, key=lambda entry: str(entry["path"])),
            "shared": sorted(shared_records, key=lambda entry: str(entry["path"])),
            "entrypoints": sorted(entrypoint_records, key=lambda entry: str(entry["path"])),
        }
        # Revalidate the anchor identity and containment before the
        # post-staging manifest verify walks the installed copy.
        _revalidate_root(prefix, root_fd, created_identity, root)
        errors = verify_staged(root, prefix, manifest)
        if errors:
            raise InstallerError(
                "post-staging verification failed:\n  " + "\n  ".join(errors)
            )
        if manifest_out is not None:
            _write_manifest(manifest_out, manifest, root)
    except BaseException:
        if prefix_created:
            _rollback_prefix(prefix, created_identity)
        raise
    finally:
        if root_fd >= 0:
            os.close(root_fd)
    return manifest


def _write_manifest(manifest_out: Path, manifest: Dict[str, object], root: Path) -> None:
    """Write the install manifest atomically, no-replace, outside the repo.

    The manifest is audit evidence for the reviewer: an existing target is
    never clobbered, and the file is never written into the repository
    (which would pollute the live tree with a runtime artifact).
    """
    manifest_out = Path(manifest_out).absolute()
    try:
        Path(os.path.realpath(manifest_out)).relative_to(Path(os.path.realpath(root)))
    except ValueError:
        pass
    else:
        raise InstallerError(
            f"install manifest must be written outside the repository: {manifest_out}"
        )
    if manifest_out.exists():
        raise InstallerError(
            f"install manifest output already exists (no-replace): {manifest_out}"
        )
    manifest_out.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(
        prefix=f".{manifest_out.name}.", dir=str(manifest_out.parent)
    )
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(
                json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
                + b"\n"
            )
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, str(manifest_out))
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


# ---------------------------------------------------------------------------
# Post-staging verification
# ---------------------------------------------------------------------------


def verify_staged(root: Path, prefix: Path, manifest: Dict[str, object]) -> List[str]:
    """Re-walk the installed prefix and prove the manifest claims.

    Exact physical set, exact modes, entrypoint executability, directory
    privacy (0700), regular single-link current-user-owned files, no
    symlink/special inode/device/mount crossing, the manifest prefix and
    root identity (the recorded prefix must be the walked prefix, the
    recorded root the verifying repository, the commit present there), the
    resolved prefix containment (outside the resolved repository), batched
    ``git hash-object --no-filters`` blob equality for committed content,
    and sha256 re-reads for pending content all fail closed.
    """
    errors: List[str] = []
    prefix = Path(prefix).absolute()
    root = Path(root).absolute()
    if os.path.islink(prefix) or not os.path.isdir(prefix):
        return [f"installed prefix is not a real directory: {prefix}"]
    if stat.S_IMODE(os.lstat(prefix).st_mode) != _DIR_MODE:
        errors.append(
            f"installed prefix is not private (0700): {prefix} "
            f"({oct(stat.S_IMODE(os.lstat(prefix).st_mode))})"
        )
    commit = str(manifest["commit"])
    if not SHA1.fullmatch(commit):
        return ["install manifest commit is invalid"]

    # Manifest prefix binding: the recorded prefix must be the walked prefix.
    recorded_prefix = manifest.get("prefix")
    if not isinstance(recorded_prefix, str) or (
        Path(recorded_prefix).absolute() != prefix
    ):
        errors.append(
            f"install manifest prefix does not bind this prefix "
            f"({recorded_prefix!r} != {prefix})"
        )
    # Root identity: the manifest records the exact repository root the
    # installer staged from; a different root cannot verify it, and the
    # bound commit must exist in that repository.
    recorded_root = manifest.get("root")
    if not isinstance(recorded_root, str) or (
        Path(recorded_root).absolute() != root
    ):
        errors.append(
            f"install manifest root does not bind this repository "
            f"({recorded_root!r} != {root})"
        )
    else:
        if not os.path.isdir(root):
            errors.append(f"repository root is not a directory: {root}")
        else:
            try:
                _git_text_bounded(
                    root,
                    ["cat-file", "-e", f"{commit}^{{commit}}"],
                    maximum=1024 * 1024,
                )
            except InstallerError as exc:
                errors.append(
                    f"bound commit is not present in the repository root: {exc}"
                )
    # Containment recheck on the resolved paths: a symlinked ancestor could
    # hide a prefix that actually lies inside the repository.
    try:
        Path(os.path.realpath(prefix)).relative_to(Path(os.path.realpath(root)))
    except ValueError:
        pass
    else:
        errors.append(f"installed prefix must be outside the repository: {prefix}")

    all_records = (
        list(manifest["files"])
        + list(manifest["shared"])
        + list(manifest["entrypoints"])
    )
    expected = {entry["path"] for entry in all_records}

    physical: Dict[str, os.stat_result] = {}
    prefix_dev = os.lstat(prefix).st_dev
    for rel, info in footprint._walk_nofollow(prefix, ""):
        if stat.S_ISDIR(info.st_mode):
            if stat.S_IMODE(info.st_mode) != _DIR_MODE:
                errors.append(
                    f"installed directory is not private (0700): {rel!r} "
                    f"({oct(stat.S_IMODE(info.st_mode))})"
                )
            continue
        physical[rel] = info
        if stat.S_ISLNK(info.st_mode):
            errors.append(f"installed symlink escapes: {rel!r}")
        elif not stat.S_ISREG(info.st_mode):
            errors.append(
                f"installed special file: {rel!r} "
                f"(mode {oct(stat.S_IMODE(info.st_mode))})"
            )
        elif info.st_uid != os.getuid():
            errors.append(f"installed file has foreign owner: {rel!r}")
        elif info.st_nlink != 1:
            errors.append(f"installed file is hardlinked: {rel!r}")
        elif info.st_dev != prefix_dev:
            errors.append(f"installed file crosses a mount point: {rel!r}")
    for rel in sorted(physical):
        if rel not in expected:
            errors.append(f"unmanifested physical file: {rel!r}")
    for rel in sorted(expected):
        if rel not in physical:
            errors.append(f"missing installed file: {rel!r}")

    # Exact modes per manifest entry, and entrypoint executability.
    for entry in all_records:
        rel = str(entry["path"])
        info = physical.get(rel)
        if info is None or not stat.S_ISREG(info.st_mode):
            continue  # missing/type already reported
        declared_mode = entry.get("mode")
        if isinstance(declared_mode, str):
            try:
                expected_mode = int(declared_mode, 8)
            except ValueError:
                errors.append(
                    f"installed mode field is invalid for {rel!r}: "
                    f"{declared_mode!r}"
                )
            else:
                if stat.S_IMODE(info.st_mode) != expected_mode:
                    errors.append(
                        f"installed mode mismatch for {rel!r} "
                        f"(expected {declared_mode}, got "
                        f"{format(stat.S_IMODE(info.st_mode), '04o')})"
                    )
    for entry in manifest.get("entrypoints", []):
        rel = str(entry["path"])
        info = physical.get(rel)
        if info is not None and stat.S_ISREG(info.st_mode) and not (
            info.st_mode & 0o111
        ):
            errors.append(f"installed entrypoint is not executable: {rel!r}")

    # Committed content: batched raw blob-exactness through the pinned
    # boundary.  ``--no-filters`` hashes the installed bytes exactly as
    # staged (no content filter can rewrite them), and the whole transcript
    # is bounded by the fair capture.
    committed_records = [entry for entry in all_records if not entry.get("pending")]
    if committed_records:
        staged_paths = [
            str(prefix / str(entry["path"])) for entry in committed_records
        ]
        payload = b"".join(path.encode("utf-8") + b"\n" for path in staged_paths)
        out = _git_bytes_bounded(
            root,
            ["hash-object", "--no-filters", "--stdin-paths"],
            maximum=MANIFEST_MAX,
            input=payload,
        )
        # ``git hash-object --stdin-paths`` prints one oid per line in the
        # same order as the stdin paths, so the output zips onto the staged
        # paths; a count mismatch or a malformed oid is a fail-closed parse
        # error.
        oids_out = [line for line in out.splitlines() if line]
        if len(oids_out) != len(staged_paths):
            raise InstallerError(
                "hash-object output count does not match the staged files "
                f"({len(oids_out)} != {len(staged_paths)})"
            )
        oid_by_path: Dict[str, str] = {}
        for path, oid in zip(staged_paths, oids_out):
            try:
                decoded = oid.decode("ascii")
            except UnicodeDecodeError as exc:
                raise InstallerError(
                    f"hash-object output is not ASCII: {exc}"
                ) from exc
            if not SHA1.fullmatch(decoded):
                raise InstallerError(
                    f"hash-object output is not a strict SHA: {decoded!r}"
                )
            oid_by_path[path] = decoded
        for entry in committed_records:
            path = str(prefix / str(entry["path"]))
            oid = oid_by_path.get(path)
            if oid != entry.get("blob"):
                errors.append(
                    f"installed bytes differ from the committed blob for "
                    f"{entry['path']!r} (expected {entry.get('blob')}, "
                    f"got {oid})"
                )

    # Pending content: sha256 re-read against the recorded digest.
    for entry in all_records:
        if not entry.get("pending"):
            continue
        rel = str(entry["path"])
        info = physical.get(rel)
        if info is None or not stat.S_ISREG(info.st_mode):
            continue
        try:
            raw = footprint._read_regular_bounded(
                prefix / rel, info.st_size, "installed pending file"
            )
        except footprint.FootprintError as exc:
            errors.append(str(exc))
            continue
        digest = hashlib.sha256(raw).hexdigest()
        if digest != entry.get("sha256"):
            errors.append(
                f"pending installed digest mismatch for {rel!r} "
                f"(expected {entry.get('sha256')}, got {digest})"
            )
    return errors


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="factory-installer",
        description=(
            "Trusted harness installer/stager for installed-tier evidence "
            "(FACTORY-LOOP-SPEC §19/§3; Task 20). Stages only committed "
            "harness content into a fresh test-owned prefix."
        ),
    )
    parser.add_argument(
        "command", choices=("install",), help="the only installer operation"
    )
    parser.add_argument("--root", required=True, metavar="ROOT")
    parser.add_argument("--commit", required=True, metavar="SHA")
    parser.add_argument("--prefix", required=True, metavar="PREFIX")
    parser.add_argument(
        "--manifest-out", metavar="FILE", default=None,
        help="write the install manifest JSON to FILE (outside the repo, "
        "never replacing an existing file)",
    )
    parser.add_argument("--json", action="store_true", help="print the manifest JSON")
    args = parser.parse_args(argv)
    try:
        manifest = install_harness(
            Path(args.root),
            Path(args.prefix),
            args.commit,
            manifest_out=Path(args.manifest_out) if args.manifest_out else None,
        )
    except InstallerError as exc:
        print(f"factory-installer: {exc}", file=sys.stderr)
        return 1
    if args.json or not args.manifest_out:
        print(json.dumps(manifest, sort_keys=True))
    print(
        f"factory-installer: installed {len(manifest['files'])} harness file(s) "
        f"at {manifest['prefix']} bound to {manifest['commit'][:12]}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
