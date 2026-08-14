#!/usr/bin/env python3
"""Validate commit-bound evidence produced by declared factory runners."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import tomllib

ROOT = Path(__file__).resolve().parent.parent
AGGREGATE = ROOT / ".factory-state/runner-evidence.json"
SHA1 = re.compile(r"^[0-9a-f]{40}$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
MAX_EVIDENCE_FILE = 16 * 1024 * 1024


def fail(message: str) -> None:
    raise SystemExit(f"factory-runner-evidence: {message}")


def git(*args: str) -> str:
    result = subprocess.run(["git", *args], cwd=ROOT, text=True, capture_output=True)
    if result.returncode:
        fail(f"Git binding failed: {' '.join(args)}")
    return result.stdout.strip()


def regular_json(path: Path) -> tuple[dict, bytes]:
    if path.is_symlink() or not path.is_file():
        fail(f"unsafe or missing evidence file: {path}")
    if path.stat().st_size > MAX_EVIDENCE_FILE:
        fail(f"evidence file exceeds size limit: {path}")
    try:
        raw = path.read_bytes()
        data = json.loads(raw)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        fail(f"invalid evidence file {path}: {exc}")
    if not isinstance(data, dict):
        fail(f"evidence must be an object: {path}")
    return data, raw


def validate(expected_commit: str | None = None) -> tuple[str, list[str]]:
    os.environ["GIT_NO_REPLACE_OBJECTS"] = "1"
    runtime = ROOT / ".factory-state"
    evidence_directory = runtime / "runner-evidence"
    if runtime.is_symlink() or not runtime.is_dir() or evidence_directory.is_symlink():
        fail("runner evidence root must be a real directory beneath .factory-state")
    if git("replace", "-l"):
        fail("Git replacement objects are forbidden")
    commit = expected_commit or git("rev-parse", "HEAD")
    if not SHA1.fullmatch(commit):
        fail("expected commit is invalid")
    git("cat-file", "-e", f"{commit}^{{commit}}")
    tree = git("rev-parse", f"{commit}^{{tree}}")
    environment_blob = git("rev-parse", f"{commit}:.factory/environment.toml")
    environment_text = git("show", f"{commit}:.factory/environment.toml")
    try:
        environment = tomllib.loads(environment_text)
    except tomllib.TOMLDecodeError:
        fail("commit-bound factory environment is invalid TOML")
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".toml") as environment_file:
        environment_file.write(environment_text); environment_file.flush()
        if subprocess.run(
            [str(ROOT / "scripts/check-factory-environment.py"), environment_file.name],
            cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        ).returncode:
            fail("commit-bound factory environment fails policy validation")
    declared = environment.get("runners", [])
    if not isinstance(declared, list):
        fail("runner declaration is invalid")
    aggregate, aggregate_raw = regular_json(AGGREGATE)
    if set(aggregate) != {"schema", "commit", "tree", "environment_blob", "runners"} or aggregate.get("schema") != "factory-runner-aggregate/v1":
        fail("aggregate schema is invalid")
    if aggregate["commit"] != commit or aggregate["tree"] != tree or aggregate["environment_blob"] != environment_blob:
        fail("aggregate Git/environment binding is stale")
    records = aggregate["runners"]
    if not isinstance(records, list) or len(records) != len(declared):
        fail("aggregate does not cover every declared runner")
    with tempfile.NamedTemporaryFile(prefix="factory-evidence-", suffix=".tar") as archive_file:
        if subprocess.run(
            ["git", "archive", "--format=tar", "--output", archive_file.name, commit],
            cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        ).returncode:
            fail("cannot reconstruct commit-bound source archive")
        archive_sha256 = hashlib.sha256(Path(archive_file.name).read_bytes()).hexdigest()
    evidenced: set[str] = set()
    for declaration, record in zip(declared, records):
        if not isinstance(record, dict) or set(record) != {"name", "manifest", "manifest_sha256", "capabilities"}:
            fail("runner aggregate record is invalid")
        if record["name"] != declaration.get("name") or record["capabilities"] != sorted(declaration.get("capabilities", [])):
            fail("runner declaration/evidence mismatch")
        relative = Path(record["manifest"])
        if relative.is_absolute() or ".." in relative.parts:
            fail("manifest path escapes the repository")
        manifest_path = ROOT / relative
        try:
            resolved_parent = manifest_path.parent.resolve(strict=True)
            evidence_root = (ROOT / ".factory-state/runner-evidence").resolve(strict=True)
        except FileNotFoundError:
            fail("manifest parent is missing")
        if evidence_root not in (resolved_parent, *resolved_parent.parents):
            fail("manifest is outside the evidence root")
        manifest, raw = regular_json(manifest_path)
        if hashlib.sha256(raw).hexdigest() != record["manifest_sha256"]:
            fail("manifest digest mismatch")
        expected_fields = {
            "schema", "result", "runner", "commit", "tree", "environment_blob",
            "verify_argv_sha256", "archive_sha256", "nonce", "capabilities",
            "exit_code", "timed_out", "started_at", "finished_at", "cleanup",
            "stdout_sha256", "stderr_sha256",
        }
        if set(manifest) != expected_fields or manifest.get("schema") != "factory-runner-receipt/v1" or manifest.get("result") != "pass":
            fail("runner manifest schema/result is invalid")
        if manifest["runner"] != record["name"] or manifest["commit"] != commit or manifest["tree"] != tree or manifest["environment_blob"] != environment_blob:
            fail("runner manifest binding mismatch")
        expected_argv_digest = hashlib.sha256(
            json.dumps(declaration["verify_argv"], separators=(",", ":")).encode()
        ).hexdigest()
        if manifest["verify_argv_sha256"] != expected_argv_digest or manifest["archive_sha256"] != archive_sha256:
            fail("runner manifest verifier/archive binding mismatch")
        if manifest["capabilities"] != record["capabilities"] or manifest["exit_code"] != 0 or manifest["timed_out"] is not False or manifest["cleanup"] is not True:
            fail("runner manifest does not prove a clean pass")
        for field in ("verify_argv_sha256", "archive_sha256", "nonce", "stdout_sha256", "stderr_sha256"):
            if not isinstance(manifest[field], str) or not SHA256.fullmatch(manifest[field]):
                fail(f"runner manifest has invalid {field}")
        for log_name, digest_field in (("stdout.log", "stdout_sha256"), ("stderr.log", "stderr_sha256")):
            log_path = manifest_path.parent / log_name
            if log_path.is_symlink() or not log_path.is_file() or log_path.stat().st_size > MAX_EVIDENCE_FILE:
                fail(f"runner log validation failed: {log_name}")
            if hashlib.sha256(log_path.read_bytes()).hexdigest() != manifest[digest_field]:
                fail(f"runner log validation failed: {log_name}")
        evidenced.update(record["capabilities"])
    return hashlib.sha256(aggregate_raw).hexdigest(), sorted(evidenced)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--expected-commit")
    parser.add_argument("--print-digest", action="store_true")
    parser.add_argument("--print-capabilities", action="store_true")
    args = parser.parse_args()
    digest, capabilities = validate(args.expected_commit)
    if args.print_digest:
        print(digest)
    elif args.print_capabilities:
        print("\n".join(capabilities))
    else:
        print(f"factory-runner-evidence: valid ({len(capabilities)} capabilities, {digest[:12]})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
