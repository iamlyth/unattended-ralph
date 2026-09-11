#!/usr/bin/env python3
"""Parallel Ralph factory campaign orchestrator.

Ties together the control-plane modules (plan_parser, selector, state,
gitutil, lock, runner, preflight, parallel) into a finite campaign loop
with parallel study, implementation, and audit phases.

Each round runs:
  1. PLANNING: parallel study subagents → planner synthesises plan.
  2. SELECTION: deterministic task selection (model never chooses).
  3. IMPLEMENTATION: parallel developers → integration developer commits.
  4. VERIFICATION: task verification on local or runner hardware.
  5. AUDIT: parallel specialist auditors → findings.

The orchestrator is the sole Git writer and the sole reader/writer of the
control-state file.  Model prose is never control protocol: outcomes are
derived from plan state, Git state, exit status, and verification results.
"""
from __future__ import annotations

import argparse
import shutil
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
    _wrap_nix_shell,
)
from .preflight import run_preflight
from .parallel import (
    SubagentResult,
    run_parallel,
    assemble_reports,
    assemble_developer_outputs,
    assemble_audit_findings,
    discover_subsystems,
    load_roles,
)

ROOT = Path(__file__).resolve().parents[2]
CONFIG_PATH = ROOT / ".factory" / "config.toml"
ENV_PATH = ROOT / ".factory" / "environment.toml"
STATE_PATH = ROOT / ".factory-state" / "factory-loop.json"
PLAN_PATH = ROOT / ".factory" / "artifacts" / "implementation-plan.md"
PROMPTS_DIR = ROOT / ".factory" / "prompts"
FINDINGS_PATH = ROOT / ".factory" / "artifacts" / "audit-findings.md"
ROLES_PATH = ROOT / ".factory" / "roles.toml"

ROLE_TIMEOUT = 900  # default seconds per role invocation (15 min)

TERMINAL_EXIT = {
    "success": 0,
    "findings": 1,
    "blocked": 1,
    "failed": 1,
    "interrupted": 1,
    "infrastructure_failure": 1,
}


# ─── Config helpers ───────────────────────────────────────────────────

def load_config() -> dict:
    with open(CONFIG_PATH, "rb") as f:
        return tomllib.load(f)


def config_build_command(config: dict) -> str:
    return str(config.get("verification", {}).get("build_command", ""))


def config_verify_command(config: dict) -> str:
    cmd = config.get("verification", {}).get("command") or []
    if isinstance(cmd, list):
        return " ".join(cmd)
    return str(cmd) if cmd else ""


def config_clean_dirs(config: dict) -> list[str]:
    return list(config.get("verification", {}).get("clean", []))


# ─── Task helpers ─────────────────────────────────────────────────────

def task_excerpt(task: Task) -> str:
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


def find_runner_for_capability(runners: list, cap: str) -> Runner | None:
    for r in runners:
        if cap in r.capabilities:
            return r
    return None


def _strip_markdown_ticks(cmd: str) -> str:
    return cmd.replace("`", "").strip() if cmd else ""


# ─── Verification ─────────────────────────────────────────────────────

def _clean_verification_dirs(root: Path, config: dict) -> None:
    for d in config_clean_dirs(config):
        target = root / d
        if target.is_dir():
            shutil.rmtree(target, ignore_errors=True)
    build_cmd = config_build_command(config)
    if build_cmd:
        wrapped = _wrap_nix_shell(build_cmd, root)
        subprocess.run(
            wrapped, cwd=str(root), shell=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=600, check=False,
        )


def run_task_verification(task: Task, runners: list, root: Path,
                          commit: str, build_command: str = "") -> VerificationResult:
    command = _strip_markdown_ticks(task.verification)
    if task.runner:
        runner = find_runner_for_capability(runners, task.runner)
        if runner is None:
            return VerificationResult(
                exit_code=127, stdout="",
                stderr=f"runner-unavailable: {task.runner}",
                runner=task.runner, command=task.verification,
            )
        return run_verification(runner, command, root, commit,
                                build_command=build_command)
    local = Runner(
        name="local", transport="local", ssh_config_alias="",
        working_directory="", capabilities=[], verify_command="",
    )
    return run_verification(local, command, root, commit)


# ─── Parallel phases ─────────────────────────────────────────────────

def run_planning_phase(roles: dict, config: dict, args, root: Path) -> bool:
    """Run the planning phase: parallel study subagents → planner.

    Returns True if planning succeeded (plan file was written/parsed).
    """
    plan_cfg = roles.get("planning", {})
    timeout = plan_cfg.get("timeout", ROLE_TIMEOUT)
    studies = list(plan_cfg.get("studies", []))

    # Auto-discover subsystems if no explicit subsystem studies are defined.
    has_subsystem_studies = any(s.get("path") for s in studies)
    if not has_subsystem_studies:
        subsystems = discover_subsystems(root)
        studies.extend(subsystems)

    if not studies:
        # No study subagents — run planner directly.
        studies = []

    print(f"  planning: {len(studies)} study subagents", file=sys.stderr)

    # Launch study subagents in parallel (read-only, approve=False).
    def study_context(name: str, sa: dict) -> str:
        ctx = f"Project root: {root}\n"
        if sa.get("path"):
            ctx += f"Focus on the `{sa['path']}` directory.\n"
        if sa.get("description"):
            ctx += f"Task: {sa['description']}\n"
        return ctx

    study_results = run_parallel(
        studies, study_context, args.provider, args.model,
        timeout, cwd=root, approve=False,
    )

    # Assemble study reports.
    study_report = assemble_reports(study_results)

    # Feed study reports to the planner.
    planner_prompt = plan_cfg.get("planner_prompt", ".factory/prompts/planner.md")
    planner_result = run_parallel(
        [{"name": "planner", "prompt": planner_prompt}],
        lambda name, sa: f"## Study Reports\n\n{study_report}\n\n"
                         f"Review discrepancies against the spec and devise "
                         f"an implementation plan at "
                         f".factory/artifacts/implementation-plan.md",
        args.provider, args.model,
        timeout, cwd=root, approve=True,
    )

    if not planner_result or not planner_result[0].success:
        stderr = planner_result[0].stderr if planner_result else "no result"
        print(f"  planning: planner failed: {stderr}", file=sys.stderr)
        return False

    return True


def run_implementation_phase(roles: dict, config: dict, args,
                              task: Task, root: Path) -> str:
    """Run the implementation phase for a single task.

    Parallel developers propose changes → integration developer applies
    and commits.  Returns the commit SHA (or empty string on failure).
    """
    impl_cfg = roles.get("implementation", {})
    timeout = impl_cfg.get("timeout", ROLE_TIMEOUT)
    developers = list(impl_cfg.get("developers", []))

    if not developers:
        developers = [{"name": "default", "prompt": ".factory/prompts/developer.md"}]

    excerpt = task_excerpt(task)

    # Single developer: run directly with --approve (old behaviour).
    if len(developers) == 1:
        print(f"  implementation: 1 developer (serial)", file=sys.stderr)
        dev = developers[0]
        from .parallel import invoke_subagent
        name, exit_code, stdout, stderr = invoke_subagent(
            dev["prompt"], excerpt, args.provider, args.model,
            timeout, cwd=root, approve=True,
        )
        if exit_code != 0:
            print(f"  implementation: developer failed (exit {exit_code})",
                  file=sys.stderr)
            return ""
        return gitutil.commit_all(
            root, f"factory: task {task.id} implementation",
        )

    # Multiple developers: run in parallel, then integration developer.
    print(f"  implementation: {len(developers)} developers (parallel)",
          file=sys.stderr)

    def dev_context(name: str, sa: dict) -> str:
        ctx = excerpt
        paths = sa.get("paths", [])
        if paths:
            ctx += f"\n\n## Your Assigned Area\n\nYou are responsible for " \
                   f"these paths ONLY: {', '.join(paths)}\n" \
                   f"Do not modify files outside your assigned area.\n"
        else:
            ctx += "\n\nYou are responsible for the entire codebase.\n"
        ctx += "\nOutput your proposed changes. Do NOT commit — the " \
               "integration developer will apply and commit all changes.\n"
        return ctx

    dev_results = run_parallel(
        developers, dev_context, args.provider, args.model,
        timeout, cwd=root, approve=False,
    )

    # Assemble developer outputs and feed to integration developer.
    proposals = assemble_developer_outputs(dev_results)
    integration_prompt = impl_cfg.get(
        "integration_prompt", ".factory/prompts/integration-developer.md")

    from .parallel import invoke_subagent
    name, exit_code, stdout, stderr = invoke_subagent(
        integration_prompt,
        f"## Task\n\n{excerpt}\n\n"
        f"## Developer Proposals\n\n{proposals}",
        args.provider, args.model,
        timeout, cwd=root, approve=True,
    )

    if exit_code != 0:
        print(f"  implementation: integration developer failed (exit {exit_code})",
              file=sys.stderr)
        return ""

    return gitutil.commit_all(
        root, f"factory: task {task.id} integrated implementation",
    )


def run_audit_phase(roles: dict, config: dict, args,
                     task: Task, root: Path) -> tuple[str, bool]:
    """Run the audit phase: parallel specialist auditors.

    Returns ``(report, has_findings)``.
    """
    audit_cfg = roles.get("audit", {})
    timeout = audit_cfg.get("timeout", 600)
    auditors = list(audit_cfg.get("auditors", []))

    if not auditors:
        # Fallback: single auditor.
        auditors = [{"name": "auditor", "prompt": ".factory/prompts/auditor.md"}]

    print(f"  audit: {len(auditors)} specialist auditors (parallel)",
          file=sys.stderr)

    excerpt = task_excerpt(task)

    def audit_context(name: str, sa: dict) -> str:
        ctx = f"You are auditing task {task.id}: {task.title}\n\n"
        ctx += f"## Task Details\n\n{excerpt}\n\n"
        if sa.get("description"):
            ctx += f"## Your Focus\n\n{sa['description']}\n"
        ctx += "\nAudit the current codebase state. Report findings as " \
               "markdown. Exit non-zero if you find BLOCKER issues.\n"
        return ctx

    audit_results = run_parallel(
        auditors, audit_context, args.provider, args.model,
        timeout, cwd=root, approve=False,
    )

    report, has_blockers = assemble_audit_findings(audit_results)
    return report, has_blockers


# ─── Terminal success ────────────────────────────────────────────────

def _finalize_success(plan: Plan, config: dict, env: dict, args,
                      roles: dict, state: State) -> str:
    """work_exhausted: run overall verification + audit, then decide."""
    commit = gitutil.current_commit(ROOT)
    vcmd = config_verify_command(config)
    _clean_verification_dirs(ROOT, config)
    local = Runner(
        name="local", transport="local", ssh_config_alias="",
        working_directory="", capabilities=[], verify_command="",
    )
    vresult = run_verification(local, vcmd, ROOT, commit)
    if vresult.exit_code != 0:
        return "failed"

    # Run full audit with all specialist auditors.
    dummy_task = Task(id=0, title="Final audit", status="completed",
                      acceptance="All tasks complete", verification=vcmd)
    report, has_blockers = run_audit_phase(roles, config, args, dummy_task, ROOT)
    FINDINGS_PATH.write_text(
        f"# Final Audit Findings\n\n{report}\n", encoding="utf-8",
    )
    return "findings" if has_blockers else "success"


# ─── Main campaign loop ──────────────────────────────────────────────

def run_campaign(args, config: dict, env: dict) -> int:
    roles = load_roles(ROOT)

    # 1. Preflight.
    preflight = run_preflight(ROOT, config, env["runners"])
    if not preflight.passed:
        for failure in preflight.failures:
            print(f"preflight failure: {failure}", file=sys.stderr)
        return 2

    with Lock(ROOT):
        state = load(STATE_PATH)
        if state.campaign_id and state.campaign_id != args.campaign_id:
            state = State(campaign_id=args.campaign_id)
        elif not state.campaign_id:
            state.campaign_id = args.campaign_id
        validate(state)

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

                print(f"\n=== Round {round_num} ===", file=sys.stderr)

                # ── 1. PLANNING (parallel study → planner) ──
                ok = run_planning_phase(roles, config, args, ROOT)
                if not ok:
                    outcome = "failed"
                    reason = "planning failed"
                    break
                try:
                    plan = parse(PLAN_PATH)
                except ValueError as exc:
                    outcome = "failed"
                    reason = f"plan parse failed: {exc}"
                    break

                # ── 2. SELECTION (deterministic, model never chooses) ──
                caps = get_available_capabilities(env["runners"])
                sel = select(plan.tasks, caps)

                if sel.status == "work_exhausted":
                    outcome = _finalize_success(
                        plan, config, env, args, roles, state)
                    reason = "all tasks complete"
                    break

                if sel.status == "blocked":
                    outcome = "blocked"
                    reason = sel.reason
                    break

                task = plan.get_task(sel.task_id)
                state.selected_task_id = task.id
                state.current_phase = "implementation"
                save(STATE_PATH, state)

                # ── 3. IMPLEMENTATION + VERIFICATION ──
                verified = False
                vresult: VerificationResult | None = None
                for attempt in range(1, args.attempts + 1):
                    state.attempt_number = attempt
                    save(STATE_PATH, state)

                    commit = run_implementation_phase(
                        roles, config, args, task, ROOT)

                    if not commit:
                        # Developer/integration failed; retry if attempts left.
                        continue

                    state.current_phase = "verification"
                    save(STATE_PATH, state)
                    _clean_verification_dirs(ROOT, config)
                    vresult = run_task_verification(
                        task, env["runners"], ROOT, commit,
                        config_build_command(config))

                    if vresult.exit_code == 0:
                        verified = True
                        break

                if not verified:
                    outcome = "failed"
                    reason = (f"task {task.id} failed verification after "
                              f"{args.attempts} attempts")
                    break

                # Mark task completed in the plan.
                task.status = "completed"
                task.evidence = (f"verification exit {vresult.exit_code} "
                                f"on {vresult.runner}")
                PLAN_PATH.write_text(dump(plan), encoding="utf-8")

                # ── 4. AUDIT (parallel specialist auditors) ──
                state.current_phase = "audit"
                save(STATE_PATH, state)
                report, has_findings = run_audit_phase(
                    roles, config, args, task, ROOT)
                if has_findings:
                    FINDINGS_PATH.write_text(
                        f"# Audit findings (round {round_num}, task "
                        f"{task.id})\n\n{report}\n",
                        encoding="utf-8",
                    )

                # ── 5. Checkpoint ──
                gitutil.commit_all(
                    ROOT, f"factory: checkpoint round {round_num}")

                state.rounds_completed = round_num
                state.current_round = round_num + 1
                state.last_outcome = ("audit_findings" if has_findings
                                      else "audit_pass")
                save(STATE_PATH, state)

        except KeyboardInterrupt:
            outcome, reason = "interrupted", "process interrupted"

        if outcome is None:
            if state.last_outcome == "audit_findings":
                outcome = "findings"
                reason = "rounds exhausted with audit findings"
            else:
                outcome = "failed"
                reason = "rounds exhausted before completion"

        state.terminal_outcome = outcome
        state.current_phase = "terminal"
        save(STATE_PATH, state)

    print(f"campaign outcome: {outcome}"
          + (f" ({reason})" if reason else ""))
    return TERMINAL_EXIT.get(outcome, 1)


# ─── CLI ──────────────────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="factory-campaign",
        description="Parallel Ralph factory campaign orchestrator",
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