#!/usr/bin/env python3
"""Parallel Ralph factory campaign orchestrator.

Ties together the control-plane modules (plan_parser, selector, state,
gitutil, lock, runner, preflight, parallel, metrics) into a finite
campaign that plans once, then loops implementation.

Campaign structure:
  0. PLANNING (once): parallel study subagents → planner synthesises
     the plan.  The plan is never revised during the campaign.
  1. LOOP (per round):
     a. SELECTION: deterministic task selection (model never chooses).
     b. IMPLEMENTATION: parallel developers → integration developer
        commits.  Each role may use a different model (model tiering).
     c. VERIFICATION: task verification on local or runner hardware.
     d. AUDIT: parallel specialist auditors → findings.
     e. REPAIR (if BLOCKERs): findings + verification output fed back
        to developer → re-verify → re-audit.  Capped at max_repairs.
     f. CHECKPOINT: commit, record metrics, advance to next round.
  2. FINALIZE: when all tasks complete → overall verification + audit
     → success or findings.

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
from .state import State  # only for type compat in other modules
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
    _resolve_model,
)
from .metrics import (
    MetricsLog,
    build_round_metrics,
)
from .issues import (
    IssueTracker,
    write_round_scratchpad,
    read_round_scratchpads,
)

ROOT = Path(__file__).resolve().parents[2]
CONFIG_PATH = ROOT / ".factory" / "config.toml"
ENV_PATH = ROOT / ".factory" / "environment.toml"
METRICS_PATH = ROOT / ".factory-state" / "metrics.jsonl"
ISSUES_PATH = ROOT / ".factory-state" / "issues.json"
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
    "stale": 1,
    "escalated": 1,
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


def config_escalation_threshold(config: dict) -> int:
    return int(config.get("campaign", {}).get("escalation_threshold", 3))


def _clean_untracked(root: Path) -> None:
    """Remove untracked files left by study/audit subagents.

    Study and audit subagents run with ``approve=False`` but pi2 may
    still create files (e.g. bash output redirection).  These untracked
    files break ``git status --porcelain`` verification and must be
    cleaned before verification and final audit.
    """
    subprocess.run(
        ["git", "clean", "-fd", "-e", ".factory-state/", "-e", ".factory/ssh/"],
        cwd=str(root), capture_output=True, timeout=30,
    )


# ─── Roles override ───────────────────────────────────────────────────

def apply_roles_override(roles: dict, override: dict) -> dict:
    """Apply a roles_override from the plan to the roles config.

    Supported override keys:
      ``skip_auditors``: list of auditor names to skip.
      ``skip_studies``: list of study subagent names to skip.
      ``add_auditors``: list of auditor dicts to add.
      ``add_studies``: list of study dicts to add.
      ``add_developers``: list of developer dicts to add/replace.
      ``auditor_models``: dict of auditor_name → model override.
      ``study_models``: dict of study_name → model override.
      ``developer_models``: dict of developer_name → model override.
      ``planner_model``: model override for the planner.
    """
    import copy
    roles = copy.deepcopy(roles)

    if not override:
        return roles

    # Skip auditors
    skip_a = set(override.get("skip_auditors", []))
    if skip_a:
        roles.setdefault("audit", {}).setdefault("auditors", [])
        roles["audit"]["auditors"] = [
            a for a in roles["audit"]["auditors"]
            if a.get("name") not in skip_a
        ]

    # Skip studies
    skip_s = set(override.get("skip_studies", []))
    if skip_s:
        roles.setdefault("planning", {}).setdefault("studies", [])
        roles["planning"]["studies"] = [
            s for s in roles["planning"]["studies"]
            if s.get("name") not in skip_s
        ]

    # Add auditors
    add_a = override.get("add_auditors", [])
    if add_a:
        roles.setdefault("audit", {}).setdefault("auditors", [])
        roles["audit"]["auditors"].extend(add_a)

    # Add studies
    add_s = override.get("add_studies", [])
    if add_s:
        roles.setdefault("planning", {}).setdefault("studies", [])
        roles["planning"]["studies"].extend(add_s)

    # Add/replace developers
    add_d = override.get("add_developers", [])
    if add_d:
        roles.setdefault("implementation", {}).setdefault("developers", [])
        existing_names = {d.get("name") for d in roles["implementation"]["developers"]}
        for d in add_d:
            if d.get("name") in existing_names:
                # Replace existing
                roles["implementation"]["developers"] = [
                    d if existing_d.get("name") == d.get("name") else existing_d
                    for existing_d in roles["implementation"]["developers"]
                ]
            else:
                roles["implementation"]["developers"].append(d)

    # Model overrides per auditor
    a_models = override.get("auditor_models", {})
    if a_models:
        for a in roles.get("audit", {}).get("auditors", []):
            if a.get("name") in a_models:
                a["model"] = a_models[a["name"]]

    # Model overrides per study
    s_models = override.get("study_models", {})
    if s_models:
        for s in roles.get("planning", {}).get("studies", []):
            if s.get("name") in s_models:
                s["model"] = s_models[s["name"]]

    # Model overrides per developer
    d_models = override.get("developer_models", {})
    if d_models:
        for d in roles.get("implementation", {}).get("developers", []):
            if d.get("name") in d_models:
                d["model"] = d_models[d["name"]]

    # Planner model
    p_model = override.get("planner_model")
    if p_model:
        roles.setdefault("planning", {})["planner_model"] = p_model

    return roles


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

def run_planning_phase(roles: dict, config: dict, args, root: Path,
                       metrics_log: MetricsLog | None = None,
                       issue_tracker: IssueTracker | None = None) -> bool:
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

    # Build planner context with study reports + historical metrics +
    # round scratchpads + issue tracker.
    planner_context = f"## Study Reports\n\n{study_report}\n\n"
    if metrics_log:
        metrics_summary = metrics_log.summary()
        if metrics_summary and "No historical" not in metrics_summary:
            planner_context += (
                f"## Historical Campaign Metrics\n\n"
                f"{metrics_summary}\n\n"
            )
    # Include prior round scratchpads for iteration continuity.
    scratchpads = read_round_scratchpads(root, last_n=3)
    if scratchpads:
        planner_context += (
            f"## Prior Round Summaries\n\n"
            f"These are structured summaries from previous rounds. "
            f"Use them to understand what was already tried and avoid "
            f"repeating the same mistakes.\n\n"
            f"{scratchpads}\n\n"
        )
    # Include issue tracker for cross-round finding deduplication.
    if issue_tracker:
        issues_summary = issue_tracker.summary()
        if issues_summary:
            planner_context += (
                f"## Issue Tracker (Cross-Round)\n\n"
                f"These are issues flagged by auditors in previous rounds. "
                f"Recurring issues indicate the repair mechanism is not "
                f"working. Escalated issues need a different approach.\n\n"
                f"{issues_summary}\n\n"
            )
    planner_context += (
        "Review discrepancies against the spec and devise an "
        "implementation plan at .factory/artifacts/implementation-plan.md\n\n"
        "If the metrics show low-precision auditors or high-rejection "
        "developers, you MAY add a `roles_override` field to the plan's "
        "YAML front matter (as a JSON string) to adjust roles for the "
        "next round. Supported keys: skip_auditors, skip_studies, "
        "add_auditors, add_studies, add_developers, auditor_models, "
        "study_models, developer_models, planner_model.\n"
        "Example: roles_override: {\"skip_auditors\": [\"security\"], "
        "\"auditor_models\": {\"efficiency\": \"qwen3:8b\"}}\n"
        "Only add `roles_override` when the metrics clearly warrant it. "
        "Do not add it on the first round or when metrics are clean.\n"
    )

    planner_prompt = plan_cfg.get("planner_prompt", ".factory/prompts/planner.md")
    planner_model = plan_cfg.get("planner_model") or args.model
    planner_result = run_parallel(
        [{"name": "planner", "prompt": planner_prompt, "model": planner_model}],
        lambda name, sa: planner_context,
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
) -> tuple[str, list[SubagentResult]]:
    """Run the implementation phase for a single task.

    Parallel developers propose changes → integration developer applies
    and commits.  Returns ``(commit_sha, dev_results)`` (or
    ``("", dev_results)`` on failure).

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

    all_dev_results: list[SubagentResult] = []

    # Single developer: run directly with --approve.
    if len(developers) == 1:
        label = "repair" if repair_context else "implementation"
        print(f"  {label}: 1 developer (serial)", file=sys.stderr)
        dev = developers[0]
        dev_model = _resolve_model(dev, args.model)
        name, exit_code, stdout, stderr = invoke_subagent(
            dev["prompt"], excerpt + extra_context,
            args.provider, dev_model,
            timeout, cwd=root, approve=True,
        )
        all_dev_results.append(SubagentResult(
            name=name, success=exit_code == 0,
            stdout=stdout, stderr=stderr, exit_code=exit_code,
        ))
        if exit_code != 0:
            print(f"  {label}: developer failed (exit {exit_code})",
                  file=sys.stderr)
            return "", all_dev_results
        msg = (f"factory: task {task.id} "
               f"{'repair' if repair_context else 'implementation'}")
        return gitutil.commit_all(root, msg), all_dev_results

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
    all_dev_results.extend(dev_results)

    proposals = assemble_developer_outputs(dev_results)
    integration_prompt = impl_cfg.get(
        "integration_prompt", ".factory/prompts/integration-developer.md")
    int_model = impl_cfg.get("integration_model") or args.model

    name, exit_code, stdout, stderr = invoke_subagent(
        integration_prompt,
        f"## Task\n\n{excerpt}\n\n"
        f"## Developer Proposals\n\n{proposals}"
        + extra_context,
        args.provider, int_model,
        timeout, cwd=root, approve=True,
    )

    if exit_code != 0:
        print(f"  {label}: integration developer failed (exit {exit_code})",
              file=sys.stderr)
        return "", all_dev_results

    msg = (f"factory: task {task.id} "
           f"{'repair' if repair_context else 'integrated implementation'}")
    return gitutil.commit_all(root, msg), all_dev_results


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
                      roles: dict) -> str:
    """work_exhausted: run overall verification + audit, then decide."""
    commit = gitutil.current_commit(ROOT)
    vcmd = config_verify_command(config)
    _clean_untracked(ROOT)
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


# ─── Plan command ────────────────────────────────────────────────────

def cmd_plan(args, config: dict, env: dict) -> int:
    """Run the planning phase: study subagents → planner → plan file.

    This is a standalone command.  Run it manually when you want to
    create or refresh the plan.  The campaign ``run`` command does NOT
    call this — it expects the plan to already exist.
    """
    base_roles = load_roles(ROOT)
    metrics_log = MetricsLog(METRICS_PATH)
    issue_tracker = IssueTracker(
        ISSUES_PATH, escalation_threshold=args.escalation_threshold)

    # Preflight.
    preflight = run_preflight(ROOT, config, env["runners"])
    if not preflight.passed:
        for failure in preflight.failures:
            print(f"preflight failure: {failure}", file=sys.stderr)
        return 2

    with Lock(ROOT):
        branch = args.branch or config.get("project", {}).get(
            "development_branch", "develop")
        gitutil.switch_branch(ROOT, branch)

        print("\n=== Planning ===", file=sys.stderr)
        ok = run_planning_phase(
            base_roles, config, args, ROOT, metrics_log,
            issue_tracker)
        if not ok:
            print("planning: failed", file=sys.stderr)
            return 1

        # Verify the plan parses.
        try:
            plan = parse(PLAN_PATH)
        except ValueError as exc:
            print(f"planning: plan parse failed: {exc}", file=sys.stderr)
            return 1

        pending = [t for t in plan.tasks if t.status == "pending"]
        completed = [t for t in plan.tasks if t.status == "completed"]
        print(f"planning: {len(plan.tasks)} tasks "
              f"({len(completed)} completed, {len(pending)} pending)",
              file=sys.stderr)
        return 0


# ─── Run command (stateless implementation loop) ─────────────────────

def cmd_run(args, config: dict, env: dict) -> int:
    """Run the implementation loop.

    Reads the plan, picks the next pending task, implements/verifies/
    audits/checkpoints.  Repeats for ``--rounds`` iterations or until
    work is exhausted.  No state file — the plan IS the state.  If
    interrupted, just run again: it picks up from the plan.
    """
    base_roles = load_roles(ROOT)
    metrics_log = MetricsLog(METRICS_PATH)
    issue_tracker = IssueTracker(
        ISSUES_PATH, escalation_threshold=args.escalation_threshold)
    max_repairs = args.max_repairs

    # Preflight.
    preflight = run_preflight(ROOT, config, env["runners"])
    if not preflight.passed:
        for failure in preflight.failures:
            print(f"preflight failure: {failure}", file=sys.stderr)
        return 2

    # The plan must exist.
    try:
        plan = parse(PLAN_PATH)
    except (ValueError, OSError) as exc:
        print(f"run: cannot read plan: {exc}", file=sys.stderr)
        print("run: create a plan first with 'factory-campaign plan'",
              file=sys.stderr)
        return 2

    with Lock(ROOT):
        branch = args.branch or config.get("project", {}).get(
            "development_branch", "develop")
        gitutil.switch_branch(ROOT, branch)

        roles = apply_roles_override(base_roles, plan.roles_override)

        start = time.time()
        outcome: str | None = None
        reason: str | None = None

        try:
            for round_num in range(1, args.rounds + 1):
                if time.time() - start > args.timeout:
                    outcome, reason = "interrupted", "campaign timeout"
                    break

                print(f"\n=== Round {round_num} ===", file=sys.stderr)

                # Re-read the plan each round — it may have been
                # updated by the previous round's checkpoint.
                try:
                    plan = parse(PLAN_PATH)
                except ValueError as exc:
                    outcome = "failed"
                    reason = f"plan parse failed: {exc}"
                    break

                # ── 1. SELECTION ──
                caps = get_available_capabilities(env["runners"])
                sel = select(plan.tasks, caps)

                if sel.status == "work_exhausted":
                    outcome = _finalize_success(
                        plan, config, env, args, roles)
                    reason = "all tasks complete"
                    break

                if sel.status == "blocked":
                    outcome = "blocked"
                    reason = sel.reason
                    break

                task = plan.get_task(sel.task_id)
                print(f"  selected: task {task.id}: {task.title}",
                      file=sys.stderr)

                round_start = time.time()
                round_dev_results = []
                round_audit_report = None
                round_v_attempts = 0
                round_v_passed = False
                round_repair_cycles = 0
                round_repair_resolved = False

                # ── 2. IMPLEMENTATION + VERIFICATION ──
                verified = False
                vresult: VerificationResult | None = None
                last_voutput = ""
                impl_time_total = 0.0
                verify_time_total = 0.0

                for attempt in range(1, args.attempts + 1):
                    impl_start = time.time()
                    commit, dev_results = run_implementation_phase(
                        roles, config, args, task, ROOT,
                        verification_output=last_voutput,
                    )
                    impl_time_total += time.time() - impl_start
                    round_dev_results.extend(dev_results)

                    if not commit:
                        continue

                    round_v_attempts = attempt
                    _clean_untracked(ROOT)
                    _clean_verification_dirs(ROOT, config)
                    verify_start = time.time()
                    vresult = run_task_verification(
                        task, env["runners"], ROOT, commit,
                        config_build_command(config))
                    verify_time_total += time.time() - verify_start

                    if vresult.exit_code == 0:
                        verified = True
                        round_v_passed = True
                        break

                    last_voutput = _format_verification_output(vresult)
                    print(f"  verification: attempt {attempt} failed "
                          f"(exit {vresult.exit_code})", file=sys.stderr)

                if not verified:
                    outcome = "failed"
                    reason = (f"task {task.id} failed verification after "
                              f"{args.attempts} attempts")
                    rm = build_round_metrics(
                        round_num, args.campaign_id, task, round_start,
                        dev_results=round_dev_results,
                        verification_attempts=round_v_attempts,
                        verification_passed=False,
                        outcome="failed",
                    )
                    metrics_log.append(rm)
                    break

                # Mark task completed in the plan.
                task.status = "completed"
                task.evidence = (f"verification exit {vresult.exit_code} "
                                f"on {vresult.runner}")
                PLAN_PATH.write_text(dump(plan), encoding="utf-8")

                # ── 3. AUDIT ──
                print(f"  audit: running specialist auditors...",
                      file=sys.stderr)
                audit_start = time.time()
                report = run_audit_phase(roles, config, args, task, ROOT)
                audit_time = time.time() - audit_start
                round_audit_report = report

                escalated = []
                if report.blockers:
                    escalated = issue_tracker.add_findings(
                        report.blockers, round_num)
                    if escalated:
                        print(f"  issues: {len(escalated)} issue(s) "
                              f"ESCALATED", file=sys.stderr)

                # ── 4. REPAIR CYCLE (if BLOCKERs) ──
                if report.has_blockers:
                    FINDINGS_PATH.write_text(
                        f"# Audit findings (round {round_num}, task "
                        f"{task.id})\n\n{report.raw_report}\n",
                        encoding="utf-8",
                    )
                    print(f"  audit: {len(report.blockers)} BLOCKER(s) "
                          f"— entering repair cycle",
                          file=sys.stderr)

                    repair_resolved = False
                    repair_time_total = 0.0
                    for repair_num in range(1, max_repairs + 1):
                        print(f"  repair {repair_num}/{max_repairs}",
                              file=sys.stderr)

                        repair_ctx = build_repair_context(
                            report,
                            verification_output=last_voutput,
                            repair_attempt=repair_num,
                        )

                        repair_impl_start = time.time()
                        commit, dev_results = run_implementation_phase(
                            roles, config, args, task, ROOT,
                            repair_context=repair_ctx,
                            verification_output=last_voutput,
                        )
                        repair_time_total += time.time() - repair_impl_start
                        round_dev_results.extend(dev_results)

                        if not commit:
                            print(f"  repair: developer failed",
                                  file=sys.stderr)
                            continue

                        _clean_untracked(ROOT)
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

                        report = run_audit_phase(
                            roles, config, args, task, ROOT)
                        round_audit_report = report

                        if not report.has_blockers:
                            repair_resolved = True
                            round_repair_resolved = True
                            print(f"  repair: all BLOCKERs resolved",
                                  file=sys.stderr)
                            break

                        print(f"  repair: {len(report.blockers)} BLOCKER(s) "
                              f"remain", file=sys.stderr)

                    round_repair_cycles = max_repairs

                    if not repair_resolved:
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

                        write_round_scratchpad(
                            ROOT, round_num, args.campaign_id, task,
                            plan_summary=f"Task {task.id}",
                            implementation_summary=f"{round_v_attempts} attempt(s)",
                            verification_summary=f"passed on {vresult.runner}",
                            audit_summary=f"{len(report.blockers)} BLOCKERs unresolved",
                            repair_summary=f"{max_repairs} cycles, unresolved",
                            outcome="blocked",
                            issues_summary=issue_tracker.summary(),
                        )

                        if issue_tracker.has_escalations:
                            outcome = "escalated"
                            reason = (f"issue(s) escalated after "
                                      f"{args.escalation_threshold} "
                                      f"recurrences")
                            break

                        rm = build_round_metrics(
                            round_num, args.campaign_id, task, round_start,
                            dev_results=round_dev_results,
                            audit_report=round_audit_report,
                            verification_attempts=round_v_attempts,
                            verification_passed=round_v_passed,
                            repair_cycles=round_repair_cycles,
                            repair_resolved=False,
                            outcome="blocked",
                            implementation_time_s=impl_time_total,
                            verification_time_s=verify_time_total,
                            audit_time_s=audit_time,
                            repair_time_s=repair_time_total,
                            escalated_issues=len(issue_tracker.escalated_issues),
                        )
                        metrics_log.append(rm)
                        continue  # next round, next task

                # ── 5. CHECKPOINT ──
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

                audit_summary = ("No BLOCKERs" if not report.has_blockers
                                 else f"{len(report.blockers)} BLOCKER(s) "
                                      f"resolved in {round_repair_cycles} "
                                      f"repair cycle(s)")
                write_round_scratchpad(
                    ROOT, round_num, args.campaign_id, task,
                    plan_summary=f"Task {task.id}: {task.title}",
                    implementation_summary=f"{round_v_attempts} attempt(s), "
                                           f"verified on {vresult.runner}",
                    verification_summary=f"exit {vresult.exit_code}, passed",
                    audit_summary=audit_summary,
                    repair_summary=(f"{round_repair_cycles} cycle(s)"
                                    if round_repair_cycles > 0 else ""),
                    outcome=("completed" if round_repair_cycles == 0
                             else "completed_with_repairs"),
                    issues_summary=issue_tracker.summary(),
                )

                round_outcome = ("completed" if round_repair_cycles == 0
                                 else "completed_with_repairs")
                rm = build_round_metrics(
                    round_num, args.campaign_id, task, round_start,
                    dev_results=round_dev_results,
                    audit_report=round_audit_report,
                    verification_attempts=round_v_attempts,
                    verification_passed=round_v_passed,
                    repair_cycles=round_repair_cycles,
                    repair_resolved=round_repair_resolved,
                    outcome=round_outcome,
                    implementation_time_s=impl_time_total,
                    verification_time_s=verify_time_total,
                    audit_time_s=audit_time,
                    repair_time_s=(repair_time_total if round_repair_cycles > 0
                                   else 0.0),
                    escalated_issues=len(issue_tracker.escalated_issues),
                )
                metrics_log.append(rm)

                print(f"  checkpoint: task {task.id} {round_outcome}",
                      file=sys.stderr)

                # After checkpoint, check if all tasks are now done.
                # If so, finalize immediately rather than waiting for
                # the next round's selection.
                try:
                    post_plan = parse(PLAN_PATH)
                    post_sel = select(post_plan.tasks, caps)
                    if post_sel.status == "work_exhausted":
                        outcome = _finalize_success(
                            post_plan, config, env, args, roles)
                        reason = "all tasks complete after checkpoint"
                        break
                except ValueError:
                    pass  # Plan might be corrupt — let next round handle

        except KeyboardInterrupt:
            outcome, reason = "interrupted", "process interrupted"

        if outcome is None:
            outcome = "findings"
            reason = "rounds exhausted before completion"

    print(f"campaign outcome: {outcome}"
          + (f" ({reason})" if reason else ""))
    return TERMINAL_EXIT.get(outcome, 1)


# ─── CLI ──────────────────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="factory-campaign",
        description="Ralph factory campaign (plan once, loop implementation)",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # Common args
    def add_common(p):
        p.add_argument("--branch", default=None)
        p.add_argument("--provider", default="ollama")
        p.add_argument("--model", default="deepseek-v4-flash")
        p.add_argument("--timeout", type=int, default=None)
        p.add_argument("--escalation-threshold", type=int, default=None)

    # plan subcommand
    plan_p = sub.add_parser("plan", help="create or refresh the plan")
    plan_p.add_argument("--campaign-id", default="plan")
    add_common(plan_p)

    # run subcommand
    run_p = sub.add_parser("run", help="run the implementation loop")
    run_p.add_argument("--campaign-id", required=True)
    run_p.add_argument("--rounds", type=int, default=None)
    run_p.add_argument("--attempts", type=int, default=None)
    run_p.add_argument("--max-repairs", type=int, default=None)
    add_common(run_p)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    config = load_config()
    env = load_environment(ENV_PATH)

    camp = config.get("campaign", {})
    if args.timeout is None:
        args.timeout = camp.get("default_timeout", 21600)
    if args.escalation_threshold is None:
        args.escalation_threshold = config_escalation_threshold(config)

    if args.command == "plan":
        return cmd_plan(args, config, env)

    if args.command == "run":
        if args.rounds is None:
            args.rounds = camp.get("default_rounds", 20)
        if args.attempts is None:
            args.attempts = camp.get("default_attempts", 3)
        if args.max_repairs is None:
            args.max_repairs = config_max_repairs(config)
        return cmd_run(args, config, env)

    return 1


if __name__ == "__main__":
    sys.exit(main())