#!/usr/bin/env python3
"""Seed a campaign audit report bound to the just-verified implementation."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import re
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parent.parent


def git(*args: str) -> str:
    return subprocess.check_output(["git", *args], cwd=ROOT, text=True).strip()


def atomic_write(path: Path, text: str) -> None:
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--round", type=int, required=True)
    parser.add_argument("--base", required=True)
    parser.add_argument("--runner-evidence-sha256", required=True)
    args = parser.parse_args()
    if args.round < 1:
        raise SystemExit("initialize-campaign-audit: round must be positive")
    if not re.fullmatch(r"[0-9a-f]{64}", args.runner_evidence_sha256):
        raise SystemExit("initialize-campaign-audit: runner evidence digest is invalid")
    git("cat-file", "-e", f"{args.base}^{{commit}}")
    if git("rev-parse", "HEAD") != args.base:
        raise SystemExit("initialize-campaign-audit: audit base must equal HEAD")
    plan_commit = git("log", "-1", "--format=%H", "--", ".factory/artifacts/implementation-plan.md")
    plan_blob = git("rev-parse", "HEAD:.factory/artifacts/implementation-plan.md")
    environment_blob = git("rev-parse", "HEAD:.factory/environment.toml")
    for directory in (ROOT / ".ralph", ROOT / ".ralph/agent"):
        if directory.is_symlink() or not directory.is_dir():
            raise SystemExit(f"initialize-campaign-audit: unsafe directory {directory}")
    for target in (ROOT / ".factory/artifacts/campaign-audit.md", ROOT / ".ralph/agent/scratchpad.md"):
        if target.is_symlink() or (target.exists() and not target.is_file()):
            raise SystemExit(f"initialize-campaign-audit: unsafe target {target}")
    report = f"""---
schema: ralph-campaign-audit/v1
round: {args.round}
audit_base_commit: {args.base}
plan_commit: {plan_commit}
plan_blob: {plan_blob}
environment_blob: {environment_blob}
runner_evidence_sha256: {args.runner_evidence_sha256}
result: pending
---
# Campaign Round {args.round} Independent Gap Audit

Fresh audit initialized. Distrust the preceding completion claim and replace
this notice with production-path evidence and either a clean pass or concrete
findings for the next fresh planning round.
"""
    scratch = f"""# Campaign Audit Round {args.round} — Scratchpad

Fresh independent audit initialized at `{args.base}`.
"""
    atomic_write(ROOT / ".factory/artifacts/campaign-audit.md", report)
    atomic_write(ROOT / ".ralph/agent/scratchpad.md", scratch)
    print(f"initialize-campaign-audit: seeded round {args.round} at {args.base[:12]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
