# Ralph Software Factory Boilerplate

A reusable, single-writer implementation of Geoffrey Huntley's Ralph Wiggum development technique using Ralph Orchestrator, jailed Pi, Ollama, adaptive read-only subagents, Git checkpoints, quota waiting, and crash recovery.

`docs/SPEC.md` is retained as the first trial specification. Product implementation is intentionally absent on this branch.

## Operating model

- `main` is the human-controlled release branch.
- `develop` is the autonomous implementation branch.
- One committed `docs/SPEC.md` is the source of truth; Git versions it.
- A planning-only Ralph loop creates `IMPLEMENTATION_PLAN.md` for the exact spec commit.
- Each implementation iteration selects one bounded task and starts with fresh model context.
- Pi subagents perform parallel read-only planning, research, review, security, and documentation analysis.
- Exactly one primary worker may edit, stage, or commit repository files.
- Tests and documentation are completion gates.
- You review `develop` and manually promote it to `main`.

No Git worktrees are used. `features.parallel` is disabled in both Ralph configurations.

## Relationship to Huntley's playbook

The prompts track the high-value patterns in [`ghuntley/how-to-ralph-wiggum`](https://github.com/ghuntley/how-to-ralph-wiggum) (reviewed at `88d488a148af97e4a3f22b11b4c3598c79d6a577`): deterministic orientation, search-before-assumption, a concise operational `AGENTS.md`, acceptance-derived test backpressure, a scheduler-style primary context, immediate plan updates for discoveries, complete implementations without placeholders, investigation of unrelated failures, and documentation that captures why.

Deliberate safety differences remain: eight adaptive read-only subagents rather than hundreds of mutating agents; one writer and serialized builds; jailed Pi rather than skipped permissions; no worktrees; no autonomous specification edits; no active-ledger pruning; and no automatic push, tag, or promotion to `main`.

## Prerequisites

- Ralph Orchestrator with the native Pi backend
- `pi2` configured with the `@tintinweb/pi-subagents` extension
- Ollama provider/model access
- Bash, Git, Python 3.11+, curl, flock, and optionally ShellCheck
- A clean `develop` branch with at least one commit

The project tracks `.pi/subagents.json` with a maximum of eight simultaneous read-only subagents. Project agents in `.pi/agents/` intentionally expose no `bash`, `edit`, or `write` tools.

## Initial setup

1. Merge this boilerplate branch into `develop`.
2. Configure Ollama Cloud usage credentials:

   ```bash
   source scripts/update-ollama-cookies.sh
   ```

3. Confirm access and quota parsing:

   ```bash
   ./scripts/ollama-usage-guard.sh --check
   ```

4. Replace every placeholder in `AGENTS.md` with concise project-specific build, run, targeted-test, full-verification, and production-smoke commands.
5. Edit `docs/SPEC.md` and commit it separately:

   ```bash
   git add docs/SPEC.md
   git commit -m "spec: define the next release"
   ```

## Plan

Run the planning-only fresh-context loop:

```bash
./scripts/ralph-plan.sh
```

The planner may only modify `IMPLEMENTATION_PLAN.md` and the recovery scratchpad. A fresh invocation atomically replaces both with minimal cycle state before Ralph starts, so completed tasks are not carried into every future prompt. Previous plans remain available through Git history. `--resume` preserves the current draft byte-for-byte. The generated plan records:

- the spec path;
- the latest commit that changed the spec;
- the exact spec blob ID;
- the base commit;
- a requirement-by-requirement conformance matrix;
- an exhaustive interaction/API/CLI acceptance inventory;
- bounded tasks, dependencies, acceptance evidence, and documentation impact;
- a mandatory final documentation/specification audit that depends on every other task.

Inspect the plan before implementation. Every task in a newly accepted plan must be `pending`; inherited completed or in-progress tasks fail the planning gate. Every partial, missing, or ambiguous conformance row must map to a pending task. `scripts/check-plan-freshness.sh` prevents a stale plan or altered cycle base from running after the specification changes.

For a headless planning loop:

```bash
./scripts/ralph-plan.sh --no-tui
```

## Implement

Start the single-writer build loop:

```bash
./scripts/ralph-run.sh
```

Each iteration:

1. validates branch and plan freshness;
2. waits for Ollama quota when necessary;
3. selects one ready task;
4. fans out only read-only analysis;
5. implements and tests one task with one writer;
6. updates the plan and recovery scratchpad;
7. creates a Git checkpoint;
8. exits so the next task receives fresh context.

Only the final documentation and specification audit may produce `LOOP_COMPLETE`. `scripts/validate-implementation-plan.py` requires every conformance row to be verified, every task complete, the final audit to depend on every other task, and the plan status to be complete. The final gate also rejects unresolved open bugs before project verification.

Iteration count is not completion evidence. If final acceptance discovers a gap, the worker preserves the ledger, appends a uniquely numbered remediation task, adds it to the final audit dependencies, returns the audit to pending, and continues. The configured 1000-iteration and one-year runtime values are safety ceilings, not targets; reaching them or an external session limit leaves the cycle incomplete with a recovery handoff and never emits `LOOP_COMPLETE`.

## Maintain one bug

Portable canonical bug state lives in `open-bugs.md` and `closed-bugs.md`; GitHub and Forgejo issue URLs are optional manual references and may exist on either or both providers. No issue API, automatic sync, or credentials are used.

```bash
./scripts/bug-ledger.py validate
./scripts/bug-ledger.py list
./scripts/ralph-maintenance-plan.sh BUG-0001
./scripts/ralph-maintenance-run.sh
```

A maintenance cycle selects exactly one triaged ordinary defect. A fresh planning invocation replaces the prior maintenance plan and scratchpad with a minimal selected-bug skeleton; Git and `closed-bugs.md` retain prior evidence, while `--resume` preserves the active draft. All newly planned tasks must be `pending`. Planning commits the strictly parsed plan and then marks the defect `planned`; implementation marks it `in_progress` before product changes, and only an `in_progress` defect may close. Contract changes or product decisions are blocked and returned to the human specification workflow; maintenance never edits `docs/SPEC.md`. Ignored runtime state binds the selected ID and loop mode, while immutable `MAINTENANCE_PLAN.md` metadata binds the planning checkpoint parent, bug fingerprint, and committed spec. Ledger writes serialize and interrupted closure has a narrowly safe `recover` command. See `docs/BUG_WORKFLOW.md` for intake, ticket states, link/unlink commands, GitHub/Forgejo URL expectations, closure evidence, and recovery.

## Adaptive concurrency

Configured ceilings live in `factory.toml`:

```toml
[concurrency]
adaptive = true
planning_subagents = 8
research_subagents = 8
review_subagents = 8
implementation_advisors = 2
mutating_workers = 1
integration_workers = 1
min_model_requests = 1
max_model_requests = 8
```

These are ceilings, not targets. The coordinating agent starts with the smallest useful fan-out and increases only for independent read-only work. Source mutation and integration remain serialized.

## Quota waiting

Every iteration invokes:

```bash
./scripts/ollama-usage-guard.sh --wait
```

When session or weekly utilization reaches the configured threshold, the hook remains alive and polls until usage resets below it. Transient network errors are retried. Expired cookies stop with an actionable error rather than waiting forever.

Useful settings in `.ollama-usage-env`:

```bash
OLLAMA_THRESHOLD=80
OLLAMA_WAIT_INTERVAL_SECONDS=300
OLLAMA_WAIT_MAX_SECONDS=0  # unlimited
```

If the backend reaches quota during an already-running request, `scripts/ralph-run.sh` checks quota, waits, repairs runtime markers, and resumes with `--continue`.

## Stop and recover

Ralph has no true pause control. `Ctrl+C` aborts the active backend. Resume later with:

```bash
./scripts/ralph-recover.sh
```

Preview recovery without changes:

```bash
./scripts/ralph-recover.sh --dry-run
```

Planning and maintenance recovery use:

```bash
./scripts/ralph-recover.sh --mode planning
./scripts/ralph-recover.sh --mode maintenance-planning
./scripts/ralph-recover.sh --mode maintenance
```

Recovery never resets Git or starts a second writer. See `docs/OPERATIONS.md` for details.

## Verify

```bash
./scripts/verify-boilerplate.sh
```

The verifier checks shell syntax, ShellCheck when available, TOML/JSON configuration, read-only agent tools, single-writer settings, quota behavior, plan freshness, branch policy, removed product artifacts, and secret tracking.

Project implementation plans should add their own build, lint, test, and documentation commands to the final gate. Maintenance executes `[verification].maintenance_command` directly as argv and fails if its executable is absent. The generic boilerplate intentionally has no `scripts/verify-project.sh`; projects must supply it before running maintenance.

## Release

After Ralph reports completion, review `develop`. Release manually:

```bash
git switch main
git merge --no-ff develop
git tag vX.Y.Z
```

For the next release, update the same `docs/SPEC.md` in a dedicated commit, run a new planning loop, and execute a new implementation loop. Git retains prior specifications and plans.
