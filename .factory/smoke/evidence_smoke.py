#!/usr/bin/env python3
"""Trusted evidence-smoke operator command (Task 22, ``--evidence-smoke`` lane).

Drives the *real* production campaign control plane
(``.factory/loop/campaign.py run --evidence-smoke``) against the live
repository for exactly one full planning -> implementation -> verification ->
audit round, using the designated committed smoke seam
(``evidence_smoke_driver.py``) as a deterministic synthetic role process.
No external model, credential, cookie, runner, or human is ever invoked.

Fail-closed contract:

* the run is accepted only on a clean worktree at the exact bound branch and
  commit (``--branch`` / mandatory ``--expect-commit``; the campaign CLI
  re-checks);
* the designated driver is bound to its exact committed blob at the bound
  commit, and the campaign CLI refuses any other role candidate;
* pre-existing ``.factory-state`` entries (foreign runtime bytes) are
  snapshotted before the run and re-verified byte-for-byte (digest, mode,
  mtime) afterwards — nothing foreign is deleted, quarantined, or mutated;
* before any state recovery, an existing recovery orphan, an existing state
  digest ledger, or an existing structured result/evidence artifact is
  rejected (no-replace, never reconciled);
* the campaign child runs in its own new session; a wedged campaign is
  terminated and reaped through TERM -> bounded grace -> unconditional KILL
  of the whole process group, and the marker-bearing driver/gate argv lets
  the survivor scan detect actual escaped leaves;
* the campaign must terminate ``success`` with the exact one-round phase
  history, the state/digest-ledger contracts, every state digest re-derived
  from the exact committed blobs at the phase commits, the canonical
  byte-bound planner revision (Task 22 stays ``pending`` in the planner
  output; only the developer marks it ``complete`` per spec §6.2), exactly
  two orchestrator commits (the planner revision and the developer
  task-complete/evidence commit), the single tracked evidence artifact
  committed and cross-bound to the planner's commit/plan digest, and the
  final audit task still pending (the round proves one full phase cycle,
  not acceptance);
* the evidence round is explicitly labeled *private source methodology
  evidence* (seam label ``evidence-smoke``); it is never a real model/human
  outcome, never installed-tier evidence, never GIT-01 acceptance evidence,
  and never acceptance-tier evidence.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import signal
import stat
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

HERE = Path(__file__).resolve().parent
ROOT_DEFAULT = HERE.parent.parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import evidence_smoke_common as common  # noqa: E402

SCHEMA_NAME = "factory-evidence-smoke/v1"
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
SHA40_RE = re.compile(r"^[0-9a-f]{40}$")
PLAN_BLOB_MAX = 4 * 1024 * 1024
DRIVER_MAX = 512 * 1024
STATE_SNAPSHOT_MAX_ENTRIES = 20000
STATE_SNAPSHOT_MAX_FILE = 512 * 1024 * 1024
GIT_TIMEOUT = 120.0
CAMPAIGN_TIMEOUT = 3600.0
CAMPAIGN_KILL_GRACE = 5.0
CAMPAIGN_REAP_BOUND = 10.0
CAMPAIGN_PIPE_COLLECT = 5.0
CAMPAIGN_CAPTURE_LIMIT = 2 * 1024 * 1024


class EvidenceSmokeError(Exception):
    """Every fail-closed evidence-smoke failure."""


def _fail(message: str) -> None:
    raise EvidenceSmokeError(message)


def _load_loop_modules(root: Path):
    """Import the loop authorities of ``root`` (the repo the run drives)."""
    loop = root / ".factory" / "loop"
    if str(loop) not in sys.path:
        sys.path.insert(0, str(loop))
    import gitutil  # noqa: PLC0415
    import plan_parser  # noqa: PLC0415
    import state as state_module  # noqa: PLC0415

    return gitutil, plan_parser, state_module


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _pinned_git(gitutil, root: Path, argv: Sequence[str], timeout: float = GIT_TIMEOUT):
    return gitutil.git_run(["-C", str(root), *argv], timeout=timeout)


def _git_blob(gitutil, root: Path, relpath: str, commit: str) -> bytes:
    resolved = _pinned_git(gitutil, root, ["rev-parse", f"{commit}:{relpath}"])
    if resolved.returncode != 0 or not SHA40_RE.fullmatch(resolved.stdout.strip()):
        _fail(f"path {relpath!r} is not tracked at {commit[:12]}")
    raw = gitutil.git_bytes(
        ["-C", str(root), "cat-file", "blob", resolved.stdout.strip()],
        timeout=GIT_TIMEOUT,
    )
    if raw.returncode != 0:
        _fail(f"cannot read blob {relpath!r} at {commit[:12]}")
    return raw.stdout


def _read_bounded(path: Path, label: str, maximum: int) -> bytes:
    """Anchored nofollow bounded read of a worktree file (B1/B2)."""
    return common.secure_read_bytes(path, maximum=maximum, what=label)


def _branch_of(gitutil, root: Path) -> str:
    result = _pinned_git(gitutil, root, ["rev-parse", "--abbrev-ref", "HEAD"])
    if result.returncode != 0:
        _fail("cannot resolve the current branch")
    return result.stdout.strip()


def _head(gitutil, root: Path) -> str:
    result = _pinned_git(gitutil, root, ["rev-parse", "--verify", "HEAD"])
    if result.returncode != 0 or not SHA40_RE.fullmatch(result.stdout.strip()):
        _fail("cannot resolve a 40-hex HEAD")
    return result.stdout.strip()


def _assert_clean_tree(gitutil, root: Path) -> None:
    result = _pinned_git(
        gitutil, root, ["status", "--porcelain", "-z", "--untracked-files=all"]
    )
    if result.returncode != 0 or result.stdout:
        _fail(
            "the worktree is not clean; the evidence-smoke round requires a "
            "clean tree at the exact bound commit"
        )


# ---------------------------------------------------------------------------
# Foreign-state snapshot (never touched; preserved byte-for-byte)
# ---------------------------------------------------------------------------


def snapshot_factory_state(root: Path) -> Dict[str, Dict[str, object]]:
    """Record digest/mode/mtime of every existing ``.factory-state`` entry.

    The recorded snapshot is the "before" authority: after the run the same
    entries must be byte-identical (digest, mode, mtime), proving no foreign
    ``.factory-state`` byte was deleted, quarantined, or modified.
    """
    state_dir = root / ".factory-state"
    entries: Dict[str, Dict[str, object]] = {}
    if not state_dir.is_dir():
        return entries
    for path in sorted(state_dir.rglob("*")):
        try:
            info = path.lstat()
        except OSError:
            continue
        if stat.S_ISDIR(info.st_mode):
            continue
        if not stat.S_ISREG(info.st_mode):
            _fail(f"unsafe pre-existing `.factory-state` entry {path}")
        if info.st_size > STATE_SNAPSHOT_MAX_FILE:
            _fail(f"pre-existing `.factory-state` entry {path} exceeds the bound")
        rel = str(path.relative_to(state_dir))
        entries[rel] = {
            "sha256": _sha256(path.read_bytes()),
            "mode": stat.S_IMODE(info.st_mode),
            "mtime_ns": info.st_mtime_ns,
        }
        if len(entries) > STATE_SNAPSHOT_MAX_ENTRIES:
            _fail("the `.factory-state` snapshot exceeds the entry bound")
    return entries


def verify_foreign_state(root: Path, before: Dict[str, Dict[str, object]]) -> None:
    after = snapshot_factory_state(root)
    problems: List[str] = []
    for rel, expected in before.items():
        current = after.get(rel)
        if current is None:
            problems.append(f"pre-existing entry deleted: {rel}")
        elif current != expected:
            problems.append(
                f"pre-existing entry changed (digest/mode/mtime): {rel}"
            )
    if problems:
        _fail(
            "foreign `.factory-state` bytes were not preserved byte-for-byte: "
            + "; ".join(problems)
        )


# ---------------------------------------------------------------------------
# Preflight
# ---------------------------------------------------------------------------


def _preflight(
    root: Path,
    gitutil,
    plan_parser,
    state_module,
    *,
    branch: str,
    expect_commit: str,
) -> Tuple[str, bytes]:
    """Fail closed unless the run may drive the repository at the exact bound.

    The bound exact commit is mandatory (``--expect-commit``): a live smoke
    round never runs against an unverified HEAD.  The preflight additionally
    rejects, before any state recovery can run, (1) an existing recovery
    orphan (a torn ``.factory-loop.json.<hex>`` writer leftover), (2) a
    collision with an existing structured result channel (no-replace), and
    (3) an existing state digest ledger (the round's own ledger must be
    fresh) — so a leftover or foreign lifecycle artifact can never be
    reconciled, overwritten, or appended to.
    """
    if not (root / ".factory" / "loop" / "campaign.py").is_file():
        _fail(f"{root} is not a canonical factory repository")
    head = _head(gitutil, root)
    if head != expect_commit:
        _fail(
            f"bound commit {head} does not match the expected exact commit "
            f"{expect_commit}; the smoke round fails closed on a non-exact "
            "commit"
        )
    current_branch = _branch_of(gitutil, root)
    if current_branch != branch:
        _fail(
            f"branch {current_branch!r} does not match the required branch "
            f"{branch!r}"
        )
    _assert_clean_tree(gitutil, root)
    state_dir = root / ".factory-state"
    if state_dir.is_dir():
        for path in sorted(state_dir.iterdir()):
            if common.STATE_ORPHAN_RE.fullmatch(path.name):
                _fail(
                    "an existing recovery-orphan state file must be resolved "
                    f"by the operator before the evidence round: {path}"
                )
    state_file = state_dir / state_module.STATE_FILE_NAME
    if state_file.exists():
        _fail(
            "`.factory-state/factory-loop.json` already exists; the "
            "evidence-smoke round must start from a fresh campaign state and "
            "must never overwrite existing lifecycle state"
        )
    ledger = state_dir / "state-digest-ledger.jsonl"
    if ledger.exists():
        _fail(
            "`.factory-state/state-digest-ledger.jsonl` already exists; the "
            "round's digest ledger must start fresh and must never append to "
            "foreign lifecycle bytes"
        )
    for rel, label in (
        (common.PHASE_RESULT_REL, "phase result"),
        (common.AUDIT_RESULT_REL, "audit result"),
        (common.DESIGNATED_EVIDENCE_REL, "evidence artifact"),
    ):
        collision = root / rel
        if collision.exists():
            _fail(
                f"an existing {label} collides with the evidence round's "
                f"no-replace output: {rel}"
            )
    driver_blob = _git_blob(gitutil, root, common.DESIGNATED_DRIVER_REL, head)
    driver_worktree = _read_bounded(
        root / common.DESIGNATED_DRIVER_REL, "designated smoke seam",
        DRIVER_MAX,
    )
    if driver_worktree != driver_blob:
        _fail(
            "the designated smoke seam is not the exact committed blob at "
            "the bound commit"
        )
    plan_blob = _git_blob(gitutil, root, common.PLAN_REL, head)
    try:
        plan = plan_parser.Plan.from_bytes(plan_blob)
    except plan_parser.PlanError as exc:
        _fail(f"the committed plan does not parse: {exc}")
    task = next(
        (t for t in plan.tasks if t.number == common.EVIDENCE_TASK_ID), None
    )
    if task is None or task.status != "pending":
        _fail(
            f"the evidence task {common.EVIDENCE_TASK_ID} must be `pending` "
            "for the evidence round"
        )
    return head, plan_blob


def _campaign_argv(
    root: Path,
    *,
    branch: str,
    campaign_id: str,
    rounds: int,
    task_id: int,
    bound_commit: str,
    role_timeout: float,
    gate_timeout: float,
) -> List[str]:
    gate = "./" + common.GATE_REL
    acceptance = [
        gate, "--root", str(root), "--evidence", common.DESIGNATED_EVIDENCE_REL,
        "--task", str(task_id), "--campaign-id", campaign_id, "--mode",
        "acceptance",
    ]
    verification = [
        gate, "--root", str(root), "--evidence", common.DESIGNATED_EVIDENCE_REL,
        "--task", str(task_id), "--campaign-id", campaign_id, "--mode", "verify",
    ]
    argv = [
        sys.executable, str(root / ".factory" / "loop" / "campaign.py"),
        "--root", str(root),
        "run",
        "--campaign-id", campaign_id,
        "--rounds", str(rounds),
        "--branch", branch,
        "--provider", "synthetic",
        "--model", "fixture-model",
        "--role-driver", common.DESIGNATED_DRIVER_REL,
        "--developer-evidence-path", common.DESIGNATED_EVIDENCE_REL,
        "--phase-result", common.PHASE_RESULT_REL,
        "--audit-result", common.AUDIT_RESULT_REL,
        "--role-timeout", str(role_timeout),
        "--gate-timeout", str(gate_timeout),
        "--evidence-smoke",
        "--evidence-bound-commit", bound_commit,
    ]
    for token in acceptance:
        argv += ["--acceptance-command", json.dumps(acceptance)]
        break
    for token in verification:
        argv += ["--verification-command", json.dumps(verification)]
        break
    return argv


def _pgid_has_live_members(pid: int) -> bool:
    """True while any live (non-zombie) member of process group ``pid`` remains."""
    try:
        os.killpg(pid, 0)
    except (ProcessLookupError, PermissionError):
        return False
    try:
        for pid_dir in Path("/proc").iterdir():
            if not pid_dir.name.isdigit():
                continue
            try:
                stat_fields = (pid_dir / "stat").read_text(
                    encoding="ascii", errors="replace"
                )
            except OSError:
                continue
            end = stat_fields.rfind(")")
            if end < 0:
                continue
            fields = stat_fields[end + 1:].split()
            if len(fields) < 3:
                continue
            try:
                pgrp = int(fields[2])
            except ValueError:
                continue
            if pgrp == pid and fields[0] not in ("Z", "X"):
                return True
    except OSError:
        pass
    return False


def _kill_campaign_group(process, grace: float) -> None:
    """TERM -> full bounded grace -> unconditional KILL of the whole group.

    The campaign is spawned in its own new session/process group; the group
    kill reaches every member that stayed in the group, the leader is reaped
    with a bounded wait, and a group that cannot be drained within the bounds
    fails closed instead of ever hanging the operator (F process contract).
    """
    pid = process.pid
    try:
        os.killpg(pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        pass
    deadline = time.monotonic() + grace
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        if not _pgid_has_live_members(pid):
            break
        time.sleep(min(0.02, remaining))
    try:
        os.killpg(pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass
    reap_deadline = time.monotonic() + CAMPAIGN_REAP_BOUND
    while True:
        if not _pgid_has_live_members(pid):
            break
        if time.monotonic() >= reap_deadline:
            raise EvidenceSmokeError(
                "the campaign process group did not die within the bounded "
                "KILL reap window; live group members survive"
            )
        time.sleep(0.02)
    if process.poll() is None:
        try:
            process.wait(timeout=CAMPAIGN_REAP_BOUND)
        except subprocess.TimeoutExpired as exc:
            raise EvidenceSmokeError(
                "the campaign group leader was not reaped within the bound"
            ) from exc


def _capture_stream_bounded(stream, limit: int, sink: Dict[str, object]) -> None:
    """Drain one child stream to a bounded tail (bytes).

    The sink records ``data`` (the retained tail within the cap), ``total``
    (every byte the child wrote to the pipe), and ``overcap`` (True once the
    stream exceeded the cap *while the child still ran*) — a flooding child
    is detected during the run, never only at EOF, so the supervisor can
    terminate its group promptly instead of waiting out the deadline.
    """
    chunks: List[bytes] = []
    total = 0
    overcap = False
    while True:
        try:
            chunk = stream.read(65536)
        except (OSError, ValueError):
            break
        if not chunk:
            break
        if isinstance(chunk, str):
            chunk = chunk.encode("utf-8", errors="replace")
        total += len(chunk)
        if total > limit:
            if not overcap:
                # Publish the overcap *while the child still runs* so the
                # supervisor can terminate the group promptly instead of
                # waiting out the deadline; the sink is read by the main
                # loop, never only after EOF.
                overcap = True
                sink["overcap"] = True
            chunks = chunks[-1:]
        else:
            chunks.append(chunk)
    sink["data"] = b"".join(chunks)[-limit:]
    sink["total"] = total
    sink["overcap"] = overcap


def _run_campaign(
    argv: List[str], root: Path, campaign_id: str
) -> Tuple[int, Dict[str, object]]:
    """Spawn the campaign in a new session and supervise it to a bounded end.

    The campaign child starts in its own new process session/group; on
    timeout the *whole* group is terminated (TERM, full bounded grace,
    unconditional KILL) and reaped, so a wedged campaign can never outlive
    the operator.  stdout/stderr are drained by bounded readers while the run
    proceeds.  Escaped descendants are not part of the campaign group (every
    role/git child starts its own session); they are detected by the
    marker-aware survivor scan after the run (F process contract).
    """
    try:
        process = subprocess.Popen(
            argv,
            cwd=str(root),
            env=os.environ,
            start_new_session=True,
            close_fds=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    except OSError as exc:
        _fail(f"cannot spawn the evidence-smoke campaign: {exc}")
    sinks: Dict[str, Dict[str, bytes]] = {
        "stdout": {"data": b""},
        "stderr": {"data": b""},
    }
    readers = []
    for name, stream in (("stdout", process.stdout), ("stderr", process.stderr)):
        if stream is None:
            continue
        thread = threading.Thread(
            target=_capture_stream_bounded,
            args=(stream, CAMPAIGN_CAPTURE_LIMIT, sinks[name]),
            name=f"evidence-smoke-{name}",
            daemon=True,
        )
        thread.start()
        readers.append(thread)
    timed_out = False
    deadline = time.monotonic() + CAMPAIGN_TIMEOUT
    while True:
        if process.poll() is not None and all(
            not reader.is_alive() for reader in readers
        ):
            break
        if time.monotonic() >= deadline:
            timed_out = True
            break
        time.sleep(0.02)
    if timed_out:
        _kill_campaign_group(process, CAMPAIGN_KILL_GRACE)
    for reader in readers:
        reader.join(timeout=CAMPAIGN_PIPE_COLLECT)
    for stream in (process.stdout, process.stderr):
        if stream is not None:
            try:
                stream.close()
            except OSError:
                pass
    if timed_out:
        _fail(
            "the campaign subprocess exceeded its bound; the whole process "
            "group was terminated and reaped"
        )
    stdout = sinks["stdout"]["data"].decode("utf-8", errors="replace")
    stderr = sinks["stderr"]["data"].decode("utf-8", errors="replace")
    data: Optional[Dict[str, object]] = None
    try:
        data = json.loads(stdout)
    except ValueError:
        pass
    if data is None:
        detail = (stderr or stdout or "").strip()
        _fail(
            f"the evidence-smoke campaign produced no machine-readable result "
            f"(rc={process.returncode}): {detail[-2000:]}"
        )
    return process.returncode, data


# ---------------------------------------------------------------------------
# Post-verification (honest outcome; foreign state preserved)
# ---------------------------------------------------------------------------


def _assert_phase_history(data: Dict[str, object]) -> None:
    history = data.get("phase_history")
    if not isinstance(history, list) or not history:
        _fail("the campaign result has no phase history")
    expected = common.expected_phase_history()
    got = [
        (int(record["round"]), str(record["phase"]), str(record["outcome"]))
        for record in history
    ]
    if got != expected:
        _fail(
            f"the campaign phase history {got} does not equal the expected "
            f"single evidence round {expected}"
        )
    for record in history:
        if not SHA40_RE.fullmatch(str(record.get("head_commit"))):
            _fail("a phase record carries an invalid head_commit")
        if not SHA256_RE.fullmatch(str(record.get("plan_digest"))):
            _fail("a phase record carries an invalid plan_digest")
        if record.get("phase") in ("verification", "audit"):
            if not SHA256_RE.fullmatch(str(record.get("result_digest"))):
                _fail(
                    "a verification/audit phase record must bind the exact "
                    "result digest"
                )


def _assert_state(
    root: Path, gitutil, plan_parser, state_module, *, branch, campaign_id,
    rounds, head, planning_head: str, spec_path: str,
):
    state = state_module.load_state(
        root,
        expected_branch=branch,
        expected_campaign_id=campaign_id,
        expected_rounds_requested=rounds,
    )
    if state.current_phase != "success" or state.last_outcome != "success":
        _fail(
            "the terminal state is not `success`: "
            f"{state.current_phase}/{state.last_outcome}"
        )
    state_path = root / ".factory-state" / state_module.STATE_FILE_NAME
    info = state_path.stat()
    if stat.S_IMODE(info.st_mode) & 0o777 != 0o600 or info.st_uid != os.geteuid():
        _fail("the state file is not private mode-0600 and current-owner")
    # ``plan_digest`` is bound on the planning -> implementation edge to the
    # planner's committed revision (the canonical marker revision), not to
    # the final task-completed head.
    planner_blob = _git_blob(gitutil, root, common.PLAN_REL, planning_head)
    if state.plan_digest != _sha256(planner_blob):
        _fail(
            "the state plan_digest does not match the planner's committed "
            "revision"
        )
    if state.phase_base_commit != planning_head:
        _fail(
            "the state phase base commit does not match the planner's "
            "committed revision"
        )
    # State digest re-derivation (State finding): every digest in the
    # terminal control state must be re-derived from the exact committed
    # blobs at the bound phase commits — the specification, the audit
    # objective registry, and the four role prompts are authoritative from
    # Git, never from operator claims or from the mutable worktree.
    spec_data = _git_blob(gitutil, root, spec_path, head)
    if state.specification_digest != _sha256(spec_data):
        _fail(
            "the state specification_digest does not match the exact "
            "committed spec blob at the final head"
        )
    audit_data = _git_blob(
        gitutil, root, ".factory/audit-objectives/registry.json", head
    )
    if state.audit_objectives_digest != _sha256(audit_data):
        _fail(
            "the state audit_objectives_digest does not match the exact "
            "committed audit-objective registry blob"
        )
    for role in common.ROLE_NAMES:
        prompt_data = _git_blob(
            gitutil, root, f".factory/prompts/{role}.md", head
        )
        expected = _sha256(prompt_data)
        recorded = state.role_prompt_digests.get(role)
        if recorded != expected:
            _fail(
                f"the state role_prompt_digest for {role!r} does not match "
                "the exact committed prompt blob"
            )
    # Exact ledger contract: every untrusted phase of the round is
    # digest-ledgered with a well-formed tag/digest pair, and the tags
    # cover exactly the four phases of the round (planning, implementation,
    # verification, audit) plus their retries.
    ledger = state_module.read_phase_digest_ledger(root)
    common.validate_round_ledger(ledger)
    for tag, digest in ledger.items():
        if not SHA256_RE.fullmatch(str(digest)):
            _fail(f"the state digest ledger records a malformed digest for {tag}")
    return state


def _assert_plan_outcomes(
    root: Path, gitutil, plan_parser, *, head: str, original_plan: bytes,
    task_id: int,
) -> None:
    committed = _git_blob(gitutil, root, common.PLAN_REL, head)
    plan = plan_parser.Plan.from_bytes(committed)
    task = next((t for t in plan.tasks if t.number == task_id), None)
    if task is None or task.status != "complete":
        _fail(f"the committed plan must record task {task_id} complete")
    final = next(
        (t for t in plan.tasks if t.number == common.FINAL_AUDIT_TASK_ID), None
    )
    if final is None or final.status == "complete":
        _fail("the final audit task must stay pending after the evidence round")
    original = plan_parser.Plan.from_bytes(original_plan)
    if (
        original.spec_path != plan.spec_path
        or original.spec_commit != plan.spec_commit
        or original.spec_blob != plan.spec_blob
        or original.base_commit != plan.base_commit
    ):
        _fail("the evidence round changed a canonical plan binding")


def _planner_revision_check(
    gitutil, root: Path, *, planning_head: str, original_plan: bytes,
) -> None:
    planner_blob = _git_blob(gitutil, root, common.PLAN_REL, planning_head)
    expected = common.plan_with_smoke_marker(original_plan)
    if planner_blob != expected:
        _fail(
            "the planner's committed revision is not the canonical byte-bound "
            "revision of the committed plan"
        )


def _assert_evidence_committed(
    gitutil, root: Path, *, head: str, campaign_id: str, task_id: int,
    planning_head: str, planning_plan_digest: str,
) -> None:
    listing = _pinned_git(
        gitutil, root, ["ls-files", "--", common.DESIGNATED_EVIDENCE_REL]
    )
    if listing.returncode != 0 or not listing.stdout.strip():
        _fail("the evidence artifact is not tracked in the repository")
    data = _git_blob(gitutil, root, common.DESIGNATED_EVIDENCE_REL, head)
    payload = common.validate_smoke_evidence(
        data, task_id=task_id, campaign_id=campaign_id
    )
    # Evidence cross-binding (State finding): the developer evidence must
    # bind the exact commit and the exact plan bytes the developer worked
    # from — the planner's committed revision — so a substituted or
    # paraphrased task/plan can never satisfy the evidence artifact.
    if str(payload["bound_commit"]) != planning_head:
        _fail(
            "the evidence artifact bound_commit does not match the planner's "
            "committed phase head"
        )
    if str(payload["plan_digest_worked"]) != planning_plan_digest:
        _fail(
            "the evidence artifact plan_digest_worked does not match the "
            "exact committed plan bytes of the developer phase"
        )


def _marker_processes(campaign_id: str) -> List[int]:
    """Live non-zombie PIDs whose cmdline carries the campaign marker.

    The survivor scan only reports *actual leaves*: the process must be a
    real (non-zombie) process whose cmdline is readable and carries the
    validated unique campaign marker.  Zombie entries and the scanning
    process itself never count, and a PID whose cmdline vanished mid-scan is
    skipped — the scan reports real survivors, never artifacts.
    """
    return common.marker_processes(campaign_id)


def _assert_no_survivors(campaign_id: str, baseline: List[int]) -> None:
    """Fail closed when a *new* marker-bearing process survives the round.

    Processes that already carried the marker before the campaign spawned
    (a foreign orphan collision) are deliberately untouched and excluded;
    only processes that appeared during the run are campaign leaves and fail
    the round closed.
    """
    baseline_set = set(baseline)
    for pid in _marker_processes(campaign_id):
        if pid in baseline_set:
            continue
        _fail(
            f"a process survives the evidence round: pid {pid}; the marker "
            "carries the validated unique campaign id"
        )


def _docs_gate_env(gitutil, *, root: Path) -> Dict[str, str]:
    """The sanitized allowlist environment the post-round docs gates receive.

    The caller's full environment is never inherited (B3): only the
    documented benign keys are copied, ``PATH`` is replaced by the scrubbed
    pinned-tool path (the pinned interpreter/git parents plus the standard
    system bin directories — a malicious ``PATH`` can never substitute a
    fake tool), every lock metadata key and Git redirector is stripped, and
    any surviving credential-shaped key fails closed.
    """
    allowlist = (
        "HOME", "USER", "LOGNAME", "LANG", "LC_ALL", "LC_CTYPE",
        "LC_COLLATE", "LC_MESSAGES", "LC_MONETARY", "LC_NUMERIC", "LC_TIME",
        "TERM", "TZ", "SHELL", "XDG_RUNTIME_DIR", "XDG_CONFIG_HOME",
        "XDG_CACHE_HOME", "XDG_DATA_HOME", "NO_COLOR", "CLICOLOR",
        "CLICOLOR_FORCE",
    )
    environment: Dict[str, str] = {}
    for key in allowlist:
        if key in os.environ:
            environment[key] = os.environ[key]
    environment["PATH"] = common.pinned_search_path(
        interpreter=sys.executable, git_executable=gitutil.GIT_EXECUTABLE
    )
    environment["FACTORY_VERIFIER_ROOT"] = str(root)
    # Bytecode writes are disabled for the whole gate child, so the pinned
    # Git bootstrap and every subprocess can never create a
    # ``__pycache__``/``*.pyc`` artifact in the repository.
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    for key in tuple(environment):
        if key.startswith("FACTORY_LOOP_LOCK_") or key.startswith("FACTORY_LOCK_"):
            environment.pop(key, None)
    environment = gitutil.sanitize_git_environment(environment)
    for key in environment:
        upper = key.upper()
        if any(
            token in upper
            for token in (
                "COOKIE", "TOKEN", "PASSWORD", "PASSWD", "API_KEY",
                "SECRET", "CREDENTIAL", "PRIVATE_KEY", "AUTH",
            )
        ):
            _fail(
                f"refusing to spawn a documentation gate with a "
                f"credential-shaped environment key {key!r}"
            )
    return environment


def _bind_docs_gate(
    root: Path, gitutil, relpath: str, commit: str
) -> int:
    """Bind one committed docs-gate script and return its retained descriptor.

    The worktree script must be a regular single-link current-user-owned
    executable whose bytes equal the exact committed blob at ``commit``; a
    retained ``O_NOFOLLOW`` descriptor pins the bound inode so a pathname or
    content substitution between bind and exec fails closed, and the child
    executes the pinned interpreter with the descriptor path
    ``/proc/self/fd/<fd>`` through ``pass_fds`` — committed blob/descriptor
    execution with no ``PATH``-resolved shebang (B3).
    """
    path = root / relpath
    raw = common.secure_read_bytes(path, maximum=PLAN_BLOB_MAX, what=relpath)
    if not raw.startswith(b"#!"):
        raise EvidenceSmokeError(
            f"documentation gate {relpath} is not an interpreter script"
        )
    info = path.lstat()
    if not info.st_mode & 0o111:
        raise EvidenceSmokeError(
            f"documentation gate {relpath} is not executable"
        )
    committed = _git_blob(gitutil, root, relpath, commit)
    if raw != committed:
        raise EvidenceSmokeError(
            f"documentation gate {relpath} is not the exact committed blob at "
            f"the bound commit"
        )
    flags = (
        os.O_RDONLY
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )
    try:
        descriptor = os.open(path.absolute(), flags)
    except OSError as exc:
        raise EvidenceSmokeError(
            f"cannot retain the documentation-gate descriptor {relpath}: {exc}"
        ) from exc
    try:
        held = os.fstat(descriptor)
        named = path.lstat()
        if (
            not stat.S_ISREG(held.st_mode)
            or held.st_uid != os.getuid()
            or held.st_nlink != 1
            or held.st_mode & 0o022
            or (held.st_dev, held.st_ino) != (named.st_dev, named.st_ino)
        ):
            raise EvidenceSmokeError(
                f"unsafe documentation-gate descriptor {relpath}"
            )
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


def _run_documentation_gates(
    root: Path, gitutil, *, commit: str
) -> None:
    """Run the Task 22 documented verification gates after the round.

    ``.factory/tools/check-plan-freshness.sh`` and ``.factory/tools/check-generic-leakage.sh``
    are the acceptance-criteria gates of the live evidence round; when they
    exist in the repository the evidence smoke runs them and fails closed on
    any nonzero exit.  Every gate child receives the sanitized allowlist
    environment with the scrubbed pinned ``PATH``, runs the pinned bash
    interpreter, and executes the committed blob through a retained
    descriptor (``/proc/self/fd/<fd>`` via ``pass_fds``) — a pathname or
    content substitution can never substitute the executed bytes and a
    malicious ``PATH``/environment can never reach a gate child (B3).
    """
    bash = _pinned_bash()
    for script in ("check-plan-freshness.sh", "check-generic-leakage.sh"):
        relpath = ".factory/tools/" + script
        path = root / relpath
        if not path.is_file():
            continue
        descriptor = _bind_docs_gate(root, gitutil, relpath, commit)
        try:
            result = _run_pinned_gate_script(
                bash, descriptor, [],
                env=_docs_gate_env(gitutil, root=root),
                root=root, timeout=300.0,
            )
        finally:
            try:
                os.close(descriptor)
            except OSError:
                pass
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "").strip()
            _fail(
                f"{script} failed after the evidence round: {detail[-1200:]}"
            )


def _run_pinned_gate(
    argv: Sequence[str], *, env: Dict[str, str], root: Path, timeout: float,
    pass_fds: Sequence[int] = (),
    capture_limit: int = CAMPAIGN_CAPTURE_LIMIT,
) -> subprocess.CompletedProcess[str]:
    """Run one pinned gate child in a new session with bounded group kill.

    The child starts in its own new process session/group.  stdout/stderr
    are drained by bounded reader threads *while the run proceeds* — fair
    (neither stream can block the other) and capped, so a flooding gate can
    never grow the operator's memory without bound.  A gate that exceeds
    the capture cap (overcap) or the deadline is terminated and reaped
    through the same TERM -> full bounded grace -> unconditional KILL
    contract as the campaign itself, and the trailing pipe collection after
    the group termination is itself bounded.
    """
    try:
        process = subprocess.Popen(
            argv, cwd=str(root), env=env, start_new_session=True,
            close_fds=True, pass_fds=tuple(f for f in pass_fds if f > 2),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
    except OSError as exc:
        _fail(f"cannot spawn the documentation gate: {exc}")
    sinks: Dict[str, Dict[str, object]] = {
        "stdout": {"data": b"", "total": 0, "overcap": False},
        "stderr": {"data": b"", "total": 0, "overcap": False},
    }
    readers = []
    for name, stream in (("stdout", process.stdout), ("stderr", process.stderr)):
        if stream is None:
            continue
        thread = threading.Thread(
            target=_capture_stream_bounded,
            args=(stream, capture_limit, sinks[name]),
            name=f"evidence-smoke-gate-{name}",
            daemon=True,
        )
        thread.start()
        readers.append(thread)
    reason: Optional[str] = None
    deadline = time.monotonic() + timeout
    while True:
        if any(bool(sink["overcap"]) for sink in sinks.values()):
            reason = "output exceeded the bounded capture cap " \
                f"({capture_limit} bytes)"
            break
        if process.poll() is not None and all(
            not reader.is_alive() for reader in readers
        ):
            break
        if time.monotonic() >= deadline:
            reason = "exceeded its bound"
            break
        time.sleep(0.02)
    if reason is not None:
        _kill_campaign_group(process, CAMPAIGN_KILL_GRACE)
    # Bounded trailing pipe collection: a group member that ignored the
    # group termination may have held the pipe write ends open, so the
    # reader joins are themselves bounded and the pipe ends are force-closed
    # afterwards — a bounded gate run can never hang the operator on a pipe
    # held by a survivor.
    for reader in readers:
        reader.join(timeout=CAMPAIGN_PIPE_COLLECT)
    for stream in (process.stdout, process.stderr):
        if stream is not None:
            try:
                stream.close()
            except OSError:
                pass
    for reader in readers:
        reader.join(timeout=CAMPAIGN_PIPE_COLLECT)
    if reason is not None:
        _fail(
            f"documentation gate {reason}; the whole process group was "
            "terminated and reaped"
        )
    return subprocess.CompletedProcess(
        argv, process.returncode,
        sinks["stdout"]["data"].decode("utf-8", errors="replace"),
        sinks["stderr"]["data"].decode("utf-8", errors="replace"),
    )


def _run_pinned_gate_script(
    interpreter: str, descriptor: int, argv_tail: Sequence[str], *,
    env: Dict[str, str], root: Path, timeout: float,
) -> subprocess.CompletedProcess[str]:
    """Execute a committed descriptor through a pinned interpreter."""
    argv = [interpreter, f"/proc/self/fd/{descriptor}", *argv_tail]
    return _run_pinned_gate(
        argv, env=env, root=root, timeout=timeout, pass_fds=(descriptor,)
    )


def _pinned_bash() -> str:
    """An absolute pinned bash interpreter (never a ``PATH``-resolved name).

    Candidates are an explicit ``FACTORY_SMOKE_BASH`` pin, the fixed FHS
    locations, the NixOS system profile, and the immutable root-owned Nix
    store — each validated as an absolute regular executable whose path the
    caller cannot substitute (owner differs from the caller's uid, no
    group/other write bits).  A malicious ``PATH`` can never select the
    interpreter.
    """
    candidates: List[str] = [
        os.environ.get("FACTORY_SMOKE_BASH", ""),
        "/bin/bash", "/usr/bin/bash", "/run/current-system/sw/bin/bash",
    ]
    try:
        import glob  # noqa: PLC0415

        candidates.extend(sorted(glob.glob("/nix/store/*/bin/bash")))
    except OSError:
        pass
    for candidate in candidates:
        if not candidate or not candidate.startswith("/"):
            continue
        path = Path(candidate)
        try:
            info = path.lstat()
        except OSError:
            continue
        if (
            stat.S_ISREG(info.st_mode)
            and info.st_mode & 0o111
            and not info.st_mode & 0o022
        ):
            if info.st_uid != os.getuid():
                return candidate
            # A caller-owned pinned bash (the explicit pin) is acceptable only
            # when the parent directory is not group/other-writable.
            parent = path.parent
            try:
                parent_info = parent.lstat()
            except OSError:
                continue
            if parent_info.st_mode & 0o022 == 0:
                return candidate
    raise EvidenceSmokeError(
        "no pinned bash interpreter is available (FACTORY_SMOKE_BASH may "
        "pin one)"
    )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="factory-evidence-smoke",
        description=(
            "Trusted evidence-smoke operator command (Task 22). Drives the "
            "real production campaign for exactly one planning/implementation/"
            "verification/audit round with the designated deterministic smoke "
            "seam; fails closed on a dirty tree, wrong branch/commit, unbound "
            "driver, foreign-state mutation, or a non-success round."
        ),
    )
    parser.add_argument("--root", default=str(ROOT_DEFAULT))
    sub = parser.add_subparsers(dest="command", required=True)
    p_run = sub.add_parser("run", help="run the evidence-smoke round")
    p_run.add_argument("--branch", required=True)
    p_run.add_argument(
        "--expect-commit",
        required=True,
        metavar="SHA40",
        help=(
            "the exact bound commit the smoke round must observe; the live "
            "round is mandatory-exact and never runs against an unverified "
            "HEAD"
        ),
    )
    p_run.add_argument("--campaign-id", default="")
    p_run.add_argument("--rounds", type=int, default=1)
    p_run.add_argument("--task", type=int, default=common.EVIDENCE_TASK_ID)
    p_run.add_argument("--role-timeout", type=float, default=900.0)
    p_run.add_argument("--gate-timeout", type=float, default=1800.0)
    args = parser.parse_args(argv)

    root = Path(args.root).absolute()
    if args.command != "run":
        _fail("only the `run` command exists")
    if args.rounds != 1:
        _fail("the evidence round is exactly one full phase cycle (`--rounds 1`)")
    if args.task != common.EVIDENCE_TASK_ID:
        _fail(
            f"the evidence round works the designated task "
            f"{common.EVIDENCE_TASK_ID}"
        )
    gitutil, plan_parser, state_module = _load_loop_modules(root)
    head = _head(gitutil, root)
    if not SHA40_RE.fullmatch(args.expect_commit):
        _fail("`--expect-commit` must be a 40-hex Git commit hash")
    if head != args.expect_commit:
        _fail(
            f"HEAD {head} does not match the mandatory exact commit "
            f"{args.expect_commit}"
        )
    campaign_id = args.campaign_id or common.smoke_campaign_id(head)
    if not common.is_smoke_campaign_id(campaign_id):
        _fail(
            f"campaign id {campaign_id!r} must carry the private seam label"
        )
    original_plan = _git_blob(gitutil, root, common.PLAN_REL, head)
    _preflight(
        root, gitutil, plan_parser, state_module,
        branch=args.branch, expect_commit=args.expect_commit,
    )
    before = snapshot_factory_state(root)
    baseline = _marker_processes(campaign_id)
    argv = _campaign_argv(
        root, branch=args.branch, campaign_id=campaign_id, rounds=args.rounds,
        task_id=args.task, bound_commit=head,
        role_timeout=args.role_timeout, gate_timeout=args.gate_timeout,
    )
    rc, data = _run_campaign(argv, root, campaign_id)
    if str(data.get("terminal_phase")) != "success" or rc != 0:
        _fail(
            f"the evidence round terminated {data.get('terminal_phase')} "
            f"(rc={rc}); the honest round outcome is not success"
        )
    _assert_phase_history(data)
    planning_head = str(data["phase_history"][0]["head_commit"])
    spec_path = plan_parser.Plan.from_bytes(original_plan).spec_path
    state = _assert_state(
        root, gitutil, plan_parser, state_module, branch=args.branch,
        campaign_id=campaign_id, rounds=args.rounds,
        head=str(data["head_commit"]), planning_head=planning_head,
        spec_path=spec_path,
    )
    planning_plan_blob = _git_blob(
        gitutil, root, common.PLAN_REL, planning_head
    )
    _assert_evidence_committed(
        gitutil, root, head=str(data["head_commit"]),
        campaign_id=campaign_id, task_id=args.task,
        planning_head=planning_head,
        planning_plan_digest=_sha256(planning_plan_blob),
    )
    _assert_plan_outcomes(
        root, gitutil, plan_parser, head=str(data["head_commit"]),
        original_plan=original_plan, task_id=args.task,
    )
    _planner_revision_check(
        gitutil, root, planning_head=planning_head, original_plan=original_plan,
    )
    verify_foreign_state(root, before)
    _assert_no_survivors(campaign_id, baseline)
    _assert_clean_tree(gitutil, root)
    _run_documentation_gates(
        root, gitutil, commit=str(data["head_commit"]),
    )
    summary = {
        "schema": SCHEMA_NAME,
        "ok": True,
        "campaign_id": campaign_id,
        "bound_commit": head,
        "terminal_phase": str(data["terminal_phase"]),
        "rounds_completed": data.get("rounds_completed"),
        "head_commit": str(data["head_commit"]),
        "phase_history": data.get("phase_history"),
        "plan_digest": _sha256(
            _git_blob(gitutil, root, common.PLAN_REL, str(data["head_commit"]))
        ),
        "state_digest": state_module.state_digest(state),
        "evidence_artifact": common.DESIGNATED_EVIDENCE_REL,
        "label": common.SEAM_LABEL,
    }
    print(json.dumps(summary, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except EvidenceSmokeError as exc:
        print(f"factory-evidence-smoke: {exc}", file=sys.stderr)
        sys.exit(1)
