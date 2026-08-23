# Factory Operations

## Durable and volatile state

Durable, tracked state:

- `docs/FACTORY-LOOP-SPEC.md`: canonical specification for this boilerplate cycle (`docs/SPEC.md` remains the adopting-product placeholder, never planned against)
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

## Control-state authority (STATE-01)

The trusted control plane keeps exactly one mutable lifecycle file,
`.factory-state/factory-loop.json` (schema `factory-state/v1`), carrying only
the §11 fields: schema, repository identity, branch, campaign id, round
budget/counters, current phase, spec/plan/prompt-set digests, phase base
commit, selected task id, attempt counter, monotonic phase/attempt start
markers, and a trusted `last_outcome` enum. It is the only mutable lifecycle
file; the append-only `.factory-state/state-digest-ledger.jsonl` records
evidence only and is never orchestration state. All writes are atomic,
no-follow, mode-0600, and ownership/mode/link-count/(dev, inode) checked via
`scripts/factory_state_io.py`; loading re-validates the recorded repository
identity against the canonical root descriptor and any expected campaign
binding, so a forged, moved, symlinked, oversized, wrong-owner, or wrong-mode
file fails closed. The §11 transition table is enforced edge for edge,
campaign-scoped bindings are write-once, round/attempt counters are
monotonic, and a terminal phase accepts no further transition. `plan_digest`
(Task 19 S8) is the SHA-256 of the exact bytes of the committed
`factory-plan/v1` plan document at the bound `phase_base_commit`; it binds only
on the `planning -> implementation` edge and is write-once until the next
round's binding, so a round's plan can never silently change. A zeroed epoch
monotonic marker is rejected as tamper, an active attempt can never precede
the phase that owns it, an attempt marker is zero whenever no attempt is
active, and a persisted `last_outcome` must be a §13 outcome of its owning
phase (S3/S9). The state-file and private-directory owner checks compare
real `stat` metadata against an internal expected owner UID (default: the
current user), so the exact owner-rejection branch is always exercisable
with real stat metadata and a wrong expected UID, with no `chown` required.

Trusted control-plane operations only (each prints one machine-readable
outcome; `ROOT` defaults to the canonical repository):

```bash
.factory/loop/state.py --root ROOT init --campaign-id C --rounds N \
  --base-commit H --spec-digest S --plan-digest P --audit-digest A \
  --role-digest ROLE=HEX
.factory/loop/state.py --root ROOT show|digest|recover
.factory/loop/state.py --root ROOT advance OUTCOME \
  [--plan-digest P --base-commit H]
.factory/loop/state.py --root ROOT begin-attempt TASK_ID
.factory/loop/state.py --root ROOT record-retry OUTCOME
.factory/loop/state.py --root ROOT record-phase-digest TAG
.factory/loop/state.py --root ROOT verify-phase-digest TAG
```

`init` (Task 19 S1) runs deterministic crash-window recovery first, refuses a
prior campaign binding already recorded in the digest ledger, then publishes
atomically with no-replace semantics — it can never clobber existing state or
a raced pathname. `recover` (Task 19 S2) restores the single last validated
state from a torn write or removes validated orphaned temporaries/quarantines
of the atomic writer; it fails closed on ambiguous or foreign artifacts, only
deletes exact mode-0600 same-UID single-link regular markers, and preserves
unknown files untouched. Recovery reports `clean` *only* when the private
directory is truly absent — an existing symlinked, filed, wrong-mode, or
foreign-owned directory fails closed instead of being reported clean — and a
distinct `existing-empty` outcome when the directory is present and completely
empty (never confused with a truly absent directory). When a
digest ledger exists, a quarantined state is restored only when its digest
matches the *latest* recorded ledger entry (a state tampered after it was
recorded, or one matching only an earlier/superseded entry, fails closed with
the quarantine preserved; a present-but-zero-byte ledger is ambiguous torn
evidence and likewise blocks the restore), and after linking the
quarantine into the canonical name the canonical state is re-validated in
place *before* the quarantine is deleted and its inode is checked against the
quarantine so a substitution fails closed. The owner-tamper probe (S7) never
skips: `owner_tamper_gate` performs a real `chown(2)` and declares whether
the owner
check is exercisable, so coverage is genuinely exercised when possible and
honestly declared unavailable (with a fail-closed reason) when not.

Recovery is deterministic: the harness records the state digest before every
untrusted phase and reopens/revalidates the file after it, so any same-user
mutation of content, mode, owner, link-count, or pathname identity not
produced by a trusted transition fails closed. Resume by reloading
`.factory-state/factory-loop.json` (phase never moves backward, counters are
monotonic); `init` refuses to overwrite existing state.

## Finite campaign authority (Task 9)

`.factory/loop/campaign.py` is the trusted standard-library orchestrator. It
holds one root-descriptor writer lock through planning, implementation,
verification, and audit; starts each role as a fresh confined process; and
performs all Git status, history, staging, and commit operations outside the
model sandbox through the pinned descriptor-anchored Git authority. Trusted
Git calls and gates have finite timeouts. Pre-existing dirty work, unexpected
renames/copies/gitlinks, unsafe path references, stale plan bases, and
ambiguous crash state fail closed rather than being reset or overwritten.

The implementation phase deterministically selects the first runnable task
from the canonical plan and binds the developer to that exact committed task
section. Auditor launches bind the deterministic objective selected from the
committed audit-objective registry. A newly completed task is accepted only
after the exact resulting committed plan and its repository-relative
verification references pass deterministic checks. Model prose, exit text,
and completion tokens are not lifecycle protocols.

Work exhaustion and external blockers proceed to verification and independent
audit instead of spinning. The finite campaign terminates as `success`,
`findings`, `blocked`, `failed`, `infrastructure_failure`, or `interrupted`;
every terminal, including an interrupted or infrastructure-failed audit, is
persisted before result publication and cannot be re-entered. The result and
per-phase contracts are
`.factory/schemas/factory-campaign-result-v1.schema.json` and
`.factory/schemas/factory-phase-result-v1.schema.json`.

```bash
.factory/loop/campaign.py --root ROOT run --campaign-id ID --rounds 5 \
  --branch boilerplate-develop
.factory/loop/campaign.py --root ROOT show
```

`--role-driver` and scenario/result-file options are deterministic hidden-suite
fixtures only; they are not production confinement or acceptance evidence.

## Root-descriptor lock and Git writer boundary (LOCK-01, GIT-01, PROC-01)

The writer boundary is an exclusive Linux `flock` on the *already-open
canonical Git top-level directory descriptor* itself — opened with
`O_DIRECTORY|O_NOFOLLOW|O_CLOEXEC` — implemented in the hidden
`.factory/loop/lock.py` (Task 5). There is no replaceable lock-file
pathname. A second concurrent launcher fails immediately
(`RootLockHeldError`): exactly one writer.

Acquisition is *mandatory* (review finding F2): the canonical repository
identity, the required branch, and the bound specification/plan
commitments (`SpecBinding`/`PlanBinding` from the committed plan front
matter) must all be supplied and are validated *while the lock is held*, so
an acquisition without the full binding set, or a moved, rebased, or
re-bound checkout, fails closed before any launch. Every Git read of the
holder is *bound to the locked descriptor's inode*: the pinned Git runs
with `-C /proc/self/fd/<anchor>` where the anchor is a freshly opened
descriptor resolved *through the lock descriptor itself*, and the canonical
pathname is re-verified against the locked (device, inode) before and after
each read — a rename/rebind of the canonical path can never redirect a
single read and pathname drift fails closed even when the reads stayed on
the locked inode.

The descriptor is close-on-exec and `RootLock.spawn_child` runs every
child with `close_fds`, a stripped environment, and always in a **new
process session**: an untrusted leaf inherits neither the lock descriptor
nor lock metadata and cannot signal or observe the holder through a shared
session. `pass_fds` entries that alias the root lock inode — the lock
descriptor itself, a `dup` of it (same open file description), or a
separately opened root descriptor — are rejected before exec (F8). Because
`flock` locks are bound to the open file description, a separately opened
root descriptor cannot unlock the holder. A bounded child `timeout`
terminates and reaps the child's *entire* new process group with a
**TERM → KILL** sequence: TERM to the group, then the *full* bounded grace
is always observed (the leader exiting on TERM is never taken as “the group
is gone” — a TERM-ignoring descendant that holds the stdout/stderr write
ends survives in the group), then KILL to the group *unconditionally*, then
a bounded group-gone verification and a reaped leader; the trailing pipe
collection is itself bounded and the pipe ends are force-closed when a
stubborn member held them open, so a bounded spawn can never hang the
holder and no live process-group member survives. It surfaces as
`RootLockTimeoutError`, and a nonzero child exit under `check=True` surfaces
as `RootLockCommandError` (F3/F9). Every lock/authority failure — including
the wrapped pinned-Git import/invocation (`GitBoundaryError`) and the
`flock` `OSError` — routes through the unified `RootLockError` exception
contract (F9).

The child environment has every `FACTORY_LOOP_LOCK_*` key removed by
prefix and every *legacy* `FACTORY_LOCK_*` key removed as well (F10), so a
leaf of the new loop sees neither the current metadata nor the metadata of
the previous lifecycle machinery.

Escaped-descendant detection snapshots the **full bounded** descendant
closure from the `/proc/<pid>/stat` parent table
(`capture_descendants`, F1): an over-bound closure fails closed rather than
silently truncating. Every captured PID records its **starttime and parent
PID** (`CapturedProcess`) from the same snapshot, so the captured scope is
reuse-safe: `live_scope` re-enumerates only the still-live members of a
captured scope whose recorded identity (start time) still matches the live
process, so a PID reused by an unrelated process is **excluded** and never
reported as a live descendant (Task 6 supervision consumes this surface, so
descendant accounting is never stale). The control plane's own trusted
ancestor chain is walked per-pid through `_self_ancestry` — following each
PID's own `/proc/<pid>/stat` parent, not a single `os.getppid()` — so a
multi-level trusted chain is fully trusted and an escaped double-fork or
`setsid` descendant that survives bounded termination, or any untrusted
process that retains the repository-root/lock inode handle, fails closed
(`detect_escaped_descendants`/`assert_no_escaped_descendants` →
`EscapedDescendantError`); recovery must not reacquire the writer boundary
while it raises.

All trusted state/branch Git calls (`state.live_branch`, lock bindings) use
the pinned absolute executable resolved by `.factory/loop/gitutil.py`
(`GIT_EXECUTABLE`). Git selection never consults a caller-controlled
`PATH` (F4): candidates are fixed absolute locations — `/usr/bin/git`,
`/bin/git`, the NixOS system profile, and immutable root-owned Nix-store
paths (pattern-validated, foreign-owned, and non-writable by the caller) —
and an environment with no valid candidate fails closed rather than
falling back to an unqualified `git`. **Non-root trust requirement:** the
boundary refuses to resolve as uid 0, because under root every candidate
path component is caller-owned and no candidate can be proven immutable
without weakening the ownership check — the trusted control plane must run
as an unprivileged user. Every trusted Git invocation also
strips the complete `GIT_CONFIG*` environment family — including
`GIT_CONFIG_PARAMETERS`, `GIT_CONFIG_COUNT`, and `GIT_CONFIG_KEY_*`/
`GIT_CONFIG_VALUE_*` (F5) — plus the object-store/index/work-tree
redirectors. The committed `scripts/git-commit-guard.sh` branch boundary is
preserved untouched.

## Fresh-context execution, invocation contract, and supervision (Task 6)

Every role attempt of the new loop runs in a **fresh process** behind the
existing secure wrapper (`scripts/pi2-secure-exec.py`, invoked never
reimplemented) in one-shot mode: a new process session/group, no resumed
session, no session storage shared with any previous loop identity, no
automatic memory injection, and only allowlisted prompt inputs. The hidden
control-plane module is `.factory/loop/launch.py`; the launch API and the
`LaunchResult`/`StreamResult` types are exported from the hidden
`.factory.loop` package surface. The operator CLI is reachable **only** as
`python -m factory.loop.launch` (through the external-prefix alias that puts
`factory` on `PYTHONPATH` resolving to the canonical `.factory/` directory)
and, when installed, the external-prefix launcher entry point; there is no
visible bare `scripts/` wrapper. `excerpt` derives the exact committed
`factory-plan/v1` task-section bytes and their SHA-256 digest; `launch` runs
one supervised attempt and prints the machine-readable `factory-launch-
result/v1` JSON (`.factory/schemas/factory-launch-result-v1.schema.json`),
which is validated before it is printed:

```bash
python -m factory.loop.launch launch --root ROOT --role ROLE \
  --model MODEL --provider PROVIDER --backend BACKEND \
  --role-prompt ROLE.md --prompt-set-digest HEX --policy AGENTS.md \
  --spec SPEC.md --plan PLAN.md --bound-commit SHA \
  --allowed-tools a,b --runtime-limit N --inactivity-limit M \
  [--task-id N --task-excerpt-digest HEX] [--audit-objective FILE]
python -m factory.loop.launch excerpt --plan PLAN.md --task-id N
```

**Exact invocation contract (§20).** The binding names the exact
model/provider, the static role-prompt digest, the campaign-bound
prompt-set digest, the deterministic audit-objective digest (auditor
only), the canonical workspace and bound commit, and (developer only) the
exact task id whose excerpt bytes are re-derived from the committed plan
and digest-matched — a substituted, paraphrased, or foreign task fails
closed before launch. The CLI re-derives every authoritative byte for the
backend, spec/plan, role prompt, policy, and wrapper through fd-anchored,
no-follow, size-bounded reads of the **committed blobs at the bound
commit** and accepts no operator-claimed or caller-supplied path, blob,
digest, or binding (F5); the wrapper and model backend run only from their
exact bound-commit bytes or an external trusted executable (F2). The child
executes with the canonical workspace as its working directory; its
environment is rebuilt from the documented `ENV_ALLOWLIST` plus the
`FACTORY_LOOP_LAUNCH_*` invocation fields — no credential, lock, Git
redirector, or session variable from the parent is inherited — and the
per-launch invariants (new session, stripped environment, no root-inode
descriptor) are re-verified against `/proc` (a crash before the read-back
window is recorded as `unverifiable-crashed`, never a stale handle).

**Supervision.** The supervisor snapshots the role's *own* live descendant
closure once (`capture_descendants`), pinning every PID to its `/proc`
starttime and parent identity (F6), installs itself as a child subreaper
before spawn (F7) so an escaped double-fork/`setsid` descendant can never
orphan, and scopes orphan/reap handling to children spawned after the
snapshot — **pre-existing children keep their exit status with their own
owner** (F6). A hard runtime limit and an inactivity limit bound every run;
bounded termination delivers TERM, INT, and HUP to the **full process
group**, observes a bounded grace, escalates to KILL of the whole group,
then verifies the group is gone and reaps the leader. Every group signal is
gated on the leader's pinned starttime identity (F4): the monitor and the
snapshot use a non-reaping `/proc` liveness check, so a leader that exits
while a pipe-holding descendant lives is never reaped early — the
process-group id is never freed before the identity check, and a reused
PID is never signaled or reaped (fail closed). Any exception,
`KeyboardInterrupt`, or TERM/INT/HUP received after spawn still takes the
bounded terminate-then-reap path with no survivor (F1/F3); the scoped
handlers are restored when the attempt ends. A crashed or interrupted
attempt leaves its dirty work intact and never overwrites it; an escaped
descendant that survives bounded termination fails closed
(`EscapedDescendantError`) for operator inspection. The only completion
signal is the machine-readable, schema-validated result: role, model/
provider, outcome, returncode, signal, reason, terminated-by signals,
bounded per-stream digest/tail captures, and snapshot/live counts — no
argv or environment ever appears in a result.

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

### Guard contract and schema reference (QUOTA-01, QUOTA-02)

The retained `scripts/ollama-usage-guard.sh` ``--check``/``--wait`` exit table
and §10 decision table are now enforced by the hidden standard-library guard
``.factory/loop/usage.py`` (with its fetch child ``.factory/loop/usage_fetch.py``)
run before every model invocation. Exit codes: 0 allowed, 1 quota threshold,
2 fatal (missing/expired cookies or unparseable settings), 3 transient. A
single check emits the machine-readable ``ollama-usage/v1`` status object
(schema ``.factory/schemas/ollama-usage-v1.schema.json``) with only the
redacted fields ``session_percent``, ``weekly_percent``,
``threshold_percent``, ``blocked``, and ``reset_hint``.

Credential hardening (QUOTA-02): the cookie reaches the fetch child only on
its private stdin pipe; child argv is fully structural and the child
environment is rebuilt from a documented allowlist — never inherited — so
``/proc/<pid>/cmdline`` and ``/proc/<pid>/environ`` carry no credential
token. The cookie source and ``.ollama-usage-env`` store are read with
``O_NOFOLLOW`` and must be a regular single-link current-user-owned file,
not group/other-writable, and bounded in size. Cookie buffers are zeroized,
no temporary files are created, every fetch child is reaped, and only
redacted status leaves the guard. Signals received while waiting terminate
the wait and the in-flight fetch child and exit ``128 + signum``. The
synthetic-secret proc probe is in ``tests/test-pi2-ollama-wrapper.sh``.

Loopback usage-settings transport is **diagnostics/test-only** (Task 7
review, obligations 9 and 14): the ordinary production launch CLI and API
expose no loopback opt-in and reject an ``http://`` settings URL naming
``127.0.0.1`` or ``localhost`` in the launch authority *before* any fetch.
The hermetic suite reaches the loopback transport only through a private
authority seam that additionally requires a synthetic confinement proof — a
diagnostics/test facility, never a production feature.

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

## Model workspace confinement (Task 8)

Every planner, developer, tester, and auditor backend runs behind the staged
exact-commit `.factory/loop/confine_launcher.py`. The launcher applies a real
Landlock ruleset before executing the secure wrapper. If the required Landlock
ABI or any rule cannot be installed, launch fails closed.

The model sees only its role's committed plan/spec/code/test inputs and exact
private HOME, scratch, prompt, session, and staged-executable paths. Shared
`/tmp`, `/proc`, `/run`, host configuration, `.git`, `.ralph`,
`.factory-state`, context summaries, migration archives, credential stores,
and hidden factory source/tests/prompts are not allowlisted. Every allowlist
component is checked with `lstat`; symlinks, external resolved targets, and
hardlink aliases of forbidden files fail closed. Git history and commit
operations belong to the trusted orchestrator, never the model process.

Developer writes are limited to existing product paths and the plan; planner
writes only the plan; tester writes only existing build/test artifact paths;
auditor is read-only. No role may create a new top-level workspace directory.
A production confinement proof binds the exact commit, workspace, provider,
rule-spec digest, guard-source digests, and every credential channel actually
consumed. Synthetic proofs are hidden test seams and are never acceptance
evidence. The machine contract is
`.factory/schemas/factory-confinement-v1.schema.json`.

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
