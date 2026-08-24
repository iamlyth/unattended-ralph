# Factory Operations

## Durable and volatile state

Durable, tracked state:

- `docs/FACTORY-LOOP-SPEC.md`: canonical specification for this boilerplate cycle (`docs/SPEC.md` remains the adopting-product placeholder, never planned against)
- `.factory/artifacts/implementation-plan.md`: the canonical plan and sole task ledger
- `.factory/bugs/open.md` / `.factory/bugs/closed.md`: portable canonical defect state
- `.ralph/agent/scratchpad.md`: legacy crash handoff (not injected into model context)
- source, tests, README, and operational documentation
- `.factory/config.toml`, static role prompts (`.factory/prompts/planner.md`, `developer.md`, `tester.md`, `auditor.md`), frozen legacy Ralph configs, and project subagent definitions

Volatile, ignored state:

- `.factory-state/`: the single mutable control-state file
  `.factory-state/factory-loop.json` plus append-only evidence (digest
  ledger, receipts, runner evidence); created mode 0700
- legacy event streams and pointer files under `.ralph/` (the new loop creates none)
- Pi transcripts and scheduled-agent state
- legacy `.factory-lock` only during one-time migration and `.bug-ledger.lock`
- `.ollama-usage-env` (legacy workspace store; the operator store is external)

Git checkpoints make the plan and implementation recoverable. The new loop
creates no runtime task queue, event stream, memory store, or second mutable
control-state authority.

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

## Evidence-smoke lane (Task 22)

The evidence-smoke lane instantiates the live campaign machinery for exactly
one full planning -> implementation -> verification -> audit round against
real production paths — `.factory/loop/campaign.py run --evidence-smoke` on
the canonical plan and `.factory-state/` — using the designated committed
smoke seam (`.factory/smoke/evidence_smoke_driver.py`) as a deterministic
synthetic role process. No external model, credential, cookie, runner, or
human is ever invoked. The seam is explicitly *private source methodology*
evidence only: never a real model or human outcome, never installed-tier
evidence, never GIT-01 acceptance evidence, and never acceptance-tier
evidence.

```bash
python3 .factory/smoke/evidence_smoke.py --root ROOT run \
  --branch boilerplate-develop --expect-commit SHA40
```

`--expect-commit` is mandatory: the round only runs against the exact bound
commit and fails closed on any other HEAD. The operator command fails closed
unless the worktree is clean at the exact bound branch/commit, the
designated seam is the exact committed blob, the campaign id carries the
private seam label (`evidence-smoke-`), and no `.factory-state`
`factory-loop.json`, recovery orphan (`.factory-loop.json.<32hex>`), state
digest ledger, or structured result/evidence artifact exists yet (every
smoke output is no-replace). The campaign CLI re-checks the same boundary
(`--evidence-smoke` refuses an arbitrary role candidate, a non-`synthetic`
provider, a multi-round run, a foreign seam label, or a missing exact bound
commit). Before the run, every pre-existing `.factory-state` entry is
snapshotted (digest, mode, mtime); afterwards the same entries must be
byte-identical, so foreign runtime bytes are never deleted, quarantined, or
modified. The round terminates `success` with the exact one-round phase
history, instantiates `.factory-state/factory-loop.json` (write-once
bindings, monotonic counters, terminal `success`) and the append-only digest
ledger, and makes exactly two orchestrator commits: the canonical
byte-bound planner revision (Task 22 stays `pending` in the planner output;
per spec §6.2 only the developer role may mark the selected task
`complete`) and the developer task-complete revision carrying one bounded
tracked evidence artifact under `.factory/artifacts/`
(`campaign-smoke-evidence.json`, schema `factory-smoke-evidence/v1`). The
round leaves the final audit task pending — the round proves one full phase
cycle, not acceptance. Tester/auditor results are exact
`factory-phase-result/v1` `pass` documents; no row is elevated and no
receipt is minted. The role driver and both gate modes execute only from
their bound committed descriptors through the pinned interpreter
(`/proc/self/fd/<fd>` with `pass_fds`; no pathname exec, no `PATH`-resolved
shebang), the post-round documentation gates run through the same
committed-descriptor authority under a sanitized allowlist environment with
a scrubbed pinned `PATH`, and the campaign child is supervised as a new
session with a bounded TERM -> grace -> KILL of the whole process group and
marker-based survivor detection. State digest verification,
`scripts/check-plan-freshness.sh`, and `scripts/check-generic-leakage.sh`
pass.

## Findings flow (Task 10)

Tester and auditor outcomes never create runtime tasks. When the trusted
classifier derives `findings` or `blocked`, the orchestrator publishes one
write-once `factory-findings-receipt/v1` under the private `.factory-state/`
evidence namespace. The bounded, no-follow receipt binds the campaign, source
round, phase and phase tag, exact phase-base commit, exact result digest,
trusted gate/capability outcomes, and the state digest-ledger tag. The exact
phase-result bytes are separately preserved and revalidated; receipt outcome,
findings, and blockers must match those bytes. Publication is idempotent only
for byte-identical crash recovery, while any conflicting pre-existing artifact
fails closed. A pass cannot mint findings, and malformed, missing, stale,
foreign, synthetic, torn, tampered, or unreachable-commit evidence fails
closed.

At the next round only, the fresh planner receives a canonical
`factory-findings/v1` payload whose bytes and digest are part of its launch
binding. External blockers remain structured findings. The planner must revise
the canonical implementation plan; developers receive no receipt, tester or
auditor result, prior prompt, or findings payload. Selection continues to read
only the canonical plan and minimal control state, so evidence can never become
a competing task authority. Production tester and auditor launches receive a
real Landlock write grant for only their pre-created exact result file; sibling
`.factory-state` access remains denied. Contracts are
`.factory/schemas/factory-findings-receipt-v1.schema.json` and
`.factory/schemas/factory-findings-v1.schema.json`.

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

The autonomous lifecycle runs only on the configured development branch. `main` is protected by policy and never modified by the factory. `scripts/branch-guard.sh` also rejects multiple Git worktrees. The campaign CLI requires the exact `--branch`; there is no trial-branch escape in the new loop.

## Multi-round campaign

Campaigns are unattended and headless; there is no TUI.

```bash
python3 .factory/loop/campaign.py --root "$PWD" run \
  --campaign-id primary-YYYYMMDD-HHMMSS --rounds 3 \
  --branch boilerplate-develop --provider ollama --model <model> \
  --backend <absolute-model-backend>
python3 .factory/loop/campaign.py --root "$PWD" show
```

Each mandatory round starts a fresh specification plan at a new Git base, runs
single-writer implementation and the configured campaign verifier, then starts
an independent production-evidence audit. State is persisted atomically in
the single `.factory-state/factory-loop.json` file; prior plans and audits
remain in Git. The lifecycle lock is an exclusive Linux `flock` on the
already-open canonical repository-root directory itself; there is no
replaceable lock-file authority. The trusted orchestrator retains that
dynamic descriptor, while role processes, hooks, gates, verifiers,
runner/evidence commands, tests, and product leaves receive neither a root
descriptor nor lock metadata. A separately opened root FD cannot unlock the
orchestrator's open-file description. Safe legacy lock files are acquired,
quarantined, revalidated, and removed once; busy or ambiguous migration state
stops the lifecycle. After interruption, confirm no role process is alive and
re-run the same `run` command; there is no `--resume`/`--restart` flag.

The legacy `scripts/ralph-campaign.sh` (and its `.factory-state/ralph-campaign.json`
and `ralph-supervision-migrate.py` machinery) is a frozen deprecated surface
kept only for history; the new loop never depends on it. The legacy Ralph
`ralph emit`/completion-token protocol is not reimplemented: completion is
derived from plan state, Git state, exit status, and deterministic gates
(§13), never from a model output line. Ordinary scratchpad-only updates remain
uncommitted in the worktree for recovery. A strict final-handoff checkpoint may
commit only that file once, then the final gate attests a clean unchanged HEAD
with no later tracked commit. Any nonzero leaf or gate result stops immediately
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

## Evidence and verifier authority (Task 12)

Before the tester starts, the trusted campaign opens the configured committed
verifier with `O_NOFOLLOW`, binds its owner/mode/link/inode and exact blob, and
retains that descriptor. Immediately before the gate it revalidates descriptor,
pathname, commit, and bytes, then executes through `/proc/self/fd/<n>`; a
pathname swap cannot change the executed verifier. The child inherits exactly
that one read-only verifier descriptor — proven from inside the child through
`/proc/self/fdinfo` (read-only access mode, zero position, failed write probe)
and an exact fd-table check — and never the root-lock descriptor or any other
holder alias. Trusted Git and `ssh-keygen` calls use pinned immutable absolute
executables, sanitized Git environments, and finite timeouts; the complete-mode
campaign-audit validator runs the runner-evidence checker under the same
sanitized, bounded invocation and fails closed cleanly (no traceback) if that
check times out.

Machine receipts and adjacent stdout/stderr are bounded, no-follow,
owner/mode/link/inode-checked, atomically published without same-tag
replacement, and tied to protected coordinator state. Untrusted roles do not
receive the coordinator nonce and cannot mint receipts from environment claims.
An evidence line uses the exact grammar `PASS|FAIL|BLOCKED <shell-quoted argv>
[receipt: path]` or `[manifest: path]`; its command must equal the receipt argv.
PASS requires recorded exit 0, and any genuine BLOCKED evidence forces a
`findings` audit result.

Machine evidence never elevates tiers: ordinary receipts are capped at
`installed`, signed runner manifests at `real_system`, and no machine artifact
can claim `human`. Missing hardware, compositor, target consumer, signer, or
human review remains a finding. Receipts, manifests, and evidence tiers verify
claims but never select implementation tasks.

## Installed-tier smoke suite (Task 20)

Installed-tier evidence proves the *installed harness itself*: a clean
exact-commit copy staged into test-owned external and hidden prefixes, with
the production control-plane CLIs executed from that copy. It never claims
`real_system`/`human`/product behavior and never calls an external model,
runner, or human.

Run the installed smoke suite exactly like the other hidden suites (it is
also part of `scripts/verify-boilerplate.sh`):

```bash
./.factory/tests/test-factory-installed.sh
```

The trusted installer stages committed harness content (`.factory/` and
`.pi/` at the bound commit) plus the shared authority
(`scripts/factory_state_io.py`) and the operator entrypoints (the
`.factory/bin/factory-launch` external-prefix launcher and the
`scripts/machine-receipt.py` receipt wrapper) into a fresh absolute prefix
outside the repository, preserving executable modes and proving
blob-exactness through the pinned Git boundary (batched `cat-file` with a
hard-capped fair single-event-loop capture and `hash-object --no-filters`
re-proof).  The prefix is descriptor-anchored: created and opened with
`O_DIRECTORY|O_NOFOLLOW|O_CLOEXEC`, identity-recorded from the fd before
the private-0700 mode is pinned via `fchmod`, and every directory/file is
staged through `openat` dirfd chains with `O_NOFOLLOW` (no pathname
writes), revalidating the anchor identity and containment before each
stage.  Every installed directory is private mode 0700, the pending
Task-20-era additions are staged only from the exact reviewer allowlist
(never a stray or secret-named `.factory` file — a secret-shaped path is
rejected for committed content too), a declared authority outside
`.factory`/`.pi`/`scripts` fails closed, every malformed batched-Git
transcript fails closed as an `InstallerError`, the manifest is written
outside the repository without replacing an existing file, and a failed
install rolls back the created prefix identity-safely (a prefix path
swapped to a symlink/bind-mount into the repository fails closed before
any repository write):

```bash
python3 .factory/loop/installer.py install --root "$PWD" \
  --commit "$(git rev-parse HEAD)" --prefix "$TMPDIR/prefix" \
  --manifest-out "$TMPDIR/manifest.json"
```

The installed physical-file inventory is captured by the *installed*
footprint authority (`--installed-inventory`) and asserts the exact
manifest + shared + entrypoint set — never a product or foreign namespace:

```bash
PYTHONPATH="$TMPDIR/alias" python3 -m factory.loop.footprint \
  --installed-inventory "$TMPDIR/prefix" --manifest "$TMPDIR/manifest.json"
```

Every installed-tier gate (launch help and excerpt, the external-prefix
launcher entry point, `factory-campaign`, the parser/selector/state CLIs,
the receipt wrapper, and the installed inventory) runs from the installed
copy under a sanitized environment and is minted as an exact-commit machine
receipt bound to the audit coordinator in the fixture authority
(`.factory-state/audit-coordinator.json` with the exact bound commit), so
`scripts/check-audit-receipts.py` exits 0 and a gate that exits nonzero or
prints a skip marker is never PASS.  Module-form gates carry an
installed-root attestation (the receipt argv/stdout binds the resolved
installed module root; a source-tree invocation resolves a different root
and can never mint an equivalent receipt), the `factory-launch` entry point
forwards INT/TERM/HUP/QUIT to the child and bounded-waits/reaps it before
removing its operator alias (a delayed-termination child keeps the alias
until it exits, the child's actual or 128+signal status is preserved, and
a signal-ignoring child is KILLed within the bounded grace), and a
clean-commit simulation proves the post-commit install never double-stages
an entrypoint.  The suite never mutates the live `.factory-state/`: the
driver snapshots every foreign runtime file's digest, mode, and mtime plus
the tracked/untracked Git state and the ignored/bytecode inventory before
and after and proves byte-for-byte preservation.

Task 20 is **complete at exact commit `6b9c626`** as installed-harness
mechanics with fixture-authority receipts only: the installed suite builds
and proves the installed-tier machinery against the fixture authority, and
the live `.factory-state/` foreign evidence is never touched, replaced, or
relabeled.  The **live** installed-functional evidence — the fresh
generic-namespace evidence, the coordinator receipts at the audit base, and
the check-installed acceptance — is owned by pending Task 23 after Task 22
runs the live campaign; Task 20 itself never stages live evidence.

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

### Operator credential store (Task 15 migration)

The operator credential store lives **outside the model workspace**: the
guard resolves ``$OLLAMA_USAGE_ENV_FILE`` when the operator sets it,
otherwise ``$XDG_CONFIG_HOME/unattended-ralph/ollama-usage-env`` (or
``~/.config/unattended-ralph/ollama-usage-env``).  The legacy workspace store
``<repository root>/.ollama-usage-env`` is **detected with metadata only**
and is never read, sourced, or parsed as a credential authority; the
operator must migrate it to the external store (the migration reports its
presence in the flat evidence report ``.factory-state/migration.json``).  This also applies to the
retained shell guard and ``scripts/update-ollama-cookies.sh``, which warn on
the legacy store without reading it.

### Allowed

The session and weekly percentages are below `OLLAMA_THRESHOLD`; the model invocation proceeds.

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

## Credential and output boundary (Task 11)

Before any model or deterministic gate starts, the trusted control plane binds
`scripts/credential-guard.py` to its exact committed bytes and compiles its
redaction API. A missing, changed, symlinked, oversized, writable, or invalid
guard fails closed. The same guard is the sole masking authority for Pi SDK
`tool_call` input, `tool_result` patches, model stdout/stderr tails, and gate or
acceptance-command details. Raw stream digests remain evidence-only; every
retained text channel is bounded and redacted, including private-key blocks
split across capture eviction boundaries. Redaction failure publishes only
`[REDACTION FAILED]`.

Model children and gates receive rebuilt allowlisted environments. Credential-
shaped variables, Git redirectors, inherited lock metadata, authentication
paths, and parent-only configuration are stripped. Ollama credential material
continues through the private bounded usage channel and never through model
argv/environment/output. Synthetic secret fixtures test these boundaries; no
real credential value is test input or evidence.

The model-facing Git shim selects only fixed absolute trusted Git candidates
(or an immutable validated Nix-store executable), never caller `PATH`, and
refuses bypass flags and root execution. External backends are accepted only
when their exact immutable executable/source paths are bound into a real
Landlock confinement proof; the synthetic-proof seam cannot authorize them.

## Ralph migration and freeze (Task 15)

The tracked `.factory/ralph-freeze` marker blocks every new legacy Ralph
launcher before it acquires the factory lock. `--help` remains available and
legacy recovery reports deprecation, but the new Python campaign does not call
these scripts. The marker is trusted only as a regular non-symlink file: a
present marker that is a symlink, FIFO, socket, device, or directory fails
closed (the launchers and the hidden migration authority both refuse), and a
missing marker preserves the legacy not-frozen semantics.

A documented operator-only override exists solely for bounded legacy recovery;
it is never part of normal orchestration. Completing **full** legacy recovery
(resuming an already in-flight legacy cycle through the frozen launcher that
`scripts/ralph-recover.sh` execs) requires the exact override value — set
`FACTORY_RALPH_FREEZE_OVERRIDE=1` into the recovery environment. The value must
be exactly `1`; any other value (unset, `0`, `2`, `yes`) leaves the launchers
frozen. `ralph-recover.sh` itself is not a new launch and runs without the
override to repair/validate markers (`--prepare-only`/`--dry-run`).

The hidden migration authority is metadata-only:

```bash
python3 .factory/loop/migration.py --root "$PWD" status
python3 .factory/loop/migration.py --root "$PWD" derive
# Explicit operator action after reviewing the report:
python3 .factory/loop/migration.py --root "$PWD" migrate \
  --campaign-id ID --rounds 5
```

It binds the canonical plan and HEAD, preserves dirty path metadata,
receipt/evidence metadata, and blocked facts, then initializes the single
`factory-state/v1` authority with no-replace semantics. It never opens `.ralph`
content, imports task/memory/event/completion state, reads the removed context
summary, or reads legacy credential bytes. The report lives only as the flat
evidence artifact `.factory-state/migration.json` inside the ignored
`.factory-state/` namespace (never a second mutable control authority).

### Migration blob bounds (Task 15)

The migration digests exactly the committed blobs a real launch would
consume, each under a hard per-kind cap: the specification, the plan, every
role prompt, and the audit-objectives registry are all capped at **1 MiB**
(matching the launch prompt-input limit).  Each blob is resolved to a strict
40-hex object ID through the pinned no-replace Git boundary and is exact-size
pre-checked with `git cat-file -s` *before any body byte is read*; an
oversized, non-hex, replace-ref, or size-mutating object fails closed and the
body read is never issued.  The bounded byte capture itself (`git_bytes_bounded`)
runs the pinned Git child in its own process group, drains stdout/stderr
fairly against one shared deadline (a child that floods one pipe cannot
deadlock the capture), and terminates/reaps the whole group (TERM, bounded
grace, KILL, leader reap) on a wedged or over-bound run, leaving no zombie.

### Accepted shell-level freeze race (Task 16)

The shell freeze gates in the deprecated `scripts/ralph-*` launchers are
best-effort presence checks: a same-uid local writer can delete the tracked
`.factory/ralph-freeze` marker between a shell's `[ -f ]` check and the launch
it guards, so a shell gate can never be a strong tamper boundary.  The hidden
migration authority (`is_ralph_frozen`) re-stats the marker no-follow and
fails closed on an unsafe marker, and the tracked marker itself is the
authoritative launch boundary of the new control plane.  This residual is
documented and **accepted for the Task 16 adversarial pass**, never silently
relied on.

## Clean stop

The fresh loop is headless: interrupt the campaign process (`Ctrl+C`/`SIGINT`
or `SIGTERM`). The bounded supervisor terminates and reaps the full model
process group, preserves dirty work, and leaves the single control-state file
resumable at the same phase. Do not use `kill -9` unless the process cannot
terminate normally.

## Recovery

Recovery is derived from Git, the canonical plan, the single
`factory-state/v1` file, and process liveness — never from model prose or
runtime ledgers. Confirm no role process is alive, then re-run the same
campaign command; `state.py recover` deterministically restores a torn write
or removes validated orphaned writer artifacts:

```bash
python3 .factory/loop/state.py --root "$PWD" show
python3 .factory/loop/state.py --root "$PWD" recover
python3 .factory/loop/campaign.py --root "$PWD" run \
  --campaign-id <same-id> --rounds <same> --branch <same> [same options]
```

A clean committed task resumes from the next deterministic task; an
`in_progress` task resumes from current code and Git diff in a fresh context.
On a clean tree with no campaign initialized, `show` honestly reports
`lifecycle marker is missing` (no lifecycle state) and `recover` reports
`clean`; there is nothing to resume until a campaign (or `state.py init`)
creates the state file.

An ambiguous live process, changed repository identity, changed branch, unsafe
file, stale specification binding, changed plan base, rewound counter, or
invalid state transition fails closed for human/operator inspection. Recovery
never resets Git and never starts a second writer.

The frozen legacy recovery path (`scripts/ralph-recover.sh`) exists only for
an already in-flight legacy cycle; it requires the operator-only
`FACTORY_RALPH_FREEZE_OVERRIDE=1` escape and is not a new launch.

## Bug maintenance

GitHub and Forgejo issues are optional manual references; a bug may link either or both with `bug-ledger.py link|unlink`. Never store PATs in the repo or embed credentials/query tokens in URLs. Validate and inspect canonical state with `scripts/bug-ledger.py validate|list|show|fingerprint` and maintain it with `add`, `link`, `unlink`, `set-status`, `close`, and safe interrupted-close `recover`. States are `open`, `triaged`, `planned`, `in_progress`, `blocked`, and `closed`; close requires `in_progress`.

The legacy maintenance loops (`scripts/ralph-maintenance-plan.sh` /
`scripts/ralph-maintenance-run.sh`) are frozen deprecated forwarders: the
fresh loop's complete role set is planner, developer, tester, and auditor, so
it has no maintenance role. Product defects are triaged by the human and enter
the canonical plan through a planning revision; `[verification].maintenance_command`
and `scripts/verify-project.sh` are project-supplied. Full details are in
`docs/BUG_WORKFLOW.md`.

## Specification changes

Never edit the specification during implementation. `check-plan-freshness.sh` compares both the latest spec commit and the exact Git blob against plan metadata. If they differ:

1. stop the campaign;
2. commit the revised canonical specification;
3. start a new campaign from a clean tree so the fresh planning phase replaces the plan and leaves the completed plan only in Git history;
4. inspect the replacement plan and confirm it contains only current pending gaps, not completed historical tasks;
5. start a new implementation phase.

## Documentation gate

Every implementation plan ends with **Final documentation and specification audit**. Read-only reviewers compare source, tests, configuration, README, operations, and the specification. The sole writer corrects documentation and runs final verification. Completion is forbidden until this gate passes.

## Troubleshooting

- **`expected <development-branch>`**: merge/switch to the configured development branch (`.factory/config.toml` `development_branch`).
- **`exactly one working tree`**: remove stale worktrees and run `git worktree prune`.
- **`plan is unplanned`**: start a fresh planning phase
  (`python3 .factory/loop/campaign.py --root "$PWD" run --campaign-id <id> --rounds <n> --branch <branch>`).
- **`fresh implementation plan may contain only pending tasks`**: remove carried-over lifecycle tasks; inspect current code and plan only remaining spec gaps.
- **missing plan draft on resume**: `state.py recover` restores a torn write or removes validated orphaned writer markers; never restore an old completed plan as the active draft.
- **`lifecycle marker is missing` from `state.py show`**: no campaign has initialized `.factory-state/factory-loop.json` on this tree; start or resume a campaign before expecting lifecycle state.
- **`specification changed after planning`**: commit the revised canonical specification and start a new campaign from a clean tree.
- **`factory-state/v1` tamper/transition error**: inspect `.factory-state/factory-loop.json` ownership/mode and the digest ledger; the state file is the single authority.
- **quota wait appears idle**: the usage guard prints each usage poll; lower the polling interval temporarily for diagnostics.
- **cookie expired**: refresh the external operator store (`$OLLAMA_USAGE_ENV_FILE`, else `$XDG_CONFIG_HOME/unattended-ralph/ollama-usage-env`) with `source scripts/update-ollama-cookies.sh`.
