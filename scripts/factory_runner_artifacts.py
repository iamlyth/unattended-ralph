#!/usr/bin/env python3
"""Strict retained-artifact protocol shared by runner endpoint and client."""
from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import tempfile
from dataclasses import dataclass

PROTOCOL = "factory-runner-artifacts/v1"
MAX_ARTIFACTS = 64
MAX_ARTIFACT_FILE = 8 * 1024 * 1024
MAX_ARTIFACT_BYTES = 48 * 1024 * 1024
PATH_RE = re.compile(r"^[a-z0-9][a-z0-9._/-]*$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
MEDIA_TYPES = {"application/json", "text/plain", "image/png", "image/svg+xml", "application/yaml"}

class ArtifactError(ValueError):
    pass

@dataclass
class HeldArtifacts:
    """Root-owned immutable copies used by analyzers, signing, and export.

    The candidate paths are opened once by ``collect``.  These copies are then
    created from the returned bytes, never by reopening candidate paths.
    """
    root: Path
    descriptors: list[dict]
    payload: list[dict]
    fds: dict[str, int]

    def close(self) -> None:
        for fd in self.fds.values():
            try: os.close(fd)
            except OSError: pass
        self.fds.clear()
        import shutil
        shutil.rmtree(self.root, ignore_errors=True)


def hold(descriptors: list[dict], payload: list[dict], parent: Path) -> HeldArtifacts:
    """Seal exact collected bytes in root-owned files and retained descriptors."""
    decoded = decode_payload(payload, descriptors)
    root = Path(tempfile.mkdtemp(prefix="held-", dir=parent))
    root.chmod(0o700)
    fds: dict[str, int] = {}
    try:
        for descriptor, data in decoded:
            rel = canonical_path(descriptor["path"])
            directory = root.joinpath(*rel.parts[:-1])
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            path = root.joinpath(*rel.parts)
            fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o400)
            try:
                os.write(fd, data); os.fsync(fd); os.fchmod(fd, 0o400)
                before = os.fstat(fd)
                if before.st_uid != os.geteuid() or before.st_nlink != 1 or before.st_size != len(data):
                    raise ArtifactError("held artifact inode is unsafe")
                os.lseek(fd, 0, os.SEEK_SET)
                fds[descriptor["path"]] = fd
            except Exception:
                os.close(fd); raise
        for directory, children, _ in os.walk(root, topdown=False):
            for child in children: (Path(directory)/child).chmod(0o500)
        root.chmod(0o500)
        return HeldArtifacts(root, list(descriptors), list(payload), fds)
    except Exception:
        for fd in fds.values():
            try: os.close(fd)
            except OSError: pass
        import shutil
        shutil.rmtree(root, ignore_errors=True)
        raise

def canonical_path(value: str) -> PurePosixPath:
    if not isinstance(value, str) or len(value.encode()) > 240 or not PATH_RE.fullmatch(value):
        raise ArtifactError("artifact path is not canonical")
    path = PurePosixPath(value)
    if path.is_absolute() or not path.parts or any(p in ("", ".", "..") for p in path.parts):
        raise ArtifactError("artifact path escapes its root")
    if value != path.as_posix() or value.casefold() != value:
        raise ArtifactError("artifact path is not canonical lowercase POSIX")
    return path

def canonical_descriptors_bytes(descriptors: list[dict]) -> bytes:
    return (json.dumps(descriptors, sort_keys=True, separators=(",", ":")) + "\n").encode()

def descriptors_digest(descriptors: list[dict]) -> str:
    return hashlib.sha256(canonical_descriptors_bytes(descriptors)).hexdigest()

def validate_descriptors(descriptors: object, capabilities: list[str]) -> tuple[int, str]:
    if not isinstance(descriptors, list) or len(descriptors) > MAX_ARTIFACTS:
        raise ArtifactError("artifact count exceeds protocol limit")
    seen: set[str] = set(); folded: set[str] = set(); total = 0
    previous = ""
    for item in descriptors:
        if not isinstance(item, dict) or set(item) != {"path","capability","media_type","type","mode","size","sha256"}:
            raise ArtifactError("artifact descriptor fields are invalid")
        value = canonical_path(item["path"]).as_posix()
        if value <= previous or value in seen or value.casefold() in folded:
            raise ArtifactError("artifact paths are duplicate, case-ambiguous, or unordered")
        previous=value; seen.add(value); folded.add(value.casefold())
        if item["capability"] not in capabilities or value.split("/",1)[0] != item["capability"]:
            raise ArtifactError("artifact capability ownership mismatch")
        if item["media_type"] not in MEDIA_TYPES or item["type"] != "file" or item["mode"] not in (0o600,0o640,0o644):
            raise ArtifactError("artifact type/media/mode is invalid")
        # SVG is permitted only under its exact suffix and exact MIME.  This
        # prevents a text/JSON artifact from acquiring active-image semantics
        # merely by changing one descriptor field.
        if (value.endswith(".svg")) != (item["media_type"] == "image/svg+xml"):
            raise ArtifactError("SVG artifact suffix/media binding is invalid")
        if type(item["size"]) is not int or item["size"] < 0 or item["size"] > MAX_ARTIFACT_FILE:
            raise ArtifactError("artifact size exceeds protocol limit")
        if not isinstance(item["sha256"],str) or not SHA256_RE.fullmatch(item["sha256"]):
            raise ArtifactError("artifact digest is invalid")
        total += item["size"]
        if total > MAX_ARTIFACT_BYTES: raise ArtifactError("aggregate artifact size exceeds protocol limit")
    return total, descriptors_digest(descriptors)

def collect(root: Path, capabilities: list[str], requirements: dict[str,dict], *, expected_uid: int | None = None) -> tuple[list[dict], list[dict]]:
    """Open every approved file no-follow, reject hostile inode/layout races, return held bytes.

    The privileged broker passes the dropped runner UID explicitly; ordinary
    unprivileged callers default to their real UID.  Ownership is never inferred
    from the broker's effective root identity.
    """
    descriptors=[]; payload=[]
    owner = os.getuid() if expected_uid is None else expected_uid
    root_info=root.lstat()
    if not stat.S_ISDIR(root_info.st_mode) or stat.S_ISLNK(root_info.st_mode) or root_info.st_uid != owner or root_info.st_mode & 0o077:
        raise ArtifactError("artifact root ownership/mode is unsafe")
    for capability in sorted(capabilities):
        cap=root/capability; req=requirements.get(capability,{})
        allowed=req.get("files", {})
        required=set(req.get("required", []))
        if not cap.exists():
            if required: raise ArtifactError(f"required artifact directory absent for {capability}")
            continue
        ci=cap.lstat()
        if not stat.S_ISDIR(ci.st_mode) or stat.S_ISLNK(ci.st_mode) or ci.st_uid != owner or ci.st_mode & 0o077:
            raise ArtifactError("artifact capability directory is unsafe")
        found=set()
        cap_fd=os.open(cap,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW|os.O_CLOEXEC)
        try: names=sorted(os.listdir(cap_fd))
        except Exception:
            os.close(cap_fd); raise
        if len(descriptors) + len(names) > MAX_ARTIFACTS:
            os.close(cap_fd)
            raise ArtifactError("artifact count exceeds protocol limit before open")
        for name in names:
            rel=f"{capability}/{name}"; canonical_path(rel)
            if name not in allowed: raise ArtifactError(f"unapproved artifact: {rel}")
            li=os.stat(name,dir_fd=cap_fd,follow_symlinks=False)
            if not stat.S_ISREG(li.st_mode) or stat.S_ISLNK(li.st_mode) or li.st_nlink != 1 or li.st_uid != owner or stat.S_IMODE(li.st_mode)&0o022:
                raise ArtifactError(f"artifact is not a unique owned regular file: {rel}")
            # Enforce both per-file and cumulative bounds from no-follow
            # metadata before opening or reading candidate-controlled bytes.
            if li.st_size > MAX_ARTIFACT_FILE or sum(x["size"] for x in descriptors) + li.st_size > MAX_ARTIFACT_BYTES:
                raise ArtifactError(f"artifact bounds exceeded before open: {rel}")
            fd=os.open(name, os.O_RDONLY|os.O_NOFOLLOW|os.O_CLOEXEC, dir_fd=cap_fd)
            try:
                before=os.fstat(fd)
                if (before.st_dev,before.st_ino)!=(li.st_dev,li.st_ino):
                    raise ArtifactError(f"artifact replaced while opening: {rel}")
                if before.st_size > MAX_ARTIFACT_FILE or (before.st_size and before.st_blocks*512 < before.st_size):
                    raise ArtifactError(f"artifact is oversized or sparse: {rel}")
                chunks=[]; remaining=before.st_size
                while remaining:
                    chunk=os.read(fd,min(65536,remaining))
                    if not chunk: raise ArtifactError(f"artifact shortened during read: {rel}")
                    chunks.append(chunk); remaining-=len(chunk)
                if os.read(fd,1): raise ArtifactError(f"artifact grew during read: {rel}")
                after=os.fstat(fd)
                if (before.st_dev,before.st_ino,before.st_size,before.st_mtime_ns,before.st_ctime_ns)!=(after.st_dev,after.st_ino,after.st_size,after.st_mtime_ns,after.st_ctime_ns):
                    raise ArtifactError(f"artifact mutated during read: {rel}")
                data=b"".join(chunks); media=allowed[name]
                if media == "image/svg+xml" and (not name.endswith(".svg") or b"<svg" not in data[:4096].lower()):
                    raise ArtifactError(f"SVG artifact has invalid suffix/content: {rel}")
                descriptor={"path":rel,"capability":capability,"media_type":media,"type":"file","mode":0o600,"size":len(data),"sha256":hashlib.sha256(data).hexdigest()}
                descriptors.append(descriptor); payload.append({"path":rel,"data_b64":base64.b64encode(data).decode("ascii")}); found.add(name)
            finally: os.close(fd)
        os.close(cap_fd)
        missing=required-found
        if missing: raise ArtifactError(f"required artifacts absent for {capability}: {sorted(missing)}")
    descriptors.sort(key=lambda x:x["path"]); payload.sort(key=lambda x:x["path"])
    validate_descriptors(descriptors,capabilities)
    return descriptors,payload

def decode_payload(payload: object, descriptors: list[dict]) -> list[tuple[dict,bytes]]:
    if not isinstance(payload,list) or len(payload)!=len(descriptors): raise ArtifactError("artifact payload count mismatch")
    result=[]
    for item,desc in zip(payload,descriptors):
        if not isinstance(item,dict) or set(item)!={"path","data_b64"} or item["path"]!=desc["path"] or not isinstance(item["data_b64"],str):
            raise ArtifactError("artifact payload order/framing mismatch")
        if len(item["data_b64"]) > ((MAX_ARTIFACT_FILE+2)//3)*4:
            raise ArtifactError("artifact base64 exceeds bound")
        try: data=base64.b64decode(item["data_b64"],validate=True)
        except Exception as exc: raise ArtifactError("artifact base64 is invalid") from exc
        if len(data)!=desc["size"] or hashlib.sha256(data).hexdigest()!=desc["sha256"]:
            raise ArtifactError("artifact bytes do not match signed descriptor")
        if desc["media_type"] == "image/svg+xml" and b"<svg" not in data[:4096].lower():
            raise ArtifactError("SVG artifact content is invalid")
        result.append((desc,data))
    return result
