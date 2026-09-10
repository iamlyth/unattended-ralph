#!/usr/bin/env python3
"""Minimal Ralph factory campaign orchestrator.

Ties together the control-plane modules (plan_parser, selector, state,
gitutil, lock, runner, preflight) into the finite campaign loop described in
spec sections 7, 9, 11, 13 and 15.

Each round runs: planning -> implementation -> verification -> audit.
The orchestrator is the sole Git writer and the sole reader/writer of the
control-state file. Model prose is never control protocol: outcomes are
derived from plan state, Git state, exit status, and verification results.

Terminal outcomes (spec 9.2): success, findings, blocked, failed,
interrupted, infrastructure_failure.
"""
from __future__ import annotations

import argparse
import subprocess
import sys
import time
import tomllib
from pathlib import Path

from .plan_parser import parse, dump, Plan, Task
from .selector import select
from .state import load, save, validate, State
from . import gitutil
from .lock import Lock
from .runner import (
    Runner,
    load_environment,
    get_available_capabilities,
    run_verification,
    VerificationResult,
)
from .preflight import run_preflight

ROOT = Path(__file__).resolve().parents[2]
CONFIG_PATH = ROOT / ".factory" / "config.toml"
ENV_PATH = ROOT / ".factory" / "environment.toml"
STATE_PATH = ROOT / ".factory-state" / "factory-loop.json"
PLAN_PATH = ROOT / ".factory" / "artifacts" / "implementation-plan.md"
PROMPTS_DIR = ROOT / ".factory" / "prompts"
FINDINGS_PATH = ROOT / ".factory" / "artifacts" / "audit-findings.md"

ROLE_TIMEOUT = 600  # seconds per role invocation

# Exit codes for terminal outcomes. Preflight failure exits 2.
TERMINAL_EXIT = {
    "success": 0,
    "findings": 1,
    "blocked": 1,
    "failed": 1,
    "interrupted": 1,
    "infrastructure_failure": 1,
}


class _RoleResult:
    """Minimal stand-in for a subprocess result (also used on launch errors)."""

    def __init__(self, returncode: int, stdout: str, stderr: str) -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def load_config() -> dict:
    """Load config.toml via tomllib."""
    with open(CONFIG_PATH, "rb") as handle:
        return tomllib.load(handle)


def invoke_role(role: str, provider: str, model: str, root: Path,
                extra: str = "") -> _RoleResult:
    """Invoke a role in a fresh context (spec 7).

    The role prompt is the only input channel; ``extra`` (e.g. the selected
    task excerpt) is appended to the prompt. Captures stdout/stderr/exit code.
    """
    prompt_path = root / ".factory" / "prompts" / f"{role}.md"
    try:
        prompt = prompt_path.read_text(encoding="utf-8")
    except OSError as exc:
        return _RoleResult(127, "", f"cannot read prompt {prompt_path}: {exc}")
    if extra:
        prompt += "\n\n" + extra
    cmd = [
        "pi2", "--provider", provider, "--model", model,
        "--print", "--no-session", "--approve",
    ]
    try:
        result = subprocess.run(
            cmd,
            input=prompt,
            capture_output=True,
            text=True,
            cwd=str(root),
            timeout=ROLE_TIMEOUT,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return _RoleResult(127, "", f"role invocation failed: {exc}")
    return _RoleResult(result.returncode, result.stdout or "", result.stderr or "")


def task_excerpt(task: Task) -> str:
    """Render a task verbatim for the developer/auditor prompt."""
    deps = ", ".join(str(d) for d in task.dependencies) if task.dependencies else "none"
    return "\n".join([
        f"## Task {task.id}: {task.title}",
        f"Title: {task.title}",
        f"Status: {task.status}",
        f"Dependencies: {deps}",
        f"Acceptance: {task.acceptance}",
        f"Verification: {task.verification}",
        f"Runner: {task.runner or 'none'}",
        f"Evidence: {task.evidence or 'none'}",
    ])


def config_verify_command(config: dict) -> str:
    """The overall verification command string from config (spec 13.1)."""
    cmd = config.get("verification", {}).get("command") or []
    if isinstance(cmd, list):
        return " ".join(cmd)
    return str(cmd) if cmd else ""


def find_runner_for_capability(runners: list, capability: str) -> Runner | None:
    """Return the first runner declaring the given capability."""
    for runner in runners:
        if capability in runner.capabilities:
            return runner
    return None


def run_task_verification(task: Task, runners: list, root: Path,
                          commit: str) -> VerificationResult:
    """Run a task's verification locally or on a runner (spec 8.3)."""
    if task.runner:
        runner = find_runner_for_capability(runners, task.runner)
        if runner is None:
            return VerificationResult(
                exit_code=127,
                stdout="",
                stderr=f"runner-unavailable: {task.runner}",
                runner=task.runner,
                command=task.verification,
            )
        return run_verification(runner, task.verification, root, commit)
    local = Runner(
        name="local",
        transport="local",
        ssh_config_alias="",
        working_directory="",
        capabilities=[],
        verify_command="",
    )
    return run_verification(local, task.verification, root, commit)


def _finalize_success(plan: Plan, config: dict, env: dict, args,
                      state: State) -> str:
    """work_exhausted: run overall verification + audit, then decide.

    Never silently succeeds (spec 9.2). Returns a terminal outcome.
    """
    commit = gitutil.current_commit(ROOT)
    vcmd = config_verify_command(config)
    local = Runner(
        name="local", transport="local", ssh_config_alias="",
        working_directory="", capabilities=[], verify_command="",
    )
    vresult = run_verification(local, vcmd, ROOT, commit)
    if vresult.exit_code != 0:
        return "failed"
    aud = invoke_role("auditor", args.provider, args.model, ROOT,
                      "Final audit: all plan tasks are complete.")
    if aud.returncode != 0:
        return "findings"
    return "success"


def run_campaign(args, config: dict, env: dict) -> int:
    """Run the full campaign loop. Returns the process exit code."""
    # 1. Preflight (spec 13.3). Failure -> exit 2.
    preflight = run_preflight(ROOT, config, env["runners"])
    if not preflight.passed:
        for failure in preflight.failures:
            print(f"preflight failure: {failure}", file=sys.stderr)
        return 2

    with Lock(ROOT):
        # 2. Load or create control state (spec 10).
        state = load(STATE_PATH)
        if state.campaign_id and state.campaign_id != args.campaign_id:
            state = State(campaign_id=args.campaign_id)
        elif not state.campaign_id:
            state.campaign_id = args.campaign_id
        validate(state)

        # 3. Switch to the development branch (spec 11).
        branch = args.branch or config.get("project", {}).get(
            "development_branch", "develop")
        gitutil.switch_branch(ROOT, branch)

        start = time.time()
        outcome: str | None = None
        reason: str | None = None

        try:
            for round_num in range(state.current_round, args.rounds + 1):
                if time.time() - start > args.timeout:
                    outcome, reason = "interrupted", "campaign timeout"
                    break

                state.current_round = round_num
                state.current_phase = "planning"
                state.last_outcome = None
                save(STATE_PATH, state)

                # 6a. PLANNING
                plan_result = invoke_role("planner", args.provider,
                                          args.model, ROOT)
                if plan_result.returncode != 0:
                    outcome = "failed"
                    reason = (f"planning failed (exit "
                              f"{plan_result.returncode})")
                    break
                try:
                    plan = parse(PLAN_PATH)
                except ValueError as exc:
                    outcome = "failed"
                    reason = f"plan parse failed: {exc}"
                    break

                # 6c/6d. Runner availability + deterministic selection.
                caps = get_available_capabilities(env["runners"])
                sel = select(plan.tasks, caps)

                # 6e. work_exhausted -> verification + audit, then success.
                if sel.status == "work_exhausted":
                    outcome = _finalize_success(plan, config, env, args, state)
                    reason = "all tasks complete"
                    break

                # 6f. blocked -> terminate blocked.
                if sel.status == "blocked":
                    outcome = "blocked"
                    reason = sel.reason
                    break

                task = plan.get_task(sel.task_id)
                state.selected_task_id = task.id
                state.current_phase = "implementation"
                save(STATE_PATH, state)

                # 6g-6k. IMPLEMENTATION + VERIFICATION with attempt budget.
                verified = False
                for attempt in range(1, args.attempts + 1):
                    state.attempt_number = attempt
                    save(STATE_PATH, state)

                    dev = invoke_role("developer", args.provider, args.model,
                                      ROOT, task_excerpt(task))
                    commit = gitutil.commit_all(
                        ROOT,
                        f"factory: task {task.id} round {round_num} "
                        f"attempt {attempt}",
                    )

                    state.current_phase = "verification"
                    save(STATE_PATH, state)
                    vresult = run_task_verification(task, env["runners"],
                                                    ROOT, commit)
                    if vresult.exit_code == 0:
                        verified = True
                        break
                    # Verification failed; retry if attempts remain.

                if not verified:
                    outcome = "failed"
                    reason = (f"task {task.id} failed verification after "
                              f"{args.attempts} attempts")
                    break

                # Mark the task completed in the sole task ledger.
                task.status = "completed"
                task.evidence = (f"verification exit {vresult.exit_code} "
                                f"on {vresult.runner}")
                PLAN_PATH.write_text(dump(plan), encoding="utf-8")

                # 6l. AUDIT.
                state.current_phase = "audit"
                save(STATE_PATH, state)
                aud = invoke_role("auditor", args.provider, args.model, ROOT,
                                  task_excerpt(task))
                audit_findings = aud.returncode != 0
                if audit_findings:
                    FINDINGS_PATH.write_text(
                        f"# Audit findings (round {round_num}, task "
                        f"{task.id})\n\n{aud.stdout}\n{aud.stderr}\n",
                        encoding="utf-8",
                    )

                # 6n. Commit checkpoint.
                gitutil.commit_all(ROOT, f"factory: checkpoint round {round_num}")

                # 6o. Update state, advance round.
                state.rounds_completed = round_num
                state.current_round = round_num + 1
                state.last_outcome = ("audit_findings" if audit_findings
                                      else "audit_pass")
                save(STATE_PATH, state)
        except KeyboardInterrupt:
            outcome, reason = "interrupted", "process interrupted"

        # 7. Rounds exhausted without a terminal outcome.
        if outcome is None:
            if state.last_outcome == "audit_findings":
                outcome, reason = "findings", "rounds exhausted with audit findings"
            else:
                outcome, reason = "failed", "rounds exhausted before completion"

        state.terminal_outcome = outcome
        state.current_phase = "terminal"
        save(STATE_PATH, state)

    print(f"campaign outcome: {outcome}"
          + (f" ({reason})" if reason else ""))
    return TERMINAL_EXIT.get(outcome, 1)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="campaign.py",
        description="Minimal Ralph factory campaign orchestrator",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    run_p = sub.add_parser("run", help="run a finite campaign")
    run_p.add_argument("--campaign-id", required=True)
    run_p.add_argument("--rounds", type=int, default=None)
    run_p.add_argument("--branch", default=None)
    run_p.add_argument("--provider", required=True)
    run_p.add_argument("--model", required=True)
    run_p.add_argument("--attempts", type=int, default=None)
    run_p.add_argument("--timeout", type=int, default=None)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    config = load_config()
    env = load_environment(ENV_PATH)
    if args.command != "run":
        return 1
    camp = config.get("campaign", {})
    if args.rounds is None:
        args.rounds = camp.get("default_rounds", 20)
    if args.attempts is None:
        args.attempts = camp.get("default_attempts", 3)
    if args.timeout is None:
        args.timeout = camp.get("default_timeout", 21600)
    return run_campaign(args, config, env)


if __name__ == "__main__":
    sys.exit(main())
