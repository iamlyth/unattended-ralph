"""Preflight checks before a campaign (spec section 13.3).

Simple "is it ready to go?" checks. No readiness policy, no production
authority enrollment, no signed trust chain. Any failure is reported as
`infrastructure_failure` by the orchestrator.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

PLACEHOLDER_MARKER = "SPEC_PENDING_HUMAN_SUPPLY"

@dataclass
class PreflightResult:
    passed: bool
    failures: list[str] = field(default_factory=list)


def _git(root: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=str(root),
                          capture_output=True, text=True, timeout=30)

def _check_spec(root: Path, config: dict, failures: list[str]) -> None:
    spec_rel = config.get("project", {}).get("spec")
    if not spec_rel:
        failures.append("config.toml has no project.spec path")
        return
    spec = root / spec_rel
    if not spec.is_file():
        failures.append(f"spec file does not exist: {spec_rel}")
        return
    try:
        text = spec.read_text(encoding="utf-8")
    except OSError as exc:
        failures.append(f"spec file is not readable: {spec_rel} ({exc})")
        return
    if PLACEHOLDER_MARKER in text:
        failures.append(f"spec file is still the placeholder (contains "
                        f"{PLACEHOLDER_MARKER!r}): {spec_rel}")


def _check_branch(root: Path, config: dict, failures: list[str]) -> None:
    branch = config.get("project", {}).get("development_branch")
    if branch:
        cur = _git(root, "rev-parse", "--abbrev-ref", "HEAD")
        if cur.returncode == 0 and cur.stdout.strip() != branch:
            failures.append(f"development branch mismatch: on "
                            f"{cur.stdout.strip()!r}, expected {branch!r}")
    status = _git(root, "status", "--porcelain")
    if status.returncode != 0:
        failures.append("git status failed; is this a git repository?")
    elif status.stdout.strip():
        failures.append("development branch is not clean (uncommitted changes)")
    if _git(root, "log", "--oneline", "-1").returncode != 0:
        failures.append("development branch has no commits")


def _check_runners(runners: list, failures: list[str]) -> None:
    for runner in runners:
        name = getattr(runner, "name", None) or repr(runner)
        check = getattr(runner, "check_runner_available", None)
        if callable(check):
            try:
                ok = check()
            except Exception as exc:  # noqa: BLE001 - surface connectivity error
                failures.append(f"runner {name!r} unreachable: {exc}")
                continue
            if not ok:
                failures.append(f"runner {name!r} is not reachable")
            continue
        alias = getattr(runner, "ssh_config_alias", None)
        if not alias:
            failures.append(f"runner {name!r} has no reachability check")
            continue
        probe = subprocess.run(
            ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", alias, "true"],
            capture_output=True, text=True, timeout=30)
        if probe.returncode != 0:
            failures.append(f"runner {name!r} is not reachable over SSH")


def _check_verification(root: Path, config: dict, failures: list[str]) -> None:
    cmd = config.get("verification", {}).get("command") or \
        config.get("verification", {}).get("campaign_command")
    if not cmd:
        failures.append("config.toml has no verification.command")
        return
    path = Path(cmd[0])
    if not path.is_absolute():
        path = root / path
    if not path.is_file():
        failures.append(f"verification command does not exist: {cmd[0]}")
    elif not os.access(str(path), os.X_OK):
        failures.append(f"verification command is not executable: {cmd[0]}")


def _check_plan(root: Path, config: dict, failures: list[str]) -> None:
    plan_rel = config.get("project", {}).get("plan")
    if not plan_rel:
        failures.append("config.toml has no project.plan path")
        return
    # OK if it does not exist yet; the first planning round creates it.
    if (root / plan_rel).exists() and not (root / plan_rel).is_file():
        failures.append(f"plan path exists but is not a file: {plan_rel}")


def run_preflight(root: str | Path, config: dict, runners: list) -> PreflightResult:
    """Run all preflight checks. Returns a PreflightResult."""
    root = Path(root)
    failures: list[str] = []
    _check_spec(root, config, failures)
    _check_branch(root, config, failures)
    _check_runners(runners, failures)
    _check_verification(root, config, failures)
    _check_plan(root, config, failures)
    return PreflightResult(passed=not failures, failures=failures)
