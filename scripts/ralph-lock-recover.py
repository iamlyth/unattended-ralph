#!/usr/bin/env python3
"""Validate and optionally remove one stale .ralph/loop.lock."""

from __future__ import annotations

import argparse
from pathlib import Path

from ralph_lock import RalphLockError, remove_stale_lock


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("ralph_directory", type=Path)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    try:
        record = remove_stale_lock(args.ralph_directory, dry_run=args.dry_run)
    except (OSError, RalphLockError) as exc:
        raise SystemExit(f"ralph-lock-recover: {exc}") from exc
    if record is not None:
        print(record["pid"])


if __name__ == "__main__":
    main()
