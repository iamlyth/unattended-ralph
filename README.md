# Ralph Software Factory — Minimal

A minimal, autonomous software factory based on the Ralph Wiggum technique.
The factory builds software from a canonical specification using a fresh-context
loop with deterministic task selection, a multi-model developer ensemble,
parallel auditing, and awareness of external test runners. It is
human-out-of-the-loop: once a campaign starts, it runs to a terminal outcome
without human intervention.

The control plane is stdlib-only Python under `.factory/loop/` (12 modules,
~3,300 lines). It is **stateless** — the plan IS the state. There is no
control-state file. If a campaign is interrupted, just run it again; it reads
the plan and picks the next pending task. This mirrors the original Ralph loop:
`while :; do factory-campaign run; done`.

## Prerequisites

- Python 3.11+ (the control plane is stdlib-only)
- Git
- An agent CLI (`pi2`, `pi`, or future: `claude`, `codex`)
- Model provider access (Ollama, OpenAI, Google, etc.)
- SSH access to runners (optional, only if you declare external runners)

## Quick start

```bash
# 1. Write your product spec
$EDITOR docs/SPEC.md

# 2. Declare runners (if any)
$EDITOR .factory/environment.toml

# 3. Set your verification command and harness
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

## Workflow

```
═══════════════════════════════════════════════════════════════════════
  PHASE 1: PLANNING  (factory-campaign plan — manual, run once)
═══════════════════════════════════════════════════════════════════════

  ┌─ Study: spec ────────────────────────┐
  │  (CLI --model)                       │
  │  Reads docs/SPEC.md                  │      All run in
  └──────────────────────────────────────┘      parallel
  ┌─ Study: architecture ────────────────┐      (read-only)
  │  (CLI --model)                       │
  │  Reads codebase structure            │
  └──────────────────────────────────────┘
  ┌─ Study: bugs ────────────────────────┐
  │  (CLI --model)                       │
  │  Runs tests, reads .factory/bugs/    │
  └──────────────────────────────────────┘
  ┌─ Study: quality ─────────────────────┐
  │  (CLI --model)                       │
  │  Scans for hacks, dead code,         │
  │  simplification opportunities        │
  └──────────────────────────────────────┘
  ┌─ Study: subsystem (auto) ────────────┐
  │  (CLI --model)                       │  (one per src/ subdir)
  └──────────────────────────────────────┘
                    │
                    ▼
  ┌─ Planner ────────────────────────────┐
  │  (CLI --model)                       │
  │  Synthesizes study reports           │
  │  Writes implementation-plan.md       │
  │  Includes code quality cleanup tasks │
  └──────────────────────────────────────┘

═══════════════════════════════════════════════════════════════════════
  PHASE 2-6: IMPLEMENTATION LOOP  (factory-campaign run — per round)
═══════════════════════════════════════════════════════════════════════

  ┌─ SELECT ─────────────────────────────────────────────────────┐
  │  Deterministic selector (trusted, no model)                  │
  │  Picks next pending task from plan                           │
  └──────────────────────────────────────────────────────────────┘
                    │
                    ▼

  ┌─ IMPLEMENT: 3 developers in parallel (worktree-isolated) ────┐
  │                                                              │
  │  ┌─ approach-a ──────────┐  ┌─ approach-b ──────────┐       │
  │  │ model: configurable   │  │ model: configurable   │       │
  │  │ provider: configurable│  │ provider: configurable│       │
  │  │ "Minimal & direct"    │  │ "Robust & defensive"  │       │
  │  │ --approve (edits      │  │ --approve (edits      │       │
  │  │  files, builds,       │  │  files, builds,       │       │
  │  │  tests in worktree)   │  │  tests in worktree)   │       │
  │  └───────────────────────┘  └───────────────────────┘       │
  │                                                              │
  │  ┌─ approach-c ──────────┐                                  │
  │  │ model: configurable   │  Each developer gets an isolated │
  │  │ provider: configurable│  git worktree — real files, real │
  │  │ "Refactor & reuse"    │  builds, real tests. No sharing. │
  │  │ --approve             │                                  │
  │  └───────────────────────┘                                  │
  │                                                              │
  │  Harness collects git diff from each worktree as a patch     │
  └──────────────────────────────────────────────────────────────┘
                    │
                    ▼

  ┌─ JUDGE: evaluates patches (read-only, --no-tools) ───────────┐
  │  model: configurable (e.g. gpt-5.6-sol)                     │
  │                                                              │
  │  Patches are included IN the prompt (no file reading).      │
  │  Judge reads all 3 patches, evaluates against acceptance     │
  │  criteria, and prints:                                       │
  │    SELECTED: approach-X                                      │
  │    FALLBACK: approach-Y, approach-Z                          │
  │                                                              │
  │  Single API call — minutes, not hours.                       │
  └──────────────────────────────────────────────────────────────┘
                    │
                    ▼

  ┌─ HARNESS: applies + verifies + commits ──────────────────────┐
  │  (Python, no model needed — seconds)                         │
  │                                                              │
  │  1. git apply approach-X.patch                               │
  │  2. ./scripts/verify.sh (build + test)                       │
  │  3. If verification FAILS → try FALLBACK approaches          │
  │  4. If verification PASSES → git commit                      │
  │  5. If ALL approaches fail → task marked blocked             │
  └──────────────────────────────────────────────────────────────┘
                    │
              verification passed?
               /         \
             No           Yes
              │             │
              ▼             ▼
    try next fallback   ┌─ AUDIT: 8 specialist auditors (parallel) ──┐
    or mark blocked     │                                            │
                        │  All read-only, each different focus:      │
                        │                                            │
                        │  Priority (highest wins conflicts):        │
                        │  ┌──────────────────┐  ┌──────────────────┐│
                        │  │ security    (7)  │  │ functional   (6) ││
                        │  └──────────────────┘  └──────────────────┘│
                        │  ┌──────────────────┐  ┌──────────────────┐│
                        │  │ spec-compl.  (5) │  │ compatibility(4) ││
                        │  └──────────────────┘  └──────────────────┘│
                        │  ┌──────────────────┐  ┌──────────────────┐│
                        │  │ efficiency   (3) │  │ readability  (2) ││
                        │  └──────────────────┘  └──────────────────┘│
                        │  ┌──────────────────┐  ┌──────────────────┐│
                        │  │ simplifier   (1) │  │ linting      (0) ││
                        │  └──────────────────┘  └──────────────────┘│
                        │                                            │
                        │  Each can use a different model/provider.  │
                        └────────────────────────────────────────────┘
                              │
                        BLOCKERs found?
                         /         \
                       No           Yes
                        │             │
                        ▼             ▼
                  checkpoint    ┌─ REPAIR CYCLE (up to max_repairs) ─┐
                  (commit)      │                                     │
                        │       │  3 developers run AGAIN with        │
                        ▼       │  BLOCKER findings as context.       │
                   next round   │  Judge selects best fix.            │
                                │  Harness applies + verifies.        │
                                │  Audit re-runs.                     │
                                │                                     │
                                │  Still BLOCKERs? → next repair      │
                                │  All repairs exhausted? → blocked   │
                                └─────────────────────────────────────┘
                                          │
                                    BLOCKERs resolved?
                                     /         \
                                   No           Yes
                                    │             │
                                    ▼             ▼
                              blocked      checkpoint
                                          (commit)
                                                │
                                                ▼
                                           next round


═══════════════════════════════════════════════════════════════════════
  FINALIZATION  (when all tasks completed/blocked)
═══════════════════════════════════════════════════════════════════════

  ┌─ Overall verification ───────────────────────────────────────┐
  │  ./scripts/verify.sh (full build + test)                     │
  └──────────────────────────────────────────────────────────────┘
                    │
                    ▼
  ┌─ Final audit: 8 auditors in parallel ────────────────────────┐
  │  Reviews entire codebase, not just one task                  │
  └──────────────────────────────────────────────────────────────┘
                    │
                    ▼
  ┌─ Terminal outcome ───────────────────────────────────────────┐
  │  success (exit 0)    — all done, audit clean                 │
  │  findings (exit 1)   — all done, audit found BLOCKERs        │
  │  blocked (exit 1)    — tasks blocked, can't proceed          │
  │  escalated (exit 1)  — same issue recurs N times             │
  │  interrupted (exit 1)— timeout                               │
  └──────────────────────────────────────────────────────────────┘
```

## Two commands

The factory has two independent commands:

### `factory-campaign plan`

Creates or refreshes the plan. Runs study subagents in parallel (spec,
architecture, bugs, quality, + one per source subsystem), then the planner
synthesises their reports into the canonical plan. The code quality study
subagent scans for hacks, dead code, and simplification opportunities — the
planner creates cleanup tasks from these findings, ensuring the codebase
improves incrementally over the campaign.

### `factory-campaign run`

Runs the implementation loop. Reads the plan, picks the next pending task,
runs the developer ensemble → judge → harness → audit → repair → checkpoint.
Repeats for `--rounds` iterations or until all tasks are done. No state file —
the plan IS the state.

## Multi-model developer ensemble

Three developers independently implement the same task from different
directions, each in an isolated git worktree:

| Approach | Style | Configurable |
|----------|-------|-------------|
| approach-a | Minimal & direct — least code, simple solutions | model + provider |
| approach-b | Robust & defensive — edge cases, input validation | model + provider |
| approach-c | Refactor & reuse — study existing code, improve as you go | model + provider |

Each developer gets a **real, isolated git worktree** where they can edit,
build, and test independently. The harness collects `git diff` from each
worktree as a patch.

The **judge** (a separate model, running with `--no-tools`) evaluates all 3
patches inline in a single API call and selects the best one. The **harness**
(Python, no model) then applies the selected patch, runs verification, and
commits — in seconds, not hours.

If the selected patch fails verification, the harness automatically tries the
fallback approaches in order. If all fail, the task is marked blocked.

## Roles

Roles are fresh-context CLI invocations — no memory, no session resume. Each
role gets a clean slate every time it runs. Roles are configured in
`.factory/roles.toml`.

**Planning phase** (`factory-campaign plan`):

| Role | Count | Description |
|------|-------|-------------|
| Study subagents | 4 + auto-discovered | Analyse the codebase in parallel (read-only) |
| Planner | 1 | Synthesises study reports into the canonical plan |

**Implementation loop** (`factory-campaign run`, per round):

| Role | Count | Description |
|------|-------|-------------|
| Developers | 3 | Independent implementations in worktrees (with `--approve`) |
| Judge | 1 | Evaluates patches, selects best (read-only, `--no-tools`) |
| Auditors | 8 | Parallel specialist review (read-only) |

The eight specialist auditors and their priority (highest wins conflicts):

1. **security** — vulnerabilities, attack surface, input validation
2. **functional** — does the code actually work
3. **spec-compliance** — compliance with the project specification
4. **compatibility** — platform, dependency, and API compatibility
5. **efficiency** — performance, resource usage, complexity
6. **readability** — human readability, natural code flow, clear naming
7. **simplifier** — code complexity reduction, dead code, over-engineering
8. **linting** — language linting, readability

### Repair cycle (Generator-Critic pattern)

When auditors find BLOCKERs, the repair cycle activates:

1. 3 developers run again with BLOCKER findings as context
2. Judge selects the best repair patch
3. Harness applies + verifies
4. Audit re-runs
5. Repeat up to `max_repairs` (default 3)
6. If BLOCKERs persist → task marked `blocked`

Different approaches may be selected for different repair rounds — one model
might produce the best initial implementation, another the best fix for a
specific BLOCKER.

### Cross-auditor conflict resolution

When two auditors flag BLOCKER on the same file, the lower-priority BLOCKER is
downgraded to WARN. Priority order: security > functional > spec-compliance >
compatibility > efficiency > readability > simplifier > linting.

## Model tiering

Each role in `roles.toml` supports optional `model` and `provider` fields.
Different roles can use different models and providers:

```toml
# Developers — different models for different approaches
[[implementation.developers]]
name = "approach-a"
model = "deepseek-v4.1-flash"      # cheap, fast
provider = "ollama-cloud"

[[implementation.developers]]
name = "approach-b"
model = "gpt-5.6-luna"             # stronger reasoning
provider = "openai-codex"

# Judge — fast model, no tools needed
integration_model = "gpt-5.6-sol"
integration_provider = "openai-codex"
integration_timeout = 3600

# Auditors — split across providers
[[audit.auditors]]
name = "security"
model = "glm-5.3"
provider = "ollama-cloud"

[[audit.auditors]]
name = "spec-compliance"
model = "gpt-5.6-sol"
provider = "openai-codex"
```

If no `model` is specified, the CLI `--model` argument is used as fallback.
If no `provider` is specified, the CLI `--provider` argument is used.

## Configurable harness command

The agent CLI command is configurable in `config.toml`:

```toml
[harness]
# "pi2" = pi2 coding agent (default, cloud providers)
# "pi"  = pi coding agent (offline, local models)
# Future: "claude", "codex", "antigravity"
command = "pi2"
```

This enables using different agent harnesses without changing the control
plane code.

## Selected approach tracking

The judge reports which approach it selected via `SELECTED: approach-X` in
its output. The campaign logs this for model comparison:

```
implementation: applying approach-b (gpt-5.6-luna)
```

Over multiple rounds, you can track which model produces the best code most
often — useful for model selection and cost optimization.

## Metrics and feedback

- **Per-round metrics** logged to `.factory-state/metrics.jsonl`: auditor
  precision, developer rejection rate, repair cycle stats, per-phase time.
- **Round scratchpads** written to `.factory-state/rounds/N.md`: structured
  summary after each round. Last 3 fed to the planner.
- **Accumulating issue tracker** at `.factory-state/issues.json`: tracks
  BLOCKER findings across rounds. Same issue recurring N times → campaign
  terminates as `escalated`.
- **Selected approach tracking**: which model won each round, for model
  comparison.

## Code quality (entropy prevention)

Without cleanup, every campaign adds code but never removes it — complexity
grows monotonically until the factory chokes on its own output. Two mechanisms
create a **negative feedback loop**:

1. **Code quality study subagent** (proactive) — scans the entire codebase
   during planning for hacks, dead code, duplication, and simplification
   opportunities. The planner creates cleanup tasks from HIGH/MEDIUM findings.

2. **Simplifier auditor** (reactive) — reviews each task's changes for
   unnecessary complexity, dead code, and over-engineering. BLOCKERs force
   the developer to simplify.

Together, they ensure the codebase reaches equilibrium rather than growing
forever.

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

## Configuration files

| File | Purpose |
|------|---------|
| `.factory/config.toml` | Project spec, verification command, harness command, campaign defaults |
| `.factory/environment.toml` | Runner declarations and capabilities |
| `.factory/roles.toml` | Role definitions, model/provider overrides, subagent prompts |
| `.factory/prompts/*.md` | Role prompt files (planner, developers, judge, auditors, studies) |
| `docs/SPEC.md` | The product specification (source of truth, never edited during implementation) |
| `docs/FACTORY-LOOP-SPEC.md` | The factory loop specification |

## File layout

```
.factory/
  bin/factory-campaign       # CLI entrypoint (plan / run)
  loop/                      # Control plane (12 Python modules, stdlib-only)
    campaign.py              # Orchestrator: auto-clean, plan-once, implementation loop
    parallel.py              # Parallel subagent execution, os.killpg, audit parsing
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
  prompts/                   # Role prompt files
    planner.md               # Planner
    developer-a.md           # Minimal & direct approach
    developer-b.md           # Robust & defensive approach
    developer-d.md           # Refactor & reuse approach (used by approach-c)
    integration-developer.md # Judge (evaluator, no tools)
    auditor-security.md      # 8 specialist auditors
    auditor-functional.md
    auditor-spec.md
    auditor-compatibility.md
    auditor-efficiency.md
    auditor-readability.md
    auditor-simplifier.md
    auditor-linting.md
    study-spec.md            # Study subagents
    study-architecture.md
    study-bugs.md
    study-quality.md
    study-subsystem.md
  config.toml                # Project configuration + harness command
  environment.toml           # Runner declarations
  roles.toml                 # Role structure, model/provider tiering
  ssh/                       # SSH config and keys (gitignored)
  patches/                   # Developer patches (gitignored, temporary)
  artifacts/                 # Plan and audit artifacts
  worktrees/                 # Developer worktrees (gitignored, temporary)
```

## Recovery

The campaign is **stateless**. Recovery is derived from Git and the plan:

- A clean committed task resumes from the next deterministic task.
- An interrupted campaign resumes by just running again.
- The campaign auto-cleans before preflight: commits the plan file, reverts
  other changes, removes untracked files. No manual cleanup needed.
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