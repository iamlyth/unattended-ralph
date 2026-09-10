"""Git utilities for the Ralph factory.

The orchestrator is the sole Git writer. Roles never run git commit.
Per spec §11.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path


def _git(root: str | Path, *args: str) -> subprocess.CompletedProcess:
    """Run a git command with sanitized environment."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env["GIT_TERMINAL_PROMPT"] = "0"
    return subprocess.run(
        ["git", *args],
        cwd=str(root),
        capture_output=True,
        text=True,
        env=env,
    )


def current_branch(root: str | Path) -> str:
    """Get current branch name."""
    r = _git(root, "rev-parse", "--abbrev-ref", "HEAD")
    if r.returncode != 0:
        raise RuntimeError(f"git rev-parse failed: {r.stderr}")
    return r.stdout.strip()


def current_commit(root: str | Path) -> str:
    """Get current HEAD sha1."""
    r = _git(root, "rev-parse", "HEAD")
    if r.returncode != 0:
        raise RuntimeError(f"git rev-parse HEAD failed: {r.stderr}")
    return r.stdout.strip()


def is_clean(root: str | Path) -> bool:
    """Check if working tree is clean."""
    r = _git(root, "status", "--porcelain")
    if r.returncode != 0:
        raise RuntimeError(f"git status failed: {r.stderr}")
    return len(r.stdout.strip()) == 0


def commit_all(root: str | Path, message: str) -> str:
    """Stage all changes and commit. Returns the new commit sha."""
    _git(root, "add", "-A")
    r = _git(root, "commit", "-m", message)
    # returncode 0 = committed, returncode 1 = nothing to commit
    if r.returncode not in (0, 1):
        raise RuntimeError(f"git commit failed: {r.stderr}")
    return current_commit(root)


def switch_branch(root: str | Path, branch_name: str) -> None:
    """Switch to branch, creating it if it doesn't exist."""
    # Try to switch; if it fails because branch doesn't exist, create it
    r = _git(root, "checkout", branch_name)
    if r.returncode != 0:
        r = _git(root, "checkout", "-b", branch_name)
        if r.returncode != 0:
            raise RuntimeError(f"git checkout failed: {r.stderr}")


def file_at_commit(root: str | Path, commit: str, path: str) -> str:
    """Get file content at a specific commit."""
    r = _git(root, "show", f"{commit}:{path}")
    if r.returncode != 0:
        raise RuntimeError(f"git show {commit}:{path} failed: {r.stderr}")
    return r.stdout