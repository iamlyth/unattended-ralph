"""Deterministic audit-objective registry authority (Task 8; FACTORY-LOOP-SPEC §6.4).

The auditor role receives a deterministic audit objective selected from a
committed, product-neutral registry.  The registry is digest-bound at
campaign start (``factory-state/v1`` ``audit_objectives_digest``) and is
immutable during a campaign; the selected objective is the only additional
role input permitted beyond §5.1.  Objectives are falsification lenses, not
implementation tasks or project memory.

Selection is a pure function of the round number: ``index = (round - 1) % len``
over the committed registry order, so the same registry and round always
select the same objective (deterministic, no randomness, no runtime state).
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import sys
from typing import List, Mapping, Sequence, Tuple

SCHEMA_NAME = "audit-objectives/v1"
REGISTRY_RELPATH = Path("audit-objectives") / "registry.json"
SCHEMA_FILE = "audit-objectives-v1.schema.json"

MAX_REGISTRY_BYTES = 1024 * 1024
_BLOB_CHUNK = 65536
ID_RE = re.compile(r"^AUD-[0-9]{2}$")


class AuditObjectiveError(Exception):
    """The registry is missing, unsafe, malformed, or violates the contract."""


def registry_path(root: object) -> Path:
    """The canonical committed registry path beneath a workspace root."""
    return Path(str(root)).absolute() / ".factory" / REGISTRY_RELPATH


def read_registry_bytes(path: object = None) -> bytes:
    """Bounded no-follow read of the committed registry bytes.

    ``path`` is the registry file path (defaults to the canonical path
    relative to the current repository root).  A symlink final component, a
    non-regular file, an oversized file, or a file that changes while being
    read fails closed.
    """
    if path is None:
        path = Path(__file__).resolve().parents[1] / REGISTRY_RELPATH
    flags = (
        os.O_RDONLY
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )
    try:
        descriptor = os.open(str(path), flags)
    except OSError as exc:
        raise AuditObjectiveError(f"cannot open the audit-objective registry {path}: {exc}") from exc
    try:
        info = os.fstat(descriptor)
        if not stat_regular(info):
            raise AuditObjectiveError(f"the registry {path} is not a regular file")
        if info.st_size > MAX_REGISTRY_BYTES:
            raise AuditObjectiveError(
                f"the registry {path} exceeds the {MAX_REGISTRY_BYTES}-byte bound"
            )
        before = (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)
        chunks: List[bytes] = []
        remaining = info.st_size + 1
        while remaining:
            chunk = os.read(descriptor, min(_BLOB_CHUNK, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        after = os.fstat(descriptor)
        if (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns) != before:
            raise AuditObjectiveError(f"the registry {path} changed while being read")
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def stat_regular(info: object) -> bool:
    import stat

    return stat.S_ISREG(info.st_mode)


def parse_registry(data: bytes) -> Mapping[str, object]:
    """Deterministically parse and validate the committed registry.

    Rejects: a non-JSON document, a wrong schema name, a missing/empty
    objective list, duplicate objective IDs, an ID that does not match
    ``AUD-<NN>``, a non-sequential ID set, an empty title or objective
    text, or any extra field (the schema is closed).  The parsed structure
    is a deterministic function of the registry bytes.
    """
    try:
        document = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise AuditObjectiveError(f"the registry is not valid JSON: {exc}") from exc
    if not isinstance(document, dict):
        raise AuditObjectiveError("the registry must be a JSON object")
    if document.get("schema") != SCHEMA_NAME:
        raise AuditObjectiveError(
            f"the registry schema must be exactly {SCHEMA_NAME!r}, got "
            f"{document.get('schema')!r}"
        )
    objectives = document.get("objectives")
    if not isinstance(objectives, list) or not objectives:
        raise AuditObjectiveError("the registry must carry at least one objective")
    seen: List[str] = []
    for index, entry in enumerate(objectives):
        if not isinstance(entry, dict):
            raise AuditObjectiveError(f"objective {index} is not an object")
        extra = set(entry) - {"id", "title", "objective"}
        if extra:
            raise AuditObjectiveError(
                f"objective {index} carries unknown field(s) {sorted(extra)!r}"
            )
        objective_id = entry.get("id")
        title = entry.get("title")
        text = entry.get("objective")
        if not isinstance(objective_id, str) or not ID_RE.fullmatch(objective_id):
            raise AuditObjectiveError(
                f"objective {index} id must match AUD-<NN>, got {objective_id!r}"
            )
        if not isinstance(title, str) or not title.strip():
            raise AuditObjectiveError(f"objective {index} has an empty title")
        if not isinstance(text, str) or not text.strip():
            raise AuditObjectiveError(f"objective {index} has empty objective text")
        if objective_id in seen:
            raise AuditObjectiveError(f"duplicate objective id {objective_id!r}")
        seen.append(objective_id)
    # Sequential AUD-NN ordering with no gaps (deterministic ordering).
    expected = [f"AUD-{index + 1:02d}" for index in range(len(objectives))]
    if seen != expected:
        raise AuditObjectiveError(
            f"objective ids must be sequential from AUD-01, got {seen!r}"
        )
    return document


def registry_digest(data: bytes) -> str:
    """The campaign-bound SHA-256 of the exact registry bytes."""
    return hashlib.sha256(data).hexdigest()


def objectives_list(document: Mapping[str, object]) -> Sequence[Mapping[str, str]]:
    """The validated objectives in committed order."""
    return tuple(document["objectives"])  # type: ignore[arg-type]


def select_audit_objective(
    round_number: int, document: Mapping[str, object]
) -> Mapping[str, str]:
    """Deterministic per-round objective selection (§6.4).

    ``index = (round - 1) % len(objectives)`` over the committed registry
    order.  A non-positive round fails closed; selection never depends on
    randomness, wall-clock time, or runtime state.
    """
    if isinstance(round_number, bool) or not isinstance(round_number, int) or round_number < 1:
        raise AuditObjectiveError(
            f"round_number must be a positive integer, got {round_number!r}"
        )
    objectives = objectives_list(document)
    return objectives[(round_number - 1) % len(objectives)]


def load_registry(root: object) -> Tuple[bytes, Mapping[str, object]]:
    """Read + validate the canonical registry; returns ``(bytes, document)``."""
    data = read_registry_bytes(registry_path(root))
    return data, parse_registry(data)


def _cli(argv: Sequence[str]) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        prog="factory-audit-objectives",
        description="Audit-objective registry (digest, validate, deterministic select).",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    p_digest = sub.add_parser("digest", help="print the campaign-bound registry digest")
    p_digest.add_argument("--registry", metavar="FILE", default=None)
    p_select = sub.add_parser("select", help="select the objective for a round")
    p_select.add_argument("--round", required=True, type=int)
    p_select.add_argument("--registry", metavar="FILE", default=None)
    p_validate = sub.add_parser("validate", help="validate the registry")
    p_validate.add_argument("--registry", metavar="FILE", default=None)
    args = parser.parse_args(argv)
    try:
        data = read_registry_bytes(args.registry)
        document = parse_registry(data)
    except AuditObjectiveError as exc:
        print(f"factory-audit-objectives: {exc}", file=sys.stderr)
        return 2
    if args.command == "digest":
        print(registry_digest(data))
        return 0
    if args.command == "validate":
        print(f"{len(objectives_list(document))} objectives, digest "
              f"{registry_digest(data)}")
        return 0
    try:
        selected = select_audit_objective(args.round, document)
    except AuditObjectiveError as exc:
        print(f"factory-audit-objectives: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(dict(selected), sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    sys.exit(_cli(sys.argv[1:]))
