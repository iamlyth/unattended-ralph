#!/usr/bin/env python3
"""Root-owned detached-signature executor for factory-runner receipts.

Installed root-owned (0700 root:root, non-symlink) on the disposable runner VM
and reached only through a narrowly scoped sudoers entry from the unprivileged
forced-command endpoint (`factory-runner-server.py`). The endpoint never
signs: this process rebuilds the canonical manifest itself after validating
every field, so a caller can never have arbitrary bytes signed. Failures,
skips, unsupported capability claims, unbound digests, tampered or oversized
input, and post-cleanup mutations are rejected before a signature exists.

The signing identity is class-bound and root-configured. sudo sets SUDO_USER
and SUDO_UID to the invoking unprivileged account; the policy
(/etc/factory-runner/runner-policy.json) binds that account to exactly one
runner class, and the private key and principal file for that class are
root-owned mode-0600 non-symlink files that the runner account can never read
or replace. The manifest's capability set must equal the class allowlist
exactly and its namespace must match the policy namespace. Environment
overrides exist only for the disposable test harness (sudo resets the
environment in production, so the installed defaults always apply there).
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys

# Explicit sibling-module resolution: the deployed root-owned copies share
# one directory, and the disposable harness runs them with python -I.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from factory_runner_policy import PolicyError, class_for_name, load_policy

SHA1 = re.compile(r"^[0-9a-f]{40}$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
NAME = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
ALGORITHM = "ssh-ed25519"
MAX_REQUEST = 1024 * 1024
MAX_SIGNATURE = 64 * 1024
SSH_KEYGEN = shutil.which("ssh-keygen") or "/usr/bin/ssh-keygen"


def fail(message: str) -> None:
    sys.stderr.write(f"factory-runner-signer: {message}\n")
    sys.stderr.flush()
    raise SystemExit(1)


def checked_stat(path: Path) -> os.stat_result:
    if path.is_symlink():
        fail(f"refusing a symlink path: {path}")
    try:
        return path.stat()
    except OSError as exc:
        fail(f"signer state unavailable at {path}: {type(exc).__name__}")


def resolve_signing_state() -> tuple[Path, Path, str]:
    """Resolve (key path, principal file, class name) from the root policy.

    Production: sudo sets SUDO_USER/SUDO_UID; the policy binds that account to
    one class and the class carries its own root-owned key/principal paths.
    Harness: FACTORY_SIGNER_KEY / FACTORY_SIGNER_PRINCIPAL_FILE override the
    paths and an optional FACTORY_SIGNER_CLASS enables the class capability
    policy check. Without either, the signer refuses to run.
    """
    sudo_user = os.environ.get("SUDO_USER")
    sudo_uid = os.environ.get("SUDO_UID")
    if sudo_user:
        try:
            policy = load_policy()
        except PolicyError as exc:
            fail(f"runner policy is unavailable: {exc}")
        try:
            runner_class = class_for_name(policy, sudo_user)
        except PolicyError as exc:
            fail(f"signer class binding failed: {exc}")
        if sudo_uid is not None:
            try:
                effective_uid = int(sudo_uid)
            except ValueError:
                fail("sudo uid is invalid")
            if effective_uid != runner_class["uid"]:
                fail("signer sudo identity does not match the runner class")
        return (
            Path(runner_class["signer_key"]),
            Path(runner_class["signer_principal_file"]),
            runner_class,
        )
    key_override = os.environ.get("FACTORY_SIGNER_KEY")
    principal_override = os.environ.get("FACTORY_SIGNER_PRINCIPAL_FILE")
    if not key_override or not principal_override:
        fail("signer must run via sudo (root-configured class) or with explicit harness overrides")
    runner_class = None
    class_override = os.environ.get("FACTORY_SIGNER_CLASS")
    if class_override:
        try:
            policy = load_policy()
            runner_class = class_for_name(policy, class_override)
        except PolicyError as exc:
            fail(f"runner policy is unavailable: {exc}")
    return Path(key_override), Path(principal_override), runner_class


def validate_key_state(key: Path, principal_file: Path) -> None:
    key_stat = checked_stat(key)
    principal_stat = checked_stat(principal_file)
    if not stat.S_ISREG(key_stat.st_mode):
        fail("signing key is not a regular file")
    if not stat.S_ISREG(principal_stat.st_mode):
        fail("signer principal is not a regular file")
    if key_stat.st_uid != 0 or key_stat.st_mode & 0o077:
        fail("signing key must be root-owned mode 0600")
    if principal_stat.st_uid != 0 or principal_stat.st_mode & 0o077:
        fail("signer principal must be root-owned mode 0600")


def load_principal(principal_file: Path) -> str:
    try:
        principal = principal_file.read_text(encoding="utf-8").strip()
    except OSError as exc:
        fail(f"cannot read signer principal: {type(exc).__name__}")
    if not NAME.fullmatch(principal):
        fail("signer principal is invalid")
    return principal


def derive_public_key(key: Path) -> str:
    result = subprocess.run(
        [SSH_KEYGEN, "-y", "-f", str(key)],
        capture_output=True, text=True, timeout=30,
    )
    if result.returncode != 0:
        fail("cannot derive the signer public key from the private key")
    public = result.stdout.strip()
    fields = public.split()
    if len(fields) < 2 or not fields[0].startswith("ssh-"):
        fail("signer public key is invalid")
    # Normalize to the same two-field form the trust policy carries (the
    # derived key may include a comment from the private-key filename).
    return " ".join(fields[:2])


def validate_manifest(raw: bytes, runner_class: dict) -> dict:
    if len(raw) > MAX_REQUEST or not raw.endswith(b"\n"):
        fail("invalid signing request")
    try:
        request = json.loads(raw)
    except (UnicodeError, json.JSONDecodeError):
        fail("signing request is not valid JSON")
    if (
        not isinstance(request, dict)
        or set(request) != {"schema", "manifest"}
        or request.get("schema") != "factory-runner-sign-request/v1"
    ):
        fail("signing request schema is invalid")
    manifest = request["manifest"]
    request_fields = {
        "schema", "result", "runner", "commit", "tree", "environment_blob",
        "verify_argv_sha256", "archive_sha256", "nonce", "capabilities",
        "exit_code", "timed_out", "started_at", "finished_at", "cleanup",
        "stdout_sha256", "stderr_sha256",
    }
    if not isinstance(manifest, dict) or set(manifest) != request_fields:
        fail("signing request manifest fields are invalid")
    if manifest.get("schema") != "factory-runner-receipt/v1":
        fail("signing request is not a runner receipt")
    if manifest.get("result") != "pass":
        fail("only passing receipts can be signed")
    if not isinstance(manifest["runner"], str) or not NAME.fullmatch(manifest["runner"]):
        fail("signing request runner is invalid")
    if runner_class is not None and manifest["runner"] != runner_class["name"]:
        fail("signing request runner does not match the runner class")
    for field in ("commit", "tree", "environment_blob"):
        if not isinstance(manifest[field], str) or not SHA1.fullmatch(manifest[field]):
            fail(f"signing request {field} is invalid")
    for field in ("verify_argv_sha256", "archive_sha256", "nonce", "stdout_sha256", "stderr_sha256"):
        if not isinstance(manifest[field], str) or not SHA256.fullmatch(manifest[field]):
            fail(f"signing request {field} is invalid")
    if manifest["exit_code"] != 0 or manifest["timed_out"] is not False or manifest["cleanup"] is not True:
        fail("signing request does not prove a clean pass")
    started = manifest["started_at"]
    finished = manifest["finished_at"]
    if (
        not isinstance(started, int) or not isinstance(finished, int)
        or started < 0 or finished < started
    ):
        fail("signing request timestamps are invalid")
    capabilities = manifest["capabilities"]
    if (
        not isinstance(capabilities, list) or not capabilities
        or len(capabilities) != len(set(capabilities))
        or not all(isinstance(item, str) and NAME.fullmatch(item) for item in capabilities)
    ):
        fail("signing request capabilities are invalid")
    if runner_class is not None:
        allowed = runner_class["allowed_capabilities"]
        if sorted(capabilities) != sorted(allowed):
            fail("signing request capabilities do not equal the runner class allowlist")
    return manifest


def main() -> int:
    if os.getuid() != os.geteuid() or os.geteuid() != 0:
        fail("signer must run as root")
    key_path, principal_path, runner_class = resolve_signing_state()
    validate_key_state(key_path, principal_path)
    principal = load_principal(principal_path)
    if runner_class is not None and principal != runner_class["name"]:
        fail("signer principal does not match the runner class")
    policy_namespace = None
    if runner_class is not None:
        try:
            policy_namespace = load_policy()["namespace"]
        except PolicyError as exc:
            fail(f"runner policy is unavailable: {exc}")
    namespace = policy_namespace or os.environ.get(
        "FACTORY_SIGNER_NAMESPACE", "factory-runner-receipt"
    )
    public = derive_public_key(key_path)
    key_sha256 = hashlib.sha256(public.encode("utf-8")).hexdigest()
    raw = sys.stdin.buffer.read(MAX_REQUEST + 1)
    evidence = validate_manifest(raw, runner_class)
    manifest = dict(evidence)
    manifest.update({
        "signer_principal": principal,
        "signer_key_sha256": key_sha256,
        "namespace": namespace,
        "signature_algorithm": ALGORITHM,
    })
    canonical = (json.dumps(manifest, sort_keys=True, indent=2) + "\n").encode("utf-8")
    signed = subprocess.run(
        [SSH_KEYGEN, "-Y", "sign", "-f", str(key_path), "-n", namespace],
        input=canonical, capture_output=True, timeout=120,
    )
    if signed.returncode != 0:
        fail(
            "signature generation failed: "
            + signed.stderr.decode("utf-8", errors="replace").strip()
        )
    signature = signed.stdout
    if (
        not signature or len(signature) > MAX_SIGNATURE
        or not signature.startswith(b"-----BEGIN SSH SIGNATURE-----")
    ):
        fail("signature generation produced invalid output")
    response = {
        "schema": "factory-runner-sign-response/v1",
        "result": "signed",
        "manifest_b64": base64.b64encode(canonical).decode("ascii"),
        "signature_b64": base64.b64encode(signature).decode("ascii"),
        "signer_principal": principal,
        "signer_key_sha256": key_sha256,
        "signature_algorithm": ALGORITHM,
        "namespace": namespace,
        "signature_sha256": hashlib.sha256(signature).hexdigest(),
    }
    sys.stdout.write(json.dumps(response, sort_keys=True, separators=(",", ":")) + "\n")
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
