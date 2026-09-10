"""SSH runner management with capability awareness (spec section 8).

Runners are declared in ``.factory/environment.toml``. Verification that needs
a runner capability runs on the runner: the repo is synced via rsync over SSH,
then the command runs there via SSH. Local verification runs via subprocess.
Unreachable runners are reported unavailable (blocked), never silently skipped
or faked as passing. Stdlib + ssh/rsync only.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import subprocess
import tomllib
from typing import Any

SSH_OPTIONS = ("-o", "ConnectTimeout=5", "-o", "BatchMode=yes")

# Never sync VCS metadata, mutable control state, or local-only artifacts.
RSYNC_EXCLUDES = (".git", ".factory-state", ".factory-state/", "__pycache__")


@dataclass
class Runner:
    name: str
    transport: str  # "ssh" or "local"
    ssh_config_alias: str  # SSH alias, used when transport == "ssh"
    working_directory: str  # path on the runner
    capabilities: list[str] = field(default_factory=list)
    verify_command: str = field(default="")  # command to run for verification


@dataclass
class VerificationResult:
    """Captured outcome of a verification run."""

    exit_code: int
    stdout: str
    stderr: str
    runner: str  # "local" or runner name
    command: str  # the actual command that was run


def _runner_from_table(table: dict[str, Any]) -> Runner:
    """Build a Runner, normalizing ``verify_argv`` (list) to ``verify_command``."""
    verify_command = table.get("verify_command", "")
    if not verify_command and table.get("verify_argv"):
        verify_command = " ".join(table["verify_argv"])
    return Runner(
        name=table["name"],
        transport=table.get("transport", "ssh"),
        ssh_config_alias=table.get("ssh_config_alias", ""),
        working_directory=table.get("working_directory", ""),
        capabilities=list(table.get("capabilities", [])),
        verify_command=verify_command,
    )


def load_environment(env_path: str | Path) -> dict[str, Any]:
    """Parse environment.toml -> ``{'runners': [...], 'tools': [...]}``."""
    path = Path(env_path)
    if not path.is_file():
        return {"runners": [], "tools": []}
    with path.open("rb") as handle:
        data = tomllib.load(handle)
    runners = [_runner_from_table(table) for table in data.get("runners", [])]
    tools = list(data.get("tools", []))
    return {"runners": runners, "tools": tools}


def _ssh_argv(alias: str, remote_command: str) -> list[str]:
    return ["ssh", *SSH_OPTIONS, alias, remote_command]


def check_runner_available(runner: Runner, timeout: int = 5) -> bool:
    """Check if a runner is reachable via ``ssh {alias} true``."""
    if runner.transport != "ssh" or not runner.ssh_config_alias:
        return False
    try:
        proc = subprocess.run(
            _ssh_argv(runner.ssh_config_alias, "true"),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return proc.returncode == 0


def get_available_capabilities(runners: list[Runner]) -> set[str]:
    """Set of capabilities from all reachable runners."""
    available: set[str] = set()
    for runner in runners:
        if check_runner_available(runner):
            available.update(runner.capabilities)
    return available


def sync_to_runner(runner: Runner, root: str | Path) -> bool:
    """Sync the repo to the runner's working dir via rsync over SSH."""
    if runner.transport != "ssh" or not runner.ssh_config_alias:
        return False
    source = f"{Path(root)}/"
    destination = f"{runner.ssh_config_alias}:{runner.working_directory}/"
    argv = ["rsync", "-az", "--delete"]
    for exclude in RSYNC_EXCLUDES:
        argv.append(f"--exclude={exclude}")
    argv.extend([source, destination])
    try:
        proc = subprocess.run(
            argv,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    except OSError:
        return False
    return proc.returncode == 0


def _blocked(runner: Runner, reason: str, command: str) -> VerificationResult:
    """A blocked result: an unusable runner is never a silent pass."""
    return VerificationResult(
        exit_code=127,
        stdout="",
        stderr=f"runner-unavailable: {runner.name} ({reason})",
        runner=runner.name,
        command=command,
    )


def run_verification(
    runner: Runner, command: str, root: str | Path, commit: str
) -> VerificationResult:
    """Run verification on a runner, capturing stdout/stderr/exit code.

    ``local`` runs ``command`` via subprocess in ``root``; ``ssh`` syncs the
    repo to the runner's working dir, then runs ``cd {workdir} && {command}``
    on the runner. ``commit`` is recorded for traceability.
    """
    if runner.transport == "local":
        return _run_local(command, root, runner.name)

    if runner.transport != "ssh" or not runner.ssh_config_alias:
        return _blocked(runner, f"transport {runner.transport!r}", command)

    if not sync_to_runner(runner, root):
        return _blocked(runner, "rsync sync failed", command)

    remote_command = f"cd {runner.working_directory} && {command}"
    try:
        proc = subprocess.run(
            _ssh_argv(runner.ssh_config_alias, remote_command),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
    except OSError as exc:
        return _blocked(runner, f"ssh failed: {exc}", remote_command)

    return VerificationResult(
        exit_code=proc.returncode,
        stdout=proc.stdout or "",
        stderr=proc.stderr or "",
        runner=runner.name,
        command=remote_command,
    )


def _run_local(command: str, root: str | Path, runner_name: str) -> VerificationResult:
    try:
        proc = subprocess.run(
            command,
            cwd=str(root),
            shell=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
    except OSError as exc:
        return VerificationResult(
            exit_code=127,
            stdout="",
            stderr=f"local verification failed: {exc}",
            runner=runner_name,
            command=command,
        )
    return VerificationResult(
        exit_code=proc.returncode,
        stdout=proc.stdout or "",
        stderr=proc.stderr or "",
        runner=runner_name,
        command=command,
    )
