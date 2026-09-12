# Minimal Ralph Factory Specification

Status: active canonical specification for the factory boilerplate

## 1. Purpose

A minimal, autonomous software factory based on the Ralph Wiggum technique.
The factory builds software from a canonical specification using a fresh-context
loop with deterministic task selection, independent verification, and awareness
of external test runners. It is human-out-of-the-loop: once a campaign starts,
it runs to a terminal outcome without human intervention.

The factory preserves the core properties of the Ralph loop:

- one canonical specification (source of truth, never edited during implementation);
- one canonical implementation plan (sole task ledger on disk);
- one deterministically selected task per implementation attempt;
- a fresh model context for every role invocation (no memory carry-over);
- independent testing and audit that cannot be bypassed by the model;
- finite campaigns with honest terminal outcomes;
- external runner awareness for testing with resources not on the build machine.

## 2. Non-goals

The factory is **not**:

- a hardened security confinement system (no Landlock, ptrace, seccomp, or
  kernel-level jailing — the model CLI's own sandboxing is trusted);
- a cryptographic evidence system (test exit codes and command receipts
  suffice — no signed receipts, evidence tiers, or blob-exact install proofs);
- a multi-writer distributed scheduler;
- a replacement for the product specification;
- an autonomous release or `main`-branch promotion system (humans promote);
- a mechanism for treating unavailable hardware or human review as passing.

## 3. Sources of truth

### 3.1 Authoritative model inputs

Each role invocation receives **only** these inputs in a fresh context:

- the role prompt (`.factory/prompts/{role}.md`);
- `AGENTS.md` (project operational guide: build/test commands, code patterns);
- the canonical specification (`config.toml` `[project].spec`);
- the canonical implementation plan (`.factory/artifacts/implementation-plan.md`);
- the current code and tests at the bound Git commit.

No role receives prior conversation, memory, scratchpad, context summary, or
completion claims from another role. Each role is a separate fresh CLI invocation.

### 3.2 Forbidden context authorities

The following are never authoritative inputs to any role:

- `.factory-state/` (control state — orchestrator-only);
- `.factory/loop/` (control plane source — orchestrator-only);
- prior role conversations or outputs;
- session memory, handoffs, or context summaries;
- external documentation not committed in the repository.

### 3.3 Canonical task authority

The implementation plan is the sole task ledger. Bugs, test findings, audit
findings, and newly discovered work MUST become plan tasks through a planning
revision. They MUST NOT create an independent task queue.

## 4. Roles

The factory uses a configurable parallel role structure defined in
`.factory/roles.toml`. Each role is a separate fresh CLI invocation with a
static prompt. No role resumes a prior session or inherits another role's
memory.

### 4.1 Study subagents (planning phase)

Study subagents run **in parallel** during the planning phase to analyse
the codebase. They are read-only (no `--approve`). The harness auto-discovers
subsystems from the source tree and creates one study subagent per major
directory. Default study subagents:

- **Spec study**: reads the project spec and summarises requirements.
- **Architecture study**: maps the codebase structure and module layout.
- **Bug study**: runs the test suite and identifies failures and TODOs.
- **Subsystem study** (one per source directory): deep-dives into one
  subsystem's files, interfaces, and test coverage.

All study reports are collected and fed to the planner.

### 4.2 Planner

- Inputs: planner prompt, `AGENTS.md`, specification, current plan, current
  code, **study reports** from parallel study subagents.
- Responsibilities: review study reports for discrepancies against the spec;
  create or revise the canonical plan; translate findings into bounded tasks
  with dependencies and acceptance criteria; never modify product code or
  the specification.
- The planner is the only role that creates, removes, splits, or reorders
  tasks. The planner does not execute tool calls itself — it relies on the
  study subagents' analysis.

### 4.3 Developers (implementation phase)

Developers implement the selected task. The structure is configurable:

- **Single developer** (default): one developer implements the entire task
  with `--approve` and the orchestrator commits.
- **Multiple developers**: one developer per isolated codebase area (e.g.
  frontend, backend, renderer, networking) runs in parallel. Each developer
  proposes changes for their area only. An **integration developer** then
  reviews all proposals, resolves conflicts, applies changes, builds, and
  commits.

The developer(s) are the only roles that write product code. The
integration developer is the only one who commits when multiple developers
are configured.

### 4.4 Tester

- Inputs: tester prompt, `AGENTS.md`, specification, plan, committed code.
- Responsibilities: run deterministic verification independently of the
  developer; produce exact command receipts (stdout/stderr/exit code); never
  edit product code; never elevate unavailable evidence.
- The tester MUST run the verification command and report the actual result.
  It MUST NOT skip tests, weaken assertions, or report success without running
  the real verification.
- In the current implementation, the orchestrator runs verification directly
  (locally or on a runner) rather than invoking a tester role subprocess.

### 4.5 Auditors (audit phase)

Specialist auditors run **in parallel** during the audit phase. Each focuses
on a different quality axis. Default auditors:

- **Linting**: language conventions, readability, concise comments.
- **Efficiency**: performance, resource leaks, algorithmic complexity.
- **Security**: vulnerabilities, attack surface, input validation.
- **Functional**: does the code actually work (build + test verification).
- **Spec compliance**: does the implementation match the specification.
- **Compatibility**: platform, dependency, and API compatibility.

All auditors are read-only. Each auditor reports findings with severity:

- **BLOCKER**: must be fixed before the task can be considered complete.
- **WARN**: should be improved but is not blocking.
- **INFO**: observation for future reference.

#### 4.5.1 Cross-auditor conflict resolution

When two auditors both flag BLOCKER findings that reference the same file,
the harness resolves the conflict by **priority**:

```
security > functional > spec-compliance > compatibility > efficiency > linting
```

The higher-priority auditor's BLOCKER stands. The lower-priority auditor's
BLOCKER is downgraded to WARN. Both findings remain in the report — only the
severity changes. A conflict resolution note is recorded so the developer
can review whether the resolution was correct.

#### 4.5.2 Closed-loop repair cycle

When the audit phase finds BLOCKER issues, the harness enters a **repair
cycle** instead of just reporting findings:

1. BLOCKER findings + conflict notes + verification output are packaged as
   repair context.
2. The developer is re-invoked with the repair context appended to its
   prompt. The developer makes targeted fixes (not a full re-implementation).
3. Verification re-runs on the repaired code.
4. Audit re-runs on the repaired code.
5. If all BLOCKERs are resolved, the task is marked complete and the round
   checkpoints.
6. If BLOCKERs remain, steps 1–4 repeat, up to `max_repairs` (default: 3).
7. If BLOCKERs persist after `max_repairs`, the task is marked `blocked` with
   the reason. The planner may split the task or add remediation tasks in the
   next round.

This is the **Generator-Critic** pattern: auditors (critics) feed concrete
revision instructions back to the developer (generator). The audit is not a
report card — it is a quality mechanism that drives repair.

#### 4.5.3 Early exit on clean audit

If all auditors return no BLOCKER findings, the repair cycle is skipped
entirely. The task is marked complete and the round checkpoints immediately.
No repair cycles are wasted on clean code.

### 4.6 Round-adaptive role selection

The planner may emit a `roles_override` field in the plan's YAML front
matter (JSON-encoded) to adjust roles for the next round. This allows the
factory to adapt its role configuration based on what the metrics show.

Supported override keys:

| Key | Description |
|---|---|
| `skip_auditors` | List of auditor names to skip next round |
| `skip_studies` | List of study subagent names to skip |
| `add_auditors` | List of auditor dicts to add |
| `add_studies` | List of study dicts to add |
| `add_developers` | List of developer dicts to add or replace |
| `auditor_models` | Dict of auditor name → model override |
| `study_models` | Dict of study name → model override |
| `developer_models` | Dict of developer name → model override |
| `planner_model` | Model override for the planner |

Example: if round 1's audit found zero security issues but 4 efficiency
issues, the planner can drop the security auditor and add a second
efficiency-focused developer for round 2.

### 4.7 Metrics feedback loop

The harness logs per-round metrics to `.factory-state/metrics.jsonl`:

- **Auditor precision**: fraction of BLOCKERs upheld (not overturned by
  conflict resolution). Auditors with precision below 50% are flagged in
  the metrics summary with a warning.
- **Developer rejection rate**: fraction of proposals rejected by the
  integration developer. Developers with rejection rates above 50% are
  flagged.
- **Repair cycle stats**: average repair cycles per round, fraction
  resolved by repair.
- **Round outcomes**: count of completed, blocked, and failed rounds.

The metrics summary is fed to the planner at the start of each round. The
planner can use it to emit `roles_override` adjustments. Without this
loop, roles.toml tuning is guesswork.

### 4.8 Model tiering

Each role may specify a `model` field in `roles.toml` to use a different
model than the CLI `--model` default. This enables cost optimization:

- **Study subagents**: read-only, focused tasks → cheaper/faster model.
- **Auditors**: read-only, focused tasks → cheaper/faster model.
- **Planner**: synthesizes reports and makes decisions → strongest model.
- **Integration developer**: reconciles proposals and commits → strongest model.
- **Developers**: write code → strong model (or default).

Phase-level overrides: `planner_model` and `integration_model` in
`roles.toml`. Per-subagent overrides: `model` field on each study,
developer, or auditor entry.

### 4.9 Round scratchpad

After each round, the harness writes a structured summary to
`.factory/rounds/N.md` containing:

- What was planned (task selected).
- What was implemented (attempts, files changed).
- What verification produced (exit code, runner).
- What auditors found (BLOCKERs, conflicts).
- What repair cycles did (cycles used, resolved or not).
- What issues are tracked (from the issue tracker).
- The round outcome.

At the start of the next round, the last 3 round scratchpads are read
and included in the planner's context. This provides iteration continuity
across fresh-context invocations — the planner knows what was tried
before and can avoid repeating the same mistakes.

### 4.10 Accumulating issue tracker

The harness maintains `.factory/issues.json` — a persistent record of
audit findings across rounds. When an auditor flags a BLOCKER:

1. The tracker checks if a matching issue exists (same auditor + same
   file reference).
2. If yes, `repeat_count` is incremented.
3. If no, a new issue is created.
4. If `repeat_count` reaches `escalation_threshold`, the issue is marked
   `escalated` and the campaign terminates with outcome `escalated`.
5. If an issue is not seen in a subsequent round, it is marked `resolved`.

The issue tracker summary is fed to the planner and study agents so they
know which issues are recurring. Recurring issues signal that the repair
mechanism is not working and a different approach is needed.

### 4.11 Cost accounting per phase

The harness tracks wall-clock time per phase per round:

- `planning_time_s`: study subagents + planner.
- `implementation_time_s`: developers + integration.
- `verification_time_s`: task verification.
- `audit_time_s`: specialist auditors.
- `repair_time_s`: repair cycle (developer + verify + audit per cycle).

This data is included in the metrics summary. If planning consistently
costs more than implementation, the fan-out is too wide for the value it
produces — the planner can reduce study subagents via `roles_override`.

## 5. Plan contract

The plan is a markdown file at `.factory/artifacts/implementation-plan.md`.

### 5.1 Front matter

```yaml
---
spec_path: docs/SPEC.md
spec_commit: <sha1>
base_commit: <sha1>
status: active
---
```

### 5.2 Tasks

Each task is a `## Task N:` heading with fields:

| Field | Required | Description |
|---|---|---|
| `Title:` | yes | Unique short description |
| `Status:` | yes | `pending`, `in_progress`, `completed`, `blocked` |
| `Dependencies:` | no | Comma-separated task numbers |
| `Acceptance:` | yes | What must be true for this task to be complete |
| `Verification:` | yes | Command(s) to run to verify |
| `Runner:` | no | Required runner capability (e.g., `physical-controller`) |
| `Evidence:` | no | What the tester/auditor produced |

The final task MUST be `## Final documentation and specification audit` and
MUST depend on every other task.

### 5.3 Task properties

- Task numbers are unique, increasing, and contiguous.
- The plan parser fails closed on: duplicate task numbers, missing required
  fields, unknown dependencies, self-dependencies, or a missing final audit task.
- Completed tasks remain in the plan with `Status: completed`.

## 6. Deterministic task selection

The trusted selector (`.factory/loop/selector.py`) picks exactly one task per
implementation attempt. The model never chooses among tasks.

Selection rules (in priority order):

1. The selected task MUST be `pending` or `in_progress`.
2. All of the task's dependencies MUST be `completed`.
3. If the task requires a `Runner:` capability, that runner MUST be available.
4. Among eligible tasks, select the lowest-numbered one.
5. If no task is eligible, the selector returns `work_exhausted` or `blocked`.

## 7. Fresh-context execution

Each role invocation is a fresh CLI call:

```bash
pi2 --provider ollama --model {model} --print --no-session [--approve] < prompt
```

- No `--resume`, no session continuation, no memory injection.
- The prompt is the only input channel; all other context comes from the
  repository on disk or from the orchestrator (study reports, task excerpts).
- The orchestrator captures stdout, stderr, and exit code.
- Model prose is never control protocol — the orchestrator derives outcomes
  from plan state, Git state, exit status, and verification results.

### 7.1 Parallel execution

Study subagents and specialist auditors run in parallel via
`ThreadPoolExecutor`. Each subagent is an independent `pi2` subprocess with
its own fresh context. Subagents that only read the codebase (study,
audit) run without `--approve`. Developers run with `--approve` when single
(serial mode) or without `--approve` when multiple (parallel propose mode,
where the integration developer applies changes).

### 7.2 Task-resource budget

Each task has a bounded attempt budget (default: 3 attempts). Each campaign
has a bounded round budget (default: 20 rounds). A campaign timeout (default:
21600 seconds / 6 hours) is enforced. When any budget is exhausted, the
campaign terminates honestly.

## 8. Runner awareness and resource allocation

### 8.1 Environment declaration

`.factory/environment.toml` declares external runners and local tools:

```toml
schema_version = 1

[[runners]]
name = "ralphrunner"
transport = "ssh"
ssh_config_alias = "ralphrunner"
working_directory = "/srv/factory-work/controller-box"
capabilities = ["physical-controller", "inputplumber-system-dbus", "gpu-compositor"]
verify_command = "./scripts/verify.sh"
```

- SSH aliases, credentials, and provisioning stay outside the repository.
- An empty runners section means all verification runs locally.
- Capabilities are strings; the factory does not infer or invent them.

### 8.2 Runner availability check

Before each campaign and before selecting a task that requires a runner
capability, the orchestrator checks runner availability:

```bash
ssh -o ConnectTimeout=5 -o BatchMode=yes {ssh_config_alias} true
```

- If a required runner is unreachable, the task is marked `blocked` with the
  reason `runner-unavailable: {runner_name}`.
- If no task can proceed due to runner unavailability, the campaign terminates
  `blocked` with an explicit reason — it never silently skips runner-dependent
  tests or pretends they passed.

### 8.3 Runner verification

When a task requires a runner capability, verification runs on the runner:

1. The orchestrator syncs the current commit to the runner's working directory
   via `rsync` over SSH.
2. The orchestrator runs the task's `Verification:` command on the runner via
   `ssh {alias} cd {working_directory} && {command}`.
3. The orchestrator captures stdout, stderr, and exit code as the verification
   receipt.
4. Local verification (no runner required) runs directly via `subprocess.run`.

### 8.4 No-cheat verification guarantee

The tester role independently runs verification and reports the actual result.
The orchestrator NEVER takes the developer's word that tests passed — it runs
the verification command itself (locally or on a runner) and records the real
exit code. The auditor checks for:

- weakened assertions or removed tests;
- skipped test cases without explicit blocked status;
- tests that always pass (tautological assertions);
- verification commands that don't actually test the implementation.

If the auditor finds any of these, it creates a finding that becomes a plan
task in the next planning round.

## 9. Campaign semantics

### 9.1 Phase machine

Each round executes:

```
planning → selection → implementation → verification → audit
  → if clean audit: checkpoint → next round
  → if BLOCKERs: [repair → verification → audit] × max_repairs
    → if resolved: checkpoint → next round
    → if unresolvable: task blocked → checkpoint → next round
```

- **Planning**: parallel study subagents analyse the codebase; the planner
  synthesises their reports into the plan. Outcome: `planned` or `failed`.
- **Selection**: deterministic task selection (model never chooses).
- **Implementation**: parallel developers propose changes per area; the
  integration developer applies and commits. Outcome: `task_completed`,
  `task_progress`, `task_failed`, or `interrupted`.
- **Verification**: the orchestrator runs the verification command
  independently (locally or on a runner). Outcome: `verified` or
  `verification_failed`. Verification output is captured and fed back to
  the developer on the next attempt or repair cycle.
- **Audit**: parallel specialist auditors review the codebase. Outcome:
  `audit_pass` (no BLOCKERs) or `audit_findings` (BLOCKERs found).
- **Repair** (if BLOCKERs): BLOCKER findings + verification output are
  fed back to the developer. The developer makes targeted fixes.
  Re-verification and re-audit follow. Up to `max_repairs` cycles.
- **Checkpoint**: commit and advance to the next round.

### 9.2 Terminal outcomes

A campaign always terminates with exactly one of:

| Outcome | Meaning |
|---|---|
| `success` | All plan tasks completed, all verification passed, audit clean |
| `findings` | Work done but audit found issues that need remediation |
| `blocked` | Tasks remain but cannot proceed (runner unavailable, external dependency) |
| `failed` | Planning failed or a task failed after exhausting its attempt budget |
| `stale` | No improvement in audit findings for K consecutive rounds (stale_rounds threshold) |
| `escalated` | Same issue recurred N times across rounds (escalation_threshold exceeded) |
| `interrupted` | Campaign timeout or process interruption |
| `infrastructure_failure` | Runner unreachable, SSH failure, or other infrastructure error |

The campaign NEVER spins on empty work. `work_exhausted` (no eligible tasks)
always reaches verification and audit and never silently succeeds.

### 9.3 Round and attempt bounds

- `--rounds N` (default 20): maximum planning-implement-verify-audit cycles.
- `--implementation-attempts N` (default 3): maximum retries for one task
  during the implementation+verification phase.
- `--max-repairs N` (default 3): maximum repair cycles after audit finds
  BLOCKER issues. Each repair cycle runs developer → verification → audit.
- `--stale-rounds K` (default 3): if audit findings don't improve for K
  consecutive rounds (no clean audit), the campaign terminates with
  outcome `stale`.
- `--escalation-threshold N` (default 3): if the same issue (same auditor
  + same file) recurs N times across rounds, it is marked `escalated` and
  the campaign terminates with outcome `escalated`.
- `--campaign-timeout S` (default 21600): wall-clock timeout in seconds.

When a budget is exhausted, the campaign terminates with the honest outcome.
Reaching a ceiling is NEVER success.

## 10. Minimal control state

One JSON file: `.factory-state/factory-loop.json`

```json
{
  "schema": "factory-state/v1",
  "campaign_id": "unique-id",
  "current_round": 1,
  "current_phase": "planning",
  "selected_task_id": null,
  "attempt_number": 1,
  "repair_count": 0,
  "stale_rounds": 0,
  "last_outcome": null,
  "terminal_outcome": null,
  "rounds_completed": 0,
  "phase_history": []
}
```

- The orchestrator reads and writes this file; roles never see it.
- Round and attempt counters are monotonic (never go backward).
- Recovery derives from Git + plan + this state file.

## 11. Git boundary

- The orchestrator is the sole Git writer. Roles never run `git commit`.
- After each implementation+verification cycle, the orchestrator commits:
  `git add -A && git commit -m "factory: task {N} round {R}"`.
- The orchestrator works on the development branch (`config.toml`).
- The human promotes to `main` manually:
  `git switch main && git merge --no-ff {branch} && git tag vX.Y.Z`.
- No worktrees. No automatic push or tag.

## 12. Recovery

Recovery is derived from Git, the plan, and the control-state file:

- A clean committed task resumes from the next deterministic task.
- An `in_progress` task resumes from current code and Git diff.
- An interrupted attempt's tests are rerun.
- An ambiguous state (changed branch, stale spec binding, rewound counter)
  fails closed for operator inspection.
- Recovery never resets, discards, or silently overwrites dirty work.

## 13. Configuration

### 13.1 config.toml

```toml
[project]
spec = "docs/SPEC.md"
plan = ".factory/artifacts/implementation-plan.md"
development_branch = "develop"
release_branch = "main"

[verification]
command = ["./scripts/verify.sh"]

[campaign]
default_rounds = 20
default_attempts = 3
default_timeout = 21600
max_repairs = 3
stale_rounds = 3
escalation_threshold = 3

[git]
checkpoint_each_iteration = true
```

### 13.2 environment.toml

Declares runners and tools (see §8.1). This is the only file that needs
project-specific editing for runner setup.

### 13.3 roles.toml

Declares the parallel role structure (see §4). This file defines:

- `[planning]`: study subagents and planner prompt.
- `[implementation]`: developer subagents and integration developer prompt.
- `[audit]`: specialist auditor subagents.

Projects can add, remove, or modify subagents to tailor the process. The
default structure (3 studies + auto-discovered subsystems, 1 developer,
6 auditors) is a good starting point. See `.factory/roles.toml` for the
full format.

### 13.4 Preflight

Before a campaign, the orchestrator runs a simple preflight:

1. The spec file exists and is not the placeholder.
2. The development branch is clean with at least one commit.
3. All declared runners are reachable (SSH connectivity check).
4. The verification command exists and is executable.
5. The plan file exists (or will be created by the first planning round).

If any check fails, the campaign reports `infrastructure_failure` with the
reason and exits. No readiness policy, no production authority enrollment,
no signed trust chain — just "is it ready to go?"

## 14. Implementation language

The control plane is Python 3.11+ (stdlib only, no external dependencies).
The model CLI is invoked via `subprocess.run` with the prompt on stdin.

## 15. Setup

```bash
# 1. Write your product spec
$EDITOR docs/SPEC.md

# 2. Declare runners (if any)
$EDITOR .factory/environment.toml

# 3. Set your verification command
$EDITOR .factory/config.toml

# 4. Run a campaign
python3 .factory/loop/campaign.py run \
  --campaign-id my-project-v1 \
  --rounds 20 \
  --branch develop \
  --provider ollama \
  --model qwen3.6:27b
```

That's it. No Landlock kernel, no installer/stager, no readiness policy,
no SSH key enrollment, no 34 schemas. Just spec, plan, loop, runners, done.

## 16. Requirement registry

| ID | Requirement |
|---|---|
| SPEC-01 | One canonical specification, never edited during implementation |
| PLAN-01 | One canonical plan as sole task ledger; parser fails closed on malformed input |
| TASK-01 | Deterministic selection of exactly one task per attempt; model never chooses |
| CTX-01 | Every role is a fresh CLI invocation with no memory or session resume |
| ROLE-01 | Configurable parallel roles: study subagents, planner, developers, integration developer, specialist auditors |
| AUDIT-01 | Auditors use BLOCKER/WARN/INFO severity; BLOCKERs trigger repair cycle |
| AUDIT-02 | Cross-auditor conflicts resolved by priority (security > functional > spec > compatibility > efficiency > linting) |
| AUDIT-03 | Closed-loop repair: BLOCKERs + verification output fed back to developer; capped at max_repairs cycles |
| AUDIT-04 | Early exit on clean audit: no repair cycle when no BLOCKERs found |
| AUDIT-05 | Unresolvable BLOCKERs after max_repairs → task marked blocked, not silently passed |
| METRIC-01 | Per-round metrics logged to JSONL: auditor precision, developer rejection rate, repair stats |
| METRIC-02 | Metrics summary fed to planner to enable feedback-driven role tuning |
| ADAPT-01 | Planner may emit roles_override in plan front matter to adjust roles per round |
| ADAPT-02 | Supported overrides: skip/add auditors, studies, developers; model overrides per role |
| TIER-01 | roles.toml supports per-role model field for cost-optimized model tiering |
| STALE-01 | stale_rounds threshold: campaign stops after K consecutive rounds with no audit improvement |
| STALE-02 | escalation_threshold: same issue recurring N times → campaign terminates as escalated |
| COST-01 | Per-phase wall-clock time tracked per round (planning, implementation, verification, audit, repair) |
| COST-02 | Cost data included in metrics summary; planning > implementation triggers fan-out warning |
| SCRATCH-01 | Round scratchpad written to .factory/rounds/N.md after each round |
| SCRATCH-02 | Prior round scratchpads (last 3) fed to planner for iteration continuity |
| ISSUES-01 | Accumulating issue tracker at .factory/issues.json with cross-round deduplication |
| ISSUES-02 | Recurring issues (same auditor + same file) increment repeat_count; threshold → escalation |
| VERIFY-01 | Tester independently runs verification and reports actual exit code |
| VERIFY-02 | Auditor checks for weakened assertions, skipped tests, or fake passes |
| RUNNER-01 | Runners declared in environment.toml with SSH transport and capabilities |
| RUNNER-02 | Runner availability checked before campaign and before runner-dependent tasks |
| RUNNER-03 | Unreachable runners cause blocked status, never silent skip or fake pass |
| CAMP-01 | Finite campaigns with six terminal outcomes; never spins on empty work |
| CAMP-02 | Reaching a budget ceiling is never success |
| STATE-01 | One minimal control-state file; monotonic counters; orchestrator-only |
| GIT-01 | Orchestrator is sole Git writer; roles never commit |
| RECOV-01 | Recovery from Git + plan + state; dirty work never discarded |