#!/usr/bin/env python3
"""Seed a campaign audit report bound to the just-verified implementation.

Also mints the protected audit coordinator state (`.factory-state/audit-coordinator.json`)
with a fresh random nonce bound to the round and audit base. `scripts/machine-receipt.py`
requires this coordinator binding (round/base/nonce) to mint receipts; a bare model
call cannot mint receipts outside the audit coordinator's bounded invocation.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import tempfile
import time

ROOT = Path(__file__).resolve().parent.parent
SHA1 = re.compile(r"^[0-9a-f]{40}$")
COORDINATOR_FILE = ROOT / ".factory-state/audit-coordinator.json"


def git(*args: str) -> str:
    return subprocess.check_output(["git", *args], cwd=ROOT, text=True).strip()


def atomic_write(path: Path, text: str, mode: int | None = None) -> None:
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        if mode is not None:
            os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def coordinator_state(round_number: int, base: str) -> dict:
    state_dir = ROOT / ".factory-state"
    if state_dir.is_symlink() or not state_dir.is_dir():
        raise SystemExit("initialize-campaign-audit: .factory-state must be a real directory")
    info = state_dir.lstat()
    if info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise SystemExit("initialize-campaign-audit: unsafe .factory-state directory")
    state_dir.chmod(0o700)
    if COORDINATOR_FILE.exists() or COORDINATOR_FILE.is_symlink():
        raise SystemExit(
            "initialize-campaign-audit: audit coordinator state already exists; "
            "refusing to mint a new nonce for an active audit"
        )
    return json.dumps({
        "schema": "ralph-audit-coordinator/v1",
        "round": round_number,
        "base_commit": base,
        "nonce": hashlib.sha256(os.urandom(32)).hexdigest(),
        "created_at": int(time.time()),
    }, sort_keys=True, indent=2) + "\n"


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
    if not SHA1.fullmatch(args.base):
        raise SystemExit("initialize-campaign-audit: base must be a strict 40-hex commit")
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
    atomic_write(COORDINATOR_FILE, coordinator_state(args.round, args.base), mode=0o600)
    print(f"initialize-campaign-audit: seeded round {args.round} at {args.base[:12]}")
    print("initialize-campaign-audit: minted audit coordinator binding (nonce protected in .factory-state)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
