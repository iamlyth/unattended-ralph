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
SIGNER_TRUST = ROOT / ".factory/signer-trust.json"
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


def load_signer_trust() -> dict:
    """Load the tracked runner signer trust policy (public keys only).

    Private signing stays out-of-tree and root-owned; this repository carries
    only the policy and public keys. While no signer is provisioned
    (`enabled: false`, empty public_keys), runner manifests cannot certify
    capability evidence and are rejected as unsigned legacy/local manifests.
    """
    if SIGNER_TRUST.is_symlink() or not SIGNER_TRUST.is_file():
        fail("signer trust policy is missing: .factory/signer-trust.json")
    if SIGNER_TRUST.stat().st_size > MAX_EVIDENCE_FILE:
        fail("signer trust policy exceeds the size limit")
    try:
        data = json.loads(SIGNER_TRUST.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        fail(f"invalid signer trust policy: {exc}")
    expected = {
        "schema", "description", "require_signature", "enabled",
        "namespace", "public_keys", "allowed_principals",
    }
    if not isinstance(data, dict) or set(data) != expected or data.get("schema") != "ralph-runner-signer-trust/v1":
        fail("signer trust policy schema is invalid")
    if data.get("require_signature") is not True:
        fail("signer trust policy must require_signature=true")
    if type(data.get("enabled")) is not bool:
        fail("signer trust policy enabled flag is invalid")
    namespace = data.get("namespace")
    if not isinstance(namespace, str) or not namespace or "\n" in namespace:
        fail("signer trust policy namespace is invalid")
    keys = data.get("public_keys")
    principals = data.get("allowed_principals")
    if not isinstance(keys, list):
        fail("signer trust policy public_keys must be an array")
    if not isinstance(principals, list) or not all(isinstance(item, str) and item and "\n" not in item for item in principals):
        fail("signer trust policy allowed_principals is invalid")
    for key in keys:
        if not isinstance(key, dict):
            fail("signer trust policy public key entries must be objects")
        principal = key.get("principal")
        public_key = key.get("public_key")
        if (
            not isinstance(principal, str) or not principal or "\n" in principal
            or not isinstance(public_key, str) or not public_key.startswith("ssh-")
            or "\n" in public_key
        ):
            fail("signer trust policy public key entry is invalid")
    return data


def verify_manifest_signature(signer: dict, manifest_path: Path, raw: bytes) -> None:
    """Verify a runner manifest's detached signature with ssh-keygen -Y verify."""
    if not signer["enabled"] or not signer["public_keys"]:
        fail(
            "runner trust is not provisioned: no signer is configured, so unsigned "
            "legacy/local runner manifests are rejected and runner-evidenced "
            "capabilities stay unevidenced"
        )
    signature_path = manifest_path.parent / f"{manifest_path.stem}.sig"
    if signature_path.is_symlink() or not signature_path.is_file():
        fail(f"runner manifest has no detached signature: {signature_path}")
    if signature_path.stat().st_size > MAX_EVIDENCE_FILE:
        fail(f"runner signature exceeds the size limit: {signature_path}")
    allowed_signers: list[str] = []
    for key in signer["public_keys"]:
        allowed_signers.append(f"{key['principal']} {key['public_key']}")
    namespace = signer["namespace"]
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix="-allowed-signers", delete=False) as allowed:
        allowed.write("\n".join(allowed_signers) + "\n")
        allowed_path = Path(allowed.name)
    try:
        signature_bytes = signature_path.read_bytes()
        signature_file = tempfile.NamedTemporaryFile(delete=False)
        try:
            signature_file.write(signature_bytes)
            signature_file.close()
            # ssh-keygen -Y verify requires the signing principal (-I); the
            # signature carries the key, so each trust principal is tried.
            failures: list[str] = []
            verified = False
            for key in signer["public_keys"]:
                result = subprocess.run(
                    [
                        "ssh-keygen", "-Y", "verify",
                        "-f", str(allowed_path),
                        "-I", key["principal"],
                        "-n", namespace,
                        "-s", signature_file.name,
                    ],
                    input=raw, capture_output=True,
                )
                if result.returncode == 0:
                    verified = True
                    break
                failures.append(
                    result.stderr.decode("utf-8", errors="replace").strip()
                )
            if not verified:
                detail = next((item for item in failures if item), "signature verification failed")
                fail(f"runner manifest signature is invalid or fabricated: {detail}")
        finally:
            try:
                os.unlink(signature_file.name)
            except FileNotFoundError:
                pass
    finally:
        try:
            allowed_path.unlink()
        except FileNotFoundError:
            pass


def validate(expected_commit: str | None = None) -> tuple[str, list[str]]:
    os.environ["GIT_NO_REPLACE_OBJECTS"] = "1"
    signer = load_signer_trust()
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
        verify_manifest_signature(signer, manifest_path, raw)
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
