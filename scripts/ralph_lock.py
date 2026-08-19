#!/usr/bin/env python3
"""Race-safe stale-owner validation for Ralph's volatile loop lock."""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import secrets
import stat
import sys
from typing import Callable

BOOT_ID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


class RalphLockError(RuntimeError):
    pass


def require_linux_primitives() -> None:
    if (
        sys.platform != "linux"
        or not hasattr(os, "O_NOFOLLOW")
        or not hasattr(os, "O_DIRECTORY")
        or any(function not in os.supports_dir_fd for function in (os.open, os.stat, os.rename, os.unlink))
        or not Path("/proc/self/fd").is_dir()
    ):
        raise RalphLockError("required Linux no-follow/dirfd/proc primitives are unavailable")


def current_boot_id() -> str | None:
    try:
        value = Path("/proc/sys/kernel/random/boot_id").read_text(encoding="ascii").strip().lower()
    except OSError:
        return None
    return value if BOOT_ID.fullmatch(value) else None


def process_start_time(pid: int) -> int | None:
    try:
        raw = Path(f"/proc/{pid}/stat").read_text(encoding="ascii")
    except (FileNotFoundError, ProcessLookupError):
        return None
    except OSError as exc:
        raise RalphLockError(f"cannot inspect process {pid}: {exc}") from exc
    close = raw.rfind(")")
    if close < 0:
        raise RalphLockError(f"ambiguous process stat for PID {pid}")
    fields = raw[close + 2 :].split()
    if len(fields) <= 19:
        raise RalphLockError(f"ambiguous process stat for PID {pid}")
    try:
        value = int(fields[19])
    except ValueError as exc:
        raise RalphLockError(f"ambiguous process start time for PID {pid}") from exc
    return value if value > 0 else None


def parse_record(raw: bytes) -> dict:
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise RalphLockError(f"invalid Ralph loop lock: {exc}") from exc
    if not isinstance(data, dict) or not isinstance(data.get("pid"), int) or data["pid"] <= 0:
        raise RalphLockError("Ralph loop lock has no unambiguous positive PID")
    has_boot = "boot_id" in data
    has_start = "start_time" in data
    if has_boot != has_start:
        raise RalphLockError("Ralph loop lock has a partial process identity")
    if has_boot:
        boot = data["boot_id"]
        start = data["start_time"]
        if (
            not isinstance(boot, str)
            or not BOOT_ID.fullmatch(boot.lower())
            or not isinstance(start, int)
            or start <= 0
        ):
            raise RalphLockError("Ralph loop lock has an invalid process identity")
        data["boot_id"] = boot.lower()
    return data


def _validate_file(info: os.stat_result) -> None:
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.getuid()
        or info.st_nlink != 1
        or info.st_mode & 0o022
        or info.st_size > 16384
    ):
        raise RalphLockError("unsafe Ralph loop lock")


def owner_is_stale(record: dict) -> bool:
    pid = record["pid"]
    start = process_start_time(pid)
    if start is None:
        return True
    if "boot_id" not in record:
        raise RalphLockError(f"live PID {pid} owns an unbound Ralph loop lock")
    boot = current_boot_id()
    if boot is None:
        raise RalphLockError("cannot determine the current boot identity")
    if record["boot_id"] == boot and record["start_time"] == start:
        raise RalphLockError(f"live process {pid} owns the Ralph loop lock")
    # The PID exists but is demonstrably a different process or boot.
    return True


def remove_stale_lock(
    ralph_directory: Path,
    *,
    dry_run: bool = False,
    before_unlink: Callable[[], None] | None = None,
) -> dict | None:
    require_linux_primitives()
    directory_fd = os.open(
        ralph_directory,
        os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
    )
    descriptor: int | None = None
    try:
        directory_info = os.fstat(directory_fd)
        directory_named = ralph_directory.lstat()
        if (
            not stat.S_ISDIR(directory_info.st_mode)
            or directory_info.st_uid != os.getuid()
            or directory_info.st_mode & 0o022
            or (directory_info.st_dev, directory_info.st_ino)
            != (directory_named.st_dev, directory_named.st_ino)
        ):
            raise RalphLockError("unsafe .ralph directory")
        try:
            descriptor = os.open(
                "loop.lock",
                os.O_RDONLY | os.O_NOFOLLOW,
                dir_fd=directory_fd,
            )
        except FileNotFoundError:
            return None
        before = os.fstat(descriptor)
        _validate_file(before)
        named = os.stat("loop.lock", dir_fd=directory_fd, follow_symlinks=False)
        _validate_file(named)
        if (before.st_dev, before.st_ino) != (named.st_dev, named.st_ino):
            raise RalphLockError("Ralph loop lock changed while opening")
        raw = os.read(descriptor, 16385)
        if len(raw) > 16384:
            raise RalphLockError("Ralph loop lock exceeds the validation limit")
        record = parse_record(raw)
        owner_is_stale(record)
        if dry_run:
            return record

        # Retain the original descriptor and revalidate inode, bytes, and owner
        # identity immediately before atomically quarantining the directory entry.
        after = os.fstat(descriptor)
        current = os.stat("loop.lock", dir_fd=directory_fd, follow_symlinks=False)
        _validate_file(after)
        _validate_file(current)
        if (
            (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
            != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
            or (after.st_dev, after.st_ino) != (current.st_dev, current.st_ino)
        ):
            raise RalphLockError("Ralph loop lock was replaced during recovery")
        os.lseek(descriptor, 0, os.SEEK_SET)
        repeated = os.read(descriptor, 16385)
        if repeated != raw or parse_record(repeated) != record:
            raise RalphLockError("Ralph loop lock record changed during recovery")
        owner_is_stale(record)
        final_directory = ralph_directory.lstat()
        if (final_directory.st_dev, final_directory.st_ino) != (
            directory_info.st_dev, directory_info.st_ino
        ):
            raise RalphLockError(".ralph directory changed during recovery")
        if before_unlink is not None:
            # The race seam is deliberately after the final byte/owner check;
            # quarantine validation below remains the removal authority.
            before_unlink()
        quarantine = f".loop.lock.quarantine-{secrets.token_hex(16)}"
        os.rename("loop.lock", quarantine, src_dir_fd=directory_fd, dst_dir_fd=directory_fd)
        os.fsync(directory_fd)
        quarantined = os.stat(quarantine, dir_fd=directory_fd, follow_symlinks=False)
        opened = os.fstat(descriptor)
        identity = (before.st_dev, before.st_ino)
        if (
            (quarantined.st_dev, quarantined.st_ino) != identity
            or (opened.st_dev, opened.st_ino) != identity
        ):
            raise RalphLockError("Ralph loop lock was substituted at quarantine")
        _validate_file(quarantined)
        _validate_file(opened)
        os.unlink(quarantine, dir_fd=directory_fd)
        os.fsync(directory_fd)
        return record
    except FileNotFoundError as exc:
        raise RalphLockError("Ralph loop lock disappeared during recovery") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
        os.close(directory_fd)
