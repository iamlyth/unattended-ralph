"""Simple file lock for single-writer enforcement.

Uses fcntl.flock on the repository's .git directory.
Per spec §11 (single writer).
"""
from __future__ import annotations

import fcntl
from pathlib import Path


class Lock:
    """Exclusive lock on the repository's .git directory."""

    def __init__(self, root: str | Path):
        self.root = Path(root)
        self._fd = None
        self._lockfile = None

    def __enter__(self) -> "Lock":
        gitdir = self.root / ".git"
        if not gitdir.exists():
            # Not a git repo or detached .git — use root
            gitdir = self.root
        self._lockfile = gitdir / ".factory.lock"
        self._fd = open(self._lockfile, "w")
        fcntl.flock(self._fd.fileno(), fcntl.LOCK_EX)
        return self

    def __exit__(self, *args) -> None:
        if self._fd is not None:
            fcntl.flock(self._fd.fileno(), fcntl.LOCK_UN)
            self._fd.close()
            self._fd = None