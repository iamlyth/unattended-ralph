#!/usr/bin/env python3
"""Read and atomically write small lifecycle markers without following symlinks."""

from __future__ import annotations

import argparse
from pathlib import Path

from factory_state_io import StateIOError, atomic_write_text, read_text, remove

ROOT = Path(__file__).resolve().parents[2]


def main() -> None:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    read = sub.add_parser("read")
    read.add_argument("name")
    read.add_argument("--missing-ok", action="store_true")
    write = sub.add_parser("write")
    write.add_argument("name")
    write.add_argument("value")
    delete = sub.add_parser("remove")
    delete.add_argument("name")
    delete.add_argument("--require-existing", action="store_true")
    args = parser.parse_args()
    try:
        if args.command == "read":
            value = read_text(ROOT, args.name, maximum=16384, missing_ok=args.missing_ok)
            if value is not None:
                print(value.rstrip("\n"))
        elif args.command == "write":
            if "\x00" in args.value:
                raise StateIOError("lifecycle marker contains NUL")
            atomic_write_text(ROOT, args.name, args.value.rstrip("\n") + "\n")
        else:
            remove(ROOT, args.name, missing_ok=not args.require_existing)
    except (OSError, StateIOError) as exc:
        raise SystemExit(f"factory-state-file: {exc}") from exc


if __name__ == "__main__":
    main()
