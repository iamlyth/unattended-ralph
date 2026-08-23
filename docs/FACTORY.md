# Factory Boilerplate

This document covers the Ralph Software Factory development infrastructure
used to implement the product defined by the bound canonical specification. In
this boilerplate cycle the canonical specification is `docs/FACTORY-LOOP-SPEC.md`
(`.factory/config.toml` `[project].spec`); `docs/SPEC.md` remains the
adopting-product placeholder and is never planned against. It is not relevant to
end users — it documents the autonomous development loop, branch policy, quota
management, and recovery procedures.

The factory boilerplate is retained on the configured development branch only. End users
should refer to [README.md](../README.md) for product documentation.

## What this is

A reusable, single-writer implementation of Geoffrey Huntley's Ralph Wiggum
development technique using Ralph Orchestrator, jailed Pi, Ollama, adaptive
read-only subagents, Git checkpoints, quota waiting, and crash recovery.

The canonical bound specification (this cycle: `docs/FACTORY-LOOP-SPEC.md`, per
`.factory/config.toml` `[project].spec`; `docs/SPEC.md` stays the adopting-product
placeholder and is never planned against) is the source of truth.
`.factory/artifacts/implementation-plan.md`
tracks task status and verification evidence.

## Operating model

- `main` is the human-controlled release branch.
- The configured development branch (`.factory/config.toml` `development_branch`) is the autonomous implementation branch.
- One committed canonical specification is the source of truth; Git versions it. This cycle binds `docs/FACTORY-LOOP-SPEC.md` (`.factory/config.toml` `[project].spec`); the adopting product supplies its own `docs/SPEC.md` later.
- A planning-only Ralph loop creates `.factory/artifacts/implementation-plan.md` for the exact spec commit.
- Each implementation iteration selects one bounded task and starts with fresh model context.
- Pi subagents perform parallel read-only planning, research, review, security, and documentation analysis.
- Exactly one primary worker may edit, stage, or commit repository files.
- Tests and documentation are completion gates.
- You review the configured development branch and manually promote it to `main`.

No Git worktrees are used. `features.parallel` is disabled in both Ralph configurations.

## Relationship to Huntley's playbook

The prompts are periodically compared against [`ghuntley/how-to-ralph-wiggum`](https://github.com/ghuntley/how-to-ralph-wiggum) (reviewed at commit `88d488a148af97e4a3f22b11b4c3598c79d6a577`). This boilerplate adopts the playbook's highest-value context and backpressure patterns:

- deterministic orientation: study the specification, plan, concise `AGENTS.md`, source, tests, and shared patterns every iteration;
- **do not assume functionality is missing**—search and trace production behavior first;
- keep the primary context as scheduler and use parallel subagents as disposable read-only memory;
- derive tests from behavioral acceptance criteria, including performance and edge cases, while leaving implementation choices to the worker;
- keep operational learning in brief `AGENTS.md`, progress/evidence in the plan, and only the current crash handoff in the scratchpad;
- update the plan immediately when discoveries create work, implement completely without placeholders, investigate unrelated failures, and use tests/build/lint/install checks as backpressure;
- capture why tests and documentation constraints matter.

Deliberate safety differences are retained: at most eight adaptive read-only subagents rather than hundreds of mutating agents; one repository writer and serialized builds; a jailed Pi backend rather than skipped permissions; no worktrees; no autonomous specification edits; no pruning of the active-cycle ledger; no automatic push, tag, or promotion to `main`. Fresh planning still discards the prior active plan from working context while Git preserves its history.

## Durable and volatile state

Durable, tracked state:

- `docs/FACTORY-LOOP-SPEC.md`: canonical specification for this boilerplate cycle (`docs/SPEC.md` remains the adopting-product placeholder, never planned against)
- `AGENTS.md`: concise build/run/validation commands and durable operational patterns
- `.factory/artifacts/implementation-plan.md`: feature task status and verification evidence
- `.factory/bugs/open.md` / `.factory/bugs/closed.md`: portable canonical defect state
- `.factory/artifacts/maintenance-plan.md`: one selected bug, fingerprint, tasks, and evidence
- `.ralph/agent/scratchpad.md`: concise crash handoff
- source, tests, README, and operational documentation
- `.factory/config.toml`, Ralph configs, prompts, and project subagent definitions

Volatile, ignored state:

- event streams and pointer files under `.ralph/`
- loop locks, diagnostics, API state, task/memory stores, and TUI exports
- Pi transcripts and scheduled-agent state
- legacy `.factory-lock` only during one-time migration,
  `.bug-ledger.lock`, and `.factory-state/` lifecycle markers
- `.ollama-usage-env`

Git checkpoints make the plan, scratchpad, and implementation recoverable. Event/task files improve same-disk recovery but are not treated as portable project history.

## Branch policy

The autonomous lifecycle runs only on the configured development branch. `main` is protected by policy and never modified by the factory. `scripts/branch-guard.sh` also rejects multiple Git worktrees.

A boilerplate experiment on a `factory/*` branch requires the explicit temporary override:

```bash
FACTORY_ALLOW_TRIAL_BRANCH=1 ./scripts/ralph-plan.sh
```

Do not carry this override into normal development.

## Prerequisites

- Ralph Orchestrator with the native Pi backend
- `pi2` configured with the `@tintinweb/pi-subagents` extension
- Ollama provider/model access
- Bash, Git, Python 3.11+, curl, flock, and optionally ShellCheck
- A clean configured development branch with at least one commit

The project tracks `.pi/subagents.json` with a maximum of eight simultaneous read-only subagents. Project agents in `.pi/agents/` intentionally expose no `bash`, `edit`, or `write` tools.

## Initial setup

1. Merge this boilerplate branch into the configured development branch.
2. Configure Ollama Cloud usage credentials:

   ```bash
   source scripts/update-ollama-cookies.sh
   ```

3. Confirm access and quota parsing:

   ```bash
   ./scripts/ollama-usage-guard.sh --check
   ```

4. Edit `docs/SPEC.md` and commit it separately:

   ```bash
   git add docs/SPEC.md
   git commit -m "spec: define the next release"
   ```

## Plan

Run the planning-only fresh-context loop:

```bash
./scripts/ralph-plan.sh
```

The planner may only modify `.factory/artifacts/implementation-plan.md` and the recovery scratchpad. A fresh invocation atomically replaces both with minimal cycle state before Ralph starts, so completed tasks are not carried into future prompts. Previous plans remain available through Git history; `--resume` preserves the active draft. The generated plan records:

- the spec path;
- the latest commit that changed the spec;
- the exact spec blob ID;
- the base commit;
- a requirement-by-requirement specification conformance matrix;
- an exhaustive interaction acceptance inventory;
- bounded tasks, dependencies, acceptance evidence, and documentation impact;
- a mandatory final documentation/specification audit that depends on every other task and executes the specification's definition of done.

Inspect the plan before implementation. Every newly accepted task must be `pending`; inherited completed, in-progress, or blocked tasks fail the planning gate. Every `partial`, `missing`, or `ambiguous` conformance row must map to a task. `scripts/check-plan-freshness.sh` prevents a stale plan or altered cycle base from running after the specification changes.

### Plan contract: `factory-plan/v1`

The canonical plan conforms to the committed schema `factory-plan/v1`
(`.factory/schemas/factory-plan-v1.schema.md`, with the machine-readable
model contract in `.factory/schemas/factory-plan-v1.schema.json`) and is
parsed by the stdlib-only deterministic parser `.factory/loop/plan_parser.py`.
The parser is part of the acceptance boundary: it binds the canonical front
matter (spec path/commit/blob, base commit, lifecycle status), unique and
contiguous task IDs, the allowed statuses and transition table,
dependency/priority fields, and the conformance matrix and interaction
inventory, and it rejects duplicate headings/keys/IDs, unknown lifecycle
states, ambiguous task sections, out-of-order or cyclic dependencies, and
non-contiguous IDs. Output is a deterministic function of the plan bytes and
`parse -> serialize -> parse` round-trips byte-exactly without semantic loss.

The hardened acceptance boundary (Task 18) closes untrusted-input gaps with
exact adversarial fixtures (`.factory/tests/fixtures/plan-*.md`): a UTF-8 BOM
prefix never parses and trailing blank lines round-trip byte-exactly; a
`verified` conformance row must reference only `complete` tasks and may not
appear in an `active` plan; the matrix must cover every ID in the committed
§24 registry (`.factory/schemas/factory-plan-v1.requirements.json`), no
more and no fewer; a `complete` lifecycle requires every task complete;
dependency and matrix ranges are never materialized and every endpoint is
bounded to the parsed task count, so oversized/overflowing ranges raise a
bounded `PlanError`; structured lifecycle fields reject continuation lines;
interaction-boundary text must be non-empty; `spec_path` must be
repository-relative and free of `.`/`..` traversal segments; the final-audit
task must be last and depend on every other task; a non-verified row must be
owned by a non-complete task; and missing/duplicated titles and empty
required values are exact defects. Every invalid fixture raises a documented
`PlanError` with no traceback or excessive allocation; every accepted
fixture (including `plan-valid-base.md` and `plan-trailing-blank-line.md`)
serializes byte-identically and stays accepted by the legacy validator.

Inspect or validate the parsed plan at any time:

```bash
python3 .factory/loop/plan_parser.py parse .factory/artifacts/implementation-plan.md
python3 .factory/loop/plan_parser.py dump .factory/artifacts/implementation-plan.md
python3 scripts/validate-implementation-plan.py planning .factory/artifacts/implementation-plan.md
```

The harness-owned conformance suite for the parser lives under the hidden
namespace (`.factory/tests/test-factory-plan-parser.py`, defect fixtures in
`.factory/tests/fixtures/plan-*.md`) per HIDE-01: harness-only tests never
land in the adopting product's visible test tree.

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

Ralph 2.10.1 otherwise interprets the real `ralph emit` acknowledgement as
a five-second deadline, kills Pi while it finishes the tool turn, and counts
that kill as an iteration failure. `scripts/pi2-ollama.sh` explicitly loads a
Pi tool-call extension that rewrites only a direct final `ralph emit` command to
`scripts/pi-cli-shims/ralph`. The shim resolves the real Ralph binary from the
jail's trusted PATH, preserves its status and stderr, and changes only the
successful command's exact acknowledgement; every other Ralph command is directly executed. Arbitrary
identical model/backend output remains visible to Ralph's fail-safe detector.
The wrapper and `scripts/pi2-secure-exec.py` retain exec-style process semantics,
so backend signals propagate without an orphaning relay process. The tradeoff is
that Pi may use a short final model turn after publication; a genuine silent hang
remains bounded by Ralph's normal five-minute inactivity timeout.

Only the final documentation and specification audit may produce `LOOP_COMPLETE`. It must satisfy the specification's definition of done: all conformance rows verified, every interaction exercised through production event dispatch with semantic outcomes, full installed verification, no contradictory open bugs, adversarial reviews, current documentation, and a clean tree.

There is no minimum iteration count: high quality is determined by evidence, not loop volume. Conversely, completing the originally planned tasks is not enough when acceptance discovers another gap. The worker preserves the ledger, appends a new uniquely numbered remediation task, adds it to the final audit dependencies, returns the audit to pending, and continues. `.factory/ralph/implementation.yml` permits up to 1000 iterations and a one-year runtime as safety ceilings. If those or an external session ceiling are reached, the plan remains active/blocked with a recovery handoff; a ceiling never constitutes completion.

### Completion protocol and checkpoint guards

Ralph recognizes a completion promise only when the reserved token is the exact final non-empty model-output line outside all `<event>` tags. A token inside an event is never completion. Sender-side Pi rewriting and command filtering are defense in depth, not an authority: same-UID code can hide or replace an executable. The trusted receiver therefore enforces the strict event-topic/schema allowlist, recursively rejects reserved tokens (including ordered string fragments), and terminates the leaf fail-closed. Each prompt still requires the standalone final line after the normal event is closed.

Before every checkpoint, planning revalidates the launcher's immutable specification metadata and cycle `base_commit`; maintenance planning performs its equivalent freshness check. `scripts/check-scratchpad.sh` requires one level-one handoff document, and every iteration hook passes its lifecycle token so contamination fails before checkpointing. `--allow-missing` covers only Ralph's fresh-loop scratchpad removal, while `--allow-oversize` warns without accepting an oversized final handoff. An ordinary checkpoint never commits a scratchpad-only change: it leaves the latest non-empty handoff in the worktree for `--resume` and recovery. Substantive source, test, plan-state, ledger, or documentation changes may commit with the scratchpad.

The tightly scoped `--final-handoff` checkpoint accepts no dirty path except the scratchpad and permits at most one metadata-only final commit per durable lifecycle cycle. In hook-finalized modes it runs before `scripts/ralph-completion-gate.sh`; the gate then records at most one successful clean-HEAD attestation for that cycle and fails if HEAD or the tracked tree changes during validation. Maintenance planning instead runs its completion hook validate-only: the untrusted hook chain must not hold the factory lock, so after Ralph returns success the trusted launcher performs the ledger transition, the strict final handoff, the gate attestation, and the final-state attestation under the retained lock, with the finalizer permitting only the scratchpad that the handoff commits. No tracked commit follows a passing attestation. In implementation completion, front matter must be exactly `complete`, every task must be `complete`, and every conformance row must be `verified`; unavailable declared capabilities remain findings until real evidence exists.

When the strict gate rejects a valid premature completion request, it still writes an atomic, one-shot marker bound to the launcher nonce, lifecycle mode, loop ID, and canonical workspace. Hook payload bytes are retained in memory, and marker removal is a dirfd/no-follow quarantine-then-validate operation, so pathname substitution cannot authorize continuation. The supervisor consumes only a matching marker and continues the same cycle with `--continue`. Completion-rejection, stale, and combined no-progress counts are persisted under `.factory-state/`, so process or campaign resume cannot reset their ceilings (eight, two, and eight by default). Stale, malformed, mismatched, or symlink markers cannot authorize continuation. History replacement, malformed records, exhausted budgets, and arbitrary non-quota failures are terminal; quota exhaustion remains inside the leaf launcher's verified wait path.

A phase-start handshake records the exact received event delta digest and size, the exact first trusted record digest and size, and its attempt/cycle nonce. Campaign confirmation requires the same inode, exact total size, and identical bytes; both later appends and same-size rewrites fail closed. `.ralph`, pointer markers, and event streams must be owned and not group/other writable. These controls require Linux `O_NOFOLLOW`, dirfd, `/proc`, and directory `flock` primitives; the lifecycle exits explicitly when they are unavailable.

## Run a finite multi-round campaign

A campaign removes the human-operated outer loop while retaining objective
stopping boundaries:

```bash
# Unattended by default:
./scripts/ralph-campaign.sh --rounds 3
# Optional attended diagnostic display:
./scripts/ralph-campaign.sh --rounds 3 --tui
```

Each mandatory round records the current clean `HEAD` as a new base, runs a
fresh `ralph-plan.sh` cycle, runs the resulting plan through `ralph-run.sh`,
executes `verification.campaign_command`, validates installed-functional
evidence, transfers the exact clean Git tree to every declared runner, and
validates commit-bound runner receipts.
Immediately before local verification, the campaign opens and retains an immutable descriptor to the binding helper before any untrusted phase, recomputes and compares the tracked config and executable Git blobs, content digests, canonical argv, and secure modes, then executes the exact opened verifier inode through the retained `/proc/self/fd` descriptor; implementation-time replacement, same-size rewrite, writable modes, or binding drift fails before the verifier runs. It then launches an independent adversarial audit
through `ralph-audit.sh`. A prior completion claim never shortens the requested
round count. The next round's fresh planner consumes the preceding
`.factory/artifacts/campaign-audit.md`; prior plans and audit reports remain in Git history.

Ignored state in `.factory-state/ralph-campaign.json` records the requested
rounds, selected TUI mode, current phase, each round base, phase-start markers,
commits, and audit results. Start a new campaign only from a clean configured development branch.
Resume an interrupted active campaign with exactly matching options:

```bash
./scripts/ralph-campaign.sh --rounds 3 --resume
# Only when the saved campaign was explicitly attended:
./scripts/ralph-campaign.sh --rounds 3 --resume --tui
```

### Rebind a reviewed pre-verification fast-forward

A stopped first round may exceptionally need to include reviewed linear commits
that landed after its implementation checkpoint but before any verification
binding was recorded. Confirm no Ralph process is alive, retain an
operator-controlled copy of `.factory-state/ralph-campaign.json`, and record its
digest before recovery:

```bash
sha256sum .factory-state/ralph-campaign.json
cp -a .factory-state/ralph-campaign.json /operator-controlled/ralph-campaign.before-rebind.json
old=<recorded-implementation-commit>
new=$(git rev-parse HEAD)
./scripts/ralph-campaign-state.py rebind-implementation \
  --expected-old "$old" --new "$new"
```

Do not use the generic `update` command or edit the JSON. The dedicated command
acquires the factory lock and succeeds only for an active first-round
`verification` phase whose verification, runner-evidence, and audit fields are
all unset. The explicit old value must match, the new value must be the current
clean development-branch HEAD and a strict merge-free descendant, and state is strictly
validated before and after an fsync-backed atomic replacement. Its JSON receipt
records the old/new commits and before/after state SHA-256 digests. It never
runs Ralph or changes evidence. Review the receipt, then use the normal campaign
`--resume`; verification reruns for the rebound commit. All later-round,
already-verified, dirty, backward, equal, non-ancestor, merge, or wrong-old
requests fail without changing state.

The trusted campaign, launchers, and state transitions share an inherited
exclusive `flock` on the already-open canonical repository-root directory, so
planning, implementation, verification, audit checkpointing, and recovery
retain one repository writer without a replaceable authority pathname. Ralph/Pi,
hooks, gates, verifiers, runners, evidence checkers, tests, and product commands
run only after an already-loaded shell function closes the dynamic repository-root descriptor and unsets all lock metadata in a subshell before any mutable workspace executable runs, so background descendants retain, unlock, or claim nothing. A separately opened root FD cannot unlock the parent's
open-file description. Migration first acquires any safe legacy `.factory-lock`,
fails if it is busy or ambiguous, then quarantines and validates it before
removal.
Quota waits and bounded rejection/stale recovery remain inside each leaf.
The campaign does not retry arbitrary nonzero leaf or gate results: it returns
nonzero with durable state still active at the same resumable phase. Invalid or
corrupt state, rewritten bases, dirty boundaries, and conflicting options fail
the same way. Intermediate audit findings become mandatory input to the
next round. Findings in the final configured round leave the campaign blocked
and return nonzero rather than claiming completion; begin another reviewed
campaign to remediate them.

### Declared tools and runners

`.factory/environment.toml` is the tracked, credential-free declaration of what
the factory can actually execute. Agents must not invent undeclared tools, capabilities, runners, or external evidence. Hostnames, usernames, ports,
private-key paths, passwords, tokens, and secrets remain outside Git. Validate
it with:

```bash
./scripts/check-factory-environment.py
```

Planning, implementation, and independent audit prompts treat the declaration
as exhaustive. During verification, `scripts/run-factory-runners.py` creates a
history-free `git archive` of the exact clean commit, rejects tracked symlinks,
gitlinks, or special modes that this protocol cannot reproduce safely, sends the
archive through the pinned SSH alias, verifies the extracted Git tree remotely, runs the fixed argv without
reusing a checkout or HOME, and cleans the remote workspace. Local receipts and
bounded logs are written beneath `.factory-state/runner-evidence/` and validated
by `scripts/check-factory-runner-evidence.py`. A failed transport, tree binding,
probe, verifier, cleanup receipt, signer, or evidence digest stops the campaign.

Runner receipts are signed by a root-owned signer on the disposable runner VM
(`scripts/factory-runner-signer.py`, installed root-owned and reached only
through a narrow sudoers rule). The unprivileged forced-command endpoint never
signs: after the exact archive/tree/environment/verifier/probes all pass, the
root signer re-validates every manifest field (clean pass only, capability set
exactly equal to the runner class allowlist, bound digests, no caller-supplied
signer identity), rebuilds the canonical signed manifest itself, and returns
the detached signature plus aggregate signer metadata. The runner protocol is
class-based: `/etc/factory-runner/runner-policy.json` (root-owned, out-of-tree,
validated by `scripts/factory_runner_policy.py`) binds the executing UID to
exactly one class with its workspace root, approved verifier argv, capability
allowlist, and root-owned signer key/principal; neither the endpoint nor the
signer hardcodes product names, verifier paths, capability names, workspace
roots, or the receipt namespace. The private signing key is root-owned mode
0600 on the runner, unavailable to the runner accounts, and is never printed
or copied into Git; this repository carries only the public keys and trust
policy in `.factory/signer-trust.json`. Signer rotation is fail-closed: a
receipt signed by a key that is no longer in the trust store is rejected.

`.factory/config.toml` lists product-specific capabilities required for a clean audit.
Only capabilities covered by accepted exact-commit evidence count; all others
remain findings until their production probes and artifacts are implemented.
Runner provisioning and credentials are maintained outside this repository.

## Maintain one bug

Ordinary defects stay out of `docs/SPEC.md`. Canonical state is tracked in
`.factory/bugs/open.md` and `.factory/bugs/closed.md`, with optional manual references to GitHub,
Forgejo, or both. After human triage, run:

```bash
./scripts/ralph-maintenance-plan.sh BUG-0001
./scripts/ralph-maintenance-run.sh
```

A fresh invocation replaces the previous maintenance plan and scratchpad with a minimal selected-bug skeleton; prior evidence remains in Git and the closed ledger, while `--resume` preserves an interrupted draft. Every newly accepted maintenance task must be pending. The dedicated plan is bound to the immutable bug intake, committed spec, and
planning checkpoint. The single-writer maintenance loop adds regression tests,
implements the fix, runs the configured project verifier, records closure
evidence, and moves only that bug into the closed ledger. Contract changes are
blocked and returned to the specification workflow. See
[BUG_WORKFLOW.md](BUG_WORKFLOW.md).

## Adaptive concurrency

Configured ceilings live in `.factory/config.toml`:

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

The script restores a missing tracked scratchpad, removes only a stale lock, recognizes timestamped and fallback event streams, reconstructs pointer files, and starts `ralph-run.sh --resume`.

If unfinished runtime tasks belong to multiple loop IDs, recovery refuses to guess; pass the intended ID explicitly:

```bash
./scripts/ralph-recover.sh --loop-id primary-YYYYMMDD-HHMMSS
```

Planning and maintenance recovery use:

```bash
./scripts/ralph-recover.sh --mode planning
./scripts/ralph-recover.sh --mode campaign-audit
./scripts/ralph-recover.sh --mode maintenance-planning
./scripts/ralph-recover.sh --mode maintenance
```

New loops persist their lifecycle mode and recovery rejects a mismatched mode.
Recovery never resets Git or starts a second writer.

## Specification changes

Never edit the specification during implementation. `check-plan-freshness.sh` compares both the latest spec commit and the exact Git blob against plan metadata. If they differ:

1. stop the implementation loop;
2. commit the revised `docs/SPEC.md`;
3. run `./scripts/ralph-plan.sh`, which seeds minimal plan/scratchpad state and leaves the completed plan only in Git history;
4. inspect the replacement plan and confirm it contains only current pending gaps;
5. start a new implementation loop.

## Documentation gate

Every implementation plan ends with **Final documentation and specification audit**. `scripts/validate-implementation-plan.py` requires the plan to contain a conformance matrix, interaction inventory, canonical task statuses, and a final audit depending on every other task. At implementation completion it rejects unfinished tasks and any matrix classification other than `verified`. The final gate also validates bug ledgers, rejects unresolved open bugs, runs project verification, and then requires commit-bound `test_installed_functional` evidence with zero skips. `scripts/verify-project.sh` writes the local evidence only after the mandatory test and packaging gates pass; `scripts/check-installed-functional-evidence.sh` invalidates it if production or acceptance inputs changed afterward. This prevents mocked or proxy-only coverage, fixture assembly without production dispatch, backend-less skips, or optional smoke skips from satisfying installed production behavior.

Read-only reviewers compare source, tests, configuration, README, operations, and the specification, specifically looking for tests that bypass production initialization/event dispatch or assert pixels without semantic behavior. The sole writer corrects documentation and runs final verification. If review finds a gap, Ralph appends remediation and continues; `LOOP_COMPLETE` is forbidden until the complete definition of done passes.

## Machine-readable acceptance evidence

Proxy evidence must not be promoted to production verification. Three tracked artifacts make acceptance machine-checked:

- `.factory/artifacts/conformance.json` (schema `ralph-conformance/v1`) is the only authority for `verified` claims. Each requirement row declares classification (`verified`/`partial`/`missing`/`ambiguous`/`blocked`/`not_applicable`), evidence tier (`unit`/`simulated`/`private_integration`/`installed`/`real_system`/`human`), required capabilities, the exact evidence commit, and receipt/artifact refs. `scripts/validate-conformance.py planning|complete` checks the schema, cross-checks the plan matrix, and rejects `verified` rows that are below the normative required tier, unevidenced, or backed by an undeclared capability. `blocked` and `partial` rows always fail implementation completion; `not_applicable` requires a spec-scoped reason.
- `.factory/capability-contracts.json` (schema `ralph-capability-contract/v1`) defines one probe per declared/required capability: probe argv, must-execute marker, must-not-skip tokens, and deny-simulated markers. `scripts/check-capability-contracts.py` rejects contracts for undeclared capabilities and declared capabilities without contracts; `scripts/check-capability-evidence.py` requires a fresh exact-commit runner receipt whose probe section executed (no skip) and shows no simulated marker. Missing contract, probe, or receipt is unevidenced and never auto-reclassified. The generic repository keeps an empty contract instance; per-product contracts belong in the product repository.
- Audit reports must cite machine receipts: coordinator-executed commands are wrapped by `scripts/machine-receipt.py --tag <tag> -- <argv...>` and recorded under `.factory-state/audit-receipts/`. `scripts/check-audit-receipts.py` requires every executable-evidence line to carry PASS/FAIL/BLOCKED plus a `[receipt: ...]`/`[manifest: ...]` reference, PASS requires exit 0, and any BLOCKED evidence forces `result: findings`. Subagent prose cannot certify runtime.

Pixel/offscreen framebuffer checks are not real visual acceptance, private/session-scoped service instances are not the real system service, a synthetic test producer is not the target consumer, and an evidence declaration is not evidence. `final-gate.sh` `--implementation` and `--campaign-audit` run all three layers; `--planning` validates an existing sidecar so a fresh cycle stays pendable before migration.

## Machine visual-audit scaffold

A product-neutral, optional machine visual-audit framework ships in the
boilerplate as a scaffold (`.factory/visual-audit.toml`, the
`scripts/visual-audit-*.py/.sh` and `scripts/check-visual-audit.py` tools,
`tests/test-visual-audit.sh`, the review schema, and the frozen review prompt).
It is **disabled by default** and defaults **no vision model**: `vision_model`
is a consumer-configured placeholder that stays empty until a consumer sets it,
and `scripts/visual-audit-probe.sh` fails closed unless the consumer configures
`VISUAL_AUDIT_VISION_MODEL`. The generic capture adapter
(`scripts/visual-capture-driver.sh`) also fails closed with a clear message
until the consumer implements installed exact-commit capture; tests use only
explicit test-only mock drivers.

All mutable capture/review/calibration/probe state lives under the ignored
`.factory-state/visual-audit/` directory; nothing mutable is tracked. When a
consumer enables the framework it must first implement an installed
exact-commit capture driver, replace the placeholder inventory/calibration
templates with its own visual states, and prove a real non-skipping image
round-trip through `scripts/visual-audit-probe.sh`.

The production SDK review must use the same Pi model/credential authority as
factory `pi2`. Provision Pi's standard variable in the operator/service
environment before probing, calibration, and review:

```bash
export PI_CODING_AGENT_DIR="<trusted-pi2-agent-directory>"
```

The directory must be a canonical, invoking-user-owned directory beneath that
user's `~/.pi`, with no group/other-writable path component. Its `auth.json` and
`models.json` must be owned, single-link regular non-symlink files, owner
readable, and inaccessible to group/other (normally mode `0600`). The SDK passes
those two paths explicitly to `ModelRuntime.create`; `agentDir` alone is not a
credential-runtime binding. Missing or unsafe authority fails closed instead of
falling back to `~/.pi/agent`. There is deliberately no visual-audit-specific
auth, model-config, or agent-directory override, and credential contents are
never logged or copied by the scaffold.

Authority is **supplemental, findings-only, and never elevating**:

- A machine visual review can only *add* findings; it never certifies that any
  command ran, any window opened, or any runtime behavior occurred. A review
  pass means only that the configured vision model reported no machine
  findings over the provenance-bound screenshots.
- Machine vision never promotes an evidence tier: it does not write to
  `.factory/artifacts/conformance.json`, never reclassifies `verified`/tier
  rows, and never replaces deterministic/real-system/human acceptance. The
  conformance sidecar remains the only authority for verified claims.
- Every review report is bound to the exact commit/tree, the frozen prompt and
  schema digests, and the exact captured image bytes; replay, drift, tamper,
  outage, and shared-session races fail closed (`scripts/check-visual-audit.py`
  is the aggregate gate). A receipt proves invocation, not visual truth.

Machine vision is **supplemental falsification/findings-only**, never a
certification oracle:

- A machine PASS cannot certify visual truth, and it cannot elevate
  unit/simulated/private-integration/installed evidence to
  `real_system`/`human`. A clean review only falsifies nothing; it confirms
  nothing about real system behavior.
- Captures and review reports do **not** replace compositor, physical,
  target-consumer, or human evidence. A screenshot is not a compositor
  observation, a framebuffer grab is not a physical display, a synthetic
  producer is not the target consumer, and no machine report substitutes for
  a human judgement call.
- Machine-generated baselines cannot self-certify goldens: a baseline is not
  independent ground truth, and human/golden acceptance remains out-of-band
  (a separate, human-reviewed artifact and decision).
- When enabled, the completion gate validates an existing exact-commit report
  only; it never invokes the capture driver or vision model under the
  lifecycle lock. Gate validation reads a stored report and does not run any
  machine vision at completion time.

## Credential boundary guard

`scripts/credential-guard.py` and the project-local Pi extension enforce a
best-effort credential boundary at tool invocation and result persistence.
Potential environment/authentication dumps and sensitive direct file paths are
blocked before execution. Text returned by tools, including nested result
details, is redacted before Pi displays or persists it. Classification and
redaction subprocesses receive candidate text on standard input, never in
process arguments, and any guard failure blocks the call or replaces the
result with `[REDACTION FAILED]` without echoing the candidate.

Large Bash results require an additional precaution because Pi's built-in Bash
tool writes its overflow file before emitting `tool_result`. The extension
accepts only the expected owned, single-link, regular `pi-bash-*.log` path in
the system temporary directory, changes it to owner-only access, and atomically
replaces it with redacted bytes. Sanitization failure truncates that recognized
owned file and fails the result closed. There is nevertheless a small
pre-hook crash window between the built-in tool writing raw overflow and the
`tool_result` handler sanitizing it. The guard therefore reduces exposure but
does not claim perfect prevention; operators must keep temporary storage
private and rotate any credential known to have appeared in prior output.

These controls are defense in depth, not permission to place credentials in
prompts, commands, tracked files, logs, or test fixtures. Adversarial tests use
synthetic secret-shaped values only.

## Verify

```bash
./scripts/verify-boilerplate.sh
```

The verifier checks shell syntax, ShellCheck when available, TOML/JSON configuration, read-only agent tools, single-writer settings, quota behavior, plan freshness, branch policy, removed product artifacts, and secret tracking.

## Release

After Ralph reports completion, review the configured development branch. Release manually:

```bash
git switch main
git merge --no-ff <development-branch>
git tag vX.Y.Z
```

For the next release, update the same `docs/SPEC.md` in a dedicated commit, run a new planning loop, and execute a new implementation loop. Git retains prior specifications and plans.