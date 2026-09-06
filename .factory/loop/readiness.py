#!/usr/bin/env python3
"""Generic fail-closed round-zero readiness and campaign launch authority."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import stat
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
    "runner-aggregate": ("./scripts/check-factory-runner-evidence.py",),
    "capability-evidence": ("./scripts/check-capability-evidence.py",),
    "conformance-planning": ("./scripts/validate-conformance.py", "planning", ".factory/artifacts/conformance.json"),
    "boilerplate-verification": ("./scripts/verify-boilerplate.sh",),
    "final-acceptance": ("./scripts/final-gate.sh", "--implementation"),
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
    human = value["human_approval"]
    if human is not None:
        hfields={"required","approval_schema","approval_path","signature_path","signature_namespace","trust_scope","checklist","captures"}
        if not isinstance(human,dict) or set(human)!=hfields or human["required"] is not True:
            raise ReadinessError("human approval policy is malformed")
        for key in ("approval_schema","signature_namespace","trust_scope"):
            if not isinstance(human[key],str) or not IDENT.fullmatch(human[key]): raise ReadinessError(f"human {key} is invalid")
        _safe_relative(human["approval_path"],"human approval path"); _safe_relative(human["signature_path"],"human signature path")
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


def validate_aggregate(value: object, policy: Mapping[str,object], *, accepted_commit: str, tree: str, environment_blob: str) -> str:
    """Canonical class/capability interface; input ordering has no authority."""
    if not isinstance(value,dict) or set(value)!={"schema","commit","tree","environment_blob","runners"} or value.get("schema") not in {"factory-runner-aggregate/v1","factory-runner-aggregate/v4"}:
        raise ReadinessFindings("runner aggregate schema is invalid")
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


def validate_human_authority(policy: Mapping[str, object], approval_raw: bytes | None, trust_raw: bytes | None, *, accepted_commit: str, accepted_tree: str, blob_at: Callable[[str, str], bytes]) -> str:
    """Validate project-adapted external human authority without product assumptions."""
    human = policy.get("human_approval")
    if human is None:
        return digest({"required": False})
    if approval_raw is None or trust_raw is None:
        raise HumanAuthorityBlocked("required external human authority is missing")
    try:
        approval=json.loads(approval_raw); trust=json.loads(trust_raw)
    except (UnicodeError,ValueError) as exc:
        raise HumanAuthorityBlocked(f"external human authority is malformed: {exc}") from exc
    if not isinstance(human,Mapping): raise HumanAuthorityBlocked("human policy is malformed")
    if not isinstance(trust,dict) or set(trust)!={"schema","status","scope","keys"} or trust.get("schema")!="factory-human-trust/v1" or trust.get("status")!="active" or trust.get("scope")!=human["trust_scope"] or not isinstance(trust.get("keys"),list) or not trust["keys"]:
        raise HumanAuthorityBlocked("external human trust is absent, inactive, or wrong-scope")
    required={"schema","status","commit","tree","checklist","captures","reviewer","signature_sha256"}
    if not isinstance(approval,dict) or set(approval)!=required or approval.get("schema")!=human["approval_schema"] or approval.get("status")!="approved" or approval.get("commit")!=accepted_commit or approval.get("tree")!=accepted_tree or approval.get("checklist")!=human["checklist"] or not SHA256.fullmatch(str(approval.get("signature_sha256",""))):
        raise HumanAuthorityBlocked("human approval binding/checklist is incomplete")
    reviewers=[k for k in trust["keys"] if isinstance(k,dict) and set(k)=={"id","public_key"} and k.get("id")==approval.get("reviewer")]
    if len(reviewers)!=1: raise HumanAuthorityBlocked("human reviewer is not uniquely trusted")
    captures=approval.get("captures")
    if not isinstance(captures,list) or len(captures)!=len(human["captures"]): raise HumanAuthorityBlocked("human capture set is incomplete")
    by_id={item.get("id"):item for item in captures if isinstance(item,dict)}
    for binding in human["captures"]:
        item=by_id.get(binding["id"])
        if not isinstance(item,dict): raise HumanAuthorityBlocked(f"human capture {binding['id']} is missing")
        path=_safe_relative(item.get(binding["path_field"]),"human capture")
        if hashlib.sha256(blob_at(accepted_commit,path)).hexdigest()!=item.get(binding["digest_field"]): raise HumanAuthorityBlocked(f"human capture {binding['id']} digest is stale")
    return digest(approval_raw+b"\0"+trust_raw)


def readiness_bindings(*, accepted_commit:str, accepted_tree:str, current_commit:str, current_tree:str, config_sha256:str, environment_sha256:str, specification_sha256:str, plan_sha256:str, contracts_sha256:str, policy_sha256:str, trust_sha256:str, install_manifest_sha256:str) -> dict:
    result=locals().copy()
    for key,value in result.items():
        pattern=SHA1 if key.endswith("commit") or key.endswith("tree") else SHA256
        if not isinstance(value,str) or not pattern.fullmatch(value): raise ReadinessError(f"binding {key} is malformed")
    return result


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
    """FD-backed coordinator secret plus durable one-use, restart-bound records."""
    def __init__(self, root:Path, namespace:str, campaign_id:str, nonce:str, marker:object):
        if not IDENT.fullmatch(campaign_id) or not SHA256.fullmatch(nonce): raise AuthorizationError("authorization identity is invalid")
        self.root=Path(root).absolute(); self.namespace=_safe_relative(namespace,"campaign namespace"); self.campaign_id=campaign_id; self.nonce=nonce; self.marker=marker
        directory=self.root/self.namespace; directory.mkdir(mode=0o700,parents=True,exist_ok=True)
        self.dirfd=os.open(directory,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW|getattr(os,"O_CLOEXEC",0))
        self.key=secrets.token_bytes(32); self.pid=os.getpid(); self.start=_proc_start(self.pid)
    def close(self):
        if getattr(self,"dirfd",-1)>=0: os.close(self.dirfd); self.dirfd=-1
    def mint(self, claims:Mapping[str,object]) -> str:
        token=secrets.token_hex(32); body={"schema":AUTH_SCHEMA,"campaign_id":self.campaign_id,"readiness_nonce":self.nonce,"token":token,"coordinator_pid":self.pid,"coordinator_start":self.start,"used":False,"claims":dict(claims)}
        body["mac"]=hmac.new(self.key,canonical_bytes(body),hashlib.sha256).hexdigest()
        raw=canonical_bytes(body); name=f"launch-{token}.json"; fd=os.open(name,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600,dir_fd=self.dirfd)
        try: os.write(fd,raw); os.fsync(fd)
        finally: os.close(fd)
        return token
    def consume(self,token:str,expected:Mapping[str,object]) -> None:
        if not SHA256.fullmatch(token) or os.getpid()!=self.pid or _proc_start(self.pid)!=self.start: raise AuthorizationError("authorization cannot cross restart")
        name=f"launch-{token}.json"; fd=os.open(name,os.O_RDWR|os.O_NOFOLLOW,dir_fd=self.dirfd)
        try:
            info=os.fstat(fd); raw=os.read(fd,256*1024)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink!=1 or stat.S_IMODE(info.st_mode)!=0o600: raise AuthorizationError("authorization record is unsafe")
            data=json.loads(raw)
            mac=data.pop("mac",None)
            if not isinstance(mac,str) or not hmac.compare_digest(mac,hmac.new(self.key,canonical_bytes(data),hashlib.sha256).hexdigest()): raise AuthorizationError("authorization MAC is invalid")
            if data.get("used") is not False or data.get("campaign_id")!=self.campaign_id or data.get("readiness_nonce")!=self.nonce or data.get("claims")!=dict(expected): raise AuthorizationError("authorization replay or binding mismatch")
            data["used"]=True; data["mac"]=hmac.new(self.key,canonical_bytes(data),hashlib.sha256).hexdigest(); updated=canonical_bytes(data)
            os.lseek(fd,0,os.SEEK_SET); os.write(fd,updated); os.ftruncate(fd,len(updated)); os.fsync(fd)
        except (OSError,ValueError) as exc: raise AuthorizationError(f"authorization record unavailable: {exc}") from exc
        finally: os.close(fd)


def _proc_start(pid:int)->str:
    try: return Path(f"/proc/{pid}/stat").read_text().split()[21]
    except (OSError,IndexError): raise AuthorizationError("coordinator process identity unavailable")
