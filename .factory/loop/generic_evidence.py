#!/usr/bin/env python3
"""Trusted generic evidence publisher (Task 23; EVID-01 §19, CTX-02 §18).

Task 23 owns the **live** installed-tier evidence for the generic harness at
the bound Task-23 commit: a clean exact-commit run of the installed harness
suite, a fresh live evidence namespace under
``.factory-state/generic-evidence/<exact-commit>/``, an installed-harness
machine receipt under the allowlisted ``installed-harness-smoke`` category
in ``.factory-state/audit-receipts/``, and the audit coordinator binding
(``.factory-state/audit-coordinator.json``) bridged from the *validated*
``factory-state/v1`` control state plus the exact clean HEAD — never from
Ralph state or the foreign adopting-product contents of the pre-existing
``.factory-state/``.

The publisher is strictly **two-stage**:

1. ``prepare`` (read-only): acquires the exclusive root-descriptor writer
   lock, validates the ``factory-state/v1`` control state and the exact
   clean HEAD, verifies every pre-publish path is absent (the generic
   namespace, the coordinator file, and the receipt paths must not
   preexist), snapshots every pre-existing ``.factory-state`` file's bytes
   digest / mode / mtime, and writes the staging record (commit, round,
   fresh nonce, state digest, and the before-snapshot) into a test-owned
   staging directory.  No ``.factory-state`` byte is ever written by
   ``prepare``.
2. ``publish`` (write stage): re-validates the exact clean HEAD and the
   staged binding, re-proves the before-snapshot still matches the live
   ``.factory-state``, runs the installed harness suite
   (``./.factory/tests/test-factory-installed.sh``) with a sanitized
   bounded environment (exit 0, no skip marker, bounded transcript, clean
   tree afterwards), and only then creates the audit coordinator state
   (no-overwrite), mints the installed-harness machine receipt through the
   trusted ``machine-receipt.py`` authority, validates the minted receipt
   (hardened no-follow validation, coordinator/commit/argv bindings,
   allowlisted category argv, byte digest), and *only after the receipt is
   accepted* publishes the installed-functional evidence record into the
   generic namespace (private 0700 directories, mode-0600 single-link
   no-replace artifacts) plus a preservation proof showing every
   pre-existing ``.factory-state`` file is byte/mode/mtime identical and
   the only additions are the new generic namespace, the coordinator path,
   and the receipt paths.

No model, runner, hardware, or human is ever invoked: the publisher is
deterministic control-plane code with a one-writer lock, and a failed or
skipped suite leaves no artifacts.  The evidence record binds the receipt
digest, the exact commit, and the coordinator round/nonce so
``scripts/check-installed-functional-evidence.sh`` can require a matching
installed-harness receipt before accepting the generic evidence.

**Crash recovery/resume (Task 23).** Every canonical write is atomic
no-replace, and a crashed run resumes deterministically on the next
``publish`` with the same staging record:

* the installed-harness receipt set is resumed only when it is *complete*
  and byte-exact to the staged artifacts (a torn set fails closed with no
  deletion); the authorizing coordinator is reused, never overwritten;
* a canonical empty/partial evidence namespace is finalized only when the
  exact staging record + the reused coordinator + the validated receipt +
  the expected record bytes all match (a tampered or foreign partial fails
  closed with no deletion); a completed namespace is a duplicate
  publication and fails closed;
* a fresh namespace is published as a **privately complete temp namespace**
  (owned 0700, record written and fsynced inside) moved onto the canonical
  name with Linux ``renameat2`` ``RENAME_NOREPLACE`` so the namespace
  appears atomically and can never clobber an existing one; the fallback
  (no ``renameat2`` support) publishes per-file with the same no-replace
  guarantee.  The publisher's own ``.partial-*`` temp namespace left by a
  SIGKILL is validated (exact expected record bytes) and resumed, or fails
  closed with no deletion.
* after the suite the ``.factory-state`` snapshot must be **fully
  unchanged** — no additions, mutations, or deletions at all — before the
  first coordinator/receipt/evidence canonical write, so a hostile passing
  suite that leaves an extra file can never produce canonical artifacts.
"""

from __future__ import annotations

import argparse
import ctypes
import fcntl
import hashlib
import importlib.util
import json
import os
import re
import shutil
import stat
import sys
import tempfile
import time
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

LOOP_DIR = Path(__file__).resolve().parent
ROOT = LOOP_DIR.parent.parent

EVIDENCE_NS = ".factory-state/generic-evidence"
COORDINATOR_FILE = ".factory-state/audit-coordinator.json"
RECEIPTS_DIR = ".factory-state/audit-receipts"
RECEIPT_TAG = "installed-harness-smoke"
SUITE_ARGV = ["./.factory/tests/test-factory-installed.sh"]
RECORD_NAME = "installed-functional.json"
PRESERVATION_NAME = "preservation.json"
STAGING_NAME = "staging.json"
EVIDENCE_SCHEMA = "factory-generic-installed-functional/v1"
STAGING_SCHEMA = "factory-generic-evidence-staging/v1"
PRESERVATION_SCHEMA = "factory-generic-preservation/v1"
COORDINATOR_SCHEMA = "ralph-audit-coordinator/v1"
POLICY_REL = ".factory/campaign-receipt-policy.json"
MACHINE_RECEIPT_REL = "scripts/machine-receipt.py"
CHECKER_REL = "scripts/check-installed-functional-evidence.sh"
STATE_FILE = ".factory-state/factory-loop.json"

SHA1 = re.compile(r"^[0-9a-f]{40}$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
# A skip marker in a certified transcript: a standalone SKIP/SKIPPED token
# optionally followed by a separator (``:``, ``,``, space, …) or end of
# line.  An embedded occurrence (``test_skipped``, ``SKIPPEDX``) is never a
# marker because the token must start at a non-word boundary.
SKIP_TOKEN = re.compile(r"(?<!\S)(?:SKIP|SKIPPED)(?:[^\w]|$)")

MAX_RECORD = 64 * 1024
MAX_SNAPSHOT = 16 * 1024 * 1024
MAX_SUITE_LOG = 4 * 1024 * 1024  # mirrors the machine-receipt bound
# The suite runs under the same bounded supervised runner the trusted
# machine-receipt wrapper uses (new session, subreaper, captured descendant
# scope, full TERM -> grace -> KILL -> reap, escaped-descendant detection)
# with the same 7200-second bound as the receipt/evidence campaign — the
# receipt and the publisher can never observe different time budgets.
SUITE_TIMEOUT = 7200

# Allowed *new* paths under `.factory-state` (relative to it) that a
# successful publication may add on top of the before-snapshot: the
# coordinator file, the installed-harness receipt artifacts, and the
# dedicated generic namespace.
ALLOWED_NEW_PREFIXES = (
    "audit-coordinator.json",
    "audit-receipts/",
    "generic-evidence/",
)

# The three canonical installed-harness receipt artifacts (stdout
# transcript, stderr transcript, and the receipt JSON record).
RECEIPT_ARTIFACT_NAMES = ("stdout", "stderr", "json")

# The hidden evidence, state, and git authorities are loaded by committed
# path (the same idiom state.py uses for scripts/factory_state_io.py), so
# the publisher works both as a package member and as a direct script.
sys.path.insert(0, str(LOOP_DIR))
import evidence as evidence_module  # noqa: E402
import gitutil  # noqa: E402
import state as state_module  # noqa: E402

_machine_spec = importlib.util.spec_from_file_location(
    "machine_receipt", ROOT / MACHINE_RECEIPT_REL
)
if _machine_spec is None or _machine_spec.loader is None:
    raise RuntimeError(f"cannot load {MACHINE_RECEIPT_REL}")
_machine = importlib.util.module_from_spec(_machine_spec)
_machine_spec.loader.exec_module(_machine)


class GenericEvidenceError(RuntimeError):
    """Base fail-closed error of the generic evidence publisher."""


def _sanitize_environment() -> None:
    """Strip every non-essential environment variable before any child spawn.

    The installed suite and every subprocess inherit the publisher's
    environment; a sanitized environment carries no source-tree path, no
    credential/cookie/session variable, no Git redirector family, and no
    campaign-binding variable, and bytecode writes are disabled so neither
    the repository nor any installed copy can ever gain a ``__pycache__``
    artifact.
    """
    minimal = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": os.environ.get("HOME", str(Path.home())),
        "TMPDIR": os.environ.get("TMPDIR", tempfile.gettempdir()),
        "LANG": os.environ.get("LANG", "C.UTF-8"),
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    os.environ.clear()
    os.environ.update(minimal)


class _WriterLock:
    """Exclusive flock on the canonical root directory descriptor (§12).

    The lock is taken on an already-open ``O_DIRECTORY|O_NOFOLLOW|O_CLOEXEC``
    descriptor of the canonical Git top-level directory (never a replaceable
    lockfile pathname), so exactly one writer can publish at a time and a
    second publisher fails closed immediately.
    """

    def __init__(self, root: Path) -> None:
        flags = (
            os.O_RDONLY
            | os.O_DIRECTORY
            | os.O_NOFOLLOW
            | getattr(os, "O_CLOEXEC", 0)
        )
        try:
            self._fd = os.open(root, flags)
        except OSError as exc:
            raise GenericEvidenceError(
                f"cannot open the canonical root no-follow: {exc}"
            ) from exc
        try:
            info = os.fstat(self._fd)
            if not stat.S_ISDIR(info.st_mode):
                raise GenericEvidenceError(
                    f"canonical root is not a directory: {root}"
                )
            try:
                fcntl.flock(self._fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise GenericEvidenceError(
                    "another writer holds the canonical root-descriptor "
                    "lock; exactly one generic evidence writer is allowed"
                ) from exc
            except OSError as exc:
                raise GenericEvidenceError(
                    f"cannot take the exclusive root-descriptor flock on "
                    f"{root}: {exc}"
                ) from exc
            self._identity = f"{info.st_dev:x}:{info.st_ino:x}"
        except BaseException:
            os.close(self._fd)
            self._fd = -1
            raise

    def close(self) -> None:
        if getattr(self, "_fd", -1) >= 0:
            try:
                os.close(self._fd)
            except OSError:
                pass
            self._fd = -1

    def __enter__(self) -> "_WriterLock":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()


def _check_state_directory(root: Path) -> Path:
    """Validate the private ``.factory-state`` directory (no-follow, owned)."""
    runtime = root / ".factory-state"
    if runtime.is_symlink() or not runtime.is_dir():
        raise GenericEvidenceError(
            ".factory-state must be a real private directory"
        )
    info = runtime.lstat()
    if info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise GenericEvidenceError("unsafe .factory-state directory (owner/mode)")
    if stat.S_IMODE(info.st_mode) != 0o700:
        raise GenericEvidenceError(
            "unsafe .factory-state directory (must be exactly 0700)"
        )
    return runtime


def _resolve_bindings(root: Path) -> Tuple[object, str, str]:
    """Validate the exact clean HEAD and the factory-state/v1 control state.

    Returns ``(state, head, branch)``.  The state is loaded through the
    hardened ``state.load_state`` authority (repository-identity revalidated,
    §11 field set and invariants enforced); the exact clean HEAD must be a
    strict 40-hex commit with an empty ``git status --porcelain``; and the
    state's write-once ``branch`` binding must equal the live branch.  No
    Ralph path or foreign ``.factory-state`` content is ever read here.
    """
    if not SHA1.fullmatch(gitutil.resolve_head(root) or ""):
        raise GenericEvidenceError("cannot resolve an exact 40-hex HEAD")
    head = gitutil.resolve_head(root)
    assert head is not None and SHA1.fullmatch(head)
    status = gitutil.git_run(
        ["-C", str(root), "status", "--porcelain"],
        timeout=gitutil.GIT_TIMEOUT,
    )
    if status.returncode != 0 or status.stdout.strip():
        raise GenericEvidenceError(
            "the working tree must be clean (git status --porcelain empty)"
        )
    state = state_module.load_state(root)
    branch = state_module.live_branch(root)
    if state.branch != branch:
        raise GenericEvidenceError(
            f"control state branch {state.branch!r} does not match the live "
            f"branch {branch!r}"
        )
    return state, head, branch


def _snapshot_state_files(root: Path) -> Dict[str, dict]:
    """Record every pre-existing ``.factory-state`` file's digest/mode/mtime.

    A regular file is recorded as ``{sha256, mode, mtime_ns}`` (nanosecond
    precision, so a same-second rewrite is still caught); a symlink is
    recorded as its link target so a symlink substitution is caught; any
    other special inode is recorded by mode.  The walk never follows
    symlinked directories.
    """
    base = root / ".factory-state"
    result: Dict[str, dict] = {}
    if not base.is_dir():
        return result
    for dirpath, dirnames, filenames in os.walk(base, followlinks=False):
        dirnames[:] = [
            name for name in dirnames
            if not os.path.islink(os.path.join(dirpath, name))
        ]
        for name in sorted(filenames):
            path = Path(dirpath) / name
            try:
                info = path.lstat()
            except OSError:
                continue
            rel = str(path.relative_to(base))
            if stat.S_ISLNK(info.st_mode):
                try:
                    result[rel] = {"symlink": os.readlink(path)}
                except OSError:
                    continue
                continue
            if not stat.S_ISREG(info.st_mode):
                result[rel] = {
                    "mode": format(stat.S_IMODE(info.st_mode), "04o")
                }
                continue
            try:
                raw = path.read_bytes()
            except OSError:
                continue
            result[rel] = {
                "sha256": hashlib.sha256(raw).hexdigest(),
                "mode": format(stat.S_IMODE(info.st_mode), "04o"),
                "mtime_ns": int(info.st_mtime_ns),
            }
    return result


def _snapshot_deltas(
    before: Dict[str, dict], after: Dict[str, dict]
) -> Tuple[bool, List[str], List[str], List[str]]:
    """Return ``(preserved, added, removed, changed)`` between snapshots.

    ``preserved`` is true exactly when every pre-existing file is still
    present with identical digest/mode/mtime.
    """
    added = sorted(set(after) - set(before))
    removed = sorted(set(before) - set(after))
    changed = sorted(
        key for key in before
        if key in after and before[key] != after[key]
    )
    preserved = not removed and not changed
    return preserved, added, removed, changed


def _atomic_write_noreplace(path: Path, data: bytes, *, mode: int) -> None:
    """Publish one mode-0600/0700 artifact with atomic no-replace semantics.

    Publication links the validated temporary inode into the canonical name
    (``os.link``, unlike ``rename``, cannot clobber), then the published
    inode is re-validated and the temporary unlinked.  An existing path, a
    symlink, or a raced pathname fails closed.
    """
    if path.is_symlink() or path.exists():
        raise GenericEvidenceError(
            f"refusing to replace an existing artifact (no-replace): {path}"
        )
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary_path = Path(temporary)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, mode)
        try:
            os.link(temporary, str(path))
        except FileExistsError:
            raise GenericEvidenceError(
                f"publication raced during no-replace publish: {path.name}"
            ) from None
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        info = path.lstat()
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or info.st_nlink != 1
            or stat.S_IMODE(info.st_mode) != mode
        ):
            raise GenericEvidenceError(
                f"published artifact is unsafe (owner/mode/link-count): {path}"
            )
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def _private_mkdir(path: Path, *, exclusive: bool) -> None:
    """Create one owned private 0700 directory (never following a symlink)."""
    try:
        info = path.lstat()
    except FileNotFoundError:
        info = None
    if info is not None:
        if exclusive:
            raise GenericEvidenceError(
                f"refusing to reuse an existing namespace directory: {path}"
            )
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise GenericEvidenceError(
                f"unsafe namespace path (symlink or not a directory): {path}"
            )
        if info.st_uid != os.getuid():
            raise GenericEvidenceError(f"foreign-owned namespace path: {path}")
        return
    try:
        os.mkdir(path, 0o700)
    except FileExistsError:
        if exclusive:
            raise GenericEvidenceError(
                f"namespace directory raced into existence: {path}"
            )
    except OSError as exc:
        raise GenericEvidenceError(
            f"cannot create the private namespace directory {path}: {exc}"
        ) from exc
    info = path.lstat()
    if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
        raise GenericEvidenceError(
            f"unsafe created namespace directory (owner/mode): {path}"
        )


def _evidence_record_bytes(
    head: str,
    receipt_ref: str,
    receipt_sha256: str,
    stdout_sha256: str,
    round_number: int,
    nonce: str,
) -> bytes:
    """The exact installed-functional evidence record bytes for a binding.

    The record is deterministic in every field: schema, exact commit,
    receipt reference/digest, suite stdout digest, and coordinator
    round/nonce.  A partial-namespace resume re-verifies an existing record
    against these bytes, so a tampered or foreign record can never be
    completed silently.
    """
    record = {
        "schema": EVIDENCE_SCHEMA,
        "commit": head,
        "test": "test_installed_functional",
        "result": "PASS",
        "skipped": 0,
        "receipt": receipt_ref,
        "receipt_sha256": receipt_sha256,
        "suite_stdout_sha256": stdout_sha256,
        "coordinator_round": round_number,
        "coordinator_nonce": nonce,
    }
    return (json.dumps(record, sort_keys=True, indent=2) + "\n").encode("utf-8")


def _renameat2_noreplace(source: Path, target: Path) -> bool:
    """Atomically move ``source`` onto ``target`` with RENAME_NOREPLACE.

    Uses the Linux ``renameat2`` ``RENAME_NOREPLACE`` primitive when the
    libc/fs provides it, so an existing (even empty) target can never be
    clobbered by the rename.  Returns ``False`` when the primitive is
    unavailable (the caller falls back to exclusive-dir + per-file
    no-replace publication); a raced or otherwise failed rename raises
    :class:`GenericEvidenceError`.
    """
    libc = ctypes.CDLL(None, use_errno=True)
    renameat2 = getattr(libc, "renameat2", None)
    if renameat2 is None:
        return False
    _AT_FDCWD = -100
    _RENAME_NOREPLACE = 1
    result = renameat2(
        _AT_FDCWD,
        str(source).encode("utf-8"),
        _AT_FDCWD,
        str(target).encode("utf-8"),
        _RENAME_NOREPLACE,
    )
    if result == 0:
        return True
    errno = ctypes.get_errno()
    if errno == getattr(ctypes, "errno").EEXIST:
        raise GenericEvidenceError(
            f"the generic evidence namespace target appeared during "
            f"publication (no-replace): {target.name}"
        )
    if errno in (
        getattr(ctypes, "errno").ENOSYS,
        getattr(ctypes, "errno").EINVAL,
        getattr(ctypes, "errno").ENOTSUP,
    ):
        # The running kernel/filesystem lacks renameat2 NOREPLACE support;
        # the caller falls back to the exclusive no-replace publication.
        return False
    raise GenericEvidenceError(
        f"cannot atomically publish the generic evidence namespace: "
        f"{os.strerror(errno)} ({source.name} -> {target.name})"
    )



def _verify_prepublish_paths_absent(root: Path, head: str) -> None:
    """Pre-publish path authority: nothing may preexist that would be
    overwritten or that would make a fresh publication ambiguous.

    The generic evidence namespace for this commit must not preexist
    (duplicate publication fails closed).  The coordinator may preexist
    **only** when it is an exact-matching binding (reused, never
    overwritten — checked later by :func:`_resolve_coordinator`); the
    receipts directory may preexist only as a real directory (its exact
    content is reconciled by the no-replace/resume authority at receipt
    publication time).  No pre-existing artifact is ever deleted.
    """
    coordinator = root / COORDINATOR_FILE
    if coordinator.is_symlink():
        raise GenericEvidenceError(
            "the audit coordinator path must never be a symlink"
        )
    receipts = root / RECEIPTS_DIR
    if receipts.is_symlink() or (receipts.exists() and not receipts.is_dir()):
        raise GenericEvidenceError("unsafe pre-existing audit-receipts path")
    generic = root / EVIDENCE_NS
    if generic.is_symlink() or (generic.exists() and not generic.is_dir()):
        raise GenericEvidenceError("unsafe pre-existing generic-evidence path")
    namespace = generic / head
    if namespace.is_symlink():
        raise GenericEvidenceError(
            "the generic evidence namespace must never be a symlink"
        )
    if not namespace.exists():
        return
    # A pre-existing namespace is accepted only when it is a **resumable
    # partial** of this same publication: a real owned 0700 directory
    # carrying at most the evidence record (whose bytes are re-verified
    # against the expected binding at publication time, together with the
    # exact staging record, the authorizing coordinator, and the validated
    # receipt).  A completed namespace (record + preservation proof) or any
    # unexpected file means a duplicate publication and fails closed.
    if not namespace.is_dir():
        raise GenericEvidenceError(
            "the generic evidence namespace for this commit is not a "
            "directory and must not preexist"
        )
    info = namespace.lstat()
    if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
        raise GenericEvidenceError(
            "unsafe pre-existing generic evidence namespace (owner/mode)"
        )
    entries = sorted(entry.name for entry in namespace.iterdir())
    if PRESERVATION_NAME in entries:
        raise GenericEvidenceError(
            "the generic evidence namespace for this commit already exists "
            "and must not preexist for a fresh publication (duplicate "
            "publication fails closed)"
        )
    if entries not in ([], [RECORD_NAME]):
        raise GenericEvidenceError(
            f"a partial generic evidence namespace carries unexpected files "
            f"({entries}); refusing to delete or overwrite it"
        )


def _read_staging(staging: Path) -> dict:
    """Read and structurally validate the staged prepare record."""
    if staging.is_symlink() or not staging.is_dir():
        raise GenericEvidenceError("staging directory is missing or unsafe")
    info = staging.lstat()
    if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
        raise GenericEvidenceError("staging directory must be owned and 0700")
    path = staging / STAGING_NAME
    if path.is_symlink() or not path.is_file():
        raise GenericEvidenceError("staging record is missing or unsafe")
    record_info = path.lstat()
    if (
        record_info.st_uid != os.getuid()
        or record_info.st_nlink != 1
        or record_info.st_mode & 0o022
    ):
        raise GenericEvidenceError("staging record is unsafe (owner/mode/link-count)")
    if record_info.st_size > MAX_SNAPSHOT:
        raise GenericEvidenceError("staging record exceeds the snapshot size bound")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise GenericEvidenceError(f"staging record is invalid: {exc}") from exc
    if not isinstance(data, dict) or data.get("schema") != STAGING_SCHEMA:
        raise GenericEvidenceError("staging record schema is invalid")
    return data


def _write_staging_record(staging: Path, record: dict) -> None:
    _atomic_write_noreplace(
        staging / STAGING_NAME,
        (json.dumps(record, sort_keys=True, indent=2) + "\n").encode("utf-8"),
        mode=0o600,
    )


def _suite_checks(stdout: bytes, stderr: bytes, exit_code: int) -> None:
    """The installed suite run must be bounded, exit 0, and never skip.

    Both certified channels are scanned: a skip marker in stdout **or**
    stderr disqualifies the run — a skipped suite is never certified PASS
    even when the skip note lands on the stderr channel.
    """
    if len(stdout) > MAX_SUITE_LOG or len(stderr) > MAX_SUITE_LOG:
        raise GenericEvidenceError(
            "the installed suite transcript exceeded the receipt bound"
        )
    if exit_code != 0:
        raise GenericEvidenceError(
            f"the installed harness suite exited nonzero ({exit_code}); no "
            "installed-functional evidence may be produced"
        )
    if not stdout.strip():
        raise GenericEvidenceError(
            "the installed harness suite produced no certified output"
        )
    if SKIP_TOKEN.search(stdout.decode("utf-8", "replace")) or SKIP_TOKEN.search(
        stderr.decode("utf-8", "replace")
    ):
        raise GenericEvidenceError(
            "the installed harness suite printed a skip marker (stdout or "
            "stderr); it can never be certified as PASS"
        )


def _policy_category_argv(root: Path) -> List[List[str]]:
    """The committed receipt-policy argv allowlist for the receipt tag."""
    path = root / POLICY_REL
    raw = evidence_module.secure_read_bytes(
        path, maximum=2 * 1024 * 1024, what="receipt policy"
    )[0]
    try:
        policy = json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise GenericEvidenceError(f"receipt policy is invalid: {exc}") from exc
    if not isinstance(policy, dict):
        raise GenericEvidenceError("receipt policy must be an object")
    categories = policy.get("categories")
    if not isinstance(categories, list):
        raise GenericEvidenceError("receipt policy has no categories list")
    for category in categories:
        if not isinstance(category, dict) or category.get("name") != RECEIPT_TAG:
            continue
        argv = category.get("argv")
        if not isinstance(argv, list) or not argv:
            raise GenericEvidenceError(
                f"receipt policy category {RECEIPT_TAG!r} must declare "
                "allowlisted argv arrays"
            )
        return [list(entry) for entry in argv]
    raise GenericEvidenceError(
        f"receipt policy has no allowlisted {RECEIPT_TAG!r} category"
    )


def _verify_receipt(
    root: Path,
    head: str,
    round_number: int,
    nonce: str,
    receipt_sha256: str,
    stdout_sha256: str,
) -> dict:
    """Accept the minted installed-harness receipt (or fail closed).

    The receipt must pass the hardened hidden validation, exit 0, bind the
    exact commit and the coordinator round/nonce, carry the allowlisted
    installed category argv, match its recorded byte digest, and its
    certified stdout transcript must show the suite's own digest with no
    skip marker.  Only after this acceptance may the installed-functional
    evidence record be published.
    """
    ref = f"{RECEIPTS_DIR}/{RECEIPT_TAG}.json"
    receipt = evidence_module.validate_receipt(root, ref)
    if receipt["exit_code"] != 0:
        raise GenericEvidenceError("the installed-harness receipt did not exit 0")
    if receipt["evidence_commit"] != head:
        raise GenericEvidenceError(
            "the installed-harness receipt is not bound to the exact HEAD"
        )
    if receipt["coordinator_round"] != round_number:
        raise GenericEvidenceError(
            "the installed-harness receipt coordinator round does not match "
            "the bridged audit coordinator"
        )
    if receipt["coordinator_nonce"] != nonce:
        raise GenericEvidenceError(
            "the installed-harness receipt coordinator nonce does not match "
            "the bridged audit coordinator"
        )
    if receipt["argv"] != SUITE_ARGV:
        raise GenericEvidenceError("the installed-harness receipt argv is not the suite argv")
    if not any(receipt["argv"] == allowed for allowed in _policy_category_argv(root)):
        raise GenericEvidenceError(
            "the installed-harness receipt argv is not allowlisted for "
            f"the {RECEIPT_TAG!r} installed category"
        )
    receipt_path = root / ref
    digest = hashlib.sha256(
        evidence_module.secure_read_bytes(
            receipt_path, maximum=1024 * 1024, what="installed-harness receipt"
        )[0]
    ).hexdigest()
    if digest != receipt_sha256:
        raise GenericEvidenceError(
            "the minted installed-harness receipt digest does not match the "
            "expected binding"
        )
    stdout_path = root / f"{RECEIPTS_DIR}/{RECEIPT_TAG}.stdout"
    transcript = evidence_module.secure_read_bytes(
        stdout_path, maximum=evidence_module.MAX_ARTIFACT,
        what="installed-harness stdout transcript",
    )[0]
    if hashlib.sha256(transcript).hexdigest() != stdout_sha256:
        raise GenericEvidenceError(
            "the installed-harness suite stdout digest does not match the "
            "expected binding"
        )
    if SKIP_TOKEN.search(transcript.decode("utf-8", "replace")):
        raise GenericEvidenceError(
            "the installed-harness suite stdout transcript contains a skip "
            "marker"
        )
    stderr_path = root / f"{RECEIPTS_DIR}/{RECEIPT_TAG}.stderr"
    stderr_transcript = evidence_module.secure_read_bytes(
        stderr_path, maximum=evidence_module.MAX_ARTIFACT,
        what="installed-harness stderr transcript",
    )[0]
    if hashlib.sha256(stderr_transcript).hexdigest() != receipt["stderr_sha256"]:
        raise GenericEvidenceError(
            "the installed-harness suite stderr digest does not match the "
            "recorded receipt"
        )
    if SKIP_TOKEN.search(stderr_transcript.decode("utf-8", "replace")):
        raise GenericEvidenceError(
            "the installed-harness suite stderr transcript contains a skip "
            "marker"
        )
    return receipt


def _create_coordinator(root: Path, round_number: int, head: str, nonce: str) -> None:
    """Bridge the audit coordinator from the validated state + exact HEAD.

    The coordinator state is published with no-replace semantics: an
    existing coordinator path (an active audit) is never overwritten.
    """
    path = root / COORDINATOR_FILE
    if path.is_symlink() or path.exists():
        raise GenericEvidenceError(
            "the audit coordinator state already exists; refusing to mint a "
            "new nonce for an active audit (no overwrite)"
        )
    data = {
        "schema": COORDINATOR_SCHEMA,
        "round": round_number,
        "base_commit": head,
        "nonce": nonce,
        "created_at": int(time.time()),
    }
    _atomic_write_noreplace(
        path,
        (json.dumps(data, sort_keys=True, indent=2) + "\n").encode("utf-8"),
        mode=0o600,
    )


def _read_coordinator(root: Path) -> dict:
    """Hardened read + schema validation of the audit coordinator state.

    A symlink, hardlink alias, foreign owner, wrong mode, oversized, or
    malformed coordinator fails closed exactly like the machine-receipt
    authority's own coordinator check.
    """
    path = root / COORDINATOR_FILE
    if path.is_symlink() or not path.is_file():
        raise GenericEvidenceError(
            "the audit coordinator state is missing or unsafe"
        )
    info = path.lstat()
    if (
        info.st_uid != os.getuid()
        or info.st_nlink != 1
        or info.st_mode & 0o022
        or stat.S_IMODE(info.st_mode) != 0o600
        or info.st_size > MAX_RECORD
    ):
        raise GenericEvidenceError(
            "the audit coordinator state is unsafe (owner/mode/link-count)"
        )
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise GenericEvidenceError(
            f"the audit coordinator state is invalid: {exc}"
        ) from exc
    expected = {"schema", "round", "base_commit", "nonce", "created_at"}
    if (
        not isinstance(data, dict)
        or set(data) != expected
        or data.get("schema") != COORDINATOR_SCHEMA
        or type(data.get("round")) is not int
        or data["round"] < 1
        or not isinstance(data.get("base_commit"), str)
        or not SHA1.fullmatch(data["base_commit"])
        or not isinstance(data.get("nonce"), str)
        or not SHA256.fullmatch(data["nonce"])
        or not isinstance(data.get("created_at"), int)
    ):
        raise GenericEvidenceError("the audit coordinator state is invalid")
    return data


def _resolve_coordinator(
    root: Path, record: dict
) -> str:
    """Reuse an existing exact-matching coordinator or stage our own.

    Returns the effective nonce.  An existing coordinator is **never
    overwritten**: it is accepted only when its hardened state is valid and
    its round and base_commit exactly match the staged binding, and its
    nonce is then reused (the active audit's authorization is preserved).  A
    mismatch fails closed.  A missing coordinator is created no-replace from
    the staged nonce.
    """
    path = root / COORDINATOR_FILE
    if path.is_symlink() or path.exists():
        data = _read_coordinator(root)
        if (
            data["round"] != record["round"]
            or data["base_commit"] != record["commit"]
        ):
            raise GenericEvidenceError(
                "an audit coordinator already exists whose round/commit does "
                "not match the staged binding ("
                f"round {data['round']}/{record['round']}, "
                f"base {data['base_commit'][:12]}/{record['commit'][:12]}); "
                "refusing to overwrite an active audit"
            )
        return data["nonce"]
    _create_coordinator(root, record["round"], record["commit"], record["nonce"])
    return record["nonce"]


def _receipt_artifacts(
    root: Path,
    round_number: int,
    head: str,
    nonce: str,
    exit_code: int,
    stdout: bytes,
    stderr: bytes,
    started: float,
) -> Dict[str, bytes]:
    """Build the exact installed-harness receipt artifact bytes.

    The receipt record is byte-for-byte the same ``ralph-audit-receipt/v1``
    shape the trusted ``machine-receipt.py`` wrapper records, so a staged
    artifact and a wrapper-minted artifact are interchangeable evidence.
    """
    argv = list(SUITE_ARGV)
    receipt = {
        "schema": "ralph-audit-receipt/v1",
        "tag": RECEIPT_TAG,
        "argv": argv,
        "argv_sha256": hashlib.sha256(
            json.dumps(argv, separators=(",", ":")).encode("utf-8")
        ).hexdigest(),
        "exit_code": exit_code,
        "stdout_sha256": hashlib.sha256(stdout).hexdigest(),
        "stderr_sha256": hashlib.sha256(stderr).hexdigest(),
        "started_at": int(started),
        "finished_at": int(time.time()),
        "evidence_commit": head,
        "coordinator_round": round_number,
        "coordinator_nonce": nonce,
    }
    return {
        "stdout": stdout,
        "stderr": stderr,
        "json": (
            json.dumps(receipt, sort_keys=True, indent=2) + "\n"
        ).encode("utf-8"),
    }


def _canonical_receipt_paths(root: Path) -> Dict[str, Path]:
    return {
        name: root / f"{RECEIPTS_DIR}/{RECEIPT_TAG}.{name}"
        for name in RECEIPT_ARTIFACT_NAMES
    }


def _validate_receipt_artifacts(
    root: Path,
    paths: Dict[str, Path],
    *,
    head: str,
    round_number: int,
    nonce: str,
) -> dict:
    """Validate a complete receipt artifact set (hardened, bindings, no skip).

    The artifacts must be regular single-link current-user-owned mode-0600
    files; the receipt JSON must conform to ``ralph-audit-receipt/v1`` with
    matching argv/digests; the exit code must be 0; the evidence commit,
    coordinator round, and coordinator nonce must exactly match the binding;
    the argv must be the allowlisted installed-harness suite argv; and
    **both** certified transcripts must carry no skip marker.  Returns the
    parsed receipt record.
    """
    payloads: Dict[str, bytes] = {}
    for name in RECEIPT_ARTIFACT_NAMES:
        path = paths[name]
        if path.is_symlink() or not path.is_file():
            raise GenericEvidenceError(
                f"receipt artifact is missing or unsafe: {path.name}"
            )
        info = path.lstat()
        if (
            info.st_uid != os.getuid()
            or info.st_nlink != 1
            or stat.S_IMODE(info.st_mode) != 0o600
        ):
            raise GenericEvidenceError(
                f"receipt artifact is unsafe (owner/mode/link-count): "
                f"{path.name}"
            )
        maximum = 1024 * 1024 if name == "json" else MAX_SUITE_LOG
        if info.st_size > maximum:
            raise GenericEvidenceError(
                f"receipt artifact exceeds the bound: {path.name}"
            )
        payloads[name] = path.read_bytes()
    try:
        receipt = json.loads(payloads["json"].decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise GenericEvidenceError(
            f"the installed-harness receipt is invalid: {exc}"
        ) from exc
    if not isinstance(receipt, dict):
        raise GenericEvidenceError("the installed-harness receipt must be an object")
    expected = {
        "schema", "tag", "argv", "argv_sha256", "exit_code",
        "stdout_sha256", "stderr_sha256", "started_at", "finished_at",
        "evidence_commit", "coordinator_round", "coordinator_nonce",
    }
    if (
        set(receipt) != expected
        or receipt.get("schema") != "ralph-audit-receipt/v1"
        or receipt.get("tag") != RECEIPT_TAG
    ):
        raise GenericEvidenceError("the installed-harness receipt schema is invalid")
    argv = receipt.get("argv")
    if not isinstance(argv, list) or not all(
        isinstance(item, str) and item for item in argv
    ):
        raise GenericEvidenceError("the installed-harness receipt argv is invalid")
    argv_digest = hashlib.sha256(
        json.dumps(argv, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    if argv != SUITE_ARGV or receipt.get("argv_sha256") != argv_digest:
        raise GenericEvidenceError(
            "the installed-harness receipt argv is not the exact suite argv "
            "or its digest mismatches"
        )
    if (
        hashlib.sha256(payloads["stdout"]).hexdigest()
        != receipt.get("stdout_sha256")
        or hashlib.sha256(payloads["stderr"]).hexdigest()
        != receipt.get("stderr_sha256")
    ):
        raise GenericEvidenceError(
            "the installed-harness receipt transcript digests do not match "
            "its artifacts"
        )
    if receipt.get("exit_code") != 0:
        raise GenericEvidenceError(
            "the installed-harness receipt did not exit 0"
        )
    if receipt.get("evidence_commit") != head:
        raise GenericEvidenceError(
            "the installed-harness receipt is not bound to the exact HEAD"
        )
    if receipt.get("coordinator_round") != round_number:
        raise GenericEvidenceError(
            "the installed-harness receipt coordinator round does not match "
            "the bridged audit coordinator"
        )
    if receipt.get("coordinator_nonce") != nonce:
        raise GenericEvidenceError(
            "the installed-harness receipt coordinator nonce does not match "
            "the bridged audit coordinator"
        )
    for field in ("started_at", "finished_at"):
        if type(receipt.get(field)) is not int:
            raise GenericEvidenceError(
                f"the installed-harness receipt {field} is invalid"
            )
    for channel in ("stdout", "stderr"):
        if SKIP_TOKEN.search(payloads[channel].decode("utf-8", "replace")):
            raise GenericEvidenceError(
                f"the installed-harness suite {channel} transcript contains "
                "a skip marker"
            )
    if not any(argv == allowed for allowed in _policy_category_argv(root)):
        raise GenericEvidenceError(
            "the installed-harness receipt argv is not allowlisted for "
            f"the {RECEIPT_TAG!r} installed category"
        )
    return receipt


def _stage_receipt_artifacts(
    staging: Path, artifacts: Dict[str, bytes]
) -> None:
    """Stage the receipt artifacts privately (0700 dir, 0600 no-replace).

    The staged bytes are validated **before** any canonical write; a
    conflicting staged artifact (a different crashed run's bytes) fails
    closed instead of being silently replaced.
    """
    directory = staging / "receipts"
    _private_mkdir(directory, exclusive=False)
    for name in RECEIPT_ARTIFACT_NAMES:
        path = directory / f"{RECEIPT_TAG}.{name}"
        if path.is_symlink() or path.exists():
            existing = path.read_bytes()
            if existing != artifacts[name]:
                raise GenericEvidenceError(
                    f"a conflicting staged receipt artifact already exists: "
                    f"{path.name}; refusing to replace it"
                )
            continue
        _atomic_write_noreplace(path, artifacts[name], mode=0o600)


def _publish_receipts_canonical(
    root: Path, staging: Path, artifacts: Dict[str, bytes],
    *, head: str, round_number: int, nonce: str,
) -> dict:
    """Publish the staged receipt artifacts canonically (no-replace).

    The canonical write is atomic no-replace per artifact.  A pre-existing
    canonical artifact is **never deleted**: it is accepted only when the
    whole set is complete and byte-exact to the staged bytes (a crash
    between staging and canonical publication resumes deterministically), and
    any partial or conflicting set fails closed for operator inspection.
    """
    receipts = root / RECEIPTS_DIR
    _private_mkdir(receipts, exclusive=False)
    canonical = _canonical_receipt_paths(root)
    existing_names = [
        name for name in RECEIPT_ARTIFACT_NAMES
        if canonical[name].is_symlink() or canonical[name].exists()
    ]
    if existing_names:
        if len(existing_names) != len(RECEIPT_ARTIFACT_NAMES):
            raise GenericEvidenceError(
                "a partial crash left some installed-harness receipt "
                f"artifacts ({existing_names}); refusing to delete or "
                "overwrite them — resolve the partial state explicitly"
            )
        for name in RECEIPT_ARTIFACT_NAMES:
            if canonical[name].read_bytes() != artifacts[name]:
                raise GenericEvidenceError(
                    "a complete installed-harness receipt set exists whose "
                    f"{name} artifact does not match this run's staged bytes; "
                    "refusing to overwrite or delete it"
                )
        return _validate_receipt_artifacts(
            root, canonical,
            head=head, round_number=round_number, nonce=nonce,
        )
    for name in RECEIPT_ARTIFACT_NAMES:
        _atomic_write_noreplace(canonical[name], artifacts[name], mode=0o600)
    return _validate_receipt_artifacts(
        root, canonical,
        head=head, round_number=round_number, nonce=nonce,
    )


def _mint_receipt(
    root: Path,
    staging: Path,
    round_number: int,
    head: str,
    nonce: str,
    exit_code: int,
    stdout: bytes,
    stderr: bytes,
    started: float,
) -> str:
    """Mint the installed-harness receipt through the trusted wrapper authority.

    ``machine-receipt.py``'s coordinator binding is re-validated (the
    protected coordinator state must authorize the exact round/base/nonce),
    the receipt artifacts are staged privately, validated, and only then
    published canonically with no-replace/resume semantics.  The minted
    artifact is byte-for-byte the same shape an audit coordinator's bounded
    invocation would produce.
    """
    binding_round, binding_commit, binding_nonce = _machine.coordinator_binding(
        root, round_number, head, nonce
    )
    artifacts = _receipt_artifacts(
        root, binding_round, binding_commit, binding_nonce,
        exit_code, stdout, stderr, started,
    )
    _stage_receipt_artifacts(staging, artifacts)
    _validate_receipt_artifacts(
        root,
        {
            name: staging / "receipts" / f"{RECEIPT_TAG}.{name}"
            for name in RECEIPT_ARTIFACT_NAMES
        },
        head=binding_commit,
        round_number=binding_round,
        nonce=binding_nonce,
    )
    _publish_receipts_canonical(
        root, staging, artifacts,
        head=binding_commit,
        round_number=binding_round,
        nonce=binding_nonce,
    )
    return f"{RECEIPTS_DIR}/{RECEIPT_TAG}.json"


def _prepare(root: Path, staging: Path) -> int:
    root = Path(root).absolute()
    staging = Path(staging).absolute()
    if staging.is_symlink():
        raise GenericEvidenceError("staging path must never be a symlink")
    if staging.exists():
        # A pre-existing staging directory must be a real owned private 0700
        # directory before any staging byte is written (E): a foreign-owned,
        # wrong-mode, or non-directory staging path fails closed.
        if not staging.is_dir():
            raise GenericEvidenceError(
                "staging path exists and is not a directory"
            )
        info = staging.lstat()
        if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
            raise GenericEvidenceError(
                "preexisting staging directory must be owned and exactly "
                "0700 before any write"
            )
        if (staging / STAGING_NAME).exists():
            raise GenericEvidenceError(
                "a staging record already exists; duplicate preparation fails "
                "closed"
            )
    else:
        staging.mkdir(mode=0o700)
        info = staging.lstat()
        if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
            raise GenericEvidenceError("cannot create a private staging directory")
    with _WriterLock(root):
        state, head, branch = _resolve_bindings(root)
        _check_state_directory(root)
        _verify_prepublish_paths_absent(root, head)
        before = _snapshot_state_files(root)
        nonce = hashlib.sha256(os.urandom(32)).hexdigest()
        record = {
            "schema": STAGING_SCHEMA,
            "commit": head,
            "branch": branch,
            "campaign_id": state.campaign_id,
            "round": state.current_round,
            "nonce": nonce,
            "state_digest": state_module.state_digest(state),
            "before": before,
        }
        _write_staging_record(staging, record)
    print(
        f"generic-evidence: prepared commit {head[:12]} round "
        f"{state.current_round} (staging: {staging})"
    )
    return 0


def _publish(root: Path, staging: Path) -> int:
    root = Path(root).absolute()
    staging = Path(staging).absolute()
    record = _read_staging(staging)
    if not isinstance(record.get("commit"), str) or not SHA1.fullmatch(record["commit"]):
        raise GenericEvidenceError("staging record has an invalid commit")
    if type(record.get("round")) is not int or record["round"] < 1:
        raise GenericEvidenceError("staging record has an invalid round")
    if not isinstance(record.get("nonce"), str) or not SHA256.fullmatch(record["nonce"]):
        raise GenericEvidenceError("staging record has an invalid nonce")
    before = record.get("before")
    if not isinstance(before, dict):
        raise GenericEvidenceError("staging record has an invalid before-snapshot")
    with _WriterLock(root):
        state, head, branch = _resolve_bindings(root)
        runtime = _check_state_directory(root)
        if record["commit"] != head:
            raise GenericEvidenceError(
                "the exact clean HEAD changed since prepare; refusing to publish"
            )
        if record["round"] != state.current_round:
            raise GenericEvidenceError(
                "the control-state round changed since prepare; refusing to publish"
            )
        if state_module.state_digest(state) != record.get("state_digest"):
            raise GenericEvidenceError(
                "the control-state digest changed since prepare; refusing to publish"
            )
        _verify_prepublish_paths_absent(root, head)
        current = _snapshot_state_files(root)
        preserved, added, removed, changed = _snapshot_deltas(before, current)
        if not preserved or removed or changed:
            raise GenericEvidenceError(
                "the pre-existing .factory-state changed between prepare and "
                "publish (digest/mode/mtime); foreign runtime files must be "
                "preserved byte-for-byte"
            )

        # Crash recovery/resume: when a previous run crashed after the
        # canonical receipt was published but before the evidence record,
        # the complete validated receipt artifact set is resumed
        # deterministically (never re-executed, never re-minted).  A partial
        # receipt set fails closed explicitly with no deletion.
        canonical_paths = _canonical_receipt_paths(root)
        existing_receipt_names = [
            name for name in RECEIPT_ARTIFACT_NAMES
            if canonical_paths[name].is_symlink()
            or canonical_paths[name].exists()
        ]
        resumed_receipt = None
        if existing_receipt_names:
            if len(existing_receipt_names) != len(RECEIPT_ARTIFACT_NAMES):
                raise GenericEvidenceError(
                    "a partial crash left a torn installed-harness receipt "
                    f"({existing_receipt_names}); refusing to delete or "
                    "overwrite it — resolve the partial state explicitly"
                )
            # The coordinator must preexist and exactly match the receipt
            # binding (a receipt without its authorizing coordinator is a
            # torn state); the existing coordinator is reused, never
            # overwritten.
            nonce = _resolve_coordinator(root, record)
            resumed_receipt = _validate_receipt_artifacts(
                root, canonical_paths,
                head=head, round_number=record["round"], nonce=nonce,
            )

        if resumed_receipt is None:
            # Stage 2 runs the installed harness suite with a sanitized
            # bounded supervised environment (new session, subreaper,
            # captured descendant scope, bounded 7200s timeout, full
            # TERM -> grace -> KILL -> reap, escaped-descendant detection);
            # any nonzero exit or skip marker on stdout **or** stderr aborts
            # before the first `.factory-state` write.
            started = time.time()
            exit_code, stdout, stderr, overflow = _machine.run_bounded(
                list(SUITE_ARGV), root
            )
            if overflow:
                raise GenericEvidenceError(
                    "the installed suite output exceeded the receipt bound"
                )
            _suite_checks(stdout, stderr, exit_code)
            status = gitutil.git_run(
                ["-C", str(root), "status", "--porcelain"],
                timeout=gitutil.GIT_TIMEOUT,
            )
            if status.returncode != 0 or status.stdout.strip():
                raise GenericEvidenceError(
                    "the installed suite dirtied the working tree; refusing "
                    "to publish"
                )
            still = _snapshot_state_files(root)
            still_preserved, still_added, still_removed, still_changed = (
                _snapshot_deltas(before, still)
            )
            if not still_preserved or still_added or still_removed or still_changed:
                raise GenericEvidenceError(
                    "the installed suite changed .factory-state (no "
                    "additions, mutations, or deletions at all); refusing to "
                    "publish — a hostile passing suite can never leave "
                    "canonical artifacts"
                )

            # Coordinator sequencing (C): an existing exact-matching
            # coordinator is reused (round and base_commit must match the
            # staged binding) or a fresh one is created no-replace; a
            # mismatch fails closed.  The effective nonce is the reused
            # coordinator's nonce when one exists — never a replacement of
            # an active audit's authorization.  Only a passing suite reaches
            # this first `.factory-state` write.
            nonce = _resolve_coordinator(root, record)

            # Only a passing suite proceeds to the receipt mint (staged
            # privately, validated, then published canonically no-replace
            # with byte-exact resume) and the evidence publication.
            receipt_ref = _mint_receipt(
                root, staging, record["round"], head, nonce,
                exit_code, stdout, stderr, started,
            )
            receipt_path = root / receipt_ref
            receipt_sha256 = hashlib.sha256(
                receipt_path.read_bytes()
            ).hexdigest()
            stdout_sha256 = hashlib.sha256(stdout).hexdigest()
            _verify_receipt(
                root, head, record["round"], nonce,
                receipt_sha256, stdout_sha256,
            )
        else:
            receipt_ref = f"{RECEIPTS_DIR}/{RECEIPT_TAG}.json"
            receipt_path = root / receipt_ref
            receipt_sha256 = hashlib.sha256(
                receipt_path.read_bytes()
            ).hexdigest()
            stdout_path = root / f"{RECEIPTS_DIR}/{RECEIPT_TAG}.stdout"
            stdout_sha256 = hashlib.sha256(
                evidence_module.secure_read_bytes(
                    stdout_path, maximum=evidence_module.MAX_ARTIFACT,
                    what="installed-harness stdout transcript",
                )[0]
            ).hexdigest()

        generic = root / EVIDENCE_NS
        _private_mkdir(generic, exclusive=False)
        namespace = generic / head
        record_bytes = _evidence_record_bytes(
            head, receipt_ref, receipt_sha256, stdout_sha256,
            record["round"], nonce,
        )
        if namespace.is_symlink():
            raise GenericEvidenceError(
                "the generic evidence namespace must never be a symlink"
            )
        if namespace.exists():
            # Canonical empty/partial namespace resume (crash between the
            # atomic namespace publication and the preservation proof): the
            # namespace is finalized only when the exact staging record + the
            # authorizing coordinator + the validated receipt + the expected
            # record bytes all match.  A tampered or foreign partial fails
            # closed with **no deletion** for operator inspection.
            if not namespace.is_dir():
                raise GenericEvidenceError(
                    "the generic evidence namespace is not a directory"
                )
            info = namespace.lstat()
            if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
                raise GenericEvidenceError(
                    "unsafe generic evidence namespace (owner/mode)"
                )
            entries = sorted(entry.name for entry in namespace.iterdir())
            if PRESERVATION_NAME in entries:
                raise GenericEvidenceError(
                    "the generic evidence namespace for this commit already "
                    "exists (duplicate publication fails closed)"
                )
            if entries not in ([], [RECORD_NAME]):
                raise GenericEvidenceError(
                    f"a partial generic evidence namespace carries unexpected "
                    f"files ({entries}); refusing to delete or overwrite it — "
                    "resolve the partial state explicitly"
                )
            if RECORD_NAME in entries:
                existing_path = namespace / RECORD_NAME
                if existing_path.is_symlink() or not existing_path.is_file():
                    raise GenericEvidenceError(
                        "the partial generic evidence record is unsafe "
                        "(symlink or not a regular file)"
                    )
                record_info = existing_path.lstat()
                if (
                    record_info.st_uid != os.getuid()
                    or record_info.st_nlink != 1
                    or stat.S_IMODE(record_info.st_mode) != 0o600
                ):
                    raise GenericEvidenceError(
                        "the partial generic evidence record is unsafe "
                        "(owner/mode/link-count)"
                    )
                if existing_path.read_bytes() != record_bytes:
                    raise GenericEvidenceError(
                        "a partial generic evidence record does not match the "
                        "expected binding bytes; refusing to overwrite or "
                        "delete it"
                    )
            else:
                _atomic_write_noreplace(
                    namespace / RECORD_NAME, record_bytes, mode=0o600
                )
        else:
            # Privately complete temp namespace + atomic no-replace rename:
            # the evidence record is fully written inside an owned 0700 temp
            # directory and moved onto the canonical namespace with Linux
            # renameat2 RENAME_NOREPLACE, so the canonical namespace appears
            # atomically and can never be clobbered.  A crashed previous run
            # that left our own private temp namespace is validated (exact
            # expected record bytes) and resumed; a tampered temp fails
            # closed with no deletion.  When renameat2 is unavailable the
            # fallback publishes per-file with the same no-replace guarantee.
            token = hashlib.sha256(
                (head + "\x00" + nonce).encode("utf-8")
            ).hexdigest()[:16]
            temp_ns = generic / f".partial-{token}"
            created_here = False
            try:
                if temp_ns.is_symlink() or temp_ns.exists():
                    if temp_ns.is_symlink() or not temp_ns.is_dir():
                        raise GenericEvidenceError(
                            f"the private temp namespace is unsafe: "
                            f"{temp_ns.name}"
                        )
                    temp_info = temp_ns.lstat()
                    if (
                        temp_info.st_uid != os.getuid()
                        or stat.S_IMODE(temp_info.st_mode) != 0o700
                    ):
                        raise GenericEvidenceError(
                            f"the private temp namespace is unsafe "
                            f"(owner/mode): {temp_ns.name}"
                        )
                    temp_entries = sorted(
                        entry.name for entry in temp_ns.iterdir()
                    )
                    if temp_entries != [RECORD_NAME]:
                        raise GenericEvidenceError(
                            f"the private temp namespace carries unexpected "
                            f"files ({temp_entries}); refusing to touch it"
                        )
                    temp_record = temp_ns / RECORD_NAME
                    if (
                        temp_record.is_symlink()
                        or not temp_record.is_file()
                        or temp_record.read_bytes() != record_bytes
                    ):
                        raise GenericEvidenceError(
                            "the private temp namespace record does not match "
                            "the expected binding bytes; refusing to touch it"
                        )
                else:
                    _private_mkdir(temp_ns, exclusive=True)
                    created_here = True
                    _atomic_write_noreplace(
                        temp_ns / RECORD_NAME, record_bytes, mode=0o600
                    )
                if not _renameat2_noreplace(temp_ns, namespace):
                    if namespace.is_symlink() or namespace.exists():
                        raise GenericEvidenceError(
                            "the generic evidence namespace target appeared "
                            "during publication (no-replace)"
                        )
                    _private_mkdir(namespace, exclusive=True)
                    _atomic_write_noreplace(
                        namespace / RECORD_NAME, record_bytes, mode=0o600
                    )
            finally:
                if created_here:
                    shutil.rmtree(temp_ns, ignore_errors=True)
        after = _snapshot_state_files(root)
        after_preserved, after_added, after_removed, after_changed = _snapshot_deltas(
            before, after
        )
        if not after_preserved or after_removed or after_changed:
            raise GenericEvidenceError(
                "a pre-existing .factory-state file changed during publication"
            )
        for rel in after_added:
            if not any(
                rel == prefix or rel.startswith(prefix)
                for prefix in ALLOWED_NEW_PREFIXES
            ):
                raise GenericEvidenceError(
                    f"unexpected new .factory-state path during publication: {rel}"
                )
        proof = {
            "schema": PRESERVATION_SCHEMA,
            "commit": head,
            "before": before,
            "after": after,
            "added": after_added,
            "removed": after_removed,
            "changed": after_changed,
            "preserved": after_preserved,
        }
        _atomic_write_noreplace(
            namespace / PRESERVATION_NAME,
            (json.dumps(proof, sort_keys=True, indent=2) + "\n").encode("utf-8"),
            mode=0o600,
        )
    print(
        f"generic-evidence: published {EVIDENCE_NS}/{head[:12]} "
        f"([receipt: {receipt_ref}])"
    )
    return 0


def _run(root: Path, staging: Optional[Path]) -> int:
    if staging is None:
        # Internal `run` mode: the staging directory is transient test-owned
        # state; it is always removed on every exit path (including a
        # failed publish), so an internal run never litters a staging
        # artifact.
        staging = Path(tempfile.mkdtemp(prefix="generic-evidence-staging."))
        os.chmod(staging, 0o700)
        try:
            _prepare(root, staging)
            return _publish(root, staging)
        finally:
            shutil.rmtree(staging, ignore_errors=True)
    _prepare(root, staging)
    return _publish(root, staging)


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python3 .factory/loop/generic_evidence.py",
        description="Trusted two-stage generic evidence publisher (Task 23)",
    )
    parser.add_argument("--root", default=str(ROOT))
    sub = parser.add_subparsers(dest="command", required=True)

    prepare = sub.add_parser("prepare", help="validate and snapshot (no writes)")
    prepare.add_argument("--staging", required=True)

    publish = sub.add_parser("publish", help="run the suite and publish evidence")
    publish.add_argument("--staging", required=True)

    run = sub.add_parser("run", help="prepare then publish")
    run.add_argument("--staging", default=None)

    args = parser.parse_args(argv)
    root = Path(args.root).absolute()
    _sanitize_environment()
    try:
        if args.command == "prepare":
            return _prepare(root, Path(args.staging))
        if args.command == "publish":
            return _publish(root, Path(args.staging))
        return _run(root, Path(args.staging) if args.staging else None)
    except GenericEvidenceError as exc:
        print(f"generic-evidence: {exc}", file=sys.stderr)
        return 1
    except state_module.StateError as exc:
        print(f"generic-evidence: control-state failure: {exc}", file=sys.stderr)
        return 1
    except evidence_module.EvidenceError as exc:
        print(f"generic-evidence: evidence failure: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
