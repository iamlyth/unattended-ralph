#!/usr/bin/env python3
"""Focused security regressions for Ralph lifecycle state and event boundaries."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

SOURCE = Path(__file__).resolve().parent.parent
HEX = "a" * 64


def run(
    command: list[str],
    root: Path,
    *,
    env: dict[str, str] | None = None,
    check: bool = True,
    pass_fds: tuple[int, ...] = (),
) -> subprocess.CompletedProcess[str]:
    merged = os.environ.copy()
    if env:
        merged.update(env)
    result = subprocess.run(
        command, cwd=root, env=merged, text=True, capture_output=True, pass_fds=pass_fds
    )
    if check and result.returncode:
        raise AssertionError((command, result.returncode, result.stdout, result.stderr))
    return result


def copy_scripts(root: Path, *names: str) -> None:
    (root / "scripts").mkdir(exist_ok=True)
    for name in names:
        shutil.copy2(SOURCE / "scripts" / name, root / "scripts" / name)


def test_symlink_safe_state_markers() -> None:
    root = Path(tempfile.mkdtemp(prefix="factory-state-test."))
    try:
        (root / ".factory-state").mkdir(mode=0o700)
        copy_scripts(root, "factory-state-file.py", "factory_state_io.py")
        helper = root / "scripts/factory-state-file.py"
        external = root / "external"
        external.write_text("untouched\n", encoding="utf-8")
        marker = root / ".factory-state/loop-mode"
        marker.symlink_to(external)
        for arguments in (("read", "loop-mode"), ("write", "loop-mode", "planning"), ("remove", "loop-mode")):
            rejected = run([str(helper), *arguments], root, check=False)
            assert rejected.returncode != 0, arguments
            assert external.read_text(encoding="utf-8") == "untouched\n"
        marker.unlink()
        run([str(helper), "write", "loop-mode", "planning"], root)
        assert run([str(helper), "read", "loop-mode"], root).stdout.strip() == "planning"

        shutil.rmtree(root / ".factory-state")
        outside = root / "outside-state"
        outside.mkdir()
        (root / ".factory-state").symlink_to(outside, target_is_directory=True)
        rejected = run([str(helper), "write", "loop-mode", "audit"], root, check=False)
        assert rejected.returncode != 0 and not any(outside.iterdir())
    finally:
        shutil.rmtree(root)


def test_state_removal_quarantines_post_check_substitution() -> None:
    module = load_module(SOURCE / "scripts/factory_state_io.py", "factory_state_io_race_module")
    root = Path(tempfile.mkdtemp(prefix="factory-state-race-test."))
    try:
        state = root / ".factory-state"
        state.mkdir(mode=0o700)
        marker = state / "completion-rejected.json"
        replacement = state / "replacement"

        def install_original() -> None:
            marker.write_text('{"value":"original"}\n', encoding="utf-8")
            marker.chmod(0o600)
            replacement.write_text('{"value":"replacement"}\n', encoding="utf-8")
            replacement.chmod(0o600)

        def substitute() -> None:
            marker.unlink()
            replacement.rename(marker)

        install_original()
        try:
            module.remove(root, "completion-rejected.json", after_final_check=substitute)
        except module.StateIOError as exc:
            assert "substituted at quarantine" in str(exc)
        else:
            raise AssertionError("post-check marker replacement was removed")
        quarantines = list(state.glob(".completion-rejected.json.quarantine-*"))
        assert len(quarantines) == 1
        assert json.loads(quarantines[0].read_text(encoding="utf-8"))["value"] == "replacement"
        quarantines[0].unlink()

        install_original()
        try:
            module.consume_json(
                root,
                "completion-rejected.json",
                lambda data: data["value"],
                after_final_check=substitute,
            )
        except module.StateIOError as exc:
            assert "substituted at quarantine" in str(exc)
        else:
            raise AssertionError("post-validation rejection marker replacement was consumed")
        quarantines = list(state.glob(".completion-rejected.json.quarantine-*"))
        assert len(quarantines) == 1
        assert json.loads(quarantines[0].read_text(encoding="utf-8"))["value"] == "replacement"
    finally:
        shutil.rmtree(root)


def load_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_loop_lock_process_identity_and_inode_race() -> None:
    module = load_module(SOURCE / "scripts/ralph_lock.py", "ralph_lock_test_module")
    root = Path(tempfile.mkdtemp(prefix="factory-loop-lock-test."))
    try:
        lock = root / "loop.lock"
        boot = module.current_boot_id()
        start = module.process_start_time(os.getpid())
        assert boot and start

        lock.write_text(
            json.dumps({"pid": os.getpid(), "boot_id": boot, "start_time": start}) + "\n",
            encoding="utf-8",
        )
        try:
            module.remove_stale_lock(root)
        except module.RalphLockError as exc:
            assert "live process" in str(exc)
        else:
            raise AssertionError("live boot/start identity was removed")
        assert lock.exists()

        # A reused PID with a different recorded start identity is stale.
        lock.write_text(
            json.dumps({"pid": os.getpid(), "boot_id": boot, "start_time": start + 1}) + "\n",
            encoding="utf-8",
        )
        module.remove_stale_lock(root)
        assert not lock.exists()

        lock.write_text(json.dumps({"pid": 99_999_999}) + "\n", encoding="utf-8")
        replacement = root / "replacement"
        replacement.write_text("replacement\n", encoding="utf-8")

        def replace_before_unlink() -> None:
            lock.unlink()
            replacement.rename(lock)

        try:
            module.remove_stale_lock(root, before_unlink=replace_before_unlink)
        except module.RalphLockError as exc:
            assert any(word in str(exc) for word in ("replaced", "substituted", "unsafe"))
        else:
            raise AssertionError("replacement inode was unlinked")
        quarantines = list(root.glob(".loop.lock.quarantine-*"))
        assert len(quarantines) == 1
        assert quarantines[0].read_text(encoding="utf-8") == "replacement\n"
    finally:
        shutil.rmtree(root)


def event_record(topic: str, payload: object, *, iteration: int | None = None) -> dict[str, object]:
    record: dict[str, object] = {"ts": "2026-08-19T00:00:00+00:00", "topic": topic, "payload": payload}
    if iteration is not None:
        record.update({"iteration": iteration, "hat": "loop"})
        if iteration == 0:
            record["triggered"] = "planner"
    return record


def test_receiver_side_token_contamination() -> None:
    root = Path(tempfile.mkdtemp(prefix="factory-event-test."))
    try:
        (root / ".factory-state").mkdir(mode=0o700)
        (root / ".ralph").mkdir()
        (root / ".factory/prompts").mkdir(parents=True)
        prompt = "trusted start prompt names LOOP_COMPLETE"
        (root / ".factory/prompts/implementation.md").write_text(prompt, encoding="utf-8")
        copy_scripts(root, "ralph-event-boundary.py", "factory_state_io.py")
        event_path = root / ".ralph/events-20260819-000000.jsonl"
        event_path.write_text("", encoding="utf-8")
        (root / ".ralph/current-events").write_text(
            ".ralph/events-20260819-000000.jsonl\n", encoding="utf-8"
        )
        (root / ".ralph/current-loop-id").write_text("test-loop\n", encoding="utf-8")
        (root / ".factory-state/loop-mode").write_text("implementation\n", encoding="utf-8")
        supervision = root / ".factory-state/ralph-supervision-implementation.json"
        supervision.write_text('{"progress":"preserve"}\n', encoding="utf-8")
        progress = supervision.read_bytes()
        helper = root / "scripts/ralph-event-boundary.py"
        env = {"FACTORY_RALPH_ATTEMPT_ID": HEX, "FACTORY_RALPH_CYCLE_ID": "b" * 64}

        contaminated = (
            {"topic": "LOOP_COMPLETE", "payload": "indirect emit topic"},
            {"topic": "factory.implement", "payload": ["/absolute/bin/ralph", "emit", "LOOP_COMPLETE"]},
            {"topic": "factory.implement", "payload": {"parts": ["LOOP_", "COMPLETE"]}},
            {
                "ts": "2026-08-19T00:00:01+00:00",
                "iteration": 0,
                "hat": "loop",
                "topic": "factory.implement",
                "triggered": "planner",
                "payload": "spoofed later LOOP_COMPLETE",
            },
        )
        for bad in contaminated:
            run([str(helper), "begin", "implementation"], root, env=env)
            with event_path.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(event_record("factory.implement", prompt, iteration=0)) + "\n")
                stream.write(json.dumps(bad) + "\n")
            rejected = run([str(helper), "finish", "implementation"], root, env=env, check=False)
            assert rejected.returncode != 0 and (
                "contaminated" in rejected.stderr or "allowlist" in rejected.stderr
                or "invalid schema" in rejected.stderr
            )
            assert supervision.read_bytes() == progress
            assert not (root / ".factory-state/ralph-launch-handshake-implementation.json").exists()

        # A clean receiver stream accepts the one exact starting prompt and
        # creates the launch handshake.
        run([str(helper), "begin", "implementation"], root, env=env)
        with event_path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(event_record("factory.implement", prompt, iteration=0)) + "\n")
            summary = {key: 0 for key in (
                "cache_read_tokens", "cache_write_tokens", "context_pct", "context_tokens",
                "context_window", "cost_usd", "duration_ms", "input_tokens", "num_turns",
                "output_tokens",
            )}
            stream.write(json.dumps(event_record("iteration.summary", json.dumps(summary), iteration=1)) + "\n")
        run([str(helper), "finish", "implementation"], root, env=env)
        handshake = json.loads(
            (root / ".factory-state/ralph-launch-handshake-implementation.json").read_text(encoding="utf-8")
        )
        assert handshake["schema"] == "ralph-launch-handshake/v2"
        assert handshake["attempt_id"] == HEX and handshake["campaign_state_sha256"] is None
        assert handshake["events_size"] == handshake["events_offset"] + handshake["events_delta_size"]
        assert handshake["start_record_nonce"] == HEX

        event_path.chmod(0o664)
        rejected = run([str(helper), "begin", "implementation"], root, env=env, check=False)
        assert rejected.returncode != 0 and "unsafe Ralph event stream" in rejected.stderr
        event_path.chmod(0o644)
        (root / ".ralph/current-events").chmod(0o666)
        rejected = run([str(helper), "begin", "implementation"], root, env=env, check=False)
        assert rejected.returncode != 0 and "unsafe Ralph marker" in rejected.stderr
        (root / ".ralph/current-events").chmod(0o644)
        (root / ".ralph").chmod(0o775)
        rejected = run([str(helper), "begin", "implementation"], root, env=env, check=False)
        assert rejected.returncode != 0 and "unsafe .ralph directory" in rejected.stderr
    finally:
        shutil.rmtree(root)


def init_git(root: Path) -> None:
    run(["git", "init", "-q", "-b", "develop"], root)
    run(["git", "config", "user.name", "test"], root)
    run(["git", "config", "user.email", "test@example.invalid"], root)


def test_verifier_executable_blob_binding() -> None:
    root = Path(tempfile.mkdtemp(prefix="factory-verifier-test."))
    try:
        (root / ".factory").mkdir()
        copy_scripts(root, "campaign-verifier-binding.py")
        verifier = root / "scripts/verify-project.sh"
        verifier.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
        verifier.chmod(0o755)
        (root / ".factory/config.toml").write_text(
            '[verification]\ncampaign_command = ["./scripts/verify-project.sh", "--strict"]\n',
            encoding="utf-8",
        )
        (root / ".factory/verifier-acceptance.json").write_text(
            json.dumps({"schema": "ralph-verifier-acceptance/v1", "gates": [{"name": "test-one.sh", "args": []}]}),
            encoding="utf-8",
        )
        init_git(root)
        run(["git", "add", "."], root)
        run(["git", "commit", "-qm", "base"], root)
        helper = root / "scripts/campaign-verifier-binding.py"
        binding = json.loads(run([str(helper)], root).stdout)
        digest = binding["sha256"]
        assert binding["helper"] is None  # direct pathname invocation performs no self-binding
        run([str(helper), "--expected-digest", digest, "--exec"], root)
        assert binding["binding"]["executable_blob"] == run(
            ["git", "rev-parse", "HEAD:scripts/verify-project.sh"], root
        ).stdout.strip()

        verifier.write_text("#!/usr/bin/env bash\nexit 7\n", encoding="utf-8")
        rejected = run([str(helper), "--expected-digest", digest, "--exec"], root, check=False)
        assert rejected.returncode != 0 and (
            "committed blob" in rejected.stderr or "binding changed" in rejected.stderr
        )
        run(["git", "restore", "--", "scripts/verify-project.sh"], root)

        verifier.chmod(0o775)
        rejected = run([str(helper)], root, check=False)
        assert rejected.returncode != 0 and "unsafe file" in rejected.stderr
        verifier.chmod(0o755)

        config = root / ".factory/config.toml"
        config.write_text(config.read_text(encoding="utf-8") + "# changed\n", encoding="utf-8")
        rejected = run([str(helper), "--expected-digest", digest, "--exec"], root, check=False)
        assert rejected.returncode != 0 and (
            "committed blob" in rejected.stderr or "binding changed" in rejected.stderr
        )
        run(["git", "restore", "--", ".factory/config.toml"], root)
        config.chmod(0o664)
        rejected = run([str(helper)], root, check=False)
        assert rejected.returncode != 0 and "unsafe file" in rejected.stderr
        config.chmod(0o644)

        external = root / "external-verifier"
        external.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
        external.chmod(0o755)
        verifier.unlink()
        verifier.symlink_to(external)
        rejected = run([str(helper)], root, check=False)
        assert rejected.returncode != 0 and "symlink" in rejected.stderr
    finally:
        shutil.rmtree(root)


def test_retained_helper_fd_immune_to_pathname_swap() -> None:
    root = Path(tempfile.mkdtemp(prefix="factory-helper-race."))
    try:
        (root / ".factory").mkdir()
        copy_scripts(root, "campaign-verifier-binding.py")
        verifier = root / "scripts/verify-project.sh"
        verifier.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
        verifier.chmod(0o755)
        (root / ".factory/config.toml").write_text(
            '[verification]\ncampaign_command = ["./scripts/verify-project.sh", "--strict"]\n',
            encoding="utf-8",
        )
        (root / ".factory/verifier-acceptance.json").write_text(
            json.dumps({"schema": "ralph-verifier-acceptance/v1", "gates": [{"name": "test-one.sh", "args": []}]}),
            encoding="utf-8",
        )
        init_git(root)
        run(["git", "add", "."], root)
        run(["git", "commit", "-qm", "base"], root)
        helper = root / "scripts/campaign-verifier-binding.py"
        original_bytes = helper.read_bytes()
        original_blob = run(
            ["git", "rev-parse", "HEAD:scripts/campaign-verifier-binding.py"], root
        ).stdout.strip()
        fd = os.open(helper, os.O_RDONLY)
        try:
            os.set_inheritable(fd, True)
            # Replace the helper pathname before invocation. The retained
            # descriptor still refers to the exact committed inode opened at
            # campaign startup, so the original helper runs and self-binds;
            # the substitute never runs.
            helper.rename(root / "scripts/campaign-verifier-binding.py.original")
            substitute = root / "scripts/campaign-verifier-binding.py"
            substitute.write_text(
                "#!/usr/bin/env python3\nprint('SUBSTITUTE-HELPER-RAN')\n", encoding="utf-8"
            )
            substitute.chmod(0o755)
            result = run([f"/proc/self/fd/{fd}"], root, pass_fds=(fd,))
            # A second invocation through the same retained descriptor must
            # still bind: the shared open-file description position is rewound
            # before each self-binding read.
            repeated = run([f"/proc/self/fd/{fd}"], root, pass_fds=(fd,))
            assert json.loads(repeated.stdout)["sha256"] == json.loads(result.stdout)["sha256"]
        finally:
            os.close(fd)
        assert "SUBSTITUTE-HELPER-RAN" not in result.stdout
        binding = json.loads(result.stdout)
        assert binding["binding"]["schema"] == "campaign-verifier-binding/v1"
        assert binding["helper"] == {
            "path": "scripts/campaign-verifier-binding.py",
            "sha256": hashlib.sha256(original_bytes).hexdigest(),
            "blob": original_blob,
            "mode": "0755",
        }
        assert binding["sha256"]
    finally:
        shutil.rmtree(root)


def test_verifier_swap_in_final_exec_race_runs_original() -> None:
    root = Path(tempfile.mkdtemp(prefix="factory-verifier-race."))
    try:
        (root / ".factory").mkdir()
        copy_scripts(root, "campaign-verifier-binding.py")
        verifier = root / "scripts/verify-project.sh"
        verifier.write_text(
            "#!/usr/bin/env bash\necho ORIGINAL-VERIFIER-RAN\nexit 0\n", encoding="utf-8"
        )
        verifier.chmod(0o755)
        (root / ".factory/config.toml").write_text(
            '[verification]\ncampaign_command = ["./scripts/verify-project.sh", "--strict"]\n',
            encoding="utf-8",
        )
        (root / ".factory/verifier-acceptance.json").write_text(
            json.dumps({"schema": "ralph-verifier-acceptance/v1", "gates": [{"name": "test-one.sh", "args": []}]}),
            encoding="utf-8",
        )
        init_git(root)
        run(["git", "add", "."], root)
        run(["git", "commit", "-qm", "base"], root)
        driver = root / "scripts/race-driver.py"
        driver.write_text(
            """import importlib.util, os, sys
from pathlib import Path
root = Path(sys.argv[1])
spec = importlib.util.spec_from_file_location('cvb', root / 'scripts/campaign-verifier-binding.py')
cvb = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cvb)
verifier = root / 'scripts/verify-project.sh'
real_execve = os.execve
def racing_execve(path, argv, env):
    # The final validation->exec race: swap the verifier pathname now.
    verifier.rename(root / 'scripts/verify-project.sh.original')
    substitute = root / 'scripts/verify-project.sh'
    substitute.write_text('#!/usr/bin/env bash\\necho SUBSTITUTE-VERIFIER-RAN\\nexit 7\\n')
    substitute.chmod(0o755)
    return real_execve(path, argv, env)
os.execve = racing_execve
_binding, digest, command, executable, executable_bytes = cvb.binding()
cvb.execute_verified(command, executable, executable_bytes)
""",
            encoding="utf-8",
        )
        result = run([sys.executable, str(driver), str(root)], root, check=False)
        assert result.returncode == 0, (result.returncode, result.stdout, result.stderr)
        assert "ORIGINAL-VERIFIER-RAN" in result.stdout
        assert "SUBSTITUTE-VERIFIER-RAN" not in result.stdout
    finally:
        shutil.rmtree(root)


def test_one_time_supervision_migration() -> None:
    root = Path(tempfile.mkdtemp(prefix="factory-migration-test."))
    try:
        (root / ".factory").mkdir()
        (root / ".factory-state").mkdir(mode=0o700)
        (root / ".ralph").mkdir()
        copy_scripts(
            root,
            "campaign-verifier-binding.py",
            "factory_lock.py",
            "factory_state_io.py",
            "ralph-campaign-state.py",
            "ralph-supervision-migrate.py",
            "ralph-supervision.sh",
        )
        verifier = root / "scripts/verify-project.sh"
        verifier.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
        verifier.chmod(0o755)
        (root / ".factory/config.toml").write_text(
            '[verification]\ncampaign_command = ["./scripts/verify-project.sh"]\n', encoding="utf-8"
        )
        (root / ".factory/verifier-acceptance.json").write_text(
            json.dumps({"schema": "ralph-verifier-acceptance/v1", "gates": [{"name": "test-one.sh", "args": []}]}),
            encoding="utf-8",
        )
        (root / ".gitignore").write_text(".factory-state/\n", encoding="utf-8")
        (root / "history").write_text("base\n", encoding="utf-8")
        init_git(root)
        run(["git", "add", "."], root)
        run(["git", "commit", "-qm", "base"], root)
        base = run(["git", "rev-parse", "HEAD"], root).stdout.strip()
        (root / "history").write_text("base\nplan\n", encoding="utf-8")
        run(["git", "add", "history"], root)
        run(["git", "commit", "-qm", "plan"], root)
        plan = run(["git", "rev-parse", "HEAD"], root).stdout.strip()
        state = {
            "schema": "ralph-campaign/v2",
            "status": "active",
            "rounds_requested": 2,
            "round": 1,
            "phase": "implementation",
            "tui": False,
            "verification_command_sha256": "c" * 64,
            "rounds": [{
                "number": 1,
                "base_commit": base,
                "planning_started": True,
                "plan_commit": plan,
                "implementation_started": True,
                "implementation_commit": None,
                "verification_commit": None,
                "runner_evidence_sha256": None,
                "audit_started": False,
                "audit_commit": None,
                "audit_result": None,
            }],
        }
        campaign = root / ".factory-state/ralph-campaign.json"
        campaign.write_text(json.dumps(state, sort_keys=True, indent=2) + "\n", encoding="utf-8")
        original = campaign.read_bytes()
        digest = hashlib.sha256(original).hexdigest()
        durable = root / ".factory-state/ralph-supervision-implementation.json"
        durable.write_text(
            json.dumps({
                "schema": "ralph-supervision/v1",
                "mode": "implementation",
                "stale_recoveries": 1,
                "completion_recoveries": 2,
                "no_progress_recoveries": 3,
            }) + "\n",
            encoding="utf-8",
        )
        helper = root / "scripts/ralph-supervision-migrate.py"
        arguments = [str(helper), "--mode", "implementation", "--expected-campaign-sha256", digest]
        run(arguments, root)
        migrated = json.loads(durable.read_text(encoding="utf-8"))
        assert [migrated[key] for key in (
            "stale_recoveries", "completion_recoveries", "no_progress_recoveries"
        )] == [1, 2, 3]
        assert campaign.read_bytes() == original

        marker = root / ".factory-state/ralph-supervision-migration-implementation.json"
        receipt = json.loads(marker.read_text(encoding="utf-8"))
        continuation = run(
            [
                "bash", "-c",
                "source scripts/ralph-supervision.sh; "
                "RALPH_SUPERVISION_INITIALIZED=true; "
                "RALPH_SUPERVISION_STATE_MODE=implementation; "
                "export FACTORY_RALPH_CYCLE_ID=\"$1\"; "
                "ralph_supervision_should_continue implementation",
                "migration-continuation", receipt["cycle_id"],
            ],
            root,
        )
        assert continuation.returncode == 0
        state_after = durable.read_bytes()
        marker.unlink()  # Simulate interruption between the two durable writes.
        run(arguments, root)
        assert durable.read_bytes() == state_after and marker.is_file()
        rejected = run(arguments, root, check=False)
        assert rejected.returncode != 0 and "already migrated" in rejected.stderr
        assert campaign.read_bytes() == original

        receipt = json.loads(marker.read_text(encoding="utf-8"))
        run(
            [
                str(root / "scripts/ralph-campaign-state.py"),
                "promote-verifier-binding",
                "--mode", "implementation",
                "--expected-old", "c" * 64,
                "--new", receipt["verification_binding_sha256"],
            ],
            root,
        )
        promoted = json.loads(campaign.read_text(encoding="utf-8"))
        assert promoted["verification_command_sha256"] == receipt["verification_binding_sha256"]
        assert marker.is_file()
        run([str(root / "scripts/ralph-campaign-state.py"), "show"], root)
    finally:
        shutil.rmtree(root)


def main() -> None:
    test_symlink_safe_state_markers()
    test_state_removal_quarantines_post_check_substitution()
    test_loop_lock_process_identity_and_inode_race()
    test_receiver_side_token_contamination()
    test_verifier_executable_blob_binding()
    test_retained_helper_fd_immune_to_pathname_swap()
    test_verifier_swap_in_final_exec_race_runs_original()
    test_one_time_supervision_migration()
    print("test: orchestration security checks passed")


if __name__ == "__main__":
    main()
