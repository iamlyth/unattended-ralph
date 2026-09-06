#!/usr/bin/env python3
"""Hidden stdlib migration/deprecation authority (Task 15; MIG-01, CTX-02).

This module implements the generic-first migration of the Ralph Orchestrator
control plane (FACTORY-LOOP-SPEC §21) as a read-only, standard-library
authority of the hidden ``.factory/loop/`` package.  It never invokes Ralph,
never reads a Ralph runtime store, and never treats legacy state as a task,
memory, event, completion-token, or context-summary authority.

The migration derives exactly the authorities the new control plane accepts:

* the committed plan (``factory-plan/v1``) and its spec/commit/blob/base
  binding plus the exact plan digest (``plan_digest``);
* the current Git commit and the full dirty-work surface (preserved, never
  reset, never rewritten);
* the evidence artifacts already published under the ignored
  ``.factory-state/`` namespace (receipts, manifests, results — enumerated
  with strict stat metadata only, never credential content);
* the external blockers recorded in ``.factory/artifacts/blocked-facts.json``;
* the existing ``factory-state/v2`` control state when present (the single
  minimal mutable authority; the migration never creates a second one).

The migration surface reports, with **metadata only**, the legacy artifacts
that must not become new-path authorities:

* ``.ralph/`` runtime state — detected by directory presence (``lexists`` /
  ``lstat``); its tasks, memories, event streams, loop locks, and completion
  tokens are never opened, parsed, imported, or copied;
* ``.factory/artifacts/context-summary.md`` — presence only; the bytes are
  never read or validated, because FACTORY-LOOP-SPEC §5.2 forbids persisted
  context summaries as a competing task authority;
* the legacy workspace-scoped ``.ollama-usage-env`` credential store —
  presence and strict ``stat`` metadata (size, mode, owner, links) only; the
  bytes are never read, so the migration can never leak a credential and
  never accepts the legacy store as a credential authority.  The operator
  store of the new path lives outside the model workspace (Task 7 review).

``freeze`` (new Ralph launch freezing): the tracked marker
``.factory/ralph-freeze`` freezes new legacy Ralph campaign/planning/
implementation/audit/maintenance launches.  Deprecated visible
``.factory/tools/ralph-*`` entry points check the marker and refuse to start a new
Ralph control-plane cycle; the operator-only
``FACTORY_RALPH_FREEZE_OVERRIDE=1`` escape exists solely to recover an
already in-flight legacy cycle during migration.  Recovery itself
(``.factory/tools/ralph-recover.sh``) is not a new launch and stays usable.

The shell-level freeze gate is a best-effort presence check with an accepted
TOCTOU residual (Task 16): a same-uid local writer can delete the marker
between the shell's ``[ -f ]`` check and the launch it guards, so the shell
gate can never be a strong tamper boundary — only the hidden
:func:`is_ralph_frozen` re-stats the marker no-follow and fails closed on an
unsafe marker.  That residual is documented in ``docs/OPERATIONS.md`` and is
accepted for the Task 16 adversarial pass, not silently relied on.

``migrate`` (the operator translation step): derives the identical snapshot and
publishes the incremental evidence authority plus the initial ``factory-state/v2``
control state through the trusted no-replace authority
(``state.init_state``), which refuses to overwrite an existing control-state
file or a prior campaign's digest ledger — so the migration can never create a
second mutable control-state authority.  A migration report
(``factory-migration/v1``) is published atomically as ``migration.json``
inside the ignored ``.factory-state/`` evidence namespace (private 0700
directory, same no-follow state I/O); nothing is ever written to ``.ralph/``
or the tracked tree.

This module is part of the hidden control-plane package and is imported
exactly like its siblings (package mode from the hidden loop, flat mode from
the hidden ``.factory/tests/`` suite).  It uses only the Python standard
library plus the committed hidden-loop modules (``gitutil``, ``plan_parser``,
``state``) — never Ralph, never a subprocess, never a wall-clock timestamp
(the snapshot is a deterministic function of the repository and Git state).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path
import re
import stat
import subprocess
import sys
from typing import Dict, List, Mapping, Optional, Sequence, Tuple

try:  # package import (the hidden `.factory/loop/` package)
    from . import gitutil
    from . import plan_parser
    from . import state as state_module
except ImportError:  # flat import used by the hidden `.factory/tests/` suite
    import gitutil  # type: ignore[no-redef]
    import plan_parser  # type: ignore[no-redef]
    import state as state_module  # type: ignore[no-redef]

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MIGRATION_SCHEMA = "factory-migration/v1"
# The report lives directly inside the ignored evidence namespace: the state
# I/O authority publishes single-segment names under ``.factory-state/`` and
# never creates a second mutable directory tree, so ``migration.json`` is a
# flat evidence artifact next to the ledger/result files it enumerates.
REPORT_DIR = ".factory-state"
REPORT_NAME = "migration.json"

# The tracked freeze marker: its presence freezes new legacy Ralph launches.
FREEZE_MARKER_RELPATH = ".factory/ralph-freeze"
# Operator-only escape for recovering an in-flight legacy cycle (never a new
# launch).  Documented in docs/OPERATIONS.md.
FREEZE_OVERRIDE_ENV = "FACTORY_RALPH_FREEZE_OVERRIDE"

# The forbidden legacy authority paths the migration may only *detect*.
CONTEXT_SUMMARY_RELPATH = ".factory/artifacts/context-summary.md"
LEGACY_RALPH_NAMESPACE = ".ralph"
# Workspace- or repository-scoped legacy Ollama credential stores; never a
# new-path credential authority (the operator store lives outside the model
# workspace, Task 10 review).
LEGACY_ENV_STORE_RELPATHS = (".ollama-usage-env",)

DEFAULT_PLAN_PATH = ".factory/artifacts/implementation-plan.md"
DEFAULT_CAMPAIGN_ID = "migrated"

# Bounds for every file the migration reads or enumerates (finite, fail
# closed on overflow — never unbounded).  The per-kind Git blob caps exactly
# match the launch prompt-input limits (``launch.PROMPT_INPUT_MAX``, 1 MiB
# per input kind: role prompt, specification, implementation plan, audit
# objective), so the migration can never accept a blob that a real launch
# could not consume.  Every migration blob is size-pre-checked with the
# pinned ``git cat-file -s`` (finite bounded timeout, replace refs disabled)
# against its exact per-kind cap *before* any blob byte is read, and the
# subsequent read is a hard-bounded capture of at most the same cap.
PLAN_BLOB_MAX = 1024 * 1024
SPEC_BLOB_MAX = 1024 * 1024
ROLE_PROMPT_MAX = 1024 * 1024
AUDIT_OBJECTIVES_MAX = 1024 * 1024
# ``git cat-file -s`` prints a bare decimal byte count and a newline; a
# fixed tiny bound keeps even the size query a bounded capture, and the
# same bound covers the ``rev-parse`` 40-hex blob-ID outputs.
CAT_FILE_SIZE_MAX = 256
BLOB_ID_MAX = 256
BLOCKED_FACTS_MAX = 1024 * 1024
STATUS_PORCELAIN_MAX = 4 * 1024 * 1024
EVIDENCE_CAP = 20000
EVIDENCE_SCAN_CAP = 100000

SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
SHA40_RE = re.compile(r"^[0-9a-f]{40}$")

# The trusted harness runtime namespaces; untracked entries there are
# orchestrator-owned transient state, never role/preserved work (mirrors the
# campaign authority).  Tracked modifications still surface as dirty work.
HARNESS_RUNTIME_PREFIXES = (".factory-state", ".ralph", ".pi", "$tmp")

# The frozen legacy launch entry points that may not start a new Ralph cycle.
FROZEN_LAUNCHERS = (
    ".factory/tools/ralph-campaign.sh",
    ".factory/tools/ralph-plan.sh",
    ".factory/tools/ralph-run.sh",
    ".factory/tools/ralph-audit.sh",
    ".factory/tools/ralph-maintenance-plan.sh",
    ".factory/tools/ralph-maintenance-run.sh",
)


class MigrationError(Exception):
    """Base class for every fail-closed migration failure."""


class MigrationUnavailableError(MigrationError):
    """The migration authority cannot operate (no pinned Git, no root)."""


class MigrationSnapshotError(MigrationError):
    """A derived migration snapshot violates its contract or a bound."""


# ---------------------------------------------------------------------------
# Machine models
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LegacyEnvStore:
    """Strict *metadata-only* report of a legacy workspace env store.

    ``path`` is the repository-relative legacy path; every other field is a
    deterministic ``lstat`` value.  ``read_bytes`` is always ``False`` — the
    migration never opens the store, so a credential byte can never enter a
    snapshot, log, receipt, or process.  ``operator_action`` states what the
    operator must do (migrate the store outside the model workspace); the
    migration itself never moves, copies, or sources the file.
    """

    path: str
    present: bool
    is_regular: bool
    is_symlink: bool
    nlink: int
    size: int
    mode_octal: str
    uid: int
    mtime_ns: int
    read_bytes: bool = False
    operator_action: str = (
        "the legacy workspace credential store must be migrated by the "
        "operator to the external operator store (outside the model "
        "workspace); the migration and the new control plane never read it "
        "as a credential authority"
    )

    def to_dict(self) -> Dict[str, object]:
        return {
            "path": self.path,
            "present": self.present,
            "is_regular": self.is_regular,
            "is_symlink": self.is_symlink,
            "nlink": self.nlink,
            "size": self.size,
            "mode_octal": self.mode_octal,
            "uid": self.uid,
            "mtime_ns": self.mtime_ns,
            "read_bytes": self.read_bytes,
            "operator_action": self.operator_action,
        }


@dataclass(frozen=True)
class LegacySurface:
    """Presence-only surface of the legacy Ralph/context/summary/store.

    Nothing under these surfaces is ever imported; the migration records
    only that the legacy surface exists (so the operator knows the migration
    left it preserved as read-only recovery history).
    """

    ralph_runtime_present: bool
    context_summary_present: bool
    env_store: Optional[LegacyEnvStore]

    def to_dict(self) -> Dict[str, object]:
        return {
            "ralph_runtime_present": self.ralph_runtime_present,
            "context_summary_present": self.context_summary_present,
            "context_summary_read": False,
            "env_store": self.env_store.to_dict() if self.env_store else None,
        }


@dataclass(frozen=True)
class DirtyEntry:
    """One preserved dirty-work entry ``(status_flags, repo-relative path)``."""

    flags: str
    path: str
    harness_runtime_untracked: bool = False

    def to_dict(self) -> Dict[str, object]:
        return {
            "flags": self.flags,
            "path": self.path,
            "harness_runtime_untracked": self.harness_runtime_untracked,
        }


@dataclass(frozen=True)
class EvidenceEntry:
    """One evidence artifact under ``.factory-state/`` (metadata only)."""

    path: str
    size: int
    mode_octal: str
    uid: int
    nlink: int
    read_bytes: bool = False

    def to_dict(self) -> Dict[str, object]:
        return {
            "path": self.path,
            "size": self.size,
            "mode_octal": self.mode_octal,
            "uid": self.uid,
            "nlink": self.nlink,
            "read_bytes": self.read_bytes,
        }


@dataclass(frozen=True)
class MigrationSnapshot:
    """The deterministic migration snapshot (``factory-migration/v1``).

    The snapshot is a pure function of the repository Git state plus the
    committed plan/sidecars: plan bindings and digest, head commit, dirty
    work (preserved, enumerated never reset), evidence artifact metadata,
    structured external blockers, the existing ``factory-state/v2`` state
    (never created by the migration), and the presence-only legacy surface.
    No wall-clock timestamp and no model/Ralph-derived prose exists in the
    snapshot; the no-import contract fields are explicit and machine
    checkable.
    """

    schema: str
    head_commit: str
    plan_path: str
    plan_digest: str
    spec_path: str
    spec_commit: str
    spec_blob: str
    base_commit: str
    dirty: Tuple[DirtyEntry, ...]
    evidence: Tuple[EvidenceEntry, ...]
    blockers: Tuple[Mapping[str, object], ...]
    state: Optional[Mapping[str, object]]
    state_error: Optional[str]
    legacy: LegacySurface
    ralph_imported: bool = False
    context_summary_read: bool = False
    env_store_read: bool = False

    def to_dict(self) -> Dict[str, object]:
        return {
            "schema": self.schema,
            "head_commit": self.head_commit,
            "plan_path": self.plan_path,
            "plan_digest": self.plan_digest,
            "spec_path": self.spec_path,
            "spec_commit": self.spec_commit,
            "spec_blob": self.spec_blob,
            "base_commit": self.base_commit,
            "dirty": [entry.to_dict() for entry in self.dirty],
            "evidence": [entry.to_dict() for entry in self.evidence],
            "blockers": [dict(entry) for entry in self.blockers],
            "state": self.state,
            "state_error": self.state_error,
            "legacy": self.legacy.to_dict(),
            "no_import": {
                "ralph_imported": self.ralph_imported,
                "context_summary_read": self.context_summary_read,
                "env_store_read": self.env_store_read,
            },
        }

    def validate(self) -> None:
        if self.schema != MIGRATION_SCHEMA:
            raise MigrationSnapshotError(
                f"schema must be exactly {MIGRATION_SCHEMA!r}"
            )
        if not SHA40_RE.fullmatch(self.head_commit):
            raise MigrationSnapshotError("head_commit must be a 40-hex commit")
        if not SHA256_RE.fullmatch(self.plan_digest):
            raise MigrationSnapshotError("plan_digest must be a 64-hex digest")
        for name, value in (
            ("plan_path", self.plan_path),
            ("spec_path", self.spec_path),
        ):
            if not isinstance(value, str) or not value:
                raise MigrationSnapshotError(f"`{name}` must be non-empty")
        if not SHA40_RE.fullmatch(self.spec_commit) or not SHA40_RE.fullmatch(
            self.spec_blob
        ):
            raise MigrationSnapshotError(
                "spec_commit/spec_blob must be 40-hex Git object IDs"
            )
        if not SHA40_RE.fullmatch(self.base_commit):
            raise MigrationSnapshotError("base_commit must be a 40-hex commit")
        if self.ralph_imported or self.context_summary_read or self.env_store_read:
            raise MigrationSnapshotError(
                "the migration snapshot violates the no-import contract"
            )
        if self.state_error and self.state is not None:
            raise MigrationSnapshotError(
                "state and state_error are mutually exclusive"
            )


# ---------------------------------------------------------------------------
# Safe detection primitives (metadata only)
# ---------------------------------------------------------------------------


def _as_root(root) -> Path:
    """Validate the canonical repository root (no-follow, real directory)."""
    root = Path(root).absolute()
    try:
        info = os.stat(root, follow_symlinks=False)
    except OSError as exc:
        raise MigrationError(f"cannot stat repository root {root}: {exc}") from exc
    if not stat.S_ISDIR(info.st_mode):
        raise MigrationError(f"repository root is not a directory: {root}")
    return root


def is_ralph_frozen(root) -> bool:
    """True when the tracked freeze marker freezes legacy Ralph launches.

    A missing marker means *not frozen* (legacy semantics unchanged).  Any
    *present* marker that is not an ordinary non-symlink file — a symlink,
    FIFO, socket, device, or directory — fails closed with
    :class:`MigrationUnavailableError`: an unsafe marker must neither freeze
    nor silently unfreeze a launch.
    """
    root = _as_root(root)
    marker = root / FREEZE_MARKER_RELPATH
    try:
        info = os.lstat(marker)
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise MigrationUnavailableError(
            f"cannot inspect the freeze marker {marker}: {exc}"
        ) from exc
    if not stat.S_ISREG(info.st_mode):
        kind = "a symlink" if stat.S_ISLNK(info.st_mode) else "not a regular file"
        raise MigrationUnavailableError(
            f"the freeze marker {marker} is {kind}; it must be a regular file"
        )
    return True


def _lstat_meta(path: Path) -> Dict[str, object]:
    """Deterministic ``lstat`` metadata of one path (never the content)."""
    info = os.lstat(path)
    return {
        "present": True,
        "is_regular": stat.S_ISREG(info.st_mode),
        "is_symlink": stat.S_ISLNK(info.st_mode),
        "nlink": info.st_nlink,
        "size": info.st_size,
        "mode_octal": oct(stat.S_IMODE(info.st_mode)),
        "uid": info.st_uid,
        "mtime_ns": info.st_mtime_ns,
    }


def detect_legacy_env_store(root) -> Optional[LegacyEnvStore]:
    """Metadata-only detection of a workspace-scoped legacy credential store.

    The store bytes are never opened or read (``read_bytes`` stays false),
    so the migration cannot leak a credential into a snapshot, receipt, or
    log.  A symlinked legacy store is still *reported* (the operator must
    act) but is never followed.
    """
    root = _as_root(root)
    for relpath in LEGACY_ENV_STORE_RELPATHS:
        path = root / relpath
        try:
            info = os.lstat(path)
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise MigrationUnavailableError(
                f"cannot inspect the legacy credential store {path}: {exc}"
            ) from exc
        return LegacyEnvStore(
            path=relpath,
            present=True,
            is_regular=stat.S_ISREG(info.st_mode),
            is_symlink=stat.S_ISLNK(info.st_mode),
            nlink=info.st_nlink,
            size=info.st_size,
            mode_octal=oct(stat.S_IMODE(info.st_mode)),
            uid=info.st_uid,
            mtime_ns=info.st_mtime_ns,
            read_bytes=False,
        )
    return None


def _lexists(root: Path, relpath: str) -> bool:
    try:
        os.lstat(root / relpath)
        return True
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise MigrationUnavailableError(
            f"cannot inspect {relpath!r}: {exc}"
        ) from exc


def legacy_surface(root) -> LegacySurface:
    """Presence-only legacy surface (never reads any legacy byte)."""
    root = _as_root(root)
    ralph_present = _lexists(root, LEGACY_RALPH_NAMESPACE)
    summary_present = _lexists(root, CONTEXT_SUMMARY_RELPATH)
    return LegacySurface(
        ralph_runtime_present=ralph_present,
        context_summary_present=summary_present,
        env_store=detect_legacy_env_store(root),
    )


# ---------------------------------------------------------------------------
# Trusted read-only Git derivation
# ---------------------------------------------------------------------------


def _git(root: Path, argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
    try:
        return gitutil.git_run(
            ["-C", str(root), *argv], timeout=gitutil.GIT_TIMEOUT
        )
    except gitutil.GitBoundaryError as exc:
        raise MigrationUnavailableError(
            f"the pinned Git boundary failed during migration derivation: {exc}"
        ) from exc


def _git_bytes(
    root: Path, argv: Sequence[str], *, maximum: Optional[int] = None
) -> bytes:
    """Byte-preserving pinned Git output; raises on boundary failure.

    ``maximum`` turns the capture into a hard-bounded pipe read (never an
    unbounded capture); the default keeps the plain byte read, used only for
    structurally tiny outputs (``rev-parse``, ``cat-file -s``) that callers
    bound explicitly where required.
    """
    try:
        if maximum is None:
            result = gitutil.git_bytes(
                ["-C", str(root), *argv], timeout=gitutil.GIT_TIMEOUT
            )
        else:
            result = gitutil.git_bytes_bounded(
                ["-C", str(root), *argv],
                maximum=maximum,
                timeout=gitutil.GIT_TIMEOUT,
            )
    except gitutil.GitBoundaryError as exc:
        raise MigrationUnavailableError(
            f"the pinned Git boundary failed during migration derivation: {exc}"
        ) from exc
    if result.returncode != 0:
        raise MigrationUnavailableError(
            f"pinned Git command failed: {' '.join(argv)}"
        )
    return result.stdout


def _git_blob_bounded(
    root: Path, blob_id: str, maximum: int, what: str
) -> bytes:
    """Exact pre-checked bounded read of one committed migration blob (F1).

    The blob's *exact* size is first queried with the pinned
    ``git cat-file -s`` (finite bounded timeout; replace refs are disabled by
    the pinned Git boundary) and must satisfy the per-kind cap *before* any
    blob byte is read; only then is the blob content read with a hard-bounded
    capture (``git cat-file blob``, at most ``maximum`` bytes), and the
    captured length must equal the pre-checked exact size.  An oversized,
    malformed, or size-mutating blob fails closed before its bytes are ever
    returned — never an unbounded capture.
    """
    if not SHA40_RE.fullmatch(blob_id):
        raise MigrationUnavailableError(
            f"the {what} blob is not a 40-hex Git object ID: {blob_id!r}"
        )
    size_out = _git_bytes(
        root, ["cat-file", "-s", blob_id], maximum=CAT_FILE_SIZE_MAX
    )
    size_text = size_out.decode("utf-8", "replace").strip()
    if not re.fullmatch(r"[0-9]{1,12}", size_text):
        raise MigrationUnavailableError(
            f"cannot parse the exact size of the {what} blob "
            f"{blob_id}: {size_text!r}"
        )
    size = int(size_text)
    if size > maximum:
        raise MigrationUnavailableError(
            f"the {what} blob {blob_id} is {size} bytes, exceeding the "
            f"{maximum}-byte per-kind cap"
        )
    raw = _git_bytes(root, ["cat-file", "blob", blob_id], maximum=maximum)
    if len(raw) != size:
        raise MigrationUnavailableError(
            f"the {what} blob {blob_id} changed size between the exact size "
            f"pre-check and the bounded read ({size} != {len(raw)})"
        )
    return raw


def _head_commit(root: Path) -> str:
    result = _git(root, ["rev-parse", "--verify", "HEAD"])
    if result.returncode != 0:
        raise MigrationUnavailableError("cannot resolve HEAD of the repository")
    head = result.stdout.strip()
    if not SHA40_RE.fullmatch(head):
        raise MigrationUnavailableError("resolved HEAD is not a 40-hex commit")
    return head


def _committed_blob_id(root: Path, head: str, relpath: str, what: str) -> str:
    """The exact 40-hex blob ID of ``head:<relpath>`` (strict SHA, no refs).

    The blob ID is resolved with the pinned no-replace Git boundary and must
    be a strict 40-hex Git object ID before any byte is read: a ref, a
    non-hex string, or a replace-ref object fails closed here, so the
    per-kind bounded blob reads below can never be handed an ambiguous or
    redirectable object name (Task 15 F1).
    """
    try:
        raw = _git_bytes(
            root,
            ["rev-parse", f"{head}:{relpath}"],
            maximum=BLOB_ID_MAX,
        )
        blob = raw.decode("ascii").strip()
    except (MigrationUnavailableError, UnicodeDecodeError) as exc:
        raise MigrationUnavailableError(
            f"{what} {relpath!r} is not committed at the snapshot head"
        ) from exc
    if not SHA40_RE.fullmatch(blob):
        raise MigrationUnavailableError(
            f"{what} at {relpath!r} does not resolve to a strict 40-hex Git "
            f"object ID: {blob!r}"
        )
    return blob


def _plan_binding(root: Path, head: str, plan_path: str):
    """``(exact_blob_bytes, parsed_plan)`` of the committed plan at ``head``."""
    resolved = _git(root, ["rev-parse", f"{head}:{plan_path}"])
    if resolved.returncode != 0:
        raise MigrationUnavailableError(
            f"the plan path {plan_path!r} is not tracked at {head}"
        )
    blob = resolved.stdout.strip()
    if not SHA40_RE.fullmatch(blob):
        raise MigrationUnavailableError("the plan does not resolve to a blob")
    raw = _git_blob_bounded(root, blob, PLAN_BLOB_MAX, "plan")
    try:
        plan = plan_parser.Plan.from_bytes(raw)
    except plan_parser.PlanError as exc:
        raise MigrationUnavailableError(
            f"the committed plan at {head} does not parse: {exc}"
        ) from exc
    return raw, plan


def _dirty_entries(root: Path) -> Tuple[DirtyEntry, ...]:
    """Porcelain ``-z`` dirty-work entries (preserved, never reset)."""
    raw = _git_bytes(
        root,
        ["status", "--porcelain", "-z", "--untracked-files=all"],
        maximum=STATUS_PORCELAIN_MAX,
    )
    if len(raw) > STATUS_PORCELAIN_MAX:
        raise MigrationUnavailableError("the Git status is oversized")
    entries: List[DirtyEntry] = []
    index = 0
    while index < len(raw):
        end = raw.find(b"\x00", index)
        if end < 0:
            raise MigrationUnavailableError("malformed porcelain status stream")
        entry = raw[index:end]
        if len(entry) < 4:
            raise MigrationUnavailableError("malformed porcelain status entry")
        flags = entry[0:2]
        path_bytes = entry[3:]
        if flags[0:1] in (b"R", b"C"):
            # A rename/copy record is ``XY old\0new\0``; only the explicit
            # target is enumerated, never interpreted implicitly.
            raise MigrationUnavailableError(
                "rename/copy status entries are not explicit migration scope"
            )
        decoded = path_bytes.decode("utf-8", "replace")
        harness = flags == b"??" and any(
            decoded == prefix or decoded.startswith(prefix + "/")
            for prefix in HARNESS_RUNTIME_PREFIXES
        )
        entries.append(
            DirtyEntry(
                flags=flags.decode("ascii", "replace"),
                path=decoded,
                harness_runtime_untracked=harness,
            )
        )
        index = end + 1
    return tuple(entries)


def _evidence_entries(
    root: Path, *, _expected_uid: Optional[int] = None
) -> Tuple[EvidenceEntry, ...]:
    """Bounded metadata enumeration of evidence under ``.factory-state/``.

    Only evidence-shaped artifacts are recorded: audit receipts, runner
    evidence, signed manifests/signatures, campaign/phase results, the
    state-digest ledger, and migration reports.  Only path/size/mode/owner/
    link metadata is recorded — artifact bytes are never opened, so a signed
    artifact or a log with an embedded credential never enters the snapshot.

    Enumeration is fail-closed on unsafe stat metadata (F4): the private
    state directory itself must be owned by the expected uid with an exact
    private 0700 mode, and every enumerated artifact must be a regular
    non-symlink single-link file owned by the expected uid with no
    group/other write bits — a foreign-owned, writable, or hardlinked
    artifact fails the whole derivation, so the metadata enumeration can
    never bless a swapped or tampered evidence file.  ``_expected_uid`` is
    the underscore-private deterministic owner-check hook used by the hidden
    suite (established idiom in ``factory_state_io.py``); production always
    compares against the current UID.
    """
    state_dir = root / ".factory-state"
    expected_uid = os.getuid() if _expected_uid is None else _expected_uid
    try:
        info = os.lstat(state_dir)
    except FileNotFoundError:
        return ()
    except OSError as exc:
        raise MigrationUnavailableError(
            f"cannot inspect .factory-state: {exc}"
        ) from exc
    if (
        not stat.S_ISDIR(info.st_mode)
        or stat.S_ISLNK(info.st_mode)
        or info.st_uid != expected_uid
        or info.st_mode & 0o077
    ):
        raise MigrationUnavailableError(
            ".factory-state must be a private 0700 directory owned by the "
            "current uid (metadata enumeration)"
        )

    def _is_evidence(rel: Path) -> bool:
        parts = rel.parts
        if len(parts) >= 3 and parts[1] in ("audit-receipts", "runner-evidence",
                                            "migration"):
            return True
        if len(parts) == 2 and parts[0] == ".factory-state" and parts[1] in (
            "state-digest-ledger.jsonl",
            "runner-evidence.json",
            "phase-result.json",
            "audit-result.json",
            "installed-functional-evidence.env",
            "migration.json",
        ):
            return True
        if len(parts) == 2 and parts[0] == ".factory-state" and (
            parts[1].startswith("campaign-result-") or parts[1].endswith("-result.json")
        ):
            return True
        if rel.name in ("manifest.json", "manifest.sig"):
            return True
        return False

    entries: List[EvidenceEntry] = []
    scanned = 0
    for base, dirs, files in os.walk(state_dir, topdown=True, followlinks=False):
        dirs[:] = [name for name in sorted(dirs)]
        for name in sorted(files):
            scanned += 1
            if scanned > EVIDENCE_SCAN_CAP:
                raise MigrationUnavailableError(
                    "evidence scan exceeds the bounded walk cap"
                )
            path = Path(base) / name
            try:
                file_info = os.lstat(path)
            except FileNotFoundError:
                continue
            except OSError as exc:
                raise MigrationUnavailableError(
                    f"cannot inspect evidence artifact {path}: {exc}"
                ) from exc
            if not stat.S_ISREG(file_info.st_mode):
                continue
            rel = path.relative_to(root)
            if not _is_evidence(rel):
                # Only evidence-shaped artifacts are safety-checked and
                # enumerated here; the control-state file and the digest
                # ledger are owned by the state authority and are validated
                # by ``state_module`` (``_control_state``), never by the
                # evidence walk — a tampered state file is reported as
                # ``state_error``, not mis-enumerated as evidence.
                continue
            if (
                stat.S_ISLNK(file_info.st_mode)
                or file_info.st_uid != expected_uid
                or file_info.st_mode & 0o022
                or file_info.st_nlink != 1
            ):
                raise MigrationUnavailableError(
                    f"evidence artifact {path} is not a safe owned "
                    "single-link regular file (metadata enumeration)"
                )
            entries.append(
                EvidenceEntry(
                    path=rel.as_posix(),
                    size=file_info.st_size,
                    mode_octal=oct(stat.S_IMODE(file_info.st_mode)),
                    uid=file_info.st_uid,
                    nlink=file_info.st_nlink,
                    read_bytes=False,
                )
            )
            if len(entries) >= EVIDENCE_CAP:
                break
        if len(entries) >= EVIDENCE_CAP:
            break
    return tuple(entries)


class _DuplicateKeyError(ValueError):
    """A JSON object repeated a key (fail-closed duplicate-key document)."""


def _unique_object_pairs(
    pairs: Sequence[Tuple[str, object]]
) -> Dict[str, object]:
    """``json.loads`` ``object_pairs_hook`` rejecting any repeated key."""
    out: Dict[str, object] = {}
    for key, value in pairs:
        if key in out:
            raise _DuplicateKeyError(f"duplicate JSON object key: {key!r}")
        out[key] = value
    return out


# Every descriptor the migration opens is anchored at the trusted repository
# root and uses O_RDONLY|O_NOFOLLOW|O_CLOEXEC, so no pathname component can
# redirect the read and no descriptor survives an exec boundary.  O_NONBLOCK
# additionally turns a FIFO swap into an immediate fail-closed rejection
# instead of a blocking open.
_DIRFD_FLAGS = (
    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    | getattr(os, "O_CLOEXEC", 0)
)
_BLOCKER_OPEN_FLAGS = (
    os.O_RDONLY
    | os.O_NOFOLLOW
    | getattr(os, "O_CLOEXEC", 0)
    | getattr(os, "O_NONBLOCK", 0)
)


def _open_dirfd(anchor: int, name: str, *, what: str) -> int:
    """Open one trusted directory component anchored at ``anchor``.

    The component is statted no-follow and opened with ``O_DIRECTORY``/
    ``O_NOFOLLOW``/``O_CLOEXEC`` relative to the already-held descriptor, and
    the opened descriptor's ``dev:inode`` must equal the stat, so a directory
    swapped between the two syscalls fails closed.
    """
    try:
        info = os.stat(name, dir_fd=anchor, follow_symlinks=False)
    except OSError as exc:
        raise MigrationUnavailableError(
            f"cannot inspect {what}: {exc}"
        ) from exc
    if not stat.S_ISDIR(info.st_mode):
        raise MigrationUnavailableError(f"{what} is not a real directory")
    try:
        descriptor = os.open(name, _DIRFD_FLAGS, dir_fd=anchor)
    except OSError as exc:
        raise MigrationUnavailableError(
            f"cannot safely open {what}: {exc}"
        ) from exc
    try:
        opened = os.fstat(descriptor)
    except BaseException:
        os.close(descriptor)
        raise
    if (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
        os.close(descriptor)
        raise MigrationUnavailableError(f"{what} changed while being opened")
    return descriptor


def _validate_blockers_info(info: os.stat_result, *, expected_uid: int) -> None:
    """Fail-closed owner/mode/link identity of the blocker sidecar file."""
    if (
        not stat.S_ISREG(info.st_mode)
        or stat.S_ISLNK(info.st_mode)
        or info.st_uid != expected_uid
        or info.st_mode & 0o022
        or info.st_nlink != 1
        or info.st_size > BLOCKED_FACTS_MAX
    ):
        raise MigrationUnavailableError(
            "blocked-facts.json is not a safe owned single-link regular file"
        )


def _read_blockers(
    root: Path, *, _expected_uid: Optional[int] = None
) -> Tuple[Mapping[str, object], ...]:
    """Structured external blockers from the committed sidecar (never prose).

    The sidecar is read through dirfds anchored at the trusted repository root
    and its ``.factory/artifacts`` parent directory: every open uses
    ``O_RDONLY | O_NOFOLLOW | O_CLOEXEC`` (plus ``O_NONBLOCK`` so a FIFO swap
    fails closed instead of blocking), and the file's owner/mode/link-count and
    the name↔descriptor ``dev:inode`` pair are re-verified before and after
    the bounded descriptor read.  A symlink swap to any other file (including
    the legacy credential store) fails without ever opening the target.  JSON
    documents with duplicate object keys are rejected.

    ``_expected_uid`` is the underscore-private deterministic owner-check hook
    used by the hidden suite; production always compares against the current
    UID (established idiom in ``factory_state_io.py``).
    """
    expected = os.getuid() if _expected_uid is None else _expected_uid
    root = _as_root(root)
    root_fd = os.open(root, _DIRFD_FLAGS)
    try:
        factory_fd = _open_dirfd(root_fd, ".factory", what=".factory")
        try:
            artifacts_fd = _open_dirfd(
                factory_fd, "artifacts", what=".factory/artifacts"
            )
            try:
                try:
                    descriptor = os.open(
                        "blocked-facts.json", _BLOCKER_OPEN_FLAGS,
                        dir_fd=artifacts_fd,
                    )
                except FileNotFoundError:
                    return ()
                except OSError as exc:
                    raise MigrationUnavailableError(
                        f"cannot safely open blocked-facts.json: {exc}"
                    ) from exc
                try:
                    before = os.fstat(descriptor)
                    # The name↔descriptor identity is checked *before* the
                    # owner/mode validation: a replacement that already
                    # unlinked the opened inode (its ``st_nlink`` drops to
                    # zero) must be reported as a swap, not as a bad file.
                    named = os.stat(
                        "blocked-facts.json", dir_fd=artifacts_fd,
                        follow_symlinks=False,
                    )
                    if (before.st_dev, before.st_ino) != (
                        named.st_dev, named.st_ino
                    ):
                        raise MigrationUnavailableError(
                            "blocked-facts.json changed while opening"
                        )
                    _validate_blockers_info(before, expected_uid=expected)
                    chunks: List[bytes] = []
                    remaining = BLOCKED_FACTS_MAX + 1
                    while remaining:
                        chunk = os.read(descriptor, min(65536, remaining))
                        if not chunk:
                            break
                        chunks.append(chunk)
                        remaining -= len(chunk)
                    raw = b"".join(chunks)
                    after = os.fstat(descriptor)
                    named_after = os.stat(
                        "blocked-facts.json", dir_fd=artifacts_fd,
                        follow_symlinks=False,
                    )
                    if (
                        len(raw) > BLOCKED_FACTS_MAX
                        or (
                            before.st_dev, before.st_ino, before.st_size,
                            before.st_mtime_ns,
                        )
                        != (
                            after.st_dev, after.st_ino, after.st_size,
                            after.st_mtime_ns,
                        )
                        or (after.st_dev, after.st_ino)
                        != (named_after.st_dev, named_after.st_ino)
                    ):
                        raise MigrationUnavailableError(
                            "blocked-facts.json changed while reading"
                        )
                finally:
                    os.close(descriptor)
            finally:
                os.close(artifacts_fd)
        finally:
            os.close(factory_fd)
    finally:
        os.close(root_fd)
    try:
        data = json.loads(
            raw.decode("utf-8"), object_pairs_hook=_unique_object_pairs
        )
    except (UnicodeDecodeError, json.JSONDecodeError, _DuplicateKeyError) as exc:
        raise MigrationUnavailableError(
            f"blocked-facts.json is malformed: {exc}"
        ) from exc
    facts = data.get("facts") if isinstance(data, Mapping) else None
    if not isinstance(facts, list):
        raise MigrationUnavailableError("blocked-facts.json has no facts list")
    rows: List[Mapping[str, object]] = []
    for fact in facts:
        if not isinstance(fact, Mapping):
            raise MigrationUnavailableError("a blocked fact is not an object")
        rows.append(
            {
                "id": fact.get("id"),
                "title": fact.get("title"),
                "status": fact.get("status"),
                "requirements": fact.get("requirements"),
            }
        )
    return tuple(rows)


def _control_state(root: Path):
    """``(state_dict_or_None, error_or_None)`` of ``factory-state/v2``.

    The state file is read through the trusted state authority (no-follow,
    identity/mode checks) and *never written* by the migration.  A tampered
    or unsafe state file is surfaced as ``state_error`` (fail-closed
    reporting) so the operator decides; the migration itself never creates
    or repairs it.
    """
    state_file = root / ".factory-state" / state_module.STATE_FILE_NAME
    try:
        info = os.lstat(state_file)
    except FileNotFoundError:
        return None, None
    except OSError as exc:
        return None, f"cannot inspect the control-state file: {exc}"
    if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode):
        return (
            None,
            "the control-state file is not a regular non-symlink file",
        )
    try:
        loaded = state_module.load_state(root)
    except state_module.StateError as exc:
        return None, f"the control-state file failed closed: {exc}"
    state_data = loaded.to_dict()
    state_data["state_digest"] = state_module.state_digest(loaded)
    return state_data, None


# ---------------------------------------------------------------------------
# Snapshot derivation
# ---------------------------------------------------------------------------


def derive_migration_snapshot(
    root,
    *,
    plan_path: str = ".factory/artifacts/implementation-plan.md",
) -> MigrationSnapshot:
    """Derive the deterministic ``factory-migration/v1`` snapshot.

    The derivation is strictly read-only: the repository, the plan, the
    dirty worktree, the evidence artifacts, the blocker sidecar, and the
    ``factory-state/v2`` file is read; nothing is written and nothing is
    reset.  The legacy Ralph/context-summary/credential surfaces are
    detected with ``lstat`` metadata only and are never imported.
    """
    root = _as_root(root)
    head = _head_commit(root)
    plan_bytes, plan = _plan_binding(root, head, plan_path)
    plan_digest = hashlib.sha256(plan_bytes).hexdigest()
    dirty = _dirty_entries(root)
    evidence = _evidence_entries(root)
    blockers = _read_blockers(root)
    state_data, state_error = _control_state(root)
    legacy = legacy_surface(root)
    snapshot = MigrationSnapshot(
        schema=MIGRATION_SCHEMA,
        head_commit=head,
        plan_path=plan_path,
        plan_digest=plan_digest,
        spec_path=plan.spec_path,
        spec_commit=plan.spec_commit,
        spec_blob=plan.spec_blob,
        base_commit=plan.base_commit,
        dirty=dirty,
        evidence=evidence,
        blockers=blockers,
        state=state_data,
        state_error=state_error,
        legacy=legacy,
        ralph_imported=False,
        context_summary_read=False,
        env_store_read=False,
    )
    snapshot.validate()
    return snapshot


def write_migration_report(root, snapshot: MigrationSnapshot) -> str:
    """Atomically publish the migration report under the ignored evidence dir.

    Returns the repository-relative report path.  The report is evidence,
    never orchestration or a second mutable control authority, and never
    appears in ``.ralph/`` or the tracked tree.  It is written through the
    exact same no-follow private state I/O as the lifecycle markers (mode
    0600 file inside the 0700 ``.factory-state/`` directory).
    """
    root = _as_root(root)
    try:
        state_module.atomic_write_json(root, REPORT_NAME, snapshot.to_dict())
    except state_module.StateIOError as exc:
        raise MigrationUnavailableError(
            f"cannot publish the migration report: {exc}"
        ) from exc
    return f"{REPORT_DIR}/{REPORT_NAME}"


def migrate_control_state(
    root,
    *,
    campaign_id: str,
    rounds_requested: int,
    branch: Optional[str] = None,
    plan_path: str = DEFAULT_PLAN_PATH,
) -> None:
    """Operator command: translate the derived snapshot into ``factory-state/v2``.

    The initial state is written only through the trusted no-replace
    authority (``state.init_state``), which refuses to overwrite an existing
    state file or a second campaign's digest ledger — so the migration can
    never create a second mutable control-state authority.  No Ralph runtime
    task, memory, event, completion token, or context summary participates;
    the state carries exactly the §11 field set derived from the committed
    plan and current Git state.  Every campaign blob — specification, each
    role prompt, and the audit-objectives registry — is resolved to a strict
    40-hex object ID and read through the bounded no-replace blob helper
    (exact size pre-check against its per-kind cap *before* any byte is
    read, then a hard-bounded capture), so no oversized or swapped object
    can ever be digested.
    """
    root = _as_root(root)
    snapshot = derive_migration_snapshot(root, plan_path=plan_path)
    if branch is None:
        result = _git(root, ["rev-parse", "--abbrev-ref", "HEAD"])
        if result.returncode != 0:
            raise MigrationUnavailableError("cannot resolve the live branch")
        branch = result.stdout.strip()
    spec_data = _git_blob_bounded(
        root, snapshot.spec_blob, SPEC_BLOB_MAX, "specification"
    )
    prompt_digests: Dict[str, str] = {}
    for role in ("planner", "developer", "tester", "auditor"):
        rel = f".factory/prompts/{role}.md"
        blob = _committed_blob_id(
            root, snapshot.head_commit, rel, f"role prompt {role!r}"
        )
        prompt_digests[role] = hashlib.sha256(
            _git_blob_bounded(
                root, blob, ROLE_PROMPT_MAX, f"role prompt {role!r}"
            )
        ).hexdigest()
    audit_blob = _committed_blob_id(
        root,
        snapshot.head_commit,
        ".factory/audit-objectives/registry.json",
        "the audit-objectives registry",
    )
    audit_digest = hashlib.sha256(
        _git_blob_bounded(
            root, audit_blob, AUDIT_OBJECTIVES_MAX, "audit-objectives registry"
        )
    ).hexdigest()
    try:
        from . import campaign as campaign_module
    except ImportError:
        import campaign as campaign_module  # type: ignore[no-redef]
    registry, implementation_digests, hook_digest = (
        campaign_module._derive_pre_round_binding(
            root, bound_commit=snapshot.head_commit
        )
    )
    del registry, implementation_digests
    state_module.init_state(
        root,
        campaign_id=campaign_id,
        rounds_requested=rounds_requested,
        specification_digest=hashlib.sha256(spec_data).hexdigest(),
        plan_digest=snapshot.plan_digest,
        role_prompt_digests=prompt_digests,
        audit_objectives_digest=audit_digest,
        pre_round_hook_configuration_digest=hook_digest,
        pre_round_hook_commit=snapshot.head_commit,
        phase_base_commit=snapshot.head_commit,
        branch=branch,
    )
    # Evidence of the completed translation: the same deterministic snapshot
    # that was derived read-only is published as the migration report.  The
    # report is never written to ``.ralph/`` and never touches a legacy byte.
    write_migration_report(root, snapshot)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="factory-migration",
        description=(
            "Generic Ralph-control-plane migration/deprecation (FACTORY-LOOP-"
            "SPEC §21; Task 15). Read-only derivation; never imports Ralph "
            "runtime state, context summaries, or workspace credential "
            "stores."
        ),
    )
    parser.add_argument(
        "--root",
        metavar="ROOT",
        default=str(Path(__file__).resolve().parent.parent.parent),
        help="canonical repository root (default: this repository)",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_derive = sub.add_parser("derive", help="derive the migration snapshot")
    p_derive.add_argument(
        "--plan-path", default=DEFAULT_PLAN_PATH,
        help="repository-relative canonical plan path",
    )
    p_derive.add_argument(
        "--no-report", action="store_true",
        help="print the snapshot without publishing the evidence report",
    )

    p_status = sub.add_parser("status", help="freeze/legacy-surface status")

    p_freeze = sub.add_parser(
        "freeze", help="print whether new Ralph launches are frozen"
    )
    p_freeze.add_argument(
        "--guard",
        action="store_true",
        help=(
            "exit-code freeze guard for the deprecated shell launchers: "
            "exit 0 when frozen, exit 1 when not frozen, exit 2 when the "
            "marker is unsafe or the authority is unavailable (fail closed)"
        ),
    )

    p_migrate = sub.add_parser(
        "migrate", help="translate the snapshot into the factory-state/v2 file"
    )
    p_migrate.add_argument("--campaign-id", default=DEFAULT_CAMPAIGN_ID)
    p_migrate.add_argument("--rounds", type=int, required=True)
    p_migrate.add_argument("--branch", default=None)
    p_migrate.add_argument("--plan-path", default=DEFAULT_PLAN_PATH)

    args = parser.parse_args(argv)
    root = Path(args.root)
    try:
        if args.command == "derive":
            snapshot = derive_migration_snapshot(root, plan_path=args.plan_path)
            if not args.no_report:
                write_migration_report(root, snapshot)
            print(json.dumps(snapshot.to_dict(), sort_keys=True, indent=2))
            return 0
        if args.command == "status":
            frozen = is_ralph_frozen(root)
            surface = legacy_surface(root)
            print(
                json.dumps(
                    {
                        "schema": MIGRATION_SCHEMA,
                        "freeze_active": frozen,
                        "freeze_marker": FREEZE_MARKER_RELPATH,
                        "freeze_override": FREEZE_OVERRIDE_ENV,
                        "legacy": surface.to_dict(),
                    },
                    sort_keys=True, indent=2,
                )
            )
            return 0
        if args.command == "freeze":
            # Task 16: ``--guard`` is the retained-authority freeze gate for
            # the deprecated shell launchers.  The decision is a single
            # no-follow re-stat in this process (never a cached ``[ -f ]``
            # shell check): frozen -> 0, not frozen -> 1, unsafe/unavailable
            # -> 2 (fail closed), so a same-uid local writer cannot widen the
            # check-to-launch window with a stale or unsafe marker reading.
            if args.guard:
                if not is_ralph_frozen(root):
                    return 1
                return 0
            print("frozen" if is_ralph_frozen(root) else "not-frozen")
            return 0
        if args.command == "migrate":
            migrate_control_state(
                root,
                campaign_id=args.campaign_id,
                rounds_requested=args.rounds,
                branch=args.branch,
                plan_path=args.plan_path,
            )
            print(
                json.dumps(
                    {
                        "schema": "factory-migration/v1",
                        "action": "migrated",
                        "state_file": ".factory-state/"
                        + state_module.STATE_FILE_NAME,
                    },
                    sort_keys=True, indent=2,
                )
            )
            return 0
    except MigrationError as exc:
        print(f"factory-migration: {exc}", file=sys.stderr)
        return 2
    except state_module.StateError as exc:
        print(f"factory-migration: control state failed closed: {exc}", file=sys.stderr)
        return 2
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
