# Ralph Software Factory — Minimal

A minimal, autonomous software factory based on the Ralph Wiggum technique.
The factory builds software from a canonical specification using a fresh-context
loop with deterministic task selection, independent verification, parallel
auditing, and awareness of external test runners. It is human-out-of-the-loop:
once a campaign starts, it runs to a terminal outcome without human
intervention.

The control plane is stdlib-only Python under `.factory/loop/` (12 modules,
~3,300 lines). It is **stateless** — the plan IS the state. There is no
control-state file. If a campaign is interrupted, just run it again; it reads
the plan and picks the next pending task. This mirrors the original Ralph loop:
`while :; do factory-campaign run; done`.

## Prerequisites

- Python 3.11+ (the control plane is stdlib-only)
- Git
- An Ollama server (the model provider)
- SSH access to runners (optional, only if you declare external runners)

## Quick start

```bash
# 1. Write your product spec
$EDITOR docs/SPEC.md

# 2. Declare runners (if any)
$EDITOR .factory/environment.toml

# 3. Set your verification command
$EDITOR .factory/config.toml

# 4. Configure roles and model tiering
$EDITOR .factory/roles.toml

# 5. Create the plan (manual, independent)
python3 .factory/bin/factory-campaign plan \
  --campaign-id my-project-v1 --branch develop \
  --provider ollama --model deepseek-v4-flash

# 6. Run the implementation loop (stateless, resumable)
python3 .factory/bin/factory-campaign run \
  --campaign-id my-project-v1 --rounds 20 --branch develop \
  --provider ollama --model deepseek-v4-flash --attempts 2 --max-repairs 3

# Interrupted? Just run again — it picks up from the plan.
```

## Two commands

The factory has two independent commands:

### `factory-campaign plan`

Creates or refreshes the plan. Runs study subagents in parallel (up to 11:
spec, architecture, bugs, + one per source subsystem), then the planner
synthesises their reports into the canonical plan. Run this manually when you
want to (re)plan.

### `factory-campaign run`

Runs the implementation loop. Reads the plan, picks the next pending task,
implements → verifies → audits → repairs → checkpoints. Repeats for `--rounds`
iterations or until all tasks are done. No state file — the plan IS the state.

```
factory-campaign run
  → Round 1: select task → implement → verify → audit → repair? → checkpoint
  → Round 2: select task → implement → verify → audit → repair? → checkpoint
  → ...
  → All tasks done → finalize (overall verify + audit) → terminal outcome
```

## How it works

### Roles

Roles are fresh-context CLI invocations — no memory, no session resume. Each
role gets a clean slate every time it runs. Roles are configured in
`.factory/roles.toml`.

**Planning phase** (`factory-campaign plan`):

| Role | Count | Description |
|------|-------|-------------|
| Study subagents | 3 + auto-discovered | Analyse the codebase in parallel (read-only) |
| Planner | 1 | Synthesises study reports into the canonical plan |

**Implementation loop** (`factory-campaign run`, per round):

| Role | Count | Description |
|------|-------|-------------|
| Developer | 1+ | Implements the selected task (with `--approve`) |
| Integration developer | 1 | Merges parallel developer proposals, commits |
| Auditors | 6 | Parallel specialist review (read-only) |

The six specialist auditors and their priority (highest wins conflicts):

1. **security** — vulnerabilities, attack surface, input validation
2. **functional** — does the code actually work
3. **spec-compliance** — compliance with the project specification
4. **compatibility** — platform, dependency, and API compatibility
5. **efficiency** — performance, resource usage, complexity
6. **linting** — language linting, readability

### Implementation loop

Each round:

1. **Select** — the trusted selector deterministically picks the next pending
   task; the model never chooses among tasks.
2. **Implement** — the developer implements only the selected task.
3. **Verify** — the orchestrator independently runs the verification command
   (locally or on a runner) and reports the actual exit code.
4. **Audit** — six specialist auditors review in parallel, each producing
   BLOCKER / WARN / INFO findings with file references.
5. **Repair** — if any BLOCKERs: the developer is re-invoked with the audit
   findings + verification output. Re-verify, re-audit. Capped at `max_repairs`
   (default 3). Unresolvable → task marked `blocked`.
6. **Checkpoint** — the orchestrator (sole Git writer) commits.

If all auditors return no BLOCKERs, the repair cycle is skipped entirely (early
exit on clean audit).

### Cross-auditor conflict resolution

When two auditors flag BLOCKER on the same file, the lower-priority BLOCKER is
downgraded to WARN. Priority order: security > functional > spec-compliance >
compatibility > efficiency > linting.

### Terminal outcomes

A campaign always terminates with exactly one of eight outcomes:

| Outcome | Exit | Meaning |
|---------|------|---------|
| `success` | 0 | All tasks complete, final verification + audit clean |
| `findings` | 1 | All tasks complete, but audit found BLOCKERs |
| `blocked` | 1 | One or more tasks blocked (unresolvable by the developer) |
| `failed` | 1 | Verification failed after all attempts |
| `escalated` | 1 | Same issue recurred N times across rounds |
| `interrupted` | 1 | Campaign timeout or manual interruption |
| `infrastructure_failure` | 1 | Runner unreachable, preflight failure |

Reaching a budget ceiling is **never** success.

## Model tiering

Each role in `roles.toml` supports an optional `model` field. Study subagents
and auditors (read-only, focused tasks) can use cheaper/faster models; the
planner and integration developer (synthesis, decisions) should use the
strongest model.

```toml
# Example: cheap model for studies, strong model for auditors
[[planning.studies]]
name = "spec"
model = "deepseek-v4-flash"     # cheap, read-only

[[audit.auditors]]
name = "security"
model = "glm-5.3"               # stronger, fewer false positives
```

If no `model` is specified, the CLI `--model` argument is used as fallback.

## Metrics and feedback

- **Per-round metrics** logged to `.factory-state/metrics.jsonl`: auditor
  precision (BLOCKERs upheld vs overturned), developer rejection rate, repair
  cycle stats, per-phase wall-clock time, round outcomes.
- **Round scratchpads** written to `.factory-state/rounds/N.md`: structured
  summary after each round. Last 3 fed to the planner for iteration continuity.
- **Accumulating issue tracker** at `.factory-state/issues.json`: tracks
  BLOCKER findings across rounds. Same issue recurring N times → campaign
  terminates as `escalated`.
- **Cost accounting**: per-phase time tracked. If planning consistently costs
  more than implementation, the metrics summary warns about fan-out.

## Runner setup

External runners are declared in `.factory/environment.toml` with an SSH
transport, a working directory, and the capabilities they provide:

```toml
[[runners]]
name = "dev-runner-vm"
transport = "ssh"
ssh_config_alias = "dev-runner"
working_directory = "/srv/dev-runner/workspaces/my-project"
capabilities = ["kernel-uinput", "systemd-user"]
```

SSH config and keys live outside the repository (e.g. in `.factory/ssh/`,
gitignored). An empty runners section means all verification runs locally.
Runner-dependent tasks run on the declared runner; an unreachable runner marks
the task `blocked`, never a silent skip or fake pass.

The harness syncs the repo to the runner via rsync (excluding `.git`,
`.factory-state`, `build`), rebuilds from source on the runner (so build
artifacts contain correct runner-local paths), then runs the verification
command. If `shell.nix` exists, verification commands are wrapped in
`nix-shell --run '...'`.

## Configuration files

| File | Purpose |
|------|---------|
| `.factory/config.toml` | Project spec path, verification command, campaign defaults |
| `.factory/environment.toml` | Runner declarations and capabilities |
| `.factory/roles.toml` | Role definitions, model overrides, subagent prompts |
| `.factory/prompts/*.md` | Role prompt files (planner, developer, auditors, studies) |
| `docs/SPEC.md` | The product specification (source of truth, never edited during implementation) |
| `docs/FACTORY-LOOP-SPEC.md` | The factory loop specification (16 sections, 33 requirements) |

## File layout

```
.factory/
  bin/factory-campaign       # CLI entrypoint (plan / run)
  loop/                      # Control plane (12 Python modules, stdlib-only)
    campaign.py              # Orchestrator: plan-once, implementation loop, finalize
    parallel.py              # Parallel subagent execution, audit parsing, conflict resolution
    plan_parser.py           # Plan parser with roles_override support
    selector.py              # Deterministic task selection
    runner.py                # SSH runner verification with nix-shell wrapping
    preflight.py             # Pre-campaign checks
    metrics.py               # Per-round metrics tracking and JSONL logging
    issues.py                # Issue tracker and round scratchpad I/O
    gitutil.py               # Git operations
    lock.py                  # Campaign lock (prevents concurrent runs)
    state.py                 # State dataclass (used by metrics/issues)
    __init__.py              # Package exports
  prompts/                   # 15 role prompt files
  config.toml                # Project configuration
  environment.toml           # Runner declarations
  roles.toml                 # Role structure and model tiering
  ssh/                       # SSH config and keys (gitignored)
  artifacts/                 # Plan and audit artifacts
  state/                     # Metrics, issues, round scratchpads (gitignored)
```

## Recovery

The campaign is **stateless**. Recovery is derived from Git and the plan:

- A clean committed task resumes from the next deterministic task.
- An interrupted campaign resumes by just running again.
- An ambiguous state (changed branch, stale spec binding) fails closed for
  operator inspection.
- Recovery never resets, discards, or silently overwrites dirty work.

## Release

Release is manual. After the campaign reports a terminal outcome, review the
configured development branch and promote to `main`:

```bash
git switch main
git merge --no-ff <development-branch>
git tag vX.Y.Z
```

There is no automatic push, tag, or promotion.