#!/usr/bin/env python3
"""Atomically seed a fresh Ralph planning cycle without carrying old task history."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import tempfile
import tomllib

ROOT = Path(__file__).resolve().parents[2]


def git(*args: str) -> str:
    return subprocess.check_output(["git", *args], cwd=ROOT, text=True).strip()


def atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def config() -> dict:
    with (ROOT / ".factory/config.toml").open("rb") as stream:
        return tomllib.load(stream)


def spec_metadata(base: str) -> tuple[str, str, str]:
    spec_path = config()["project"]["spec"]
    if not (ROOT / spec_path).is_file():
        raise SystemExit(f"initialize-plan-cycle: missing specification '{spec_path}'")
    spec_commit = git("log", "-1", "--format=%H", "--", spec_path)
    spec_blob = git("rev-parse", f"HEAD:{spec_path}")
    git("cat-file", "-e", f"{base}^{{commit}}")
    return spec_path, spec_commit, spec_blob


def bug_record(bug_id: str) -> tuple[dict, str]:
    output = subprocess.check_output(
        [str(ROOT / ".factory/tools/bug-ledger.py"), "show", bug_id],
        cwd=ROOT,
        text=True,
    )
    record_text, fingerprint = output.rsplit("\nfingerprint:", 1)
    return json.loads(record_text), fingerprint.strip()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("specification", "maintenance"))
    parser.add_argument("--base", required=True)
    parser.add_argument("--bug-id")
    args = parser.parse_args()

    base = args.base.strip()
    spec_path, spec_commit, spec_blob = spec_metadata(base)

    if args.mode == "specification":
        if args.bug_id:
            parser.error("--bug-id is valid only in maintenance mode")
        plan_path = ROOT / config()["project"].get("plan", ".factory/artifacts/implementation-plan.md")
        plan = f"""---
spec_path: {spec_path}
spec_commit: {spec_commit}
spec_blob: {spec_blob}
base_commit: {base}
status: active
---

# Implementation Plan

Fresh planning cycle initialized. Replace this notice with a concise plan for
only the implementation gaps in the current committed specification. Do not
copy completed tasks from an earlier plan; Git history is their archive.
"""
        scratch = f"""# Planning Scratchpad

Fresh specification-planning cycle initialized at `{base}`.
The previous plan remains available only through Git history.
"""
    else:
        if not args.bug_id:
            parser.error("maintenance mode requires --bug-id")
        record, fingerprint = bug_record(args.bug_id)
        plan_path = ROOT / config()["issues"].get("maintenance_plan", ".factory/artifacts/maintenance-plan.md")
        plan = f"""---
bug_id: {args.bug_id}
bug_fingerprint: {fingerprint}
spec_path: {spec_path}
spec_commit: {spec_commit}
spec_blob: {spec_blob}
base_commit: {base}
status: active
---

# Maintenance Plan: {args.bug_id} — {record['title']}

Fresh maintenance-planning cycle initialized. Replace this notice with a
bounded plan for this bug only. Do not copy tasks or evidence from an earlier
maintenance cycle; Git history and the closed ledger are their archive.
"""
        scratch = f"""# Maintenance {args.bug_id} — Scratchpad

Fresh maintenance-planning cycle initialized at `{base}`.
The previous maintenance plan remains available only through Git history.
"""

    atomic_write(plan_path, plan)
    atomic_write(ROOT / ".ralph/agent/scratchpad.md", scratch)
    print(f"initialize-plan-cycle: seeded {plan_path.name} at {base[:12]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
