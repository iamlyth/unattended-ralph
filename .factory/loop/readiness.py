#!/usr/bin/env python3
"""Generic fail-closed round-zero readiness and campaign launch authority."""
from __future__ import annotations

import base64
import fcntl
import hashlib
import hmac
import json
import os
import re
import secrets
import shutil
import stat
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Callable, Mapping, Sequence

POLICY_SCHEMA = "factory-readiness-policy/v1"
RESULT_SCHEMA = "factory-readiness-result/v2"
AUTH_SCHEMA = "factory-campaign-launch-authority/v1"
POLICY_PATH = ".factory/readiness-policy.json"
SHA1 = re.compile(r"^[0-9a-f]{40}$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
IDENT = re.compile(r"^[a-z0-9](?:[a-z0-9._-]{0,62}[a-z0-9])?$")
ZERO = "0" * 64
GATE_REGISTRY = {
    "runner-aggregate": ("./.factory/tools/check-factory-runner-evidence.py",),
    "capability-evidence": ("./.factory/tools/check-capability-evidence.py",),
    "conformance-planning": ("./.factory/tools/validate-conformance.py", "planning", ".factory/artifacts/conformance.json"),
    "conformance-implementation": ("./.factory/tools/validate-conformance.py", "implementation", ".factory/artifacts/conformance.json"),
    "boilerplate-verification": ("./.factory/tools/verify-boilerplate.sh",),
    "final-acceptance": ("./.factory/tools/final-gate.sh", "--implementation"),
}

class ReadinessError(RuntimeError): pass
class ReadinessFindings(ReadinessError): pass
class HumanAuthorityBlocked(ReadinessError): pass
class InfrastructureFailure(ReadinessError): pass
class AuthorizationError(ReadinessError): pass


def canonical_bytes(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode() + b"\n"


def digest(value: bytes | object) -> str:
    raw = value if isinstance(value, bytes) else canonical_bytes(value)
    return hashlib.sha256(raw).hexdigest()


def _safe_relative(value: object, label: str) -> str:
    if not isinstance(value, str) or not value or value.startswith("/"):
        raise ReadinessError(f"{label} must be repository-relative")
    parts = PurePosixPath(value).parts
    if any(p in ("", ".", "..") or any(ord(c) < 32 for c in p) for p in parts):
        raise ReadinessError(f"{label} is unsafe")
    return value


def read_dirfd_file(root: Path, relative: str, *, maximum: int = 4 * 1024 * 1024) -> bytes:
    """Bounded no-follow openat walk; no mutable pathname is trusted after open."""
    relative = _safe_relative(relative, "authority path")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    fd = os.open(Path(root).absolute(), flags)
    try:
        parts = PurePosixPath(relative).parts
        for part in parts[:-1]:
            child = os.open(part, flags, dir_fd=fd)
            os.close(fd); fd = child
        leaf = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0), dir_fd=fd)
        try:
            info = os.fstat(leaf)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.getuid() or info.st_mode & 0o022:
                raise InfrastructureFailure(f"unsafe authority file: {relative}")
            chunks, total = [], 0
            while True:
                chunk = os.read(leaf, min(65536, maximum + 1 - total))
                if not chunk: break
                chunks.append(chunk); total += len(chunk)
                if total > maximum: raise InfrastructureFailure(f"oversized authority file: {relative}")
            return b"".join(chunks)
        finally: os.close(leaf)
    except OSError as exc:
        raise InfrastructureFailure(f"cannot open authority {relative}: {exc}") from exc
    finally: os.close(fd)


def read_external_trust(path_text: str, *, maximum: int = 256 * 1024) -> bytes:
    """Read root-owned external trust through a no-follow component chain."""
    path=Path(path_text)
    if not path.is_absolute(): raise InfrastructureFailure("human trust path is not absolute")
    current=Path("/")
    for part in path.parts[1:]:
        current/=part; info=os.stat(current,follow_symlinks=False)
        if stat.S_ISLNK(info.st_mode) or info.st_uid!=0 or info.st_mode&0o022:
            raise InfrastructureFailure("human trust ancestry is not root-owned immutable")
    fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_CLOEXEC)
    try:
        info=os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink!=1 or info.st_uid!=0 or info.st_mode&0o022 or info.st_size>maximum:
            raise InfrastructureFailure("human trust file is unsafe")
        raw=os.read(fd,maximum+1)
        if len(raw)>maximum: raise InfrastructureFailure("human trust file is oversized")
        return raw
    finally: os.close(fd)


def _ident_list(value: object, label: str, *, nonempty: bool = False) -> tuple[str, ...]:
    if not isinstance(value, list) or (nonempty and not value) or any(not isinstance(v, str) or not IDENT.fullmatch(v) for v in value) or len(value) != len(set(value)):
        raise ReadinessError(f"{label} must be a unique canonical identifier list")
    return tuple(value)


def validate_policy(value: object) -> dict:
    fields = {"schema","status","production_authority","required_runner_classes","required_capabilities","conformance_gate_ids","core_gate_ids","human_approval","invalidation"}
    if not isinstance(value, dict) or set(value) != fields or value.get("schema") != POLICY_SCHEMA or value.get("status") != "active":
        raise ReadinessError("readiness policy schema/fields are invalid")
    authority = value["production_authority"]
    if not isinstance(authority, dict) or set(authority) != {"enrolled","reason"} or not isinstance(authority["enrolled"], bool) or not isinstance(authority["reason"], str) or not authority["reason"]:
        raise ReadinessError("production authority enrollment is malformed")
    classes = value["required_runner_classes"]
    if not isinstance(classes, list): raise ReadinessError("runner classes must be an array")
    seen = set()
    for item in classes:
        if not isinstance(item, dict) or set(item) != {"id","capabilities"} or not isinstance(item["id"], str) or not IDENT.fullmatch(item["id"]) or item["id"] in seen:
            raise ReadinessError("runner class declaration is malformed or duplicated")
        seen.add(item["id"]); _ident_list(item["capabilities"], "runner capabilities")
    required_caps = set(_ident_list(value["required_capabilities"], "required capabilities"))
    if not required_caps.issubset({cap for item in classes for cap in item["capabilities"]}):
        raise ReadinessError("required capability has no declared runner class")
    gates = _ident_list(value["conformance_gate_ids"], "conformance gates", nonempty=True) + _ident_list(value["core_gate_ids"], "core gates", nonempty=True)
    unknown = set(gates) - set(GATE_REGISTRY)
    if unknown: raise ReadinessError(f"unknown fixed readiness gate adapter: {sorted(unknown)!r}")
    if authority["enrolled"] is True and "conformance-implementation" not in gates:
        raise ReadinessError("enrolled production readiness requires complete implementation conformance")
    human = value["human_approval"]
    if human is not None:
        hfields={"required","approval_schema","approval_path","signature_path","signature_namespace","trust_scope","trust_path","checklist","captures"}
        if not isinstance(human,dict) or set(human)!=hfields or human["required"] is not True:
            raise ReadinessError("human approval policy is malformed")
        for key in ("approval_schema","signature_namespace","trust_scope"):
            if not isinstance(human[key],str) or not IDENT.fullmatch(human[key]): raise ReadinessError(f"human {key} is invalid")
        _safe_relative(human["approval_path"],"human approval path"); _safe_relative(human["signature_path"],"human signature path")
        if not isinstance(human["trust_path"],str) or not human["trust_path"].startswith("/") or ".." in PurePosixPath(human["trust_path"]).parts:
            raise ReadinessError("human trust path must be a safe absolute external path")
        _ident_list(human["checklist"],"human checklist",nonempty=True)
        if not isinstance(human["captures"],list): raise ReadinessError("human captures must be an array")
        for capture in human["captures"]:
            if not isinstance(capture,dict) or set(capture)!={"id","path_field","digest_field"} or any(not isinstance(capture[k],str) or not IDENT.fullmatch(capture[k]) for k in capture): raise ReadinessError("human capture binding is malformed")
    inv=value["invalidation"]
    if not isinstance(inv,dict) or set(inv)!={"accepted_commit","current_product","paths"}: raise ReadinessError("invalidation policy is malformed")
    accepted=_ident_list(inv["accepted_commit"],"accepted invalidation",nonempty=True); current=_ident_list(inv["current_product"],"current invalidation",nonempty=True)
    if set(accepted+current)-set(GATE_REGISTRY): raise ReadinessError("invalidation names an unknown adapter")
    if not isinstance(inv["paths"],dict) or set(inv["paths"])-set(current): raise ReadinessError("invalidation paths are not current-gate scoped")
    for gate, paths in inv["paths"].items():
        if not isinstance(paths,list) or not paths: raise ReadinessError(f"invalidation paths for {gate} are empty")
        for path in paths: _safe_relative(path.rstrip("/"),"invalidation path")
    return value


def load_policy(root: Path, raw: bytes | None = None) -> tuple[dict,str]:
    raw = read_dirfd_file(root, POLICY_PATH) if raw is None else raw
    try: value=json.loads(raw.decode("utf-8"))
    except (UnicodeError,ValueError) as exc: raise ReadinessError(f"readiness policy is malformed: {exc}") from exc
    return validate_policy(value), hashlib.sha256(raw).hexdigest()


def gate_argv(gate_id: str) -> tuple[str,...]:
    try: return GATE_REGISTRY[gate_id]
    except KeyError as exc: raise ReadinessError(f"unknown fixed readiness gate adapter: {gate_id}") from exc


def validate_aggregate(value: object, policy: Mapping[str,object], *, accepted_commit: str, tree: str, environment_blob: str, campaign_id: str = "", readiness_nonce: str = "") -> str:
    """Canonical aggregate-v4 class/capability interface."""
    fields={"schema","campaign_id","readiness_nonce","commit","tree","environment_blob","runners"}
    if not isinstance(value,dict) or set(value)!=fields or value.get("schema")!="factory-runner-aggregate/v4":
        raise ReadinessFindings("runner aggregate schema is invalid")
    if value.get("campaign_id")!=campaign_id or value.get("readiness_nonce")!=readiness_nonce:
        raise ReadinessFindings("runner aggregate campaign/readiness binding is stale")
    if (value["commit"],value["tree"],value["environment_blob"]) != (accepted_commit,tree,environment_blob): raise ReadinessFindings("runner aggregate binding is stale")
    records=value["runners"]
    if not isinstance(records,list): raise ReadinessFindings("runner aggregate records are malformed")
    actual={}
    for record in records:
        if not isinstance(record,dict): raise ReadinessFindings("runner aggregate record is malformed")
        runner_class=record.get("class",record.get("name")); caps=record.get("capabilities")
        if not isinstance(runner_class,str) or not IDENT.fullmatch(runner_class) or runner_class in actual or not isinstance(caps,list) or len(caps)!=len(set(caps)) or any(not isinstance(c,str) or not IDENT.fullmatch(c) for c in caps): raise ReadinessFindings("runner aggregate class/capabilities are ambiguous")
        if record.get("result","pass") != "pass": raise ReadinessFindings(f"runner class {runner_class} did not pass")
        actual[runner_class]=set(caps)
    expected={item["id"]:set(item["capabilities"]) for item in policy["required_runner_classes"]} # type: ignore[index]
    if set(actual)!=set(expected): raise ReadinessFindings("runner aggregate does not cover the exact required class set")
    for name,caps in expected.items():
        if actual[name] != caps: raise ReadinessFindings(f"runner class {name} capability set differs from policy")
    covered=set().union(*actual.values()) if actual else set()
    if not set(policy["required_capabilities"]).issubset(covered): raise ReadinessFindings("required capabilities are not covered")
    normalized={"schema":"factory-runner-aggregate/canonical-v1","commit":accepted_commit,"tree":tree,"environment_blob":environment_blob,"runners":[{"class":n,"capabilities":sorted(actual[n])} for n in sorted(actual)]}
    return digest(normalized)


def _trusted_ssh_keygen() -> str:
    candidates=["/usr/bin/ssh-keygen","/bin/ssh-keygen","/run/current-system/sw/bin/ssh-keygen"]
    discovered=shutil.which("ssh-keygen")
    if discovered: candidates.append(discovered)
    for candidate in candidates:
        resolved=os.path.realpath(candidate)
        try:
            try:
                from . import gitutil as _gitutil
            except ImportError:
                import gitutil as _gitutil  # type: ignore[no-redef]
            _gitutil.require_trusted_executable(resolved)
            info=os.stat(resolved)
            if resolved.startswith("/nix/store/"):
                return resolved
            current=Path("/")
            for part in Path(resolved).parts[1:-1]:
                current/=part; parent=os.stat(current,follow_symlinks=False)
                if parent.st_uid!=0 or parent.st_mode&0o022: raise OSError("mutable ancestry")
            if stat.S_ISREG(info.st_mode) and info.st_uid==0 and not info.st_mode&0o022 and info.st_mode&0o111:
                return resolved
        except (OSError, _gitutil.GitBoundaryError):
            continue
    raise HumanAuthorityBlocked("no immutable root-owned ssh-keygen is available")


def validate_human_authority(policy: Mapping[str, object], approval_raw: bytes | None,
                             trust_raw: bytes | None, *, signature_raw: bytes | None = None,
                             accepted_commit: str, accepted_tree: str,
                             blob_at: Callable[[str, str], bytes], now: int | None = None) -> str:
    """Cryptographically verify canonical approval bytes with external SSH trust."""
    human = policy.get("human_approval")
    if human is None:
        return digest({"required": False})
    if approval_raw is None or trust_raw is None or signature_raw is None:
        raise HumanAuthorityBlocked("required external human authority is missing")
    if len(approval_raw)>256*1024 or len(trust_raw)>256*1024 or len(signature_raw)>64*1024:
        raise HumanAuthorityBlocked("external human authority exceeds its bound")
    try:
        approval=json.loads(approval_raw); trust=json.loads(trust_raw)
    except (UnicodeError,ValueError) as exc:
        raise HumanAuthorityBlocked(f"external human authority is malformed: {exc}") from exc
    if not isinstance(human,Mapping): raise HumanAuthorityBlocked("human policy is malformed")
    trust_fields={"schema","status","scope","namespace","keys"}
    if not isinstance(trust,dict) or set(trust)!=trust_fields or trust.get("schema")!="factory-human-trust/v2" or trust.get("status")!="active" or trust.get("scope")!=human["trust_scope"] or trust.get("namespace")!=human["signature_namespace"] or not isinstance(trust.get("keys"),list) or not trust["keys"]:
        raise HumanAuthorityBlocked("external human trust is absent, inactive, or wrong-scope")
    required={"schema","status","commit","tree","checklist","captures","reviewer","issued_at"}
    if not isinstance(approval,dict) or set(approval)!=required or approval.get("schema")!=human["approval_schema"] or approval.get("status")!="approved" or approval.get("commit")!=accepted_commit or approval.get("tree")!=accepted_tree or approval.get("checklist")!=human["checklist"] or type(approval.get("issued_at")) is not int:
        raise HumanAuthorityBlocked("human approval binding/checklist is incomplete")
    reviewer=approval.get("reviewer")
    key_fields={"principal","public_key","issued_at","revoked_at"}
    reviewers=[k for k in trust["keys"] if isinstance(k,dict) and set(k)==key_fields and k.get("principal")==reviewer]
    if len(reviewers)!=1: raise HumanAuthorityBlocked("human reviewer is not uniquely trusted")
    key=reviewers[0]; current=int(time.time()) if now is None else now
    if type(key["issued_at"]) is not int or key["issued_at"]>approval["issued_at"] or approval["issued_at"]>current:
        raise HumanAuthorityBlocked("human approval/key issuance time is invalid")
    if key["revoked_at"] is not None:
        raise HumanAuthorityBlocked("human approval key is currently revoked")
    public_key=key["public_key"]
    if not isinstance(public_key,str) or "\n" in public_key or not public_key.startswith("ssh-ed25519 ") or not IDENT.fullmatch(str(reviewer)):
        raise HumanAuthorityBlocked("human trust key/principal is invalid")
    captures=approval.get("captures")
    if not isinstance(captures,list) or len(captures)!=len(human["captures"]): raise HumanAuthorityBlocked("human capture set is incomplete")
    by_id={item.get("id"):item for item in captures if isinstance(item,dict)}
    for binding in human["captures"]:
        item=by_id.get(binding["id"])
        if not isinstance(item,dict): raise HumanAuthorityBlocked(f"human capture {binding['id']} is missing")
        path=_safe_relative(item.get(binding["path_field"]),"human capture")
        if hashlib.sha256(blob_at(accepted_commit,path)).hexdigest()!=item.get(binding["digest_field"]): raise HumanAuthorityBlocked(f"human capture {binding['id']} digest is stale")
    signed=canonical_bytes(approval)
    if approval_raw != signed:
        raise HumanAuthorityBlocked("human approval bytes are not canonical")
    allowed_fd=os.memfd_create("factory-human-allowed",os.MFD_CLOEXEC)
    sig_fd=os.memfd_create("factory-human-signature",os.MFD_CLOEXEC)
    try:
        os.write(allowed_fd,f"{reviewer} {public_key}\n".encode()); os.lseek(allowed_fd,0,0)
        os.write(sig_fd,signature_raw); os.lseek(sig_fd,0,0)
        verified=subprocess.run(
            [_trusted_ssh_keygen(),"-Y","verify","-f",f"/proc/self/fd/{allowed_fd}",
             "-I",str(reviewer),"-n",str(human["signature_namespace"]),
             "-s",f"/proc/self/fd/{sig_fd}"], input=signed,
            stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,
            pass_fds=(allowed_fd,sig_fd),timeout=10,check=False,
        )
        if verified.returncode!=0: raise HumanAuthorityBlocked("human detached signature verification failed")
    except (OSError,subprocess.TimeoutExpired) as exc:
        raise HumanAuthorityBlocked(f"human signature verifier unavailable: {exc}") from exc
    finally:
        os.close(allowed_fd); os.close(sig_fd)
    return digest(signed+b"\0"+trust_raw+b"\0"+signature_raw)


def readiness_bindings(*, accepted_commit:str, accepted_tree:str, current_commit:str, current_tree:str, config_sha256:str, environment_sha256:str, specification_sha256:str, plan_sha256:str, contracts_sha256:str, policy_sha256:str, trust_sha256:str, install_manifest_sha256:str) -> dict:
    result=locals().copy()
    for key,value in result.items():
        pattern=SHA1 if key.endswith("commit") or key.endswith("tree") else SHA256
        if not isinstance(value,str) or not pattern.fullmatch(value): raise ReadinessError(f"binding {key} is malformed")
    return result


def evaluate(policy: Mapping[str, object], *, aggregate_sha256: str, gate_results: Mapping[str, Mapping[str, object]], human_sha256: str) -> tuple[str, dict[str, str]]:
    """Classify canonical adapter outputs without accepting caller commands.

    An adapter result is exactly ``{"ran": bool, "exit": int, "digest":
    sha256}``. Missing/not-run adapters are infrastructure failures, nonzero
    validated gates are findings, and a missing required human authority is a
    human block. This interface is independent of runner class names.
    """
    validate_policy(dict(policy))
    if policy["production_authority"]["enrolled"] is not True:  # type: ignore[index]
        return "human_block", {"aggregate": ZERO, "human": ZERO}
    if not SHA256.fullmatch(aggregate_sha256):
        return "infrastructure_failure", {"aggregate": ZERO, "human": ZERO}
    results={"aggregate":aggregate_sha256,"human":human_sha256}
    required=tuple(policy["conformance_gate_ids"])+tuple(policy["core_gate_ids"])  # type: ignore[arg-type]
    for gate in required:
        item=gate_results.get(gate)
        if not isinstance(item,Mapping) or set(item)!={"ran","exit","digest"} or item.get("ran") is not True or type(item.get("exit")) is not int or not SHA256.fullmatch(str(item.get("digest",""))):
            return "infrastructure_failure", results
        results[gate]=str(item["digest"])
        if item["exit"] != 0: return "findings", results
    if policy.get("human_approval") is not None and (not SHA256.fullmatch(human_sha256) or human_sha256==ZERO):
        return "human_block", results
    if not SHA256.fullmatch(human_sha256): return "infrastructure_failure", results
    return "complete", results


def result_document(*,campaign_id:str,nonce:str,status:str,bindings:Mapping[str,str],results:Mapping[str,str]) -> dict:
    outcomes={"complete":"pass","findings":"findings","human_block":"blocked","infrastructure_failure":"infrastructure_failure"}
    if not IDENT.fullmatch(campaign_id) or not SHA256.fullmatch(nonce) or status not in outcomes: raise ReadinessError("readiness result identity/status is invalid")
    if any(not SHA256.fullmatch(v) for v in results.values()): raise ReadinessError("readiness result digest is malformed")
    if status=="complete" and (not results or ZERO in results.values()): raise ReadinessError("complete readiness requires canonical nonzero results")
    return {"schema":RESULT_SCHEMA,"campaign_id":campaign_id,"nonce":nonce,"status":status,"terminal_outcome":outcomes[status],"bindings":dict(bindings),"results":dict(results)}


def validate_result(value:object,*,campaign_id:str,nonce:str,bindings:Mapping[str,str]) -> None:
    if not isinstance(value,dict) or set(value)!={"schema","campaign_id","nonce","status","terminal_outcome","bindings","results"} or value.get("schema")!=RESULT_SCHEMA: raise ReadinessError("readiness cache schema is invalid")
    expected=result_document(campaign_id=campaign_id,nonce=nonce,status=value.get("status"),bindings=bindings,results=value.get("results",{}))
    if value != expected: raise ReadinessError("readiness cache binding is forged, stale, or cross-campaign")


@dataclass(frozen=True)
class CampaignPermit:
    campaign_id: str
    nonce: str
    marker: object


class AuthorizationStore:
    """Campaign-lock-bound, FD-secret-backed, durable one-use launch records.

    Construction is private to :func:`open_locked_authorization_store`; public
    imports and standalone launch code cannot create this authority merely by
    possessing Python objects or module globals.
    """
    def __init__(self, root:Path, namespace:str, campaign_id:str, nonce:str,
                 lock_fd:int, readiness_document: Mapping[str, object]):
        if (not isinstance(readiness_document,Mapping)
                or readiness_document.get("schema")!=RESULT_SCHEMA
                or readiness_document.get("status")!="complete"
                or readiness_document.get("campaign_id")!=campaign_id
                or readiness_document.get("nonce")!=nonce):
            raise AuthorizationError("complete exact-campaign readiness is required")
        if not IDENT.fullmatch(campaign_id) or not SHA256.fullmatch(nonce):
            raise AuthorizationError("authorization identity is invalid")
        root=Path(root).absolute(); root_info=os.stat(root,follow_symlinks=False)
        lock_info=os.fstat(lock_fd)
        if not stat.S_ISDIR(lock_info.st_mode) or (root_info.st_dev,root_info.st_ino)!=(lock_info.st_dev,lock_info.st_ino):
            raise AuthorizationError("authorization store is not bound to the campaign root lock")
        self.root=root; self.namespace=_safe_relative(namespace,"campaign namespace")
        self.campaign_id=campaign_id; self.nonce=nonce
        directory=self.root/self.namespace
        info=os.stat(directory,follow_symlinks=False)
        if not stat.S_ISDIR(info.st_mode) or info.st_uid!=os.getuid() or stat.S_IMODE(info.st_mode)!=0o700:
            raise AuthorizationError("campaign authorization directory is unsafe")
        self.dirfd=os.open(directory,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW|getattr(os,"O_CLOEXEC",0))
        self.keyfd=os.memfd_create("factory-launch-key", os.MFD_CLOEXEC|os.MFD_ALLOW_SEALING)
        os.write(self.keyfd,secrets.token_bytes(32)); os.lseek(self.keyfd,0,os.SEEK_SET)
        fcntl.fcntl(self.keyfd,fcntl.F_ADD_SEALS,
                    fcntl.F_SEAL_WRITE|fcntl.F_SEAL_GROW|fcntl.F_SEAL_SHRINK|fcntl.F_SEAL_SEAL)
        self.pid=os.getpid(); self.start=_proc_start(self.pid)
    def _key(self)->bytes:
        if self.keyfd<0: raise AuthorizationError("authorization store is closed")
        return os.pread(self.keyfd,32,0)
    def close(self):
        for name in ("keyfd","dirfd"):
            fd=getattr(self,name,-1)
            if fd>=0: os.close(fd); setattr(self,name,-1)
    def mint(self, claims:Mapping[str,object]) -> str:
        token=secrets.token_hex(32); body={"schema":AUTH_SCHEMA,"campaign_id":self.campaign_id,"readiness_nonce":self.nonce,"token":token,"coordinator_pid":self.pid,"coordinator_start":self.start,"claims":dict(claims)}
        body["mac"]=hmac.new(self._key(),canonical_bytes(body),hashlib.sha256).hexdigest()
        raw=canonical_bytes(body); name=f"launch-{token}.json"; fd=os.open(name,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600,dir_fd=self.dirfd)
        try: os.write(fd,raw); os.fsync(fd)
        finally: os.close(fd)
        os.fsync(self.dirfd)
        return token
    def consume(self,token:str,expected:Mapping[str,object]) -> None:
        if not SHA256.fullmatch(token) or os.getpid()!=self.pid or _proc_start(self.pid)!=self.start: raise AuthorizationError("authorization cannot cross restart")
        name=f"launch-{token}.json"; used=f"consumed-{token}.json"
        try:
            fd=os.open(name,os.O_RDONLY|os.O_NOFOLLOW,dir_fd=self.dirfd)
            try:
                info=os.fstat(fd); raw=os.read(fd,256*1024+1)
                if not stat.S_ISREG(info.st_mode) or info.st_nlink!=1 or stat.S_IMODE(info.st_mode)!=0o600 or len(raw)>256*1024: raise AuthorizationError("authorization record is unsafe")
                data=json.loads(raw); mac=data.pop("mac",None)
                if not isinstance(mac,str) or not hmac.compare_digest(mac,hmac.new(self._key(),canonical_bytes(data),hashlib.sha256).hexdigest()): raise AuthorizationError("authorization MAC is invalid")
                if data.get("campaign_id")!=self.campaign_id or data.get("readiness_nonce")!=self.nonce or data.get("claims")!=dict(expected): raise AuthorizationError("authorization binding mismatch")
            finally: os.close(fd)
            # Atomic rename is the durable consume point.  A crash after this
            # point cannot make the token reusable after restart.
            os.rename(name,used,src_dir_fd=self.dirfd,dst_dir_fd=self.dirfd)
            os.fsync(self.dirfd)
        except FileNotFoundError as exc: raise AuthorizationError("authorization was replayed or is absent") from exc
        except (OSError,ValueError) as exc: raise AuthorizationError(f"authorization record unavailable: {exc}") from exc


def _open_locked_authorization_store(root:Path, namespace:str, campaign_id:str,
                                     nonce:str, lock_fd:int,
                                     readiness_document:Mapping[str,object])->AuthorizationStore:
    """Internal coordinator mint requiring lock plus complete readiness."""
    return AuthorizationStore(root,namespace,campaign_id,nonce,lock_fd,
                              readiness_document)


def _proc_start(pid:int)->str:
    try: return Path(f"/proc/{pid}/stat").read_text().split()[21]
    except (OSError,IndexError): raise AuthorizationError("coordinator process identity unavailable")
