#!/usr/bin/env python3
"""Adversarial checks for the campaign implementation-rebind recovery."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import tempfile

SOURCE = Path(__file__).resolve().parent.parent / "scripts/ralph-campaign-state.py"
DIGEST = "a" * 64


class Repo:
    def __init__(self, descendants: int = 2, verification_phase: bool = True) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        (self.root / "scripts").mkdir()
        (self.root / ".factory").mkdir()
        (self.root / ".factory/config.toml").write_text(
            "[project]\ndevelopment_branch = \"develop\"\n", encoding="utf-8"
        )
        (self.root / ".factory-state").mkdir(mode=0o700)
        shutil.copy2(SOURCE, self.root / "scripts/ralph-campaign-state.py")
        scripts = Path(__file__).resolve().parent.parent / "scripts"
        shutil.copy2(scripts / "factory_lock.py", self.root / "scripts/factory_lock.py")
        shutil.copy2(scripts / "factory_state_io.py", self.root / "scripts/factory_state_io.py")
        (self.root / ".gitignore").write_text(
            ".factory-state/\n.factory-lock\n__pycache__/\n", encoding="utf-8"
        )
        (self.root / "history.txt").write_text("base\n", encoding="utf-8")
        self.git("init", "-q", "-b", "develop")
        self.git("config", "user.name", "test")
        self.git("config", "user.email", "test@example.invalid")
        self.git("add", ".")
        self.git("commit", "-qm", "base")
        self.base = self.head()
        self.plan = self.commit("plan")
        self.old = self.commit("implementation")
        self.descendants = [self.commit(f"descendant-{number}") for number in range(1, descendants + 1)]
        self.configure_state(verification_phase)

    @property
    def state_path(self) -> Path:
        return self.root / ".factory-state/ralph-campaign.json"

    def close(self) -> None:
        self.temporary.cleanup()

    def git(self, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", *args], cwd=self.root, text=True, capture_output=True, check=check,
        )

    def head(self) -> str:
        return self.git("rev-parse", "HEAD").stdout.strip()

    def commit(self, label: str) -> str:
        with (self.root / "history.txt").open("a", encoding="utf-8") as stream:
            stream.write(f"{label}\n")
        self.git("add", "history.txt")
        self.git("commit", "-qm", label)
        return self.head()

    def helper(self, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        result = subprocess.run(
            [str(self.root / "scripts/ralph-campaign-state.py"), *args],
            cwd=self.root, text=True, capture_output=True,
        )
        if check and result.returncode:
            raise AssertionError(f"helper failed ({result.returncode}): {result.stderr}")
        return result

    def configure_state(self, verification_phase: bool = True) -> None:
        self.helper(
            "start", "--rounds", "3", "--tui", "false",
            "--verification-digest", DIGEST, "--base", self.base,
        )
        data = self.state()
        data["phase"] = "implementation"
        data["rounds"][0]["planning_started"] = True
        data["rounds"][0]["plan_commit"] = self.plan
        data["rounds"][0]["implementation_started"] = True
        if verification_phase:
            data["rounds"][0]["implementation_commit"] = self.old
            data["phase"] = "verification"
        self.state_path.write_text(json.dumps(data, sort_keys=True, indent=2) + "\n", encoding="utf-8")
        self.helper("show")

    def state(self) -> dict:
        return json.loads(self.state_path.read_text(encoding="utf-8"))

    def assert_rejected(self, *args: str, contains: str | None = None) -> None:
        before = self.state_path.read_bytes()
        result = self.helper(*args, check=False)
        assert result.returncode != 0, (args, result.stdout, result.stderr)
        if contains is not None:
            assert contains in result.stderr, (contains, result.stderr)
        assert self.state_path.read_bytes() == before, f"rejected operation changed state: {args}"

    def rebind_args(self, old: str | None = None, new: str | None = None) -> tuple[str, ...]:
        return (
            "rebind-implementation", "--expected-old", old or self.old,
            "--new", new or self.head(),
        )


def test_success_and_normal_write_once() -> None:
    repo = Repo()
    try:
        before = hashlib.sha256(repo.state_path.read_bytes()).hexdigest()
        result = repo.helper(*repo.rebind_args())
        receipt = json.loads(result.stdout)
        state = repo.state()
        after = hashlib.sha256(repo.state_path.read_bytes()).hexdigest()
        assert state["rounds"][0]["implementation_commit"] == repo.head()
        assert state["rounds"][0]["verification_commit"] is None
        assert state["rounds"][0]["runner_evidence_sha256"] is None
        assert receipt == {
            "operation": "rebind-implementation",
            "round": 1,
            "expected_old": repo.old,
            "new": repo.head(),
            "state_sha256_before": before,
            "state_sha256_after": after,
        }
        repo.helper("show")  # Persisted state still passes strict validation.
        repo.assert_rejected(
            "update", "--expect-phase", "verification",
            "--round-field", f"implementation_commit={json.dumps(repo.old)}",
            contains="not writable in verification",
        )
    finally:
        repo.close()

    repo = Repo(verification_phase=False)
    try:
        repo.helper(
            "update", "--expect-phase", "implementation", "--phase", "verification",
            "--round-field", f"implementation_commit={json.dumps(repo.old)}",
        )
        repo.assert_rejected(
            "update", "--expect-phase", "verification",
            "--round-field", f"implementation_commit={json.dumps(repo.head())}",
            contains="not writable in verification",
        )
    finally:
        repo.close()


def test_identity_phase_and_head_rejections() -> None:
    repo = Repo()
    try:
        repo.assert_rejected(*repo.rebind_args(old=repo.plan), contains="does not match --expected-old")
        repo.assert_rejected(*repo.rebind_args(new=repo.descendants[0]), contains="must equal current HEAD")
        repo.assert_rejected(
            "rebind-implementation", "--expected-old", "bad", "--new", repo.head(),
            contains="is invalid",
        )
        (repo.root / "history.txt").write_text("dirty\n", encoding="utf-8")
        repo.assert_rejected(*repo.rebind_args(), contains="clean Git tree")
    finally:
        repo.close()

    repo = Repo()
    try:
        repo.git("switch", "-qc", "recovery-trial")
        repo.assert_rejected(*repo.rebind_args(), contains="develop branch")
    finally:
        repo.close()

    repo = Repo(verification_phase=False)
    try:
        repo.assert_rejected(*repo.rebind_args(), contains="active verification phase")
    finally:
        repo.close()


def test_equal_backward_and_nonancestor_rejections() -> None:
    repo = Repo(descendants=0)
    try:
        repo.assert_rejected(*repo.rebind_args(), contains="strictly descend")
    finally:
        repo.close()

    repo = Repo()
    try:
        repo.git("reset", "--hard", repo.plan)
        repo.assert_rejected(*repo.rebind_args(new=repo.plan), contains="recorded anchor")
    finally:
        repo.close()

    repo = Repo()
    try:
        repo.git("switch", "--detach", repo.plan)
        alternate = repo.commit("alternate")
        repo.git("branch", "-f", "develop", alternate)
        repo.git("switch", "-q", "develop")
        repo.assert_rejected(*repo.rebind_args(new=alternate), contains="recorded anchor")
    finally:
        repo.close()


def test_merge_range_rejected() -> None:
    repo = Repo(descendants=0)
    try:
        repo.git("switch", "-qc", "side")
        (repo.root / "side.txt").write_text("side\n", encoding="utf-8")
        repo.git("add", "side.txt")
        repo.git("commit", "-qm", "side")
        repo.git("switch", "-q", "develop")
        repo.commit("main")
        repo.git("merge", "-q", "--no-ff", "side", "-m", "merge")
        repo.assert_rejected(*repo.rebind_args(), contains="range contains a merge")
    finally:
        repo.close()


def test_later_round_rejected() -> None:
    repo = Repo(descendants=0)
    try:
        repo.helper(
            "update", "--expect-phase", "verification", "--phase", "audit",
            "--round-field", f"verification_commit={json.dumps(repo.old)}",
            "--round-field", f"runner_evidence_sha256={json.dumps(DIGEST)}",
        )
        audit = repo.commit("audit")
        state = repo.state()
        state["rounds"][0]["audit_started"] = True
        repo.state_path.write_text(json.dumps(state, sort_keys=True, indent=2) + "\n", encoding="utf-8")
        repo.helper(
            "update", "--expect-phase", "audit",
            "--round-field", f"audit_commit={json.dumps(audit)}",
            "--round-field", 'audit_result="pass"',
        )
        repo.helper("advance", "--expect-phase", "audit")
        plan2 = repo.commit("plan-2")
        state = repo.state()
        state["phase"] = "implementation"
        state["rounds"][1]["planning_started"] = True
        state["rounds"][1]["plan_commit"] = plan2
        repo.state_path.write_text(json.dumps(state, sort_keys=True, indent=2) + "\n", encoding="utf-8")
        implementation2 = repo.commit("implementation-2")
        state = repo.state()
        state["phase"] = "verification"
        state["rounds"][1]["implementation_started"] = True
        state["rounds"][1]["implementation_commit"] = implementation2
        repo.state_path.write_text(json.dumps(state, sort_keys=True, indent=2) + "\n", encoding="utf-8")
        repo.helper("show")
        new2 = repo.commit("new-2")
        repo.assert_rejected(
            "rebind-implementation", "--expected-old", implementation2, "--new", new2,
            contains="first campaign round",
        )
    finally:
        repo.close()


def test_lock_and_unsafe_lock_rejections() -> None:
    repo = Repo()
    try:
        repo.helper("show")
        lock_fd = os.open(repo.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            repo.assert_rejected(*repo.rebind_args(), contains="another planner")
        finally:
            os.close(lock_fd)
    finally:
        repo.close()

    repo = Repo()
    try:
        repo.helper("show")
        original_mode = stat.S_IMODE(repo.root.stat().st_mode)
        repo.root.chmod(original_mode | 0o020)
        try:
            repo.assert_rejected(*repo.rebind_args(), contains="unsafe repository root")
        finally:
            repo.root.chmod(original_mode)
    finally:
        repo.close()


def test_future_phase_fields_rejected() -> None:
    repo = Repo(verification_phase=False)
    try:
        state = repo.state()
        state["rounds"][0]["implementation_commit"] = repo.old
        repo.state_path.write_text(json.dumps(state, sort_keys=True, indent=2) + "\n", encoding="utf-8")
        repo.assert_rejected("show", contains="future-phase fields")
    finally:
        repo.close()

    repo = Repo()
    try:
        state = repo.state()
        state["rounds"][0]["runner_evidence_sha256"] = DIGEST
        repo.state_path.write_text(json.dumps(state, sort_keys=True, indent=2) + "\n", encoding="utf-8")
        repo.assert_rejected("show", contains="future-phase fields")
    finally:
        repo.close()


def test_audit_binding_reconstruction() -> None:
    repo = Repo()
    try:
        repo.helper(
            "update", "--expect-phase", "verification", "--phase", "audit",
            "--round-field", f"verification_commit={json.dumps(repo.old)}",
            "--round-field", f"runner_evidence_sha256={json.dumps(DIGEST)}",
        )
        binding = json.loads(repo.helper("audit-binding").stdout)
        assert binding == {
            "round": 1,
            "base": repo.old,
            "runner_evidence_sha256": DIGEST,
        }
    finally:
        repo.close()


def test_malformed_terminal_replacement_rejected() -> None:
    repo = Repo()
    try:
        malformed = repo.state()
        malformed["status"] = "complete"
        malformed["phase"] = "complete"
        del malformed["verification_command_sha256"]
        repo.state_path.write_text(json.dumps(malformed) + "\n", encoding="utf-8")
        repo.assert_rejected(
            "start", "--rounds", "1", "--tui", "false",
            "--verification-digest", DIGEST, "--base", repo.head(),
            "--replace-terminal", contains="unexpected fields",
        )
    finally:
        repo.close()


def main() -> None:
    test_success_and_normal_write_once()
    test_identity_phase_and_head_rejections()
    test_equal_backward_and_nonancestor_rejections()
    test_merge_range_rejected()
    test_later_round_rejected()
    test_lock_and_unsafe_lock_rejections()
    test_future_phase_fields_rejected()
    test_audit_binding_reconstruction()
    test_malformed_terminal_replacement_rejected()
    print("test: Ralph campaign state recovery checks passed")


if __name__ == "__main__":
    main()
