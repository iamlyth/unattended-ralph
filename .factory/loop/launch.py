#!/usr/bin/env python3
"""Fresh-context execution, invocation contract, and supervision (Task 6).

This module implements the fresh-context boundary of
``docs/FACTORY-LOOP-SPEC.md`` (CTX-01, TASK-02, PROC-01; §5, §9, §12, §17,
§20) as the trusted control-plane module ``.factory/loop/launch.py``.  It
is the deterministic Task-6 deliverable:

* **Fresh process per role.** Every role attempt starts a *new* process via
  the existing secure wrapper (``.factory/tools/pi2-secure-exec.py``, invoked never
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
* **Dedicated-broker supervision (review-owned F6/F7)**.  Every role runs
  beneath the exact-commit confinement/exec broker.  That fresh process has
  no pre-existing children, installs itself as a child subreaper before its
  target fork, and ptrace-pins the complete target lineage.  It therefore
  kills and reaps only identities proved by its own child namespace; the
  outer coordinator never infers ownership by subtracting a baseline of
  unrelated children.  A pre-existing coordinator child may fork and exit
  during an attempt without its worker or exit status being touched.
* **Subreaper / reaping (F7)**.  A double-fork or ``setsid`` descendant is
  adopted inside the dedicated broker lineage and is ptrace-pinned,
  bounded-SIGKILLed, reaped, and reported over the broker-only lifecycle
  channel before the outer launch can complete.  The broker's one-byte
  clean/escaped/failure verdict is unforgeable by the target, and PID/PGID
  reuse outside that lineage cannot widen cleanup ownership.
* **Bounded termination**.  A hard runtime limit and an inactivity limit
  bound every run.  Termination delivers **TERM, INT, and HUP to the full
  process group**, observes a bounded grace, escalates to **KILL** of the
  whole group, then verifies the group is gone and reaps the leader within
  a bound.  Escaped descendants and un-reaped groups fail closed.
* **Lock boundary**.  The child inherits no lock descriptor (``close_fds``;
  only descriptor-anchored Landlock rules are passed and consumed before
  model exec) and no lock/Git metadata; the invariants
  are re-verified against ``/proc/<pid>`` per launch (session identity,
  environment strip, no root-inode descriptor).
* **Dirty work preservation**.  Supervision never touches the workspace
  contents; a crashed or interrupted attempt leaves its dirty work intact
  and never silently overwrites it.
* **Structured bounded results**.  The only completion signal is the
  machine-readable :class:`LaunchResult` (exit status, signal, reason,
  bounded per-stream digest/tail, snapshot counts) — never a
  model-completion marker and never raw unbounded output, so no credential
  material can be carried in a result.
"""

from __future__ import annotations

import argparse
import ctypes
import fcntl
import functools
import hashlib
import json
import math
import os
from pathlib import Path
import re
import secrets
import selectors
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
    from . import workspace_confinement as real_confinement_authority
    from . import redaction as output_redaction
    from . import task_budget as task_budget_module
    from .gitutil import (
        GIT_ENV_STRIP,
        GitBoundaryError,
        git_bytes,
        require_trusted_executable,
        require_trusted_regular_file,
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
    import workspace_confinement as real_confinement_authority  # type: ignore[no-redef]
    import redaction as output_redaction  # type: ignore[no-redef]
    import task_budget as task_budget_module  # type: ignore[no-redef]
    from gitutil import (  # type: ignore[no-redef]
        GIT_ENV_STRIP,
        GitBoundaryError,
        git_bytes,
        require_trusted_executable,
        require_trusted_regular_file,
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
    "BUDGET_CAPTURE_MINIMUM",
    "BUDGET_REASON_PREFIX",
    "BUDGET_SAMPLE_INTERVAL",
    "BudgetExhaustedError",
    "BudgetUsage",
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
# or caller-claimed provider fails closed and can never bypass fixed provider
# policy. ``ollama`` retains the Task 8 confinement/source proof; its quota
# decision belongs to the campaign pre-round registry. ``synthetic`` is the
# hermetic hidden-suite provider (no network or real model backend).
SUPPORTED_PROVIDERS = frozenset({"ollama", "openai-codex", "synthetic"})

# Providers that require the exact staged usage-source and credential-store
# confinement proof. This is not a quota-decision table: quota is never run
# inside ``authorize_launch``.
PROVIDER_GUARD_REQUIRED = frozenset({"ollama"})
CANONICAL_OLLAMA_SETTINGS_URL = "https://ollama.com/settings"

# The existing secure wrapper — invoked, never reimplemented (§18).
SECURE_WRAPPER = ".factory/tools/pi2-secure-exec.py"
PI2_BACKEND_ADAPTER = ".factory/loop/pi2_backend.py"

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
DEFAULT_RUNTIME_LIMIT = 7200.0
# Pi emits no supervisor-visible bytes while its tool loop is active. A full
# serial project/factory verification turn can therefore be externally silent
# for close to an hour even though every inner command is independently
# bounded. Keep a finite inactivity bound below the two-hour hard role limit;
# the six-hour campaign deadline remains the stronger whole-campaign bound.
DEFAULT_INACTIVITY_LIMIT = 7000.0

# Termination sequence: TERM, INT, and HUP are each delivered to the *full
# process group* before the bounded grace expires and KILL escalates (§9).
TERMINATION_SIGNALS = (signal.SIGTERM, signal.SIGINT, signal.SIGHUP)

# Phase 2A task-resource budget: the supervisor samples the live process
# tree (CPU seconds and live/descendant count) at this interval and checks
# the combined captured output bytes after every feed.  The wall-clock
# dimension is exact (monotonic); CPU/live are real /proc measurements
# sampled at a bounded frequency, never simulated counters.
BUDGET_SAMPLE_INTERVAL = 1.0
# The descendant-capture bound used for budget sampling is at least the
# existing supervision bound and always above the configured live-process
# budget, so a tree that exceeds the budget is reported as live-process
# exhaustion rather than an incomplete snapshot.
BUDGET_CAPTURE_MINIMUM = 512
# Budget reason prefix carried in ``LaunchResult.reason``; the suffix is the
# closed exhaustion-reason enum value (``wall_time``, ``cpu_time``,
# ``output_bytes``, ``live_processes``, ``accounting_untrusted``).
BUDGET_REASON_PREFIX = "budget:"
# Clock ticks per second for /proc stat utime/stime (proc(5) fields 14/15).
_CLK_TCK = float(os.sysconf("SC_CLK_TCK")) if hasattr(os, "sysconf") else 100.0

# TOCTOU-free verify-to-interpreter (F2): exact committed wrapper/backend
# bytes are staged in a private mode-0700 directory as non-executable mode-0400
# single-link data. Approved immutable interpreters read those paths; no
# caller-owned staged pathname crosses execve. An *external* trusted executable
# is not staged (its bytes
# are not committed): its fully resolved path (including the containing
# directories of every symlink target) is revalidated immediately at exec.
EXEC_STAGING_PREFIX = "factory-loop-exec-"
STAGED_WRAPPER_NAME = "pi2-secure-exec.py"
STAGED_BACKEND_NAME = "backend"
STAGED_GUARD_EXTENSION_NAME = "pi-factory-guard-extension.mjs"
STAGED_CREDENTIAL_GUARD_NAME = "credential-guard.py"
STAGED_GIT_SHIM_NAME = "pi-cli-shims/git"
STAGED_USAGE_GUARD_NAME = "usage.py"
STAGED_USAGE_FETCH_NAME = "usage_fetch.py"
CREDENTIAL_GUARD = ".factory/tools/credential-guard.py"
PI_GIT_SHIM = ".factory/tools/pi-cli-shims/git"
USAGE_GUARD_SOURCES = (".factory/loop/usage.py", ".factory/loop/usage_fetch.py")
# Staged .factory/tools/modules are readable data, never direct execve targets. They
# run only as arguments to an approved immutable interpreter inside the
# seccomp broker boundary.
STAGED_FILE_MODE = 0o400
STAGED_DIR_MODE = 0o700
DEFAULT_KILL_GRACE = 1.0
REAP_TIMEOUT = 2.0
GROUP_GONE_TIMEOUT = 2.0
# Trusted one-byte protocol emitted only by the staged ptrace broker.  Its
# target closes the write descriptor before untrusted execution, so model
# output can neither spoof the verdict nor hold this lifecycle channel open.
_CONFINEMENT_STATUS_CLEAN = b"C"
_CONFINEMENT_STATUS_ESCAPED = b"E"
_CONFINEMENT_STATUS_FAILED = b"F"
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

# The environment key the trusted pre-spawn authority uses to forward the
# exact committed credential-guard digest to the model-side Pi extension.
# The extension and guard are staged from exact committed bytes; the extension
# re-hashes that staged sibling and compares this transported digest before
# any guard invocation (it has no Git access inside the model Landlock).
PI_FACTORY_GUARD_DIGEST_ENV = "PI_FACTORY_GUARD_DIGEST"
PI_FACTORY_GUARD_PYTHON_ENV = "PI_FACTORY_GUARD_PYTHON"
# The Pi2 adapter publishes only descriptor identity metadata, never
# credential bytes, for the exact-commit extension to consume synchronously
# at the common ``tool_call`` boundary. Names deliberately avoid credential-
# shaped words so the generic child-environment rejection remains useful.
PI_FACTORY_TOOL_FD_ENV = "PI_FACTORY_TOOL_FD"
PI_FACTORY_TOOL_FD_DEV_ENV = "PI_FACTORY_TOOL_FD_DEV"
PI_FACTORY_TOOL_FD_INO_ENV = "PI_FACTORY_TOOL_FD_INO"

# The credential-return pipe: the trusted parent provisions one private pipe
# per openai-codex launch; the model-side extension writes the detached
# credential bytes there at session shutdown (never a pathname).  The parent
# drains it through a dedicated reader thread, caps it at 1 MiB while
# draining, marks oversize, and never logs the bytes.  The write end travels
# in the adapter's transient argv and is forwarded to the extension through
# the sanitized env; it is absent from the model process argv.
CREDENTIAL_RETURN_MAX_BYTES = 1 << 20
CREDENTIAL_RETURN_ENV = "PI_FACTORY_CREDENTIAL_RETURN_FD"
# The credential-return reader starts at spawn and drains for the whole
# (arbitrarily long, bounded) campaign runtime, so there is no spawn-relative
# drain deadline: it exits only on EOF or on the stop event.  It polls through
# a selector with a short timeout so it can observe the stop event promptly;
# a descendant that never closes the write end keeps the reader alive until
# cleanup signals stop, and the bounded join on every consume/cleanup path
# fails closed (no EOF) rather than waiting unboundedly.
# Bounded join window for the reader thread on every consume/cleanup path;
# cleanup never waits unboundedly and never deadlocks on a stuck reader.
CREDENTIAL_RETURN_JOIN_TIMEOUT = 2.0

# The committed model-side Pi extension (Task 11 review): the generic
# factory guard extension loaded by the model backend through ``--extension``
# in the exact child argv.  It enforces the model-side Git command boundary
# (routing direct commit verbs through ``.factory/tools/pi-cli-shims/git`` and
# blocking bypass/unguarded verbs), the credential/path tool-input guard,
# the exact-commit credential-guard digest binding, bounded tool-result
# redaction, and overflow-log process cleanup.  The extension is a committed
# workspace blob verified against the bound commit (F5) and is readable by
# the confined model through the workspace ``.factory/tools/`` read allowlist.
PI_FACTORY_GUARD_EXTENSION = ".factory/tools/pi-factory-guard-extension.mjs"

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
    "CLICOLOR_FORCE", "TMPDIR", "NIX_PATH", "NIX_REMOTE",
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


class BudgetExhaustedError(SupervisionError):
    """The task's cumulative resource budget is already exhausted (Phase 2A).

    Raised before a fresh implementation attempt is spawned when the
    cumulative per-task ledger already consumed the configured budget (or
    the accounting cannot be trusted).  The attempt is never started, so a
    budget-exhausted task can never run unbounded; the campaign classifies
    the bounded exhaustion as a non-success outcome (interrupted with dirty
    work preserved, or a clean task failure) and never as acceptance.
    """


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
    # Exact transient result channel bound into the canonical confinement
    # specification. Empty for roles/attempts with no structured handoff.
    result_write_path: str = ""
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
            "never bypass the fixed provider policy"
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
    if binding.result_write_path:
        if binding.role not in ("tester", "auditor"):
            raise InvocationError(
                "`result_write_path` is allowed only for tester/auditor roles"
            )
        result_path = Path(binding.result_write_path)
        if not result_path.is_absolute():
            raise InvocationError("`result_write_path` must be absolute")
        if any(
            part in ("", ".", "..") or any(ord(char) < 0x20 for char in part)
            for part in result_path.parts
        ):
            raise InvocationError("`result_write_path` is not a canonical safe path")
        try:
            relative_result = result_path.relative_to(
                Path(binding.workspace).absolute()
            )
        except ValueError as exc:
            raise InvocationError(
                "`result_write_path` must remain inside the canonical workspace"
            ) from exc
        campaign_channel = (
            len(relative_result.parts) >= 4
            and relative_result.parts[0] == ".factory-state"
            and relative_result.parts[1] == "campaigns"
            and re.fullmatch(
                r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}",
                relative_result.parts[2],
            ) is not None
        )
        legacy_channel = (
            len(relative_result.parts) >= 2
            and relative_result.parts[0] == ".factory-state"
            and relative_result.parts[1] != "campaigns"
        )
        if not (campaign_channel or legacy_channel):
            raise InvocationError(
                "`result_write_path` must be the exact control-plane-owned "
                "handoff path under a dedicated "
                ".factory-state/campaigns/<id>/ namespace (or the isolated "
                "legacy fixture namespace)"
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
    task_budget: Optional[Mapping[str, object]] = None,
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
        if task_budget is not None:
            # Phase 2A: the trusted cumulative resource budget is explicit
            # role context.  The model may run as many focused
            # inspect/edit/test/diagnose cycles as fit these cumulative
            # limits; the per-command timeout remains a defense-in-depth
            # floor.  Exhaustion is enforced by the trusted supervisor and
            # can never produce acceptance.
            sections.append(b"")
            sections.append(b"## Task resource budget (cumulative across attempts)")
            sections.append(
                b"- Wall-clock: "
                + str(task_budget["wall_time_seconds"]).encode("ascii")
                + b" seconds"
            )
            sections.append(
                b"- Process-tree CPU: "
                + str(task_budget["cpu_time_seconds"]).encode("ascii")
                + b" seconds"
            )
            sections.append(
                b"- Combined captured output: "
                + str(task_budget["output_bytes"]).encode("ascii")
                + b" bytes"
            )
            sections.append(
                b"- Live/descendant processes: "
                + str(task_budget["max_live_processes"]).encode("ascii")
            )
            sections.append(
                b"- Per-command timeout (defense-in-depth): "
                + str(task_budget["per_command_timeout_seconds"]).encode("ascii")
                + b" seconds"
            )
            sections.append(
                b"Your focused runs are diagnostic only; acceptance remains "
                b"an independently bound exact-commit verifier run by the "
                b"trusted orchestrator."
            )
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
    if binding.role in ("tester", "auditor") and binding.result_write_path:
        # The structured handoff is explicit role context, never a general
        # environment variable. Landlock grants write access to this one
        # pre-created file only; prose/stdout remains non-authoritative.
        result_path = str(Path(binding.result_write_path).absolute())
        sections.extend([
            b"",
            b"## Structured phase-result channel (mandatory)",
            b"Write the final machine result to this exact UTF-8 path: "
            + result_path.encode("utf-8"),
            b"Do not choose, rename, or reopen any alternate result path.",
            b"The file must contain exactly one JSON object matching "
            b"factory-phase-result/v1: required keys `schema` and `outcome`; "
            b"`schema` must be `factory-phase-result/v1`; `outcome` must be "
            b"`pass`, `findings`, or `blocked`; optional `findings` and "
            b"`blocked_on` are arrays of non-empty strings; no other keys.",
            b"Writing prose only, printing JSON only, or leaving this exact "
            b"pre-created file empty is an infrastructure failure.",
        ])
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
# Prompt transport
# --------------------------------------------------------------------------

def _sealed_prompt_memfd(prompt: bytes) -> int:
    """Create the production prompt channel as an immutable anonymous memfd.

    The descriptor is populated from the composed bytes, rewound, and sealed
    against write/grow/shrink/further-seal changes before it is carried by the
    launch authority.  No prompt pathname exists at any point.
    """
    if len(prompt) > PROMPT_MAX_BYTES:
        raise InvocationError(f"prompt exceeds the {PROMPT_MAX_BYTES}-byte bound")
    flags = getattr(os, "MFD_CLOEXEC", 0) | getattr(os, "MFD_ALLOW_SEALING", 0)
    required = (
        getattr(fcntl, "F_SEAL_SEAL", 0)
        | getattr(fcntl, "F_SEAL_SHRINK", 0)
        | getattr(fcntl, "F_SEAL_GROW", 0)
        | getattr(fcntl, "F_SEAL_WRITE", 0)
    )
    if not hasattr(os, "memfd_create") or required == 0:
        raise LaunchError("sealed memfd prompt transport is unavailable")
    try:
        descriptor = os.memfd_create("factory-prompt", flags)
        view = memoryview(prompt)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError("short prompt memfd write")
            view = view[written:]
        os.lseek(descriptor, 0, os.SEEK_SET)
        fcntl.fcntl(descriptor, fcntl.F_ADD_SEALS, required)
        actual = fcntl.fcntl(descriptor, fcntl.F_GET_SEALS)
        if actual & required != required:
            raise OSError("mandatory prompt seals were not applied")
        return descriptor
    except OSError as exc:
        try:
            os.close(descriptor)
        except (OSError, UnboundLocalError):
            pass
        raise LaunchError(f"cannot create the sealed prompt memfd: {exc}") from exc


# --------------------------------------------------------------------------
# Exact child argv and environment
# --------------------------------------------------------------------------

def child_environment(
    binding: InvocationBinding,
    *,
    guard_digest: Optional[str] = None,
    staged_path: Optional[Path] = None,
    approved_path_dirs: Sequence[str] = (),
) -> Dict[str, str]:
    """The exact child environment: allowlist plus the invocation fields.

    The environment is *built*, never inherited: only the documented
    :data:`ENV_ALLOWLIST` keys are copied from the parent (when present) and
    the ``FACTORY_LOOP_LAUNCH_*`` fields below are added.  No
    ``FACTORY_LOOP_LOCK_*``/legacy ``FACTORY_LOCK_*`` key, no
    ``GIT_CONFIG*``/``GIT_DIR`` redirector, no ``PI_*`` session/memory
    variable, and no credential/session variable can reach the leaf — this
    is the exact-env allowlist (§9, §20).  The lock strip is applied again
    as defense in depth.

    ``guard_digest`` (when given) is the SHA-256 of the exact committed
    credential guard the pre-spawn authority verified: it is forwarded to
    the model-side Pi extension as ``PI_FACTORY_GUARD_DIGEST`` so the
    extension can bind its staged guard sibling to the exact committed bytes
    without any Git access (Task 11 review).  A 64-hex digest is required
    when the key is carried at all.
    """
    verify_invocation(binding)
    environment: Dict[str, str] = {}
    for key in ENV_ALLOWLIST:
        if key == "PATH":
            continue
        if key in os.environ:
            environment[key] = os.environ[key]
    if staged_path is not None:
        staged = Path(staged_path).absolute()
        staged_shim = staged / STAGED_GIT_SHIM_NAME
        # Validate the staged shim layout symlink-safely: the staging
        # directory, every intermediate directory naming the shim, and the
        # shim file itself must be real (never a symlink) so the extension's
        # relative ``./pi-cli-shims/git`` resolution cannot be redirected to a
        # caller-owned target.
        if (
            not staged.is_dir()
            or not staged_shim.is_file()
            or staged_shim.is_symlink()
            or any(
                part.is_symlink()
                for part in staged_shim.parents
                if part != staged and staged in part.parents
            )
        ):
            raise InvocationError(
                "the sealed staged command PATH is missing its Git shim"
            )
        # Preserve only immutable system/store tool directories from the
        # operator PATH. The launch-owned staging directory is first but is
        # read-only/non-executable under Landlock. Recognized Git commands run
        # its exact-commit shim as data through immutable Bash; obfuscated
        # unqualified Git can execute neither that staged path nor real Git.
        trusted_dirs: List[str] = []
        for candidate in os.environ.get("PATH", "").split(os.pathsep):
            if not candidate or not os.path.isabs(candidate):
                continue
            resolved = os.path.realpath(candidate)
            if resolved in trusted_dirs or not os.path.isdir(resolved):
                continue
            try:
                # A representative executable is not required; the same
                # immutable-chain authority validates the directory itself.
                import stat as _stat
                current = Path(resolved)
                boundary = Path("/nix/store") if resolved.startswith("/nix/store/") else Path(resolved).anchor
                while True:
                    info = current.lstat()
                    sticky = _stat.S_ISDIR(info.st_mode) and bool(info.st_mode & _stat.S_ISVTX)
                    if info.st_uid == os.getuid() and not sticky:
                        raise OSError("caller-owned PATH directory")
                    if info.st_mode & 0o022 and not sticky:
                        raise OSError("writable PATH directory")
                    if current == boundary or current == current.parent:
                        break
                    current = current.parent
            except OSError:
                continue
            trusted_dirs.append(resolved)
        for candidate in approved_path_dirs:
            resolved = os.path.realpath(candidate)
            if (
                resolved != candidate or not resolved.startswith("/nix/store/")
                or not os.path.isdir(resolved) or resolved in trusted_dirs
            ):
                continue
            # These directories come only from exact executable-file rules in
            # the already validated confinement specification. PATH visibility
            # does not grant execution: Landlock and the inode broker still
            # authorize only each named file, never this directory broadly.
            trusted_dirs.append(resolved)
        environment["PATH"] = os.pathsep.join([str(staged), *trusted_dirs])
    else:
        # Non-production spawn fixtures have no staged shim but still need the
        # immutable Nix-shell tool directories.  Preserve only direct store or
        # fixed FHS directories; caller-owned/profile-relative components are
        # never copied.
        safe = []
        for candidate in os.environ.get("PATH", "").split(os.pathsep):
            resolved = os.path.realpath(candidate) if candidate else ""
            if not resolved or resolved in safe or not os.path.isdir(resolved):
                continue
            if resolved.startswith("/nix/store/") or resolved in (
                "/run/current-system/sw/bin", "/usr/bin", "/bin"
            ):
                safe.append(resolved)
        environment["PATH"] = os.pathsep.join(safe or ["/usr/bin", "/bin"])
    if guard_digest is not None:
        if (
            not isinstance(guard_digest, str)
            or not SHA256_RE.fullmatch(guard_digest)
        ):
            raise InvocationError(
                "the forwarded credential-guard digest must be a 64-hex "
                "SHA-256 of the exact committed guard blob"
            )
        environment[PI_FACTORY_GUARD_DIGEST_ENV] = guard_digest
        environment[PI_FACTORY_GUARD_PYTHON_ENV] = require_trusted_interpreter()
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
    prompt_descriptor: int,
    session_dir: Path,
    *,
    secure_wrapper: Optional[Path] = None,
    guard_extension: Optional[Path] = None,
    prompt_digest: Optional[str] = None,
    auth_fd: int = -1,
    credential_return_fd: int = -1,
) -> List[str]:
    """Build the exact one-shot argv for the secure wrapper.

    ``argv = [python, wrapper, --prompt-fd, <fd>, --prompt-sha256, <digest>, --, backend,
    --extension, <guard-extension>, --provider, P, --model, M, --print,
    --no-session, --session-dir, <dir>, --no-skills, --no-themes,
    --no-context-files, --tools, T]``.

    Every flag is structural (the one-shot/no-resume/no-session contract of
    §9/§20) or derived from the binding; no session-resume or conversation
    flag can appear (``FORBIDDEN_BACKEND_FLAGS`` is re-checked here as
    defense in depth), and no prompt content or credential material is ever
    put in argv.  The model-side Pi guard extension (Task 11 review) is
    always loaded through ``--extension`` with the private staged path the
    launch authority supplies, so the model process always runs the exact
    committed git-boundary / credential-guard / redaction extension — never
    a mutable workspace, caller-supplied, or PATH-resolved extension.
    """
    verify_invocation(binding)
    # Absolute trusted interpreter: the wrapper is executed with the
    # control-plane interpreter, whose resolution is bounded to the trusted
    # set and never follows an attacker-controlled path.
    trusted_interpreter = require_trusted_interpreter()
    wrapper = Path(secure_wrapper or secure_wrapper_path(binding.workspace))
    if not wrapper.is_absolute() or not wrapper.is_file():
        raise InvocationError(
            f"the secure wrapper must be an absolute existing file: {wrapper}"
        )
    tools = ",".join(binding.allowed_tools)
    extension = Path(
        guard_extension
        or (Path(binding.workspace).absolute() / PI_FACTORY_GUARD_EXTENSION)
    )
    if not extension.is_absolute() or not extension.is_file():
        raise InvocationError(
            f"the model-side Pi guard extension is missing at {extension}; "
            "the model process must always run the committed staged guard "
            "extension (Task 11)"
        )
    if type(prompt_descriptor) is not int or prompt_descriptor < 0:
        raise InvocationError("the inherited prompt descriptor must be nonnegative")
    if prompt_digest is None or not SHA256_RE.fullmatch(prompt_digest):
        raise InvocationError("the prompt memfd digest must be 64-hex SHA-256")
    argv = [
        trusted_interpreter,
        str(wrapper),
        "--prompt-fd",
        str(prompt_descriptor),
        "--prompt-sha256",
        prompt_digest,
    ]
    backend = Path(binding.backend).absolute()
    staged_backend = secure_wrapper is not None and backend.parent == wrapper.parent
    backend_argv = (
        [trusted_interpreter, str(backend)]
        if staged_backend else [str(backend)]
    )
    argv.extend([
        "--",
        *backend_argv,
        "--extension", str(extension),
        "--provider", binding.provider,
        "--model", binding.model,
        "--print",
        "--no-session",
        "--session-dir", str(session_dir),
        "--no-skills",
        "--no-themes",
        "--no-context-files",
        "--tools", tools,
    ])
    if auth_fd >= 0:
        # B1 security review: the openai-codex credential descriptor number
        # travels only in the backend adapter's transient argv (never an env
        # var the model could read, never a pathname).  The adapter consumes
        # it before exec'ing the model CLI, so the number is absent from the
        # model process argv/environment.
        if type(auth_fd) is not int:
            raise InvocationError("the auth descriptor must be an integer")
        argv.extend(["--auth-fd", str(auth_fd)])
    if credential_return_fd >= 0:
        # The credential-return pipe write end travels in the adapter's
        # transient argv exactly like the auth descriptor; the adapter
        # forwards the number to the extension through the sanitized env and
        # it is absent from the model process argv.
        if type(credential_return_fd) is not int:
            raise InvocationError(
                "the credential return descriptor must be an integer"
            )
        argv.extend(["--credential-return-fd", str(credential_return_fd)])
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

    ``exec_dir`` (staging), the session directory, and the sanitized home —
    the exact path-backed resources the confinement rules bind and cleanup
    removes. The prompt is an anonymous sealed descriptor, never a directory.
    """
    directories: List[Optional[Path]] = [authority._exec_dir]
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
    boundary (``close_fds`` plus only pre-exec Landlock rule anchors, built
    environment, new session) is still asserted by the parent, so the security property does
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
    """Bounded output capture: a digest over a prefix and a bounded tail.

    The digest covers the bounded raw prefix (a deterministic hash of the
    child's exact output); the retained tail is redacted through the
    committed credential guard before it can enter a result, so credentials
    or secrets rendered into child output never appear in results, logs,
    receipts, or repository state (Task 11).  The private-key block state at
    the tail boundary is tracked incrementally so a block that straddles the
    retained window is still masked; whether the retained tail begins
    mid-line is tracked so the incomplete first line is conservatively
    dropped before redaction (a >window opaque token value cut at the
    boundary can never leak a raw fragment); a redaction failure fails the
    tail closed to the fixed ``[REDACTION FAILED]`` marker.
    """

    __slots__ = (
        "_digest", "_digested", "_tail", "_total", "truncated",
        "_tail_start_block", "_tail_partial", "_tail_mid_line",
        "_redactor",
    )

    def __init__(
        self,
        redactor: Optional["output_redaction.Redactor"] = None,
    ) -> None:
        self._digest = hashlib.sha256()
        self._digested = 0
        self._tail = bytearray()
        self._total = 0
        self.truncated = False
        # Private-key block state at the start of the retained tail window
        # (and the trailing partial line at that boundary), updated whenever
        # the tail is trimmed so a block opened before the window is still
        # masked.  ``None`` means no redactor is bound (the stream is not
        # part of a production capture).
        self._tail_start_block = False
        self._tail_partial = ""
        # True when the retained tail's first byte is not at a line start
        # (the eviction boundary cut through a line), so the incomplete
        # first line is conservatively dropped before redaction (Task 11
        # review: a >KEY_BLOCK_WINDOW opaque TOKEN value straddling the
        # boundary must never leak a raw fragment).
        self._tail_mid_line = False
        self._redactor = redactor

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
            excess = len(self._tail) - OUTPUT_TAIL_CAP
            trimmed = bytes(self._tail[:excess])
            del self._tail[:excess]
            self.truncated = True
            # Advance the block state to the new tail boundary so a
            # private-key block that opened before the retained window is
            # still seeded when the tail is redacted; the non-empty trailing
            # partial line marks a boundary that cut through a line (the
            # retained tail begins mid-line).
            in_block, partial = output_redaction.scan_key_block_state(
                self._tail_start_block, self._tail_partial, trimmed
            )
            self._tail_start_block, self._tail_partial = in_block, partial
            self._tail_mid_line = bool(partial)

    def result(self) -> "StreamResult":
        tail = self._tail.decode("utf-8", "replace")
        if self._redactor is not None and (tail or self._tail_partial):
            try:
                tail = self._redactor.redact_tail(
                    tail,
                    self._tail_start_block,
                    self._tail_partial,
                    bound_bytes=OUTPUT_TAIL_CAP,
                    mid_line=self._tail_mid_line,
                )
            except output_redaction.OutputRedactionError:
                # Fail closed: a redaction failure never exposes the raw
                # tail; it carries the fixed marker instead.
                tail = output_redaction.REDACTION_FAILED
        return StreamResult(
            bytes=self._total,
            digest=self._digest.hexdigest(),
            tail=tail,
            truncated=self.truncated,
        )


@dataclass(frozen=True)
class BudgetUsage:
    """One attempt's measured resource usage (Phase 2A).

    ``wall_time_seconds`` is the attempt's elapsed wall time, ``cpu_time_seconds``
    the cumulative user+system CPU of the live process tree sampled from
    ``/proc`` (real kernel accounting, never a simulated counter),
    ``output_bytes`` the combined captured stdout+stderr byte count, and
    ``max_live_processes`` the peak live/descendant process count observed.
    The supervisor records these monotonically into the per-task cumulative
    ledger after every attempt.
    """

    wall_time_seconds: float = 0.0
    cpu_time_seconds: float = 0.0
    output_bytes: int = 0
    max_live_processes: int = 0


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


def _dispose_launch_authority(authority: object) -> None:
    """Close resources still owned by a genuine unconsumed authority."""
    if not isinstance(authority, LaunchAuthority) or authority._mint is not _MINT_SECRET:
        return
    for descriptor in authority._confinement_rule_fds:
        try:
            os.close(descriptor)
        except OSError:
            pass
    authority._confinement_rule_fds = ()
    if authority._prompt_fd >= 0:
        try:
            os.close(authority._prompt_fd)
        except OSError:
            pass
        authority._prompt_fd = -1
    if authority._auth_fd >= 0:
        try:
            os.close(authority._auth_fd)
        except OSError:
            pass
        authority._auth_fd = -1
    _remove_private_directories(_authority_private_directories(authority))


def _outer_launch_cleanup(method):
    """Wrap the entire authority-consumption path in cleanup immediately.

    This outer ``try/finally`` exists before :meth:`run` can transfer a single
    Landlock or prompt descriptor.  It therefore covers prompt composition,
    digest checks, redactor preflight, signal/broker setup, and spawn — not
    only the post-spawn body.  Cleanup is idempotent, so the narrower lifecycle
    finally inside ``run`` remains defense in depth.
    """
    @functools.wraps(method)
    def guarded(self, *args, **kwargs):
        authority = args[0] if args else kwargs.get("authority")
        try:
            return method(self, *args, **kwargs)
        finally:
            self._restore_signal_handlers()
            self._cleanup()
            _dispose_launch_authority(authority)
    return guarded


class LaunchSupervision:
    """Descendant-scoped supervision of one fresh model process (F6/F7).

    Owns the outer F6 identity snapshot, the TERM/INT/HUP→KILL lifecycle for
    the exact dedicated broker PID, crash-before-snapshot/PID-reuse guards,
    and the structured bounded result.  The fresh confinement broker owns the
    command-only subreaper namespace and all descendant cleanup (F7).
    """

    def __init__(
        self,
        binding: InvocationBinding,
        *,
        root: Optional[Path] = None,
        session_dir: Optional[Path] = None,
        kill_grace: float = DEFAULT_KILL_GRACE,
        budget: Optional[Mapping[str, object]] = None,
        ledger: Optional[task_budget_module.BudgetLedger] = None,
    ) -> None:
        verify_invocation(binding)
        self.binding = binding
        self.root = Path(root or binding.workspace).absolute()
        self.kill_grace = kill_grace
        # Phase 2A task-resource budget: ``budget`` is the validated
        # ``factory-task-budget/v1`` document and ``ledger`` the cumulative
        # per-task ledger bound to (campaign, selected task).  Both are
        # supplied by the trusted campaign for developer attempts; ``None``
        # disables budget enforcement (planner/tester/auditor and the
        # deterministic driver seam).  The ledger is runtime state under the
        # ignored ``.factory-state/`` namespace, never model-writable.
        self._budget: Optional[Mapping[str, object]] = budget
        self._ledger: Optional[task_budget_module.BudgetLedger] = ledger
        self._remaining: Optional[Dict[str, float]] = None
        if budget is not None and ledger is not None:
            self._remaining = task_budget_module.remaining_limits(ledger, budget)
        # Measured usage of the current attempt, updated by the monitor loop
        # and recorded into the ledger after the attempt (monotonic).
        self._budget_usage = BudgetUsage()
        self._budget_exhausted_reason: Optional[str] = None
        self._started: float = 0.0
        # The last budget-sample descendant closure (identity-pinned), retained
        # so post-run cleanup can be verified against the exact measured tree.
        self._budget_captured: frozenset = frozenset()
        self.prompt_fd: Optional[int] = None
        self.session_dir = Path(session_dir) if session_dir else None
        self._child: Optional[subprocess.Popen[bytes]] = None
        self._captured: frozenset = frozenset()
        self._capture_error: Optional[str] = None
        # Descendant ownership is intentionally absent here.  The fresh
        # confinement/exec broker owns the command-only subreaper namespace;
        # this outer coordinator waits and signals only that exact broker PID.
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
        self._guard_extension: Optional[Path] = None
        self._prompt_digest: Optional[str] = None
        self._auth_fd: int = -1
        self._staged_digests: Dict[str, str] = {}
        self._external_paths: Tuple[str, ...] = ()
        self._external_runtime_bindings: Tuple[_ExternalRuntimeBinding, ...] = ()
        self._exec_dir: Optional[Path] = None
        # Task 8 confinement binding carried by the verified authority (the
        # exact specification, the real proof, the sanitized home, and the
        # staged confine launcher); ``None`` when the token carries none.
        self._confinement_spec: Optional[Dict[str, object]] = None
        self._confinement_proof: Optional[object] = None
        self._sanitized_home: Optional[Path] = None
        self._confined_launcher: Optional[Path] = None
        self._confinement_rule_fds: Tuple[int, ...] = ()
        self._confinement_status_fd: Optional[int] = None
        self._usage_guard_digests: Optional[Tuple[str, str]] = None
        # Task 11: the verified credential-guard redactor, built by :meth:`run`
        # before the child is spawned so no launch can capture child output
        # without a verified redaction authority (fail closed).  ``_guard_digest``
        # is the SHA-256 of the exact committed guard bytes, forwarded to the
        # model-side Pi extension through the sanitized launch env
        # (``PI_FACTORY_GUARD_DIGEST``) so the extension binds its staged guard
        # sibling to the same exact-commit authority without any Git access.
        self._redactor: Optional["output_redaction.Redactor"] = None
        self._guard_digest: Optional[str] = None
        # Credential-return pipe state (openai-codex only): the read end is
        # held by the parent and drained by a dedicated reader thread; the
        # write end travels to the model through the adapter argv/pass_fds and
        # is closed by the parent immediately after spawn.  The thread caps
        # the returned bytes at 1 MiB, marks oversize, and never logs them.
        self._credential_return_fd: int = -1
        self._credential_return_write_fd: int = -1
        self._credential_return_thread: Optional[threading.Thread] = None
        self._credential_return_stop: Optional[threading.Event] = None
        self._credential_return_data: Optional[bytearray] = None
        self._credential_return_oversize: bool = False
        self._credential_return_eof: bool = False

    # -- dedicated broker lifecycle (F7) -------------------------------------
    # The staged confine launcher installs PR_SET_CHILD_SUBREAPER in its own
    # fresh process before forking the untrusted target.  Installing it in this
    # long-lived coordinator would mix unrelated child lineages and is
    # deliberately forbidden by construction.

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
        (crash-before-snapshot window) the scope is empty: the dedicated
        broker cannot fork further after death, and PTRACE_O_EXITKILL plus its
        private lifecycle channel make descendant cleanup fail closed.
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

    # -- Phase 2A task-resource budget enforcement -----------------------------

    def _budget_capture_maximum(self) -> int:
        """The descendant-capture bound for budget sampling.

        At least the existing supervision bound and always above the
        configured live-process budget, so a tree that exceeds the budget is
        reported as live-process exhaustion rather than an incomplete
        snapshot.
        """
        if self._budget is None:
            return BUDGET_CAPTURE_MINIMUM
        return max(
            BUDGET_CAPTURE_MINIMUM,
            int(self._budget["max_live_processes"]) + 1,
        )

    def _measure_tree(self) -> Tuple[float, int]:
        """Measure cumulative CPU seconds and live process count of the tree.

        The measurement re-snapshots the full descendant closure of the
        launched leader (the existing identity-safe capture machinery, never
        a baseline subtraction) and sums the real user+system CPU ticks of
        every still-live, identity-matching member from ``/proc/<pid>/stat``.
        A tree that exceeds the capture bound is reported as the bound (the
        live-process budget is then exhausted); an unreadable ``/proc`` fails
        closed with :class:`BudgetExhaustedError` (``accounting_untrusted``)
        rather than silently under-counting.
        """
        child = self._child
        if child is None or self._leader_exited(child):
            return 0.0, 0
        pid = child.pid
        try:
            captured = capture_descendants(pid, maximum=self._budget_capture_maximum())
        except RootLockUnsafeError:
            # The tree exceeds the capture bound: report the bound as the
            # live count so the live-process budget is exhausted (never an
            # incomplete snapshot mistaken for a small tree).
            return 0.0, self._budget_capture_maximum()
        self._budget_captured = captured
        live = live_scope(captured)
        ticks = 0
        for member in live:
            fields = _proc_stat_fields(member)
            if fields is None or len(fields) < 13:
                continue
            try:
                ticks += int(fields[11]) + int(fields[12])
            except ValueError:
                continue
        return ticks / _CLK_TCK, len(live)

    def _budget_preflight(self) -> None:
        """Fail closed before spawn when the cumulative budget is exhausted.

        A task whose cumulative ledger already consumed the configured budget
        (or whose accounting cannot be trusted) is never started: the attempt
        is refused with :class:`BudgetExhaustedError` so a budget-exhausted
        task can never run unbounded and exhaustion can never produce
        acceptance.
        """
        if self._budget is None or self._ledger is None:
            return
        reason = task_budget_module.exhausted_reason(self._ledger, self._budget)
        if reason is not None:
            raise BudgetExhaustedError(
                f"task resource budget exhausted: {reason}"
            )

    def _record_budget_usage(self, elapsed: float) -> None:
        """Record one attempt's measured usage into the cumulative ledger.

        The usage is accumulated monotonically (never replaced, never
        decreased) and the ledger is published with the no-replace
        byte-idempotent authority, so a tampered or foreign ledger fails
        closed.  The first exhausted dimension is recorded with its closed
        exhaustion reason; the attempt's own budget-termination reason wins
        over the derived precedence so the actual cause is never masked.
        """
        if self._budget is None or self._ledger is None:
            return
        usage = self._budget_usage
        self._ledger.record_attempt(
            wall_time_seconds=elapsed,
            cpu_time_seconds=usage.cpu_time_seconds,
            output_bytes=usage.output_bytes,
            max_live_processes=usage.max_live_processes,
        )
        reason = self._budget_exhausted_reason
        if reason is None:
            reason = task_budget_module.exhausted_reason(self._ledger, self._budget)
        if reason is not None:
            self._ledger.mark_exhausted(reason)
        task_budget_module.save_ledger(self.root, self._ledger)

    # -- spawning --------------------------------------------------------------

    def _provision_credential_return_pipe(self) -> None:
        """Create the private credential-return pipe for an openai-codex launch.

        The read end is held by the parent and drained by a dedicated reader
        thread; the write end travels to the model through the adapter argv,
        ``pass_fds``, and the confined launcher's ``--target-only-fds``.  The
        pipe is created with ``O_CLOEXEC`` so no unrelated exec can inherit it.
        """
        if self.binding.provider.lower() != "openai-codex":
            return
        try:
            read_fd, write_fd = os.pipe2(getattr(os, "O_CLOEXEC", 0))
        except (AttributeError, OSError) as exc:
            raise SupervisionError(
                f"cannot create the credential-return pipe: {exc}"
            ) from exc
        self._credential_return_fd = read_fd
        self._credential_return_write_fd = write_fd
        self._credential_return_stop = threading.Event()

    def _zero_credential_return(self) -> None:
        """Zero and drop any buffered credential-return bytes.

        Called on consume, on every consume error, and on noncompleted
        cleanup so no credential byte survives in parent memory after the
        channel is done with.
        """
        data = self._credential_return_data
        self._credential_return_data = None
        if data is not None:
            data.clear()

    def _drain_credential_return(self) -> None:
        """Drain the credential-return pipe read end until EOF (never logs bytes).

        Runs on a dedicated daemon thread.  The read end is nonblocking and
        polled through a selector with a short timeout, so the thread can
        observe the stop event and exit promptly.  The reader starts at spawn
        and drains for the whole (arbitrarily long, bounded) campaign runtime,
        so there is no spawn-relative drain deadline: it exits only on EOF or
        on the stop event.  Bytes are capped at 1 MiB while draining; once the
        cap is exceeded the thread keeps draining (so the child never blocks
        on a full pipe) but discards the content and marks the result oversize.
        The read end is closed at EOF or on stop.  No credential byte is ever
        logged or echoed.
        """
        fd = self._credential_return_fd
        if fd < 0:
            return
        data = bytearray()
        oversize = False
        eof = False
        stop = self._credential_return_stop
        try:
            os.set_blocking(fd, False)
        except OSError:
            pass
        selector = selectors.DefaultSelector()
        registered = False
        try:
            selector.register(fd, selectors.EVENT_READ)
            registered = True
        except (KeyError, OSError):
            # A selector that cannot register the read end cannot drain; fail
            # closed (no EOF) rather than busy-looping on an empty selector.
            pass
        try:
            if registered:
                while stop is None or not stop.is_set():
                    try:
                        events = selector.select(timeout=0.5)
                    except OSError:
                        break
                    if not events:
                        continue
                    try:
                        chunk = os.read(fd, 65536)
                    except (BlockingIOError, InterruptedError):
                        continue
                    except OSError:
                        break
                    if not chunk:
                        eof = True
                        break
                    if oversize:
                        continue
                    if len(data) + len(chunk) > CREDENTIAL_RETURN_MAX_BYTES:
                        oversize = True
                        data = bytearray()
                    else:
                        data.extend(chunk)
        finally:
            try:
                selector.close()
            except Exception:
                pass
            try:
                os.close(fd)
            except OSError:
                pass
            self._credential_return_fd = -1
        self._credential_return_data = data
        self._credential_return_oversize = oversize
        self._credential_return_eof = eof

    def spawn(self) -> subprocess.Popen[bytes]:
        """Spawn the fresh one-shot model process behind the wrapper.

        The child starts a new process session/group with ``close_fds=True``
        and inherits only the descriptor-anchored Landlock rule set through
        ``pass_fds``.  The confine launcher consumes and closes those anchors
        before exec, so the model leaf inherits no control-plane descriptor or
        lock metadata.  ``cwd`` is the canonical workspace.

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
        if self.prompt_fd is None:
            raise SupervisionError("no sealed prompt memfd was inherited")
        if self.session_dir is None:
            raise SupervisionError("no session directory was prepared")
        if self._prompt_digest is None or not SHA256_RE.fullmatch(self._prompt_digest):
            raise SupervisionError(
                "no exact launch-authority prompt digest is bound for the "
                "wrapper's sealed snapshot"
            )
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
                    f"staged script/module {staged_path} changed since "
                    "verification; refusing interpreter launch (F2)"
                )
        try:
            _revalidate_external_runtimes(self._external_runtime_bindings)
        except InvocationError as exc:
            raise SupervisionError(
                "an external Pi/runtime path changed in digest, device, inode, "
                f"or immutable identity immediately before exec: {exc} (F2)"
            ) from exc
        approved_path_dirs = sorted({
            str(Path(str(rule["path"])).parent)
            for rule in (self._confinement_spec or {}).get("rules", [])
            if isinstance(rule, dict)
            and "execute" in rule.get("access", [])
            and str(rule.get("path", "")).startswith("/nix/store/")
        })
        # Provision the private credential-return pipe before any argv is built
        # so the write end can travel through the adapter argv, ``pass_fds``,
        # and the confined launcher's ``--target-only-fds``.  Only an
        # openai-codex launch provisions a pipe; every other provider leaves
        # the state at its closed defaults.
        self._provision_credential_return_pipe()
        env = child_environment(
            self.binding,
            guard_digest=self._guard_digest,
            staged_path=self._exec_dir,
            approved_path_dirs=approved_path_dirs,
        )
        argv = child_argv(
            self.binding, self.prompt_fd, self.session_dir,
            secure_wrapper=self._staged_wrapper,
            guard_extension=self._guard_extension,
            prompt_digest=self._prompt_digest,
            auth_fd=self._auth_fd,
            credential_return_fd=self._credential_return_write_fd,
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
            if (
                self._confined_launcher is None
                or self._confinement_proof is None
                or not self._confinement_rule_fds
            ):
                raise SupervisionError(
                    "the verified authority carries a confinement "
                    "specification without the staged confine launcher or "
                    "its real confinement proof; no model can be started "
                    "(fail closed)"
                )
            try:
                real_confinement_authority.validate_rule_anchors(
                    self._confinement_spec, self._confinement_rule_fds
                )
                real_confinement_authority.validate_proof(
                    self._confinement_proof,
                    self.binding,
                    confinement_spec=self._confinement_spec,
                    _strict_channels=False,
                    _executing_guard_digests=self._usage_guard_digests,
                )
            except real_confinement_authority.ConfinementError as exc:
                raise SupervisionError(
                    "the confinement proof does not bind the exact "
                    "specification being applied at exec time; no model can "
                    f"be started (fail closed): {exc}"
                ) from exc
            spec_path = self._publish_confinement_spec()
        if not hasattr(signal, "pthread_sigmask"):
            raise SupervisionError(
                "pthread_sigmask is unavailable; TERM/INT/HUP cannot be "
                "blocked across the spawn window, so an unrecorded child "
                "could be left running (fail closed)"
            )
        supervision_write: Optional[int] = None
        if self._confinement_spec is not None:
            pipe_flags = getattr(os, "O_CLOEXEC", 0)
            try:
                status_read, supervision_write = os.pipe2(pipe_flags)
            except (AttributeError, OSError) as exc:
                raise SupervisionError(
                    f"cannot create the confined lifecycle pipe: {exc}; "
                    "descendant escape reporting must fail closed"
                ) from exc
            self._confinement_status_fd = status_read
            target_only = [
                fd for fd in (self._auth_fd, self._credential_return_write_fd)
                if fd >= 0
            ]
            argv = [
                sys.executable,
                str(self._confined_launcher),
                "--spec-file",
                str(spec_path),
                "--rule-fds",
                ",".join(str(fd) for fd in self._confinement_rule_fds),
                *(
                    ["--target-only-fds", ",".join(str(fd) for fd in target_only)]
                    if target_only else []
                ),
                "--supervision-fd",
                str(supervision_write),
                "--",
                *argv,
            ]
        oldmask = signal.pthread_sigmask(signal.SIG_BLOCK, TERMINATION_SIGNALS)
        try:
            try:
                extra_fds: Tuple[int, ...] = (
                    (supervision_write,) if supervision_write is not None else ()
                )
                auth_pass: Tuple[int, ...] = (
                    (self._auth_fd,) if self._auth_fd >= 0 else ()
                )
                credential_pass: Tuple[int, ...] = (
                    (self._credential_return_write_fd,)
                    if self._credential_return_write_fd >= 0 else ()
                )
                process = subprocess.Popen(
                    argv,
                    cwd=str(self.binding.workspace),
                    env=env,
                    start_new_session=True,
                    close_fds=True,
                    pass_fds=(
                        *self._confinement_rule_fds, self.prompt_fd,
                        *auth_pass, *credential_pass, *extra_fds,
                    ),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    preexec_fn=_child_reset_spawn_mask,
                )
            except (OSError, ValueError, subprocess.SubprocessError) as exc:
                if self._confinement_status_fd is not None:
                    try:
                        os.close(self._confinement_status_fd)
                    except OSError:
                        pass
                    self._confinement_status_fd = None
                if self._credential_return_fd >= 0:
                    try:
                        os.close(self._credential_return_fd)
                    except OSError:
                        pass
                    self._credential_return_fd = -1
                if self._credential_return_write_fd >= 0:
                    try:
                        os.close(self._credential_return_write_fd)
                    except OSError:
                        pass
                    self._credential_return_write_fd = -1
                raise LaunchError(f"cannot spawn the model process: {exc}") from exc
            finally:
                if supervision_write is not None:
                    try:
                        os.close(supervision_write)
                    except OSError:
                        pass
            self._child = process
            # The child now owns its inherited anchor copies.  Close every
            # parent descriptor immediately; the launcher closes the child
            # copies after Landlock consumes them and before model exec.
            for descriptor in self._confinement_rule_fds:
                try:
                    os.close(descriptor)
                except OSError:
                    pass
            self._confinement_rule_fds = ()
            try:
                os.close(self.prompt_fd)
            except OSError:
                pass
            self.prompt_fd = None
            # Authority has transferred to the child. The parent must not keep
            # a second readable copy for the entire model lifetime: close and
            # reset it immediately after successful Popen, before monitoring.
            if self._auth_fd >= 0:
                try:
                    os.close(self._auth_fd)
                except OSError:
                    pass
                self._auth_fd = -1
            # The parent must not keep a second writable copy of the
            # credential-return pipe: close it immediately so the read end
            # reaches EOF exactly when the model closes its copy, then start
            # the dedicated reader thread that drains the returned credential.
            if self._credential_return_write_fd >= 0:
                try:
                    os.close(self._credential_return_write_fd)
                except OSError:
                    pass
                self._credential_return_write_fd = -1
            if self._credential_return_fd >= 0:
                self._credential_return_thread = threading.Thread(
                    target=self._drain_credential_return, daemon=True
                )
                self._credential_return_thread.start()
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
        stdout = _BoundedStream(redactor=self._redactor)
        stderr = _BoundedStream(redactor=self._redactor)
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
        # ``select.select`` is capped by FD_SETSIZE even when the process
        # soft limit is much higher.  Exact per-inode confinement can retain
        # enough descriptor anchors that the two output pipes are numbered
        # above that cap.  Use the platform's scalable selector (epoll on the
        # supported Linux hosts), so monitor correctness is independent of
        # how many exact rule anchors precede the pipes.
        selector = selectors.DefaultSelector()
        for descriptor in open_fds:
            selector.register(descriptor, selectors.EVENT_READ)
        last_activity = time.monotonic()
        reason: Optional[str] = None
        # Phase 2A budget state: the usage snapshot is refreshed on every
        # sample and on every output feed; the exhausted reason is recorded
        # when a budget dimension overflows so the ledger records the actual
        # cause (never a derived guess).
        self._budget_usage = BudgetUsage()
        self._budget_exhausted_reason = None
        last_sample = time.monotonic()
        try:
            while True:
                if self._pending_signal is not None:
                    reason = f"signal:{signal.Signals(self._pending_signal).name}"
                    break
                if self._leader_exited(child) and not open_fds:
                    break
                now = time.monotonic()
                if now >= deadline:
                    # Phase 2A: when the cumulative wall budget is the
                    # binding constraint on the deadline (smaller than the
                    # per-attempt runtime limit), the exhaustion is a
                    # budget:wall_time termination, never a plain runtime
                    # limit; the ledger records the actual cause.
                    if (
                        self._budget is not None
                        and self._ledger is not None
                        and float(self._remaining["wall_time_seconds"])
                        < self.binding.runtime_limit
                    ):
                        self._budget_exhausted_reason = "wall_time"
                        reason = BUDGET_REASON_PREFIX + "wall_time"
                    else:
                        reason = "runtime"
                    break
                if inactivity_limit and now - last_activity > inactivity_limit:
                    reason = "inactivity"
                    break
                if self._budget is not None and self._ledger is not None:
                    if now - last_sample >= BUDGET_SAMPLE_INTERVAL:
                        last_sample = now
                        try:
                            cpu, live = self._measure_tree()
                        except BudgetExhaustedError as exc:
                            # Accounting cannot be trusted (unreadable
                            # /proc): fail closed instead of silently
                            # under-counting.
                            self._budget_exhausted_reason = "accounting_untrusted"
                            reason = BUDGET_REASON_PREFIX + "accounting_untrusted"
                            break
                        self._budget_usage = BudgetUsage(
                            wall_time_seconds=now - self._started,
                            cpu_time_seconds=cpu,
                            output_bytes=stdout._total + stderr._total,
                            max_live_processes=max(
                                self._budget_usage.max_live_processes, live
                            ),
                        )
                        if cpu >= float(self._remaining["cpu_time_seconds"]):
                            self._budget_exhausted_reason = "cpu_time"
                            reason = BUDGET_REASON_PREFIX + "cpu_time"
                            break
                        if live >= int(self._remaining["max_live_processes"]):
                            self._budget_exhausted_reason = "live_processes"
                            reason = BUDGET_REASON_PREFIX + "live_processes"
                            break
                if not open_fds:
                    # The leader is still alive with all pipes closed (a
                    # descendant held them and released them): keep bounded
                    # monitoring without busy-spinning until the deadline.
                    time.sleep(0.05)
                    continue
                try:
                    events = selector.select(0.2)
                except InterruptedError:
                    # A caught TERM/INT/HUP woke the selector: re-check the
                    # pending-signal flag and the deadline immediately.
                    continue
                except (OSError, ValueError) as exc:
                    raise SupervisionError(
                        "model output selector failed before process exit"
                    ) from exc
                for key, _mask in events:
                    descriptor = key.fd
                    try:
                        chunk = os.read(descriptor, 65536)
                    except (BlockingIOError, InterruptedError):
                        continue
                    except OSError:
                        chunk = b""
                    if not chunk:
                        open_fds.discard(descriptor)
                        try:
                            selector.unregister(descriptor)
                        except (KeyError, OSError, ValueError):
                            pass
                        try:
                            files[descriptor].close()
                        except OSError:
                            pass
                    else:
                        last_activity = time.monotonic()
                        streams[descriptor].feed(chunk)
                        if self._budget is not None and self._ledger is not None:
                            total = stdout._total + stderr._total
                            self._budget_usage = BudgetUsage(
                                wall_time_seconds=time.monotonic() - self._started,
                                cpu_time_seconds=self._budget_usage.cpu_time_seconds,
                                output_bytes=total,
                                max_live_processes=self._budget_usage.max_live_processes,
                            )
                            if total >= int(self._remaining["output_bytes"]):
                                self._budget_exhausted_reason = "output_bytes"
                                reason = BUDGET_REASON_PREFIX + "output_bytes"
                                break
                if reason is not None:
                    break
                if self._leader_exited(child) and not open_fds:
                    break
        finally:
            selector.close()
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

    # -- broker-proven escape cleanup (F7) -----------------------------------
    # The outer process has no child-adoption ownership surface.  The helper
    # below remains the identity-safe pidfd primitive used by focused failure
    # tests and by any already-proven identity cleanup; it never discovers or
    # classifies a PID.  Discovery, termination, and reaping happen inside the
    # dedicated ptrace/subreaper broker.

    @staticmethod
    def _kill_pinned_identity(pid: int, starttime: int) -> bool:
        """SIGKILL one exact process through a revalidated pidfd.

        A ``/proc`` check followed by ``os.kill(pid, ...)`` has a PID-reuse
        window.  ``pidfd_open`` first pins the kernel process object; the
        starttime is then re-read and must still match before the signal is
        sent through that pidfd.  If Python/kernel support for identity-safe
        signaling is unavailable while the identity is live, cleanup fails
        closed rather than falling back to a numeric signal.
        """
        if not _is_live_with_identity(pid, starttime):
            return False
        pidfd_open = getattr(os, "pidfd_open", None)
        pidfd_send_signal = getattr(signal, "pidfd_send_signal", None)
        if not callable(pidfd_open) or not callable(pidfd_send_signal):
            raise SupervisionError(
                "identity-safe pidfd signaling is unavailable for live "
                f"escaped descendant {pid}; refusing numeric os.kill"
            )
        try:
            descriptor = pidfd_open(pid, 0)
        except ProcessLookupError:
            return False
        except OSError as exc:
            raise SupervisionError(
                f"cannot pidfd-pin escaped descendant {pid}: {exc}"
            ) from exc
        try:
            # The PID could have been recycled before pidfd_open.  The pidfd
            # now pins whichever object was opened; authorize signaling only
            # after /proc proves it is still the snapshotted identity.
            if not _is_live_with_identity(pid, starttime):
                return False
            try:
                pidfd_send_signal(descriptor, signal.SIGKILL, None, 0)
            except ProcessLookupError:
                return False
            except OSError as exc:
                raise SupervisionError(
                    f"cannot pidfd-SIGKILL escaped descendant {pid}: {exc}"
                ) from exc
            return True
        finally:
            try:
                os.close(descriptor)
            except OSError:
                pass

    def _consume_confinement_status(self, *, allow_missing: bool) -> None:
        """Consume the broker-only descendant lifecycle report exactly once.

        The broker process has been reaped before this method runs, and the
        target closed its copy before untrusted execution, so the read cannot
        be held open by an escaped descendant.  A clean byte accepts the
        tracer lifecycle; an escape byte means the broker already bounded-
        killed and reaped a descendant. It fails natural completion closed,
        while a supervisor-requested termination accepts that cleanup. A
        malformed/failure report, or a missing report on natural completion,
        is a supervision boundary failure.
        """
        descriptor = self._confinement_status_fd
        self._confinement_status_fd = None
        if descriptor is None:
            if self._confinement_spec is not None and not allow_missing:
                raise SupervisionError(
                    "the confined executable broker produced no lifecycle channel"
                )
            return
        try:
            try:
                report = os.read(descriptor, 2)
            except OSError as exc:
                raise SupervisionError(
                    f"cannot read the confined lifecycle report: {exc}"
                ) from exc
        finally:
            try:
                os.close(descriptor)
            except OSError:
                pass
        if report == _CONFINEMENT_STATUS_CLEAN:
            return
        if report == _CONFINEMENT_STATUS_ESCAPED:
            if allow_missing:
                # The outer supervisor deliberately terminated the attempt;
                # descendants still attached when the target receives that
                # sequence are part of bounded cleanup, not a natural-exit
                # escape.  The broker has already killed and reaped them.
                return
            raise EscapedDescendantError(
                "the atomic executable broker bounded-terminated and reaped "
                "an escaped model descendant; recovery is blocked"
            )
        if report == b"" and allow_missing:
            # The outer supervisor SIGKILLed the broker as part of its own
            # bounded timeout sequence.  PTRACE_O_EXITKILL is the kernel
            # fallback for every attached target/descendant in this path.
            return
        if report == _CONFINEMENT_STATUS_FAILED:
            raise SupervisionError(
                "the confined executable broker reported a lifecycle failure"
            )
        raise SupervisionError(
            f"the confined executable broker returned malformed lifecycle "
            f"status {report!r}"
        )

    def _consume_credential_return(self) -> Optional[bytes]:
        """Join the reader thread and return exactly one JSON credential document.

        Runs only after the child process tree is fully terminated and the
        confinement lifecycle report is consumed, so the write end is closed
        and the reader thread has reached EOF.  The join is bounded; a reader
        that did not finish, did not reach EOF (drain timeout or stop), or
        returned oversize/malformed content fails closed and the buffered
        bytes are zeroed.  Returns ``None`` when no credential was returned.
        The bytes are never logged.
        """
        thread = self._credential_return_thread
        if thread is not None:
            thread.join(timeout=CREDENTIAL_RETURN_JOIN_TIMEOUT)
            if thread.is_alive():
                self._zero_credential_return()
                raise SupervisionError(
                    "the credential-return reader did not finish within the "
                    "bounded join window"
                )
            self._credential_return_thread = None
        if self._credential_return_oversize:
            self._zero_credential_return()
            raise SupervisionError(
                "the credential-return pipe exceeded the 1 MiB bound"
            )
        if not self._credential_return_eof:
            self._zero_credential_return()
            raise SupervisionError(
                "the credential-return reader was stopped before reaching EOF"
            )
        data = self._credential_return_data
        self._credential_return_data = None
        if data is None or not data:
            return None
        try:
            document, end = json.JSONDecoder().raw_decode(data.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            data.clear()
            raise SupervisionError(
                "the returned credential is not exactly one JSON document"
            ) from exc
        if end != len(data) or not isinstance(document, dict):
            data.clear()
            raise SupervisionError(
                "the returned credential is not exactly one JSON document"
            )
        returned = bytes(data)
        data.clear()
        return returned

    def _publish_credential_return_to_private_home(self, data: bytes) -> None:
        """Atomically publish the returned credential to the launch-private home.

        Writes the returned bytes to ``<sanitized-home>/.pi/agent2/auth.json``
        as a mode-0600 single-link file through a nofollow-safe atomic
        replace, so the existing operator persistence authority
        (:func:`_persist_private_pi2_auth`) can read and validate it.  The
        private home and every intermediate directory must be real
        directories (never symlinks).

        Every directory component is pinned once: opened with
        ``O_DIRECTORY|O_NOFOLLOW`` and fstat-bound (dev/ino/mode/uid) against
        the lstat expectation taken just before the open, so a path swap in
        the window is detected and fails closed.  From then on only relative
        names anchored to the pinned agent-directory descriptor are used: a
        unique ``O_CREAT|O_EXCL`` temp name, ``os.replace`` with
        ``src_dir_fd``/``dst_dir_fd``, and ``os.unlink`` cleanup.  A swap
        after pinning cannot redirect the bytes anywhere else: the rename
        operates on the anchored directory, so the credential lands in the
        verified original home or the publish fails closed — never in an
        attacker-chosen directory.
        """
        if self._sanitized_home is None:
            raise SupervisionError(
                "openai-codex launch has no private home for credential refresh"
            )
        home = Path(self._sanitized_home)
        agent_dir = home / ".pi" / "agent2"
        try:
            agent_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise SupervisionError(
                f"cannot create the private Pi2 agent directory: {exc}"
            ) from exc
        components = ((home, ""), (home / ".pi", ".pi"), (agent_dir, "agent2"))
        expected = []
        for path, _name in components:
            try:
                info = os.lstat(path)
            except OSError as exc:
                raise SupervisionError(
                    f"private Pi2 path is unavailable: {exc}"
                ) from exc
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
                raise SupervisionError(
                    "private Pi2 path is a symlink or not a directory"
                )
            expected.append(info)
        nofollow = getattr(os, "O_NOFOLLOW", 0)
        directory_fd = -1
        pi_fd = -1
        agent_fd = -1
        temp_fd = -1
        temp_name: Optional[str] = None
        try:
            open_flags = os.O_RDONLY | os.O_DIRECTORY | nofollow | os.O_CLOEXEC
            directory_fd = os.open(str(home), open_flags)
            pi_fd = os.open(".pi", open_flags, dir_fd=directory_fd)
            agent_fd = os.open("agent2", open_flags, dir_fd=pi_fd)
            for fd, info in ((directory_fd, expected[0]), (pi_fd, expected[1]),
                             (agent_fd, expected[2])):
                pinned = os.fstat(fd)
                if (pinned.st_dev != info.st_dev or pinned.st_ino != info.st_ino
                        or pinned.st_uid != info.st_uid
                        or stat.S_IMODE(pinned.st_mode)
                        != stat.S_IMODE(info.st_mode)
                        or not stat.S_ISDIR(pinned.st_mode)):
                    raise OSError(
                        "private Pi2 directory identity changed after verification"
                    )
            # Everything below is anchored to the pinned agent-directory
            # descriptor: temporary creation, rename, and cleanup use only
            # relative names with dir_fd=agent_fd, never pathnames.
            temp_name = (
                f".auth-return-{os.getpid()}.{threading.get_ident()}."
                f"{secrets.token_hex(8)}"
            )
            temp_fd = os.open(
                temp_name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | nofollow
                | os.O_CLOEXEC,
                0o600,
                dir_fd=agent_fd,
            )
            os.fchmod(temp_fd, 0o600)
            temp_info = os.fstat(temp_fd)
            if (temp_info.st_nlink != 1
                    or stat.S_IMODE(temp_info.st_mode) != 0o600):
                raise OSError(
                    "credential-return temp target preconditions not met"
                )
            view = memoryview(data)
            while view:
                written = os.write(temp_fd, view)
                if written <= 0:
                    raise OSError("short credential-return write")
                view = view[written:]
            os.fsync(temp_fd)
            os.close(temp_fd)
            temp_fd = -1
            os.replace(temp_name, "auth.json", src_dir_fd=agent_fd,
                       dst_dir_fd=agent_fd)
            temp_name = None
            os.fsync(agent_fd)
        except OSError as exc:
            raise SupervisionError(
                f"cannot publish the returned credential: {exc}"
            ) from exc
        finally:
            if temp_name is not None and agent_fd >= 0:
                try:
                    os.unlink(temp_name, dir_fd=agent_fd)
                except OSError:
                    pass
            for fd in (temp_fd, agent_fd, pi_fd, directory_fd):
                if fd >= 0:
                    try:
                        os.close(fd)
                    except OSError:
                        pass

    # -- the run -----------------------------------------------------------------

    @_outer_launch_cleanup
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
                for descriptor in authority._confinement_rule_fds:
                    try:
                        os.close(descriptor)
                    except OSError:
                        pass
                authority._confinement_rule_fds = ()
                if authority._prompt_fd >= 0:
                    try:
                        os.close(authority._prompt_fd)
                    except OSError:
                        pass
                    authority._prompt_fd = -1
                if authority._auth_fd >= 0:
                    try:
                        os.close(authority._auth_fd)
                    except OSError:
                        pass
                    authority._auth_fd = -1
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
        self._guard_extension = Path(authority._guard_extension)
        self._guard_digest = authority._guard_digest
        self._staged_digests = dict(authority._staged_digests)
        self._external_paths = tuple(authority._external_paths)
        self._external_runtime_bindings = tuple(authority._external_runtime_bindings)
        self._exec_dir = Path(authority._exec_dir)
        self._confinement_spec = dict(authority._confinement_spec) \
            if authority._confinement_spec else None
        self._confinement_proof = authority._confinement_proof
        self._sanitized_home = authority._sanitized_home
        self._confined_launcher = authority._confined_launcher
        if not authority._confinement_rule_fds:
            raise SupervisionError(
                "the verified authority carries no descriptor-anchored "
                "confinement rules or was already consumed"
            )
        self._confinement_rule_fds = tuple(authority._confinement_rule_fds)
        authority._confinement_rule_fds = ()
        if type(authority._prompt_fd) is not int or authority._prompt_fd < 0:
            raise SupervisionError(
                "the verified authority carries no sealed prompt memfd or was already consumed"
            )
        self.prompt_fd = authority._prompt_fd
        authority._prompt_fd = -1
        if type(authority._auth_fd) is not int or authority._auth_fd < -1:
            raise SupervisionError(
                "the verified authority carries an invalid auth descriptor"
            )
        self._auth_fd = authority._auth_fd
        authority._auth_fd = -1
        self._usage_guard_digests = authority._usage_guard_digests
        # The session path is proof-bound; the prompt is the already-consumed
        # anonymous sealed descriptor and is never represented by a pathname.
        if authority._session_dir is None:
            raise SupervisionError(
                "the verified authority carries no per-launch session path; "
                "no model can be started (fail closed)"
            )
        self.session_dir = Path(authority._session_dir)
        prompt = compose_prompt(
            binding,
            role_prompt=blobs["role_prompt"],
            agents=blobs["policy"],
            spec=blobs["spec"],
            plan=blobs["plan"],
            audit_objective=blobs.get("audit_objective"),
            task_excerpt=blobs.get("task_excerpt"),
            findings=blobs.get("findings"),
            task_budget=self._budget,
        )
        self._prompt_digest = hashlib.sha256(prompt).hexdigest()
        if _prompt_memfd_sha256(self.prompt_fd) != self._prompt_digest:
            raise SupervisionError(
                "the authority's sealed prompt memfd does not match the "
                "composed prompt bytes; refusing a substituted prompt (F5)"
            )
        # Task 11: every child/tool output channel is redacted through the
        # exact committed credential guard.  The redactor is verified and
        # bound to ``(workspace, bound_commit)`` *before* the child is
        # spawned, so a missing, uncommitted, or substituted guard fails
        # closed and no model can start with an unredacted capture channel.
        try:
            self._redactor = output_redaction.redactor_for(
                self.binding.workspace, self.binding.bound_commit
            )
            if self._redactor.digest != self._guard_digest:
                raise output_redaction.OutputRedactionError(
                    "the staged model guard digest differs from the exact "
                    "committed control-plane redactor digest"
                )
        except output_redaction.OutputRedactionError as exc:
            raise SupervisionError(
                "child output redaction is unavailable because the "
                f"credential guard cannot be verified: {exc}; no model may "
                "start with an unredacted output channel (Task 11)"
            ) from exc
        try:
            self.install_signal_handlers()
            # Phase 2A: a task whose cumulative budget is already exhausted
            # is never started (fail closed before spawn).
            self._budget_preflight()
            child = self.spawn()
            started = time.monotonic()
            self._started = started
            try:
                invariants = verify_child_invariants(
                    child.pid, binding.workspace, binding
                )
                snapshot = self.snapshot()
                # Phase 2A: the wall-clock budget caps the supervisor's own
                # runtime deadline (the smaller of the two bounds wins), so a
                # task whose cumulative wall budget is nearly consumed is
                # bounded by the budget, never by the role runtime limit.
                wall_budget = (
                    float(self._remaining["wall_time_seconds"])
                    if self._remaining is not None else None
                )
                deadline = started + (
                    min(binding.runtime_limit, wall_budget)
                    if wall_budget is not None
                    else binding.runtime_limit
                )
                out, err, reason, _ = self._monitor(
                    child, deadline, binding.inactivity_limit
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
                self._consume_confinement_status(
                    allow_missing=(
                        reason is not None
                        or (returncode is not None and returncode < 0)
                    )
                )
                if binding.provider.lower() == "openai-codex" and outcome == "completed":
                    if self._sanitized_home is None:
                        raise SupervisionError(
                            "openai-codex launch has no private home for credential refresh"
                        )
                    # The child process tree is fully terminated and the
                    # confinement lifecycle report is consumed, so the
                    # credential-return write end is closed and the reader
                    # thread has reached EOF.  A completed openai-codex launch
                    # must have returned exactly one JSON credential document
                    # through the pipe; it is published to the launch-private
                    # home and then persisted by the existing operator
                    # authority.  A noncompleted launch discards the returned
                    # bytes (never persisted).
                    returned = self._consume_credential_return()
                    if returned is None:
                        raise SupervisionError(
                            "openai-codex completed without a returned "
                            "credential document"
                        )
                    self._publish_credential_return_to_private_home(returned)
                    _persist_private_pi2_auth(Path(self._sanitized_home))
                elapsed = time.monotonic() - started
                # Phase 2A: record the attempt's measured usage into the
                # cumulative per-task ledger (monotonic, no-replace) before
                # the result is returned, so a tampered or foreign ledger
                # fails closed and the campaign classifies the bounded
                # exhaustion from the ledger.
                self._record_budget_usage(elapsed)
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
        post-spawn body: the exact broker group is bounded-terminated (TERM →
        INT → HUP → KILL), the broker is reaped, and its private lifecycle
        report is consumed.  The dedicated broker either kills/reaps every
        ptrace-pinned descendant itself or PTRACE_O_EXITKILL does so if the
        outer supervisor must KILL the broker.  No outer direct-child scan or
        baseline inference participates in cleanup.
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
            self._consume_confinement_status(allow_missing=True)
        except (EscapedDescendantError, SupervisionError):
            # The dedicated broker lifecycle is authoritative.  A malformed
            # failure report supersedes the triggering error rather than
            # pretending cleanup completed.
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
        """Close all inherited anchors/memfds and remove private directories."""
        self._close_streams()
        for descriptor in self._confinement_rule_fds:
            try:
                os.close(descriptor)
            except OSError:
                pass
        self._confinement_rule_fds = ()
        if self._confinement_status_fd is not None:
            try:
                os.close(self._confinement_status_fd)
            except OSError:
                pass
            self._confinement_status_fd = None
        if self.prompt_fd is not None:
            try:
                os.close(self.prompt_fd)
            except OSError:
                pass
            self.prompt_fd = None
        if self._auth_fd >= 0:
            try:
                os.close(self._auth_fd)
            except OSError:
                pass
            self._auth_fd = -1
        # Stop, join (bounded), and close the credential-return reader without
        # deadlock.  The child is always reaped before cleanup runs, so the
        # write end is normally closed and the thread reaches EOF promptly;
        # a stuck reader (a descendant that never closed the write end) is
        # stopped via the event, joined within a bounded window, and the read
        # end is closed to unblock it.  Any buffered credential bytes are
        # zeroed on this noncompleted path.
        if self._credential_return_thread is not None:
            if self._credential_return_stop is not None:
                self._credential_return_stop.set()
            self._credential_return_thread.join(
                timeout=CREDENTIAL_RETURN_JOIN_TIMEOUT
            )
            if self._credential_return_thread.is_alive():
                if self._credential_return_fd >= 0:
                    try:
                        os.close(self._credential_return_fd)
                    except OSError:
                        pass
                    self._credential_return_fd = -1
                self._credential_return_thread.join(
                    timeout=CREDENTIAL_RETURN_JOIN_TIMEOUT
                )
            self._credential_return_thread = None
        self._zero_credential_return()
        if self._credential_return_fd >= 0:
            try:
                os.close(self._credential_return_fd)
            except OSError:
                pass
            self._credential_return_fd = -1
        if self._credential_return_write_fd >= 0:
            try:
                os.close(self._credential_return_write_fd)
            except OSError:
                pass
            self._credential_return_write_fd = -1
        directories: List[Optional[Path]] = [
            self.session_dir, self._exec_dir, self._sanitized_home,
        ]
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
    # Quota credentials and controls are deliberately absent from this model-
    # side interface. The campaign parent runs QUOTA-01 for each invocation.


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
# boundary, stages exact committed wrapper/backend bytes into a private
# mode-0700 directory as non-executable mode-0400 interpreter inputs (or
# revalidates an external trusted executable's fully resolved path), and binds
# the token to the verified
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
    non-executable mode-0400 interpreter inputs, or a revalidated immutable
    external executable). ``binding`` is the *verified* binding (its backend
    already replaced by the staged script path), ``blobs`` maps each prompt
    component to its exact verified bytes, ``wrapper`` is the exact staged
    wrapper passed to the immutable interpreter, ``staged_digests``
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
        "_guard_extension",
        "_guard_digest",
        "_staged_digests",
        "_external_paths",
        "_external_runtime_bindings",
        "_exec_dir",
        "_prompt_fd",
        "_auth_fd",
        "_session_dir",
        "_confinement_spec",
        "_confinement_proof",
        "_sanitized_home",
        "_confined_launcher",
        "_confinement_rule_fds",
        "_usage_guard_digests",
        "_mint",
    )

    def __init__(
        self,
        binding: "InvocationBinding",
        blobs: Mapping[str, bytes],
        *,
        wrapper: Path,
        guard_extension: Path,
        guard_digest: str,
        staged_digests: Mapping[str, str],
        external_paths: Sequence[str],
        external_runtime_bindings: Sequence["_ExternalRuntimeBinding"],
        exec_dir: Path,
        prompt_fd: int,
        auth_fd: int = -1,
        session_dir: Path,
        confinement_spec: Optional[Mapping[str, object]] = None,
        confinement_proof: Optional[object] = None,
        sanitized_home: Optional[Path] = None,
        confined_launcher: Optional[Path] = None,
        confinement_rule_fds: Sequence[int] = (),
        usage_guard_digests: Optional[Sequence[str]] = None,
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
        self._guard_extension = Path(guard_extension)
        if not SHA256_RE.fullmatch(guard_digest):
            raise LaunchError("the staged credential-guard digest is invalid")
        self._guard_digest = guard_digest
        self._staged_digests = dict(staged_digests)
        self._external_paths = tuple(external_paths)
        self._external_runtime_bindings = tuple(external_runtime_bindings)
        if tuple(item.path for item in self._external_runtime_bindings) != self._external_paths:
            raise LaunchError("external runtime path and identity bindings differ")
        self._exec_dir = Path(exec_dir)
        # The composed prompt is carried only by this sealed anonymous memfd;
        # it has no pathname and is inherited by the wrapper exactly once.
        if type(prompt_fd) is not int or prompt_fd < 0:
            raise LaunchError("the launch authority prompt memfd is invalid")
        self._prompt_fd = prompt_fd
        if type(auth_fd) is not int or auth_fd < -1:
            raise LaunchError("the launch authority auth descriptor is invalid")
        self._auth_fd = auth_fd
        self._session_dir = Path(session_dir)
        # Task 8 confinement binding: the exact ``factory-confinement/v1``
        # specification the confined child applies, the real (never synthetic)
        # confinement proof minted against it, the fresh sanitized home the
        # proof/spec bind, and the staged confine-launcher executable.  A
        # descriptor anchors carried to the child.  No token can be minted
        # without this real confinement authority.
        self._confinement_spec = (
            dict(confinement_spec) if confinement_spec is not None else None
        )
        self._confinement_proof = confinement_proof
        self._sanitized_home = Path(sanitized_home) if sanitized_home else None
        self._confined_launcher = Path(confined_launcher) if confined_launcher else None
        self._confinement_rule_fds = tuple(int(fd) for fd in confinement_rule_fds)
        self._usage_guard_digests = (
            tuple(usage_guard_digests) if usage_guard_digests is not None else None
        )
        self._mint = _mint


def _prompt_memfd_sha256(descriptor: int) -> str:
    """Revalidate a sealed prompt descriptor and hash it without a pathname."""
    required = (
        getattr(fcntl, "F_SEAL_SEAL", 0)
        | getattr(fcntl, "F_SEAL_SHRINK", 0)
        | getattr(fcntl, "F_SEAL_GROW", 0)
        | getattr(fcntl, "F_SEAL_WRITE", 0)
    )
    try:
        info = os.fstat(descriptor)
        seals = fcntl.fcntl(descriptor, fcntl.F_GET_SEALS)
    except OSError as exc:
        raise LaunchError(f"cannot inspect the sealed prompt memfd: {exc}") from exc
    if not stat.S_ISREG(info.st_mode) or info.st_size > PROMPT_MAX_BYTES:
        raise LaunchError("the sealed prompt memfd is not a bounded regular file")
    if required == 0 or seals & required != required:
        raise LaunchError("the prompt memfd lacks mandatory immutable seals")
    digest = hashlib.sha256()
    offset = 0
    while offset < info.st_size:
        chunk = os.pread(descriptor, min(BLOB_READ_CHUNK, info.st_size - offset), offset)
        if not chunk:
            raise LaunchError("the sealed prompt memfd was truncated")
        digest.update(chunk)
        offset += len(chunk)
    return digest.hexdigest()


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
    """Publish exact committed bytes as a private mode-0400 single-link file.

    The file is created with ``O_EXCL``/``O_NOFOLLOW`` and is never executable.
    Staged Python/shell modules cross the kernel boundary only as readable
    arguments to an approved immutable interpreter, so a copied ELF or a
    caller-owned pathname can never become a broker-authorized exec target.
    The file's SHA-256 is re-checked immediately before interpreter launch.

    ``name`` may carry subdirectories (e.g. ``pi-cli-shims/git``) so the
    staged layout is deterministic and matches the extension's relative
    resolution.  Each intermediate directory is created fresh as a private
    mode-0700 directory and is never a symlink, so the staged shim's relative
    path cannot be redirected to a caller-owned target.
    """
    path = directory / name
    parent = path.parent
    if parent != directory:
        relative = parent.relative_to(directory)
        current = directory
        for part in relative.parts:
            current = current / part
            try:
                os.mkdir(current, STAGED_DIR_MODE)
            except FileExistsError:
                if current.is_symlink() or not current.is_dir():
                    raise LaunchError(
                        f"the staged interpreter input directory {current} is a symlink or not a directory"
                    ) from None
            except OSError as exc:
                raise LaunchError(
                    f"cannot create the staged interpreter input directory {current}: {exc}"
                ) from exc
    flags = (
        os.O_WRONLY | os.O_CREAT | os.O_EXCL
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )
    try:
        descriptor = os.open(str(path), flags, 0o700)
    except OSError as exc:
        raise LaunchError(f"cannot create the staged interpreter input {path}: {exc}") from exc
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
        raise LaunchError(f"cannot finalize the staged interpreter input {path}: {exc}") from exc
    info = path.stat()
    if stat.S_ISLNK(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.getuid():
        raise LaunchError(f"the staged interpreter input {path} has unsafe ownership/link state")
    if info.st_mode & 0o222:
        raise LaunchError(f"the staged interpreter input {path} is writable")
    return path


def _exec_staging_dir() -> Path:
    """Fresh private mode-0700 directory for staged interpreter inputs."""
    try:
        directory = Path(tempfile.mkdtemp(prefix=EXEC_STAGING_PREFIX, dir="/tmp"))
    except OSError as exc:
        raise LaunchError(
            f"cannot create the private staging directory: {exc}"
        ) from exc
    os.chmod(directory, STAGED_DIR_MODE)
    return directory


_NIX_LITERAL_RE = re.compile(rb"/nix/store/[A-Za-z0-9._+/@=-]+")


@dataclass(frozen=True)
class _ExternalRuntimeBinding:
    """Exact immutable external runtime identity retained to exec."""

    path: str
    sha256: str
    device: int
    inode: int
    executable: bool


def _bind_external_runtime(path: str, *, executable: bool) -> _ExternalRuntimeBinding:
    """Bind canonical path, immutable chain, bytes, device, and inode."""
    canonical = os.path.realpath(path)
    if not canonical or canonical != path:
        raise InvocationError(
            f"external runtime path is not canonical: {path!r} -> {canonical!r}"
        )
    try:
        if executable:
            require_trusted_executable(canonical)
        else:
            require_trusted_regular_file(canonical)
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
        descriptor = os.open(canonical, flags)
        try:
            before = os.fstat(descriptor)
            digest = hashlib.sha256()
            offset = 0
            while True:
                chunk = os.pread(descriptor, 65536, offset)
                if not chunk:
                    break
                digest.update(chunk)
                offset += len(chunk)
            after = os.fstat(descriptor)
            named = os.lstat(canonical)
        finally:
            os.close(descriptor)
    except (OSError, GitBoundaryError) as exc:
        raise InvocationError(
            f"cannot bind immutable external runtime {canonical}: {exc}"
        ) from exc
    identity = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
    if identity != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns):
        raise InvocationError(
            f"immutable external runtime changed while binding: {canonical}"
        )
    if (named.st_dev, named.st_ino) != (before.st_dev, before.st_ino):
        raise InvocationError(
            f"immutable external runtime pathname changed while binding: {canonical}"
        )
    return _ExternalRuntimeBinding(
        canonical, digest.hexdigest(), before.st_dev, before.st_ino, executable
    )


def _revalidate_external_runtime(binding: _ExternalRuntimeBinding) -> None:
    """Re-derive a retained external identity immediately at a trust edge."""
    current = _bind_external_runtime(binding.path, executable=binding.executable)
    if current != binding:
        raise InvocationError(
            f"immutable external runtime identity changed: {binding.path}"
        )


def _revalidate_external_runtimes(
    bindings: Sequence[_ExternalRuntimeBinding],
) -> None:
    for binding in bindings:
        _revalidate_external_runtime(binding)


def _resolve_pi2_runtime(wrapper: str) -> Tuple[_ExternalRuntimeBinding, _ExternalRuntimeBinding]:
    """Resolve immutable Node/CLI files named by the exact Pi2 wrapper chain."""
    wrapper = os.path.realpath(wrapper)
    if Path(wrapper).name != "pi2":
        raise InvocationError("openai-codex requires the trusted pi2 executable")
    pending = [wrapper]
    seen: set[str] = set()
    cli_candidates: set[str] = set()
    while pending:
        path = pending.pop(0)
        if path in seen:
            continue
        if len(seen) >= 64:
            raise InvocationError("pi2 immutable wrapper chain exceeds its bound")
        try:
            require_trusted_executable(path)
            data = Path(path).read_bytes()
        except (OSError, GitBoundaryError) as exc:
            raise InvocationError(f"cannot bind the immutable pi2 runtime: {exc}") from exc
        seen.add(path)
        if len(data) > (1 << 20) or b"\x00" in data[:4096]:
            continue
        for raw in _NIX_LITERAL_RE.findall(data):
            candidate = raw.decode("utf-8", "strict").rstrip("),;:")
            if candidate.endswith("/dist/cli.js") and os.path.isfile(candidate):
                cli_candidates.add(candidate)
            if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
                resolved = os.path.realpath(candidate)
                try:
                    first = Path(resolved).read_bytes()[:2]
                except OSError:
                    continue
                if first == b"#!" and resolved not in seen and resolved not in pending:
                    pending.append(resolved)
    node_link = Path.home() / ".pi" / "agent2" / "bin" / "node"
    node = os.path.realpath(str(node_link))
    if len(cli_candidates) != 1:
        raise InvocationError(
            f"pi2 wrapper chain must identify exactly one Pi CLI, found {len(cli_candidates)}"
        )
    # CLI data is as security-sensitive as Node: the adapter invokes this exact
    # module directly, so bind its canonical immutable chain and byte/inode
    # identity even though it has no execute bit.
    cli = os.path.realpath(next(iter(cli_candidates)))
    if not cli.startswith("/nix/store/"):
        raise InvocationError("the Pi CLI module is not canonical Nix-store data")
    node_binding = _bind_external_runtime(node, executable=True)
    cli_binding = _bind_external_runtime(cli, executable=False)
    return node_binding, cli_binding


def _prepare_private_pi2_home(sanitized_home: Path) -> int:
    """Prepare the private Pi2 agent directory and return the sealed auth fd.

    The operator's ``auth.json`` credential is **never** written to any
    model/tool-readable path before confinement (B1 security review). Only the
    non-secret catalog and empty settings are initially materialised; the
    credential bytes travel in one anonymous writable memfd. After Landlock,
    the exact adapter creates Pi's private mode-0600 auth file. The guard
    extension identity-checks and detaches that file around each tool, closes
    every descriptor alias, then restores it from extension-owned memory for
    the next authenticated turn. Landlock also denies ``/proc``.
    """
    source = Path.home() / ".pi" / "agent2"
    target = sanitized_home / ".pi" / "agent2"
    target.mkdir(mode=0o700, parents=True, exist_ok=False)
    for name, maximum in (("models.json", 4 << 20),):
        src = source / name
        info = os.lstat(src)
        if (
            not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
            or info.st_nlink != 1 or info.st_mode & 0o077
            or info.st_size > maximum
        ):
            raise InvocationError(f"operator Pi2 {name} has unsafe ownership or mode")
        source_fd = os.open(src, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        dest = target / name
        dest_fd = os.open(
            dest, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC, 0o600
        )
        try:
            while True:
                chunk = os.read(source_fd, 65536)
                if not chunk:
                    break
                os.write(dest_fd, chunk)
            os.fsync(dest_fd)
        finally:
            os.close(source_fd)
            os.close(dest_fd)
    settings = target / "settings.json"
    fd = os.open(settings, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC, 0o600)
    try:
        os.write(fd, b"{}\n")
        os.fsync(fd)
    finally:
        os.close(fd)
    # The credential crosses the pre-confinement boundary only in an anonymous
    # writable memfd. The exact adapter/extension own its post-confinement
    # detach/restore cycle; tool subprocesses do not inherit it and Landlock
    # denies /proc.
    auth_source = source / "auth.json"
    info = os.lstat(auth_source)
    if (
        not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
        or info.st_nlink != 1 or info.st_mode & 0o077
        or info.st_size > (1 << 20)
    ):
        raise InvocationError(
            f"operator Pi2 auth.json has unsafe ownership or mode"
        )
    if not hasattr(os, "memfd_create"):
        raise InvocationError(
            "openai-codex requires memfd credential transport, which is "
            "unavailable on this host (fail closed)"
        )
    auth_fd = -1
    try:
        auth_fd = os.memfd_create("factory-pi2-auth", 0)
        source_fd = os.open(
            auth_source, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC
        )
        try:
            while True:
                chunk = os.read(source_fd, 65536)
                if not chunk:
                    break
                os.write(auth_fd, chunk)
        finally:
            os.close(source_fd)
        os.lseek(auth_fd, 0, os.SEEK_SET)
        after = os.fstat(auth_fd)
        if not stat.S_ISREG(after.st_mode) or after.st_size != info.st_size:
            raise InvocationError(
                "operator Pi2 auth.json memfd transport failed verification"
            )
        return auth_fd
    except BaseException:
        if auth_fd >= 0:
            try:
                os.close(auth_fd)
            except OSError:
                pass
        raise


def _persist_private_pi2_auth(
    sanitized_home: Path, *, operator_home: Optional[Path] = None
) -> bool:
    """Atomically persist a Pi2 OAuth refresh from one trusted role process.

    Fresh roles must not reuse model context, but rotating OAuth refresh tokens
    are provider state rather than model memory. Pi writes a refreshed token to
    the launch-private ``auth.json``; after the child is fully reaped, this
    trusted parent validates that file and atomically replaces the operator's
    existing mode-0600 store. No model/tool path can select either endpoint.

    The operator ``agent2`` directory is pinned once: opened with
    ``O_DIRECTORY|O_NOFOLLOW`` and fstat-bound (dev/ino/uid/mode) against the
    lstat expectation taken just before the open, so a path swap in the window
    is detected and fails closed.  From then on only relative names anchored
    to that pinned directory descriptor are used: a unique
    ``O_CREAT|O_EXCL|O_NOFOLLOW`` temp file (fstat-checked for
    nlink/regular/uid/mode), a dirfd-relative nofollow re-validation of the
    expected ``auth.json`` identity immediately before the replace, an
    ``os.replace`` with ``src_dir_fd``/``dst_dir_fd``, and ``os.unlink``
    cleanup.  A pathname swap after binding therefore cannot redirect the
    credential anywhere: the bytes land in the verified original directory or
    the refresh fails closed.
    """
    source = Path(sanitized_home) / ".pi" / "agent2" / "auth.json"
    target_dir = (operator_home or Path.home()) / ".pi" / "agent2"
    target = target_dir / "auth.json"
    try:
        source_info = os.lstat(source)
        target_dir_info = os.lstat(target_dir)
        target_info = os.lstat(target)
    except OSError as exc:
        raise SupervisionError(f"Pi2 credential refresh path is unavailable: {exc}") from exc
    uid = os.getuid()
    if (
        not stat.S_ISREG(source_info.st_mode) or stat.S_ISLNK(source_info.st_mode)
        or source_info.st_uid != uid or source_info.st_nlink != 1
        or stat.S_IMODE(source_info.st_mode) != 0o600
        or source_info.st_size <= 0 or source_info.st_size > (1 << 20)
        or not stat.S_ISDIR(target_dir_info.st_mode)
        or stat.S_ISLNK(target_dir_info.st_mode) or target_dir_info.st_uid != uid
        or target_dir_info.st_mode & 0o022
        or not stat.S_ISREG(target_info.st_mode) or stat.S_ISLNK(target_info.st_mode)
        or target_info.st_uid != uid or target_info.st_nlink != 1
        or stat.S_IMODE(target_info.st_mode) != 0o600
        or target_info.st_size <= 0 or target_info.st_size > (1 << 20)
    ):
        raise SupervisionError("Pi2 credential refresh path has unsafe identity or mode")
    def read_bound_auth(path: Path, expected: os.stat_result, label: str) -> bytearray:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        try:
            opened = os.fstat(descriptor)
            if (
                (opened.st_dev, opened.st_ino) != (expected.st_dev, expected.st_ino)
                or not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1
                or opened.st_uid != uid or stat.S_IMODE(opened.st_mode) != 0o600
            ):
                raise SupervisionError(f"Pi2 {label} auth identity changed before refresh")
            payload = bytearray()
            while len(payload) <= (1 << 20):
                chunk = os.read(
                    descriptor, min(65536, (1 << 20) + 1 - len(payload))
                )
                if not chunk:
                    break
                payload.extend(chunk)
            if not payload or len(payload) > (1 << 20):
                raise SupervisionError(
                    f"Pi2 {label} auth bytes are empty or oversized"
                )
            return payload
        finally:
            os.close(descriptor)

    data = read_bound_auth(source, source_info, "private")
    try:
        original_data = read_bound_auth(target, target_info, "operator")
    except BaseException:
        data[:] = b"\x00" * len(data)
        raise
    try:
        try:
            parsed = json.loads(data)
            original = json.loads(original_data)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise SupervisionError("Pi2 auth refresh is malformed JSON") from exc
        codex = parsed.get("openai-codex") if isinstance(parsed, dict) else None
        original_codex = (
            original.get("openai-codex") if isinstance(original, dict) else None
        )
        if (
            not isinstance(codex, dict)
            or not isinstance(original_codex, dict)
            or codex.get("type") != "oauth"
            or original_codex.get("type") != "oauth"
        ):
            raise SupervisionError(
                "Pi2 auth refresh lacks the original openai-codex OAuth binding"
            )
        access = codex.get("access")
        refresh = codex.get("refresh")
        account = codex.get("accountId")
        expires = codex.get("expires")
        original_refresh = original_codex.get("refresh")
        original_account = original_codex.get("accountId")
        original_access = original_codex.get("access")
        original_expires = original_codex.get("expires")
        if (
            not isinstance(access, str) or not (16 <= len(access) <= 16_384)
            or not isinstance(refresh, str) or not (16 <= len(refresh) <= 16_384)
            or not isinstance(original_refresh, str)
            or not (16 <= len(original_refresh) <= 16_384)
            or not isinstance(account, str) or not account or len(account) > 1024
            or account != original_account
            or isinstance(expires, bool) or not isinstance(expires, (int, float))
            or not math.isfinite(float(expires))
            or float(expires) <= time.time() * 1000.0 + 300_000.0
        ):
            raise SupervisionError(
                "Pi2 auth refresh has invalid access/refresh/account/expiry binding"
            )
        # The provider may rotate its refresh token, but an arbitrary new
        # refresh string is not accepted by shape alone. It must form one
        # coherent transition from the exact operator credential: same account
        # and unrelated providers, a new access token, and a strictly later
        # finite expiry. An unchanged refresh remains a legitimate access-token
        # refresh and is accepted.
        if refresh != original_refresh and (
            access == original_access
            or isinstance(original_expires, bool)
            or not isinstance(original_expires, (int, float))
            or not math.isfinite(float(original_expires))
            or float(expires) <= float(original_expires)
        ):
            raise SupervisionError(
                "Pi2 rotating refresh token is not bound to a coherent provider transition"
            )
        if set(parsed) != set(original) or any(
            parsed[key] != original[key]
            for key in original
            if key != "openai-codex"
        ):
            raise SupervisionError(
                "Pi2 auth refresh changed an unrelated operator credential binding"
            )
    except BaseException:
        data[:] = b"\x00" * len(data)
        raise
    finally:
        original_data[:] = b"\x00" * len(original_data)
    directory_fd = os.open(
        target_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    )
    temp_name: Optional[str] = None
    temp_fd = -1
    try:
        anchored = os.fstat(directory_fd)
        if (
            anchored.st_dev != target_dir_info.st_dev
            or anchored.st_ino != target_dir_info.st_ino
            or anchored.st_uid != uid
            or stat.S_IMODE(anchored.st_mode)
            != stat.S_IMODE(target_dir_info.st_mode)
            or not stat.S_ISDIR(anchored.st_mode)
        ):
            raise SupervisionError("Pi2 operator auth directory identity changed")
        # From here only relative names anchored to the pinned directory
        # descriptor are used (temp creation, target re-validation, replace,
        # and cleanup all pass dir_fd=directory_fd), so a pathname swap after
        # binding cannot redirect the credential anywhere else.
        temp_name = (
            f".auth-refresh-{os.getpid()}.{threading.get_ident()}."
            f"{secrets.token_hex(8)}"
        )
        temp_fd = os.open(
            temp_name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
            | os.O_CLOEXEC,
            0o600,
            dir_fd=directory_fd,
        )
        os.fchmod(temp_fd, 0o600)
        temp_info = os.fstat(temp_fd)
        if (
            temp_info.st_nlink != 1
            or not stat.S_ISREG(temp_info.st_mode)
            or temp_info.st_uid != uid
            or stat.S_IMODE(temp_info.st_mode) != 0o600
        ):
            raise SupervisionError(
                "Pi2 auth refresh temp target preconditions not met"
            )
        view = memoryview(data)
        while view:
            written = os.write(temp_fd, view)
            if written <= 0:
                raise OSError("short Pi2 auth refresh write")
            view = view[written:]
        os.fsync(temp_fd)
        os.close(temp_fd)
        temp_fd = -1
        current_target = os.stat(
            "auth.json", dir_fd=directory_fd, follow_symlinks=False
        )
        if (
            current_target.st_dev != target_info.st_dev
            or current_target.st_ino != target_info.st_ino
            or current_target.st_uid != uid
            or current_target.st_nlink != 1
            or stat.S_IMODE(current_target.st_mode) != 0o600
            or not stat.S_ISREG(current_target.st_mode)
        ):
            raise SupervisionError(
                "Pi2 operator auth identity changed before replace"
            )
        os.replace(
            temp_name, "auth.json", src_dir_fd=directory_fd,
            dst_dir_fd=directory_fd,
        )
        temp_name = None
        os.fsync(directory_fd)
        return True
    except OSError as exc:
        raise SupervisionError(f"cannot persist Pi2 credential refresh: {exc}") from exc
    finally:
        data[:] = b"\x00" * len(data)
        if temp_fd >= 0:
            os.close(temp_fd)
        if temp_name is not None:
            try:
                os.unlink(temp_name, dir_fd=directory_fd)
            except OSError:
                pass
        os.close(directory_fd)


def _stage_launch_executables(
    binding: "InvocationBinding",
) -> Tuple[
    Path, Path, Path, str, Dict[str, str], List[str],
    List[_ExternalRuntimeBinding], Path, bool,
]:
    """Stage every committed byte that will execute in the model process.

    The wrapper, Pi guard extension, credential guard, Git shim, and an
    in-workspace backend are copied from exact bound-commit blobs into one
    private mode-0700 directory.  ``--extension`` points only at the staged
    extension, whose relative guard/shim paths therefore resolve to the same
    digest-bound staging directory.  Every staged digest is rechecked at the
    immediate exec boundary.
    """
    workspace = Path(binding.workspace).absolute()
    bound_commit = binding.bound_commit
    staged_digests: Dict[str, str] = {}
    external_paths: List[str] = []
    external_runtime_bindings: List[_ExternalRuntimeBinding] = []
    exec_dir = _exec_staging_dir()

    def stage_committed(relpath: str, name: str, label: str,
                        maximum: int = PROMPT_INPUT_MAX) -> Tuple[Path, bytes]:
        source = workspace / relpath
        data = _read_committed_blob(
            str(source), workspace, bound_commit, label, maximum
        )
        path = _stage_bytes(exec_dir, name, data)
        staged_digests[str(path)] = hashlib.sha256(data).hexdigest()
        return path, data

    try:
        wrapper, _ = stage_committed(
            SECURE_WRAPPER, STAGED_WRAPPER_NAME, "secure wrapper"
        )
        extension, _ = stage_committed(
            PI_FACTORY_GUARD_EXTENSION,
            STAGED_GUARD_EXTENSION_NAME,
            "model-side Pi guard extension",
        )
        _guard_path, guard_bytes = stage_committed(
            CREDENTIAL_GUARD,
            STAGED_CREDENTIAL_GUARD_NAME,
            "model-side credential guard",
            output_redaction.MAX_GUARD_SOURCE_BYTES,
        )
        stage_committed(
            PI_GIT_SHIM, STAGED_GIT_SHIM_NAME, "model-side Git shim"
        )
        guard_digest = hashlib.sha256(guard_bytes).hexdigest()

        def bind_external_backend(resolved: str) -> Path:
            resolved = os.path.realpath(resolved)
            wrapper_binding = _bind_external_runtime(resolved, executable=True)
            external_paths.append(wrapper_binding.path)
            external_runtime_bindings.append(wrapper_binding)
            if binding.provider.lower() != "openai-codex":
                return Path(resolved)
            node_binding, cli_binding = _resolve_pi2_runtime(resolved)
            adapter_source = _read_committed_blob(
                str(workspace / PI2_BACKEND_ADAPTER), workspace, bound_commit,
                "Pi2 factory adapter", PROMPT_INPUT_MAX,
            )
            if (
                adapter_source.count(b"@@FACTORY_PI2_NODE@@") != 1
                or adapter_source.count(b"@@FACTORY_PI2_CLI@@") != 1
            ):
                raise InvocationError("the committed Pi2 adapter markers are ambiguous")
            adapter_source = adapter_source.replace(
                b"@@FACTORY_PI2_NODE@@", node_binding.path.encode("utf-8")
            ).replace(b"@@FACTORY_PI2_CLI@@", cli_binding.path.encode("utf-8"))
            staged = _stage_bytes(exec_dir, STAGED_BACKEND_NAME, adapter_source)
            staged_digests[str(staged)] = hashlib.sha256(adapter_source).hexdigest()
            for runtime in (node_binding, cli_binding):
                external_paths.append(runtime.path)
                external_runtime_bindings.append(runtime)
            return staged

        backend_path = Path(binding.backend).absolute()
        backend_is_symlink = os.path.islink(str(backend_path))
        if backend_is_symlink:
            resolved = os.path.realpath(str(backend_path))
            try:
                Path(resolved).relative_to(workspace)
            except ValueError:
                try:
                    require_trusted_executable(resolved)
                except GitBoundaryError as exc:
                    raise InvocationError(
                        f"model backend {backend_path} is a symlink whose resolved "
                        f"target {resolved} is not a trusted immutable executable: "
                        f"{exc} (F2)"
                    ) from exc
                trusted_backend = bind_external_backend(resolved)
                return (wrapper, trusted_backend, extension, guard_digest,
                        staged_digests, external_paths, external_runtime_bindings,
                        exec_dir, binding.provider.lower() == "openai-codex")
            backend_bytes = _read_committed_blob(
                resolved, workspace, bound_commit,
                "model backend", PROMPT_INPUT_MAX,
            )
        else:
            try:
                backend_path.relative_to(workspace)
            except ValueError:
                resolved = os.path.realpath(str(backend_path))
                try:
                    require_trusted_executable(resolved)
                except GitBoundaryError as exc:
                    raise InvocationError(
                        f"model backend {backend_path} is neither a committed "
                        f"workspace blob nor an external trusted executable: "
                        f"{exc} (F2)"
                    ) from exc
                trusted_backend = bind_external_backend(resolved)
                return (wrapper, trusted_backend, extension, guard_digest,
                        staged_digests, external_paths, external_runtime_bindings,
                        exec_dir, binding.provider.lower() == "openai-codex")
            backend_bytes = _read_committed_blob(
                str(backend_path), workspace, bound_commit,
                "model backend", PROMPT_INPUT_MAX,
            )
        first_line = backend_bytes.splitlines()[0] if backend_bytes else b""
        if backend_bytes.startswith(b"\x7fELF"):
            raise InvocationError(
                "a copied workspace ELF can never become a staged executable; "
                "use a trusted immutable external backend"
            )
        if not first_line.startswith(b"#!") or b"python" not in first_line.lower():
            raise InvocationError(
                "a staged workspace backend must be a Python script consumed "
                "by the trusted immutable interpreter"
            )
        staged_backend = _stage_bytes(
            exec_dir, STAGED_BACKEND_NAME, backend_bytes
        )
        staged_digests[str(staged_backend)] = hashlib.sha256(
            backend_bytes
        ).hexdigest()
        return (wrapper, staged_backend, extension, guard_digest,
                staged_digests, external_paths, external_runtime_bindings,
                exec_dir, False)
    except BaseException:
        shutil.rmtree(exec_dir, ignore_errors=True)
        raise


class UsageConfigError(Exception):
    """Inert confinement-adapter configuration error (no quota API)."""


class _UsageConfinementAdapter:
    """Only the non-credential path contract needed to mint a proof."""

    DEFAULT_SETTINGS_URL = CANONICAL_OLLAMA_SETTINGS_URL

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
            raise UsageConfigError("cannot resolve the operator usage store") from exc
        if store == root or store.is_relative_to(root):
            raise UsageConfigError("operator usage store is inside the model workspace")


def _stage_committed_usage_guard(
    binding: "InvocationBinding", exec_dir: Path,
    staged_digests: Dict[str, str],
) -> Tuple[object, Tuple[str, str]]:
    """Stage and import the exact bound-commit usage guard blob pair.

    ``usage.py`` derives its fetch-child path from ``__file__``; assigning
    the staged path therefore makes every fetch execute the staged committed
    ``usage_fetch.py`` sibling rather than a mutable worktree pathname.
    """
    names = (STAGED_USAGE_GUARD_NAME, STAGED_USAGE_FETCH_NAME)
    staged: List[Path] = []
    source_blobs: List[bytes] = []
    for relpath, name in zip(USAGE_GUARD_SOURCES, names):
        data = _read_committed_blob(
            str(Path(binding.workspace).absolute() / relpath),
            Path(binding.workspace).absolute(),
            binding.bound_commit,
            f"usage guard source {relpath}",
            real_confinement_authority._MAX_GUARD_SOURCE_BYTES,
        )
        path = _stage_bytes(exec_dir, name, data)
        staged_digests[str(path)] = hashlib.sha256(data).hexdigest()
        staged.append(path)
        source_blobs.append(data)
    if b'DEFAULT_SETTINGS_URL = "https://ollama.com/settings"' not in source_blobs[0].splitlines():
        raise InvocationError(
            "the committed usage guard does not carry the canonical Ollama settings endpoint"
        )
    # Never import or execute the staged quota implementation in the launch
    # coordinator. The inert adapter carries only confinement path semantics;
    # no cookie/quota callable can become reachable through globals,
    # sys.modules, an authority token, or supervision state.
    adapter = _UsageConfinementAdapter()
    return adapter, tuple(
        hashlib.sha256(data).hexdigest() for data in source_blobs
    )  # type: ignore[return-value]


def _backend_is_external(binding: "InvocationBinding") -> bool:
    """True when the model backend resolves outside the canonical workspace.

    A backend path outside the workspace (or a workspace symlink whose
    resolved target is outside) is an external trusted executable whose
    configuration must be confined and transported under the same
    mandatory real-confinement and credential/source boundary as the
    Ollama provider (Task 11).
    """
    workspace = Path(binding.workspace).absolute()
    backend = Path(binding.backend).absolute()
    try:
        backend.relative_to(workspace)
    except ValueError:
        return True
    if os.path.islink(str(backend)):
        resolved = os.path.realpath(str(backend))
        try:
            Path(resolved).relative_to(workspace)
        except ValueError:
            return True
    return False


def _canonical_ollama_settings_url(guard: object) -> str:
    """Return the one production endpoint, or fail before credential access.

    The launch API has no URL argument.  Both this control-plane constant and
    the exact-commit usage module must name the byte-exact canonical endpoint,
    whose parsed authority is HTTPS, ``ollama.com``, effective port 443, and
    path ``/settings`` with no userinfo/query/fragment.  This check runs before
    any cookie/store descriptor is opened.
    """
    value = getattr(guard, "DEFAULT_SETTINGS_URL", None)
    if value != CANONICAL_OLLAMA_SETTINGS_URL:
        raise InvocationError(
            "the committed usage guard does not carry the canonical Ollama settings endpoint"
        )
    try:
        parsed = urllib.parse.urlsplit(value)
        port = parsed.port
    except (TypeError, ValueError) as exc:
        raise InvocationError("the canonical Ollama settings endpoint is malformed") from exc
    effective_port = 443 if port is None else port
    if (
        parsed.scheme != "https"
        or parsed.hostname != "ollama.com"
        or effective_port != 443
        or parsed.path != "/settings"
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.netloc not in ("ollama.com", "ollama.com:443")
    ):
        raise InvocationError(
            "the production Ollama settings endpoint must be exactly HTTPS "
            "ollama.com:443 /settings without userinfo, query, or fragment"
        )
    return CANONICAL_OLLAMA_SETTINGS_URL


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
    task_budget: Optional[Mapping[str, object]] = None,
    _authorization_store: Optional[object] = None,
    _authorization_token: str = "",
    _authorization_claims: Optional[Mapping[str, object]] = None,
) -> LaunchAuthority:
    """Mint the unforgeable verified-committed authority token (F2/F5).

    Every authoritative byte is verified against the committed Git blobs at
    the bound commit: the wrapper and backend pass the F2 boundary (exact
    committed bytes become non-executable mode-0400 interpreter inputs in a
    private mode-0700 directory, or an external trusted executable's fully
    resolved path is revalidated) and each prompt blob's digest must equal the binding's
    digest — a substituted, paraphrased, foreign, or operator-claimed byte
    set fails closed.  The returned :class:`LaunchAuthority` is the only
    value :meth:`LaunchSupervision.run` accepts; it cannot be constructed
    from operator claims.

    **Mandatory real confinement.**  Every invocation, including the
    hermetic synthetic model provider, uses an internally constructed
    canonical ``factory-confinement/v1`` specification and an internally
    minted real Landlock proof.  The installed/programmatic surface accepts
    no caller proof, confinement specification, saved-HTML transport, or
    loopback opt-in.  Every allowlist rule binds dev/inode/type/owner/link
    count and is retained as an inherited descriptor, so the confined child
    never reopens a validated rule by pathname.

    **Cleanup on authorization failure (Task 8 review, finding 6).**  Any
    failure to authorize or confine the launch removes and cleans every
    per-launch private directory the mint created — the staging directory,
    the prompt directory, the session directory, and the sanitized home —
    so no private or credential material survives a failed authorization.

    **Ollama confinement, not quota policy.**  An ``ollama`` provider still
    receives the same exact-commit staged usage-source and Task 8 proof that
    keeps operator credential stores inaccessible to model tools.  The
    decision table itself is absent from this mint and from its public API:
    the campaign parent has already run it immediately before this invocation.
    No ``--usage-guard-*`` launch option exists, and authorization
    neither opens a cookie store nor invokes ``require_quota``.

    Real providers also require a fresh one-use mint from the exclusively
    locked Campaign. Standalone and programmatic launch remain synthetic-only.
    """
    verify_invocation(binding)
    if binding.provider.lower() != "synthetic":
        try:
            from . import readiness as _readiness
        except ImportError:
            import readiness as _readiness  # type: ignore[no-redef]
        if type(_authorization_store) is not _readiness.AuthorizationStore:
            raise InvocationError(
                "real-provider authorization requires the locked readiness store; standalone/programmatic launch is synthetic-only"
            )
        if not isinstance(_authorization_claims, Mapping):
            raise InvocationError("real-provider launch claims are absent")
        try:
            _authorization_store.consume(_authorization_token,
                                         _authorization_claims)
        except _readiness.AuthorizationError as exc:
            raise InvocationError(f"real-provider authorization refused: {exc}") from exc
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
    (
        wrapper,
        backend,
        guard_extension,
        guard_digest,
        staged_digests,
        external_paths,
        external_runtime_bindings,
        exec_dir,
        pi2_verified,
    ) = _stage_launch_executables(binding)
    private_dirs: List[Path] = [exec_dir]
    prompt_fd = -1
    auth_fd = -1
    rule_fd_list: List[int] = []
    rule_fds: Tuple[int, ...] = ()
    try:
        # Production constructs every confinement input itself.  Callers can
        # neither inject a proof/spec/home nor opt into a synthetic transport.
        sanitized_home = real_confinement_authority.sanitized_home_directory()
        private_dirs.append(sanitized_home)
        if binding.provider.lower() == "openai-codex":
            # B2 security review: the openai-codex credential is provisioned
            # only after the exact immutable external pi2 wrapper identity is
            # verified.  A workspace backend (or any other backend) with the
            # openai-codex provider fails closed and never receives the
            # credential.
            if not pi2_verified:
                raise InvocationError(
                    "openai-codex requires the exact immutable external pi2 "
                    "wrapper as the model backend; a workspace or other "
                    "backend fails closed before any credential is "
                    "provisioned (B2)"
                )
            # The initial discovery is not enough: immediately before opening
            # operator auth bytes, revalidate the canonical pi2 wrapper, Node,
            # and CLI digest/dev/inode bindings as one complete identity.
            _revalidate_external_runtimes(external_runtime_bindings)
            auth_fd = _prepare_private_pi2_home(sanitized_home)
        try:
            base_spec = real_confinement_authority.confinement_spec(
                binding,
                sanitized_home=sanitized_home,
                extra_write=(
                    [binding.result_write_path]
                    if binding.result_write_path else []
                ),
                _rule_descriptors=rule_fd_list,
            )
        except real_confinement_authority.ConfinementError as exc:
            raise InvocationError(
                f"cannot construct the canonical confinement specification: {exc}"
            ) from exc

        # The exact committed guard and confine-launcher bytes are staged for
        # every provider.  The synthetic model backend remains hermetic, but
        # its filesystem proof is the same real Landlock proof as production.
        committed_usage_guard, usage_guard_digests = _stage_committed_usage_guard(
            binding, exec_dir, staged_digests
        )
        if binding.provider.lower() in PROVIDER_GUARD_REQUIRED:
            # Validate the *exact bound-commit module that will execute*, not
            # merely the already-imported worktree module, before proof/channel
            # construction can inspect any cookie/store descriptor.
            _canonical_ollama_settings_url(committed_usage_guard)
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
            task_budget=task_budget,
        )
        prompt_fd = _sealed_prompt_memfd(prompt)
        session_dir = _session_directory()
        private_dirs.append(session_dir)

        confinement_spec = real_confinement_authority.with_private_launch_paths(
            base_spec,
            staging_dir=exec_dir,
            session_dir=session_dir,
            _rule_descriptors=rule_fd_list,
        )
        try:
            # Every descriptor is the one opened by the exact component-wise
            # validation operation; production never closes and reopens a rule
            # pathname.  Revalidate those retained anchors before proof mint.
            rule_fds = tuple(rule_fd_list)
            real_confinement_authority.validate_rule_anchors(
                confinement_spec, rule_fds
            )
            real_proof = real_confinement_authority.prove_confinement(
                binding,
                confinement_spec=confinement_spec,
                cookie_file=None,
                cookie_stdin=False,
                _executing_guard_digests=usage_guard_digests,
                _usage_guard_module=committed_usage_guard,
            )
            real_confinement_authority.validate_proof(
                real_proof,
                binding,
                confinement_spec=confinement_spec,
                cookie_file=None,
                cookie_stdin=False,
                _executing_guard_digests=usage_guard_digests,
                _usage_guard_module=committed_usage_guard,
            )
        except real_confinement_authority.ConfinementUnavailable as exc:
            raise InvocationError(
                "the production launch fails closed: the real model "
                f"workspace confinement is unavailable on this host ({exc})"
            ) from exc
        except real_confinement_authority.ConfinementError as exc:
            raise InvocationError(
                "the production launch fails closed: the real confinement "
                f"proof/rule anchor cannot bind this invocation ({exc})"
            ) from exc
        # Quota has already run in the trusted campaign parent for this exact
        # invocation; no credential or quota surface enters LaunchAuthority.
        verified = replace(binding, backend=backend)
        return LaunchAuthority(
            verified,
            blobs,
            wrapper=wrapper,
            guard_extension=guard_extension,
            guard_digest=guard_digest,
            staged_digests=staged_digests,
            external_paths=external_paths,
            external_runtime_bindings=external_runtime_bindings,
            exec_dir=exec_dir,
            prompt_fd=prompt_fd,
            auth_fd=auth_fd,
            session_dir=session_dir,
            confinement_spec=confinement_spec,
            confinement_proof=real_proof,
            sanitized_home=sanitized_home,
            confined_launcher=confined_launcher,
            confinement_rule_fds=rule_fds,
            usage_guard_digests=usage_guard_digests,
            _mint=_MINT_SECRET,
        )
    except BaseException:
        # Task 8 review, finding 6: every private per-launch directory this
        # mint created — the staging directory, the prompt directory, the
        # session directory, and the sanitized home — is removed on any
        # authorization failure, so no private or credential material
        # survives a failed authorization.
        for descriptor in set(rule_fds or tuple(rule_fd_list)):
            try:
                os.close(descriptor)
            except OSError:
                pass
        if prompt_fd >= 0:
            try:
                os.close(prompt_fd)
            except OSError:
                pass
        if auth_fd >= 0:
            try:
                os.close(auth_fd)
            except OSError:
                pass
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
    executable = os.path.realpath(sys.executable)
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
    try:
        head = resolve_head(workspace)
    except GitBoundaryError as exc:
        # Task 11 (Task 9 residual): a pinned-Git failure (timeout, missing
        # binary, broken pipe) is a clean fail-closed invocation error, never
        # a raw GitBoundaryError traceback escaping the launch CLI.
        raise InvocationError(
            f"cannot resolve the workspace HEAD through the pinned Git "
            f"boundary: {exc}"
        ) from exc
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
        # Task 8 production confinement is constructed and minted entirely
        # inside ``authorize_launch``.  No caller-provided proof/spec/home or
        # synthetic transport exists on this CLI/programmatic surface.
        # F2/F5: mint the unforgeable verified-committed authority.  The mint
        # verifies the wrapper/backend against the committed blobs (or the
        # immutable external authority), stages exact committed bytes into a
        # private mode-0700 directory as non-executable mode-0400 interpreter
        # inputs (or revalidates the external path), and binds prompt bytes to the
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
    except GitBoundaryError as exc:
        # Task 11 (Task 9 residual): a pinned Git failure during pre-flight
        # (HEAD resolution, committed-blob reads) is a clean fail-closed CLI
        # error, never a traceback.
        print(
            f"factory-launch: pinned Git boundary fail-closed: {exc}",
            file=sys.stderr,
        )
        return EXIT_INVOCATION
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
