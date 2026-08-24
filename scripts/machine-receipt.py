#!/usr/bin/env python3
"""Record a machine audit receipt for one coordinator-executed command.

An audit coordinator may only certify runtime behavior by executing a command
through this wrapper (or by referencing an accepted runner manifest). The
receipt binds the exact argv, the command's exit code, and SHA-256 digests of
the bounded stdout/stderr transcript under `.factory-state/audit-receipts/`.
`scripts/check-audit-receipts.py` requires a matching receipt (exit 0 for PASS)
for every executable-evidence line in the campaign audit report; subagent prose
cannot certify runtime.

Authorization (receipt minting is coordinator-bounded):
- a bare model call with no campaign binding fails;
- inside a campaign audit the coordinator exports the protected launch
  binding: `FACTORY_CAMPAIGN_AUDIT_ROUND`, `FACTORY_CAMPAIGN_AUDIT_BASE`, and
  `FACTORY_CAMPAIGN_AUDIT_NONCE`. The nonce is minted into the protected
  `.factory-state/audit-coordinator.json` state by the audit coordinator
  (`scripts/initialize-campaign-audit.py`) and never by the model;
- the receipt records `evidence_commit` equal to the campaign audit base
  (strict 40-hex) plus the coordinator round/nonce binding, so stale or
  cross-round receipts are rejected by the campaign-audit gate.

Usage:
  scripts/machine-receipt.py --tag <tag> -- <argv...>
  (campaign bound: FACTORY_CAMPAIGN_AUDIT_ROUND/BASE/NONCE must be exported)
  tests may pass --audit-round/--evidence-commit/--nonce explicitly together
  with a matching `.factory-state/audit-coordinator.json` fixture.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import resource
import stat
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TAG = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
SHA1 = re.compile(r"^[0-9a-f]{40}$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
MAX_LOG = 4 * 1024 * 1024
COORDINATOR_FILE = ".factory-state/audit-coordinator.json"
# The wrapper runs against a target repository it must never mutate: bytecode
# caching is disabled so importing the trusted loop authority (``lock.py``
# and its imports) can never create a ``__pycache__``/``*.pyc`` artifact in
# the target tree (a clean-tree checker would otherwise fail on the untracked
# bytecode files).
sys.dont_write_bytecode = True


def fail(message: str) -> None:
    raise SystemExit(f"machine-receipt: {message}")


def receipts_dir(root: Path) -> Path:
    runtime = root / ".factory-state"
    if runtime.is_symlink() or not runtime.exists():
        fail("machine-receipt requires a real .factory-state directory")
    if not runtime.is_dir():
        fail("unsafe .factory-state path")
    runtime.chmod(0o700)
    receipts = runtime / "audit-receipts"
    if receipts.is_symlink() or (receipts.exists() and not receipts.is_dir()):
        fail("unsafe audit-receipts path")
    receipts.mkdir(mode=0o700, exist_ok=True)
    return receipts


def atomic_write(path: Path, data: bytes) -> None:
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def verify_artifact(path: Path, what: str) -> None:
    """Harden one published receipt artifact (owner/mode/link-count/inode).

    The adjacent stdout/stderr transcripts and the receipt JSON must be
    regular single-link current-user-owned files that are not
    group/other-writable; a symlink, hardlink alias, foreign owner, or
    wrong mode fails closed so a substituted artifact can never certify
    runtime.
    """
    if path.is_symlink() or not path.is_file():
        fail(f"{what} is not a regular file: {path}")
    try:
        info = path.stat()
    except OSError as exc:
        fail(f"cannot stat {what}: {path}: {type(exc).__name__}")
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.getuid()
        or info.st_nlink != 1
        or info.st_mode & 0o022
    ):
        fail(f"{what} is unsafe (owner/mode/link-count/inode): {path}")


def atomic_write_noreplace(path: Path, data: bytes) -> None:
    """Publish one receipt artifact with atomic no-replace semantics.

    Task 12 §19: same-tag coordinator receipt publication must fail closed
    rather than silently replace an existing receipt (Task 10 residual): an
    existing canonical artifact, or a raced pathname, can never be
    overwritten.  Publication uses ``linkat``-style ``os.link`` (unlike
    ``rename`` it cannot clobber), then the published inode is re-validated
    and the temporary unlinked.
    """
    if path.is_symlink() or path.exists():
        fail(f"receipt artifact already exists; same-tag publication fails "
             f"closed (no-replace): {path.name}")
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary_path = Path(temporary)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o600)
        try:
            os.link(temporary, str(path))
        except FileExistsError:
            fail(f"receipt artifact raced during publication: {path.name}")
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        verify_artifact(path, f"receipt artifact {path.name}")
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def regular_json(path: Path, maximum: int) -> dict:
    if path.is_symlink() or not path.is_file():
        fail(f"unsafe or missing coordinator state: {path}")
    if path.stat().st_size > maximum:
        fail(f"coordinator state exceeds the size limit: {path}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        fail(f"invalid coordinator state {path}: {exc}")
    if not isinstance(data, dict):
        fail(f"coordinator state must be an object: {path}")
    return data


def coordinator_binding(root: Path, round_number: int | None, evidence_commit: str | None,
                        nonce: str | None) -> tuple[int, str, str]:
    """Validate the audit coordinator binding; every receipt is round-bound.

    LOW5 (trusted coordinator-only mint path): minting is authorized **only**
    by the protected ``.factory-state/audit-coordinator.json`` state that the
    trusted audit coordinator mints (``scripts/initialize-campaign-audit.py``),
    never by environment variables alone.  The resolved round/base/nonce
    (from the env or explicit test binding) must match that protected state
    exactly; an auditor that inherits no coordinator binding, or that sets
    env keys against a missing/deleted state, can never mint a receipt.
    """
    env_round = os.environ.get("FACTORY_CAMPAIGN_AUDIT_ROUND", "")
    env_base = os.environ.get("FACTORY_CAMPAIGN_AUDIT_BASE", "")
    env_nonce = os.environ.get("FACTORY_CAMPAIGN_AUDIT_NONCE", "")
    if round_number is None:
        round_number = int(env_round) if env_round.isdigit() else None
    if evidence_commit is None:
        evidence_commit = env_base or None
    if nonce is None:
        nonce = env_nonce or None
    if (
        round_number is None or round_number < 1
        or not isinstance(evidence_commit, str) or not SHA1.fullmatch(evidence_commit)
        or not isinstance(nonce, str) or not SHA256.fullmatch(nonce)
    ):
        fail(
            "receipt minting is bound to the audit coordinator: "
            "FACTORY_CAMPAIGN_AUDIT_ROUND/BASE/NONCE (or explicit test binding) are required; "
            "a bare model receipt call is not authorized"
        )
    state = root / COORDINATOR_FILE
    # The protected coordinator state is mandatory: an environment-only
    # binding (no state, or a state the caller could delete/replace) never
    # authorizes a receipt.
    if state.is_symlink() or not state.is_file():
        fail(
            "audit coordinator state is missing or unsafe; machine receipts "
            "are minted only inside the coordinator's protected invocation"
        )
    info = state.stat()
    if info.st_uid != os.getuid() or info.st_nlink != 1 or info.st_mode & 0o022:
        fail("audit coordinator state is unsafe (owner/mode/link-count)")
    data = regular_json(state, 16384)
    expected = {"schema", "round", "base_commit", "nonce", "created_at"}
    if (
        set(data) != expected
        or data.get("schema") != "ralph-audit-coordinator/v1"
        or type(data.get("round")) is not int
        or data["round"] < 1
        or not isinstance(data.get("base_commit"), str)
        or not SHA1.fullmatch(data["base_commit"])
        or not isinstance(data.get("nonce"), str)
        or not SHA256.fullmatch(data["nonce"])
        or not isinstance(data.get("created_at"), int)
    ):
        fail("audit coordinator state is invalid")
    if data["round"] != round_number or data["base_commit"] != evidence_commit or data["nonce"] != nonce:
        fail("supplied audit binding does not match the protected coordinator state")
    return round_number, evidence_commit, nonce


def _load_lock_supervision(root: Path):
    """Load the trusted root-descriptor lock/supervision authority.

    The wrapper authority reuses the hidden control plane's supervised
    process boundary (``.factory/loop/lock.py``) for the descendant capture,
    escaped-descendant detection, and the live group-gone probe — the same
    trusted launch/lock primitives the campaign and launch supervision use.
    The authority is resolved from the wrapper's own installed/source tree
    first (the installed copy always carries the full hidden surface), then
    from the target repository root.  An unavailable authority fails closed
    instead of degrading the runner.
    """
    candidates = [
        Path(__file__).resolve().parent.parent / ".factory" / "loop",
        Path(root).absolute() / ".factory" / "loop",
    ]
    seen: set[str] = set()
    for loop in candidates:
        if str(loop) in seen or not (loop / "lock.py").is_file():
            continue
        seen.add(str(loop))
        if str(loop) not in sys.path:
            sys.path.insert(0, str(loop))
        try:
            import lock as lock_module  # noqa: PLC0415
        except Exception:  # ImportError and boundary failures alike
            continue
        return lock_module
    fail("trusted lock supervision is unavailable")
    return None


def _install_subreaper() -> None:
    """Install this process as a child subreaper (``PR_SET_CHILD_SUBREAPER``).

    Mirrors the launch supervisor's F7 subreaper lifecycle: with the flag
    set, every orphaned descendant of the bounded command (a double-fork or
    ``setsid`` escape) is reparented to this process and reaped here, so an
    escaped descendant can never orphan to PID 1 and be lost.
    """
    import ctypes

    libc = ctypes.CDLL(None, use_errno=True)
    result = libc.prctl(36, 1, 0, 0, 0)  # PR_SET_CHILD_SUBREAPER == 36
    if result != 0:
        fail(
            "cannot install the child subreaper (prctl errno "
            f"{ctypes.get_errno()})"
        )


def _baseline_children() -> dict[int, int]:
    """PID -> starttime of every live child of this process before spawn.

    The orphan/reap scope is the command snapshot only (F6/F7): a child
    that existed before the run is explicitly excluded — identity-pinned by
    its starttime, so a PID reused after a baseline child exits is never
    mistaken for a baseline child and never signaled, killed, or reaped by
    the bounded supervision.
    """
    me = os.getpid()
    baseline: dict[int, int] = {}
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            stat_fields = (entry / "stat").read_text(
                encoding="ascii", errors="replace"
            )
        except OSError:
            continue
        end = stat_fields.rfind(")")
        if end < 0:
            continue
        fields = stat_fields[end + 1:].split()
        if len(fields) < 20:
            continue
        try:
            if int(fields[1]) == me:
                baseline[int(entry.name)] = int(fields[19])
        except ValueError:
            continue
    return baseline


def _terminate_group(process, lock_module, kill_grace: float) -> None:
    """TERM -> full bounded grace -> unconditional KILL -> reaped group.

    Mirrors the trusted root-lock group termination (F3): TERM is delivered
    per-PID to the identity-pinned leader and every member of the new
    process group and the *full* bounded grace is always observed before the
    KILL — the leader exiting on TERM is never taken as the group being gone
    — then the group is KILLed per-PID and verified to have no live pinned
    member within a bounded window, and the leader is reaped with a bounded
    wait.  The numeric group id is used only as a ``/proc`` scan key (never
    ``killpg``), so a group id reused by a foreign group after the leader is
    reaped is never signaled.
    """
    pid = process.pid
    lock_module.terminate_pinned_group(
        pid, grace=kill_grace, reap_bound=REAP_BOUND
    )
    if process.poll() is None:
        try:
            process.wait(timeout=REAP_BOUND)
        except subprocess.TimeoutExpired as exc:
            raise SystemExit(
                "machine-receipt: the bounded process-group leader was not "
                "reaped within the "
                f"{REAP_BOUND:.1f}s bound"
            ) from exc


# Bounded suite supervision: the wrapper's command runs in a new session
# with a subreaper, an identity-pinned baseline snapshot, a captured
# descendant scope, a bounded timeout, and a termination+stabilization loop
# that monitors the leader/group and every descendant reparented to this
# subreaper (``/proc/self/task/<tid>/children``) until the stdout and stderr
# pipes EOF AND no owned descendant remains for a bounded stabilization
# window — the same trusted launch/lock supervision contract as the campaign
# and launch authorities (F1/F3/F4/F6/F7, §9/§12).
RUN_TIMEOUT = 7200.0  # aligned with the receipt/evidence campaign bound
KILL_GRACE = 5.0
REAP_BOUND = 10.0
STABILIZE_WINDOW = 0.3  # the settled condition must hold for this window
STABILIZE_BOUND = 15.0  # bounded total window to reach settlement
DRAIN_BOUND = 10.0      # bounded window for the pipes to EOF after termination


def run_bounded(argv: list[str], cwd: Path) -> tuple[int, bytes, bytes, bool]:
    def limit() -> None:
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
        resource.setrlimit(resource.RLIMIT_NOFILE, (256, 256))

    root = Path(cwd).absolute()
    lock_module = _load_lock_supervision(root)
    _install_subreaper()
    baseline = _baseline_children()

    process = subprocess.Popen(
        argv, cwd=root, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        start_new_session=True, close_fds=True, preexec_fn=limit,
    )
    group_pid = process.pid
    try:
        captured = lock_module.capture_descendants(process.pid)
    except lock_module.RootLockUnsafeError as exc:
        _terminate_group(process, lock_module, KILL_GRACE)
        fail(f"cannot capture the bounded suite descendant scope: {exc}")
    assert process.stdout is not None and process.stderr is not None
    stdout = bytearray(); stderr = bytearray(); overflow = threading.Event()

    def drain(stream, output: bytearray) -> None:
        try:
            while True:
                chunk = stream.read(65_536)
                if not chunk:
                    return
                if len(output) + len(chunk) > MAX_LOG:
                    overflow.set()
                    return
                output.extend(chunk)
        finally:
            try:
                stream.close()
            except OSError:
                pass

    threads = [
        threading.Thread(target=drain, args=(process.stdout, stdout), daemon=True),
        threading.Thread(target=drain, args=(process.stderr, stderr), daemon=True),
    ]
    for thread in threads:
        thread.start()

    def children_of_self() -> dict[int, int]:
        """PID -> starttime of every child of this process.

        Reads ``/proc/self/task/<tid>/children`` for every thread of this
        process, so a descendant reparented to the subreaper (an escaped
        double-fork/``setsid`` child whose parent chain died) is observed
        with its exact identity — a PID reused by an unrelated process
        after the original died carries a different starttime and is never
        counted as an owned child.
        """
        found: dict[int, int] = {}
        try:
            tids = os.listdir("/proc/self/task")
        except OSError:
            return found
        for tid in tids:
            try:
                raw = Path(f"/proc/self/task/{tid}/children").read_text(
                    encoding="ascii", errors="replace"
                )
            except OSError:
                continue
            for token in raw.split():
                try:
                    pid = int(token)
                except ValueError:
                    continue
                fields = lock_module._proc_stat_fields(pid)
                if fields is None or len(fields) < 20:
                    continue
                try:
                    found[pid] = int(fields[19])
                except ValueError:
                    continue
        return found

    def pgid_of(pid: int) -> int | None:
        fields = lock_module._proc_stat_fields(pid)
        if fields is None or len(fields) < 3:
            return None
        try:
            return int(fields[2])
        except ValueError:
            return None

    def is_zombie(pid: int) -> bool:
        fields = lock_module._proc_stat_fields(pid)
        return fields is not None and bool(fields) and fields[0] == "Z"

    def alive(pid: int, starttime: int | None) -> bool:
        """Identity-pinned liveness: exists, is not a zombie, and (when the
        starttime is known) still carries the recorded starttime."""
        if starttime is not None:
            return lock_module._is_live_with_identity(pid, starttime)
        return os.path.exists(f"/proc/{pid}") and not is_zombie(pid)

    def reap_children() -> None:
        """waitpid (WNOHANG) every child of this process except the leader.

        The reap is per-PID (never a blanket ``waitpid(-1)`` that could reap
        a baseline child of the control plane); baseline children are
        excluded by identity, and the leader's exit status belongs to
        ``process``."""
        for pid, starttime in children_of_self().items():
            if pid == group_pid:
                continue
            if pid in baseline and baseline[pid] == starttime:
                continue
            try:
                os.waitpid(pid, os.WNOHANG)
            except (ChildProcessError, InterruptedError):
                continue

    def owned_descendants() -> dict[int, int | None]:
        """Every identity-pinned descendant still owned by the bounded run.

        An owned descendant is a live member of the run's original process
        group, a captured-scope member still carrying its recorded
        starttime, or a child reparented to this subreaper — with the
        pre-run baseline children and the leader itself excluded.  The
        group, reparented-children, and captured-scope views overlap
        deliberately: a descendant that escapes the group
        (``setsid``/double-fork) before its parent chain dies is visible
        through the captured scope or (once its parent exits) as a
        reparented child, and a descendant spawned *after* the capture
        snapshot is still caught as a group member or reparented child.
        """
        owned: dict[int, int | None] = {}
        for pid in lock_module._iter_pids():
            fields = lock_module._proc_stat_fields(pid)
            if fields is None or len(fields) < 3 or fields[0] == "Z":
                continue
            try:
                if int(fields[2]) == group_pid:
                    starttime = int(fields[19]) if len(fields) >= 20 else None
                    owned[pid] = starttime
            except ValueError:
                continue
        for pid, starttime in children_of_self().items():
            if pid == group_pid or is_zombie(pid):
                continue
            if pid in baseline and baseline[pid] == starttime:
                continue
            owned.setdefault(pid, starttime)
        for entry in captured:
            if entry.pid == group_pid:
                continue
            if lock_module._is_live_with_identity(entry.pid, entry.starttime):
                owned.setdefault(entry.pid, entry.starttime)
        return owned

    def terminate_owned(owned: dict[int, int | None]) -> None:
        """TERM -> bounded grace -> unconditional KILL -> bounded reap.

        SIGTERM and SIGKILL are delivered **per-PID by identity** — never by
        a numeric group id (``killpg``) — so a PGID reused by a foreign
        group after the leader is reaped can never be signaled (F4).  The
        leader and every known member are pinned to their starttime;
        escaped (``setsid``/double-fork) owned PIDs are signaled per-PID
        too; a member forked during the grace is captured by the repeated
        ``/proc`` scan and killed while a pinned member still lives (or
        while its ancestry reaches a pinned member).  Baseline children and
        foreign PIDs are never signaled; the leader exiting on TERM is never
        taken as the group being gone.
        """
        escaped = {
            pid: starttime for pid, starttime in owned.items()
            if starttime is not None and pgid_of(pid) != group_pid
        }
        pinned = {
            pid: starttime for pid, starttime in owned.items()
            if starttime is not None and pgid_of(pid) == group_pid
        }
        lock_module.terminate_pinned_group(
            group_pid,
            grace=KILL_GRACE,
            reap_bound=REAP_BOUND,
            pinned=pinned,
            extra=escaped,
        )
        deadline = time.monotonic() + REAP_BOUND
        while True:
            reap_children()
            if (
                not lock_module._pgid_has_live_members(group_pid)
                and not any(alive(pid, st) for pid, st in owned.items())
            ):
                return
            if time.monotonic() >= deadline:
                live = sorted(
                    pid for pid, st in owned.items() if alive(pid, st)
                )
                fail(
                    "the bounded command group/escaped descendants did not "
                    f"die within the KILL reap window ({REAP_BOUND:.1f}s); "
                    f"live descendants survive: {live}"
                )
            time.sleep(0.02)

    # Wait for the leader (bounded), while the drain threads read the pipes
    # concurrently; an overflow aborts the wait so termination happens now.
    timed_out = False
    deadline = time.monotonic() + RUN_TIMEOUT
    while not overflow.is_set():
        if process.poll() is not None:
            break
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            timed_out = True
            break
        time.sleep(min(0.05, remaining))

    # Termination + stabilization: keep monitoring the leader/group and the
    # reparented descendants until the pipes EOF AND no owned descendant
    # remains for the bounded stabilization window.  Every owned descendant
    # is terminated (TERM -> grace -> KILL -> reap); every escaped
    # descendant seen — even one cleaned up — forces the receipt to fail.
    escaped_seen: set[int] = set()
    settle_deadline = time.monotonic() + STABILIZE_BOUND
    clean_since: float | None = None
    while True:
        owned = owned_descendants()
        pipes_done = not any(thread.is_alive() for thread in threads)
        if owned:
            clean_since = None
            escaped_seen.update(
                pid for pid in owned if pgid_of(pid) != group_pid
            )
            terminate_owned(owned)
        else:
            reap_children()
            if pipes_done:
                if clean_since is None:
                    clean_since = time.monotonic()
                elif time.monotonic() - clean_since >= STABILIZE_WINDOW:
                    break
            else:
                clean_since = None
        if time.monotonic() >= settle_deadline:
            current = owned_descendants()
            live = sorted(pid for pid, st in current.items() if alive(pid, st))
            fail(
                "the bounded command descendants did not settle within "
                f"{STABILIZE_BOUND:.1f}s; live descendants survive: {live}"
            )
        time.sleep(0.02)

    if timed_out:
        fail(
            "the bounded command exceeded the "
            f"{RUN_TIMEOUT:.0f}s timeout; the run was terminated and no "
            "receipt was minted"
        )
    if process.returncode is None:
        process.poll()  # reap the leader if bounded termination killed it
    if escaped_seen:
        # Fail closed even though every escaped descendant was cleaned up:
        # a run whose descendants escaped the bounded group can never be
        # certified PASS, regardless of the leader's exit status.
        fail(
            "escaped descendants survived the bounded command termination: "
            f"{sorted(escaped_seen)} (all were terminated during bounded "
            "cleanup; a run with escaped descendants can never mint a "
            "receipt)"
        )
    for thread in threads:
        thread.join(timeout=DRAIN_BOUND)
    return process.returncode, bytes(stdout), bytes(stderr), overflow.is_set()


def record_receipt(root: Path, tag: str, argv: list[str], exit_code: int,
                   stdout: bytes, stderr: bytes, started: float,
                   round_number: int, evidence_commit: str, nonce: str) -> Path:
    receipts = receipts_dir(root)
    stdout_path = receipts / f"{tag}.stdout"
    stderr_path = receipts / f"{tag}.stderr"
    # Task 12 §19: same-tag receipt publication is no-replace — an existing
    # artifact (a pre-planted or forged receipt, or a reused tag) fails
    # closed instead of being silently replaced.
    for existing in (stdout_path, stderr_path, receipts / f"{tag}.json"):
        if existing.is_symlink() or existing.exists():
            fail(
                f"receipt tag {tag!r} already published; same-tag "
                "publication fails closed (no-replace)"
            )
    atomic_write_noreplace(stdout_path, stdout)
    atomic_write_noreplace(stderr_path, stderr)
    receipt = {
        "schema": "ralph-audit-receipt/v1",
        "tag": tag,
        "argv": argv,
        "argv_sha256": hashlib.sha256(json.dumps(argv, separators=(",", ":")).encode()).hexdigest(),
        "exit_code": exit_code,
        "stdout_sha256": hashlib.sha256(stdout).hexdigest(),
        "stderr_sha256": hashlib.sha256(stderr).hexdigest(),
        "started_at": int(started),
        "finished_at": int(time.time()),
        "evidence_commit": evidence_commit,
        "coordinator_round": round_number,
        "coordinator_nonce": nonce,
    }
    receipt_path = receipts / f"{tag}.json"
    atomic_write_noreplace(receipt_path, (json.dumps(receipt, sort_keys=True, indent=2) + "\n").encode())
    verify_artifact(stdout_path, f"receipt stdout {tag}")
    verify_artifact(stderr_path, f"receipt stderr {tag}")
    verify_artifact(receipt_path, f"receipt record {tag}")
    return receipt_path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tag", required=True)
    parser.add_argument("--root", default=str(ROOT))
    parser.add_argument("--audit-round", type=int)
    parser.add_argument("--evidence-commit")
    parser.add_argument("--nonce")
    parser.add_argument("argv", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    root = Path(args.root).resolve()
    if not TAG.fullmatch(args.tag):
        fail("invalid receipt tag")
    argv = args.argv
    if argv and argv[0] == "--":
        argv = argv[1:]
    if not argv:
        fail("no command provided")
    if any(item == "" for item in argv):
        fail("argv must be non-empty strings")
    round_number, evidence_commit, nonce = coordinator_binding(
        root, args.audit_round, args.evidence_commit, args.nonce
    )
    started = time.time()
    returncode, stdout, stderr, overflow = run_bounded(argv, root)
    if overflow:
        # Overflow keeps a bounded diagnostic on stderr (never the raw
        # truncated transcript, which can carry secrets) and exits nonzero:
        # a truncated run is never recorded, and the diagnostic itself is
        # bounded and secret-free.
        fail(
            "command output exceeded the receipt limit (stdout/stderr "
            f"bounded to {MAX_LOG} bytes); the run was terminated and the "
            "truncated transcript is withheld from the diagnostic to avoid "
            "leaking raw secrets; no receipt was minted"
        )
    receipt_path = record_receipt(
        root, args.tag, argv, returncode, stdout, stderr, started,
        round_number, evidence_commit, nonce,
    )
    print(f"[receipt: {receipt_path.relative_to(root)}]")
    return returncode if returncode < 255 else 1


if __name__ == "__main__":
    raise SystemExit(main())
