#!/usr/bin/env python3
"""Strict committed machine sidecars for the concise active plan (Phase 2D1).

The canonical plan (``factory-plan/v2``) carries only active/pending/
in_progress/blocked tasks with concise Scope/Acceptance, the current blocker,
and the latest actionable failure.  Completed/cancelled tasks and the plan
acceptance/evidence history live in two strict committed machine sidecars
under ``.factory/artifacts/``:

* ``plan-archive.jsonl`` (``factory-plan-archive/v1``) — one JSONL record per
  archived completed/cancelled task, preserving every task/status/acceptance/
  evidence datum losslessly for audit (the full Evidence narrative is kept
  verbatim in ``evidence``; exact commit/evidence references are extracted
  into ``evidence_refs`` as safe inert data, never interpreted as authority);
* ``plan-history.jsonl`` (``factory-plan-history/v1``) — one JSONL record per
  plan acceptance/evidence event (migration, archive, reopen, acceptance).

Both sidecars are content-addressed: the plan front matter ``sidecars:``
binding carries their exact SHA-256 digests, and the composite plan binding
digest (:func:`plan_binding_digest`) binds the active plan plus both required
sidecar digests.  Sidecars are append-only (or deterministically migrated),
bounded (size/count/record caps, duplicate-key rejection), and are queryable
by trusted tools but excluded from routine role prompts and never interpreted
as task/command authority.  The existing ``.factory/artifacts/conformance.json``,
blocked facts, and audit receipts remain the authorities; this module never
duplicates raw receipts.

The module is part of the hidden control-plane package and is imported
exactly like its siblings (package mode from the hidden loop, flat mode from
the hidden ``.factory/tests/`` suite).  It uses only the Python standard
library.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import tempfile
from dataclasses import dataclass, field as dataclass_field
from pathlib import Path
from typing import Dict, List, Mapping, Optional, Sequence, Tuple

ARCHIVE_SCHEMA = "factory-plan-archive/v1"
HISTORY_SCHEMA = "factory-plan-history/v1"
ARCHIVE_FILE = "plan-archive.jsonl"
HISTORY_FILE = "plan-history.jsonl"
ARTIFACTS_DIR = ".factory/artifacts"

# Bounds: every sidecar is finite and fails closed on overflow.  The per-file
# cap is far below the plan blob cap so a sidecar can never smuggle an
# attacker-sized document into a prompt-adjacent read; the per-record cap keeps
# one malformed line from dominating a file; the record-count cap bounds the
# completed-ID index and every scan.
SIDECAR_MAX_BYTES = 1024 * 1024
RECORD_MAX_BYTES = 64 * 1024
RECORD_MAX_COUNT = 10000

SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
SHA40_RE = re.compile(r"^[0-9a-f]{40}$")
IDENT_RE = re.compile(r"^[a-z0-9](?:[a-z0-9._-]{0,62}[a-z0-9])?$")
PROVENANCE_VALUES = ("migration", "campaign", "reopen")
EVENT_VALUES = ("migrated", "archived", "reopened", "accepted", "plan_revised")
ARCHIVE_STATUSES = ("complete", "cancelled")

# The exact field set of one archive record (strict; unknown or repeated keys
# fail closed).  ``evidence`` is the full v1 Evidence narrative preserved
# verbatim (bounded inert data, never authority); ``evidence_refs`` is the
# curated list of safe inert references extracted from it (Phase 2D1
# security remediation A).
ARCHIVE_FIELDS = (
    "schema", "task_id", "title", "priority", "dependencies", "status",
    "scope", "acceptance", "verification", "documentation_impact",
    "evidence", "evidence_refs", "archived_commit", "provenance",
)

# Safe inert-reference grammar for archive evidence refs.  A ref is inert
# data (never task/command authority) but must still be a safe
# repository-relative reference: non-empty, free of whitespace, control
# characters, and backslashes, not absolute, and free of empty/``.``/``..``
# segments.  Command-shaped prose (e.g. ``git diff HEAD -- …``) is rejected
# by the whitespace rule and stays in the ``evidence`` narrative.
EVIDENCE_REF_RE = re.compile(r"^[^\s/\\][^\s\\]*$")


def validate_evidence_ref(ref: str) -> None:
    """Fail closed when ``ref`` is not a safe inert reference.

    Rejects empty, whitespace, control characters, backslashes, absolute
    paths, and empty/``.``/``..`` segments.  The reference is inert data;
    this check only guarantees it can never be misread as a traversal or
    an absolute path by any consumer.
    """
    if not isinstance(ref, str) or not ref:
        raise PlanSidecarError("an evidence ref must be a non-empty string")
    if any(ch.isspace() for ch in ref):
        raise PlanSidecarError(
            f"evidence ref {ref!r} must not contain whitespace"
        )
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in ref):
        raise PlanSidecarError(
            f"evidence ref {ref!r} must not contain control characters"
        )
    if "\\" in ref:
        raise PlanSidecarError(
            f"evidence ref {ref!r} must not contain a backslash"
        )
    if ref.startswith("/"):
        raise PlanSidecarError(
            f"evidence ref {ref!r} must be repository-relative (not absolute)"
        )
    if any(segment in ("", ".", "..") for segment in ref.split("/")):
        raise PlanSidecarError(
            f"evidence ref {ref!r} must not contain empty, `.`, or `..` segments"
        )
HISTORY_FIELDS = (
    "schema", "event", "task_id", "commit", "plan_digest", "detail",
)


class PlanSidecarError(Exception):
    """A sidecar document was forged, unsafe, malformed, or out of bounds."""


class PlanSidecarBindingError(PlanSidecarError):
    """A sidecar digest does not match the plan's committed binding."""


class PlanSidecarTransitionError(PlanSidecarError):
    """An append/archive transition violates the sidecar's monotonic contract."""


def _reject_duplicate_keys(pairs):
    """JSON object-pairs hook: reject duplicate object keys in sidecar data."""
    result: Dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise PlanSidecarError(f"duplicate JSON object key: {key!r}")
        result[key] = value
    return result


def _as_root(root) -> Path:
    root = Path(root).absolute()
    try:
        info = os.stat(root, follow_symlinks=False)
    except OSError as exc:
        raise PlanSidecarError(f"cannot stat repository root {root}: {exc}") from exc
    if not stat.S_ISDIR(info.st_mode):
        raise PlanSidecarError(f"repository root is not a directory: {root}")
    return root


def _artifacts_dir(root: Path) -> Path:
    """The committed artifacts directory, validated no-follow."""
    path = root / ARTIFACTS_DIR
    try:
        info = os.stat(path, follow_symlinks=False)
    except OSError as exc:
        raise PlanSidecarError(f"cannot stat {ARTIFACTS_DIR}: {exc}") from exc
    if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
        raise PlanSidecarError(f"{ARTIFACTS_DIR} must be a real directory (no-follow)")
    return path


def _read_sidecar(root, name: str) -> bytes:
    """Bounded no-follow read of one committed sidecar file."""
    root = _as_root(root)
    path = _artifacts_dir(root) / name
    try:
        info = os.stat(path, follow_symlinks=False)
    except FileNotFoundError:
        raise PlanSidecarError(f"sidecar {name} is missing")
    except OSError as exc:
        raise PlanSidecarError(f"cannot inspect sidecar {name}: {exc}") from exc
    if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode):
        raise PlanSidecarError(f"sidecar {name} is not a regular non-symlink file")
    if info.st_size > SIDECAR_MAX_BYTES:
        raise PlanSidecarError(
            f"sidecar {name} is {info.st_size} bytes, exceeding the "
            f"{SIDECAR_MAX_BYTES}-byte cap"
        )
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise PlanSidecarError(f"cannot read sidecar {name}: {exc}") from exc
    if len(data) > SIDECAR_MAX_BYTES:
        raise PlanSidecarError(f"sidecar {name} grew past its size cap")
    return data


def _atomic_write_committed(path: Path, data: bytes) -> None:
    """Atomic no-follow write of a committed sidecar (temp + fsync + rename).

    The target directory is validated as a real non-symlink directory; the
    temporary file is created in the same directory (same filesystem) with
    mode 0600, fsynced, and atomically renamed over the target.  A crash
    leaves either the old bytes or the new bytes, never a partial document.
    """
    directory = path.parent
    try:
        dir_info = os.stat(directory, follow_symlinks=False)
    except OSError as exc:
        raise PlanSidecarError(f"cannot stat {directory}: {exc}") from exc
    if not stat.S_ISDIR(dir_info.st_mode) or stat.S_ISLNK(dir_info.st_mode):
        raise PlanSidecarError(f"{directory} must be a real directory (no-follow)")
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(directory)
    )
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.rename(tmp_name, path)
        try:
            dir_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except OSError:
            pass  # directory fsync is best-effort; the file fsync already ran
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


# ---------------------------------------------------------------------------
# Archive sidecar (factory-plan-archive/v1)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ArchiveRecord:
    """One archived completed/cancelled task, lossless for audit.

    ``evidence`` preserves the full v1 Evidence narrative verbatim (bounded
    inert data, never authority); ``evidence_refs`` is the curated list of
    safe inert references extracted from it (Phase 2D1 security remediation
    A).  Command-shaped prose stays in the narrative and is never a ref.
    """

    schema: str
    task_id: int
    title: str
    priority: int
    dependencies: Tuple[int, ...]
    status: str
    scope: str
    acceptance: str
    verification: str
    documentation_impact: str
    evidence: str
    evidence_refs: Tuple[str, ...]
    archived_commit: str
    provenance: str

    def to_dict(self) -> Dict[str, object]:
        return {
            "schema": self.schema,
            "task_id": self.task_id,
            "title": self.title,
            "priority": self.priority,
            "dependencies": list(self.dependencies),
            "status": self.status,
            "scope": self.scope,
            "acceptance": self.acceptance,
            "verification": self.verification,
            "documentation_impact": self.documentation_impact,
            "evidence": self.evidence,
            "evidence_refs": list(self.evidence_refs),
            "archived_commit": self.archived_commit,
            "provenance": self.provenance,
        }


def _validate_archive_record(value: object) -> None:
    if not isinstance(value, Mapping) or set(value) != set(ARCHIVE_FIELDS):
        raise PlanSidecarError(
            "an archive record must contain exactly the archive field set"
        )
    if value.get("schema") != ARCHIVE_SCHEMA:
        raise PlanSidecarError(f"archive record schema must be {ARCHIVE_SCHEMA!r}")
    if isinstance(value.get("task_id"), bool) or not isinstance(
        value.get("task_id"), int
    ) or int(value["task_id"]) < 1:
        raise PlanSidecarError("archive task_id must be a positive integer")
    if not isinstance(value.get("title"), str) or not value["title"].strip():
        raise PlanSidecarError("archive title must be non-empty")
    if isinstance(value.get("priority"), bool) or not isinstance(
        value.get("priority"), int
    ) or int(value["priority"]) < 1:
        raise PlanSidecarError("archive priority must be a positive integer")
    if value.get("status") not in ARCHIVE_STATUSES:
        raise PlanSidecarError(
            f"archive status must be one of {'|'.join(ARCHIVE_STATUSES)}"
        )
    deps = value.get("dependencies")
    if not isinstance(deps, list) or not all(
        isinstance(dep, int) and not isinstance(dep, bool) and dep >= 1
        for dep in deps
    ):
        raise PlanSidecarError("archive dependencies must be a list of positive integers")
    if len(set(deps)) != len(deps):
        raise PlanSidecarError("archive dependencies must not repeat")
    for name in ("scope", "acceptance", "verification", "documentation_impact"):
        if not isinstance(value.get(name), str):
            raise PlanSidecarError(f"archive {name} must be a string")
    refs = value.get("evidence_refs")
    if not isinstance(refs, list) or not all(
        isinstance(ref, str) and ref for ref in refs
    ):
        raise PlanSidecarError("archive evidence_refs must be a list of non-empty strings")
    for ref in refs:
        validate_evidence_ref(ref)
    if not isinstance(value.get("evidence"), str):
        raise PlanSidecarError("archive evidence must be a string")
    if not isinstance(value.get("archived_commit"), str) or not SHA40_RE.fullmatch(
        str(value["archived_commit"])
    ):
        raise PlanSidecarError("archive archived_commit must be a 40-hex commit")
    if value.get("provenance") not in PROVENANCE_VALUES:
        raise PlanSidecarError(
            f"archive provenance must be one of {'|'.join(PROVENANCE_VALUES)}"
        )


def parse_archive_record(data: object) -> ArchiveRecord:
    if not isinstance(data, dict):
        raise PlanSidecarError("an archive record must be a JSON object")
    _validate_archive_record(data)
    return ArchiveRecord(
        schema=str(data["schema"]),
        task_id=int(data["task_id"]),
        title=str(data["title"]),
        priority=int(data["priority"]),
        dependencies=tuple(int(dep) for dep in data["dependencies"]),
        status=str(data["status"]),
        scope=str(data["scope"]),
        acceptance=str(data["acceptance"]),
        verification=str(data["verification"]),
        documentation_impact=str(data["documentation_impact"]),
        evidence=str(data["evidence"]),
        evidence_refs=tuple(str(ref) for ref in data["evidence_refs"]),
        archived_commit=str(data["archived_commit"]),
        provenance=str(data["provenance"]),
    )


def parse_archive(data: bytes) -> List[ArchiveRecord]:
    """Parse a bounded JSONL archive sidecar into ordered records.

    Empty input is a valid empty archive.  A malformed line, a duplicate
    task_id, an oversized line, or a duplicate JSON key fails closed.
    """
    if len(data) > SIDECAR_MAX_BYTES:
        raise PlanSidecarError("archive sidecar exceeds its size cap")
    if not data:
        return []
    records: List[ArchiveRecord] = []
    seen: set = set()
    for line_number, raw in enumerate(data.split(b"\n"), start=1):
        if not raw:
            continue
        if len(raw) > RECORD_MAX_BYTES:
            raise PlanSidecarError(
                f"archive line {line_number} exceeds the {RECORD_MAX_BYTES}-byte cap"
            )
        try:
            value = json.loads(
                raw.decode("utf-8"), object_pairs_hook=_reject_duplicate_keys
            )
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise PlanSidecarError(
                f"archive line {line_number} is malformed: {exc}"
            ) from exc
        record = parse_archive_record(value)
        if record.task_id in seen:
            raise PlanSidecarError(
                f"archive repeats task_id {record.task_id} (line {line_number})"
            )
        seen.add(record.task_id)
        records.append(record)
        if len(records) > RECORD_MAX_COUNT:
            raise PlanSidecarError(
                f"archive exceeds the {RECORD_MAX_COUNT}-record cap"
            )
    return records


def serialize_archive(records: Sequence[ArchiveRecord]) -> bytes:
    """Deterministic JSONL serialization of archive records (one per line)."""
    lines: List[bytes] = []
    for record in records:
        _validate_archive_record(record.to_dict())
        lines.append(
            json.dumps(
                record.to_dict(), sort_keys=True, ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
        )
    return b"\n".join(lines) + (b"\n" if lines else b"")


def completed_ids(records: Sequence[ArchiveRecord]) -> frozenset:
    """The trusted completed-ID index: every archived task id.

    The index is a pure function of the archive records and is bound to the
    archive sidecar digest by the plan's ``sidecars:`` front matter binding;
    a missing/conflicting/reopened id fails closed at the caller that binds
    the index to the digest.
    """
    return frozenset(record.task_id for record in records)


def read_archive(root) -> List[ArchiveRecord]:
    """Securely reopen and validate the committed archive sidecar."""
    return parse_archive(_read_sidecar(root, ARCHIVE_FILE))


def write_archive(root, records: Sequence[ArchiveRecord]) -> str:
    """Atomically write the archive sidecar; returns its SHA-256 digest."""
    root = _as_root(root)
    data = serialize_archive(records)
    _atomic_write_committed(_artifacts_dir(root) / ARCHIVE_FILE, data)
    return hashlib.sha256(data).hexdigest()


def append_archive(root, record: ArchiveRecord) -> str:
    """Append one archive record (append-only; never rewrites history).

    The existing records are re-read and validated, the new record must not
    collide with an existing task_id, and the file is rewritten atomically
    with the appended record.  Returns the new sidecar digest.
    """
    root = _as_root(root)
    existing = read_archive(root)
    if any(record.task_id == existing_record.task_id for existing_record in existing):
        raise PlanSidecarTransitionError(
            f"archive already contains task_id {record.task_id}; a completed "
            "task may be archived exactly once"
        )
    return write_archive(root, [*existing, record])


# ---------------------------------------------------------------------------
# History sidecar (factory-plan-history/v1)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class HistoryRecord:
    """One plan acceptance/evidence history event (append-only)."""

    schema: str
    event: str
    task_id: Optional[int]
    commit: str
    plan_digest: str
    detail: str

    def to_dict(self) -> Dict[str, object]:
        return {
            "schema": self.schema,
            "event": self.event,
            "task_id": self.task_id,
            "commit": self.commit,
            "plan_digest": self.plan_digest,
            "detail": self.detail,
        }


def _validate_history_record(value: object) -> None:
    if not isinstance(value, Mapping) or set(value) != set(HISTORY_FIELDS):
        raise PlanSidecarError(
            "a history record must contain exactly the history field set"
        )
    if value.get("schema") != HISTORY_SCHEMA:
        raise PlanSidecarError(f"history record schema must be {HISTORY_SCHEMA!r}")
    if value.get("event") not in EVENT_VALUES:
        raise PlanSidecarError(
            f"history event must be one of {'|'.join(EVENT_VALUES)}"
        )
    task_id = value.get("task_id")
    if task_id is not None and (
        isinstance(task_id, bool) or not isinstance(task_id, int) or int(task_id) < 1
    ):
        raise PlanSidecarError("history task_id must be a positive integer or null")
    if not isinstance(value.get("commit"), str) or not SHA40_RE.fullmatch(
        str(value["commit"])
    ):
        raise PlanSidecarError("history commit must be a 40-hex commit")
    if not isinstance(value.get("plan_digest"), str) or not SHA256_RE.fullmatch(
        str(value["plan_digest"])
    ):
        raise PlanSidecarError("history plan_digest must be a 64-hex SHA-256 digest")
    if not isinstance(value.get("detail"), str):
        raise PlanSidecarError("history detail must be a string")


def parse_history_record(data: object) -> HistoryRecord:
    if not isinstance(data, dict):
        raise PlanSidecarError("a history record must be a JSON object")
    _validate_history_record(data)
    return HistoryRecord(
        schema=str(data["schema"]),
        event=str(data["event"]),
        task_id=int(data["task_id"]) if data["task_id"] is not None else None,
        commit=str(data["commit"]),
        plan_digest=str(data["plan_digest"]),
        detail=str(data["detail"]),
    )


def parse_history(data: bytes) -> List[HistoryRecord]:
    """Parse a bounded JSONL history sidecar into ordered records."""
    if len(data) > SIDECAR_MAX_BYTES:
        raise PlanSidecarError("history sidecar exceeds its size cap")
    if not data:
        return []
    records: List[HistoryRecord] = []
    for line_number, raw in enumerate(data.split(b"\n"), start=1):
        if not raw:
            continue
        if len(raw) > RECORD_MAX_BYTES:
            raise PlanSidecarError(
                f"history line {line_number} exceeds the {RECORD_MAX_BYTES}-byte cap"
            )
        try:
            value = json.loads(
                raw.decode("utf-8"), object_pairs_hook=_reject_duplicate_keys
            )
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise PlanSidecarError(
                f"history line {line_number} is malformed: {exc}"
            ) from exc
        records.append(parse_history_record(value))
        if len(records) > RECORD_MAX_COUNT:
            raise PlanSidecarError(
                f"history exceeds the {RECORD_MAX_COUNT}-record cap"
            )
    return records


def serialize_history(records: Sequence[HistoryRecord]) -> bytes:
    """Deterministic JSONL serialization of history records."""
    lines: List[bytes] = []
    for record in records:
        _validate_history_record(record.to_dict())
        lines.append(
            json.dumps(
                record.to_dict(), sort_keys=True, ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
        )
    return b"\n".join(lines) + (b"\n" if lines else b"")


def read_history(root) -> List[HistoryRecord]:
    """Securely reopen and validate the committed history sidecar."""
    return parse_history(_read_sidecar(root, HISTORY_FILE))


def write_history(root, records: Sequence[HistoryRecord]) -> str:
    """Atomically write the history sidecar; returns its SHA-256 digest."""
    root = _as_root(root)
    data = serialize_history(records)
    _atomic_write_committed(_artifacts_dir(root) / HISTORY_FILE, data)
    return hashlib.sha256(data).hexdigest()


def append_history(root, record: HistoryRecord) -> str:
    """Append one history event (append-only).  Returns the new digest."""
    root = _as_root(root)
    existing = read_history(root)
    return write_history(root, [*existing, record])


# ---------------------------------------------------------------------------
# Digests and the composite plan binding
# ---------------------------------------------------------------------------


def sidecar_digest(data: bytes) -> str:
    """Content address of one sidecar document (SHA-256 of its exact bytes)."""
    return hashlib.sha256(data).hexdigest()


def plan_binding_digest(
    plan_bytes: bytes, archive_bytes: bytes, history_bytes: bytes
) -> str:
    """Composite digest binding the active plan plus both required sidecars.

    ``sha256(sha256(plan) | 0x00 | sha256(archive) | 0x00 | sha256(history))``
    — a change to the active plan or to either sidecar changes the binding,
    so the campaign's ``plan_digest`` (state, launch binding, lease claims)
    provably binds the whole plan-sidecar pair.
    """
    return hashlib.sha256(
        hashlib.sha256(plan_bytes).digest()
        + b"\x00"
        + hashlib.sha256(archive_bytes).digest()
        + b"\x00"
        + hashlib.sha256(history_bytes).digest()
    ).hexdigest()


def plan_sidecar_binding(
    plan_bytes: bytes,
    archive_records: Optional[Sequence[object]] = None,
) -> Dict[str, str]:
    """The ``sidecars:`` front-matter binding of a parsed v2 plan.

    Returns ``{"archive": <sha256>, "history": <sha256>}``.  Raises
    ``PlanSidecarError`` when the plan is not v2 or the binding is malformed.
    """
    try:
        from . import plan_parser  # deferred: plan_parser never imports this module
    except ImportError:  # flat-import mode used by the hidden harness suite
        import plan_parser  # type: ignore[no-redef]

    plan = plan_parser.Plan.from_bytes(
        plan_bytes, archive_records=archive_records
    )
    if plan.schema != "factory-plan/v2":
        raise PlanSidecarError(
            f"plan schema {plan.schema!r} has no sidecar binding"
        )
    if not plan.sidecars:
        raise PlanSidecarError("a v2 plan must carry a `sidecars:` binding")
    return dict(plan.sidecars)


def verify_sidecar_binding(
    plan_bytes: bytes,
    archive_bytes: bytes,
    history_bytes: bytes,
    archive_records: Optional[Sequence[object]] = None,
) -> None:
    """Fail closed when the plan's sidecar binding does not match the bytes."""
    binding = plan_sidecar_binding(plan_bytes, archive_records=archive_records)
    expected_archive = sidecar_digest(archive_bytes)
    expected_history = sidecar_digest(history_bytes)
    if binding.get("archive") != expected_archive:
        raise PlanSidecarBindingError(
            "the plan's archive sidecar binding does not match the committed "
            "archive bytes"
        )
    if binding.get("history") != expected_history:
        raise PlanSidecarBindingError(
            "the plan's history sidecar binding does not match the committed "
            "history bytes"
        )


def load_plan_binding(root, plan_bytes: bytes) -> Dict[str, object]:
    """Read both sidecars and verify the plan's binding against them.

    Returns ``{"archive": records, "history": records, "archive_digest": ...,
    "history_digest": ..., "binding_digest": ...}``.  A missing, malformed,
    or digest-mismatched sidecar fails closed.
    """
    root = _as_root(root)
    archive_bytes = _read_sidecar(root, ARCHIVE_FILE)
    history_bytes = _read_sidecar(root, HISTORY_FILE)
    archive_records = parse_archive(archive_bytes)
    verify_sidecar_binding(
        plan_bytes, archive_bytes, history_bytes,
        archive_records=archive_records,
    )
    return {
        "archive": archive_records,
        "history": parse_history(history_bytes),
        "archive_digest": sidecar_digest(archive_bytes),
        "history_digest": sidecar_digest(history_bytes),
        "binding_digest": plan_binding_digest(plan_bytes, archive_bytes, history_bytes),
    }
