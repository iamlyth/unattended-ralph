#!/usr/bin/env python3
"""Strict structured verifier-failure artifact (EVID-02).

The trusted control plane records every deterministic verifier failure as a
strict structured artifact (schema ``factory-verifier-failure/v1``) carrying
the exact command, the exact exit status, expected vs observed, a bounded
relevant output tail and/or a bounded output reference, changed files,
artifact references, an environment/capability classification, and a rerun
scope.  The artifact is the structured-handoff foundation for the
implementer-owned inspect/edit/test/diagnose loop: a later implementer
converges on a failing verifier from the exact command, the exact status,
the expected/observed delta, and the bounded output tail — never from
unstructured prose.

Trust and data boundaries (EVID-02):

* **Strings are data, never executable authority.**  The ``command`` field
  is an exact argv *record* of what the trusted control plane invoked; no
  consumer may re-execute it from this artifact, and the artifact itself
  never carries a path/argv authority.  ``output_ref``/``artifact_refs`` are
  bounded references to already-produced evidence artifacts, never
  executable paths.
* **Bounded sizes.**  Every string and array is size-bounded by the
  committed schema and re-checked by the validator; an oversized artifact
  fails closed before any consumer reads it.
* **Closed enums.**  ``phase``, ``environment_classification``,
  ``capability_classification``, and ``rerun_scope`` are closed enums; an
  unknown value fails closed.
* **Duplicate-key rejection.**  Parsing uses an ``object_pairs_hook`` that
  rejects any repeated JSON object key, so a forged artifact cannot hide a
  drifted field behind a duplicate.
* **Exact-commit binding.**  ``commit`` is the exact 40-hex commit the
  verifier ran at; a stale or forged commit fails closed.

The artifact is evidence, never orchestration state: it is produced by the
trusted control plane, validated against the committed schema, and consumed
only as a structured handoff.  It never weakens round-zero readiness,
infrastructure-failure fail-closed closes, or human authority.
"""

from __future__ import annotations

import json
import os
import re
import stat
from pathlib import Path
from typing import Callable, Dict, List, Mapping, Optional, Sequence

SCHEMA_NAME = "factory-verifier-failure/v1"
SCHEMA_FILE = "factory-verifier-failure-v1.schema.json"
MAX_ARTIFACT_BYTES = 256 * 1024

SAFE_CAMPAIGN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
SHA40_RE = re.compile(r"^[0-9a-f]{40}$")

# Closed enums (EVID-02): an unknown value fails closed.
PHASES = ("verification", "audit")
ENVIRONMENT_CLASSIFICATIONS = ("clean", "dirty", "foreign", "unavailable")
CAPABILITY_CLASSIFICATIONS = (
    "available", "unavailable", "skipped", "undeclared", "not_required",
)
RERUN_SCOPES = ("full", "targeted", "none")

# Bounded-size ceilings (EVID-02): enforced by the validator in addition to
# the committed JSON schema, so a forged oversized artifact fails closed even
# if a consumer skips the schema.
MAX_COMMAND_ITEMS = 64
MAX_COMMAND_ARG_BYTES = 4096
MAX_TEXT_BYTES = 4096
MAX_OUTPUT_TAIL_BYTES = 65536
MAX_REF_BYTES = 1024
MAX_CHANGED_FILES = 256
MAX_ARTIFACT_REFS = 64


class VerifierFailureError(Exception):
    """A verifier-failure artifact was forged, unsafe, or malformed."""


class VerifierFailureMalformedError(VerifierFailureError):
    """The artifact violates the committed schema or a bounded-size rule."""


def _reject_duplicate_keys(pairs: List[tuple]) -> Dict[str, object]:
    """JSON object-pairs hook: reject any repeated object key.

    A duplicate key silently overwrites its predecessor under a plain
    ``dict`` decode and can hide a drifted authority; the acceptance
    boundary rejects it instead (EVID-02).
    """
    result: Dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise VerifierFailureMalformedError(
                f"duplicate JSON object key: {key!r}"
            )
        result[key] = value
    return result


# Control characters (C0 controls plus DEL) are never valid in a reference.
_CONTROL_CHARS = frozenset(chr(c) for c in range(0x20)) | frozenset(chr(0x7F))


def _validate_safe_ref(value: str, name: str) -> None:
    """Reject unsafe reference/path syntax in a verifier-failure ref.

    ``output_ref``/``artifact_refs``/``changed_files`` are bounded
    repository-relative references to already-produced evidence artifacts,
    never executable paths (EVID-02).  A value that is absolute, contains a
    backslash or a control character, or contains a dot/dotdot/empty path
    segment fails closed.  A bare non-path identifier (for example a digest
    or a single-segment artifact name) is preserved: it is not a path and
    carries no traversal or absolute authority.
    """
    if not value:
        raise VerifierFailureMalformedError(
            f"verifier-failure {name} must be a non-empty string"
        )
    if value.startswith("/"):
        raise VerifierFailureMalformedError(
            f"verifier-failure {name} must be a repository-relative reference, "
            "not an absolute path"
        )
    if "\\" in value:
        raise VerifierFailureMalformedError(
            f"verifier-failure {name} must not contain a backslash"
        )
    if any(ch in _CONTROL_CHARS for ch in value):
        raise VerifierFailureMalformedError(
            f"verifier-failure {name} must not contain control characters"
        )
    for segment in value.split("/"):
        if not segment:
            raise VerifierFailureMalformedError(
                f"verifier-failure {name} must not contain an empty path segment"
            )
        if segment in (".", ".."):
            raise VerifierFailureMalformedError(
                f"verifier-failure {name} must not contain a dot/dotdot path "
                "segment"
            )


def _load_schema() -> Dict[str, object]:
    """Read the committed schema with a no-follow, identity-safe open.

    The schema is a committed control-plane authority: a symlink in any
    component, a non-regular file, a wrong owner, or a group/other-writable
    file fails closed instead of being read (EVID-02 hardening).  The read
    is bounded and the bytes are parsed only after the identity checks pass.
    """
    here = Path(__file__).resolve().parents[1]  # .factory/
    path = here / "schemas" / SCHEMA_FILE
    try:
        descriptor = os.open(
            path,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0),
        )
    except OSError as exc:
        raise VerifierFailureError(
            f"cannot open the committed schema {path}: {exc}"
        ) from exc
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise VerifierFailureError(
                f"the committed schema {path} is not a regular file"
            )
        if info.st_uid != os.getuid() or info.st_mode & 0o022:
            raise VerifierFailureError(
                f"the committed schema {path} is not owned/private; the "
                "schema authority fails closed"
            )
        if info.st_size > 256 * 1024:
            raise VerifierFailureError(f"the committed schema {path} is oversized")
        data = os.read(descriptor, info.st_size + 1)
        if len(data) > 256 * 1024:
            raise VerifierFailureError(f"the committed schema {path} is oversized")
    finally:
        os.close(descriptor)
    try:
        schema = json.loads(data)
    except ValueError as exc:
        raise VerifierFailureError(f"the committed schema {path} is not JSON") from exc
    if not isinstance(schema, dict):
        raise VerifierFailureError(f"the committed schema {path} is not an object")
    return schema


_SCHEMA: Optional[Dict[str, object]] = None


def _schema() -> Dict[str, object]:
    global _SCHEMA
    if _SCHEMA is None:
        _SCHEMA = _load_schema()
    return _SCHEMA


def _json_type(value: object) -> str:
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    if value is None:
        return "null"
    raise VerifierFailureMalformedError(
        f"value of type {type(value).__name__} is not JSON-serializable"
    )


def _check_instance(
    instance: object, schema: object, path: str, context: str
) -> None:
    """Validate ``instance`` against the JSON-Schema subset the committed
    schemas use (type/enum/pattern/minLength/maxLength/minimum/maximum/
    minItems/maxItems/items/properties/required/additionalProperties).  Any
    mismatch fails closed."""
    if not isinstance(schema, dict):
        return
    expected = schema.get("type")
    if expected is not None:
        types = expected if isinstance(expected, list) else [expected]
        if _json_type(instance) not in types:
            raise VerifierFailureMalformedError(
                f"{context} violation at {path or '(root)'}: expected "
                f"{expected!r}, got {_json_type(instance)!r}"
            )
    if "enum" in schema and instance not in schema["enum"]:
        raise VerifierFailureMalformedError(
            f"{context} violation at {path or '(root)'}: value {instance!r} "
            f"is not one of {schema['enum']!r}"
        )
    if isinstance(instance, str):
        if "minLength" in schema and len(instance) < schema["minLength"]:
            raise VerifierFailureMalformedError(
                f"{context} violation at {path or '(root)'}: string below the "
                "minimum length"
            )
        if "maxLength" in schema and len(instance) > schema["maxLength"]:
            raise VerifierFailureMalformedError(
                f"{context} violation at {path or '(root)'}: string above the "
                "maximum length"
            )
        if "pattern" in schema and re.fullmatch(schema["pattern"], instance) is None:
            raise VerifierFailureMalformedError(
                f"{context} violation at {path or '(root)'}: pattern mismatch"
            )
    if isinstance(instance, (int, float)) and not isinstance(instance, bool):
        if "minimum" in schema and instance < schema["minimum"]:
            raise VerifierFailureMalformedError(
                f"{context} violation at {path or '(root)'}: value below minimum"
            )
        if "maximum" in schema and instance > schema["maximum"]:
            raise VerifierFailureMalformedError(
                f"{context} violation at {path or '(root)'}: value above maximum"
            )
    if isinstance(instance, list):
        if "minItems" in schema and len(instance) < schema["minItems"]:
            raise VerifierFailureMalformedError(
                f"{context} violation at {path or '(root)'}: array below the "
                "minimum item count"
            )
        if "maxItems" in schema and len(instance) > schema["maxItems"]:
            raise VerifierFailureMalformedError(
                f"{context} violation at {path or '(root)'}: array above the "
                "maximum item count"
            )
        if "items" in schema:
            for index, item in enumerate(instance):
                _check_instance(item, schema["items"], f"{path}[{index}]", context)
    if isinstance(instance, dict):
        if "properties" in schema:
            for key, subschema in schema["properties"].items():
                if key in instance:
                    _check_instance(
                        instance[key], subschema, f"{path}.{key}", context
                    )
        if "required" in schema:
            for key in schema["required"]:
                if key not in instance:
                    raise VerifierFailureMalformedError(
                        f"{context} violation at {path or '(root)'}: missing "
                        f"required field {key!r}"
                    )
        if schema.get("additionalProperties") is False:
            declared = set((schema.get("properties") or {}).keys())
            for key in instance:
                if key not in declared:
                    raise VerifierFailureMalformedError(
                        f"{context} violation at {path or '(root)'}: extra "
                        f"field {key!r}"
                    )


def validate_artifact(artifact: Mapping[str, object]) -> None:
    """Validate one verifier-failure artifact against the committed schema.

    Enforces the exact field set, closed enums, bounded sizes, and the
    semantic data boundaries (command strings are data, never executable
    authority; refs are bounded references).  Any mismatch fails closed.
    """
    if not isinstance(artifact, dict):
        raise VerifierFailureMalformedError(
            "verifier-failure artifact must be a JSON object"
        )
    _check_instance(artifact, _schema(), "", "verifier-failure")
    # Semantic re-checks beyond the JSON schema (defense in depth): the
    # bounded-size ceilings and the closed enums are enforced here too, so a
    # schema that drifts can never silently widen the boundary.
    campaign_id = artifact.get("campaign_id")
    if not isinstance(campaign_id, str) or not SAFE_CAMPAIGN_ID_RE.fullmatch(
        campaign_id
    ):
        raise VerifierFailureMalformedError(
            "verifier-failure campaign_id is invalid"
        )
    commit = artifact.get("commit")
    if not isinstance(commit, str) or not SHA40_RE.fullmatch(commit):
        raise VerifierFailureMalformedError(
            "verifier-failure commit must be a 40-hex commit"
        )
    phase = artifact.get("phase")
    if phase not in PHASES:
        raise VerifierFailureMalformedError(
            f"verifier-failure phase must be one of {PHASES!r}"
        )
    command = artifact.get("command")
    if (
        not isinstance(command, list)
        or not command
        or len(command) > MAX_COMMAND_ITEMS
        or any(
            not isinstance(arg, str) or not arg or len(arg) > MAX_COMMAND_ARG_BYTES
            for arg in command
        )
    ):
        raise VerifierFailureMalformedError(
            "verifier-failure command must be a non-empty bounded argv record"
        )
    exit_status = artifact.get("exit_status")
    if (
        isinstance(exit_status, bool)
        or not isinstance(exit_status, int)
        or not (-128 <= exit_status <= 255)
    ):
        raise VerifierFailureMalformedError(
            "verifier-failure exit_status must be an integer in [-128, 255]"
        )
    for name in ("expected", "observed"):
        value = artifact.get(name)
        if not isinstance(value, str) or not value or len(value) > MAX_TEXT_BYTES:
            raise VerifierFailureMalformedError(
                f"verifier-failure {name} must be a non-empty bounded string"
            )
    output_tail = artifact.get("output_tail", "")
    if not isinstance(output_tail, str) or len(output_tail) > MAX_OUTPUT_TAIL_BYTES:
        raise VerifierFailureMalformedError(
            "verifier-failure output_tail must be a bounded string"
        )
    for name in ("output_ref",):
        value = artifact.get(name, "")
        if not isinstance(value, str) or len(value) > MAX_REF_BYTES:
            raise VerifierFailureMalformedError(
                f"verifier-failure {name} must be a bounded string"
            )
        if value:
            _validate_safe_ref(value, name)
    for name, ceiling in (
        ("changed_files", MAX_CHANGED_FILES),
        ("artifact_refs", MAX_ARTIFACT_REFS),
    ):
        values = artifact.get(name, [])
        if (
            not isinstance(values, list)
            or len(values) > ceiling
            or any(
                not isinstance(item, str) or not item or len(item) > MAX_REF_BYTES
                for item in values
            )
        ):
            raise VerifierFailureMalformedError(
                f"verifier-failure {name} must be a bounded string array"
            )
        for item in values:
            _validate_safe_ref(item, name)
    environment = artifact.get("environment_classification")
    if environment not in ENVIRONMENT_CLASSIFICATIONS:
        raise VerifierFailureMalformedError(
            "verifier-failure environment_classification is not a closed enum value"
        )
    capability = artifact.get("capability_classification")
    if capability not in CAPABILITY_CLASSIFICATIONS:
        raise VerifierFailureMalformedError(
            "verifier-failure capability_classification is not a closed enum value"
        )
    rerun = artifact.get("rerun_scope")
    if rerun not in RERUN_SCOPES:
        raise VerifierFailureMalformedError(
            "verifier-failure rerun_scope is not a closed enum value"
        )


def artifact_bytes(artifact: Mapping[str, object]) -> bytes:
    """The deterministic canonical artifact bytes (sorted JSON, no newline)."""
    validate_artifact(artifact)
    return json.dumps(
        dict(artifact), sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")


def parse_artifact(raw: bytes) -> Dict[str, object]:
    """Parse raw artifact bytes with duplicate-key rejection and validate.

    A repeated JSON object key, an oversized document, non-UTF-8 bytes, or
    any schema/enum/bound violation fails closed (EVID-02).
    """
    if not isinstance(raw, bytes) or len(raw) > MAX_ARTIFACT_BYTES:
        raise VerifierFailureMalformedError(
            "verifier-failure artifact is oversized or not bytes"
        )
    try:
        text = raw.decode("utf-8")
    except UnicodeError as exc:
        raise VerifierFailureMalformedError(
            "verifier-failure artifact is not UTF-8"
        ) from exc
    try:
        data = json.loads(text, object_pairs_hook=_reject_duplicate_keys)
    except json.JSONDecodeError as exc:
        raise VerifierFailureMalformedError(
            f"verifier-failure artifact is not valid JSON: {exc}"
        ) from exc
    validate_artifact(data)
    return data


def validate_commit_context(
    artifact: Mapping[str, object],
    *,
    expected_commit: str,
    root: Path,
    resolver: Optional[Callable[[str], bool]] = None,
) -> None:
    """Validate the artifact's commit against the expected bound commit and
    repository resolvability (EVID-02 contextual binding).

    The artifact records the exact commit the verifier ran at.  A consumer
    that knows the expected bound commit (the commit the trusted control
    plane launched the verifier at) must reject an artifact whose ``commit``
    differs from that expected commit, and must reject a commit the
    repository cannot resolve.  ``resolver`` is the pinned-Git resolvability
    probe (``True`` when the 40-hex commit exists in the repository); when
    it is omitted the artifact's own 40-hex syntax check is the only
    resolvability gate, so callers that cannot prove repository
    resolvability fail closed by refusing to accept the artifact as
    contextually bound.
    """
    if not isinstance(expected_commit, str) or not SHA40_RE.fullmatch(
        expected_commit
    ):
        raise VerifierFailureMalformedError(
            "the expected bound commit must be a 40-hex commit hash"
        )
    commit = artifact.get("commit")
    if not isinstance(commit, str) or not SHA40_RE.fullmatch(commit):
        raise VerifierFailureMalformedError(
            "verifier-failure commit must be a 40-hex commit hash"
        )
    if commit != expected_commit:
        raise VerifierFailureMalformedError(
            f"verifier-failure commit {commit!r} does not match the expected "
            f"bound commit {expected_commit!r}; a stale or forged artifact "
            "fails closed"
        )
    if resolver is not None:
        try:
            resolvable = bool(resolver(commit))
        except Exception as exc:
            raise VerifierFailureError(
                f"cannot probe repository resolvability of {commit}: {exc}"
            ) from exc
        if not resolvable:
            raise VerifierFailureMalformedError(
                f"verifier-failure commit {commit!r} is not resolvable in the "
                "repository; a stale or forged artifact fails closed"
            )
    elif not Path(root).is_dir():
        raise VerifierFailureError(
            f"cannot validate the verifier-failure commit context: the "
            f"repository root {root!r} is not a directory"
        )


def build_artifact(
    *,
    campaign_id: str,
    phase: str,
    commit: str,
    command: Sequence[str],
    exit_status: int,
    expected: str,
    observed: str,
    output_tail: str = "",
    output_ref: str = "",
    changed_files: Sequence[str] = (),
    artifact_refs: Sequence[str] = (),
    environment_classification: str = "clean",
    capability_classification: str = "not_required",
    rerun_scope: str = "full",
) -> Dict[str, object]:
    """Build a schema-valid verifier-failure artifact from trusted inputs.

    The trusted control plane calls this when a deterministic verifier
    fails; the returned artifact is validated before it is returned, so a
    caller can never mint an invalid record.  ``command`` is recorded as
    data (the exact argv the control plane invoked) and is never an
    executable authority.
    """
    artifact: Dict[str, object] = {
        "schema": SCHEMA_NAME,
        "campaign_id": campaign_id,
        "phase": phase,
        "commit": commit,
        "command": list(command),
        "exit_status": exit_status,
        "expected": expected,
        "observed": observed,
        "environment_classification": environment_classification,
        "capability_classification": capability_classification,
        "rerun_scope": rerun_scope,
    }
    # Optional fields are omitted when empty: the schema requires a
    # non-empty string whenever a field is present, so an empty optional
    # field is represented by absence, never by an empty string.
    if output_tail:
        artifact["output_tail"] = output_tail
    if output_ref:
        artifact["output_ref"] = output_ref
    if changed_files:
        artifact["changed_files"] = list(changed_files)
    if artifact_refs:
        artifact["artifact_refs"] = list(artifact_refs)
    validate_artifact(artifact)
    return artifact
