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
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RECEIPT = re.compile(r"\[receipt:\s*([^\]]+)\]")
MANIFEST = re.compile(r"\[manifest:\s*([^\]]+)\]")
STATUS = re.compile(r"\b(PASS|FAIL|BLOCKED)\b")
SHA256 = re.compile(r"^[0-9a-f]{64}$")


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


def validate_receipt(root: Path, reference: str) -> dict:
    path = resolve(root, reference)
    data = regular_json(path)
    expected = {
        "schema", "tag", "argv", "argv_sha256", "exit_code",
        "stdout_sha256", "stderr_sha256", "started_at", "finished_at", "evidence_commit",
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
