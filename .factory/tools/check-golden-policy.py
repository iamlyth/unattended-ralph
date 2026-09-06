#!/usr/bin/env python3
"""Enforce the protected golden baseline policy.

`.factory/golden-policy.json` declares the golden directories and the review
manifest. Rules (unattended mode):

- every working-tree change to a tracked golden (modified, new, or deleted)
  fails the gate: golden approval is out-of-band and non-automatable;
- the review manifest must be empty: agent-authored `human: true`, reviewer
  strings, and environment reviewer identity are never accepted as
  self-attestation (no verifiable external attestation mechanism is
  provisioned yet), so any manifest entry is rejected;
- the generic harness ships no product golden-generation command; adopting
  projects keep generation outside the unattended control plane.

Usage:
  .factory/tools/check-golden-policy.py [--root ROOT]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
POLICY_DEFAULT = ".factory/golden-policy.json"
REVIEW_DEFAULT = ".factory/golden-review.json"


def fail(message: str) -> None:
    raise SystemExit(f"golden-policy: {message}")


def git(root: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=root, text=True, stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )


def regular_json(path: Path) -> dict:
    if path.is_symlink() or not path.is_file():
        fail(f"unsafe or missing JSON file: {path}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        fail(f"invalid JSON file {path}: {exc}")
    if not isinstance(data, dict):
        fail(f"JSON file must be an object: {path}")
    return data


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def blob_sha256(root: Path, commit: str, ref: str) -> str | None:
    result = subprocess.run(
        ["git", "show", f"{commit}:{ref}"],
        cwd=root, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
    )
    if result.returncode:
        return None
    return sha256_bytes(result.stdout)


def tracked_files(root: Path, directory: str) -> dict[str, str | None]:
    result = git(root, "ls-tree", "-r", "HEAD", "--", directory)
    files: dict[str, str | None] = {}
    if result.returncode:
        return files
    for line in result.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) != 2:
            continue
        path = parts[1]
        files[path] = blob_sha256(root, "HEAD", path)
    return files


def working_files(root: Path, directory: str) -> dict[str, str | None]:
    base = root / directory
    files: dict[str, str | None] = {}
    if not base.is_dir() or base.is_symlink():
        return files
    for path in sorted(base.rglob("*")):
        if path.is_symlink() or not path.is_file():
            continue
        try:
            relative = path.relative_to(root)
        except ValueError:
            fail(f"golden path escapes the repository: {path}")
        files[str(relative)] = sha256_bytes(path.read_bytes())
    return files


def validate_review_manifest(path: Path) -> list[dict]:
    data = regular_json(path)
    if data.get("schema") != "ralph-golden-review/v1":
        fail(f"review manifest schema must be ralph-golden-review/v1: {path}")
    entries = data.get("entries")
    if not isinstance(entries, list):
        fail(f"review manifest must declare an entries array: {path}")
    if entries:
        fail(
            "golden review manifest entries are self-attestation: agent-authored "
            "`human: true`, reviewer strings, and environment reviewer identity "
            "are never accepted by unattended gates. Golden approval is "
            "out-of-band and non-automatable; no verifiable external attestation "
            "mechanism is provisioned yet, so every golden change and every "
            "manifest entry is rejected as a finding."
        )
    return entries


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", default=str(ROOT))
    args = parser.parse_args()
    root = Path(args.root).resolve()

    policy_path = root / POLICY_DEFAULT
    review_path = root / REVIEW_DEFAULT
    if policy_path.is_symlink() or not policy_path.is_file():
        fail(f"golden policy must be a regular tracked file: {policy_path}")
    if review_path.is_symlink() or not review_path.is_file():
        fail(f"golden review manifest must be a regular tracked file: {review_path}")

    policy = regular_json(policy_path)
    if policy.get("schema") != "ralph-golden-policy/v1":
        fail(f"golden policy schema must be ralph-golden-policy/v1: {policy_path}")
    generation = policy.get("generation")
    if not isinstance(generation, dict) or generation.get("allow_overwrite_of_active") is not False:
        fail("golden policy must declare generation.allow_overwrite_of_active: false")
    directories = policy.get("golden_directories")
    if not isinstance(directories, list) or not directories or not all(
        isinstance(item, str) and item and not Path(item).is_absolute() and ".." not in Path(item).parts
        for item in directories
    ):
        fail("golden policy golden_directories must be non-empty safe relative paths")
    if policy.get("review_manifest") != str(review_path.relative_to(root)):
        fail("golden policy review_manifest does not match the tracked review manifest")

    entries = validate_review_manifest(review_path)

    changed: dict[str, dict] = {}
    for directory in directories:
        tracked = tracked_files(root, directory)
        working = working_files(root, directory)
        for path in sorted(set(tracked) | set(working)):
            before = tracked.get(path)
            after = working.get(path)
            if before == after:
                continue
            changed[path] = {"before": before, "after": after}

    if changed:
        fail(
            "golden baseline changed but golden approval is out-of-band and "
            "non-automatable: no verifiable external attestation mechanism is "
            "provisioned, so the change is a finding and cannot be accepted by "
            "an unattended gate"
        )

    print(f"golden-policy: valid ({len(entries)} out-of-band review pending, goldens unchanged)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
