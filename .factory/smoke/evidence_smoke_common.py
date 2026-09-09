#!/usr/bin/env python3
"""Deterministic shared helpers of the designated evidence-smoke lane (Task 22).

This module is the single authority for the deterministic bytes of the
evidence-smoke round: the canonical planner revision (the committed plan plus
exactly one fixed marker line in the ``Goal and non-goals`` section), the
developer completion revision (the current plan with the selected task
``- Status: pending`` line flipped to ``complete``), the machine-readable
developer evidence artifact (schema ``factory-smoke-evidence/v1``), and the
private seam label.  The designated seam driver
(``evidence_smoke_driver.py``), the deterministic gates
(``evidence_smoke_gate.py``), the trusted operator command
(``evidence_smoke.py``), and the hidden smoke suite all consume this module,
so the planner output is byte-bound to the exact revision this module
derives and any drift between the seam and its verifier fails closed.

The lane is *private source methodology evidence*: it drives the real
production ``campaign.py run`` control plane with a deterministic synthetic
role process that never reads a model, credential, cookie, runner, or human.
Its output is never a real model outcome, never real confinement evidence,
never installed-tier evidence, never GIT-01 acceptance evidence, and never
acceptance-tier evidence.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from pathlib import Path
from typing import Dict, List, Mapping, Optional, Tuple

SCHEMA_NAME = "factory-smoke-evidence/v1"

# The private seam label.  Every evidence-smoke campaign id carries this
# prefix; the campaign CLI refuses to run the ``--evidence-smoke`` mode with
# any other label, and every evidence artifact records it.
SEAM_LABEL_PREFIX = "evidence-smoke-"
SEAM_LABEL = (
    "private source methodology evidence; never a real model or human outcome"
)

# The designated committed seam and the single bounded tracked evidence
# artifact the developer role may create (scope-authority enforced).
DESIGNATED_DRIVER_REL = ".factory/smoke/evidence_smoke_driver.py"
DESIGNATED_EVIDENCE_REL = ".factory/artifacts/campaign-smoke-evidence.json"
GATE_REL = ".factory/smoke/evidence_smoke_gate.py"
PLAN_REL = ".factory/artifacts/implementation-plan.md"
PHASE_RESULT_REL = ".factory-state/evidence-smoke-phase-result.json"
AUDIT_RESULT_REL = ".factory-state/evidence-smoke-audit-result.json"

ROLE_NAMES = ("planner", "developer", "tester", "auditor")

# The evidence round's task: Task 22 in the canonical plan stays `pending` in
# the planner revision and is the only runnable task, so the deterministic
# selector works exactly the live evidence round.
EVIDENCE_TASK_ID = 22
# The final documentation and specification audit task must stay pending after
# the evidence round: the round proves one full phase cycle, not acceptance.
FINAL_AUDIT_TASK_ID = 25

# One fixed, deterministic marker line inserted into the canonical plan's
# ``Goal and non-goals`` section by the planner.  The revision is otherwise
# byte-identical to the committed plan (bindings, tasks, statuses, matrix and
# inventory are untouched), so ``Task 22`` stays ``pending`` for the evidence
# round and the revision remains canonical and byte-bound.
SMOKE_MARKER = (
    "- Smoke evidence round (evidence-smoke): deterministic designated "
    "harness seam; no external model, cookies, credentials, runner, or human."
)
_TASK_HEADING_RE = re.compile(r"^## Task\s+(\d+):")

SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
SHA40_RE = re.compile(r"^[0-9a-f]{40}$")
EVIDENCE_MAX = 256 * 1024
PLAN_BLOB_MAX = 4 * 1024 * 1024

# The trusted control-state file and its crash-window orphan names (the
# ``state`` authority's ``TEMP_ORPHAN_RE`` contract): an evidence-smoke round
# must never start next to an existing recovery orphan (``.factory-loop.json``
# plus 32 hex digits), because recovery would reconcile the leftover instead
# of the round's own fresh state.  The smoke preflight rejects any such
# orphan before recovery can run.
STATE_FILE_NAME = "factory-loop.json"
STATE_LEDGER_REL = ".factory-state/state-digest-ledger.jsonl"
STATE_ORPHAN_RE = re.compile(
    rf"^\.{re.escape(STATE_FILE_NAME)}\.[0-9a-f]{{32}}$"
)


class EvidenceSmokeError(Exception):
    """Base class for every fail-closed evidence-smoke failure."""


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def secure_read_bytes(path: Path, *, maximum: int, what: str) -> bytes:
    """Bounded no-follow read anchored to owner/mode/nlink/inode pre and post.

    The file must be a regular single-link current-user-owned file that is
    not group/other-writable, must not exceed ``maximum`` bytes, and the
    descriptor identity must match the pathname at both ends of the read — a
    symlink, mode, owner, link-count, inode, or size substitution fails
    closed (B2 gate bounded-read contract).
    """
    absolute = path.absolute()
    try:
        if absolute.resolve(strict=True) != absolute:
            raise EvidenceSmokeError(
                f"{what} path contains a symlink component: {path}"
            )
    except OSError as exc:
        raise EvidenceSmokeError(
            f"{what} path is unavailable: {path}: {exc}"
        ) from exc
    flags = (
        os.O_RDONLY
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )
    try:
        descriptor = os.open(absolute, flags)
    except OSError as exc:
        raise EvidenceSmokeError(f"cannot open {what} {path}: {exc}") from exc
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
            raise EvidenceSmokeError(f"unsafe {what} file: {path}")
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
            or (
                before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns
            )
            != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
            or (after.st_dev, after.st_ino)
            != (named_after.st_dev, named_after.st_ino)
        ):
            raise EvidenceSmokeError(
                f"{what} file changed while reading: {path}"
            )
        return raw
    finally:
        os.close(descriptor)


def pinned_search_path(*, interpreter: str, git_executable: str) -> str:
    """The scrubbed PATH of pinned trusted tool directories (B3).

    The caller's ``PATH`` is never inherited by a gate child; the scrubbed
    PATH is built only from the parent directory of the pinned interpreter
    (``sys.executable``), the parent directory of the pinned Git executable
    (``gitutil.GIT_EXECUTABLE``), the fixed standard system bin directories,
    and the immutable store toolchain directories of the deterministic shell
    tools the documentation gates invoke.  A malicious ``PATH`` therefore
    cannot substitute a fake ``python3``/``git``/``bash``/coreutils for any
    tool a deterministic gate resolves.
    """
    directories: List[str] = []
    for candidate in (interpreter, git_executable):
        parent = str(Path(candidate).resolve().parent)
        if parent not in directories:
            directories.append(parent)
    for candidate in (
        "/usr/bin", "/bin", "/usr/sbin", "/sbin", "/usr/local/bin",
    ):
        if candidate not in directories:
            directories.append(candidate)
    try:
        import glob  # noqa: PLC0415

        for tool in (
            "grep", "sed", "cat", "head", "tail", "dirname", "basename",
            "realpath", "sort", "printf", "env", "tr", "wc", "cut", "find",
            "mapfile", "cmp",
        ):
            for found in sorted(glob.glob(f"/nix/store/*/bin/{tool}")):
                parent = str(Path(found).resolve().parent)
                if parent not in directories:
                    directories.append(parent)
                break
    except OSError:
        pass
    return os.pathsep.join(directories)


def smoke_campaign_id(commit: str) -> str:
    """The private-label campaign id derived deterministically from the bound commit."""
    if not SHA40_RE.fullmatch(commit):
        raise EvidenceSmokeError("the bound commit must be a 40-hex Git commit hash")
    return SEAM_LABEL_PREFIX + commit[:8]


def is_smoke_campaign_id(campaign_id: str) -> bool:
    return campaign_id.startswith(SEAM_LABEL_PREFIX)


def marker_processes(campaign_id: str) -> List[int]:
    """Live non-zombie PIDs whose cmdline carries the campaign marker.

    The survivor scan only reports *actual leaves*: the process must be a
    real (non-zombie) process whose cmdline is readable and carries the
    validated unique campaign marker.  Zombie entries and the scanning
    process itself never count, and a PID whose cmdline vanished mid-scan is
    skipped — the scan reports real survivors, never artifacts.
    """
    marker = campaign_id.encode("utf-8")
    found: List[int] = []
    for pid_dir in Path("/proc").iterdir():
        if not pid_dir.name.isdigit():
            continue
        pid = int(pid_dir.name)
        try:
            stat_text = (pid_dir / "stat").read_text(
                encoding="ascii", errors="replace"
            )
        except OSError:
            continue
        end = stat_text.rfind(")")
        if end < 0:
            continue
        fields = stat_text[end + 1:].split()
        if fields and fields[0] in ("Z", "X"):
            # A zombie is not a live leaf; it is an un-reaped corpse.
            continue
        try:
            cmdline = (pid_dir / "cmdline").read_bytes()
        except OSError:
            continue
        if marker in cmdline:
            found.append(pid)
    return found


def plan_with_smoke_marker(data: bytes) -> bytes:
    """The deterministic *semantic* planner revision of the committed plan.

    The planner revision appends the fixed smoke note to the selected
    (``EVIDENCE_TASK_ID``) task's ``Scope`` field so the revision is a genuine
    semantic planning change: the trusted campaign's meaningful-substance
    boundary commits exactly this revision once (a scope change is a semantic
    task-field change), which the evidence round requires so the planner's
    output is a real committed revision rather than metadata-only prose.
    Every other byte of the plan (bindings, the task statuses - ``Task 22``
    stays pending until the developer works it - matrix, and inventory) is
    preserved verbatim, so the revision is a pure deterministic function of
    the committed plan.
    """
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise EvidenceSmokeError("the plan is not valid UTF-8") from exc
    if SMOKE_MARKER in text:
        raise EvidenceSmokeError(
            "the plan already carries the smoke marker; a repeated revision "
            "is ambiguous"
        )
    lines = text.split("\n")
    start = None
    for index, line in enumerate(lines):
        match = _TASK_HEADING_RE.match(line)
        if match and int(match.group(1)) == EVIDENCE_TASK_ID:
            start = index
            break
    if start is None:
        raise EvidenceSmokeError(
            f"the plan has no `## Task {EVIDENCE_TASK_ID}:` section"
        )
    end = next(
        (index for index in range(start + 1, len(lines))
         if lines[index].startswith("## ")),
        len(lines),
    )
    out = list(lines)
    for index in range(start, end):
        if out[index].startswith("- Scope:"):
            out[index] = out[index].rstrip() + " " + SMOKE_MARKER
            return "\n".join(out).encode("utf-8")
    raise EvidenceSmokeError(
        f"task {EVIDENCE_TASK_ID} has no `- Scope:` line to revise"
    )


def plan_with_task_complete(data: bytes, task_id: int) -> bytes:
    """The deterministic developer revision: exactly one task becomes complete.

    Only the ``- Status: pending`` line of the ``## Task <task_id>:`` block is
    flipped to ``complete``; every other byte of the plan (bindings, other
    tasks, matrix, interactions, and the smoke marker) is unchanged.
    """
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise EvidenceSmokeError("the plan is not valid UTF-8") from exc
    lines = text.split("\n")
    start = None
    for index, line in enumerate(lines):
        match = _TASK_HEADING_RE.match(line)
        if match and int(match.group(1)) == task_id:
            start = index
            break
    if start is None:
        raise EvidenceSmokeError(f"the plan has no `## Task {task_id}:` section")
    end = next(
        (index for index in range(start + 1, len(lines))
         if lines[index].startswith("## ")),
        len(lines),
    )
    out = list(lines)
    for index in range(start, end):
        if out[index] == "- Status: pending":
            out[index] = "- Status: complete"
            return "\n".join(out).encode("utf-8")
    raise EvidenceSmokeError(
        f"task {task_id} has no `- Status: pending` line to complete"
    )


def smoke_evidence_bytes(
    *,
    campaign_id: str,
    bound_commit: str,
    task_id: int,
    round_no: int,
    attempt: int,
    task_excerpt_digest: str,
    plan_digest_worked: str,
    findings_present: bool,
) -> bytes:
    """Deterministic bytes of the developer evidence artifact (no wall clock).

    The artifact is the single bounded, harness-owned tracked evidence file
    the developer role creates under ``.factory/artifacts/``; the trusted
    campaign commits it with the task completion.
    """
    payload: Dict[str, object] = {
        "schema": SCHEMA_NAME,
        "seam": "evidence-smoke",
        "label": SEAM_LABEL,
        "channel": "designated-smoke-seam",
        "campaign_id": campaign_id,
        "bound_commit": bound_commit,
        "role": "developer",
        "task_id": task_id,
        "round": round_no,
        "attempt": attempt,
        "task_excerpt_digest": task_excerpt_digest,
        "plan_digest_worked": plan_digest_worked,
        "findings_present": bool(findings_present),
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    ) + b"\n"


def validate_smoke_evidence(
    data: bytes, *, task_id: int, campaign_id: Optional[str] = None
) -> Dict[str, object]:
    """Fail closed unless ``data`` is a well-formed smoke evidence artifact."""
    if len(data) > EVIDENCE_MAX:
        raise EvidenceSmokeError("the evidence artifact exceeds the byte bound")
    try:
        payload = json.loads(data.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise EvidenceSmokeError(f"the evidence artifact is not valid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise EvidenceSmokeError("the evidence artifact must be a JSON object")
    if payload.get("schema") != SCHEMA_NAME:
        raise EvidenceSmokeError(
            f"the evidence artifact schema must be `{SCHEMA_NAME}`"
        )
    if payload.get("seam") != "evidence-smoke":
        raise EvidenceSmokeError("the evidence artifact does not name the evidence-smoke seam")
    if payload.get("label") != SEAM_LABEL:
        raise EvidenceSmokeError("the evidence artifact lost its private seam label")
    if payload.get("channel") != "designated-smoke-seam":
        raise EvidenceSmokeError(
            "the evidence artifact must name the designated smoke channel"
        )
    if payload.get("role") != "developer":
        raise EvidenceSmokeError("the evidence artifact must be developer work")
    if int(payload.get("task_id", -1)) != task_id:
        raise EvidenceSmokeError(
            f"the evidence artifact must bind task {task_id}, "
            f"got {payload.get('task_id')!r}"
        )
    for field in ("campaign_id", "bound_commit", "task_excerpt_digest",
                  "plan_digest_worked"):
        value = payload.get(field)
        if not isinstance(value, str) or not value:
            raise EvidenceSmokeError(f"the evidence artifact lacks `{field}`")
    for field in ("bound_commit",):
        if not SHA40_RE.fullmatch(str(payload.get(field))):
            raise EvidenceSmokeError("the evidence artifact binds an invalid commit")
    for field in ("task_excerpt_digest", "plan_digest_worked"):
        if not SHA256_RE.fullmatch(str(payload.get(field))):
            raise EvidenceSmokeError(
                f"the evidence artifact `{field}` must be a 64-hex SHA-256 digest"
            )
    if not isinstance(payload.get("round"), int) or payload.get("round") < 1:
        raise EvidenceSmokeError("the evidence artifact must carry a positive round")
    if not isinstance(payload.get("attempt"), int) or payload.get("attempt") < 1:
        raise EvidenceSmokeError("the evidence artifact must carry a positive attempt")
    if campaign_id is not None and payload.get("campaign_id") != campaign_id:
        raise EvidenceSmokeError(
            "the evidence artifact campaign id does not match the bound campaign"
        )
    return payload


def expected_phase_history() -> List[Tuple[int, str, str]]:
    """The exact one-round phase history the evidence smoke must produce."""
    return [
        (1, "planning", "planned"),
        (1, "implementation", "task_completed"),
        (1, "verification", "pass"),
        (1, "audit", "pass"),
    ]


ROUND_PHASES = ("planning", "implementation", "verification", "audit")


def validate_round_ledger(ledger: Mapping[str, str]) -> None:
    """Fail closed unless the ledger records exactly the four round phases.

    The one-round evidence smoke records exactly one before/after digest tag
    per untrusted phase (planning, implementation, verification, audit); a
    forged, repeated, or foreign tag changes the exact tag set and fails
    closed (exact ledger tags/history contract).
    """
    phases = set()
    for tag in ledger:
        for phase in ROUND_PHASES:
            if tag.startswith(f"r1.{phase}."):
                phases.add(phase)
    if len(ledger) != len(ROUND_PHASES) or phases != set(ROUND_PHASES):
        raise EvidenceSmokeError(
            f"the state digest ledger does not record exactly the four round "
            f"phases ({len(ledger)} entries, phases {sorted(phases)})"
        )
