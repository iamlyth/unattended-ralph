# Factory Operations

## Durable and volatile state

Durable, tracked state:

- `docs/SPEC.md`: approved requirements
- `.factory/artifacts/implementation-plan.md`: task status and verification evidence
- `.factory/bugs/open.md` / `.factory/bugs/closed.md`: portable canonical defect state
- `.factory/artifacts/maintenance-plan.md`: one selected bug, fingerprint, tasks, and evidence
- `.ralph/agent/scratchpad.md`: concise crash handoff
- source, tests, README, and operational documentation
- `.factory/config.toml`, Ralph configs, prompts, and project subagent definitions

Volatile, ignored state:

- event streams and pointer files under `.ralph/`
- loop locks, diagnostics, API state, task/memory stores, and TUI exports
- Pi transcripts and scheduled-agent state
- `.factory-lock`, `.bug-ledger.lock`, and `.factory-state/` selection/loop-mode markers
- `.ollama-usage-env`

Git checkpoints make the plan, scratchpad, and implementation recoverable. Event/task files improve same-disk recovery but are not treated as portable project history.

## Branch policy

The autonomous lifecycle runs only on the configured development branch. `main` is protected by policy and never modified by the factory. `scripts/branch-guard.sh` also rejects multiple Git worktrees.

A boilerplate experiment on a `factory/*` branch requires the explicit temporary override:

```bash
FACTORY_ALLOW_TRIAL_BRANCH=1 ./scripts/ralph-plan.sh
```

Do not carry this override into normal development.

## Multi-round campaign

Campaigns are unattended and headless by default; use `--tui` only for an
attended diagnostic display.

```bash
./scripts/ralph-campaign.sh --rounds 3
```

Each mandatory round starts a fresh specification plan at a new Git base, runs
single-writer implementation and the configured campaign verifier, then starts
an independent production-evidence audit. State is persisted atomically in
`.factory-state/ralph-campaign.json`; prior plans and audits remain in Git.
The lifecycle lock is an exclusive Linux `flock` on the already-open canonical
repository-root directory itself; there is no replaceable lock-file authority.
The trusted supervisor retains that dynamic descriptor, while Ralph/Pi, hooks,
gates, verifiers, runner/evidence commands, tests, and product leaves receive
neither a root descriptor nor lock metadata. A separately opened root FD cannot
unlock the supervisor's open-file description. Safe legacy lock files are
acquired, quarantined, revalidated, and removed once; busy or ambiguous
migration state stops the lifecycle. After interruption, confirm no child
Ralph process is alive and resume the exact phase with matching options:

```bash
./scripts/ralph-campaign.sh --rounds 3 --resume
```

If a checkout was stopped under the previous lock-file authority, run the
one-time migration before resuming. The helper holds the stable factory lock,
strictly validates the campaign and current committed verifier blob, preserves
any legacy recovery counters, and creates cycle-bound supervision and migration
markers without changing the campaign JSON. Its deterministic partial-write
recovery may complete an interrupted first invocation, while an already
completed migration is rejected:

```bash
./scripts/ralph-supervision-migrate.py --mode implementation \
  --expected-campaign-sha256 <digest-of-the-saved-campaign-json>
```

Only a later explicitly authorized operator action may resume that saved state
with `./scripts/ralph-campaign.sh --rounds N --resume`; resume validates the
exact migration marker and atomically promotes the saved legacy verifier digest
before any leaf launch, so later interruptions remain resumable.

Ralph 2.10.1 misclassifies its five-second post-`ralph emit` SIGTERM as a
failed iteration. The Pi2 wrapper explicitly loads a tool-call extension that
routes only a direct final emit through `scripts/pi-cli-shims/ralph`; the shim
preserves the real command's status and stderr while changing its exact
acknowledgement. Arbitrary identical output remains fail-closed. The bounded
no-follow prompt bridge and wrapper retain exec-style signal propagation. Remove
this compatibility path only after the pinned Ralph integration probe passes
without it.

Use `--restart` only to replace a terminal saved campaign. Lifecycle tokens in a
scratchpad or `ralph emit` topic/payload are rejected before checkpointing; only
the exact standalone final model-output line requests completion. Ordinary
scratchpad-only updates remain uncommitted in the worktree for recovery. A
strict final-handoff checkpoint may commit only that file once, then the final
gate attests a clean unchanged HEAD with no later tracked commit. A
current-attempt `loop_stale` result may receive at most two recoveries.
Completion rejection and combined no-progress ceilings are also persisted, so
restarting a child or resuming the campaign cannot reset them. Quota handling
stays in leaf launchers. Any other nonzero leaf or gate result stops immediately
with campaign state active at the same phase; the campaign never unlinks its
locked pathname or retries an arbitrary failure. Corrupt history/state, dirty
boundaries, stale Git bindings, exhausted ceilings, verifier changes, and
final-round findings all remain resumable blockers rather than skipped work.
`.factory/environment.toml` declares tools and runners
without endpoints or credentials. Campaign verification runs every declared
runner and validates exact-commit evidence:

```bash
./scripts/run-factory-runners.py
./scripts/check-factory-runner-evidence.py
```

Runner evidence is signed by a root-owned signer on the disposable runner VM:
the unprivileged forced-command endpoint never signs, and after the exact
archive/tree/environment/verifier/probes all pass, the root signer re-validates
every manifest field (clean pass only, supported capabilities, bound digests,
no caller-supplied signer identity), rebuilds the canonical signed manifest
itself, and returns the detached signature plus aggregate signer metadata.
`.factory/signer-trust.json` carries only public keys; the private signing key
is root-owned mode 0600 on the runner and never leaves it. The detached
signature (`manifest.sig`) and aggregate signer metadata are validated with
`ssh-keygen -Y verify`; rotation is fail-closed (removed keys are rejected).
Until a signer is provisioned (`enabled = true`), unsigned legacy/local
manifests are rejected and runner-evidenced capabilities stay unevidenced.

Required capabilities in `.factory/config.toml` must be both declared and
evidenced before an independent audit may pass. Runner provisioning remains
outside the repository.

## Quota states

### Allowed

The session and weekly percentages are below `OLLAMA_THRESHOLD`; Ralph starts the next iteration.

### Waiting

At or above the threshold, the guard sleeps for `OLLAMA_WAIT_INTERVAL_SECONDS` and checks again. A zero `OLLAMA_WAIT_MAX_SECONDS` means unlimited waiting. SIGINT/SIGTERM still stop the process.

### Transient failure

Network and server failures are retried in wait mode. Single-check mode returns status 3 so supervisors can distinguish them from quota and credential failures.

### Fatal failure

Missing/expired cookies or an unparseable settings page return status 2 and require operator action:

```bash
source scripts/update-ollama-cookies.sh
```

## Clean stop

In TUI or foreground mode, press `Ctrl+C`. Ralph aborts the backend and leaves durable state for recovery. Do not use `kill -9` unless the process cannot terminate normally.

For a headless process, read `.ralph/loop.lock` and send SIGINT to its PID from the host.

## Recovery

1. Confirm no Ralph process is alive.
2. Run:

   ```bash
   ./scripts/ralph-recover.sh --dry-run
   ```

3. Check the inferred loop ID and event stream.
4. Resume:

   ```bash
   ./scripts/ralph-recover.sh
   ```

The script restores a missing tracked scratchpad, removes only a stale lock, recognizes timestamped and fallback event streams, reconstructs pointer files, and starts `ralph-run.sh --resume`. New launches persist `.factory-state/loop-mode`; recovery rejects a requested mode that differs. Legacy runs without the marker retain inference behavior with a warning.

If unfinished runtime tasks belong to multiple loop IDs, recovery refuses to guess; pass the intended ID explicitly:

```bash
./scripts/ralph-recover.sh --loop-id primary-YYYYMMDD-HHMMSS
```

Campaign audit recovery uses:

```bash
./scripts/ralph-recover.sh --mode campaign-audit
```

After leaf recovery completes, resume the campaign command so it records the
audit result and continues the next configured phase.

## Bug maintenance

GitHub and Forgejo issues are optional manual references; a bug may link either or both with `bug-ledger.py link|unlink`. Never store PATs in the repo or embed credentials/query tokens in URLs. Validate and inspect canonical state with `scripts/bug-ledger.py validate|list|show|fingerprint` and maintain it with `add`, `link`, `unlink`, `set-status`, `close`, and safe interrupted-close `recover`. States are `open`, `triaged`, `planned`, `in_progress`, `blocked`, and `closed`; close requires `in_progress`.

An ordinary defect restores the approved contract and can use:

```bash
./scripts/ralph-maintenance-plan.sh BUG-0001
./scripts/ralph-maintenance-run.sh
```

Triage the bug before planning. A fresh maintenance-planning command atomically seeds a minimal selected-bug plan and scratchpad; it never copies the prior cycle, and the planning gate accepts only pending tasks. Use `--resume` to preserve an interrupted draft instead of starting over. Successful planning marks it `planned`; the first implementation task marks it `in_progress`. If `contract_change` is true, expected behavior requires a product decision, or the spec would need editing, block maintenance and use the human specification workflow. One cycle handles one bug. Recovery uses `--mode maintenance-planning` or `--mode maintenance`; both preserve quota waiting, the factory lock, clean-tree policy, loop-mode binding, and checkpoints. Maintenance completion also requires the executable argv configured as `[verification].maintenance_command`; this boilerplate intentionally leaves `scripts/verify-project.sh` for each project to provide. Full details are in `docs/BUG_WORKFLOW.md`.

## Specification changes

Never edit the specification during implementation. `check-plan-freshness.sh` compares both the latest spec commit and the exact Git blob against plan metadata. If they differ:

1. stop the implementation loop;
2. commit the revised `docs/SPEC.md`;
3. run `./scripts/ralph-plan.sh`; this atomically seeds a minimal plan and scratchpad and leaves the completed plan only in Git history;
4. inspect the replacement plan and confirm it contains only current pending gaps, not completed historical tasks;
5. start a new implementation loop.

## Documentation gate

Every implementation plan ends with **Final documentation and specification audit**. Read-only reviewers compare source, tests, configuration, README, operations, and the specification. The sole writer corrects documentation and runs final verification. `LOOP_COMPLETE` is forbidden until this gate passes.

## Troubleshooting

- **`expected <development-branch>`**: merge/switch to the configured development branch; use the trial override only for this boilerplate branch.
- **`exactly one working tree`**: remove stale worktrees and run `git worktree prune`.
- **`plan is unplanned`**: run the planning loop.
- **`fresh implementation plan may contain only pending tasks`**: remove carried-over lifecycle tasks; inspect current code and plan only remaining spec gaps.
- **missing planning base/draft on resume**: recover the interrupted lifecycle markers; never restore an old completed plan as the active draft.
- **`specification changed after planning`**: commit the spec and replan.
- **`another factory process holds .factory-lock`**: confirm the existing planner/worker is stopped before deleting a stale `.factory-lock`.
- **quota wait appears idle**: the guard prints each usage poll; lower the polling interval temporarily for diagnostics.
- **cookie expired**: refresh with `source scripts/update-ollama-cookies.sh`.
