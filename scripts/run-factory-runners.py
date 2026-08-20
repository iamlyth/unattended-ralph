#!/usr/bin/env python3
"""Run declared SSH factory runners against an exact clean Git tree."""

from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path
import resource
import signal
import subprocess
import tempfile
import tomllib

ROOT = Path(__file__).resolve().parent.parent
STATE_ROOT = ROOT / ".factory-state" / "runner-evidence"
MAX_RESPONSE = 16 * 1024 * 1024


def fail(message: str) -> None:
    raise SystemExit(f"factory-runner: {message}")


def git(*args: str) -> str:
    result = subprocess.run(["git", *args], cwd=ROOT, text=True, capture_output=True)
    if result.returncode:
        fail(f"Git command failed: {' '.join(args)}")
    return result.stdout.strip()


def digest_json(value: object) -> str:
    return hashlib.sha256(json.dumps(value, separators=(",", ":"), sort_keys=False).encode()).hexdigest()


def atomic_write(path: Path, data: bytes) -> None:
    runtime = ROOT / ".factory-state"
    if runtime.is_symlink() or not runtime.is_dir():
        fail(".factory-state must be a real mode-0700 directory")
    try:
        relative = path.relative_to(runtime)
    except ValueError:
        fail(f"evidence path escapes .factory-state: {path}")
    current = runtime
    for part in relative.parts[:-1]:
        current = current / part
        if current.is_symlink():
            fail(f"evidence parent is a symlink: {current}")
        current.mkdir(mode=0o700, exist_ok=True)
        if not current.is_dir():
            fail(f"evidence parent is not a directory: {current}")
    if path.is_symlink() or (path.exists() and not path.is_file()):
        fail(f"unsafe evidence path: {path}")
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


def load_runners(commit: str) -> list[dict]:
    environment_text = git("show", f"{commit}:.factory/environment.toml")
    try:
        data = tomllib.loads(environment_text)
    except tomllib.TOMLDecodeError:
        fail("committed factory environment is invalid TOML")
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".toml") as environment_file:
        environment_file.write(environment_text); environment_file.flush()
        if subprocess.run(
            [str(ROOT / "scripts/check-factory-environment.py"), environment_file.name],
            cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        ).returncode:
            fail("committed factory environment fails policy validation")
    runners = data.get("runners", [])
    if not isinstance(runners, list):
        fail("factory runners must be an array")
    return runners


def limit_transport_output() -> None:
    resource.setrlimit(resource.RLIMIT_FSIZE, (MAX_RESPONSE, MAX_RESPONSE))


def ssh_binary() -> str:
    launcher = Path.home() / ".ssh/factory-ssh"
    if not launcher.is_symlink():
        fail("trusted SSH launcher must be the externally provisioned ~/.ssh/factory-ssh symlink")
    try:
        target = launcher.resolve(strict=True)
    except OSError:
        fail("trusted SSH launcher target is unavailable")
    if not target.is_file() or not os.access(target, os.X_OK):
        fail("trusted SSH launcher target is not executable")
    return str(launcher)


def run_runner(runner: dict, commit: str, tree: str, environment_blob: str, archive: bytes) -> dict:
    name = runner["name"]
    argv = runner["verify_argv"]
    capabilities = runner["capabilities"]
    argv_sha = digest_json(argv)
    archive_sha = hashlib.sha256(archive).hexdigest()
    nonce = hashlib.sha256(os.urandom(32)).hexdigest()
    commit_object = subprocess.check_output(["git", "cat-file", "commit", commit], cwd=ROOT)
    if len(commit_object) > 65_536:
        fail("commit object exceeds protocol limit")
    request = {
        "schema": "factory-runner-request/v1",
        "runner": name,
        "commit": commit,
        "commit_object_b64": base64.b64encode(commit_object).decode(),
        "tree": tree,
        "environment_blob": environment_blob,
        "verify_argv": argv,
        "verify_argv_sha256": argv_sha,
        "archive_sha256": archive_sha,
        "archive_size": len(archive),
        "working_directory": runner["working_directory"],
        "capabilities": capabilities,
        "nonce": nonce,
    }
    payload = json.dumps(request, separators=(",", ":")).encode() + b"\n" + archive
    with tempfile.TemporaryFile() as stdout_file, tempfile.TemporaryFile() as stderr_file:
        try:
            process = subprocess.Popen(
                [
                    ssh_binary(), "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes",
                    "-o", "IdentitiesOnly=yes", "-o", "UpdateHostKeys=no",
                    "-o", "ClearAllForwardings=yes", "-o", "ForwardAgent=no",
                    "-o", "PermitLocalCommand=no", "-o", "RequestTTY=no", "-T",
                    runner["ssh_config_alias"], "factory-runner-v1",
                ],
                stdin=subprocess.PIPE, stdout=stdout_file, stderr=stderr_file,
                start_new_session=True, preexec_fn=limit_transport_output,
            )
            process.communicate(input=payload, timeout=7500)
        except (OSError, subprocess.TimeoutExpired) as exc:
            if 'process' in locals() and process.poll() is None:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()
            fail(f"runner {name} transport failed: {type(exc).__name__}")
        stdout_file.seek(0); stderr_file.seek(0)
        transport_stdout = stdout_file.read(MAX_RESPONSE + 1)
        transport_stderr = stderr_file.read(MAX_RESPONSE + 1)
        returncode = process.returncode
    if len(transport_stdout) > MAX_RESPONSE or len(transport_stderr) > MAX_RESPONSE:
        fail(f"runner {name} response exceeded limits")
    try:
        receipt = json.loads(transport_stdout)
    except (UnicodeError, json.JSONDecodeError):
        fail(f"runner {name} returned malformed protocol output")
    if returncode != 0 or not isinstance(receipt, dict) or receipt.get("result") != "pass":
        error = receipt.get("error", "remote verification failed") if isinstance(receipt, dict) else "remote verification failed"
        fail(f"runner {name} failed: {error}")
    expected = {
        "schema", "result", "runner", "commit", "tree", "environment_blob",
        "verify_argv_sha256", "archive_sha256", "nonce", "capabilities",
        "exit_code", "timed_out", "stdout_b64", "stderr_b64", "started_at",
        "finished_at", "cleanup",
    }
    if set(receipt) != expected or receipt["schema"] != "factory-runner-receipt/v1":
        fail(f"runner {name} receipt fields are invalid")
    bindings = {
        "runner": name,
        "commit": commit,
        "tree": tree,
        "environment_blob": environment_blob,
        "verify_argv_sha256": argv_sha,
        "archive_sha256": archive_sha,
        "nonce": nonce,
    }
    if any(receipt.get(key) != value for key, value in bindings.items()):
        fail(f"runner {name} receipt binding mismatch")
    if (
        receipt["capabilities"] != sorted(capabilities)
        or receipt["exit_code"] != 0
        or receipt["timed_out"] is not False
        or receipt["cleanup"] is not True
    ):
        fail(f"runner {name} did not evidence every declared capability")
    try:
        stdout = base64.b64decode(receipt.pop("stdout_b64"), validate=True)
        remote_stderr = base64.b64decode(receipt.pop("stderr_b64"), validate=True)
    except Exception:
        fail(f"runner {name} returned invalid log encoding")
    stderr = remote_stderr + transport_stderr
    evidence_dir = STATE_ROOT / name / commit
    if evidence_dir.is_symlink() or (evidence_dir.exists() and not evidence_dir.is_dir()):
        fail(f"unsafe evidence directory for {name}")
    atomic_write(evidence_dir / "stdout.log", stdout)
    atomic_write(evidence_dir / "stderr.log", stderr)
    manifest = dict(receipt)
    manifest.update({
        "stdout_sha256": hashlib.sha256(stdout).hexdigest(),
        "stderr_sha256": hashlib.sha256(stderr).hexdigest(),
    })
    manifest_bytes = (json.dumps(manifest, sort_keys=True, indent=2) + "\n").encode()
    atomic_write(evidence_dir / "manifest.json", manifest_bytes)
    return {
        "name": name,
        "manifest": str((evidence_dir / "manifest.json").relative_to(ROOT)),
        "manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
        "capabilities": receipt["capabilities"],
    }


def development_branch() -> str:
    """Return the development branch the factory may mutate, from config.toml.

    The branch contract is declared in `.factory/config.toml` and consumed by
    `scripts/branch-guard.sh`; runner verification must not hardcode it.
    """
    config = ROOT / ".factory/config.toml"
    try:
        with config.open("rb") as stream:
            value = tomllib.load(stream)["project"]["development_branch"]
    except (OSError, tomllib.TOMLDecodeError, KeyError) as exc:
        fail(f"development branch is not declared in .factory/config.toml: {exc}")
    if not isinstance(value, str) or not value:
        fail("development_branch must be a non-empty string")
    return value


def main() -> int:
    os.environ["GIT_NO_REPLACE_OBJECTS"] = "1"
    branch = development_branch()
    if git("branch", "--show-current") != branch:
        fail(f"runner verification requires {branch}")
    if git("status", "--porcelain", "--untracked-files=normal"):
        fail("runner verification requires a clean Git tree")
    if git("replace", "-l"):
        fail("Git replacement objects are forbidden")
    runtime = ROOT / ".factory-state"
    if runtime.is_symlink() or (runtime.exists() and not runtime.is_dir()):
        fail(".factory-state must be a real directory")
    runtime.mkdir(mode=0o700, exist_ok=True)
    runtime.chmod(0o700)
    if STATE_ROOT.is_symlink() or (STATE_ROOT.exists() and not STATE_ROOT.is_dir()):
        fail("runner evidence root must be a real directory")
    STATE_ROOT.mkdir(mode=0o700, exist_ok=True)
    commit = git("rev-parse", "HEAD")
    tree = git("rev-parse", "HEAD^{tree}")
    environment_blob = git("rev-parse", "HEAD:.factory/environment.toml")
    unsupported = [
        line for line in git("ls-tree", "-r", commit).splitlines()
        if line and not line.startswith(("100644 blob ", "100755 blob "))
    ]
    if unsupported:
        fail("tracked symlinks, gitlinks, and special Git modes are unsupported by the runner archive")
    runners = load_runners(commit)
    with tempfile.NamedTemporaryFile(prefix="factory-source-", suffix=".tar") as archive_file:
        subprocess.run(
            ["git", "archive", "--format=tar", "--output", archive_file.name, commit],
            cwd=ROOT, check=True,
        )
        archive = Path(archive_file.name).read_bytes()
    records = [run_runner(runner, commit, tree, environment_blob, archive) for runner in runners]
    aggregate = {
        "schema": "factory-runner-aggregate/v1",
        "commit": commit,
        "tree": tree,
        "environment_blob": environment_blob,
        "runners": records,
    }
    aggregate_bytes = (json.dumps(aggregate, sort_keys=True, indent=2) + "\n").encode()
    atomic_write(ROOT / ".factory-state/runner-evidence.json", aggregate_bytes)
    print(f"factory-runner: {len(records)} runner(s) passed for {commit[:12]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
