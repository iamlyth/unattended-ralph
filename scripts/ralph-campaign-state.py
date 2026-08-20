#!/usr/bin/env python3
"""Atomic, Git-bound local state for the multi-round Ralph campaign."""

from __future__ import annotations

import argparse
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess

from factory_lock import FactoryLockError, locked
from factory_state_io import StateIOError, atomic_write_json, read_bytes, read_json, remove

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
    try:
        runtime_info = runtime.lstat()
    except FileNotFoundError:
        fail(".factory-state is missing")
    if (
        stat.S_ISLNK(runtime_info.st_mode)
        or not stat.S_ISDIR(runtime_info.st_mode)
        or runtime_info.st_uid != os.getuid()
        or runtime_info.st_mode & 0o077
    ):
        fail(".factory-state must be a private real directory")
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


def validate(data: object, *, allow_terminal_head_mismatch: bool = False) -> dict:
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
    if type(data["rounds_requested"]) is not int or data["rounds_requested"] < 1:
        fail("rounds_requested is invalid")
    if type(data["round"]) is not int or not 1 <= data["round"] <= data["rounds_requested"]:
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
        if (
            not isinstance(record, dict)
            or set(record) != ROUND_KEYS
            or type(record["number"]) is not int
            or record["number"] != number
        ):
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
    # Reject populated future-phase fields rather than merely checking that the
    # current phase has its minimum prerequisites.
    if phase == "planning" and (
        current["plan_commit"] is not None
        or current["implementation_started"]
        or current["implementation_commit"] is not None
        or current["verification_commit"] is not None
        or current["runner_evidence_sha256"] is not None
        or current["audit_started"]
        or current["audit_commit"] is not None
        or current["audit_result"] is not None
    ):
        fail("planning state contains future-phase fields")
    if phase == "implementation" and (
        current["implementation_commit"] is not None
        or current["verification_commit"] is not None
        or current["runner_evidence_sha256"] is not None
        or current["audit_started"]
        or current["audit_commit"] is not None
        or current["audit_result"] is not None
    ):
        fail("implementation state contains future-phase fields")
    if phase == "verification" and (
        current["verification_commit"] is not None
        or current["runner_evidence_sha256"] is not None
        or current["audit_started"]
        or current["audit_commit"] is not None
        or current["audit_result"] is not None
    ):
        fail("verification state contains future-phase fields")
    if phase == "audit" and (
        (current["audit_commit"] is None) != (current["audit_result"] is None)
        or (current["audit_commit"] is not None and not current["audit_started"])
    ):
        fail("audit state has inconsistent current-phase fields")
    if not current["planning_started"] and current["plan_commit"] is not None:
        fail("planning commit exists without a confirmed launch")
    if not current["implementation_started"] and current["implementation_commit"] is not None:
        fail("implementation commit exists without a confirmed launch")
    if not current["audit_started"] and (current["audit_commit"] is not None or current["audit_result"] is not None):
        fail("audit result exists without a confirmed launch")
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
    elif not allow_terminal_head_mismatch and git("rev-parse", "HEAD") != current["audit_commit"]:
        fail("terminal campaign HEAD does not match its audit commit")
    return data


def read_state_file(path: Path) -> object:
    try:
        data = read_json(ROOT, path.name, maximum=1024 * 1024)
    except (OSError, StateIOError) as exc:
        fail(f"missing or unsafe state file: {exc}")
    assert data is not None
    return data


def load(*, allow_terminal_head_mismatch: bool = False) -> dict:
    return validate(
        read_state_file(state_path()),
        allow_terminal_head_mismatch=allow_terminal_head_mismatch,
    )


def load_status_lenient() -> str:
    """Read only the status field, skipping full validation.

    Used by `start --replace-terminal`: replacing a terminal campaign must
    work even when HEAD has moved past the terminal audit commit (which
    would make strict `load()` fail). We only need to confirm the saved
    campaign is not active before overwriting it.
    """
    path = state_path()
    if path.is_symlink() or not path.is_file():
        fail(f"missing or unsafe state file: {path}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        fail(f"invalid state: {exc}")
    if not isinstance(data, dict) or not isinstance(data.get("status"), str):
        fail("invalid state: missing status")
    return data["status"]


def atomic_write(data: dict) -> None:
    validate(data)
    path = state_path()
    try:
        atomic_write_json(ROOT, path.name, data, indent=2)
    except (OSError, StateIOError) as exc:
        fail(f"unsafe state path: {exc}")


def file_sha256(path: Path) -> str:
    try:
        raw = read_bytes(ROOT, path.name, maximum=1024 * 1024)
        assert raw is not None
        return hashlib.sha256(raw).hexdigest()
    except (OSError, StateIOError) as exc:
        fail(f"cannot digest campaign state: {exc}")


@contextmanager
def recovery_lock():
    """Acquire or verify the stable git-common lock for every state mutation."""
    try:
        with locked(ROOT) as descriptor:
            yield descriptor
    except FactoryLockError as exc:
        fail(str(exc))


def promote_verifier_binding(args: argparse.Namespace) -> None:
    if not DIGEST.fullmatch(args.expected_old) or not DIGEST.fullmatch(args.new):
        fail("old or new verifier binding digest is invalid")
    expected_phase = "audit" if args.mode == "campaign-audit" else args.mode
    with recovery_lock():
        data = load()
        if data["status"] != "active" or data["phase"] != expected_phase:
            fail("verifier binding migration does not match the active campaign phase")
        if data["verification_command_sha256"] == args.new:
            return
        if data["verification_command_sha256"] != args.expected_old:
            fail("saved verifier binding does not match the expected legacy digest")
        marker_name = f"ralph-supervision-migration-{args.mode}.json"
        try:
            marker = read_json(ROOT, marker_name, maximum=16384)
        except (OSError, StateIOError) as exc:
            fail(f"cannot safely read verifier migration marker: {exc}")
        expected_keys = {
            "schema", "mode", "cycle_id", "campaign_state_sha256", "round",
            "legacy_verification_command_sha256", "verification_binding_sha256",
        }
        if (
            not isinstance(marker, dict)
            or set(marker) != expected_keys
            or marker.get("schema") != "ralph-supervision-migration/v1"
            or marker.get("mode") != args.mode
            or marker.get("campaign_state_sha256") != file_sha256(state_path())
            or marker.get("round") != data["round"]
            or marker.get("legacy_verification_command_sha256") != args.expected_old
            or marker.get("verification_binding_sha256") != args.new
            or not isinstance(marker.get("cycle_id"), str)
            or not DIGEST.fullmatch(marker["cycle_id"])
        ):
            fail("verifier binding migration marker does not match the saved campaign")
        data["verification_command_sha256"] = args.new
        atomic_write(data)


def require_rebind_head(requested_new: str) -> None:
    if git("symbolic-ref", "--quiet", "--short", "HEAD", check=False) != "develop":
        fail("rebind requires the develop branch")
    if git("rev-parse", "HEAD") != requested_new:
        fail("requested new implementation commit must equal current HEAD")
    if git("status", "--porcelain=v1", "--untracked-files=normal"):
        fail("rebind requires a clean Git tree")


def rebind_implementation(expected_old: str, requested_new: str) -> None:
    if not SHA.fullmatch(expected_old) or not SHA.fullmatch(requested_new):
        fail("expected-old or new implementation commit is invalid")
    with recovery_lock():
        data = load()
        if data["status"] != "active" or data["phase"] != "verification":
            fail("rebind requires an active verification phase")
        if data["round"] != 1 or len(data["rounds"]) != 1:
            fail("rebind is limited to the first campaign round")
        record = data["rounds"][0]
        if record["implementation_commit"] != expected_old:
            fail("implementation commit does not match --expected-old")
        if (
            record["verification_commit"] is not None
            or record["runner_evidence_sha256"] is not None
            or record["audit_started"]
            or record["audit_commit"] is not None
            or record["audit_result"] is not None
        ):
            fail("verification, evidence, and audit fields must still be unset")
        require_rebind_head(requested_new)
        if expected_old == requested_new:
            fail("new implementation commit must strictly descend from expected-old")
        if subprocess.run(
            ["git", "merge-base", "--is-ancestor", expected_old, requested_new], cwd=ROOT,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        ).returncode:
            fail("new implementation commit is not a descendant of expected-old")
        if git("rev-list", "--min-parents=2", f"{expected_old}..{requested_new}"):
            fail("implementation rebind range contains a merge")

        before_digest = file_sha256(state_path())
        record["implementation_commit"] = requested_new
        # Recheck mutable Git preconditions immediately before the atomic write.
        require_rebind_head(requested_new)
        atomic_write(data)
        persisted = load()
        if persisted != data:
            fail("persisted campaign state does not match the validated rebind")
        print(json.dumps({
            "operation": "rebind-implementation",
            "round": 1,
            "expected_old": expected_old,
            "new": requested_new,
            "state_sha256_before": before_digest,
            "state_sha256_after": file_sha256(state_path()),
        }, sort_keys=True))


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


def start_campaign(args: argparse.Namespace) -> None:
    with recovery_lock():
        if args.rounds < 1 or not DIGEST.fullmatch(args.verification_digest) or not SHA.fullmatch(args.base):
            fail("rounds, base, or verification digest is invalid")
        git("cat-file", "-e", f"{args.base}^{{commit}}")
        path = state_path()
        if path.exists() or path.is_symlink():
            # allow_terminal_head_mismatch keeps strict validation (the local
            # test suite rejects malformed state even under --replace-terminal)
            # while letting a terminal campaign be replaced after HEAD moved
            # past its audit commit; an active campaign is always refused.
            existing = load(allow_terminal_head_mismatch=args.replace_terminal)
            if args.replace_terminal:
                if existing["status"] == "active":
                    fail("saved campaign is active; cannot replace it")
            else:
                fail("saved campaign exists; resume it or explicitly replace a terminal campaign")
        record = empty_round(1)
        record["base_commit"] = args.base
        atomic_write({
            "schema": "ralph-campaign/v2", "status": "active",
            "rounds_requested": args.rounds, "round": 1, "phase": "planning",
            "tui": args.tui == "true", "verification_command_sha256": args.verification_digest,
            "rounds": [record],
        })


def _open_ralph_directory() -> int:
    if not hasattr(os, "O_NOFOLLOW") or not hasattr(os, "O_DIRECTORY"):
        fail("required Linux no-follow primitives are unavailable")
    ralph = ROOT / ".ralph"
    directory_fd = os.open(ralph, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    info = os.fstat(directory_fd)
    named = ralph.lstat()
    if (
        not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.getuid()
        or info.st_mode & 0o022
        or (info.st_dev, info.st_ino) != (named.st_dev, named.st_ino)
    ):
        os.close(directory_fd)
        fail("unsafe .ralph directory")
    return directory_fd


def _read_ralph_marker(name: str) -> str:
    directory_fd = _open_ralph_directory()
    descriptor = None
    try:
        descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory_fd)
        info = os.fstat(descriptor)
        named = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or info.st_nlink != 1
            or info.st_mode & 0o022
            or info.st_size > 4096
            or (info.st_dev, info.st_ino) != (named.st_dev, named.st_ino)
        ):
            fail(f"unsafe Ralph launch marker: {name}")
        return os.read(descriptor, 4097).decode("utf-8").strip()
    except (OSError, UnicodeError) as exc:
        fail(f"cannot validate Ralph launch marker {name}: {exc}")
    finally:
        if descriptor is not None:
            os.close(descriptor)
        os.close(directory_fd)


def launch_handshake_name(phase: str) -> str:
    mode = "campaign-audit" if phase == "audit" else phase
    return f"ralph-launch-handshake-{mode}.json"


def discard_launch_handshake(phase: str) -> None:
    try:
        remove(ROOT, launch_handshake_name(phase))
    except (OSError, StateIOError) as exc:
        fail(f"cannot remove completed launch handshake: {exc}")


def confirm_launch(args: argparse.Namespace) -> None:
    phase = args.expect_phase
    mode = "campaign-audit" if phase == "audit" else phase
    marker_name = launch_handshake_name(phase)
    with recovery_lock():
        data = load()
        if data["status"] != "active" or data["phase"] != phase:
            fail(f"launch confirmation expected active phase {phase}")
        record = data["rounds"][-1]
        started_key = {
            "planning": "planning_started",
            "implementation": "implementation_started",
            "audit": "audit_started",
        }[phase]
        if record[started_key]:
            return
        try:
            handshake = read_json(ROOT, marker_name, maximum=16384, missing_ok=args.missing_ok)
        except (OSError, StateIOError) as exc:
            fail(f"cannot safely read launch handshake: {exc}")
        if handshake is None:
            raise SystemExit(3)
        expected_keys = {
            "schema", "mode", "cycle_id", "attempt_id", "campaign_round",
            "campaign_phase", "campaign_state_sha256", "loop_id", "events",
            "events_dev", "events_ino", "events_offset", "events_size", "events_sha256",
            "events_delta_size", "events_delta_sha256", "start_record_size",
            "start_record_sha256", "start_record_nonce",
        }
        if (
            not isinstance(handshake, dict)
            or set(handshake) != expected_keys
            or handshake.get("schema") != "ralph-launch-handshake/v2"
            or handshake.get("mode") != mode
            or handshake.get("campaign_round") != str(data["round"])
            or handshake.get("campaign_phase") != phase
            or handshake.get("campaign_state_sha256") != file_sha256(state_path())
            or not isinstance(handshake.get("cycle_id"), str)
            or not DIGEST.fullmatch(handshake["cycle_id"])
            or not isinstance(handshake.get("attempt_id"), str)
            or not DIGEST.fullmatch(handshake["attempt_id"])
            or handshake.get("start_record_nonce") != handshake.get("attempt_id")
            or not isinstance(handshake.get("events_sha256"), str)
            or not DIGEST.fullmatch(handshake["events_sha256"])
            or not isinstance(handshake.get("events_delta_sha256"), str)
            or not DIGEST.fullmatch(handshake["events_delta_sha256"])
            or not isinstance(handshake.get("start_record_sha256"), str)
            or not DIGEST.fullmatch(handshake["start_record_sha256"])
            or any(
                type(handshake.get(key)) is not int or handshake[key] < 0
                for key in (
                    "events_dev", "events_ino", "events_offset", "events_size",
                    "events_delta_size", "start_record_size",
                )
            )
            or handshake.get("events_size")
            != handshake.get("events_offset") + handshake.get("events_delta_size")
            or handshake.get("start_record_size", 0) > handshake.get("events_delta_size", -1)
        ):
            fail("launch handshake does not match this campaign phase")
        try:
            supervision = read_json(ROOT, f"ralph-supervision-{mode}.json", maximum=16384)
            loop_mode = read_bytes(ROOT, "loop-mode", maximum=128)
        except (OSError, StateIOError) as exc:
            fail(f"cannot validate launch cycle state: {exc}")
        if (
            not isinstance(supervision, dict)
            or supervision.get("schema") != "ralph-supervision/v2"
            or supervision.get("mode") != mode
            or supervision.get("cycle_id") != handshake["cycle_id"]
            or loop_mode is None
            or loop_mode.decode("utf-8").strip() != mode
            or _read_ralph_marker("current-loop-id") != handshake["loop_id"]
            or _read_ralph_marker("current-events") != handshake["events"]
        ):
            fail("launch handshake cycle or marker state changed")
        event_relative = handshake["events"]
        if not isinstance(event_relative, str) or not re.fullmatch(r"\.ralph/events(?:-[0-9]{8}-[0-9]{6})?\.jsonl", event_relative):
            fail("launch handshake event path is invalid")
        event_name = event_relative.removeprefix(".ralph/")
        directory_fd = _open_ralph_directory()
        descriptor = None
        try:
            descriptor = os.open(event_name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory_fd)
            info = os.fstat(descriptor)
            named = os.stat(event_name, dir_fd=directory_fd, follow_symlinks=False)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.getuid()
                or info.st_nlink != 1
                or info.st_mode & 0o022
                or (info.st_dev, info.st_ino) != (named.st_dev, named.st_ino)
                or (info.st_dev, info.st_ino) != (handshake["events_dev"], handshake["events_ino"])
                or info.st_size != handshake["events_size"]
                or info.st_size > 64 * 1024 * 1024
            ):
                fail("launch handshake event stream changed")
            raw = os.pread(descriptor, handshake["events_size"], 0)
            delta = raw[handshake["events_offset"]:]
            if (
                len(raw) != handshake["events_size"]
                or hashlib.sha256(raw).hexdigest() != handshake["events_sha256"]
                or len(delta) != handshake["events_delta_size"]
                or hashlib.sha256(delta).hexdigest() != handshake["events_delta_sha256"]
                or len(delta) < handshake["start_record_size"]
                or hashlib.sha256(delta[:handshake["start_record_size"]]).hexdigest()
                != handshake["start_record_sha256"]
                or not delta[:handshake["start_record_size"]].endswith(b"\n")
            ):
                fail("launch handshake event bytes changed")
            after = os.fstat(descriptor)
            if (info.st_size, info.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                fail("launch handshake event stream changed while reading")
        finally:
            if descriptor is not None:
                os.close(descriptor)
            os.close(directory_fd)
        record[started_key] = True
        atomic_write(data)


def update_state(args: argparse.Namespace) -> None:
    with recovery_lock():
        data = load()
        if data["status"] != "active" or data["phase"] != args.expect_phase:
            fail(f"expected active phase {args.expect_phase}, found {data['status']}:{data['phase']}")
        allowed_fields = {
            "planning": {"base_commit", "plan_commit"},
            "implementation": {"implementation_commit"},
            "verification": {"verification_commit", "runner_evidence_sha256"},
            "audit": {"audit_commit", "audit_result"},
        }[args.expect_phase]
        record = data["rounds"][-1]
        for assignment in args.round_field:
            if "=" not in assignment:
                fail("--round-field requires key=JSON")
            key, raw = assignment.split("=", 1)
            if key not in allowed_fields:
                fail(f"field {key} is not writable in {args.expect_phase}")
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
            expected_next = {
                "planning": "implementation", "implementation": "verification",
                "verification": "audit",
            }.get(args.expect_phase)
            if args.phase != expected_next:
                fail(f"invalid transition {args.expect_phase} -> {args.phase}")
            if args.expect_phase in {"planning", "implementation"}:
                discard_launch_handshake(args.expect_phase)
            data["phase"] = args.phase
        atomic_write(data)


def discard_audit_coordinator() -> None:
    """Discard the protected audit coordinator binding when the audit phase ends."""
    try:
        remove(ROOT, "audit-coordinator.json")
    except (OSError, StateIOError) as exc:
        fail(f"cannot remove the audit coordinator binding: {exc}")


def advance_state(args: argparse.Namespace) -> None:
    with recovery_lock():
        data = load()
        if data["status"] != "active" or data["phase"] != args.expect_phase or not complete_record(data["rounds"][-1]):
            fail("cannot advance an incomplete audit phase")
        if data["round"] >= data["rounds_requested"]:
            fail("cannot advance beyond requested rounds")
        if git("rev-parse", "HEAD") != data["rounds"][-1]["audit_commit"]:
            fail("cannot advance away from the recorded audit commit")
        discard_launch_handshake("audit")
        discard_audit_coordinator()
        data["round"] += 1
        data["phase"] = "planning"
        record = empty_round(data["round"])
        record["base_commit"] = data["rounds"][-1]["audit_commit"]
        data["rounds"].append(record)
        atomic_write(data)


def finish_state(args: argparse.Namespace) -> None:
    with recovery_lock():
        data = load()
        record = data["rounds"][-1]
        if data["status"] != "active" or data["phase"] != "audit" or not complete_record(record):
            fail("campaign can finish only from a completed audit phase")
        if record["audit_result"] != args.result or git("rev-parse", "HEAD") != record["audit_commit"]:
            fail("finish result or HEAD does not match the recorded audit")
        discard_launch_handshake("audit")
        discard_audit_coordinator()
        data["status"] = "complete" if args.result == "pass" else "blocked"
        data["phase"] = "complete" if args.result == "pass" else "blocked-findings"
        atomic_write(data)


def audit_binding() -> dict[str, object]:
    data = load()
    if data["status"] != "active" or data["phase"] != "audit":
        fail("no active campaign audit binding")
    record = data["rounds"][-1]
    return {
        "round": data["round"],
        "base": record["verification_commit"],
        "runner_evidence_sha256": record["runner_evidence_sha256"],
    }


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
    sub.add_parser("audit-binding")
    get = sub.add_parser("get"); get.add_argument("path")
    update = sub.add_parser("update")
    update.add_argument("--expect-phase", required=True); update.add_argument("--phase")
    update.add_argument("--round-field", action="append", default=[])
    confirm = sub.add_parser("confirm-launch")
    confirm.add_argument("--expect-phase", choices=("planning", "implementation", "audit"), required=True)
    confirm.add_argument("--missing-ok", action="store_true")
    promote = sub.add_parser("promote-verifier-binding")
    promote.add_argument("--expected-old", required=True)
    promote.add_argument("--new", required=True)
    promote.add_argument(
        "--mode", choices=("planning", "implementation", "campaign-audit"), required=True
    )
    rebind = sub.add_parser("rebind-implementation")
    rebind.add_argument("--expected-old", required=True)
    rebind.add_argument("--new", required=True)
    advance = sub.add_parser("advance"); advance.add_argument("--expect-phase", default="audit")
    finish = sub.add_parser("finish"); finish.add_argument("--result", choices=("pass", "findings"), required=True)
    args = parser.parse_args()

    if args.command == "start":
        start_campaign(args)
    elif args.command == "promote-verifier-binding":
        promote_verifier_binding(args)
    elif args.command == "rebind-implementation":
        rebind_implementation(args.expected_old, args.new)
    elif args.command == "update":
        update_state(args)
    elif args.command == "confirm-launch":
        confirm_launch(args)
    elif args.command == "advance":
        advance_state(args)
    elif args.command == "finish":
        finish_state(args)
    elif args.command == "audit-binding":
        print(json.dumps(audit_binding(), sort_keys=True))
    else:
        data = load()
        if args.command == "show":
            print(json.dumps(data, sort_keys=True, indent=2))
        elif args.command == "get":
            value = get_path(data, args.path)
            if isinstance(value, bool):
                print("true" if value else "false")
            elif value is not None:
                print(value if isinstance(value, str) else json.dumps(value, sort_keys=True))
    return 0


if __name__ == "__main__": raise SystemExit(main())
