#!/usr/bin/env python3
"""Hidden harness-footprint inventory authority (HIDE-01, §3; Task 13).

FACTORY-LOOP-SPEC §3 requires the harness to stay visually and operationally
out of the adopting product's way:

* committed harness configuration, prompts, implementation, and harness-only
  tests live under the hidden ``.factory/`` namespace;
* runtime state, locks, temporary files, logs, and receipts live under the
  ignored ``.factory-state/`` namespace, created mode 0700;
* Pi role definitions MAY live under the hidden ``.pi/`` namespace;
* the product repository root receives no new visible harness files; legacy
  root harness files (``PROMPT.md``, ``IMPLEMENTATION_PLAN.md``,
  ``factory.toml``, ...) must never reappear;
* product build systems and packaging discovery MUST exclude ``.factory/``,
  ``.factory-state/``, and ``.pi/``;
* generated projects MUST be able to remove the harness by deleting the
  hidden factory namespaces without deleting product source or product
  tests.

This module is the deterministic inventory authority behind those rules.  It
enumerates every harness-installed or harness-generated path (tracked
content, present-on-disk content, and external-prefix installs) and fails
closed on every documented escape class:

* **absolute / traversal / control paths**: a harness path must be a safe
  repository-relative path — no leading ``/``, no ``..``/``.``/empty
  segments, no backslashes, no NUL or control characters;
* **namespace containment**: every harness-owned path's first segment must be
  byte-exact ``.factory``, ``.factory-state``, or ``.pi``; ``.ralph/`` is
  legacy recovery history only and is never created or extended by the new
  path;
* **case-fold and Unicode escapes**: a path whose first segment is only a
  case-folded, NFC/NFD/NFKC-normalized, or trailing-dot/space variant of a
  hidden namespace (for example ``.Factory/``, ``.factory.``, or
  ``.factory`` with a trailing space) is rejected — only the exact ASCII
  names qualify;
* **symlink escapes**: no symlink may exist inside the hidden namespaces
  (a symlink cannot redirect a harness path out of its namespace), and no
  product-tree entry may be a symlink whose resolved target lands inside a
  hidden namespace;
* **hardlink aliasing**: a harness-owned inode may never be shared with a
  product-path file (``st_nlink > 1`` across the namespaces fails);
* **special inodes and mount crossings**: the on-disk walk rejects FIFOs,
  sockets, and device nodes beneath the hidden namespaces or the legacy
  ``.ralph/`` namespace, and rejects any entry whose ``st_dev`` differs from
  the repository root's (a namespace crossing a mount point);
  ``remove_harness()`` refuses to delete a namespace whose root or any
  descendant crosses a filesystem boundary;
* **exact runtime permissions**: ``.factory-state/`` must exist with mode
  exactly 0700 and be owned by the invoking user — the ownership check is
  never skipped under root (root fails closed earlier through the
  pinned-Git resolver);
* **complete external installs**: an external-prefix harness copy must
  carry exactly its operator manifest — no omitted manifest file and no
  extra product file or symlink — plus only explicitly listed trusted
  executable entrypoints, under a real (never symlinked) prefix outside the
  *resolved* product tree;
* **ignored-artifact discovery**: on-disk product discovery is derived from
  the pinned ``git ls-files -co --exclude-standard -z`` set, so git-ignored
  credentials, locks, caches, logs, and build artifacts can never be fed
  into a packaging glob;
* **case-fold/NFKC escapes everywhere**: a case-fold or NFC/NFD/NFKC
  variant of a hidden namespace is rejected in *every* product-tree
  segment, tracked and on disk, not only in the first segment.
* **legacy root harness files**: the product root must never again receive
  ``PROMPT.md``, ``IMPLEMENTATION_PLAN.md``, ``factory.toml``, or the other
  forbidden root factory files, tracked or on disk;
* **packaging/build contamination**: harness entries never appear inside the
  product source/test/packaging/build trees, and a product install prefix
  never receives hidden-namespace content, harness marker basenames,
  symlinks, special inodes, or mount crossings — checked recursively over
  the whole staged tree;
* **tracked runtime state**: ``.factory-state/`` is ignored runtime state and
  may never be tracked by Git (a present ``.factory-state/`` that is not
  git-ignored also fails);
* **external executable prefix**: the harness's pinned external executable
  (the Git boundary binary) must be absolute and resolve under exactly the
  supported immutable executable roots (``/usr/bin``, ``/bin``, ``/sbin``,
  ``/run/current-system``, or the Nix store) — never ``/etc``, ``/lib``,
  a caller-controlled path, or inside the product tree; an
  operator-installed harness copy outside the product tree must carry
  exactly the hidden-namespace manifest, never product files.

The inventory is deliberately byte-exact and deterministic: it uses only the
Python standard library plus the pinned Git boundary, performs no network
access, never writes to the repository, and is safe to run from any clean
tree.  The packaging gate is the harness-owned driver
``.factory/tests/test-factory-footprint.sh`` (invoked from
``scripts/verify-boilerplate.sh``); the suite
``.factory/tests/test-factory-footprint.py`` proves every escape class with
adversarial fixture repositories.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import stat
import sys
import unicodedata
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple

try:  # package-import mode (the hidden control-plane package)
    from . import gitutil
except ImportError:  # flat-import mode used by the hidden `.factory/tests/` suite
    import gitutil  # type: ignore[no-redef]


class FootprintError(RuntimeError):
    """A harness-owned path escapes the allowed hidden namespaces (HIDE-01)."""


# The only hidden namespaces the harness may install or generate.  The names
# are byte-exact ASCII: a case-fold or Unicode-normalized variant of a name
# is never the namespace (a case-insensitive or normalization-blessed
# filesystem could otherwise alias the namespace with a product path).
HIDDEN_NAMESPACES: Tuple[str, ...] = (".factory", ".factory-state", ".pi")
HIDDEN_NAMESPACE_SET: frozenset = frozenset(HIDDEN_NAMESPACES)

# Legacy Ralph recovery history (MIG-01).  The new path never creates or
# modifies entries under ``.ralph/``; the tracked legacy file(s) that may
# exist during migration are listed exactly.
LEGACY_NAMESPACES: Tuple[str, ...] = (".ralph",)
LEGACY_TRACKED_ALLOWLIST: frozenset = frozenset({
    ".ralph/agent/scratchpad.md",
})

# Root harness files that must never appear in the product root — tracked or
# on disk.  This is the same set the verifier's forbidden-root-file check
# enforces, kept authoritative here so a single inventory catches both.
FORBIDDEN_ROOT_FACTORY_FILES: frozenset = frozenset({
    "PROMPT.md",
    "IMPLEMENTATION_PLAN.md",
    "MAINTENANCE_PLAN.md",
    "CAMPAIGN_AUDIT.md",
    "factory.toml",
    "factory-environment.toml",
    "open-bugs.md",
    "closed-bugs.md",
    "ralph.yml",
    "ralph.plan.yml",
    "ralph.audit.yml",
    "ralph.maintenance.yml",
    "ralph.maintenance-plan.yml",
})

# Product source/test/packaging/build tree names.  Harness files must never
# be discovered inside them, and a hidden-namespace entry or a legacy root
# harness file inside one of these trees is packaging/build contamination.
PRODUCT_TREE_NAMES: frozenset = frozenset({
    "src", "tests", "packaging", "data", "cmake", "build",
})

# Trusted external executable prefixes.  A harness-pinned external
# executable (``gitutil.GIT_EXECUTABLE``, the trusted interpreter) must be
# absolute and resolve under one of these roots — never under a
# caller-controlled path and never inside the product tree.  The immutable-
# chain authority (``gitutil.require_trusted_executable``) additionally
# proves the resolved path itself cannot be replaced by the caller.
#
# The list is exactly the supported immutable *executable* roots, mirroring
# the fixed candidates of ``gitutil.FIXED_GIT_CANDIDATES``: the FHS
# executable directories (``/usr/bin``, ``/bin``, ``/sbin``),
# ``/run/current-system`` (the NixOS system profile), and the immutable
# Nix store.  ``/lib`` and ``/lib64`` are shared-library directories, not
# executable roots, and ``/etc`` is configuration; no supported candidate
# ever lives under them, so admitting them would widen the trust boundary
# without covering any pinned executable.
TRUSTED_EXTERNAL_PREFIXES: Tuple[str, ...] = (
    "/usr/bin", "/bin", "/sbin", "/run/current-system", "/nix/store",
)

# Basenames that unambiguously identify a harness config artifact regardless
# of which product tree it leaked into (packaging contamination detection).
_HARNESS_FILENAME_MARKERS: frozenset = frozenset({
    "factory.toml",
    "factory-environment.toml",
    "factory-loop.json",
    "factory-loop.jsonl",
    "factory-state.json",
    "factory-plan-v1.schema.md",
    "factory-plan-v1.schema.json",
})

# Every root-level harness filename the product root must never carry — the
# legacy root factory files plus the harness config-artifact basenames.
# Used by the root-level tracked/on-disk checks and by both discovery
# channels so a marker can never reach a packaging glob.
_ROOT_HARNESS_FILENAMES: frozenset = (
    FORBIDDEN_ROOT_FACTORY_FILES | _HARNESS_FILENAME_MARKERS
)

# ---------------------------------------------------------------------------
# Path safety and namespace classification
# ---------------------------------------------------------------------------


def safe_relpath(path: str) -> Optional[str]:
    """``None`` when ``path`` is an unsafe repository-relative harness path.

    A safe path is non-empty, not absolute, free of empty/``.``/``..``
    segments, and free of backslashes and NUL/control characters.  Any
    unsafe path is a fail-closed harness escape (the same contract the
    campaign result channel enforces).
    """
    if not path or path.startswith("/") or path.startswith("\\"):
        return None
    if path.endswith("/"):
        path = path[:-1]
    if not path or any(segment in ("", ".", "..") for segment in path.split("/")):
        return None
    if "\\" in path or any(ord(char) < 0x20 for char in path):
        return None
    return path


def _namespace_escape(name: str) -> Optional[str]:
    """The hidden namespace a *variant* of ``name`` aliases, or ``None``.

    Returns the canonical namespace name when ``name`` is not byte-exact but
    compares equal under case folding, under NFC/NFD/NFKC Unicode
    normalization, or after trailing-dot/space stripping.  A byte-exact
    name never matches (it is the legitimate namespace itself).  This
    catches case-insensitive-filesystem aliasing (``.Factory/``),
    normalization-blessed aliasing (for example a fullwidth ``ｆ`` that
    NFKC-normalizes to an ASCII letter), and Windows/macOS trailing
    dot/space trimming (``.factory.`` and ``.factory `` are created as
    ``.factory`` by those filesystems), in any combination (``.FACTORY.``).
    """
    for namespace in HIDDEN_NAMESPACES:
        if name == namespace:
            continue
        # Windows and macOS silently strip trailing dots and spaces when
        # creating or reading directory entries, so ``.factory.`` and
        # ``.factory `` alias ``.factory`` there; compare the stripped and
        # unstripped forms under every folding/normalization variant so a
        # combined alias (``.FACTORY. ``) still fails closed.
        for candidate in (name, name.rstrip(". ")):
            if candidate.casefold() == namespace.casefold():
                return namespace
            for form in ("NFC", "NFD", "NFKC"):
                try:
                    if unicodedata.normalize(form, candidate) == namespace:
                        return namespace
                except TypeError:  # never reached (defensive)
                    continue
    return None


def classify_path(relpath: str) -> str:
    """``'harness'`` | ``'legacy'`` | ``'product'`` for a safe relpath.

    A harness-owned path is one whose first segment is byte-exact one of the
    hidden namespaces; ``.ralph/`` is legacy recovery history; everything
    else is product-owned.  An unsafe path raises ``FootprintError``.
    """
    first = relpath.split("/", 1)[0]
    if first in HIDDEN_NAMESPACE_SET:
        return "harness"
    if first in LEGACY_NAMESPACES:
        return "legacy"
    return "product"


# ---------------------------------------------------------------------------
# Tracked-content inventory
# ---------------------------------------------------------------------------


def tracked_files(root: Path) -> List[str]:
    """Every tracked repository-relative path at ``root`` (pinned Git)."""
    result = gitutil.git_run(["-C", str(root), "ls-files", "-z"], timeout=60)
    if result.returncode != 0:
        raise FootprintError(
            f"cannot enumerate tracked files at {root}: "
            f"{result.stderr.strip() or result.stdout.strip()}"
        )
    return [path for path in result.stdout.split("\0") if path]


def tracked_modes(root: Path) -> Dict[str, str]:
    """``{relpath: mode}`` for every tracked path (git ls-files -s -z).

    Each NUL-terminated record has the form
    ``<mode> <object> <stage><TAB><path>``; with ``-z`` the path is emitted
    raw (never C-quoted), so splitting on the *first* TAB is safe even for
    paths that themselves contain spaces or tabs.  Whitespace-splitting a
    newline-formatted ``-s`` listing would corrupt any path containing a
    space, which is why the NUL form is mandatory here.
    """
    try:
        result = gitutil.git_run(
            ["-C", str(root), "ls-files", "-s", "-z"], timeout=60
        )
    except gitutil.GitBoundaryError as exc:
        raise FootprintError(f"cannot read tracked modes: {exc}") from exc
    if result.returncode != 0:
        raise FootprintError(f"cannot read tracked modes: {result.stderr.strip()}")
    modes: Dict[str, str] = {}
    for record in result.stdout.split("\0"):
        if not record:
            continue
        meta, sep, relpath = record.partition("\t")
        if not sep or not relpath:
            raise FootprintError(
                f"cannot parse git ls-files -s record: {record!r}"
            )
        modes[relpath] = meta.split()[0]
    return modes


def _check_tracked(root: Path) -> List[str]:
    """Fail-closed checks over the tracked repository content."""
    errors: List[str] = []
    modes = tracked_modes(root)
    for relpath in sorted(tracked_files(root)):
        safe = safe_relpath(relpath)
        if safe is None:
            errors.append(
                f"tracked path is absolute/traversal/control: {relpath!r}"
            )
            continue
        first = relpath.split("/", 1)[0]
        if relpath == first:  # root-level file
            if first in FORBIDDEN_ROOT_FACTORY_FILES:
                errors.append(
                    f"legacy root harness file is tracked: {relpath!r}"
                )
            elif first in _HARNESS_FILENAME_MARKERS:
                errors.append(
                    f"harness config artifact is tracked at the product "
                    f"root: {relpath!r}"
                )
            elif _namespace_escape(first) is not None:
                errors.append(
                    f"case/Unicode namespace escape is tracked: {relpath!r}"
                )
            continue
        if first in HIDDEN_NAMESPACE_SET:
            if first == ".factory-state":
                errors.append(
                    f"runtime state is tracked (must be ignored): {relpath!r}"
                )
                continue
            if first == ".pi":
                if not (relpath == ".pi/subagents.json" or
                        (relpath.startswith(".pi/agents/") and
                         relpath.count("/") == 2 and
                         relpath.endswith(".md"))):
                    errors.append(
                        f".pi/ must contain only required role definitions, "
                        f"found: {relpath!r}"
                    )
                    continue
            # Harness content under the hidden namespaces (``.factory`` and
            # the allowed ``.pi`` role definitions) is allowed; a tracked
            # symlink or gitlink (submodule) mode is never — either would let
            # the harness redirect content outside its namespace.
            mode = modes.get(relpath)
            if mode in ("120000", "160000"):
                errors.append(
                    f"harness path is a tracked symlink/gitlink: {relpath!r}"
                )
            continue
        if first in LEGACY_NAMESPACES:
            if relpath not in LEGACY_TRACKED_ALLOWLIST:
                errors.append(
                    f"legacy namespace gains a new tracked entry: {relpath!r}"
                )
                continue
            mode = modes.get(relpath)
            if mode in ("120000", "160000"):
                errors.append(
                    f"harness path is a tracked symlink/gitlink: {relpath!r}"
                )
            continue
        # Product tree: reject harness-contamination shapes.  A case-fold or
        # NFKC variant of a hidden namespace in *any* segment aliases the
        # namespace on a case-insensitive / normalization-blessed filesystem;
        # a root harness marker or harness-config basename at *any* depth is
        # a leaked harness artifact; a byte-exact hidden namespace inside the
        # declared product source/test/packaging/build trees is packaging
        # contamination.
        segments = relpath.split("/")
        for segment in segments:
            escaped = _namespace_escape(segment)
            if escaped is not None:
                errors.append(
                    f"case/Unicode namespace escape in product path: "
                    f"{relpath!r} (segment {segment!r} aliases {escaped!r})"
                )
            if segment in FORBIDDEN_ROOT_FACTORY_FILES:
                errors.append(
                    f"legacy root harness file leaked into product tree: "
                    f"{relpath!r}"
                )
            if segment in _HARNESS_FILENAME_MARKERS:
                errors.append(
                    f"harness config artifact inside product tree: {relpath!r}"
                )
        if first in PRODUCT_TREE_NAMES:
            if any(segment in HIDDEN_NAMESPACE_SET for segment in segments[1:]):
                errors.append(
                    f"harness namespace inside product tree: {relpath!r}"
                )
                continue
    return errors


# ---------------------------------------------------------------------------
# On-disk (generated/ignored) inventory
# ---------------------------------------------------------------------------


def _walk_nofollow(root: Path, rel_prefix: str) -> Iterable[Tuple[str, os.stat_result]]:
    """Yield ``(relpath, lstat)`` for every entry under ``root/rel_prefix``.

    Directory symlinks are never followed (``os.walk(followlinks=False)``);
    they surface as entries so the caller's symlink check fails closed.
    """
    base = root / rel_prefix
    try:
        names = sorted(os.listdir(base))
    except OSError as exc:
        raise FootprintError(f"cannot list {rel_prefix!r}: {exc}") from exc
    stack: List[str] = []
    for name in names:
        rel = f"{rel_prefix}/{name}" if rel_prefix else name
        try:
            info = os.lstat(root / rel)
        except OSError as exc:
            raise FootprintError(f"cannot stat {rel!r}: {exc}") from exc
        yield rel, info
        if stat.S_ISDIR(info.st_mode) and not stat.S_ISLNK(info.st_mode):
            stack.append(rel)
    while stack:
        rel = stack.pop()
        try:
            names = sorted(os.listdir(root / rel))
        except OSError as exc:
            raise FootprintError(f"cannot list {rel!r}: {exc}") from exc
        for name in names:
            child = f"{rel}/{name}"
            try:
                info = os.lstat(root / child)
            except OSError as exc:
                raise FootprintError(f"cannot stat {child!r}: {exc}") from exc
            yield child, info
            if stat.S_ISDIR(info.st_mode) and not stat.S_ISLNK(info.st_mode):
                stack.append(child)


def _resolved_under(root: Path, rel: str, namespace: str) -> bool:
    """True when ``root/rel`` (a symlink) resolves inside ``root/namespace``."""
    try:
        resolved = Path(os.path.realpath(root / rel))
    except OSError:
        return False
    try:
        resolved.relative_to((root / namespace).resolve())
    except ValueError:
        return False
    return True


def _check_on_disk(root: Path, *, include_runtime: bool = True) -> List[str]:
    """Fail-closed checks over the on-disk generated namespaces.

    ``include_runtime=False`` skips the ``.factory-state/`` mode/owner/ignore
    checks and the product-tree scan (used by the suite for fixtures that
    have no runtime directory yet); the hidden-namespace symlink/special
    walk, the mount-device checks, and the legacy ``.ralph/`` scan always
    run.
    """
    errors: List[str] = []

    # Top-level name hygiene: a case/Unicode variant of a hidden namespace
    # in the repository root is an escape (case-insensitive filesystems).
    try:
        top_names = sorted(os.listdir(root))
    except OSError as exc:
        raise FootprintError(f"cannot list repository root: {exc}") from exc
    for name in top_names:
        if name in HIDDEN_NAMESPACE_SET or name in LEGACY_NAMESPACES:
            continue
        if name in FORBIDDEN_ROOT_FACTORY_FILES:
            errors.append(f"legacy root harness file is present: {name!r}")
            continue
        if name in _HARNESS_FILENAME_MARKERS:
            errors.append(
                f"harness config artifact is present at the product root: "
                f"{name!r}"
            )
            continue
        escaped = _namespace_escape(name)
        if escaped is not None:
            errors.append(
                f"case/Unicode namespace escape is present: {name!r} "
                f"(aliases {escaped!r})"
            )

    # Harness inodes that must never be shared with a product path, plus the
    # repository's own device: an entry whose ``st_dev`` differs is a mount
    # point crossing and fails closed everywhere the hidden walk descends.
    harness_inodes: Set[Tuple[int, int]] = set()
    root_dev = os.lstat(root).st_dev

    for namespace in HIDDEN_NAMESPACES:
        if not os.path.lexists(root / namespace):
            continue
        entry = os.lstat(root / namespace)
        if stat.S_ISLNK(entry.st_mode):
            errors.append(f"hidden namespace is a symlink: {namespace!r}")
            continue
        if not stat.S_ISDIR(entry.st_mode):
            errors.append(f"hidden namespace is not a directory: {namespace!r}")
            continue
        if entry.st_dev != root_dev:
            errors.append(
                f"hidden namespace crosses a mount point: {namespace!r}"
            )
        if namespace == ".factory-state" and include_runtime:
            if stat.S_IMODE(entry.st_mode) != 0o700:
                errors.append(
                    f".factory-state must be created mode 0700, has "
                    f"{oct(stat.S_IMODE(entry.st_mode))}"
                )
            # The ownership check is unconditional: it is not skipped for
            # root (running as root the pinned-Git resolver already fails
            # closed, so an inventory can never silently pass here).
            if entry.st_uid != os.getuid():
                errors.append(
                    ".factory-state must be owned by the invoking user "
                    f"(uid {os.getuid()}), has uid {entry.st_uid}"
                )
            ignored = gitutil.git_run(
                ["-C", str(root), "check-ignore", "-q", ".factory-state"],
                timeout=30,
            )
            if ignored.returncode != 0:
                errors.append(
                    ".factory-state must be git-ignored (runtime state "
                    "must never enter packaging discovery)"
                )
        for rel, info in _walk_nofollow(root, namespace):
            if stat.S_ISLNK(info.st_mode):
                errors.append(f"symlink inside hidden namespace: {rel!r}")
                continue
            if stat.S_ISDIR(info.st_mode):
                if info.st_dev != root_dev:
                    errors.append(
                        f"hidden namespace crosses a mount point: {rel!r}"
                    )
                continue
            if stat.S_ISREG(info.st_mode):
                if info.st_nlink > 1:
                    harness_inodes.add((info.st_dev, info.st_ino))
                continue
            errors.append(
                f"hidden namespace contains a special file: {rel!r} "
                f"(mode {oct(stat.S_IMODE(info.st_mode))})"
            )

    # Legacy ``.ralph/`` recovery history is scanned the same way: the
    # namespace must be a real directory, and no symlink, special inode, or
    # mount crossing may exist beneath it.  Regular on-disk files are allowed
    # only when git-ignored legacy runtime output or exactly the tracked
    # migration allowlist; a non-ignored entry outside the allowlist is new
    # legacy content the new path must never create.
    if os.path.lexists(root / ".ralph"):
        entry = os.lstat(root / ".ralph")
        if stat.S_ISLNK(entry.st_mode):
            errors.append("legacy namespace is a symlink: '.ralph'")
        elif stat.S_ISDIR(entry.st_mode):
            if entry.st_dev != root_dev:
                errors.append("legacy namespace crosses a mount point: '.ralph'")
            on_disk: Set[str] = set()
            for rel, info in _walk_nofollow(root, ".ralph"):
                if stat.S_ISLNK(info.st_mode):
                    errors.append(f"symlink inside legacy namespace: {rel!r}")
                    continue
                if stat.S_ISDIR(info.st_mode):
                    if info.st_dev != root_dev:
                        errors.append(
                            f"legacy namespace crosses a mount point: {rel!r}"
                        )
                    continue
                if not stat.S_ISREG(info.st_mode):
                    errors.append(
                        f"legacy namespace contains a special file: {rel!r}"
                    )
                    continue
                on_disk.add(rel)
            if on_disk:
                result = gitutil.git_run(
                    ["-C", str(root), "ls-files", "-co", "--exclude-standard",
                     "-z", "--", ".ralph"],
                    timeout=60,
                )
                if result.returncode != 0:
                    raise FootprintError(
                        f"cannot enumerate .ralph on-disk entries: "
                        f"{result.stderr.strip()}"
                    )
                non_ignored = {p for p in result.stdout.split("\0") if p}
                for rel in sorted(non_ignored - LEGACY_TRACKED_ALLOWLIST):
                    errors.append(
                        f"legacy namespace gains a new on-disk entry: {rel!r}"
                    )

    if not include_runtime:
        return errors

    # Product-tree scan: no product path may alias the harness (hardlink or
    # symlink), and no harness-shaped artifact may live inside the product
    # source/build/packaging trees.  The case-fold/NFKC escape, root-marker,
    # harness-config, and exact-hidden-namespace checks apply to *every*
    # nested segment, mirroring the tracked inventory.
    product_stack: List[str] = []
    for name in top_names:
        if name in HIDDEN_NAMESPACE_SET or name in LEGACY_NAMESPACES:
            continue
        if name == ".git":
            continue
        if name in ("__pycache__",):
            continue
        product_stack.append(name)
    while product_stack:
        rel = product_stack.pop()
        if "/" in rel:
            for segment in rel.split("/"):
                escaped = _namespace_escape(segment)
                if escaped is not None:
                    errors.append(
                        f"case/Unicode namespace escape is present in product "
                        f"path: {rel!r} (segment {segment!r} aliases "
                        f"{escaped!r})"
                    )
                if segment in FORBIDDEN_ROOT_FACTORY_FILES:
                    errors.append(
                        f"legacy root harness file leaked into product tree: "
                        f"{rel!r}"
                    )
                if segment in _HARNESS_FILENAME_MARKERS:
                    errors.append(
                        f"harness config artifact in product tree: {rel!r}"
                    )
                if segment in HIDDEN_NAMESPACE_SET:
                    errors.append(
                        f"harness namespace inside product tree: {rel!r}"
                    )
        try:
            info = os.lstat(root / rel)
        except OSError:
            continue
        if stat.S_ISLNK(info.st_mode):
            for namespace in HIDDEN_NAMESPACES:
                if _resolved_under(root, rel, namespace):
                    errors.append(
                        f"product path is a symlink into a hidden namespace: "
                        f"{rel!r}"
                    )
                    break
            continue
        if stat.S_ISDIR(info.st_mode):
            try:
                for name in sorted(os.listdir(root / rel)):
                    product_stack.append(f"{rel}/{name}")
            except OSError:
                continue
            continue
        if stat.S_ISREG(info.st_mode):
            if (info.st_dev, info.st_ino) in harness_inodes:
                errors.append(
                    f"product path shares an inode with harness content "
                    f"(hardlink alias): {rel!r}"
                )
    return errors


# ---------------------------------------------------------------------------
# External executable and install-prefix inventory
# ---------------------------------------------------------------------------


def check_pinned_external_executables() -> List[str]:
    """Fail closed when a harness-pinned external executable is untrusted.

    The Git boundary binary must be absolute and resolve under
    :data:`TRUSTED_EXTERNAL_PREFIXES`; the immutable-chain authority
    (``gitutil.require_trusted_executable``) is the deeper proof that the
    caller cannot replace the resolved path.  The interpreter is deliberately
    not part of this deterministic inventory: the trusted launch authority
    (``launch.require_trusted_interpreter``) validates ``sys.executable``
    through the same immutable-chain check at launch time, while this gate
    must stay environment-independent for the live boilerplate.
    """
    errors: List[str] = []
    path = getattr(gitutil, "GIT_EXECUTABLE", "")
    if not path or not path.startswith("/"):
        errors.append(f"pinned git is not an absolute external path: {path!r}")
        return errors
    if not any(path == prefix or path.startswith(prefix + "/")
               for prefix in TRUSTED_EXTERNAL_PREFIXES):
        errors.append(
            f"pinned git escapes the trusted external prefix: {path!r}"
        )
        return errors
    try:
        gitutil.require_trusted_executable(path)
    except gitutil.GitBoundaryError as exc:
        errors.append(f"pinned git fails the immutable-chain check: {exc}")
    return errors


def verify_external_install(
    root: Path,
    prefix: Path,
    manifest: Optional[Sequence[str]] = None,
    entrypoints: Sequence[str] = (),
) -> List[str]:
    """Validate an operator-installed harness copy under an external prefix.

    The install contract (§3 operator interface) is that an external-prefix
    harness copy carries *only* hidden-namespace content plus a single
    operator entrypoint: no product source, test, packaging, or build file
    may be installed outside the product tree.  ``manifest`` (when given)
    is the exact list of repository-relative paths the installer copied:
    every entry must be a harness-owned path, every entry must be present on
    disk under ``prefix`` (no omitted manifest file), and no physical file
    may exist under ``prefix`` outside the manifest — with the sole
    exception of ``entrypoints``, an explicit list of trusted operator
    entrypoints that must each be a regular executable file.  Symlinks and
    special inodes are never allowed anywhere in the installed copy.
    ``prefix`` must be absolute, a real (never symlinked) directory, and
    outside the *resolved* product repository.
    """
    errors: List[str] = []
    raw_prefix = str(prefix)
    if not raw_prefix.startswith("/"):
        errors.append(f"external prefix is not absolute: {prefix}")
        return errors
    prefix = prefix.absolute()
    root = root.absolute()
    # The prefix itself must never be a symlink: the installed copy has to
    # be a real directory tree, and a symlink could redirect the inventory
    # walk (or a later removal) outside the declared prefix.  Containment
    # is then compared on the *resolved* paths, so a root or prefix reached
    # through a symlinked ancestor can never hide a prefix that actually
    # lies inside the product tree.
    try:
        prefix_lstat = os.lstat(prefix)
    except OSError:
        prefix_lstat = None
    if prefix_lstat is not None and stat.S_ISLNK(prefix_lstat.st_mode):
        errors.append(f"external install prefix is a symlink: {prefix}")
        return errors
    resolved_root = Path(os.path.realpath(root))
    resolved_prefix = Path(os.path.realpath(prefix))
    try:
        resolved_prefix.relative_to(resolved_root)
    except ValueError:
        pass
    else:
        errors.append(
            f"external prefix must be outside the product tree: {prefix}"
        )
        return errors
    if manifest is None:
        # Legacy mode: no manifest given, only the physical hygiene checks.
        if not prefix.exists():
            return errors
        for rel, info in _walk_nofollow(prefix, ""):
            if stat.S_ISLNK(info.st_mode):
                errors.append(f"installed harness symlink escapes: {rel!r}")
            elif not (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode)):
                errors.append(f"installed harness special file: {rel!r}")
        return errors

    manifest_set: Set[str] = set()
    for relpath in manifest:
        safe = safe_relpath(relpath)
        if safe is None:
            errors.append(f"manifest path is unsafe: {relpath!r}")
            continue
        if classify_path(relpath) != "harness":
            errors.append(
                f"external install carries a non-harness path: {relpath!r}"
            )
            continue
        manifest_set.add(relpath)
    entrypoint_set: Set[str] = set()
    for relpath in entrypoints:
        safe = safe_relpath(relpath)
        if safe is None:
            errors.append(f"entrypoint path is unsafe: {relpath!r}")
            continue
        entrypoint_set.add(relpath)
    if not prefix.exists():
        errors.append(f"external install prefix does not exist: {prefix}")
        return errors

    physical: Dict[str, os.stat_result] = {}
    for rel, info in _walk_nofollow(prefix, ""):
        physical[rel] = info
    # Completeness: every manifest path must be physically present as a
    # regular file (directories under the prefix are containers, not
    # manifest items).
    for rel in sorted(manifest_set):
        info = physical.get(rel)
        if info is None:
            errors.append(f"external install omits manifest file: {rel!r}")
        elif not stat.S_ISREG(info.st_mode):
            errors.append(f"manifest file is not a regular file: {rel!r}")
    # Exactness: no physical file outside manifest | entrypoints, and each
    # entrypoint must be an explicit regular executable file.
    for rel, info in sorted(physical.items()):
        if stat.S_ISDIR(info.st_mode):
            continue
        if rel in entrypoint_set:
            if not (stat.S_ISREG(info.st_mode) and info.st_mode & 0o111):
                errors.append(
                    f"entrypoint is not an explicit trusted executable: {rel!r}"
                )
            continue
        if rel not in manifest_set:
            errors.append(
                f"external install carries an unmanifested path: {rel!r}"
            )
    # No symlink may appear anywhere in the installed copy (a symlink could
    # redirect the harness's own entrypoint or blob reads outside the
    # prefix), and special inodes are rejected the same way.
    for rel, info in sorted(physical.items()):
        if stat.S_ISLNK(info.st_mode):
            errors.append(f"installed harness symlink escapes: {rel!r}")
        elif not (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode)):
            errors.append(f"installed harness special file: {rel!r}")
    return errors


def verify_product_install(prefix: Path) -> List[str]:
    """Fail when a *product* install prefix contains harness entries.

    Product packaging must never install the hidden namespaces anywhere in
    the staged tree: ``.factory/``, ``.factory-state/``, or ``.pi/`` at any
    depth, a root harness marker or harness config-artifact basename at any
    depth, and a case/Unicode/trailing-dot namespace alias are all
    packaging/install contamination.  The staged tree must be real product
    content: a symlink anywhere (a link could redirect a packaged file
    outside the prefix), a FIFO/socket/device node, or an entry crossing a
    mount point (``st_dev`` differing from the prefix root) fails closed.
    """
    errors: List[str] = []
    prefix = prefix.absolute()
    if not prefix.exists():
        return errors
    if os.path.islink(prefix):
        errors.append(f"product install prefix is a symlink: {prefix}")
        return errors
    if not os.path.isdir(prefix):
        errors.append(f"product install prefix is not a directory: {prefix}")
        return errors
    prefix_dev = os.lstat(prefix).st_dev
    for rel, info in _walk_nofollow(prefix, ""):
        for segment in rel.split("/"):
            if segment in HIDDEN_NAMESPACE_SET:
                errors.append(
                    f"product install prefix contains a harness namespace: "
                    f"{rel!r}"
                )
            if segment in FORBIDDEN_ROOT_FACTORY_FILES:
                errors.append(
                    f"product install prefix contains a root harness file: "
                    f"{rel!r}"
                )
            if segment in _HARNESS_FILENAME_MARKERS:
                errors.append(
                    f"product install prefix contains a harness config "
                    f"artifact: {rel!r}"
                )
            escaped = _namespace_escape(segment)
            if escaped is not None:
                errors.append(
                    f"product install prefix contains a namespace alias: "
                    f"{rel!r} (segment {segment!r} aliases {escaped!r})"
                )
        if stat.S_ISLNK(info.st_mode):
            errors.append(f"product install prefix contains a symlink: {rel!r}")
        elif not (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode)):
            errors.append(
                f"product install prefix contains a special file: {rel!r}"
            )
        elif info.st_dev != prefix_dev:
            errors.append(
                f"product install prefix crosses a mount point: {rel!r}"
            )
    return errors


def _namespace_root_identity(info: os.stat_result) -> Tuple[int, int, int]:
    """The identity that pins one namespace root between check and removal.

    ``(st_dev, st_ino, st_mode)``: a bind-mount swap over the namespace
    changes ``st_dev``/``st_ino`` and a symlink swap changes ``st_mode``, so
    re-verifying the identity immediately before deletion catches a
    check-to-use substitution instead of deleting into the swapped tree.
    """
    return (info.st_dev, info.st_ino, info.st_mode)


def _namespace_tree_identities(
    root: Path, name: str, root_dev: int
) -> Dict[str, Tuple[int, int, int]]:
    """Snapshot every descendant identity of ``root/name`` (fail-closed).

    Returns ``{relpath: (st_dev, st_ino, st_mode)}`` for every entry below
    the namespace root; a descendant on a different device (a mount point)
    refuses the removal.
    """
    identities: Dict[str, Tuple[int, int, int]] = {}
    for rel, child in _walk_nofollow(root, name):
        if child.st_dev != root_dev:
            raise FootprintError(
                f"refusing to remove harness namespace {name!r}: "
                f"descendant {rel!r} crosses a filesystem/mount boundary "
                f"(st_dev {child.st_dev} != {root_dev})"
            )
        identities[rel] = (child.st_dev, child.st_ino, child.st_mode)
    return identities


def remove_harness(root: Path) -> List[str]:
    """Delete the hidden namespaces; product content is never touched.

    Returns the removed namespace names.  The removal is fail-closed: every
    namespace root and every descendant is verified against the repository's
    own device *before anything is deleted*, and a namespace whose root or a
    descendant crosses a filesystem/mount boundary (a different ``st_dev``)
    refuses the entire removal, leaving every namespace untouched.  Used by
    generated projects to remove the harness without product loss, and by
    the hidden suite to prove that deleting the hidden namespaces leaves
    every product file byte-identical.

    Task 16 closes the Task 13 accepted check-to-use (TOCTOU) residual: the
    removal is not a single ``stat``-then-``rmtree``.  Every namespace root's
    identity (``st_dev``/``st_ino``/``st_mode``) and the identity of every
    descendant are snapshotted during the verification walk, and each
    namespace is re-walked and re-stat'ed *immediately before* its deletion;
    a root/descendant that changed identity, a namespace that became a
    symlink or mount point, or any added/missing/replaced entry refuses the
    entire removal with nothing deleted.  A privileged bind-mount swap or
    symlink substitution between the verification pass and the deletion pass
    therefore fails closed instead of crossing into the mounted tree.
    """
    root = root.absolute()
    root_dev = os.lstat(root).st_dev
    planned: Dict[
        str,
        Optional[Tuple[Tuple[int, int, int], Dict[str, Tuple[int, int, int]]]],
    ] = {}
    for name in HIDDEN_NAMESPACES:
        target = root / name
        try:
            info = os.lstat(target)
        except OSError:
            continue
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            # A symlink or non-directory namespace is removed by unlinking
            # the entry itself; the link is never followed.
            planned[name] = None
            continue
        if info.st_dev != root_dev:
            raise FootprintError(
                f"refusing to remove harness namespace {name!r}: it crosses "
                f"a filesystem/mount boundary (st_dev {info.st_dev} != "
                f"{root_dev})"
            )
        planned[name] = (
            _namespace_root_identity(info),
            _namespace_tree_identities(root, name, root_dev),
        )
    removed: List[str] = []
    for name in planned:
        entry = planned[name]
        target = root / name
        info = os.lstat(target)
        if entry is None:
            # The entry was verified as a symlink/non-directory; it must
            # still be one (a swap to a real directory or a mount point is
            # refused, never followed).
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
                target.unlink()
                removed.append(name)
                continue
            raise FootprintError(
                f"refusing to remove harness namespace {name!r}: entry changed "
                f"between verification and removal"
            )
        root_identity, identities = entry
        if (
            stat.S_ISLNK(info.st_mode)
            or not stat.S_ISDIR(info.st_mode)
            or _namespace_root_identity(info) != root_identity
        ):
            # The namespace root changed identity since the verification
            # pass (a bind-mount swap or symlink substitution over the
            # entry); refuse the removal instead of deleting into the
            # swapped tree.
            raise FootprintError(
                f"refusing to remove harness namespace {name!r}: entry changed "
                f"between verification and removal"
            )
        current = _namespace_tree_identities(root, name, root_dev)
        if current != identities:
            raise FootprintError(
                f"refusing to remove harness namespace {name!r}: tree changed "
                f"between verification and removal"
            )
        shutil.rmtree(target)
        removed.append(name)
    return removed


# ---------------------------------------------------------------------------
# Aggregate inventory
# ---------------------------------------------------------------------------


class FootprintReport:
    """A deterministic, machine-readable inventory result."""

    __slots__ = ("root", "tracked_errors", "on_disk_errors", "external_errors")

    def __init__(self, root: Path) -> None:
        self.root = root
        self.tracked_errors: List[str] = []
        self.on_disk_errors: List[str] = []
        self.external_errors: List[str] = []

    @property
    def errors(self) -> List[str]:
        return self.tracked_errors + self.on_disk_errors + self.external_errors

    @property
    def ok(self) -> bool:
        return not self.errors

    def to_dict(self) -> Dict[str, object]:
        return {
            "schema": "factory-footprint/v1",
            "root": str(self.root),
            "tracked_errors": self.tracked_errors,
            "on_disk_errors": self.on_disk_errors,
            "external_errors": self.external_errors,
        }


def inventory(
    root: Path,
    *,
    include_runtime: bool = True,
    check_external: bool = True,
) -> FootprintReport:
    """Run the full footprint inventory of ``root`` (fail-closed per class)."""
    root = Path(root).absolute()
    if not root.is_dir():
        raise FootprintError(f"repository root is not a directory: {root}")
    report = FootprintReport(root)
    report.tracked_errors = _check_tracked(root)
    report.on_disk_errors = _check_on_disk(root, include_runtime=include_runtime)
    if check_external:
        report.external_errors = check_pinned_external_executables()
    return report


def product_discovery(root: Path) -> List[str]:
    """Product source/test/packaging discovery (hidden namespaces excluded).

    Returns the deterministic set of repository-relative files a product
    build system would discover: every tracked file whose first segment is
    not a hidden namespace, not a namespace alias, not the legacy
    ``.ralph/`` namespace, and not a root harness filename (legacy root
    factory file or harness config-artifact basename).  The harness
    namespaces are excluded by construction, so a packaging glob built from
    this discovery can never pick up ``.factory/``, ``.factory-state/``, or
    ``.pi/`` artifacts.
    """
    root = Path(root).absolute()
    discovered: List[str] = []
    for relpath in sorted(tracked_files(root)):
        safe = safe_relpath(relpath)
        if safe is None:
            continue
        first = relpath.split("/", 1)[0]
        if first in HIDDEN_NAMESPACE_SET:
            continue
        if first in LEGACY_NAMESPACES:
            continue
        if _namespace_escape(first) is not None:
            continue
        if "/" not in relpath and relpath in _ROOT_HARNESS_FILENAMES:
            continue
        discovered.append(relpath)
    return discovered


def discover_on_disk(root: Path) -> List[str]:
    """On-disk product discovery: hidden/legacy namespaces never enter it.

    The discovery set is derived from the pinned Git boundary
    ``git ls-files -co --exclude-standard -z`` (tracked files plus untracked
    files that are not git-ignored), so git-ignored credentials, locks,
    caches, logs, and build artifacts are excluded by the repository's own
    ignore rules and can never be fed into a packaging manifest.  Hidden
    namespaces, namespace aliases, the legacy ``.ralph/`` namespace,
    ``.git/``, and root harness markers (legacy root factory files and
    harness config-artifact basenames) are filtered by first segment on top
    of Git's own exclusion.  A product build system that walks the tree
    through this discovery can never yield a harness artifact, and the
    returned set carries no ``.factory``/``.factory-state``/``.pi`` content
    and no packaging secret.
    """
    root = Path(root).absolute()
    result = gitutil.git_run(
        ["-C", str(root), "ls-files", "-co", "--exclude-standard", "-z"],
        timeout=60,
    )
    if result.returncode != 0:
        raise FootprintError(
            f"cannot enumerate on-disk files at {root}: "
            f"{result.stderr.strip() or result.stdout.strip()}"
        )
    discovered: List[str] = []
    for rel in result.stdout.split("\0"):
        if not rel:
            continue
        if rel.startswith(".git/"):
            continue
        first = rel.split("/", 1)[0]
        if first in HIDDEN_NAMESPACE_SET or first in LEGACY_NAMESPACES:
            continue
        if _namespace_escape(first) is not None:
            continue
        if "/" not in rel and rel in _ROOT_HARNESS_FILENAMES:
            continue
        discovered.append(rel)
    return sorted(discovered)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: Optional[Sequence[str]] = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        prog="factory-footprint",
        description=(
            "Hidden harness-footprint inventory authority (HIDE-01 §3; "
            "Task 13). Fails when a harness-owned path escapes the hidden "
            "namespaces or the trusted external prefix."
        ),
    )
    parser.add_argument(
        "--root",
        default=str(Path(__file__).resolve().parents[2]),
        help="canonical repository root (default: this repository)",
    )
    parser.add_argument(
        "--product-discovery",
        action="store_true",
        help="print the deterministic product-discovery file set",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="print the machine-readable inventory report",
    )
    args = parser.parse_args(argv)
    root = Path(args.root).absolute()
    try:
        report = inventory(root)
    except FootprintError as exc:
        print(f"factory-footprint: {exc}", file=sys.stderr)
        return 2
    if args.product_discovery:
        for relpath in product_discovery(root):
            print(relpath)
        return 0
    if args.json:
        print(json.dumps(report.to_dict(), sort_keys=True))
    else:
        for error in report.errors:
            print(f"factory-footprint: {error}", file=sys.stderr)
    return 0 if report.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
