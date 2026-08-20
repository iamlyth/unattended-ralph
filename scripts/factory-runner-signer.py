#!/usr/bin/env python3
"""Root-owned detached-signature executor for factory-runner receipts.

Installed root-owned (0700 root:root, non-symlink) on the disposable runner VM
and reached only through a narrowly scoped sudoers entry from the unprivileged
forced-command endpoint (`factory-runner-server.py`). The endpoint never
signs: this process rebuilds the canonical manifest itself after validating
every field, so a caller can never have arbitrary bytes signed. Failures,
skips, unsupported capability claims, unbound digests, tampered or oversized
input, and post-cleanup mutations are rejected before a signature exists.

The private signing key stays out-of-tree on the runner at a root-owned
mode-0600 non-symlink path, unavailable to the unprivileged runner accounts,
and is never printed or copied into Git. The repository carries only the
matching public key and trust policy (`.factory/signer-trust.json`).
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

SHA1 = re.compile(r"^[0-9a-f]{40}$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
NAME = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
NAMESPACE = "factory-runner-receipt"
ALGORITHM = "ssh-ed25519"
# Out-of-tree root-owned provisioning paths; environment overrides exist only
# for the disposable test harness (sudo resets the environment in production,
# so the installed defaults always apply there).
KEY = Path(os.environ.get("FACTORY_SIGNER_KEY", "/etc/factory-runner/signer-key"))
PRINCIPAL_FILE = Path(
    os.environ.get("FACTORY_SIGNER_PRINCIPAL_FILE", "/etc/factory-runner/signer-principal")
)
SUPPORTED_CAPABILITIES = {
    "project-gate", "user-service",
}
REQUEST_FIELDS = {
    "schema", "result", "runner", "commit", "tree", "environment_blob",
    "verify_argv_sha256", "archive_sha256", "nonce", "capabilities",
    "exit_code", "timed_out", "started_at", "finished_at", "cleanup",
    "stdout_sha256", "stderr_sha256",
}
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


def validate_key_state() -> None:
    key_stat = checked_stat(KEY)
    principal_stat = checked_stat(PRINCIPAL_FILE)
    if not stat.S_ISREG(key_stat.st_mode):
        fail("signing key is not a regular file")
    if not stat.S_ISREG(principal_stat.st_mode):
        fail("signer principal is not a regular file")
    if key_stat.st_uid != 0 or key_stat.st_mode & 0o077:
        fail("signing key must be root-owned mode 0600")
    if principal_stat.st_uid != 0 or principal_stat.st_mode & 0o077:
        fail("signer principal must be root-owned mode 0600")


def load_principal() -> str:
    try:
        principal = PRINCIPAL_FILE.read_text(encoding="utf-8").strip()
    except OSError as exc:
        fail(f"cannot read signer principal: {type(exc).__name__}")
    if not NAME.fullmatch(principal):
        fail("signer principal is invalid")
    return principal


def derive_public_key() -> str:
    result = subprocess.run(
        [SSH_KEYGEN, "-y", "-f", str(KEY)],
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


def validate_manifest(raw: bytes) -> dict:
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
    if not isinstance(manifest, dict) or set(manifest) != REQUEST_FIELDS:
        fail("signing request manifest fields are invalid")
    if manifest.get("schema") != "factory-runner-receipt/v1":
        fail("signing request is not a runner receipt")
    if manifest.get("result") != "pass":
        fail("only passing receipts can be signed")
    if not isinstance(manifest["runner"], str) or not NAME.fullmatch(manifest["runner"]):
        fail("signing request runner is invalid")
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
    if not set(capabilities) <= SUPPORTED_CAPABILITIES:
        fail("signing request claims an unsupported capability")
    return manifest


def main() -> int:
    if os.getuid() != os.geteuid() or os.geteuid() != 0:
        fail("signer must run as root")
    validate_key_state()
    principal = load_principal()
    public = derive_public_key()
    key_sha256 = hashlib.sha256(public.encode("utf-8")).hexdigest()
    raw = sys.stdin.buffer.read(MAX_REQUEST + 1)
    evidence = validate_manifest(raw)
    manifest = dict(evidence)
    manifest.update({
        "signer_principal": principal,
        "signer_key_sha256": key_sha256,
        "namespace": NAMESPACE,
        "signature_algorithm": ALGORITHM,
    })
    canonical = (json.dumps(manifest, sort_keys=True, indent=2) + "\n").encode("utf-8")
    signed = subprocess.run(
        [SSH_KEYGEN, "-Y", "sign", "-f", str(KEY), "-n", NAMESPACE],
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
        "namespace": NAMESPACE,
        "signature_sha256": hashlib.sha256(signature).hexdigest(),
    }
    sys.stdout.write(json.dumps(response, sort_keys=True, separators=(",", ":")) + "\n")
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
