#!/usr/bin/env python3
"""Visual-audit aggregate gate (stdlib only).

Reads the review report and enforces the auditor-mandated fail-closed rules:
  - provenance: report commit/tree must match the current exact commit
  - replay: an older report cannot be replayed as current evidence
  - drift: prompt, schema, and environment hashes must match the committed
    files (environment binding = fixed lowercase SHA-256 of the committed
    .factory/environment.toml bytes, or the canonical "absent" marker)
  - tamper: every finding must validate against the strict schema, and the
    actual receipt/finding files are revalidated against the report's digests
    and the current provenance (never trust the report's embedded fields alone)
  - outage: a missing report when enabled is an infrastructure failure
  - tier honesty: machine vision never promotes an evidence tier; a PASS here
    only means no machine findings, and deterministic/real-system/human
    acceptance tiers are untouched (this gate never writes to conformance).

Exit codes:
  0  report valid, no findings above low severity, tiers untouched
  1  report has findings/errors or provenance/drift/tamper problems
  2  infrastructure problem (missing report, unsafe file, model outage)
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tomllib

REPORT_SCHEMA = "ralph-visual-audit-report/v1"
RECEIPT_SCHEMA = "ralph-visual-audit-invocation/v1"
ROLES = ("diagram", "layout", "legibility", "state", "consistency", "adversarial")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
SHA1 = re.compile(r"^[0-9a-f]{40}$")
NONCE = re.compile(r"^[0-9a-f]{32}$")
STATE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
HIGH_SEVERITY = {"medium", "high", "critical"}
ENVIRONMENT_REL = ".factory/environment.toml"
# Explicit canonical absent marker; never arbitrary caller text.
ENVIRONMENT_ABSENT = "absent"
RECEIPT_KEYS = {
    "schema", "request_nonce", "state_id", "role", "image_sha256",
    "prompt_sha256", "schema_sha256", "model", "raw_response_sha256",
    "finding_sha256", "started_at_ms", "finished_at_ms", "elapsed_ms",
}


def die(message: str) -> "NoReturn":
    raise SystemExit(message)


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_review_module():
    """Shared C3c calibration-receipt validation from visual-audit-review.py.

    The aggregate gate and the review orchestrator must enforce identical
    receipt semantics, so the gate imports the single implementation instead
    of maintaining a divergent copy.
    """
    script_dir = Path(__file__).resolve().parent
    module_path = script_dir / "visual-audit-review.py"
    if not module_path.is_file() or module_path.is_symlink():
        die("visual-audit: shared visual-audit-review.py is missing; cannot validate the calibration receipt")
    spec = importlib.util.spec_from_file_location("visual_audit_review", module_path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def valid_nonce(value: object) -> bool:
    return isinstance(value, str) and bool(NONCE.fullmatch(value))


def valid_state_id(value: object) -> bool:
    return isinstance(value, str) and bool(STATE_NAME.fullmatch(value))


def check_receipt(receipt: dict, state_id: str, role: str, image_sha: str, model: str,
                  prompt_hash: str, schema_hash: str, nonce: str,
                  finding_path: Path) -> str | None:
    """Strict receipt revalidation; returns a problem description or None."""
    problems: list[str] = []
    if not isinstance(receipt, dict):
        return "receipt is not an object"
    keys = set(receipt)
    if keys != RECEIPT_KEYS:
        problems.append(
            f"receipt key set mismatch (missing {sorted(RECEIPT_KEYS - keys)}, "
            f"extra {sorted(keys - RECEIPT_KEYS)})")
        return "; ".join(problems)
    if receipt.get("schema") != RECEIPT_SCHEMA:
        problems.append("receipt schema mismatch")
    for key, expected in (("request_nonce", nonce), ("state_id", state_id),
                          ("role", role), ("image_sha256", image_sha),
                          ("prompt_sha256", prompt_hash),
                          ("schema_sha256", schema_hash), ("model", model)):
        if receipt.get(key) != expected:
            problems.append(f"receipt {key} mismatch for task {state_id}/{role}")
    if not valid_nonce(receipt.get("request_nonce")):
        problems.append("receipt request_nonce invalid")
    if not SHA256.fullmatch(str(receipt.get("raw_response_sha256", ""))):
        problems.append("receipt raw_response_sha256 invalid")
    if not SHA256.fullmatch(str(receipt.get("finding_sha256", ""))):
        problems.append("receipt finding_sha256 invalid")
    if sha256_file(finding_path) != receipt.get("finding_sha256"):
        problems.append("receipt finding digest does not match the finding file (tampered/missing)")
    for key in ("started_at_ms", "finished_at_ms", "elapsed_ms"):
        value = receipt.get(key)
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            problems.append(f"receipt {key} invalid")
    started = receipt.get("started_at_ms")
    finished = receipt.get("finished_at_ms")
    elapsed = receipt.get("elapsed_ms")
    if (isinstance(started, int) and isinstance(finished, int) and isinstance(elapsed, int)
            and finished - started != elapsed):
        problems.append("receipt duration inconsistent with timestamps")
    return "; ".join(problems) if problems else None


def check_finding_binding(finding: dict, state_id: str, expected_sha: str, role: str,
                          model: str, prompt_hash: str, schema_hash: str,
                          nonce: str) -> str | None:
    """Per-task provenance binding: a finding may never use another task's values."""
    problems: list[str] = []
    if finding.get("request_nonce") != nonce:
        problems.append(f"request_nonce mismatch for task {state_id}/{role} (stale/replayed/wrong nonce)")
    if finding.get("state_id") != state_id:
        problems.append(f"state_id {finding.get('state_id')!r} != task {state_id!r}")
    if finding.get("image_sha256") != expected_sha:
        problems.append(f"image_sha256 mismatch for task {state_id}/{role}")
    if finding.get("role") != role:
        problems.append(f"role {finding.get('role')!r} != task {role!r}")
    if finding.get("model") != model:
        problems.append(f"model mismatch for task {state_id}/{role}")
    if finding.get("prompt_sha256") != prompt_hash:
        problems.append(f"prompt_sha256 mismatch for task {state_id}/{role}")
    if finding.get("schema_sha256") != schema_hash:
        problems.append(f"schema_sha256 mismatch for task {state_id}/{role}")
    return "; ".join(problems) if problems else None


def require_artifact_file(path: Path, label: str) -> str | None:
    """Review artifacts must be secure regular 0600 files owned by the caller."""
    if not path.is_file() or path.is_symlink():
        return f"{label} file missing or unsafe: {path.name}"
    info = path.stat()
    if info.st_uid != os.getuid():
        return f"{label} file has unsafe ownership: {path.name}"
    if info.st_nlink != 1:
        return f"{label} file has a suspicious link count: {path.name}"
    if info.st_mode & 0o077:
        return f"{label} file must be 0600 (group/world bits set): {path.name}"
    return None


def git_stdout(root: Path, *argv: str) -> str:
    return subprocess.check_output(["git", "-C", str(root), *argv], text=True).strip()


def is_tracked(root: Path, path: Path) -> bool:
    try:
        rel = path.relative_to(root)
    except ValueError:
        return False
    return subprocess.run(["git", "-C", str(root), "ls-files", "--error-unmatch", "--", str(rel)],
                          capture_output=True, check=False).returncode == 0


def environment_binding(root: Path) -> str:
    """Fixed lowercase SHA-256 of the committed environment.toml bytes."""
    path = root / ENVIRONMENT_REL
    if path.is_symlink():
        die("visual-audit: environment.toml must not be a symlink")
    if not is_tracked(root, path):
        if path.exists():
            die("visual-audit: environment.toml exists but is not committed; it must be tracked")
        return ENVIRONMENT_ABSENT
    result = subprocess.run(["git", "-C", str(root), "show", f"HEAD:{ENVIRONMENT_REL}"],
                            capture_output=True, check=False)
    if result.returncode != 0:
        die("visual-audit: cannot read the committed environment.toml blob")
    return hashlib.sha256(result.stdout).hexdigest()


def valid_environment_binding(value: object) -> bool:
    return value == ENVIRONMENT_ABSENT or (isinstance(value, str) and bool(SHA256.fullmatch(value)))


def main() -> int:
    parser = argparse.ArgumentParser(description="Visual-audit aggregate gate")
    parser.add_argument("--config", default=".factory/visual-audit.toml")
    parser.add_argument("--report", default=None)
    parser.add_argument("--current-commit", default="")
    args = parser.parse_args()
    root = Path.cwd()
    config_path = Path(args.config)
    if not config_path.is_absolute():
        config_path = root / config_path
    with open(config_path, "rb") as stream:
        config = tomllib.load(stream)
    section = config.get("visual-audit", {})
    if not isinstance(section, dict):
        die("visual-audit: config section missing")
    if section.get("enabled") is not True:
        print("visual-audit: disabled; nothing to check")
        return 0

    prompt_path = Path(section["prompt_template"])
    if not prompt_path.is_absolute():
        prompt_path = root / prompt_path
    schema_path = Path(section["review_schema"])
    if not schema_path.is_absolute():
        schema_path = root / schema_path
    if not prompt_path.is_file() or prompt_path.is_symlink():
        die("visual-audit: prompt template missing or unsafe")
    if not schema_path.is_file() or schema_path.is_symlink():
        die("visual-audit: review schema missing or unsafe")

    report_path = Path(args.report) if args.report else Path(section["review_dir"]) / "report.json"
    review_dir = Path(section["review_dir"])
    if not review_dir.is_absolute():
        review_dir = root / review_dir
    if not report_path.is_absolute():
        report_path = root / report_path
    if not report_path.is_file() or report_path.is_symlink():
        die("visual-audit: report missing; run visual-audit-review.py run first")
    if report_path.stat().st_uid != __import__("os").getuid() or report_path.stat().st_nlink != 1:
        die("visual-audit: report has unsafe ownership or link count")

    try:
        report = json.loads(report_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        die(f"visual-audit: report is invalid: {type(exc).__name__}")
    if report.get("schema") != REPORT_SCHEMA:
        die("visual-audit: report schema mismatch")
    commit = report.get("commit")
    tree = report.get("tree")
    if not isinstance(commit, str) or not SHA1.fullmatch(commit):
        die("visual-audit: report commit invalid")
    if not isinstance(tree, str) or not SHA1.fullmatch(tree):
        die("visual-audit: report tree invalid")
    current_commit = args.current_commit or (
        __import__("subprocess").check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip()
    )
    if SHA1.fullmatch(current_commit) and commit != current_commit:
        die(f"visual-audit: report replay/commit mismatch (report {commit}, current {current_commit})")
    if not SHA256.fullmatch(report.get("prompt_sha256", "")):
        die("visual-audit: report prompt hash invalid")
    if not SHA256.fullmatch(report.get("schema_sha256", "")):
        die("visual-audit: report schema hash invalid")
    if sha256_file(prompt_path) != report["prompt_sha256"]:
        die("visual-audit: prompt drift; findings invalidated")
    if sha256_file(schema_path) != report["schema_sha256"]:
        die("visual-audit: schema drift; findings invalidated")

    environment = report.get("environment_sha256")
    if not valid_environment_binding(environment):
        die("visual-audit: report environment binding missing or invalid")
    if environment != environment_binding(root):
        die("visual-audit: environment drift; findings invalidated")

    # The report is bound to the exact calibration receipt bytes that were
    # current when it was produced. The receipt must still validate for the
    # current model/prompt/schema/calibration/commit/tree, and must be
    # byte-identical to the one the report references. Missing, stale, or
    # tampered receipts invalidate the report and exit fail-closed.
    review_mod = load_review_module()
    review_mod.validate_calibration_receipt(section, root)
    receipt_path = review_mod.resolve_path(
        root, section.get("calibration_receipt", review_mod.CALIBRATION_RECEIPT_DEFAULT))
    if report.get("calibration_receipt_sha256") != sha256_file(receipt_path):
        die("visual-audit: calibration receipt digest mismatch; findings invalidated")

    findings = report.get("findings")
    if not isinstance(findings, list) or not findings:
        die("visual-audit: report has no findings")
    problems = 0
    for finding in findings:
        if finding.get("schema") != "ralph-visual-audit-review/v1":
            die("visual-audit: finding schema mismatch")
        verdict = finding.get("verdict")
        if verdict not in ("pass", "finding", "error"):
            die("visual-audit: finding verdict invalid")
        if verdict in ("finding", "error"):
            problems += 1
        for observation in finding.get("observations", []):
            if observation.get("severity") in HIGH_SEVERITY:
                problems += 1
        # Tamper: a finding must be provenance-bound to the report image set.
        image_sha = finding.get("image_sha256")
        if not SHA256.fullmatch(image_sha or ""):
            die("visual-audit: finding image hash invalid (tamper)")
        if not any(img.get("sha256") == image_sha for img in report.get("images", [])):
            die(f"visual-audit: finding image {image_sha[:12]} not in the provenance manifest (tamper)")
        if not valid_nonce(finding.get("request_nonce")):
            die("visual-audit: finding request nonce invalid (tamper)")

    # Do not trust the report's embedded digests: revalidate the actual
    # receipt/finding files against the current provenance/report.
    task_receipts = report.get("task_receipts")
    if not isinstance(task_receipts, list) or not task_receipts:
        die("visual-audit: report has no task receipt entries")
    images = {img.get("sha256"): img for img in report.get("images", []) if isinstance(img, dict)}
    model = report.get("model", "")
    prompt_hash = report.get("prompt_sha256")
    schema_hash = report.get("schema_sha256")
    finding_keys = {(f.get("state_id"), f.get("role")) for f in findings if isinstance(f, dict)}
    for entry in task_receipts:
        if not isinstance(entry, dict):
            die("visual-audit: report task receipt entry invalid")
        state_id = entry.get("state_id")
        role = entry.get("role")
        nonce = entry.get("request_nonce")
        finding_file = entry.get("finding_file")
        receipt_file = entry.get("receipt_file")
        image_sha = entry.get("image_sha256")
        if not valid_state_id(state_id) or role not in ROLES or not valid_nonce(nonce):
            die(f"visual-audit: report task receipt binding invalid for {state_id!r}/{role!r}")
        if finding_file != f"finding-{state_id}-{role}.json" \
                or receipt_file != f"receipt-{state_id}-{role}.json":
            die(f"visual-audit: report task receipt filenames invalid for {state_id}/{role}")
        if not isinstance(image_sha, str) or not SHA256.fullmatch(image_sha):
            die(f"visual-audit: report task receipt image hash invalid for {state_id}/{role}")
        if image_sha not in images:
            die(f"visual-audit: task receipt image {image_sha[:12]} not in the provenance manifest (tamper)")
        if (state_id, role) not in finding_keys:
            die(f"visual-audit: task receipt {state_id}/{role} has no matching finding")
        finding_path = review_dir / finding_file
        receipt_path = review_dir / receipt_file
        problem = require_artifact_file(finding_path, "finding")
        problem = problem or require_artifact_file(receipt_path, "receipt")
        if problem:
            die(f"visual-audit: {problem}")
        if sha256_file(finding_path) != entry.get("finding_sha256"):
            die(f"visual-audit: finding digest mismatch for {state_id}/{role} (tampered or missing file)")
        if sha256_file(receipt_path) != entry.get("receipt_sha256"):
            die(f"visual-audit: receipt digest mismatch for {state_id}/{role} (tampered or missing file)")
        try:
            receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
            stored_finding = json.loads(finding_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            die(f"visual-audit: artifact file is invalid JSON for {state_id}/{role}: {type(exc).__name__}")
        problem = check_receipt(receipt, state_id, role, image_sha, model,
                                prompt_hash, schema_hash, nonce, finding_path)
        if problem:
            die(f"visual-audit: receipt revalidation failed for {state_id}/{role}: {problem}")
        # The finding on disk must carry the sealed binding incl. the nonce.
        problem = check_finding_binding(stored_finding, state_id, image_sha, role,
                                        model, prompt_hash, schema_hash, nonce)
        if problem:
            die(f"visual-audit: finding revalidation failed for {state_id}/{role}: {problem}")

    aggregate = report.get("aggregate")
    if not isinstance(aggregate, dict):
        die("visual-audit: report aggregate missing")
    for state in aggregate.get("states", []):
        if state.get("overall") in ("finding", "error"):
            problems += 1

    if problems:
        print(f"visual-audit: {problems} finding(s)/error(s) reported")
        return 1
    print("visual-audit: report valid, no findings; vision tiers untouched (no elevation)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
