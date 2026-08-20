#!/usr/bin/env python3
"""Check a campaign audit report against its per-round falsification objective.

`.factory/campaign-objectives.json` maps every audit round (round r uses
objectives[(r-1) mod N]) to a product-neutral falsification objective with
objective-specific receipt categories. An audit round cannot pass without
machine receipts (or accepted runner manifests) covering every category of
that round's objective: replaying the same generic suite cannot satisfy all
rounds.

Matching rules (strict):
- a `[receipt: <path>]` reference satisfies a category when the receipt's
  `tag` field equals the category exactly;
- a `[manifest: <path>]` reference satisfies a category when the manifest
  path contains the category as one of its path segments;
- BLOCKED evidence lines carry no receipt/manifest and satisfy nothing;
- every required category of the round's objective must be covered by at
  least one executable-evidence line in `## Evidence reviewed`.

Usage:
  scripts/check-campaign-objectives.py [--round N] [--report PATH] [--root ROOT]
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OBJECTIVES_DEFAULT = ROOT / ".factory/campaign-objectives.json"
REPORT_DEFAULT = ROOT / ".factory/artifacts/campaign-audit.md"
RECEIPT = re.compile(r"\[receipt:\s*([^\]]+)\]")
MANIFEST = re.compile(r"\[manifest:\s*([^\]]+)\]")


def fail(message: str) -> None:
    raise SystemExit(f"campaign-objectives: {message}")


def load_module(name: str, path: Path):
    if path.is_symlink() or not path.is_file():
        fail(f"factory script is unavailable: {path}")
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        fail(f"cannot load factory script: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_objectives(root: Path) -> dict:
    path = root / ".factory/campaign-objectives.json"
    if path.is_symlink() or not path.is_file():
        fail(f"campaign objectives map must be a regular tracked file: {path}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        fail(f"cannot parse campaign objectives map {path}: {exc}")
    if not isinstance(data, dict) or data.get("schema") != "ralph-campaign-objectives/v1":
        fail(f"campaign objectives map schema must be ralph-campaign-objectives/v1: {path}")
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


def receipt_tags(root: Path, reference: str) -> list[str]:
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
        tag = data.get("tag")
        return [tag] if isinstance(tag, str) and tag else []
    if schema == "factory-runner-receipt/v1":
        capabilities = data.get("capabilities", [])
        return [item for item in capabilities if isinstance(item, str)] if isinstance(capabilities, list) else []
    fail(f"objective evidence has an unknown schema: {reference}")
    return []


def manifest_segments(root: Path, reference: str) -> list[str]:
    path = resolve(root, reference)
    try:
        relative = path.relative_to(root.resolve())
    except ValueError:
        fail(f"objective manifest escapes the repository: {reference}")
    return list(relative.parts)


def covered_categories(root: Path, lines: list[str], required: set[str]) -> set[str]:
    covered: set[str] = set()
    for line in lines:
        for receipt_ref in RECEIPT.findall(line):
            for tag in receipt_tags(root, receipt_ref):
                if tag in required:
                    covered.add(tag)
        for manifest_ref in MANIFEST.findall(line):
            segments = manifest_segments(root, manifest_ref)
            for segment in segments:
                if segment in required:
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
    parser.add_argument("--report", default=str(REPORT_DEFAULT))
    parser.add_argument("--root", default=str(ROOT))
    args = parser.parse_args()
    root = Path(args.root).resolve()
    round_number = args.round
    if round_number is None:
        env_round = os.environ.get("FACTORY_CAMPAIGN_AUDIT_ROUND", "")
        if not env_round.isdigit() or int(env_round) < 1:
            fail("an audit round (--round or FACTORY_CAMPAIGN_AUDIT_ROUND) is required")
        round_number = int(env_round)
    if round_number < 1:
        fail("audit round must be positive")
    data = load_objectives(root)
    objectives = data["objectives"]
    objective = objectives[(round_number - 1) % len(objectives)]
    required = set(objective["receipt_categories"])
    report_path = root / args.report if not Path(args.report).is_absolute() else Path(args.report)
    if report_path.is_symlink() or not report_path.is_file():
        fail(f"audit report is missing: {report_path}")
    lines = parse_evidence_lines(report_path)
    covered = covered_categories(root, lines, required)
    missing = sorted(required - covered)
    if missing:
        fail(
            f"round {round_number} objective `{objective['key']}` requires objective-specific "
            f"receipt categories not covered by the audit evidence: {missing}"
        )
    print(
        f"campaign-objectives: round {round_number} objective `{objective['key']}` "
        f"covered all {len(required)} required receipt categories"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
