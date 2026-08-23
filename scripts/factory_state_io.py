#!/usr/bin/env python3
"""Dirfd-bound, no-follow I/O for private .factory-state lifecycle files."""

from __future__ import annotations

from contextlib import contextmanager
import json
import os
from pathlib import Path
import re
import secrets
import stat
import sys
from typing import Callable, Iterator, TypeVar

T = TypeVar("T")

SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")

# Close-on-exec on every descriptor the state authority opens (F2): the
# directory, marker, and temporary descriptors must never be inherited
# across an exec boundary into a model/verifier process, even by a child
# spawned with ``close_fds=False``.
_CLOEXEC = getattr(os, "O_CLOEXEC", 0)


class StateIOError(RuntimeError):
    pass


def require_linux_primitives() -> None:
    """Fail closed when any no-follow/dirfd/close-on-exec primitive is unavailable."""
    required = ("O_NOFOLLOW", "O_DIRECTORY", "O_CLOEXEC")
    missing = [name for name in required if not hasattr(os, name)]
    dirfd_functions = (os.open, os.stat, os.unlink, os.rename, os.link)
    if (
        sys.platform != "linux"
        or missing
        or any(function not in os.supports_dir_fd for function in dirfd_functions)
        or not Path("/proc/self/fd").is_dir()
    ):
        detail = ", ".join(missing) if missing else "dirfd/proc"
        raise StateIOError(f"required Linux no-follow primitives are unavailable: {detail}")


def _name(name: str) -> str:
    if not SAFE_NAME.fullmatch(name) or name in {".", ".."}:
        raise StateIOError(f"unsafe lifecycle marker name: {name!r}")
    return name


def _internal_orphan_name(name: str) -> str:
    """Validate a narrowly scoped internal orphan/quarantine marker name.

    The established atomic writer itself creates orphaned markers whose names
    are ``.{marker}.{32-hex}`` (a torn temporary that never linked) and
    ``.{marker}.quarantine-{32-hex}`` (the validated predecessor of an
    interrupted update).  Those names legitimately start with a dot, so they
    cannot pass :func:`_name`; recovery accepts them only through this exact
    shape check (used as the ``name_validator`` for internal reads), never
    through a relaxed public-name rule.  Any other name fails closed.
    """
    if not _INTERNAL_ORPHAN_RE.fullmatch(name):
        raise StateIOError(f"unsafe internal orphan marker name: {name!r}")
    return name


_INTERNAL_ORPHAN_RE = re.compile(
    r"^\.(?P<base>[A-Za-z0-9][A-Za-z0-9._-]{0,127})"
    r"(?:\.quarantine-|\.)[0-9a-f]{32}$"
)


def _resolve_expected_uid(_expected_uid: int | None) -> int:
    """Resolve the internal expected owner UID (default: the current user).

    Every owner check in this module compares real ``stat`` metadata against
    this expected UID, so a deterministic always-runnable test can pass a
    wrong expected UID and exercise the exact owner-rejection branch with
    real stat metadata and without requiring ``chown`` (Task 19 S7).  The
    knob is underscore-private ``_expected_uid`` precisely because it is an
    internal test hook: production never passes it (the real owner check
    always compares against the current UID) and no CLI surface can set it.
    """
    return os.getuid() if _expected_uid is None else _expected_uid


def _validate_directory(info: os.stat_result, *, _expected_uid: int) -> None:
    if (
        not stat.S_ISDIR(info.st_mode)
        or info.st_uid != _expected_uid
        or info.st_mode & 0o077
    ):
        raise StateIOError(".factory-state must be a private owned directory")


def _validate_file(
    info: os.stat_result,
    *,
    _expected_uid: int,
    maximum: int,
    allow_linked: bool = False,
) -> None:
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid != _expected_uid
        or (info.st_nlink not in (1, 2) if allow_linked else info.st_nlink != 1)
        or info.st_mode & 0o022
        or info.st_size > maximum
    ):
        raise StateIOError("unsafe lifecycle marker")


@contextmanager
def state_dir(
    root: Path, *, create: bool = False, _expected_uid: int | None = None
) -> Iterator[int]:
    require_linux_primitives()
    expected = _resolve_expected_uid(_expected_uid)
    root = root.absolute()
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | _CLOEXEC
    root_fd = os.open(root, flags)
    directory_fd: int | None = None
    try:
        try:
            info = os.stat(".factory-state", dir_fd=root_fd, follow_symlinks=False)
        except FileNotFoundError:
            if not create:
                raise StateIOError(".factory-state is missing")
            os.mkdir(".factory-state", 0o700, dir_fd=root_fd)
            os.fsync(root_fd)
            info = os.stat(".factory-state", dir_fd=root_fd, follow_symlinks=False)
        _validate_directory(info, _expected_uid=expected)
        directory_fd = os.open(".factory-state", flags, dir_fd=root_fd)
        opened = os.fstat(directory_fd)
        _validate_directory(opened, _expected_uid=expected)
        if (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
            raise StateIOError(".factory-state changed while being opened")
        yield directory_fd
    finally:
        if directory_fd is not None:
            os.close(directory_fd)
        os.close(root_fd)


def read_bytes(
    root: Path,
    name: str,
    *,
    maximum: int = 1024 * 1024,
    missing_ok: bool = False,
    name_validator: Callable[[str], str] | None = None,
    _expected_uid: int | None = None,
    allow_linked: bool = False,
) -> bytes | None:
    if name_validator is None:
        name = _name(name)
    else:
        name = name_validator(name)
    expected = _resolve_expected_uid(_expected_uid)
    with state_dir(root, _expected_uid=expected) as directory_fd:
        try:
            descriptor = os.open(
                name,
                os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | _CLOEXEC,
                dir_fd=directory_fd,
            )
        except FileNotFoundError:
            if missing_ok:
                return None
            raise StateIOError(f"lifecycle marker is missing: {name}")
        except OSError as exc:
            raise StateIOError(f"cannot safely open lifecycle marker {name}: {exc}") from exc
        try:
            before = os.fstat(descriptor)
            _validate_file(
                before, _expected_uid=expected, maximum=maximum,
                allow_linked=allow_linked,
            )
            named = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            if (before.st_dev, before.st_ino) != (named.st_dev, named.st_ino):
                raise StateIOError(f"lifecycle marker changed while opening: {name}")
            chunks: list[bytes] = []
            remaining = maximum + 1
            while remaining:
                chunk = os.read(descriptor, min(65536, remaining))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            data = b"".join(chunks)
            after = os.fstat(descriptor)
            named_after = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            if (
                len(data) > maximum
                or (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
                != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
                or (after.st_dev, after.st_ino) != (named_after.st_dev, named_after.st_ino)
            ):
                raise StateIOError(f"lifecycle marker changed while reading: {name}")
            return data
        finally:
            os.close(descriptor)


def read_text(
    root: Path,
    name: str,
    *,
    maximum: int = 1024 * 1024,
    missing_ok: bool = False,
    name_validator: Callable[[str], str] | None = None,
    _expected_uid: int | None = None,
    allow_linked: bool = False,
) -> str | None:
    raw = read_bytes(
        root, name, maximum=maximum, missing_ok=missing_ok,
        name_validator=name_validator, _expected_uid=_expected_uid,
        allow_linked=allow_linked,
    )
    if raw is None:
        return None
    try:
        return raw.decode("utf-8")
    except UnicodeError as exc:
        raise StateIOError(f"lifecycle marker is not UTF-8: {name}") from exc


def read_json(
    root: Path,
    name: str,
    *,
    maximum: int = 1024 * 1024,
    missing_ok: bool = False,
    name_validator: Callable[[str], str] | None = None,
    _expected_uid: int | None = None,
    allow_linked: bool = False,
) -> object | None:
    text = read_text(
        root, name, maximum=maximum, missing_ok=missing_ok,
        name_validator=name_validator, _expected_uid=_expected_uid,
        allow_linked=allow_linked,
    )
    if text is None:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise StateIOError(f"invalid JSON lifecycle marker {name}: {exc}") from exc


def atomic_write(
    root: Path,
    name: str,
    data: bytes,
    *,
    create_directory: bool = True,
    no_replace: bool = False,
) -> None:
    """Atomically publish ``data`` under ``name`` inside the private directory.

    ``no_replace=True`` gives atomic no-replace semantics (Task 19 S1): the
    existence check and the ``linkat`` publication happen inside one locked
    directory scope, so the call can never clobber an existing marker or a
    raced pathname; an existing target fails closed with a dedicated error
    without touching it (no quarantine, no temporary file), exactly as a
    fresh campaign init must.
    """
    name = _name(name)
    if len(data) > 1024 * 1024:
        raise StateIOError("lifecycle marker exceeds 1 MiB")
    with state_dir(root, create=create_directory) as directory_fd:
        try:
            existing = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        except FileNotFoundError:
            existing = None
        if existing is not None:
            if no_replace:
                raise StateIOError(
                    f"refusing to overwrite lifecycle marker {name}"
                )
            _validate_file(
                existing, _expected_uid=os.getuid(), maximum=1024 * 1024
            )
        temporary = f".{name}.{secrets.token_hex(16)}"
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | _CLOEXEC,
            0o600,
            dir_fd=directory_fd,
        )
        temporary_exists = True
        quarantine: str | None = None
        try:
            view = memoryview(data)
            while view:
                written = os.write(descriptor, view)
                if written <= 0:
                    raise StateIOError("short lifecycle marker write")
                view = view[written:]
            os.fsync(descriptor)
            os.close(descriptor)
            descriptor = -1
            if existing is not None:
                current = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
                if (current.st_dev, current.st_ino) != (existing.st_dev, existing.st_ino):
                    raise StateIOError(f"lifecycle marker replaced before update: {name}")
                quarantine = f".{name}.quarantine-{secrets.token_hex(16)}"
                os.rename(name, quarantine, src_dir_fd=directory_fd, dst_dir_fd=directory_fd)
                os.fsync(directory_fd)
                quarantined = os.stat(quarantine, dir_fd=directory_fd, follow_symlinks=False)
                _validate_file(
                    quarantined, _expected_uid=os.getuid(), maximum=1024 * 1024
                )
                if (quarantined.st_dev, quarantined.st_ino) != (existing.st_dev, existing.st_ino):
                    raise StateIOError(f"lifecycle marker substituted at quarantine: {name}")
            try:
                # linkat publishes only if the canonical name is still absent;
                # unlike rename it cannot silently replace a raced pathname.
                os.link(
                    temporary, name,
                    src_dir_fd=directory_fd, dst_dir_fd=directory_fd,
                    follow_symlinks=False,
                )
            except FileExistsError as exc:
                raise StateIOError(f"lifecycle marker raced during update: {name}") from exc
            os.unlink(temporary, dir_fd=directory_fd)
            temporary_exists = False
            if quarantine is not None:
                os.unlink(quarantine, dir_fd=directory_fd)
                quarantine = None
            os.fsync(directory_fd)
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            if temporary_exists:
                try:
                    os.unlink(temporary, dir_fd=directory_fd)
                except FileNotFoundError:
                    pass
            # A validated original is intentionally retained under quarantine
            # on publication failure so a raced pathname is never unlinked and
            # durable state is not destroyed silently.


def atomic_write_text(root: Path, name: str, value: str, *, no_replace: bool = False) -> None:
    atomic_write(root, name, value.encode("utf-8"), no_replace=no_replace)


def atomic_write_json(
    root: Path, name: str, value: object, *, indent: int | None = None,
    no_replace: bool = False,
) -> None:
    raw = json.dumps(value, sort_keys=True, indent=indent, separators=None if indent else (",", ":"))
    atomic_write_text(root, name, raw + "\n", no_replace=no_replace)


def consume_json(
    root: Path,
    name: str,
    validator: Callable[[object], T],
    *,
    maximum: int = 16384,
    after_final_check: Callable[[], None] | None = None,
) -> T:
    """Validate and one-shot consume JSON through one retained descriptor."""
    name = _name(name)
    with state_dir(root) as directory_fd:
        descriptor: int | None = None
        try:
            descriptor = os.open(
                name, os.O_RDONLY | os.O_NOFOLLOW | _CLOEXEC,
                dir_fd=directory_fd,
            )
            before = os.fstat(descriptor)
            named = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            _validate_file(before, _expected_uid=os.getuid(), maximum=maximum)
            _validate_file(named, _expected_uid=os.getuid(), maximum=maximum)
            if (before.st_dev, before.st_ino) != (named.st_dev, named.st_ino):
                raise StateIOError(f"lifecycle marker changed while opening: {name}")
            raw = os.read(descriptor, maximum + 1)
            after_read = os.fstat(descriptor)
            if (
                len(raw) > maximum
                or (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
                != (
                    after_read.st_dev, after_read.st_ino,
                    after_read.st_size, after_read.st_mtime_ns,
                )
            ):
                raise StateIOError(f"lifecycle marker changed while reading: {name}")
            try:
                data = json.loads(raw.decode("utf-8"))
            except (UnicodeError, json.JSONDecodeError) as exc:
                raise StateIOError(f"invalid JSON lifecycle marker {name}: {exc}") from exc
            result = validator(data)
            opened = os.fstat(descriptor)
            identity = (before.st_dev, before.st_ino)
            if (
                (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns)
                != (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
            ):
                raise StateIOError(f"lifecycle marker changed before consumption: {name}")
            if after_final_check is not None:
                after_final_check()
            quarantine = f".{name}.quarantine-{secrets.token_hex(16)}"
            os.rename(name, quarantine, src_dir_fd=directory_fd, dst_dir_fd=directory_fd)
            os.fsync(directory_fd)
            quarantined = os.stat(quarantine, dir_fd=directory_fd, follow_symlinks=False)
            opened = os.fstat(descriptor)
            if (
                (quarantined.st_dev, quarantined.st_ino) != identity
                or (opened.st_dev, opened.st_ino) != identity
            ):
                raise StateIOError(f"lifecycle marker substituted at quarantine: {name}")
            _validate_file(
                quarantined, _expected_uid=os.getuid(), maximum=maximum
            )
            _validate_file(opened, _expected_uid=os.getuid(), maximum=maximum)
            os.unlink(quarantine, dir_fd=directory_fd)
            os.fsync(directory_fd)
            return result
        finally:
            if descriptor is not None:
                os.close(descriptor)


def remove(
    root: Path,
    name: str,
    *,
    missing_ok: bool = True,
    after_final_check: Callable[[], None] | None = None,
) -> None:
    name = _name(name)
    with state_dir(root) as directory_fd:
        descriptor: int | None = None
        try:
            try:
                descriptor = os.open(
                    name, os.O_RDONLY | os.O_NOFOLLOW | _CLOEXEC,
                    dir_fd=directory_fd,
                )
            except FileNotFoundError:
                if missing_ok:
                    return
                raise StateIOError(f"lifecycle marker is missing: {name}")
            before = os.fstat(descriptor)
            named = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            _validate_file(
                before, _expected_uid=os.getuid(), maximum=1024 * 1024
            )
            _validate_file(named, _expected_uid=os.getuid(), maximum=1024 * 1024)
            if (named.st_dev, named.st_ino) != (before.st_dev, before.st_ino):
                raise StateIOError(f"lifecycle marker changed while opening: {name}")
            current = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            if (current.st_dev, current.st_ino) != (before.st_dev, before.st_ino):
                raise StateIOError(f"lifecycle marker replaced before removal: {name}")
            if after_final_check is not None:
                after_final_check()
            quarantine = f".{name}.quarantine-{secrets.token_hex(16)}"
            os.rename(name, quarantine, src_dir_fd=directory_fd, dst_dir_fd=directory_fd)
            os.fsync(directory_fd)
            quarantined = os.stat(quarantine, dir_fd=directory_fd, follow_symlinks=False)
            opened = os.fstat(descriptor)
            identity = (before.st_dev, before.st_ino)
            if (
                (quarantined.st_dev, quarantined.st_ino) != identity
                or (opened.st_dev, opened.st_ino) != identity
            ):
                raise StateIOError(f"lifecycle marker substituted at quarantine: {name}")
            _validate_file(
                quarantined, _expected_uid=os.getuid(), maximum=1024 * 1024
            )
            _validate_file(opened, _expected_uid=os.getuid(), maximum=1024 * 1024)
            os.unlink(quarantine, dir_fd=directory_fd)
            os.fsync(directory_fd)
        finally:
            if descriptor is not None:
                os.close(descriptor)
