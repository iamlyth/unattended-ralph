#!/usr/bin/env python3
"""Create the canonical, deterministic factory-runner source install manifest.

This tool is intentionally unprivileged.  It obtains every byte from Git's
object database, never from the work tree, and refuses a dirty/untracked tree.
The resulting manifest is suitable for detached signing with ssh-keygen -Y.
"""
from __future__ import annotations
import argparse, base64, hashlib, json, os, pathlib, re, stat, subprocess, sys

SCHEMA = "factory-runner-install-manifest/v2"
ALLOWED_MODES = {"100644": 0o644, "100755": 0o755}
REQUIRED = {
    "scripts/install-factory-runner-v2.sh",
    "scripts/factory-runner-root-bootstrap",
    "scripts/factory-runner-broker.py",
    "scripts/factory-runner-signer.py",
    "scripts/factory-runner-server.py",
    "scripts/factory_runner_policy.py",
    "scripts/factory_runner_artifacts.py",
    "scripts/factory_runner_authority.py",
    "scripts/build-runner-probe-authority.py",
    ".factory/schemas/factory-runner-policy-v1.schema.json",
    ".factory/schemas/factory-runner-receipt-v3.schema.json",
    ".factory/schemas/factory-runner-aggregate-v4.schema.json",
    ".factory/schemas/factory-runner-artifacts-v1.schema.json",
}

class ManifestError(RuntimeError): pass

def git(root: pathlib.Path, *args: str, data: bytes | None = None) -> bytes:
    p = subprocess.run(["git", "-C", os.fspath(root), *args], input=data,
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if p.returncode:
        raise ManifestError(p.stderr.decode("utf-8", "replace").strip() or "git failed")
    return p.stdout

def build(root: pathlib.Path, revision: str) -> dict:
    root = root.resolve()
    if git(root, "status", "--porcelain=v1", "-z", "--untracked-files=all"):
        raise ManifestError("authority source must be clean; dirty and untracked files are forbidden")
    commit = git(root, "rev-parse", "--verify", f"{revision}^{{commit}}").decode().strip()
    if revision == "HEAD" and commit != git(root, "rev-parse", "HEAD").decode().strip():
        raise ManifestError("HEAD changed during manifest generation")
    tree = git(root, "rev-parse", f"{commit}^{{tree}}").decode().strip()
    if not re.fullmatch(r"[0-9a-f]{40,64}", commit) or not re.fullmatch(r"[0-9a-f]{40,64}", tree):
        raise ManifestError("unsupported Git object identifier")
    raw = git(root, "ls-tree", "-rz", "--full-tree", "-r", commit)
    files: dict[str, dict] = {}
    for record in raw.split(b"\0"):
        if not record: continue
        meta, path_b = record.split(b"\t", 1)
        mode_b, typ_b, oid_b = meta.split(b" ")
        try: path = path_b.decode("utf-8")
        except UnicodeDecodeError as exc: raise ManifestError("non-UTF-8 tree path is forbidden") from exc
        mode, typ, oid = mode_b.decode(), typ_b.decode(), oid_b.decode()
        if mode not in ALLOWED_MODES or typ != "blob":
            kind = "symlink" if mode == "120000" else "gitlink/submodule" if mode == "160000" else f"mode {mode}"
            raise ManifestError(f"unsupported tree member {path!r}: {kind}")
        body = git(root, "cat-file", "blob", oid)
        actual_oid = git(root, "hash-object", "--stdin", data=body).decode().strip()
        if actual_oid != oid: raise ManifestError(f"Git blob mismatch: {path}")
        files[path] = {"blob": oid, "sha256": hashlib.sha256(body).hexdigest(), "mode": ALLOWED_MODES[mode], "size": len(body)}
    missing = REQUIRED - files.keys()
    if missing: raise ManifestError("install closure is incomplete: " + ", ".join(sorted(missing)))
    commit_object = git(root, "cat-file", "commit", commit)
    # The exact member set and descriptors form the source Merkle root.  The
    # Git tree remains the primary Merkle commitment; this second digest makes
    # bootstrap validation independent of Git's hash algorithm.
    closure = hashlib.sha256()
    for path, desc in sorted(files.items()):
        closure.update(path.encode()+b"\0")
        closure.update(json.dumps(desc, sort_keys=True, separators=(",", ":")).encode()+b"\0")
    return {"schema": SCHEMA, "commit": commit, "tree": tree,
            "commit_object_b64": base64.b64encode(commit_object).decode(),
            "source_merkle_sha256": closure.hexdigest(), "files": files,
            "installer": "scripts/install-factory-runner-v2.sh",
            "authority_builder": "scripts/build-runner-probe-authority.py",
            "symlinks": "reject", "submodules": "reject"}

def main() -> int:
    ap=argparse.ArgumentParser(); ap.add_argument("--source", type=pathlib.Path, required=True)
    ap.add_argument("--revision", default="HEAD"); ap.add_argument("--output", type=pathlib.Path, required=True)
    ns=ap.parse_args()
    try: value=build(ns.source,ns.revision)
    except ManifestError as exc: print(f"manifest: {exc}",file=sys.stderr);return 1
    encoded=(json.dumps(value,sort_keys=True,separators=(",", ":"))+"\n").encode()
    ns.output.parent.mkdir(parents=True,exist_ok=True)
    fd=os.open(ns.output,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_CLOEXEC,0o600)
    try: os.write(fd,encoded);os.fsync(fd)
    finally: os.close(fd)
    print(hashlib.sha256(encoded).hexdigest())
    return 0
if __name__=="__main__": raise SystemExit(main())
