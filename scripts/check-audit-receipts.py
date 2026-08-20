#!/usr/bin/env python3
"""Validate executable-evidence lines in a campaign audit against machine receipts.

Every `- Executable evidence:` line in `## Evidence reviewed` must carry a
status marker (PASS, FAIL, or BLOCKED) and a machine reference:
- `[receipt: <path>]` for a coordinator-executed command recorded by
  `scripts/machine-receipt.py`, or
- `[manifest: <path>]` for an accepted runner receipt manifest.

Prose-only claims cannot certify runtime. A PASS claim requires a receipt with
exit 0 (or a pass manifest); a FAIL claim requires a receipt with a non-zero
exit. Any BLOCKED evidence forces `result: findings`: a report with BLOCKED
content may never claim `result: pass`.

Exact-commit and coordinator bindings (provenance hardening):
- every receipt must record a strict 40-hex `evidence_commit`;
- under a campaign audit binding (`FACTORY_CAMPAIGN_AUDIT_ROUND` and
  `FACTORY_CAMPAIGN_AUDIT_BASE`), every receipt's `evidence_commit` must equal
  the audit base and its `coordinator_round` must equal the round, so stale
  receipts and receipts reused across rounds are rejected;
- when the protected `.factory-state/audit-coordinator.json` state exists, the
  receipt's coordinator nonce must match it (receipts minted outside the audit
  coordinator's bounded invocation are rejected);
- receipts without the coordinator binding fields are legacy/local and
  rejected.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RECEIPT = re.compile(r"\[receipt:\s*([^\]]+)\]")
MANIFEST = re.compile(r"\[manifest:\s*([^\]]+)\]")
STATUS = re.compile(r"\b(PASS|FAIL|BLOCKED)\b")
SHA1 = re.compile(r"^[0-9a-f]{40}$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
COORDINATOR_FILE = ".factory-state/audit-coordinator.json"


def fail(message: str) -> None:
    raise SystemExit(f"audit-receipts: {message}")


def regular_json(path: Path) -> dict:
    if path.is_symlink() or not path.is_file():
        fail(f"unsafe or missing evidence file: {path}")
    try:
        raw = path.read_bytes()
        data = json.loads(raw)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        fail(f"invalid evidence file {path}: {exc}")
    if not isinstance(data, dict):
        fail(f"evidence must be an object: {path}")
    return data


def resolve(root: Path, reference: str) -> Path:
    path = Path(reference)
    if path.is_absolute() or ".." in path.parts:
        fail(f"evidence reference escapes the repository: {reference}")
    resolved = (root / reference).resolve()
    try:
        resolved.relative_to(root.resolve())
    except ValueError:
        fail(f"evidence reference escapes the repository: {reference}")
    return resolved


def campaign_binding(root: Path) -> tuple[int | None, str | None, str | None]:
    """Return (round, base, nonce) from the environment and protected state."""
    env_round = os.environ.get("FACTORY_CAMPAIGN_AUDIT_ROUND", "")
    env_base = os.environ.get("FACTORY_CAMPAIGN_AUDIT_BASE", "")
    env_nonce = os.environ.get("FACTORY_CAMPAIGN_AUDIT_NONCE", "")
    state = root / COORDINATOR_FILE
    state_round = None
    state_base = None
    state_nonce = None
    if state.exists():
        if state.is_symlink() or not state.is_file():
            fail("audit coordinator state is unsafe")
        try:
            data = json.loads(state.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            fail(f"invalid audit coordinator state: {exc}")
        expected = {"schema", "round", "base_commit", "nonce", "created_at"}
        if (
            not isinstance(data, dict)
            or set(data) != expected
            or data.get("schema") != "ralph-audit-coordinator/v1"
            or type(data.get("round")) is not int
            or data["round"] < 1
            or not isinstance(data.get("base_commit"), str)
            or not SHA1.fullmatch(data["base_commit"])
            or not isinstance(data.get("nonce"), str)
            or not SHA256.fullmatch(data["nonce"])
        ):
            fail("audit coordinator state is invalid")
        state_round = data["round"]
        state_base = data["base_commit"]
        state_nonce = data["nonce"]
        if env_round and env_round.isdigit() and int(env_round) != data["round"]:
            fail("campaign audit round does not match the protected coordinator state")
        if env_base and env_base != data["base_commit"]:
            fail("campaign audit base does not match the protected coordinator state")
        if env_nonce and env_nonce != data["nonce"]:
            fail("campaign audit nonce does not match the protected coordinator state")
    round_number = int(env_round) if env_round.isdigit() else state_round
    base = env_base or state_base
    nonce = state_nonce or env_nonce or None
    if (round_number is None) != (base is None):
        fail("campaign audit round and base bindings must be supplied together")
    return round_number, base, nonce


def validate_receipt(root: Path, reference: str) -> dict:
    path = resolve(root, reference)
    data = regular_json(path)
    expected = {
        "schema", "tag", "argv", "argv_sha256", "exit_code",
        "stdout_sha256", "stderr_sha256", "started_at", "finished_at",
        "evidence_commit", "coordinator_round", "coordinator_nonce",
    }
    if set(data) != expected or data.get("schema") != "ralph-audit-receipt/v1":
        fail(f"receipt schema is invalid: {reference}")
    if not isinstance(data["argv"], list) or not all(isinstance(item, str) and item for item in data["argv"]):
        fail(f"receipt argv is invalid: {reference}")
    argv_digest = hashlib.sha256(json.dumps(data["argv"], separators=(",", ":")).encode()).hexdigest()
    if data["argv_sha256"] != argv_digest:
        fail(f"receipt argv digest mismatch: {reference}")
    for field in ("stdout_sha256", "stderr_sha256"):
        if not isinstance(data[field], str) or not SHA256.fullmatch(data[field]):
            fail(f"receipt {field} is invalid: {reference}")
    for log_name, digest_field in (("stdout", "stdout_sha256"), ("stderr", "stderr_sha256")):
        log_path = path.parent / f"{path.stem}.{log_name}"
        if log_path.is_symlink() or not log_path.is_file():
            fail(f"receipt log is missing: {log_path}")
        if hashlib.sha256(log_path.read_bytes()).hexdigest() != data[digest_field]:
            fail(f"receipt log digest mismatch: {log_path}")
    if not isinstance(data["exit_code"], int):
        fail(f"receipt exit_code is invalid: {reference}")
    evidence_commit = data["evidence_commit"]
    if not isinstance(evidence_commit, str) or not SHA1.fullmatch(evidence_commit):
        fail(f"receipt evidence_commit must be a strict 40-hex commit: {reference}")
    coordinator_round = data["coordinator_round"]
    if type(coordinator_round) is not int or coordinator_round < 1:
        fail(f"receipt coordinator_round is invalid: {reference}")
    coordinator_nonce = data["coordinator_nonce"]
    if not isinstance(coordinator_nonce, str) or not SHA256.fullmatch(coordinator_nonce):
        fail(f"receipt coordinator_nonce is invalid: {reference}")
    return data


def validate_manifest(root: Path, reference: str) -> dict:
    path = resolve(root, reference)
    data = regular_json(path)
    if data.get("schema") != "factory-runner-receipt/v1" or data.get("result") != "pass":
        fail(f"runner manifest does not prove a clean pass: {reference}")
    if data.get("exit_code") != 0:
        fail(f"runner manifest has a nonzero exit: {reference}")
    return data


def parse_evidence(root: Path, report: Path) -> tuple[list[dict], bool]:
    text = report.read_text(encoding="utf-8")
    match = re.search(r"^## Evidence reviewed\s*\n(.*?)(?=^## |\Z)", text, re.M | re.S)
    if not match:
        fail("audit report has no Evidence section")
    blocked_anywhere = bool(re.search(r"\bBLOCKED\b", text))
    expected_round, expected_base, expected_nonce = campaign_binding(root)
    lines: list[dict] = []
    for line in match.group(1).splitlines():
        stripped = line.strip()
        if not stripped.startswith("- Executable evidence:"):
            continue
        status = STATUS.search(stripped)
        if not status:
            fail(f"executable evidence line lacks a PASS/FAIL/BLOCKED marker: {stripped}")
        marker = status.group(1)
        receipt_match = RECEIPT.search(stripped)
        manifest_match = MANIFEST.search(stripped)
        if marker == "BLOCKED":
            lines.append({"line": stripped, "marker": marker, "blocked": True})
            continue
        if not receipt_match and not manifest_match:
            fail(f"executable evidence line has no receipt/manifest reference (fabricated prose): {stripped}")
        receipt = None
        if receipt_match:
            receipt = validate_receipt(root, receipt_match.group(1))
            if expected_round is not None and receipt["coordinator_round"] != expected_round:
                fail(
                    f"receipt {receipt_match.group(1)} belongs to round "
                    f"{receipt['coordinator_round']}, not the active round {expected_round} "
                    f"(stale or reused across rounds)"
                )
            if expected_base is not None and receipt["evidence_commit"] != expected_base:
                fail(
                    f"receipt {receipt_match.group(1)} evidence_commit {receipt['evidence_commit'][:12]} "
                    f"does not equal the campaign audit base {expected_base[:12]} (stale or reused)"
                )
            if expected_nonce is not None and receipt["coordinator_nonce"] != expected_nonce:
                fail(f"receipt {receipt_match.group(1)} does not match the active audit coordinator nonce")
        if manifest_match:
            validate_manifest(root, manifest_match.group(1))
        exit_code = receipt["exit_code"] if receipt is not None else 0
        if marker == "PASS" and exit_code != 0:
            fail(f"PASS claim has a receipt with exit {exit_code}: {stripped}")
        if marker == "FAIL" and exit_code == 0 and receipt is not None:
            fail(f"FAIL claim has a receipt with exit 0: {stripped}")
        lines.append({"line": stripped, "marker": marker, "blocked": False})
    return lines, blocked_anywhere


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("path", nargs="?", default=str(ROOT / ".factory/artifacts/campaign-audit.md"))
    parser.add_argument("--root", default=str(ROOT))
    args = parser.parse_args()
    root = Path(args.root).resolve()
    report_path = root / args.path if not Path(args.path).is_absolute() else Path(args.path)
    if report_path.is_symlink() or not report_path.is_file():
        fail(f"audit report is missing: {report_path}")
    result = None
    for line in report_path.read_text(encoding="utf-8").splitlines():
        match = re.fullmatch(r"result:\s*(pass|findings)", line.strip())
        if match:
            result = match.group(1)
    if result is None:
        fail("audit report has no machine-readable result")
    lines, blocked_any = parse_evidence(root, report_path)
    if blocked_any and result == "pass":
        fail("BLOCKED evidence forces result: findings")
    if result == "pass":
        for entry in lines:
            if entry["marker"] == "FAIL":
                fail(f"pass audit contains a FAIL evidence line: {entry['line']}")
    print(f"audit-receipts: valid ({len(lines)} executable evidence line(s), result={result})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
