#!/usr/bin/env python3
"""Record a machine audit receipt for one coordinator-executed command.

An audit coordinator may only certify runtime behavior by executing a command
through this wrapper (or by referencing an accepted runner manifest). The
receipt binds the exact argv, the command's exit code, and SHA-256 digests of
the bounded stdout/stderr transcript under `.factory-state/audit-receipts/`.
`scripts/check-audit-receipts.py` requires a matching receipt (exit 0 for PASS)
for every executable-evidence line in the campaign audit report; subagent prose
cannot certify runtime.

Usage:
  scripts/machine-receipt.py --tag <tag> -- <argv...>
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import resource
import subprocess
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TAG = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
MAX_LOG = 4 * 1024 * 1024


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


def git_head(root: Path) -> str:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=root, text=True, capture_output=True,
        env={**os.environ, "GIT_NO_REPLACE_OBJECTS": "1"},
    )
    if result.returncode:
        fail("cannot resolve evidence commit")
    return result.stdout.strip()


def record_receipt(root: Path, tag: str, argv: list[str], exit_code: int,
                   stdout: bytes, stderr: bytes, started: float) -> Path:
    receipts = receipts_dir(root)
    stdout_path = receipts / f"{tag}.stdout"
    stderr_path = receipts / f"{tag}.stderr"
    atomic_write(stdout_path, stdout)
    atomic_write(stderr_path, stderr)
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
        "evidence_commit": git_head(root),
    }
    receipt_path = receipts / f"{tag}.json"
    atomic_write(receipt_path, (json.dumps(receipt, sort_keys=True, indent=2) + "\n").encode())
    return receipt_path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tag", required=True)
    parser.add_argument("--root", default=str(ROOT))
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
    started = time.time()
    returncode, stdout, stderr, overflow = run_bounded(argv, root)
    if overflow:
        fail("command output exceeded the receipt limit")
    receipt_path = record_receipt(root, args.tag, argv, returncode, stdout, stderr, started)
    print(f"[receipt: {receipt_path.relative_to(root)}]")
    return returncode if returncode < 255 else 1


if __name__ == "__main__":
    raise SystemExit(main())
