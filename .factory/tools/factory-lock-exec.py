#!/usr/bin/env python3
"""Acquire/validate the git-common factory lock at a lifecycle boundary."""

from __future__ import annotations

import os
from pathlib import Path
import sys

from factory_lock import (
    FactoryLockError,
    acquire,
    inherited_descriptor,
    validate_open_lock,
)


def fail(message: str) -> None:
    raise SystemExit(f"factory-lock: {message}")


def execute(command: list[str]) -> None:
    if not command:
        fail("missing command")
    try:
        os.execvpe(command[0], command, os.environ)
    except OSError as exc:
        fail(f"cannot execute lifecycle command: {exc}")


def main() -> None:
    if len(sys.argv) < 3:
        fail("usage: factory-lock-exec.py ROOT [--check|-- COMMAND]")
    root = Path(sys.argv[1])
    arguments = sys.argv[2:]
    try:
        if arguments == ["--check"]:
            descriptor = inherited_descriptor(root)
            if descriptor is None:
                fail("lifecycle did not bootstrap the factory lock")
            validate_open_lock(descriptor, root)
            return
        if not arguments or arguments[0] != "--":
            fail("expected -- before lifecycle command")
        if inherited_descriptor(root) is None:
            acquire(root, inheritable=True)
        execute(arguments[1:])
    except FactoryLockError as exc:
        fail(str(exc))


if __name__ == "__main__":
    main()
