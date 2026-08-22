#!/usr/bin/env python3
"""Visual-audit provenance manifest (stdlib only).

Binds every captured screenshot to exact Git state and capture metadata so a
review finding can always be traced back to the exact committed tree it was
captured from. A provenance mismatch (wrong commit, wrong tree, missing
image, altered hash) invalidates every dependent finding.

Usage:
  visual-audit-provenance.py manifest --out DIR --commit C --tree T
      Write a manifest describing the images in DIR (files named
      <state_id>.png or <state_id>-<crop>.png). Emits JSON to stdout and
      writes <DIR>/provenance.json atomically. The environment binding is a
      fixed lowercase SHA-256 of the committed .factory/environment.toml
      bytes (or the canonical marker "absent" when the file is not committed)
      computed here from the repository; arbitrary caller text is never
      accepted.
  visual-audit-provenance.py verify --out DIR
      Validate <DIR>/provenance.json: schema, commit/tree fields, the
      environment binding, and that every listed image exists with a matching
      SHA-256. Exit 0 on success.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
from pathlib import Path
import secrets
import stat
import subprocess
import sys

SCHEMA = "ralph-visual-audit-provenance/v1"
STATE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
SHA1 = re.compile(r"^[0-9a-f]{40}$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
ENVIRONMENT_REL = ".factory/environment.toml"
# Explicit canonical marker used only when the generic template contract has
# no committed environment.toml (it is committed in every boilerplate repo,
# so the marker is the exceptional absent case, never arbitrary text).
ENVIRONMENT_ABSENT = "absent"


def die(message: str) -> "NoReturn":
    raise SystemExit(f"visual-audit-provenance: {message}")


def git_stdout(root: Path, *argv: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(root), *argv], capture_output=True, check=False)


def is_tracked(root: Path, path: Path) -> bool:
    try:
        rel = path.relative_to(root)
    except ValueError:
        return False
    result = git_stdout(root, "ls-files", "--error-unmatch", "--", str(rel))
    return result.returncode == 0


def environment_binding(root: Path) -> str:
    """Fixed lowercase SHA-256 of the committed environment.toml bytes.

    Never accepts caller text: the blob is read from the committed Git object
    (HEAD:.factory/environment.toml). A present-but-untracked file is an error;
    a genuinely absent file binds to the canonical marker.
    """
    path = root / ENVIRONMENT_REL
    if path.is_symlink():
        die("environment.toml must not be a symlink")
    if not is_tracked(root, path):
        if path.exists():
            die("environment.toml exists but is not committed; it must be tracked")
        return ENVIRONMENT_ABSENT
    result = git_stdout(root, "show", f"HEAD:{ENVIRONMENT_REL}")
    if result.returncode != 0:
        die("cannot read the committed environment.toml blob")
    return hashlib.sha256(result.stdout).hexdigest()


def image_entries(directory: Path) -> list[dict]:
    entries: list[dict] = []
    for path in sorted(directory.glob("*.png")):
        name = path.name
        base = name[:-4] if name.endswith(".png") else name
        state_id = base
        if not STATE_NAME.fullmatch(state_id):
            die(f"image name is not bound to a valid state id: {name}")
        data = path.read_bytes()
        entries.append({
            "state_id": state_id,
            "file": name,
            "sha256": hashlib.sha256(data).hexdigest(),
            "size": len(data),
        })
    return entries


def atomic_write_json(path: Path, data: dict) -> None:
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(8)}")
    raw = (json.dumps(data, indent=2, sort_keys=True) + "\n").encode()
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(fd, raw)
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(temporary, path)
    path.chmod(0o600)


def cmd_manifest(args: argparse.Namespace) -> int:
    out = Path(args.out)
    if out.exists():
        if not out.is_dir() or out.is_symlink():
            die("manifest output is not a plain directory")
        if out.stat().st_mode & 0o077:
            die("manifest output directory must not be group/world accessible")
    else:
        out.mkdir(mode=0o700, parents=True)
    if not SHA1.fullmatch(args.commit):
        die("commit must be a 40-hex Git object id")
    if not SHA1.fullmatch(args.tree):
        die("tree must be a 40-hex Git object id")
    images = image_entries(out)
    if not images:
        die("no capture images found in output directory")
    # Environment binding is computed from the committed repo bytes; caller
    # text (e.g. an --environment flag) is never accepted.
    environment_blob = environment_binding(Path.cwd())
    manifest = {
        "schema": SCHEMA,
        "commit": args.commit,
        "tree": args.tree,
        "environment_blob": environment_blob,
        "images": images,
    }
    atomic_write_json(out / "provenance.json", manifest)
    print(json.dumps(manifest, indent=2, sort_keys=True))
    return 0


def cmd_verify(args: argparse.Namespace) -> int:
    out = Path(args.out)
    if not out.is_dir() or out.is_symlink():
        die("manifest output is not a plain directory")
    path = out / "provenance.json"
    if not path.is_file() or path.is_symlink():
        die("provenance.json is missing or unsafe")
    if path.stat().st_uid != os.getuid() or path.stat().st_nlink != 1:
        die("provenance.json has unsafe ownership or link count")
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        die(f"provenance.json is invalid: {type(exc).__name__}")
    if not isinstance(manifest, dict) or manifest.get("schema") != SCHEMA:
        die("provenance.json schema is invalid")
    commit = manifest.get("commit")
    tree = manifest.get("tree")
    if not isinstance(commit, str) or not SHA1.fullmatch(commit):
        die("provenance.json commit is invalid")
    if not isinstance(tree, str) or not SHA1.fullmatch(tree):
        die("provenance.json tree is invalid")
    environment = manifest.get("environment_blob")
    if not isinstance(environment, str):
        die("provenance.json environment_blob is invalid")
    if environment != ENVIRONMENT_ABSENT and not SHA256.fullmatch(environment):
        die("provenance.json environment_blob must be the committed environment.toml sha256 or the canonical absent marker")
    binding = environment_binding(Path.cwd())
    if environment != binding:
        die(f"provenance.json environment binding mismatch (manifest {environment[:16]!r} != committed {binding[:16]!r})")
    images = manifest.get("images")
    if not isinstance(images, list) or not images:
        die("provenance.json images array is invalid")
    seen: set[str] = set()
    for entry in images:
        if not isinstance(entry, dict) or set(entry) != {"state_id", "file", "sha256", "size"}:
            die("provenance.json image entry schema is invalid")
        state_id = entry["state_id"]
        file_name = entry["file"]
        digest = entry["sha256"]
        size = entry["size"]
        if not isinstance(state_id, str) or not STATE_NAME.fullmatch(state_id):
            die("provenance.json image state_id is invalid")
        if not isinstance(file_name, str) or "/" in file_name or ".." in file_name:
            die("provenance.json image file is invalid")
        if not isinstance(digest, str) or not SHA256.fullmatch(digest):
            die("provenance.json image sha256 is invalid")
        if not isinstance(size, int) or isinstance(size, bool) or size <= 0:
            die("provenance.json image size is invalid")
        if file_name in seen:
            die(f"provenance.json duplicates image {file_name}")
        seen.add(file_name)
        image_path = out / file_name
        if not image_path.is_file() or image_path.is_symlink():
            die(f"provenance image missing or unsafe: {file_name}")
        if image_path.stat().st_uid != os.getuid() or image_path.stat().st_nlink != 1:
            die(f"provenance image has unsafe ownership or link count: {file_name}")
        actual = hashlib.sha256(image_path.read_bytes()).hexdigest()
        if actual != digest:
            die(f"provenance image hash mismatch: {file_name}")
        if image_path.stat().st_size != size:
            die(f"provenance image size mismatch: {file_name}")
    print(f"visual-audit-provenance: valid ({len(images)} images at commit {commit[:12]})")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Visual-audit provenance manifest tool")
    sub = parser.add_subparsers(dest="command", required=True)
    m = sub.add_parser("manifest")
    m.add_argument("--out", required=True)
    m.add_argument("--commit", required=True)
    m.add_argument("--tree", required=True)
    m.set_defaults(func=cmd_manifest)
    v = sub.add_parser("verify")
    v.add_argument("--out", required=True)
    v.set_defaults(func=cmd_verify)
    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
