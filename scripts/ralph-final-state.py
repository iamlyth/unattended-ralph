#!/usr/bin/env python3
"""Durable, cycle-bound final checkpoint and clean-HEAD attestation state."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import re
import subprocess

from factory_state_io import StateIOError, atomic_write_json, read_json

ROOT = Path(__file__).resolve().parent.parent
MODES = {"implementation", "planning", "campaign-audit", "maintenance-planning", "maintenance"}
SHA = re.compile(r"^[0-9a-f]{40}$")
CYCLE = re.compile(r"^[0-9a-f]{64}$")


def fail(message: str) -> None:
    raise SystemExit(f"ralph-final-state: {message}")


def git(*args: str, check: bool = True) -> str:
    result = subprocess.run(["git", *args], cwd=ROOT, text=True, capture_output=True)
    if check and result.returncode:
        fail(f"Git check failed for {' '.join(args)}")
    return result.stdout.strip()


def marker_name(mode: str) -> str:
    return f"final-handoff-{mode}.json"


def cycle_id() -> str:
    value = os.environ.get("FACTORY_RALPH_CYCLE_ID", "")
    if not CYCLE.fullmatch(value):
        fail("missing or invalid durable cycle ID")
    return value


def validate(data: object, mode: str) -> dict:
    expected = {"schema", "mode", "cycle_id", "checkpoint_head", "attested_head"}
    if (
        not isinstance(data, dict)
        or set(data) != expected
        or data.get("schema") != "ralph-final-state/v1"
        or data.get("mode") != mode
        or not isinstance(data.get("cycle_id"), str)
        or not CYCLE.fullmatch(data["cycle_id"])
        or not isinstance(data.get("checkpoint_head"), str)
        or not SHA.fullmatch(data["checkpoint_head"])
        or data.get("attested_head") is not None
        and (not isinstance(data["attested_head"], str) or not SHA.fullmatch(data["attested_head"]))
    ):
        fail("final-state marker has an invalid schema")
    git("cat-file", "-e", f"{data['checkpoint_head']}^{{commit}}")
    if data["attested_head"] is not None:
        git("cat-file", "-e", f"{data['attested_head']}^{{commit}}")
    return data


def load(mode: str, *, missing_ok: bool = False) -> dict | None:
    try:
        data = read_json(ROOT, marker_name(mode), maximum=16384, missing_ok=missing_ok)
    except (OSError, StateIOError) as exc:
        fail(f"cannot safely read final-state marker: {exc}")
    if data is None:
        return None
    return validate(data, mode)


def write(mode: str, data: dict) -> None:
    validate(data, mode)
    try:
        atomic_write_json(ROOT, marker_name(mode), data)
    except (OSError, StateIOError) as exc:
        fail(f"cannot safely write final-state marker: {exc}")


def require_head(head: str) -> None:
    if not SHA.fullmatch(head) or git("rev-parse", "HEAD") != head:
        fail("requested final HEAD is not current HEAD")
    git("cat-file", "-e", f"{head}^{{commit}}")


def is_ancestor(older: str, newer: str) -> bool:
    return subprocess.run(
        ["git", "merge-base", "--is-ancestor", older, newer],
        cwd=ROOT,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    ).returncode == 0


def allow_checkpoint_commit(mode: str, head: str) -> None:
    require_head(head)
    cycle = cycle_id()
    prior_cycle_commits = git("log", "--format=%H", "--fixed-strings", "--grep", f"Cycle: {cycle}")
    if prior_cycle_commits:
        fail("cycle already used its final checkpoint commit")
    previous = load(mode, missing_ok=True)
    if previous is not None and previous["cycle_id"] == cycle:
        fail("cycle already used its final checkpoint commit")
    if previous is not None:
        if previous["attested_head"] is None:
            fail("previous lifecycle cycle has no successful attestation")
        if not is_ancestor(previous["attested_head"], head):
            fail("previous final attestation is not in current history")
    print("allowed")


def ensure_checkpoint(mode: str, head: str) -> None:
    require_head(head)
    cycle = cycle_id()
    previous = load(mode, missing_ok=True)
    if previous is not None and previous["cycle_id"] == cycle:
        if previous["attested_head"] is not None:
            fail("cycle already has a successful final attestation")
        if not is_ancestor(previous["checkpoint_head"], head):
            fail("cycle final checkpoint history was rewritten")
        print("existing")
        return
    if previous is not None:
        if previous["attested_head"] is None:
            fail("previous lifecycle cycle has no successful attestation")
        if not is_ancestor(previous["attested_head"], head):
            fail("previous final attestation is not in current history")
    write(mode, {
        "schema": "ralph-final-state/v1",
        "mode": mode,
        "cycle_id": cycle,
        "checkpoint_head": head,
        "attested_head": None,
    })
    print("created")


def attest(mode: str, head: str) -> None:
    require_head(head)
    if git("status", "--porcelain=v1", "--untracked-files=normal"):
        fail("attestation requires a clean Git tree")
    data = load(mode)
    if data["cycle_id"] != cycle_id():
        fail("final checkpoint belongs to another lifecycle cycle")
    if data["attested_head"] is not None:
        fail("cycle final attestation may succeed at most once")
    checkpoint = data["checkpoint_head"]
    if not is_ancestor(checkpoint, head):
        fail("final checkpoint history was rewritten")
    if checkpoint != head:
        if git("rev-list", "--min-parents=2", f"{checkpoint}..{head}"):
            fail("post-checkpoint history contains a merge")
        changed = [item for item in git("diff", "--name-only", checkpoint, head).splitlines() if item]
        metadata_only = {
            ".ralph/agent/scratchpad.md",
            ".factory/bugs/open.md",
            ".factory/bugs/closed.md",
        }
        if not changed or set(changed).issubset(metadata_only):
            fail("metadata-only history cannot advance a final attestation")
    data["attested_head"] = head
    write(mode, data)
    print(head)


def verify(mode: str) -> None:
    head = git("rev-parse", "HEAD")
    if git("status", "--porcelain=v1", "--untracked-files=normal"):
        fail("verified attestation no longer has a clean Git tree")
    data = load(mode)
    if data["cycle_id"] != cycle_id() or data["attested_head"] != head:
        fail("current cycle and HEAD do not match the successful attestation")
    print(head)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("allow-checkpoint-commit", "ensure-checkpoint", "attest", "verify"))
    parser.add_argument("mode", choices=sorted(MODES))
    parser.add_argument("head", nargs="?")
    args = parser.parse_args()
    if args.command == "verify":
        if args.head is not None:
            fail("verify does not accept a HEAD argument")
        verify(args.mode)
    else:
        if args.head is None:
            fail(f"{args.command} requires a HEAD")
        if args.command == "allow-checkpoint-commit":
            allow_checkpoint_commit(args.mode, args.head)
        elif args.command == "ensure-checkpoint":
            ensure_checkpoint(args.mode, args.head)
        else:
            attest(args.mode, args.head)


if __name__ == "__main__":
    main()
