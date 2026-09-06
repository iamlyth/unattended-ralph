#!/usr/bin/env python3
"""Dedicated visual-audit resource lease (stdlib only).

The visual-audit pipeline must never hold the campaign factory lock across
untrusted product/model execution. This tool owns a separate lease file with
CLOEXEC and an isolated session boundary: capture is serial (one lease holder
at a time), and concurrent or nested capture attempts are refused instead of
queued, so a shared-session race is impossible.

Usage:
  visual-audit-lease.py acquire --lock PATH [--owner NAME] [--wait]
  visual-audit-lease.py check   --lock PATH
  visual-audit-lease.py release --lock PATH
"""

from __future__ import annotations

import argparse
import fcntl
import os
from pathlib import Path
import sys
import time

MAX_WAIT = 60


def die(message: str) -> "NoReturn":
    raise SystemExit(f"visual-audit-lease: {message}")


def open_lock(path: Path) -> int:
    if path.is_symlink():
        die("lease path must not be a symlink")
    parent = path.parent
    if not parent.is_dir() or parent.is_symlink():
        die("lease parent must be a real directory")
    try:
        fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o600)
    except OSError as exc:
        die(f"cannot open lease: {type(exc).__name__}: {exc}")
    info = os.fstat(fd)
    if info.st_uid != os.getuid() or info.st_mode & 0o022:
        die("lease file has unsafe ownership or permissions")
    return fd


def acquire(path: Path, owner: str, wait: bool) -> int:
    fd = open_lock(path)
    deadline = time.monotonic() + MAX_WAIT if wait else time.monotonic()
    while True:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            break
        except OSError:
            if not wait or time.monotonic() >= deadline:
                os.close(fd)
                die("another capture is holding the visual-audit lease (race refused)")
            time.sleep(1)
    payload = f"{os.getpid()}|{owner}".encode()
    os.ftruncate(fd, 0)
    os.write(fd, payload)
    os.fsync(fd)
    print(f"visual-audit-lease: acquired by {owner} (pid {os.getpid()})")
    # Keep the fd open; the holder must retain this process's fd. The
    # CLOEXEC flag guarantees the lease is never inherited into the
    # untrusted capture driver or the vision model process.
    os.set_inheritable(fd, False)
    globals()["_held_fd"] = fd
    return 0


def check(path: Path) -> int:
    fd = open_lock(path)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        data = os.read(fd, 256).decode("utf-8", errors="replace").strip()
        os.close(fd)
        print(f"visual-audit-lease: held by {data or 'unknown owner'}")
        return 1
    fcntl.flock(fd, fcntl.LOCK_UN)
    os.close(fd)
    print("visual-audit-lease: free")
    return 0


def release(path: Path) -> int:
    fd = open_lock(path)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        die("cannot release a lease owned by another process")
    os.ftruncate(fd, 0)
    fcntl.flock(fd, fcntl.LOCK_UN)
    os.close(fd)
    print("visual-audit-lease: released")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Visual-audit resource lease")
    sub = parser.add_subparsers(dest="command", required=True)
    a = sub.add_parser("acquire")
    a.add_argument("--lock", required=True)
    a.add_argument("--owner", default="capture")
    a.add_argument("--wait", action="store_true")
    a.set_defaults(func=acquire)
    c = sub.add_parser("check")
    c.add_argument("--lock", required=True)
    c.set_defaults(func=check)
    r = sub.add_parser("release")
    r.add_argument("--lock", required=True)
    r.set_defaults(func=release)
    args = parser.parse_args()
    if args.command == "acquire":
        return args.func(Path(args.lock), args.owner, args.wait)
    if args.command == "check":
        return args.func(Path(args.lock))
    if args.command == "release":
        return args.func(Path(args.lock))
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
