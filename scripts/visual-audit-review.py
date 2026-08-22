#!/usr/bin/env python3
"""Parallel read-only machine visual-audit review (stdlib only).

The auditor-mandated pipeline:
  1. Serial/isolated exact-commit capture (visual-audit-capture.sh) produces a
     provenance-bound image set; the capture lease is a dedicated CLOEXEC
     lease, never the campaign factory lock.
  2. Calibration must pass with the exact frozen model/prompt/schema: a
     known-good image is reviewed 3 times (all must pass) and every known-bad
     image must be flagged. Any misclassification, drift, outage, or missing
     image fails closed and blocks live review.
  3. Live review runs independent role shards (diagram, layout, legibility,
     state, consistency, adversarial) in parallel over the frozen artifacts;
     every finding is validated against the committed strict schema and is
     provenance-bound to the exact captured image bytes.
  4. Aggregation is deterministic and advisory: machine vision never
     certifies runtime, never promotes an evidence tier, and can only add
     findings. A receipt proves invocation, not visual truth.

Request/response sealing (per task):
  - The orchestrator generates a cryptographically random request nonce (>= 128
    bits, 32 lowercase hex chars) per task and passes it to the SDK driver. The
    frozen prompt demands the exact echo, and the finding schema requires it.
  - The SDK verifies every model-returned sealed field (schema, state_id,
    image hash, role, model, prompt hash, schema hash, nonce) against the task
    values and never repairs/overwrites them; any mismatch is rejected per task
    before aggregation.
  - The SDK writes a strict invocation receipt (schema, nonce, task bindings,
    input image hash, raw response SHA-256, normalized finding SHA-256,
    timestamps/duration, model identity) as a regular 0600 file. The Python
    side verifies receipt exact keys/values, filename, ownership/link/mode,
    finding digest, and task binding; a missing/tampered/replayed receipt or
    finding fails closed.
  - The report deterministically includes each receipt SHA-256 and finding
    SHA-256; `report-check` and `check-visual-audit.py` revalidate the actual
    receipt/finding files and digests against the current provenance/report
    instead of trusting report fields. The nonce proves invocation freshness
    only; supplemental authority stays in the docs.

Trust hardening (before ANY reviewer/model subprocess is spawned, `run`
performs provenance verification and fails closed on):
  - image ownership/link/symlink/hash and manifest schema
  - manifest commit == current HEAD and manifest tree == HEAD^{tree}
  - the environment binding is the fixed lowercase SHA-256 of the committed
    .factory/environment.toml bytes (or the canonical marker "absent"); caller
    text is never accepted
  - tracked worktree/index cleanliness for the relevant framework/capture
    inputs
  - state ids are validated before any output/calibration path is built
Every returned finding is then re-validated per task: its state_id,
image_sha256, role, model, prompt hash, schema hash and request nonce must
exactly equal the task values, so a finding can never borrow another manifest
image or a stale invocation.

Execution-path overrides (VISUAL_AUDIT_CONFIG, VISUAL_AUDIT_SDK_DRIVER,
VISUAL_AUDIT_VISION_MODEL) are honored only with the explicit
RALPH_VISUAL_AUDIT_TESTING=1 marker; production paths must resolve to tracked
regular non-symlink files under the repository with expected ownership/mode.

Vision model outage, malformed output, image tamper, protocol drift, replay
of an older report, stale/replayed invocation artifacts, and shared-session
races all fail closed.

Usage:
  visual-audit-review.py run
  visual-audit-review.py calibrate
  visual-audit-review.py verify-json --finding FILE
  visual-audit-review.py report-check --report FILE [--current-commit HASH]
"""

from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import subprocess
import sys
import tomllib

SCHEMA = "ralph-visual-audit-review/v1"
REPORT_SCHEMA = "ralph-visual-audit-report/v1"
PROVENANCE_SCHEMA = "ralph-visual-audit-provenance/v1"
RECEIPT_SCHEMA = "ralph-visual-audit-invocation/v1"
CALIBRATION_SET_SCHEMA = "ralph-visual-audit-calibration/v1"
CALIBRATION_RECEIPT_SCHEMA = "ralph-visual-audit-calibration-receipt/v1"
PROBE_RECEIPT_SCHEMA = "ralph-visual-audit-probe-receipt/v1"
ROLES = ("diagram", "layout", "legibility", "state", "consistency", "adversarial")
STATE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
SHA1 = re.compile(r"^[0-9a-f]{40}$")
NONCE = re.compile(r"^[0-9a-f]{32}$")
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
ENVIRONMENT_REL = ".factory/environment.toml"
# Durable receipt defaults (config keys may override in tests).
CALIBRATION_RECEIPT_DEFAULT = ".factory-state/visual-audit/calibration-receipt.json"
PROBE_RECEIPT_DEFAULT = ".factory-state/visual-audit/probe-receipt.json"
PROBE_FINDING_DEFAULT = ".factory-state/visual-audit/probe-finding.json"
# Explicit canonical absent marker: used only when the boilerplate contract
# allows a repo without a committed .factory/environment.toml. It is a fixed
# literal, never arbitrary caller text.
ENVIRONMENT_ABSENT = "absent"
TESTING_MARKER = "RALPH_VISUAL_AUDIT_TESTING"
# Tracked inputs the review consumes from the repository. State paths under
# the ignored .factory-state/visual-audit/ are deliberately excluded: they are
# mutable capture/review outputs bound by hash, not inputs.
INPUT_KEYS = ("prompt_template", "review_schema", "inventory", "calibration_set",
              "capture_driver", "sdk_driver")
FRAMEWORK_SCRIPT_RELS = (
    "scripts/visual-audit-review.py", "scripts/visual-audit-provenance.py",
    "scripts/visual-audit-capture.py", "scripts/visual-audit-capture.sh",
    "scripts/visual-audit-lease.py", "scripts/visual-audit-probe.sh",
    "scripts/visual-audit-review-sdk.mjs", "scripts/check-visual-audit.py",
    "scripts/visual-capture-driver.sh",
)


RECEIPT_KEYS = {
    "schema", "request_nonce", "state_id", "role", "image_sha256",
    "prompt_sha256", "schema_sha256", "model", "raw_response_sha256",
    "finding_sha256", "started_at_ms", "finished_at_ms", "elapsed_ms",
}
# Exact allowed keys per calibration control: id/expectation are required,
# sha256 is required (exact lowercase 64-hex), note is the only optional key.
# Any unexpected key fails the declared set.
CALIBRATION_CONTROL_KEYS = ("id", "expectation", "sha256", "note")


def die(message: str) -> "NoReturn":
    raise SystemExit(f"visual-audit-review: {message}")


def testing_mode() -> bool:
    return os.environ.get(TESTING_MARKER) == "1"


def git(root: Path, *argv: str, check: bool = False) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(root), *argv], capture_output=True, check=check)


def git_stdout(root: Path, *argv: str) -> str:
    result = git(root, *argv)
    if result.returncode != 0:
        die(f"git {' '.join(argv)} failed: {result.stderr.decode(errors='replace').strip()}")
    return result.stdout.decode("utf-8").strip()


def is_tracked(root: Path, path: Path) -> bool:
    try:
        rel = path.relative_to(root)
    except ValueError:
        return False
    return git(root, "ls-files", "--error-unmatch", "--", str(rel)).returncode == 0


def under_root(root: Path, path: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except (ValueError, OSError):
        return False


def resolve_path(root: Path, value: str) -> Path:
    path = Path(value)
    if not path.is_absolute():
        path = root / path
    return path


def require_regular_file(path: Path, label: str, executable: bool = False) -> None:
    """Expected mode/ownership for production execution paths."""
    if not path.is_file() or path.is_symlink():
        die(f"{label} must be a regular non-symlink file: {path}")
    info = path.stat()
    if info.st_uid != os.getuid():
        die(f"{label} has unsafe ownership: {path}")
    if info.st_mode & 0o022:
        die(f"{label} must not be group/world writable: {path}")
    if executable and not os.access(path, os.X_OK):
        die(f"{label} must be executable: {path}")


def config(path: Path) -> dict:
    with open(path, "rb") as stream:
        data = tomllib.load(stream)
    section = data.get("visual-audit", {})
    if not isinstance(section, dict):
        die("visual-audit section is missing")
    return section


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


def new_nonce() -> str:
    """Fresh cryptographically random 128-bit request nonce (32 hex chars)."""
    return secrets.token_hex(16)


def valid_nonce(value: object) -> bool:
    return isinstance(value, str) and bool(NONCE.fullmatch(value))


def require_artifact_file(path: Path, label: str) -> str | None:
    """Review artifacts (finding/receipt) must be secure regular 0600 files.

    Returns a problem description or None when the file is safe.
    """
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


def require_receipt_file(path: Path, label: str) -> str | None:
    """Durable probe/calibration receipts must be secure regular 0600 files.

    Returns a problem description or None when the file is safe.
    """
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


def atomic_write_json(path: Path, data: dict) -> None:
    """Write a durable receipt atomically: 0600 temp, fsync, rename, dir fsync.

    The receipt is written under the ignored .factory-state/visual-audit/
    mutable state directory; the parent must not be group/world accessible.
    """
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.parent.stat().st_mode & 0o077:
        die(f"receipt directory must not be group/world accessible: {path.parent}")
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(8)}")
    raw = (json.dumps(data, indent=2, sort_keys=True) + "\n").encode("utf-8")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(fd, raw)
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(temporary, path)
    path.chmod(0o600)
    dir_fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)


def current_framework_binding(root: Path) -> tuple[str, str]:
    """Current framework commit/tree binding for durable receipts."""
    return git_stdout(root, "rev-parse", "HEAD"), git_stdout(root, "rev-parse", "HEAD^{tree}")


def validate_calibration_declared_set(cal: dict) -> list[dict]:
    """Immutable declared set: >= 1 known-good and >= 2 distinct known-bad
    controls with valid ids and pass/finding expectations. Returns the control
    list (id + expectation). Any structural violation fails closed."""
    if cal.get("schema") != CALIBRATION_SET_SCHEMA:
        die("calibration set schema invalid")
    images = cal.get("images")
    if not isinstance(images, list) or not images:
        die("calibration set declares no controls")
    controls: list[dict] = []
    seen: set[str] = set()
    declared_hashes: set[str] = set()
    good = 0
    bad = 0
    for entry in images:
        if not isinstance(entry, dict):
            die("calibration control must be an object")
        cid = entry.get("id")
        expectation = entry.get("expectation")
        declared = entry.get("sha256")
        if set(entry) - set(CALIBRATION_CONTROL_KEYS):
            die(f"calibration control {cid!r} has unexpected keys: {sorted(set(entry) - set(CALIBRATION_CONTROL_KEYS))}")
        if not valid_state_id(cid):
            die(f"calibration control has an invalid id: {cid!r}")
        if cid in seen:
            die(f"calibration control id is duplicated: {cid!r}")
        seen.add(cid)
        if expectation not in ("pass", "finding"):
            die(f"calibration control {cid!r} has an invalid expectation: {expectation!r}")
        if not isinstance(declared, str) or not SHA256.fullmatch(declared):
            die(f"calibration control {cid!r} must declare an exact lowercase 64-hex sha256: {declared!r}")
        if declared in declared_hashes:
            die(f"calibration control declared sha256 is duplicated: {declared}")
        declared_hashes.add(declared)
        if expectation == "pass":
            good += 1
        else:
            bad += 1
        controls.append({"id": cid, "expectation": expectation, "sha256": declared})
    if good < 1:
        die("calibration declared set must contain at least one known-good control (expectation pass)")
    if bad < 2:
        die("calibration declared set must contain at least two distinct known-bad controls (expectation finding)")
    return controls


def prevalidate_calibration(section: dict, root: Path, cal: dict) -> list[dict]:
    """Load EVERY declared control image and verify its ACTUAL SHA-256 equals
    the declared sha256 BEFORE any reviewer/model subprocess is spawned.

    The whole set must prevalidate (declared-set structure, exact sha256 per
    control, no duplicate ids or duplicate declared/actual hashes, and no
    byte-overlap with the live captures) before classification runs begin; a
    partial or tampered set refuses calibration. Returns verified control
    bindings whose hashes are used by the receipt."""
    controls = validate_calibration_declared_set(cal)
    capture_dir = resolve_path(root, section["capture_dir"])
    cal_dir = capture_dir / "calibration"
    live = live_capture_sha256s(capture_dir)
    verified: list[dict] = []
    actual_hashes: set[str] = set()
    for control in controls:
        cid = control["id"]
        image, actual = control_image(cal_dir, cid)
        # The self-referential-overlap hazard takes precedence over the sha
        # drift diagnostic: a control byte-identical to a live capture must be
        # refused even when its declared sha256 is (also) stale.
        if actual in live:
            die(f"calibration control {cid!r} overlaps a live capture image (identical bytes); refused")
        if actual != control["sha256"]:
            die(f"calibration control {cid!r} declared sha256 does not match the control image (wrong sha256)")
        if actual in actual_hashes:
            die(f"calibration control {cid!r} is byte-identical to another control (duplicate hash)")
        actual_hashes.add(actual)
        verified.append({"id": cid, "expectation": control["expectation"],
                         "sha256": actual, "image": image})
    return verified


def control_image(cal_dir: Path, cid: str) -> tuple[Path, str]:
    """Calibration control image must be a present regular non-symlink PNG.

    Returns (path, sha256). State ids are validated before any path is built.
    """
    image = cal_dir / f"{cid}.png"
    if not image.is_file() or image.is_symlink():
        die(f"calibration control image missing or unsafe: {cid}")
    info = image.stat()
    if info.st_uid != os.getuid() or info.st_nlink != 1 or info.st_mode & 0o022:
        die(f"calibration control image has unsafe ownership, link count, or mode: {cid}")
    data = image.read_bytes()
    if data[:8] != PNG_MAGIC:
        die(f"calibration control image is not a PNG: {cid}")
    return image, sha256_bytes(data)


def live_capture_sha256s(capture_dir: Path) -> set[str]:
    """SHA-256s of every live capture image from the provenance manifest."""
    provenance = capture_dir / "provenance.json"
    if not provenance.is_file() or provenance.is_symlink():
        return set()
    try:
        manifest = json.loads(provenance.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        die(f"live capture provenance manifest is invalid: {type(exc).__name__}")
    hashes: set[str] = set()
    for entry in manifest.get("images", []):
        if isinstance(entry, dict) and SHA256.fullmatch(str(entry.get("sha256", ""))):
            hashes.add(entry["sha256"])
    return hashes


def validate_probe_receipt(section: dict, root: Path) -> None:
    """The enabled configuration requires a real probe receipt before
    calibration: a secure 0600 receipt (and its finding artifact) recording a
    passing, non-skipping probe bound to the current model/schema/commit/tree.
    Missing, stale, or tampered probe receipts fail closed."""
    path = resolve_path(root, section.get("probe_receipt", PROBE_RECEIPT_DEFAULT))
    problem = require_receipt_file(path, "probe receipt")
    if problem:
        die(f"probe receipt required before calibration: {problem}; run scripts/visual-audit-probe.sh first")
    try:
        receipt = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        die(f"probe receipt is invalid: {type(exc).__name__}")
    if receipt.get("schema") != PROBE_RECEIPT_SCHEMA:
        die("probe receipt schema mismatch (tampered)")
    commit, tree = current_framework_binding(root)
    if receipt.get("commit") != commit:
        die(f"probe receipt commit mismatch (stale): receipt {receipt.get('commit')} != HEAD {commit}")
    if receipt.get("tree") != tree:
        die("probe receipt tree mismatch (stale)")
    model = section.get("vision_model", "")
    if receipt.get("model") != model:
        die(f"probe receipt model drift: receipt {receipt.get('model')!r} != configured {model!r}")
    schema_path = resolve_path(root, section["review_schema"])
    if receipt.get("schema_sha256") != sha256_file(schema_path):
        die("probe receipt schema drift; probe invalidated")
    for key, label in (("prompt_sha256", "probe prompt hash"), ("image_sha256", "probe image hash"),
                       ("finding_sha256", "probe finding hash")):
        if not SHA256.fullmatch(str(receipt.get(key, ""))):
            die(f"probe receipt {label} invalid (tampered)")
    if receipt.get("ok") is not True:
        die("probe receipt does not record a passing probe (tampered)")
    finding_path = resolve_path(root, section.get("probe_finding", PROBE_FINDING_DEFAULT))
    problem = require_artifact_file(finding_path, "probe finding")
    if problem:
        die(f"probe finding artifact missing or unsafe: {problem}")
    if sha256_file(finding_path) != receipt["finding_sha256"]:
        die("probe receipt finding digest mismatch (tampered)")
    try:
        finding = json.loads(finding_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        die(f"probe finding is invalid JSON: {type(exc).__name__}")
    if finding.get("schema") != SCHEMA or finding.get("state_id") != "probe" \
            or finding.get("role") != "probe" or finding.get("verdict") != "pass":
        die("probe finding does not record a passing probe (tampered)")
    if finding.get("image_sha256") != receipt.get("image_sha256") \
            or finding.get("model") != receipt.get("model") \
            or finding.get("prompt_sha256") != receipt.get("prompt_sha256") \
            or finding.get("schema_sha256") != receipt.get("schema_sha256"):
        die("probe finding bindings do not match the probe receipt (tampered)")


def validate_calibration_receipt(section: dict, root: Path) -> dict:
    """Live `run` requires a valid non-tampered calibration receipt matching
    the current model/prompt/schema/calibration/commit/tree. Missing, stale, or
    tampered receipts fail closed BEFORE any reviewer/model subprocess. Every
    control's recorded image hash/result is re-derived from the current control
    images and the current declared set, so drift or tamper is detected."""
    path = resolve_path(root, section.get("calibration_receipt", CALIBRATION_RECEIPT_DEFAULT))
    problem = require_receipt_file(path, "calibration receipt")
    if problem:
        die(f"calibration receipt required: {problem}; run visual-audit-review.py calibrate first")
    try:
        receipt = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        die(f"calibration receipt is invalid: {type(exc).__name__}")
    if receipt.get("schema") != CALIBRATION_RECEIPT_SCHEMA:
        die("calibration receipt schema mismatch (tampered)")
    commit, tree = current_framework_binding(root)
    if receipt.get("commit") != commit:
        die(f"calibration receipt commit stale (receipt {receipt.get('commit')} != HEAD {commit})")
    if receipt.get("tree") != tree:
        die("calibration receipt tree mismatch (stale)")
    model = section.get("vision_model", "")
    if receipt.get("model") != model:
        die(f"calibration receipt model drift: receipt {receipt.get('model')!r} != configured {model!r}")
    prompt_path = resolve_path(root, section["prompt_template"])
    schema_path = resolve_path(root, section["review_schema"])
    if receipt.get("prompt_sha256") != sha256_file(prompt_path):
        die("calibration receipt prompt drift; receipt invalidated")
    if receipt.get("schema_sha256") != sha256_file(schema_path):
        die("calibration receipt schema drift; receipt invalidated")
    cal_path = resolve_path(root, section["calibration_set"])
    if receipt.get("calibration_sha256") != sha256_file(cal_path):
        die("calibration drift: the declared calibration set changed since the receipt (receipt invalidated)")
    controls = validate_calibration_declared_set(json.loads(cal_path.read_text(encoding="utf-8")))
    receipt_controls = receipt.get("controls")
    if not isinstance(receipt_controls, list) or len(receipt_controls) != len(controls):
        die("calibration receipt controls do not match the declared set (tampered)")
    by_id = {c.get("id"): c for c in receipt_controls if isinstance(c, dict)}
    if set(by_id) != {c["id"] for c in controls}:
        die("calibration receipt control ids do not match the declared set (tampered)")
    cal_dir = resolve_path(root, section["capture_dir"]) / "calibration"
    for control in controls:
        cid = control["id"]
        entry = by_id[cid]
        if entry.get("expectation") != control["expectation"]:
            die(f"calibration receipt expectation mismatch for {cid} (tampered)")
        if entry.get("ok") is not True:
            die(f"calibration receipt records a failed control: {cid} (tampered)")
        verdicts = entry.get("verdicts")
        if not isinstance(verdicts, list) or not verdicts:
            die(f"calibration receipt control {cid} has no verdicts (tampered)")
        if control["expectation"] == "pass" and not all(v == "pass" for v in verdicts):
            die(f"calibration receipt known-good control {cid} did not pass (false-positive)")
        if control["expectation"] == "finding" and any(v == "pass" for v in verdicts):
            die(f"calibration receipt known-bad control {cid} passed (false-negative)")
        if not SHA256.fullmatch(str(entry.get("image_sha256", ""))):
            die(f"calibration receipt image hash invalid for {cid} (tampered)")
        _, current_sha = control_image(cal_dir, cid)
        if current_sha != entry["image_sha256"]:
            die(f"calibration control image changed since calibration for {cid} (tampered)")
    return receipt


def valid_state_id(value: object) -> bool:
    return isinstance(value, str) and bool(STATE_NAME.fullmatch(value))


def check_receipt(receipt: dict, state_id: str, role: str, expected_sha: str, model: str,
                  prompt_hash: str, schema_hash: str, nonce: str,
                  finding_path: Path) -> str | None:
    """Strict per-task receipt validation (exact keys, values, digests).

    Returns a problem description or None. A missing/tampered/replayed receipt
    must fail closed, so every field is compared to the task and the finding
    digest is recomputed from the finding file bytes.
    """
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
        problems.append(f"receipt schema mismatch: {receipt.get('schema')!r}")
    for key, expected in (("request_nonce", nonce), ("state_id", state_id),
                          ("role", role), ("image_sha256", expected_sha),
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


def valid_environment_binding(value: object) -> bool:
    return value == ENVIRONMENT_ABSENT or (isinstance(value, str) and bool(SHA256.fullmatch(value)))


def environment_binding(root: Path) -> str:
    """Fixed lowercase SHA-256 of the committed .factory/environment.toml bytes.

    Reads the committed blob (HEAD:<file>), never the worktree, so a dirty
    worktree cannot change the binding; caller text is never accepted.
    """
    path = root / ENVIRONMENT_REL
    if path.is_symlink():
        die("environment.toml must not be a symlink")
    if not is_tracked(root, path):
        if path.exists():
            die("environment.toml exists but is not committed; it must be tracked")
        return ENVIRONMENT_ABSENT
    result = git(root, "show", f"HEAD:{ENVIRONMENT_REL}")
    if result.returncode != 0:
        die("cannot read the committed environment.toml blob")
    return sha256_bytes(result.stdout)


def gate_overrides() -> None:
    """Execution-path overrides require the explicit test marker."""
    if testing_mode():
        return
    for var in ("VISUAL_AUDIT_CONFIG", "VISUAL_AUDIT_SDK_DRIVER", "VISUAL_AUDIT_VISION_MODEL"):
        if os.environ.get(var):
            die(f"{var} is a test-only execution override; set {TESTING_MARKER}=1 to use it")


def production_config_policy(root: Path, config_path: Path) -> None:
    """Production config must be a tracked regular file under the repo."""
    if testing_mode():
        return
    if not under_root(root, config_path):
        die("production config must resolve to a file under the repository")
    if not is_tracked(root, config_path):
        die("production config must be a tracked file")
    require_regular_file(config_path, "config")


def production_input_policy(section: dict, root: Path) -> None:
    """Production execution paths must be tracked repo files, not overrides."""
    if testing_mode():
        return
    for key in INPUT_KEYS:
        value = section.get(key)
        if not value:
            die(f"{key} must be configured")
        path = resolve_path(root, str(value))
        if not under_root(root, path) or not is_tracked(root, path):
            die(f"{key} must resolve to a tracked file under the repository in production")
        require_regular_file(path, key)


def framework_input_paths(section: dict, root: Path) -> list[Path]:
    """Relevant tracked framework/capture inputs for the clean check.

    Only paths under the repository are asserted; test-only overrides that
    resolve outside the repo (mock drivers, external configs) are skipped.
    """
    inputs: list[Path] = []
    default_config = root / ".factory/visual-audit.toml"
    if default_config.exists():
        inputs.append(default_config)
    for key in INPUT_KEYS:
        value = section.get(key)
        if not value:
            continue
        path = resolve_path(root, str(value))
        if under_root(root, path):
            inputs.append(path)
    inputs.append(root / ENVIRONMENT_REL)
    for rel in FRAMEWORK_SCRIPT_RELS:
        candidate = root / rel
        if candidate.exists():
            inputs.append(candidate)
    seen: set[str] = set()
    unique: list[Path] = []
    for path in inputs:
        key = str(path)
        if key not in seen:
            seen.add(key)
            unique.append(path)
    return unique


def require_tracked_clean(root: Path, inputs: list[Path]) -> None:
    """Tracked worktree/index must be clean for every relevant input.

    In testing mode, untracked paths that resolve inside the repository are
    test-only overrides (mock drivers, external configs) and are skipped;
    production already rejects untracked execution paths earlier.
    """
    rels: list[str] = []
    for path in inputs:
        try:
            rel = path.relative_to(root)
        except ValueError:
            continue
        if not is_tracked(root, path):
            if not testing_mode():
                die(f"framework/capture input must be a tracked file: {rel}")
            continue
        rels.append(str(rel))
    if not rels:
        return
    result = git(root, "status", "--porcelain", "--", *rels)
    if result.returncode != 0:
        die("git status failed while checking framework input cleanliness")
    if result.stdout.strip():
        die("tracked framework/capture inputs are dirty; refusing to review stale state")


def verify_provenance(manifest: dict, capture_dir: Path) -> None:
    """Image ownership/link/symlink/hash and manifest structure (fail closed)."""
    if manifest.get("schema") != PROVENANCE_SCHEMA:
        die("provenance manifest schema invalid")
    commit = manifest.get("commit")
    tree = manifest.get("tree")
    if not isinstance(commit, str) or not SHA1.fullmatch(commit):
        die("provenance manifest commit invalid")
    if not isinstance(tree, str) or not SHA1.fullmatch(tree):
        die("provenance manifest tree invalid")
    environment = manifest.get("environment_blob")
    if not valid_environment_binding(environment):
        die("provenance manifest environment binding invalid (must be a sha256 or the canonical absent marker)")
    images = manifest.get("images")
    if not isinstance(images, list) or not images:
        die("provenance manifest images array is invalid")
    seen: set[str] = set()
    for entry in images:
        if not isinstance(entry, dict) or set(entry) != {"state_id", "file", "sha256", "size"}:
            die("provenance manifest image entry schema is invalid")
        state_id = entry["state_id"]
        file_name = entry["file"]
        digest = entry["sha256"]
        size = entry["size"]
        if not valid_state_id(state_id):
            die(f"provenance manifest image state_id is invalid: {state_id!r}")
        if not isinstance(file_name, str) or "/" in file_name or ".." in file_name or file_name in seen:
            die(f"provenance manifest image file is invalid: {file_name!r}")
        seen.add(file_name)
        if not isinstance(digest, str) or not SHA256.fullmatch(digest):
            die(f"provenance manifest image sha256 is invalid: {file_name}")
        if not isinstance(size, int) or isinstance(size, bool) or size <= 0:
            die(f"provenance manifest image size is invalid: {file_name}")
        image_path = capture_dir / file_name
        if not image_path.is_file() or image_path.is_symlink():
            die(f"capture image missing or unsafe (link/symlink): {file_name}")
        info = image_path.stat()
        if info.st_uid != os.getuid() or info.st_nlink != 1 or info.st_mode & 0o022:
            die(f"capture image has unsafe ownership, link count, or mode: {file_name}")
        if info.st_size != size:
            die(f"capture image size mismatch: {file_name}")
        if sha256_file(image_path) != digest:
            die(f"capture image hash mismatch: {file_name}")
    print(f"visual-audit-review: provenance verified ({len(images)} images at commit {commit[:12]})")


def require_exact_commit_state(root: Path, manifest: dict) -> None:
    current = git_stdout(root, "rev-parse", "HEAD")
    if manifest["commit"] != current:
        die(f"provenance commit {manifest['commit']} != current HEAD {current} (stale capture)")
    tree = git_stdout(root, "rev-parse", "HEAD^{tree}")
    if manifest["tree"] != tree:
        die(f"provenance tree {manifest['tree']} != HEAD tree {tree} (tree mismatch)")


def load_review_schema(path: Path) -> dict:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if raw.get("$id") != SCHEMA:
        die("review schema $id does not match the expected schema")
    return raw


def validate_finding(finding: dict, schema: dict) -> None:
    """Strict structural validation against the committed schema."""
    if not isinstance(finding, dict):
        raise SystemExit("visual-audit-review: finding is not an object")
    required = {"schema", "state_id", "image_sha256", "role", "model",
                "prompt_sha256", "schema_sha256", "request_nonce",
                "verdict", "observations"}
    if not required.issubset(set(finding)):
        raise SystemExit(f"visual-audit-review: finding missing keys {sorted(required - set(finding))}")
    if set(finding) - required - {"untrusted_image_text_note"}:
        raise SystemExit(f"visual-audit-review: finding has unexpected keys {sorted(set(finding) - required)}")
    if finding["schema"] != SCHEMA:
        raise SystemExit("visual-audit-review: finding schema mismatch")
    if finding["role"] not in ROLES and finding["role"] != "probe":
        raise SystemExit(f"visual-audit-review: invalid role {finding['role']}")
    if finding["verdict"] not in ("pass", "finding", "error"):
        raise SystemExit(f"visual-audit-review: invalid verdict {finding['verdict']}")
    for key in ("image_sha256", "prompt_sha256", "schema_sha256"):
        if not SHA256.fullmatch(finding.get(key, "")):
            raise SystemExit(f"visual-audit-review: {key} invalid")
    if not valid_nonce(finding.get("request_nonce")):
        raise SystemExit("visual-audit-review: request_nonce invalid")
    observations = finding["observations"]
    if not isinstance(observations, list):
        raise SystemExit("visual-audit-review: observations is not an array")
    for obs in observations:
        if not isinstance(obs, dict) or set(obs) - {"code", "severity", "description", "region", "expected", "observed"}:
            raise SystemExit("visual-audit-review: observation schema invalid")
        if not re.fullmatch(r"^[A-Z][A-Z0-9_]{2,63}$", obs.get("code", "")):
            raise SystemExit("visual-audit-review: observation code invalid")
        if obs.get("severity") not in ("info", "low", "medium", "high", "critical"):
            raise SystemExit("visual-audit-review: observation severity invalid")
        if not isinstance(obs.get("description", ""), str) or not obs["description"]:
            raise SystemExit("visual-audit-review: observation description invalid")


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
        problems.append(f"image_sha256 {str(finding.get('image_sha256'))[:16]}... != task {expected_sha[:16]}...")
    if finding.get("role") != role:
        problems.append(f"role {finding.get('role')!r} != task {role!r}")
    if finding.get("model") != model:
        problems.append(f"model {finding.get('model')!r} != task {model!r}")
    if finding.get("prompt_sha256") != prompt_hash:
        problems.append(f"prompt_sha256 mismatch for task {state_id}/{role}")
    if finding.get("schema_sha256") != schema_hash:
        problems.append(f"schema_sha256 mismatch for task {state_id}/{role}")
    return "; ".join(problems) if problems else None


def build_sdk_argv(section: dict, driver: Path) -> list[str]:
    """SDK driver argv; tests may substitute VISUAL_AUDIT_SDK_DRIVER only with
    the RALPH_VISUAL_AUDIT_TESTING=1 marker (gated in main())."""
    injected = os.environ.get("VISUAL_AUDIT_SDK_DRIVER", "")
    if injected:
        base = injected.split()
    else:
        if not driver.is_file() or driver.is_symlink():
            die("SDK driver missing")
        node = shutil.which("node")
        if not node:
            die("node is unavailable; visual-audit requires the pi SDK driver")
        base = [node, str(driver)]
    return base


def run_one(section: dict, driver: Path, image: Path, expected_sha: str, state_id: str,
            role: str, prompt_path: Path, prompt_hash: str, schema_hash: str, nonce: str,
            out_dir: Path, timeout: int) -> tuple[int, str, dict | None, dict | None]:
    argv = build_sdk_argv(section, driver) + [
        "--image", str(image),
        "--expected-sha256", expected_sha,
        "--state-id", state_id,
        "--role", role,
        "--prompt-file", str(prompt_path),
        "--prompt-sha256", prompt_hash,
        "--schema-sha256", schema_hash,
        "--model", section.get("vision_model", ""),
        "--request-nonce", nonce,
        "--out-dir", str(out_dir),
    ]
    try:
        result = subprocess.run(argv, capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return 124, "reviewer timed out", None, None
    except OSError as exc:
        return 127, f"reviewer unavailable: {type(exc).__name__}: {exc}", None, None
    output = result.stdout.decode("utf-8", errors="replace") + result.stderr.decode("utf-8", errors="replace")
    if result.returncode != 0:
        return result.returncode, output, None, None
    finding_path = out_dir / f"finding-{state_id}-{role}.json"
    receipt_path = out_dir / f"receipt-{state_id}-{role}.json"
    for path, label in ((finding_path, "finding"), (receipt_path, "receipt")):
        problem = require_artifact_file(path, label)
        if problem:
            return 2, f"{problem} for {state_id}/{role}", None, None
    try:
        finding = json.loads(finding_path.read_text(encoding="utf-8"))
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        return 2, f"finding/receipt file is invalid JSON: {type(exc).__name__}", None, None
    model = section.get("vision_model", "")
    problem = check_receipt(receipt, state_id, role, expected_sha, model,
                            prompt_hash, schema_hash, nonce, finding_path)
    if problem:
        return 2, f"receipt mismatch for {state_id}/{role}: {problem}", None, None
    problem = check_finding_binding(finding, state_id, expected_sha, role,
                                    model, prompt_hash, schema_hash, nonce)
    if problem:
        return 2, f"finding binding mismatch for {state_id}/{role}: {problem}", None, None
    return 0, output, finding, receipt


def aggregate(findings: list[dict]) -> dict:
    by_state: dict[str, dict] = {}
    for finding in findings:
        entry = by_state.setdefault(finding["state_id"], {"findings": [], "verdicts": []})
        entry["findings"].append(finding)
        entry["verdicts"].append(finding["verdict"])
    aggregate = {"states": []}
    for state_id, entry in sorted(by_state.items()):
        verdicts = entry["verdicts"]
        critical = [f for f in entry["findings"] if f["verdict"] == "error"]
        flagged = [f for f in entry["findings"] if f["verdict"] == "finding"]
        passed = [f for f in entry["findings"] if f["verdict"] == "pass"]
        overall = "error" if critical else ("finding" if flagged else ("pass" if passed else "error"))
        aggregate["states"].append({
            "state_id": state_id,
            "overall": overall,
            "reviewers": len(verdicts),
            "passed": len(passed),
            "findings": len(flagged),
            "errors": len(critical),
            "observation_count": sum(len(f["observations"]) for f in entry["findings"]),
        })
    return aggregate


def cmd_calibrate(section: dict, root: Path, driver: Path) -> int:
    if section.get("enabled") is not True:
        print("visual-audit-review: visual audit disabled; skipping calibration")
        return 0
    # The enabled configuration requires a real, non-skipping probe receipt
    # BEFORE calibration is trusted (secure 0600 receipt + finding artifact).
    validate_probe_receipt(section, root)
    cal_path = resolve_path(root, str(section["calibration_set"]))
    cal = json.loads(cal_path.read_text(encoding="utf-8"))
    # The ENTIRE declared set must prevalidate (exact sha256 per control, no
    # duplicate ids or duplicate hashes, no live-capture overlap) BEFORE any
    # reviewer/model subprocess is spawned; a partial or tampered set refuses
    # classification and never writes a receipt.
    verified = prevalidate_calibration(section, root, cal)
    prompt_path = resolve_path(root, section["prompt_template"])
    schema_path = resolve_path(root, section["review_schema"])
    prompt_hash = sha256_file(prompt_path)
    schema_hash = sha256_file(schema_path)
    timeout = int(section.get("review_timeout_seconds", 300))
    capture_dir = resolve_path(root, section["capture_dir"])
    out_dir = capture_dir / "calibration-out"
    out_dir.mkdir(parents=True, exist_ok=True)
    results = []
    failed = 0
    for control in verified:
        cid = control["id"]
        expectation = control["expectation"]
        image = control["image"]
        image_sha = control["sha256"]
        runs = 3 if expectation == "pass" else 1
        verdicts = []
        for run in range(runs):
            rc, output, finding, receipt = run_one(
                section, driver, image, image_sha, cid, "adversarial",
                prompt_path, prompt_hash, schema_hash, new_nonce(), out_dir, timeout)
            if rc != 0:
                print(f"visual-audit-review: calibration {cid} run {run + 1} failed (rc={rc})", file=sys.stderr)
                verdicts.append("error")
                continue
            validate_finding(finding, load_review_schema(schema_path))
            verdicts.append(finding["verdict"])
        if expectation == "pass":
            ok = len(verdicts) == 3 and all(v == "pass" for v in verdicts)
        else:
            ok = len(verdicts) == 1 and verdicts[0] != "pass"
        if not ok:
            label = "false-positive known-good" if expectation == "pass" else "false-negative known-bad"
            print(f"visual-audit-review: calibration FAILED: {cid} expected {expectation}, got {verdicts} ({label})", file=sys.stderr)
            failed += 1
        else:
            print(f"visual-audit-review: calibration OK: {cid} -> {verdicts}")
        results.append({
            "id": cid,
            "expectation": expectation,
            "image_sha256": image_sha,
            "ok": ok,
            "verdicts": verdicts,
        })
    if failed:
        raise SystemExit(f"visual-audit-review: calibration failed ({failed} control(s) misclassified); no receipt written")
    # Only a fully passing calibration writes the durable 0600 receipt,
    # atomically, bound to the model, prompt/schema/calibration hashes, every
    # control image hash/result, and the current framework commit/tree.
    commit, tree = current_framework_binding(root)
    receipt = {
        "schema": CALIBRATION_RECEIPT_SCHEMA,
        "commit": commit,
        "tree": tree,
        "model": section.get("vision_model", ""),
        "prompt_sha256": prompt_hash,
        "schema_sha256": schema_hash,
        "calibration_sha256": sha256_file(cal_path),
        "controls": results,
    }
    receipt_path = resolve_path(root, section.get("calibration_receipt", CALIBRATION_RECEIPT_DEFAULT))
    atomic_write_json(receipt_path, receipt)
    print(f"visual-audit-review: calibration passed; receipt written to {receipt_path}")
    return 0


def cmd_run(section: dict, root: Path, driver: Path) -> int:
    if section.get("enabled") is not True:
        print("visual-audit-review: visual audit disabled; nothing to review")
        return 0
    # ---- fail-closed receipt gate BEFORE any reviewer/model subprocess ------
    # Live `run` requires a valid non-tampered calibration receipt matching the
    # current model/prompt/schema/calibration/commit/tree; a missing, stale, or
    # tampered receipt fails before the model is ever contacted.
    calibration_receipt = validate_calibration_receipt(section, root)
    capture_dir = resolve_path(root, section["capture_dir"])
    provenance = capture_dir / "provenance.json"
    if not provenance.is_file() or provenance.is_symlink():
        die("no provenance manifest; run visual-audit-capture.sh first")
    info = provenance.stat()
    if info.st_uid != os.getuid() or info.st_nlink != 1 or info.st_mode & 0o022:
        die("provenance manifest has unsafe ownership, link count, or mode")
    try:
        manifest = json.loads(provenance.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        die(f"provenance manifest is invalid: {type(exc).__name__}")

    # ---- provenance verification BEFORE any reviewer/model subprocess -------
    verify_provenance(manifest, capture_dir)
    require_exact_commit_state(root, manifest)
    binding = environment_binding(root)
    if manifest["environment_blob"] != binding:
        die(f"environment binding mismatch: manifest {manifest['environment_blob']!r} != committed {binding!r}")
    require_tracked_clean(root, framework_input_paths(section, root))
    # A calibration control byte-identical to a live capture would let a review
    # "pass" by reusing the calibrated control; re-checked here too.
    calibration_hashes = {c.get("image_sha256") for c in calibration_receipt.get("controls", [])}
    if any(e.get("sha256") in calibration_hashes for e in manifest["images"]):
        die("calibration control overlaps a live capture image (identical bytes)")
    # -------------------------------------------------------------------------

    prompt_path = resolve_path(root, section["prompt_template"])
    schema_path = resolve_path(root, section["review_schema"])
    prompt_hash = sha256_file(prompt_path)
    schema_hash = sha256_file(schema_path)
    timeout = int(section.get("review_timeout_seconds", 300))
    parallelism = int(section.get("review_parallelism", 4))

    inventory = json.loads((root / section["inventory"]).read_text(encoding="utf-8"))
    if inventory.get("schema") != "ralph-visual-audit-inventory/v1":
        die("inventory schema invalid")
    states = {s["id"]: s for s in inventory["states"]}
    # State ids are validated before any output path is constructed.
    for state in inventory["states"]:
        if not valid_state_id(state["id"]):
            die(f"inventory state id invalid: {state['id']!r}")
    images = {e["file"]: e for e in manifest["images"]}
    review_dir = resolve_path(root, section["review_dir"])
    review_dir.mkdir(parents=True, exist_ok=True)
    review_dir.chmod(0o700)
    if review_dir.stat().st_mode & 0o077:
        die("review dir must not be group/world accessible")

    tasks = []
    for file_name, entry in sorted(images.items()):
        state = states.get(entry["state_id"])
        if state is None:
            die(f"capture image {file_name} has no inventory state {entry['state_id']}")
        image_path = capture_dir / file_name
        for role in ROLES:
            # Fresh cryptographically random per-task request nonce (>=128 bits).
            tasks.append((state, image_path, entry["sha256"], role, new_nonce()))

    schema = load_review_schema(schema_path)
    records: list[tuple[str, str, dict, dict]] = []  # (state_id, role, finding, receipt)
    with concurrent.futures.ThreadPoolExecutor(max_workers=parallelism) as pool:
        futures = {
            pool.submit(run_one, section, driver, image_path, expected_sha,
                        state["id"], role, prompt_path, prompt_hash, schema_hash,
                        nonce, review_dir, timeout): (state["id"], role)
            for state, image_path, expected_sha, role, nonce in tasks
        }
        for future in concurrent.futures.as_completed(futures):
            state_id, role = futures[future]
            rc, output, finding, receipt = future.result()
            if rc != 0:
                # Fail closed on reviewer outage/tooling/tamper failure: an
                # error finding alone could be mistaken for a completed review.
                die(f"reviewer outage for {state_id}/{role} (rc={rc}): {output[:400]}")
            validate_finding(finding, schema)
            records.append((state_id, role, finding, receipt))

    # Deterministic ordering: findings and task receipts are sorted by
    # (state_id, role) so the report is reproducible across runs.
    records.sort(key=lambda rec: (rec[0], rec[1]))
    findings = [rec[2] for rec in records]
    task_receipts = []
    for state_id, role, finding, receipt in records:
        finding_sha = sha256_file(review_dir / f"finding-{state_id}-{role}.json")
        receipt_sha = sha256_file(review_dir / f"receipt-{state_id}-{role}.json")
        task_receipts.append({
            "state_id": state_id,
            "role": role,
            "request_nonce": receipt["request_nonce"],
            "image_sha256": receipt["image_sha256"],
            "finding_file": f"finding-{state_id}-{role}.json",
            "finding_sha256": finding_sha,
            "receipt_file": f"receipt-{state_id}-{role}.json",
            "receipt_sha256": receipt_sha,
        })

    report = {
        "schema": REPORT_SCHEMA,
        "commit": manifest["commit"],
        "tree": manifest["tree"],
        "environment_sha256": binding,
        "prompt_sha256": prompt_hash,
        "schema_sha256": schema_hash,
        "model": section.get("vision_model", ""),
        # The report is bound to the exact calibration receipt bytes that were
        # current when the live review ran, so a replaced/tampered receipt
        # invalidates the report at check time.
        "calibration_receipt_sha256": sha256_file(
            resolve_path(root, section.get("calibration_receipt", CALIBRATION_RECEIPT_DEFAULT))),
        "images": manifest["images"],
        "findings": findings,
        "task_receipts": task_receipts,
        "aggregate": aggregate(findings),
    }
    output = review_dir / "report.json"
    output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    output.chmod(0o600)
    print(json.dumps(report["aggregate"], indent=2, sort_keys=True))
    print(f"visual-audit-review: {len(findings)} findings written to {output}")
    return 0


def verify_report_artifacts(section: dict, root: Path, report: dict) -> None:
    """Revalidate receipt/finding files and digests against the report.

    Does not trust the report's digest fields: each receipt/finding file is
    re-read, its safety (regular 0600, ownership, link count) and SHA-256 are
    recomputed, the receipt's strict key/value set and task binding are
    rechecked, and every finding/image is bound to the current provenance
    image set. Missing, tampered, or replayed artifacts fail closed.
    """
    review_dir = resolve_path(root, section["review_dir"])
    task_receipts = report.get("task_receipts")
    if not isinstance(task_receipts, list) or not task_receipts:
        die("report has no task receipt entries")
    images = {img["sha256"]: img for img in report.get("images", []) if isinstance(img, dict)}
    model = report.get("model", "")
    prompt_hash = report.get("prompt_sha256")
    schema_hash = report.get("schema_sha256")
    if not SHA256.fullmatch(str(prompt_hash)) or not SHA256.fullmatch(str(schema_hash)):
        die("report prompt/schema hashes invalid")
    finding_keys: set[tuple[str, str]] = set()
    for finding in report.get("findings", []):
        if isinstance(finding, dict):
            finding_keys.add((finding.get("state_id"), finding.get("role")))
    for entry in task_receipts:
        if not isinstance(entry, dict):
            die("report task receipt entry invalid")
        state_id = entry.get("state_id")
        role = entry.get("role")
        nonce = entry.get("request_nonce")
        finding_file = entry.get("finding_file")
        receipt_file = entry.get("receipt_file")
        image_sha = entry.get("image_sha256")
        if not valid_state_id(state_id) or role not in ROLES or not valid_nonce(nonce):
            die(f"report task receipt binding invalid for {state_id!r}/{role!r}")
        if finding_file != f"finding-{state_id}-{role}.json" \
                or receipt_file != f"receipt-{state_id}-{role}.json":
            die(f"report task receipt filenames invalid for {state_id}/{role}")
        if not isinstance(image_sha, str) or not SHA256.fullmatch(image_sha):
            die(f"report task receipt image hash invalid for {state_id}/{role}")
        if image_sha not in images:
            die(f"report task receipt image {image_sha[:12]} not in the provenance manifest (tamper)")
        if (state_id, role) not in finding_keys:
            die(f"report task receipt {state_id}/{role} has no matching finding")
        finding_path = review_dir / finding_file
        receipt_path = review_dir / receipt_file
        problem = require_artifact_file(finding_path, "finding")
        problem = problem or require_artifact_file(receipt_path, "receipt")
        if problem:
            die(problem)
        if sha256_file(finding_path) != entry.get("finding_sha256"):
            die(f"report finding digest mismatch for {state_id}/{role} (tampered or missing file)")
        if sha256_file(receipt_path) != entry.get("receipt_sha256"):
            die(f"report receipt digest mismatch for {state_id}/{role} (tampered or missing file)")
        try:
            receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
            finding = json.loads(finding_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            die(f"artifact file is invalid JSON for {state_id}/{role}: {type(exc).__name__}")
        problem = check_receipt(receipt, state_id, role, image_sha, model,
                                prompt_hash, schema_hash, nonce, finding_path)
        if problem:
            die(f"receipt revalidation failed for {state_id}/{role}: {problem}")
        problem = check_finding_binding(finding, state_id, image_sha, role,
                                        model, prompt_hash, schema_hash, nonce)
        if problem:
            die(f"finding revalidation failed for {state_id}/{role}: {problem}")


def cmd_report_check(section: dict, root: Path, report_path: Path, current_commit: str) -> int:
    """Replay/drift/tamper gate on an existing report."""
    if not report_path.is_file() or report_path.is_symlink():
        die("report is missing or unsafe")
    report = json.loads(report_path.read_text(encoding="utf-8"))
    if report.get("schema") != REPORT_SCHEMA:
        die("report schema mismatch")
    if current_commit and report.get("commit") != current_commit:
        die(f"report replay/commit mismatch: report {report.get('commit')} vs current {current_commit}")
    environment = report.get("environment_sha256")
    if not valid_environment_binding(environment):
        die("report environment binding is missing or invalid")
    if environment != environment_binding(root):
        die("environment drift: report environment binding differs from the committed environment.toml")
    prompt_path = resolve_path(root, section["prompt_template"])
    schema_path = resolve_path(root, section["review_schema"])
    if sha256_file(prompt_path) != report.get("prompt_sha256"):
        die("prompt drift: report prompt hash differs from the committed template")
    if sha256_file(schema_path) != report.get("schema_sha256"):
        die("schema drift: report schema hash differs from the committed schema")
    # The report is bound to the exact calibration receipt bytes that were
    # current when it was produced; the receipt must still be valid for the
    # current model/prompt/schema/calibration/commit/tree and byte-identical
    # to the bound one. Missing/stale/tampered receipts invalidate the report.
    validate_calibration_receipt(section, root)
    receipt_path = resolve_path(root, section.get("calibration_receipt", CALIBRATION_RECEIPT_DEFAULT))
    if report.get("calibration_receipt_sha256") != sha256_file(receipt_path):
        die("report calibration receipt binding mismatch (receipt replaced or tampered)")
    findings = report.get("findings")
    if not isinstance(findings, list) or not findings:
        die("report has no findings")
    for finding in findings:
        validate_finding(finding, load_review_schema(schema_path))
    # Do not trust the report's embedded digests: revalidate the actual
    # receipt/finding files against the current provenance/report.
    verify_report_artifacts(section, root, report)
    return 0


def cmd_verify_json(section: dict, root: Path, finding_path: Path) -> int:
    schema_path = resolve_path(root, section["review_schema"])
    finding = json.loads(finding_path.read_text(encoding="utf-8"))
    validate_finding(finding, load_review_schema(schema_path))
    print("visual-audit-review: finding JSON valid")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Machine visual-audit review")
    parser.add_argument("--config", default=".factory/visual-audit.toml")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("run")
    sub.add_parser("calibrate")
    v = sub.add_parser("verify-json")
    v.add_argument("--finding", required=True)
    r = sub.add_parser("report-check")
    r.add_argument("--report", required=True)
    r.add_argument("--current-commit", default="")
    args = parser.parse_args()

    gate_overrides()
    root = Path.cwd()
    config_path = resolve_path(root, args.config)
    if not config_path.is_file() or config_path.is_symlink():
        die("config file is missing or unsafe")
    section = config(config_path)
    if not testing_mode():
        production_config_policy(root, config_path)
        production_input_policy(section, root)

    driver = resolve_path(root, section.get("sdk_driver", "scripts/visual-audit-review-sdk.mjs"))
    if args.command == "run":
        return cmd_run(section, root, driver)
    if args.command == "calibrate":
        return cmd_calibrate(section, root, driver)
    if args.command == "verify-json":
        return cmd_verify_json(section, root, Path(args.finding))
    if args.command == "report-check":
        return cmd_report_check(section, root, Path(args.report), args.current_commit)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
