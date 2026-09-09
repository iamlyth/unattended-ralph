#!/usr/bin/env python3
"""Generic task-scoped product-path lease authority (Phase 2C1).

This module is the pure, deterministic foundation for task-scoped write
authority over product-owned verification surfaces.  It implements two
committed schemas:

* ``factory-path-lease-policy/v1`` (``.factory/path-lease-policy.json``) —
  closed config mapping scope IDs to bounded repository-relative path
  prefixes/patterns for product-owned verification surfaces (scripts, build
  environment/Nix files, packaging, CI/forge files), plus absolute immutable
  deny zones (``.factory`` security/control machinery, ``.factory-state``,
  ``.git``, credential/key/env authorities, the product spec path, and all
  goldens/approval/release/human-authority surfaces as configured).  Deny is
  dominant and non-overridable: no carve-out exists in this schema, and a
  human-only override mechanism is deliberately out of scope (it must never
  be mintable by a campaign/model);
* ``factory-task-path-lease/v1`` — strict claims bound to campaign ID,
  selected task ID, attempt, exact HEAD commit, plan digest, policy digest,
  requested/granted scopes, the exact deny-dominant expanded paths/patterns,
  issued/deadline bounds, a unique attempt nonce, and a canonical claim
  digest.

The authority is foundation-only: it mints, parses, validates, and
context-validates claims as DATA.  A claim is never self-authorizing — no
same-UID workspace JSON may grant itself authority.  Phase 2C2 binds claims
into the existing signed launch authority and applies exact no-follow path
grants; this module deliberately does not touch Landlock/workspace
confinement or launch behavior.

The claim digest is an UNKEYED SHA-256 over the canonical claim bytes.  It
is a deterministic integrity check against accidental corruption and
forgery-by-a-non-writer, but it is NOT authoritative on its own: any
same-UID writer can recompute it, so it never proves provenance or
authorization.  Only the trusted harness mints and re-validates claims, and
Phase 2C2 binds the claim into the existing signed launch token (HMAC/FD
authority) — until that binding exists, the digest is non-authoritative
integrity data, never a grant.

Security properties:

* deny-dominant expansion — a requested scope's paths that fall inside an
  immutable deny zone are removed; a scope that grants nothing after
  expansion is forbidden and the request fails closed; deny zones are
  absolute and non-overridable (no carve-out exists in this schema);
* overlapping deny escapes are rejected at policy load — a scope path that
  contains a deny path or a scope pattern that could match a deny path is a
  policy error;
* paths and patterns reject absolute paths, ``..``/``.``/empty segments,
  backslashes, control characters, and unsafe globs; ``**`` recursive
  traversal is rejected in grant patterns as symlink-ambiguous (deny patterns
  may use ``**`` conservatively because over-matching deny only removes
  grants);
* claims never resolve symlinks and carry no free-form commands; every
  granted path/pattern is a bounded repository-relative prefix/pattern;
* replay prevention — a claim binds campaign/task/attempt/commit/plan
  digest/policy digest/nonce/issued/deadline; context validation fails
  closed on any mismatch, and expiry fails closed outside
  ``[issued_at, deadline]``;
* security-sensitive leases (a scope marked ``audit_required``) mark
  ``audit_required`` so the independent audit is mandatory.

The module is a pure function of its inputs: identical policy bytes produce
identical models, digests, and expansions, and identical claim bytes produce
identical models and digests.
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import re
import stat
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

SCHEMA_NAME = "factory-path-lease-policy/v1"
CLAIM_SCHEMA_NAME = "factory-task-path-lease/v1"
POLICY_RELPATH = ".factory/path-lease-policy.json"
POLICY_SCHEMA_FILE = "factory-path-lease-policy-v1.schema.json"
CLAIM_SCHEMA_FILE = "factory-task-path-lease-v1.schema.json"

MAX_POLICY_BYTES = 64 * 1024
MAX_CLAIM_BYTES = 64 * 1024
MAX_SCOPES = 64
MAX_PATHS = 256
MAX_PATTERNS = 256
MAX_PATH_LENGTH = 512
MAX_PATTERN_LENGTH = 512
MAX_SCOPE_ID_LENGTH = 64
MAX_CAMPAIGN_ID_LENGTH = 128
DEFAULT_LEASE_SECONDS = 3600
MAX_LEASE_SECONDS = 86400

SCOPE_ID_RE = re.compile(r"^[a-z][a-z0-9_-]*$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
SHA40_RE = re.compile(r"^[0-9a-f]{40}$")
CAMPAIGN_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,128}$")
SAFE_SEGMENT_RE = re.compile(r"^[A-Za-z0-9._+@-]+$")
GLOB_CLASS_RE = re.compile(r"^[A-Za-z0-9._+@-]+$")
# Glob metacharacters that are never allowed in a grant pattern segment.
FORBIDDEN_GLOB_CHARS = frozenset("{}!^$\\`\"'|&;()<> \t\n\r")


class PathLeaseError(Exception):
    """Base class for every fail-closed path-lease failure."""


class PathLeasePolicyError(PathLeaseError):
    """The committed path-lease policy is malformed, unsafe, or unbound."""


class PathLeaseClaimError(PathLeaseError):
    """A path-lease claim is malformed, forged, or inconsistent."""


class PathLeaseContextError(PathLeaseError):
    """A path-lease claim does not match the trusted launch context."""


@dataclass(frozen=True)
class LeaseScope:
    """One closed-config scope: bounded paths/patterns plus audit flag."""

    scope_id: str
    paths: Tuple[str, ...]
    patterns: Tuple[str, ...]
    audit_required: bool


@dataclass(frozen=True)
class PathLeasePolicy:
    """The committed path-lease policy (closed config, deny-dominant)."""

    scopes: Tuple[LeaseScope, ...]
    deny_paths: Tuple[str, ...]
    deny_patterns: Tuple[str, ...]


@dataclass(frozen=True)
class TaskPathLease:
    """A strict task-scoped path-lease claim (DATA, never self-authorizing)."""

    schema: str
    campaign_id: str
    task_id: int
    attempt: int
    head_commit: str
    plan_digest: str
    policy_digest: str
    requested_scopes: Tuple[str, ...]
    granted_scopes: Tuple[str, ...]
    granted_paths: Tuple[str, ...]
    granted_patterns: Tuple[str, ...]
    issued_at: str
    deadline: str
    nonce: str
    audit_required: bool
    claim_digest: str


def _reject_duplicate_keys(
    pairs: List[tuple], error_type: type = PathLeasePolicyError
) -> Dict[str, object]:
    """JSON object-pairs hook: reject any repeated object key fail-closed."""
    result: Dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise error_type(
                f"duplicate JSON object key in the path-lease document: {key!r}"
            )
        result[key] = value
    return result


def _reject_duplicate_keys_policy(pairs: List[tuple]) -> Dict[str, object]:
    return _reject_duplicate_keys(pairs, PathLeasePolicyError)


def _reject_duplicate_keys_claim(pairs: List[tuple]) -> Dict[str, object]:
    return _reject_duplicate_keys(pairs, PathLeaseClaimError)


def is_scope_id(value: object) -> bool:
    """True for a closed-format scope ID (``^[a-z][a-z0-9_-]*$``)."""
    return (
        isinstance(value, str)
        and bool(SCOPE_ID_RE.fullmatch(value))
        and len(value) <= MAX_SCOPE_ID_LENGTH
    )


def is_safe_path(path: object) -> bool:
    """True for a bounded repository-relative exact path prefix.

    Rejects absolute paths, backslashes, control characters, empty/``.``/``..``
    segments, and any glob or shell metacharacter.  A path is a literal
    prefix: it never resolves symlinks and Phase 2C2 opens it no-follow.
    """
    if not isinstance(path, str) or not path or len(path) > MAX_PATH_LENGTH:
        return False
    if path.startswith("/") or "\\" in path:
        return False
    if any(ord(char) < 0x20 or ord(char) == 0x7F for char in path):
        return False
    segments = path.split("/")
    if any(segment in ("", ".", "..") for segment in segments):
        return False
    return all(SAFE_SEGMENT_RE.fullmatch(segment) for segment in segments)


def _valid_glob_segment(segment: str) -> bool:
    """True for one bounded glob segment (``*``, ``?``, ``[...]`` classes).

    ``**`` is handled by the caller (a full segment only, and only where
    recursive traversal is allowed).  Classes are bounded to the safe
    character set; negation (``!``/``^``), brace expansion, and shell
    metacharacters are rejected.
    """
    if not segment:
        return False
    index = 0
    while index < len(segment):
        char = segment[index]
        if char in "*?":
            index += 1
            continue
        if char == "[":
            close = segment.find("]", index + 1)
            if close == -1:
                return False
            content = segment[index + 1:close]
            if not content or not GLOB_CLASS_RE.fullmatch(content):
                return False
            index = close + 1
            continue
        if char in FORBIDDEN_GLOB_CHARS:
            return False
        if not SAFE_SEGMENT_RE.fullmatch(char):
            return False
        index += 1
    return True


def is_safe_pattern(pattern: object, *, allow_doublestar: bool = False) -> bool:
    """True for a bounded repository-relative glob pattern.

    A pattern is a ``/``-separated sequence of literal or glob segments with
    at least one glob metacharacter.  ``**`` is allowed only as a complete
    segment and only when ``allow_doublestar`` is set (deny patterns);
    grant patterns reject ``**`` as symlink-ambiguous recursive traversal.
    """
    if (
        not isinstance(pattern, str)
        or not pattern
        or len(pattern) > MAX_PATTERN_LENGTH
    ):
        return False
    if pattern.startswith("/") or "\\" in pattern:
        return False
    if any(ord(char) < 0x20 or ord(char) == 0x7F for char in pattern):
        return False
    segments = pattern.split("/")
    if any(segment in ("", ".", "..") for segment in segments):
        return False
    has_glob = False
    for segment in segments:
        if segment == "**":
            if not allow_doublestar:
                return False
            has_glob = True
            continue
        if not _valid_glob_segment(segment):
            return False
        if any(char in "*?[" for char in segment):
            has_glob = True
    return has_glob


def _static_prefix(pattern: str) -> str:
    """The leading literal run of a pattern before its first glob character."""
    for index, char in enumerate(pattern):
        if char in "*?[":
            return pattern[:index]
    return pattern


def _rep(segment: str) -> str:
    """A representative literal string a glob segment can match."""
    out: List[str] = []
    index = 0
    while index < len(segment):
        char = segment[index]
        if char == "*":
            out.append("a")
            index += 1
        elif char == "?":
            out.append("a")
            index += 1
        elif char == "[":
            close = segment.find("]", index + 1)
            content = segment[index + 1:close]
            out.append(content[0] if content else "a")
            index = close + 1
        else:
            out.append(char)
            index += 1
    return "".join(out)


def _segment_compatible(a: str, b: str) -> bool:
    """Conservative: could a literal/glob segment match the same text as b?"""
    if a == b:
        return True
    if "*" in a or "?" in a or "[" in a or "*" in b or "?" in b or "[" in b:
        return fnmatch.fnmatchcase(_rep(a), b) or fnmatch.fnmatchcase(_rep(b), a)
    return False


def _pattern_reaches_deny_path(pattern: str, deny_path: str) -> bool:
    """True when a grant pattern could match a deny path or a path under it.

    ``pattern`` never contains ``**`` (grant patterns reject it), so it
    matches exactly ``len(segments)`` segments.  It reaches ``deny_path``
    when the segment counts align and every segment is compatible, when it
    matches a path under the deny path, or when it matches a prefix of the
    deny path (a write to that prefix could create the deeper immutable
    deny path inside it — M1).
    """
    pattern_segments = pattern.split("/")
    deny_segments = deny_path.split("/")
    if len(pattern_segments) == len(deny_segments):
        return fnmatch.fnmatchcase(deny_path, pattern)
    if len(pattern_segments) > len(deny_segments):
        return all(
            _segment_compatible(a, b)
            for a, b in zip(pattern_segments[:len(deny_segments)], deny_segments)
        )
    # The pattern is shorter than the deny path: it reaches the deny zone
    # when it can match a prefix of the deny path, because a write to that
    # prefix could create the deeper immutable deny path inside it.
    return all(
        _segment_compatible(a, b)
        for a, b in zip(pattern_segments, deny_segments[:len(pattern_segments)])
    )


def _patterns_overlap(a: str, b: str) -> bool:
    """Conservative: could two patterns match a common path?

    ``a`` is a grant pattern (no ``**``); ``b`` may be a deny pattern with
    ``**``.  The check is deliberately conservative: an uncertain overlap is
    treated as an overlap so deny-dominant filtering can never let a grant
    pattern reach a deny zone.
    """
    a_segments = a.split("/")
    b_segments = b.split("/")
    if "**" not in b_segments:
        if len(a_segments) != len(b_segments):
            return False
        return all(
            _segment_compatible(x, y) for x, y in zip(a_segments, b_segments)
        )
    fixed = [segment for segment in b_segments if segment != "**"]
    if len(fixed) > len(a_segments):
        return False
    index = 0
    for segment in fixed:
        found = False
        while index < len(a_segments):
            if _segment_compatible(a_segments[index], segment):
                found = True
                index += 1
                break
            index += 1
        if not found:
            return False
    return True


def _path_in_deny(path: str, policy: PathLeasePolicy) -> bool:
    """True when an exact path falls inside an immutable deny zone."""
    for deny_path in policy.deny_paths:
        if path == deny_path or path.startswith(deny_path + "/"):
            return True
    for deny_pattern in policy.deny_patterns:
        if fnmatch.fnmatchcase(path, deny_pattern):
            return True
    return False


def _pattern_deny_safe(pattern: str, policy: PathLeasePolicy) -> bool:
    """True when a grant pattern cannot reach any deny zone path.

    Defense in depth: policy load already rejects scope patterns that reach
    deny paths, so this filter is a deterministic re-check at request time.
    """
    for deny_path in policy.deny_paths:
        if _pattern_reaches_deny_path(pattern, deny_path):
            return False
    for deny_pattern in policy.deny_patterns:
        if _patterns_overlap(pattern, deny_pattern):
            return False
    return True


def _validate_scope_deny_escapes(policy: PathLeasePolicy) -> None:
    """Reject scope grants that contain or reach an immutable deny path.

    A scope path that contains a deny path grants the deny zone itself (the
    deny-dominant filter cannot split a prefix), and a scope pattern that
    could match a deny path, a path under it, or a prefix of it (a write to
    that prefix could create the deeper immutable deny path inside it — M1)
    reaches the deny zone; all are overlapping deny escapes and fail closed
    at policy load.  A scope path that is itself inside a deny zone is not
    an escape: deny-dominant expansion removes it at request time.  Deny
    zones are absolute and non-overridable, so no carve-out can ever make an
    escape safe.
    """
    for scope in policy.scopes:
        for path in scope.paths:
            for deny_path in policy.deny_paths:
                if deny_path.startswith(path + "/"):
                    raise PathLeasePolicyError(
                        f"scope {scope.scope_id!r} path {path!r} contains the "
                        f"immutable deny zone {deny_path!r} (overlapping deny "
                        "escape)"
                    )
        for pattern in scope.patterns:
            for deny_path in policy.deny_paths:
                if _pattern_reaches_deny_path(pattern, deny_path):
                    raise PathLeasePolicyError(
                        f"scope {scope.scope_id!r} pattern {pattern!r} can "
                        f"reach the immutable deny zone {deny_path!r} "
                        "(overlapping deny escape)"
                    )


def _parse_path_list(value: object, what: str) -> List[str]:
    if not isinstance(value, list):
        raise PathLeasePolicyError(f"`{what}` must be a JSON array")
    if len(value) > MAX_PATHS:
        raise PathLeasePolicyError(
            f"`{what}` may carry at most {MAX_PATHS} entries"
        )
    result: List[str] = []
    for entry in value:
        if not is_safe_path(entry):
            raise PathLeasePolicyError(
                f"`{what}` entry {entry!r} is not a safe repository-relative "
                "path prefix (absolute, traversal, backslash, control, or "
                "glob characters are rejected)"
            )
        if entry in result:
            raise PathLeasePolicyError(
                f"`{what}` has a duplicate path {entry!r}"
            )
        result.append(entry)
    return result


def _parse_pattern_list(
    value: object, what: str, *, allow_doublestar: bool
) -> List[str]:
    if not isinstance(value, list):
        raise PathLeasePolicyError(f"`{what}` must be a JSON array")
    if len(value) > MAX_PATTERNS:
        raise PathLeasePolicyError(
            f"`{what}` may carry at most {MAX_PATTERNS} entries"
        )
    result: List[str] = []
    for entry in value:
        if not is_safe_pattern(entry, allow_doublestar=allow_doublestar):
            raise PathLeasePolicyError(
                f"`{what}` entry {entry!r} is not a safe bounded glob pattern "
                "(absolute, traversal, backslash, control, unsafe-glob, or "
                "symlink-ambiguous `**` patterns are rejected)"
            )
        if entry in result:
            raise PathLeasePolicyError(
                f"`{what}` has a duplicate pattern {entry!r}"
            )
        result.append(entry)
    return result


def parse_policy(data: bytes) -> PathLeasePolicy:
    """Deterministically parse and validate the committed policy document.

    Rejects: non-JSON, a wrong schema name, an unknown/missing field, a
    duplicate key, an unsafe path/pattern, a duplicate path/pattern, an
    invalid scope ID, an overlapping deny escape, or a deny zone that is
    not absolute/non-overridable.  The parsed policy is a deterministic function
    of the document bytes.
    """
    try:
        document = json.loads(
            data.decode("utf-8"), object_pairs_hook=_reject_duplicate_keys_policy
        )
    except (UnicodeDecodeError, ValueError) as exc:
        raise PathLeasePolicyError(
            f"the path-lease policy is not valid JSON: {exc}"
        ) from exc
    if not isinstance(document, dict):
        raise PathLeasePolicyError("the path-lease policy must be a JSON object")
    if document.get("schema") != SCHEMA_NAME:
        raise PathLeasePolicyError(
            f"the path-lease policy schema must be exactly {SCHEMA_NAME!r}, "
            f"got {document.get('schema')!r}"
        )
    expected = {"schema", "scopes", "deny"}
    extra = sorted(set(document) - expected)
    missing = sorted(expected - set(document))
    if extra or missing:
        raise PathLeasePolicyError(
            "the path-lease policy must carry exactly the documented field set"
            + (f" (extra: {extra})" if extra else "")
            + (f" (missing: {missing})" if missing else "")
        )

    scopes_document = document["scopes"]
    if not isinstance(scopes_document, dict):
        raise PathLeasePolicyError("`scopes` must be a JSON object")
    if len(scopes_document) > MAX_SCOPES:
        raise PathLeasePolicyError(
            f"`scopes` may carry at most {MAX_SCOPES} entries"
        )
    scopes: List[LeaseScope] = []
    for scope_id, scope_document in scopes_document.items():
        if not is_scope_id(scope_id):
            raise PathLeasePolicyError(
                f"scope ID {scope_id!r} must match `^[a-z][a-z0-9_-]*$`"
            )
        if not isinstance(scope_document, dict):
            raise PathLeasePolicyError(f"scope {scope_id!r} must be an object")
        scope_expected = {"paths", "patterns", "audit_required"}
        scope_extra = sorted(set(scope_document) - scope_expected)
        scope_missing = sorted(scope_expected - set(scope_document))
        if scope_extra or scope_missing:
            raise PathLeasePolicyError(
                f"scope {scope_id!r} must carry exactly the documented field set"
                + (f" (extra: {scope_extra})" if scope_extra else "")
                + (f" (missing: {scope_missing})" if scope_missing else "")
            )
        audit_required = scope_document["audit_required"]
        if not isinstance(audit_required, bool):
            raise PathLeasePolicyError(
                f"scope {scope_id!r} `audit_required` must be a boolean"
            )
        scopes.append(LeaseScope(
            scope_id=scope_id,
            paths=tuple(_parse_path_list(
                scope_document["paths"], f"scope {scope_id} paths"
            )),
            patterns=tuple(_parse_pattern_list(
                scope_document["patterns"], f"scope {scope_id} patterns",
                allow_doublestar=False,
            )),
            audit_required=audit_required,
        ))

    deny_document = document["deny"]
    if not isinstance(deny_document, dict):
        raise PathLeasePolicyError("`deny` must be a JSON object")
    deny_expected = {"paths", "patterns"}
    deny_extra = sorted(set(deny_document) - deny_expected)
    deny_missing = sorted(deny_expected - set(deny_document))
    if deny_extra or deny_missing:
        raise PathLeasePolicyError(
            "`deny` must carry exactly the documented field set"
            + (f" (extra: {deny_extra})" if deny_extra else "")
            + (f" (missing: {deny_missing})" if deny_missing else "")
        )
    deny_paths = tuple(_parse_path_list(deny_document["paths"], "deny paths"))
    deny_patterns = tuple(_parse_pattern_list(
        deny_document["patterns"], "deny patterns", allow_doublestar=True
    ))

    policy = PathLeasePolicy(
        scopes=tuple(scopes),
        deny_paths=deny_paths,
        deny_patterns=deny_patterns,
    )
    _validate_scope_deny_escapes(policy)
    return policy


def load_policy_config(root: object) -> PathLeasePolicy:
    """Load the committed path-lease policy with a bounded no-follow read.

    A missing document fails closed (the lease authority is never silently
    disabled); a malformed, unsafe, or unbounded document fails closed as a
    policy error.  The read is identity-pinned (no-follow, regular file,
    bounded size, unchanged while read) so a symlink or concurrent swap can
    never substitute a different policy.  Every path component is opened
    dirfd/no-follow through ``openat``: a symlink in any component (the
    root, ``.factory``, or the policy file itself) fails closed.
    """
    root_path = Path(str(root)).absolute()
    rel = Path(POLICY_RELPATH)
    parts = rel.parts
    parent_fd = os.open(
        str(root_path),
        os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0),
    )
    try:
        for part in parts[:-1]:
            try:
                parent_fd = os.open(
                    part,
                    os.O_RDONLY | os.O_DIRECTORY
                    | getattr(os, "O_NOFOLLOW", 0)
                    | getattr(os, "O_CLOEXEC", 0),
                    dir_fd=parent_fd,
                )
            except OSError as exc:
                raise PathLeasePolicyError(
                    f"cannot open the committed path-lease policy parent "
                    f"component {part!r}: {exc}"
                ) from exc
            try:
                info = os.fstat(parent_fd)
            except OSError as exc:
                raise PathLeasePolicyError(
                    f"cannot stat the path-lease policy parent {part!r}: {exc}"
                ) from exc
            if not stat.S_ISDIR(info.st_mode):
                raise PathLeasePolicyError(
                    f"the path-lease policy parent {part!r} is not a directory"
                )
        flags = (
            os.O_RDONLY
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0)
        )
        try:
            descriptor = os.open(parts[-1], flags, dir_fd=parent_fd)
        except OSError as exc:
            raise PathLeasePolicyError(
                f"cannot open the committed path-lease policy "
                f"{root_path / POLICY_RELPATH}: {exc}"
            ) from exc
        try:
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode):
                raise PathLeasePolicyError(
                    f"the path-lease policy {root_path / POLICY_RELPATH} is "
                    "not a regular file"
                )
            if info.st_size > MAX_POLICY_BYTES:
                raise PathLeasePolicyError(
                    f"the path-lease policy {root_path / POLICY_RELPATH} "
                    f"exceeds the {MAX_POLICY_BYTES}-byte bound"
                )
            before = (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)
            raw = bytearray()
            while True:
                chunk = os.read(descriptor, 65536)
                if not chunk:
                    break
                raw.extend(chunk)
                if len(raw) > MAX_POLICY_BYTES:
                    raise PathLeasePolicyError(
                        f"the path-lease policy {root_path / POLICY_RELPATH} "
                        "exceeds the bound"
                    )
            after = os.fstat(descriptor)
            if (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns) != before:
                raise PathLeasePolicyError(
                    f"the path-lease policy {root_path / POLICY_RELPATH} changed "
                    "while being read"
                )
        finally:
            os.close(descriptor)
    finally:
        os.close(parent_fd)
    return parse_policy(bytes(raw))


def canonical_policy_bytes(policy: PathLeasePolicy) -> bytes:
    """Deterministic canonical encoding of the policy model.

    The encoding is the exact committed field set in sorted-key JSON, so the
    digest is a deterministic function of the policy and any semantic change
    (a weakened deny zone, a broadened scope) changes it.
    """
    payload = {
        "schema": SCHEMA_NAME,
        "scopes": {
            scope.scope_id: {
                "paths": list(scope.paths),
                "patterns": list(scope.patterns),
                "audit_required": scope.audit_required,
            }
            for scope in policy.scopes
        },
        "deny": {
            "paths": list(policy.deny_paths),
            "patterns": list(policy.deny_patterns),
        },
    }
    return json.dumps(
        payload, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def policy_digest(policy: PathLeasePolicy) -> str:
    """Deterministic SHA-256 of the canonical policy document."""
    return hashlib.sha256(canonical_policy_bytes(policy)).hexdigest()


# Module-level alias so ``mint_claim``/``validate_claim`` can compute the
# canonical policy digest even though their ``policy_digest`` parameter
# shadows the function name inside the function body.
_policy_digest_of = policy_digest


def expand_request(
    requested_scopes: Sequence[str], policy: PathLeasePolicy
) -> Tuple[List[str], List[str], List[str], bool]:
    """Deny-dominant expansion of a scope request to granted paths/patterns.

    Returns ``(granted_scopes, granted_paths, granted_patterns,
    audit_required)``.  Unknown, duplicate, or malformed scope IDs fail
    closed; a requested scope that grants nothing after deny-dominant
    expansion is forbidden and fails closed.  ``audit_required`` is set when
    any granted scope is marked ``audit_required``.
    """
    if not isinstance(requested_scopes, (list, tuple)):
        raise PathLeaseClaimError("requested scopes must be a list")
    seen: set = set()
    for scope_id in requested_scopes:
        if not is_scope_id(scope_id):
            raise PathLeaseClaimError(
                f"malformed write scope {scope_id!r} (must match "
                "`^[a-z][a-z0-9_-]*$`)"
            )
        if scope_id in seen:
            raise PathLeaseClaimError(f"duplicate requested scope {scope_id!r}")
        seen.add(scope_id)
    by_id = {scope.scope_id: scope for scope in policy.scopes}
    unknown = sorted(seen - set(by_id))
    if unknown:
        raise PathLeaseClaimError(
            f"unknown write scopes: {', '.join(unknown)}"
        )

    granted_scopes: List[str] = []
    granted_paths: List[str] = []
    granted_patterns: List[str] = []
    audit_required = False
    for scope_id in requested_scopes:
        scope = by_id[scope_id]
        scope_paths: List[str] = []
        for path in scope.paths:
            if _path_in_deny(path, policy):
                continue  # deny-dominant: deny beats allow
            scope_paths.append(path)
        scope_patterns = [
            pattern
            for pattern in scope.patterns
            if _pattern_deny_safe(pattern, policy)
        ]
        if not scope_paths and not scope_patterns:
            raise PathLeaseClaimError(
                f"scope {scope_id!r} grants nothing after deny-dominant "
                "expansion (forbidden)"
            )
        granted_scopes.append(scope_id)
        granted_paths.extend(scope_paths)
        granted_patterns.extend(scope_patterns)
        if scope.audit_required:
            audit_required = True
    granted_paths = sorted(set(granted_paths))
    granted_patterns = sorted(set(granted_patterns))
    return granted_scopes, granted_paths, granted_patterns, audit_required


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(timestamp: datetime) -> str:
    return timestamp.isoformat()


def _parse_iso(value: str, what: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise PathLeaseClaimError(
            f"{what} {value!r} is not an ISO-8601 timestamp"
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        raise PathLeaseClaimError(f"{what} {value!r} must be UTC")
    return parsed


def canonical_claim_bytes(claim: TaskPathLease) -> bytes:
    """Deterministic canonical encoding of a claim (excluding its digest)."""
    payload = {
        "schema": claim.schema,
        "campaign_id": claim.campaign_id,
        "task_id": claim.task_id,
        "attempt": claim.attempt,
        "head_commit": claim.head_commit,
        "plan_digest": claim.plan_digest,
        "policy_digest": claim.policy_digest,
        "requested_scopes": list(claim.requested_scopes),
        "granted_scopes": list(claim.granted_scopes),
        "granted_paths": list(claim.granted_paths),
        "granted_patterns": list(claim.granted_patterns),
        "issued_at": claim.issued_at,
        "deadline": claim.deadline,
        "nonce": claim.nonce,
        "audit_required": claim.audit_required,
    }
    return json.dumps(
        payload, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def claim_digest(claim: TaskPathLease) -> str:
    """Deterministic SHA-256 of the canonical claim bytes."""
    return hashlib.sha256(canonical_claim_bytes(claim)).hexdigest()


def claim_to_bytes(claim: TaskPathLease) -> bytes:
    """Deterministic full-document serialization (including the digest).

    This is the exact document ``parse_claim`` accepts; ``canonical_claim_bytes``
    remains the digest input (all fields except ``claim_digest``).
    """
    payload = json.loads(canonical_claim_bytes(claim).decode("utf-8"))
    payload["claim_digest"] = claim.claim_digest
    return json.dumps(
        payload, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _validate_claim_bindings(
    *,
    campaign_id: object,
    task_id: object,
    attempt: object,
    head_commit: object,
    plan_digest: object,
    policy_digest: object,
) -> None:
    if (
        not isinstance(campaign_id, str)
        or not CAMPAIGN_ID_RE.fullmatch(campaign_id)
    ):
        raise PathLeaseClaimError(
            f"campaign_id {campaign_id!r} must match "
            "`^[A-Za-z0-9._-]{1,128}$`"
        )
    if isinstance(task_id, bool) or not isinstance(task_id, int) or task_id < 1:
        raise PathLeaseClaimError(f"task_id must be a positive integer, got {task_id!r}")
    if isinstance(attempt, bool) or not isinstance(attempt, int) or attempt < 1:
        raise PathLeaseClaimError(f"attempt must be a positive integer, got {attempt!r}")
    if not isinstance(head_commit, str) or not SHA40_RE.fullmatch(head_commit):
        raise PathLeaseClaimError(
            f"head_commit must be a 40-character Git object ID, got {head_commit!r}"
        )
    for name, value in (("plan_digest", plan_digest), ("policy_digest", policy_digest)):
        if not isinstance(value, str) or not SHA256_RE.fullmatch(value):
            raise PathLeaseClaimError(
                f"{name} must be a 64-character SHA-256 digest, got {value!r}"
            )


def mint_claim(
    *,
    campaign_id: str,
    task_id: int,
    attempt: int,
    head_commit: str,
    plan_digest: str,
    policy_digest: str,
    requested_scopes: Sequence[str],
    policy: PathLeasePolicy,
    issued_at: Optional[datetime] = None,
    deadline: Optional[datetime] = None,
    nonce: Optional[str] = None,
    now: Optional[datetime] = None,
) -> TaskPathLease:
    """Mint a strict task-scoped path-lease claim (trusted harness only).

    The claim is DATA: it binds the exact launch context (campaign, task,
    attempt, HEAD commit, plan digest, policy digest), the requested and
    deny-dominant granted scopes, the exact expanded paths/patterns, the
    issued/deadline bounds, and a unique attempt nonce, and is sealed by a
    canonical claim digest.  A same-UID workspace JSON can never self-
    authorize: only the trusted harness mints claims and only the trusted
    harness re-validates them against its own context.
    """
    _validate_claim_bindings(
        campaign_id=campaign_id,
        task_id=task_id,
        attempt=attempt,
        head_commit=head_commit,
        plan_digest=plan_digest,
        policy_digest=policy_digest,
    )
    if not isinstance(policy_digest, str) or policy_digest != _policy_digest_of(policy):
        raise PathLeaseClaimError(
            "policy_digest does not match the committed path-lease policy"
        )
    granted_scopes, granted_paths, granted_patterns, audit_required = (
        expand_request(requested_scopes, policy)
    )
    if nonce is None:
        nonce = os.urandom(32).hex()
    if not isinstance(nonce, str) or not SHA256_RE.fullmatch(nonce):
        raise PathLeaseClaimError(
            f"nonce must be a 64-character hex value, got {nonce!r}"
        )
    issued = issued_at if issued_at is not None else (now if now is not None else _utc_now())
    if not isinstance(issued, datetime):
        raise PathLeaseClaimError("issued_at must be a datetime")
    if issued.tzinfo is None or issued.utcoffset() != timedelta(0):
        raise PathLeaseClaimError("issued_at must be an aware UTC datetime")
    if deadline is None:
        deadline = issued + timedelta(seconds=DEFAULT_LEASE_SECONDS)
    if not isinstance(deadline, datetime):
        raise PathLeaseClaimError("deadline must be a datetime")
    if deadline.tzinfo is None or deadline.utcoffset() != timedelta(0):
        raise PathLeaseClaimError("deadline must be an aware UTC datetime")
    lease_seconds = (deadline - issued).total_seconds()
    if lease_seconds <= 0 or lease_seconds > MAX_LEASE_SECONDS:
        raise PathLeaseClaimError(
            f"lease duration must be within (0, {MAX_LEASE_SECONDS}] seconds, "
            f"got {lease_seconds:g}"
        )
    claim = TaskPathLease(
        schema=CLAIM_SCHEMA_NAME,
        campaign_id=campaign_id,
        task_id=task_id,
        attempt=attempt,
        head_commit=head_commit,
        plan_digest=plan_digest,
        policy_digest=policy_digest,
        requested_scopes=tuple(requested_scopes),
        granted_scopes=tuple(granted_scopes),
        granted_paths=tuple(granted_paths),
        granted_patterns=tuple(granted_patterns),
        issued_at=_iso(issued),
        deadline=_iso(deadline),
        nonce=nonce,
        audit_required=audit_required,
        claim_digest="",
    )
    return TaskPathLease(
        **{**claim.__dict__, "claim_digest": claim_digest(claim)}
    )


def parse_claim(data: bytes) -> TaskPathLease:
    """Strictly parse a claim document (duplicate keys and drift fail closed)."""
    try:
        document = json.loads(
            data.decode("utf-8"), object_pairs_hook=_reject_duplicate_keys_claim
        )
    except (UnicodeDecodeError, ValueError) as exc:
        raise PathLeaseClaimError(
            f"the path-lease claim is not valid JSON: {exc}"
        ) from exc
    if not isinstance(document, dict):
        raise PathLeaseClaimError("the path-lease claim must be a JSON object")
    if document.get("schema") != CLAIM_SCHEMA_NAME:
        raise PathLeaseClaimError(
            f"the path-lease claim schema must be exactly {CLAIM_SCHEMA_NAME!r}, "
            f"got {document.get('schema')!r}"
        )
    expected = {
        "schema", "campaign_id", "task_id", "attempt", "head_commit",
        "plan_digest", "policy_digest", "requested_scopes", "granted_scopes",
        "granted_paths", "granted_patterns", "issued_at", "deadline", "nonce",
        "audit_required", "claim_digest",
    }
    extra = sorted(set(document) - expected)
    missing = sorted(expected - set(document))
    if extra or missing:
        raise PathLeaseClaimError(
            "the path-lease claim must carry exactly the documented field set"
            + (f" (extra: {extra})" if extra else "")
            + (f" (missing: {missing})" if missing else "")
        )
    _validate_claim_bindings(
        campaign_id=document["campaign_id"],
        task_id=document["task_id"],
        attempt=document["attempt"],
        head_commit=document["head_commit"],
        plan_digest=document["plan_digest"],
        policy_digest=document["policy_digest"],
    )
    requested_scopes = document["requested_scopes"]
    granted_scopes = document["granted_scopes"]
    if not isinstance(requested_scopes, list) or not isinstance(granted_scopes, list):
        raise PathLeaseClaimError("requested/granted scopes must be JSON arrays")
    if len(requested_scopes) > MAX_SCOPES or len(granted_scopes) > MAX_SCOPES:
        raise PathLeaseClaimError(
            f"the claim may carry at most {MAX_SCOPES} requested/granted scopes"
        )
    for scope_id in requested_scopes + granted_scopes:
        if not is_scope_id(scope_id):
            raise PathLeaseClaimError(
                f"malformed scope ID {scope_id!r} in the claim"
            )
    if len(requested_scopes) != len(set(requested_scopes)):
        raise PathLeaseClaimError("the claim has duplicate requested scopes")
    if len(granted_scopes) != len(set(granted_scopes)):
        raise PathLeaseClaimError("the claim has duplicate granted scopes")
    if not set(granted_scopes).issubset(set(requested_scopes)):
        raise PathLeaseClaimError(
            "the claim grants scopes that were not requested"
        )
    granted_paths = document["granted_paths"]
    granted_patterns = document["granted_patterns"]
    if not isinstance(granted_paths, list) or not isinstance(granted_patterns, list):
        raise PathLeaseClaimError("granted paths/patterns must be JSON arrays")
    if len(granted_paths) > MAX_PATHS:
        raise PathLeaseClaimError(
            f"the claim may carry at most {MAX_PATHS} granted paths"
        )
    if len(granted_patterns) > MAX_PATTERNS:
        raise PathLeaseClaimError(
            f"the claim may carry at most {MAX_PATTERNS} granted patterns"
        )
    for path in granted_paths:
        if not is_safe_path(path):
            raise PathLeaseClaimError(
                f"granted path {path!r} is not a safe repository-relative prefix"
            )
    for pattern in granted_patterns:
        if not is_safe_pattern(pattern, allow_doublestar=False):
            raise PathLeaseClaimError(
                f"granted pattern {pattern!r} is not a safe bounded glob pattern"
            )
    if len(granted_paths) != len(set(granted_paths)):
        raise PathLeaseClaimError("the claim has duplicate granted paths")
    if len(granted_patterns) != len(set(granted_patterns)):
        raise PathLeaseClaimError("the claim has duplicate granted patterns")
    issued_at = document["issued_at"]
    deadline = document["deadline"]
    if not isinstance(issued_at, str) or not isinstance(deadline, str):
        raise PathLeaseClaimError("issued_at/deadline must be strings")
    issued = _parse_iso(issued_at, "issued_at")
    deadline_dt = _parse_iso(deadline, "deadline")
    if deadline_dt <= issued:
        raise PathLeaseClaimError("claim deadline must be after issued_at")
    if (deadline_dt - issued).total_seconds() > MAX_LEASE_SECONDS:
        raise PathLeaseClaimError(
            f"claim lease duration exceeds the {MAX_LEASE_SECONDS}-second bound"
        )
    nonce = document["nonce"]
    if not isinstance(nonce, str) or not SHA256_RE.fullmatch(nonce):
        raise PathLeaseClaimError(
            f"nonce must be a 64-character hex value, got {nonce!r}"
        )
    audit_required = document["audit_required"]
    if not isinstance(audit_required, bool):
        raise PathLeaseClaimError("audit_required must be a boolean")
    claim_digest_value = document["claim_digest"]
    if not isinstance(claim_digest_value, str) or not SHA256_RE.fullmatch(claim_digest_value):
        raise PathLeaseClaimError("claim_digest must be a 64-character SHA-256 digest")
    claim = TaskPathLease(
        schema=CLAIM_SCHEMA_NAME,
        campaign_id=document["campaign_id"],
        task_id=document["task_id"],
        attempt=document["attempt"],
        head_commit=document["head_commit"],
        plan_digest=document["plan_digest"],
        policy_digest=document["policy_digest"],
        requested_scopes=tuple(requested_scopes),
        granted_scopes=tuple(granted_scopes),
        granted_paths=tuple(granted_paths),
        granted_patterns=tuple(granted_patterns),
        issued_at=issued_at,
        deadline=deadline,
        nonce=nonce,
        audit_required=audit_required,
        claim_digest=claim_digest_value,
    )
    if claim_digest(claim) != claim_digest_value:
        raise PathLeaseClaimError(
            "the claim digest does not match the canonical claim bytes (forged "
            "or tampered claim)"
        )
    return claim


def validate_claim(claim: TaskPathLease, policy: PathLeasePolicy) -> None:
    """Validate a claim's structure and its consistency with the policy.

    Re-derives the deny-dominant expansion from the requested scopes and
    fails closed on any drift (granted scopes/paths/patterns/audit flag that
    do not match the trusted policy intersection), on a policy-digest
    mismatch, or on a forged digest.
    """
    if not isinstance(claim, TaskPathLease):
        raise PathLeaseClaimError("validate_claim expects a TaskPathLease")
    if claim.schema != CLAIM_SCHEMA_NAME:
        raise PathLeaseClaimError(
            f"claim schema must be exactly {CLAIM_SCHEMA_NAME!r}"
        )
    if claim.claim_digest != claim_digest(claim):
        raise PathLeaseClaimError(
            "the claim digest does not match the canonical claim bytes (forged "
            "or tampered claim)"
        )
    if claim.policy_digest != _policy_digest_of(policy):
        raise PathLeaseClaimError(
            "the claim policy_digest does not match the committed path-lease "
            "policy (policy drift)"
        )
    granted_scopes, granted_paths, granted_patterns, audit_required = (
        expand_request(claim.requested_scopes, policy)
    )
    if (
        list(claim.granted_scopes) != granted_scopes
        or list(claim.granted_paths) != granted_paths
        or list(claim.granted_patterns) != granted_patterns
        or claim.audit_required != audit_required
    ):
        raise PathLeaseClaimError(
            "the claim's granted set drifts from the trusted policy "
            "intersection (forged or stale claim)"
        )


def validate_claim_context(
    claim: TaskPathLease,
    *,
    campaign_id: str,
    task_id: int,
    attempt: int,
    head_commit: str,
    plan_digest: str,
    policy_digest: str,
    now: Optional[datetime] = None,
) -> None:
    """Validate a claim against the trusted launch context (replay/expiry).

    Every binding must match the trusted context exactly: campaign ID,
    selected task ID, attempt, exact HEAD commit, plan digest, and policy
    digest.  A mismatch — a replay across campaign/task/attempt/commit, a
    plan/policy digest drift, or a claim minted for a different context —
    fails closed.  The current time must fall within ``[issued_at, deadline]``;
    an expired or not-yet-issued claim fails closed.  The unique attempt
    nonce is bound by the claim digest, so a replayed claim can never be
    re-sealed for a different context.
    """
    if not isinstance(claim, TaskPathLease):
        raise PathLeaseContextError("validate_claim_context expects a TaskPathLease")
    bindings = (
        ("campaign_id", claim.campaign_id, campaign_id),
        ("task_id", claim.task_id, task_id),
        ("attempt", claim.attempt, attempt),
        ("head_commit", claim.head_commit, head_commit),
        ("plan_digest", claim.plan_digest, plan_digest),
        ("policy_digest", claim.policy_digest, policy_digest),
    )
    for name, claimed, trusted in bindings:
        if claimed != trusted:
            raise PathLeaseContextError(
                f"claim {name} {claimed!r} does not match the trusted launch "
                f"context {trusted!r} (replay or context mismatch)"
            )
    current = now if now is not None else _utc_now()
    issued = _parse_iso(claim.issued_at, "issued_at")
    deadline = _parse_iso(claim.deadline, "deadline")
    if current < issued:
        raise PathLeaseContextError(
            "claim is not yet issued (issued_at is in the future)"
        )
    if current > deadline:
        raise PathLeaseContextError("claim has expired (past its deadline)")


def _cli(argv: Sequence[str]) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        prog="factory-path-lease",
        description=(
            "Generic task-scoped product-path lease authority (policy "
            "validation, deny-dominant expansion, claim mint/validate/context)."
        ),
    )
    parser.add_argument(
        "--root",
        metavar="ROOT",
        default=str(Path(__file__).resolve().parent.parent.parent),
        help="canonical repository root (default: this repository)",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    p_show = sub.add_parser("policy-show", help="print the effective policy")
    p_show.add_argument("--json", action="store_true", help="print JSON")
    p_validate = sub.add_parser("policy-validate", help="validate the committed policy")
    p_expand = sub.add_parser("expand", help="deny-dominant expansion of a scope request")
    p_expand.add_argument("scopes", nargs="+", help="requested scope IDs")
    args = parser.parse_args(argv)
    try:
        policy = load_policy_config(Path(args.root))
    except PathLeaseError as exc:
        print(f"factory-path-lease: {exc}", file=sys.stderr)
        return 2
    if args.command == "policy-validate":
        print(
            f"policy scopes={','.join(s.scope_id for s in policy.scopes) or '-'} "
            f"deny_paths={len(policy.deny_paths)} "
            f"deny_patterns={len(policy.deny_patterns)}"
        )
        return 0
    if args.command == "expand":
        try:
            scopes, paths, patterns, audit = expand_request(args.scopes, policy)
        except PathLeaseError as exc:
            print(f"factory-path-lease: {exc}", file=sys.stderr)
            return 2
        print(
            f"granted_scopes={','.join(scopes) or '-'} "
            f"granted_paths={','.join(paths) or '-'} "
            f"granted_patterns={','.join(patterns) or '-'} "
            f"audit_required={audit}"
        )
        return 0
    if args.json:
        print(json.dumps({
            "schema": SCHEMA_NAME,
            "scopes": {
                scope.scope_id: {
                    "paths": list(scope.paths),
                    "patterns": list(scope.patterns),
                    "audit_required": scope.audit_required,
                }
                for scope in policy.scopes
            },
            "deny": {
                "paths": list(policy.deny_paths),
                "patterns": list(policy.deny_patterns),
            },
        }, sort_keys=True, separators=(",", ":")))
        return 0
    print(
        f"scopes={','.join(s.scope_id for s in policy.scopes) or '-'} "
        f"deny_paths={','.join(policy.deny_paths) or '-'} "
        f"deny_patterns={','.join(policy.deny_patterns) or '-'}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(_cli(sys.argv[1:]))
