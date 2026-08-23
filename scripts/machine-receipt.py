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


def run_bounded(argv: list[str], cwd: Path) -> tuple[int, bytes, bytes, bool]:
    def limit() -> None:
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
        resource.setrlimit(resource.RLIMIT_NOFILE, (256, 256))

    process = subprocess.Popen(
        argv, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        start_new_session=True, preexec_fn=limit,
    )
    assert process.stdout is not None and process.stderr is not None
    stdout = bytearray(); stderr = bytearray(); overflow = threading.Event()

    def drain(stream, output: bytearray) -> None:
        while True:
            chunk = stream.read(65_536)
            if not chunk:
                return
            if len(output) + len(chunk) > MAX_LOG:
                overflow.set()
                try:
                    os.killpg(process.pid, 9)
                except ProcessLookupError:
                    pass
                return
            output.extend(chunk)

    threads = [
        threading.Thread(target=drain, args=(process.stdout, stdout), daemon=True),
        threading.Thread(target=drain, args=(process.stderr, stderr), daemon=True),
    ]
    for thread in threads:
        thread.start()
    try:
        process.wait(timeout=7200)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, 9)
        process.wait()
        overflow.set()
    for thread in threads:
        thread.join(timeout=5)
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
        fail("command output exceeded the receipt limit")
    receipt_path = record_receipt(
        root, args.tag, argv, returncode, stdout, stderr, started,
        round_number, evidence_commit, nonce,
    )
    print(f"[receipt: {receipt_path.relative_to(root)}]")
    return returncode if returncode < 255 else 1


if __name__ == "__main__":
    raise SystemExit(main())
