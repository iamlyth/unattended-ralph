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
  the confinement specification that will be applied.  Installed code has
  no synthetic-proof mint or caller proof transport;
* **fail-closed primitive gating**: the confinement is applied through the
  Linux Landlock LSM (``landlock_create_ruleset`` / ``landlock_add_rule`` /
  ``landlock_restrict_self``) — a real, unprivileged, deny-by-default
  filesystem confinement primitive.  If the primitive is unavailable on the
  host, ``prove_confinement`` raises :class:`ConfinementUnavailable` and the
  production Ollama launch fails closed; no simulated acceptance is ever
  minted by the production authority.

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
import re
import shutil
import stat
import subprocess
import sys
from typing import Dict, List, Mapping, Optional, Sequence, Tuple

try:  # package-import mode (the hidden control-plane package)
    from . import gitutil
except ImportError:  # flat-import mode used by the hidden harness suite
    import gitutil  # type: ignore[no-redef]


class UsageConfigError(Exception):
    """Inert credential-path configuration error; no quota implementation."""


class _UsagePathAdapter:
    @staticmethod
    def _default_env_file() -> str:
        override = os.environ.get("OLLAMA_USAGE_ENV_FILE")
        if override:
            return override
        base = Path(os.environ["XDG_CONFIG_HOME"]) if os.environ.get(
            "XDG_CONFIG_HOME"
        ) else Path.home() / ".config"
        return str(base / "unattended-ralph" / "ollama-usage-env")

    @staticmethod
    def assert_store_outside_workspace(path_text: str, workspace: object) -> None:
        try:
            store = Path(path_text).resolve()
            root = Path(str(workspace)).resolve()
        except OSError as exc:
            raise UsageConfigError("cannot resolve operator usage store") from exc
        if store == root or store.is_relative_to(root):
            raise UsageConfigError("operator usage store is inside model workspace")


_USAGE_PATHS = _UsagePathAdapter()

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

# ``.factory/`` control-plane namespaces denied by default. Tester/auditor
# receive explicit read-only loop/test grants below so verifier and state
# integrity can be inspected; no role receives runtime-state or prompt bytes.
FORBIDDEN_FACTORY_SUB = frozenset({
    ".factory/loop", ".factory/tests", ".factory/prompts",
    ".factory/state", ".factory/ralph",
})

# The narrow explicit system-path allowlist (Task 8 review, findings 3/7).
# The model has no broad ``/proc``, ``/tmp``, ``/etc``, ``/dev``, ``/run``,
# or ``/var`` grant.  ``SYSTEM_READ_ROOTS`` are non-Nix runtime/library roots.
# The Nix store root is deliberately absent: :func:`_toolchain_closure_paths`
# resolves only the immutable transitive closures of exact approved tools and
# backend runtimes, and each closure member receives its own read-only rule.
# Executable files are granted one by one by :func:`_tool_execute_paths`,
# excluding every real Git entrypoint. ``SYSTEM_READ`` is the narrowest
# explicit set of host files
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
SYSTEM_READ_ROOTS = (
    "/usr", "/bin", "/lib", "/lib64", "/sbin",
)
NIX_STORE_ROOT_RE = re.compile(r"^/nix/store/[0-9a-z]{32}-[^/]+$")
MAX_TOOLCHAIN_CLOSURE_PATHS = 2048
MAX_TOOLCHAIN_QUERY_BYTES = 256 * 1024
MAX_PROJECT_SHELL_INPUTS = 256
MAX_PROJECT_SHELL_OUTPUTS = 512
SYSTEM_EXECUTABLE_DIRS = (
    "/usr/bin", "/usr/sbin", "/bin", "/sbin",
)
# Bounded executable capability set required by the backend, model tools,
# build/test gates, and ordinary diagnostics.  Product binaries created under
# the workspace are covered by role workspace rules; this list controls host
# executables only and intentionally contains no Git family entry.
ALLOWED_SYSTEM_EXECUTABLE_NAMES = frozenset({
    "sh", "bash", "dash", "env", "python", "python3", "node", "pi",
    "cmake", "ctest", "ninja", "make", "meson", "nix", "nix-shell",
    "cc", "c++", "gcc", "g++", "clang", "clang++", "ld", "ar", "ranlib",
    "pkg-config", "xvfb-run", "Xvfb", "xauth", "xdotool",
    "import", "convert", "magick", "compare", "identify",
    "awk", "sed", "grep", "egrep", "fgrep", "find", "cat", "head", "tail",
    "cp", "mv", "rm", "mkdir", "rmdir", "ln", "chmod", "touch", "tee",
    "sort", "uniq", "cut", "tr", "wc", "xargs", "printf", "date", "sleep",
    "timeout", "which", "whereis", "uname", "id", "pwd", "basename",
    "dirname", "realpath", "readlink", "stat", "install", "tar", "gzip",
    "bzip2", "xz", "zstd", "patch", "diff", "cmp", "file", "ldd",
})
SYSTEM_READ = (
    "/etc/passwd", "/etc/group", "/etc/ssl/certs",
    "/dev/null", "/dev/urandom", "/dev/random", "/dev/zero", "/dev/tty",
    # Exact daemon endpoint directory required by NIX_REMOTE=daemon. No other
    # /nix/var state is visible; store data remains closure-granular above.
    "/nix/var/nix/daemon-socket",
)
NETWORK_CONFIG_LINKS = (
    "/etc/resolv.conf", "/etc/hosts", "/etc/nsswitch.conf",
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
        ".factory/loop",
        ".factory/tests",
        ".factory/artifacts/conformance.json",
        ".factory/requirement-policy.json",
        ".factory/capability-contracts.json",
        ".factory/bugs/open.md",
    }),
    "auditor": frozenset({
        ".factory/loop",
        ".factory/tests",
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
# visible ``.factory/tools/`` tree is the security/harness surface (guards,
# receipts, verifier, ralph entrypoints).  Genuine product entries
# (``src/``, ``.factory/tests/legacy/``, product data, and product documentation other than
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
    entry or lies beneath a denied directory (``.factory/tools/...``,
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
# per-launch private home/scratch/staging paths (the prompt is a sealed memfd; see
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


def _open_path_anchor(path_text: str, label: str = "allowlisted path") -> int:
    """Open ``path_text`` by a no-follow descriptor walk from ``/``.

    Every component is resolved relative to the descriptor for its verified
    parent.  The returned ``O_PATH`` descriptor therefore names the exact
    filesystem object that was validated; no later pathname reopen is needed
    to apply the Landlock rule.  Intermediate or final symlinks, ``..``
    components, missing objects, and non-directory parents fail closed.
    """
    path = Path(path_text)
    if not path.is_absolute() or ".." in path.parts:
        raise ConfinementError(
            f"{label} {path_text!r} is not a clean absolute path"
        )
    path_flags = (
        getattr(os, "O_PATH", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )
    if not getattr(os, "O_PATH", 0) or not getattr(os, "O_NOFOLLOW", 0):
        raise ConfinementUnavailable(
            "descriptor-anchored confinement requires O_PATH and O_NOFOLLOW"
        )
    try:
        current = os.open("/", path_flags | os.O_DIRECTORY)
    except OSError as exc:
        raise ConfinementError(f"cannot open filesystem root: {exc}") from exc
    try:
        parts = path.parts[1:]
        if not parts:
            return os.dup(current)
        for index, part in enumerate(parts):
            try:
                descriptor = os.open(part, path_flags, dir_fd=current)
            except OSError as exc:
                raise ConfinementError(
                    f"cannot descriptor-anchor {label} {path_text} at "
                    f"component {part!r}: {exc}"
                ) from exc
            info = os.fstat(descriptor)
            if stat.S_ISLNK(info.st_mode):
                os.close(descriptor)
                raise ConfinementError(
                    f"{label} {path_text} has a symlink component {part!r}; "
                    "descriptor anchoring never follows symlinks"
                )
            if index != len(parts) - 1 and not stat.S_ISDIR(info.st_mode):
                os.close(descriptor)
                raise ConfinementError(
                    f"{label} {path_text} has a non-directory parent "
                    f"component {part!r}"
                )
            os.close(current)
            current = descriptor
        result = current
        current = -1
        return result
    finally:
        if current >= 0:
            os.close(current)


def _descriptor_identity(descriptor: int) -> Dict[str, int]:
    """Security identity bound into one confinement rule."""
    info = os.fstat(descriptor)
    return {
        "dev": int(info.st_dev),
        "ino": int(info.st_ino),
        "type": int(stat.S_IFMT(info.st_mode)),
        "uid": int(info.st_uid),
        "nlink": int(info.st_nlink),
    }


def _path_identity(path_text: str, label: str = "allowlisted path") -> Dict[str, int]:
    descriptor = _open_path_anchor(path_text, label)
    try:
        return _descriptor_identity(descriptor)
    finally:
        os.close(descriptor)


def validate_rule_anchors(
    spec: Mapping[str, object], descriptors: Sequence[int]
) -> None:
    """Revalidate retained descriptors against all bound rule identities."""
    rules = spec.get("rules")
    if not isinstance(rules, list) or len(rules) != len(descriptors):
        raise ConfinementError(
            "the confinement rule descriptor count does not match the specification"
        )
    if len(set(descriptors)) != len(descriptors):
        raise ConfinementError("the confinement rule descriptors are not unique")
    for rule, descriptor in zip(rules, descriptors):
        if not isinstance(rule, Mapping) or not isinstance(
            rule.get("identity"), Mapping
        ):
            raise ConfinementError("a confinement rule lacks path identity")
        try:
            actual = _descriptor_identity(descriptor)
        except OSError as exc:
            raise ConfinementError(
                f"a confinement rule descriptor is closed or invalid: {exc}"
            ) from exc
        if actual != dict(rule["identity"]):
            raise ConfinementError(
                f"retained descriptor for {rule.get('path')} no longer matches "
                "its dev/ino/type/owner/nlink identity"
            )



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


def _assert_single_link_allowlisted_file(path_text: str) -> None:
    """Reject hardlinked regular files without enumerating denied state.

    The old inode scanner walked ``.ralph/`` and ``.factory-state/`` and
    silently stopped at a fixed entry cap.  That both crossed the denied
    namespace boundary and failed open.  An exact-file allowlist entry is
    now accepted only when a no-follow ``lstat`` proves a regular file has
    exactly one link.  No forbidden namespace is enumerated or read.
    """
    try:
        info = os.lstat(path_text)
    except OSError as exc:
        raise ConfinementError(
            f"cannot inspect allowlisted path {path_text}: {exc} (fail closed)"
        ) from exc
    if stat.S_ISREG(info.st_mode) and info.st_nlink != 1:
        raise ConfinementError(
            f"allowlisted regular file {path_text} has link count "
            f"{info.st_nlink}, not exactly one; hardlinked allowlist entries "
            "are never accepted (fail closed)"
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
    for entry in _workspace_read_entries(workspace):
        path = workspace / entry
        if _existing(str(path)) is not None:
            _validate_allowlist_path(str(path), workspace, "workspace read path")
            _assert_single_link_allowlisted_file(str(path))
            paths.append(path)
    for relative in sorted(
        _BASE_FACTORY_READS | ROLE_FACTORY_READS.get(role, frozenset())
    ):
        path = workspace / relative
        if _existing(str(path)) is not None:
            _validate_allowlist_path(
                str(path), workspace, "workspace .factory read path"
            )
            _assert_single_link_allowlisted_file(str(path))
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
    tooling, factory configs, harness docs, the legacy ``.factory/tools/``
    security surface) and never ``.git``, whose history/commit authority
    belongs to the trusted orchestrator (Task 8 review, finding 2; Task 9
    review HIGH).
    """
    def validated(path: Path) -> Optional[Path]:
        if _existing(str(path)) is None:
            return None
        _validate_allowlist_path(str(path), workspace, "workspace write path")
        _assert_single_link_allowlisted_file(str(path))
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


def _canonical_nix_path() -> Optional[str]:
    value = os.environ.get("NIX_PATH")
    if not value:
        return None
    match = re.fullmatch(r"nixpkgs=(/nix/store/[0-9a-z]{32}-[^:]+)", value)
    if match is None:
        raise ConfinementError("refusing a non-canonical model NIX_PATH")
    _validate_immutable_store_root(match.group(1))
    return value


def _nix_store_root(path_text: str) -> Optional[str]:
    """Return the canonical immutable store item containing ``path_text``."""
    resolved = os.path.realpath(path_text)
    if not resolved.startswith("/nix/store/"):
        return None
    parts = resolved.split("/")
    if len(parts) < 4:
        raise ConfinementError(f"malformed Nix-store tool path {resolved!r}")
    root = "/".join(parts[:4])
    if not NIX_STORE_ROOT_RE.fullmatch(root):
        raise ConfinementError(f"non-canonical Nix-store tool root {root!r}")
    return root


def _validate_immutable_store_root(path_text: str) -> str:
    if not NIX_STORE_ROOT_RE.fullmatch(path_text):
        raise ConfinementError(
            f"Nix closure query returned a path outside the store: {path_text!r}"
        )
    try:
        info = os.lstat(path_text)
    except OSError as exc:
        raise ConfinementError(
            f"Nix closure item is unavailable: {path_text}: {exc}"
        ) from exc
    if (
        not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode))
        or stat.S_ISLNK(info.st_mode)
        or info.st_uid == os.getuid() or info.st_mode & 0o022
        or os.path.realpath(path_text) != path_text
    ):
        raise ConfinementError(
            f"Nix closure item is not immutable canonical store data: {path_text}"
        )
    return path_text


def _trusted_nix_command(name: str) -> Tuple[str, str]:
    """Return the immutable PATH spelling and real executable for one Nix CLI."""
    argv0 = shutil.which(name)
    if not argv0:
        raise ConfinementUnavailable(f"{name} is unavailable for exact Nix binding")
    executable = os.path.realpath(argv0)
    try:
        gitutil.require_trusted_executable(executable)
    except gitutil.GitBoundaryError as exc:
        raise ConfinementUnavailable(
            f"{name} authority is not immutable: {exc}"
        ) from exc
    return argv0, executable


def _nix_query_environment(executable: str) -> Dict[str, str]:
    environment = {
        "HOME": "/",
        "LANG": os.environ.get("LANG", "C.UTF-8"),
        "PATH": os.path.dirname(executable),
    }
    if os.environ.get("NIX_REMOTE") == "daemon":
        environment["NIX_REMOTE"] = "daemon"
    nix_path = _canonical_nix_path()
    if nix_path is not None:
        environment["NIX_PATH"] = nix_path
    return environment


def _run_nix_query(
    argv: Sequence[str], *, argv0: str, executable: str, cwd: Path
) -> bytes:
    """Run one immutable Nix metadata query with bounded, credential-free I/O."""
    try:
        result = subprocess.run(
            [argv0, *argv], executable=executable, cwd=cwd,
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, env=_nix_query_environment(executable),
            timeout=30.0, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ConfinementUnavailable(f"exact Nix metadata query failed: {exc}") from exc
    if result.returncode != 0:
        raise ConfinementUnavailable("exact Nix metadata query returned nonzero")
    if len(result.stdout) > MAX_TOOLCHAIN_QUERY_BYTES:
        raise ConfinementError("exact Nix metadata query output is oversized")
    return result.stdout


def _project_shell_input_paths(workspace: Path) -> List[str]:
    """Resolve the exact direct input outputs of committed ``shell.nix``.

    A nested ``nix-shell`` changes PATH only after Landlock is active. Binding
    executable policy to the parent's ambient PATH therefore made display
    tools such as xdotool order-dependent: a focused test entered Nix first,
    while the complete boilerplate process did not. The trusted parent now
    evaluates the clean, commit-bound ``shell.nix`` as Nix metadata, extracts
    only its selected direct input outputs, and later grants only allowlisted
    executable *files* from their bounded immutable closure. It never grants
    ``/nix/store`` or EXECUTE on a package directory.
    """
    shell = workspace / "shell.nix"
    if not os.path.lexists(shell):
        return []
    _validate_allowlist_path(str(shell), workspace, "project shell expression")
    _assert_single_link_allowlisted_file(str(shell))
    if not stat.S_ISREG(os.lstat(shell).st_mode):
        raise ConfinementError("project shell expression is not a regular file")

    instantiate_argv0, instantiate = _trusted_nix_command("nix-instantiate")
    raw_drv = _run_nix_query(
        ["--readonly-mode", str(shell)], argv0=instantiate_argv0,
        executable=instantiate, cwd=workspace,
    )
    try:
        drv_lines = raw_drv.decode("utf-8", "strict").splitlines()
    except UnicodeDecodeError as exc:
        raise ConfinementError("project shell derivation output is not UTF-8") from exc
    if len(drv_lines) != 1:
        raise ConfinementError("project shell did not produce exactly one derivation")
    drv = drv_lines[0]
    if not NIX_STORE_ROOT_RE.fullmatch(drv) or not drv.endswith(".drv"):
        raise ConfinementError("project shell produced a non-canonical derivation")

    nix_argv0, nix = _trusted_nix_command("nix")
    metadata_raw = _run_nix_query(
        ["--extra-experimental-features", "nix-command",
         "derivation", "show", drv],
        argv0=nix_argv0, executable=nix, cwd=workspace,
    )
    try:
        metadata = json.loads(metadata_raw)
        derivations = metadata["derivations"]
        if not isinstance(derivations, dict) or len(derivations) != 1:
            raise ValueError
        shell_meta = next(iter(derivations.values()))
        input_drvs = shell_meta["inputs"]["drvs"]
    except (UnicodeError, json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        raise ConfinementError("project shell derivation metadata is malformed") from exc
    if not isinstance(input_drvs, dict) or not 0 < len(input_drvs) <= MAX_PROJECT_SHELL_INPUTS:
        raise ConfinementError("project shell input derivation count is invalid")

    store_argv0, store = _trusted_nix_command("nix-store")
    outputs: List[str] = []
    for drv_name, selection in sorted(input_drvs.items()):
        input_drv = f"/nix/store/{drv_name}"
        if not NIX_STORE_ROOT_RE.fullmatch(input_drv) or not input_drv.endswith(".drv"):
            raise ConfinementError("project shell input derivation is non-canonical")
        if not isinstance(selection, dict) or set(selection) != {"dynamicOutputs", "outputs"}:
            raise ConfinementError("project shell input selection is malformed")
        if selection["dynamicOutputs"] not in ({}, []):
            raise ConfinementError("dynamic project shell outputs are unsupported")
        names = selection["outputs"]
        if not isinstance(names, list) or not names or not all(
            isinstance(name, str) and re.fullmatch(r"[A-Za-z0-9+._?-]+", name)
            for name in names
        ):
            raise ConfinementError("project shell selected output names are invalid")
        for name in sorted(names):
            raw_output = _run_nix_query(
                ["-q", "--binding", name, input_drv], argv0=store_argv0,
                executable=store, cwd=workspace,
            )
            try:
                lines = raw_output.decode("utf-8", "strict").splitlines()
            except UnicodeDecodeError as exc:
                raise ConfinementError("project shell output path is not UTF-8") from exc
            if len(lines) != 1:
                raise ConfinementError("project shell output binding is malformed")
            outputs.append(_validate_immutable_store_root(lines[0]))
            if len(outputs) > MAX_PROJECT_SHELL_OUTPUTS:
                raise ConfinementError("project shell selected too many outputs")
    if len(set(outputs)) != len(outputs):
        raise ConfinementError("project shell selected duplicate outputs")
    return sorted(outputs)


def _toolchain_closure_paths(seed_paths: Sequence[str]) -> List[str]:
    """Resolve a bounded exact immutable Nix closure for approved runtimes.

    The trusted parent queries only store items containing already selected
    exact executable/backend paths. Caller PATH text cannot add a mutable root:
    every seed is canonicalized, every query result must be one canonical
    foreign-owned non-writable store directory, output is byte/count bounded,
    and no ``/nix/store`` ancestor rule is ever granted.
    """
    roots = sorted({
        root for path in seed_paths
        if (root := _nix_store_root(path)) is not None
    })
    if not roots:
        return []
    nix_store_argv0, nix_store = _trusted_nix_command("nix-store")
    raw = _run_nix_query(
        ["-qR", *roots], argv0=nix_store_argv0,
        executable=nix_store, cwd=Path("/"),
    )
    try:
        lines = raw.decode("utf-8", "strict").splitlines()
    except UnicodeDecodeError as exc:
        raise ConfinementError("Nix toolchain closure output is not UTF-8") from exc
    if not lines or len(lines) > MAX_TOOLCHAIN_CLOSURE_PATHS:
        raise ConfinementError("Nix toolchain closure count is empty or oversized")
    closure = sorted(set(lines))
    if len(closure) != len(lines):
        raise ConfinementError("Nix toolchain closure output contains duplicates")
    for root in roots:
        if root not in closure:
            raise ConfinementError(
                f"Nix toolchain closure omitted its seed root {root}"
            )
    return [_validate_immutable_store_root(path) for path in closure]


def _tool_read_paths(toolchain_closure: Sequence[str] = ()) -> List[str]:
    """Existing system/tool read paths (deny-by-default keeps the rest out).

    Each entry is validated with the no-symlink-component check: the narrow
    explicit enumeration (findings 3/7) is the containment contract, and a
    host where an entry resolves through a symlink fails closed (the entry
    is dropped only when it does not exist at all). Nix reads are exact
    immutable closure items, never the broad store root.
    """
    paths: List[str] = []
    for path in SYSTEM_READ + SYSTEM_READ_ROOTS:
        existing = _existing(path)
        if existing is None:
            continue
        _no_symlink_components(existing, "system read path")
        _resolved_is_self(existing, "system read path")
        paths.append(existing)
    for link in NETWORK_CONFIG_LINKS:
        resolved = os.path.realpath(link)
        try:
            info = os.stat(resolved)
        except OSError:
            continue
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid == os.getuid()
            or info.st_mode & 0o022
        ):
            raise ConfinementError(
                f"network configuration target {resolved!r} is not trusted read-only data"
            )
        _no_symlink_components(resolved, "network configuration target")
        if resolved not in paths:
            paths.append(resolved)
    for path in toolchain_closure:
        validated = _validate_immutable_store_root(path)
        if validated not in paths:
            paths.append(validated)
    return paths


def _tool_alias_seed_paths() -> List[str]:
    """Immutable PATH spellings for selected tool names (including symlinks)."""
    paths: List[str] = []
    for directory in os.environ.get("PATH", "").split(os.pathsep):
        if not directory or not os.path.isabs(directory):
            continue
        resolved_dir = os.path.realpath(directory)
        if not os.path.isdir(resolved_dir):
            continue
        try:
            names = sorted(os.listdir(resolved_dir))
        except OSError:
            continue
        for name in names:
            lowered = name.lower()
            if not (
                name in ALLOWED_SYSTEM_EXECUTABLE_NAMES
                or lowered.startswith(("python", "node"))
            ) or lowered == "git" or lowered.startswith("git-"):
                continue
            candidate = os.path.join(resolved_dir, name)
            try:
                info = os.stat(candidate)
            except OSError:
                continue
            if stat.S_ISREG(info.st_mode) and info.st_mode & 0o111:
                paths.append(candidate)
    return sorted(set(paths))


def _tool_execute_paths(toolchain_closure: Sequence[str] = ()) -> List[str]:
    """Exact executable files available to model subprocesses, never Git.

    Granting EXECUTE on ``/usr`` or ``/nix/store`` lets a generated/sourced
    script bypass the staged broker with an absolute Git pathname.  Landlock
    execution is therefore file-granular: immutable executable directories
    remain readable for libraries/data, while each executable reachable from
    the sanitized PATH is resolved to its real regular file and granted
    separately.  Git names and the pinned real Git inode are omitted, so
    direct argv, absolute argv, shell source/eval, generated scripts, and
    interpreter subprocesses all hit the same kernel execution denial.  The
    launch-owned staging directory is granted separately and contains the
    only executable named ``git``: the exact-commit shim/broker.
    """
    directories: List[str] = []
    closure_bin_dirs = [
        os.path.join(root, "bin") for root in toolchain_closure
        if NIX_STORE_ROOT_RE.fullmatch(root)
    ]
    for candidate in [*os.environ.get("PATH", "").split(os.pathsep),
                      *SYSTEM_EXECUTABLE_DIRS, *closure_bin_dirs]:
        if not candidate or not os.path.isabs(candidate):
            continue
        resolved_dir = os.path.realpath(candidate)
        if resolved_dir not in directories and os.path.isdir(resolved_dir):
            directories.append(resolved_dir)
    pinned_git = os.path.realpath(str(gitutil.GIT_EXECUTABLE))
    try:
        git_info = os.stat(pinned_git)
        git_identity = (git_info.st_dev, git_info.st_ino)
    except OSError:
        git_identity = None
    paths: List[str] = []
    for directory in sorted(directories):
        try:
            names = sorted(os.listdir(directory))
        except OSError:
            continue
        for name in names:
            lowered = name.lower()
            allowed_name = (
                name in ALLOWED_SYSTEM_EXECUTABLE_NAMES
                or lowered.startswith(("python", "node"))
            )
            if (
                not allowed_name
                or lowered == "git"
                or lowered.startswith("git-")
            ):
                continue
            candidate = os.path.join(directory, name)
            resolved = os.path.realpath(candidate)
            try:
                info = os.stat(resolved)
            except OSError:
                continue
            if not stat.S_ISREG(info.st_mode) or not info.st_mode & 0o111:
                continue
            try:
                gitutil.require_trusted_executable(resolved)
            except gitutil.GitBoundaryError:
                # Landlock EXECUTE must never cover caller-owned/mutable files;
                # the atomic exec broker binds only this immutable set to its
                # protected descriptor table.
                continue
            if git_identity is not None and (info.st_dev, info.st_ino) == git_identity:
                continue
            if resolved in paths:
                continue
            _no_symlink_components(resolved, "system executable file")
            _resolved_is_self(resolved, "system executable file")
            paths.append(resolved)
    # The executing Python may not be present in the inherited PATH of a
    # hermetic caller; it remains explicitly selected.
    python = os.path.realpath(sys.executable)
    if python != pinned_git and python not in paths:
        _no_symlink_components(python, "Python executable")
        _resolved_is_self(python, "Python executable")
        try:
            gitutil.require_trusted_executable(python)
        except gitutil.GitBoundaryError as exc:
            raise ConfinementUnavailable(
                f"executing Python is not immutable: {exc}"
            ) from exc
        paths.append(python)
    # Linux applies Landlock EXECUTE to the PT_INTERP loader while starting a
    # normal dynamic ELF, so the exact loader inode must be present in this
    # low-level Landlock set. It is *not* an approved userspace exec target:
    # the staged confine launcher omits loaders from its protected descriptor
    # table and rewrites approved execve requests to inode-bound execveat.
    # Thus ``ld-linux <readable-elf>`` is denied without breaking an approved
    # dynamic ELF, including under a shared pathname-buffer race.
    try:
        with open("/proc/self/maps", "r", encoding="utf-8") as stream:
            for line in stream:
                mapped = line.rsplit(" ", 1)[-1].strip()
                if ("ld-linux" in mapped or "ld-musl" in mapped) and os.path.isfile(mapped):
                    resolved_loader = os.path.realpath(mapped)
                    try:
                        gitutil.require_trusted_executable(resolved_loader)
                    except gitutil.GitBoundaryError as exc:
                        raise ConfinementUnavailable(
                            f"executing ELF interpreter is not immutable: {exc}"
                        ) from exc
                    if resolved_loader not in paths:
                        paths.append(resolved_loader)
    except OSError as exc:
        raise ConfinementUnavailable(
            f"cannot bind the executing ELF interpreter: {exc}"
        ) from exc
    # Nix symlinks are resolved above. Grant only each selected regular-file
    # inode, never its package root: a package directory can contain multicall
    # or helper entrypoints outside the approved name set.
    return sorted(set(paths))


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
        try:
            gitutil.require_trusted_executable(resolved)
        except gitutil.GitBoundaryError as exc:
            raise ConfinementError(
                f"external backend is not an immutable trusted executable: {exc}"
            ) from exc
        # Exact file only. Granting EXECUTE on its parent/install root would
        # reopen absolute host executables (including Git) behind the broker.
        return [resolved], [resolved]
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
    environment = {
        "HOME": str(home),
        "XDG_CONFIG_HOME": str(home / ".config"),
        "XDG_CACHE_HOME": str(home / ".cache"),
        "XDG_DATA_HOME": str(home / ".local" / "share"),
        "XDG_STATE_HOME": str(home / ".local" / "state"),
        "XDG_RUNTIME_DIR": str(home / "run"),
        "TMPDIR": str(home / "run"),
        "NIX_REMOTE": "daemon",
    }
    nix_path = _canonical_nix_path()
    if nix_path is not None:
        environment["NIX_PATH"] = nix_path
    return environment


def confinement_spec(
    binding: object,
    *,
    sanitized_home: Path,
    extra_read: Sequence[str] = (),
    extra_write: Sequence[str] = (),
    _rule_descriptors: Optional[List[int]] = None,
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
    if _rule_descriptors is not None and _rule_descriptors:
        raise ConfinementError("the retained rule descriptor list must start empty")

    def add_rule(path_text: str, access: Sequence[str]) -> None:
        path = Path(path_text).absolute()
        path_value = str(path)
        descriptor = _open_path_anchor(path_value, "confinement rule")
        rule = {
            "path": path_value,
            "access": sorted(set(access)),
            "identity": _descriptor_identity(descriptor),
        }
        rules.append(rule)
        if _rule_descriptors is None:
            os.close(descriptor)
        else:
            # Keep the descriptor from the exact validation operation.  It is
            # never closed/reopened by pathname before Landlock consumes it.
            _rule_descriptors.append(descriptor)

    for path in _role_read_paths(role, workspace):
        add_rule(str(path), (ACCESS_READ,))
    for path in _role_write_paths(role, workspace):
        # Model-writable workspace paths are never executable. Scripts remain
        # usable as data through an exact approved interpreter.
        add_rule(str(path), (ACCESS_READ, ACCESS_WRITE))
    ambient_tool_execute = _tool_execute_paths()
    backend_read, backend_execute = _backend_paths(
        Path(binding.backend), workspace
    )
    # Include immutable PATH package roots and the exact selected inputs of the
    # commit-bound project shell. Nix exposes multicall tools such as nix-shell
    # through immutable store symlinks; the broker validates those aliases and
    # resolves them only to already approved exact inodes.
    nix_path = _canonical_nix_path()
    nixpkgs_source = nix_path.split("=", 1)[1] if nix_path is not None else ""
    project_shell_inputs = _project_shell_input_paths(workspace)
    toolchain_closure = _toolchain_closure_paths([
        *ambient_tool_execute, *backend_read, *backend_execute,
        *_tool_alias_seed_paths(), *project_shell_inputs,
        *([nixpkgs_source] if nixpkgs_source else []),
    ])
    tool_execute = _tool_execute_paths(toolchain_closure)
    for path in _tool_read_paths(toolchain_closure):
        add_rule(path, (ACCESS_READ,))
    for path in tool_execute:
        add_rule(path, (ACCESS_READ, ACCESS_EXECUTE))
    for path in backend_read:
        add_rule(path, (ACCESS_READ,))
    for path in backend_execute:
        add_rule(path, (ACCESS_READ, ACCESS_EXECUTE))
    for path in extra_read:
        # Task 10 review (REQ 4): every extra allowlist entry is validated
        # exactly like the role allowlists (no symlink in any component,
        # resolved containment), so an extra grant can never smuggle a
        # symlink or an escaping alias.
        _validate_allowlist_path(path, workspace, "extra read path")
        _assert_single_link_allowlisted_file(path)
        add_rule(path, (ACCESS_READ,))
    for path in extra_write:
        # The exact transient phase/audit result file of the confined
        # tester/auditor (REQ 4): an extra write grant is an *exact-file*
        # rule on one existing regular file inside the repository.  The
        # trusted orchestrator pre-creates the mode-0600 result file before
        # the launch — Landlock cannot grant the creation of a
        # not-yet-existing file through an exact-file rule — so a missing
        # extra write path fails closed at specification build.  The
        # no-follow component walk rejects any symlink component; a path
        # outside the workspace (a foreign transient channel) and a
        # non-regular file (a directory grant would cover every sibling
        # under ``.factory-state/``) fail closed too.
        _validate_allowlist_path(path, workspace, "extra write path")
        extra = Path(path).absolute()
        if not extra.is_relative_to(workspace):
            raise ConfinementError(
                f"extra write path {path} is outside the model workspace "
                f"{workspace}; the transient result channel stays inside "
                "the repository (fail closed)"
            )
        try:
            info = os.lstat(str(extra))
        except OSError:
            raise ConfinementError(
                f"extra write path {path} does not exist; the trusted "
                "orchestrator must pre-create the exact result file before "
                "the launch (Landlock cannot grant the creation of a "
                "not-yet-existing file through an exact-file rule, fail "
                "closed)"
            ) from None
        if not stat.S_ISREG(info.st_mode):
            raise ConfinementError(
                f"extra write path {path} is not a regular file; an "
                "exact-file write grant must name one transient result "
                "file, never a directory or special file (fail closed)"
            )
        _assert_single_link_allowlisted_file(path)
        add_rule(path, (ACCESS_READ, ACCESS_WRITE))

    # The exact per-launch private home (Task 8 review, finding 4): the
    # model receives no broad ``/tmp`` grant — only this launch's own
    # mode-0700 private home (with its scratch subdirectories) is granted
    # read+write, never execute, so a sibling launch's private directories stay
    # denied by default.  The launch authority additionally adds this
    # launch's exact staging/session paths through
    # :func:`with_private_launch_paths` before the proof is minted.  The
    # home path is validated like every other allowlist entry (no symlink
    # component; the fresh private directory resolves to itself).
    _no_symlink_components(str(Path(sanitized_home).absolute()), "sanitized home")
    _resolved_is_self(str(Path(sanitized_home).absolute()), "sanitized home")
    add_rule(
        str(Path(sanitized_home).absolute()),
        (ACCESS_READ, ACCESS_WRITE),
    )

    # The sanitized home is inside the shared temporary directory, which is
    # *not* granted; only the exact home path above is writable.  Nothing
    # else is needed for it.
    if _rule_descriptors is None:
        rules.sort(key=lambda rule: str(rule["path"]))
    else:
        pairs = sorted(
            zip(rules, _rule_descriptors), key=lambda pair: str(pair[0]["path"])
        )
        rules[:] = [pair[0] for pair in pairs]
        _rule_descriptors[:] = [pair[1] for pair in pairs]
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
    session_dir: Path,
    _rule_descriptors: Optional[List[int]] = None,
) -> List[Dict[str, object]]:
    """The exact per-launch private-path rules (Task 8 review, finding 4).

    The model receives no broad ``/tmp`` grant: only this launch's own
    private paths are granted, each with the *least* rights it needs —

    * ``staging_dir`` — read only, never execute; committed scripts and
      modules remain data consumed by an approved immutable interpreter;
    * ``session_dir`` — read+write, never execute.

    The composed prompt has no pathname rule: production carries it only in
    an inherited sealed memfd, from composition through the secure wrapper.

    Every path is validated like every other allowlist entry (finding 1):
    a symlink in any component fails closed and the resolved target must
    be the path itself, so an alias or symlink can never smuggle a broader
    grant.  A sibling launch's private directories are never granted and
    stay denied by default.
    """
    rules: List[Dict[str, object]] = []

    def append_rule(path: Path, access: Sequence[str], what: str) -> None:
        path_text = str(path.absolute())
        _no_symlink_components(path_text, what)
        _resolved_is_self(path_text, what)
        descriptor = _open_path_anchor(path_text, what)
        rules.append({
            "path": path_text,
            "access": sorted(set(access)),
            "identity": _descriptor_identity(descriptor),
        })
        if _rule_descriptors is None:
            os.close(descriptor)
        else:
            _rule_descriptors.append(descriptor)

    staging = Path(staging_dir).absolute()
    session = Path(session_dir).absolute()
    append_rule(staging, (ACCESS_READ,), "private staging directory")
    append_rule(session, (ACCESS_READ, ACCESS_WRITE), "private session directory")
    # No staging entry receives Landlock EXECUTE. A private caller-owned path
    # is absent from the broker's immutable protected descriptor table;
    # Python/shell scripts are passed as readable arguments to approved
    # immutable interpreters instead of crossing execve by pathname.
    return rules


def with_private_launch_paths(
    spec: Mapping[str, object],
    *,
    staging_dir: Path,
    session_dir: Path,
    _rule_descriptors: Optional[List[int]] = None,
) -> Dict[str, object]:
    """A copy of ``spec`` augmented with the exact per-launch private rules.

    The launch authority (``launch.authorize_launch``) adds these rules
    *before* the real confinement proof is minted, so the proof's
    specification digest binds the exact allowlists the confined child
    applies — including this launch's own staging/session paths and never the
    broad shared temporary directory. The prompt is an inherited sealed memfd.
    """
    augmented = json.loads(json.dumps(spec))
    if not isinstance(augmented, dict) or not isinstance(
        augmented.get("rules"), list
    ):
        raise ConfinementError(
            "cannot augment a confinement specification without a rules list"
        )
    existing_rules = list(augmented["rules"])
    if _rule_descriptors is not None and len(_rule_descriptors) != len(existing_rules):
        raise ConfinementError(
            "retained base-rule descriptor count does not match the specification"
        )
    private_descriptors: Optional[List[int]] = (
        [] if _rule_descriptors is not None else None
    )
    private_rules = private_launch_rules(
        staging_dir=staging_dir,
        session_dir=session_dir,
        _rule_descriptors=private_descriptors,
    )
    if _rule_descriptors is None:
        augmented["rules"].extend(private_rules)
        augmented["rules"].sort(key=lambda rule: str(rule["path"]))
    else:
        assert private_descriptors is not None
        pairs = list(zip(existing_rules, _rule_descriptors))
        pairs.extend(zip(private_rules, private_descriptors))
        pairs.sort(key=lambda pair: str(pair[0]["path"]))
        augmented["rules"] = [pair[0] for pair in pairs]
        _rule_descriptors[:] = [pair[1] for pair in pairs]
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
        identity = rule.get("identity")
        if set(rule) != {"path", "access", "identity"}:
            raise ConfinementError(
                "a confinement rule must carry exactly path/access/identity"
            )
        if not isinstance(path, str) or not path:
            raise ConfinementError("a confinement rule must carry a path")
        if not isinstance(access, list) or not access:
            raise ConfinementError(f"rule {path} must carry an access list")
        expected_identity_keys = {"dev", "ino", "type", "uid", "nlink"}
        if (
            not isinstance(identity, Mapping)
            or set(identity) != expected_identity_keys
            or not all(type(identity[key]) is int and identity[key] >= 0
                       for key in expected_identity_keys)
        ):
            raise ConfinementError(
                f"rule {path} has an invalid dev/ino/type/owner/nlink identity"
            )
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
_PR_SET_NO_NEW_PRIVS = 38
_PR_GET_NO_NEW_PRIVS = 39

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
    if abi < 3:
        raise ConfinementUnavailable(
            "Landlock ABI 3 or newer is required so truncate is mediated; "
            f"host reported ABI {abi}"
        )
    pid = os.fork()
    if pid == 0:
        # Child: prove the complete production sequence — set+verify
        # no_new_privs, create a ruleset, add a real descriptor-anchored rule,
        # restrict_self, and close every descriptor.  Never return to the
        # parent path; _exit with the probe outcome.
        try:
            _probe_apply_ruleset(_handled_access_bits(abi))
            os._exit(0)
        except Exception:
            os._exit(1)
    _, status = os.waitpid(pid, 0)
    if not os.WIFEXITED(status) or os.WEXITSTATUS(status) != 0:
        raise ConfinementUnavailable(
            "the Landlock ruleset probe failed in a fresh child; model "
            "workspace confinement cannot be applied (fail closed)"
        )


def _set_no_new_privs() -> None:
    """Set and verify PR_SET_NO_NEW_PRIVS for unprivileged Landlock."""
    import ctypes
    import errno

    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(_PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) != 0:
        raise ConfinementUnavailable(
            "PR_SET_NO_NEW_PRIVS failed: "
            + errno.errorcode.get(ctypes.get_errno(), str(ctypes.get_errno()))
        )
    if libc.prctl(_PR_GET_NO_NEW_PRIVS, 0, 0, 0, 0) != 1:
        raise ConfinementUnavailable(
            "PR_SET_NO_NEW_PRIVS could not be verified"
        )


def _probe_apply_ruleset(handled_bits: int) -> None:
    """Apply a real rule and prove that an ungranted truncate is denied."""
    import ctypes
    import errno
    import tempfile

    class PathBeneath(ctypes.Structure):
        _fields_ = [("allowed_access", ctypes.c_uint64), ("parent_fd", ctypes.c_int)]

    ruleset = -1
    anchor = -1
    probe_fd, probe_name = tempfile.mkstemp(prefix="factory-landlock-truncate-")
    os.write(probe_fd, b"must-not-truncate")
    os.unlink(probe_name)
    probe_descriptor_path = f"/proc/self/fd/{probe_fd}"
    try:
        _set_no_new_privs()
        ruleset = _create_ruleset(handled_bits)
        anchor = os.open(
            "/", getattr(os, "O_PATH", os.O_RDONLY)
            | os.O_DIRECTORY | getattr(os, "O_CLOEXEC", 0)
        )
        allowed = (_ACCESS_READ_FILE | _ACCESS_READ_DIR) & handled_bits
        beneath = PathBeneath(allowed, anchor)
        libc = ctypes.CDLL(None, use_errno=True)
        if libc.syscall(
            _LANDLOCK_ADD_RULE, ruleset, _LANDLOCK_RULE_PATH_BENEATH,
            ctypes.byref(beneath), 0,
        ) != 0:
            raise ConfinementUnavailable(
                "landlock_add_rule probe failed: "
                + errno.errorcode.get(ctypes.get_errno(), str(ctypes.get_errno()))
            )
        if libc.syscall(_LANDLOCK_RESTRICT_SELF, ruleset, 0) != 0:
            raise ConfinementUnavailable(
                "landlock_restrict_self probe failed: "
                + errno.errorcode.get(ctypes.get_errno(), str(ctypes.get_errno()))
            )
        try:
            denied = os.open(probe_descriptor_path, os.O_WRONLY | os.O_TRUNC)
        except PermissionError:
            denied = -1
        else:
            os.close(denied)
            raise ConfinementUnavailable(
                "Landlock truncate negative probe unexpectedly succeeded"
            )
    finally:
        os.close(probe_fd)
        if anchor >= 0:
            os.close(anchor)
        if ruleset >= 0:
            os.close(ruleset)


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
_GUARD_GIT_TIMEOUT = 30.0
_GUARD_SOURCE_PREFIX = ".factory/loop"


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

    Only :func:`prove_confinement` mints this token; the installed module has
    no synthetic-proof mint or caller-supplied proof transport.
    """

    __slots__ = (
        "_bound_commit",
        "_workspace",
        "_provider",
        "_guard_source_digests",
        "_credential_channels",
        "_confinement_spec_digest",
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
    """SHA-256 of the loaded-tree ``usage.py`` / ``usage_fetch.py`` files."""
    return tuple(_module_bytes(module) for module in GUARD_SOURCE_MODULES)  # type: ignore[return-value]


def _committed_guard_source_digests(binding: object) -> Tuple[str, str]:
    """Read both guard modules from ``binding.bound_commit`` via trusted Git.

    A digest of the current worktree proves only self-consistency.  The proof
    authority instead binds the exact committed blobs using the pinned,
    sanitized, bounded Git authority; missing/oversized blobs fail closed.
    """
    try:
        workspace = Path(binding.workspace).absolute()
        commit = binding.bound_commit
    except AttributeError as exc:
        raise ConfinementError(
            f"cannot bind committed guard source to the invocation: {exc}"
        ) from exc
    if not isinstance(commit, str) or len(commit) != 40 or any(
        char not in "0123456789abcdef" for char in commit
    ):
        raise ConfinementError(
            "committed usage-guard binding requires a strict 40-hex commit"
        )
    digests: List[str] = []
    for module in GUARD_SOURCE_MODULES:
        relpath = f"{_GUARD_SOURCE_PREFIX}/{module}"
        try:
            result = gitutil.git_bytes_bounded(
                ["-C", str(workspace), "show", f"{commit}:{relpath}"],
                maximum=_MAX_GUARD_SOURCE_BYTES,
                timeout=_GUARD_GIT_TIMEOUT,
            )
        except gitutil.GitBoundaryError as exc:
            raise ConfinementError(
                f"cannot read committed usage-guard blob {relpath}: {exc}"
            ) from exc
        if result.returncode != 0:
            raise ConfinementError(
                f"usage-guard source {relpath} is not a blob at bound commit "
                f"{commit}; proof minting fails closed"
            )
        digests.append(hashlib.sha256(result.stdout).hexdigest())
    return tuple(digests)  # type: ignore[return-value]


def _bound_guard_source_digests(
    binding: object, executing: Optional[Sequence[str]] = None
) -> Tuple[str, str]:
    """Require executing/staged guard bytes to equal the committed blobs."""
    committed = _committed_guard_source_digests(binding)
    actual = tuple(executing) if executing is not None else _executing_guard_source_digests()
    if actual != committed:
        raise ConfinementError(
            "the usage guard being imported/executed is not the exact "
            "usage.py / usage_fetch.py blob pair at the bound commit"
        )
    return committed


def _derive_credential_channels(
    *,
    cookie_file: Optional[str],
    cookie_stdin: bool,
    env_store: Optional[str],
    guard_module: object = _USAGE_PATHS,
) -> Tuple[CredentialChannel, ...]:
    """The exact credential channels this guard invocation will consume.

    The default operator env store is always a consumed channel (the guard
    falls back to it); an explicitly specified cookie file and stdin
    provenance are additional channels when requested.
    """
    channels: List[CredentialChannel] = []
    store = env_store or guard_module._default_env_file()
    channels.append(CredentialChannel("env_store", store))
    if cookie_file:
        channels.append(CredentialChannel("cookie_file", cookie_file))
    if cookie_stdin:
        channels.append(CredentialChannel("stdin", None))
    return normalize_channels(channels)


def _assert_channel_outside_workspace(
    channel: CredentialChannel, workspace: Path,
    guard_module: object = _USAGE_PATHS,
) -> None:
    """Fail closed when a file channel is scoped inside the model workspace."""
    if channel.kind == "stdin":
        return
    assert isinstance(channel.path, str)
    _assert_store_outside(channel.path, workspace, guard_module)


def prove_confinement(
    binding: object,
    *,
    cookie_file: Optional[str] = None,
    cookie_stdin: bool = False,
    env_store: Optional[str] = None,
    confinement_spec: Optional[Mapping[str, object]] = None,
    _executing_guard_digests: Optional[Sequence[str]] = None,
    _usage_guard_module: Optional[object] = None,
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

    This installed authority has no synthetic proof path.
    """
    require_confinement_primitive()
    if confinement_spec is None:
        raise ConfinementError(
            "a real confinement proof requires the exact confinement "
            "specification that will be applied to the model child"
        )
    validate_confinement_spec(confinement_spec, binding)
    workspace = Path(binding.workspace).absolute()
    guard_module = _usage_guard_module or _USAGE_PATHS
    channels = _derive_credential_channels(
        cookie_file=cookie_file, cookie_stdin=cookie_stdin,
        env_store=env_store, guard_module=guard_module,
    )
    for channel in channels:
        _assert_channel_outside_workspace(channel, workspace, guard_module)
        if _channel_covered_by_spec(channel, confinement_spec):
            raise ConfinementError(
                f"the {channel.kind} credential channel "
                f"{channel.path!r} is granted by the confinement allowlist "
                "and cannot be proven inaccessible to model tools (fail "
                "closed)"
            )
    committed_guard_digests = _bound_guard_source_digests(
        binding, _executing_guard_digests
    )
    return ConfinementProof(
        bound_commit=binding.bound_commit,
        workspace=str(workspace),
        provider=binding.provider.lower(),
        guard_source_digests=committed_guard_digests,
        credential_channels=channels,
        confinement_spec_digest=spec_digest(confinement_spec),
        _mint=_PROOF_MINT_SECRET,
    )


def _assert_store_outside(
    store: str, workspace: object, guard_module: object = _USAGE_PATHS
) -> None:
    try:
        guard_module.assert_store_outside_workspace(store, workspace)
    except Exception as exc:
        # The exact committed usage module owns the configuration error type;
        # never depend on the mutable worktree module's class identity.
        if exc.__class__.__name__ != "UsageConfigError":
            raise
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
    _executing_guard_digests: Optional[Sequence[str]] = None,
    _usage_guard_module: Optional[object] = None,
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
        executing = _bound_guard_source_digests(
            binding, _executing_guard_digests
        )
        if proof.guard_source_digests != executing:
            raise ConfinementError(
                "the confinement proof does not bind the exact committed "
                "guard source (usage.py / usage_fetch.py digests differ); "
                "an operator-claimed or caller-controlled guard source is "
                "never accepted"
            )
        workspace = Path(binding.workspace).absolute()
        guard_module = _usage_guard_module or _USAGE_PATHS
        for channel in proof.credential_channels:
            _assert_channel_outside_workspace(channel, workspace, guard_module)
        if not proof.confinement_spec_digest:
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
                guard_module=guard_module,
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
