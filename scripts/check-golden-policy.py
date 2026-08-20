#!/usr/bin/env python3
"""Enforce the protected golden baseline policy.

`.factory/golden-policy.json` declares the golden directories and the review
manifest. Rules:

- every working-tree change to a tracked golden (modified, new, or deleted)
  requires exactly one reviewed entry in the manifest for that exact path;
- the entry binds the before hash (committed golden at HEAD) and the after
  hash (working-tree content) with an explicit human reviewer identity and a
  reason; documentation alone never substitutes;
- when the tree is clean, every manifest entry must correspond to a real
  golden transition in Git history (a commit C where the path's blob at C
  hashes to `after` and its blob at C^ hashes to `before`), so the review
  record remains durable and cannot drift from the baselines;
- manifest entries that match no real working-tree change and no real
  committed transition are stale and rejected;
- with no golden changes and no history transitions the manifest must be
  empty;
- the policy must declare that generation cannot overwrite active goldens
  (enforced operationally by `scripts/generate-golden.sh`, which refuses to
  run without the review manifest and reviewer identity environment).

Usage:
  scripts/check-golden-policy.py [--root ROOT]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
POLICY_DEFAULT = ".factory/golden-policy.json"
REVIEW_DEFAULT = ".factory/golden-review.json"
DATE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")


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
    seen: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict):
            fail("every review manifest entry must be an object")
        expected = {"path", "before_sha256", "after_sha256", "reason", "reviewer", "human", "reviewed_at"}
        if set(entry) != expected:
            fail("review manifest entry fields do not match the schema")
        entry_path = entry["path"]
        if not isinstance(entry_path, str) or not entry_path or Path(entry_path).is_absolute():
            fail(f"review manifest entry path is invalid: {entry_path!r}")
        if entry_path in seen:
            fail(f"review manifest has duplicate entries for {entry_path}")
        seen.add(entry_path)
        for field in ("before_sha256", "after_sha256"):
            value = entry[field]
            if value is not None and not (isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value)):
                fail(f"review manifest entry {entry_path} {field} must be a SHA-256 or null")
        if not isinstance(entry["reason"], str) or not entry["reason"].strip():
            fail(f"review manifest entry {entry_path} requires a reason")
        if not isinstance(entry["reviewer"], str) or not entry["reviewer"].strip():
            fail(f"review manifest entry {entry_path} requires a reviewer identity")
        if entry.get("human") is not True:
            fail(f"review manifest entry {entry_path} requires human: true")
        if not isinstance(entry["reviewed_at"], str) or not DATE.fullmatch(entry["reviewed_at"]):
            fail(f"review manifest entry {entry_path} requires an ISO-8601 reviewed_at")
    return entries


def transition_verified(root: Path, entry: dict) -> bool:
    """A manifest entry is real when some commit in history performed it."""
    entry_path = entry["path"]
    result = git(root, "log", "--format=%H", "--", entry_path)
    if result.returncode:
        return False
    commits = [line for line in result.stdout.splitlines() if line]
    for commit in commits:
        parents = git(root, "rev-parse", f"{commit}^").stdout.strip() if commit else ""
        parent = parents.splitlines()[0] if parents else None
        after = blob_sha256(root, commit, entry_path)
        before = blob_sha256(root, parent, entry_path) if parent else None
        if after == entry["after_sha256"] and before == entry["before_sha256"]:
            return True
    return False


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
    entry_by_path = {entry["path"]: entry for entry in entries}

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

    if not changed:
        for entry in entries:
            if not transition_verified(root, entry):
                fail(
                    f"golden review manifest entry {entry['path']} matches no real "
                    f"transition in Git history (stale or forged)"
                )
        print(f"golden-policy: valid ({len(entries)} durable reviewed transition(s))")
        return 0

    for path, hashes in sorted(changed.items()):
        entry = entry_by_path.get(path)
        if entry is None:
            fail(f"golden baseline changed without a review manifest entry: {path}")
        if entry["before_sha256"] != hashes["before"]:
            fail(
                f"golden review entry {path} before hash {entry['before_sha256']} "
                f"does not match HEAD {hashes['before']}"
            )
        if entry["after_sha256"] != hashes["after"]:
            fail(
                f"golden review entry {path} after hash {entry['after_sha256']} "
                f"does not match the working tree {hashes['after']}"
            )
    for entry in entries:
        if entry["path"] not in changed:
            fail(f"golden review manifest has a stale entry: {entry['path']}")

    print(f"golden-policy: ok ({len(changed)} reviewed golden change(s))")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
