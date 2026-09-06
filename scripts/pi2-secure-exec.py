#!/usr/bin/env python3
"""Verify an inherited sealed prompt memfd, then exec the model backend."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import os
import re
import stat
from typing import NoReturn

MAX_PROMPT_BYTES = 4 * 1024 * 1024
SHA256 = re.compile(r"^[0-9a-f]{64}$")
_REQUIRED_SEALS = (
    getattr(fcntl, "F_SEAL_SEAL", 0)
    | getattr(fcntl, "F_SEAL_SHRINK", 0)
    | getattr(fcntl, "F_SEAL_GROW", 0)
    | getattr(fcntl, "F_SEAL_WRITE", 0)
)


def die(message: str) -> NoReturn:
    raise SystemExit(f"pi2-secure-exec: {message}")


def verify_prompt_memfd(descriptor: int, expected_sha256: str) -> None:
    """Verify the inherited anonymous prompt without opening any pathname."""
    if descriptor < 0:
        die("prompt descriptor is invalid")
    if not SHA256.fullmatch(expected_sha256):
        die("prompt SHA-256 binding is missing or malformed")
    try:
        info = os.fstat(descriptor)
    except OSError as exc:
        die(f"cannot inspect inherited prompt descriptor: {exc}")
    if not stat.S_ISREG(info.st_mode):
        die("inherited prompt descriptor is not regular")
    if info.st_size > MAX_PROMPT_BYTES:
        die(f"prompt exceeds {MAX_PROMPT_BYTES} bytes")
    if _REQUIRED_SEALS == 0:
        die("kernel prompt sealing constants are unavailable")
    try:
        actual_seals = fcntl.fcntl(descriptor, fcntl.F_GET_SEALS)
    except OSError as exc:
        die(f"cannot verify prompt memfd seals: {exc}")
    if actual_seals & _REQUIRED_SEALS != _REQUIRED_SEALS:
        die("inherited prompt descriptor lacks the mandatory immutable seals")

    digest = hashlib.sha256()
    offset = 0
    while offset < info.st_size:
        try:
            chunk = os.pread(descriptor, min(65536, info.st_size - offset), offset)
        except OSError as exc:
            die(f"cannot read inherited prompt descriptor: {exc}")
        if not chunk:
            die("inherited prompt descriptor was truncated")
        digest.update(chunk)
        offset += len(chunk)
    after = os.fstat(descriptor)
    if (after.st_dev, after.st_ino, after.st_size) != (
        info.st_dev, info.st_ino, info.st_size
    ):
        die("inherited prompt descriptor changed while being verified")
    if digest.hexdigest() != expected_sha256:
        die("inherited prompt does not match the launch-authority digest")
    os.lseek(descriptor, 0, os.SEEK_SET)


def main() -> None:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--prompt-fd", type=int, required=True)
    parser.add_argument("--prompt-sha256", required=True)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = args.command
    if command and command[0] == "--":
        command = command[1:]
    if not command:
        die("missing backend command")
    verify_prompt_memfd(args.prompt_fd, args.prompt_sha256)
    if args.prompt_fd != 0:
        os.dup2(args.prompt_fd, 0)
        os.close(args.prompt_fd)
    os.execvpe(command[0], command, os.environ)


if __name__ == "__main__":
    main()
