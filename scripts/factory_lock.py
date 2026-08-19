#!/usr/bin/env python3
"""Linux repository-root flock acquisition and inherited-FD validation."""

from __future__ import annotations

from contextlib import contextmanager
import fcntl
import os
from pathlib import Path
import secrets
import stat
import subprocess
import sys
from typing import Callable, Iterator

LEGACY_NAME = ".factory-lock"
ENV_KEYS = (
    "FACTORY_LOCK_HELD",
    "FACTORY_LOCK_FD",
    "FACTORY_LOCK_ID",
    "FACTORY_LOCK_ROOT",
)


class FactoryLockError(RuntimeError):
    pass


def require_linux_primitives() -> None:
    required = ("O_NOFOLLOW", "O_DIRECTORY")
    missing = [name for name in required if not hasattr(os, name)]
    dirfd_functions = (os.open, os.stat, os.unlink, os.rename)
    if (
        sys.platform != "linux"
        or missing
        or any(function not in os.supports_dir_fd for function in dirfd_functions)
        or not Path("/proc/self/fd").is_dir()
    ):
        detail = ", ".join(missing) if missing else "dirfd/flock/proc"
        raise FactoryLockError(f"required Linux no-follow primitives are unavailable: {detail}")


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=root, text=True, capture_output=True,
        env={**os.environ, "GIT_NO_REPLACE_OBJECTS": "1"},
    )
    if result.returncode:
        raise FactoryLockError(f"cannot resolve repository lock state: {' '.join(args)}")
    return result.stdout.strip()


def _validate_directory(info: os.stat_result, label: str) -> None:
    forbidden = 0o022
    if (
        not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.getuid()
        or info.st_mode & forbidden
    ):
        raise FactoryLockError(f"unsafe {label}")


def _validate_regular(info: os.stat_result, label: str) -> None:
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.getuid()
        or info.st_nlink != 1
        or info.st_mode & 0o022
    ):
        raise FactoryLockError(f"unsafe {label}")


def repository_root(root: Path) -> Path:
    require_linux_primitives()
    root = root.absolute()
    try:
        resolved = root.resolve(strict=True)
        named = root.lstat()
    except OSError as exc:
        raise FactoryLockError(f"repository root is unavailable: {exc}") from exc
    if resolved != root or stat.S_ISLNK(named.st_mode):
        raise FactoryLockError("repository root must be a canonical path without symlinks")
    _validate_directory(named, "repository root")
    top = Path(_git(root, "rev-parse", "--path-format=absolute", "--show-toplevel"))
    if top.absolute() != root:
        raise FactoryLockError("factory lock root is not the Git top level")
    git_marker = root / ".git"
    try:
        git_info = git_marker.lstat()
    except OSError as exc:
        raise FactoryLockError(f"Git directory is unavailable: {exc}") from exc
    if not stat.S_ISDIR(git_info.st_mode) or stat.S_ISLNK(git_info.st_mode):
        raise FactoryLockError("Git worktrees or redirected Git directories are forbidden")
    return root


def _open_root(root: Path) -> int:
    root = repository_root(root)
    descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        opened = os.fstat(descriptor)
        named = root.lstat()
        _validate_directory(opened, "open repository root")
        _validate_directory(named, "repository root")
        if (opened.st_dev, opened.st_ino) != (named.st_dev, named.st_ino):
            raise FactoryLockError("repository root changed while opening")
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def validate_open_lock(descriptor: int, root: Path) -> os.stat_result:
    root = repository_root(root)
    try:
        opened = os.fstat(descriptor)
        named = root.lstat()
        _validate_directory(opened, "open repository root")
        _validate_directory(named, "repository root")
        if (opened.st_dev, opened.st_ino) != (named.st_dev, named.st_ino):
            raise FactoryLockError("inherited factory lock is not the canonical repository root")
        expected_root = os.environ.get("FACTORY_LOCK_ROOT")
        if expected_root and Path(expected_root).absolute() != root:
            raise FactoryLockError("inherited factory lock root changed")
        return opened
    except OSError as exc:
        raise FactoryLockError(f"cannot validate factory lock: {exc}") from exc


def inherited_descriptor(root: Path) -> int | None:
    if os.environ.get("FACTORY_LOCK_HELD") != "1":
        return None
    raw = os.environ.get("FACTORY_LOCK_FD", "")
    try:
        descriptor = int(raw)
    except ValueError as exc:
        raise FactoryLockError("invalid inherited factory lock descriptor") from exc
    if descriptor < 3:
        raise FactoryLockError("invalid inherited factory lock descriptor")
    opened = validate_open_lock(descriptor, root)
    if os.environ.get("FACTORY_LOCK_ID") != f"{opened.st_dev}:{opened.st_ino}":
        raise FactoryLockError("inherited factory lock identity changed")
    try:
        # This succeeds only for a descriptor sharing the owner's open-file
        # description. A separately opened repository FD remains contended.
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        raise FactoryLockError("inherited descriptor does not own the factory lock") from exc
    except OSError as exc:
        raise FactoryLockError(f"cannot verify inherited factory lock: {exc}") from exc
    validate_open_lock(descriptor, root)
    return descriptor


def _quarantine_locked_file(
    directory_fd: int,
    name: str,
    descriptor: int,
    expected: os.stat_result,
    label: str,
    *,
    after_final_check: Callable[[], None] | None = None,
) -> None:
    opened = os.fstat(descriptor)
    named = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    _validate_regular(opened, label)
    _validate_regular(named, label)
    identity = (expected.st_dev, expected.st_ino)
    if (opened.st_dev, opened.st_ino) != identity or (named.st_dev, named.st_ino) != identity:
        raise FactoryLockError(f"{label} changed during migration")
    if after_final_check is not None:
        after_final_check()
    quarantine = f".{name}.quarantine-{secrets.token_hex(16)}"
    os.rename(name, quarantine, src_dir_fd=directory_fd, dst_dir_fd=directory_fd)
    os.fsync(directory_fd)
    quarantined = os.stat(quarantine, dir_fd=directory_fd, follow_symlinks=False)
    opened_after = os.fstat(descriptor)
    if (
        (quarantined.st_dev, quarantined.st_ino) != identity
        or (opened_after.st_dev, opened_after.st_ino) != identity
    ):
        raise FactoryLockError(f"{label} was replaced at quarantine")
    _validate_regular(quarantined, label)
    _validate_regular(opened_after, label)
    os.unlink(quarantine, dir_fd=directory_fd)
    os.fsync(directory_fd)


def _migrate_root_legacy(
    root_fd: int,
    *,
    after_final_check: Callable[[], None] | None = None,
) -> None:
    try:
        descriptor = os.open(
            LEGACY_NAME, os.O_RDWR | os.O_APPEND | os.O_NOFOLLOW, dir_fd=root_fd,
        )
    except FileNotFoundError:
        return
    except OSError as exc:
        raise FactoryLockError(f"cannot safely migrate legacy factory lock: {exc}") from exc
    try:
        opened = os.fstat(descriptor)
        named = os.stat(LEGACY_NAME, dir_fd=root_fd, follow_symlinks=False)
        _validate_regular(opened, "legacy factory lock")
        _validate_regular(named, "legacy factory lock")
        if (opened.st_dev, opened.st_ino) != (named.st_dev, named.st_ino):
            raise FactoryLockError("legacy factory lock changed while opening")
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise FactoryLockError("legacy factory lock is still held") from exc
        _quarantine_locked_file(
            root_fd, LEGACY_NAME, descriptor, opened, "legacy factory lock",
            after_final_check=after_final_check,
        )
    finally:
        os.close(descriptor)


def acquire(root: Path, *, inheritable: bool) -> int:
    root = repository_root(root)
    descriptor = _open_root(root)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise FactoryLockError("another planner, worker, or recovery process is active") from exc
        _migrate_root_legacy(descriptor)
        opened = validate_open_lock(descriptor, root)
        os.set_inheritable(descriptor, inheritable)
        os.environ["FACTORY_LOCK_HELD"] = "1"
        os.environ["FACTORY_LOCK_FD"] = str(descriptor)
        os.environ["FACTORY_LOCK_ID"] = f"{opened.st_dev}:{opened.st_ino}"
        os.environ["FACTORY_LOCK_ROOT"] = str(root)
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _clear_environment() -> None:
    for key in ENV_KEYS:
        os.environ.pop(key, None)


@contextmanager
def locked(root: Path) -> Iterator[int]:
    descriptor = inherited_descriptor(root)
    close = descriptor is None
    if descriptor is None:
        descriptor = acquire(root, inheritable=False)
    try:
        validate_open_lock(descriptor, root)
        yield descriptor
        validate_open_lock(descriptor, root)
    finally:
        if close:
            os.close(descriptor)
            _clear_environment()
