#!/usr/bin/env python3
"""Root-installed stdin protocol endpoint for a disposable factory runner."""

from __future__ import annotations

import base64
import fcntl
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import resource
import shutil
import signal
import subprocess
import sys
import tarfile
import threading
import time

SHA1 = re.compile(r"^[0-9a-f]{40}$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
NAME = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
ROOT = Path("/srv/dev-runner/workspaces")
MAX_ARCHIVE = 128 * 1024 * 1024
MAX_FILES = 10_000
MAX_CONTENT = 256 * 1024 * 1024
MAX_LOG = 4 * 1024 * 1024
NAMESPACE = "factory-runner-receipt"
SUDO = "/usr/bin/sudo"
SIGNER_HELPER = "/usr/local/libexec/factory-runner-signer"


def emit(value: dict) -> None:
    sys.stdout.write(json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def fail(message: str) -> None:
    emit({"schema": "factory-runner-receipt/v1", "result": "fail", "error": message})
    raise SystemExit(1)


def clean_env(home: Path) -> dict[str, str]:
    runtime = f"/run/user/{os.getuid()}"
    return {
        "HOME": str(home),
        "XDG_CACHE_HOME": str(home / ".cache"),
        "XDG_CONFIG_HOME": str(home / ".config"),
        "XDG_DATA_HOME": str(home / ".local/share"),
        "XDG_RUNTIME_DIR": runtime,
        "DBUS_SESSION_BUS_ADDRESS": f"unix:path={runtime}/bus",
        "PATH": "/nix/var/nix/profiles/default/bin:/usr/local/bin:/usr/bin:/bin",
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "NIX_REMOTE": "daemon",
    }


def limit_verifier() -> None:
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    resource.setrlimit(resource.RLIMIT_NOFILE, (256, 256))
    resource.setrlimit(resource.RLIMIT_CPU, (7200, 7200))


def run_bounded(argv: list[str], cwd: Path, env: dict[str, str]) -> tuple[int, bytes, bytes]:
    process = subprocess.Popen(
        argv, cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        start_new_session=True, preexec_fn=limit_verifier,
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
                    os.killpg(process.pid, signal.SIGKILL)
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
        os.killpg(process.pid, signal.SIGKILL)
        process.wait()
        fail("runner verification timed out")
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    for thread in threads:
        thread.join(timeout=5)
    if overflow.is_set() or any(thread.is_alive() for thread in threads):
        fail("runner verification output exceeded limits")
    return process.returncode, bytes(stdout), bytes(stderr)


def remove_workspace(work: Path) -> None:
    if not work.exists() and not work.is_symlink():
        return
    if work.is_symlink() or not work.is_dir():
        work.unlink()
    else:
        shutil.rmtree(work)


def sign_manifest(evidence: dict) -> dict:
    """Ask the root-owned signer to certify the server-generated evidence.

    The signer is a root-owned helper reached only through a narrowly scoped
    sudoers entry (never a caller-supplied path). It rebuilds the canonical
    manifest itself and returns the detached signature plus aggregate signer
    metadata; the exact signed bytes are echoed back so the client can store
    them verbatim. Any signer failure fails this run closed: no unsigned
    receipt can ever be emitted.
    """
    payload = json.dumps(
        {"schema": "factory-runner-sign-request/v1", "manifest": evidence},
        separators=(",", ":"),
    ).encode() + b"\n"
    try:
        result = subprocess.run(
            [SUDO, "-n", SIGNER_HELPER],
            input=payload, capture_output=True, timeout=120,
        )
    except OSError as exc:
        fail(f"cannot invoke the runner signer: {type(exc).__name__}")
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        fail(f"runner signer rejected the receipt: {detail or 'nonzero exit'}")
    try:
        response = json.loads(result.stdout)
    except (UnicodeError, json.JSONDecodeError):
        fail("runner signer returned malformed output")
    expected = {
        "schema", "result", "manifest_b64", "signature_b64", "signer_principal",
        "signer_key_sha256", "signature_algorithm", "namespace", "signature_sha256",
    }
    if (
        not isinstance(response, dict) or set(response) != expected
        or response.get("schema") != "factory-runner-sign-response/v1"
        or response.get("result") != "signed"
        or response.get("namespace") != NAMESPACE
    ):
        fail("runner signer response schema is invalid")
    if (
        not isinstance(response["signer_principal"], str)
        or not NAME.fullmatch(response["signer_principal"])
    ):
        fail("runner signer principal is invalid")
    if response.get("signature_algorithm") != "ssh-ed25519":
        fail("runner signer algorithm is unsupported")
    for field in ("signer_key_sha256", "signature_sha256"):
        if not isinstance(response[field], str) or not SHA256.fullmatch(response[field]):
            fail(f"runner signer response {field} is invalid")
    try:
        manifest_bytes = base64.b64decode(response["manifest_b64"], validate=True)
        signature = base64.b64decode(response["signature_b64"], validate=True)
    except Exception:
        fail("runner signer returned invalid encodings")
    if hashlib.sha256(signature).hexdigest() != response["signature_sha256"]:
        fail("runner signer signature digest mismatch")
    if not signature.startswith(b"-----BEGIN SSH SIGNATURE-----"):
        fail("runner signer returned an invalid detached signature")
    expected_manifest = dict(evidence)
    expected_manifest.update({
        "signer_principal": response["signer_principal"],
        "signer_key_sha256": response["signer_key_sha256"],
        "namespace": NAMESPACE,
        "signature_algorithm": response["signature_algorithm"],
    })
    expected_bytes = (json.dumps(expected_manifest, sort_keys=True, indent=2) + "\n").encode()
    if manifest_bytes != expected_bytes:
        fail("runner signer manifest does not match the verified evidence")
    return response


def main() -> int:
    if os.getuid() == 0 or os.geteuid() == 0 or os.getuid() != os.geteuid():
        fail("server requires a dedicated unprivileged runner identity")
    if os.environ.get("SSH_ORIGINAL_COMMAND") != "factory-runner-v1":
        fail("server must be invoked by the fixed SSH protocol command")
    line = sys.stdin.buffer.readline(65_537)
    if not line or len(line) > 65_536 or not line.endswith(b"\n"):
        fail("invalid request header")
    try:
        request = json.loads(line)
    except Exception:
        fail("request is not valid JSON")
    expected = {
        "schema", "runner", "commit", "commit_object_b64", "tree", "environment_blob",
        "verify_argv", "verify_argv_sha256", "archive_sha256", "archive_size",
        "working_directory", "capabilities", "nonce",
    }
    if not isinstance(request, dict) or set(request) != expected or request.get("schema") != "factory-runner-request/v1":
        fail("request schema or fields are invalid")
    if not isinstance(request["runner"], str) or not NAME.fullmatch(request["runner"]):
        fail("invalid runner")
    if not isinstance(request["nonce"], str) or not SHA256.fullmatch(request["nonce"]):
        fail("invalid nonce")
    for key in ("commit", "tree", "environment_blob"):
        if not isinstance(request[key], str) or not SHA1.fullmatch(request[key]):
            fail("invalid Git binding")
    try:
        commit_object = base64.b64decode(request["commit_object_b64"], validate=True)
    except Exception:
        fail("invalid commit object encoding")
    if len(commit_object) > 65_536 or not commit_object.startswith(f"tree {request['tree']}\n".encode()):
        fail("commit object is not bound to the requested tree")
    for key in ("verify_argv_sha256", "archive_sha256"):
        if not isinstance(request[key], str) or not SHA256.fullmatch(request[key]):
            fail("invalid digest")
    argv = request["verify_argv"]
    if argv != ["./scripts/verify-boilerplate.sh"]:
        fail("verifier argv is not approved")
    capabilities = request["capabilities"]
    if (
        not isinstance(capabilities, list) or not capabilities
        or len(capabilities) != len(set(capabilities))
        or not all(isinstance(item, str) and NAME.fullmatch(item) for item in capabilities)
    ):
        fail("invalid capabilities")
    if not set(capabilities) <= {"project-gate", "user-service"}:
        fail("unsupported capability claim")
    argv_digest = hashlib.sha256(json.dumps(argv, separators=(",", ":")).encode()).hexdigest()
    if argv_digest != request["verify_argv_sha256"]:
        fail("verifier digest mismatch")
    size = request["archive_size"]
    if not isinstance(size, int) or not 0 < size <= MAX_ARCHIVE:
        fail("invalid archive size")
    work = Path(request["working_directory"])
    if work.parent != ROOT or not NAME.fullmatch(work.name):
        fail("workspace is outside approved root")
    archive = sys.stdin.buffer.read(size + 1)
    if len(archive) != size:
        fail("archive length mismatch")
    if hashlib.sha256(archive).hexdigest() != request["archive_sha256"]:
        fail("archive digest mismatch")

    if ROOT.is_symlink() or not ROOT.is_dir():
        fail("unsafe workspace root")
    root_stat = ROOT.stat()
    if root_stat.st_uid != 0 or root_stat.st_mode & 0o022:
        fail("workspace root must be root-owned and not group/world writable")
    work_stat = work.stat() if work.exists() and not work.is_symlink() else None
    if (
        work_stat is None or not work.is_dir() or work_stat.st_uid != os.getuid()
        or work_stat.st_mode & 0o077
    ):
        fail("project workspace must be a mode-0700 runner-owned directory")
    job = work / "job"
    lock_path = work / ".factory-runner.lock"
    started = int(time.time())
    with lock_path.open("a+b") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        remove_workspace(job)
        job.mkdir(mode=0o700)
        try:
            count = 0
            total = 0
            with tarfile.open(fileobj=io.BytesIO(archive), mode="r:") as stream:
                for member in stream:
                    count += 1
                    if count > MAX_FILES:
                        fail("archive has too many entries")
                    path = PurePosixPath(member.name)
                    if path.is_absolute() or not path.parts or any(part in ("", ".", "..") for part in path.parts):
                        fail("unsafe archive path")
                    if not (member.isdir() or member.isfile()):
                        fail("archive contains unsupported entry type")
                    target = job.joinpath(*path.parts)
                    if member.isdir():
                        target.mkdir(mode=0o755, parents=True, exist_ok=True)
                        continue
                    total += member.size
                    if total > MAX_CONTENT:
                        fail("archive content is too large")
                    target.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
                    source = stream.extractfile(member)
                    if source is None:
                        fail("archive file is unreadable")
                    with target.open("xb") as output:
                        shutil.copyfileobj(source, output)
                    target.chmod(0o755 if member.mode & 0o111 else 0o644)

            home = work / f".factory-home-{request['nonce']}"
            remove_workspace(home)
            home.mkdir(mode=0o700)
            env = clean_env(home)
            subprocess.run(
                ["/usr/bin/git", "init", "-q"], cwd=job, env=env, check=True,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            subprocess.run(
                ["/usr/bin/git", "add", "-f", "--all"], cwd=job, env=env, check=True,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            tree = subprocess.check_output(
                ["/usr/bin/git", "write-tree"], cwd=job, env=env, text=True,
            ).strip()
            if tree != request["tree"]:
                fail("extracted tree does not match requested Git tree")
            commit = subprocess.run(
                ["/usr/bin/git", "hash-object", "-t", "commit", "-w", "--stdin"],
                cwd=job, env=env, input=commit_object, capture_output=True, check=True,
            ).stdout.decode().strip()
            if commit != request["commit"]:
                fail("commit object hash does not match requested commit")
            subprocess.run(
                ["/usr/bin/git", "update-ref", "HEAD", commit], cwd=job, env=env,
                check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            if subprocess.check_output(
                ["/usr/bin/git", "status", "--porcelain", "--untracked-files=normal"],
                cwd=job, env=env,
            ):
                fail("remote checkout is not clean after exact commit reconstruction")

            probes: dict[str, bool] = {}
            if "user-service" in capabilities:
                probe = subprocess.run(
                    [
                        "/usr/bin/systemd-run", "--user", "--quiet", "--wait",
                        "--pipe", "--collect", "--service-type=exec",
                        "/usr/bin/printf", "factory-user-service-ok",
                    ],
                    env=env, capture_output=True, timeout=30,
                )
                probes["user-service"] = probe.returncode == 0 and probe.stdout == b"factory-user-service-ok"
            if any(not value for value in probes.values()):
                fail("trusted capability probe failed")

            returncode, stdout, stderr = run_bounded(argv, job, env)
            probes["project-gate"] = returncode == 0

            evidenced = sorted(capability for capability in capabilities if probes.get(capability, False))
            finished_at = int(time.time())
            if returncode != 0 or len(evidenced) != len(capabilities):
                # Failure/skip receipts are never signed and never carry a manifest.
                emit({
                    "schema": "factory-runner-receipt/v1", "result": "fail",
                    "runner": request["runner"], "commit": request["commit"],
                    "tree": tree, "environment_blob": request["environment_blob"],
                    "verify_argv_sha256": request["verify_argv_sha256"],
                    "archive_sha256": request["archive_sha256"], "nonce": request["nonce"],
                    "capabilities": evidenced, "exit_code": returncode,
                    "timed_out": False, "stdout_b64": base64.b64encode(stdout).decode(),
                    "stderr_b64": base64.b64encode(stderr).decode(),
                    "started_at": started, "finished_at": finished_at, "cleanup": True,
                })
                return 0
            evidence = {
                "schema": "factory-runner-receipt/v1", "result": "pass",
                "runner": request["runner"], "commit": request["commit"],
                "tree": tree, "environment_blob": request["environment_blob"],
                "verify_argv_sha256": request["verify_argv_sha256"],
                "archive_sha256": request["archive_sha256"], "nonce": request["nonce"],
                "capabilities": evidenced, "exit_code": 0,
                "timed_out": False, "started_at": started, "finished_at": finished_at,
                "cleanup": True,
                "stdout_sha256": hashlib.sha256(stdout).hexdigest(),
                "stderr_sha256": hashlib.sha256(stderr).hexdigest(),
            }
            signed = sign_manifest(evidence)
            emit({
                "schema": "factory-runner-receipt/v1", "result": "pass",
                "runner": request["runner"], "commit": request["commit"],
                "tree": tree, "environment_blob": request["environment_blob"],
                "verify_argv_sha256": request["verify_argv_sha256"],
                "archive_sha256": request["archive_sha256"], "nonce": request["nonce"],
                "capabilities": evidenced, "exit_code": 0,
                "timed_out": False, "stdout_b64": base64.b64encode(stdout).decode(),
                "stderr_b64": base64.b64encode(stderr).decode(),
                "started_at": started, "finished_at": finished_at, "cleanup": True,
                "manifest_b64": signed["manifest_b64"],
                "signature_b64": signed["signature_b64"],
                "signer_principal": signed["signer_principal"],
                "signer_key_sha256": signed["signer_key_sha256"],
                "signature_algorithm": signed["signature_algorithm"],
                "namespace": signed["namespace"],
                "signature_sha256": signed["signature_sha256"],
            })
        except subprocess.TimeoutExpired:
            fail("runner verification timed out")
        except SystemExit:
            raise
        except Exception as exc:
            fail(f"runner execution failed: {type(exc).__name__}")
        finally:
            remove_workspace(job)
            if 'home' in locals() and home.parent == work:
                remove_workspace(home)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
