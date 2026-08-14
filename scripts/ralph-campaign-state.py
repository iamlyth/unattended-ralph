#!/usr/bin/env python3
"""Atomic, Git-bound local state for the multi-round Ralph campaign."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parent.parent
# Recorded Git bindings must not be reinterpreted through local replacement refs.
os.environ["GIT_NO_REPLACE_OBJECTS"] = "1"
DEFAULT_STATE = ROOT / ".factory-state/ralph-campaign.json"
SHA = re.compile(r"^[0-9a-f]{40}$")
DIGEST = re.compile(r"^[0-9a-f]{64}$")
PHASES = {"planning", "implementation", "verification", "audit", "complete", "blocked-findings"}
ROUND_KEYS = {
    "number", "base_commit", "planning_started", "plan_commit",
    "implementation_started", "implementation_commit", "verification_commit",
    "runner_evidence_sha256", "audit_started", "audit_commit", "audit_result",
}


def fail(message: str) -> None:
    raise SystemExit(f"ralph-campaign-state: {message}")


def state_path() -> Path:
    runtime = ROOT / ".factory-state"
    if runtime.is_symlink() or not runtime.is_dir():
        fail(".factory-state must be a real directory")
    configured = Path(os.environ.get("FACTORY_CAMPAIGN_STATE", DEFAULT_STATE))
    try:
        parent = configured.parent.resolve(strict=True)
        root = (ROOT / ".factory-state").resolve(strict=True)
    except FileNotFoundError:
        fail("campaign state directory is missing")
    if parent != root or configured.name != "ralph-campaign.json":
        fail("state path must be .factory-state/ralph-campaign.json")
    return configured


def git(*args: str, check: bool = True) -> str:
    result = subprocess.run(["git", *args], cwd=ROOT, text=True, capture_output=True)
    if check and result.returncode:
        fail(f"Git binding failed for {' '.join(args)}")
    return result.stdout.strip()


def empty_round(number: int) -> dict:
    return {
        "number": number, "base_commit": None, "planning_started": False,
        "plan_commit": None, "implementation_started": False,
        "implementation_commit": None, "verification_commit": None,
        "runner_evidence_sha256": None,
        "audit_started": False, "audit_commit": None, "audit_result": None,
    }


def complete_record(record: dict) -> bool:
    return (
        all(record[key] is not None for key in (
            "base_commit", "plan_commit", "implementation_commit",
            "verification_commit", "runner_evidence_sha256", "audit_commit", "audit_result"))
        and record["planning_started"] and record["implementation_started"]
        and record["audit_started"]
    )


def validate(data: object) -> dict:
    if git("replace", "-l"):
        fail("Git replacement objects are forbidden during a campaign")
    required = {
        "schema", "status", "rounds_requested", "round", "phase", "tui",
        "verification_command_sha256", "rounds",
    }
    if not isinstance(data, dict) or set(data) != required:
        fail("state has unexpected fields")
    if data["schema"] != "ralph-campaign/v2" or data["status"] not in {"active", "complete", "blocked"}:
        fail("state schema or status is invalid")
    if not isinstance(data["rounds_requested"], int) or data["rounds_requested"] < 1:
        fail("rounds_requested is invalid")
    if not isinstance(data["round"], int) or not 1 <= data["round"] <= data["rounds_requested"]:
        fail("current round is invalid")
    if data["phase"] not in PHASES or not isinstance(data["tui"], bool):
        fail("phase or TUI state is invalid")
    if not isinstance(data["verification_command_sha256"], str) or not DIGEST.fullmatch(data["verification_command_sha256"]):
        fail("verification command digest is invalid")
    records = data["rounds"]
    if not isinstance(records, list) or len(records) != data["round"]:
        fail("round records are inconsistent")
    previous_audit = None
    for number, record in enumerate(records, 1):
        if not isinstance(record, dict) or set(record) != ROUND_KEYS or record["number"] != number:
            fail(f"round {number} record is invalid")
        for key in ("base_commit", "plan_commit", "implementation_commit", "verification_commit", "audit_commit"):
            value = record[key]
            if value is not None:
                if not isinstance(value, str) or not SHA.fullmatch(value):
                    fail(f"round {number} {key} is invalid")
                git("cat-file", "-e", f"{value}^{{commit}}")
        evidence_digest = record["runner_evidence_sha256"]
        if evidence_digest is not None and (not isinstance(evidence_digest, str) or not DIGEST.fullmatch(evidence_digest)):
            fail(f"round {number} runner evidence digest is invalid")
        for key in ("planning_started", "implementation_started", "audit_started"):
            if not isinstance(record[key], bool):
                fail(f"round {number} {key} is invalid")
        if record["audit_result"] not in {None, "pass", "findings"}:
            fail(f"round {number} audit result is invalid")
        if previous_audit is not None and record["base_commit"] != previous_audit:
            fail(f"round {number} base is not the preceding audit commit")
        chain = [record[key] for key in ("base_commit", "plan_commit", "implementation_commit", "verification_commit", "audit_commit") if record[key]]
        for older, newer in zip(chain, chain[1:]):
            if git("rev-list", "--min-parents=2", f"{older}..{newer}"):
                fail(f"round {number} commit chain contains a merge")
            if subprocess.run(
                ["git", "merge-base", "--is-ancestor", older, newer], cwd=ROOT,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            ).returncode:
                fail(f"round {number} commit chain was rewritten")
        if record["verification_commit"] and record["verification_commit"] != record["implementation_commit"]:
            fail(f"round {number} verification must bind the implementation commit")
        if number < len(records) and not complete_record(record):
            fail(f"round {number} is incomplete before a later round")
        previous_audit = record["audit_commit"]

    current = records[-1]
    phase = data["phase"]
    if phase in {"implementation", "verification", "audit", "complete", "blocked-findings"}:
        if not current["planning_started"] or not current["base_commit"] or not current["plan_commit"]:
            fail(f"phase {phase} lacks completed planning state")
    if phase in {"verification", "audit", "complete", "blocked-findings"}:
        if not current["implementation_started"] or not current["implementation_commit"]:
            fail(f"phase {phase} lacks completed implementation state")
    if phase in {"audit", "complete", "blocked-findings"} and (
        not current["verification_commit"] or not current["runner_evidence_sha256"]
    ):
        fail(f"phase {phase} lacks verification or runner-evidence binding")
    if phase in {"complete", "blocked-findings"} and not complete_record(current):
        fail(f"phase {phase} lacks a completed audit")
    if data["status"] == "active" and phase not in {"planning", "implementation", "verification", "audit"}:
        fail("active state has a terminal phase")
    if data["status"] == "complete" and phase != "complete":
        fail("complete state has the wrong phase")
    if data["status"] == "blocked" and phase != "blocked-findings":
        fail("blocked state has the wrong phase")
    if data["status"] == "active":
        anchor = {
            "planning": current["base_commit"],
            "implementation": current["plan_commit"],
            "verification": current["implementation_commit"],
            "audit": current["verification_commit"],
        }[phase]
        if anchor and subprocess.run(
            ["git", "merge-base", "--is-ancestor", anchor, "HEAD"], cwd=ROOT,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        ).returncode:
            fail(f"active {phase} history no longer contains its recorded anchor")
    elif git("rev-parse", "HEAD") != current["audit_commit"]:
        fail("terminal campaign HEAD does not match its audit commit")
    return data


def load() -> dict:
    path = state_path()
    if path.is_symlink() or not path.is_file():
        fail(f"missing or unsafe state file: {path}")
    try:
        return validate(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        fail(f"invalid state: {exc}")


def atomic_write(data: dict) -> None:
    validate(data)
    path = state_path()
    if path.is_symlink() or (path.exists() and not path.is_file()):
        fail(f"unsafe state path: {path}")
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(data, stream, sort_keys=True, indent=2)
            stream.write("\n"); stream.flush(); os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        try: os.unlink(temporary)
        except FileNotFoundError: pass


def get_path(data: object, expression: str) -> object:
    current = data
    for piece in expression.split("."):
        if isinstance(current, list): current = current[int(piece)]
        elif isinstance(current, dict) and piece in current: current = current[piece]
        else: fail(f"unknown state path: {expression}")
    return current


def parse_value(text: str) -> object:
    try: return json.loads(text)
    except json.JSONDecodeError as exc: fail(f"state value must be JSON: {exc}")


def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    start = sub.add_parser("start")
    start.add_argument("--rounds", type=int, required=True)
    start.add_argument("--tui", choices=("true", "false"), required=True)
    start.add_argument("--verification-digest", required=True)
    start.add_argument("--base", required=True)
    start.add_argument("--replace-terminal", action="store_true")
    sub.add_parser("show")
    get = sub.add_parser("get"); get.add_argument("path")
    update = sub.add_parser("update")
    update.add_argument("--expect-phase", required=True); update.add_argument("--phase")
    update.add_argument("--round-field", action="append", default=[])
    advance = sub.add_parser("advance"); advance.add_argument("--expect-phase", default="audit")
    finish = sub.add_parser("finish"); finish.add_argument("--result", choices=("pass", "findings"), required=True)
    args = parser.parse_args()

    if args.command == "start":
        if args.rounds < 1 or not DIGEST.fullmatch(args.verification_digest) or not SHA.fullmatch(args.base):
            fail("rounds, base, or verification digest is invalid")
        git("cat-file", "-e", f"{args.base}^{{commit}}")
        path = state_path()
        if path.exists():
            existing = load()
            if existing["status"] == "active" or not args.replace_terminal:
                fail("saved campaign exists; resume it or explicitly replace a terminal campaign")
        record = empty_round(1)
        record["base_commit"] = args.base
        atomic_write({
            "schema": "ralph-campaign/v2", "status": "active",
            "rounds_requested": args.rounds, "round": 1, "phase": "planning",
            "tui": args.tui == "true", "verification_command_sha256": args.verification_digest,
            "rounds": [record],
        })
        return 0

    data = load()
    if args.command == "show": print(json.dumps(data, sort_keys=True, indent=2))
    elif args.command == "get":
        value = get_path(data, args.path)
        if isinstance(value, bool): print("true" if value else "false")
        elif value is not None: print(value if isinstance(value, str) else json.dumps(value, sort_keys=True))
    elif args.command == "update":
        if data["status"] != "active" or data["phase"] != args.expect_phase:
            fail(f"expected active phase {args.expect_phase}, found {data['status']}:{data['phase']}")
        allowed_fields = {
            "planning": {"base_commit", "planning_started", "plan_commit"},
            "implementation": {"implementation_started", "implementation_commit"},
            "verification": {"verification_commit", "runner_evidence_sha256"},
            "audit": {"audit_started", "audit_commit", "audit_result"},
        }[args.expect_phase]
        record = data["rounds"][-1]
        for assignment in args.round_field:
            if "=" not in assignment: fail("--round-field requires key=JSON")
            key, raw = assignment.split("=", 1)
            if key not in allowed_fields: fail(f"field {key} is not writable in {args.expect_phase}")
            value = parse_value(raw)
            current = record[key]
            if current not in (None, False) and current != value:
                fail(f"field {key} is write-once")
            if current is False and value is not True:
                fail(f"phase marker {key} may only transition false to true")
            if current is None and value is None:
                fail(f"field {key} cannot be recorded as null")
            record[key] = value
        if args.phase:
            expected_next = {"planning": "implementation", "implementation": "verification", "verification": "audit"}.get(args.expect_phase)
            if args.phase != expected_next:
                fail(f"invalid transition {args.expect_phase} -> {args.phase}")
            data["phase"] = args.phase
        atomic_write(data)
    elif args.command == "advance":
        if data["status"] != "active" or data["phase"] != args.expect_phase or not complete_record(data["rounds"][-1]):
            fail("cannot advance an incomplete audit phase")
        if data["round"] >= data["rounds_requested"]: fail("cannot advance beyond requested rounds")
        if git("rev-parse", "HEAD") != data["rounds"][-1]["audit_commit"]:
            fail("cannot advance away from the recorded audit commit")
        data["round"] += 1; data["phase"] = "planning"
        record = empty_round(data["round"]); record["base_commit"] = data["rounds"][-1]["audit_commit"]
        data["rounds"].append(record); atomic_write(data)
    elif args.command == "finish":
        record = data["rounds"][-1]
        if data["status"] != "active" or data["phase"] != "audit" or not complete_record(record):
            fail("campaign can finish only from a completed audit phase")
        if record["audit_result"] != args.result or git("rev-parse", "HEAD") != record["audit_commit"]:
            fail("finish result or HEAD does not match the recorded audit")
        data["status"] = "complete" if args.result == "pass" else "blocked"
        data["phase"] = "complete" if args.result == "pass" else "blocked-findings"
        atomic_write(data)
    return 0


if __name__ == "__main__": raise SystemExit(main())
