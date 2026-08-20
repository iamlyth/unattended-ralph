#!/usr/bin/env python3
"""Root-installed stdin protocol endpoint for a disposable factory runner.

The endpoint is class-based and fully root-configured: every authorization
decision (approved verifier argv, capability allowlist, workspace root,
signer helper) comes from the root-owned runner policy
(/etc/factory-runner/runner-policy.json) for the executing unprivileged UID.
The committed project archive contributes only the capability-contract
definitions that drive the non-skipping probes; no product name, verifier
path, capability name, or workspace root is hardcoded here.

The endpoint validates the request class from the executing UID, requires the
requested capability set to equal the class allowlist exactly, runs the
project gate, then executes each requested capability's committed contract
probe (requiring a probe marker, required stdout markers, and no skip or
simulated markers), and asks the root-owned signer to certify only manifests
whose fields prove that clean pass. Failure and skip receipts are never
signed.
"""

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
import stat
import subprocess
import sys
import tarfile
import threading
import time

# Explicit sibling-module resolution: the deployed root-installed copies share
# one directory, and the disposable harness runs them with python -I.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from factory_runner_policy import PolicyError, class_for_uid, load_policy, validate_argv

SHA1 = re.compile(r"^[0-9a-f]{40}$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
NAME = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
MAX_ARCHIVE = 128 * 1024 * 1024
MAX_FILES = 10_000
MAX_CONTENT = 256 * 1024 * 1024
MAX_LOG = 4 * 1024 * 1024
SUDO = "/usr/bin/sudo"
CONTRACT_SCHEMA = "ralph-capability-contract/v1"
CONTRACT_FIELDS = {
    "name", "status", "probe_argv", "probe_marker", "probe_stage",
    "probe_stdout_contains", "probe_is_verify_run",
    "must_execute", "must_not_skip", "deny_simulated_markers",
}
CONTRACT_REQUIRED = {
    "name", "probe_argv", "probe_marker", "must_execute",
    "must_not_skip", "deny_simulated_markers",
}


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


def load_contracts(job: Path) -> dict[str, dict]:
    """Load capability-contract definitions from the committed project archive.

    A requested capability must have exactly one contract with
    must_execute=true here; the archive is the exact committed tree, so an
    operator cannot inject or weaken contract definitions.
    """
    path = job / ".factory" / "capability-contracts.json"
    if path.is_symlink() or not path.is_file():
        fail("committed capability-contracts.json is missing or unsafe")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        fail(f"committed capability-contracts.json is invalid: {type(exc).__name__}")
    if not isinstance(data, dict) or data.get("schema") != CONTRACT_SCHEMA:
        fail("committed capability-contracts.json schema is invalid")
    contracts = data.get("capabilities", [])
    if not isinstance(contracts, list):
        fail("committed capability-contracts.json has no capabilities array")
    loaded: dict[str, dict] = {}
    for index, contract in enumerate(contracts):
        if (
            not isinstance(contract, dict)
            or set(contract) - CONTRACT_FIELDS
            or not CONTRACT_REQUIRED.issubset(set(contract))
        ):
            fail(f"committed contract contracts[{index}] fields are invalid")
        name = contract["name"]
        if not isinstance(name, str) or not NAME.fullmatch(name):
            fail(f"committed contract contracts[{index}].name is invalid")
        if contract["must_execute"] is not True:
            fail(f"committed contract {name} must set must_execute=true")
        if name in loaded:
            fail(f"committed contract names must be unique: {name}")
        status = contract.get("status", "declared")
        if status not in ("declared", "candidate"):
            fail(f"committed contract {name} has an invalid status")
        probe_argv = contract["probe_argv"]
        if not isinstance(probe_argv, list) or not probe_argv or not all(
            isinstance(item, str) and item for item in probe_argv
        ):
            fail(f"committed contract {name} probe_argv is invalid")
        for field in ("probe_marker", "probe_stage"):
            value = contract.get(field, "")
            if not isinstance(value, str):
                fail(f"committed contract {name} {field} is invalid")
        if contract.get("probe_stage", "post") not in ("env", "post"):
            fail(f"committed contract {name} probe_stage is invalid")
        if contract.get("probe_is_verify_run") not in (None, True, False):
            fail(f"committed contract {name} probe_is_verify_run is invalid")
        for field in ("probe_stdout_contains", "must_not_skip", "deny_simulated_markers"):
            value = contract.get(field, [])
            if not isinstance(value, list) or not all(
                isinstance(item, str) and item for item in value
            ):
                fail(f"committed contract {name} {field} is invalid")
        loaded[name] = contract
    return loaded


def probe_tokens_found(combined: bytes, tokens: list[str]) -> list[str]:
    hits: set[str] = set()
    for token in tokens:
        if token.encode("utf-8") in combined:
            hits.add(token)
    return sorted(hits)


def run_contract_probe(
    contract: dict, job: Path, env: dict[str, str]
) -> tuple[bool, bytes, bytes]:
    """Run one post-gate capability contract probe.

    Pass requires: exit 0, every probe_stdout_contains substring in the probe
    stdout, and no must_not_skip or deny_simulated_markers token anywhere in
    the probe output. Probe output is appended (marker-delimited) to the
    signed aggregate log exactly as check-capability-evidence scans it.
    """
    argv = contract["probe_argv"]
    marker = contract.get("probe_marker", "")
    returncode, stdout, stderr = run_bounded(argv, job, env)
    appended_stdout = b""
    appended_stderr = b""
    if returncode == 0:
        if marker:
            appended_stdout = b"\n" + marker.encode("utf-8") + b"\n" + stdout
            appended_stderr = stderr
        else:
            appended_stdout = stdout
            appended_stderr = stderr
    if returncode != 0:
        return False, appended_stdout, appended_stderr
    required = contract.get("probe_stdout_contains", [])
    for substring in required:
        if substring.encode("utf-8") not in stdout:
            return False, appended_stdout, appended_stderr
    denied = probe_tokens_found(
        stdout + b"\n" + stderr,
        contract.get("must_not_skip", []) + contract.get("deny_simulated_markers", []),
    )
    if denied:
        return False, appended_stdout, appended_stderr
    return True, appended_stdout, appended_stderr


def sign_manifest(evidence: dict, signer_helper: str) -> dict:
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
            [SUDO, "-n", signer_helper],
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
        "namespace": response["namespace"],
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
    try:
        policy = load_policy()
    except PolicyError as exc:
        fail(f"runner policy is unavailable: {exc}")
    namespace = policy["namespace"]
    try:
        runner_class = class_for_uid(policy, os.getuid())
    except PolicyError as exc:
        fail(f"runner class binding failed: {exc}")
    line = sys.stdin.buffer.readline(65_537)
    if not line or len(line) > 65_536 or not line.endswith(b"\n"):
        fail("invalid request header")
    try:
        request = json.loads(line)
    except Exception:
        fail("request is not valid JSON")
    expected = {
        "schema", "runner", "class", "commit", "commit_object_b64", "tree",
        "environment_blob", "verify_argv", "verify_argv_sha256",
        "archive_sha256", "archive_size", "working_directory",
        "capabilities", "nonce",
    }
    if not isinstance(request, dict) or set(request) != expected or request.get("schema") != "factory-runner-request/v1":
        fail("request schema or fields are invalid")
    if not isinstance(request["runner"], str) or not NAME.fullmatch(request["runner"]):
        fail("invalid runner")
    if request["runner"] != runner_class["name"] or request["class"] != runner_class["name"]:
        fail("request runner/class does not match the executing runner class")
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
    approved_argv = runner_class["verify_argv"]
    if argv != approved_argv:
        fail("verifier argv is not approved for this runner class")
    try:
        validate_argv(argv, "request")
    except PolicyError as exc:
        fail(f"request verifier argv is invalid: {exc}")
    capabilities = request["capabilities"]
    if (
        not isinstance(capabilities, list) or not capabilities
        or len(capabilities) != len(set(capabilities))
        or not all(isinstance(item, str) and NAME.fullmatch(item) for item in capabilities)
    ):
        fail("invalid capabilities")
    allowed = runner_class["allowed_capabilities"]
    if set(capabilities) != set(allowed) or sorted(capabilities) != sorted(allowed):
        fail("capability claim does not equal the runner class allowlist exactly")
    argv_digest = hashlib.sha256(json.dumps(argv, separators=(",", ":")).encode()).hexdigest()
    if argv_digest != request["verify_argv_sha256"]:
        fail("verifier digest mismatch")
    size = request["archive_size"]
    if not isinstance(size, int) or not 0 < size <= MAX_ARCHIVE:
        fail("invalid archive size")
    workspace_root = Path(runner_class["workspace_root"])
    work = Path(request["working_directory"])
    if work.parent != workspace_root or not NAME.fullmatch(work.name):
        fail("workspace is outside the approved class root")
    archive = sys.stdin.buffer.read(size + 1)
    if len(archive) != size:
        fail("archive length mismatch")
    if hashlib.sha256(archive).hexdigest() != request["archive_sha256"]:
        fail("archive digest mismatch")

    if workspace_root.is_symlink() or not workspace_root.is_dir():
        fail("unsafe workspace root")
    root_stat = workspace_root.stat()
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

            contracts = load_contracts(job)
            missing = [capability for capability in capabilities if capability not in contracts]
            if missing:
                fail(f"requested capability lacks a committed contract: {missing}")
            for capability in capabilities:
                if contracts[capability].get("status", "declared") != "declared":
                    fail(f"capability {capability} contract is not promoted (status=candidate)")

            returncode, stdout, stderr = run_bounded(argv, job, env)
            probes: dict[str, bool] = {}
            for capability in capabilities:
                contract = contracts[capability]
                if contract.get("probe_is_verify_run"):
                    probes[capability] = returncode == 0
                    continue
                stage = contract.get("probe_stage", "post")
                if stage == "env":
                    env_rc, env_stdout, env_stderr = run_bounded(
                        contract["probe_argv"], job, env,
                    )
                    probes[capability] = (
                        env_rc == 0
                        and all(
                            item.encode("utf-8") in env_stdout
                            for item in contract.get("probe_stdout_contains", [])
                        )
                        and not probe_tokens_found(
                            env_stdout + b"\n" + env_stderr,
                            contract.get("must_not_skip", [])
                            + contract.get("deny_simulated_markers", []),
                        )
                    )
                    continue
                if returncode != 0:
                    probes[capability] = False
                    continue
                passed, probe_stdout, probe_stderr = run_contract_probe(contract, job, env)
                probes[capability] = passed
                if len(stdout) + len(stderr) + len(probe_stdout) + len(probe_stderr) > MAX_LOG:
                    fail("combined runner verification output exceeded limits")
                stdout += probe_stdout
                stderr += probe_stderr
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
            signed = sign_manifest(evidence, runner_class["signer_helper"])
            if signed.get("namespace") != namespace:
                fail("runner signer namespace does not match the runner policy")
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
