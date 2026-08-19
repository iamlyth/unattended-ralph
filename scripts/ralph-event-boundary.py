#!/usr/bin/env python3
"""Snapshot and strictly validate Ralph event reception for one attempt."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys

from factory_state_io import (
    StateIOError,
    atomic_write_json,
    read_bytes,
    read_json,
    read_text,
    remove,
    require_linux_primitives,
)

ROOT = Path(__file__).resolve().parent.parent
RALPH = ROOT / ".ralph"
TOKENS = {
    "PLAN_COMPLETE",
    "LOOP_COMPLETE",
    "AUDIT_COMPLETE",
    "MAINTENANCE_PLAN_COMPLETE",
    "MAINTENANCE_COMPLETE",
}
MODES = {"planning", "implementation", "campaign-audit", "maintenance-planning", "maintenance"}
START_TOPICS = {
    "planning": "factory.plan",
    "implementation": "factory.implement",
    "campaign-audit": "factory.audit",
    "maintenance-planning": "factory.maintenance.plan",
    "maintenance": "factory.maintenance.implement",
}
PROMPTS = {
    "planning": ".factory/prompts/plan.md",
    "implementation": ".factory/prompts/implementation.md",
    "campaign-audit": ".factory/prompts/audit.md",
    "maintenance-planning": ".factory/prompts/maintenance-plan.md",
    "maintenance": ".factory/prompts/maintenance.md",
}
EVENT_NAME = re.compile(r"^events(?:-[0-9]{8}-[0-9]{6})?\.jsonl$")
LOOP_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
HEX64 = re.compile(r"^[0-9a-f]{64}$")
TIMESTAMP = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$"
)


class BoundaryError(RuntimeError):
    pass


def _ralph_fd() -> int:
    require_linux_primitives()
    descriptor = os.open(RALPH, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        info = os.fstat(descriptor)
        named = RALPH.lstat()
        if (
            not stat.S_ISDIR(info.st_mode)
            or info.st_uid != os.getuid()
            or info.st_mode & 0o022
            or (info.st_dev, info.st_ino) != (named.st_dev, named.st_ino)
        ):
            raise BoundaryError("unsafe .ralph directory")
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _validate_owned_file(info: os.stat_result, label: str, maximum: int) -> None:
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.getuid()
        or info.st_nlink != 1
        or info.st_mode & 0o022
        or info.st_size > maximum
    ):
        raise BoundaryError(f"unsafe {label}")


def _read_marker(directory_fd: int, name: str, *, missing_ok: bool = False) -> str | None:
    try:
        descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory_fd)
    except FileNotFoundError:
        if missing_ok:
            return None
        raise BoundaryError(f"missing Ralph marker {name}")
    try:
        info = os.fstat(descriptor)
        named = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        _validate_owned_file(info, f"Ralph marker {name}", 4096)
        _validate_owned_file(named, f"Ralph marker {name}", 4096)
        if (info.st_dev, info.st_ino) != (named.st_dev, named.st_ino):
            raise BoundaryError(f"unsafe Ralph marker {name}")
        raw = os.read(descriptor, 4097)
        after = os.fstat(descriptor)
        if (
            len(raw) > 4096
            or (info.st_size, info.st_mtime_ns) != (after.st_size, after.st_mtime_ns)
        ):
            raise BoundaryError(f"Ralph marker changed while reading: {name}")
        return raw.decode("utf-8").strip()
    except UnicodeError as exc:
        raise BoundaryError(f"Ralph marker is not UTF-8: {name}") from exc
    finally:
        os.close(descriptor)


def _read_open_file(descriptor: int, size: int, maximum: int, label: str) -> bytes:
    if size > maximum:
        raise BoundaryError(f"{label} exceeds limit")
    raw = os.pread(descriptor, size, 0)
    after = os.fstat(descriptor)
    if len(raw) != size or after.st_size != size:
        raise BoundaryError(f"{label} changed while reading")
    return raw


def _event_files(directory_fd: int) -> dict[str, dict[str, int | str]]:
    result: dict[str, dict[str, int | str]] = {}
    for name in os.listdir(directory_fd):
        if not EVENT_NAME.fullmatch(name):
            continue
        descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory_fd)
        try:
            info = os.fstat(descriptor)
            named = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            _validate_owned_file(info, f"Ralph event stream: {name}", 64 * 1024 * 1024)
            _validate_owned_file(named, f"Ralph event stream: {name}", 64 * 1024 * 1024)
            if (info.st_dev, info.st_ino) != (named.st_dev, named.st_ino):
                raise BoundaryError(f"unsafe Ralph event stream: {name}")
            raw = _read_open_file(descriptor, info.st_size, 64 * 1024 * 1024, name)
            after = os.fstat(descriptor)
            if (info.st_size, info.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                raise BoundaryError(f"Ralph event stream changed while snapshotting: {name}")
            result[name] = {
                "dev": info.st_dev,
                "ino": info.st_ino,
                "size": info.st_size,
                "mtime_ns": info.st_mtime_ns,
                "sha256": hashlib.sha256(raw).hexdigest(),
            }
        finally:
            os.close(descriptor)
    return result


def _snapshot_name(mode: str) -> str:
    return f"ralph-attempt-{mode}.json"


def _campaign_binding(mode: str) -> tuple[str | None, str | None, str | None]:
    round_number = os.environ.get("FACTORY_CAMPAIGN_ROUND")
    phase = os.environ.get("FACTORY_CAMPAIGN_PHASE")
    if round_number is None and phase is None:
        return None, None, None
    expected_phase = "audit" if mode == "campaign-audit" else mode
    if phase != expected_phase or round_number is None or not re.fullmatch(r"[1-9][0-9]*", round_number):
        raise BoundaryError("invalid campaign launch binding")
    raw = read_bytes(ROOT, "ralph-campaign.json", maximum=1024 * 1024)
    assert raw is not None
    return round_number, phase, hashlib.sha256(raw).hexdigest()


def begin(mode: str) -> None:
    attempt = os.environ.get("FACTORY_RALPH_ATTEMPT_ID", "")
    cycle = os.environ.get("FACTORY_RALPH_CYCLE_ID", "")
    if not HEX64.fullmatch(attempt) or not HEX64.fullmatch(cycle):
        raise BoundaryError("missing attempt or cycle binding")
    campaign_round, campaign_phase, campaign_digest = _campaign_binding(mode)
    directory_fd = _ralph_fd()
    try:
        data = {
            "schema": "ralph-event-snapshot/v2",
            "mode": mode,
            "attempt_id": attempt,
            "cycle_id": cycle,
            "campaign_round": campaign_round,
            "campaign_phase": campaign_phase,
            "campaign_state_sha256": campaign_digest,
            "files": _event_files(directory_fd),
            "current_events": _read_marker(directory_fd, "current-events", missing_ok=True),
            "current_loop_id": _read_marker(directory_fd, "current-loop-id", missing_ok=True),
        }
    finally:
        os.close(directory_fd)
    atomic_write_json(ROOT, _snapshot_name(mode), data)


def _ordered_strings(value: object) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        result: list[str] = []
        for item in value:
            result.extend(_ordered_strings(item))
        return result
    if isinstance(value, dict):
        result = []
        for key, item in value.items():
            result.extend(_ordered_strings(key))
            result.extend(_ordered_strings(item))
        return result
    return []


def _contains_reserved(strings: list[str]) -> bool:
    combined = "".join(strings)
    return any(token in value for token in TOKENS for value in (*strings, combined))


def _expected_prompt(mode: str) -> str:
    path = ROOT / PROMPTS[mode]
    absolute = path.absolute()
    try:
        if absolute.resolve(strict=True) != absolute:
            raise BoundaryError("Ralph prompt path contains a symlink")
        descriptor = os.open(absolute, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError as exc:
        raise BoundaryError(f"cannot safely open Ralph prompt: {exc}") from exc
    try:
        info = os.fstat(descriptor)
        _validate_owned_file(info, "Ralph prompt", 1024 * 1024)
        raw = _read_open_file(descriptor, info.st_size, 1024 * 1024, "Ralph prompt")
        return raw.decode("utf-8")
    except UnicodeError as exc:
        raise BoundaryError("Ralph prompt is not UTF-8") from exc
    finally:
        os.close(descriptor)


def _prompt_matches(received: object, expected: str) -> bool:
    if received == expected:
        return True
    truncated = expected[:500] + f"... [truncated, {len(expected)} chars total]"
    return received == truncated


def _trusted_start(record: object, mode: str, prompt: str) -> bool:
    return (
        isinstance(record, dict)
        and set(record) == {"ts", "iteration", "hat", "topic", "triggered", "payload"}
        and record.get("iteration") == 0
        and record.get("hat") == "loop"
        and record.get("topic") == START_TOPICS[mode]
        and isinstance(record.get("ts"), str)
        and bool(TIMESTAMP.fullmatch(record["ts"]))
        and record.get("triggered") == "planner"
        and _prompt_matches(record.get("payload"), prompt)
    )


def _validate_protocol_record(record: object, mode: str) -> None:
    if not isinstance(record, dict):
        raise BoundaryError("event record is not an object")
    timestamp = record.get("ts")
    if not isinstance(timestamp, str) or not TIMESTAMP.fullmatch(timestamp):
        raise BoundaryError("event record has an invalid timestamp")
    topic = record.get("topic")
    if topic == START_TOPICS[mode]:
        if set(record) != {"ts", "topic", "payload"} or not isinstance(record.get("payload"), str):
            raise BoundaryError("mode publication has an invalid schema")
        return
    if topic in {"iteration.summary", "loop.terminate"}:
        if (
            set(record) != {"ts", "iteration", "hat", "topic", "payload"}
            or type(record.get("iteration")) is not int
            or record["iteration"] < 1
            or record.get("hat") != "loop"
            or not isinstance(record.get("payload"), str)
        ):
            raise BoundaryError(f"{topic} record has an invalid schema")
        if topic == "iteration.summary":
            try:
                summary = json.loads(record["payload"])
            except json.JSONDecodeError as exc:
                raise BoundaryError("iteration summary payload is not JSON") from exc
            summary_keys = {
                "cache_read_tokens", "cache_write_tokens", "context_pct", "context_tokens",
                "context_window", "cost_usd", "duration_ms", "input_tokens", "num_turns",
                "output_tokens",
            }
            if not isinstance(summary, dict) or set(summary) != summary_keys:
                raise BoundaryError("iteration summary payload has an invalid schema")
        return
    raise BoundaryError(f"event topic is outside the lifecycle protocol allowlist: {topic!r}")


def _read_delta(
    directory_fd: int, name: str, offset: int, prior_digest: str | None,
) -> tuple[list[tuple[object, bytes]], os.stat_result, bytes]:
    descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory_fd)
    try:
        info = os.fstat(descriptor)
        named = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        _validate_owned_file(info, f"Ralph event stream: {name}", 64 * 1024 * 1024)
        _validate_owned_file(named, f"Ralph event stream: {name}", 64 * 1024 * 1024)
        if (
            (info.st_dev, info.st_ino) != (named.st_dev, named.st_ino)
            or info.st_size < offset
        ):
            raise BoundaryError(f"event stream changed unsafely: {name}")
        if offset and prior_digest is not None:
            prefix = os.pread(descriptor, offset, 0)
            if len(prefix) != offset or hashlib.sha256(prefix).hexdigest() != prior_digest:
                raise BoundaryError(f"event stream prefix was rewritten: {name}")
        length = info.st_size - offset
        if length > 8 * 1024 * 1024:
            raise BoundaryError("attempt event delta exceeds 8 MiB")
        raw = os.pread(descriptor, length, offset)
        after = os.fstat(descriptor)
        if len(raw) != length or (info.st_size, info.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
            raise BoundaryError(f"event stream changed while validating: {name}")
    finally:
        os.close(descriptor)
    if raw and not raw.endswith(b"\n"):
        raise BoundaryError(f"event stream lacks a complete final record: {name}")
    records: list[tuple[object, bytes]] = []
    for line in raw.splitlines(keepends=True):
        if line == b"\n":
            raise BoundaryError(f"event stream contains an empty record: {name}")
        try:
            record = json.loads(line.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise BoundaryError(f"invalid event record in {name}: {exc}") from exc
        records.append((record, line))
    return records, info, raw


def finish(mode: str) -> bool:
    snapshot = read_json(ROOT, _snapshot_name(mode), maximum=1024 * 1024)
    expected_snapshot_keys = {
        "schema", "mode", "attempt_id", "cycle_id", "campaign_round",
        "campaign_phase", "campaign_state_sha256", "files", "current_events",
        "current_loop_id",
    }
    if not isinstance(snapshot, dict) or set(snapshot) != expected_snapshot_keys:
        raise BoundaryError("attempt event snapshot has an invalid schema")
    attempt = os.environ.get("FACTORY_RALPH_ATTEMPT_ID", "")
    cycle = os.environ.get("FACTORY_RALPH_CYCLE_ID", "")
    campaign_round, campaign_phase, campaign_digest = _campaign_binding(mode)
    if (
        snapshot["schema"] != "ralph-event-snapshot/v2"
        or snapshot["mode"] != mode
        or snapshot["attempt_id"] != attempt
        or snapshot["cycle_id"] != cycle
        or snapshot["campaign_round"] != campaign_round
        or snapshot["campaign_phase"] != campaign_phase
        or snapshot["campaign_state_sha256"] != campaign_digest
    ):
        raise BoundaryError("attempt event snapshot binding changed")
    before_files = snapshot["files"]
    if not isinstance(before_files, dict):
        raise BoundaryError("attempt event snapshot files are invalid")

    prompt = _expected_prompt(mode)
    directory_fd = _ralph_fd()
    try:
        current_events = _read_marker(directory_fd, "current-events", missing_ok=True)
        current_loop = _read_marker(directory_fd, "current-loop-id", missing_ok=True)
        if not isinstance(current_events, str) or not current_events.startswith(".ralph/"):
            raise BoundaryError("current-events marker is missing or non-canonical")
        event_name = current_events.removeprefix(".ralph/")
        if not EVENT_NAME.fullmatch(event_name):
            raise BoundaryError("current-events identifies an invalid event stream")
        after_files = _event_files(directory_fd)
        changed_names: list[str] = []
        for name, current in after_files.items():
            prior = before_files.get(name)
            if prior != current:
                changed_names.append(name)
        for name in before_files:
            if name not in after_files:
                raise BoundaryError(f"event stream disappeared during attempt: {name}")
        if changed_names != [event_name]:
            if not changed_names:
                return False
            raise BoundaryError("attempt changed an event stream outside current-events")
        current = after_files[event_name]
        prior = before_files.get(event_name)
        offset = 0
        prior_digest: str | None = None
        if isinstance(prior, dict):
            if prior.get("dev") != current["dev"] or prior.get("ino") != current["ino"]:
                raise BoundaryError("current event stream inode changed during attempt")
            old_size = prior.get("size")
            prior_digest = prior.get("sha256") if isinstance(prior.get("sha256"), str) else None
            if type(old_size) is not int or current["size"] < old_size or prior_digest is None:
                raise BoundaryError("event stream snapshot is invalid")
            offset = old_size
        records, event_info, delta = _read_delta(directory_fd, event_name, offset, prior_digest)
    finally:
        os.close(directory_fd)

    if not records:
        return False
    first_record, first_line = records[0]
    if not _trusted_start(first_record, mode, prompt):
        raise BoundaryError("attempt does not begin with the exact trusted start record")
    protocol_strings: list[str] = []
    for record, _line in records[1:]:
        protocol_strings.extend(_ordered_strings(record))
        if _contains_reserved(protocol_strings):
            raise BoundaryError("reserved lifecycle token contaminated received event stream")
        _validate_protocol_record(record, mode)

    if not isinstance(current_loop, str) or not LOOP_ID.fullmatch(current_loop):
        raise BoundaryError("current-loop-id is missing or invalid")
    recorded_mode = read_text(ROOT, "loop-mode", maximum=128)
    if recorded_mode is None or recorded_mode.strip() != mode:
        raise BoundaryError("loop-mode does not match the received event attempt")
    handshake = {
        "schema": "ralph-launch-handshake/v2",
        "mode": mode,
        "cycle_id": cycle,
        "attempt_id": attempt,
        "campaign_round": campaign_round,
        "campaign_phase": campaign_phase,
        "campaign_state_sha256": campaign_digest,
        "loop_id": current_loop,
        "events": current_events,
        "events_dev": event_info.st_dev,
        "events_ino": event_info.st_ino,
        "events_offset": offset,
        "events_size": event_info.st_size,
        "events_sha256": current["sha256"],
        "events_delta_size": len(delta),
        "events_delta_sha256": hashlib.sha256(delta).hexdigest(),
        "start_record_size": len(first_line),
        "start_record_sha256": hashlib.sha256(first_line).hexdigest(),
        "start_record_nonce": attempt,
    }
    atomic_write_json(ROOT, f"ralph-launch-handshake-{mode}.json", handshake)
    remove(ROOT, _snapshot_name(mode))
    return True


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("operation", choices=("begin", "finish"))
    parser.add_argument("mode", choices=sorted(MODES))
    args = parser.parse_args()
    try:
        if args.operation == "begin":
            begin(args.mode)
        else:
            raise SystemExit(0 if finish(args.mode) else 3)
    except (OSError, StateIOError, BoundaryError) as exc:
        raise SystemExit(f"ralph-event-boundary: {exc}") from exc


if __name__ == "__main__":
    main()
