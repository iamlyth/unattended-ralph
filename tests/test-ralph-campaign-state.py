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
            "[project]\ndevelopment_branch = \"develop\"\n"
            "[verification]\ncampaign_command = [\"./scripts/verify-boilerplate.sh\"]\n",
            encoding="utf-8",
        )
        (self.root / ".factory/verifier-acceptance.json").write_text(
            json.dumps({
                "schema": "ralph-verifier-acceptance/v1",
                "gates": [
                    {"name": "gate-one.sh", "args": []},
                    {"name": "gate-two.sh", "args": []},
                ],
            }, indent=2) + "\n",
            encoding="utf-8",
        )
        (self.root / "scripts/verify-boilerplate.sh").write_text(
            "#!/usr/bin/env bash\nset -euo pipefail\nexit 0\n", encoding="utf-8"
        )
        (self.root / "scripts/verify-boilerplate.sh").chmod(0o755)
        (self.root / ".factory-state").mkdir(mode=0o700)
        shutil.copy2(SOURCE, self.root / "scripts/ralph-campaign-state.py")
        scripts = Path(__file__).resolve().parent.parent / "scripts"
        shutil.copy2(scripts / "factory_lock.py", self.root / "scripts/factory_lock.py")
        shutil.copy2(scripts / "factory_state_io.py", self.root / "scripts/factory_state_io.py")
        shutil.copy2(scripts / "campaign-verifier-binding.py", self.root / "scripts/campaign-verifier-binding.py")
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


def real_binding(repo: "Repo") -> dict:
    result = subprocess.run(
        [str(repo.root / "scripts/campaign-verifier-binding.py")],
        cwd=repo.root, text=True, capture_output=True,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_verifier_contract_roundtrip() -> None:
    repo = Repo(verification_phase=False)
    try:
        binding = real_binding(repo)
        digest = binding["sha256"]
        repo.helper("record-verifier-contract", "--digest", digest, "--binding", json.dumps(binding))
        contract = json.loads(repo.helper("verifier-contract").stdout)
        assert contract["schema"] == "ralph-verifier-contract/v1"
        assert contract["acceptance_gates"] == ["gate-one.sh", "gate-two.sh"]
        assert contract["acceptance_entries"] == [
            {"name": "gate-one.sh", "args": []},
            {"name": "gate-two.sh", "args": []},
        ]
        # --if-missing keeps the original baseline contract.
        repo.helper(
            "record-verifier-contract", "--if-missing",
            "--digest", "f" * 64, "--binding", json.dumps(binding),
        )
        contract = json.loads(repo.helper("verifier-contract").stdout)
        assert contract["digest"] == digest
        # A mismatched current binding classifies as ambiguous.
        classification = repo.helper(
            "classify-verifier-change", "--expected-old", digest, "--new", "e" * 64,
        ).stdout.strip()
        assert classification == "ambiguous"
    finally:
        repo.close()


def _record_baseline(repo: "Repo") -> str:
    binding = real_binding(repo)
    digest = binding["sha256"]
    state = repo.state()
    state["verification_command_sha256"] = digest
    repo.state_path.write_text(json.dumps(state, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    repo.helper("record-verifier-contract", "--digest", digest, "--binding", json.dumps(binding))
    return digest


def _rewrite_manifest(repo: "Repo", gates: list[dict]) -> str:
    manifest = json.loads((repo.root / ".factory/verifier-acceptance.json").read_text())
    manifest["gates"] = gates
    (repo.root / ".factory/verifier-acceptance.json").write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
    )
    repo.git("add", ".factory/verifier-acceptance.json")
    repo.git("commit", "-qm", "retune gates")
    new_binding = real_binding(repo)
    return new_binding["sha256"]


def test_verifier_arg_only_change_requires_marker() -> None:
    # A name-identical arg edit must not auto-rebind: it is weakening, so it
    # halts on the operator migration marker and leaves state unchanged.
    repo = Repo(verification_phase=False)
    try:
        digest = _record_baseline(repo)
        new_digest = _rewrite_manifest(repo, [
            {"name": "gate-one.sh", "args": ["--strict"]},
            {"name": "gate-two.sh", "args": []},
        ])
        assert new_digest != digest
        state_digest = hashlib.sha256(repo.state_path.read_bytes()).hexdigest()
        repo.assert_rejected(
            "promote-verifier-binding", "--mode", "implementation",
            "--expected-old", digest, "--new", new_digest,
            contains="migration marker",
        )
        assert repo.state()["verification_command_sha256"] == digest
        assert hashlib.sha256(repo.state_path.read_bytes()).hexdigest() == state_digest
        classification = repo.helper(
            "classify-verifier-change", "--expected-old", digest, "--new", new_digest,
        ).stdout.strip()
        assert classification == "weakening"
    finally:
        repo.close()


def test_verifier_reorder_requires_marker() -> None:
    # Swapping the execution order of existing gates must not auto-rebind.
    repo = Repo(verification_phase=False)
    try:
        digest = _record_baseline(repo)
        new_digest = _rewrite_manifest(repo, [
            {"name": "gate-two.sh", "args": []},
            {"name": "gate-one.sh", "args": []},
        ])
        assert new_digest != digest
        state_digest = hashlib.sha256(repo.state_path.read_bytes()).hexdigest()
        repo.assert_rejected(
            "promote-verifier-binding", "--mode", "implementation",
            "--expected-old", digest, "--new", new_digest,
            contains="migration marker",
        )
        assert repo.state()["verification_command_sha256"] == digest
        assert hashlib.sha256(repo.state_path.read_bytes()).hexdigest() == state_digest
        classification = repo.helper(
            "classify-verifier-change", "--expected-old", digest, "--new", new_digest,
        ).stdout.strip()
        assert classification == "weakening"
    finally:
        repo.close()


def test_verifier_duplicate_names_rejected_by_binding() -> None:
    # A manifest that repeats a gate name is ambiguous and is rejected outright
    # by the binding before any classification or promotion can observe it.
    repo = Repo(verification_phase=False)
    try:
        digest = _record_baseline(repo)
        manifest = json.loads((repo.root / ".factory/verifier-acceptance.json").read_text())
        manifest["gates"].insert(1, {"name": "gate-one.sh", "args": []})
        (repo.root / ".factory/verifier-acceptance.json").write_text(
            json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
        )
        repo.git("add", ".factory/verifier-acceptance.json")
        repo.git("commit", "-qm", "duplicate gate name")
        result = subprocess.run(
            [str(repo.root / "scripts/campaign-verifier-binding.py")],
            cwd=repo.root, text=True, capture_output=True,
        )
        assert result.returncode != 0, result.stdout
        assert "repeats gate name" in result.stderr, result.stderr
        # The contract path and the campaign promotion path both refuse: the
        # duplicate manifest cannot produce a binding digest, so promote fails
        # at current_binding() before it can touch campaign state.
        state_digest = hashlib.sha256(repo.state_path.read_bytes()).hexdigest()
        repo.assert_rejected(
            "promote-verifier-binding", "--mode", "implementation",
            "--expected-old", digest, "--new", "e" * 64,
            contains="current campaign verifier binding is invalid",
        )
        assert hashlib.sha256(repo.state_path.read_bytes()).hexdigest() == state_digest
    finally:
        repo.close()



def test_verifier_strengthening_auto_rebind() -> None:
    repo = Repo(verification_phase=False)
    try:
        binding = real_binding(repo)
        digest = binding["sha256"]
        state = repo.state()
        state["verification_command_sha256"] = digest
        repo.state_path.write_text(json.dumps(state, sort_keys=True, indent=2) + "\n", encoding="utf-8")
        repo.helper("record-verifier-contract", "--digest", digest, "--binding", json.dumps(binding))
        # Grow the gate list (strict strengthening) and commit.
        manifest = json.loads((repo.root / ".factory/verifier-acceptance.json").read_text())
        manifest["gates"].append({"name": "gate-three.sh", "args": []})
        (repo.root / ".factory/verifier-acceptance.json").write_text(
            json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
        )
        repo.git("add", ".factory/verifier-acceptance.json")
        repo.git("commit", "-qm", "grow gates")
        new_binding = real_binding(repo)
        new_digest = new_binding["sha256"]
        assert new_digest != digest
        repo.helper(
            "promote-verifier-binding", "--mode", "implementation",
            "--expected-old", digest, "--new", new_digest,
        )
        state = repo.state()
        assert state["verification_command_sha256"] == new_digest
        contract = json.loads(repo.helper("verifier-contract").stdout)
        assert contract["acceptance_gates"] == ["gate-one.sh", "gate-two.sh", "gate-three.sh"]
        audit = (repo.root / ".factory-state/verifier-rebind-audit.jsonl").read_text().splitlines()
        assert len(audit) == 1
        assert json.loads(audit[0])["classification"] == "auto-strengthening"
        # Idempotent: promoting to the already-saved digest is a no-op.
        repo.helper(
            "promote-verifier-binding", "--mode", "implementation",
            "--expected-old", new_digest, "--new", new_digest,
        )
        assert len((repo.root / ".factory-state/verifier-rebind-audit.jsonl").read_text().splitlines()) == 1
    finally:
        repo.close()


def test_verifier_weakening_requires_marker() -> None:
    repo = Repo(verification_phase=False)
    try:
        binding = real_binding(repo)
        digest = binding["sha256"]
        state = repo.state()
        state["verification_command_sha256"] = digest
        repo.state_path.write_text(json.dumps(state, sort_keys=True, indent=2) + "\n", encoding="utf-8")
        repo.helper("record-verifier-contract", "--digest", digest, "--binding", json.dumps(binding))
        # Weakening: remove a gate and commit.
        manifest = json.loads((repo.root / ".factory/verifier-acceptance.json").read_text())
        manifest["gates"] = [g for g in manifest["gates"] if g["name"] != "gate-two.sh"]
        (repo.root / ".factory/verifier-acceptance.json").write_text(
            json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
        )
        repo.git("add", ".factory/verifier-acceptance.json")
        repo.git("commit", "-qm", "shrink gates")
        new_binding = real_binding(repo)
        new_digest = new_binding["sha256"]
        # Without the operator marker the promotion is rejected, state unchanged.
        repo.assert_rejected(
            "promote-verifier-binding", "--mode", "implementation",
            "--expected-old", digest, "--new", new_digest,
            contains="migration marker",
        )
        assert repo.state()["verification_command_sha256"] == digest
        # Classification reports the weakening.
        classification = repo.helper(
            "classify-verifier-change", "--expected-old", digest, "--new", new_digest,
        ).stdout.strip()
        assert classification == "weakening"
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
    test_verifier_contract_roundtrip()
    test_verifier_strengthening_auto_rebind()
    test_verifier_weakening_requires_marker()
    test_verifier_arg_only_change_requires_marker()
    test_verifier_reorder_requires_marker()
    test_verifier_duplicate_names_rejected_by_binding()
    print("test: Ralph campaign state recovery checks passed")


if __name__ == "__main__":
    main()
