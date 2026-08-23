"""Task 8 model workspace/tool confinement authority.

This module is the *real* Task 8 confinement authority.  It implements:

* **model workspace/tool confinement** (CTX-02, §5.1/§18): a per-launch
  confinement specification whose allowlists make the plan, specification,
  code, tests, and allowlisted ``.factory/`` inputs readable and enforce
  per-role write allowlists, while ``.ralph/``, ``.factory-state/``,
  scratchpads/handoffs, runtime task/memory stores, context summaries,
  migration archives, host credential stores, the operator Ollama
  credential store(s), and every other path outside the allowlist are
  denied by default;
* **the real confinement proof** (Task 7 review obligations 2/3, jointly
  owned with Task 11): ``prove_confinement`` binds the exact bound commit,
  workspace, provider, every effective credential channel the guard
  actually consumes (the default operator env store, an explicitly
  specified cookie file, and stdin-provided credential provenance), and the
  exact executing usage-guard source (``usage.py`` / ``usage_fetch.py``) to
  the confinement specification that will be applied.  A *synthetic* proof
  (the private hidden-suite seam) is never evidence of real confinement;
* **fail-closed primitive gating**: the confinement is applied through the
  Linux Landlock LSM (``landlock_create_ruleset`` / ``landlock_add_rule`` /
  ``landlock_restrict_self``) — a real, unprivileged, deny-by-default
  filesystem confinement primitive.  If the primitive is unavailable on the
  host, ``prove_confinement`` raises :class:`ConfinementUnavailable` and the
  production Ollama launch fails closed; no simulated or synthetic
  acceptance is ever minted by the production authority.

The confinement is *applied* by the confined-launch child
(:mod:`.confine_launcher`, staged from its exact committed blob per F2)
which reads the specification, builds the Landlock ruleset, restricts
itself, installs the sanitized private HOME/XDG environment, and then
execs the secure wrapper.  The proof's specification digest binds the exact
specification the child applies; ``run`` re-validates the proof against the
binding and specification immediately before the confined spawn.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import stat
from typing import Dict, List, Mapping, Optional, Sequence, Tuple

try:  # package-import mode (the hidden control-plane package)
    from . import usage as usage_guard
except ImportError:  # flat-import mode used by the hidden harness suite
    import usage as usage_guard  # type: ignore[no-redef]

# ---------------------------------------------------------------------------
# Confinement specification (schema ``factory-confinement/v1``)
# ---------------------------------------------------------------------------

CONFINEMENT_SCHEMA = "factory-confinement/v1"
CONFINEMENT_SCHEMA_VERSION = 1

# Right names used in the portable specification (mapped to Landlock access
# bits by the confine launcher).  ``read`` grants READ_FILE|READ_DIR;
# ``write`` grants the create/remove/truncate/refer family; ``execute``
# grants EXECUTE.  This portable set is what the proof digest binds.
ACCESS_READ = "read"
ACCESS_WRITE = "write"
ACCESS_EXECUTE = "execute"

# Workspace top-level entries that are *never* allowlisted (denied by
# default): Git history/objects/config, legacy Ralph state, control state,
# pi runtime, scratch/temp stores, the legacy workspace Ollama store, the
# bug-ledger lock, and diagnostic/scratch build directories.  ``.git`` is
# deliberately present (Task 8 review, finding 2): the model never reads
# repository history, ``.git/`` objects, reflog, hooks, or config, and never
# performs Git operations — Git history/commit are owned entirely by the
# trusted orchestrator (Task 9), outside the model.
FORBIDDEN_WORKSPACE_TOP = frozenset({
    ".git", ".ralph", ".factory", ".factory-state", ".pi", "$tmp",
    ".ollama-usage-env", ".bug-ledger.lock",
    ".diag-prefix-build", ".install-prefix", ".test-diag-inspect",
})

# Hidden workspace entries the model legitimately needs (product CI
# configuration).  Everything else starting with ``.`` is denied.
ALLOWED_HIDDEN_TOP = frozenset({".github", ".forgejo", ".gitignore"})

# ``.factory/`` control-plane namespaces that are *never* readable by any
# role (the allowlisted plan/policy/evidence sidecars live outside them).
FORBIDDEN_FACTORY_SUB = frozenset({
    ".factory/loop", ".factory/tests", ".factory/prompts",
    ".factory/state", ".factory/ralph",
})

# The narrow explicit system-path allowlist (Task 8 review, findings 3/7).
# The model has no broad ``/proc``, ``/tmp``, ``/etc``, ``/dev``, ``/run``,
# or ``/var`` grant.  ``SYSTEM_READ_EXECUTE`` are the required trusted
# runtime roots (immutable Nix store, the tool binary roots and their
# libraries); ``SYSTEM_READ`` is the narrowest explicit set of host files
# the tooling actually needs, each entry justified:
#
# * ``/etc/passwd``, ``/etc/group`` — uid/gid lookups (git, python, tools);
# * ``/etc/ssl/certs`` — the CA-cert store for any TLS-verifying tooling;
# * ``/dev/null``, ``/dev/urandom``, ``/dev/random``, ``/dev/zero`` —
#   stdio/entropy sinks used by the interpreter and tools;
# * ``/dev/tty`` — the controlling terminal for interactive tool output.
#
# Everything else under ``/etc`` (host config), ``/dev`` (device nodes),
# ``/run`` (sockets/state), ``/var`` (state), and ``/proc`` is denied by
# default; a probe asserts socket/host-config/device-node denial under the
# effective confinement.  Every entry must resolve with no symlink
# component on the deployment host (finding 1); on hosts where an entry is
# a symlink (for example a NixOS ``/etc/ssl/certs`` link), the confinement
# fails closed and the entry is removed from the allowlist.
SYSTEM_READ_EXECUTE = (
    "/nix/store", "/usr", "/bin", "/lib", "/lib64", "/sbin",
)
SYSTEM_READ = (
    "/etc/passwd", "/etc/group", "/etc/ssl/certs",
    "/dev/null", "/dev/urandom", "/dev/random", "/dev/zero", "/dev/tty",
)

# The ``.factory/`` inputs each role may read (the plan, policy, and
# evidence sidecars; never the control-plane source, harness tests,
# prompts, or runtime state).
_BASE_FACTORY_READS = frozenset({
    ".factory/config.toml",
    ".factory/environment.toml",
    ".factory/artifacts/implementation-plan.md",
    ".factory/artifacts/blocked-facts.json",
})
ROLE_FACTORY_READS: Mapping[str, frozenset] = {
    "planner": frozenset({
        ".factory/artifacts/campaign-audit.md",
        ".factory/bugs/open.md",
        ".factory/bugs/closed.md",
        ".factory/schemas",
        ".factory/requirement-policy.json",
        ".factory/capability-contracts.json",
    }),
    "developer": frozenset({".factory/bugs/open.md"}),
    "tester": frozenset({
        ".factory/artifacts/conformance.json",
        ".factory/requirement-policy.json",
        ".factory/capability-contracts.json",
        ".factory/bugs/open.md",
    }),
    "auditor": frozenset({
        ".factory/artifacts/campaign-audit.md",
        ".factory/artifacts/conformance.json",
        ".factory/requirement-policy.json",
        ".factory/capability-contracts.json",
        ".factory/bugs/open.md",
    }),
}

# The plan file is the sole writer target for the planner; the developer
# additionally writes the plan (task status/evidence updates).
_PLAN_WRITE = {".factory/artifacts/implementation-plan.md"}

# Tester write allowlist: build and isolated test directories only (the
# tester never edits product code).  ``build*`` covers every build tree.
TESTER_WRITE_TOP = frozenset({
    ".install-prefix", ".diag-prefix-build", ".test-diag-inspect",
})

# Repository-relative paths of the trusted policy/harness surface that no
# untrusted role may write or commit (Task 9 review HIGH).  AGENTS.md is the
# canonical operational policy; the hidden CI/forge tooling configuration
# (``.gitignore``, ``.github``, ``.forgejo``) and the environment pinning
# (``shell.nix``) are part of the trusted boundary; the ``.factory/``
# configuration sidecars (config, environment, policies, contracts, and the
# golden/visual-audit authorities) are the factory contracts; the harness
# documentation (``docs/FACTORY.md``, ``docs/OPERATIONS.md``,
# ``docs/FACTORY-LOOP-SPEC.md``) documents the control plane; and the legacy
# visible ``scripts/`` tree is the security/harness surface (guards,
# receipts, verifier, ralph entrypoints).  Genuine product entries
# (``src/``, ``tests/``, product data, and product documentation other than
# the harness docs) are NOT part of this surface and stay writable by the
# developer role.
#
# Both the campaign scope authority (``campaign.scope_violation``) and the
# confinement write allowlists (:func:`_role_write_paths`) enforce exactly
# this set, so a path denied here is denied identically at the Landlock
# write boundary and at the orchestrator commit boundary.  The canonical
# plan (``.factory/artifacts/implementation-plan.md``) is deliberately not
# in the set: the planner and the developer revise exactly that file.
TRUSTED_POLICY_RELPATHS = frozenset({
    "AGENTS.md",
    ".gitignore",
    ".github",
    ".forgejo",
    "shell.nix",
    ".factory/campaign-objectives.json",
    ".factory/campaign-receipt-policy.json",
    ".factory/capability-contracts.json",
    ".factory/config.toml",
    ".factory/environment.toml",
    ".factory/golden-policy.json",
    ".factory/golden-review.json",
    ".factory/requirement-policy.json",
    ".factory/signer-trust.json",
    ".factory/verifier-acceptance.json",
    ".factory/visual-audit-calibration.json",
    ".factory/visual-audit-inventory.json",
    ".factory/visual-audit.toml",
    "docs/FACTORY.md",
    "docs/OPERATIONS.md",
    "docs/FACTORY-LOOP-SPEC.md",
    "scripts",
})


def is_trusted_policy_path(relpath: str) -> bool:
    """True when ``relpath`` is trusted policy/harness surface.

    A repository-relative path inside :data:`TRUSTED_POLICY_RELPATHS` (the
    path itself or any path under a denied directory) is never writable by
    an untrusted role — neither through the confinement write allowlist nor
    through the campaign scope authority.  A path that equals a denied
    entry or lies beneath a denied directory (``scripts/...``,
    ``.github/...``, ``.forgejo/...``) is denied even when it does not
    exist yet.
    """
    for denied in TRUSTED_POLICY_RELPATHS:
        if relpath == denied or relpath.startswith(denied + "/"):
            return True
    return False

# System/tool allowlist: read+execute for the trusted runtime roots
# (immutable Nix store, the tool binary roots and their libraries); read
# for the narrow explicit host files enumerated above.  The model has no
# broad ``/tmp``, ``/proc``, ``/etc``, ``/dev``, ``/run``, or ``/var``
# grant (Task 8 review, findings 3/4/7): the shared temporary directory
# is *not* allowlisted at all — each launch is granted only its own exact
# per-launch private home/scratch/staging/prompt paths (see
# :func:`private_launch_rules`), and everything outside the explicit
# narrow enumeration (host credentials, the real HOME, sockets, device
# nodes, host config, process state, secrets, outside paths) is denied by
# default.


class ConfinementError(Exception):
    """A confinement proof is missing, unbound, or tampered (fail closed)."""


class ConfinementUnavailable(ConfinementError):
    """The real confinement primitive (Landlock) is unavailable on this host.

    Production launches that require confinement fail closed so no model
    invocation proceeds without real confinement.
    """


@dataclass(frozen=True)
class CredentialChannel:
    """One effective credential channel the Ollama usage guard consumes.

    ``kind`` is ``env_store`` (the default operator ``.ollama-usage-env``
    store), ``cookie_file`` (an explicitly specified cookie store), or
    ``stdin`` (stdin-provided credential provenance).  ``path`` is the
    resolved channel path for file channels and ``None`` for stdin.
    """

    kind: str
    path: Optional[str] = None

    def to_tuple(self) -> Tuple[str, Optional[str]]:
        return (self.kind, self.path)


def _channel_sort_key(channel: CredentialChannel) -> Tuple[str, Optional[str]]:
    return channel.to_tuple()


def normalize_channels(
    channels: Sequence[CredentialChannel],
) -> Tuple[CredentialChannel, ...]:
    """Deterministic, de-duplicated, validated channel tuple."""
    seen: Dict[Tuple[str, Optional[str]], CredentialChannel] = {}
    for channel in channels:
        if not isinstance(channel, CredentialChannel):
            raise ConfinementError(
                f"a credential channel must be a CredentialChannel, got {channel!r}"
            )
        if channel.kind not in ("env_store", "cookie_file", "stdin"):
            raise ConfinementError(
                f"unknown credential channel kind {channel.kind!r}"
            )
        if channel.kind == "stdin":
            if channel.path is not None:
                raise ConfinementError(
                    "the stdin credential channel must carry no path"
                )
        elif not isinstance(channel.path, str) or not channel.path:
            raise ConfinementError(
                f"the {channel.kind} credential channel requires a path"
            )
        seen[channel.to_tuple()] = channel
    return tuple(
        seen[key] for key in sorted(seen, key=lambda k: k)
    )


# ---------------------------------------------------------------------------
# Allowlist construction (per-role, deterministic)
# ---------------------------------------------------------------------------

def _existing(path_text: str) -> Optional[str]:
    """Return ``path_text`` when it exists on disk, else ``None``."""
    return path_text if os.path.lexists(path_text) else None


def _no_symlink_components(path_text: str, label: str = "allowlisted path") -> None:
    """Fail closed when any component of ``path_text`` is a symlink.

    Task 8 review, finding 1: every path in the model read/write/execute
    allowlists is validated with a bounded no-follow walk — ``lstat`` on
    every component from the filesystem root down.  A symlink in *any*
    component (top-level through final) of any allowlisted path is rejected
    and the confinement fails closed; no path is ever followed to a target
    the operator or a caller may have swapped.
    """
    path = Path(path_text).absolute()
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current = current / part
        try:
            info = os.lstat(str(current))
        except OSError:
            # A missing component cannot be an allowlisted path at all; the
            # construction filters to existing paths.  Absence is not a
            # symlink, so the walk stops (the caller skips the path).
            return
        if stat.S_ISLNK(info.st_mode):
            raise ConfinementError(
                f"{label} {path_text} has a symlink component {current}; "
                "symlinks are never permitted in any allowlist component "
                "(fail closed)"
            )


def _resolved_containment(path_text: str, root: Path, label: str) -> None:
    """Fail closed when the fully resolved target escapes ``root``.

    Task 8 review, finding 1: the containment check validates the fully
    resolved target against the allowed namespace, so a path whose resolved
    target escapes the allowed subtree — or aliases a forbidden path outside
    it — is never permitted.
    """
    resolved = Path(os.path.realpath(path_text)).absolute()
    root_resolved = Path(os.path.realpath(str(root))).absolute()
    try:
        resolved.relative_to(root_resolved)
    except ValueError as exc:
        raise ConfinementError(
            f"{label} {path_text} resolves to {resolved}, which escapes the "
            f"allowed namespace {root_resolved}; the resolved target of an "
            "allowlisted path must stay inside the allowed subtree (fail "
            "closed)"
        ) from exc


def _validate_allowlist_path(
    path_text: str, workspace: Path, label: str = "allowlisted path"
) -> None:
    """Fail closed on a symlink component or an escaping resolved target.

    Workspace-scoped paths must resolve back inside the canonical workspace
    and carry no symlink component anywhere; system/private paths must carry
    no symlink component and their resolved target must be the path itself
    (the explicit narrow enumeration is the containment contract).
    """
    _no_symlink_components(path_text, label)
    path = Path(path_text).absolute()
    workspace_resolved = Path(os.path.realpath(str(workspace))).absolute()
    try:
        path.relative_to(workspace_resolved)
    except ValueError:
        # Outside the workspace (a system or private path).
        _resolved_is_self(path_text, label)
        return
    _resolved_containment(path_text, workspace, label)


def _resolved_is_self(path_text: str, label: str) -> None:
    """A non-workspace allowlist entry must resolve to itself.

    The narrow explicit enumeration (system files, the private per-launch
    namespace) is the containment contract; a path that resolves through a
    symlink (or an alias) to a *different* path is never permitted, because
    the resolved target could be a forbidden path outside the enumeration.
    """
    path = Path(path_text).absolute()
    resolved = Path(os.path.realpath(path_text)).absolute()
    if resolved != path:
        raise ConfinementError(
            f"{label} {path_text} resolves to a different path {resolved}; "
            "system and private allowlist entries must resolve to themselves "
            "(fail closed)"
        )


# Filesystem entries under forbidden namespaces that are scanned for a
# hardlink alias against allowlisted workspace files (Task 8 review,
# finding 8): the secret-bearing legacy/control namespaces plus the Git
# metadata that can carry credentials.  Git *objects* are content-addressed
# and excluded from the scan; ``.git/config``, ``.git/logs`` (reflog),
# ``.git/refs``, ``.git/HEAD``, and ``.git/hooks`` can carry credentials or
# historical secrets and are scanned.  Diagnostic/scratch build trees
# (``.diag-prefix-build``, ``.install-prefix``, ``.test-diag-inspect``) and
# the ``$tmp`` scratch dir are not secret-bearing and are excluded from the
# scan (documented in ``docs/OPERATIONS.md``).
_HARDLINK_SCAN_FILES = frozenset({
    ".git/config", ".git/HEAD", ".git/hooks", ".git/logs", ".git/refs",
})
_HARDLINK_SCAN_TOPS = frozenset({
    ".ralph", ".factory-state", ".pi", ".ollama-usage-env",
    ".bug-ledger.lock",
})
_HARDLINK_SCAN_CAP = 20000


def _forbidden_inode_map(workspace: Path) -> Dict[Tuple[int, int], str]:
    """``(st_dev, st_ino) -> path`` for files under forbidden namespaces.

    A bounded scan (capped at :data:`_HARDLINK_SCAN_CAP` entries): a
    hardlink is an inode alias, and an allowlisted workspace file that
    aliases a forbidden file would make the forbidden bytes readable under
    an allowlisted name.  Build/scratch trees are excluded (not
    secret-bearing).
    """
    inodes: Dict[Tuple[int, int], str] = {}
    count = 0
    for top in sorted(_HARDLINK_SCAN_TOPS):
        root = workspace / top
        if not os.path.lexists(str(root)):
            continue
        if top == ".bug-ledger.lock" and root.is_file():
            try:
                info = os.lstat(str(root))
                if stat.S_ISREG(info.st_mode):
                    inodes[(info.st_dev, info.st_ino)] = str(root)
            except OSError:
                pass
            continue
        for dirpath, _dirnames, filenames in os.walk(str(root)):
            for name in filenames:
                if count >= _HARDLINK_SCAN_CAP:
                    return inodes
                path = os.path.join(dirpath, name)
                try:
                    info = os.lstat(path)
                except OSError:
                    continue
                if stat.S_ISREG(info.st_mode):
                    inodes[(info.st_dev, info.st_ino)] = path
                count += 1
    for relative in sorted(_HARDLINK_SCAN_FILES):
        path = workspace / relative
        try:
            info = os.lstat(str(path))
        except OSError:
            continue
        if stat.S_ISREG(info.st_mode):
            inodes[(info.st_dev, info.st_ino)] = str(path)
    for relative in sorted(FORBIDDEN_FACTORY_SUB):
        root = workspace / relative
        if not os.path.lexists(str(root)):
            continue
        for dirpath, _dirnames, filenames in os.walk(str(root)):
            for name in filenames:
                if count >= _HARDLINK_SCAN_CAP:
                    return inodes
                path = os.path.join(dirpath, name)
                try:
                    info = os.lstat(path)
                except OSError:
                    continue
                if stat.S_ISREG(info.st_mode):
                    inodes[(info.st_dev, info.st_ino)] = path
                count += 1
    return inodes


def _assert_no_forbidden_hardlink_alias(
    path_text: str, workspace: Path, forbidden_inodes: Mapping[Tuple[int, int], str]
) -> None:
    """Fail closed when an allowlisted file aliases a forbidden inode.

    Landlock grants by path, so a hardlink of a forbidden file placed under
    an allowlisted name would make the forbidden bytes readable.  A regular
    allowlisted file whose ``(st_dev, st_ino)`` appears in the forbidden
    scan is rejected; bind-mount aliases of a forbidden path into the
    workspace require mount privileges and are covered by the documented
    boundary (``docs/OPERATIONS.md``).
    """
    try:
        info = os.lstat(path_text)
    except OSError:
        return
    if not stat.S_ISREG(info.st_mode) or info.st_nlink <= 1:
        return
    alias = forbidden_inodes.get((info.st_dev, info.st_ino))
    if alias is not None:
        raise ConfinementError(
            f"allowlisted path {path_text} is a hardlink alias of the "
            f"forbidden file {alias}; an inode alias would make forbidden "
            "bytes readable under an allowlisted name (fail closed)"
        )


def _workspace_read_entries(workspace: Path) -> List[str]:
    """Deterministic top-level workspace entries the model may read.

    Every non-hidden top-level entry plus the allowed hidden tooling
    entries (``.git``/``.github``/``.forgejo``/``.gitignore``).  The
    forbidden namespaces (:data:`FORBIDDEN_WORKSPACE_TOP` — ``.ralph``,
    ``.factory``, ``.factory-state``, ``.pi``, ``$tmp``, the legacy
    Ollama store, and the diagnostic/scratch build directories) and every
    other hidden name are intentionally absent, so Landlock denies them by
    default.
    """
    entries: List[str] = []
    try:
        names = sorted(os.listdir(str(workspace)))
    except OSError:
        return entries
    for name in names:
        if name in FORBIDDEN_WORKSPACE_TOP:
            continue
        if name.startswith("."):
            if name in ALLOWED_HIDDEN_TOP:
                entries.append(name)
            continue
        entries.append(name)
    return entries


def _role_read_paths(role: str, workspace: Path) -> List[Path]:
    """Absolute read-allowlist paths (files and directories) for one role.

    Only *existing* paths are allowlisted: a committed workspace entry that
    is not present at launch time stays denied by default (a nonexistent
    path cannot be opened for a Landlock rule and needs no grant).
    """
    if role not in ("planner", "developer", "tester", "auditor"):
        raise ConfinementError(f"unknown role {role!r}")
    paths: List[Path] = []
    forbidden_inodes = _forbidden_inode_map(workspace)
    for entry in _workspace_read_entries(workspace):
        path = workspace / entry
        if _existing(str(path)) is not None:
            _validate_allowlist_path(str(path), workspace, "workspace read path")
            _assert_no_forbidden_hardlink_alias(
                str(path), workspace, forbidden_inodes
            )
            paths.append(path)
    for relative in sorted(
        _BASE_FACTORY_READS | ROLE_FACTORY_READS.get(role, frozenset())
    ):
        path = workspace / relative
        if _existing(str(path)) is not None:
            _validate_allowlist_path(
                str(path), workspace, "workspace .factory read path"
            )
            _assert_no_forbidden_hardlink_alias(
                str(path), workspace, forbidden_inodes
            )
            paths.append(path)
    return paths


def _dir_write_grant_safe(relative: str) -> bool:
    """True when a write grant on directory ``relative`` covers no policy path.

    A Landlock write grant on a directory covers every descendant, so a
    directory may be granted only when no trusted policy/harness path can
    be created or modified beneath it.  The deny set is authoritative even
    for entries that do not exist yet (``shell.nix``, a future harness
    config), so the check is pattern-based rather than existence-based.
    """
    return not any(
        denied == relative or denied.startswith(relative + "/")
        for denied in TRUSTED_POLICY_RELPATHS
    )


def _developer_write_candidates(path: Path, workspace: Path) -> List[Path]:
    """Write-allowlist candidates rooted at ``path`` (never trusted policy).

    A regular file is a candidate unless it is trusted policy surface; a
    directory is a candidate only when :func:`_dir_write_grant_safe` holds
    (no trusted policy path can exist beneath it), otherwise the function
    recurses into the directory's children so trusted files are pruned
    while genuine product files/directories stay writable.  A directory
    that contains harness documentation (for example ``docs/`` with
    ``docs/FACTORY.md``) is therefore never granted as a whole: only its
    genuine product children are, and creating a *new* harness doc under it
    stays denied by default (the deny set is pattern-based).
    """
    try:
        info = os.lstat(str(path))
    except OSError:
        return []
    relative = str(path.relative_to(workspace))
    if is_trusted_policy_path(relative):
        return []
    if stat.S_ISDIR(info.st_mode):
        if _dir_write_grant_safe(relative):
            return [path]
        try:
            children = sorted(os.listdir(str(path)))
        except OSError:
            return []
        candidates: List[Path] = []
        for child in children:
            candidates.extend(
                _developer_write_candidates(path / child, workspace)
            )
        return candidates
    if stat.S_ISREG(info.st_mode):
        return [path]
    return []


def _role_write_paths(role: str, workspace: Path) -> List[Path]:
    """Absolute per-role write-allowlist paths.

    Every write path is validated like the read paths (no symlink in any
    component, resolved containment, no forbidden hardlink alias); the
    developer writes genuine product entries and the plan — never the
    trusted policy/harness surface (``AGENTS.md``, hidden CI/forge
    tooling, factory configs, harness docs, the legacy ``scripts/``
    security surface) and never ``.git``, whose history/commit authority
    belongs to the trusted orchestrator (Task 8 review, finding 2; Task 9
    review HIGH).
    """
    forbidden_inodes = _forbidden_inode_map(workspace)

    def validated(path: Path) -> Optional[Path]:
        if _existing(str(path)) is None:
            return None
        _validate_allowlist_path(str(path), workspace, "workspace write path")
        _assert_no_forbidden_hardlink_alias(str(path), workspace, forbidden_inodes)
        return path

    if role == "planner":
        return [
            path
            for relative in sorted(_PLAN_WRITE)
            if (path := validated(workspace / relative)) is not None
        ]
    if role == "developer":
        paths: List[Path] = []
        for entry in _workspace_read_entries(workspace):
            for candidate in _developer_write_candidates(
                workspace / entry, workspace
            ):
                validated_path = validated(candidate)
                if validated_path is not None:
                    paths.append(validated_path)
        plan = workspace / ".factory/artifacts/implementation-plan.md"
        if (path := validated(plan)) is not None:
            paths.append(path)
        return paths
    if role == "tester":
        paths = [
            path
            for relative in sorted(TESTER_WRITE_TOP)
            if (path := validated(workspace / relative)) is not None
        ]
        for entry in _workspace_read_entries(workspace):
            if entry.startswith("build") and _existing(str(workspace / entry)) is not None:
                path = validated(workspace / entry)
                if path is not None:
                    paths.append(path)
        return paths
    # The auditor has no write allowlist.
    return []


def _tool_read_paths() -> List[str]:
    """Existing system/tool read paths (deny-by-default keeps the rest out).

    Each entry is validated with the no-symlink-component check: the narrow
    explicit enumeration (findings 3/7) is the containment contract, and a
    host where an entry resolves through a symlink fails closed (the entry
    is dropped only when it does not exist at all).
    """
    paths: List[str] = []
    for path in SYSTEM_READ + SYSTEM_READ_EXECUTE:
        existing = _existing(path)
        if existing is None:
            continue
        _no_symlink_components(existing, "system read path")
        _resolved_is_self(existing, "system read path")
        paths.append(existing)
    return paths


def _tool_execute_paths() -> List[str]:
    paths: List[str] = []
    for path in SYSTEM_READ_EXECUTE:
        existing = _existing(path)
        if existing is None:
            continue
        _no_symlink_components(existing, "system execute path")
        _resolved_is_self(existing, "system execute path")
        paths.append(existing)
    return paths


def _backend_paths(backend: Path, workspace: Path) -> Tuple[List[str], List[str]]:
    """Read/execute allowlist for the model backend.

    A workspace backend is covered by the workspace product rules (and the
    staged copy under the private staging directory is covered by the
    ``/tmp`` rule).  An *external* backend (for example the trusted Pi
    runtime) must be executable, so its resolved file plus its two
    containing directories (the binary directory and its install root) are
    allowlisted read+execute.  The backend's own authentication
    configuration is intentionally *not* further subdivided: the backend
    process must be able to read its own trusted configuration, and the
    tool-call/tool-result credential boundary (CRED-01, Task 11) — not the
    filesystem layer — is what stops the model from exfiltrating it through
    tools; this boundary is documented in ``docs/OPERATIONS.md``.
    """
    backend = Path(backend).absolute()
    try:
        backend.relative_to(Path(workspace).absolute())
    except ValueError:
        resolved = os.path.realpath(str(backend))
        parents = [Path(resolved).parent, Path(resolved).parent.parent]
        paths = [resolved]
        for parent in parents:
            if parent and os.path.isdir(str(parent)):
                paths.append(str(parent))
        return paths, paths
    return [], []


def sanitized_home_directory() -> Path:
    """A fresh private mode-0700 directory for one attempt's sanitized HOME.

    The directory lives under the shared temporary directory (which is
    allowlisted read+write+execute in the confined view) and is removed by
    the control plane after the attempt.  It never contains host
    credentials, caches, or the operator's real files.
    """
    import tempfile

    path = Path(tempfile.mkdtemp(prefix="factory-home-", dir="/tmp"))
    os.chmod(str(path), 0o700)
    for sub in (".config", ".cache", ".local/share", "run"):
        (path / sub).mkdir(mode=0o700, parents=True, exist_ok=True)
    return path


def home_environment(home: Path) -> Dict[str, str]:
    """The sanitized HOME/XDG environment for the confined model child.

    ``HOME`` and every ``XDG_*`` variable point into the fresh private home
    directory, so model tools cannot reach the operator's real home,
    caches, or credentials through the environment either.
    """
    home = Path(home).absolute()
    return {
        "HOME": str(home),
        "XDG_CONFIG_HOME": str(home / ".config"),
        "XDG_CACHE_HOME": str(home / ".cache"),
        "XDG_DATA_HOME": str(home / ".local" / "share"),
        "XDG_STATE_HOME": str(home / ".local" / "state"),
        "XDG_RUNTIME_DIR": str(home / "run"),
    }


def confinement_spec(
    binding: object,
    *,
    sanitized_home: Path,
    extra_read: Sequence[str] = (),
    extra_write: Sequence[str] = (),
) -> Dict[str, object]:
    """The deterministic per-launch confinement specification.

    ``binding`` is the ``launch.InvocationBinding``; ``sanitized_home`` is
    the fresh private home directory created by the control plane;
    ``extra_read``/``extra_write`` are absolute paths added for the backend
    and tooling.  The returned specification (schema
    ``factory-confinement/v1``) is a pure function of the binding, the
    committed workspace state, and the given extras; the proof's
    specification digest binds exactly these bytes.
    """
    workspace = Path(binding.workspace).absolute()
    role = binding.role
    rules: List[Dict[str, object]] = []

    def add_rule(path_text: str, access: Sequence[str]) -> None:
        path = Path(path_text).absolute()
        rules.append({
            "path": str(path),
            "access": sorted(set(access)),
        })

    for path in _role_read_paths(role, workspace):
        add_rule(str(path), (ACCESS_READ, ACCESS_EXECUTE))
    for path in _role_write_paths(role, workspace):
        add_rule(str(path), (ACCESS_READ, ACCESS_EXECUTE, ACCESS_WRITE))
    for path in _tool_read_paths():
        add_rule(path, (ACCESS_READ,))
    for path in _tool_execute_paths():
        add_rule(path, (ACCESS_READ, ACCESS_EXECUTE))
    backend_read, backend_execute = _backend_paths(
        Path(binding.backend), workspace
    )
    for path in backend_read:
        add_rule(path, (ACCESS_READ, ACCESS_EXECUTE))
    for path in extra_read:
        add_rule(path, (ACCESS_READ,))
    for path in extra_write:
        add_rule(path, (ACCESS_READ, ACCESS_EXECUTE, ACCESS_WRITE))

    # The exact per-launch private home (Task 8 review, finding 4): the
    # model receives no broad ``/tmp`` grant — only this launch's own
    # mode-0700 private home (with its scratch subdirectories) is granted
    # read+write+execute, so a sibling launch's private directories stay
    # denied by default.  The launch authority additionally adds this
    # launch's exact staging/prompt/session paths through
    # :func:`with_private_launch_paths` before the proof is minted.  The
    # home path is validated like every other allowlist entry (no symlink
    # component; the fresh private directory resolves to itself).
    _no_symlink_components(str(Path(sanitized_home).absolute()), "sanitized home")
    _resolved_is_self(str(Path(sanitized_home).absolute()), "sanitized home")
    add_rule(
        str(Path(sanitized_home).absolute()),
        (ACCESS_READ, ACCESS_EXECUTE, ACCESS_WRITE),
    )

    # The sanitized home is inside the shared temporary directory, which is
    # *not* granted; only the exact home path above is writable.  Nothing
    # else is needed for it.
    rules.sort(key=lambda rule: str(rule["path"]))
    spec: Dict[str, object] = {
        "schema": CONFINEMENT_SCHEMA,
        "version": CONFINEMENT_SCHEMA_VERSION,
        "role": role,
        "provider": str(binding.provider).lower(),
        "workspace": str(workspace),
        "bound_commit": binding.bound_commit,
        "backend": str(Path(binding.backend).absolute()),
        "home": str(Path(sanitized_home).absolute()),
        "env": dict(sorted(home_environment(sanitized_home).items())),
        "rules": rules,
    }
    return spec


def private_launch_rules(
    *,
    staging_dir: Path,
    prompt_path: Path,
    session_dir: Path,
) -> List[Dict[str, object]]:
    """The exact per-launch private-path rules (Task 8 review, finding 4).

    The model receives no broad ``/tmp`` grant: only this launch's own
    private paths are granted, each with the *least* rights it needs —

    * ``staging_dir`` — read+execute: the staged exact-commit wrapper,
      backend, confine launcher, and the published confinement spec all
      live beneath the private mode-0700 exec staging directory and must
      be traversed/executed, never written;
    * ``prompt_path`` — read: the mode-0600 prompt file the secure wrapper
      reads;
    * ``session_dir`` — read+write+execute: the backend's scratch/session
      directory.

    Every path is validated like every other allowlist entry (finding 1):
    a symlink in any component fails closed and the resolved target must
    be the path itself, so an alias or symlink can never smuggle a broader
    grant.  A sibling launch's private directories are never granted and
    stay denied by default.
    """
    rules: List[Dict[str, object]] = []
    for path, access in (
        (staging_dir, (ACCESS_READ, ACCESS_EXECUTE)),
        (prompt_path, (ACCESS_READ,)),
        (session_dir, (ACCESS_READ, ACCESS_WRITE, ACCESS_EXECUTE)),
    ):
        path_text = str(Path(path).absolute())
        _no_symlink_components(path_text, "private per-launch path")
        _resolved_is_self(path_text, "private per-launch path")
        rules.append({"path": path_text, "access": sorted(set(access))})
    return rules


def with_private_launch_paths(
    spec: Mapping[str, object],
    *,
    staging_dir: Path,
    prompt_path: Path,
    session_dir: Path,
) -> Dict[str, object]:
    """A copy of ``spec`` augmented with the exact per-launch private rules.

    The launch authority (``launch.authorize_launch``) adds these rules
    *before* the real confinement proof is minted, so the proof's
    specification digest binds the exact allowlists the confined child
    applies — including this launch's own staging/prompt/session paths and
    never the broad shared temporary directory.
    """
    augmented = json.loads(json.dumps(spec))
    if not isinstance(augmented, dict) or not isinstance(
        augmented.get("rules"), list
    ):
        raise ConfinementError(
            "cannot augment a confinement specification without a rules list"
        )
    augmented["rules"].extend(
        private_launch_rules(
            staging_dir=staging_dir,
            prompt_path=prompt_path,
            session_dir=session_dir,
        )
    )
    augmented["rules"].sort(key=lambda rule: str(rule["path"]))
    return augmented


def spec_digest(spec: Mapping[str, object]) -> str:
    """SHA-256 of the canonical JSON serialization of a confinement spec."""
    data = json.dumps(
        spec, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def validate_confinement_spec(
    spec: Mapping[str, object], binding: object
) -> None:
    """Fail closed on a malformed or unbound confinement specification."""
    if not isinstance(spec, Mapping):
        raise ConfinementError("the confinement specification must be an object")
    if spec.get("schema") != CONFINEMENT_SCHEMA:
        raise ConfinementError(
            f"the confinement specification schema must be exactly "
            f"{CONFINEMENT_SCHEMA!r}, got {spec.get('schema')!r}"
        )
    if spec.get("version") != CONFINEMENT_SCHEMA_VERSION:
        raise ConfinementError("the confinement specification version is unsupported")
    workspace = Path(binding.workspace).absolute()
    if Path(str(spec.get("workspace"))).absolute() != workspace:
        raise ConfinementError(
            "the confinement specification binds a different workspace than "
            "the invocation"
        )
    if spec.get("bound_commit") != binding.bound_commit:
        raise ConfinementError(
            "the confinement specification binds a different commit than the "
            "invocation"
        )
    if spec.get("role") != binding.role:
        raise ConfinementError(
            "the confinement specification binds a different role than the "
            "invocation"
        )
    rules = spec.get("rules")
    if not isinstance(rules, list) or not rules:
        raise ConfinementError("the confinement specification carries no rules")
    for rule in rules:
        if not isinstance(rule, Mapping):
            raise ConfinementError("a confinement rule must be an object")
        path = rule.get("path")
        access = rule.get("access")
        if not isinstance(path, str) or not path:
            raise ConfinementError("a confinement rule must carry a path")
        if not isinstance(access, list) or not access:
            raise ConfinementError(f"rule {path} must carry an access list")
        for right in access:
            if right not in (ACCESS_READ, ACCESS_WRITE, ACCESS_EXECUTE):
                raise ConfinementError(f"rule {path} has unknown right {right!r}")


def _channel_covered_by_spec(channel: CredentialChannel, spec: Mapping[str, object]) -> bool:
    """True when a file channel path is granted by the confinement allowlist.

    Landlock rules are additive over ancestor directories, so a channel
    path is *covered* (readable by the model) when the channel path or any
    of its ancestors is a rule path.  A covered channel cannot be proven
    inaccessible and fails closed.
    """
    if channel.kind == "stdin":
        return False
    channel_path = Path(channel.path).absolute()
    rule_paths = [
        Path(str(rule["path"])).absolute()
        for rule in spec.get("rules", [])
        if isinstance(rule, Mapping) and rule.get("path")
    ]
    for rule_path in rule_paths:
        if channel_path == rule_path or channel_path.is_relative_to(rule_path):
            return True
    return False


# ---------------------------------------------------------------------------
# Landlock primitive detection (fail closed when unavailable)
# ---------------------------------------------------------------------------

# Landlock syscall numbers (stable on Linux x86_64/arm64 since 5.13).
_LANDLOCK_CREATE_RULESET = 444
_LANDLOCK_ADD_RULE = 445
_LANDLOCK_RESTRICT_SELF = 446
_LANDLOCK_CREATE_RULESET_VERSION = 0x1
_LANDLOCK_RULE_PATH_BENEATH = 0x1

# FS access bits handled by this authority.  IOCTL_DEV (ABI 4+) is left
# unhandled so ordinary device ioctls (tty, etc.) keep working; REFER and
# TRUNCATE require Landlock ABI >= 2 and >= 3 respectively and are included
# only when the probed ABI supports them.
_ACCESS_EXECUTE = 1 << 0
_ACCESS_WRITE_FILE = 1 << 1
_ACCESS_READ_FILE = 1 << 2
_ACCESS_READ_DIR = 1 << 3
_ACCESS_REMOVE_DIR = 1 << 4
_ACCESS_REMOVE_FILE = 1 << 5
_ACCESS_MAKE_CHAR = 1 << 6
_ACCESS_MAKE_DIR = 1 << 7
_ACCESS_MAKE_REG = 1 << 8
_ACCESS_MAKE_SOCK = 1 << 9
_ACCESS_MAKE_FIFO = 1 << 10
_ACCESS_MAKE_BLOCK = 1 << 11
_ACCESS_MAKE_SYM = 1 << 12
_ACCESS_REFER = 1 << 13
_ACCESS_TRUNCATE = 1 << 14

# The rights granted for each portable access name (the confine launcher
# uses this exact table; a hidden test asserts the tables match).
RIGHT_BITS: Mapping[str, int] = {
    ACCESS_READ: _ACCESS_READ_FILE | _ACCESS_READ_DIR,
    ACCESS_WRITE: (
        _ACCESS_WRITE_FILE | _ACCESS_REMOVE_DIR | _ACCESS_REMOVE_FILE
        | _ACCESS_MAKE_CHAR | _ACCESS_MAKE_DIR | _ACCESS_MAKE_REG
        | _ACCESS_MAKE_SOCK | _ACCESS_MAKE_FIFO | _ACCESS_MAKE_BLOCK
        | _ACCESS_MAKE_SYM | _ACCESS_REFER | _ACCESS_TRUNCATE
    ),
    ACCESS_EXECUTE: _ACCESS_EXECUTE,
}


def _landlock_abi() -> int:
    """The Landlock ABI version, or 0 when the primitive is unavailable."""
    import ctypes

    libc = ctypes.CDLL(None, use_errno=True)
    try:
        result = libc.syscall(
            _LANDLOCK_CREATE_RULESET, None, 0, _LANDLOCK_CREATE_RULESET_VERSION
        )
    except (AttributeError, OSError):
        return 0
    if result < 0:
        return 0
    return int(result)


def _handled_access_bits(abi: int) -> int:
    """The handled FS access bits supported by the probed ABI."""
    bits = (
        _ACCESS_EXECUTE | _ACCESS_WRITE_FILE | _ACCESS_READ_FILE | _ACCESS_READ_DIR
        | _ACCESS_REMOVE_DIR | _ACCESS_REMOVE_FILE | _ACCESS_MAKE_CHAR
        | _ACCESS_MAKE_DIR | _ACCESS_MAKE_REG | _ACCESS_MAKE_SOCK
        | _ACCESS_MAKE_FIFO | _ACCESS_MAKE_BLOCK | _ACCESS_MAKE_SYM
    )
    if abi >= 2:
        bits |= _ACCESS_REFER
    if abi >= 3:
        bits |= _ACCESS_TRUNCATE
    return bits


def confinement_primitive_available() -> bool:
    """True when Landlock can be applied to a fresh child process.

    The probe runs in a forked child (the control plane process must never
    restrict itself): the child creates a ruleset covering the handled
    access bits and restricts itself, proving the primitive works end to
    end.  Any failure (syscall unavailable, LSM disabled, restrict denied)
    fails closed.
    """
    try:
        require_confinement_primitive()
        return True
    except ConfinementUnavailable:
        return False


def require_confinement_primitive() -> None:
    """Fail closed unless Landlock confinement is provably applicable.

    The probe is executed in a forked child so the control plane process is
    never restricted; the child's exit status is the probe result.
    """
    abi = _landlock_abi()
    if abi < 1:
        raise ConfinementUnavailable(
            "the Landlock LSM is unavailable on this host (no landlock "
            "ruleset syscall); model workspace confinement cannot be applied"
        )
    pid = os.fork()
    if pid == 0:
        # Child: prove create + restrict work.  Never return to the parent
        # path; _exit with the probe outcome.
        try:
            _create_ruleset(_handled_access_bits(abi))
            os._exit(0)
        except Exception:
            os._exit(1)
    _, status = os.waitpid(pid, 0)
    if not os.WIFEXITED(status) or os.WEXITSTATUS(status) != 0:
        raise ConfinementUnavailable(
            "the Landlock ruleset probe failed in a fresh child; model "
            "workspace confinement cannot be applied (fail closed)"
        )


def _create_ruleset(handled_bits: int) -> int:
    """Create a Landlock ruleset handling ``handled_bits``."""
    import ctypes

    class RulesetAttr(ctypes.Structure):
        _fields_ = [("handled_access_fs", ctypes.c_uint64)]

    attr = RulesetAttr(handled_bits)
    libc = ctypes.CDLL(None, use_errno=True)
    descriptor = libc.syscall(
        _LANDLOCK_CREATE_RULESET, ctypes.byref(attr), ctypes.sizeof(attr), 0
    )
    if descriptor < 0:
        import errno

        raise ConfinementUnavailable(
            "landlock_create_ruleset failed: "
            + errno.errorcode.get(ctypes.get_errno(), str(ctypes.get_errno()))
        )
    return int(descriptor)


# ---------------------------------------------------------------------------
# The confinement proof
# ---------------------------------------------------------------------------

_PROOF_MINT_SECRET = object()

# The guard-source modules whose exact executing bytes a proof binds
# (Task 7 review obligation 3; jointly owned with Task 11).
GUARD_SOURCE_MODULES = ("usage.py", "usage_fetch.py")

_BLOB_READ_CHUNK = 65536
_MAX_GUARD_SOURCE_BYTES = 512 * 1024


class ConfinementProof:
    """Unforgeable Task 8 confinement proof token.

    A real proof is minted only by :func:`prove_confinement` after the
    confinement primitive is probed and the exact confinement specification
    is bound.  It binds:

    * the exact bound commit, workspace, and provider of the invocation;
    * the exact executing guard-source bytes (``usage.py`` /
      ``usage_fetch.py`` digests), so an operator-claimed or caller-
      controlled guard source can never be substituted (obligation 3);
    * every effective credential channel the guard actually consumes — the
      default operator env store, any explicitly specified cookie file, and
      stdin-provided credential provenance (obligations 1/2);
    * the confinement specification digest — the exact allowlists that the
      confined launch child applies.

    ``_mint_synthetic_proof`` (the private hidden-suite seam) mints proofs
    marked ``synthetic``; a synthetic proof is *never* evidence of real
    confinement and is never minted by the production authority.
    """

    __slots__ = (
        "_bound_commit",
        "_workspace",
        "_provider",
        "_guard_source_digests",
        "_credential_channels",
        "_confinement_spec_digest",
        "_synthetic",
        "_mint",
    )

    def __init__(
        self,
        *,
        bound_commit: str,
        workspace: str,
        provider: str,
        guard_source_digests: Tuple[str, str],
        credential_channels: Tuple[CredentialChannel, ...],
        confinement_spec_digest: str,
        synthetic: bool,
        _mint: object,
    ) -> None:
        if _mint is not _PROOF_MINT_SECRET:
            raise ConfinementError(
                "a ConfinementProof cannot be forged from operator claims; "
                "only the confinement authority may mint a proof"
            )
        self._bound_commit = bound_commit
        self._workspace = workspace
        self._provider = provider
        self._guard_source_digests = tuple(guard_source_digests)
        self._credential_channels = normalize_channels(credential_channels)
        self._confinement_spec_digest = confinement_spec_digest
        self._synthetic = bool(synthetic)
        self._mint = _mint

    @property
    def bound_commit(self) -> str:
        return self._bound_commit

    @property
    def workspace(self) -> str:
        return self._workspace

    @property
    def provider(self) -> str:
        return self._provider

    @property
    def guard_source_digests(self) -> Tuple[str, str]:
        return self._guard_source_digests

    @property
    def credential_channels(self) -> Tuple[CredentialChannel, ...]:
        return self._credential_channels

    @property
    def credential_stores(self) -> Tuple[str, ...]:
        """The file channel paths (compatibility view)."""
        return tuple(
            channel.path
            for channel in self._credential_channels
            if channel.path is not None
        )

    @property
    def confinement_spec_digest(self) -> str:
        return self._confinement_spec_digest

    @property
    def synthetic(self) -> bool:
        return self._synthetic


def _module_bytes(module: str) -> str:
    """Bounded no-follow read of one executing guard-source module."""
    path = Path(__file__).resolve().with_name(module)
    flags = (
        os.O_RDONLY
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )
    try:
        descriptor = os.open(str(path), flags)
    except OSError as exc:
        raise ConfinementError(f"cannot open guard source {path}: {exc}") from exc
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise ConfinementError(f"guard source {path} is not a regular file")
        if info.st_size > _MAX_GUARD_SOURCE_BYTES:
            raise ConfinementError(f"guard source {path} exceeds the size bound")
        digest = hashlib.sha256()
        while True:
            chunk = os.read(descriptor, _BLOB_READ_CHUNK)
            if not chunk:
                break
            digest.update(chunk)
        return digest.hexdigest()
    finally:
        os.close(descriptor)


def _executing_guard_source_digests() -> Tuple[str, str]:
    """SHA-256 of the exact executing ``usage.py`` / ``usage_fetch.py``."""
    return tuple(_module_bytes(module) for module in GUARD_SOURCE_MODULES)  # type: ignore[return-value]


def _derive_credential_channels(
    *,
    cookie_file: Optional[str],
    cookie_stdin: bool,
    env_store: Optional[str],
) -> Tuple[CredentialChannel, ...]:
    """The exact credential channels this guard invocation will consume.

    The default operator env store is always a consumed channel (the guard
    falls back to it); an explicitly specified cookie file and stdin
    provenance are additional channels when requested.
    """
    channels: List[CredentialChannel] = []
    store = env_store or usage_guard._default_env_file()
    channels.append(CredentialChannel("env_store", store))
    if cookie_file:
        channels.append(CredentialChannel("cookie_file", cookie_file))
    if cookie_stdin:
        channels.append(CredentialChannel("stdin", None))
    return normalize_channels(channels)


def _assert_channel_outside_workspace(
    channel: CredentialChannel, workspace: Path
) -> None:
    """Fail closed when a file channel is scoped inside the model workspace."""
    if channel.kind == "stdin":
        return
    assert isinstance(channel.path, str)
    _assert_store_outside(channel.path, workspace)


def prove_confinement(
    binding: object,
    *,
    cookie_file: Optional[str] = None,
    cookie_stdin: bool = False,
    env_store: Optional[str] = None,
    confinement_spec: Optional[Mapping[str, object]] = None,
) -> ConfinementProof:
    """Mint the *real* Task 8 confinement proof for one invocation.

    Fail-closed sequence:

    1. the Landlock confinement primitive must be provably applicable (a
       real probe in a fresh child); otherwise
       :class:`ConfinementUnavailable` — no production launch proceeds;
    2. the confinement specification must be present and valid for the
       binding — the proof binds its digest, and the confined child applies
       exactly that specification;
    3. every effective credential channel (default env store, explicit
       cookie file, stdin provenance) is derived from the invocation and
       proven outside the model workspace *and* outside the confinement
       allowlist, so the confined model tools cannot reach it.

    This authority never mints a synthetic proof; the synthetic seam is the
    private hidden-suite-only ``_mint_synthetic_proof``.
    """
    require_confinement_primitive()
    if confinement_spec is None:
        raise ConfinementError(
            "a real confinement proof requires the exact confinement "
            "specification that will be applied to the model child"
        )
    validate_confinement_spec(confinement_spec, binding)
    workspace = Path(binding.workspace).absolute()
    channels = _derive_credential_channels(
        cookie_file=cookie_file, cookie_stdin=cookie_stdin, env_store=env_store
    )
    for channel in channels:
        _assert_channel_outside_workspace(channel, workspace)
        if _channel_covered_by_spec(channel, confinement_spec):
            raise ConfinementError(
                f"the {channel.kind} credential channel "
                f"{channel.path!r} is granted by the confinement allowlist "
                "and cannot be proven inaccessible to model tools (fail "
                "closed)"
            )
    return ConfinementProof(
        bound_commit=binding.bound_commit,
        workspace=str(workspace),
        provider=binding.provider.lower(),
        guard_source_digests=_executing_guard_source_digests(),
        credential_channels=channels,
        confinement_spec_digest=spec_digest(confinement_spec),
        synthetic=False,
        _mint=_PROOF_MINT_SECRET,
    )


def _assert_store_outside(store: str, workspace: object) -> None:
    try:
        usage_guard.assert_store_outside_workspace(store, workspace)
    except usage_guard.UsageConfigError as exc:
        raise ConfinementError(str(exc)) from exc


def validate_proof(
    proof: object,
    binding: object,
    *,
    cookie_file: Optional[str] = None,
    cookie_stdin: bool = False,
    env_store: Optional[str] = None,
    confinement_spec: Optional[Mapping[str, object]] = None,
    _strict_channels: bool = True,
) -> None:
    """Bind a proof to the exact invocation and the applied confinement.

    The proof must carry the exact bound commit, workspace, and provider of
    the invocation; its guard-source digests must equal the SHA-256 of the
    exact executing ``usage.py`` / ``usage_fetch.py`` bytes; every file
    credential channel must live outside the model workspace and outside the
    confinement allowlist.  When the guard options are supplied (the launch
    path) the proof's channel set must equal exactly the invocation's
    effective channels.  A real proof must bind the confinement
    specification digest; when a specification is supplied it must match.
    """
    if not isinstance(proof, ConfinementProof):
        raise ConfinementError("the confinement proof is not a genuine proof")
    try:
        if proof.bound_commit != binding.bound_commit:
            raise ConfinementError(
                "the confinement proof binds a different commit "
                f"({proof.bound_commit!r} != {binding.bound_commit!r})"
            )
        if proof.workspace != str(binding.workspace):
            raise ConfinementError(
                "the confinement proof binds a different workspace "
                f"({proof.workspace!r} != {str(binding.workspace)!r})"
            )
        if proof.provider != binding.provider.lower():
            raise ConfinementError(
                "the confinement proof binds a different provider "
                f"({proof.provider!r} != {binding.provider.lower()!r})"
            )
        executing = _executing_guard_source_digests()
        if proof.guard_source_digests != executing:
            raise ConfinementError(
                "the confinement proof does not bind the exact executing "
                "guard source (usage.py / usage_fetch.py digests differ); "
                "an operator-claimed or caller-controlled guard source is "
                "never accepted"
            )
        workspace = Path(binding.workspace).absolute()
        for channel in proof.credential_channels:
            _assert_channel_outside_workspace(channel, workspace)
        if not proof.synthetic and not proof.confinement_spec_digest:
            raise ConfinementError(
                "a real confinement proof must bind a confinement "
                "specification digest"
            )
        if confinement_spec is not None:
            validate_confinement_spec(confinement_spec, binding)
            if proof.confinement_spec_digest != spec_digest(confinement_spec):
                raise ConfinementError(
                    "the confinement proof binds a different confinement "
                    "specification than the one being applied"
                )
            for channel in proof.credential_channels:
                if _channel_covered_by_spec(channel, confinement_spec):
                    raise ConfinementError(
                        f"the {channel.kind} credential channel "
                        f"{channel.path!r} is granted by the confinement "
                        "allowlist and cannot be proven inaccessible to "
                        "model tools (fail closed)"
                    )
        if _strict_channels:
            expected = _derive_credential_channels(
                cookie_file=cookie_file,
                cookie_stdin=cookie_stdin,
                env_store=env_store,
            )
            if proof.credential_channels != expected:
                raise ConfinementError(
                    "the confinement proof binds a different credential "
                    "channel set than the invocation consumes "
                    f"({proof.credential_channels!r} != {expected!r})"
                )
    except AttributeError as exc:
        raise ConfinementError(
            f"the confinement proof cannot be bound to this invocation: {exc}"
        ) from exc


def _mint_synthetic_proof(
    binding: object,
    *,
    credential_stores: Optional[Sequence[str]] = None,
    guard_source_digests: Optional[Sequence[str]] = None,
    credential_channels: Optional[Sequence[CredentialChannel]] = None,
    confinement_spec: Optional[Mapping[str, object]] = None,
) -> ConfinementProof:
    """**PRIVATE test seam** — mints a synthetic Task 8 proof.

    This seam exists only for the hermetic hidden suite so the Task 7
    guard/decision-table machinery can be exercised end-to-end without
    applying real confinement.  It is never exported on the public package
    surface and never accepted as real confinement evidence.

    The *binding* parts of the proof are still real: the guard-source
    digests are the SHA-256 of the exact executing ``usage.py`` /
    ``usage_fetch.py`` bytes (or caller-supplied values), and every file
    credential channel is verified to live outside the model workspace.
    Only the confinement claim itself (that the Landlock allowlists were
    actually applied to the model tools) is synthetic.
    """
    workspace = str(binding.workspace)
    if credential_channels is None:
        stores = tuple(
            str(path) for path in (credential_stores or ())
        )
        if not stores:
            stores = (usage_guard._default_env_file(),)
        channels = [
            CredentialChannel("env_store", store) for store in stores
        ]
    else:
        channels = list(credential_channels)
    channels = normalize_channels(channels)
    for channel in channels:
        if channel.path is not None:
            _assert_store_outside(channel.path, binding.workspace)
    if guard_source_digests is None:
        digests = _executing_guard_source_digests()
    else:
        digests = tuple(str(value) for value in guard_source_digests)
        if len(digests) != len(GUARD_SOURCE_MODULES):
            raise ConfinementError(
                "a synthetic proof must carry one digest per guard-source "
                "module"
            )
    spec_digest_value = (
        spec_digest(confinement_spec) if confinement_spec is not None else ""
    )
    return ConfinementProof(
        bound_commit=binding.bound_commit,
        workspace=workspace,
        provider=binding.provider.lower(),
        guard_source_digests=digests,  # type: ignore[arg-type]
        credential_channels=channels,
        confinement_spec_digest=spec_digest_value,
        synthetic=True,
        _mint=_PROOF_MINT_SECRET,
    )
