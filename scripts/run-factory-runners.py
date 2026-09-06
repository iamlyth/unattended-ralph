#!/usr/bin/env python3
"""Run declared SSH factory runners against an exact clean Git tree."""

from __future__ import annotations

import base64
import ctypes
import errno
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import resource
import signal
import stat
import subprocess
import sys
import tempfile
import tomllib

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent))
from factory_runner_artifacts import (ArtifactError, PROTOCOL as ARTIFACT_PROTOCOL,
    MAX_ARTIFACTS, MAX_ARTIFACT_FILE, MAX_ARTIFACT_BYTES, decode_payload,
    descriptors_digest, validate_descriptors)

ROOT = Path(__file__).resolve().parent.parent
# Importing protocol and pinned Git authority must not dirty a clean evidence tree.
sys.path.insert(0, str(ROOT / ".factory" / "loop"))
import gitutil  # noqa: E402

GIT = gitutil.GIT_EXECUTABLE
EXIT_TRANSPORT = 20
EXIT_FINDINGS = 21
EXIT_INTEGRITY = 22
STATE_BASE = ROOT / ".factory-state"
STATE_ROOT = STATE_BASE / "runner-evidence"
# Bind mutable publication ancestry once. All later publication/deletion is
# relative to this retained inode, so an ancestor pathname swap is irrelevant.
STATE_BASE.mkdir(mode=0o700,exist_ok=True)
_state_info=os.lstat(STATE_BASE)
if not stat.S_ISDIR(_state_info.st_mode) or stat.S_ISLNK(_state_info.st_mode) or _state_info.st_uid!=os.getuid() or stat.S_IMODE(_state_info.st_mode)!=0o700:
    raise RuntimeError(".factory-state is not a private owned directory")
STATE_DIRFD = os.open(STATE_BASE,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW|os.O_CLOEXEC)
MAX_RESPONSE = 80 * 1024 * 1024


def fail(message: str, code: int = EXIT_INTEGRITY) -> None:
    # Diagnostics are deliberately structural and bounded. Callers must never
    # pass remote bytes, environment values, hostnames, or credentials here.
    print(f"factory-runner: {message}", file=sys.stderr)
    raise SystemExit(code)


def git(*args: str) -> str:
    result = subprocess.run(
        [GIT, *args], cwd=ROOT, text=True, capture_output=True,
        env=gitutil.sanitize_git_environment(os.environ), timeout=120,
    )
    if result.returncode:
        fail(f"Git command failed: {' '.join(args)}")
    return result.stdout.strip()


def digest_json(value: object) -> str:
    return hashlib.sha256(json.dumps(value, separators=(",", ":"), sort_keys=False).encode()).hexdigest()


def _state_parent_fd(path: Path,create=False) -> tuple[int,str]:
    try:parts=path.relative_to(STATE_BASE).parts
    except ValueError:fail("publication path escapes retained .factory-state")
    fd=os.dup(STATE_DIRFD)
    try:
        for part in parts[:-1]:
            if create:
                try:os.mkdir(part,0o700,dir_fd=fd)
                except FileExistsError:pass
            nfd=os.open(part,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW|os.O_CLOEXEC,dir_fd=fd);os.close(fd);fd=nfd
        return fd,parts[-1]
    except BaseException:os.close(fd);raise

def rename_noreplace(source: Path, destination: Path) -> None:
    """renameat2 beneath the retained state dirfd; no ancestry is re-resolved."""
    libc=ctypes.CDLL(None,use_errno=True);renameat2=getattr(libc,"renameat2",None)
    if renameat2 is None: fail("renameat2 is unavailable; cannot publish evidence race-free")
    renameat2.argtypes=[ctypes.c_int,ctypes.c_char_p,ctypes.c_int,ctypes.c_char_p,ctypes.c_uint];renameat2.restype=ctypes.c_int
    sfd,sname=_state_parent_fd(source);dfd,dname=_state_parent_fd(destination,True)
    try:rc=renameat2(sfd,os.fsencode(sname),dfd,os.fsencode(dname),1)
    finally:os.close(sfd);os.close(dfd)
    if rc!=0:
        code=ctypes.get_errno()
        if code==errno.EEXIST: fail("runner evidence publication collision")
        fail(f"runner evidence publication failed with errno {code}")


def anchored_delete(root: Path) -> None:
    """Delete a bounded publication tree without resolving state ancestry."""
    pfd,name=_state_parent_fd(root)
    try:
        fd=os.open(name,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW|os.O_CLOEXEC,dir_fd=pfd)
        def clear(dfd,depth=0):
            if depth>32:fail("runner evidence deletion depth exceeds bound")
            names=os.listdir(dfd)
            if len(names)>MAX_ARTIFACTS*8+32:fail("runner evidence deletion entry bound exceeded")
            for child in names:
                info=os.stat(child,dir_fd=dfd,follow_symlinks=False)
                if stat.S_ISDIR(info.st_mode):
                    cfd=os.open(child,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW|os.O_CLOEXEC,dir_fd=dfd)
                    try:clear(cfd,depth+1)
                    finally:os.close(cfd)
                    os.rmdir(child,dir_fd=dfd)
                else:os.unlink(child,dir_fd=dfd)
        try:clear(fd)
        finally:os.close(fd)
        os.rmdir(name,dir_fd=pfd)
    finally:os.close(pfd)

def hold_tree(root: Path) -> tuple[dict[str,tuple[int,int,int,int,int]],list[int]]:
    """Open and retain every staging inode through verification/publication."""
    result={};fds=[]
    try:
        for path in [root,*sorted(root.rglob("*"))]:
            named=os.lstat(path)
            if stat.S_ISLNK(named.st_mode) or not (stat.S_ISDIR(named.st_mode) or stat.S_ISREG(named.st_mode)):
                fail("runner evidence staging contains an unsafe inode")
            flags=os.O_RDONLY|os.O_NOFOLLOW|os.O_CLOEXEC
            if stat.S_ISDIR(named.st_mode):flags|=os.O_DIRECTORY
            fd=os.open(path,flags);info=os.fstat(fd)
            if (named.st_dev,named.st_ino)!=(info.st_dev,info.st_ino):fail("runner evidence staging inode changed while opening")
            fds.append(fd);result[path.relative_to(root).as_posix()]=(info.st_dev,info.st_ino,info.st_size,info.st_mtime_ns,stat.S_IFMT(info.st_mode))
        return result,fds
    except BaseException:
        for fd in fds:os.close(fd)
        raise

def tree_identities(root: Path) -> dict[str,tuple[int,int,int,int,int]]:
    identities,fds=hold_tree(root)
    for fd in fds:os.close(fd)
    return identities


def atomic_write(path: Path, data: bytes) -> None:
    pfd,name=_state_parent_fd(path,True);temporary=f".{name}.{os.urandom(12).hex()}"
    try:
        fd=os.open(temporary,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW|os.O_CLOEXEC,0o600,dir_fd=pfd)
        try:os.write(fd,data);os.fsync(fd)
        finally:os.close(fd)
        libc=ctypes.CDLL(None,use_errno=True);renameat2=libc.renameat2
        if renameat2(pfd,os.fsencode(temporary),pfd,os.fsencode(name),1)!=0:fail("runner evidence file publication collision")
    finally:
        try:os.unlink(temporary,dir_fd=pfd)
        except FileNotFoundError:pass
        os.close(pfd)


def load_runners(commit: str) -> list[dict]:
    environment_text = git("show", f"{commit}:.factory/environment.toml")
    try:
        data = tomllib.loads(environment_text)
    except tomllib.TOMLDecodeError:
        fail("committed factory environment is invalid TOML")
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".toml") as environment_file:
        environment_file.write(environment_text); environment_file.flush()
        if subprocess.run(
            [sys.executable, str(ROOT / "scripts/check-factory-environment.py"), environment_file.name],
            cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        ).returncode:
            fail("committed factory environment fails policy validation")
    runners = data.get("runners", [])
    if not isinstance(runners, list):
        fail("factory runners must be an array")
    return runners


def limit_transport_output() -> None:
    resource.setrlimit(resource.RLIMIT_FSIZE, (MAX_RESPONSE, MAX_RESPONSE))


LAUNCHER_MANIFEST = "/etc/factory-runner/client/ssh-launcher.json"

def _trusted_chain(path: Path, fixture: bool) -> None:
    if not path.is_absolute(): fail("trusted SSH launcher path is not absolute", EXIT_TRANSPORT)
    owners={0,os.getuid()} if fixture else {0};current=Path("/")
    for part in path.parts[1:]:
        current/=part;info=os.lstat(current)
        if stat.S_ISLNK(info.st_mode) or info.st_uid not in owners or info.st_mode&0o022:
            fail("trusted SSH launcher ancestry is unsafe",EXIT_TRANSPORT)

class HeldLauncher:
    """Root-enrolled external launcher executed only through its held inode."""
    def __init__(self):
        fixture=os.environ.get("FACTORY_RUNNER_FIXTURE_MODE")=="1"
        configured=os.environ.get("FACTORY_SSH_LAUNCHER_MANIFEST",LAUNCHER_MANIFEST)
        manifest=Path(configured);_trusted_chain(manifest,fixture)
        try:
            mfd=os.open(manifest,os.O_RDONLY|os.O_NOFOLLOW|os.O_CLOEXEC);mi=os.fstat(mfd)
            if not stat.S_ISREG(mi.st_mode) or mi.st_nlink!=1:raise OSError("unsafe manifest inode")
            raw=os.read(mfd,65537)
            if len(raw)>65536 or os.read(mfd,1):raise OSError("oversized manifest")
            if (os.lstat(manifest).st_dev,os.lstat(manifest).st_ino)!=(mi.st_dev,mi.st_ino):raise OSError("manifest replaced")
            doc=json.loads(raw);os.close(mfd)
        except (OSError,ValueError,UnicodeError):
            try:os.close(mfd)
            except (OSError,UnboundLocalError):pass
            fail("trusted SSH launcher manifest is invalid",EXIT_TRANSPORT)
        if (not isinstance(doc,dict) or set(doc)!={"schema","path","sha256","device","inode"}
                or doc.get("schema")!="factory-ssh-launcher/v1" or not isinstance(doc.get("path"),str)
                or type(doc.get("device")) is not int or type(doc.get("inode")) is not int
                or doc["device"]<0 or doc["inode"]<=0
                or not re.fullmatch(r"[0-9a-f]{64}",str(doc.get("sha256","")))):
            fail("trusted SSH launcher manifest fields are invalid",EXIT_TRANSPORT)
        self.path=Path(doc["path"]);_trusted_chain(self.path,fixture)
        self.fd=os.open(self.path,os.O_RDONLY|os.O_NOFOLLOW|os.O_CLOEXEC);info=os.fstat(self.fd)
        if (not stat.S_ISREG(info.st_mode) or not info.st_mode&0o111 or info.st_nlink!=1
                or (info.st_dev,info.st_ino)!=(doc["device"],doc["inode"])):
            self.close();fail("trusted SSH launcher inode differs from enrollment",EXIT_TRANSPORT)
        h=hashlib.sha256();off=0
        while True:
            chunk=os.pread(self.fd,65536,off)
            if not chunk:break
            h.update(chunk);off+=len(chunk)
        if h.hexdigest()!=doc["sha256"]:
            self.close();fail("trusted SSH launcher digest differs from enrollment",EXIT_TRANSPORT)
        self.executable=f"/proc/self/fd/{self.fd}"
    def close(self):
        if getattr(self,"fd",None) is not None:os.close(self.fd);self.fd=None
    def __enter__(self):return self
    def __exit__(self,*_):self.close()


def _ssh_argv(runner: dict, executable: str) -> list[str]:
    return [executable, "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes",
            "-o", "IdentitiesOnly=yes", "-o", "UpdateHostKeys=no",
            "-o", "ClearAllForwardings=yes", "-o", "ForwardAgent=no",
            "-o", "PermitLocalCommand=no", "-o", "RequestTTY=no", "-T",
            runner["ssh_config_alias"], "factory-runner-v2"]


def _obtain_broker_nonce(runner: dict, campaign_id: str, readiness_nonce: str) -> str:
    request={"schema":"factory-runner-nonce-request/v1","runner":runner["name"],
             "campaign_id":campaign_id,"readiness_nonce":readiness_nonce}
    with HeldLauncher() as launcher:
        result=subprocess.run(_ssh_argv(runner,launcher.executable),input=(json.dumps(request,separators=(",",":"))+"\n").encode(),capture_output=True,timeout=120,pass_fds=(launcher.fd,))
    try: response=json.loads(result.stdout)
    except (UnicodeError,json.JSONDecodeError): fail("runner broker nonce response is malformed",EXIT_TRANSPORT)
    if result.returncode or not isinstance(response,dict) or set(response)!={"schema","nonce","runner","campaign_id","readiness_nonce"} or response.get("schema")!="factory-runner-nonce/v1" or response.get("runner")!=runner["name"] or response.get("campaign_id")!=campaign_id or response.get("readiness_nonce")!=readiness_nonce or not re.fullmatch(r"[0-9a-f]{64}",str(response.get("nonce",""))):
        fail("runner broker refused nonce issuance",EXIT_TRANSPORT)
    return response["nonce"]


def _verify_before_publication(staging: Path, manifest: dict, manifest_bytes: bytes,
                               commit: str, tree: str, capabilities: list[str]) -> None:
    """Apply current+issuance signature trust and root semantics to held bytes."""
    checker_path = ROOT / "scripts" / "check-factory-runner-evidence.py"
    spec = importlib.util.spec_from_file_location("factory_transfer_evidence_checker", checker_path)
    if spec is None or spec.loader is None:
        fail("canonical signature checker cannot be loaded")
    checker = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(checker)
        issuance = checker.load_signer_trust(commit, "issuance")
        current = checker.load_signer_trust(git("rev-parse", "HEAD"), "current revocation")
        checker.verify_manifest_signature(
            issuance, current, manifest, staging / "manifest.json", manifest_bytes)
    except SystemExit:
        fail("runner detached signature/principal/key/revocation validation failed")
    except Exception:
        fail("canonical signature checker failed before publication")
    # Semantic acceptance was already performed by the root authority's
    # externally enrolled analyzer registry over held artifact descriptors.
    # The client intentionally has no project-controlled analyzer surface.


def run_runner(runner: dict, commit: str, tree: str, environment_blob: str, archive: bytes,
               campaign_id: str | None = None, readiness_nonce: str | None = None) -> dict:
    name = runner["name"]
    campaign_id = campaign_id or os.environ.get("FACTORY_CAMPAIGN_ID", "")
    readiness_nonce = readiness_nonce or os.environ.get("FACTORY_READINESS_NONCE", "")
    if not re.fullmatch(r"[a-z0-9](?:[a-z0-9._-]{0,62}[a-z0-9])?", campaign_id) or not re.fullmatch(r"[0-9a-f]{64}", readiness_nonce):
        fail("campaign/readiness anti-replay binding is missing")
    capabilities = runner["capabilities"]
    archive_sha = hashlib.sha256(archive).hexdigest()
    nonce = _obtain_broker_nonce(runner, campaign_id, readiness_nonce)
    enrollment = ROOT / ".factory" / "runner-policy-enrollment.json"
    try:
        enrolled = json.loads(enrollment.read_text())
        authority = enrolled["probe_authorities"][name]
        if (set(enrolled) != {"schema","status","probe_authorities","note"}
                or enrolled["schema"] != "factory-runner-policy-enrollment/v1"
                or enrolled["status"] != "enrolled"
                or set(authority) != {"version","authority_sha256","status"}
                or authority["status"] != "enrolled"
                or type(authority["version"]) is not int or authority["version"] < 1
                or not re.fullmatch(r"[0-9a-f]{64}", authority["authority_sha256"])):
            raise ValueError("not enrolled")
        authority_sha256 = authority["authority_sha256"]
    except (OSError,ValueError,KeyError,TypeError):
        fail(f"runner {name} has no exact enrolled probe authority")
    commit_object = subprocess.check_output(
        [GIT, "cat-file", "commit", commit], cwd=ROOT,
        env=gitutil.sanitize_git_environment(os.environ), timeout=120,
    )
    if len(commit_object) > 65_536:
        fail("commit object exceeds protocol limit")
    request = {
        "schema": "factory-runner-request/v2",
        "runner": name,
        "class": name,
        "commit": commit,
        "commit_object_b64": base64.b64encode(commit_object).decode(),
        "tree": tree,
        "environment_blob": environment_blob,
        "authority_sha256": authority_sha256,
        "archive_sha256": archive_sha,
        "archive_size": len(archive),
        "capabilities": sorted(capabilities),
        "campaign_id": campaign_id,
        "readiness_nonce": readiness_nonce,
        "nonce": nonce,
    }
    payload = json.dumps(request, separators=(",", ":")).encode() + b"\n" + archive
    with tempfile.TemporaryFile() as stdout_file, tempfile.TemporaryFile() as stderr_file:
        try:
            with HeldLauncher() as launcher:
                process = subprocess.Popen(
                    _ssh_argv(runner,launcher.executable),
                    stdin=subprocess.PIPE, stdout=stdout_file, stderr=stderr_file,
                    start_new_session=True, preexec_fn=limit_transport_output,
                    pass_fds=(launcher.fd,),
                )
                process.communicate(input=payload, timeout=7500)
        except (OSError, subprocess.TimeoutExpired) as exc:
            if 'process' in locals() and process.poll() is None:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()
            fail(
                f"runner {name} transport failed: {type(exc).__name__}",
                EXIT_TRANSPORT,
            )
        stdout_file.seek(0); stderr_file.seek(0)
        transport_stdout = stdout_file.read(MAX_RESPONSE + 1)
        transport_stderr = stderr_file.read(MAX_RESPONSE + 1)
        returncode = process.returncode
    if len(transport_stdout) > MAX_RESPONSE or len(transport_stderr) > MAX_RESPONSE:
        fail(f"runner {name} response exceeded limits")
    if returncode != 0 and not transport_stdout:
        fail(
            f"runner {name} transport exited without a protocol response "
            f"(rc={returncode}, stderr_bytes={len(transport_stderr)}, "
            f"stderr_sha256={hashlib.sha256(transport_stderr).hexdigest()})",
            EXIT_TRANSPORT,
        )
    try:
        receipt = json.loads(transport_stdout)
    except (UnicodeError, json.JSONDecodeError):
        # Never echo remote bytes: they may contain host diagnostics or secret
        # candidates. Structural metadata is sufficient to distinguish an
        # empty/contaminated/incompatible protocol response safely.
        fail(
            f"runner {name} returned malformed protocol output "
            f"(rc={returncode}, stdout_bytes={len(transport_stdout)}, "
            f"stderr_bytes={len(transport_stderr)}, "
            f"stdout_sha256={hashlib.sha256(transport_stdout).hexdigest()}, "
            f"stderr_sha256={hashlib.sha256(transport_stderr).hexdigest()})"
        )
    if returncode != 0 or not isinstance(receipt, dict) or receipt.get("result") != "pass":
        # Remote verification is an honest product/capability finding. Do not
        # expose the runner-provided error text: it is untrusted transport data.
        fail(f"runner {name} verification did not pass", EXIT_FINDINGS)
    expected = {
        "schema", "result", "runner", "commit", "tree", "environment_blob",
        "archive_sha256", "campaign_id", "readiness_nonce", "authority_sha256", "nonce", "capabilities",
        "exit_code", "timed_out", "stdout_b64", "stderr_b64", "started_at",
        "finished_at", "cleanup", "manifest_b64", "signature_b64",
        "signer_principal", "signer_key_sha256", "signature_algorithm",
        "namespace", "signature_sha256", "artifact_protocol", "artifact_limits",
        "artifact_count", "artifact_bytes", "artifact_manifest_sha256",
        "artifact_scope_sha256", "artifacts", "artifact_payload", "host_authority",
    }
    if set(receipt) != expected or receipt["schema"] != "factory-runner-receipt/v3":
        fail(f"runner {name} receipt fields are invalid")
    bindings = {
        "runner": name,
        "commit": commit,
        "tree": tree,
        "environment_blob": environment_blob,
        "archive_sha256": archive_sha,
        "campaign_id": campaign_id, "readiness_nonce": readiness_nonce,
        "authority_sha256": authority_sha256, "nonce": nonce,
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
    if receipt.get("namespace") != "factory-runner-receipt" or receipt.get("signature_algorithm") != "ssh-ed25519":
        fail(f"runner {name} signer binding is invalid")
    for field in ("signer_key_sha256", "signature_sha256"):
        if not isinstance(receipt[field], str) or not re.fullmatch(r"^[0-9a-f]{64}$", receipt[field]):
            fail(f"runner {name} signer digest is invalid")
    if not isinstance(receipt["signer_principal"], str) or not receipt["signer_principal"]:
        fail(f"runner {name} signer principal is invalid")
    try:
        stdout = base64.b64decode(receipt.pop("stdout_b64"), validate=True)
        remote_stderr = base64.b64decode(receipt.pop("stderr_b64"), validate=True)
        manifest_bytes = base64.b64decode(receipt.pop("manifest_b64"), validate=True)
        signature = base64.b64decode(receipt.pop("signature_b64"), validate=True)
        artifact_payload = receipt.pop("artifact_payload")
    except Exception:
        fail(f"runner {name} returned invalid log or signature encoding")
    if not signature.startswith(b"-----BEGIN SSH SIGNATURE-----"):
        fail(f"runner {name} returned an invalid detached signature")
    try:
        manifest = json.loads(manifest_bytes)
    except (UnicodeError, json.JSONDecodeError):
        fail(f"runner {name} returned an invalid signed manifest")
    expected_manifest = dict(receipt)
    expected_manifest.update({"stdout_sha256": hashlib.sha256(stdout).hexdigest(),
                              "stderr_sha256": hashlib.sha256(remote_stderr).hexdigest()})
    expected_manifest.pop("signature_sha256", None)
    expected_manifest_bytes = (json.dumps(expected_manifest, sort_keys=True, indent=2) + "\n").encode()
    if manifest_bytes != expected_manifest_bytes or manifest != expected_manifest:
        fail(f"runner {name} signed manifest does not match the receipt")
    if (
        hashlib.sha256(signature).hexdigest() != receipt["signature_sha256"]
        or manifest["signer_principal"] != receipt["signer_principal"]
        or manifest["signer_key_sha256"] != receipt["signer_key_sha256"]
        or manifest["namespace"] != receipt["namespace"]
        or manifest["signature_algorithm"] != receipt["signature_algorithm"]
    ):
        fail(f"runner {name} signed manifest signer binding mismatch")
    try:
        total, artifact_digest = validate_descriptors(receipt["artifacts"], receipt["capabilities"])
        decoded_artifacts = decode_payload(artifact_payload, receipt["artifacts"])
    except ArtifactError as exc:
        fail(f"runner {name} artifact framing is invalid: {exc}")
    expected_scope = hashlib.sha256(json.dumps({"campaign_id": campaign_id,
        "readiness_nonce": readiness_nonce, "runner": name, "commit": commit, "nonce": nonce,
        "artifact_manifest_sha256": artifact_digest}, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    if (receipt["artifact_protocol"] != ARTIFACT_PROTOCOL
            or receipt["artifact_limits"] != {"count":MAX_ARTIFACTS,"file_bytes":MAX_ARTIFACT_FILE,"aggregate_bytes":MAX_ARTIFACT_BYTES}
            or receipt["artifact_count"] != len(receipt["artifacts"])
            or receipt["artifact_bytes"] != total
            or receipt["artifact_manifest_sha256"] != artifact_digest
            or receipt["artifact_scope_sha256"] != expected_scope):
        fail(f"runner {name} signed artifact summary is invalid")
    evidence_dir = STATE_ROOT / campaign_id / readiness_nonce / name / commit / nonce
    runner_dir = evidence_dir.parent
    if runner_dir.is_symlink(): fail(f"unsafe runner evidence parent for {name}")
    runner_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    if evidence_dir.is_symlink() or evidence_dir.exists():
        fail(f"runner evidence publication collision for {name}")
    staging_parent = ROOT / ".factory-state" / ".runner-transfer-staging"
    if staging_parent.is_symlink(): fail("runner transfer staging root is unsafe")
    staging_parent.mkdir(mode=0o700, exist_ok=True)
    staging = staging_parent / f"{campaign_id}-{readiness_nonce}-{name}-{commit}-{nonce}"
    if staging.exists() or staging.is_symlink(): fail(f"runner evidence staging collision for {name}")
    staging.mkdir(mode=0o700)
    held_fds=[]
    try:
        atomic_write(staging / "stdout.log", stdout)
        # SSH diagnostics are deliberately outside the signed artifact set.
        atomic_write(staging / "stderr.log", remote_stderr)
        if transport_stderr: atomic_write(staging / "transport.stderr", transport_stderr)
        atomic_write(staging / "manifest.json", manifest_bytes)
        atomic_write(staging / "manifest.sig", signature)
        for descriptor, data in decoded_artifacts:
            atomic_write(staging / "artifacts" / Path(descriptor["path"]), data)
        held_identity,held_fds=hold_tree(staging)
        _verify_before_publication(
            staging, manifest, manifest_bytes, commit, tree,
            list(receipt["capabilities"]),
        )
        if tree_identities(staging)!=held_identity:
            fail(f"runner evidence staging inode changed during verification for {name}")
        rename_noreplace(staging, evidence_dir)
        if tree_identities(evidence_dir)!=held_identity:
            anchored_delete(evidence_dir)
            fail(f"runner evidence published inode revalidation failed for {name}")
    finally:
        for fd in held_fds:os.close(fd)
        try:anchored_delete(staging)
        except FileNotFoundError:pass
    return {
        "name": name,
        "manifest": str((evidence_dir / "manifest.json").relative_to(ROOT)),
        "manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
        "capabilities": receipt["capabilities"],
        "artifact_manifest_sha256": artifact_digest,
        "artifact_count": len(decoded_artifacts),
        "artifact_bytes": total,
        "signer": {
            "principal": receipt["signer_principal"],
            "key_sha256": receipt["signer_key_sha256"],
            "algorithm": receipt["signature_algorithm"],
            "signature_sha256": receipt["signature_sha256"],
        },
    }


def run_every_runner(
    runners: list[dict],
    commit: str,
    tree: str,
    environment_blob: str,
    archive: bytes,
    *,
    invoke=run_runner,
) -> tuple[list[dict], list[tuple[str, int]]]:
    """Attempt every declared runner, never short-circuiting on failure.

    Each runner is invoked independently. A runner that fails (transport,
    findings, or integrity) is recorded as a ``(name, exit_code)`` failure
    and the remaining runners are still attempted, so a single unavailable
    runner can never mask the evidence state of the others. Returns the
    successful runner records and the ordered list of failures.

    ``invoke`` defaults to the production :func:`run_runner`; tests substitute
    a no-network mock so the no-short-circuit contract is provable without
    SSH transport.
    """
    records: list[dict] = []
    failures: list[tuple[str, int]] = []
    for runner in runners:
        name = runner["name"]
        try:
            records.append(invoke(runner, commit, tree, environment_blob, archive))
        except SystemExit as exc:
            code = exc.code if isinstance(exc.code, int) else EXIT_INTEGRITY
            failures.append((name, code))
        except Exception:  # pragma: no cover - defensive: any failure is integrity
            failures.append((name, EXIT_INTEGRITY))
    return records, failures


def dominant_failure_code(failures: list[tuple[str, int]]) -> int:
    """Return safe order-independent failure precedence.

    Integrity outranks transport, which outranks product findings. Unknown
    categories are treated as integrity failures.
    """
    priority = {EXIT_FINDINGS: 1, EXIT_TRANSPORT: 2, EXIT_INTEGRITY: 3}
    return max(
        (code if code in priority else EXIT_INTEGRITY for _, code in failures),
        key=lambda code: priority[code],
    )


def development_branch() -> str:
    try:
        value = tomllib.loads(git("show", "HEAD:.factory/config.toml"))["project"]["development_branch"]
    except (tomllib.TOMLDecodeError, KeyError):
        fail("development branch is absent from committed config")
    if not isinstance(value, str) or not value:
        fail("development branch is invalid")
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
            [GIT, "archive", "--format=tar", "--output", archive_file.name, commit],
            cwd=ROOT, check=True,
            env=gitutil.sanitize_git_environment(os.environ), timeout=120,
        )
        archive = Path(archive_file.name).read_bytes()
    records, failures = run_every_runner(runners, commit, tree, environment_blob, archive)
    if failures:
        # Infrastructure/integrity cannot be hidden by declaration order or an
        # earlier product finding: integrity outranks transport, which outranks
        # product findings. Unknown exit categories fail closed as integrity.
        dominant = dominant_failure_code(failures)
        names = ", ".join(name for name, _ in failures)
        print(f"factory-runner: {len(failures)} runner(s) failed for {commit[:12]}: {names}", file=sys.stderr)
        return dominant
    campaign_id = os.environ.get("FACTORY_CAMPAIGN_ID", "")
    readiness_nonce = os.environ.get("FACTORY_READINESS_NONCE", "")
    if not re.fullmatch(r"[a-z0-9](?:[a-z0-9._-]{0,62}[a-z0-9])?", campaign_id) or not re.fullmatch(r"[0-9a-f]{64}", readiness_nonce):
        fail("campaign/readiness anti-replay binding is missing")
    aggregate = {
        "schema": "factory-runner-aggregate/v4",
        "campaign_id": campaign_id,
        "readiness_nonce": readiness_nonce,
        "commit": commit,
        "tree": tree,
        "environment_blob": environment_blob,
        "runners": records,
    }
    aggregate_bytes = (json.dumps(aggregate, sort_keys=True, indent=2) + "\n").encode()
    aggregate_path = STATE_ROOT / campaign_id / readiness_nonce / "aggregate.json"
    if aggregate_path.exists() or aggregate_path.is_symlink():
        fail("runner aggregate publication collision")
    atomic_write(aggregate_path, aggregate_bytes)
    print(f"factory-runner: {len(records)} runner(s) passed for {commit[:12]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
