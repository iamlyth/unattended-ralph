"""Hidden evidence, receipt, and verifier authority (Task 12; EVID-01 §19).

Retains the exact-commit signed runner receipts, capability contracts,
evidence tiers, visual provenance, atomic publication, installed/human
evidence tiers, and the coordinator-bounded receipt wrapper under the hidden
``.factory.loop`` control plane.  Two authorities live here:

1. **Verifier binding** (:func:`bind_verifier`, :class:`HeldVerifier`,
   :func:`revalidate_verifier`): the deterministic verification entrypoint
   (``[verification].campaign_command``) is opened no-follow and bound to its
   committed blob, secure identity, and inode **before** any untrusted phase
   runs.  The bound descriptor is held across the untrusted phase; every
   later execution re-validates the binding (inode identity, owner,
   mode/link-count, byte digest, and committed blob at the current head) and
   fails closed on any pathname, content, or committed-tree substitution.
   The mechanism mirrors the accepted ``.factory/tools/campaign-verifier-binding.py``
   authority (immutable descriptor; pathname replacement rejected) as hidden
   standard-library code.  The retained descriptor is executed through
   ``/proc/self/fd/<fd>`` with the bound command argv passed to the kernel
   verbatim.  Because the kernel resolves the script argument through the
   descriptor path, a shebang script sees ``$0`` as ``/proc/self/fd/<fd>`` —
   never the canonical repository path — while every argument after the
   script path is preserved unchanged; a gate that resolves ``$0``-relative
   resources must therefore be written against the fd path (or use an
   absolute/allowlisted path), and gates must never rely on the canonical
   path appearing in ``$0``.

2. **Receipt/manifest validation** (:func:`validate_receipt`,
   :func:`validate_manifest_ref`, :func:`validate_evidence_text`): machine
   receipts and exact signed runner manifests are the only runtime evidence.
   PASS requires the bound command to exit 0 with verified identity, commit,
   digests, and timestamps; BLOCKED evidence forces an audit ``findings``
   result; audits cite exact ``[receipt: …]`` / ``[manifest: …]``
   references.  A model assertion, free-text command transcript, or
   tier-elevating claim is never a receipt: receipt evidence is capped at
   the ``installed`` tier, signed runner manifests at ``real_system``, and
   ``human`` acceptance can never be machine-claimed.  Receipts and
   manifests are validated with hardened owner/mode/link-count/inode checks
   on the JSON and adjacent stdout/stderr artifacts.

   Evidence is never a task-selection authority: the deterministic selector
   (``selector.py``) is a pure function of plan + state and never reads
   receipts, manifests, or evidence state (§19).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import stat
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

try:  # package import (the hidden `.factory/loop/` package)
    from . import gitutil
except ImportError:  # flat import used by the hidden `.factory/tests/` suite
    import gitutil  # type: ignore[no-redef]

SCHEMA_NAME = "factory-evidence-verifier-binding/v1"
BINDING_COMMIT_MAX = 16 * 1024 * 1024  # committed verifier executable bound
# Finite bound for every direct trusted Git read of this module (MED2): the
# pinned absolute Git executable can never wait forever behind the evidence
# boundary (mirrors the hidden gitutil authority's own finite bound).
GIT_READ_TIMEOUT = 120.0
MAX_ARTIFACT = 64 * 1024 * 1024  # bounded stdout/stderr receipt artifacts
MAX_RECEIPT = 1024 * 1024  # bounded receipt JSON
MAX_EVIDENCE_TEXT = 64 * 1024 * 1024  # bounded audit report text

SHA1 = re.compile(r"^[0-9a-f]{40}$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")

# The exact evidence-line prefix and the only status tokens accepted (LOW4:
# an anchored whole-token grammar — a status word embedded in a command,
# path, or citation is never a status, and no other spelling is accepted).
EVIDENCE_PREFIX = "- Executable evidence:"
STATUS_TOKENS = frozenset(("PASS", "FAIL", "BLOCKED"))

# §19 evidence tiers (ascending; higher index = higher tier).
TIERS = ["unit", "simulated", "private_integration", "installed", "real_system", "human"]
TIER_INDEX = {tier: index for index, tier in enumerate(TIERS)}

# Receipt/manifest citation syntax in audit evidence lines.
RECEIPT_RE = re.compile(r"\[receipt:\s*([^\]]+)\]")
MANIFEST_RE = re.compile(r"\[manifest:\s*([^\]]+)\]")
TIER_CLAIM_RE = re.compile(r"\btier\s*=\s*([a-z_]+)")

MANIFEST_CHECKER_REL = ".factory/tools/check-factory-runner-evidence.py"


class EvidenceError(RuntimeError):
    """Base fail-closed error of the evidence authority."""


class VerifierBindingError(EvidenceError):
    """The verifier entrypoint could not be bound or was substituted."""


class ReceiptError(EvidenceError):
    """A receipt or manifest citation failed validation."""


class TierError(EvidenceError):
    """An evidence claim attempts to elevate its tier above its channel."""


# ---------------------------------------------------------------------------
# Hardened no-follow secured reads (owner/mode/link-count/inode)
# ---------------------------------------------------------------------------


def secure_read_bytes(
    path: Path, *, maximum: int, what: str
) -> Tuple[bytes, os.stat_result]:
    """Read ``path`` through one retained no-follow descriptor.

    The file must be a regular single-link current-user-owned file that is
    not group/other-writable, must not exceed ``maximum`` bytes, and the
    descriptor's identity must match the pathname at both ends of the read
    (a symlink, mode, owner, link-count, or inode substitution fails closed).
    """
    absolute = path.absolute()
    try:
        if absolute.resolve(strict=True) != absolute:
            raise VerifierBindingError(
                f"{what} path contains a symlink component: {path}"
            )
    except OSError as exc:
        raise VerifierBindingError(f"{what} path is unavailable: {path}: {exc}") from exc
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        descriptor = os.open(absolute, flags)
    except OSError as exc:
        raise VerifierBindingError(f"cannot open {what} {path}: {exc}") from exc
    try:
        before = os.fstat(descriptor)
        named = absolute.lstat()
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_uid != os.getuid()
            or before.st_nlink != 1
            or before.st_mode & 0o022
            or (before.st_dev, before.st_ino) != (named.st_dev, named.st_ino)
            or before.st_size > maximum
        ):
            raise VerifierBindingError(f"unsafe {what} file: {path}")
        chunks: List[bytes] = []
        remaining = maximum + 1
        while remaining:
            chunk = os.read(descriptor, min(65536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
        after = os.fstat(descriptor)
        named_after = absolute.lstat()
        if (
            len(raw) > maximum
            or (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
            != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
            or (after.st_dev, after.st_ino) != (named_after.st_dev, named_after.st_ino)
        ):
            raise VerifierBindingError(f"{what} file changed while reading: {path}")
        return raw, before
    finally:
        os.close(descriptor)


def _read_trusted_bytes(path: Path, *, maximum: int) -> Tuple[bytes, os.stat_result]:
    """Bounded read of an already-validated immutable external executable.

    The immutable-chain authority (:func:`gitutil.require_trusted_executable`)
    has already validated every resolved path component, so the final file is
    read with ordinary ``stat`` (store binaries are legitimately symlinked)
    under a size bound and a before/after identity check.
    """
    absolute = path.absolute()
    try:
        info = os.stat(absolute)
    except OSError as exc:
        raise VerifierBindingError(
            f"cannot stat the external trusted executable {path}: {exc}"
        ) from exc
    if not stat.S_ISREG(info.st_mode) or info.st_size > maximum:
        raise VerifierBindingError(f"unsafe external executable: {path}")
    with open(absolute, "rb") as stream:
        raw = stream.read(maximum + 1)
    if len(raw) > maximum:
        raise VerifierBindingError(f"external executable exceeds the bound: {path}")
    after = os.stat(absolute)
    if (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns) != (
        after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns
    ):
        raise VerifierBindingError(f"external executable changed while reading: {path}")
    return raw, info


def _resolve_evidence(root: Path, reference: str) -> Path:
    """Resolve one evidence citation inside the repository (no traversal).

    The parent directory is resolved (so a symlinked intermediate component
    cannot redirect a citation out of the repository) but the **final
    pathname is returned unresolved**: ``secure_read_bytes``' no-follow
    open and final-component equality check are what reject a symlinked
    evidence file or transcript.  A fully-resolved path here would silently
    canonicalize a symlink away and validate its target instead.
    """
    if not reference or reference != reference.strip():
        raise ReceiptError(f"evidence reference is not clean: {reference!r}")
    path = Path(reference)
    if path.is_absolute() or ".." in path.parts:
        raise ReceiptError(f"evidence reference escapes the repository: {reference}")
    try:
        resolved = (root / reference).resolve(strict=True)
        resolved.relative_to(root.resolve())
        parent = (root / path.parent).resolve(strict=True)
    except (OSError, ValueError) as exc:
        raise ReceiptError(
            f"evidence reference escapes the repository or is unavailable: "
            f"{reference}"
        ) from exc
    return parent / path.name


def secure_json(root: Path, reference: str, *, maximum: int = MAX_RECEIPT) -> Tuple[dict, bytes]:
    """Bounded, no-follow, identity-checked JSON read for a receipt record."""
    path = _resolve_evidence(root, reference)
    raw, _ = secure_read_bytes(path, maximum=maximum, what="evidence record")
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ReceiptError(f"invalid evidence record {reference}: {exc}") from exc
    if not isinstance(data, dict):
        raise ReceiptError(f"evidence record must be an object: {reference}")
    return data, raw


def secure_artifact(root: Path, reference: str, *, maximum: int = MAX_ARTIFACT) -> bytes:
    """Bounded no-follow receipt-artifact read with identity checks."""
    path = _resolve_evidence(root, reference)
    raw, _ = secure_read_bytes(path, maximum=maximum, what="receipt artifact")
    return raw


# ---------------------------------------------------------------------------
# Verifier binding (committed blob opened/bound before the untrusted phase)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class VerifierBinding:
    """One deterministic verification entrypoint bound to its committed identity.

    ``executable`` is the canonical repository-relative ``./path`` (bound to
    the exact committed blob and inode) or an absolute trusted external
    executable (validated through the immutable-chain authority).  ``inode``
    is empty for external executables; ``commit``/``blob`` are empty too
    because an external executable has no repository blob.
    """

    schema: str = SCHEMA_NAME
    command: Tuple[str, ...] = ()
    executable: str = ""
    commit: str = ""
    blob: str = ""
    sha256: str = ""
    mode: str = ""
    inode: Tuple[int, int] = ()
    external: bool = False

    def to_dict(self) -> Dict[str, object]:
        return {
            "schema": self.schema,
            "command": list(self.command),
            "executable": self.executable,
            "commit": self.commit,
            "blob": self.blob,
            "sha256": self.sha256,
            "mode": self.mode,
            "inode": list(self.inode),
            "external": self.external,
        }

    def digest(self) -> str:
        canonical = json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _canonical_relative(executable_arg: str) -> str:
    """Validate and return the canonical repo-relative ``./path`` argument."""
    if not executable_arg.startswith("./"):
        raise VerifierBindingError(
            "verifier executable must be a canonical repository-relative ./path "
            "or an absolute trusted executable"
        )
    relative = executable_arg[2:]
    if (
        not relative
        or relative.startswith("/")
        or relative.endswith("/")
        or any(segment in ("", ".", "..") for segment in relative.split("/"))
        or "\\" in relative
        or any(ord(char) < 0x20 or ord(char) == 0x7F for char in relative)
    ):
        raise VerifierBindingError(
            f"verifier executable path is not canonical: {executable_arg!r}"
        )
    canonical = "./" + relative
    if canonical != executable_arg:
        raise VerifierBindingError(
            f"verifier executable path is not canonical: {executable_arg!r}"
        )
    return canonical


def _committed_blob_bytes(root: Path, git: object, commit: str, relpath: str) -> bytes:
    """Exact committed blob bytes of ``<commit>:<relpath>`` (bounded).

    Every trusted Git read is finite-bounded (MED2): the pinned absolute Git
    executable can never wait forever behind the binding boundary, so the
    fallback path passes the module's finite timeout explicitly instead of
    the default (unbounded) subprocess timeout.
    """
    if git is not None and hasattr(git, "blob_at"):
        try:
            raw = git.blob_at(commit, relpath)
        except Exception as exc:  # CampaignGitError surfaces as a binding failure
            raise VerifierBindingError(
                f"cannot read the committed verifier blob {commit}:{relpath}: {exc}"
            ) from exc
    else:
        result = gitutil.git_bytes(
            ["rev-parse", f"{commit}:{relpath}"], cwd=root, timeout=GIT_READ_TIMEOUT
        )
        if result.returncode != 0:
            raise VerifierBindingError(
                f"verifier {relpath!r} is not tracked at {commit}"
            )
        blob_id = result.stdout.strip().decode("ascii")
        if not SHA1.fullmatch(blob_id):
            raise VerifierBindingError(
                f"verifier {relpath!r} at {commit} does not resolve to a blob"
            )
        blob_result = gitutil.git_bytes(
            ["cat-file", "blob", blob_id], cwd=root, timeout=GIT_READ_TIMEOUT
        )
        if blob_result.returncode != 0:
            raise VerifierBindingError(f"cannot read verifier blob {blob_id}")
        raw = blob_result.stdout
    if len(raw) > BINDING_COMMIT_MAX:
        raise VerifierBindingError(f"verifier {relpath!r} exceeds the bound size")
    return raw


def bind_verifier(
    root: Path,
    command: Sequence[str],
    *,
    commit: Optional[str] = None,
    git: Optional[object] = None,
) -> VerifierBinding:
    """Open and bind the committed verifier entrypoint before any untrusted phase.

    For a canonical repository-relative ``./path`` the worktree file must be
    a regular single-link current-user-owned executable that is not
    group/other-writable, and its bytes must equal the exact committed blob
    at ``commit``; the bound inode is recorded so a later path substitution
    fails closed.  For an absolute external trusted executable the immutable
    chain authority validates every path component and no repository blob
    binding applies.  An empty or malformed command fails closed: a campaign
    without a deterministic verifier can never pass.
    """
    root = Path(root).absolute()
    command = tuple(command)
    if not command or any(not isinstance(item, str) or not item for item in command):
        raise VerifierBindingError(
            "a deterministic verifier command is required; an empty or "
            "malformed verifier command cannot be bound"
        )
    if any("\x00" in item for item in command):
        raise VerifierBindingError("verifier command contains a NUL byte")
    executable_arg = command[0]
    if executable_arg.startswith("./"):
        canonical = _canonical_relative(executable_arg)
        relpath = canonical[2:]
        path = root.joinpath(*relpath.split("/"))
        raw, info = secure_read_bytes(path, maximum=BINDING_COMMIT_MAX, what="verifier")
        if not info.st_mode & 0o111:
            raise VerifierBindingError(f"verifier is not executable: {path}")
        if commit is None:
            commit = gitutil.resolve_head(root)
            if not commit:
                raise VerifierBindingError("cannot resolve the bound commit for the verifier")
        committed = _committed_blob_bytes(root, git, commit, relpath)
        if raw != committed:
            raise VerifierBindingError(
                f"verifier worktree bytes do not equal the committed blob at "
                f"{commit[:12]}:{relpath}; a substitution fails closed before "
                "untrusted execution"
            )
        binding = VerifierBinding(
            command=command,
            executable=canonical,
            commit=commit,
            blob=hashlib.sha1(committed).hexdigest(),
            sha256=hashlib.sha256(raw).hexdigest(),
            mode=format(stat.S_IMODE(info.st_mode), "04o"),
            inode=(info.st_dev, info.st_ino),
            external=False,
        )
    elif executable_arg.startswith("/"):
        # External trusted executable: pinned immutable-chain authority only
        # (the same rule Task 6 applies to external wrapper/backend paths).
        try:
            gitutil.require_trusted_executable(executable_arg)
        except gitutil.GitBoundaryError as exc:
            raise VerifierBindingError(
                f"external verifier executable is not a pinned trusted "
                f"executable: {exc}"
            ) from exc
        raw, info = _read_trusted_bytes(
            Path(executable_arg), maximum=BINDING_COMMIT_MAX
        )
        if not info.st_mode & 0o111:
            raise VerifierBindingError(f"verifier is not executable: {executable_arg}")
        binding = VerifierBinding(
            command=command,
            executable=executable_arg,
            commit="",
            blob="",
            sha256=hashlib.sha256(raw).hexdigest(),
            mode=format(stat.S_IMODE(info.st_mode), "04o"),
            inode=(),
            external=True,
        )
    else:
        raise VerifierBindingError(
            "verifier executable must be a canonical repository-relative ./path "
            "or an absolute trusted executable"
        )
    return binding


class HeldVerifier:
    """The bound verifier descriptor retained across the untrusted phase.

    Opened with ``O_RDONLY|O_NOFOLLOW|O_CLOEXEC`` before the untrusted
    phase, the descriptor pins the exact bound inode: a later pathname
    substitution can never change what the retained descriptor refers to,
    so :meth:`revalidate` fails closed instead of executing substituted
    bytes.  For external trusted executables no descriptor is held; every
    revalidation re-runs the pinned-chain authority.
    """

    def __init__(self, root: Path, binding: VerifierBinding) -> None:
        self._root = Path(root).absolute()
        self.binding = binding
        self._descriptor: Optional[int] = None
        if not binding.external:
            path = self._root.joinpath(*binding.executable[2:].split("/"))
            flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
            try:
                self._descriptor = os.open(path.absolute(), flags)
            except OSError as exc:
                raise VerifierBindingError(
                    f"cannot retain the bound verifier descriptor: {exc}"
                ) from exc
            self._revalidate_descriptor()

    @property
    def fd(self) -> int:
        """The retained verifier descriptor number (repo-relative bindings).

        The descriptor is opened ``O_RDONLY|O_NOFOLLOW|O_CLOEXEC`` before the
        untrusted phase and pins the bound inode; the child executes
        ``/proc/self/fd/<fd>`` so the kernel resolves the exact bound inode at
        exec time even when the pathname was substituted after the last
        revalidation (MED1).
        """
        if self._descriptor is None:
            raise VerifierBindingError(
                "repo-relative verifier descriptor is not retained"
            )
        return self._descriptor

    def _revalidate_descriptor(self) -> None:
        binding = self.binding
        if self._descriptor is None:
            raise VerifierBindingError("bound verifier descriptor was not retained")
        try:
            info = os.fstat(self._descriptor)
        except OSError as exc:
            raise VerifierBindingError(
                f"bound verifier descriptor became invalid: {exc}"
            ) from exc
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or info.st_nlink != 1
            or info.st_mode & 0o022
            or (info.st_dev, info.st_ino) != binding.inode
        ):
            raise VerifierBindingError(
                "bound verifier descriptor identity no longer matches the "
                "binding (owner/mode/link-count/inode substitution)"
            )

    def _read_descriptor(self) -> bytes:
        if self._descriptor is None:
            raise VerifierBindingError("bound verifier descriptor is closed")
        try:
            os.lseek(self._descriptor, 0, os.SEEK_SET)
        except OSError as exc:
            raise VerifierBindingError(
                f"cannot rewind the bound verifier descriptor: {exc}"
            ) from exc
        chunks: List[bytes] = []
        total = 0
        while True:
            chunk = os.read(self._descriptor, 65536)
            if not chunk:
                break
            total += len(chunk)
            if total > BINDING_COMMIT_MAX:
                raise VerifierBindingError("bound verifier exceeds the read limit")
            chunks.append(chunk)
        return b"".join(chunks)

    def revalidate(
        self,
        *,
        git: Optional[object] = None,
        current_commit: Optional[str] = None,
    ) -> bytes:
        """Return the exact bound bytes, failing closed on any substitution.

        The descriptor pins the bound inode; this additionally re-checks the
        descriptor identity, byte digest, the pathname identity (a swapped
        pathname now resolving to a different inode), and — for
        repo-relative verifiers — that the committed blob at
        ``current_commit`` still equals the bound blob.
        """
        binding = self.binding
        if binding.external:
            try:
                gitutil.require_trusted_executable(binding.executable)
            except gitutil.GitBoundaryError as exc:
                raise VerifierBindingError(
                    f"external verifier executable is no longer a pinned "
                    f"trusted executable: {exc}"
                ) from exc
            current, _ = _read_trusted_bytes(
                Path(binding.executable), maximum=BINDING_COMMIT_MAX
            )
            if hashlib.sha256(current).hexdigest() != binding.sha256:
                raise VerifierBindingError(
                    "external verifier executable changed after binding"
                )
            return current
        raw = self._read_descriptor()
        if hashlib.sha256(raw).hexdigest() != binding.sha256:
            raise VerifierBindingError(
                "bound verifier bytes changed after binding (content substitution)"
            )
        path = self._root.joinpath(*binding.executable[2:].split("/"))
        try:
            named = path.lstat()
        except OSError as exc:
            raise VerifierBindingError(
                f"bound verifier pathname is gone: {exc}"
            ) from exc
        if (
            not stat.S_ISREG(named.st_mode)
            or (named.st_dev, named.st_ino) != binding.inode
            or named.st_nlink != 1
        ):
            raise VerifierBindingError(
                "bound verifier pathname no longer resolves to the bound "
                "inode (path substitution fails closed)"
            )
        if current_commit is not None:
            relpath = binding.executable[2:]
            committed = _committed_blob_bytes(self._root, git, current_commit, relpath)
            if hashlib.sha1(committed).hexdigest() != binding.blob:
                raise VerifierBindingError(
                    "the committed verifier blob changed after binding "
                    "(committed-tree substitution fails closed)"
                )
        return raw

    def spawn(
        self, command: Sequence[str]
    ) -> Tuple[Sequence[str], str, Sequence[int]]:
        """Return ``(argv, executable, pass_fds)`` for one fd-pinned execution.

        MED1: the child executes ``/proc/self/fd/<fd>`` — the retained
        descriptor — so the kernel resolves the exact bound inode at exec time
        and a pathname substitution after the last revalidation can never
        substitute the bytes that execute.  ``argv`` is the bound command
        passed to the kernel verbatim (canonical ``argv[0]`` and every
        argument), so every argument **after** the script path is preserved
        unchanged.  The kernel's shebang dispatch is *not* byte-preserving on
        argv[0], though: for a shebang script the kernel re-executes the
        interpreter with the descriptor path ``/proc/self/fd/<fd>`` as the
        script argument, so inside the script ``$0`` is ``/proc/self/fd/<fd>``
        — never the canonical repository path — while a non-script binary
        keeps the passed canonical ``argv[0]``.  Gates must therefore never
        depend on ``$0`` naming the canonical path (F1; asserted empirically
        by the shebang-swap tests).

        ``pass_fds`` carries exactly the verifier descriptor — never the root
        lock descriptor (the lock boundary already refuses any passed alias of
        the locked inode) and no other trusted-holder descriptor.  The child
        intentionally inherits this one read-only descriptor (``O_RDONLY``,
        the accepted read-only verifier fd inheritance of F2): the shebang
        interpreter re-opens ``/proc/self/fd/<fd>`` through the inherited fd,
        so the fd must survive the exec; the child can read the bound verifier
        through it but can never write, and the root lock descriptor is never
        inherited.  The descriptor's owner/mode/link-count/inode identity is
        re-validated on **every** execution request (MED1).
        """
        command = tuple(command)
        if not command or command != self.binding.command:
            raise VerifierBindingError(
                "the spawn command must equal the bound verifier command"
            )
        if self.binding.external:
            try:
                gitutil.require_trusted_executable(self.binding.executable)
            except gitutil.GitBoundaryError as exc:
                raise VerifierBindingError(
                    f"external verifier executable is no longer a pinned "
                    f"trusted executable: {exc}"
                ) from exc
            return (list(command), self.binding.executable, ())
        # Repo-relative binding: re-validate the retained descriptor identity
        # (owner/mode/link-count/inode) every time, then hand the child exactly
        # that descriptor and the canonical command argv.
        self._revalidate_descriptor()
        return (list(command), f"/proc/self/fd/{self.fd}", (self.fd,))

    def close(self) -> None:
        if self._descriptor is not None:
            try:
                os.close(self._descriptor)
            except OSError:
                pass
            self._descriptor = None

    def __enter__(self) -> "HeldVerifier":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()


def revalidate_verifier(
    root: Path,
    binding: VerifierBinding,
    *,
    git: Optional[object] = None,
    current_commit: Optional[str] = None,
    held: Optional[HeldVerifier] = None,
) -> bytes:
    """Standalone fail-closed revalidation immediately before execution."""
    if held is not None:
        return held.revalidate(git=git, current_commit=current_commit)
    return HeldVerifier(root, binding).revalidate(git=git, current_commit=current_commit)


# ---------------------------------------------------------------------------
# Receipt/manifest validation (hardened stdout/stderr owner/mode/nlink/inode)
# ---------------------------------------------------------------------------


def validate_receipt(root: Path, reference: str) -> Dict[str, object]:
    """Validate one ``[receipt: …]`` record and its hardened adjacent logs.

    Schema, argv digest, exit code, strict 40-hex evidence commit, positive
    coordinator round, 64-hex nonce, and the stdout/stderr digest artifacts
    (regular single-link current-user-owned, not group/other-writable,
    bounded, inode-stable) are all verified; a symlink, swapped
    owner/mode/link-count/inode, missing artifact, or digest mismatch fails
    closed as a :class:`ReceiptError`.
    """
    try:
        path = _resolve_evidence(root, reference)
        data, _raw = secure_json(root, reference)
        expected = {
            "schema", "tag", "argv", "argv_sha256", "exit_code",
            "stdout_sha256", "stderr_sha256", "started_at", "finished_at",
            "evidence_commit", "coordinator_round", "coordinator_nonce",
        }
        if set(data) != expected or data.get("schema") != "ralph-audit-receipt/v1":
            raise ReceiptError(f"receipt schema is invalid: {reference}")
        argv = data.get("argv")
        if not isinstance(argv, list) or not all(isinstance(item, str) and item for item in argv):
            raise ReceiptError(f"receipt argv is invalid: {reference}")
        argv_digest = hashlib.sha256(
            json.dumps(argv, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        if data.get("argv_sha256") != argv_digest:
            raise ReceiptError(f"receipt argv digest mismatch: {reference}")
        for field in ("stdout_sha256", "stderr_sha256"):
            if not isinstance(data.get(field), str) or not SHA256.fullmatch(str(data.get(field))):
                raise ReceiptError(f"receipt {field} is invalid: {reference}")
        # The adjacent transcripts keep the *same repository-relative form* as
        # the cited receipt record: ``secure_artifact`` resolves only
        # repository-relative references (an absolute or traversing reference is
        # itself fail-closed evidence that a citation escapes the repository).
        rel = path.relative_to(root)
        for log_name, digest_field_name in (("stdout", "stdout_sha256"), ("stderr", "stderr_sha256")):
            log_ref = str((rel.parent / f"{rel.stem}.{log_name}").as_posix())
            raw = secure_artifact(root, log_ref)
            if hashlib.sha256(raw).hexdigest() != data[digest_field_name]:
                raise ReceiptError(f"receipt log digest mismatch: {log_ref}")
        exit_code = data.get("exit_code")
        if type(exit_code) is not int:
            raise ReceiptError(f"receipt exit_code is invalid: {reference}")
        evidence_commit = data.get("evidence_commit")
        if not isinstance(evidence_commit, str) or not SHA1.fullmatch(evidence_commit):
            raise ReceiptError(f"receipt evidence_commit must be 40-hex: {reference}")
        coordinator_round = data.get("coordinator_round")
        if type(coordinator_round) is not int or coordinator_round < 1:
            raise ReceiptError(f"receipt coordinator_round is invalid: {reference}")
        coordinator_nonce = data.get("coordinator_nonce")
        if not isinstance(coordinator_nonce, str) or not SHA256.fullmatch(coordinator_nonce):
            raise ReceiptError(f"receipt coordinator_nonce is invalid: {reference}")
        return data
    except VerifierBindingError as exc:
        raise ReceiptError(
            f"receipt evidence is unsafe or missing: {exc}"
        ) from exc


def validate_manifest_ref(
    root: Path,
    reference: str,
    expected_commit: str,
    *,
    _checker_rel: str = MANIFEST_CHECKER_REL,
) -> None:
    """Require an exact signed aggregate runner record at ``expected_commit``.

    The reference must be an exact record in the runner-evidence aggregate
    that passes the same signer/commit/tree/environment/archive/argv
    validation as ``.factory/tools/check-factory-runner-evidence.py``.  Unsigned,
    fabricated, standalone/minimal, and path-category-only manifests are
    rejected; a manifest certifies only a clean pass.
    """
    if not SHA1.fullmatch(expected_commit):
        raise ReceiptError("--expected-commit audit base must be 40-hex")
    checker = _resolve_evidence(root, _checker_rel)
    try:
        result = subprocess.run(
            [sys.executable, str(checker), "--verify-manifest", reference,
             "--expected-commit", expected_commit],
            cwd=root, capture_output=True, text=True, timeout=120,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ReceiptError(f"cannot run the strict runner-evidence helper: {exc}") from exc
    if result.returncode:
        detail = (result.stderr or result.stdout).strip()
        raise ReceiptError(
            f"runner manifest is not an accepted exact-commit runner receipt: "
            f"{reference} ({detail or 'strict runner-evidence validation failed'})"
        )


# ---------------------------------------------------------------------------
# Audit evidence lines (PASS exit 0, BLOCKED forces findings, tier ceilings)
# ---------------------------------------------------------------------------


def evidence_tier_ceiling(reference_kind: str) -> int:
    """Max tier a channel can certify (evidence cannot self-elevate).

    A coordinator machine receipt certifies executed runtime only
    (``installed`` ceiling); a signed exact-commit runner manifest certifies
    ``real_system``; prose certifies nothing and can never claim any tier.
    ``human`` acceptance is out-of-band and non-automatable and can never be
    machine-claimed by any channel.
    """
    if reference_kind == "receipt":
        return TIER_INDEX["installed"]
    if reference_kind == "manifest":
        return TIER_INDEX["real_system"]
    return -1  # prose: no tier at all


def _represented_command(represented: str) -> str:
    """Normalize one evidence-line command representation.

    An optional single surrounding backtick pair is stripped (the audit
    prompt writes the exact command as `` `cmd ...` ``); the remaining text
    must then shlex-parse to the exact receipt argv (LOW5).  A nested or
    unbalanced backtick inside the command is rejected rather than guessed.
    """
    text = represented.strip()
    if len(text) >= 2 and text[0] == "`" and text[-1] == "`":
        inner = text[1:-1].strip()
        if inner and "`" not in inner:
            return inner
        raise ReceiptError(f"evidence line command is malformed: {represented!r}")
    return text


def parse_evidence_lines(text: str) -> List[Dict[str, object]]:
    """Extract ``- Executable evidence:`` lines with status/citation/tier.

    LOW4: the status token is parsed with an **anchored whole-token grammar**
    — the first whitespace-delimited token that is exactly ``PASS``, ``FAIL``,
    or ``BLOCKED`` decides the status; a status word embedded inside a
    command, path, or citation is never a status, and no other spelling
    (``PASSED``, ``BLOCK``, …) is accepted.  The command representation is the
    text before that anchored status token (``command`` key), kept for the
    exact-argv check against the cited receipt (LOW5).
    """
    lines: List[Dict[str, object]] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped.startswith(EVIDENCE_PREFIX):
            continue
        body = stripped[len(EVIDENCE_PREFIX):].strip()
        tokens = body.split()
        status_index = next(
            (index for index, token in enumerate(tokens) if token in STATUS_TOKENS),
            None,
        )
        if status_index is None:
            raise ReceiptError(
                f"executable evidence line lacks an anchored PASS/FAIL/BLOCKED "
                f"status token: {stripped}"
            )
        marker = tokens[status_index]
        command = " ".join(tokens[:status_index])
        receipt = RECEIPT_RE.search(stripped)
        manifest = MANIFEST_RE.search(stripped)
        tier_claim = TIER_CLAIM_RE.search(stripped)
        lines.append({
            "line": stripped,
            "marker": marker,
            "command": command,
            "receipt": receipt.group(1) if receipt else None,
            "manifest": manifest.group(1) if manifest else None,
            "tier": tier_claim.group(1) if tier_claim else None,
        })
    return lines


def validate_evidence_lines(
    root: Path,
    lines: Sequence[Dict[str, object]],
    *,
    result: str,
    expected_round: Optional[int] = None,
    expected_base: Optional[str] = None,
    expected_nonce: Optional[str] = None,
) -> None:
    """Validate evidence lines (PASS exit 0, BLOCKED forces findings, refs).

    - a PASS/FAIL line must carry an exact ``[receipt: …]`` or (PASS only)
      ``[manifest: …]`` reference; free-text model claims fail;
    - PASS requires the bound to exit 0 (receipt exit 0 or an accepted pass
      manifest); FAIL requires a receipt with a non-zero exit and can never
      cite a pass manifest;
    - any BLOCKED evidence forces ``result: findings``;
    - a declared ``tier=…`` claim can never exceed its channel ceiling
      (evidence tiers are never elevated by a model claim);
    - every receipt must match the active audit coordinator round/base/nonce
      when supplied (stale or reused receipts fail closed).
    """
    if result not in ("pass", "findings"):
        raise ReceiptError(f"unsupported evidence result: {result!r}")
    blocked_anywhere = any(entry.get("marker") == "BLOCKED" for entry in lines)
    if blocked_anywhere and result == "pass":
        raise ReceiptError("BLOCKED evidence forces result: findings")
    for entry in lines:
        marker = str(entry["marker"])
        receipt_ref = entry.get("receipt")
        manifest_ref = entry.get("manifest")
        tier_claim = entry.get("tier")
        if marker == "BLOCKED":
            continue
        if not receipt_ref and not manifest_ref:
            raise ReceiptError(
                f"executable evidence line has no receipt/manifest reference "
                f"(fabricated prose can never elevate evidence): {entry['line']}"
            )
        exit_code = 0
        if receipt_ref:
            data = validate_receipt(root, receipt_ref)
            # LOW5: the command the evidence line represents must exactly
            # equal the receipt's recorded argv — a line that cites a receipt
            # minted for a different command can never certify it (a model can
            # never relabel or abbreviate what the coordinator executed).
            try:
                represented_argv = shlex.split(
                    _represented_command(str(entry.get("command", "")))
                )
            except ValueError as exc:
                raise ReceiptError(
                    f"evidence line command is not a clean argv "
                    f"(unbalanced quotes): {entry['line']}"
                ) from exc
            if represented_argv != data["argv"]:
                raise ReceiptError(
                    f"evidence line command {entry.get('command')!r} does not "
                    f"exactly equal the receipt argv {data['argv']!r} (a receipt "
                    "certifies only the exact command it executed): "
                    f"{entry['line']}"
                )
            if expected_round is not None and data["coordinator_round"] != expected_round:
                raise ReceiptError(
                    f"receipt {receipt_ref} belongs to round "
                    f"{data['coordinator_round']}, not the active round "
                    f"{expected_round} (stale or reused across rounds)"
                )
            if expected_base is not None and data["evidence_commit"] != expected_base:
                raise ReceiptError(
                    f"receipt {receipt_ref} evidence_commit does not equal the "
                    f"campaign audit base (stale or reused)"
                )
            if expected_nonce is not None and data["coordinator_nonce"] != expected_nonce:
                raise ReceiptError(
                    f"receipt {receipt_ref} does not match the active audit "
                    f"coordinator nonce"
                )
            exit_code = int(data["exit_code"])
            kind = "receipt"
        else:
            if marker == "FAIL":
                raise ReceiptError(
                    f"FAIL evidence cannot cite a runner manifest (a manifest "
                    f"certifies only a clean pass): {entry['line']}"
                )
            if expected_base is None:
                raise ReceiptError(
                    f"runner manifest {manifest_ref} requires the campaign "
                    f"audit base binding"
                )
            validate_manifest_ref(root, str(manifest_ref), expected_base)
            kind = "manifest"
        if tier_claim is not None:
            if tier_claim not in TIER_INDEX:
                raise TierError(
                    f"evidence line declares an unknown tier "
                    f"{tier_claim!r}: {entry['line']}"
                )
            ceiling = evidence_tier_ceiling(kind)
            if TIER_INDEX[tier_claim] > ceiling:
                raise TierError(
                    f"evidence line {entry['line']} claims tier "
                    f"{tier_claim} above the {kind} channel ceiling "
                    f"{TIERS[ceiling]}; a model claim can never elevate "
                    f"evidence"
                )
        if marker == "PASS" and exit_code != 0:
            raise ReceiptError(f"PASS claim has a receipt with exit {exit_code}: {entry['line']}")
        if marker == "FAIL" and receipt_ref and exit_code == 0:
            raise ReceiptError(f"FAIL claim has a receipt with exit 0: {entry['line']}")


def validate_evidence_text(
    text: str,
    *,
    root: Path,
    result: str,
    expected_round: Optional[int] = None,
    expected_base: Optional[str] = None,
    expected_nonce: Optional[str] = None,
) -> int:
    """Validate an audit report's ``## Evidence reviewed`` section.

    Returns the number of executable evidence lines processed.  A report
    with no evidence section, a prose-only PASS/FAIL, BLOCKED-in-pass,
    stale/reused receipts, or a tier-elevating claim all fail closed.
    """
    if len(text) > MAX_EVIDENCE_TEXT:
        raise ReceiptError("audit report text exceeds the evidence bound")
    match = re.search(
        r"^## Evidence reviewed\s*\n(.*?)(?=^## |\Z)", text, re.M | re.S
    )
    if not match:
        raise ReceiptError("audit report has no Evidence section")
    lines = parse_evidence_lines(match.group(1))
    # LOW4: the BLOCKED scan covers the whole report (a standalone BLOCKED
    # token anywhere forces findings, exactly like the visible checker), and
    # every BLOCKED evidence line is also collected by the anchored parser.
    if re.search(r"(?<!\S)BLOCKED(?!\S)", text) and result == "pass":
        raise ReceiptError("BLOCKED evidence forces result: findings")
    validate_evidence_lines(
        root,
        lines,
        result=result,
        expected_round=expected_round,
        expected_base=expected_base,
        expected_nonce=expected_nonce,
    )
    return len(lines)


# ---------------------------------------------------------------------------
# Module entrypoint (hidden factory loop CLI)
# ---------------------------------------------------------------------------


def _config_command(
    root: Path,
    *,
    commit: Optional[str] = None,
    git: Optional[object] = None,
) -> Tuple[str, ...]:
    """Read ``[verification].campaign_command`` from the committed config.

    LOW5 (committed config binding): the worktree ``.factory/config.toml``
    must equal the exact committed blob at the bound commit — a substituted
    config (a campaign_command pointing at an attacker script) fails closed
    before any binding or execution.  The commit defaults to the current HEAD
    when not supplied.
    """
    import tomllib

    config_path = root / ".factory/config.toml"
    raw, _ = secure_read_bytes(config_path, maximum=1024 * 1024, what="factory config")
    if commit is None:
        commit = gitutil.resolve_head(root)
        if not commit:
            raise EvidenceError("cannot resolve the bound commit for the factory config")
    try:
        committed = _committed_blob_bytes(root, git, commit, ".factory/config.toml")
    except VerifierBindingError as exc:
        raise EvidenceError(
            f"factory config is not tracked at {commit[:12]}: {exc}"
        ) from exc
    if raw != committed:
        raise EvidenceError(
            f"factory config worktree bytes do not equal the committed blob at "
            f"{commit[:12]}:.factory/config.toml (config substitution fails "
            "closed before any verifier binding)"
        )
    try:
        config = tomllib.loads(raw.decode("utf-8"))
    except (UnicodeError, tomllib.TOMLDecodeError) as exc:
        raise EvidenceError(f"invalid factory config: {exc}") from exc
    command = config.get("verification", {}).get("campaign_command")
    if (
        not isinstance(command, list)
        or not command
        or not all(isinstance(arg, str) and arg and "\x00" not in arg for arg in command)
    ):
        raise EvidenceError("verification.campaign_command must be a non-empty argv array")
    return tuple(command)


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m factory.loop.evidence",
        description="Hidden evidence, receipt, and verifier authority (Task 12)",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    bind = sub.add_parser("bind", help="open and bind the committed verifier entrypoint")
    bind.add_argument("--root", default=str(Path.cwd()))
    bind.add_argument("--commit", default=None, help="bound commit (default: HEAD)")

    reval = sub.add_parser("revalidate", help="fail-closed revalidation of a bound verifier")
    reval.add_argument("--root", default=str(Path.cwd()))
    reval.add_argument("--commit", default=None, help="current commit (default: git)")

    audit = sub.add_parser("validate-audit", help="validate audit evidence lines and refs")
    audit.add_argument("report", help="audit report path")
    audit.add_argument("--root", default=str(Path.cwd()))
    audit.add_argument("--result", choices=("pass", "findings"), default=None)
    audit.add_argument("--expected-round", type=int, default=None)
    audit.add_argument("--expected-base", default=None)
    audit.add_argument("--expected-nonce", default=None)

    args = parser.parse_args(argv)
    root = Path(args.root).absolute()
    try:
        if args.command == "bind":
            command = _config_command(root, commit=args.commit or None, git=None)
            binding = bind_verifier(
                root, command,
                commit=args.commit or None,
                git=None,
            )
            print(json.dumps(
                {"binding": binding.to_dict(), "sha256": binding.digest()},
                sort_keys=True, separators=(",", ":"),
            ))
            return 0
        if args.command == "revalidate":
            command = _config_command(root, commit=args.commit or None, git=None)
            binding = bind_verifier(
                root, command,
                commit=args.commit or None,
                git=None,
            )
            revalidate_verifier(root, binding, current_commit=args.commit)
            print(json.dumps(
                {"binding": binding.to_dict(), "sha256": binding.digest(),
                 "verified": True},
                sort_keys=True, separators=(",", ":"),
            ))
            return 0
        if args.command == "validate-audit":
            report = root / args.report
            text = report.read_text(encoding="utf-8")
            if len(text) > MAX_EVIDENCE_TEXT:
                raise ReceiptError("audit report exceeds the evidence bound")
            result = args.result
            if result is None:
                result = None
                for line in text.splitlines():
                    match = re.fullmatch(r"result:\s*(pass|findings)", line.strip())
                    if match:
                        result = match.group(1)
                if result is None:
                    raise ReceiptError("audit report has no machine-readable result")
            count = validate_evidence_text(
                text,
                root=root,
                result=result,
                expected_round=args.expected_round,
                expected_base=args.expected_base,
                expected_nonce=args.expected_nonce,
            )
            print(f"factory-evidence: valid ({count} executable evidence line(s), result={result})")
            return 0
    except EvidenceError as exc:
        print(f"factory-evidence: {exc}", file=sys.stderr)
        return 1
    except (OSError, UnicodeError) as exc:
        print(f"factory-evidence: {exc}", file=sys.stderr)
        return 1
    return 1


if __name__ == "__main__":
    sys.exit(main())
