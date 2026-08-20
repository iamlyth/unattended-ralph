#!/usr/bin/env python3
"""Check a campaign audit report against its per-round falsification objective.

`.factory/campaign-objectives.json` maps every audit round (round r uses
objectives[(r-1) mod N]) to a product-neutral falsification objective with
objective-specific receipt categories. An audit round cannot pass without
machine receipts (or accepted runner manifests) covering every category of
that round's objective: replaying the same generic suite cannot satisfy all
rounds.

Authorization (`.factory/campaign-receipt-policy.json`):
- every objective category must exist in the tracked receipt policy, which
  binds the category to its allowlisted argv (exact argv arrays) and required
  evidence tier;
- a `[receipt: <path>]` reference satisfies a category only when the receipt's
  `tag` equals the category exactly, its recorded `argv` equals one of the
  category's allowlisted argv arrays, its `evidence_commit` equals the active
  campaign audit base, and its `coordinator_round` equals the active round
  (arbitrary commands such as a bare `true`, tag/path spoofing, stale round
  receipts, and receipts reused across rounds are rejected);
- a `[manifest: <path>]` reference satisfies a category only when the category
  allows manifests and the manifest path contains the category as one of its
  path segments;
- BLOCKED evidence lines carry no receipt/manifest and satisfy nothing;
- every required category of the round's objective must be covered by at
  least one executable-evidence line in `## Evidence reviewed`.

Usage:
  scripts/check-campaign-objectives.py [--round N] [--base COMMIT] [--report PATH] [--root ROOT]
  (the audit round and audit base come from --round/--base or
   FACTORY_CAMPAIGN_AUDIT_ROUND/FACTORY_CAMPAIGN_AUDIT_BASE)
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OBJECTIVES_DEFAULT = ROOT / ".factory/campaign-objectives.json"
POLICY_DEFAULT = ROOT / ".factory/campaign-receipt-policy.json"
REPORT_DEFAULT = ROOT / ".factory/artifacts/campaign-audit.md"
RECEIPT = re.compile(r"\[receipt:\s*([^\]]+)\]")
MANIFEST = re.compile(r"\[manifest:\s*([^\]]+)\]")
SHA1 = re.compile(r"^[0-9a-f]{40}$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
TIERS = ["unit", "simulated", "private_integration", "installed", "real_system", "human"]


def fail(message: str) -> None:
    raise SystemExit(f"campaign-objectives: {message}")


def load_json(path: Path, schema: str, maximum: int = 2 * 1024 * 1024) -> dict:
    if path.is_symlink() or not path.is_file():
        fail(f"required tracked file is missing or unsafe: {path}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        fail(f"cannot parse {path}: {exc}")
    if not isinstance(data, dict) or data.get("schema") != schema:
        fail(f"{path} schema must be {schema}")
    return data


def load_objectives(root: Path) -> dict:
    path = root / ".factory/campaign-objectives.json"
    data = load_json(path, "ralph-campaign-objectives/v1")
    objectives = data.get("objectives")
    if not isinstance(objectives, list) or not objectives:
        fail("campaign objectives map must declare at least one objective")
    keys: list[str] = []
    for objective in objectives:
        if not isinstance(objective, dict):
            fail("every campaign objective must be an object")
        key = objective.get("key")
        if not isinstance(key, str) or not key or not re.fullmatch(r"[a-z0-9][a-z0-9-]*", key):
            fail(f"campaign objective has an invalid key: {key!r}")
        keys.append(key)
        categories = objective.get("receipt_categories")
        if not isinstance(categories, list) or not categories or not all(
            isinstance(item, str) and item for item in categories
        ):
            fail(f"objective {key} requires non-empty receipt_categories")
        if len(categories) != len(set(categories)):
            fail(f"objective {key} receipt_categories must be unique")
    if len(keys) != len(set(keys)):
        fail("campaign objective keys must be unique")
    return data


def load_policy(root: Path) -> dict[str, dict]:
    path = root / ".factory/campaign-receipt-policy.json"
    data = load_json(path, "ralph-receipt-policy/v1")
    categories = data.get("categories")
    if not isinstance(categories, list) or not categories:
        fail("receipt policy must declare at least one category")
    by_name: dict[str, dict] = {}
    for category in categories:
        if not isinstance(category, dict):
            fail("every receipt-policy category must be an object")
        name = category.get("name")
        if not isinstance(name, str) or not name or not re.fullmatch(r"[a-z0-9][a-z0-9._-]*", name):
            fail(f"receipt policy has an invalid category name: {name!r}")
        if name in by_name:
            fail(f"receipt policy has duplicate category {name}")
        tier = category.get("tier")
        if tier not in TIERS:
            fail(f"receipt policy category {name} has invalid evidence tier {tier!r}")
        if category.get("allow_manifest") is not True:
            category["allow_manifest"] = False
        argv = category.get("argv")
        if not isinstance(argv, list) or not argv:
            fail(f"receipt policy category {name} must declare allowlisted argv arrays")
        for entry in argv:
            if (
                not isinstance(entry, list) or not entry
                or not all(isinstance(item, str) and item for item in entry)
            ):
                fail(f"receipt policy category {name} has an invalid argv allowlist entry")
        by_name[name] = {
            "name": name,
            "tier": tier,
            "allow_manifest": category["allow_manifest"],
            "argv": [list(entry) for entry in argv],
        }
    return by_name


def resolve(root: Path, reference: str) -> Path:
    path = Path(reference)
    if path.is_absolute() or ".." in path.parts:
        fail(f"objective evidence reference escapes the repository: {reference}")
    resolved = (root / reference).resolve()
    try:
        resolved.relative_to(root.resolve())
    except ValueError:
        fail(f"objective evidence reference escapes the repository: {reference}")
    return resolved


def receipt_record(root: Path, reference: str) -> dict:
    path = resolve(root, reference)
    if path.is_symlink() or not path.is_file():
        fail(f"objective receipt is missing or unsafe: {reference}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        fail(f"invalid objective receipt {reference}: {exc}")
    if not isinstance(data, dict):
        fail(f"objective receipt must be an object: {reference}")
    schema = data.get("schema")
    if schema == "ralph-audit-receipt/v1":
        return data
    if schema == "factory-runner-receipt/v1":
        return data
    fail(f"objective evidence has an unknown schema: {reference}")
    return {}


def manifest_segments(root: Path, reference: str) -> list[str]:
    path = resolve(root, reference)
    try:
        relative = path.relative_to(root.resolve())
    except ValueError:
        fail(f"objective manifest escapes the repository: {reference}")
    return list(relative.parts)


def audit_binding(args: argparse.Namespace, root: Path) -> tuple[int, str]:
    round_number = args.round
    if round_number is None:
        env_round = os.environ.get("FACTORY_CAMPAIGN_AUDIT_ROUND", "")
        if not env_round.isdigit() or int(env_round) < 1:
            fail("an audit round (--round or FACTORY_CAMPAIGN_AUDIT_ROUND) is required")
        round_number = int(env_round)
    if round_number < 1:
        fail("audit round must be positive")
    base = args.base
    if base is None:
        base = os.environ.get("FACTORY_CAMPAIGN_AUDIT_BASE", "")
    if not isinstance(base, str) or not SHA1.fullmatch(base):
        fail("an audit base commit (--base or FACTORY_CAMPAIGN_AUDIT_BASE) is required")
    return round_number, base


def covered_categories(root: Path, lines: list[str], required: set[str],
                       round_number: int, base: str, policy: dict[str, dict]) -> set[str]:
    covered: set[str] = set()
    for line in lines:
        for receipt_ref in RECEIPT.findall(line):
            data = receipt_record(root, receipt_ref)
            if data.get("schema") != "ralph-audit-receipt/v1":
                continue
            tag = data.get("tag")
            if not isinstance(tag, str) or tag not in required:
                continue
            category = policy.get(tag)
            if category is None:
                fail(f"receipt tag {tag!r} has no tracked receipt-policy category")
            argv = data.get("argv")
            argv_sha256 = data.get("argv_sha256")
            if not isinstance(argv, list) or not all(isinstance(item, str) and item for item in argv):
                fail(f"receipt {receipt_ref} has an invalid argv")
            if not isinstance(argv_sha256, str) or not SHA256.fullmatch(argv_sha256):
                fail(f"receipt {receipt_ref} has an invalid argv_sha256")
            digest = hashlib.sha256(json.dumps(argv, separators=(",", ":")).encode()).hexdigest()
            if argv_sha256 != digest:
                fail(f"receipt {receipt_ref} argv digest mismatch")
            if argv not in category["argv"]:
                fail(
                    f"receipt {receipt_ref} argv {argv!r} is not allowlisted for category "
                    f"{tag!r} (tag/path spoofing or arbitrary command rejected)"
                )
            evidence_commit = data.get("evidence_commit")
            coordinator_round = data.get("coordinator_round")
            coordinator_nonce = data.get("coordinator_nonce")
            if not isinstance(evidence_commit, str) or not SHA1.fullmatch(evidence_commit):
                fail(f"receipt {receipt_ref} has an invalid evidence_commit")
            if evidence_commit != base:
                fail(
                    f"receipt {receipt_ref} evidence_commit {evidence_commit[:12]} does not equal "
                    f"the audit base {base[:12]} (stale or reused across rounds)"
                )
            if type(coordinator_round) is not int or coordinator_round != round_number:
                fail(
                    f"receipt {receipt_ref} round binding {coordinator_round!r} does not equal "
                    f"the active round {round_number} (stale or reused across rounds)"
                )
            if not isinstance(coordinator_nonce, str) or not SHA256.fullmatch(coordinator_nonce):
                fail(f"receipt {receipt_ref} has an invalid coordinator_nonce")
            covered.add(tag)
        for manifest_ref in MANIFEST.findall(line):
            data = receipt_record(root, manifest_ref)
            if data.get("schema") != "factory-runner-receipt/v1":
                fail(f"objective manifest is not a factory-runner-receipt: {manifest_ref}")
            if data.get("result") != "pass" or data.get("exit_code") != 0:
                fail(f"objective manifest does not prove a clean pass: {manifest_ref}")
            segments = manifest_segments(root, manifest_ref)
            for segment in segments:
                if segment in required:
                    category = policy.get(segment)
                    if category is None:
                        fail(f"manifest category {segment!r} has no tracked receipt-policy category")
                    if not category["allow_manifest"]:
                        fail(
                            f"category {segment!r} does not allow runner-manifest coverage; "
                            f"a machine receipt with the allowlisted argv is required"
                        )
                    covered.add(segment)
    return covered


def parse_evidence_lines(report: Path) -> list[str]:
    text = report.read_text(encoding="utf-8")
    match = re.search(r"^## Evidence reviewed\s*\n(.*?)(?=^## |\Z)", text, re.M | re.S)
    if not match:
        fail("audit report has no Evidence reviewed section")
    lines = []
    for line in match.group(1).splitlines():
        stripped = line.strip()
        if stripped.startswith("- Executable evidence:"):
            lines.append(stripped)
    return lines


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--round", type=int, default=None)
    parser.add_argument("--base", default=None)
    parser.add_argument("--report", default=str(REPORT_DEFAULT))
    parser.add_argument("--root", default=str(ROOT))
    args = parser.parse_args()
    root = Path(args.root).resolve()
    round_number, base = audit_binding(args, root)
    data = load_objectives(root)
    objectives = data["objectives"]
    objective = objectives[(round_number - 1) % len(objectives)]
    required = set(objective["receipt_categories"])
    policy = load_policy(root)
    missing_policy = sorted(required - set(policy))
    if missing_policy:
        fail(
            f"objective `{objective['key']}` requires receipt categories without a "
            f"tracked receipt-policy binding: {missing_policy}"
        )
    report_path = root / args.report if not Path(args.report).is_absolute() else Path(args.report)
    if report_path.is_symlink() or not report_path.is_file():
        fail(f"audit report is missing: {report_path}")
    lines = parse_evidence_lines(report_path)
    covered = covered_categories(root, lines, required, round_number, base, policy)
    missing = sorted(required - covered)
    if missing:
        fail(
            f"round {round_number} objective `{objective['key']}` requires objective-specific "
            f"receipt categories not covered by the audit evidence: {missing}"
        )
    print(
        f"campaign-objectives: round {round_number} objective `{objective['key']}` "
        f"covered all {len(required)} required receipt categories at base {base[:12]}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
