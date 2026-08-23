#!/usr/bin/env python3
"""Validate commit-bound evidence produced by declared factory runners."""

from __future__ import annotations

import argparse
import glob
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import tempfile
import tomllib

ROOT = Path(__file__).resolve().parent.parent
AGGREGATE = ROOT / ".factory-state/runner-evidence.json"
SIGNER_TRUST = ROOT / ".factory/signer-trust.json"
SHA1 = re.compile(r"^[0-9a-f]{40}$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
MAX_EVIDENCE_FILE = 16 * 1024 * 1024
# Finite bound for every trusted Git read (MED2): the pinned absolute Git
# executable can never wait forever behind the evidence boundary.
GIT_TIMEOUT = 120.0

# -- pinned immutable executable boundary (MED2) --------------------------------
#
# Every trusted Git read (and the ssh-keygen signature verifier) uses a
# pinned **absolute immutable** executable, never a PATH-derived name: an
# attacker-controlled PATH (or GIT_*/SSH_* environment) can never substitute
# a different binary behind the runner-evidence boundary.  The candidates are
# the fixed FHS locations, the NixOS system profile, and the immutable
# root-owned Nix store; each candidate must be a regular executable whose
# complete realpath chain the caller cannot modify (foreign-owned, no
# group/other write bits, sticky-protected store entries excepted), and the
# resolver fails closed as root (every component is caller-owned under uid 0).

FIXED_BIN_CANDIDATES = ("/usr/bin", "/bin", "/run/current-system/sw/bin")
NIX_STORE_BIN_GLOB = "/nix/store/*/bin"
NIX_STORE_PATH_RE = re.compile(r"^/nix/store/[0-9a-z]{32}-[^/]+/bin/[^/]+$")

# Git environment variables that can redirect where the executable reads
# repository state (F5) — stripped from every trusted invocation.
GIT_ENV_STRIP = (
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_INDEX_FILE",
    "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    "GIT_COMMON_DIR",
    "GIT_NAMESPACE",
    "GIT_CEILING_DIRECTORIES",
    "GIT_SSH",
    "GIT_SSH_COMMAND",
    "GIT_ASKPASS",
    "GIT_TERMINAL_PROMPT",
    "GIT_CONFIG_PARAMETERS",
    "GIT_EXEC_PATH",
    "GIT_TEMPLATE_DIR",
)


def sanitized_git_environment() -> dict:
    """Parent environment without every Git override key (by prefix for the
    complete ``GIT_CONFIG*`` family, exact names for the redirectors)."""
    environment = dict(os.environ)
    for key in list(environment):
        if key == "GIT_CONFIG" or key.startswith("GIT_CONFIG_") or key in GIT_ENV_STRIP:
            environment.pop(key, None)
    return environment


def _immutable_chain(path: str) -> None:
    """Fail unless the caller cannot modify ``path`` or any ancestor."""
    resolved = os.path.realpath(path)
    store_root = Path("/nix/store")
    if resolved.startswith(str(store_root) + os.sep):
        boundary = store_root
    else:
        boundary = Path(resolved).anchor
    current = Path(resolved)
    while True:
        try:
            info = current.lstat()
        except OSError as exc:
            fail(f"cannot stat pinned candidate component {current}: {exc}")
        if info.st_uid == os.getuid():
            sticky = stat.S_ISDIR(info.st_mode) and info.st_mode & stat.S_ISVTX
            if not sticky:
                fail(f"pinned candidate component {current} is caller-owned")
        if info.st_mode & 0o022 and not (
            stat.S_ISDIR(info.st_mode) and stat.S_ISVTX
        ):
            fail(f"pinned candidate component {current} is group/other-writable")
        if current == boundary or current == current.parent:
            break
        current = current.parent


def _candidate_usable(candidate: str) -> bool:
    """True when the absolute candidate is a regular immutable executable."""
    if not candidate.startswith("/"):
        return False
    try:
        _immutable_chain(candidate)
        info = os.stat(candidate)
    except (OSError, SystemExit):
        return False
    return stat.S_ISREG(info.st_mode) and bool(info.st_mode & 0o111)


def _resolve_pinned(name: str) -> str:
    """Resolve the pinned absolute immutable ``name`` executable (fail closed)."""
    if os.geteuid() == 0:
        fail(
            f"the pinned {name} boundary refuses to resolve as root: every "
            "candidate component is caller-owned under uid 0"
        )
    for directory in FIXED_BIN_CANDIDATES:
        candidate = f"{directory}/{name}"
        if os.path.exists(candidate) and _candidate_usable(candidate):
            return candidate
    try:
        found = sorted(glob.glob(f"{NIX_STORE_BIN_GLOB}/{name}"))
    except OSError:
        found = []
    for candidate in found:
        if NIX_STORE_PATH_RE.fullmatch(candidate) and _candidate_usable(candidate):
            return candidate
    fail(
        f"no immutable absolute {name} executable is available to the trusted "
        "runner-evidence boundary; refusing to resolve it from a caller PATH"
    )


GIT_EXECUTABLE = _resolve_pinned("git")
SSH_KEYGEN = _resolve_pinned("ssh-keygen")


def fail(message: str) -> None:
    raise SystemExit(f"factory-runner-evidence: {message}")


def git(*args: str) -> str:
    result = subprocess.run(
        [GIT_EXECUTABLE, *args], cwd=ROOT, text=True, capture_output=True,
        env=sanitized_git_environment(), timeout=GIT_TIMEOUT,
    )
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


def verify_manifest_signature(signer: dict, manifest: dict, manifest_path: Path, raw: bytes) -> None:
    """Verify a runner manifest's detached signature with ssh-keygen -Y verify.

    The signature is anchored to the provisioned trust: the manifest must
    declare exactly one provisioned principal/key pair (the signer's own
    identity, bound into the signed manifest by the root-owned signer), the
    namespace must match the trust policy, and the detached signature must
    verify under that principal. Rotation is fail-closed: a signature from a
    key that was removed from the trust store, or a manifest that claims an
    unknown/foreign principal or key, is rejected.
    """
    if not signer["enabled"] or not signer["public_keys"]:
        fail(
            "runner trust is not provisioned: no signer is configured, so unsigned "
            "legacy/local runner manifests are rejected and runner-evidenced "
            "capabilities stay unevidenced"
        )
    namespace = manifest.get("namespace")
    principal = manifest.get("signer_principal")
    key_sha256 = manifest.get("signer_key_sha256")
    algorithm = manifest.get("signature_algorithm")
    if not isinstance(namespace, str) or namespace != signer["namespace"]:
        fail("runner manifest namespace does not match signer trust")
    if not isinstance(principal, str) or principal not in signer["allowed_principals"]:
        fail("runner manifest signer principal is not trusted")
    if algorithm not in ("ssh-ed25519", "ssh-rsa"):
        fail("runner manifest signature algorithm is unsupported")
    matches = [
        key for key in signer["public_keys"]
        if key.get("principal") == principal
        and hashlib.sha256(key["public_key"].encode("utf-8")).hexdigest() == key_sha256
    ]
    if len(matches) != 1:
        fail("runner manifest signer key is not provisioned (rotation or substitution)")
    signature_path = manifest_path.parent / f"{manifest_path.stem}.sig"
    if signature_path.is_symlink() or not signature_path.is_file():
        fail(f"runner manifest has no detached signature: {signature_path}")
    if signature_path.stat().st_size > MAX_EVIDENCE_FILE:
        fail(f"runner signature exceeds the size limit: {signature_path}")
    allowed_signers: list[str] = []
    for key in signer["public_keys"]:
        allowed_signers.append(f"{key['principal']} {key['public_key']}")
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix="-allowed-signers", delete=False) as allowed:
        allowed.write("\n".join(allowed_signers) + "\n")
        allowed_path = Path(allowed.name)
    try:
        signature_bytes = signature_path.read_bytes()
        signature_file = tempfile.NamedTemporaryFile(delete=False)
        try:
            signature_file.write(signature_bytes)
            signature_file.close()
            result = subprocess.run(
                [
                    SSH_KEYGEN, "-Y", "verify",
                    "-f", str(allowed_path),
                    "-I", principal,
                    "-n", namespace,
                    "-s", signature_file.name,
                ],
                input=raw, capture_output=True, timeout=GIT_TIMEOUT,
            )
            if result.returncode != 0:
                detail = result.stderr.decode("utf-8", errors="replace").strip()
                fail(f"runner manifest signature is invalid or fabricated: {detail or 'verification failed'}")
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


def validate_record(declared: dict, record: dict, commit: str, tree: str,
                    environment_blob: str, archive_sha256: str, signer: dict) -> set[str]:
    """Strictly validate one aggregate record and its runner manifest.

    This is the shared canonical per-record validation: an accepted manifest
    must be an exact record in the aggregate, digest-faithful, bound to the
    aggregate's commit/tree/environment/blob/archive, argv-faithful to the
    commit-bound declaration, and signed by a provisioned trust principal.
    """
    if not isinstance(record, dict) or set(record) != {"name", "manifest", "manifest_sha256", "capabilities", "signer"}:
        fail("runner aggregate record is invalid")
    if record["name"] != declared.get("name") or record["capabilities"] != sorted(declared.get("capabilities", [])):
        fail("runner declaration/evidence mismatch")
    signer_meta = record["signer"]
    if (
        not isinstance(signer_meta, dict)
        or set(signer_meta) != {"principal", "key_sha256", "algorithm", "signature_sha256"}
    ):
        fail("runner aggregate signer metadata is invalid")
    if not isinstance(signer_meta["principal"], str) or not signer_meta["principal"]:
        fail("runner aggregate signer principal is invalid")
    for field in ("key_sha256", "signature_sha256"):
        if not isinstance(signer_meta[field], str) or not SHA256.fullmatch(signer_meta[field]):
            fail(f"runner aggregate signer {field} is invalid")
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
        "signer_principal", "signer_key_sha256", "namespace", "signature_algorithm",
    }
    if set(manifest) != expected_fields or manifest.get("schema") != "factory-runner-receipt/v1" or manifest.get("result") != "pass":
        fail("runner manifest schema/result is invalid")
    if manifest["runner"] != record["name"] or manifest["commit"] != commit or manifest["tree"] != tree or manifest["environment_blob"] != environment_blob:
        fail("runner manifest binding mismatch")
    if (
        manifest["signer_principal"] != signer_meta["principal"]
        or manifest["signer_key_sha256"] != signer_meta["key_sha256"]
        or manifest["signature_algorithm"] != signer_meta["algorithm"]
    ):
        fail("runner manifest signer metadata mismatch")
    if not isinstance(manifest["namespace"], str) or not manifest["namespace"]:
        fail("runner manifest namespace is invalid")
    expected_argv_digest = hashlib.sha256(
        json.dumps(declared["verify_argv"], separators=(",", ":")).encode()
    ).hexdigest()
    if manifest["verify_argv_sha256"] != expected_argv_digest or manifest["archive_sha256"] != archive_sha256:
        fail("runner manifest verifier/archive binding mismatch")
    if manifest["capabilities"] != record["capabilities"] or manifest["exit_code"] != 0 or manifest["timed_out"] is not False or manifest["cleanup"] is not True:
        fail("runner manifest does not prove a clean pass")
    for field in ("verify_argv_sha256", "archive_sha256", "nonce", "stdout_sha256", "stderr_sha256", "signer_key_sha256"):
        if not isinstance(manifest[field], str) or not SHA256.fullmatch(manifest[field]):
            fail(f"runner manifest has invalid {field}")
    for field in ("signer_principal", "signature_algorithm"):
        if not isinstance(manifest[field], str) or not manifest[field]:
            fail(f"runner manifest has invalid {field}")
    for log_name, digest_field in (("stdout.log", "stdout_sha256"), ("stderr.log", "stderr_sha256")):
        log_path = manifest_path.parent / log_name
        if log_path.is_symlink() or not log_path.is_file() or log_path.stat().st_size > MAX_EVIDENCE_FILE:
            fail(f"runner log validation failed: {log_name}")
        if hashlib.sha256(log_path.read_bytes()).hexdigest() != manifest[digest_field]:
            fail(f"runner log validation failed: {log_name}")
    signature_path = manifest_path.parent / f"{manifest_path.stem}.sig"
    if signature_path.is_symlink() or not signature_path.is_file() or signature_path.stat().st_size > MAX_EVIDENCE_FILE:
        fail("runner detached signature is missing or unsafe")
    if hashlib.sha256(signature_path.read_bytes()).hexdigest() != signer_meta["signature_sha256"]:
        fail("runner signature digest does not match the aggregate metadata")
    verify_manifest_signature(signer, manifest, manifest_path, raw)
    return set(record["capabilities"])


def verify_manifest_reference(reference: str, expected_commit: str) -> dict:
    """Strict helper: validate one `[manifest:]` reference as an exact record.

    Runs the canonical full aggregate validation at the expected commit and then
    requires the reference to be exactly one aggregate record. Used by the audit
    scripts so any `[manifest:]` evidence passes the same signer/
    commit/tree/environment/archive/argv validation as this checker.
    """
    if not SHA1.fullmatch(expected_commit):
        fail("--verify-manifest requires a strict 40-hex --expected-commit audit base")
    validate(expected_commit)
    aggregate, _ = regular_json(AGGREGATE)
    records = aggregate["runners"]
    matches = [record for record in records if record["manifest"] == reference]
    if len(matches) != 1:
        fail(f"runner manifest is not an exact record in the aggregate: {reference}")
    return matches[0]


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
            [GIT_EXECUTABLE, "archive", "--format=tar", "--output", archive_file.name, commit],
            cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            env=sanitized_git_environment(), timeout=GIT_TIMEOUT,
        ).returncode:
            fail("cannot reconstruct commit-bound source archive")
        archive_sha256 = hashlib.sha256(Path(archive_file.name).read_bytes()).hexdigest()
    evidenced: set[str] = set()
    for declaration, record in zip(declared, records):
        evidenced.update(
            validate_record(declaration, record, commit, tree, environment_blob,
                            archive_sha256, signer)
        )
    return hashlib.sha256(aggregate_raw).hexdigest(), sorted(evidenced)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--expected-commit")
    parser.add_argument("--print-digest", action="store_true")
    parser.add_argument("--print-capabilities", action="store_true")
    parser.add_argument("--verify-manifest")
    args = parser.parse_args()
    if args.verify_manifest:
        record = verify_manifest_reference(args.verify_manifest, args.expected_commit)
        print(
            f"factory-runner-evidence: verified manifest {record['manifest']} "
            f"(runner={record['name']}, capabilities={record['capabilities']})"
        )
        return 0
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
