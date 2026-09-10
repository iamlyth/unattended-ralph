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

Four static roles, each a separate fresh CLI invocation with a static prompt.
No role resumes a prior session or inherits another role's memory.

### 4.1 Planner

- Inputs: planner prompt, `AGENTS.md`, specification, current plan, current code.
- Responsibilities: inspect code before planning; create or revise the canonical
  plan; translate findings into bounded tasks with dependencies and acceptance
  criteria; never modify product code or the specification.
- The planner is the only role that creates, removes, splits, or reorders tasks.

### 4.2 Developer

- Inputs: developer prompt, `AGENTS.md`, specification, plan, current code,
  one deterministically selected task.
- Responsibilities: implement only the selected task; run focused verification;
  update the task's plan status; never claim final product acceptance.
- The developer is the only role that writes product code.

### 4.3 Tester

- Inputs: tester prompt, `AGENTS.md`, specification, plan, committed code.
- Responsibilities: run deterministic verification independently of the
  developer; produce exact command receipts (stdout/stderr/exit code); never
  edit product code; never elevate unavailable evidence.
- The tester MUST run the verification command and report the actual result.
  It MUST NOT skip tests, weaken assertions, or report success without running
  the real verification.

### 4.4 Auditor

- Inputs: auditor prompt, `AGENTS.md`, specification, plan, committed code,
  one deterministically selected audit objective.
- Responsibilities: perform a read-only audit at the bound commit; check for
  test quality (no weakened assertions, no skipped tests, no fake passes);
  verify that the implementation matches the specification; never edit code.
- Audit findings feed the next planner revision as plan tasks.

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
cat .factory/prompts/{role}.md | pi2 --provider ollama --model {model}
```

- No `--resume`, no session continuation, no memory injection.
- The prompt is the only input channel; all other context comes from the
  repository on disk.
- The orchestrator captures stdout, stderr, and exit code.
- Model prose is never control protocol — the orchestrator derives outcomes
  from plan state, Git state, exit status, and verification results.

### 7.1 Task-resource budget

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
verify_command = "./scripts/verify-project.sh"
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
planning → implementation → verification → audit
```

- **Planning**: the planner creates or revises the plan. Outcome: `planned`
  or `failed`.
- **Implementation**: the developer implements the one selected task. Outcome:
  `task_completed`, `task_progress`, `task_failed`, or `interrupted`.
- **Verification**: the tester runs the verification command independently.
  Outcome: `verified` or `verification_failed`.
- **Audit**: the auditor performs a read-only audit. Outcome: `audit_pass`
  or `audit_findings`.

### 9.2 Terminal outcomes

A campaign always terminates with exactly one of:

| Outcome | Meaning |
|---|---|
| `success` | All plan tasks completed, all verification passed, audit clean |
| `findings` | Work done but audit found issues that need remediation |
| `blocked` | Tasks remain but cannot proceed (runner unavailable, external dependency) |
| `failed` | Planning failed or a task failed after exhausting its attempt budget |
| `interrupted` | Campaign timeout or process interruption |
| `infrastructure_failure` | Runner unreachable, SSH failure, or other infrastructure error |

The campaign NEVER spins on empty work. `work_exhausted` (no eligible tasks)
always reaches verification and audit and never silently succeeds.

### 9.3 Round and attempt bounds

- `--rounds N` (default 20): maximum planning-implement-verify-audit cycles.
- `--implementation-attempts N` (default 3): maximum retries for one task.
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
command = ["./scripts/verify-project.sh"]
runner_command = ["./scripts/verify-on-runner.sh"]

[campaign]
default_rounds = 20
default_attempts = 3
default_timeout = 21600

[git]
checkpoint_each_iteration = true
```

### 13.2 environment.toml

Declares runners and tools (see §8.1). This is the only file that needs
project-specific editing for runner setup.

### 13.3 Preflight

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
| ROLE-01 | Four distinct roles: planner, developer, tester, auditor |
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