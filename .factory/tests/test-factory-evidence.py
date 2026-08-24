#!/usr/bin/env python3
"""Hidden evidence/receipt/verifier authority suite (EVID-01 §19; Task 12).

This test lives under the hidden ``.factory/tests/`` namespace because the
specification (HIDE-01, §3) keeps harness-only tests out of the adopting
product's visible test tree.  It verifies the retained exact-commit evidence
machinery of ``.factory/loop/evidence.py`` and its script counterparts
(``scripts/machine-receipt.py``, ``scripts/check-audit-receipts.py``):

* **held verifier binding (§19)**: the deterministic verification entrypoint
  is opened no-follow and bound to its committed blob, secure identity, and
  inode *before* any untrusted phase; a worktree/content substitution before
  binding, and a pathname/content/committed-tree/inode substitution after
  binding (pre/post untrusted) all fail closed;
* **config command parsing / path magic**: ``verification.campaign_command``
  is parsed from the committed config only, and a verifier executable must be
  a canonical ``./repo-relative`` path or a pinned trusted external
  executable — absolute, relative, traversal, dot-segment, and control-
  character paths are rejected;
* **receipt/adjacent logs**: the receipt JSON and its adjacent stdout/stderr
  transcripts are read through one no-follow descriptor with
  owner/mode/link-count/inode identity checks at both ends; symlink, hardlink
  alias, foreign owner, wrong mode, escaping, and oversized artifacts all
  fail closed;
* **same-tag no-replace publication**: ``machine-receipt.py`` publishes each
  receipt artifact atomically with no-replace semantics — an existing or
  concurrently published same-tag artifact is never overwritten, and exactly
  one concurrent publisher wins;
* **exact references**: audits cite exact ``[receipt: …]`` /
  ``[manifest: …]`` references; PASS requires exit 0, FAIL a non-zero exit,
  BLOCKED evidence forces ``result: findings``, and model prose can never
  certify runtime;
* **tiers never self-elevate**: receipts cap at ``installed``, signed runner
  manifests at ``real_system``, prose claims no tier at all, and ``human``
  acceptance can never be machine-claimed;
* **stale round/commit/digest**: receipts bound to a stale coordinator round,
  a stale evidence commit, or a stale nonce are rejected;
* **manifest signature verifier**: the strict runner-evidence helper rejects
  malformed, unsigned, and unprovisioned-signer manifests *without real
  signing keys*, using commit-bound fixture repos;
* **campaign integration**: the campaign binds the verifier before the
  untrusted tester phase, and a post-untrusted substitution fails the
  campaign closed as ``infrastructure_failure``.

The suite reuses the established fixture patterns of the other
``test-factory-*.py`` modules: real committed Git repositories and the pinned
absolute Git executable.  It never provisions real runner keys, never touches
``.ralph/`` or credentials, and creates no branch/worktree/stash.
"""

from __future__ import annotations

import dataclasses
import hashlib
import importlib.util
import io
import json
import os
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import unittest.mock

ROOT = Path(__file__).resolve().parents[2]
LOOP = ROOT / ".factory" / "loop"
FIXTURES = ROOT / ".factory" / "tests" / "fixtures"
STATE_DIR = ".factory-state"

sys.path.insert(0, str(LOOP))
import campaign as campaign_module  # noqa: E402
import evidence as evidence_module  # noqa: E402
import gitutil  # noqa: E402
import lock as lock_module  # noqa: E402
import state as state_module  # noqa: E402

GIT = gitutil.GIT_EXECUTABLE
TRUE_EXECUTABLE = Path(shutil.which("true"))
MACHINE_RECEIPT = ROOT / "scripts" / "machine-receipt.py"
CHECK_AUDIT_RECEIPTS = ROOT / "scripts" / "check-audit-receipts.py"
CHECK_RUNNER_EVIDENCE = ROOT / "scripts" / "check-factory-runner-evidence.py"
INITIALIZE_CAMPAIGN_AUDIT = ROOT / "scripts" / "initialize-campaign-audit.py"
VALIDATE_CAMPAIGN_AUDIT = ROOT / "scripts" / "validate-campaign-audit.py"
CHECK_ENV = ROOT / "scripts" / "check-factory-environment.py"

SHA1 = evidence_module.SHA1
SHA256 = evidence_module.SHA256


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def run(
    command: list[str],
    root: Path | None = None,
    *,
    check: bool = True,
    env: dict | None = None,
    timeout: float = 180,
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        command, cwd=root, text=True, capture_output=True, env=env,
        timeout=timeout,
    )
    if check and result.returncode:
        raise AssertionError(
            (command, result.returncode, result.stdout[-2000:], result.stderr[-2000:])
        )
    return result


def _git(workspace: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return run([GIT, "-C", str(workspace), *args], check=True)


def _mkdir(path: Path, mode: int = 0o700) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    os.chmod(path, mode)
    return path


def _write(path: Path, data: bytes | str) -> Path:
    path.write_bytes(data if isinstance(data, bytes) else data.encode("utf-8"))
    return path


def _load_machine_receipt() -> object:
    """Load ``scripts/machine-receipt.py`` as an importable module.

    The hidden evidence suite exercises the wrapper's bounded supervision
    in-process (baseline-child and foreign-process isolation), so the real
    wrapper authority is loaded the same way ``generic_evidence.py`` loads
    it — by committed path with a pinned interpreter, never a copy.
    """
    spec = importlib.util.spec_from_file_location(
        "machine_receipt", ROOT / "scripts" / "machine-receipt.py"
    )
    if spec is None or spec.loader is None:
        raise AssertionError("cannot load the machine-receipt authority")
    module = importlib.util.module_from_spec(spec)
    sys.modules["machine_receipt"] = module
    spec.loader.exec_module(module)
    return module


def _wait_for_pidfile(path: Path, bound: float = 10.0) -> int:
    """Poll for a written PID barrier file and parse it (bounded wait).

    The escaped/foreign children write their PID to a file as a barrier
    before holding the pipes; this is the test's deterministic rendezvous
    with the child (never a sleep-based race) and fails closed if the child
    never starts.
    """
    deadline = time.monotonic() + bound
    while True:
        if path.exists():
            try:
                return int(path.read_text(encoding="ascii").strip())
            except (OSError, ValueError):
                pass
        if time.monotonic() >= deadline:
            raise AssertionError(f"child pid barrier never appeared: {path}")
        time.sleep(0.01)


def _kill_pid(pid: int | None) -> None:
    """Best-effort SIGKILL of one PID (test cleanup; never raises)."""
    if pid is None:
        return
    try:
        os.kill(pid, 9)
    except (ProcessLookupError, PermissionError):
        pass


def _escaped_script(pidfile: Path, *, ignore_term: bool = False) -> Path:
    """Write an executable escaped-child script with a ready barrier.

    The script writes its PID to ``pidfile``, touches a ``<pidfile>.ready``
    barrier, then ``exec sleep`` to hold the wrapper's inherited
    stdout/stderr pipes open.  The leader command runs it under ``setsid``
    in the background and waits for the ``ready`` barrier before exiting, so
    the wrapper can only detect the escape **after** the PID barrier file
    exists — the test's rendezvous with the child never races the wrapper's
    termination, and the pipes cannot EOF until the wrapper terminates the
    child.  ``ignore_term`` installs ``trap '' TERM`` (which survives
    ``exec``) so the wrapper's TERM cannot end the child.
    """
    ready = pidfile.with_name(pidfile.name + ".ready")
    script = pidfile.with_name(pidfile.name + ".sh")
    body = (
        f'echo "$$" > {pidfile}\n'
        f'touch {ready}\n'
        "exec sleep 60\n"
    )
    if ignore_term:
        body = 'trap "" TERM\n' + body
    _write(script, "#!/usr/bin/env sh\n" + body)
    script.chmod(0o755)
    return script


def _escaped_command(pidfile: Path, *, ignore_term: bool = False) -> str:
    """A leader command that deterministically ``setsid``-escapes a child.

    The background ``setsid`` child is spawned first, the leader waits on the
    child's ``ready`` barrier (bounded, never a sleep-based race) before
    exiting, so the wrapper always observes the escape only after the PID
    barrier file exists.  The child then holds the inherited pipes.
    """
    script = _escaped_script(pidfile, ignore_term=ignore_term)
    ready = pidfile.with_name(pidfile.name + ".ready")
    return (
        f"sh -c 'setsid {script} & "
        f"while [ ! -e {ready} ]; do sleep 0.05; done; "
        "echo leader-done'"
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


class RepoFixture:
    """One committed synthetic repository for verifier/config/manifest tests."""

    def __init__(self, tmp: Path, name: str = "work") -> None:
        self.root = tmp / name
        self.root.mkdir(parents=True)
        for rel in ("scripts", ".factory"):
            (self.root / rel).mkdir(parents=True, exist_ok=True)
        _write(self.root / ".factory" / "me", "fixture marker\n")
        _git(self.root, "init", "-q", "-b", "develop")
        _git(self.root, "config", "user.email", "fixture@test")
        _git(self.root, "config", "user.name", "fixture")

    def commit(self, message: str = "fixture") -> str:
        _git(self.root, "add", "-A")
        _git(self.root, "commit", "-qm", message)
        return self.head()

    def head(self) -> str:
        return _git(self.root, "rev-parse", "HEAD").stdout.strip()

    def write(self, rel: str, data: bytes | str, *, mode: int = 0o644) -> Path:
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        _write(path, data)
        os.chmod(path, mode)
        return path

    def write_verifier(
        self, content: bytes = b"#!/bin/sh\nexit 0\n", rel: str = "scripts/verify.sh"
    ) -> Path:
        return self.write(rel, content, mode=0o755)

    def write_config(self, command: object) -> None:
        text = "[verification]\ncampaign_command = %s\n" % json.dumps(command)
        self.write(".factory/config.toml", text, mode=0o644)


class ReceiptFixture:
    """One coordinator-bound machine receipt (valid by construction)."""

    DEFAULT_ARGV = ("sh", "-c", "echo runtime output")

    def __init__(self, root: Path) -> None:
        self.root = root
        _mkdir(self.root / STATE_DIR)
        self.recs = _mkdir(self.root / STATE_DIR / "audit-receipts")
        self.round = 1
        self.base = "a" * 40
        self.nonce = "b" * 64
        self.coordinator()

    def coordinator(
        self, *, round_no: int = 1, base: str = "", nonce: str = ""
    ) -> None:
        self.round = round_no
        self.base = base or self.base
        self.nonce = nonce or self.nonce
        state = {
            "schema": "ralph-audit-coordinator/v1",
            "round": self.round,
            "base_commit": self.base,
            "nonce": self.nonce,
            "created_at": 1,
        }
        _write(self.root / STATE_DIR / "audit-coordinator.json",
               json.dumps(state, sort_keys=True) + "\n")
        os.chmod(self.root / STATE_DIR / "audit-coordinator.json", 0o600)

    def write(
        self,
        tag: str,
        *,
        argv: tuple[str, ...] = DEFAULT_ARGV,
        exit_code: int = 0,
        stdout: bytes = b"runtime output\n",
        stderr: bytes = b"",
        evidence_commit: str | None = None,
        round_no: int | None = None,
        nonce: str | None = None,
    ) -> dict:
        """Write one canonical receipt + adjacent transcripts; return the JSON."""
        _write(self.recs / f"{tag}.stdout", stdout)
        _write(self.recs / f"{tag}.stderr", stderr)
        os.chmod(self.recs / f"{tag}.stdout", 0o600)
        os.chmod(self.recs / f"{tag}.stderr", 0o600)
        data = {
            "schema": "ralph-audit-receipt/v1",
            "tag": tag,
            "argv": list(argv),
            "argv_sha256": hashlib.sha256(
                json.dumps(list(argv), separators=(",", ":")).encode("utf-8")
            ).hexdigest(),
            "exit_code": exit_code,
            "stdout_sha256": sha256(stdout),
            "stderr_sha256": sha256(stderr),
            "started_at": 1,
            "finished_at": 2,
            "evidence_commit": evidence_commit or self.base,
            "coordinator_round": round_no or self.round,
            "coordinator_nonce": nonce or self.nonce,
        }
        _write(self.recs / f"{tag}.json",
               json.dumps(data, sort_keys=True, indent=2) + "\n")
        os.chmod(self.recs / f"{tag}.json", 0o600)
        return data

    def tamper_log(self, tag: str, log: str, content: bytes = b"intruder\n") -> None:
        _write(self.recs / f"{tag}.{log}", content)

    def report(self, result: str, evidence_lines: list[str]) -> str:
        body = "\n".join(
            f"- Executable evidence: {line}" for line in evidence_lines
        )
        return (
            "---\nschema: ralph-campaign-audit/v1\n"
            f"result: {result}\n---\n# Audit\n## Evidence reviewed\n{body}\n"
        )

    def line(
        self,
        argv: tuple[str, ...] | list[str] = DEFAULT_ARGV,
        marker: str = "PASS",
        ref: str | None = None,
        manifest: str | None = None,
        tier: str | None = None,
    ) -> str:
        """Render one executable-evidence line payload (LOW5 exact argv).

        The represented command is the shlex rendering of the exact receipt
        argv, so the evidence validator's exact-argv equality holds for the
        fixture receipts built by :meth:`write`.
        """
        payload = f"`{shlex.join(list(argv))}` {marker}"
        if ref is not None:
            payload += f" [receipt: {ref}]"
        if manifest is not None:
            payload += f" [manifest: {manifest}]"
        if tier is not None:
            payload += f" tier={tier}"
        return payload


class ManifestFixture:
    """Commit-bound runner-evidence fixture (no real signing keys).

    The aggregate, manifest, transcripts, environment, and signer trust are
    built exactly as the strict runner-evidence checker recomputes them from
    the committed tree, so every failure the tests assert is a *signature /
    trust / structure* failure rather than a fixture-binding mismatch.
    """

    FAKE_PUBLIC_KEY = "ssh-ed25519 fake-public-key"
    FAKE_KEY_SHA256 = sha256(FAKE_PUBLIC_KEY.encode("utf-8"))

    def __init__(self, root: Path) -> None:
        self.root = root
        _mkdir(self.root)
        for rel in ("scripts", "docs", ".factory"):
            (self.root / rel).mkdir(parents=True, exist_ok=True)
        _mkdir(self.root / STATE_DIR)
        self.trust = {
            "schema": "ralph-runner-signer-trust/v1",
            "description": "hidden-suite fixture signer (no real key)",
            "require_signature": True,
            "enabled": False,
            "namespace": "factory-runner-receipt",
            "public_keys": [],
            "allowed_principals": [],
        }
        self.environment = (
            "schema_version = 1\n"
            "[[runners]]\n"
            'name = "fake-runner"\n'
            'transport = "ssh"\n'
            'ssh_config_alias = "fake-runner"\n'
            'working_directory = "/srv/dev-runner/workspaces/fake-project"\n'
            'capabilities = ["project-gate"]\n'
            'verify_argv = ["./scripts/verify-boilerplate.sh"]\n'
        )

    def commit(self, message: str = "runner fixture") -> str:
        shutil.copy2(CHECK_RUNNER_EVIDENCE, self.root / "scripts" /
                     "check-factory-runner-evidence.py")
        shutil.copy2(CHECK_ENV, self.root / "scripts" / "check-factory-environment.py")
        _write(self.root / ".factory" / "environment.toml", self.environment)
        _write(self.root / ".factory" / "signer-trust.json",
               json.dumps(self.trust, sort_keys=True, indent=2) + "\n")
        _write(self.root / "docs" / "SPEC.md", "# Fixture spec\n")
        _write(self.root / ".gitignore", ".factory-state/\n")
        _git(self.root, "init", "-q", "-b", "develop")
        _git(self.root, "config", "user.email", "fixture@test")
        _git(self.root, "config", "user.name", "fixture")
        _git(self.root, "add", "-A")
        _git(self.root, "commit", "-qm", message)
        return self.head()

    def head(self) -> str:
        return _git(self.root, "rev-parse", "HEAD").stdout.strip()

    def manifest_record(self, head: str) -> Path:
        """Build the aggregate + manifest + transcripts; return the manifest path."""
        tree = _git(self.root, "rev-parse", f"{head}^{{tree}}").stdout.strip()
        environment_blob = _git(
            self.root, "rev-parse", f"{head}:.factory/environment.toml"
        ).stdout.strip()
        argv_digest = hashlib.sha256(
            json.dumps(["./scripts/verify-boilerplate.sh"],
                       separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        subprocess.run(
            [GIT, "archive", "--format=tar", "--output",
             str(self.root / "commit-archive.tar"), head],
            cwd=self.root, check=True, capture_output=True,
        )
        archive_sha256 = sha256((self.root / "commit-archive.tar").read_bytes())
        (self.root / "commit-archive.tar").unlink()
        empty = sha256(b"")
        manifest_dir = self.root / STATE_DIR / "runner-evidence" / "fake-runner" / head
        _mkdir(manifest_dir)
        manifest_path = manifest_dir / "manifest.json"
        manifest = {
            "schema": "factory-runner-receipt/v1",
            "result": "pass",
            "runner": "fake-runner",
            "commit": head,
            "tree": tree,
            "environment_blob": environment_blob,
            "verify_argv_sha256": argv_digest,
            "archive_sha256": archive_sha256,
            "nonce": "0" * 64,
            "capabilities": ["project-gate"],
            "exit_code": 0,
            "timed_out": False,
            "started_at": 1,
            "finished_at": 2,
            "cleanup": True,
            "stdout_sha256": empty,
            "stderr_sha256": empty,
            "signer_principal": "factory-signer",
            "signer_key_sha256": self.FAKE_KEY_SHA256,
            "namespace": "factory-runner-receipt",
            "signature_algorithm": "ssh-ed25519",
        }
        raw = (json.dumps(manifest, sort_keys=True, indent=2) + "\n").encode()
        _write(manifest_path, raw)
        _write(manifest_dir / "stdout.log", b"")
        _write(manifest_dir / "stderr.log", b"")
        _write(manifest_dir / "manifest.sig", b"not-a-real-signature\n")
        signature_sha256 = sha256(b"not-a-real-signature\n")
        aggregate = {
            "schema": "factory-runner-aggregate/v1",
            "commit": head,
            "tree": tree,
            "environment_blob": environment_blob,
            "runners": [
                {
                    "name": "fake-runner",
                    "manifest": f".factory-state/runner-evidence/fake-runner/{head}/manifest.json",
                    "manifest_sha256": sha256(raw),
                    "capabilities": ["project-gate"],
                    "signer": {
                        "principal": "factory-signer",
                        "key_sha256": self.FAKE_KEY_SHA256,
                        "algorithm": "ssh-ed25519",
                        "signature_sha256": signature_sha256,
                    },
                }
            ],
        }
        _write(self.root / STATE_DIR / "runner-evidence.json",
               json.dumps(aggregate, sort_keys=True, indent=2) + "\n")
        return manifest_path


def _load_campaign_fixture_module():
    """Load the committed campaign fixture builder (importlib, dashed path)."""
    path = ROOT / ".factory" / "tests" / "test-factory-campaign.py"
    spec = importlib.util.spec_from_file_location(
        "test_factory_campaign_fixture", str(path))
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------------------
# Held verifier binding (committed blob / descriptor / inode / substitution)
# ---------------------------------------------------------------------------


class VerifierBindingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="factory-evidence-verifier."))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def test_bind_committed_repo_relative_verifier(self) -> None:
        repo = RepoFixture(self.tmp)
        repo.write_verifier()
        head = repo.commit()
        binding = evidence_module.bind_verifier(
            repo.root, ["./scripts/verify.sh"], commit=head)
        self.assertEqual(binding.schema, evidence_module.SCHEMA_NAME)
        self.assertEqual(binding.command, ("./scripts/verify.sh",))
        self.assertEqual(binding.executable, "./scripts/verify.sh")
        self.assertEqual(binding.commit, head)
        self.assertEqual(binding.mode, "0755")
        self.assertFalse(binding.external)
        self.assertEqual(len(binding.inode), 2)
        self.assertEqual(binding.blob, hashlib.sha1(b"#!/bin/sh\nexit 0\n").hexdigest())
        self.assertEqual(binding.sha256, sha256(b"#!/bin/sh\nexit 0\n"))
        # The held descriptor pins the bound inode and re-returns the bytes.
        with evidence_module.HeldVerifier(repo.root, binding) as held:
            raw = held.revalidate(current_commit=head)
            self.assertEqual(raw, b"#!/bin/sh\nexit 0\n")
        # The digest is stable across identical commands.
        binding2 = evidence_module.bind_verifier(
            repo.root, ["./scripts/verify.sh"], commit=head)
        self.assertEqual(binding.digest(), binding2.digest())

    def test_worktree_content_substitution_before_bind_fails(self) -> None:
        repo = RepoFixture(self.tmp)
        repo.write_verifier()
        head = repo.commit()
        repo.write_verifier(content=b"#!/bin/sh\nexit 7\n")
        with self.assertRaises(evidence_module.VerifierBindingError):
            evidence_module.bind_verifier(
                repo.root, ["./scripts/verify.sh"], commit=head)

    def test_pathname_substitution_after_bind_fails(self) -> None:
        repo = RepoFixture(self.tmp)
        repo.write_verifier()
        head = repo.commit()
        binding = evidence_module.bind_verifier(
            repo.root, ["./scripts/verify.sh"], commit=head)
        held = evidence_module.HeldVerifier(repo.root, binding)
        self.addCleanup(held.close)
        # Post-bind (post-untrusted): the pathname is replaced by a different
        # file at the same name — the retained descriptor still pins the old
        # inode, so the path/inode substitution fails closed.
        verifier = repo.root / "scripts" / "verify.sh"
        verifier.rename(repo.root / "scripts" / "saved.sh")
        _write(verifier, b"#!/bin/sh\nexit 9\n")
        os.chmod(verifier, 0o755)
        with self.assertRaises(evidence_module.VerifierBindingError):
            held.revalidate(git=None, current_commit=head)

    def test_content_substitution_after_bind_fails(self) -> None:
        repo = RepoFixture(self.tmp)
        repo.write_verifier()
        head = repo.commit()
        binding = evidence_module.bind_verifier(
            repo.root, ["./scripts/verify.sh"], commit=head)
        held = evidence_module.HeldVerifier(repo.root, binding)
        self.addCleanup(held.close)
        # In-place content rewrite (same inode, different bytes) is caught by
        # the retained-descriptor digest check.
        verifier = repo.root / "scripts" / "verify.sh"
        verifier.write_bytes(b"#!/bin/sh\nexit 9\n")
        with self.assertRaises(evidence_module.VerifierBindingError):
            held.revalidate(git=None, current_commit=head)

    def test_committed_tree_substitution_after_bind_fails(self) -> None:
        repo = RepoFixture(self.tmp)
        repo.write_verifier()
        head = repo.commit()
        binding = evidence_module.bind_verifier(
            repo.root, ["./scripts/verify.sh"], commit=head)
        held = evidence_module.HeldVerifier(repo.root, binding)
        self.addCleanup(held.close)
        repo.write_verifier(content=b"#!/bin/sh\nexit 5\n")
        new_head = repo.commit("verifier change")
        self.assertNotEqual(new_head, head)
        with self.assertRaises(evidence_module.VerifierBindingError):
            held.revalidate(git=None, current_commit=new_head)

    def test_unlink_after_bind_fails(self) -> None:
        repo = RepoFixture(self.tmp)
        repo.write_verifier()
        head = repo.commit()
        binding = evidence_module.bind_verifier(
            repo.root, ["./scripts/verify.sh"], commit=head)
        held = evidence_module.HeldVerifier(repo.root, binding)
        self.addCleanup(held.close)
        (repo.root / "scripts" / "verify.sh").unlink()
        with self.assertRaises(evidence_module.VerifierBindingError):
            held.revalidate(git=None, current_commit=head)

    def test_symlink_verifier_fails_before_bind(self) -> None:
        repo = RepoFixture(self.tmp)
        repo.write_verifier()
        head = repo.commit()
        verifier = repo.root / "scripts" / "verify.sh"
        verifier.unlink()
        _write(repo.root / "scripts" / "saved.sh", b"#!/bin/sh\nexit 0\n")
        os.chmod(repo.root / "scripts" / "saved.sh", 0o755)
        verifier.symlink_to(repo.root / "scripts" / "saved.sh")
        with self.assertRaises(evidence_module.VerifierBindingError):
            evidence_module.bind_verifier(
                repo.root, ["./scripts/verify.sh"], commit=head)

    def test_hardlinked_verifier_fails_before_bind(self) -> None:
        repo = RepoFixture(self.tmp)
        repo.write_verifier()
        head = repo.commit()
        os.link(repo.root / "scripts" / "verify.sh",
                repo.root / "scripts" / "alias.sh")
        with self.assertRaises(evidence_module.VerifierBindingError):
            evidence_module.bind_verifier(
                repo.root, ["./scripts/verify.sh"], commit=head)

    def test_group_writable_verifier_fails_before_bind(self) -> None:
        repo = RepoFixture(self.tmp)
        repo.write_verifier()
        head = repo.commit()
        os.chmod(repo.root / "scripts" / "verify.sh", 0o664)
        with self.assertRaises(evidence_module.VerifierBindingError):
            evidence_module.bind_verifier(
                repo.root, ["./scripts/verify.sh"], commit=head)

    def test_non_executable_verifier_fails_before_bind(self) -> None:
        repo = RepoFixture(self.tmp)
        repo.write_verifier()
        head = repo.commit()
        os.chmod(repo.root / "scripts" / "verify.sh", 0o644)
        with self.assertRaises(evidence_module.VerifierBindingError):
            evidence_module.bind_verifier(
                repo.root, ["./scripts/verify.sh"], commit=head)

    def test_empty_and_malformed_command_fails(self) -> None:
        repo = RepoFixture(self.tmp)
        repo.write_verifier()
        repo.commit()
        for bad in ([], [""], ["", "x"], ["x\x00y"], ["\x00"]):
            with self.assertRaises(evidence_module.VerifierBindingError):
                evidence_module.bind_verifier(repo.root, bad)

    def test_external_trusted_executable_bound(self) -> None:
        repo = RepoFixture(self.tmp)
        repo.commit()
        executable = str(TRUE_EXECUTABLE)
        try:
            gitutil.require_trusted_executable(executable)
        except gitutil.GitBoundaryError:
            self.skipTest("no pinned immutable external executable on this host")
        binding = evidence_module.bind_verifier(
            repo.root, [executable, "--flag"], commit=repo.head())
        self.assertTrue(binding.external)
        self.assertEqual(binding.commit, "")
        self.assertEqual(binding.blob, "")
        with evidence_module.HeldVerifier(repo.root, binding) as held:
            raw = held.revalidate()
            self.assertEqual(hashlib.sha256(raw).hexdigest(), binding.sha256)

    def test_revalidate_verifier_without_held_uses_fresh_binding(self) -> None:
        repo = RepoFixture(self.tmp)
        repo.write_verifier()
        head = repo.commit()
        binding = evidence_module.bind_verifier(
            repo.root, ["./scripts/verify.sh"], commit=head)
        raw = evidence_module.revalidate_verifier(
            repo.root, binding, current_commit=head)
        self.assertEqual(raw, b"#!/bin/sh\nexit 0\n")

    def test_canonical_relative_path_magic(self) -> None:
        valid = "./scripts/verify.sh"
        self.assertEqual(evidence_module._canonical_relative(valid), valid)
        for bad in (
            "scripts/verify.sh", "verify.sh", "./verify.sh/", "./", "..",
            ".", "./..", "./../verify.sh", "./a/./b", "./a//b",
            "/abs/path", "./has\\slash", "./with\x00nul", "./x/\x7f",
        ):
            with self.assertRaises(evidence_module.VerifierBindingError):
                evidence_module._canonical_relative(bad)

    def test_bind_external_without_pinned_chain_fails(self) -> None:
        repo = RepoFixture(self.tmp)
        repo.commit()
        fake = repo.root / "scripts" / "self-made"
        _write(fake, b"#!/bin/sh\n")
        os.chmod(fake, 0o755)
        # A caller-owned absolute executable is never a pinned trusted
        # executable: only the immutable-chain authority may bind externals.
        with self.assertRaises(evidence_module.VerifierBindingError):
            evidence_module.bind_verifier(
                repo.root, [str(fake)], commit=repo.head())

    # -- Task 12 MED1: the retained descriptor, not a later pathname, decides
    # what the child executes; descriptor chmod/hardlink substitutions fail
    # closed; the root lock descriptor is never inherited by the verifier.

    def test_spawn_executes_bound_inode_after_pathname_swap(self) -> None:
        repo = RepoFixture(self.tmp)
        content = (
            b"#!/bin/sh\n"
            b"printf 'bound-content\\n'\n"
            b"printf 'zero=%s\\n' \"$0\"\n"
            b"printf 'one=%s\\n' \"$1\"\n"
            b"printf 'two=%s\\n' \"$2\"\n"
        )
        repo.write_verifier(content=content)
        command = ["./scripts/verify.sh", "alpha", "beta"]
        head = repo.commit()
        binding = evidence_module.bind_verifier(
            repo.root, command, commit=head)
        held = evidence_module.HeldVerifier(repo.root, binding)
        self.addCleanup(held.close)
        # The last revalidation passes against the exact bound bytes.
        self.assertEqual(
            held.revalidate(git=None, current_commit=head), content)
        # Post-revalidate (in the final revalidate->exec window) the pathname
        # is swapped to a different inode: the retained descriptor still
        # resolves the bound inode at exec time via /proc/self/fd, so the
        # child executes the old committed bytes and the swap never runs.
        verifier = repo.root / "scripts" / "verify.sh"
        verifier.rename(repo.root / "scripts" / "saved.sh")
        _write(verifier, b"#!/bin/sh\nprintf 'swapped-content\\n'\n")
        os.chmod(verifier, 0o755)
        argv, executable, pass_fds = held.spawn(command)
        self.assertEqual(executable, f"/proc/self/fd/{held.fd}")
        result = subprocess.run(
            argv, executable=executable, pass_fds=tuple(pass_fds),
            capture_output=True, text=True, cwd=repo.root,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        # F1 (empirical): the old held inode executes (the swap never ran),
        # the kernel's shebang dispatch makes the script's ``$0`` the fd path
        # ``/proc/self/fd/<fd>`` — never the canonical ``./scripts/verify.sh``
        # — and every command argument after the script path is preserved.
        self.assertIn("bound-content", result.stdout)
        self.assertNotIn("swapped", result.stdout)
        self.assertIn(f"zero=/proc/self/fd/{held.fd}", result.stdout)
        self.assertNotIn("zero=./scripts/verify.sh", result.stdout)
        self.assertIn("one=alpha", result.stdout)
        self.assertIn("two=beta", result.stdout)

    def test_production_shebang_swap_executes_old_inode(self) -> None:
        # Production-style: the committed ``#!/bin/sh`` verifier is bound
        # before the untrusted phase, the pathname is swapped in the final
        # revalidate->exec window, and the whole run goes through the real
        # RootLock.spawn_child boundary (close_fds, new session, sanitized
        # environment).  The old held inode executes with ``$0`` set to the
        # fd path and the command arguments preserved (F1).
        repo = RepoFixture(self.tmp)
        repo.write("docs/SPEC.md", "spec bytes\n")
        repo.write(".factory/artifacts/implementation-plan.md", "plan bytes\n")
        content = (
            b"#!/bin/sh\n"
            b"printf 'bound-content\\n'\n"
            b"printf 'zero=%s\\n' \"$0\"\n"
            b"printf 'arg=%s\\n' \"$1\"\n"
        )
        repo.write_verifier(content=content)
        head = repo.commit()
        spec_blob = _git(
            repo.root, "rev-parse", "HEAD:docs/SPEC.md").stdout.strip()
        plan_raw = (repo.root /
                    ".factory/artifacts/implementation-plan.md").read_bytes()
        with lock_module.acquire_root_lock(
            repo.root,
            expected_identity=state_module.repository_identity(repo.root),
            expected_branch=state_module.live_branch(repo.root),
            spec=lock_module.SpecBinding(
                path="docs/SPEC.md", commit=head, blob=spec_blob),
            plan=lock_module.PlanBinding(
                path=".factory/artifacts/implementation-plan.md",
                base_commit=head,
                digest=hashlib.sha256(plan_raw).hexdigest(),
            ),
        ) as root_lock:
            command = ["./scripts/verify.sh", "payload-arg"]
            binding = evidence_module.bind_verifier(
                repo.root, command, commit=head)
            held = evidence_module.HeldVerifier(repo.root, binding)
            self.addCleanup(held.close)
            held.revalidate(git=None, current_commit=head)
            # Swap the pathname after the last revalidation (the race window).
            verifier = repo.root / "scripts" / "verify.sh"
            verifier.rename(repo.root / "scripts" / "saved.sh")
            _write(verifier, b"#!/bin/sh\nprintf 'swapped-content\\n'\n")
            os.chmod(verifier, 0o755)
            argv, executable, pass_fds = held.spawn(command)
            self.assertEqual(tuple(pass_fds), (held.fd,))
            result = root_lock.spawn_child(
                argv, executable=executable, pass_fds=pass_fds,
                timeout=60,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("bound-content", result.stdout)
            self.assertNotIn("swapped", result.stdout)
            self.assertIn(f"zero=/proc/self/fd/{held.fd}", result.stdout)
            self.assertNotIn("zero=./scripts/verify.sh", result.stdout)
            self.assertIn("arg=payload-arg", result.stdout)

    def test_descriptor_chmod_fails_closed(self) -> None:
        repo = RepoFixture(self.tmp)
        repo.write_verifier()
        head = repo.commit()
        binding = evidence_module.bind_verifier(
            repo.root, ["./scripts/verify.sh"], commit=head)
        held = evidence_module.HeldVerifier(repo.root, binding)
        self.addCleanup(held.close)
        # The retained descriptor is made group-writable after binding: the
        # per-execution descriptor identity re-validation fails closed before
        # any child could execute it.
        os.fchmod(held.fd, 0o664)
        with self.assertRaises(evidence_module.VerifierBindingError):
            held.spawn(["./scripts/verify.sh"])

    def test_descriptor_hardlink_fails_closed(self) -> None:
        repo = RepoFixture(self.tmp)
        repo.write_verifier()
        head = repo.commit()
        binding = evidence_module.bind_verifier(
            repo.root, ["./scripts/verify.sh"], commit=head)
        held = evidence_module.HeldVerifier(repo.root, binding)
        self.addCleanup(held.close)
        # A second hardlink to the bound inode breaks the single-link
        # invariant: the descriptor identity check fails closed.
        os.link(repo.root / "scripts" / "verify.sh",
                repo.root / "scripts" / "alias.sh")
        with self.assertRaises(evidence_module.VerifierBindingError):
            held.revalidate(git=None, current_commit=head)
        with self.assertRaises(evidence_module.VerifierBindingError):
            held.spawn(["./scripts/verify.sh"])

    def test_root_lock_fd_not_inherited_by_verifier_child(self) -> None:
        repo = RepoFixture(self.tmp)
        repo.write_verifier(
            content=(
                b"#!/bin/sh\n"
                b"for fd in /proc/self/fd/*; do echo \"${fd##*/}\"; done\n"
            ),
        )
        repo.write("docs/SPEC.md", "spec bytes\n")
        repo.write(".factory/artifacts/implementation-plan.md", "plan bytes\n")
        head = repo.commit()
        spec_blob = _git(
            repo.root, "rev-parse", "HEAD:docs/SPEC.md").stdout.strip()
        plan_raw = (repo.root /
                    ".factory/artifacts/implementation-plan.md").read_bytes()
        with lock_module.acquire_root_lock(
            repo.root,
            expected_identity=state_module.repository_identity(repo.root),
            expected_branch=state_module.live_branch(repo.root),
            spec=lock_module.SpecBinding(
                path="docs/SPEC.md", commit=head, blob=spec_blob),
            plan=lock_module.PlanBinding(
                path=".factory/artifacts/implementation-plan.md",
                base_commit=head,
                digest=hashlib.sha256(plan_raw).hexdigest(),
            ),
        ) as root_lock:
            binding = evidence_module.bind_verifier(
                repo.root, ["./scripts/verify.sh"], commit=head)
            with evidence_module.HeldVerifier(repo.root, binding) as held:
                argv, executable, pass_fds = held.spawn(
                    ["./scripts/verify.sh"])
                self.assertEqual(tuple(pass_fds), (held.fd,))
                self.assertNotIn(root_lock.fd, tuple(pass_fds))
                result = root_lock.spawn_child(
                    argv, executable=executable, pass_fds=pass_fds,
                    timeout=60,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                child_fds = {
                    int(line)
                    for line in result.stdout.split()
                    if line.strip().isdigit()
                }
                # The child inherits the verifier descriptor but never the
                # canonical root lock descriptor (F8): the lock fd number is
                # absent from the child's descriptor table.
                self.assertIn(held.fd, child_fds)
                self.assertNotIn(root_lock.fd, child_fds)

    def test_verifier_child_fd_read_only_via_fdinfo_and_only_explicit_fd(self) -> None:
        """Task 12 MED1 (F2): the child's inherited verifier descriptor is
        read-only and is the ONLY extra descriptor past stdio.

        The child proves read-only-ness through ``/proc/self/fdinfo`` (the
        access-mode bits ``flags & O_ACCMODE`` must be ``O_RDONLY`` with a
        zero ``pos``) plus a functional write-rejection probe on the
        inherited descriptor itself.  The parent then inspects the child's
        descriptor table (``ls /proc/$$/fd`` names the shell's own table,
        with no self-referential readdir or substitution-pipe artifact) and
        requires that every descriptor beyond stdio is exactly the explicit
        verifier fd (the shell's own script alias, when present, is the
        only tolerated extra): the root-lock descriptor and every other
        holder alias are absent.
        """
        repo = RepoFixture(self.tmp)
        content = (
            b"#!/bin/sh\n"
            b"self=${0##*/}\n"                      # fd from $0=/proc/self/fd/<fd>
            b"case \"$self\" in ''|*[!0-9]*) echo \"bad-argv0:$0\" >&2; exit 7;; esac\n"
            b"info=$(cat \"/proc/self/fdinfo/$self\") || { echo \"no-fdinfo:$self\" >&2; exit 7; }\n"
            b"flags=$(printf '%s\\n' \"$info\" | sed -n 's/^flags:[[:space:]]*//p')\n"
            b"pos=$(printf '%s\\n' \"$info\" | sed -n 's/^pos:[[:space:]]*//p')\n"
            b"mode=$(( flags & 3 ))\n"
            b"[ \"$mode\" = 0 ] || { echo \"writable-fd:$self flags=$flags\" >&2; exit 8; }\n"
            b"[ \"${pos:-0}\" = 0 ] || { echo \"advanced-pos:$self pos=$pos\" >&2; exit 8; }\n"
            b"( printf x >&\"$self\" ) 2>/dev/null && { echo \"write-succeeded:$self\" >&2; exit 9; }\n"
            b"echo 'fdinfo-readonly-ok'\n"
            b"ls /proc/$$/fd\n"
        )
        repo.write_verifier(content=content)
        repo.write("docs/SPEC.md", "spec bytes\n")
        repo.write(".factory/artifacts/implementation-plan.md", "plan bytes\n")
        head = repo.commit()
        spec_blob = _git(
            repo.root, "rev-parse", "HEAD:docs/SPEC.md").stdout.strip()
        plan_raw = (repo.root /
                    ".factory/artifacts/implementation-plan.md").read_bytes()
        with lock_module.acquire_root_lock(
            repo.root,
            expected_identity=state_module.repository_identity(repo.root),
            expected_branch=state_module.live_branch(repo.root),
            spec=lock_module.SpecBinding(
                path="docs/SPEC.md", commit=head, blob=spec_blob),
            plan=lock_module.PlanBinding(
                path=".factory/artifacts/implementation-plan.md",
                base_commit=head,
                digest=hashlib.sha256(plan_raw).hexdigest(),
            ),
        ) as root_lock:
            binding = evidence_module.bind_verifier(
                repo.root, ["./scripts/verify.sh"], commit=head)
            with evidence_module.HeldVerifier(repo.root, binding) as held:
                argv, executable, pass_fds = held.spawn(
                    ["./scripts/verify.sh"])
                # The only passed descriptor is the explicit verifier fd;
                # the root lock descriptor is never passed.
                self.assertEqual(tuple(pass_fds), (held.fd,))
                self.assertNotIn(root_lock.fd, tuple(pass_fds))
                result = root_lock.spawn_child(
                    argv, executable=executable, pass_fds=pass_fds,
                    timeout=60,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("fdinfo-readonly-ok", result.stdout)
                # The child itself proved the inherited verifier descriptor
                # is read-only through /proc/self/fdinfo (O_RDONLY access
                # mode, zero position) and by a failed write attempt.
                self.assertNotIn("writable-fd:", result.stderr)
                self.assertNotIn("advanced-pos:", result.stderr)
                self.assertNotIn("write-succeeded:", result.stderr)
                # The child's descriptor table (its own ``ls /proc/$$/fd``
                # enumeration) must contain the explicit verifier fd and no
                # root-lock or other holder alias: every fd beyond stdio is
                # either the verifier fd or the shell's own script alias.
                child_fds = {
                    int(line)
                    for line in result.stdout.splitlines()
                    if line.strip().isdigit()
                }
                self.assertIn(held.fd, child_fds)
                self.assertNotIn(root_lock.fd, child_fds)
                extra = child_fds - {0, 1, 2}
                self.assertTrue(extra.issubset({held.fd, 255}), extra)


class ConfigCommandTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="factory-evidence-config."))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def test_config_command_parsed_from_committed_config(self) -> None:
        repo = RepoFixture(self.tmp)
        repo.write_verifier()
        repo.write_config(["./scripts/verify.sh"])
        repo.commit()
        command = evidence_module._config_command(repo.root)
        self.assertEqual(command, ("./scripts/verify.sh",))

    def test_config_command_malformed_fails_closed(self) -> None:
        cases = [
            ("empty", []),
            ("blank-arg", ["./scripts/verify.sh", ""]),
            ("nul-arg", ["./scripts/verify\x00.sh"]),
            ("not-a-list", "argv-array"),
            ("number", 42),
            ("none-arg", [None]),
        ]
        for index, (label, command) in enumerate(cases):
            repo = RepoFixture(self.tmp, name=f"bad-{index}-{label}")
            repo.write_config(command)
            repo.commit()
            with self.assertRaises(evidence_module.EvidenceError):
                evidence_module._config_command(repo.root)

    def test_config_command_unsafe_config_file_fails(self) -> None:
        repo = RepoFixture(self.tmp, name="unsafe-config")
        repo.write_config(["./scripts/verify.sh"])
        repo.commit()
        os.chmod(repo.root / ".factory" / "config.toml", 0o664)
        with self.assertRaises(evidence_module.EvidenceError):
            evidence_module._config_command(repo.root)

    def test_config_command_missing_config_fails(self) -> None:
        repo = RepoFixture(self.tmp, name="no-config")
        repo.commit()
        with self.assertRaises(evidence_module.EvidenceError):
            evidence_module._config_command(repo.root)

    def test_config_command_committed_mismatch_fails(self) -> None:
        # LOW5: the committed blob is the authoritative config; an uncommitted
        # worktree substitution (a campaign_command pointing at an attacker
        # script) fails closed before any verifier binding.
        repo = RepoFixture(self.tmp, name="mismatch-config")
        repo.write_verifier()
        repo.write_config(["./scripts/verify.sh"])
        repo.commit()
        repo.write_config(["./scripts/verify.sh", "--evil-flag"])
        with self.assertRaises(evidence_module.EvidenceError):
            evidence_module._config_command(repo.root)


# ---------------------------------------------------------------------------
# Hardened no-follow reads (metadata, symlink/hardlink/owner/mode)
# ---------------------------------------------------------------------------


class SecureReadTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="factory-evidence-secure."))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.file = self.tmp / "artifact.json"
        _write(self.file, b'{"ok": true}\n')
        os.chmod(self.file, 0o600)

    def read(self) -> bytes:
        raw, _info = evidence_module.secure_read_bytes(
            self.file, maximum=1024 * 1024, what="fixture")
        return raw

    def test_secure_read_happy_path(self) -> None:
        self.assertEqual(self.read(), b'{"ok": true}\n')

    def test_symlink_fails(self) -> None:
        self.file.unlink()
        _write(self.tmp / "real.json", b'{"ok": true}\n')
        self.file.symlink_to(self.tmp / "real.json")
        with self.assertRaises(evidence_module.VerifierBindingError):
            self.read()

    def test_hardlink_alias_fails(self) -> None:
        os.link(self.file, self.tmp / "alias.json")
        with self.assertRaises(evidence_module.VerifierBindingError):
            self.read()

    def test_group_writable_fails(self) -> None:
        os.chmod(self.file, 0o664)
        with self.assertRaises(evidence_module.VerifierBindingError):
            self.read()

    def test_other_writable_fails(self) -> None:
        os.chmod(self.file, 0o602)
        with self.assertRaises(evidence_module.VerifierBindingError):
            self.read()

    def test_foreign_owner_fails(self) -> None:
        with unittest.mock.patch("os.getuid", return_value=os.getuid() + 1):
            with self.assertRaises(evidence_module.VerifierBindingError):
                self.read()

    def test_oversized_fails(self) -> None:
        _write(self.file, b"x" * (1024 * 1024 + 1))
        with self.assertRaises(evidence_module.VerifierBindingError):
            evidence_module.secure_read_bytes(
                self.file, maximum=1024, what="fixture")

    def test_json_decode_failures(self) -> None:
        _write(self.file, b"not json")
        with self.assertRaises(evidence_module.ReceiptError):
            evidence_module.secure_json(self.tmp, "artifact.json")
        _write(self.file, b"[1, 2]")
        with self.assertRaises(evidence_module.ReceiptError):
            evidence_module.secure_json(self.tmp, "artifact.json")


# ---------------------------------------------------------------------------
# Receipt validation and adjacent-log metadata hardening
# ---------------------------------------------------------------------------


class ReceiptValidationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="factory-evidence-receipt."))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.root = self.tmp / "workspace"
        _mkdir(self.root)
        self.fixture = ReceiptFixture(self.root)
        self.ref = f"{STATE_DIR}/audit-receipts/probe.json"

    def test_valid_receipt_passes(self) -> None:
        data = self.fixture.write("probe")
        validated = evidence_module.validate_receipt(self.root, self.ref)
        self.assertEqual(validated["tag"], "probe")
        self.assertEqual(validated["coordinator_round"], 1)
        self.assertEqual(validated["evidence_commit"], self.fixture.base)
        self.assertEqual(len(data["coordinator_nonce"]), 64)

    def test_adjacent_log_digest_mismatch_fails(self) -> None:
        self.fixture.write("probe")
        self.fixture.tamper_log("probe", "stdout")
        with self.assertRaises(evidence_module.ReceiptError):
            evidence_module.validate_receipt(self.root, self.ref)

    def test_adjacent_stderr_digest_mismatch_fails(self) -> None:
        self.fixture.write("probe", stderr=b"errors\n")
        self.fixture.tamper_log("probe", "stderr", content=b"changed\n")
        with self.assertRaises(evidence_module.ReceiptError):
            evidence_module.validate_receipt(self.root, self.ref)

    def test_argv_digest_mismatch_fails(self) -> None:
        data = self.fixture.write("probe", argv=("true",))
        data["argv"] = ["false"]
        _write(self.fixture.recs / "probe.json", json.dumps(data) + "\n")
        with self.assertRaises(evidence_module.ReceiptError):
            evidence_module.validate_receipt(self.root, self.ref)

    def test_receipt_schema_invalid_fails(self) -> None:
        data = self.fixture.write("probe")
        data["schema"] = "wrong"
        _write(self.fixture.recs / "probe.json", json.dumps(data) + "\n")
        with self.assertRaises(evidence_module.ReceiptError):
            evidence_module.validate_receipt(self.root, self.ref)

    def test_receipt_extra_field_fails(self) -> None:
        data = self.fixture.write("probe")
        data["extra"] = True
        _write(self.fixture.recs / "probe.json", json.dumps(data) + "\n")
        with self.assertRaises(evidence_module.ReceiptError):
            evidence_module.validate_receipt(self.root, self.ref)

    def test_receipt_missing_coordinator_binding_fails(self) -> None:
        data = self.fixture.write("probe")
        del data["coordinator_nonce"]
        _write(self.fixture.recs / "probe.json", json.dumps(data) + "\n")
        with self.assertRaises(evidence_module.ReceiptError):
            evidence_module.validate_receipt(self.root, self.ref)

    def test_receipt_exit_code_and_bindings_fail_closed(self) -> None:
        mutations = (
            ("exit_code", "0"),
            ("exit_code", True),
            ("evidence_commit", "xyz"),
            ("evidence_commit", "a" * 39),
            ("coordinator_round", 0),
            ("coordinator_round", -1),
            ("coordinator_round", True),
            ("coordinator_nonce", "short"),
            ("stdout_sha256", "bad"),
        )
        for key, value in mutations:
            data = self.fixture.write("probe")
            data[key] = value
            _write(self.fixture.recs / "probe.json", json.dumps(data) + "\n")
            with self.assertRaises(evidence_module.ReceiptError):
                evidence_module.validate_receipt(self.root, self.ref)

    def test_symlinked_receipt_json_fails(self) -> None:
        self.fixture.write("probe")
        json_path = self.fixture.recs / "probe.json"
        json_path.unlink()
        _write(self.fixture.recs / "real.json", b"{}")
        json_path.symlink_to(self.fixture.recs / "real.json")
        with self.assertRaises(evidence_module.ReceiptError):
            evidence_module.validate_receipt(self.root, self.ref)

    def test_symlinked_stdout_fails(self) -> None:
        self.fixture.write("probe")
        out = self.fixture.recs / "probe.stdout"
        out.unlink()
        _write(self.fixture.recs / "real.out", b"runtime output\n")
        out.symlink_to(self.fixture.recs / "real.out")
        with self.assertRaises(evidence_module.ReceiptError):
            evidence_module.validate_receipt(self.root, self.ref)

    def test_hardlinked_stdout_fails(self) -> None:
        self.fixture.write("probe")
        os.link(self.fixture.recs / "probe.stdout",
                self.fixture.recs / "probe.stdout.alias")
        with self.assertRaises(evidence_module.ReceiptError):
            evidence_module.validate_receipt(self.root, self.ref)

    def test_group_writable_stdout_fails(self) -> None:
        self.fixture.write("probe")
        os.chmod(self.fixture.recs / "probe.stdout", 0o664)
        with self.assertRaises(evidence_module.ReceiptError):
            evidence_module.validate_receipt(self.root, self.ref)

    def test_foreign_owner_stdout_fails(self) -> None:
        self.fixture.write("probe")
        with unittest.mock.patch("os.getuid", return_value=os.getuid() + 1):
            with self.assertRaises(evidence_module.ReceiptError):
                evidence_module.validate_receipt(self.root, self.ref)

    def test_escaping_reference_fails(self) -> None:
        self.fixture.write("probe")
        for ref in (
            str(self.fixture.recs / "probe.json"),
            f"{STATE_DIR}/../.factory-state/audit-receipts/probe.json",
            "audit-receipts/probe.json",
        ):
            with self.assertRaises(evidence_module.ReceiptError):
                evidence_module.validate_receipt(self.root, ref)

    def test_missing_receipt_fails(self) -> None:
        with self.assertRaises(evidence_module.ReceiptError):
            evidence_module.validate_receipt(
                self.root, f"{STATE_DIR}/audit-receipts/ghost.json")


# ---------------------------------------------------------------------------
# Evidence lines: PASS exit 0, BLOCKED -> findings, refs, tiers, staleness
# ---------------------------------------------------------------------------


class EvidenceLinesTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="factory-evidence-lines."))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.root = _mkdir(self.tmp / "workspace")
        self.fixture = ReceiptFixture(self.root)
        self.fixture.write("probe", exit_code=0)
        self.fixture.write("failed", exit_code=2, stdout=b"boom\n")
        self.ref = f"{STATE_DIR}/audit-receipts/probe.json"
        self.failed_ref = f"{STATE_DIR}/audit-receipts/failed.json"

    def lines(self, result: str, *evidence: str) -> str:
        return self.fixture.report(result, list(evidence))

    def test_pass_exit0_valid(self) -> None:
        text = self.lines("pass", self.fixture.line(ref=self.ref))
        count = evidence_module.validate_evidence_text(
            text, root=self.root, result="pass",
            expected_round=1, expected_base=self.fixture.base,
            expected_nonce=self.fixture.nonce,
        )
        self.assertEqual(count, 1)

    def test_pass_requires_exit0(self) -> None:
        text = self.lines("pass", self.fixture.line(ref=self.failed_ref))
        with self.assertRaises(evidence_module.ReceiptError):
            evidence_module.validate_evidence_text(
                text, root=self.root, result="pass",
                expected_round=1, expected_base=self.fixture.base)

    def test_fail_requires_nonzero_exit(self) -> None:
        text = self.lines("findings", self.fixture.line(
            marker="FAIL", ref=self.failed_ref))
        evidence_module.validate_evidence_text(
            text, root=self.root, result="findings",
            expected_round=1, expected_base=self.fixture.base)
        text2 = self.lines("findings", self.fixture.line(
            marker="FAIL", ref=self.ref))
        with self.assertRaises(evidence_module.ReceiptError):
            evidence_module.validate_evidence_text(
                text2, root=self.root, result="findings",
                expected_round=1, expected_base=self.fixture.base)

    def test_model_prose_cannot_elevate(self) -> None:
        text = self.lines("pass", "`./verify-project` PASS with all checks green")
        with self.assertRaises(evidence_module.ReceiptError):
            evidence_module.validate_evidence_text(
                text, root=self.root, result="pass")

    def test_prose_tier_claim_never_elevates(self) -> None:
        text = self.lines(
            "pass", "`cmd` PASS tier=real_system (no receipt/manifest)")
        with self.assertRaises(evidence_module.ReceiptError):
            evidence_module.validate_evidence_text(
                text, root=self.root, result="pass")

    def test_blocked_forces_findings(self) -> None:
        text = self.lines(
            "pass", "`real probe` BLOCKED (no real system service available)")
        with self.assertRaises(evidence_module.ReceiptError):
            evidence_module.validate_evidence_text(
                text, root=self.root, result="pass")
        text2 = self.lines(
            "findings", "`real probe` BLOCKED (no real system service available)")
        count = evidence_module.validate_evidence_text(
            text2, root=self.root, result="findings")
        self.assertEqual(count, 1)

    def test_evidence_line_without_marker_fails(self) -> None:
        text = self.lines("pass", f"`cmd` [receipt: {self.ref}]")
        with self.assertRaises(evidence_module.ReceiptError):
            evidence_module.validate_evidence_text(
                text, root=self.root, result="pass")

    def test_no_evidence_section_fails(self) -> None:
        with self.assertRaises(evidence_module.ReceiptError):
            evidence_module.validate_evidence_text(
                "# Audit\nNo evidence here.\n", root=self.root,
                result="pass")

    def test_unsupported_result_fails(self) -> None:
        with self.assertRaises(evidence_module.ReceiptError):
            evidence_module.validate_evidence_text(
                "## Evidence reviewed\n", root=self.root,
                result="blocked")

    def test_stale_round_fails(self) -> None:
        text = self.lines("pass", self.fixture.line(ref=self.ref))
        with self.assertRaises(evidence_module.ReceiptError):
            evidence_module.validate_evidence_text(
                text, root=self.root, result="pass",
                expected_round=2, expected_base=self.fixture.base)

    def test_stale_commit_fails(self) -> None:
        text = self.lines("pass", self.fixture.line(ref=self.ref))
        with self.assertRaises(evidence_module.ReceiptError):
            evidence_module.validate_evidence_text(
                text, root=self.root, result="pass",
                expected_round=1, expected_base="1" * 40)

    def test_stale_nonce_fails(self) -> None:
        text = self.lines("pass", self.fixture.line(ref=self.ref))
        with self.assertRaises(evidence_module.ReceiptError):
            evidence_module.validate_evidence_text(
                text, root=self.root, result="pass",
                expected_round=1, expected_base=self.fixture.base,
                expected_nonce="d" * 64)

    def test_tier_ceiling_table(self) -> None:
        self.assertEqual(
            evidence_module.TIERS,
            ["unit", "simulated", "private_integration", "installed",
             "real_system", "human"],
        )
        self.assertEqual(
            evidence_module.evidence_tier_ceiling("receipt"),
            evidence_module.TIER_INDEX["installed"])
        self.assertEqual(
            evidence_module.evidence_tier_ceiling("manifest"),
            evidence_module.TIER_INDEX["real_system"])
        self.assertEqual(evidence_module.evidence_tier_ceiling("prose"), -1)

    def test_receipt_tier_claims_within_ceiling(self) -> None:
        for tier in ("unit", "simulated", "private_integration", "installed"):
            text = self.lines(
                "pass", self.fixture.line(ref=self.ref, tier=tier))
            evidence_module.validate_evidence_text(
                text, root=self.root, result="pass",
                expected_round=1, expected_base=self.fixture.base,
                expected_nonce=self.fixture.nonce)

    def test_receipt_tier_claim_cannot_elevate(self) -> None:
        for tier in ("real_system", "human"):
            text = self.lines(
                "pass", self.fixture.line(ref=self.ref, tier=tier))
            with self.assertRaises(evidence_module.TierError):
                evidence_module.validate_evidence_text(
                    text, root=self.root, result="pass",
                    expected_round=1, expected_base=self.fixture.base)

    def test_unknown_tier_claim_fails(self) -> None:
        text = self.lines("pass", self.fixture.line(ref=self.ref, tier="bogus"))
        with self.assertRaises(evidence_module.TierError):
            evidence_module.validate_evidence_text(
                text, root=self.root, result="pass",
                expected_round=1, expected_base=self.fixture.base)

    def test_manifest_tier_ceiling_with_patched_signer(self) -> None:
        # Without real signing keys the strict runner helper cannot accept a
        # manifest, so the manifest-tier semantics are tested through the
        # validated channel by stubbing the signer acceptance only.
        ref = ".factory-state/runner-evidence/fake-runner/manifest.json"
        base = "a" * 40
        with unittest.mock.patch.object(
            evidence_module, "validate_manifest_ref", return_value=None
        ):
            text = self.lines("pass", f"`cmd` PASS [manifest: {ref}] tier=real_system")
            evidence_module.validate_evidence_text(
                text, root=self.root, result="pass",
                expected_base=base)
            text2 = self.lines(
                "pass", f"`cmd` PASS [manifest: {ref}] tier=human")
            with self.assertRaises(evidence_module.TierError):
                evidence_module.validate_evidence_text(
                    text2, root=self.root, result="pass",
                    expected_base=base)

    def test_manifest_requires_base_binding(self) -> None:
        ref = ".factory-state/runner-evidence/fake-runner/manifest.json"
        text = self.lines("pass", f"`cmd` PASS [manifest: {ref}]")
        with self.assertRaises(evidence_module.ReceiptError):
            evidence_module.validate_evidence_text(
                text, root=self.root, result="pass")

    def test_fail_cannot_cite_manifest(self) -> None:
        ref = ".factory-state/runner-evidence/fake-runner/manifest.json"
        text = self.lines("findings", f"`cmd` FAIL [manifest: {ref}]")
        with self.assertRaises(evidence_module.ReceiptError):
            evidence_module.validate_evidence_text(
                text, root=self.root, result="findings",
                expected_base="a" * 40)

    def test_human_never_machine_claimed(self) -> None:
        # A human acceptance claim on a receipt channel is always rejected.
        text = self.lines(
            "pass", self.fixture.line(ref=self.ref, tier="human"))
        with self.assertRaises(evidence_module.TierError):
            evidence_module.validate_evidence_text(
                text, root=self.root, result="pass",
                expected_round=1, expected_base=self.fixture.base)
        # On the manifest channel the strict signer acceptance is stubbed so
        # the tier-ceiling semantics (human is never machine-claimable) are
        # what is under test.
        ref = ".factory-state/runner/manifest.json"
        with unittest.mock.patch.object(
            evidence_module, "validate_manifest_ref", return_value=None
        ):
            text2 = self.lines(
                "pass", f"`cmd` PASS [manifest: {ref}] tier=human")
            with self.assertRaises(evidence_module.TierError):
                evidence_module.validate_evidence_text(
                    text2, root=self.root, result="pass",
                    expected_base=self.fixture.base)

    def test_exact_ref_whitespace_fails(self) -> None:
        text = self.lines("pass", self.fixture.line(ref=self.ref + " "))
        with self.assertRaises(evidence_module.ReceiptError):
            evidence_module.validate_evidence_text(
                text, root=self.root, result="pass")

    def test_ref_digest_is_exact(self) -> None:
        # A receipt whose stdout digest field is altered is caught even when
        # the transcript itself is unchanged.
        data = self.fixture.write("digest")
        data["stdout_sha256"] = "f" * 64
        _write(self.fixture.recs / "digest.json", json.dumps(data) + "\n")
        text = self.lines(
            "pass", self.fixture.line(
                ref=f"{STATE_DIR}/audit-receipts/digest.json"))
        with self.assertRaises(evidence_module.ReceiptError):
            evidence_module.validate_evidence_text(
                text, root=self.root, result="pass",
                expected_round=1, expected_base=self.fixture.base)

    # -- Task 12 LOW4: anchored whole-token status grammar -------------------

    def test_path_status_words_are_not_status_tokens(self) -> None:
        # A status word embedded in a command/path is never a status token:
        # only the first whitespace-delimited token that is exactly PASS, FAIL
        # or BLOCKED decides the status.
        parsed = evidence_module.parse_evidence_lines(
            "- Executable evidence: ./tools/PASS-gate ./run FAIL")
        self.assertEqual(parsed[0]["marker"], "FAIL")
        self.assertEqual(parsed[0]["command"], "./tools/PASS-gate ./run")

    def test_path_blocked_word_is_not_a_status_or_standalone(self) -> None:
        # A BLOCKED path word is neither a status token nor a standalone
        # BLOCKED marker; the line has no anchored status and fails closed.
        with self.assertRaises(evidence_module.ReceiptError):
            evidence_module.parse_evidence_lines(
                "- Executable evidence: ./probes/BLOCKED-scan run")

    def test_blocked_path_word_does_not_force_findings(self) -> None:
        # LOW4: only the exact standalone evidence grammar forces findings; a
        # BLOCKED-looking citation path is not evidence of a blocked probe.
        self.fixture.write("BLOCKED-probe", exit_code=0)
        text = self.lines(
            "pass",
            self.fixture.line(
                ref=f"{STATE_DIR}/audit-receipts/BLOCKED-probe.json"),
        )
        count = evidence_module.validate_evidence_text(
            text, root=self.root, result="pass",
            expected_round=1, expected_base=self.fixture.base,
            expected_nonce=self.fixture.nonce,
        )
        self.assertEqual(count, 1)

    def test_standalone_blocked_anywhere_forces_findings(self) -> None:
        # LOW4: a standalone BLOCKED token anywhere in the report is evidence
        # grammar and forces result: findings even when the evidence section
        # lines themselves are clean.
        text = self.lines("pass", self.fixture.line(ref=self.ref))
        text += "\nA standalone BLOCKED marker in prose.\n"
        with self.assertRaises(evidence_module.ReceiptError):
            evidence_module.validate_evidence_text(
                text, root=self.root, result="pass",
                expected_round=1, expected_base=self.fixture.base)

    # -- Task 12 LOW5: exact receipt argv and coordinator-only minting ------

    def test_exact_command_mismatch_fails(self) -> None:
        # A receipt certifies only the exact command the coordinator executed:
        # an evidence line that relabels or abbreviates the command fails
        # closed even with a valid citation.
        text = self.lines(
            "pass",
            self.fixture.line(
                argv=("sh", "-c", "echo different"), ref=self.ref))
        with self.assertRaises(evidence_module.ReceiptError):
            evidence_module.validate_evidence_text(
                text, root=self.root, result="pass",
                expected_round=1, expected_base=self.fixture.base,
                expected_nonce=self.fixture.nonce)


# ---------------------------------------------------------------------------
# Manifest signature verifier (fail-closed without real signing keys)
# ---------------------------------------------------------------------------


class ManifestValidationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="factory-evidence-manifest."))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def build(self, *, enabled: bool = False) -> tuple[Path, str, str]:
        root = _mkdir(self.tmp / "runner-ws")
        fixture = ManifestFixture(root)
        if enabled:
            fixture.trust["enabled"] = True
            fixture.trust["public_keys"] = [
                {"principal": "factory-signer",
                 "public_key": ManifestFixture.FAKE_PUBLIC_KEY},
            ]
            fixture.trust["allowed_principals"] = ["factory-signer"]
        head = fixture.commit()
        manifest_path = fixture.manifest_record(head)
        ref = manifest_path.relative_to(root).as_posix()
        return root, head, ref

    def test_no_signer_provisioned_rejects_all_manifests(self) -> None:
        root, head, ref = self.build(enabled=False)
        with self.assertRaises(evidence_module.ReceiptError):
            evidence_module.validate_manifest_ref(root, ref, head)

    def test_unsigned_manifest_fails(self) -> None:
        root, head, ref = self.build(enabled=True)
        (root / ref).parent.joinpath("manifest.sig").unlink()
        with self.assertRaises(evidence_module.ReceiptError):
            evidence_module.validate_manifest_ref(root, ref, head)

    def test_fabricated_signature_fails(self) -> None:
        root, head, ref = self.build(enabled=True)
        with self.assertRaises(evidence_module.ReceiptError):
            evidence_module.validate_manifest_ref(root, ref, head)

    def test_malformed_manifest_fails(self) -> None:
        root, head, ref = self.build(enabled=True)
        manifest_path = root / ref
        data = json.loads(manifest_path.read_text())
        del data["tree"]
        _write(manifest_path, json.dumps(data) + "\n")
        with self.assertRaises(evidence_module.ReceiptError):
            evidence_module.validate_manifest_ref(root, ref, head)

    def test_standalone_manifest_not_in_aggregate_fails(self) -> None:
        root, head, ref = self.build(enabled=False)
        _mkdir(root / ".factory-state/runner-manifests")
        _write(root / ".factory-state/runner-manifests/manifest.json",
               '{"schema": "factory-runner-receipt/v1", "result": "pass"}\n')
        with self.assertRaises(evidence_module.ReceiptError):
            evidence_module.validate_manifest_ref(
                root, ".factory-state/runner-manifests/manifest.json", head)

    def test_stale_aggregate_binding_fails(self) -> None:
        root, head, ref = self.build(enabled=False)
        with self.assertRaises(evidence_module.ReceiptError):
            evidence_module.validate_manifest_ref(root, ref, "1" * 40)

    def test_non_hex_expected_commit_fails(self) -> None:
        root, head, ref = self.build(enabled=False)
        with self.assertRaises(evidence_module.ReceiptError):
            evidence_module.validate_manifest_ref(root, ref, "xyz")

    def test_checker_cli_rejects_without_real_keys(self) -> None:
        root, head, ref = self.build(enabled=False)
        result = run(
            [sys.executable, str(root / "scripts/check-factory-runner-evidence.py"),
             "--verify-manifest", ref, "--expected-commit", head],
            root=root, check=False,
        )
        self.assertNotEqual(result.returncode, 0)


# ---------------------------------------------------------------------------
# Task 12 MED2: pinned immutable executable boundary (no real keys/runners)
# ---------------------------------------------------------------------------


class PinnedExecutableBoundaryTests(unittest.TestCase):
    """MED2: trusted Git/ssh-keygen reads use a pinned absolute immutable
    executable and a sanitized environment; a fake PATH git/ssh-keygen and
    hostile GIT_DIR/GIT_CONFIG_* cannot redirect the runner validator,
    the campaign-audit initializer, or the campaign-audit validator.
    Resolution fails closed without a usable immutable candidate, and the
    complete ``GIT_CONFIG*`` family plus redirector keys are stripped from
    every trusted invocation.  No real signing keys or runners are used.
    """

    @classmethod
    def setUpClass(cls) -> None:
        spec = importlib.util.spec_from_file_location(
            "factory_runner_evidence_checker", str(CHECK_RUNNER_EVIDENCE))
        cls.checker = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        sys.modules[spec.name] = cls.checker
        try:
            spec.loader.exec_module(cls.checker)
        except SystemExit as exc:
            raise unittest.SkipTest(
                f"no pinned immutable git/ssh-keygen on this host: {exc}")

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="factory-evidence-pinned."))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    # -- pinned resolution -----------------------------------------------------

    def test_pinned_executables_never_consult_path(self) -> None:
        fake_bin = _mkdir(self.tmp / "fake-bin")
        for name in ("git", "ssh-keygen"):
            fake = fake_bin / name
            _write(fake, b"#!/bin/sh\nexit 42\n")
            os.chmod(fake, 0o755)
        with unittest.mock.patch.dict(
            os.environ,
            {"PATH": str(fake_bin) + os.pathsep + os.environ.get("PATH", "")},
        ):
            git_resolved = self.checker._resolve_pinned("git")
            keygen_resolved = self.checker._resolve_pinned("ssh-keygen")
        self.assertTrue(git_resolved.startswith("/"))
        self.assertTrue(keygen_resolved.startswith("/"))
        self.assertNotIn(str(fake_bin), git_resolved)
        self.assertNotIn(str(fake_bin), keygen_resolved)
        self.assertEqual(git_resolved, self.checker.GIT_EXECUTABLE)

    def test_pinned_resolution_fails_closed_without_candidates(self) -> None:
        with unittest.mock.patch.object(
            self.checker, "FIXED_BIN_CANDIDATES", ()
        ):
            with unittest.mock.patch.object(
                self.checker, "NIX_STORE_BIN_GLOB", "/nonexistent-factory/*/bin"
            ):
                with self.assertRaises(SystemExit):
                    self.checker._resolve_pinned("git")
                with self.assertRaises(SystemExit):
                    self.checker._resolve_pinned("ssh-keygen")

    def test_immutable_chain_rejects_caller_owned_component(self) -> None:
        caller_bin = _mkdir(self.tmp / "caller-bin")
        binary = _write(caller_bin / "git", b"#!/bin/sh\n")
        os.chmod(binary, 0o755)
        with self.assertRaises(SystemExit):
            self.checker._immutable_chain(str(binary))

    def test_sanitized_git_environment_strips_override_keys(self) -> None:
        environment = dict(os.environ)
        for key in (
            "GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE",
            "GIT_OBJECT_DIRECTORY", "GIT_ALTERNATE_OBJECT_DIRECTORIES",
            "GIT_COMMON_DIR", "GIT_NAMESPACE", "GIT_CONFIG",
            "GIT_CONFIG_GLOBAL", "GIT_CONFIG_SYSTEM", "GIT_CONFIG_COUNT",
            "GIT_CONFIG_PARAMETERS", "GIT_SSH_COMMAND", "GIT_ASKPASS",
        ):
            environment[key] = "evil"
        stripped = self.checker.sanitized_git_environment()
        # The full GIT_CONFIG* family and every redirector key are gone.
        for key in tuple(environment):
            if key == "GIT_CONFIG" or key.startswith("GIT_CONFIG_") \
                    or key in self.checker.GIT_ENV_STRIP:
                self.assertNotIn(key, stripped)

    # -- hostile env cannot redirect the runner validator ---------------------

    def _build_manifest_fixture(self, *, enabled: bool = False):
        root = _mkdir(self.tmp / "runner-ws")
        fixture = ManifestFixture(root)
        if enabled:
            fixture.trust["enabled"] = True
            fixture.trust["public_keys"] = [
                {"principal": "factory-signer",
                 "public_key": ManifestFixture.FAKE_PUBLIC_KEY},
            ]
            fixture.trust["allowed_principals"] = ["factory-signer"]
        head = fixture.commit()
        manifest_path = fixture.manifest_record(head)
        ref = manifest_path.relative_to(root).as_posix()
        return root, head, ref

    def _fake_bin(self) -> Path:
        """Fake PATH binaries that record any invocation (never authorized)."""
        fake_bin = _mkdir(self.tmp / "fake-bin")
        marker = self.tmp / "fake-invoked"
        for name in ("git", "ssh-keygen"):
            _write(
                fake_bin / name,
                (
                    "#!/bin/sh\n"
                    f"echo '{name}' >> {marker}\n"
                    f"echo fake-{name}-invoked >&2\n"
                    "exit 42\n"
                ).encode(),
            )
            os.chmod(fake_bin / name, 0o755)
        return fake_bin

    def _hostile_env(self, fake_bin: Path) -> dict:
        attacker = _mkdir(self.tmp / "attacker")
        _write(attacker / "config", b"[core]\n\tfake = true\n")
        env = dict(os.environ)
        env["PATH"] = str(fake_bin) + os.pathsep + env.get("PATH", "")
        env["GIT_DIR"] = str(attacker)
        env["GIT_WORK_TREE"] = str(attacker)
        env["GIT_INDEX_FILE"] = str(attacker / "index")
        env["GIT_OBJECT_DIRECTORY"] = str(attacker)
        env["GIT_ALTERNATE_OBJECT_DIRECTORIES"] = str(attacker)
        env["GIT_CONFIG_GLOBAL"] = str(attacker / "config")
        env["GIT_CONFIG_SYSTEM"] = str(attacker / "config")
        env["GIT_CONFIG_COUNT"] = "1"
        env["GIT_CONFIG_KEY_0"] = "core.fake"
        env["GIT_CONFIG_VALUE_0"] = "evil"
        env["GIT_CONFIG_PARAMETERS"] = "'core.fake=evil'"
        return env

    def test_runner_validator_not_redirected_by_fake_path(self) -> None:
        fake_bin = self._fake_bin()
        marker = self.tmp / "fake-invoked"
        root, head, ref = self._build_manifest_fixture(enabled=False)
        base_command = [
            sys.executable,
            str(root / "scripts/check-factory-runner-evidence.py"),
            "--verify-manifest", ref, "--expected-commit", head,
        ]
        clean = run(base_command, root=root, check=False, env=None)
        hostile = run(base_command, root=root, check=False,
                      env=self._hostile_env(fake_bin))
        # No signer is provisioned: the validator fails for trust reasons
        # exactly as in a clean environment — the fake PATH git/ssh-keygen
        # and the hostile GIT_* overrides can never redirect it.
        self.assertNotEqual(clean.returncode, 0)
        self.assertEqual(hostile.returncode, clean.returncode)
        self.assertEqual(hostile.stderr, clean.stderr)
        self.assertFalse(marker.exists())

    def test_fake_ssh_keygen_cannot_redirect_signature_verification(self) -> None:
        fake_bin = self._fake_bin()
        marker = self.tmp / "fake-invoked"
        root, head, ref = self._build_manifest_fixture(enabled=True)
        result = run(
            [sys.executable,
             str(root / "scripts/check-factory-runner-evidence.py"),
             "--verify-manifest", ref, "--expected-commit", head],
            root=root, check=False, env=self._hostile_env(fake_bin),
        )
        # The fabricated signature fails the real pinned ssh-keygen; the fake
        # ssh-keygen first in PATH is never invoked.
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(marker.exists())
        self.assertNotIn("fake-ssh-keygen-invoked", result.stderr)

    # -- the campaign-audit coordinator is not redirectable either ------------

    def test_initialize_campaign_audit_not_redirected(self) -> None:
        fake_bin = self._fake_bin()
        marker = self.tmp / "fake-invoked"
        root = _mkdir(self.tmp / "coordinator-ws")
        for rel in (".factory/artifacts", ".ralph/agent",
                    ".factory-state", "scripts"):
            _mkdir(root / rel)
        _write(root / ".factory/artifacts/implementation-plan.md",
               "# fixture plan\n")
        _write(root / ".factory/environment.toml",
               "schema_version = 1\n[[runners]]\nname = \"x\"\n"
               "capabilities = []\nverify_argv = []\n")
        shutil.copy2(INITIALIZE_CAMPAIGN_AUDIT,
                     root / "scripts" / "initialize-campaign-audit.py")
        _git(root, "init", "-q", "-b", "develop")
        _git(root, "config", "user.email", "fixture@test")
        _git(root, "config", "user.name", "fixture")
        _git(root, "add", "-A")
        _git(root, "commit", "-qm", "fixture")
        head = _git(root, "rev-parse", "HEAD").stdout.strip()
        result = run(
            [sys.executable,
             str(root / "scripts/initialize-campaign-audit.py"),
             "--round", "1", "--base", head,
             "--runner-evidence-sha256", "0" * 64],
            root=root, check=False, env=self._hostile_env(fake_bin),
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(marker.exists())
        state_file = root / STATE_DIR / "audit-coordinator.json"
        data = json.loads(state_file.read_text(encoding="utf-8"))
        self.assertEqual(data["round"], 1)
        self.assertEqual(data["base_commit"], head)
        self.assertEqual(len(data["nonce"]), 64)
        # Coordinator sequencing (Task 23): an exact-matching re-initialization
        # reuses the existing coordinator — the nonce is preserved, never
        # overwritten — while a mismatched binding fails closed.
        nonce_before = data["nonce"]
        reinit = run(
            [sys.executable,
             str(root / "scripts/initialize-campaign-audit.py"),
             "--round", "1", "--base", head,
             "--runner-evidence-sha256", "0" * 64],
            root=root, check=False, env=self._hostile_env(fake_bin),
        )
        self.assertEqual(reinit.returncode, 0, reinit.stderr)
        self.assertIn("reused", reinit.stdout)
        data = json.loads(state_file.read_text(encoding="utf-8"))
        self.assertEqual(data["nonce"], nonce_before,
                         "the exact-matching coordinator must be reused, not re-minted")
        mismatch = run(
            [sys.executable,
             str(root / "scripts/initialize-campaign-audit.py"),
             "--round", "2", "--base", head,
             "--runner-evidence-sha256", "0" * 64],
            root=root, check=False, env=self._hostile_env(fake_bin),
        )
        self.assertNotEqual(mismatch.returncode, 0)
        self.assertIn("does not match", mismatch.stderr)
        data = json.loads(state_file.read_text(encoding="utf-8"))
        self.assertEqual(data["nonce"], nonce_before,
                         "a mismatched coordinator must never be overwritten")

    def test_validate_campaign_audit_not_redirected(self) -> None:
        fake_bin = self._fake_bin()
        marker = self.tmp / "fake-invoked"
        root = _mkdir(self.tmp / "audit-validator-ws")
        for rel in (".factory/artifacts", "scripts"):
            _mkdir(root / rel)
        _write(root / ".factory/artifacts/implementation-plan.md",
               "[fixture-plan]\n")
        _write(root / ".factory/environment.toml", "schema_version = 1\n")
        shutil.copy2(VALIDATE_CAMPAIGN_AUDIT,
                     root / "scripts" / "validate-campaign-audit.py")
        _git(root, "init", "-q", "-b", "develop")
        _git(root, "config", "user.email", "fixture@test")
        _git(root, "config", "user.name", "fixture")
        _git(root, "add", "-A")
        _git(root, "commit", "-qm", "fixture")
        head = _git(root, "rev-parse", "HEAD").stdout.strip()
        plan_blob = _git(root, "rev-parse",
                         "HEAD:.factory/artifacts/implementation-plan.md"
                         ).stdout.strip()
        env_blob = _git(
            root, "rev-parse", "HEAD:.factory/environment.toml").stdout.strip()
        report = (
            "---\n"
            "schema: ralph-campaign-audit/v1\n"
            "round: 1\n"
            f"audit_base_commit: {head}\n"
            f"plan_commit: {head}\n"
            f"plan_blob: {plan_blob}\n"
            f"environment_blob: {env_blob}\n"
            f"runner_evidence_sha256: {'0' * 64}\n"
            "result: pending\n"
            "---\n"
            "# Campaign Round 1\n"
        )
        _write(root / ".factory/artifacts/campaign-audit.md", report)
        result = run(
            [sys.executable, str(root / "scripts/validate-campaign-audit.py"),
             "metadata"],
            root=root, check=False, env=self._hostile_env(fake_bin),
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(marker.exists())
        self.assertIn("round=1", result.stdout)

    def _complete_audit_fixture(self) -> tuple[Path, str]:
        """Fixture repo whose validator runs the full complete-mode path:
        the real runner-evidence checker is invoked by the validator, and
        the checker's own reads use the pinned executable boundary.  The
        fixture carries no runner evidence, so complete mode fails
        deterministically at the digest match; the hostile env must produce
        byte-identical output and never invoke the fake binaries."""
        root = _mkdir(self.tmp / "audit-validator-complete-ws")
        for rel in (".factory/artifacts", ".factory-state", "scripts"):
            _mkdir(root / rel)
        _write(root / ".factory/artifacts/implementation-plan.md",
               "[fixture-plan]\n")
        _write(root / ".factory/environment.toml", "schema_version = 1\n")
        _write(root / ".factory/config.toml",
               "[project]\ndevelopment_branch = \"develop\"\n"
               "[campaign]\nrequired_capabilities = []\n")
        _write(
            root / ".factory/signer-trust.json",
            "{\n"
            '  "schema": "ralph-runner-signer-trust/v1",\n'
            "  \"description\": \"fixture: no signer provisioned\",\n"
            "  \"require_signature\": true,\n"
            "  \"enabled\": false,\n"
            "  \"namespace\": \"factory-runner-receipt\",\n"
            "  \"public_keys\": [],\n"
            "  \"allowed_principals\": []\n"
            "}\n",
        )
        for script in (VALIDATE_CAMPAIGN_AUDIT, CHECK_RUNNER_EVIDENCE,
                       CHECK_ENV):
            shutil.copy2(script, root / "scripts" / script.name)
        _git(root, "init", "-q", "-b", "develop")
        _git(root, "config", "user.email", "fixture@test")
        _git(root, "config", "user.name", "fixture")
        _git(root, "add", "-A")
        _git(root, "commit", "-qm", "fixture")
        head = _git(root, "rev-parse", "HEAD").stdout.strip()
        plan_blob = _git(root, "rev-parse",
                         "HEAD:.factory/artifacts/implementation-plan.md"
                         ).stdout.strip()
        env_blob = _git(
            root, "rev-parse", "HEAD:.factory/environment.toml").stdout.strip()
        report = (
            "---\n"
            "schema: ralph-campaign-audit/v1\n"
            "round: 1\n"
            f"audit_base_commit: {head}\n"
            f"plan_commit: {head}\n"
            f"plan_blob: {plan_blob}\n"
            f"environment_blob: {env_blob}\n"
            f"runner_evidence_sha256: {'0' * 64}\n"
            "result: pending\n"
            "---\n"
            "# Campaign Round 1\n"
        )
        _write(root / ".factory/artifacts/campaign-audit.md", report)
        return root, head

    def test_validate_campaign_audit_complete_not_redirected(self) -> None:
        """F4: complete-mode validation under a hostile env / fake PATH is
        byte-identical to a clean environment and never invokes the fake
        git/ssh-keygen — the runner-evidence checker the validator runs is
        itself pinned and the validator's own invocation is env-sanitized
        and bounded."""
        fake_bin = self._fake_bin()
        marker = self.tmp / "fake-invoked"
        root, head = self._complete_audit_fixture()
        command = [
            sys.executable, str(root / "scripts/validate-campaign-audit.py"),
            "complete", ".factory/artifacts/campaign-audit.md",
            "--expected-round", "1", "--expected-base", head,
            "--expected-runner-evidence-sha256", "0" * 64,
        ]
        clean = run(command, root=root, check=False, env=None)
        hostile = run(command, root=root, check=False,
                      env=self._hostile_env(fake_bin))
        # The fixture has no runner evidence, so complete mode fails
        # deterministically at the runner-evidence digest check — exactly
        # the same in the hostile environment (no redirect, no fake
        # invocation, no wedged/hung check).
        self.assertNotEqual(clean.returncode, 0)
        self.assertEqual(hostile.returncode, clean.returncode)
        self.assertEqual(hostile.stderr, clean.stderr)
        self.assertIn("runner evidence digest does not match", clean.stderr)
        self.assertFalse(marker.exists())

    def test_validate_campaign_audit_complete_timeout_fails_closed(self) -> None:
        """F4: a wedged runner-evidence check times out and fails the
        complete-mode validator closed with a clean diagnostic (no
        traceback, no hang): the finite bound converts TimeoutExpired into
        a SystemExit message."""
        root, head = self._complete_audit_fixture()
        validator_path = root / "scripts/validate-campaign-audit.py"
        spec = importlib.util.spec_from_file_location(
            "factory_validate_campaign_audit", str(validator_path))
        assert spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        try:
            spec.loader.exec_module(module)
        except SystemExit as exc:
            self.skipTest(f"no pinned immutable git on this host: {exc}")
        real_run = subprocess.run

        def wedged(argv, **kwargs):
            argv_list = [str(item) for item in argv]
            if any("check-factory-runner-evidence.py" in item
                   for item in argv_list):
                raise subprocess.TimeoutExpired(argv, kwargs.get("timeout"))
            return real_run(argv, **kwargs)

        with unittest.mock.patch.object(subprocess, "run", side_effect=wedged):
            with unittest.mock.patch.object(
                sys, "argv",
                ["validate-campaign-audit", "complete",
                 ".factory/artifacts/campaign-audit.md",
                 "--expected-round", "1", "--expected-base", head,
                 "--expected-runner-evidence-sha256", "0" * 64],
            ):
                with self.assertRaises(SystemExit) as ctx:
                    module.main()
        self.assertIn("runner-evidence digest check", str(ctx.exception))
        self.assertIn("trusted-execution bound", str(ctx.exception))
        self.assertNotIn("Traceback", str(ctx.exception))


# ---------------------------------------------------------------------------
# Same-tag no-replace concurrent publication (machine-receipt.py)
# ---------------------------------------------------------------------------


class MachineReceiptTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="factory-evidence-receipt."))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.root = _mkdir(self.tmp / "workspace")
        _mkdir(self.root / STATE_DIR)
        self.base = "a" * 40
        self.nonce = "b" * 64
        state = {
            "schema": "ralph-audit-coordinator/v1",
            "round": 1,
            "base_commit": self.base,
            "nonce": self.nonce,
            "created_at": 1,
        }
        _write(self.root / STATE_DIR / "audit-coordinator.json",
               json.dumps(state, sort_keys=True) + "\n")
        os.chmod(self.root / STATE_DIR / "audit-coordinator.json", 0o600)

    def mint(self, tag: str, *argv: str, env: dict | None = None,
             bound: bool = True) -> subprocess.CompletedProcess[str]:
        command = [sys.executable, str(MACHINE_RECEIPT), "--root", str(self.root),
                   "--tag", tag]
        if bound:
            command += ["--audit-round", "1", "--evidence-commit", self.base,
                        "--nonce", self.nonce]
        command += ["--", *argv]
        # The receipt wrapper's supervision is bounded; a 60s ceiling here
        # turns any supervision regression into a fast failure instead of an
        # unbounded hang, and never affects a passing run (<10s).
        return subprocess.run(
            command, cwd=self.root, text=True, capture_output=True, env=env,
            timeout=60,
        )

    def test_mint_records_receipt_and_logs(self) -> None:
        result = self.mint("probe", "sh", "-c", "printf 'runtime output\\n'")
        self.assertEqual(result.returncode, 0)
        ref = f"{STATE_DIR}/audit-receipts/probe.json"
        self.assertIn(f"[receipt: {ref}]", result.stdout)
        data = evidence_module.validate_receipt(self.root, ref)
        self.assertEqual(data["exit_code"], 0)
        self.assertEqual(data["coordinator_round"], 1)
        self.assertEqual(data["evidence_commit"], self.base)
        self.assertEqual(data["coordinator_nonce"], self.nonce)

    def test_same_tag_no_replace_fails_closed(self) -> None:
        first = self.mint("dup", "true")
        self.assertEqual(first.returncode, 0)
        receipt_path = self.root / STATE_DIR / "audit-receipts" / "dup.json"
        original = receipt_path.read_bytes()
        second = self.mint("dup", "true")
        self.assertNotEqual(second.returncode, 0)
        self.assertEqual(receipt_path.read_bytes(), original)
        # No transcript was clobbered by the rejected second publication.
        self.assertEqual(
            (self.root / STATE_DIR / "audit-receipts" / "dup.stdout").read_bytes(),
            b"",
        )

    def test_preplanted_tag_is_never_overwritten(self) -> None:
        recs = _mkdir(self.root / STATE_DIR / "audit-receipts")
        _write(recs / "plant.json", b'{"schema": "planted"}\n')
        result = self.mint("plant", "true")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(
            (recs / "plant.json").read_bytes(), b'{"schema": "planted"}\n')

    def test_concurrent_same_tag_publication_exactly_one_wins(self) -> None:
        results: list[subprocess.CompletedProcess[str]] = []

        def publisher() -> None:
            results.append(self.mint("race", "true"))

        threads = [threading.Thread(target=publisher) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        codes = sorted(result.returncode for result in results)
        self.assertEqual(codes, [0, 1], codes)
        data = evidence_module.validate_receipt(
            self.root, f"{STATE_DIR}/audit-receipts/race.json")
        self.assertEqual(data["exit_code"], 0)

    def test_bare_mint_without_coordinator_binding_fails(self) -> None:
        env = dict(os.environ)
        for key in ("FACTORY_CAMPAIGN_AUDIT_ROUND", "FACTORY_CAMPAIGN_AUDIT_BASE",
                    "FACTORY_CAMPAIGN_AUDIT_NONCE"):
            env.pop(key, None)
        result = self.mint("bare", "true", env=env, bound=False)
        self.assertNotEqual(result.returncode, 0)

    def test_env_only_mint_without_coordinator_state_fails(self) -> None:
        # LOW5: environment variables alone never authorize a receipt; the
        # protected coordinator state (minted by the trusted audit
        # coordinator) is mandatory, so an untrusted role-driver/auditor that
        # sets round/base/nonce env keys against a missing state can never
        # mint.
        (self.root / STATE_DIR / "audit-coordinator.json").unlink()
        env = dict(os.environ)
        env["FACTORY_CAMPAIGN_AUDIT_ROUND"] = "1"
        env["FACTORY_CAMPAIGN_AUDIT_BASE"] = self.base
        env["FACTORY_CAMPAIGN_AUDIT_NONCE"] = self.nonce
        result = self.mint("envonly", "true", env=env, bound=False)
        self.assertNotEqual(result.returncode, 0)

    def test_env_binding_must_match_protected_state(self) -> None:
        # LOW5: even with the protected state present, an env-only binding
        # that does not exactly match the coordinator-minted round/base/nonce
        # fails closed.
        env = dict(os.environ)
        env["FACTORY_CAMPAIGN_AUDIT_ROUND"] = "7"
        env["FACTORY_CAMPAIGN_AUDIT_BASE"] = "c" * 40
        env["FACTORY_CAMPAIGN_AUDIT_NONCE"] = self.nonce
        result = self.mint("envwrong", "true", env=env, bound=False)
        self.assertNotEqual(result.returncode, 0)

    def test_escaped_descendant_can_never_return_pass(self) -> None:
        """Task 23: a bounded command that ``setsid``-escapes a descendant
        which survives holding the wrapper's stdout/stderr pipes after the
        leader exits 0 is detected by the subreaper orphan scan and the run
        is never certified PASS: no receipt is minted, the wrapper exits
        nonzero naming the exact escaped PID, and the escaped descendant is
        terminated so it cannot survive the wrapper's fail-closed path.
        The child writes its PID as a barrier before holding the pipes, so
        the test synchronizes on the child, not on a sleep."""
        pidfile = self.root / "escaped-child.pid"
        # The escaped child leaves its session (``setsid``) so the wrapper's
        # TERM -> KILL group termination never reaches it; it holds the
        # inherited stdout/stderr pipes (the leader already exited), so the
        # pipes cannot EOF until the wrapper terminates it.  The command is
        # barrier-synchronized: the child writes its PID file before the
        # leader exits, so the test's rendezvous with the child never races
        # the wrapper's termination.
        escaped = self.mint("escaped", "sh", "-c",
                            _escaped_command(pidfile))
        escaped_pid = _wait_for_pidfile(pidfile)
        try:
            self.assertNotEqual(
                escaped.returncode, 0,
                "an escaped surviving descendant must never mint a PASS "
                "receipt",
            )
            self.assertIn("escaped descendants survived", escaped.stderr)
            if escaped_pid is not None:
                self.assertIn(str(escaped_pid), escaped.stderr,
                              "the wrapper must name the exact escaped PID")
            # No receipt artifact was minted.
            self.assertFalse(
                (self.root / STATE_DIR / "audit-receipts" / "escaped.json").exists()
            )
            # The escaped child was terminated during bounded cleanup: it
            # can never survive the wrapper's fail-closed path.
            if escaped_pid is not None:
                try:
                    os.kill(escaped_pid, 0)
                except ProcessLookupError:
                    pass
                else:
                    self.fail(
                        "the escaped descendant survived the wrapper's fail "
                        f"closed path (pid {escaped_pid})"
                    )
        finally:
            # Never leave the escaped sleeper behind.
            _kill_pid(escaped_pid)

    def test_term_ignoring_escaped_descendant_is_killed(self) -> None:
        """Task 23: an escaped descendant that ignores SIGTERM is escalated
        to an unconditional KILL within the bounded grace and can never
        survive the wrapper's fail-closed path; the run still never mints a
        PASS receipt.  The child writes its PID as a barrier and ignores
        TERM deterministically (``trap '' TERM`` survives ``exec``), so the
        wrapper's TERM cannot end it and only the KILL escalation can."""
        pidfile = self.root / "term-ignore.pid"
        stubborn = self.mint("term-ignore", "sh", "-c",
                             _escaped_command(pidfile, ignore_term=True))
        stubborn_pid = _wait_for_pidfile(pidfile)
        try:
            self.assertNotEqual(
                stubborn.returncode, 0,
                "a TERM-ignoring escaped descendant must never mint a PASS "
                "receipt",
            )
            self.assertIn("escaped descendants survived", stubborn.stderr)
            if stubborn_pid is not None:
                self.assertIn(str(stubborn_pid), stubborn.stderr)
                try:
                    os.kill(stubborn_pid, 0)
                except ProcessLookupError:
                    pass
                else:
                    self.fail(
                        "a TERM-ignoring escaped descendant survived the "
                        "wrapper's KILL escalation "
                        f"(pid {stubborn_pid})"
                    )
            self.assertFalse(
                (self.root / STATE_DIR / "audit-receipts" /
                 "term-ignore.json").exists()
            )
        finally:
            _kill_pid(stubborn_pid)

    def test_output_overflow_can_never_mint_a_receipt(self) -> None:
        """Task 23: a bounded command that floods stdout past the receipt
        limit is terminated and the run is never recorded: no receipt (and no
        transcript artifacts) may be minted from a truncated run.  The flood
        is a deterministic fixed-size write (no sleep), and the overflow
        diagnostic must stay bounded and must not echo the raw transcript."""
        flood = (
            "python3 -c 'import sys; "
            "sys.stdout.buffer.write(b\"x\" * 5000000); "
            "sys.stdout.flush()'"
        )
        result = self.mint("flood", "sh", "-c", flood)
        self.assertNotEqual(
            result.returncode, 0,
            "an overflowing command must never mint a receipt",
        )
        self.assertIn("exceeded the receipt limit", result.stderr)
        # The diagnostic is bounded and secret-free: it never dumps the raw
        # truncated transcript (a bounded size and no flood payload bytes).
        self.assertLess(len(result.stderr), 4096)
        self.assertNotIn("x" * 64, result.stderr)
        receipts = self.root / STATE_DIR / "audit-receipts"
        for name in ("flood.json", "flood.stdout", "flood.stderr"):
            self.assertFalse(
                (receipts / name).exists(),
                f"a truncated run must never publish {name}",
            )

    def test_baseline_and_foreign_processes_are_never_touched(self) -> None:
        """Task 23: the bounded supervision is identity-pinned and
        scope-limited.  A baseline child (a pre-existing child of the
        wrapper process, in its own session — indistinguishable from an
        escape by pgid alone) and a foreign process (an unrelated process
        whose ancestry is already gone, parented to PID 1) are never
        signaled, killed, or reaped by a run, even while the run's own
        escaped descendant is terminated; the wrapper's failure report names
        only the run's escaped PID, never the baseline or foreign PID."""
        machine_receipt = _load_machine_receipt()
        baseline_pidfile = self.root / "baseline.pid"
        escaped_pidfile = self.root / "inrun-escaped.pid"
        foreign_pidfile = self.root / "foreign.pid"
        baseline = subprocess.Popen(
            ["setsid", "sh", "-c",
             f"echo \"$$\" > {baseline_pidfile}; exec sleep 60"]
        )
        # A foreign process is double-forked so its parent chain is already
        # gone: it is neither a child of this process nor a descendant of
        # the bounded run, and must never be touched by the run.
        spawner = subprocess.Popen(
            ["sh", "-c",
             f"setsid sh -c 'echo \"$$\" > {foreign_pidfile}; exec sleep 60' "
             "& exit 0"],
        )
        spawner.wait()
        foreign_pid = _wait_for_pidfile(foreign_pidfile)
        baseline_pid = _wait_for_pidfile(baseline_pidfile)
        try:
            try:
                machine_receipt.run_bounded(
                    ["sh", "-c", _escaped_command(escaped_pidfile)],
                    self.root,
                )
                self.fail("an escaped descendant must fail run_bounded")
            except SystemExit as exc:
                report = str(exc)
                self.assertIn("escaped descendants survived", report)
            escaped_pid = _wait_for_pidfile(escaped_pidfile)
            self.assertIn(str(escaped_pid), report)
            # The baseline child and the foreign process are untouched: the
            # report names only the run's escaped PID, and both are alive.
            self.assertNotIn(str(baseline_pid), report)
            self.assertNotIn(str(foreign_pid), report)
            self.assertIsNone(
                baseline.poll(),
                f"a baseline child was killed by the bounded run (pid "
                f"{baseline_pid})",
            )
            try:
                os.kill(foreign_pid, 0)
            except ProcessLookupError:
                self.fail(
                    f"a foreign process was killed by the bounded run "
                    f"(pid {foreign_pid})"
                )
            try:
                os.kill(escaped_pid, 0)
            except ProcessLookupError:
                pass
            else:
                self.fail(
                    "the run's escaped descendant survived the bounded "
                    f"termination (pid {escaped_pid})"
                )
        finally:
            _kill_pid(baseline_pid)
            _kill_pid(foreign_pid)
            _kill_pid(escaped_pid if 'escaped_pid' in locals() else None)


class CheckAuditReceiptsTests(unittest.TestCase):
    """The visible checker hardens the adjacent logs with no-follow reads."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="factory-evidence-checker."))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        repo = RepoFixture(self.tmp, name="workspace")
        self.head = repo.commit()
        self.root = repo.root
        os.chmod(self.root, 0o700)
        self.fixture = ReceiptFixture(self.root)
        self.fixture.coordinator(base=self.head)
        self.fixture.write("probe", evidence_commit=self.head)
        self.report = self.root / "audit.md"
        _write(
            self.report,
            self.fixture.report(
                "pass", [self.fixture.line(
                    ref=f"{STATE_DIR}/audit-receipts/probe.json")]),
        )

    def check(self) -> subprocess.CompletedProcess[str]:
        return run(
            [sys.executable, str(CHECK_AUDIT_RECEIPTS), str(self.report),
             "--root", str(self.root)],
            check=False,
        )

    def test_valid_report_passes(self) -> None:
        self.assertEqual(self.check().returncode, 0)

    def test_symlinked_stdout_transcript_fails(self) -> None:
        out = self.fixture.recs / "probe.stdout"
        out.unlink()
        _write(self.fixture.recs / "real.out", b"runtime output\n")
        out.symlink_to(self.fixture.recs / "real.out")
        self.assertNotEqual(self.check().returncode, 0)

    def test_symlinked_stderr_transcript_fails(self) -> None:
        # LOW3: a symlinked stderr log is rejected by the final receipt/log
        # checker CLI (no-follow owner/mode/link-count/inode read).
        err = self.fixture.recs / "probe.stderr"
        err.unlink()
        _write(self.fixture.recs / "real.err", b"")
        err.symlink_to(self.fixture.recs / "real.err")
        self.assertNotEqual(self.check().returncode, 0)

    def test_symlinked_receipt_record_fails(self) -> None:
        # LOW3: a symlinked final receipt JSON is rejected by the checker CLI.
        record = self.fixture.recs / "probe.json"
        record.unlink()
        _write(self.fixture.recs / "real.json", b"{}")
        record.symlink_to(self.fixture.recs / "real.json")
        self.assertNotEqual(self.check().returncode, 0)

    def test_exact_command_mismatch_fails(self) -> None:
        # LOW5: the checker CLI requires the represented command to equal the
        # receipt's recorded argv exactly; a relabeled command fails.
        _write(
            self.report,
            self.fixture.report(
                "pass",
                ["`sh -c 'echo different'` PASS "
                 "[receipt: .factory-state/audit-receipts/probe.json]"]),
        )
        self.assertNotEqual(self.check().returncode, 0)

    def test_hardlinked_stdout_transcript_fails(self) -> None:
        os.link(self.fixture.recs / "probe.stdout",
                self.fixture.recs / "probe.stdout.alias")
        self.assertNotEqual(self.check().returncode, 0)

    def test_group_writable_stdout_transcript_fails(self) -> None:
        os.chmod(self.fixture.recs / "probe.stdout", 0o664)
        self.assertNotEqual(self.check().returncode, 0)


# ---------------------------------------------------------------------------
# Hidden CLI and config command entrypoints
# ---------------------------------------------------------------------------


class EvidenceCLITests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="factory-evidence-cli."))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.repo = RepoFixture(self.tmp)
        self.repo.write_verifier()
        self.repo.write_config(["./scripts/verify.sh"])
        self.head = self.repo.commit()
        self.root = self.repo.root
        self.fixture = ReceiptFixture(self.root)
        self.fixture.coordinator(base=self.head)
        self.fixture.write("probe", evidence_commit=self.head)
        self.report = self.root / "campaign-audit.md"
        _write(self.report, self.fixture.report(
            "pass",
            [self.fixture.line(ref=f"{STATE_DIR}/audit-receipts/probe.json")]))

    def captured_main(self, argv: list[str]) -> tuple[int, str]:
        stream = io.StringIO()
        with redirect_stdout(stream):
            rc = evidence_module.main(argv)
        return rc, stream.getvalue()

    def captured_main_combined(self, argv: list[str]) -> tuple[int, str, str]:
        """Run the CLI capturing both stdout and stderr (fail-closed text)."""
        stdout_stream = io.StringIO()
        stderr_stream = io.StringIO()
        with redirect_stdout(stdout_stream), redirect_stderr(stderr_stream):
            rc = evidence_module.main(argv)
        return rc, stdout_stream.getvalue(), stderr_stream.getvalue()

    def test_bind_cli_prints_binding_json(self) -> None:
        rc, captured = self.captured_main(
            ["bind", "--root", str(self.root), "--commit", self.head])
        self.assertEqual(rc, 0)
        payload = json.loads(captured)
        self.assertEqual(payload["binding"]["executable"], "./scripts/verify.sh")
        self.assertEqual(payload["binding"]["commit"], self.head)
        self.assertEqual(len(payload["sha256"]), 64)

    def test_revalidate_cli_prints_verified(self) -> None:
        rc, captured = self.captured_main(
            ["revalidate", "--root", str(self.root), "--commit", self.head])
        self.assertEqual(rc, 0)
        payload = json.loads(captured)
        self.assertTrue(payload["verified"])

    def test_validate_audit_cli(self) -> None:
        rc, captured = self.captured_main([
            "validate-audit", "campaign-audit.md",
            "--root", str(self.root), "--result", "pass",
            "--expected-round", "1", "--expected-base", self.head,
            "--expected-nonce", self.fixture.nonce,
        ])
        self.assertEqual(rc, 0)
        self.assertIn("factory-evidence: valid", captured)

    def test_bind_missing_config_fails(self) -> None:
        bare = RepoFixture(self.tmp, name="bare")
        bare.commit()
        rc, captured = self.captured_main(["bind", "--root", str(bare.root)])
        self.assertEqual(rc, 1)

    def test_bind_cli_rejects_committed_config_mismatch(self) -> None:
        # LOW5: the committed config is authoritative; an uncommitted worktree
        # substitution of .factory/config.toml fails the CLI closed before any
        # verifier binding or execution.
        self.repo.write_config(["./scripts/verify.sh", "--substituted"])
        rc, captured, err = self.captured_main_combined(
            ["bind", "--root", str(self.root), "--commit", self.head])
        self.assertEqual(rc, 1)
        self.assertIn("config substitution fails closed", err)


# ---------------------------------------------------------------------------
# Campaign integration: the verifier is bound before the untrusted phase and
# substitution fails closed as infrastructure_failure.
# ---------------------------------------------------------------------------


class CampaignVerifierIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="factory-evidence-campaign."))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.tfc = _load_campaign_fixture_module()

    def _workspace(self, scenario: dict):
        return self.tfc.FixtureWorkspace(
            self.tmp / "ws", scenario=scenario)

    def _commit_verifier(self, ws, content: bytes = b"#!/bin/sh\nexit 0\n") -> None:
        # The verifier lives under ``src/`` (product scope), never the
        # protected ``scripts/`` harness surface, so the fixture planner's
        # dirty-path scope check stays clean.
        verifier = ws.root / "src" / "verify-fixture.sh"
        _write(verifier, content)
        os.chmod(verifier, 0o755)
        _git(ws.root, "add", "-A")
        _git(ws.root, "commit", "-qm", "verifier fixture")

    def _config(self, ws):
        return dataclasses.replace(
            ws.derive_config(),
            verification_command=("./src/verify-fixture.sh",))

    def _exit_code(self, result) -> int:
        return campaign_module.TERMINAL_EXIT_CODES[result.terminal_phase]

    def test_committed_repo_relative_verifier_campaign_passes(self) -> None:
        ws = self._workspace({
            "planner": {"behavior": "planned"},
            "developer": {"behavior": "complete"},
            "tester": {"behavior": "pass"},
            "auditor": {"behavior": "pass"},
        })
        self._commit_verifier(ws)
        ws.commit_scenario()
        result = campaign_module.Campaign(self._config(ws)).run()
        self.assertEqual(result.terminal_phase, "success")
        self.assertEqual(self._exit_code(result), 0)
        self.assertTrue(any(r.phase == "verification" and r.outcome == "pass"
                            for r in result.phase_history))

    def test_post_untrusted_verifier_substitution_fails_closed(self) -> None:
        ws = self._workspace({
            "planner": {"behavior": "planned"},
            "developer": {"behavior": "complete"},
            "tester": {"behavior": "pass"},
            "auditor": {"behavior": "pass"},
        })
        self._commit_verifier(ws)
        ws.commit_scenario()
        config = self._config(ws)

        real_revalidate = evidence_module.revalidate_verifier

        def substituted(root, binding, *, git=None, current_commit=None, held=None):
            # Simulate the untrusted tester substituting the verifier pathname
            # during the untrusted phase: before the deterministic gate runs,
            # the bound verifier pathname is replaced by a different inode.
            # The substitution is scoped to the verifier binding only — the
            # role driver is an additional revalidation target of the same
            # authority (Task 22 B1) and must not be perturbed by this
            # verifier-substitution fixture.
            if not binding.executable.endswith("verify-fixture.sh"):
                return real_revalidate(root, binding, git=git,
                                        current_commit=current_commit, held=held)
            path = Path(root) / binding.executable[2:]
            path.unlink()
            _write(path, b"#!/bin/sh\nexit 9\n")
            os.chmod(path, 0o755)
            return real_revalidate(root, binding, git=git,
                                    current_commit=current_commit, held=held)

        with unittest.mock.patch.object(
            evidence_module, "revalidate_verifier",
            side_effect=substituted,
        ):
            result = campaign_module.Campaign(config).run()
        self.assertEqual(result.terminal_phase, "infrastructure_failure")
        self.assertEqual(self._exit_code(result), 5)
        records = [r for r in result.phase_history if r.phase == "verification"]
        self.assertTrue(records)
        self.assertEqual(records[-1].outcome, "infrastructure_failure")
        self.assertIn("verifier binding failed closed", records[-1].detail)

    def test_missing_verifier_path_fails_closed(self) -> None:
        ws = self._workspace({
            "planner": {"behavior": "planned"},
            "developer": {"behavior": "complete"},
            "tester": {"behavior": "pass"},
            "auditor": {"behavior": "pass"},
        })
        ws.commit_scenario()
        # The configured verifier path does not exist and is not tracked: the
        # binding authority cannot open a committed blob, so the campaign
        # fails closed as infrastructure_failure *before* the untrusted
        # tester phase runs.
        config = dataclasses.replace(
            ws.derive_config(),
            verification_command=("./src/missing-verify.sh",))
        result = campaign_module.Campaign(config).run()
        self.assertEqual(result.terminal_phase, "infrastructure_failure")
        self.assertEqual(self._exit_code(result), 5)
        # No verification record reached a pass outcome.
        self.assertFalse(any(r.phase == "verification" and r.outcome == "pass"
                             for r in result.phase_history))


if __name__ == "__main__":
    unittest.main(verbosity=2)
