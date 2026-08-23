#!/usr/bin/env python3
"""Fresh-context execution, invocation contract, and supervision (Task 6).

This module implements the fresh-context boundary of
``docs/FACTORY-LOOP-SPEC.md`` (CTX-01, TASK-02, PROC-01; §5, §9, §12, §17,
§20) as the trusted control-plane module ``.factory/loop/launch.py``.  It
is the deterministic Task-6 deliverable:

* **Fresh process per role.** Every role attempt starts a *new* process via
  the existing secure wrapper (``scripts/pi2-secure-exec.py``, invoked never
  reimplemented) in one-shot mode: a new process session/group, no resumed
  session, no session storage shared with any previous loop identity, no
  automatic memory injection, and only allowlisted prompt inputs.
* **Exact invocation contract (§20)**: the :class:`Invocation` binding names
  the exact model/provider, static role-prompt digest, campaign-bound
  prompt-set digest, deterministic audit-objective digest (auditor only),
  canonical workspace and bound commit, selected task id with an excerpt
  whose bytes are re-derived from the committed plan and digest-matched
  (fail closed on substitution, paraphrase, or another plan revision),
  allowed tools, and runtime/inactivity bounds.  The child argv is built
  only from these fields (plus fixed structural one-shot flags) and the
  child environment is rebuilt from an explicit allowlist plus the
  documented ``FACTORY_LOOP_LAUNCH_*`` invocation fields — never from the
  parent's environment wholesale, so credentials and legacy
  lock/Git/context variables cannot leak into a leaf.
* **Descendant-scoped supervision (review-owned F6/F7)**.  The supervisor
  snapshots the role's *own* live descendant closure once
  (:func:`lock.capture_descendants`) with every PID pinned to its starttime
  and parent (:class:`lock.CapturedProcess`) and re-enumerates only that
  captured scope (:func:`lock.live_scope`) — descendant accounting is never
  stale and a PID reused by an unrelated process is never treated as a
  descendant.  The captured scope is supplied to the Task-5 lock-detector
  API (:func:`lock.detect_escaped_descendants`) so the escaped-descendant
  verdict is exact.
* **Subreaper / reaping (F7)**.  The supervisor installs itself as a child
  subreaper *before* spawning, so a double-fork or ``setsid`` descendant
  that orphans is reparented to the supervisor and is always reaped when it
  dies; an escaped descendant that survives bounded termination — including
  a reparented survivor discovered through its parent identity — fails
  closed with :class:`EscapedDescendantError` for operator inspection
  (§12).  The launch snapshot guards the crash-before-snapshot window (a
  leader that dies before its scope can be captured cannot fork further and
  every prior descendant is reparented to the subreaper) and the captured
  starttime identity makes PID reuse harmless.
* **Bounded termination**.  A hard runtime limit and an inactivity limit
  bound every run.  Termination delivers **TERM, INT, and HUP to the full
  process group**, observes a bounded grace, escalates to **KILL** of the
  whole group, then verifies the group is gone and reaps the leader within
  a bound.  Escaped descendants and un-reaped groups fail closed.
* **Lock boundary**.  The child inherits no lock descriptor (``close_fds``,
  no ``pass_fds``, close-on-exec) and no lock/Git metadata; the invariants
  are re-verified against ``/proc/<pid>`` per launch (session identity,
  environment strip, no root-inode descriptor).
* **Dirty work preservation**.  Supervision never touches the workspace
  contents; a crashed or interrupted attempt leaves its dirty work intact
  and never silently overwrites it.
* **Structured bounded results**.  The only completion signal is the
  machine-readable :class:`LaunchResult` (exit status, signal, reason,
  bounded per-stream digest/tail, snapshot counts) — never a model
  completion token and never raw unbounded output, so no credential
  material can be carried in a result.
"""

from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import math
import os
from pathlib import Path
import re
import select
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
from dataclasses import dataclass, field, replace
from typing import Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

try:  # package-import mode (the hidden control-plane package)
    from . import confinement as confinement_authority
    from . import workspace_confinement as real_confinement_authority
    from . import usage as usage_guard
    from .gitutil import (
        GIT_ENV_STRIP,
        GitBoundaryError,
        git_bytes,
        require_trusted_executable,
        resolve_head,
        sanitize_git_environment,
    )
    from .lock import (
        CapturedProcess,
        EscapedDescendantError,
        RootLockError,
        RootLockUnsafeError,
        _is_live_with_identity,
        _iter_pids,
        _proc_stat_fields,
        capture_descendants,
        detect_escaped_descendants,
        live_scope,
        stripped_child_env,
    )
    from .plan_parser import Plan, PlanError, parse_plan
except ImportError:  # flat-import mode used by the hidden harness test suite
    import confinement as confinement_authority  # type: ignore[no-redef]
    import workspace_confinement as real_confinement_authority  # type: ignore[no-redef]
    import usage as usage_guard  # type: ignore[no-redef]
    from gitutil import (  # type: ignore[no-redef]
        GIT_ENV_STRIP,
        GitBoundaryError,
        git_bytes,
        require_trusted_executable,
        resolve_head,
        sanitize_git_environment,
    )
    from lock import (  # type: ignore[no-redef]
        CapturedProcess,
        EscapedDescendantError,
        RootLockError,
        RootLockUnsafeError,
        _is_live_with_identity,
        _iter_pids,
        _proc_stat_fields,
        capture_descendants,
        detect_escaped_descendants,
        live_scope,
        stripped_child_env,
    )
    from plan_parser import Plan, PlanError, parse_plan  # type: ignore[no-redef]

__all__ = [
    "BLOB_READ_CHUNK",
    "CONFINE_LAUNCHER",
    "CONFINEMENT_SPEC_SCHEMA",
    "DEFAULT_ALLOWED_TOOLS",
    "DEFAULT_INACTIVITY_LIMIT",
    "DEFAULT_RUNTIME_LIMIT",
    "ENV_ALLOWLIST",
    "GIT_BLOB_TIMEOUT",
    "INVOCATION_ENV_PREFIX",
    "InvocationBinding",
    "InvocationError",
    "LaunchAuthority",
    "LaunchError",
    "LaunchResult",
    "LaunchSupervision",
    "authorize_launch",
    "require_trusted_interpreter",
    "OUTPUT_DIGEST_CAP",
    "OUTPUT_TAIL_CAP",
    "PROMPT_INPUT_MAX",
    "PROMPT_MAX_BYTES",
    "RESULT_SCHEMA_FILE",
    "RESULT_SCHEMA_NAME",
    "ROLES",
    "SECURE_WRAPPER",
    "StreamResult",
    "SupervisionError",
    "SupervisionSignalInterrupt",
    "SUPPORTED_PROVIDERS",
    "TASK_HEADING_RE",
    "TERMINATION_SIGNALS",
    "child_argv",
    "child_environment",
    "compose_prompt",
    "derive_task_excerpt",
    "secure_wrapper_path",
    "task_excerpt_bytes",
    "task_excerpt_digest",
    "validate_launch_result",
    "verify_child_env",
    "verify_invocation",
    "verify_task_excerpt",
    "write_prompt_file",
]

# --------------------------------------------------------------------------
# Constants
# --------------------------------------------------------------------------

# The four static roles (FACTORY-LOOP-SPEC §6); no adaptive subroles exist.
ROLES = ("planner", "developer", "tester", "auditor")

# Task 8 confined launch: the child process runs through the committed
# confine launcher, which applies the exact Landlock confinement specification
# (schema ``factory-confinement/v1``) before exec'ing the secure wrapper.  The
# launcher is staged from its exact committed blob like the wrapper (F2).
CONFINE_LAUNCHER = ".factory/loop/confine_launcher.py"
STAGED_CONFINE_LAUNCHER_NAME = "confine_launcher.py"
CONFINEMENT_SPEC_SCHEMA = "factory-confinement/v1"
MAX_CONFINEMENT_SPEC_BYTES = 1024 * 1024

# Strict known-provider registry (Task 7 review, obligation 5):
# ``verify_invocation`` rejects any provider outside this set, so an unknown
# or caller-claimed provider fails closed and can never bypass the per-policy
# guard.  ``ollama`` is the retained §10 provider (guard + Task 8
# confinement proof required before invocation); ``synthetic`` is the
# hermetic test provider used only by the hidden suite (no network, no
# guard, no real model backend).
SUPPORTED_PROVIDERS = frozenset({"ollama", "synthetic"})

# Per-provider guard policy: every supported provider/model pair is gated
# per this table — no provider/model can bypass the guard by relabeling.
# ``ollama``: the §10 decision table runs inside ``authorize_launch`` and
# the invocation fails closed without a Task 8 confinement proof.
# ``synthetic``: hermetic test provider; the guard is not applicable.
PROVIDER_GUARD_REQUIRED = frozenset({"ollama"})

# The existing secure wrapper — invoked, never reimplemented (§18).
SECURE_WRAPPER = "scripts/pi2-secure-exec.py"

# Hard bounds: the wrapper itself caps the prompt at 4 MiB; the composition
# layer enforces a smaller bound so the assembled prompt can never approach
# the wrapper's limit, and each allowlisted input is independently bounded.
PROMPT_MAX_BYTES = 4 * 1024 * 1024
PROMPT_INPUT_MAX = 1024 * 1024

# Result bounds: each child stream keeps a bounded tail for operator
# inspection and a deterministic digest over a bounded prefix, so a result is
# never unbounded and cannot smuggle credentials through raw output.
OUTPUT_TAIL_CAP = 128 * 1024
OUTPUT_DIGEST_CAP = 1024 * 1024

# Default bounds (§9: "run under a hard runtime limit").  The control plane
# binds these per invocation; the defaults are conservative and documented.
DEFAULT_RUNTIME_LIMIT = 3600.0
DEFAULT_INACTIVITY_LIMIT = 600.0

# Termination sequence: TERM, INT, and HUP are each delivered to the *full
# process group* before the bounded grace expires and KILL escalates (§9).
TERMINATION_SIGNALS = (signal.SIGTERM, signal.SIGINT, signal.SIGHUP)

# TOCTOU-free verify-to-exec (F2): the exact committed wrapper/backend bytes
# are staged into a private mode-0700 directory as mode-0500 single-link
# files (owner read+execute, no write) and the child executes only those
# staged paths — never a working-tree pathname that could be swapped after
# verification.  An *external* trusted executable is not staged (its bytes
# are not committed): its fully resolved path (including the containing
# directories of every symlink target) is revalidated immediately at exec.
EXEC_STAGING_PREFIX = "factory-loop-exec-"
STAGED_WRAPPER_NAME = "pi2-secure-exec.py"
STAGED_BACKEND_NAME = "backend"
STAGED_FILE_MODE = 0o500
STAGED_DIR_MODE = 0o700
DEFAULT_KILL_GRACE = 1.0
REAP_TIMEOUT = 2.0
GROUP_GONE_TIMEOUT = 2.0
# Bounded /proc read-back window for the per-launch invariants (session id
# appears at fork, environ at exec).
INVARIANT_READBACK_WINDOW = 1.0

# F5: every authoritative blob read (role prompt, policy, spec, plan,
# wrapper, backend) is fd-anchored, no-follow, and chunk-bounded.
BLOB_READ_CHUNK = 65536
# Bounded pinned-Git blob reads (`git show <bound>:<relpath>`).
GIT_BLOB_TIMEOUT = 30.0

# The committed machine-result schema (hidden namespace, schema
# ``factory-launch-result/v1``).
RESULT_SCHEMA_NAME = "factory-launch-result/v1"
RESULT_SCHEMA_FILE = "factory-launch-result-v1.schema.json"

# Invocation metadata environment prefix carried into the child (stripped
# from nothing — the child environment is *rebuilt* from the allowlist plus
# exactly these fields, never inherited).
INVOCATION_ENV_PREFIX = "FACTORY_LOOP_LAUNCH_"

# Explicit environment allowlist for model children.  The child environment
# is *constructed* from these benign keys only (when present in the parent)
# plus the ``FACTORY_LOOP_LAUNCH_*`` invocation fields; nothing else —
# in particular no ``PI_*`` session/memory variable, no ``OLLAMA_*``
# credential/session variable, no ``GIT_CONFIG*``/``GIT_DIR`` redirector,
# no lock metadata — is inherited.  This is the exact-env allowlist contract.
ENV_ALLOWLIST = (
    "PATH", "HOME", "USER", "LOGNAME", "LANG", "LC_ALL", "LC_CTYPE",
    "LC_COLLATE", "LC_MESSAGES", "LC_MONETARY", "LC_NUMERIC", "LC_TIME",
    "TERM", "TZ", "SHELL", "XDG_RUNTIME_DIR", "XDG_CONFIG_HOME",
    "XDG_CACHE_HOME", "XDG_DATA_HOME", "NO_COLOR", "CLICOLOR",
    "CLICOLOR_FORCE",
)

# Model backend flags that must *never* appear in the constructed argv: they
# would resume a session, continue a conversation, or select session state,
# violating the fresh-context contract (§9, §20).
FORBIDDEN_BACKEND_FLAGS = (
    "--resume", "-r", "--continue", "-c", "--session", "--session-id",
    "--fork",
)

SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
SHA40_RE = re.compile(r"^[0-9a-f]{40}$")
TOOL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
# A heading line of a committed task section: ``## Task N: title``.
TASK_HEADING_RE = re.compile(r"^## Task\s+(\d+):\s*\S.*$")

# Default allowed-tool sets per static role (§6).  The operator may override
# per invocation; the binding passes the exact list to the backend.
DEFAULT_ALLOWED_TOOLS = {
    "planner": ("read", "bash", "edit", "write"),
    "developer": ("read", "bash", "edit", "write"),
    "tester": ("read", "bash"),
    "auditor": ("read", "bash"),
}


# --------------------------------------------------------------------------
# Exceptions
# --------------------------------------------------------------------------

class InvocationError(Exception):
    """A binding field, delivered input, or exact-argv/env contract is invalid.

    Raised before the child is spawned (fail closed): a malformed binding, a
    digest mismatch (substituted, paraphrased, or foreign input bytes), an
    unknown task id, a forbidden backend flag, or a credential/legacy key
    that would leak into the child environment.
    """


class LaunchError(InvocationError):
    """A fresh-process launch failed before supervision could begin."""


class SupervisionError(LaunchError):
    """Supervision failed closed (group not reaped, invariants broken, ...)."""


class SupervisionSignalInterrupt(SupervisionError):
    """The supervisor received TERM/INT/HUP during an attempt (F3).

    The group was bounded-terminated (TERM → INT → HUP → KILL) and reaped
    before this is raised, so no child is left running; ``result`` carries
    the fully constructed :class:`LaunchResult` of the interrupted attempt
    and ``signum`` the received signal, so a caller can exit with the
    conventional ``128 + signum`` status after restoring normal signal
    disposition.
    """

    def __init__(
        self,
        signum: int,
        result: Optional["LaunchResult"] = None,
    ) -> None:
        self.signum = signum
        self.result = result
        super().__init__(
            f"supervisor received {signal.Signals(signum).name} during the "
            "attempt; the process group was bounded-terminated and reaped"
        )


class ResultSchemaError(LaunchError):
    """A launch result does not conform to the committed result schema.

    The only completion signal of a supervised attempt is the machine-
    readable exit status; its JSON field set must conform to the committed
    ``factory-launch-result/v1`` schema (:data:`RESULT_SCHEMA_FILE`), so a
    result that gains, drops, or mistypes a field fails closed.
    """


# --------------------------------------------------------------------------
# Invocation binding
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class InvocationBinding:
    """The exact §20 invocation contract for one role attempt.

    Every field is bound at construction and validated by
    :func:`verify_invocation`; a malformed binding can never reach launch.
    ``task_id``/``task_excerpt_digest`` are required for the ``developer``
    role and absent otherwise; ``audit_objective_digest`` is required for the
    ``auditor`` role and empty otherwise.  ``runtime_limit`` and
    ``inactivity_limit`` are the supervisor's hard and inactivity bounds.
    """

    role: str
    model: str
    provider: str
    backend: Path
    workspace: Path
    bound_commit: str
    role_prompt_digest: str
    prompt_set_digest: str
    plan_digest: str
    policy_digest: str
    specification_digest: str
    allowed_tools: Tuple[str, ...] = ()
    task_id: Optional[int] = None
    task_excerpt_digest: Optional[str] = None
    audit_objective_digest: str = ""
    findings_digest: str = ""
    runtime_limit: float = DEFAULT_RUNTIME_LIMIT
    inactivity_limit: float = DEFAULT_INACTIVITY_LIMIT


def verify_invocation(binding: InvocationBinding) -> None:
    """Fail closed on any malformed binding field (never silent defaults)."""
    if binding.role not in ROLES:
        raise InvocationError(
            f"role must be one of {ROLES!r}, got {binding.role!r}"
        )
    provider = binding.provider.lower()
    if provider not in SUPPORTED_PROVIDERS:
        raise InvocationError(
            f"provider must be one of {sorted(SUPPORTED_PROVIDERS)!r}, got "
            f"{binding.provider!r}; an unknown provider fails closed and can "
            "never bypass the per-policy guard (Task 7 review, obligation 5)"
        )
    for name, value in (
        ("model", binding.model),
        ("provider", binding.provider),
        ("bound_commit", binding.bound_commit),
        ("plan_digest", binding.plan_digest),
        ("policy_digest", binding.policy_digest),
        ("specification_digest", binding.specification_digest),
        ("role_prompt_digest", binding.role_prompt_digest),
        ("prompt_set_digest", binding.prompt_set_digest),
    ):
        if not isinstance(value, str) or not value:
            raise InvocationError(f"`{name}` must be a non-empty string")
    for name, value in (
        ("plan_digest", binding.plan_digest),
        ("policy_digest", binding.policy_digest),
        ("specification_digest", binding.specification_digest),
        ("role_prompt_digest", binding.role_prompt_digest),
        ("prompt_set_digest", binding.prompt_set_digest),
    ):
        if not SHA256_RE.fullmatch(value):
            raise InvocationError(f"`{name}` must be a 64-hex SHA-256 digest")
    if not SHA40_RE.fullmatch(binding.bound_commit):
        raise InvocationError("`bound_commit` must be a 40-hex Git commit hash")
    if binding.audit_objective_digest:
        if not SHA256_RE.fullmatch(binding.audit_objective_digest):
            raise InvocationError(
                "`audit_objective_digest` must be a 64-hex SHA-256 digest"
            )
    if binding.findings_digest:
        if not SHA256_RE.fullmatch(binding.findings_digest):
            raise InvocationError(
                "`findings_digest` must be a 64-hex SHA-256 digest"
            )
    if binding.role != "planner" and binding.findings_digest:
        raise InvocationError(
            "`findings_digest` is allowed only for the planner role"
        )
    if binding.role == "auditor" and not binding.audit_objective_digest:
        raise InvocationError("the auditor role requires an audit-objective digest")
    if binding.role != "auditor" and binding.audit_objective_digest:
        raise InvocationError(
            "`audit_objective_digest` is allowed only for the auditor role"
        )
    if binding.role == "developer":
        if binding.task_id is None or binding.task_excerpt_digest is None:
            raise InvocationError(
                "the developer role requires `task_id` and `task_excerpt_digest`"
            )
        if not SHA256_RE.fullmatch(binding.task_excerpt_digest):
            raise InvocationError(
                "`task_excerpt_digest` must be a 64-hex SHA-256 digest"
            )
    else:
        if binding.task_id is not None or binding.task_excerpt_digest is not None:
            raise InvocationError(
                "`task_id`/`task_excerpt_digest` are allowed only for the "
                "developer role"
            )
    if binding.task_id is not None and (
        isinstance(binding.task_id, bool)
        or not isinstance(binding.task_id, int)
        or binding.task_id < 1
    ):
        raise InvocationError("`task_id` must be a positive integer")
    for name, value in (
        ("runtime_limit", binding.runtime_limit),
        ("inactivity_limit", binding.inactivity_limit),
    ):
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or value <= 0
            or math.isnan(value)
            or math.isinf(value)
        ):
            raise InvocationError(
                f"`{name}` must be a finite positive number of seconds; got "
                f"{value!r} (nan/inf/zero/negative bounds are rejected so no "
                "unbounded or never-triggering wait can be configured)"
            )
    if binding.inactivity_limit > binding.runtime_limit:
        raise InvocationError(
            "`inactivity_limit` must not exceed `runtime_limit`"
        )
    for tool in binding.allowed_tools:
        if not isinstance(tool, str) or not TOOL_RE.fullmatch(tool):
            raise InvocationError(
                f"invalid allowed tool {tool!r} in the invocation binding"
            )
    backend = Path(binding.backend)
    if not backend.is_absolute():
        raise InvocationError(
            f"`backend` must be an absolute path, got {str(backend)!r}"
        )
    try:
        info = backend.stat()
    except OSError as exc:
        raise InvocationError(
            f"`backend` {backend} is not reachable: {exc}"
        ) from exc
    if not stat.S_ISREG(info.st_mode):
        raise InvocationError(f"`backend` {backend} is not a regular file")
    workspace = Path(binding.workspace)
    if not workspace.is_absolute():
        raise InvocationError(
            f"`workspace` must be an absolute path, got {workspace!r}"
        )
    try:
        info = workspace.stat()
    except OSError as exc:
        raise InvocationError(
            f"`workspace` {workspace} is not reachable: {exc}"
        ) from exc
    if not stat.S_ISDIR(info.st_mode):
        raise InvocationError(f"`workspace` {workspace} is not a directory")


# --------------------------------------------------------------------------
# Task-excerpt derivation (TASK-02, §9, §20)
# --------------------------------------------------------------------------

def task_excerpt_bytes(plan_bytes: bytes, task_id: int) -> bytes:
    """Re-derive the exact committed bytes of ``## Task <task_id>:``.

    The excerpt is a verbatim byte slice of the committed ``factory-plan/v1``
    document: the task section spans from its ``## Task N:`` heading through
    the last line before the next ``## `` heading (including the section's
    own trailing blank lines), taken from the parsed block model so no
    re-rendering can change a byte.  The parser already guarantees unique,
    contiguous task ids, so a missing or duplicated task fails closed.
    """
    if (
        isinstance(task_id, bool)
        or not isinstance(task_id, int)
        or task_id < 1
    ):
        raise InvocationError(f"`task_id` must be a positive integer, got {task_id!r}")
    if not isinstance(plan_bytes, (bytes, bytearray)):
        raise InvocationError("`plan_bytes` must be bytes")
    try:
        text = plan_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise InvocationError(f"plan bytes are not valid UTF-8: {exc}") from exc
    try:
        plan = parse_plan(text)
    except PlanError as exc:
        raise InvocationError(f"cannot parse the committed plan: {exc}") from exc
    target = None
    for block in plan._blocks:
        if block.heading is None:
            continue
        match = TASK_HEADING_RE.fullmatch(block.heading)
        if match and int(match.group(1)) == task_id:
            target = block
            break
    if target is None:
        raise InvocationError(
            f"the committed plan has no Task {task_id} section"
        )
    section = "\n".join(target.lines)
    # The block model keeps every line, including the section's own trailing
    # blank lines; ``join`` drops exactly the newline that terminates a
    # trailing blank line, so re-append it to make the excerpt the verbatim
    # byte slice of the committed document (a leading blank line is already
    # preserved as an empty list element).  The *final* block of the document
    # is the one exception: its trailing ``""`` element is the artifact of
    # the document's final newline (``split("\\n")`` yields one empty tail
    # element), not a section blank line, so ``join`` already produced the
    # exact bytes through EOF and no extra newline is appended.
    if (
        target.lines
        and target.lines[-1] == ""
        and target is not plan._blocks[-1]
    ):
        section += "\n"
    return section.encode("utf-8")


def task_excerpt_digest(plan_bytes: bytes, task_id: int) -> str:
    """SHA-256 digest of the exact committed Task ``task_id`` section bytes."""
    return hashlib.sha256(task_excerpt_bytes(plan_bytes, task_id)).hexdigest()


def derive_task_excerpt(plan_bytes: bytes, task_id: int) -> Tuple[bytes, str]:
    """Derive the committed task bytes and their digest in one call."""
    excerpt = task_excerpt_bytes(plan_bytes, task_id)
    return excerpt, hashlib.sha256(excerpt).hexdigest()


def verify_task_excerpt(
    excerpt_bytes: bytes,
    expected_digest: str,
    *,
    task_id: Optional[int] = None,
) -> None:
    """Fail closed unless the delivered excerpt bytes carry ``expected_digest``.

    This is the substitution/paraphrase boundary (§9): the harness records
    the digest of the committed task section in the invocation binding and
    calls this before the bytes enter the prompt; any delivered bytes whose
    digest differs — a substituted task, a paraphrase, or a slice of another
    plan revision — are refused.
    """
    if not isinstance(excerpt_bytes, (bytes, bytearray)):
        raise InvocationError("`excerpt_bytes` must be bytes")
    if not SHA256_RE.fullmatch(expected_digest):
        raise InvocationError("`expected_digest` must be a 64-hex SHA-256 digest")
    digest = hashlib.sha256(excerpt_bytes).hexdigest()
    if digest != expected_digest:
        detail = f" for task {task_id}" if task_id is not None else ""
        raise InvocationError(
            f"task-excerpt digest mismatch{detail}: delivered bytes "
            f"{digest[:16]}… != bound {expected_digest[:16]}…; the delivered "
            "task bytes are substituted, paraphrased, or from another plan "
            "revision"
        )


# --------------------------------------------------------------------------
# Prompt composition (allowlisted inputs only, §5.1, §9)
# --------------------------------------------------------------------------

def _verify_input_digest(label: str, data: bytes, digest: str) -> None:
    if not isinstance(data, (bytes, bytearray)):
        raise InvocationError(f"`{label}` bytes must be bytes")
    if len(data) > PROMPT_INPUT_MAX:
        raise InvocationError(
            f"{label} exceeds the {PROMPT_INPUT_MAX}-byte prompt input bound "
            f"({len(data)} bytes)"
        )
    if not SHA256_RE.fullmatch(digest):
        raise InvocationError(f"`{label}` digest must be a 64-hex SHA-256 digest")
    actual = hashlib.sha256(data).hexdigest()
    if actual != digest:
        raise InvocationError(
            f"{label} digest mismatch: delivered bytes {actual[:16]!r} != "
            f"bound {digest[:16]!r}; the input is substituted, paraphrased, "
            "or belongs to another commit"
        )


def compose_prompt(
    binding: InvocationBinding,
    *,
    role_prompt: bytes,
    agents: bytes,
    spec: bytes,
    plan: bytes,
    audit_objective: Optional[bytes] = None,
    task_excerpt: Optional[bytes] = None,
    findings: Optional[bytes] = None,
) -> bytes:
    """Assemble the fresh-context prompt from the allowlisted inputs only.

    The prompt is a bounded deterministic document that contains exactly the
    authoritative §5.1 inputs: the static role prompt, ``AGENTS.md``, the
    canonical product specification, the canonical implementation plan, the
    audit objective (auditor only), the exact selected task excerpt
    (developer only), and the deterministic receipt-backed findings payload
    of the previous round (planner only, Task 10 §16).  Every input's bytes
    are digest-verified against the binding (a substituted or paraphrased
    input fails closed) and every input is independently bounded.  No
    session id, memory, scratchpad, task-queue, or historical-conversation
    content is ever composed.
    """
    verify_invocation(binding)
    _verify_input_digest("role prompt", role_prompt, binding.role_prompt_digest)
    _verify_input_digest("operational policy", agents, binding.policy_digest)
    _verify_input_digest("specification", spec, binding.specification_digest)
    _verify_input_digest("implementation plan", plan, binding.plan_digest)

    sections: List[bytes] = [
        b"# Factory role context",
        b"",
        b"Role: " + binding.role.encode("ascii"),
        b"Fresh process: this context is created for this attempt only; "
        b"session resume and automatic memory injection are disabled.",
        b"",
        b"## Role prompt (digest " + binding.role_prompt_digest.encode("ascii") + b")",
        role_prompt,
    ]
    sections.append(b"")
    sections.append(
        b"## Operational policy (AGENTS.md, digest "
        + binding.policy_digest.encode("ascii")
        + b")"
    )
    sections.append(agents)
    sections.append(b"")
    sections.append(
        b"## Product specification (digest "
        + binding.specification_digest.encode("ascii")
        + b")"
    )
    sections.append(spec)
    sections.append(b"")
    sections.append(
        b"## Implementation plan (digest "
        + binding.plan_digest.encode("ascii")
        + b")"
    )
    sections.append(plan)
    if binding.role == "developer":
        if task_excerpt is None:
            raise InvocationError(
                "the developer prompt requires the exact task-excerpt bytes"
            )
        verify_task_excerpt(
            task_excerpt, binding.task_excerpt_digest, task_id=binding.task_id
        )
        # §9: the excerpt must be the committed section of THIS plan; re-derive
        # and compare so a caller-built paraphrase can never slip through even
        # when its digest collided.
        derived = task_excerpt_bytes(plan, binding.task_id)
        if task_excerpt != derived:
            raise InvocationError(
                "the delivered task-excerpt bytes differ from the committed "
                f"plan section Task {binding.task_id}; refusing a "
                "substituted/paraphrased task"
            )
        sections.append(b"")
        sections.append(
            b"## Selected task excerpt (Task "
            + str(binding.task_id).encode("ascii")
            + b", digest "
            + binding.task_excerpt_digest.encode("ascii")
            + b")"
        )
        sections.append(task_excerpt)
    if binding.role == "auditor":
        if audit_objective is None:
            raise InvocationError(
                "the auditor prompt requires the audit-objective bytes"
            )
        _verify_input_digest(
            "audit objective", audit_objective, binding.audit_objective_digest
        )
        sections.append(b"")
        sections.append(
            b"## Audit objective (digest "
            + binding.audit_objective_digest.encode("ascii")
            + b")"
        )
        sections.append(audit_objective)
    if binding.role == "planner" and findings is not None:
        if not binding.findings_digest:
            raise InvocationError(
                "the planner findings payload requires a findings digest"
            )
        _verify_input_digest("findings", findings, binding.findings_digest)
        sections.append(b"")
        sections.append(
            b"## Findings from the previous round (digest "
            + binding.findings_digest.encode("ascii")
            + b")"
        )
        sections.append(b"")
        sections.append(
            b"These structured, receipt-bound findings and blocked references "
            b"from the previous verification/audit rounds must be incorporated "
            b"into this revised plan as new or revised tasks before "
            b"development starts; the next developer sees only this revised "
            b"plan."
        )
        sections.append(findings)
    prompt = b"\n".join(sections)
    if len(prompt) > PROMPT_MAX_BYTES:
        raise InvocationError(
            f"the composed prompt exceeds the {PROMPT_MAX_BYTES}-byte bound "
            f"({len(prompt)} bytes)"
        )
    return prompt


# --------------------------------------------------------------------------
# Prompt-file publication (the wrapper's secure /tmp contract)
# --------------------------------------------------------------------------

def _prompt_directory() -> Path:
    """Fresh mode-0700 directory beneath /tmp for one prompt file.

    The secure wrapper requires the prompt file to resolve beneath ``/tmp``
    (``scripts/pi2-secure-exec.py``), so the directory is always created
    there regardless of ``TMPDIR``.
    """
    try:
        return Path(tempfile.mkdtemp(prefix="factory-loop-launch-", dir="/tmp"))
    except OSError as exc:
        raise LaunchError(f"cannot create the prompt directory under /tmp: {exc}") from exc


def write_prompt_file(directory: Path, prompt: bytes) -> Path:
    """Publish ``prompt`` as a mode-0600 single-link regular file in ``directory``.

    The file is created with ``O_EXCL`` and no-follow semantics so a raced
    pathname is never reused, and satisfies the wrapper's ownership/link
    count/mode/size checks.
    """
    if len(prompt) > PROMPT_MAX_BYTES:
        raise InvocationError(
            f"prompt exceeds the {PROMPT_MAX_BYTES}-byte bound"
        )
    path = directory / "prompt.md"
    flags = (
        os.O_WRONLY | os.O_CREAT | os.O_EXCL
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )
    try:
        descriptor = os.open(path, flags, 0o600)
    except OSError as exc:
        raise LaunchError(f"cannot create the prompt file {path}: {exc}") from exc
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(prompt)
            stream.flush()
    except OSError as exc:
        raise LaunchError(f"cannot write the prompt file {path}: {exc}") from exc
    try:
        info = path.stat()
    except OSError as exc:
        raise LaunchError(f"cannot stat the prompt file {path}: {exc}") from exc
    if stat.S_ISLNK(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.getuid():
        raise LaunchError(f"the prompt file {path} has unsafe ownership/link state")
    if info.st_mode & 0o022:
        raise LaunchError(f"the prompt file {path} is group/other-writable")
    return path


# --------------------------------------------------------------------------
# Exact child argv and environment
# --------------------------------------------------------------------------

def child_environment(binding: InvocationBinding) -> Dict[str, str]:
    """The exact child environment: allowlist plus the invocation fields.

    The environment is *built*, never inherited: only the documented
    :data:`ENV_ALLOWLIST` keys are copied from the parent (when present) and
    the ``FACTORY_LOOP_LAUNCH_*`` fields below are added.  No
    ``FACTORY_LOOP_LOCK_*``/legacy ``FACTORY_LOCK_*`` key, no
    ``GIT_CONFIG*``/``GIT_DIR`` redirector, no ``PI_*`` session/memory
    variable, and no credential/session variable can reach the leaf — this
    is the exact-env allowlist (§9, §20).  The lock strip is applied again
    as defense in depth.
    """
    verify_invocation(binding)
    environment: Dict[str, str] = {}
    for key in ENV_ALLOWLIST:
        if key in os.environ:
            environment[key] = os.environ[key]
    prefix = INVOCATION_ENV_PREFIX
    environment[prefix + "ROLE"] = binding.role
    environment[prefix + "MODEL"] = binding.model
    environment[prefix + "PROVIDER"] = binding.provider
    environment[prefix + "FRESH"] = "1"
    environment[prefix + "NO_RESUME"] = "1"
    environment[prefix + "NO_MEMORY"] = "1"
    environment[prefix + "WORKSPACE"] = str(binding.workspace)
    environment[prefix + "BOUND_COMMIT"] = binding.bound_commit
    environment[prefix + "PLAN_DIGEST"] = binding.plan_digest
    environment[prefix + "ROLE_PROMPT_DIGEST"] = binding.role_prompt_digest
    environment[prefix + "PROMPT_SET_DIGEST"] = binding.prompt_set_digest
    environment[prefix + "POLICY_DIGEST"] = binding.policy_digest
    environment[prefix + "SPECIFICATION_DIGEST"] = binding.specification_digest
    environment[prefix + "ALLOWED_TOOLS"] = ",".join(binding.allowed_tools)
    environment[prefix + "RUNTIME_LIMIT"] = repr(float(binding.runtime_limit))
    environment[prefix + "INACTIVITY_LIMIT"] = repr(float(binding.inactivity_limit))
    if binding.role == "developer":
        environment[prefix + "TASK_ID"] = str(binding.task_id)
        environment[prefix + "TASK_EXCERPT_DIGEST"] = binding.task_excerpt_digest
    if binding.role == "auditor":
        environment[prefix + "AUDIT_OBJECTIVE_DIGEST"] = binding.audit_objective_digest
    # Defense in depth: strip every lock key and Git redirector by prefix.
    environment = stripped_child_env(environment)
    environment = sanitize_git_environment(environment)
    verify_child_env(environment)
    return environment


def verify_child_env(environment: Mapping[str, str]) -> None:
    """Fail closed when a forbidden/legacy/credential key would reach a leaf.

    ``environment`` (typically the exact child environment) must contain no
    lock metadata, no Git redirector, and no credential-named key — the
    allowlist contract of §9/§20.
    """
    for key in environment:
        upper = key.upper()
        if key.startswith(("FACTORY_LOOP_LOCK_", "FACTORY_LOCK_")):
            raise InvocationError(
                f"refusing a child environment carrying lock metadata key {key!r}"
            )
        if key in GIT_ENV_STRIP or key.startswith(("GIT_CONFIG", "GIT_CONFIG_")):
            raise InvocationError(
                f"refusing a child environment carrying Git redirector {key!r}"
            )
        if any(
            token in upper
            for token in ("COOKIE", "TOKEN", "PASSWORD", "PASSWD", "API_KEY",
                          "SECRET", "CREDENTIAL", "PRIVATE_KEY", "AUTH")
        ):
            raise InvocationError(
                f"refusing a child environment carrying credential-like key {key!r}"
            )


def child_argv(
    binding: InvocationBinding,
    prompt_path: Path,
    session_dir: Path,
    *,
    secure_wrapper: Optional[Path] = None,
) -> List[str]:
    """Build the exact one-shot argv for the secure wrapper.

    ``argv = [python, wrapper, --prompt-file, <prompt>, --, backend,
    --provider, P, --model, M, --print, --no-session, --session-dir, <dir>,
    --no-skills, --no-themes, --no-context-files, --tools, T]``.

    Every flag is structural (the one-shot/no-resume/no-session contract of
    §9/§20) or derived from the binding; no session-resume or conversation
    flag can appear (``FORBIDDEN_BACKEND_FLAGS`` is re-checked here as
    defense in depth), and no prompt content or credential material is ever
    put in argv.
    """
    verify_invocation(binding)
    # Absolute trusted interpreter: the wrapper is executed with the
    # control-plane interpreter, whose resolution is bounded to the trusted
    # set and never follows an attacker-controlled path.
    require_trusted_interpreter()
    wrapper = Path(secure_wrapper or secure_wrapper_path(binding.workspace))
    if not wrapper.is_absolute() or not wrapper.is_file():
        raise InvocationError(
            f"the secure wrapper must be an absolute existing file: {wrapper}"
        )
    tools = ",".join(binding.allowed_tools)
    argv = [
        sys.executable,
        str(wrapper),
        "--prompt-file",
        str(prompt_path),
        "--",
        str(binding.backend),
        "--provider", binding.provider,
        "--model", binding.model,
        "--print",
        "--no-session",
        "--session-dir", str(session_dir),
        "--no-skills",
        "--no-themes",
        "--no-context-files",
        "--tools", tools,
    ]
    for flag in FORBIDDEN_BACKEND_FLAGS:
        if flag in argv:
            raise InvocationError(
                f"refusing to construct an argv containing the resume flag {flag!r}"
            )
    return argv


def secure_wrapper_path(workspace: Path) -> Path:
    """Absolute committed wrapper path: ``<workspace>/scripts/pi2-secure-exec.py``."""
    path = Path(workspace).absolute() / SECURE_WRAPPER
    if not path.is_file():
        raise InvocationError(
            f"the secure wrapper is missing at {path}; the existing wrapper "
            "is invoked, never reimplemented"
        )
    return path


def _session_directory() -> Path:
    """Fresh mode-0700 session directory under /tmp (``--session-dir``)."""
    try:
        return Path(tempfile.mkdtemp(prefix="factory-loop-session-", dir="/tmp"))
    except OSError as exc:
        raise LaunchError(f"cannot create the session directory: {exc}") from exc


def _remove_private_directories(directories: Iterable[Optional[Path]]) -> None:
    """Best-effort removal of private per-launch directories (Task 8, finding 6).

    Removes every private directory the mint or supervisor created — the
    exec staging directory, the prompt directory, the session directory, and
    the sanitized home — so no private or credential material survives a
    failed authorization or a rejected attempt.  Removal is best-effort:
    a failure never masks the original error.
    """
    for directory in directories:
        if directory is None:
            continue
        try:
            shutil.rmtree(directory)
        except (OSError, FileNotFoundError):
            pass


def _authority_private_directories(authority: "LaunchAuthority") -> List[Optional[Path]]:
    """The exact per-launch private directories a minted authority carries.

    ``exec_dir`` (staging), the prompt file's parent directory, the session
    directory, and the sanitized home — the same four paths the
    confinement specification's ``private_launch_rules`` bind and the
    supervisor's cleanup removes.
    """
    directories: List[Optional[Path]] = [authority._exec_dir]
    if authority._prompt_path is not None:
        directories.append(Path(authority._prompt_path).parent)
    directories.append(authority._session_dir)
    directories.append(authority._sanitized_home)
    return directories


# --------------------------------------------------------------------------
# Child process invariants (per-launch read-back)
# --------------------------------------------------------------------------

def _read_child_environ(pid: int) -> Dict[str, str]:
    try:
        raw = Path(f"/proc/{pid}/environ").read_bytes()
    except OSError as exc:
        raise SupervisionError(
            f"cannot read /proc/{pid}/environ: {exc}"
        ) from exc
    result: Dict[str, str] = {}
    for entry in raw.split(b"\x00"):
        if not entry:
            continue
        key, separator, value = entry.partition(b"=")
        if separator:
            result[key.decode("utf-8", "replace")] = value.decode("utf-8", "replace")
    return result


def verify_child_invariants(
    pid: int,
    workspace: Path,
    binding: InvocationBinding,
) -> Tuple[str, ...]:
    """Per-launch verification of the §9/§12 process invariants.

    Returns the list of verified invariant names.  A violated invariant
    raises :class:`SupervisionError` (fail closed).  When the child already
    exited before the read-back window the invariants cannot be observed
    and ``("unverifiable-crashed",)`` is returned — the *structural* child
    boundary (``close_fds``, ``pass_fds=()``, built environment, new
    session) is still asserted by the parent, so the security property does
    not depend on the read-back timing.

    The read-back is a bounded settle loop that **pins the child's
    identity**: the start time from ``/proc/<pid>/stat`` is captured at the
    first observation and every ``/proc/<pid>/environ`` read is accepted
    only while that same start time still owns the PID (re-checked both
    before and after the read), so a PID reused by an unrelated process is
    never verified as the launched child.  If the pinned identity survives
    the whole bounded window without the environment ever settling on the
    fresh-context marker, the run fails closed with
    :class:`SupervisionError`; only a child that is gone before any
    observation (crash-before-readback) yields ``("unverifiable-crashed",)``.
    """
    deadline = time.monotonic() + INVARIANT_READBACK_WINDOW
    starttime: Optional[int] = None
    sid: Optional[int] = None
    environ: Optional[Dict[str, str]] = None
    while time.monotonic() < deadline:
        fields = _proc_stat_fields(pid)
        if fields is None or fields[0] == "Z":
            # The child is gone or a zombie: it cannot be observed in the
            # window and cannot fork further.  A crash before the first
            # observation is the documented ``unverifiable-crashed`` case;
            # a crash after the environment was verified keeps the verified
            # invariants.
            if environ is None:
                return ("unverifiable-crashed",)
            break
        if len(fields) < 20:
            time.sleep(0.02)
            continue
        current_start = int(fields[19])
        if starttime is None:
            # Pin the identity at the first observation so a later PID reuse
            # can never be accepted as the launched child.
            starttime = current_start
        elif current_start != starttime:
            raise SupervisionError(
                f"child {pid} starttime changed during the invariant "
                "read-back window; a PID reuse was observed and the "
                "fresh-context boundary cannot be verified (fail closed)"
            )
        if sid is None:
            try:
                sid = os.getsid(pid)
            except (ProcessLookupError, PermissionError):
                sid = None
        # Gate the environ read on the identity and re-verify it *after* the
        # read, so a between-read PID reuse can never be accepted.
        try:
            candidate = _read_child_environ(pid)
        except SupervisionError:
            candidate = None
        after = _proc_stat_fields(pid)
        if (
            after is not None
            and after[0] != "Z"
            and len(after) >= 20
            and int(after[19]) == starttime
        ):
            environ = candidate
        if (
            sid is not None
            and sid == pid
            and environ is not None
            and environ.get(INVOCATION_ENV_PREFIX + "FRESH") == "1"
        ):
            break
        time.sleep(0.02)
    if sid is None or sid != pid:
        raise SupervisionError(
            f"child {pid} did not start a new process session "
            f"(session id {sid}); the fresh-context boundary is violated"
        )
    if environ is None or environ.get(INVOCATION_ENV_PREFIX + "FRESH") != "1":
        # Fail closed: a child that is still alive after the bounded window
        # without its environment ever settling on the fresh-context marker
        # cannot be trusted.  Only a child that crashed before any
        # observation is the ``unverifiable-crashed`` case.
        if starttime is not None and _is_live_with_identity(pid, starttime):
            raise SupervisionError(
                f"child {pid} stayed alive but its environment never settled "
                f"on the fresh-context marker within the "
                f"{INVARIANT_READBACK_WINDOW:.1f}s invariant read-back window"
            )
        return ("unverifiable-crashed",)
    for key in environ:
        if key.startswith(("FACTORY_LOOP_LOCK_", "FACTORY_LOCK_")):
            raise SupervisionError(
                f"child {pid} inherited lock metadata key {key!r}; the lock "
                "boundary is violated"
            )
        if key in GIT_ENV_STRIP or key.startswith(("GIT_CONFIG", "GIT_CONFIG_")):
            raise SupervisionError(
                f"child {pid} inherited Git redirector {key!r}"
            )
    verified: List[str] = ["session", "environ"]
    root_info = os.stat(binding.workspace, follow_symlinks=False)
    try:
        descriptors = os.listdir(f"/proc/{pid}/fd")
    except (ProcessLookupError, PermissionError, OSError):
        return tuple(verified)
    for entry in descriptors:
        try:
            info = os.stat(f"/proc/{pid}/fd/{entry}")
        except OSError:
            continue
        if (info.st_dev, info.st_ino) == (root_info.st_dev, root_info.st_ino):
            raise SupervisionError(
                f"child {pid} inherited descriptor {entry} aliasing the "
                "canonical root lock inode; the writer boundary is violated"
            )
    verified.append("descriptors")
    return tuple(verified)


# --------------------------------------------------------------------------
# Supervision (F6/F7, §9, §12, §17)
# --------------------------------------------------------------------------

class _BoundedStream:
    """Bounded output capture: a digest over a prefix and a bounded tail."""

    __slots__ = ("_digest", "_digested", "_tail", "_total", "truncated")

    def __init__(self) -> None:
        self._digest = hashlib.sha256()
        self._digested = 0
        self._tail = bytearray()
        self._total = 0
        self.truncated = False

    def feed(self, chunk: bytes) -> None:
        if not chunk:
            return
        self._total += len(chunk)
        if self._digested < OUTPUT_DIGEST_CAP:
            # Digest only the bytes actually consumed from this chunk toward
            # the bounded prefix; never credit the full remaining room, or a
            # short chunk would mark the cap reached and silently stop
            # digesting the rest of the stream.
            take = chunk[: OUTPUT_DIGEST_CAP - self._digested]
            self._digest.update(take)
            self._digested += len(take)
        self._tail.extend(chunk)
        if len(self._tail) > OUTPUT_TAIL_CAP:
            self.truncated = True
            del self._tail[: len(self._tail) - OUTPUT_TAIL_CAP]

    def result(self) -> "StreamResult":
        return StreamResult(
            bytes=self._total,
            digest=self._digest.hexdigest(),
            tail=self._tail.decode("utf-8", "replace"),
            truncated=self.truncated,
        )


@dataclass(frozen=True)
class StreamResult:
    """Bounded per-stream capture (never the raw unbounded stream)."""

    bytes: int
    digest: str
    tail: str
    truncated: bool

    def to_dict(self) -> Dict[str, object]:
        return {
            "bytes": self.bytes,
            "digest": self.digest,
            "tail": self.tail,
            "truncated": self.truncated,
        }


@dataclass(frozen=True)
class LaunchResult:
    """The machine-readable completion signal of one supervised attempt.

    ``outcome`` is ``completed`` when the model process exited on its own and
    ``terminated`` when the supervisor ended it (runtime/inactivity limit).
    ``returncode`` is the child's exit status (``None`` when the leader was
    signal-killed and its exit status was not preserved); ``signal`` names
    the fatal signal when the child was killed.  ``terminated_by`` records
    the signals the supervisor delivered (TERM, INT, HUP, KILL).  ``stdout``
    and ``stderr`` are bounded per-stream captures; no environment or argv
    is ever included, so no credential material can appear in a result.
    """

    role: str
    model: str
    provider: str
    outcome: str
    returncode: Optional[int]
    signal: Optional[str]
    reason: Optional[str]
    terminated_by: Tuple[str, ...]
    elapsed: float
    stdout: StreamResult
    stderr: StreamResult
    descendants_snapshot: int
    live_descendants: int
    invariants: Tuple[str, ...]

    def to_dict(self) -> Dict[str, object]:
        return {
            "role": self.role,
            "model": self.model,
            "provider": self.provider,
            "outcome": self.outcome,
            "returncode": self.returncode,
            "signal": self.signal,
            "reason": self.reason,
            "terminated_by": list(self.terminated_by),
            "elapsed": round(self.elapsed, 6),
            "stdout": self.stdout.to_dict(),
            "stderr": self.stderr.to_dict(),
            "descendants_snapshot": self.descendants_snapshot,
            "live_descendants": self.live_descendants,
            "invariants": list(self.invariants),
        }


def _pid_matches_identity(pid: int, starttime: int) -> bool:
    """True when the process (live *or* zombie) at ``pid`` keeps ``starttime``.

    F4 identity pinning for group signals: an unreaped zombie cannot have
    its PID reused, so a matching starttime is sufficient to prove the group
    id still belongs to the launched leader before a group signal.  ``None``
    is returned for a fully reaped PID, which must never be signaled.
    """
    fields = _proc_stat_fields(pid)
    if fields is None or len(fields) < 20:
        return False
    try:
        return int(fields[19]) == starttime
    except ValueError:
        return False


def _pid_group_has_live_members(pgid: int) -> bool:
    """True when any non-zombie process still belongs to ``pgid``."""
    for pid in _iter_pids():
        fields = _proc_stat_fields(pid)
        if fields is None or len(fields) < 3 or fields[0] == "Z":
            continue
        try:
            if int(fields[2]) == pgid:
                return True
        except ValueError:
            continue
    return False


class LaunchSupervision:
    """Descendant-scoped supervision of one fresh model process (F6/F7).

    Owns the F6 descendant-scoped handle scan (a single bounded snapshot of
    the role's own live descendants, re-enumerated through
    ``live_scope``), the subreaper lifecycle (F7), the TERM/INT/HUP→KILL
    bounded termination sequence, the crash-before-snapshot/PID-reuse
    guards, and the structured bounded result.
    """

    def __init__(
        self,
        binding: InvocationBinding,
        *,
        root: Optional[Path] = None,
        prompt_path: Optional[Path] = None,
        session_dir: Optional[Path] = None,
        kill_grace: float = DEFAULT_KILL_GRACE,
    ) -> None:
        verify_invocation(binding)
        self.binding = binding
        self.root = Path(root or binding.workspace).absolute()
        self.kill_grace = kill_grace
        self.prompt_path = Path(prompt_path) if prompt_path else None
        self.session_dir = Path(session_dir) if session_dir else None
        self._child: Optional[subprocess.Popen[bytes]] = None
        self._captured: frozenset = frozenset()
        self._capture_error: Optional[str] = None
        self._pre_existing_children: set = set()
        self._elapsed: float = 0.0
        # F4: the leader's /proc starttime, pinned at spawn and re-verified
        # before *every* group signal, so a reused PID is never signaled.
        self._leader_starttime: Optional[int] = None
        # F3: scoped TERM/INT/HUP handlers installed for the attempt only;
        # the previous handlers are restored when the attempt ends.
        self._saved_handlers: Dict[int, object] = {}
        self._pending_signal: Optional[int] = None
        # TOCTOU-free verify-to-exec state bound by :meth:`run` from the
        # verified authority: the staged wrapper path, the staged files'
        # SHA-256 digests (re-checked immediately before exec), the external
        # trusted executables (revalidated immediately before exec), and the
        # private mode-0700 staging directory (removed at cleanup).
        self._staged_wrapper: Optional[Path] = None
        self._staged_digests: Dict[str, str] = {}
        self._external_paths: Tuple[str, ...] = ()
        self._exec_dir: Optional[Path] = None
        # Task 8 confinement binding carried by the verified authority (the
        # exact specification, the real proof, the sanitized home, and the
        # staged confine launcher); ``None`` when the token carries none.
        self._confinement_spec: Optional[Dict[str, object]] = None
        self._confinement_proof: Optional[object] = None
        self._sanitized_home: Optional[Path] = None
        self._confined_launcher: Optional[Path] = None

    # -- subreaper lifecycle (F7) --------------------------------------------

    def install_subreaper(self) -> None:
        """Install this process as a child subreaper (``PR_SET_CHILD_SUBREAPER``).

        With the flag set, every orphaned descendant of the model process is
        reparented to this process, so an escaped double-fork/``setsid``
        descendant can never orphan to PID 1: it is reaped here when it
        dies and detected (by its reparented identity) when it survives.
        Installing is idempotent; the flag cannot be unset.
        """
        try:
            libc = ctypes.CDLL(None, use_errno=True)
        except OSError as exc:
            raise SupervisionError(
                f"cannot load libc for PR_SET_CHILD_SUBREAPER: {exc}"
            ) from exc
        result = libc.prctl(36, 1, 0, 0, 0)  # PR_SET_CHILD_SUBREAPER == 36
        if result != 0:
            error = ctypes.get_errno()
            raise SupervisionError(
                f"cannot install the child subreaper (prctl errno {error}); "
                "escaped descendants could orphan and be lost"
            )

    def _snapshot_pre_existing_children(self) -> set:
        """Snapshot of processes already parented to this process (for scoping)."""
        me = os.getpid()
        found: set = set()
        for pid in _iter_pids():
            if pid == me:
                continue
            fields = _proc_stat_fields(pid)
            if fields is None or len(fields) < 2:
                continue
            try:
                if int(fields[1]) == me:
                    found.add(pid)
            except ValueError:
                continue
        return found

    # -- F3 scoped signal handling ----------------------------------------------

    def install_signal_handlers(self) -> None:
        """Install scoped TERM/INT/HUP handlers for the active attempt (F3).

        A signal received after spawn is recorded and the monitor loop takes
        the bounded terminate-then-reap path, so an external TERM/INT/HUP
        (for example a campaign shutdown) can never leave a child running.
        The previous handlers are saved and restored by
        :meth:`_restore_signal_handlers` when the attempt ends; installing is
        idempotent.  When the handlers cannot be installed — the caller is
        not the main thread (where ``signal.signal`` is refused), or the
        interpreter cannot deliver signals — the launch **fails loudly**
        rather than silently continuing without a signal-safe supervision
        window (an unrecorded child could be left running).
        """
        if self._saved_handlers:
            return
        try:
            for sig in TERMINATION_SIGNALS:
                self._saved_handlers[sig] = signal.getsignal(sig)
                signal.signal(sig, self._on_supervisor_signal)
        except (ValueError, TypeError) as exc:
            self._saved_handlers = {}
            raise SupervisionError(
                "cannot install the TERM/INT/HUP attempt handlers: "
                f"{exc}; refusing a launch without a signal-safe "
                "supervision window"
            ) from exc

    def _on_supervisor_signal(self, signum: int, frame: object) -> None:
        """Record an external TERM/INT/HUP; the monitor loop terminates bounded.

        Before the child exists the attempt is still in its pre-spawn window:
        the scoped handlers are restored and the signal is re-raised so the
        process dies with the signal's default disposition.
        """
        if self._child is not None:
            self._pending_signal = signum
            return
        self._restore_signal_handlers()
        signal.raise_signal(signum)

    def _restore_signal_handlers(self) -> None:
        """Restore the previous TERM/INT/HUP handlers (F3)."""
        for sig, handler in tuple(self._saved_handlers.items()):
            try:
                signal.signal(sig, handler)
            except (ValueError, TypeError):
                pass
        self._saved_handlers = {}

    # -- F4 identity pinning -----------------------------------------------------

    def _pin_leader_identity(self, pid: int) -> Optional[int]:
        """The leader's /proc start time, or ``None`` when it is gone."""
        fields = _proc_stat_fields(pid)
        if fields is None or len(fields) < 20:
            return None
        try:
            return int(fields[19])
        except ValueError:
            return None

    def _leader_exited(self, child: subprocess.Popen[bytes]) -> bool:
        """Non-reaping liveness check: has the leader exited (zombie or gone)?

        ``Popen.poll()`` reaps a zombie the moment it exits, which frees the
        PID — and therefore the process-group id — before termination runs.
        The monitor and the F1 emergency path must never reap the leader
        early, or the F4 identity check could not prove the group still
        belongs to the launched leader.  This check reads ``/proc`` only and
        leaves the zombie (and its pinned identity) untouched until
        ``child.wait()`` reaps it at the reap point.
        """
        fields = _proc_stat_fields(child.pid)
        if fields is None:
            return True
        return fields[0] == "Z"

    # -- F6 descendant-scoped handle scan --------------------------------------

    def snapshot(self) -> frozenset:
        """F6: capture exactly this role's live descendants once.

        The captured scope pins every PID to its starttime and parent PID
        (:class:`CapturedProcess`), so PID reuse can never widen the set.
        When the leader already exited before the snapshot could be taken
        (crash-before-snapshot window) the scope is empty: a dead leader
        cannot fork further and any descendant it left is reparented to
        this subreaper and accounted by :meth:`_live_reparented_children`.
        """
        if self._child is None:
            raise SupervisionError("no child process to snapshot")
        pid = self._child.pid
        if self._leader_exited(self._child):
            # Crash-before-snapshot window, taken with the *non-reaping*
            # liveness check: a zombie leader is never reaped here, so its
            # pinned /proc starttime identity (F4) survives for the later
            # group signals and the process-group id is never freed before
            # termination runs.  ``Popen.poll()`` would reap the zombie the
            # moment it exited and free the PID (and therefore the pgid)
            # ahead of the F4 identity check; the review finding is that a
            # leader that exits while a pipe-holding descendant lives must
            # still be bounded-terminated through its pinned identity, so the
            # snapshot never polls.
            self._captured = frozenset()
            self._capture_error = "crash-before-snapshot"
            return self._captured
        try:
            self._captured = capture_descendants(pid)
            self._capture_error = None
        except RootLockUnsafeError as exc:
            if self._leader_exited(self._child):
                self._captured = frozenset()
                self._capture_error = "crash-before-snapshot"
            else:
                raise SupervisionError(
                    f"cannot capture the descendant scope of {pid}: {exc}"
                ) from exc
        return self._captured

    def live_scope(self) -> frozenset:
        """Re-enumerate only the still-live members of the captured scope.

        This is the never-stale descendant accounting surface: a PID reused
        by an unrelated process after the captured one exited fails the
        starttime identity check and is excluded.
        """
        return live_scope(self._captured)

    # -- spawning --------------------------------------------------------------

    def spawn(self) -> subprocess.Popen[bytes]:
        """Spawn the fresh one-shot model process behind the wrapper.

        The child starts a new process session/group and inherits nothing:
        ``close_fds=True``, no ``pass_fds``, and a built allowlist
        environment, so the lock descriptor and lock metadata never reach a
        leaf.  ``cwd`` is the canonical workspace.

        **Signal-safe spawn window.**  TERM, INT, and HUP are blocked on the
        launch thread with ``pthread_sigmask`` from before the child is
        spawned until its identity (PID + /proc starttime) is recorded, then
        the mask is restored so any signal received in that window is
        *delivered* (pending) and the monitor loop takes the bounded
        terminate-then-reap path — a signal can never be silently lost with
        an unrecorded child running.  The child-side ``preexec_fn`` restores
        an empty mask right after the fork so the fresh process never
        inherits the launch thread's spawn-window block.  Any launch
        attempted off the main thread (where the mask/handler semantics
        cannot be made safe) fails loudly before anything is spawned.
        """
        if self._child is not None:
            raise SupervisionError("a child was already spawned")
        if self.prompt_path is None:
            raise SupervisionError("no prompt file was published")
        if self.session_dir is None:
            raise SupervisionError("no session directory was prepared")
        if threading.current_thread() is not threading.main_thread():
            raise SupervisionError(
                "a fresh-process launch is refused off the main thread: "
                "TERM/INT/HUP blocking (pthread_sigmask) and the scoped "
                "handlers require the main thread, so an off-main launch "
                "could leave an unrecorded child running"
            )
        # Exec-boundary re-checks (TOCTOU-free verify-to-exec): the
        # interpreter is absolute and trusted (bounded resolution set), the
        # staged committed bytes are still byte-exact (a swap fails closed),
        # and every external trusted executable's fully resolved path is
        # revalidated immediately before exec.
        require_trusted_interpreter()
        for staged_path, digest in self._staged_digests.items():
            if _file_sha256(Path(staged_path)) != digest:
                raise SupervisionError(
                    f"staged executable {staged_path} changed since "
                    "verification; refusing to execute swapped bytes (F2)"
                )
        for external in self._external_paths:
            try:
                require_trusted_executable(external)
            except GitBoundaryError as exc:
                raise SupervisionError(
                    f"external trusted executable {external} changed or is "
                    f"no longer immutable before exec: {exc} (F2)"
                ) from exc
        env = child_environment(self.binding)
        argv = child_argv(
            self.binding, self.prompt_path, self.session_dir,
            secure_wrapper=self._staged_wrapper,
        )
        # Task 8 confined launch: when the verified authority carries the
        # exact confinement specification and a real proof, the model child is
        # routed through the staged confine launcher.  The proof is
        # re-validated *immediately before exec* — binding the exact
        # invocation, the exact executing guard-source bytes, the credential
        # channels, and the exact specification digest being applied — so no
        # model is ever started without a proof that matches what will be
        # enforced (fail closed).
        if self._confinement_spec is not None:
            if self._confined_launcher is None or self._confinement_proof is None:
                raise SupervisionError(
                    "the verified authority carries a confinement "
                    "specification without the staged confine launcher or "
                    "its real confinement proof; no model can be started "
                    "(fail closed)"
                )
            try:
                real_confinement_authority.validate_proof(
                    self._confinement_proof,
                    self.binding,
                    confinement_spec=self._confinement_spec,
                    _strict_channels=False,
                )
            except real_confinement_authority.ConfinementError as exc:
                raise SupervisionError(
                    "the confinement proof does not bind the exact "
                    "specification being applied at exec time; no model can "
                    f"be started (fail closed): {exc}"
                ) from exc
            spec_path = self._publish_confinement_spec()
            argv = [
                sys.executable,
                str(self._confined_launcher),
                "--spec-file",
                str(spec_path),
                "--",
                *argv,
            ]
        if not hasattr(signal, "pthread_sigmask"):
            raise SupervisionError(
                "pthread_sigmask is unavailable; TERM/INT/HUP cannot be "
                "blocked across the spawn window, so an unrecorded child "
                "could be left running (fail closed)"
            )
        oldmask = signal.pthread_sigmask(signal.SIG_BLOCK, TERMINATION_SIGNALS)
        try:
            try:
                process = subprocess.Popen(
                    argv,
                    cwd=str(self.binding.workspace),
                    env=env,
                    start_new_session=True,
                    close_fds=True,
                    pass_fds=(),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    preexec_fn=_child_reset_spawn_mask,
                )
            except (OSError, ValueError, subprocess.SubprocessError) as exc:
                raise LaunchError(f"cannot spawn the model process: {exc}") from exc
            self._child = process
            # F4: pin the leader's starttime at spawn; every later group
            # signal re-verifies it so a reused PID is never signaled or
            # reaped.  The identity is recorded *before* the mask is
            # restored, so a signal received during the window is delivered
            # only after the child is fully recorded.
            self._leader_starttime = self._pin_leader_identity(process.pid)
        finally:
            # Restore the launch thread's mask; any signal received during
            # the window is delivered here (pending), recorded by the scoped
            # handler, and the monitor loop bounded-terminates the group.
            signal.pthread_sigmask(signal.SIG_SETMASK, oldmask)
        return process

    # -- bounded monitoring -----------------------------------------------------

    def _monitor(
        self,
        child: subprocess.Popen[bytes],
        deadline: float,
        inactivity_limit: float,
    ) -> Tuple[_BoundedStream, _BoundedStream, Optional[str], float]:
        stdout = _BoundedStream()
        stderr = _BoundedStream()
        streams = {child.stdout.fileno(): stdout, child.stderr.fileno(): stderr}
        files = {
            child.stdout.fileno(): child.stdout,
            child.stderr.fileno(): child.stderr,
        }
        for descriptor in streams:
            try:
                os.set_blocking(descriptor, False)
            except OSError:
                continue
        open_fds = set(streams)
        last_activity = time.monotonic()
        reason: Optional[str] = None
        try:
            while True:
                if self._pending_signal is not None:
                    reason = f"signal:{signal.Signals(self._pending_signal).name}"
                    break
                if self._leader_exited(child) and not open_fds:
                    break
                now = time.monotonic()
                if now >= deadline:
                    reason = "runtime"
                    break
                if inactivity_limit and now - last_activity > inactivity_limit:
                    reason = "inactivity"
                    break
                if not open_fds:
                    # The leader is still alive with all pipes closed (a
                    # descendant held them and released them): keep bounded
                    # monitoring without busy-spinning until the deadline.
                    time.sleep(0.05)
                    continue
                try:
                    readable, _, _ = select.select(list(open_fds), [], [], 0.2)
                except InterruptedError:
                    # A caught TERM/INT/HUP woke the select: re-check the
                    # pending-signal flag and the deadline immediately.
                    continue
                except (OSError, ValueError):
                    break
                for descriptor in readable:
                    try:
                        chunk = os.read(descriptor, 65536)
                    except (BlockingIOError, InterruptedError):
                        continue
                    except OSError:
                        chunk = b""
                    if not chunk:
                        open_fds.discard(descriptor)
                        try:
                            files[descriptor].close()
                        except OSError:
                            pass
                    else:
                        last_activity = time.monotonic()
                        streams[descriptor].feed(chunk)
                if self._leader_exited(child) and not open_fds:
                    break
        finally:
            for descriptor in tuple(open_fds):
                try:
                    files[descriptor].close()
                except OSError:
                    pass
        return stdout, stderr, reason, last_activity

    # -- bounded termination ----------------------------------------------------

    def _terminate(self, child: subprocess.Popen[bytes], reason: str) -> Tuple[str, ...]:
        """TERM → INT → HUP → (full grace) → KILL the whole process group.

        Every PID is pinned to its /proc starttime **before every group
        signal** (F4): the leader's identity is re-verified immediately
        before each TERM/INT/HUP/KILL delivery, and any identity loss — a
        reused PID, a reaped leader — fails closed with
        :class:`SupervisionError` instead of signaling an unrelated process
        group.  Each of TERM, INT, and HUP is delivered to the full new
        process group; the full bounded grace is always observed before the
        unconditional KILL of the group (a TERM-ignoring member that stays
        in the group cannot outlive the run), then the group is verified
        gone within a bound and the leader is reaped.  Returns the ordered
        delivered signal names.
        """
        pid = child.pid
        starttime = self._leader_starttime
        if starttime is None:
            starttime = self._pin_leader_identity(pid)
            self._leader_starttime = starttime
        if starttime is None:
            raise SupervisionError(
                f"cannot pin the leader {pid} starttime; refusing to signal or "
                "reap a process group that may belong to an unrelated reused "
                "PID (F4)"
            )
        delivered: List[str] = []
        segment = self.kill_grace / len(TERMINATION_SIGNALS)
        for sig in TERMINATION_SIGNALS:
            if not _pid_matches_identity(pid, starttime):
                raise SupervisionError(
                    f"leader {pid} identity changed before "
                    f"{signal.Signals(sig).name}: the PID was reused by an "
                    "unrelated process and is never signaled (F4)"
                )
            try:
                os.killpg(pid, sig)
            except (ProcessLookupError, PermissionError):
                pass
            delivered.append(signal.Signals(sig).name)
            time.sleep(segment)
        deadline = time.monotonic() + self.kill_grace
        while time.monotonic() < deadline:
            time.sleep(min(0.02, max(0.0, deadline - time.monotonic())))
        if not _pid_matches_identity(pid, starttime):
            raise SupervisionError(
                f"leader {pid} identity changed before SIGKILL: the PID was "
                "reused and its process group is never KILLed (F4)"
            )
        try:
            os.killpg(pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        delivered.append("SIGKILL")
        gone_deadline = time.monotonic() + GROUP_GONE_TIMEOUT
        while time.monotonic() < gone_deadline:
            if not _pid_group_has_live_members(pid):
                break
            time.sleep(0.02)
        if _pid_group_has_live_members(pid):
            raise SupervisionError(
                f"the bounded process group of {pid} survived the KILL reap "
                f"window ({GROUP_GONE_TIMEOUT:.1f}s)"
            )
        if child.poll() is None:
            try:
                child.wait(timeout=REAP_TIMEOUT)
            except subprocess.TimeoutExpired as exc:
                raise SupervisionError(
                    f"the process-group leader {pid} was not reaped within "
                    f"the {REAP_TIMEOUT:.1f}s bound"
                ) from exc
        return tuple(delivered)

    # -- subreaper reaping and escape detection (F7) ------------------------------

    def _owned_post_spawn_children(self) -> List[int]:
        """PIDs parented to this subreaper that are *not* pre-existing children.

        The orphan/reap scope is the launch snapshot only (F6/F7): a child
        that existed before the attempt — a previous lifecycle child or an
        unrelated process spawned by the control plane — is explicitly
        excluded, so its exit status belongs to its owner and is never
        claimed here.
        """
        me = os.getpid()
        owned: List[int] = []
        for pid in _iter_pids():
            if pid == me or pid in self._pre_existing_children:
                continue
            fields = _proc_stat_fields(pid)
            if fields is None or len(fields) < 2:
                continue
            try:
                if int(fields[1]) == me:
                    owned.append(pid)
            except ValueError:
                continue
        return owned

    def _reap_orphans(self, leader: int) -> List[int]:
        """Reap every re-parented (orphaned) descendant as the subreaper.

        The reap is scoped to the launch snapshot (F6/F7): only children
        spawned after the snapshot — the leader's reparented orphans — are
        claimed, each by its own PID (never a blanket ``waitpid(-1)`` that
        could reap a pre-existing child of the control plane).  The leader
        is reaped separately by the caller.  An escaped orphan that died
        during or after termination is therefore always reaped here, never
        left as a zombie.
        """
        reaped: List[int] = []
        while True:
            claimed = False
            for pid in self._owned_post_spawn_children():
                if pid == leader:
                    continue
                try:
                    got, _ = os.waitpid(pid, os.WNOHANG)
                except (ChildProcessError, InterruptedError):
                    continue
                if got:
                    reaped.append(got)
                    claimed = True
            if not claimed:
                break
        return reaped

    def _live_reparented_children(self, leader: int) -> List[int]:
        """Live non-zombie processes parented to this subreaper (escaped orphans).

        A descendant that escaped the group (``setsid``/double-fork) and
        whose parent chain died is reparented here; if it is still alive it
        is an escaped descendant whose exact identity this supervisor owns,
        and recovery must fail closed.
        """
        me = os.getpid()
        found: List[int] = []
        for pid in _iter_pids():
            if pid == me or pid == leader:
                continue
            fields = _proc_stat_fields(pid)
            if fields is None or len(fields) < 3 or fields[0] == "Z":
                continue
            try:
                if int(fields[1]) == me:
                    found.append(pid)
            except ValueError:
                continue
        return found

    def _detect_escaped(self, leader: int) -> Tuple[List[int], str]:
        """Combine the Task-5 captured-scope detection with reparented survivors."""
        escaped, reason = detect_escaped_descendants(
            self.binding.workspace,
            model_pid=leader,
            captured=self._captured,
            trusted_pids=tuple(self._pre_existing_children),
        )
        current = set(escaped)
        reparented = [
            pid for pid in self._live_reparented_children(leader)
            if pid not in self._pre_existing_children
        ]
        current.update(reparented)
        notes = []
        if reason:
            notes.append(reason)
        if reparented:
            notes.append(
                f"pid {reparented} was reparented to the subreaper and survives"
            )
        return sorted(current), "; ".join(notes)

    def _finalize(self, leader: int) -> None:
        """Post-run reaping and fail-closed escaped-descendant detection."""
        # Reap every dead reparented orphan first (F7): an escaped orphan that
        # died during/after termination is always reaped, never a zombie.
        self._reap_orphans(leader)
        escaped, reason = self._detect_escaped(leader)
        if escaped:
            raise EscapedDescendantError(
                f"escaped model descendants survive; recovery is blocked: "
                f"{reason}"
            )
        # A reparented orphan that died while escape detection ran is reaped
        # here so nothing is ever left as a zombie of the control plane.
        self._reap_orphans(leader)

    # -- the run -----------------------------------------------------------------

    def run(
        self,
        authority: Optional["LaunchAuthority"] = None,
    ) -> LaunchResult:
        """Run one fresh role attempt and return the machine-readable result.

        ``authority`` must be the unforgeable verified-committed token minted
        by :func:`authorize_launch`: a direct programmatic call without a
        token (or with a forged token) is refused loudly, so the exported API
        can never bypass F2/F5 and the CLI stays the sole ordinary launch
        entry.  The attempt uses the authority's *verified* binding and its
        digest-bound authoritative bytes; the wrapper/backend execute from
        the staged (or revalidated external) paths recorded in the token.
        """
        if authority is None or not isinstance(authority, LaunchAuthority):
            raise SupervisionError(
                "direct programmatic launch without a verified committed "
                "LaunchAuthority is refused (F2/F5): mint the token with "
                "authorize_launch(); operator claims can never construct one"
            )
        if authority._mint is not _MINT_SECRET:
            raise SupervisionError(
                "the launch authority token is forged; only authorize_launch() "
                "may mint a verified-committed token (F2/F5)"
            )
        if threading.current_thread() is not threading.main_thread():
            # A genuine minted token whose attempt is rejected off the main
            # thread can never be reused (its per-launch private paths belong
            # to this attempt); remove every private directory it carries so
            # no private or credential material survives the rejected launch
            # (Task 8 review, finding 6).
            if isinstance(authority, LaunchAuthority):
                _remove_private_directories(
                    _authority_private_directories(authority)
                )
            raise SupervisionError(
                "a fresh-process launch is refused off the main thread: "
                "TERM/INT/HUP blocking (pthread_sigmask) and the scoped "
                "handlers require the main thread, so an off-main launch "
                "could leave an unrecorded child running"
            )
        binding = authority._binding
        blobs = authority._blobs
        # Re-verify the authority's exact bytes against its verified binding
        # digests (the mint already verified them against the committed
        # blobs at the bound commit; re-checking closes any post-mint
        # mutation of the token's blobs).
        _verify_input_digest(
            "role prompt", blobs["role_prompt"], binding.role_prompt_digest
        )
        _verify_input_digest(
            "operational policy", blobs["policy"], binding.policy_digest
        )
        _verify_input_digest(
            "specification", blobs["spec"], binding.specification_digest
        )
        _verify_input_digest(
            "implementation plan", blobs["plan"], binding.plan_digest
        )
        if binding.role == "developer":
            task_excerpt = blobs.get("task_excerpt")
            if task_excerpt is None:
                raise SupervisionError(
                    "the verified authority carries no developer task-excerpt bytes"
                )
            verify_task_excerpt(
                task_excerpt, binding.task_excerpt_digest, task_id=binding.task_id
            )
            if task_excerpt != task_excerpt_bytes(blobs["plan"], binding.task_id):
                raise SupervisionError(
                    "the authority's task-excerpt bytes are not the committed "
                    "plan section; refusing a substituted task (F5)"
                )
        if binding.role == "auditor":
            _verify_input_digest(
                "audit objective", blobs.get("audit_objective", b""),
                binding.audit_objective_digest,
            )
        self.binding = binding
        self._staged_wrapper = Path(authority._wrapper)
        self._staged_digests = dict(authority._staged_digests)
        self._external_paths = tuple(authority._external_paths)
        self._exec_dir = Path(authority._exec_dir)
        self._confinement_spec = dict(authority._confinement_spec) \
            if authority._confinement_spec else None
        self._confinement_proof = authority._confinement_proof
        self._sanitized_home = authority._sanitized_home
        self._confined_launcher = authority._confined_launcher
        # Task 8: the mint already created the prompt file and the session
        # directory and carried their exact paths; ``run`` uses exactly those
        # paths — the same paths the confinement specification's
        # ``private_launch_rules`` bind — never re-deriving or re-creating
        # them (fail closed if the authority carries none).
        if authority._prompt_path is None or authority._session_dir is None:
            raise SupervisionError(
                "the verified authority carries no per-launch prompt/session "
                "paths; no model can be started (fail closed)"
            )
        self.prompt_path = Path(authority._prompt_path)
        self.session_dir = Path(authority._session_dir)
        prompt = compose_prompt(
            binding,
            role_prompt=blobs["role_prompt"],
            agents=blobs["policy"],
            spec=blobs["spec"],
            plan=blobs["plan"],
            audit_objective=blobs.get("audit_objective"),
            task_excerpt=blobs.get("task_excerpt"),
        )
        if _file_sha256(self.prompt_path) != hashlib.sha256(prompt).hexdigest():
            raise SupervisionError(
                "the authority's prompt file does not match the composed "
                "prompt bytes; refusing a substituted prompt file (F5)"
            )
        try:
            self.install_subreaper()
            self.install_signal_handlers()
            self._pre_existing_children = self._snapshot_pre_existing_children()
            child = self.spawn()
            started = time.monotonic()
            try:
                invariants = verify_child_invariants(
                    child.pid, binding.workspace, binding
                )
                snapshot = self.snapshot()
                out, err, reason, _ = self._monitor(
                    child, started + binding.runtime_limit, binding.inactivity_limit
                )
                if reason is None:
                    if child.poll() is None:
                        try:
                            child.wait(timeout=REAP_TIMEOUT)
                        except subprocess.TimeoutExpired as exc:
                            raise SupervisionError(
                                "the model process did not exit within the bounded "
                                "reap window"
                            ) from exc
                    outcome = "completed"
                    terminated_by: Tuple[str, ...] = ()
                else:
                    terminated_by = self._terminate(child, reason)
                    outcome = "terminated"
                # Reap the leader (already done inside _terminate for the
                # terminated path, but wait() is idempotent-safe for the
                # completed path).
                if child.poll() is None:
                    try:
                        child.wait(timeout=REAP_TIMEOUT)
                    except subprocess.TimeoutExpired as exc:
                        raise SupervisionError(
                            "the process-group leader was not reaped"
                        ) from exc
                signal_name = None
                returncode = child.returncode
                if returncode is not None and returncode < 0:
                    signal_name = signal.Signals(-returncode).name
                self._finalize(child.pid)
                elapsed = time.monotonic() - started
                result = LaunchResult(
                    role=binding.role,
                    model=binding.model,
                    provider=binding.provider,
                    outcome=outcome,
                    returncode=returncode,
                    signal=signal_name,
                    reason=reason,
                    terminated_by=terminated_by,
                    elapsed=elapsed,
                    stdout=out.result(),
                    stderr=err.result(),
                    descendants_snapshot=len(self._captured),
                    live_descendants=len(self.live_scope()),
                    invariants=invariants,
                )
            except BaseException:
                # F1: any exception, KeyboardInterrupt, or signal received
                # after spawn still takes the bounded terminate-then-reap
                # path; no code path may leave a child running.
                self._emergency_terminate()
                raise
            if self._pending_signal is not None:
                raise SupervisionSignalInterrupt(self._pending_signal, result)
            return result
        finally:
            self._restore_signal_handlers()
            self._cleanup()

    def _emergency_terminate(self) -> None:
        """F1: bounded terminate-and-reap for the post-spawn error paths.

        Runs when an exception/:class:`KeyboardInterrupt` escapes the
        post-spawn body: the group is bounded-terminated (TERM → INT → HUP →
        KILL), the leader is reaped, and the post-snapshot orphans are
        reaped, so no path leaves a child running.  Escaped-descendant
        detection still runs; a surviving escape is re-raised (chained to
        the original error) so recovery fails closed.  Cleanup failures are
        best-effort and never mask the original failure.
        """
        child = self._child
        if child is None:
            return
        try:
            # F1: the emergency cleanup *always* bounded-terminates the full
            # process group — even when the leader has already exited (a
            # zombie or a crash-before-snapshot leader) — because a
            # pipe-holding descendant in the group can still be running.  The
            # pinned starttime identity of the *unreaped* zombie gates every
            # group signal (F4), so a reused PID is never signaled and the
            # group kill is never skipped merely because its leader is gone.
            self._terminate(child, "exception")
        except BaseException:
            pass
        try:
            if child.poll() is None:
                child.wait(timeout=REAP_TIMEOUT)
        except BaseException:
            pass
        try:
            self._reap_orphans(child.pid)
        except BaseException:
            pass
        try:
            self._finalize(child.pid)
        except EscapedDescendantError:
            raise
        except BaseException:
            pass

    def _publish_confinement_spec(self) -> Path:
        """Publish the exact confinement specification into the staging directory.

        The specification is written as a bounded no-follow single-link file
        inside the private mode-0700 exec staging directory (removed at
        cleanup).  The confine launcher reads it through a bounded no-follow
        descriptor before applying Landlock, so the bytes the launcher
        applies are exactly the bytes the proof digest binds.
        """
        if self._exec_dir is None or self._confinement_spec is None:
            raise SupervisionError(
                "cannot publish the confinement specification without a "
                "staging directory and a specification"
            )
        data = json.dumps(
            self._confinement_spec, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        if len(data) > MAX_CONFINEMENT_SPEC_BYTES:
            raise SupervisionError(
                f"the confinement specification exceeds the "
                f"{MAX_CONFINEMENT_SPEC_BYTES}-byte bound"
            )
        path = self._exec_dir / "confinement-spec.json"
        flags = (
            os.O_WRONLY | os.O_CREAT | os.O_EXCL
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0)
        )
        try:
            descriptor = os.open(str(path), flags, 0o600)
        except OSError as exc:
            raise SupervisionError(
                f"cannot create the confinement specification {path}: {exc}"
            ) from exc
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(data)
                stream.flush()
        except OSError as exc:
            try:
                os.unlink(path)
            except OSError:
                pass
            raise SupervisionError(
                f"cannot write the confinement specification {path}: {exc}"
            ) from exc
        info = path.stat()
        if stat.S_ISLNK(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.getuid():
            raise SupervisionError(
                f"the confinement specification {path} has unsafe "
                "ownership/link state"
            )
        return path

    def _close_streams(self) -> None:
        """Close the child's capture pipes if they are still open.

        Every spawn opens two pipe read ends (:attr:`subprocess.Popen.stdout`
        / ``stderr``).  The monitor closes them at EOF, but the error paths
        that bypass the monitor (crash-before-snapshot, invariant/snapshot
        failures, emergency termination) must still close them here so a
        supervised attempt never leaks an unclosed pipe
        (``ResourceWarning``).  Closing an already-closed stream is a no-op;
        failures are best-effort.
        """
        child = self._child
        if child is None:
            return
        for stream in (child.stdout, child.stderr):
            if stream is None:
                continue
            try:
                stream.close()
            except (ValueError, OSError):
                pass

    def _cleanup(self) -> None:
        """Best-effort removal of the private prompt/session/staging/home dirs."""
        self._close_streams()
        directories: List[Optional[Path]] = [
            self.session_dir, self._exec_dir, self._sanitized_home,
        ]
        if self.prompt_path is not None:
            directories.append(self.prompt_path.parent)
        _remove_private_directories(directories)


# --------------------------------------------------------------------------
# Trusted operator CLI (never invoked by a model role)
# --------------------------------------------------------------------------

# CLI exit codes: the machine-readable exit status is the only completion
# signal; the printed JSON carries the detailed bound result.
EXIT_COMPLETED = 0          # child exited on its own with returncode 0
EXIT_INVOCATION = 1         # binding/input/pre-flight fail-closed error
EXIT_TERMINATED = 2         # supervisor bounded-terminated the group
EXIT_SUPERVISION = 3        # escaped descendants / invariant / reap fail-closed
EXIT_NONZERO = 4            # child exited on its own with a nonzero status


_EXCERPT_EPILOG = (
    "derive the exact committed bytes and SHA-256 digest of one `factory-plan/v1` "
    "task section; prints one machine-readable line and exits 0 only on match."
)


class _Arguments:
    pass


def _add_common_binding(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--root", required=True, help="canonical workspace root")
    parser.add_argument("--role", required=True, choices=ROLES)
    parser.add_argument("--model", required=True)
    parser.add_argument("--provider", required=True)
    parser.add_argument("--backend", required=True, help="absolute model backend")
    parser.add_argument("--role-prompt", required=True, metavar="FILE")
    parser.add_argument(
        "--role-prompt-digest", metavar="HEX", default=None,
        help="optional claimed digest; verified against the re-derived committed "
        "blob digest and never authoritative (F5)",
    )
    parser.add_argument(
        "--prompt-set-digest", required=True, metavar="HEX",
        help="campaign-bound prompt-set digest (the only campaign binding not "
        "re-derivable from one committed blob)",
    )
    parser.add_argument("--policy", required=True, metavar="FILE", help="AGENTS.md")
    parser.add_argument(
        "--policy-digest", metavar="HEX", default=None,
        help="(optional) claimed policy digest; verified against the re-derived "
        "committed blob, never authoritative",
    )
    parser.add_argument("--spec", required=True, metavar="FILE")
    parser.add_argument(
        "--spec-digest", metavar="HEX", default=None,
        help="(optional) claimed spec digest; verified against the re-derived "
        "committed blob, never authoritative",
    )
    parser.add_argument("--plan", required=True, metavar="FILE")
    parser.add_argument(
        "--plan-digest", metavar="HEX", default=None,
        help="(optional) claimed plan digest; verified against the re-derived "
        "committed blob, never authoritative",
    )
    parser.add_argument("--bound-commit", required=True, metavar="SHA")
    parser.add_argument("--allowed-tools", required=True, metavar="a,b")
    parser.add_argument("--runtime-limit", type=float, default=DEFAULT_RUNTIME_LIMIT)
    parser.add_argument(
        "--inactivity-limit", type=float, default=DEFAULT_INACTIVITY_LIMIT
    )
    parser.add_argument("--task-id", type=int, default=None)
    parser.add_argument(
        "--task-excerpt-digest", metavar="HEX", default=None,
        help="(optional) claimed developer task-excerpt digest; verified against "
        "the re-derived committed plan section, never authoritative",
    )
    parser.add_argument("--audit-objective", metavar="FILE", default=None)
    parser.add_argument(
        "--audit-objective-digest", metavar="HEX", default=None,
        help="(optional) claimed audit-objective digest; verified against the "
        "re-derived committed blob, never authoritative",
    )
    parser.add_argument(
        "--findings", metavar="FILE", default=None,
        help="(planner only) the deterministic receipt-backed findings payload "
        "of the previous round (Task 10, FIND-01, §16); unlike the committed "
        "blobs it is ephemeral evidence under the ignored .factory-state/ "
        "namespace, so it is read anchored and bounded but never bound to a "
        "commit",
    )
    parser.add_argument(
        "--findings-digest", metavar="HEX", default=None,
        help="(optional, planner only) claimed findings-payload digest; "
        "verified against the re-derived payload bytes, never authoritative",
    )
    # Ollama usage-guard driver knobs (QUOTA-01/QUOTA-02, §10): for a
    # guard-gated provider the §10 decision table runs inside the mint
    # (authorize_launch) before any model invocation.  These options only
    # *drive* the guard (cookie store/stdin, settings URL); they can never
    # bypass it.  ``html-file`` is intentionally *absent* from the
    # production launch CLI: saved-page parsing is diagnostics/test-only,
    # reachable only through the hidden ``.factory/`` suite (Task 7
    # review, obligation 4).  An ``ollama``-provider production launch also
    # fails closed until the Task 8 confinement proof authority exists (the
    # hermetic suite passes its private synthetic proof only through the
    # API seam, never through this CLI).
    parser.add_argument(
        "--usage-guard-cookie-file", metavar="FILE", default=None,
        help="mode-0600 owned cookie store for the Ollama usage guard",
    )
    parser.add_argument(
        "--usage-guard-cookie-stdin", action="store_true",
        help="read the Ollama usage-guard cookie from stdin (private channel)",
    )
    parser.add_argument(
        "--usage-guard-settings-url", metavar="URL", default=None,
        help="settings endpoint for the Ollama usage guard",
    )
    parser.add_argument(
        "--usage-guard-poll-interval", type=int, default=None, metavar="SECONDS",
        help="Ollama usage-guard wait poll interval (bounds the wait)",
    )
    parser.add_argument(
        "--usage-guard-max-wait", type=int, default=None, metavar="SECONDS",
        help="Ollama usage-guard maximum total wait",
    )
    parser.add_argument(
        "--usage-guard-max-polls", type=int, default=None, metavar="N",
        help="Ollama usage-guard maximum polls before failing closed",
    )


def _read_blob_anchored(path_text: str, label: str, maximum: int) -> bytes:
    """Fd-anchored, no-follow, size-bounded read of one authoritative blob (F5).

    The path is opened with ``O_RDONLY|O_NOFOLLOW|O_CLOEXEC`` and read
    *through the descriptor* in bounded chunks; a symlink final component,
    a non-regular file, a file that exceeds ``maximum``, or a file that
    changes (dev/ino/size/mtime) while being read all fail closed.  No
    operator-claimed or caller-supplied path is ever trusted by name.
    """
    path = Path(path_text)
    flags = (
        os.O_RDONLY
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise InvocationError(
            f"cannot open {label} {path} with no-follow semantics: {exc}"
        ) from exc
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise InvocationError(f"{label} {path} is not a regular file")
        if before.st_size > maximum:
            raise InvocationError(
                f"{label} {path} exceeds the {maximum}-byte bound "
                f"({before.st_size} bytes)"
            )
        data = bytearray()
        while len(data) <= maximum:
            chunk = os.read(
                descriptor, min(BLOB_READ_CHUNK, maximum + 1 - len(data))
            )
            if not chunk:
                break
            data.extend(chunk)
        if len(data) > maximum:
            raise InvocationError(
                f"{label} {path} exceeds the {maximum}-byte bound"
            )
        after = os.fstat(descriptor)
        if (
            before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns
        ) != (
            after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns
        ):
            raise InvocationError(f"{label} {path} changed while being read")
        return bytes(data)
    finally:
        os.close(descriptor)


def _workspace_relative(workspace: Path, path: Path, label: str) -> str:
    """The blob's repository-relative path, or fail closed (F5).

    An authoritative blob must live inside the canonical workspace so its
    bytes can be bound to the committed blob at the bound commit; a path
    that escapes the workspace (including ``..`` traversal) is refused.
    """
    try:
        relative = Path(path).absolute().relative_to(Path(workspace).absolute())
    except ValueError as exc:
        raise InvocationError(
            f"{label} path {path} is outside the canonical workspace; the "
            "CLI accepts no caller-supplied path outside the committed "
            "workspace blobs (F5)"
        ) from exc
    text = relative.as_posix()
    if text in ("..",) or text.startswith("../"):
        raise InvocationError(
            f"{label} path {path} escapes the canonical workspace (F5)"
        )
    return text


def _committed_blob(
    workspace: Path,
    bound_commit: str,
    relpath: str,
    label: str,
    maximum: int,
) -> bytes:
    """The exact blob bytes of ``<bound_commit>:<relpath>`` (bounded).

    The committed blob is read through the pinned Git executable with a
    bounded timeout; a missing blob, a non-zero Git exit, or an over-bound
    blob fails closed, so no byte is ever accepted from a path claim.
    """
    try:
        result = git_bytes(
            ["-C", str(workspace), "show", f"{bound_commit}:{relpath}"],
            timeout=GIT_BLOB_TIMEOUT,
        )
    except GitBoundaryError as exc:
        raise InvocationError(
            f"cannot read the committed {label} blob "
            f"{bound_commit}:{relpath}: {exc}"
        ) from exc
    if result.returncode != 0:
        raise InvocationError(
            f"{label} is not tracked at the bound commit "
            f"{bound_commit}:{relpath}; the CLI accepts only committed blobs (F5)"
        )
    data = result.stdout
    if len(data) > maximum:
        raise InvocationError(
            f"committed {label} blob {relpath} exceeds the {maximum}-byte bound "
            f"({len(data)} bytes)"
        )
    return data


def _read_committed_blob(
    path_text: str,
    workspace: Path,
    bound_commit: str,
    label: str,
    maximum: int,
) -> bytes:
    """F5: the working-tree blob, digest-bound to the exact committed blob.

    The file is read through an anchored no-follow bounded descriptor and
    its SHA-256 must equal the digest of ``<bound_commit>:<relpath>`` — the
    CLI re-derives every authoritative byte itself and accepts no
    operator-claimed or caller-supplied path, blob, or binding.
    """
    path = Path(path_text)
    data = _read_blob_anchored(str(path), label, maximum)
    relpath = _workspace_relative(workspace, path, label)
    committed = _committed_blob(workspace, bound_commit, relpath, label, maximum)
    derived = hashlib.sha256(data).hexdigest()
    if derived != hashlib.sha256(committed).hexdigest():
        raise InvocationError(
            f"{label} {path} is not the exact committed blob "
            f"{bound_commit}:{relpath}; the CLI accepts no caller-supplied "
            f"or substituted bytes (F5)"
        )
    return data


def _claimed_digest_matches(label: str, derived: str, claimed: Optional[str]) -> str:
    """An operator-claimed digest is never authoritative (F5).

    ``derived`` (re-derived from the committed blob bytes) is the only
    accepted value; when a claim is supplied it must equal the derived
    digest exactly, otherwise the launch fails closed.
    """
    if claimed is None:
        return derived
    if not SHA256_RE.fullmatch(claimed):
        raise InvocationError(
            f"claimed {label} digest must be a 64-hex SHA-256 digest"
        )
    if claimed != derived:
        raise InvocationError(
            f"claimed {label} digest does not match the re-derived committed "
            f"bytes ({claimed[:16]} != {derived[:16]}); an operator-claimed "
            "digest is never authoritative (F5)"
        )
    return derived


def verify_bound_executable(
    path: Path,
    workspace: Path,
    bound_commit: str,
    label: str,
    *,
    relpath: Optional[str] = None,
    maximum: int = PROMPT_MAX_BYTES,
) -> None:
    """F2: fail closed unless ``path`` runs from exact bound-commit bytes.

    An in-workspace executable (the committed wrapper/backend) must digest-
    match the committed blob at the bound commit; a path outside the
    workspace qualifies only as an *external trusted* executable — the same
    immutable-chain authority as the pinned Git binary — so an
    operator-claimed or caller-controlled path never qualifies.
    """
    path = Path(path).absolute()
    workspace = Path(workspace).absolute()
    try:
        path.relative_to(workspace)
    except ValueError:
        try:
            require_trusted_executable(str(path))
        except GitBoundaryError as exc:
            raise InvocationError(
                f"{label} {path} is neither a committed workspace blob nor an "
                f"external trusted executable: {exc} (F2)"
            ) from exc
        return
    if relpath is None:
        relpath = _workspace_relative(workspace, path, label)
    _read_committed_blob(str(path), workspace, bound_commit, label, maximum)


# ---------------------------------------------------------------------------
# Verified-committed launch authority (F2/F5) and TOCTOU-free staging
# ---------------------------------------------------------------------------
#
# The exported programmatic launch API (``LaunchSupervision.run``) cannot
# bypass F2/F5: it requires an unforgeable verified-committed
# :class:`LaunchAuthority` token that only :func:`authorize_launch` can mint.
# The mint re-derives every authoritative byte from the committed Git blobs
# at the bound commit (F5), verifies the wrapper/backend against the F2
# boundary, stages the exact committed wrapper/backend bytes into a private
# mode-0700 directory as mode-0500 files (or revalidates an external trusted
# executable's fully resolved path), and binds the token to the verified
# bytes.  A caller can therefore never construct a token from operator
# claims — the mint is the only path that can produce one, and the CLI is
# the sole ordinary production entry because only it re-derives the
# authoritative bytes from the committed repository.


_MINT_SECRET = object()


class LaunchAuthority:
    """Unforgeable verified-committed launch token (F2/F5).

    A token is minted only by :func:`authorize_launch` after every
    authoritative byte has been verified against the committed Git blobs at
    the bound commit (F5) and the wrapper/backend have passed the F2
    boundary (committed bytes staged into a private mode-0700 directory as
    mode-0500 files, or a revalidated immutable external executable).
    ``binding`` is the *verified* binding (its backend already replaced by
    the staged executable path), ``blobs`` maps each prompt component to its
    exact verified bytes, ``wrapper`` is the exact wrapper path that will be
    executed (staged or the original workspace path), ``staged_digests``
    pins the staged files' SHA-256 digests for the exec-time re-check, and
    ``external_paths`` names the external executables that are revalidated
    immediately before exec.

    There is no public constructor that can mint a token from operator
    claims: ``authorize_launch`` is the only mint and ``run()`` rejects any
    token whose private mint marker is missing or forged.
    """

    __slots__ = (
        "_binding",
        "_blobs",
        "_wrapper",
        "_staged_digests",
        "_external_paths",
        "_exec_dir",
        "_prompt_path",
        "_session_dir",
        "_confinement_spec",
        "_confinement_proof",
        "_sanitized_home",
        "_confined_launcher",
        "_mint",
    )

    def __init__(
        self,
        binding: "InvocationBinding",
        blobs: Mapping[str, bytes],
        *,
        wrapper: Path,
        staged_digests: Mapping[str, str],
        external_paths: Sequence[str],
        exec_dir: Path,
        prompt_path: Path,
        session_dir: Path,
        confinement_spec: Optional[Mapping[str, object]] = None,
        confinement_proof: Optional[object] = None,
        sanitized_home: Optional[Path] = None,
        confined_launcher: Optional[Path] = None,
        _mint: object,
    ) -> None:
        if _mint is not _MINT_SECRET:
            raise LaunchError(
                "a LaunchAuthority token cannot be forged from operator "
                "claims; only authorize_launch() may mint a verified-committed "
                "token (F2/F5)"
            )
        self._binding = binding
        self._blobs = dict(blobs)
        self._wrapper = Path(wrapper)
        self._staged_digests = dict(staged_digests)
        self._external_paths = tuple(external_paths)
        self._exec_dir = Path(exec_dir)
        # Task 8: the exact per-launch private paths (the staging directory,
        # the prompt file, and the session directory) created by the mint are
        # carried here, so ``run`` applies and cleans up exactly the paths the
        # confinement specification's ``private_launch_rules`` bind.
        self._prompt_path = Path(prompt_path)
        self._session_dir = Path(session_dir)
        # Task 8 confinement binding: the exact ``factory-confinement/v1``
        # specification the confined child applies, the real (never synthetic)
        # confinement proof minted against it, the fresh sanitized home the
        # proof/spec bind, and the staged confine-launcher executable.  A
        # token minted without real confinement carries ``None`` for every
        # field (the hermetic synthetic-proof seam).
        self._confinement_spec = (
            dict(confinement_spec) if confinement_spec is not None else None
        )
        self._confinement_proof = confinement_proof
        self._sanitized_home = Path(sanitized_home) if sanitized_home else None
        self._confined_launcher = Path(confined_launcher) if confined_launcher else None
        self._mint = _mint


def _file_sha256(path: Path) -> str:
    """SHA-256 of a staged file, read through a no-follow descriptor."""
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        descriptor = os.open(str(path), flags)
    except OSError as exc:
        raise LaunchError(f"cannot open staged file {path}: {exc}") from exc
    try:
        digest = hashlib.sha256()
        while True:
            chunk = os.read(descriptor, BLOB_READ_CHUNK)
            if not chunk:
                break
            digest.update(chunk)
        return digest.hexdigest()
    finally:
        os.close(descriptor)


def _stage_bytes(directory: Path, name: str, data: bytes) -> Path:
    """Publish the exact committed bytes as a private mode-0500 single-link file.

    The file is created with ``O_EXCL``/``O_NOFOLLOW`` (a raced pathname is
    never reused), then chmod'ed to mode 0500 (owner read+execute, no
    write), so the executed bytes are the verified committed bytes and a
    later swap of any working-tree pathname cannot change what executes.
    The file's SHA-256 is re-checked immediately before exec.
    """
    path = directory / name
    flags = (
        os.O_WRONLY | os.O_CREAT | os.O_EXCL
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )
    try:
        descriptor = os.open(str(path), flags, 0o700)
    except OSError as exc:
        raise LaunchError(f"cannot create the staged executable {path}: {exc}") from exc
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
        os.chmod(path, STAGED_FILE_MODE)
    except OSError as exc:
        try:
            os.unlink(path)
        except OSError:
            pass
        raise LaunchError(f"cannot finalize the staged executable {path}: {exc}") from exc
    info = path.stat()
    if stat.S_ISLNK(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.getuid():
        raise LaunchError(f"the staged executable {path} has unsafe ownership/link state")
    if info.st_mode & 0o222:
        raise LaunchError(f"the staged executable {path} is writable")
    return path


def _exec_staging_dir() -> Path:
    """Fresh private mode-0700 directory for one attempt's staged executables."""
    try:
        directory = Path(tempfile.mkdtemp(prefix=EXEC_STAGING_PREFIX, dir="/tmp"))
    except OSError as exc:
        raise LaunchError(
            f"cannot create the executable staging directory: {exc}"
        ) from exc
    os.chmod(directory, STAGED_DIR_MODE)
    return directory


def _stage_launch_executables(
    binding: "InvocationBinding",
) -> Tuple[Path, Path, Dict[str, str], List[str], Path]:
    """F2/TOCTOU: stage the exact committed wrapper/backend bytes or revalidate external.

    Returns ``(wrapper, backend, staged_digests, external_paths, exec_dir)``:

    * ``wrapper`` — the exact wrapper path that will be executed (always the
      committed workspace ``scripts/pi2-secure-exec.py`` staged into the
      private directory; the wrapper is never external);
    * ``backend`` — the staged committed backend, or a fully revalidated
      external trusted executable (its resolved path, including the
      containing directories of every symlink target, is validated by the
      immutable-chain authority);
    * ``staged_digests`` — ``{path: sha256}`` for every staged file, so the
      exec-time re-check can fail closed if a staged byte was swapped;
    * ``external_paths`` — external executables revalidated immediately
      before exec (closing the verify-to-exec window for paths whose bytes
      cannot be staged);
    * ``exec_dir`` — the private mode-0700 staging directory (removed by the
      supervisor's cleanup after the attempt).
    """
    workspace = Path(binding.workspace).absolute()
    bound_commit = binding.bound_commit
    staged_digests: Dict[str, str] = {}
    external_paths: List[str] = []
    exec_dir = _exec_staging_dir()

    def stage_wrapper() -> Path:
        path = _stage_bytes(exec_dir, STAGED_WRAPPER_NAME, wrapper_bytes)
        staged_digests[str(path)] = hashlib.sha256(wrapper_bytes).hexdigest()
        return path

    # The secure wrapper is always the committed workspace blob (F2): read
    # and verify the working-tree file against the committed blob through
    # the fd-anchored no-follow boundary, then stage its exact bytes.
    wrapper_source = secure_wrapper_path(workspace)
    wrapper_bytes = _read_committed_blob(
        str(wrapper_source), workspace, bound_commit,
        "secure wrapper", PROMPT_INPUT_MAX,
    )

    # The model backend is either an in-workspace committed blob (staged
    # from its exact committed bytes) or an external trusted executable
    # (revalidated through the immutable-chain authority, including the
    # containing directories of every symlink target).  A mutable symlink
    # (whose resolved target or containing directory the caller can change)
    # fails closed here: the resolved path must be a trusted immutable
    # executable or a committed in-workspace regular file.
    backend_path = Path(binding.backend).absolute()
    backend_is_symlink = os.path.islink(str(backend_path))
    if backend_is_symlink:
        resolved = os.path.realpath(str(backend_path))
        try:
            Path(resolved).relative_to(workspace)
        except ValueError:
            # A workspace symlink whose resolved target is outside the
            # workspace is external: the full resolved path — including the
            # target's containing directories — must be a trusted immutable
            # executable.
            try:
                require_trusted_executable(resolved)
            except GitBoundaryError as exc:
                raise InvocationError(
                    f"model backend {backend_path} is a symlink whose resolved "
                    f"target {resolved} is not a trusted immutable executable: "
                    f"{exc} (F2)"
                ) from exc
            external_paths.append(resolved)
            return (stage_wrapper(), Path(resolved), staged_digests,
                    external_paths, exec_dir)
        # A workspace symlink resolving back into the workspace: the staged
        # bytes are the committed blob of the *resolved* regular file.
        backend_bytes = _read_committed_blob(
            resolved, workspace, bound_commit, "model backend", PROMPT_INPUT_MAX
        )
    else:
        try:
            backend_path.relative_to(workspace)
        except ValueError:
            # External trusted backend: revalidate the fully resolved path
            # now (immediately before exec the same check runs again).
            resolved = os.path.realpath(str(backend_path))
            try:
                require_trusted_executable(resolved)
            except GitBoundaryError as exc:
                raise InvocationError(
                    f"model backend {backend_path} is neither a committed "
                    f"workspace blob nor an external trusted executable: "
                    f"{exc} (F2)"
                ) from exc
            external_paths.append(resolved)
            return (stage_wrapper(), Path(resolved), staged_digests,
                    external_paths, exec_dir)
        backend_bytes = _read_committed_blob(
            str(backend_path), workspace, bound_commit,
            "model backend", PROMPT_INPUT_MAX,
        )
    staged_backend = _stage_bytes(exec_dir, STAGED_BACKEND_NAME, backend_bytes)
    staged_digests[str(staged_backend)] = hashlib.sha256(backend_bytes).hexdigest()
    return (stage_wrapper(), staged_backend, staged_digests, external_paths, exec_dir)


def _reject_production_loopback(
    settings_url: Optional[str], *, allow_loopback: bool
) -> None:
    """The ordinary production launch rejects ``http://`` loopback settings URLs.

    Task 7 review, obligation 14 residual: loopback ``http://`` usage
    settings transport is a diagnostics/private-test seam only.  It must
    never be reachable from the ordinary production launch CLI/API, so this
    boundary is enforced in the launch authority *before* the guard runs.

    When ``allow_loopback`` (the private diagnostics seam) is false, an
    ``http://`` settings URL naming ``127.0.0.1`` or ``localhost`` fails
    closed here.  The guard's own HTTPS/loopback rule
    (``usage._validate_settings_url``) is retained for the direct
    diagnostics path and as defense-in-depth; it does not weaken this
    launch-authority boundary.  A malformed URL and a non-loopback
    ``http://`` URL fall through to the guard, which rejects them with the
    documented fatal class.
    """
    if settings_url is None or allow_loopback:
        return
    try:
        parsed = urllib.parse.urlsplit(settings_url)
    except ValueError:
        # Malformed: the guard rejects it; do not duplicate the diagnostic.
        return
    scheme = (parsed.scheme or "").lower()
    host = (parsed.hostname or "").lower()
    if scheme == "http" and host in ("127.0.0.1", "localhost"):
        raise InvocationError(
            f"the ordinary production launch rejects the http:// loopback "
            f"settings URL {settings_url!r}: loopback http usage transport "
            "is a diagnostics/test-only seam reachable only through a "
            "private authority that is absent from the CLI and requires a "
            "synthetic confinement proof"
        )


def _gate_ollama_launch(
    binding: "InvocationBinding",
    *,
    _confinement_proof: Optional[object] = None,
    cookie_file: Optional[str] = None,
    cookie_stdin: bool = False,
    settings_url: Optional[str] = None,
    poll_interval: Optional[int] = None,
    max_wait: Optional[int] = None,
    max_polls: Optional[int] = None,
    _usage_guard_html_file: Optional[str] = None,
    _usage_guard_allow_loopback: bool = False,
) -> None:
    """The Task 7 production gate for guard-gated providers.

    An ``ollama``-provider invocation reaches the §10 guard only after the
    Task 8 confinement proof authority proves ``.factory/`` and the
    operator credential store(s) are inaccessible/read-only to model tools
    and the guard source is exact-commit bound.  Until that authority is
    available the gate fails closed.  The hermetic hidden suite passes a
    *private synthetic* proof through ``_confinement_proof`` (validated
    against the exact invocation and the exact executing guard source); a
    forged, foreign, or tampered proof fails closed.

    ``_usage_guard_allow_loopback`` is the **private diagnostics/test seam**
    for loopback ``http://`` usage settings transport (Task 7 review,
    obligations 9 and 14): it is absent from the CLI and, because it enables
    a transport the ordinary production launch must reject, it additionally
    requires a synthetic confinement proof (``_confinement_proof``).  A
    caller that sets it without a proof fails closed; the ordinary
    production launch (proof absent and the seam off) always rejects an
    ``http://`` ``127.0.0.1``/``localhost`` settings URL.

    After the confinement gate, the §10 decision table runs inside the
    mint.  The operator store is never scoped into the model workspace: the
    guard resolves its canonical operator-owned store itself and the
    launch authority passes no workspace-scoped ``env_file`` (Task 7
    review, obligation 1).
    """
    # The private loopback diagnostics seam requires a synthetic confinement
    # proof: without a validated proof no launch may enable http:// loopback
    # usage transport (forged/private flag without proof fails closed).
    if _usage_guard_allow_loopback and _confinement_proof is None:
        raise InvocationError(
            "the private loopback diagnostics seam requires a synthetic "
            "confinement proof; without a validated proof no launch may "
            "enable http:// loopback usage transport"
        )
    # Production launch never inherits an ambient/store URL.  A caller must
    # provide an explicit trusted URL; otherwise the fixed HTTPS endpoint is
    # used.  This keeps the direct guard's legacy diagnostics configurability
    # outside the production launch authority.
    effective_settings_url = settings_url or usage_guard.DEFAULT_SETTINGS_URL
    _reject_production_loopback(
        effective_settings_url, allow_loopback=_usage_guard_allow_loopback
    )
    if _confinement_proof is None:
        try:
            _confinement_proof = confinement_authority.prove_confinement(binding)
        except confinement_authority.ConfinementUnavailable as exc:
            raise InvocationError(
                "ollama-provider launch fails closed: the Task 8 "
                f"confinement proof authority is not yet available ({exc})"
            ) from exc
    if isinstance(
        _confinement_proof, real_confinement_authority.ConfinementProof
    ):
        # A *real* Task 8 proof (minted by ``prove_confinement`` when the
        # exact confinement specification is supplied) is validated against
        # the exact invocation, the executing guard-source bytes, and the
        # effective credential channels this invocation consumes.
        try:
            real_confinement_authority.validate_proof(
                _confinement_proof,
                binding,
                cookie_file=cookie_file,
                cookie_stdin=cookie_stdin,
            )
        except real_confinement_authority.ConfinementError as exc:
            raise InvocationError(
                "ollama-provider launch fails closed: the real confinement "
                f"proof does not bind this invocation ({exc})"
            ) from exc
    else:
        try:
            confinement_authority.validate_proof(_confinement_proof, binding)
        except confinement_authority.ConfinementError as exc:
            raise InvocationError(
                "ollama-provider launch fails closed: the confinement proof does "
                f"not bind this invocation ({exc})"
            ) from exc
    try:
        usage_guard.require_quota(
            cookie_file=cookie_file,
            cookie_stdin=cookie_stdin,
            settings_url=effective_settings_url,
            poll_interval=poll_interval,
            max_wait=max_wait,
            max_polls=max_polls,
            html_file=_usage_guard_html_file,
        )
    except usage_guard.WaitInterrupted:
        # A TERM/INT/HUP during the initial check, the wait, or the final
        # check already terminated and reaped the fetch child; propagate so
        # the CLI exits 128+signum (Task 7 review, obligation 12).
        raise
    except usage_guard.UsageGuardError as exc:
        raise InvocationError(
            f"ollama usage guard blocked the invocation: {exc}"
        ) from exc


def authorize_launch(
    binding: "InvocationBinding",
    *,
    role_prompt: bytes,
    agents: bytes,
    spec: bytes,
    plan: bytes,
    audit_objective: Optional[bytes] = None,
    task_excerpt: Optional[bytes] = None,
    findings: Optional[bytes] = None,
    usage_guard_cookie_file: Optional[str] = None,
    usage_guard_cookie_stdin: bool = False,
    usage_guard_settings_url: Optional[str] = None,
    usage_guard_poll_interval: Optional[int] = None,
    usage_guard_max_wait: Optional[int] = None,
    usage_guard_max_polls: Optional[int] = None,
    _confinement_proof: Optional[object] = None,
    _confinement_spec: Optional[Mapping[str, object]] = None,
    _sanitized_home: Optional[Path] = None,
    _usage_guard_html_file: Optional[str] = None,
    _usage_guard_allow_loopback: bool = False,
) -> LaunchAuthority:
    """Mint the unforgeable verified-committed authority token (F2/F5).

    Every authoritative byte is verified against the committed Git blobs at
    the bound commit: the wrapper and backend pass the F2 boundary (the
    exact committed bytes are staged into a private mode-0700 directory as
    mode-0500 files, or an external trusted executable's fully resolved path
    is revalidated) and each prompt blob's digest must equal the binding's
    digest — a substituted, paraphrased, foreign, or operator-claimed byte
    set fails closed.  The returned :class:`LaunchAuthority` is the only
    value :meth:`LaunchSupervision.run` accepts; it cannot be constructed
    from operator claims.

    **Mandatory real confinement (Task 8 review, finding 5).**  Real model
    workspace confinement is mandatory for *every* provider and *every*
    public authorize API, CLI or programmatic: the mint creates the
    per-launch private staging/prompt/session paths **first**, augments the
    caller's ``factory-confinement/v1`` specification with the exact
    ``private_launch_rules`` for those paths, and **then** mints the real
    confinement proof against the augmented specification — proving the
    Landlock primitive is applicable and binding the exact specification
    digest, the exact executing guard-source bytes, and every effective
    credential channel this invocation consumes.  The only exception is the
    explicit *private* synthetic-proof seam (``_confinement_proof``),
    reachable only by the hidden ``.factory/`` suite; a launch carrying
    neither the real specification nor that seam fails closed for any
    provider or entry.

    ``_confinement_spec`` is the base ``factory-confinement/v1``
    specification (Task 8) the caller builds for the binding (the per-role
    allowlists plus the sanitized home).  The mint augments it with the
    exact per-launch private rules (staging/prompt/session paths) before the
    real proof is minted, so the proof's digest binds exactly what the
    confined child will apply, and the authority carries those exact paths
    so :meth:`LaunchSupervision.run` uses — and cleans up — exactly the
    paths the proof binds.  ``_sanitized_home`` is the fresh private
    mode-0700 home the specification binds; the supervisor removes it after
    the attempt.  A caller that supplies a confinement specification but no
    proof fails closed; the hermetic hidden suite's synthetic proof seam
    (``_confinement_proof``) never produces real confinement and is never
    evidence of it.

    **Cleanup on authorization failure (Task 8 review, finding 6).**  Any
    failure to authorize or confine the launch removes and cleans every
    per-launch private directory the mint created — the staging directory,
    the prompt directory, the session directory, and the sanitized home —
    so no private or credential material survives a failed authorization.

    ``_usage_guard_html_file`` is a **private test seam only** (Task 7
    review, obligation 4): saved-page parsing is diagnostics/test-only and
    must never appear on the production surface, so the *public* signature
    and the CLI expose no ``html-file`` option.  Only the hidden
    ``.factory/`` suite reaches this seam.

    ``_usage_guard_allow_loopback`` is the **private diagnostics/test seam**
    for loopback ``http://`` usage settings transport (Task 7 review,
    obligations 9 and 14).  The ordinary production launch CLI/API has no
    such option and always rejects an ``http://`` ``127.0.0.1``/``localhost``
    settings URL; this underscore-private authority — absent from the CLI —
    is the only way the hermetic hidden suite can exercise the loopback
    transport, and it additionally requires a synthetic confinement proof
    (``_confinement_proof``): setting it without a proof fails closed, and
    a forged/foreign/tampered proof is never accepted.

    **Ollama production gate (Task 7 review, obligation 2).**  An
    ``ollama``-provider invocation does not reach the model until the Task
    8 confinement authority *proves* that ``.factory/`` and the operator
    credential store(s) are inaccessible/read-only to model tools and the
    guard source is exact-commit bound.  The hermetic hidden suite mints a
    *private synthetic* proof (``confinement._mint_synthetic_proof``) and
    passes it through the private ``_confinement_proof`` seam; a forged,
    foreign, or tampered proof fails closed, and a synthetic proof is never
    evidence of real confinement.  The production mint (``_confinement_spec``
    supplied) proves real confinement before the guard runs.

    **Ollama usage guard (QUOTA-01, QUOTA-02; §10).**  For a guard-gated
    provider the §10 decision table runs *inside* the mint (after the real
    confinement proof binds the augmented specification): ``--check`` exit 0
    proceeds; exit 1 or 3 runs ``--wait`` and then one final ``--check``
    that must exit 0; any fatal or nonzero ``--wait`` outcome raises
    :class:`InvocationError` so the campaign terminates without invoking the
    model.  The guard's cookie never appears in a child argv or child
    environment (private stdin channel, built child environment, bounded
    mode-0600/no-follow owned stores outside the model workspace,
    zeroization, redacted output), and the ``--usage-guard-*`` options are
    the operator diagnostics/driver knobs (cookie store/stdin, settings
    URL) — a caller can never bypass the guard for a guard-gated provider.
    ``html-file`` is *not* part of the production surface: saved-page
    parsing is diagnostics/test-only, reachable only through the hidden
    ``.factory/`` suite (Task 7 review, obligation 4).  A TERM/INT/HUP
    during the guard propagates :class:`usage_guard.WaitInterrupted` so the
    CLI exits ``128 + signum`` after reaping the credential-holding fetch
    child.
    """
    verify_invocation(binding)
    # ---- Task 8 mandatory confinement contract (every provider, every
    # public entry; review finding 5) ----
    # Real model workspace confinement (the exact factory-confinement/v1
    # specification) is mandatory for every provider and every public
    # authorize API, CLI or programmatic.  The only exception is the
    # explicit *private* synthetic-proof test seam (``_confinement_proof``),
    # reachable only by the hidden ``.factory/`` suite; a launch carrying
    # neither the real specification nor that seam fails closed for any
    # provider or entry.
    if _confinement_spec is None and _confinement_proof is None:
        raise InvocationError(
            "every launch must carry a confinement proof for the real model "
            "workspace confinement (the exact factory-confinement/v1 "
            "specification) or the explicit private synthetic-proof test seam; "
            "an unconfined launch is never "
            "permitted for any provider or entry (Task 8)"
        )
    if _confinement_spec is not None and _sanitized_home is None:
        raise InvocationError(
            "a real confined launch requires the fresh sanitized home "
            "the specification binds"
        )
    _verify_input_digest("role prompt", role_prompt, binding.role_prompt_digest)
    _verify_input_digest("operational policy", agents, binding.policy_digest)
    _verify_input_digest("specification", spec, binding.specification_digest)
    _verify_input_digest("implementation plan", plan, binding.plan_digest)
    blobs: Dict[str, bytes] = {
        "role_prompt": role_prompt,
        "policy": agents,
        "spec": spec,
        "plan": plan,
    }
    if binding.role == "developer":
        if task_excerpt is None:
            raise InvocationError(
                "the developer launch requires the exact task-excerpt bytes"
            )
        verify_task_excerpt(
            task_excerpt, binding.task_excerpt_digest, task_id=binding.task_id
        )
        derived = task_excerpt_bytes(plan, binding.task_id)
        if task_excerpt != derived:
            raise InvocationError(
                "the delivered task-excerpt bytes differ from the committed "
                f"plan section Task {binding.task_id}; refusing a "
                "substituted/paraphrased task (F5)"
            )
        blobs["task_excerpt"] = task_excerpt
    if binding.role == "auditor":
        if audit_objective is None:
            raise InvocationError(
                "the auditor launch requires the audit-objective bytes"
            )
        _verify_input_digest(
            "audit objective", audit_objective, binding.audit_objective_digest
        )
        blobs["audit_objective"] = audit_objective
    if binding.role == "planner" and findings is not None:
        # Task 10 §16: the receipt-backed findings payload is a digest-bound
        # planner input (never a developer/tester/auditor input, never a
        # runtime task ledger).
        if not binding.findings_digest:
            raise InvocationError(
                "the planner findings payload requires a findings digest"
            )
        _verify_input_digest("findings", findings, binding.findings_digest)
        blobs["findings"] = findings
    # ---- staging/prompt/session creation FIRST (Task 8 reorder) ----
    # The private per-launch paths must exist before the confinement
    # specification is finalized, because the specification's
    # ``private_launch_rules`` bind the exact staging/prompt/session paths
    # the confined child will use; the real proof is minted only against
    # that augmented specification, and the authority carries the exact
    # paths so ``run`` applies and cleans up exactly what the proof binds.
    wrapper, backend, staged_digests, external_paths, exec_dir = (
        _stage_launch_executables(binding)
    )
    private_dirs: List[Path] = [exec_dir]
    try:
        # Task 8: stage the committed confine launcher into the same private
        # staging directory (F2), so the model child always runs through the
        # exact committed launcher blob when real confinement is bound.
        confined_launcher: Optional[Path] = None
        if _confinement_spec is not None:
            launcher_source = Path(binding.workspace).absolute() / CONFINE_LAUNCHER
            launcher_bytes = _read_committed_blob(
                str(launcher_source), Path(binding.workspace).absolute(),
                binding.bound_commit, "confine launcher", MAX_CONFINEMENT_SPEC_BYTES,
            )
            confined_launcher = _stage_bytes(
                exec_dir, STAGED_CONFINE_LAUNCHER_NAME, launcher_bytes
            )
            staged_digests[str(confined_launcher)] = hashlib.sha256(
                launcher_bytes
            ).hexdigest()
        prompt = compose_prompt(
            binding,
            role_prompt=blobs["role_prompt"],
            agents=blobs["policy"],
            spec=blobs["spec"],
            plan=blobs["plan"],
            audit_objective=blobs.get("audit_objective"),
            task_excerpt=blobs.get("task_excerpt"),
            findings=blobs.get("findings"),
        )
        prompt_dir = _prompt_directory()
        private_dirs.append(prompt_dir)
        prompt_path = write_prompt_file(prompt_dir, prompt)
        session_dir = _session_directory()
        private_dirs.append(session_dir)
        if _sanitized_home is not None:
            private_dirs.append(Path(_sanitized_home).absolute())
        # ---- augment the specification with the exact per-launch private
        # rules, THEN mint the real proof (the proof digest binds the
        # augmented spec the confined child will apply) ----
        confinement_spec: Optional[Dict[str, object]] = None
        real_proof: Optional[object] = None
        if _confinement_spec is not None:
            confinement_spec = real_confinement_authority.with_private_launch_paths(
                dict(_confinement_spec),
                staging_dir=exec_dir,
                prompt_path=prompt_path,
                session_dir=session_dir,
            )
            try:
                real_proof = real_confinement_authority.prove_confinement(
                    binding,
                    confinement_spec=confinement_spec,
                    cookie_file=usage_guard_cookie_file,
                    cookie_stdin=usage_guard_cookie_stdin,
                )
                real_confinement_authority.validate_proof(
                    real_proof,
                    binding,
                    confinement_spec=confinement_spec,
                    cookie_file=usage_guard_cookie_file,
                    cookie_stdin=usage_guard_cookie_stdin,
                )
            except real_confinement_authority.ConfinementUnavailable as exc:
                raise InvocationError(
                    "the production launch fails closed: the real model "
                    f"workspace confinement is unavailable on this host ({exc})"
                ) from exc
            except real_confinement_authority.ConfinementError as exc:
                raise InvocationError(
                    "the production launch fails closed: the real confinement "
                    f"proof cannot bind this invocation ({exc})"
                ) from exc
        if binding.provider.lower() in PROVIDER_GUARD_REQUIRED:
            _gate_ollama_launch(
                binding,
                _confinement_proof=_confinement_proof or real_proof,
                cookie_file=usage_guard_cookie_file,
                cookie_stdin=usage_guard_cookie_stdin,
                settings_url=usage_guard_settings_url,
                poll_interval=usage_guard_poll_interval,
                max_wait=usage_guard_max_wait,
                max_polls=usage_guard_max_polls,
                _usage_guard_html_file=_usage_guard_html_file,
                _usage_guard_allow_loopback=_usage_guard_allow_loopback,
            )
        verified = replace(binding, backend=backend)
        return LaunchAuthority(
            verified,
            blobs,
            wrapper=wrapper,
            staged_digests=staged_digests,
            external_paths=external_paths,
            exec_dir=exec_dir,
            prompt_path=prompt_path,
            session_dir=session_dir,
            confinement_spec=confinement_spec,
            confinement_proof=real_proof,
            sanitized_home=_sanitized_home,
            confined_launcher=confined_launcher,
            _mint=_MINT_SECRET,
        )
    except BaseException:
        # Task 8 review, finding 6: every private per-launch directory this
        # mint created — the staging directory, the prompt directory, the
        # session directory, and the sanitized home — is removed on any
        # authorization failure, so no private or credential material
        # survives a failed authorization.
        _remove_private_directories(private_dirs)
        raise


def require_trusted_interpreter() -> str:
    """The absolute trusted interpreter that will execute the staged wrapper.

    The interpreter is ``sys.executable`` — the already-resolved absolute
    path of the running control-plane interpreter, never a ``python3``
    re-derived from a caller-controlled ``PATH``.  It must be absolute and
    pass the same immutable-chain authority as the pinned Git binary, so an
    attacker-controlled or caller-writable interpreter can never execute the
    staged wrapper/backend (F4-style bounded interpreter resolution).
    """
    executable = sys.executable
    if not executable or not executable.startswith("/"):
        raise InvocationError(
            f"the interpreter {executable!r} is not an absolute trusted path; "
            "refusing to launch through a caller-resolved interpreter"
        )
    try:
        require_trusted_executable(executable)
    except GitBoundaryError as exc:
        raise InvocationError(
            f"the interpreter {executable!r} is not a trusted absolute "
            f"executable: {exc}; interpreter resolution is bounded to the "
            "trusted set and never follows an attacker-controlled path"
        ) from exc
    return executable


def _child_reset_spawn_mask() -> None:
    """Child-side restoration of the signal mask across the spawn window.

    ``pthread_sigmask`` blocks TERM/INT/HUP on the *launch thread* across
    the ``Popen`` fork so a signal received before the child identity is
    recorded becomes pending and is delivered only after identity recording.
    POSIX inherits that mask across ``fork``/``exec``, so without this the
    fresh process would silently ignore TERM/INT/HUP for its whole life;
    this tiny child callback (only ``pthread_sigmask``, no locks beyond the
    re-entrant GIL the fork already holds) restores an empty mask right
    after the fork, before exec.
    """
    signal.pthread_sigmask(signal.SIG_SETMASK, [])


# ---------------------------------------------------------------------------
# Committed machine-result schema (factory-launch-result/v1)
# ---------------------------------------------------------------------------

def _load_result_schema() -> Dict[str, object]:
    """Load the committed ``factory-launch-result/v1`` schema (bounded)."""
    here = Path(__file__).resolve().parents[1]  # .factory/
    path = here / "schemas" / RESULT_SCHEMA_FILE
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise LaunchError(
            f"cannot load the committed result schema {path}: {exc}"
        ) from exc
    if len(data) > PROMPT_INPUT_MAX:
        raise LaunchError(
            f"the committed result schema {path} exceeds the bounded size"
        )
    try:
        schema = json.loads(data)
    except ValueError as exc:
        raise LaunchError(
            f"the committed result schema {path} is not valid JSON: {exc}"
        ) from exc
    if not isinstance(schema, dict):
        raise LaunchError(f"the committed result schema {path} is not an object")
    return schema


def _json_type_of(value: object) -> str:
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
    raise ResultSchemaError(
        f"launch-result value of type {type(value).__name__} is not JSON-serializable"
    )


_json_type = _json_type_of


def _schema_check(instance: object, schema: object, path: str) -> None:
    """Validate ``instance`` against the JSON-Schema subset used by the schema.

    Supports ``type`` (including unions), ``enum``, ``required``,
    ``properties``, ``additionalProperties``, ``items``, ``minLength``,
    ``pattern``, and ``minimum`` — the exact subset the committed
    ``factory-launch-result/v1`` schema uses.
    """
    if not isinstance(schema, dict):
        return
    expected = schema.get("type")
    if expected is not None:
        types = expected if isinstance(expected, list) else [expected]
        if _json_type(instance) not in types:
            raise ResultSchemaError(
                f"launch-result violation at {path or 'root'}: expected type "
                f"{expected!r}, got {_json_type(instance)!r}"
            )
    if "enum" in schema and instance not in schema["enum"]:
        raise ResultSchemaError(
            f"launch-result violation at {path or '(root)'}: value {instance!r} "
            f"is not one of {schema['enum']!r}"
        )
    if isinstance(instance, str):
        if "minLength" in schema and len(instance) < schema["minLength"]:
            raise ResultSchemaError(
                f"launch-result violation at {path or '(root)'}: string shorter "
                f"than the schema minimum"
            )
        if "pattern" in schema and re.fullmatch(schema["pattern"], instance) is None:
            raise ResultSchemaError(
                f"launch-result violation at {path or '(root)'}: string does not "
                f"match the schema pattern {schema['pattern']!r}"
            )
    if isinstance(instance, (int, float)) and not isinstance(instance, bool):
        if "minimum" in schema and instance < schema["minimum"]:
            raise ResultSchemaError(
                f"launch-result violation at {path or '(root)'}: value {instance!r} "
                f"is below the schema minimum {schema['minimum']!r}"
            )
    if isinstance(instance, dict):
        properties = schema.get("properties", {})
        for key in schema.get("required", []):
            if key not in instance:
                raise ResultSchemaError(
                    f"launch-result violation at {path or '(root)'}: missing "
                    f"required property {key!r}"
                )
        for key, subschema in properties.items():
            if key in instance:
                _schema_check(instance[key], subschema, f"{path}.{key}")
        if schema.get("additionalProperties") is False:
            for key in instance:
                if key not in properties:
                    raise ResultSchemaError(
                        f"launch-result violation at {path or '(root)'}: unexpected "
                        f"property {key!r}"
                    )
    if isinstance(instance, list):
        items = schema.get("items")
        if items is not None:
            for index, item in enumerate(instance):
                _schema_check(item, items, f"{path}[{index}]")


_RESULT_SCHEMA: Optional[Dict[str, object]] = None


def validate_launch_result(result: object) -> None:
    """Fail closed unless ``result.to_dict()`` conforms to the committed schema.

    The machine-readable exit status is the only completion signal; its JSON
    field set is validated against the committed ``factory-launch-result/v1``
    schema (:data:`RESULT_SCHEMA_FILE`) so a result can never gain, drop, or
    mistype a field without failing closed.
    """
    global _RESULT_SCHEMA
    if _RESULT_SCHEMA is None:
        _RESULT_SCHEMA = _load_result_schema()
    if isinstance(result, LaunchResult):
        instance = result.to_dict()
    elif isinstance(result, Mapping):
        instance = dict(result)
    else:
        raise ResultSchemaError(
            f"a launch result must be a LaunchResult or mapping, got "
            f"{type(result).__name__}"
        )
    _schema_check(instance, _RESULT_SCHEMA, "")


def _verify_commit(workspace: Path, bound_commit: str) -> None:
    head = resolve_head(workspace)
    if head != bound_commit:
        raise InvocationError(
            f"workspace HEAD {head!r} does not match the bound commit "
            f"{bound_commit!r}; the checkout is not at the bound Git state"
        )


def _run_cli(args: argparse.Namespace) -> int:
    try:
        allowed = tuple(args.allowed_tools.split(","))
        root = Path(args.root).absolute()
        bound_commit = args.bound_commit
        if not SHA40_RE.fullmatch(bound_commit):
            raise InvocationError(
                "`--bound-commit` must be a 40-hex Git commit hash"
            )
        _verify_commit(root, bound_commit)
        # F5: every authoritative blob (role prompt, policy, spec, plan) is
        # re-derived through anchored no-follow bounded reads of the committed
        # blobs; every digest below is derived, never operator-claimed.
        role_prompt = _read_committed_blob(
            args.role_prompt, root, bound_commit, "role prompt", PROMPT_INPUT_MAX
        )
        policy = _read_committed_blob(
            args.policy, root, bound_commit, "operational policy", PROMPT_INPUT_MAX
        )
        spec = _read_committed_blob(
            args.spec, root, bound_commit, "specification", PROMPT_INPUT_MAX
        )
        plan = _read_committed_blob(
            args.plan, root, bound_commit, "implementation plan", PROMPT_INPUT_MAX
        )
        role_prompt_digest = _claimed_digest_matches(
            "role-prompt", hashlib.sha256(role_prompt).hexdigest(),
            args.role_prompt_digest,
        )
        policy_digest = _claimed_digest_matches(
            "policy", hashlib.sha256(policy).hexdigest(), args.policy_digest
        )
        spec_digest = _claimed_digest_matches(
            "spec", hashlib.sha256(spec).hexdigest(), args.spec_digest
        )
        plan_digest = _claimed_digest_matches(
            "plan", hashlib.sha256(plan).hexdigest(), args.plan_digest
        )
        audit_objective = None
        audit_objective_digest = ""
        if args.role == "auditor":
            if not args.audit_objective:
                raise InvocationError(
                    "the auditor role requires --audit-objective"
                )
            audit_objective = _read_committed_blob(
                args.audit_objective, root, bound_commit,
                "audit objective", PROMPT_INPUT_MAX,
            )
            audit_objective_digest = _claimed_digest_matches(
                "audit-objective",
                hashlib.sha256(audit_objective).hexdigest(),
                args.audit_objective_digest,
            )
        task_excerpt = None
        task_excerpt_digest = None
        if args.role == "developer":
            task_excerpt, task_excerpt_digest = derive_task_excerpt(
                plan, args.task_id
            )
            _claimed_digest_matches(
                "task-excerpt", task_excerpt_digest, args.task_excerpt_digest
            )
        # Task 10 §16 (FIND-01): the deterministic receipt-backed findings
        # payload of the previous round is a planner-only input.  Unlike the
        # committed authoritative blobs it is ephemeral evidence under the
        # ignored ``.factory-state/`` namespace, so it is read through the
        # anchored no-follow bounded reader (never by name, never a commit
        # binding) and digest-verified against the claimed digest; a
        # substituted or paraphrased payload fails closed, and any findings
        # argument on a non-planner role is rejected outright.
        findings = None
        findings_digest = ""
        if args.findings or args.findings_digest:
            if args.role != "planner":
                raise InvocationError(
                    "--findings/--findings-digest are allowed only for the "
                    "planner role; the findings payload never reaches the "
                    "developer, tester, or auditor"
                )
            if not args.findings:
                raise InvocationError(
                    "the planner findings payload requires --findings"
                )
            findings = _read_blob_anchored(
                args.findings, "findings payload", PROMPT_INPUT_MAX
            )
            findings_digest = _claimed_digest_matches(
                "findings", hashlib.sha256(findings).hexdigest(),
                args.findings_digest,
            )
        binding = InvocationBinding(
            role=args.role,
            model=args.model,
            provider=args.provider,
            backend=Path(args.backend),
            workspace=root,
            bound_commit=bound_commit,
            role_prompt_digest=role_prompt_digest,
            prompt_set_digest=args.prompt_set_digest,
            plan_digest=plan_digest,
            policy_digest=policy_digest,
            specification_digest=spec_digest,
            allowed_tools=allowed,
            task_id=args.task_id,
            task_excerpt_digest=task_excerpt_digest,
            audit_objective_digest=audit_objective_digest,
            findings_digest=findings_digest,
            runtime_limit=args.runtime_limit,
            inactivity_limit=args.inactivity_limit,
        )
        verify_invocation(binding)
        # Task 8 production confinement: every ordinary launch is confined.
        # The control plane creates a fresh private sanitized home and the
        # exact per-role confinement specification; ``authorize_launch``
        # mints the real (Landlock) proof against that specification before
        # the guard runs, and ``LaunchSupervision`` routes the model child
        # through the committed confine launcher.  A host without the
        # Landlock primitive fails closed before any model can start.
        sanitized_home = real_confinement_authority.sanitized_home_directory()
        try:
            confinement_spec = real_confinement_authority.confinement_spec(
                binding, sanitized_home=sanitized_home
            )
        except BaseException:
            try:
                shutil.rmtree(sanitized_home)
            except OSError:
                pass
            raise
        # F2/F5: mint the unforgeable verified-committed authority.  The mint
        # verifies the wrapper/backend against the committed blobs (or the
        # immutable external authority), stages the exact committed bytes into
        # a private mode-0700 directory as mode-0500 files (or revalidates
        # the external path), and binds the verified prompt bytes to the
        # token; ``run`` accepts only this token, so the CLI stays the sole
        # ordinary launch entry and the exported API cannot bypass F2/F5.
        authority = authorize_launch(
            binding,
            role_prompt=role_prompt,
            agents=policy,
            spec=spec,
            plan=plan,
            audit_objective=audit_objective,
            task_excerpt=task_excerpt,
            findings=findings,
            usage_guard_cookie_file=args.usage_guard_cookie_file,
            usage_guard_cookie_stdin=args.usage_guard_cookie_stdin,
            usage_guard_settings_url=args.usage_guard_settings_url,
            usage_guard_poll_interval=args.usage_guard_poll_interval,
            usage_guard_max_wait=args.usage_guard_max_wait,
            usage_guard_max_polls=args.usage_guard_max_polls,
            _confinement_spec=confinement_spec,
            _sanitized_home=sanitized_home,
        )
        supervisor = LaunchSupervision(binding)
        result = supervisor.run(authority)
        # The machine-readable exit status is the only completion signal; it
        # must conform to the committed result schema before it is printed.
        validate_launch_result(result)
    except EscapedDescendantError as exc:
        print(f"factory-launch: escaped-descendant fail-closed: {exc}", file=sys.stderr)
        return EXIT_SUPERVISION
    except SupervisionSignalInterrupt as exc:
        if exc.result is not None:
            validate_launch_result(exc.result)
            print(json.dumps(
                exc.result.to_dict(), sort_keys=True, separators=(",", ":")
            ))
        print(
            f"factory-launch: interrupted by {signal.Signals(exc.signum).name} "
            "after bounded termination and reap",
            file=sys.stderr,
        )
        return 128 + exc.signum
    except SupervisionError as exc:
        print(f"factory-launch: supervision fail-closed: {exc}", file=sys.stderr)
        return EXIT_SUPERVISION
    except usage_guard.WaitInterrupted as exc:
        # A TERM/INT/HUP during the §10 guard (initial check, wait, or final
        # check) already terminated and reaped the credential-holding fetch
        # child; the machine-readable exit is 128 + signum (Task 7 review,
        # obligation 12).
        print(
            f"factory-launch: ollama usage guard interrupted by "
            f"{signal.Signals(exc.signum).name} after bounded termination "
            "and reap",
            file=sys.stderr,
        )
        return 128 + exc.signum
    except InvocationError as exc:
        print(f"factory-launch: {exc}", file=sys.stderr)
        return EXIT_INVOCATION
    print(json.dumps(result.to_dict(), sort_keys=True, separators=(",", ":")))
    if result.outcome == "terminated":
        return EXIT_TERMINATED
    if result.returncode != 0:
        return EXIT_NONZERO
    return EXIT_COMPLETED


def _excerpt_cli(args: argparse.Namespace) -> int:
    try:
        plan = _read_blob_anchored(
            args.plan, "implementation plan", PROMPT_INPUT_MAX
        )
        excerpt, digest = derive_task_excerpt(plan, args.task_id)
    except InvocationError as exc:
        print(f"factory-launch: {exc}", file=sys.stderr)
        return EXIT_INVOCATION
    print(json.dumps(
        {"task_id": args.task_id, "bytes": len(excerpt), "digest": digest},
        sort_keys=True,
        separators=(",", ":"),
    ))
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Trusted operator entrypoint for one fresh-context launch."""
    import argparse

    parser = argparse.ArgumentParser(
        prog="factory-launch",
        description=(
            "Fresh-context execution, invocation contract, and supervision "
            "(FACTORY-LOOP-SPEC §9/§20; Task 6)."
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)
    p_launch = sub.add_parser("launch", help="run one supervised role attempt")
    _add_common_binding(p_launch)
    p_excerpt = sub.add_parser(
        "excerpt", help="derive the exact committed task-section digest"
    )
    p_excerpt.add_argument("--plan", required=True, metavar="FILE")
    p_excerpt.add_argument("--task-id", required=True, type=int)
    args = parser.parse_args(argv)
    if args.command == "launch":
        return _run_cli(args)
    return _excerpt_cli(args)


if __name__ == "__main__":
    sys.exit(main())
