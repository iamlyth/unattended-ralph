#!/usr/bin/env python3
"""Securely snapshot Ralph's host-/tmp prompt, then exec Pi2."""

from __future__ import annotations

import argparse
import fcntl
import os
from pathlib import Path
import stat

MAX_PROMPT_BYTES = 4 * 1024 * 1024


def die(message: str) -> "NoReturn":
    raise SystemExit(f"pi2-secure-exec: {message}")


def prompt_snapshot(path_text: str) -> int:
    path = Path(path_text)
    flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0)
    try:
        source_fd = os.open(path, flags)
    except OSError as exc:
        die(f"cannot open prompt file safely: {exc}")
    try:
        before = os.fstat(source_fd)
        resolved = Path(f"/proc/self/fd/{source_fd}").resolve(strict=True)
        try:
            resolved.relative_to(Path("/tmp").resolve(strict=True))
        except ValueError:
            die("prompt file must resolve beneath /tmp")
        if not stat.S_ISREG(before.st_mode):
            die("prompt file is not regular")
        if before.st_uid != os.getuid() or before.st_nlink != 1:
            die("prompt file has unsafe ownership or link count")
        if before.st_mode & 0o022:
            die("prompt file is group- or other-writable")
        if before.st_size > MAX_PROMPT_BYTES:
            die(f"prompt file exceeds {MAX_PROMPT_BYTES} bytes")

        remaining = before.st_size + 1
        chunks: list[bytes] = []
        while remaining:
            chunk = os.read(source_fd, min(65536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        data = b"".join(chunks)
        after = os.fstat(source_fd)
        if len(data) != before.st_size or len(data) > MAX_PROMPT_BYTES:
            die("prompt file changed size while being read")
        if (
            (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
            != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
        ):
            die("prompt file changed while being read")
    finally:
        os.close(source_fd)

    memfd_flags = getattr(os, "MFD_CLOEXEC", 0) | getattr(os, "MFD_ALLOW_SEALING", 0)
    snapshot_fd = os.memfd_create("ralph-prompt", memfd_flags)
    view = memoryview(data)
    while view:
        written = os.write(snapshot_fd, view)
        view = view[written:]
    os.lseek(snapshot_fd, 0, os.SEEK_SET)
    seals = (
        getattr(fcntl, "F_SEAL_SEAL", 0)
        | getattr(fcntl, "F_SEAL_SHRINK", 0)
        | getattr(fcntl, "F_SEAL_GROW", 0)
        | getattr(fcntl, "F_SEAL_WRITE", 0)
    )
    if seals:
        fcntl.fcntl(snapshot_fd, fcntl.F_ADD_SEALS, seals)
    return snapshot_fd


def main() -> None:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--prompt-file")
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = args.command
    if command and command[0] == "--":
        command = command[1:]
    if not command:
        die("missing backend command")
    if args.prompt_file:
        snapshot_fd = prompt_snapshot(args.prompt_file)
        os.dup2(snapshot_fd, 0)
        if snapshot_fd != 0:
            os.close(snapshot_fd)
    os.execvpe(command[0], command, os.environ)


if __name__ == "__main__":
    main()
