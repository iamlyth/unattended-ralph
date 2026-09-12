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
  6. REPAIR (if BLOCKERs): findings + verification output fed back to
     developer → re-verify → re-audit.  Capped at max_repairs cycles.
  7. CHECKPOINT: commit and advance to next round.

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
    AuditReport,
    run_parallel,
    assemble_reports,
    assemble_developer_outputs,
    assemble_audit_findings,
    build_repair_context,
    discover_subsystems,
    load_roles,
    invoke_subagent,
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


def config_max_repairs(config: dict) -> int:
    return int(config.get("campaign", {}).get("max_repairs", 3))


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


def _format_verification_output(vresult: VerificationResult) -> str:
    """Format verification result as context for the developer."""
    parts = [
        f"Verification command: {vresult.command}",
        f"Exit code: {vresult.exit_code}",
        f"Runner: {vresult.runner}",
    ]
    if vresult.stdout:
        parts.append(f"stdout:\n{vresult.stdout[:4000]}")
    if vresult.stderr:
        parts.append(f"stderr:\n{vresult.stderr[:4000]}")
    return "\n\n".join(parts)


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

    study_report = assemble_reports(study_results)

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


def run_implementation_phase(
    roles: dict, config: dict, args,
    task: Task, root: Path,
    repair_context: str = "",
    verification_output: str = "",
) -> str:
    """Run the implementation phase for a single task.

    Parallel developers propose changes → integration developer applies
    and commits.  Returns the commit SHA (or empty string on failure).

    If ``repair_context`` is provided, it is appended to the developer's
    context — this is used during repair cycles to feed audit findings
    back to the developer.  If ``verification_output`` is provided, it is
    also included so the developer can diagnose test failures.
    """
    impl_cfg = roles.get("implementation", {})
    timeout = impl_cfg.get("timeout", ROLE_TIMEOUT)
    developers = list(impl_cfg.get("developers", []))

    if not developers:
        developers = [{"name": "default", "prompt": ".factory/prompts/developer.md"}]

    excerpt = task_excerpt(task)

    # Build the full context for the developer.
    extra_context = ""
    if repair_context:
        extra_context += "\n\n" + repair_context
    if verification_output:
        extra_context += (
            "\n\n## Previous Verification Output\n\n"
            "The verification command was run after the previous "
            "implementation attempt and produced this output. Use it to "
            "diagnose and fix the failure.\n\n"
            f"```\n{verification_output[:6000]}\n```"
        )

    # Single developer: run directly with --approve.
    if len(developers) == 1:
        label = "repair" if repair_context else "implementation"
        print(f"  {label}: 1 developer (serial)", file=sys.stderr)
        dev = developers[0]
        name, exit_code, stdout, stderr = invoke_subagent(
            dev["prompt"], excerpt + extra_context,
            args.provider, args.model,
            timeout, cwd=root, approve=True,
        )
        if exit_code != 0:
            print(f"  {label}: developer failed (exit {exit_code})",
                  file=sys.stderr)
            return ""
        msg = (f"factory: task {task.id} "
               f"{'repair' if repair_context else 'implementation'}")
        return gitutil.commit_all(root, msg)

    # Multiple developers: run in parallel, then integration developer.
    label = "repair" if repair_context else "implementation"
    print(f"  {label}: {len(developers)} developers (parallel)",
          file=sys.stderr)

    def dev_context(name: str, sa: dict) -> str:
        ctx = excerpt + extra_context
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

    proposals = assemble_developer_outputs(dev_results)
    integration_prompt = impl_cfg.get(
        "integration_prompt", ".factory/prompts/integration-developer.md")

    name, exit_code, stdout, stderr = invoke_subagent(
        integration_prompt,
        f"## Task\n\n{excerpt}\n\n"
        f"## Developer Proposals\n\n{proposals}"
        + extra_context,
        args.provider, args.model,
        timeout, cwd=root, approve=True,
    )

    if exit_code != 0:
        print(f"  {label}: integration developer failed (exit {exit_code})",
              file=sys.stderr)
        return ""

    msg = (f"factory: task {task.id} "
           f"{'repair' if repair_context else 'integrated implementation'}")
    return gitutil.commit_all(root, msg)


def run_audit_phase(roles: dict, config: dict, args,
                     task: Task, root: Path) -> AuditReport:
    """Run the audit phase: parallel specialist auditors.

    Returns an ``AuditReport`` with structured findings, conflict
    resolution, and repair instructions.
    """
    audit_cfg = roles.get("audit", {})
    timeout = audit_cfg.get("timeout", 600)
    auditors = list(audit_cfg.get("auditors", []))

    if not auditors:
        auditors = [{"name": "auditor", "prompt": ".factory/prompts/auditor.md"}]

    print(f"  audit: {len(auditors)} specialist auditors (parallel)",
          file=sys.stderr)

    excerpt = task_excerpt(task)

    def audit_context(name: str, sa: dict) -> str:
        ctx = f"You are auditing task {task.id}: {task.title}\n\n"
        ctx += f"## Task Details\n\n{excerpt}\n\n"
        if sa.get("description"):
            ctx += f"## Your Focus\n\n{sa['description']}\n"
        ctx += (
            "\nAudit the current codebase state. Report findings as "
            "markdown. Use **BLOCKER** for issues that must be fixed "
            "before this task can be considered complete. Use **WARN** "
            "for improvements that should be made but are not blocking. "
            "Use **INFO** for observations.\n"
            "\nIf you find no issues, say \"No findings.\" and exit 0.\n"
            "\nFor each finding, reference the specific file path(s) "
            "involved so the developer knows where to fix.\n"
        )
        return ctx

    audit_results = run_parallel(
        auditors, audit_context, args.provider, args.model,
        timeout, cwd=root, approve=False,
    )

    return assemble_audit_findings(audit_results)


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

    dummy_task = Task(id=0, title="Final audit", status="completed",
                      acceptance="All tasks complete", verification=vcmd)
    report = run_audit_phase(roles, config, args, dummy_task, ROOT)
    FINDINGS_PATH.write_text(
        f"# Final Audit Findings\n\n{report.raw_report}\n",
        encoding="utf-8",
    )
    return "findings" if report.has_blockers else "success"


# ─── Main campaign loop ──────────────────────────────────────────────

def run_campaign(args, config: dict, env: dict) -> int:
    roles = load_roles(ROOT)
    max_repairs = args.max_repairs

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
                state.repair_count = 0
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
                # Feed verification output back to developer on retry.
                verified = False
                vresult: VerificationResult | None = None
                last_voutput = ""

                for attempt in range(1, args.attempts + 1):
                    state.attempt_number = attempt
                    save(STATE_PATH, state)

                    commit = run_implementation_phase(
                        roles, config, args, task, ROOT,
                        verification_output=last_voutput,
                    )

                    if not commit:
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

                    # Capture verification output for next attempt's developer.
                    last_voutput = _format_verification_output(vresult)
                    print(f"  verification: attempt {attempt} failed "
                          f"(exit {vresult.exit_code})", file=sys.stderr)

                if not verified:
                    outcome = "failed"
                    reason = (f"task {task.id} failed verification after "
                              f"{args.attempts} attempts")
                    break

                # Mark task completed in the plan (tentatively — audit may
                # un-mark it if repair fails).
                task.status = "completed"
                task.evidence = (f"verification exit {vresult.exit_code} "
                                f"on {vresult.runner}")
                PLAN_PATH.write_text(dump(plan), encoding="utf-8")

                # ── 4. AUDIT (parallel specialist auditors) ──
                state.current_phase = "audit"
                save(STATE_PATH, state)
                report = run_audit_phase(roles, config, args, task, ROOT)

                # ── 5. REPAIR CYCLE (if BLOCKERs found) ──
                if report.has_blockers:
                    FINDINGS_PATH.write_text(
                        f"# Audit findings (round {round_num}, task "
                        f"{task.id})\n\n{report.raw_report}\n",
                        encoding="utf-8",
                    )
                    print(f"  audit: {len(report.blockers)} BLOCKER(s) "
                          f"found — entering repair cycle",
                          file=sys.stderr)

                    repair_resolved = False
                    for repair_num in range(1, max_repairs + 1):
                        state.current_phase = "repair"
                        state.repair_count = repair_num
                        save(STATE_PATH, state)

                        repair_ctx = build_repair_context(
                            report,
                            verification_output=last_voutput,
                            repair_attempt=repair_num,
                        )

                        print(f"  repair cycle {repair_num}/{max_repairs}",
                              file=sys.stderr)

                        commit = run_implementation_phase(
                            roles, config, args, task, ROOT,
                            repair_context=repair_ctx,
                            verification_output=last_voutput,
                        )

                        if not commit:
                            print(f"  repair: developer failed",
                                  file=sys.stderr)
                            continue

                        # Re-verify after repair.
                        state.current_phase = "verification"
                        save(STATE_PATH, state)
                        _clean_verification_dirs(ROOT, config)
                        vresult = run_task_verification(
                            task, env["runners"], ROOT, commit,
                            config_build_command(config))

                        if vresult.exit_code != 0:
                            last_voutput = _format_verification_output(vresult)
                            print(f"  repair: verification failed "
                                  f"(exit {vresult.exit_code})",
                                  file=sys.stderr)
                            continue

                        # Re-audit after repair.
                        state.current_phase = "audit"
                        save(STATE_PATH, state)
                        report = run_audit_phase(
                            roles, config, args, task, ROOT)

                        if not report.has_blockers:
                            repair_resolved = True
                            print(f"  repair: all BLOCKERs resolved",
                                  file=sys.stderr)
                            break

                        print(f"  repair: {len(report.blockers)} BLOCKER(s) "
                              f"remain", file=sys.stderr)
                        FINDINGS_PATH.write_text(
                            f"# Audit findings (round {round_num}, task "
                            f"{task.id}, repair {repair_num})\n\n"
                            f"{report.raw_report}\n",
                            encoding="utf-8",
                        )

                    if not repair_resolved:
                        # Mark task as blocked — audit found unresolvable
                        # issues after max_repairs cycles.
                        task.status = "blocked"
                        task.evidence = (
                            f"verification passed but audit BLOCKERs "
                            f"unresolved after {max_repairs} repair cycles"
                        )
                        PLAN_PATH.write_text(dump(plan), encoding="utf-8")
                        gitutil.commit_all(
                            ROOT,
                            f"factory: task {task.id} blocked by audit "
                            f"after {max_repairs} repairs")
                        state.rounds_completed = round_num
                        state.current_round = round_num + 1
                        state.last_outcome = "audit_findings_unresolved"
                        save(STATE_PATH, state)
                        continue  # next round — planner may split the task

                # ── 6. CHECKPOINT (clean audit or repair resolved) ──
                if report.conflicts:
                    FINDINGS_PATH.write_text(
                        f"# Audit findings (round {round_num}, task "
                        f"{task.id})\n\n{report.raw_report}\n\n"
                        f"## Conflict Resolutions\n\n"
                        + "\n".join(f"- {c.note}" for c in report.conflicts),
                        encoding="utf-8",
                    )

                gitutil.commit_all(
                    ROOT, f"factory: checkpoint round {round_num}")

                state.rounds_completed = round_num
                state.current_round = round_num + 1
                state.last_outcome = (
                    "audit_findings_resolved" if state.repair_count > 0
                    else "audit_pass"
                )
                save(STATE_PATH, state)

        except KeyboardInterrupt:
            outcome, reason = "interrupted", "process interrupted"

        if outcome is None:
            if state.last_outcome == "audit_findings_unresolved":
                outcome = "findings"
                reason = "audit BLOCKERs unresolved after max repairs"
            elif state.last_outcome == "audit_findings_resolved":
                outcome = "findings"
                reason = "rounds exhausted after repair cycles"
            elif state.last_outcome == "audit_findings":
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
    run_p.add_argument("--max-repairs", type=int, default=None)
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
    if args.max_repairs is None:
        args.max_repairs = config_max_repairs(config)
    if args.timeout is None:
        args.timeout = camp.get("default_timeout", 21600)
    return run_campaign(args, config, env)


if __name__ == "__main__":
    sys.exit(main())