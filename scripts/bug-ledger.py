#!/usr/bin/env python3
"""Validate and maintain the portable Ralph bug ledgers (stdlib only)."""
from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import fcntl
import hashlib
import ipaddress
import json
import os
import pathlib
import re
import sys
import tempfile
import urllib.parse

SCHEMA = "ralph-bug-ledger/v1"
OPEN_PATH = pathlib.Path("open-bugs.md")
CLOSED_PATH = pathlib.Path("closed-bugs.md")
LOCK_PATH = pathlib.Path(".bug-ledger.lock")
OPEN_STATUSES = {"open", "triaged", "planned", "in_progress", "blocked"}
SEVERITIES = {"low", "medium", "high", "critical"}
PROVIDERS = {"github", "forgejo"}
FIELDS = (
    "id", "title", "status", "severity", "reported", "external",
    "contract_change", "reproduction", "expected", "actual", "acceptance",
    "resolution", "verification", "closed",
)
TRANSITIONS = {
    "open": {"triaged", "blocked"},
    "triaged": {"planned", "blocked"},
    "planned": {"in_progress", "blocked"},
    "in_progress": {"blocked"},
    "blocked": {"triaged", "planned", "in_progress"},
}
IMMUTABLE = ("id", "title", "severity", "reported", "contract_change", "reproduction", "expected", "actual", "acceptance")
FENCE = re.compile(r"```json\s*\n(.*?)\n```", re.S)
ID_RE = re.compile(r"BUG-[0-9]{4,}")


def fail(message: str) -> None:
    raise ValueError(message)


def id_number(value: str) -> int:
    return int(value.removeprefix("BUG-"))


def iso_date(value: object, field: str, *, nullable: bool = False) -> None:
    if nullable and value is None:
        return
    if not isinstance(value, str):
        fail(f"{field} must be an ISO date")
    try:
        parsed = dt.date.fromisoformat(value)
    except ValueError:
        fail(f"{field} must be an ISO date")
    if parsed.isoformat() != value:
        fail(f"{field} must use YYYY-MM-DD")


def validate_url(item: object, bug_id: str) -> None:
    if not isinstance(item, dict) or set(item) != {"provider", "url"}:
        fail(f"{bug_id}: external entries require only provider and url")
    if item["provider"] not in PROVIDERS or not isinstance(item["url"], str):
        fail(f"{bug_id}: invalid external provider/url")
    url = item["url"]
    if any(char.isspace() or ord(char) < 32 for char in url):
        fail(f"{bug_id}: external URL must not contain whitespace/control characters")
    try:
        parsed = urllib.parse.urlsplit(url)
        hostname = parsed.hostname
        port = parsed.port
    except ValueError:
        fail(f"{bug_id}: external URL has malformed authority or port")
    if (parsed.scheme != "https" or not parsed.netloc or not hostname or
            "@" in parsed.netloc or parsed.username or parsed.password or
            parsed.netloc.endswith(":") or "\\" in parsed.netloc):
        fail(f"{bug_id}: external URL must have a valid credential-free HTTPS authority")
    try:
        ipaddress.ip_address(hostname)
    except ValueError:
        labels = hostname.split(".")
        if any(not label or len(label) > 63 or
               not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?", label)
               for label in labels):
            fail(f"{bug_id}: external URL has malformed hostname")
    if port is not None and not 1 <= port <= 65535:
        fail(f"{bug_id}: external URL has invalid port")
    if parsed.query or parsed.fragment or "?" in url or "#" in url:
        fail(f"{bug_id}: external URL must not contain query or fragment")
    # GitHub and root-hosted Forgejo use /OWNER/REPO/issues/N. Retain the
    # provider-neutral singular route accepted by earlier ledgers as well.
    if not re.fullmatch(r"/(?:[^/]+/){2}(?:issues|issue)/[1-9][0-9]*/?", parsed.path):
        fail(f"{bug_id}: external URL is not an issue URL")


def validate_record(record: object, expected_closed: bool) -> dict:
    if not isinstance(record, dict) or set(record) != set(FIELDS):
        fail(f"record fields must be exactly: {', '.join(FIELDS)}")
    bug_id = record.get("id")
    if not isinstance(bug_id, str) or not ID_RE.fullmatch(bug_id):
        fail("invalid bug id (expected BUG-0001+)")
    number = id_number(bug_id)
    if number < 1 or bug_id != f"BUG-{number:04d}":
        fail("invalid bug id (expected canonical BUG-0001+)")
    for field in ("title", "reproduction", "expected", "actual", "acceptance"):
        if not isinstance(record[field], str) or not record[field].strip():
            fail(f"{bug_id}: {field} must be non-empty text")
    if record["severity"] not in SEVERITIES:
        fail(f"{bug_id}: invalid severity")
    if type(record["contract_change"]) is not bool:
        fail(f"{bug_id}: contract_change must be boolean")
    iso_date(record["reported"], f"{bug_id}: reported")
    if not isinstance(record["external"], list):
        fail(f"{bug_id}: external must be an array")
    seen_providers = set()
    for item in record["external"]:
        validate_url(item, bug_id)
        if item["provider"] in seen_providers:
            fail(f"{bug_id}: duplicate external provider")
        seen_providers.add(item["provider"])
    if expected_closed:
        if record["status"] != "closed":
            fail(f"{bug_id}: closed ledger record must have closed status")
        for field in ("resolution", "verification"):
            if not isinstance(record[field], str) or not record[field].strip():
                fail(f"{bug_id}: closed bug requires {field}")
        iso_date(record["closed"], f"{bug_id}: closed")
        if record["closed"] < record["reported"]:
            fail(f"{bug_id}: closed date cannot precede reported date")
    else:
        if record["status"] not in OPEN_STATUSES:
            fail(f"{bug_id}: invalid open status")
        if record["resolution"] != "" or record["verification"] != "" or record["closed"] is not None:
            fail(f"{bug_id}: open bug cannot contain closure evidence")
    return record


def parse_ledger(path: pathlib.Path, expected_closed: bool) -> list[dict]:
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        fail(f"missing ledger: {path}")
    if text.count("```") != 2:
        fail(f"{path}: must contain exactly one fenced block")
    matches = FENCE.findall(text)
    if len(matches) != 1 or f"Schema: `{SCHEMA}`" not in text:
        fail(f"{path}: expected one JSON array with schema {SCHEMA}")
    try:
        data = json.loads(matches[0])
    except json.JSONDecodeError as exc:
        fail(f"{path}: malformed JSON: {exc}")
    if not isinstance(data, list):
        fail(f"{path}: fenced JSON must be an array")
    records = [validate_record(item, expected_closed) for item in data]
    ids = [record["id"] for record in records]
    if len(ids) != len(set(ids)):
        fail(f"{path}: duplicate bug ID")
    if ids != sorted(ids, key=id_number):
        fail(f"{path}: records must be sorted by numeric id")
    return records


def load_all() -> tuple[list[dict], list[dict]]:
    opened = parse_ledger(OPEN_PATH, False)
    closed = parse_ledger(CLOSED_PATH, True)
    ids = [r["id"] for r in opened + closed]
    if len(ids) != len(set(ids)):
        fail("bug IDs must be unique across both ledgers")
    return opened, closed


@contextlib.contextmanager
def ledger_lock():
    with LOCK_PATH.open("a+", encoding="utf-8") as stream:
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
        yield


def render(path: pathlib.Path, records: list[dict], closed: bool) -> str:
    title = "Closed Bugs" if closed else "Open Bugs"
    purpose = "Completed defects and their verification evidence." if closed else "Canonical queue of defects awaiting maintenance."
    normalized = []
    provider_order = {"github": 0, "forgejo": 1}
    for record in sorted(records, key=lambda item: id_number(item["id"])):
        ordered = {field: record[field] for field in FIELDS}
        ordered["external"] = sorted(record["external"], key=lambda item: (provider_order[item["provider"]], item["url"]))
        normalized.append(ordered)
    payload = json.dumps(normalized, indent=2, ensure_ascii=False) + "\n"
    return f"# {title}\n\n{purpose}\n\nSchema: `{SCHEMA}`\n\n```json\n{payload}```\n"


def atomic_write(path: pathlib.Path, records: list[dict], closed: bool) -> None:
    content = render(path, records, closed)
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent, text=True)
    try:
        mode = path.stat().st_mode & 0o777 if path.exists() else 0o644
        os.fchmod(fd, mode)
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def fingerprint(record: dict) -> str:
    value = {field: record[field] for field in IMMUTABLE}
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    return hashlib.sha256(encoded).hexdigest()


def find_record(bug_id: str, opened: list[dict], closed: list[dict]) -> tuple[dict, bool]:
    for record in opened:
        if record["id"] == bug_id:
            return record, False
    for record in closed:
        if record["id"] == bug_id:
            return record, True
    fail(f"unknown bug: {bug_id}")


def external_arg(value: str) -> dict:
    provider, separator, url = value.partition("=")
    if not separator:
        raise argparse.ArgumentTypeError("external link must be PROVIDER=URL")
    item = {"provider": provider, "url": url}
    try:
        validate_url(item, "new bug")
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc
    return item


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    commands = result.add_subparsers(dest="command", required=True)
    commands.add_parser("validate")
    commands.add_parser("recover")
    listing = commands.add_parser("list")
    listing.add_argument("--status", choices=sorted(OPEN_STATUSES | {"closed"}))
    for name in ("show", "fingerprint"):
        sub = commands.add_parser(name)
        sub.add_argument("bug_id")
    add = commands.add_parser("add")
    add.add_argument("--title", required=True)
    add.add_argument("--severity", choices=sorted(SEVERITIES), required=True)
    add.add_argument("--reported", default=dt.date.today().isoformat())
    add.add_argument("--external", action="append", type=external_arg, default=[])
    add.add_argument("--contract-change", action="store_true")
    for field in ("reproduction", "expected", "actual", "acceptance"):
        add.add_argument(f"--{field}", required=True)
    status = commands.add_parser("set-status")
    status.add_argument("bug_id")
    status.add_argument("status", choices=sorted(OPEN_STATUSES))
    close = commands.add_parser("close")
    close.add_argument("bug_id")
    close.add_argument("--resolution", required=True)
    close.add_argument("--verification", required=True)
    close.add_argument("--closed", default=dt.date.today().isoformat())
    link = commands.add_parser("link")
    link.add_argument("bug_id")
    link.add_argument("provider", choices=sorted(PROVIDERS))
    link.add_argument("url")
    unlink = commands.add_parser("unlink")
    unlink.add_argument("bug_id")
    unlink.add_argument("provider", choices=sorted(PROVIDERS))
    return result


def recover_interrupted_close() -> int:
    opened = parse_ledger(OPEN_PATH, False)
    closed = parse_ledger(CLOSED_PATH, True)
    open_by_id = {record["id"]: record for record in opened}
    closed_by_id = {record["id"]: record for record in closed}
    duplicates = sorted(set(open_by_id) & set(closed_by_id), key=id_number)
    if not duplicates:
        print("bug-ledger: no interrupted close to recover")
        return 0
    for bug_id in duplicates:
        if fingerprint(open_by_id[bug_id]) != fingerprint(closed_by_id[bug_id]):
            fail(f"{bug_id}: duplicate ledgers do not have matching immutable fingerprint")
    opened = [record for record in opened if record["id"] not in set(duplicates)]
    atomic_write(OPEN_PATH, opened, False)
    print(f"bug-ledger: recovered interrupted close for {', '.join(duplicates)}")
    return 0


def mutate(args: argparse.Namespace) -> int:
    if args.command == "recover":
        return recover_interrupted_close()
    opened, closed = load_all()
    if args.command == "add":
        numeric = [id_number(item["id"]) for item in opened + closed]
        bug_id = f"BUG-{max(numeric, default=0) + 1:04d}"
        record = {
            "id": bug_id, "title": args.title, "status": "open", "severity": args.severity,
            "reported": args.reported, "external": args.external, "contract_change": args.contract_change,
            "reproduction": args.reproduction, "expected": args.expected, "actual": args.actual,
            "acceptance": args.acceptance, "resolution": "", "verification": "", "closed": None,
        }
        validate_record(record, False)
        opened.append(record)
        atomic_write(OPEN_PATH, opened, False)
        print(bug_id)
        return 0

    record, is_closed = find_record(args.bug_id, opened, closed)
    if is_closed:
        fail(f"{args.bug_id}: closed bugs are immutable")
    if args.command == "set-status":
        if args.status not in TRANSITIONS[record["status"]]:
            fail(f'{args.bug_id}: invalid transition {record["status"]} -> {args.status}')
        record["status"] = args.status
        atomic_write(OPEN_PATH, opened, False)
    elif args.command == "close":
        if record["status"] != "in_progress":
            fail(f"{args.bug_id}: close requires in_progress status")
        if not args.resolution.strip() or not args.verification.strip():
            fail("closure evidence must be non-empty")
        iso_date(args.closed, f"{args.bug_id}: closed")
        opened.remove(record)
        record.update(status="closed", resolution=args.resolution, verification=args.verification, closed=args.closed)
        validate_record(record, True)
        # Destination first: interruption is recoverable as a detectable duplicate.
        atomic_write(CLOSED_PATH, closed + [record], True)
        atomic_write(OPEN_PATH, opened, False)
    elif args.command == "link":
        item = {"provider": args.provider, "url": args.url}
        validate_url(item, args.bug_id)
        if any(link["provider"] == args.provider for link in record["external"]):
            fail(f"{args.bug_id}: duplicate external provider")
        record["external"].append(item)
        atomic_write(OPEN_PATH, opened, False)
    elif args.command == "unlink":
        matches = [link for link in record["external"] if link["provider"] == args.provider]
        if not matches:
            fail(f"{args.bug_id}: no {args.provider} external link")
        record["external"] = [link for link in record["external"] if link["provider"] != args.provider]
        atomic_write(OPEN_PATH, opened, False)
    return 0


def main() -> int:
    args = parser().parse_args()
    if args.command in {"add", "set-status", "close", "link", "unlink", "recover"}:
        with ledger_lock():
            return mutate(args)
    opened, closed = load_all()
    if args.command == "validate":
        print(f"bug-ledger: valid ({len(opened)} open, {len(closed)} closed)")
    elif args.command == "list":
        for record in sorted(opened + closed, key=lambda item: id_number(item["id"])):
            if args.status is None or record["status"] == args.status:
                print(f'{record["id"]}\t{record["status"]}\t{record["severity"]}\t{record["title"]}')
    else:
        record, _ = find_record(args.bug_id, opened, closed)
        if args.command == "fingerprint":
            print(fingerprint(record))
        else:
            print(json.dumps(record, indent=2, ensure_ascii=False))
            print(f"fingerprint: {fingerprint(record)}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ValueError as exc:
        print(f"bug-ledger: {exc}", file=sys.stderr)
        raise SystemExit(1)
