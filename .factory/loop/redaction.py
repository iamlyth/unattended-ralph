#!/usr/bin/env python3
"""Control-plane output redaction authority (Task 11; CRED-01, §18).

Every child/tool/gate output channel of the trusted control plane —
bounded launch captures (``launch._BoundedStream`` tails), deterministic
gate output and acceptance-command output (``campaign._run_gate`` /
``campaign._acceptance_gate``) — is redacted **through the committed
credential guard** (``.factory/tools/credential-guard.py``) before it can reach
results, logs, receipts, or repository state.  The guard is the single
redaction authority: the control plane never reimplements masking (§18:
"the retained extension contains only required credential enforcement ..."),
so a synthetic credential rendered into any output channel disappears from
every downstream artifact and a redaction failure fails closed.

Guard-source binding (co-owned with Task 8; Task 7 review obligation 3):
the executing guard is always the **exact committed blob** of
``.factory/tools/credential-guard.py`` at the bound commit.  A missing,
oversized, symlinked, foreign, or byte-divergent working-tree guard fails
closed before any output is redacted and before any gate runs — an
operator-claimed or caller-controlled guard source is never executed.
``usage.py`` / ``usage_fetch.py`` (the Ollama usage-guard source) remain
bound by the Task 8 real-confinement proof authority
(``workspace_confinement._executing_guard_source_digests``); this module
owns the credential-guard redaction source binding.

The module is a hidden ``.factory/loop/`` control-plane module, importable
both as a package member (``factory.loop.redaction``) and as a flat module
(the hidden ``.factory/tests/`` suite), mirroring its siblings.
"""

from __future__ import annotations

import hashlib
import importlib.util
import os
import re
import stat
import types
from pathlib import Path
from typing import Tuple

try:  # package import (the hidden `.factory/loop/` package)
    from . import gitutil
except ImportError:  # flat import used by the hidden `.factory/tests/` suite
    import gitutil  # type: ignore[no-redef]

__all__ = [
    "GIT_BLOB_TIMEOUT",
    "MAX_GUARD_SOURCE_BYTES",
    "REDACTION_FAILED",
    "REDACTION_GUARD_RELPATH",
    "KeyBlockState",
    "OutputRedactionError",
    "Redactor",
    "read_worktree_guard_source",
    "redactor_for",
    "redactor_from_bytes",
    "redact_text",
    "scan_key_block_state",
]

# The committed credential guard every control-plane redaction channel runs
# through (Task 11: redaction is never reimplemented in the control plane).
REDACTION_GUARD_RELPATH = ".factory/tools/credential-guard.py"

# Fail-closed marker a redacted channel carries when the guard cannot mask it
# (mirrors the Pi extension's ``[REDACTION FAILED]`` contract).
REDACTION_FAILED = "[REDACTION FAILED]"

# Bounds: the guard source is a small committed script; the working-tree and
# committed-blob reads are both bounded so an oversized or substituted blob
# fails closed instead of consuming unbounded work.
MAX_GUARD_SOURCE_BYTES = 4 * 1024 * 1024
GIT_BLOB_TIMEOUT = 30.0

# Private-key block markers, byte-identical to the guard's own
# ``_KEY_BEGIN_RE`` / ``_KEY_END_RE`` (``.factory/tools/credential-guard.py``).  The
# control plane tracks the *streaming block state* of captured output so a
# private-key block that straddles a capture boundary is still masked by the
# guard; it never reimplements the masking itself.
_KEY_BEGIN_RE = re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----")
_KEY_END_RE = re.compile(r"-----END [A-Z0-9 ]*PRIVATE KEY-----")

# The exact synthetic BEGIN marker the guard renders for a key-block start.
_KEY_BLOCK_START_MARKER = "[REDACTED-PRIVATE-KEY-BLOCK]"

# Retained split-marker window: ``scan_key_block_state`` returns only the
# trailing partial line (the line fragment that straddles a capture boundary)
# capped to this many bytes.  A BEGIN/END marker is at most ~60 bytes, so the
# window keeps every split-marker byte that could complete at the retained
# tail's start while bounding the partial line itself (a single giant line of
# child output must never grow the retained state without bound).  The block
# *state* is still computed over the full input before the window is applied,
# so a block opened anywhere in the evicted region is always seeded.
KEY_BLOCK_WINDOW = 512


class OutputRedactionError(Exception):
    """The committed credential guard cannot be verified or executed.

    Fail-closed: when the redaction authority is unavailable, no gate runs
    and no launch captures child output — a credential could otherwise reach
    results, logs, receipts, or repository state.
    """


KeyBlockState = Tuple[bool, str]


def scan_key_block_state(
    in_block: bool, partial: str, data: bytes
) -> KeyBlockState:
    """Incremental private-key block scan mirroring the guard's stream state.

    ``data`` is one byte chunk; ``partial`` is the trailing partial line of
    the previous chunk (prepended so a BEGIN/END marker split exactly at a
    chunk boundary is recognized once its completion arrives).  Returns
    ``(in_block_after, trailing_partial_line)``.  The state machine matches
    the guard's ``_redact_line`` transitions (an END closes a block; a BEGIN
    opens one; inside a block only END is considered), so the tracked state
    always agrees with what the guard's masker will do.
    """
    text = partial + data.decode("utf-8", "replace")
    if "\n" in text:
        head, _, tail_line = text.rpartition("\n")
    else:
        head, tail_line = "", text
    if head:
        for line in (head + "\n").splitlines():
            if in_block:
                if _KEY_END_RE.search(line):
                    in_block = False
            else:
                if _KEY_BEGIN_RE.search(line):
                    in_block = True
    if tail_line:
        if in_block:
            if _KEY_END_RE.search(tail_line):
                in_block = False
        else:
            if _KEY_BEGIN_RE.search(tail_line):
                in_block = True
        # Cap the returned trailing partial line *after* the state check, so
        # a marker anywhere in the evicted fragment still advances the block
        # state while the retained split-marker bytes stay bounded.
        if len(tail_line) > KEY_BLOCK_WINDOW:
            tail_line = tail_line[-KEY_BLOCK_WINDOW:]
    return in_block, tail_line


def _read_worktree_source(root: Path, relpath: str, maximum: int) -> bytes:
    """Anchored no-follow bounded read of one committed-script worktree file.

    Never follows a symlink (``O_NOFOLLOW``), never accepts a non-regular or
    foreign-owned file, and never accepts group/other-writable bytes — the
    same fail-closed shape the control plane applies to every committed
    authority it executes (F5-style anchored reads).
    """
    path = Path(root).absolute() / relpath
    flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        descriptor = os.open(str(path), flags)
    except OSError as exc:
        raise OutputRedactionError(
            f"cannot open the credential guard source {path}: {exc}"
        ) from exc
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise OutputRedactionError(
                f"the credential guard source {path} is not a regular file"
            )
        if before.st_uid != os.getuid():
            raise OutputRedactionError(
                f"the credential guard source {path} is not owned by the "
                "control-plane user"
            )
        if before.st_mode & 0o022:
            raise OutputRedactionError(
                f"the credential guard source {path} is group/other-writable"
            )
        if before.st_size > maximum:
            raise OutputRedactionError(
                f"the credential guard source {path} exceeds the {maximum}-byte "
                "bound"
            )
        remaining = before.st_size
        data = bytearray()
        while remaining:
            chunk = os.read(descriptor, min(65536, remaining))
            if not chunk:
                break
            data.extend(chunk)
            remaining -= len(chunk)
        after = os.fstat(descriptor)
        if (
            before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns
        ) != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns):
            raise OutputRedactionError(
                f"the credential guard source {path} changed while being read"
            )
        if len(data) != before.st_size:
            raise OutputRedactionError(
                f"the credential guard source {path} changed size while being read"
            )
        return bytes(data)
    finally:
        os.close(descriptor)


def _committed_guard_bytes(workspace: Path, bound_commit: str) -> bytes:
    """The exact committed ``.factory/tools/credential-guard.py`` blob at ``bound_commit``."""
    if (
        not isinstance(bound_commit, str)
        or len(bound_commit) != 40
        or any(ch not in "0123456789abcdef" for ch in bound_commit)
    ):
        raise OutputRedactionError(
            "the credential-guard source binding requires a 40-hex bound commit"
        )
    try:
        result = gitutil.git_bytes(
            [
                "-C", str(Path(workspace).absolute()),
                "show", f"{bound_commit}:{REDACTION_GUARD_RELPATH}",
            ],
            timeout=GIT_BLOB_TIMEOUT,
        )
    except gitutil.GitBoundaryError as exc:
        raise OutputRedactionError(
            f"cannot read the committed credential guard blob: {exc}"
        ) from exc
    if result.returncode != 0:
        raise OutputRedactionError(
            f".factory/tools/credential-guard.py is not tracked at the bound commit "
            f"{bound_commit}; the guard must be an exact committed blob"
        )
    data = result.stdout
    if len(data) > MAX_GUARD_SOURCE_BYTES:
        raise OutputRedactionError(
            f"the committed credential guard blob exceeds the "
            f"{MAX_GUARD_SOURCE_BYTES}-byte bound"
        )
    return data


def _load_guard_module(source: bytes) -> object:
    """Import the guard's exact bytes as a module (its documented library idiom).

    The module is never registered in ``sys.modules``, so two workspaces or
    two bound commits can never share a stale module instance; the guard is
    dependency-free (stdlib only).
    """
    name = f"_credential_guard_{hashlib.sha256(source).hexdigest()[:16]}"
    module = types.ModuleType(name)
    module.__file__ = REDACTION_GUARD_RELPATH
    module.__package__ = None
    try:
        exec(compile(source, REDACTION_GUARD_RELPATH, "exec"), module.__dict__)
    except (SyntaxError, ValueError) as exc:
        raise OutputRedactionError(
            f"the committed credential guard does not compile: {exc}"
        ) from exc
    for required in ("redact_text", "classify_command", "check_command_stdin"):
        if not callable(getattr(module, required, None)):
            raise OutputRedactionError(
                f"the committed credential guard is missing the {required} API"
            )
    return module


def read_worktree_guard_source(root: Path) -> bytes:
    """Bounded no-follow owned read of the working-tree guard source.

    This is the anchored worktree read the control plane uses for the guard
    source (``launch`` binds it through its own committed-blob authority;
    the campaign reads the worktree through *this* authority and the
    committed blob through its descriptor-anchored Git authority, so the
    exact-commit guard binding never routes through a second, unanchored
    Git path).  A missing, symlinked, oversized, foreign-owned, or
    group/other-writable guard fails closed.
    """
    return _read_worktree_source(Path(root), REDACTION_GUARD_RELPATH, MAX_GUARD_SOURCE_BYTES)


def redactor_from_bytes(
    worktree: bytes, committed: bytes, bound_commit: str
) -> "Redactor":
    """Verify a guard byte pair and build the exact-commit redactor.

    ``worktree`` is the working-tree guard read by the caller's own anchored
    no-follow authority; ``committed`` is the exact blob at ``bound_commit``
    read by the caller's own Git authority.  The two must be byte-identical
    (same SHA-256) and bounded, and the committed blob must compile with the
    documented guard API; any divergence fails closed so a swapped,
    symlinked, missing, oversized, or uncommitted guard never executes.
    """
    if not isinstance(worktree, bytes) or not isinstance(committed, bytes):
        raise OutputRedactionError(
            "the credential-guard source pair must be raw bytes"
        )
    if len(worktree) > MAX_GUARD_SOURCE_BYTES or len(committed) > MAX_GUARD_SOURCE_BYTES:
        raise OutputRedactionError(
            f"the credential guard source exceeds the "
            f"{MAX_GUARD_SOURCE_BYTES}-byte bound"
        )
    if hashlib.sha256(worktree).hexdigest() != hashlib.sha256(committed).hexdigest():
        raise OutputRedactionError(
            "the working-tree credential guard is not the exact committed "
            f"blob at {bound_commit}; refusing to execute substituted bytes"
        )
    return Redactor(
        _load_guard_module(committed), hashlib.sha256(committed).hexdigest()
    )


def redactor_for(workspace: Path, bound_commit: str) -> "Redactor":
    """Verify and load the exact committed credential guard (fail closed).

    Convenience wrapper over :func:`redactor_from_bytes` that reads both
    sides through this module's own authorities (the anchored no-follow
    worktree read and the pinned-Git committed-blob read).  The working-tree
    guard must be a regular, owned, non-writable, bounded file whose SHA-256
    equals the committed blob at ``bound_commit`` — a swapped, symlinked,
    missing, oversized, or uncommitted guard never executes.  The returned
    :class:`Redactor` runs only that verified guard.
    """
    worktree = read_worktree_guard_source(Path(workspace))
    committed = _committed_guard_bytes(Path(workspace), bound_commit)
    return redactor_from_bytes(worktree, committed, bound_commit)


class Redactor:
    """One verified credential-guard instance for text and bounded-tail output.

    ``digest`` is the SHA-256 of the exact committed guard bytes the
    instance runs — the digest the trusted pre-spawn authority forwards to
    the Pi extension (``PI_RALPH_GUARD_DIGEST``) so the model-side guard
    can verify its own worktree bytes against the same exact-commit binding
    without any Git access (Task 11 review).
    """

    __slots__ = ("_module", "_digest")

    def __init__(self, module: object, digest: str) -> None:
        self._module = module
        self._digest = digest

    @property
    def digest(self) -> str:
        """SHA-256 of the exact committed guard bytes bound to this instance."""
        return self._digest

    def redact_text(self, text: str) -> str:
        """Mask one text chunk through the guard (idempotent, bounded)."""
        if not isinstance(text, str):
            raise OutputRedactionError("the redaction input must be text")
        if len(text.encode("utf-8", "surrogatepass")) > MAX_GUARD_SOURCE_BYTES:
            raise OutputRedactionError("the redaction input exceeds the bound")
        try:
            return self._module.redact_text(text)  # type: ignore[attr-defined]
        except (TypeError, ValueError, RecursionError) as exc:
            raise OutputRedactionError(
                f"the credential guard failed to redact text: {exc}"
            ) from exc

    def redact_tail(
        self,
        tail_text: str,
        in_block: bool,
        partial: str,
        bound_bytes: Optional[int] = None,
        mid_line: bool = False,
    ) -> str:
        """Redact a captured tail, preserving the guard's key-block state.

        ``in_block`` is the private-block state at the start of the
        retained tail (True when the block opened before the tail window);
        ``partial`` is the trailing partial line at the boundary, prepended
        so a marker split exactly at the boundary is completed for the
        guard.  A tail whose start is inside an open block is seeded with a
        synthetic BEGIN marker so the guard masks the whole open block —
        nothing the guard could recognize as key material can survive.

        ``mid_line`` marks a retained tail whose **first byte is not at a
        line start** — the eviction boundary cut through a line, so the
        tail's first line is an incomplete fragment whose true start was
        evicted (Task 11 review).  Such a fragment can carry the *second*
        half of a credential that the guard cannot identify: a long opaque
        TOKEN value straddling the boundary loses its ``NAME=`` prefix in
        the evicted region, so no assignment rule matches and the raw
        fragment would survive.  Before redaction the incomplete first line
        is therefore **dropped conservatively** (a newline-free oversized
        tail is dropped in full, yielding no raw fragment), the evicted
        half (``partial``) is scanned against it so the private-key block
        state stays exact (a BEGIN/END marker split exactly at the boundary
        still opens/closes the block, and the block content that follows
        stays masked), and the evicted half is then discarded — it is part
        of the same split line and can no longer be faithfully completed.

        ``bound_bytes`` (when given) caps the **returned** tail to that many
        UTF-8 bytes.  The redaction input legitimately exceeds the retained
        window by the synthetic block-marker and the split-marker partial
        (``launch._BoundedStream`` retains ``OUTPUT_TAIL_CAP`` bytes and
        prepends up to ``KEY_BLOCK_WINDOW`` split bytes), and the guard can
        *grow* a line when it rewrites a private-key END marker, so the
        masked result is itself bounded after redaction.  The kept bytes are
        the most recent ones (the synthetic/partial prefix is the oldest
        retained content); the input block state is untouched, so a split
        marker is still completed and re-verified before any byte is dropped.
        """
        if not isinstance(tail_text, str):
            raise OutputRedactionError("the redaction tail must be a string")
        if bound_bytes is not None and (
            not isinstance(bound_bytes, int)
            or isinstance(bound_bytes, bool)
            or bound_bytes < 0
        ):
            raise OutputRedactionError(
                "the redaction tail byte bound must be a non-negative integer"
            )
        if mid_line:
            # The retained tail begins mid-line: its first line is the
            # retained half of a line whose start was evicted.  The guard
            # cannot identify such a fragment (a >512-byte opaque TOKEN
            # value cut at the boundary loses its ``NAME=`` prefix and
            # never matches an assignment rule), so the incomplete first
            # line is dropped before any redaction; a newline-free
            # oversized tail is dropped in full and yields no raw
            # fragment.  The evicted half (``partial``) is scanned against
            # the dropped line so the private-key block state stays exact
            # (a BEGIN/END marker split at the boundary still opens/closes
            # the block and the content after it remains masked), then
            # both halves of the split line are discarded.
            if "\n" in tail_text:
                dropped, _, tail_text = tail_text.partition("\n")
                dropped += "\n"
            else:
                dropped, tail_text = tail_text, ""
            combined = partial + dropped
            if in_block:
                if _KEY_END_RE.search(combined):
                    in_block = False
            else:
                if _KEY_BEGIN_RE.search(combined):
                    in_block = True
            partial = ""
        if not tail_text and not partial:
            return ""
        text = (
            ("-----BEGIN PRIVATE KEY-----\n" if in_block else "")
            + partial
            + tail_text
        )
        redacted = self.redact_text(text)
        if in_block:
            if redacted.startswith(_KEY_BLOCK_START_MARKER):
                redacted = redacted[len(_KEY_BLOCK_START_MARKER):]
            redacted = redacted.lstrip("\n")
        if bound_bytes is not None:
            raw = redacted.encode("utf-8", "replace")
            if len(raw) > bound_bytes:
                redacted = raw[len(raw) - bound_bytes:].decode("utf-8", "replace")
        return redacted


def redact_text(text: str, workspace: Path, bound_commit: str) -> str:
    """Convenience: verify the guard for ``(workspace, bound_commit)`` and mask one chunk."""
    return redactor_for(workspace, bound_commit).redact_text(text)


# Re-exports consumed by ``launch._BoundedStream`` (the capture block-state
# scan) and by the hidden test suite.
_KEY_BEGIN_PATTERN = _KEY_BEGIN_RE
_KEY_END_PATTERN = _KEY_END_RE
