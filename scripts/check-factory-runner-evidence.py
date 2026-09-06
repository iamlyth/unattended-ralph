#!/usr/bin/env python3
"""Validate commit-bound evidence produced by declared factory runners."""

from __future__ import annotations

import argparse
import base64
import glob
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import struct
import subprocess
import sys
import tempfile
import tomllib

sys.path.insert(0, str(Path(__file__).resolve().parent))
from factory_runner_artifacts import (ArtifactError, PROTOCOL as ARTIFACT_PROTOCOL,
    MAX_ARTIFACTS, MAX_ARTIFACT_FILE, MAX_ARTIFACT_BYTES, validate_descriptors)

ROOT = Path(__file__).resolve().parent.parent
def aggregate_path(campaign_id: str, readiness_nonce: str) -> Path:
    if not NAME.fullmatch(campaign_id) or not SHA256.fullmatch(readiness_nonce):
        fail("campaign/readiness namespace is invalid")
    return ROOT / ".factory-state" / "runner-evidence" / campaign_id / readiness_nonce / "aggregate.json"
SIGNER_TRUST_PATH = ".factory/signer-trust.json"
SHA1 = re.compile(r"^[0-9a-f]{40}$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
NAME = re.compile(r"^[a-z0-9](?:[a-z0-9._-]{0,62}[a-z0-9])?$")
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
PYTHON = _resolve_pinned("python3")


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


def require_no_symlink_chain(path: Path, boundary: Path) -> None:
    try: relative=path.relative_to(boundary)
    except ValueError: fail(f"evidence path escapes boundary: {path}")
    current=boundary
    for part in relative.parts:
        current=current/part
        try: info=current.lstat()
        except OSError: fail(f"evidence path component is missing: {current}")
        if stat.S_ISLNK(info.st_mode): fail(f"evidence path component is a symlink: {current}")


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


class DuplicateTrustField(ValueError):
    pass


def _unique_object(pairs: list[tuple[str, object]]) -> dict:
    result: dict = {}
    for key, value in pairs:
        if key in result:
            raise DuplicateTrustField(key)
        result[key] = value
    return result


def _canonical_ed25519_key(value: object) -> str:
    """Validate canonical two-field OpenSSH ed25519 public-key text and wire."""
    if not isinstance(value, str) or value.count(" ") != 1 or value.strip() != value:
        fail("signer trust public key must be canonical two-field text")
    if any(character.isspace() and character != " " for character in value):
        fail("signer trust public key contains unsupported whitespace")
    algorithm, encoded = value.split(" ")
    if algorithm != "ssh-ed25519":
        fail("signer trust public key algorithm must be ssh-ed25519")
    try:
        wire = base64.b64decode(encoded, validate=True)
    except Exception:
        fail("signer trust public key base64 is invalid")
    if base64.b64encode(wire).decode("ascii") != encoded:
        fail("signer trust public key base64 is not canonical")
    try:
        algorithm_size = struct.unpack(">I", wire[:4])[0]
        offset = 4
        wire_algorithm = wire[offset:offset + algorithm_size]
        offset += algorithm_size
        key_size = struct.unpack(">I", wire[offset:offset + 4])[0]
        offset += 4
        key = wire[offset:offset + key_size]
        offset += key_size
    except (struct.error, ValueError):
        fail("signer trust public key wire encoding is invalid")
    if wire_algorithm != b"ssh-ed25519" or key_size != 32 or len(key) != 32 or offset != len(wire):
        fail("signer trust public key wire encoding is invalid")
    return value


def load_signer_trust(commit: str, label: str) -> dict:
    """Load trust from a pinned committed Git object, never the worktree."""
    try:
        text = git("show", f"{commit}:{SIGNER_TRUST_PATH}")
    except SystemExit:
        fail(f"{label} signer trust policy is missing from commit {commit}")
    if len(text.encode("utf-8")) > MAX_EVIDENCE_FILE:
        fail(f"{label} signer trust policy exceeds the size limit")
    try:
        data = json.loads(text, object_pairs_hook=_unique_object)
    except (UnicodeError, json.JSONDecodeError, DuplicateTrustField) as exc:
        fail(f"invalid {label} signer trust policy: {exc}")
    expected = {"schema", "description", "require_signature", "enabled", "namespace", "public_keys", "allowed_principals"}
    if not isinstance(data, dict) or set(data) != expected or data.get("schema") != "ralph-runner-signer-trust/v1":
        fail(f"{label} signer trust policy schema is invalid")
    if not isinstance(data["description"], str):
        fail(f"{label} signer trust policy description is invalid")
    if data.get("require_signature") is not True or type(data.get("enabled")) is not bool:
        fail(f"{label} signer trust must require signatures and have a boolean enabled flag")
    namespace = data.get("namespace")
    if not isinstance(namespace, str) or not namespace or any(character.isspace() for character in namespace):
        fail(f"{label} signer trust namespace is invalid")
    principals = data.get("allowed_principals")
    keys = data.get("public_keys")
    if not isinstance(principals, list) or not all(isinstance(item, str) and NAME.fullmatch(item) for item in principals):
        fail(f"{label} signer trust allowed_principals is invalid")
    if len(principals) != len(set(principals)):
        fail(f"{label} signer trust contains duplicate allowed principals")
    if not isinstance(keys, list):
        fail(f"{label} signer trust public_keys must be an array")
    if data["enabled"] is False:
        if principals or keys:
            fail(f"{label} disabled signer trust must have no principals or keys")
        return data
    if not principals or not keys:
        fail(f"{label} enabled signer trust requires principals and keys")
    seen_pairs: set[tuple[str, str]] = set()
    seen_keys: set[str] = set()
    principal_counts = {principal: 0 for principal in principals}
    for index, entry in enumerate(keys):
        if not isinstance(entry, dict) or set(entry) != {"principal", "public_key"}:
            fail(f"{label} signer trust public_keys[{index}] fields are invalid")
        principal = entry["principal"]
        if not isinstance(principal, str) or not NAME.fullmatch(principal) or principal not in principal_counts:
            fail(f"{label} signer trust public key has an unallowed principal")
        public_key = _canonical_ed25519_key(entry["public_key"])
        pair = (principal, public_key)
        if pair in seen_pairs:
            fail(f"{label} signer trust contains a duplicate key entry")
        if public_key in seen_keys:
            fail(f"{label} signer trust assigns one key across principals")
        seen_pairs.add(pair)
        seen_keys.add(public_key)
        principal_counts[principal] += 1
    # v1 has no explicit rotation-window representation, so exactly one key
    # per principal is the only unambiguous currently-supported state.
    if any(count != 1 for count in principal_counts.values()):
        fail(f"{label} signer trust requires exactly one key per allowed principal")
    return data


def require_trusted_pair(trust: dict, principal: str, key_sha256: str, label: str) -> dict:
    matches = [entry for entry in trust["public_keys"] if entry["principal"] == principal and hashlib.sha256(entry["public_key"].encode("ascii")).hexdigest() == key_sha256]
    if len(matches) != 1:
        fail(f"runner signer principal/key pair is absent from {label} trust")
    return matches[0]


def verify_manifest_signature(issuance_trust: dict, current_trust: dict,
                              manifest: dict, manifest_path: Path, raw: bytes) -> None:
    """Verify a runner manifest's detached signature with ssh-keygen -Y verify.

    The signature is anchored to the provisioned trust: the manifest must
    declare exactly one provisioned principal/key pair (the signer's own
    identity, bound into the signed manifest by the root-owned signer), the
    namespace must match the trust policy, and the detached signature must
    verify under that principal. Rotation is fail-closed: a signature from a
    key that was removed from the trust store, or a manifest that claims an
    unknown/foreign principal or key, is rejected.
    """
    namespace = manifest.get("namespace")
    principal = manifest.get("signer_principal")
    key_sha256 = manifest.get("signer_key_sha256")
    algorithm = manifest.get("signature_algorithm")
    if not isinstance(namespace, str) or namespace != issuance_trust["namespace"] or namespace != current_trust["namespace"]:
        fail("runner manifest namespace does not match issuance/current signer trust")
    if not isinstance(principal, str) or not NAME.fullmatch(principal):
        fail("runner manifest signer principal is invalid")
    if algorithm != "ssh-ed25519":
        fail("runner manifest signature algorithm is unsupported")
    issuance_key = require_trusted_pair(issuance_trust, principal, key_sha256, "issuance")
    require_trusted_pair(current_trust, principal, key_sha256, "current revocation")
    signature_path = manifest_path.parent / f"{manifest_path.stem}.sig"
    if signature_path.is_symlink() or not signature_path.is_file():
        fail(f"runner manifest has no detached signature: {signature_path}")
    if signature_path.stat().st_size > MAX_EVIDENCE_FILE:
        fail(f"runner signature exceeds the size limit: {signature_path}")
    # Verify against exactly the pair declared by and bound into this manifest;
    # unrelated trusted classes cannot satisfy ssh-keygen principal matching.
    allowed_signers = [f"{principal} {issuance_key['public_key']}"]
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


def expected_probe_authority_digest(commit: str, runner: str) -> str:
    try: data=json.loads(git("show",f"{commit}:.factory/runner-policy-enrollment.json"))
    except (SystemExit,json.JSONDecodeError): fail("commit-bound probe-authority enrollment is unavailable")
    top={"schema","status","probe_authorities","note"}
    if (not isinstance(data,dict) or set(data)!=top
            or data.get("schema")!="factory-runner-policy-enrollment/v1"
            or data.get("status")!="enrolled"
            or not isinstance(data.get("note"),str) or not data["note"]):
        fail("commit-bound production probe-authority enrollment is absent or invalid")
    authorities=data.get("probe_authorities")
    if not isinstance(authorities,dict) or runner not in authorities:
        fail("runner has no exact probe-authority enrollment")
    for name,item in authorities.items():
        if (not isinstance(name,str) or not NAME.fullmatch(name) or not isinstance(item,dict)
                or set(item)!={"version","authority_sha256","status"}
                or type(item.get("version")) is not int or item["version"] < 1
                or item.get("status")!="enrolled"
                or not SHA256.fullmatch(str(item.get("authority_sha256","")))):
            fail("commit-bound probe-authority enrollment is invalid")
    return authorities[runner]["authority_sha256"]


def validate_record(declared: dict, record: dict, commit: str, tree: str,
                    environment_blob: str, archive_sha256: str,
                    issuance_trust: dict, current_trust: dict,
                    campaign_id: str, readiness_nonce: str) -> set[str]:
    """Strictly validate one aggregate record and its runner manifest.

    This is the shared canonical per-record validation: an accepted manifest
    must be an exact record in the aggregate, digest-faithful, bound to the
    aggregate's commit/tree/environment/blob/archive, argv-faithful to the
    commit-bound declaration, and signed by a provisioned trust principal.
    """
    if not isinstance(record, dict) or set(record) != {"name", "manifest", "manifest_sha256", "capabilities", "artifact_manifest_sha256", "artifact_count", "artifact_bytes", "signer"}:
        fail("runner aggregate record is invalid")
    if record["name"] != declared.get("name") or record["capabilities"] != sorted(declared.get("capabilities", [])):
        fail("runner declaration/evidence mismatch")
    signer_meta = record["signer"]
    if (
        not isinstance(signer_meta, dict)
        or set(signer_meta) != {"principal", "key_sha256", "algorithm", "signature_sha256"}
    ):
        fail("runner aggregate signer metadata is invalid")
    if (
        not isinstance(signer_meta["principal"], str)
        or signer_meta["principal"] != record["name"]
        or signer_meta["principal"] != declared.get("name")
    ):
        fail("runner aggregate signer principal is not isolated to its declared runner class")
    for field in ("key_sha256", "signature_sha256"):
        if not isinstance(signer_meta[field], str) or not SHA256.fullmatch(signer_meta[field]):
            fail(f"runner aggregate signer {field} is invalid")
    relative = Path(record["manifest"])
    expected_prefix = Path(".factory-state/runner-evidence") / campaign_id / readiness_nonce / record["name"] / commit
    if relative.is_absolute() or ".." in relative.parts or expected_prefix not in relative.parents:
        fail("manifest path escapes its campaign/readiness/runner/commit namespace")
    manifest_path = ROOT / relative
    try:
        resolved_parent = manifest_path.parent.resolve(strict=True)
        evidence_root = (ROOT / ".factory-state/runner-evidence").resolve(strict=True)
    except FileNotFoundError:
        fail("manifest parent is missing")
    if evidence_root not in (resolved_parent, *resolved_parent.parents):
        fail("manifest is outside the evidence root")
    require_no_symlink_chain(manifest_path, ROOT / ".factory-state")
    manifest, raw = regular_json(manifest_path)
    if hashlib.sha256(raw).hexdigest() != record["manifest_sha256"]:
        fail("manifest digest mismatch")
    expected_fields = {
        "schema", "result", "runner", "commit", "tree", "environment_blob",
        "archive_sha256", "campaign_id", "readiness_nonce", "authority_sha256", "nonce", "capabilities",
        "exit_code", "timed_out", "started_at", "finished_at", "cleanup",
        "stdout_sha256", "stderr_sha256", "artifact_protocol", "artifact_limits",
        "artifact_count", "artifact_bytes", "artifact_manifest_sha256",
        "artifact_scope_sha256", "artifacts", "host_authority",
        "signer_principal", "signer_key_sha256", "namespace", "signature_algorithm",
    }
    if set(manifest) != expected_fields or manifest.get("schema") != "factory-runner-receipt/v3" or manifest.get("result") != "pass":
        fail("runner manifest schema/result is invalid")
    host=manifest["host_authority"]
    containment={"systemd_scope":True,"private_mounts":True,"private_pids":True,"bounded_writable_tmpfs":True,"broker_only_signing":True}
    if (not isinstance(host,dict) or set(host)!={"executable_pins","resource_policy_sha256","resource_observations","containment","cleanup_states"}
            or not isinstance(host["executable_pins"],dict) or not host["executable_pins"]
            or not SHA256.fullmatch(str(host["resource_policy_sha256"]))
            or not isinstance(host["resource_observations"],list)
            or host["containment"]!=containment
            or not isinstance(host["cleanup_states"],list)
            or len(host["cleanup_states"])!=len(manifest["capabilities"])+1
            or any(not isinstance(x,dict) or set(x)!={"capability","unit","clean"} or x.get("clean") is not True for x in host["cleanup_states"])):
        fail("runner manifest generic host authority is invalid")
    for pin in host["executable_pins"].values():
        if not isinstance(pin,dict) or set(pin)!={"path","sha256","device","inode"} or not SHA256.fullmatch(str(pin.get("sha256",""))):
            fail("runner manifest executable enrollment is invalid")
    if (
        manifest["runner"] != record["name"]
        or manifest["runner"] != declared.get("name")
        or manifest["signer_principal"] != manifest["runner"]
        or manifest["commit"] != commit or manifest["tree"] != tree
        or manifest["environment_blob"] != environment_blob
        or manifest["campaign_id"] != campaign_id
        or manifest["readiness_nonce"] != readiness_nonce
        or manifest["authority_sha256"] != expected_probe_authority_digest(commit, record["name"])
    ):
        fail("runner manifest binding or signer class isolation mismatch")
    if (
        manifest["signer_principal"] != signer_meta["principal"]
        or manifest["signer_key_sha256"] != signer_meta["key_sha256"]
        or manifest["signature_algorithm"] != signer_meta["algorithm"]
    ):
        fail("runner manifest signer metadata mismatch")
    if not isinstance(manifest["namespace"], str) or not manifest["namespace"]:
        fail("runner manifest namespace is invalid")
    if manifest["archive_sha256"] != archive_sha256:
        fail("runner manifest archive binding mismatch")
    if manifest["capabilities"] != record["capabilities"] or manifest["exit_code"] != 0 or manifest["timed_out"] is not False or manifest["cleanup"] is not True:
        fail("runner manifest does not prove a clean pass")
    for field in ("archive_sha256", "readiness_nonce", "authority_sha256", "nonce", "stdout_sha256", "stderr_sha256", "signer_key_sha256"):
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
    try:
        artifact_total, artifact_digest = validate_descriptors(manifest["artifacts"], manifest["capabilities"])
    except ArtifactError as exc:
        fail(f"runner artifact descriptors are invalid: {exc}")
    scope_digest = hashlib.sha256(json.dumps({"campaign_id": campaign_id,
        "readiness_nonce": readiness_nonce, "runner": manifest["runner"], "commit": commit, "nonce": manifest["nonce"],
        "artifact_manifest_sha256": artifact_digest}, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    if (manifest["artifact_protocol"] != ARTIFACT_PROTOCOL
            or manifest["artifact_limits"] != {"count":MAX_ARTIFACTS,"file_bytes":MAX_ARTIFACT_FILE,"aggregate_bytes":MAX_ARTIFACT_BYTES}
            or manifest["artifact_count"] != len(manifest["artifacts"])
            or manifest["artifact_bytes"] != artifact_total
            or manifest["artifact_manifest_sha256"] != artifact_digest
            or manifest["artifact_scope_sha256"] != scope_digest
            or record["artifact_manifest_sha256"] != artifact_digest
            or record["artifact_count"] != len(manifest["artifacts"])
            or record["artifact_bytes"] != artifact_total):
        fail("runner artifact summary/binding mismatch")
    artifact_root = manifest_path.parent / "artifacts"
    expected_paths=set()
    for descriptor in manifest["artifacts"]:
        parts=PurePosixPath(descriptor["path"]).parts
        artifact_path=artifact_root.joinpath(*parts)
        expected_paths.add(artifact_path.relative_to(artifact_root).as_posix())
        try:
            parent_fd=os.open(artifact_root,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW|os.O_CLOEXEC)
            try:
                for component in parts[:-1]:
                    next_fd=os.open(component,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW|os.O_CLOEXEC,dir_fd=parent_fd)
                    os.close(parent_fd); parent_fd=next_fd
                fd=os.open(parts[-1], os.O_RDONLY|os.O_NOFOLLOW|os.O_CLOEXEC,dir_fd=parent_fd)
            finally: os.close(parent_fd)
        except OSError: fail(f"retained artifact is absent or unsafe: {descriptor['path']}")
        try:
            info=os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode)!=descriptor["mode"] or info.st_nlink!=1 or info.st_size!=descriptor["size"]:
                fail(f"retained artifact metadata mismatch: {descriptor['path']}")
            digest=hashlib.sha256()
            while True:
                chunk=os.read(fd,65536)
                if not chunk: break
                digest.update(chunk)
            if digest.hexdigest()!=descriptor["sha256"]: fail(f"retained artifact digest mismatch: {descriptor['path']}")
        finally: os.close(fd)
    actual_paths=set()
    if artifact_root.exists():
        if artifact_root.is_symlink() or not artifact_root.is_dir(): fail("retained artifact root is unsafe")
        for current, dirs, files in os.walk(artifact_root, followlinks=False):
            if any((Path(current)/d).is_symlink() for d in dirs): fail("retained artifact directory symlink is forbidden")
            for filename in files: actual_paths.add((Path(current)/filename).relative_to(artifact_root).as_posix())
    if actual_paths != expected_paths: fail("retained artifact set has missing or extra files")
    # Semantics are bound to immutable external analyzer IDs and were applied
    # by the root broker to held bytes before signing. Unknown IDs cannot enter
    # a valid authority bundle; project-controlled analyzers are never run here.
    signature_path = manifest_path.parent / f"{manifest_path.stem}.sig"
    if signature_path.is_symlink() or not signature_path.is_file() or signature_path.stat().st_size > MAX_EVIDENCE_FILE:
        fail("runner detached signature is missing or unsafe")
    if hashlib.sha256(signature_path.read_bytes()).hexdigest() != signer_meta["signature_sha256"]:
        fail("runner signature digest does not match the aggregate metadata")
    verify_manifest_signature(issuance_trust, current_trust, manifest, manifest_path, raw)
    return set(record["capabilities"])


def verify_manifest_reference(reference: str, expected_commit: str,
                              expected_campaign_id: str, expected_readiness_nonce: str) -> dict:
    """Strict helper: validate one `[manifest:]` reference as an exact record.

    Runs the canonical full aggregate validation at the expected commit and then
    requires the reference to be exactly one aggregate record. Used by the audit
    scripts so any `[manifest:]` evidence passes the same signer/
    commit/tree/environment/archive/argv validation as this checker.
    """
    if not SHA1.fullmatch(expected_commit):
        fail("--verify-manifest requires a strict 40-hex --expected-commit audit base")
    _digest, _capabilities, aggregate = validate(
        expected_commit, expected_campaign_id=expected_campaign_id,
        expected_readiness_nonce=expected_readiness_nonce, include_view=True)
    # Match only the immutable aggregate bytes held and parsed by validate();
    # never reopen a pathname after strong validation.
    records = aggregate["runners"]
    matches = [record for record in records if record["manifest"] == reference]
    if len(matches) != 1:
        fail(f"runner manifest is not an exact record in the aggregate: {reference}")
    return matches[0]


def validate(expected_commit: str | None = None, *, expected_campaign_id: str | None = None,
             expected_readiness_nonce: str | None = None,
             include_view: bool = False):
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
    head = git("rev-parse", "HEAD")
    issuance_trust = load_signer_trust(commit, "issuance")
    current_trust = load_signer_trust(head, "current revocation")
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
    declared_names = [entry.get("name") for entry in declared if isinstance(entry, dict)]
    if (
        len(declared_names) != len(declared)
        or len(declared_names) != len(set(declared_names))
        or set(issuance_trust["allowed_principals"]) != set(declared_names)
        or not set(declared_names).issubset(set(current_trust["allowed_principals"]))
    ):
        fail("every declared runner must have distinct issuance/current signer trust coverage")
    if expected_campaign_id is None or expected_readiness_nonce is None:
        fail("explicit campaign/readiness namespace is required")
    aggregate, aggregate_raw = regular_json(aggregate_path(expected_campaign_id, expected_readiness_nonce))
    if set(aggregate) != {"schema", "campaign_id", "readiness_nonce", "commit", "tree", "environment_blob", "runners"} or aggregate.get("schema") != "factory-runner-aggregate/v4":
        fail("aggregate schema is invalid")
    if expected_campaign_id is None or aggregate["campaign_id"] != expected_campaign_id:
        fail("aggregate campaign binding is stale or absent")
    if expected_readiness_nonce is None or not SHA256.fullmatch(expected_readiness_nonce) or aggregate["readiness_nonce"] != expected_readiness_nonce:
        fail("aggregate readiness nonce is stale or replayed")
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
                            archive_sha256, issuance_trust, current_trust,
                            expected_campaign_id, expected_readiness_nonce)
        )
    result=(hashlib.sha256(aggregate_raw).hexdigest(), sorted(evidenced))
    # The optional classification view is parsed from the same held aggregate
    # bytes.  It is deliberately detached from all evidence pathnames.
    return (*result, aggregate) if include_view else result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--expected-commit")
    parser.add_argument("--expected-campaign-id", default=os.environ.get("FACTORY_CAMPAIGN_ID"))
    parser.add_argument("--expected-readiness-nonce", default=os.environ.get("FACTORY_READINESS_NONCE"))
    parser.add_argument("--print-digest", action="store_true")
    parser.add_argument("--print-capabilities", action="store_true")
    parser.add_argument("--verify-manifest")
    parser.add_argument("--print-record-json", action="store_true")
    args = parser.parse_args()
    if args.verify_manifest:
        record = verify_manifest_reference(
            args.verify_manifest, args.expected_commit, args.expected_campaign_id,
            args.expected_readiness_nonce,
        )
        if args.print_record_json:
            print(json.dumps(record,sort_keys=True,separators=(",",":")))
        else:
            print(
                f"factory-runner-evidence: verified manifest {record['manifest']} "
                f"(runner={record['name']}, capabilities={record['capabilities']})"
            )
        return 0
    digest, capabilities = validate(
        args.expected_commit, expected_campaign_id=args.expected_campaign_id,
        expected_readiness_nonce=args.expected_readiness_nonce,
    )
    if args.print_digest:
        print(digest)
    elif args.print_capabilities:
        print("\n".join(capabilities))
    else:
        print(f"factory-runner-evidence: valid ({len(capabilities)} capabilities, {digest[:12]})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
