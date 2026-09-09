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
development technique using a fresh-context Python control plane: planner,
developer, tester, and auditor run as separate fresh, confined model
processes with deterministic task selection; jailed Pi with real Landlock
confinement; the retained Ollama usage guard; Git checkpoints; quota
waiting; and crash recovery.

The canonical bound specification (this cycle: `docs/FACTORY-LOOP-SPEC.md`, per
`.factory/config.toml` `[project].spec`; `docs/SPEC.md` stays the adopting-product
placeholder and is never planned against) is the source of truth.
`.factory/artifacts/implementation-plan.md`
tracks task status and verification evidence.

## Operating model

- `main` is the human-controlled release branch.
- The configured development branch (`.factory/config.toml` `development_branch`) is the autonomous implementation branch.
- One committed canonical specification is the source of truth; Git versions it. This cycle binds `docs/FACTORY-LOOP-SPEC.md` (`.factory/config.toml` `[project].spec`); the adopting product supplies its own `docs/SPEC.md` later.
- A planning phase (one of the four fresh roles) creates `.factory/artifacts/implementation-plan.md` for the exact spec commit.
- Each implementation iteration selects exactly one bounded task deterministically and starts with fresh model context.
- Four static roles — planner, developer, tester, auditor — are the complete role set; parallel model launches are forbidden. Each runs from a static digest-bound prompt: `.factory/prompts/planner.md`, `.factory/prompts/developer.md`, `.factory/prompts/tester.md`, `.factory/prompts/auditor.md`.
- Exactly one primary worker may edit, stage, or commit repository files; Git history and commit operations run in the trusted orchestrator, never in model tools.
- Tests, documentation, machine evidence, and an independent audit are completion gates.
- Before each round's planner, the trusted campaign executes the exact committed `.factory/pre-round-hooks.json` registry once in order. Its strict schema admits only fixed internal implementations and initially contains mandatory `branch_guard`; no command, argv, quota, or cookie surface is accepted.
- Exactly one root canonical mutable control-state file `.factory-state/factory-loop.json` (schema `factory-state/v1`, the exact §11 field set). Round-zero readiness and pre-round hook extension data are coordinator-owned and live in strict campaign-bound sidecars (`.factory-state/readiness.json` `factory-readiness-state/v1` and `.factory-state/pre-round-hooks.json` `factory-pre-round-hook-state/v1`), never as fields, phases, or outcomes in canonical state. `factory-state/v2` is the legacy pre-migration format accepted only by the offline migration helper.
- You review the configured development branch and manually promote it to `main`.

No Git worktrees are used.

## Fresh role boundary

The replacement control plane starts planner, developer, tester, and auditor
as separate fresh processes with static digest-bound prompts. A real Landlock
sandbox is installed before the model wrapper starts. It excludes `.git`,
legacy/runtime state, hidden factory implementation, shared `/tmp`, `/proc`,
host configuration, and credential stores; only role-specific current inputs,
existing write targets, and per-launch private paths are granted. The trusted
orchestrator performs Git history and commit operations after the role exits.
Hosts without the required confinement primitive cannot launch a model.
Credential enforcement and output masking use one exact-commit guard at the
Pi SDK tool boundary and every retained process/gate output boundary. Child and
gate environments are rebuilt from allowlists, Git is pinned independently of
caller `PATH`, and unverifiable guard or external-backend authority fails
before launch.

## Finite phase campaign

The trusted `.factory/loop/campaign.py` authority serializes one writer through
planning, implementation, verification, and audit. It derives recovery from
Git, the canonical plan, and `factory-state/v1`; it creates no runtime task
queue, model memory, event stream, context summary, or resumed model identity.
Exact task bytes and deterministic audit objectives are bound before launch,
while trusted Git operations remain outside Landlock. Work exhaustion still
runs verification and audit. Campaigns are finite and publish one of the six
specified terminal outcomes rather than retrying empty work indefinitely.

Software verification is separate from external release acceptance. When the
deterministic gate passed, no finding remains, the declared capability is
available, and the tester cited exact blocked references, verification
reports `software_verified_external_acceptance_blocked` (STATE-02): software
is fully verified while external release acceptance (human approval,
real-system evidence, or an unavailable external release authority) remains
blocked. The outcome advances to the independent audit but can never produce
campaign success — a final-round audit `pass` entered from it resolves to
the terminal `blocked` state, never `success` — and it never weakens
round-zero readiness, infrastructure-failure fail-closed closes, or human
authority. Every deterministic verifier failure is additionally recorded as
a strict structured verifier-failure artifact (`factory-verifier-failure/v1`,
EVID-02) carrying the exact command as data (never an executable path/argv
authority), the exact exit status, expected vs observed, a bounded output
tail/reference, changed files, artifact refs, an environment/capability
classification, and a rerun scope, with bounded sizes, closed enums, and
duplicate-key rejection.

Phase 2B1 inner same-task convergence: when a trusted deterministic software
verifier failure meets every convergence condition (the gate actually ran and
returned an ordinary nonzero status, the tester passed with no findings, the
declared capability is available and ran clean, no scope violation, the task
is bound, the retry budget remains, the failure is not a byte-identical
repeat, the task resource budget is not exhausted, and the campaign deadline
remains), the campaign returns to implementation for the SAME task with the
validated verifier-failure artifact, bypassing planner/tester/auditor
ceremony.  The artifact is published write-once by its content-addressed
digest and re-validated at consumption against the exact commit, campaign,
task, and digest; its fields enter the developer's sealed prompt as inert
quoted data (the command is a record, never an authority to re-execute).  The
campaign performs at most two same-task convergence retries per task
(`MAX_CONVERGENCE_RETRIES = 2`); a repeated identical failure (same
fingerprint) and a consumed task resource budget terminate the loop honestly
before that bound.  Infrastructure, capability, human/external, and
tester-finding failures never converge.

Findings are evidence, not tasks. Trusted findings/blocked outcomes produce an
exact-commit, digest-bound, write-once receipt. The next fresh planner alone
receives the deterministic findings payload and may convert it into canonical
plan work. Developers see a finding only after it appears in a revised plan;
no receipt, result file, event stream, or memory participates in selection.
The findings channel is byte-exact: the planner receives the verbatim
payload plus its SHA-256 digest, and a payload whose bytes do not carry the
delivered digest (a paraphrase, subset, or substituted revision) is refused.
The developer receives only the exact revised selected-task bytes: the
campaign driver seam derives the task-excerpt digest from the exact committed
plan at the phase head with the same real launch authority that binds the
production child environment, and the fixture driver fails closed when that
digest is absent, recording fixture evidence of the exact task-excerpt
digest, the digest of the plan it worked, and the absence of any findings
channel. That evidence lives under the fixture-only
`src/.factory-test-output/` namespace with a fixture schema; no production
authority reads it and it is never a model-visible input — the trusted suite
re-derives every digest from the committed plan with the real authority and
asserts the exact bytes itself. The hidden §22 conformance suite
(`.factory/tests/test-factory-adversarial.sh`, case 05) hard-checks every
authority prerequisite (the loop campaign/findings/launch/plan-parser/
selector/state modules, the committed fixture driver, the findings campaign
suite providing `FindingsWorkspace`, the adversarial manifest, and the
findings schemas) with a named diagnostic before running, so a missing or
renamed authority can never silently weaken a conformance case.

Verification executes the exact committed verifier through a retained file
descriptor, not a mutable pathname. Receipt and manifest citations are
metadata-hardened, exact-command-bound, and fail closed on stale identity,
signature, digest, tier, or coordinator state. Machine evidence can add
findings but cannot elevate installed/real-system/human acceptance tiers or
become task authority.

An explicit evidence-smoke lane (`.factory/loop/campaign.py run
--evidence-smoke`, driven by the trusted operator command
`.factory/smoke/evidence_smoke.py`) instantiates the live campaign machinery
for exactly one full planning/implementation/verification/audit round with
the designated deterministic committed smoke seam
(`.factory/smoke/evidence_smoke_driver.py`): it fails closed on a dirty tree,
wrong branch/commit, unbound seam, or foreign seam label, preserves every
pre-existing `.factory-state` byte (digest/mode/mtime snapshot), commits one
bounded tracked evidence artifact under `.factory/artifacts/`, and reports an
honest finite outcome — the round proves one full phase cycle (round limit)
without claiming acceptance. The seam is private source methodology
evidence only — never a real model/human outcome, never installed-tier
evidence, never GIT-01 acceptance evidence, and never acceptance-tier
evidence; no external model, credential, cookie, runner, or human is
invoked. The round makes exactly two orchestrator commits (the canonical
planner revision, then the developer task-complete revision with the
tracked evidence artifact); the planner output keeps Task 22 `pending` and
only the developer marks it `complete` (spec §6.2). See `docs/OPERATIONS.md`.

## Ralph migration boundary

`.factory/ralph-freeze` prevents new legacy campaign launches. The hidden
`.factory/loop/migration.py` authority derives migration only from the committed
plan, HEAD, dirty-path metadata, current evidence/blocker metadata, and the
single `factory-state/v1` file. It never opens or imports `.ralph` tasks,
memories, events, completion tokens, scratchpads, or the removed persisted
context summary. Legacy presence and workspace `.ollama-usage-env` are detected
with `lstat` metadata only; credential bytes are never read or moved. An
operator may migrate to the external XDG store explicitly after validating the
legacy file, but the workspace file is never new-path credential authority.
Visible `.factory/tools/ralph-*` entrypoints are frozen deprecated compatibility
surfaces and the new campaign has no dependency on them.

## Relationship to Huntley's playbook

The prompts are periodically compared against [`ghuntley/how-to-ralph-wiggum`](https://github.com/ghuntley/how-to-ralph-wiggum). The pinned commit `88d488a148af97e4a3f22b11b4c3598c79d6a577` is a **historical snapshot only and is non-authoritative**: the boilerplate never depends on the external playbook, its prompts, or that commit, and the pinned snapshot is not a contract this cycle must track. The canonical `docs/FACTORY-LOOP-SPEC.md` is the only authority. This boilerplate adopts the playbook's highest-value context and backpressure patterns:

- deterministic orientation: study the specification, plan, concise `AGENTS.md`, source, tests, and shared patterns every iteration;
- **do not assume functionality is missing**—search and trace production behavior first;
- keep the primary context as scheduler and use parallel subagents as disposable read-only memory;
- derive tests from behavioral acceptance criteria, including performance and edge cases, while leaving implementation choices to the worker;
- keep operational learning in brief `AGENTS.md`, progress/evidence in the plan, and only the current crash handoff in the scratchpad;
- update the plan immediately when discoveries create work, implement completely without placeholders, investigate unrelated failures, and use .factory/tests/legacy/build/lint/install checks as backpressure;
- capture why tests and documentation constraints matter.

Deliberate safety differences are retained: at most eight adaptive read-only subagents rather than hundreds of mutating agents; one repository writer and serialized builds; a jailed Pi backend rather than skipped permissions; no worktrees; no autonomous specification edits; no pruning of the active-cycle ledger; no automatic push, tag, or promotion to `main`. Fresh planning still discards the prior active plan from working context while Git preserves its history.

## Durable and volatile state

Durable, tracked state:

- `docs/FACTORY-LOOP-SPEC.md`: canonical specification for this boilerplate cycle (`docs/SPEC.md` remains the adopting-product placeholder, never planned against)
- `AGENTS.md`: concise build/run/validation commands and durable operational patterns
- `.factory/artifacts/implementation-plan.md`: feature task status and verification evidence
- `.factory/bugs/open.md` / `.factory/bugs/closed.md`: portable canonical defect state
- `.factory/artifacts/maintenance-plan.md`: frozen legacy maintenance artifact (the fresh loop has no maintenance role)
- `.ralph/agent/scratchpad.md`: concise crash handoff
- source, tests, README, and operational documentation
- `.factory/config.toml`, the frozen legacy Ralph configs, static role prompts, and project subagent definitions

Volatile, ignored state:

- legacy event streams and pointer files under `.ralph/` (the new loop creates none)
- legacy loop locks, diagnostics, API state, task/memory stores, and TUI exports
- Pi transcripts and scheduled-agent state
- legacy `.factory-lock` only during one-time migration,
  `.bug-ledger.lock`, and `.factory-state/` lifecycle markers
- `.ollama-usage-env`

Git checkpoints make the plan and implementation recoverable. The new loop
creates no event/task files; `.factory-state/` holds only the one mutable
state file and append-only evidence.

## Branch policy

The autonomous lifecycle runs only on the configured development branch. `main` is protected by policy and never modified by the factory. `.factory/tools/branch-guard.sh` also rejects multiple Git worktrees. The campaign CLI requires the exact `--branch`; there is no trial-branch escape in the new loop.

## Prerequisites

- Linux with the Landlock LSM (path-beneath rules) and `/proc` plus `flock`/`O_NOFOLLOW`; hosts without the required confinement or lifecycle primitives fail closed
- Python 3.11+ (stdlib-only control plane), Git, curl, flock, and optionally ShellCheck
- OpenSSH (`ssh-keygen -Y verify`) for signed runner evidence; runners and SSH aliases are declared/configured outside the repository
- Ollama provider/model access and the retained usage guard for `ollama`-provider launches
- A clean configured development branch with at least one commit

The project tracks `.pi/subagents.json` with a maximum of eight simultaneous read-only subagents. Project agents in `.pi/agents/` intentionally expose no `bash`, `edit`, or `write` tools; the fresh loop itself launches only the four role processes, never adaptive subroles.

## Initial setup

1. Merge this boilerplate branch into the configured development branch.
2. Configure Ollama usage credentials for the operator store (outside the model workspace):

   ```bash
   source .factory/tools/update-ollama-cookies.sh
   ```

3. Confirm access and quota parsing:

   ```bash
   ./.factory/tools/ollama-usage-guard.sh --check
   ```

4. This cycle's canonical specification is the committed
   `docs/FACTORY-LOOP-SPEC.md`; an adopting product replaces it with its own
   committed product specification and commits it separately before planning.

## Plan

Planning runs inside the finite campaign (`.factory/loop/campaign.py run`):
the planner role is one fresh confined process, and the trusted campaign
derives every binding from the committed plan and control state at the
current HEAD.

The planner may only modify `.factory/artifacts/implementation-plan.md`. A
fresh planning phase atomically replaces the plan with minimal cycle state,
so completed tasks are not carried into future prompts; previous plans
remain available through Git history. The generated plan records:

- the spec path;
- the latest commit that changed the spec;
- the exact spec blob ID;
- the base commit;
- a requirement-by-requirement specification conformance matrix;
- an exhaustive interaction acceptance inventory;
- bounded tasks, dependencies, acceptance evidence, and documentation impact;
- a mandatory final documentation/specification audit that depends on every other task and executes the specification's definition of done.

Inspect the plan before implementation. Every newly accepted task must be `pending`; inherited completed, in-progress, or blocked tasks fail the planning gate. Every `partial`, `missing`, or `ambiguous` conformance row must map to a task. `.factory/tools/check-plan-freshness.sh` prevents a stale plan or altered cycle base from running after the specification changes.

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
python3 .factory/tools/validate-implementation-plan.py planning .factory/artifacts/implementation-plan.md
```

The harness-owned conformance suite for the parser lives under the hidden
namespace (`.factory/tests/test-factory-plan-parser.py`, defect fixtures in
`.factory/tests/fixtures/plan-*.md`) per HIDE-01: harness-only tests never
land in the adopting product's visible test tree.

### Deterministic task selection

`.factory/loop/selector.py` implements the trusted §8 selection boundary as a
pure, stdlib-only function of the parsed plan (plus the optional bound base
commit). It rejects a stale plan (front-matter `base_commit` differs from the
bound commit) or an ambiguous plan (an `in_progress` task whose dependencies
are not all `complete`); resumes the sole `in_progress` task; otherwise sorts
runnable `pending` tasks (every dependency `complete`) by explicit numeric
priority then lexicographic task identifier; and selects exactly one. When
none are runnable it classifies the phase `work_exhausted` (no pending or
`in_progress` task remains) or `blocked` (unfinished tasks remain but none
can run, each blocked directly or transitively through a blocked
dependency). The selector performs no I/O, never reads a runtime task ledger,
control-state file, environment, or process state, and the model never
chooses among tasks. The harness-owned suite `.factory/tests/test-factory-
selector.py` (fixtures `.factory/tests/fixtures/plan-select-*.md`) proves the
exact selection order, tie-breaks, single-task guarantee, and empty-work
classifications; inspect it at any time with:

```bash
python3 .factory/loop/selector.py select .factory/artifacts/implementation-plan.md
```

## Implement

The implementation phase is the same finite campaign (`.factory/loop/campaign.py run`): each attempt launches the developer as a fresh confined process through `python -m factory.loop.launch launch` with the exact selected task excerpt (bytes re-derived from the committed plan and digest-matched), then the trusted campaign validates the result against plan state and Git. Each iteration:

1. validates branch, plan freshness, and the single control-state file;
2. relies on the round's already-completed ordered pre-round hook sequence (planner retries never rerun it);
3. deterministically selects one ready task (`.factory/loop/selector.py`);
4. implements and tests exactly that task with one writer;
5. updates the plan and (legacy) crash handoff;
6. commits one coherent checkpoint through the trusted pinned-Git authority;
7. exits so the next task receives fresh context.

Only the final documentation and specification audit may complete the cycle. It must satisfy the specification's definition of done: all conformance rows verified, every interaction exercised through production paths with semantic outcomes, full installed verification, no contradictory open bugs, adversarial reviews, current documentation, and a clean tree.

There is no minimum iteration count: high quality is determined by evidence, not loop volume. Conversely, completing the originally planned tasks is not enough when acceptance discovers another gap. The planner preserves the plan, appends a new uniquely numbered remediation task, adds it to the final audit dependencies, returns the audit to pending, and continues. The configured attempt/round budgets are safety ceilings, not targets; a reached ceiling leaves the cycle incomplete with durable state, and a ceiling never constitutes completion.

### Checkpoint protocol and completion

Completion is never a model claim. The trusted harness derives phase
outcomes from plan state, Git state, exit status, and deterministic gates
(§13); model prose and completion tokens are not control protocol. Each
phase records its digest in the append-only evidence ledger before it starts
and re-validates it after, so a mid-phase mutation of the control-state file
fails closed. `.factory/ralph-freeze` keeps the legacy launchers from
starting new cycles; `.factory/tools/check-scratchpad.sh` still guards the legacy
handoff document on the frozen surface.

An ordinary checkpoint never commits a scratchpad-only change: it leaves the
latest non-empty handoff in the worktree for recovery. Substantive source,
test, plan-state, ledger, or documentation changes may commit with it. The
final gate (`.factory/tools/final-gate.sh --implementation`) attests a clean
unchanged HEAD and requires every task `complete`, every conformance row
`verified`, and exact-commit installed evidence; no tracked commit follows a
passing attestation.

These controls require Linux `O_NOFOLLOW`, dirfd, `/proc`, and directory
`flock` primitives; the lifecycle exits explicitly when they are unavailable.

## Production round-zero readiness

Real-provider production has a mandatory round zero governed by the strict
committed `.factory/readiness-policy.json` (`factory-readiness-policy/v1`). The
policy declares generic runner classes/capabilities, conformance/core gate IDs,
optional externally provisioned human trust/checklist/capture bindings, and
accepted-commit/current-product invalidation scopes. Gate IDs resolve through a
fixed internal adapter registry; arbitrary commands are not admitted. The
boilerplate default enrolls no production authority, so it explicitly reports
`blocked` before external execution and imposes no product hardware.

Readiness is a coordinator-owned round-zero concern, not a canonical phase or
outcome: it runs *before* canonical state initialization, so canonical
`current_phase` is never `readiness` and `current_round` starts at 1 per §11.
Its binding/cursor/status and the five separate result digests live in the
strict `factory-readiness-state/v1` sidecar (`.factory-state/readiness.json`),
never in canonical state. A readiness-only campaign publishes the separate
`factory-readiness-result/v2` result and never initializes canonical state.

All readiness inputs are exact commit/tree/config/environment/specification/
plan/contracts/policy/trust/install bindings. Evidence remains attached to its
accepted commit; later campaign HEADs must be descendants and current-product
gates rerun according to invalidation policy. Every role mint freshly reopens
canonical authority; cached readiness output is never authorization. Only the
exclusively locked Campaign can mint a one-use role token bound to exact phase,
role, task, attempt, prompt/tools, provider/model/runtime and current/accepted
commit/tree. Standalone launch is synthetic-only. Missing human authority is
`human_block`, acceptance gaps are `findings`, and unavailable primitives are
`infrastructure_failure`. `--readiness-only` ends at `readiness_complete`,
never campaign `success`.

## Run a finite multi-round campaign

A campaign removes the human-operated outer loop while retaining objective
stopping boundaries:

```bash
"${INSTALL_PREFIX:?verified install}/.factory/bin/factory-campaign" --root "$PWD" run \
  --campaign-id "${CAMPAIGN_ID:?unique id}" --rounds "${ROUNDS:-3}" \
  --branch boilerplate-develop --provider "${PI_PROVIDER:?real provider}" \
  --model "${PI_MODEL:?model}" --backend "${PI2_BACKEND:?immutable pi2 path}" \
  --accepted-commit "${ACCEPTED_COMMIT:?clean HEAD}" \
  --install-manifest "${INSTALL_MANIFEST:?verified manifest}" \
  --campaign-timeout "${CAMPAIGN_TIMEOUT:-21600}" \
  --verification-command ./.factory/tools/verify-boilerplate.sh \
  --acceptance-command ./.factory/tools/verify-boilerplate.sh
python3 .factory/loop/state.py --root "$PWD" show
```

Campaigns are headless and unattended; there is no TUI. Each mandatory round
records the current clean `HEAD` as a new base, runs a fresh planning phase,
runs the resulting plan through implementation attempts, executes the
explicit digest-bound verification and acceptance commands, and validates
installed-functional evidence. Capability and runner acquisition commands run
only when the adopting project's committed configuration declares and supplies
them; an empty generic capability set does not invent a hardware or product
gate.
Immediately before local verification, the campaign opens and retains an immutable descriptor to the binding helper before any untrusted phase, recomputes and compares the tracked config and executable Git blobs, content digests, canonical argv, and secure modes, then executes the exact opened verifier inode through the retained `/proc/self/fd` descriptor; implementation-time replacement, same-size rewrite, writable modes, or binding drift fails before the verifier runs. It then launches an independent adversarial audit as a fresh auditor role. A prior completion claim never shortens the requested
round count. The next round's fresh planner consumes the preceding
`.factory/artifacts/campaign-audit.md`; prior plans and audit reports remain in Git history.

All lifecycle state is persisted atomically in the single
`.factory-state/factory-loop.json` file; the legacy
`.factory-state/ralph-campaign.json` is a frozen deprecated surface. Start a new campaign only from a clean configured development branch. Interrupted campaigns resume from the persisted state by re-running the same `run` command; there is no `--resume`/`--restart` flag.

### Verifier, lock, and runner authority

The trusted campaign, launchers, and state transitions share an inherited
exclusive `flock` on the already-open canonical repository-root directory, so
planning, implementation, verification, audit checkpointing, and recovery
retain one repository writer without a replaceable authority pathname. Model
roles, hooks, gates, verifiers, runners, evidence checkers, tests, and product commands
run only after the orchestrator closes the dynamic repository-root descriptor and unsets all lock metadata before any mutable workspace executable runs, so background descendants retain, unlock, or claim nothing. A separately opened root FD cannot unlock the parent's
open-file description. Migration first acquires any safe legacy `.factory-lock`,
fails if it is busy or ambiguous, then quarantines and validates it before
removal.
Production real-provider roles run only through the authenticated Pi2 adapter:
the external `pi2`, its resolved Node executable, and Pi CLI module are bound by
canonical path, digest, device, and inode and revalidated immediately before
exec. Credentials travel through a sealed anonymous descriptor into the
launch-private mode-0600 store, are detached and revalidated before every tool
call, and never enter prompt, argv, ambient environment, result, or evidence
bytes. Raw Pi fallback and mutable backend substitution are rejected. High-FD
pipes are monitored without `select(2)` limits, and all private homes, staged
executables, sessions, handoffs, and non-persisting caches are removed after
success, timeout, interruption, or authorization failure.

Bounded role retry and stale-state recovery remain inside the campaign.
Provider quota policy is not a model-facing launch surface; the canonical §10
Ollama check/wait mismatch remains explicitly unresolved in the plan rather
than being treated as accepted conformance. The campaign does not retry
arbitrary nonzero leaf or gate results: it returns
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
./.factory/tools/check-factory-environment.py
```

Planning, implementation, and independent audit prompts treat the declaration
as exhaustive. During verification, `.factory/tools/run-factory-runners.py` creates a
history-free `git archive` of the exact clean commit, rejects tracked symlinks,
gitlinks, or special modes that this protocol cannot reproduce safely, sends the
archive through the pinned SSH alias, verifies the extracted Git tree remotely, runs the fixed argv without
reusing a checkout or HOME, and cleans the remote workspace. Local receipts and
bounded logs are written beneath `.factory-state/runner-evidence/` and validated
by `.factory/tools/check-factory-runner-evidence.py`. A failed transport, tree binding,
probe, verifier, cleanup receipt, signer, or evidence digest stops the campaign.

Runner receipts use the generic v3 trust boundary. A restrictive SSH
`ForcedCommand` reaches an unprivileged parser-free trampoline, which can call
only the root broker. The broker issues a bounded, expiring, one-use nonce;
constructs the candidate from the exact clean Git archive; and obtains every
probe, semantic analyzer, executable identity, and resource declaration from a
versioned root-owned external authority. Candidate contracts, checkers, probes,
and analyzers are never executed as authority. Unknown analyzer or resource
IDs fail closed.

Each gate and capability receives immutable candidate source plus separate
bounded home/build/output storage under systemd PID, mount, cgroup, process,
output, and deadline controls. Devices, exact D-Bus proxy calls, host fact
collectors, and dedicated-host requirements are optional per-capability and
default denied. There are no built-in product services or devices. Artifact
bytes are opened once, copied into root-held descriptors, semantically checked
there, and are the same bytes described, signed, and published. Publication is
dirfd-anchored, atomic, and no-replace.

The signer is not an oracle: it requires a broker-only one-shot descriptor
token, independently reloads `factory-runner-policy/v3`, reconstructs
`factory-runner-receipt/v3`, and signs only an exact clean pass. The aggregate
is `factory-runner-aggregate/v4`; retained artifacts are
`factory-runner-artifacts/v1`. Issuance trust and current trust are both checked,
so revocation is immediate and fail-closed. Class/capability lists are bounded
but otherwise arbitrary. The generic repository contains no production keys,
credentials, enrollment, capability, or evidence.

`.factory/config.toml` lists product-specific capabilities required for a clean audit.
Only capabilities covered by accepted exact-commit evidence count; all others
remain findings until their production probes and artifacts are implemented.
Runner provisioning and credentials are maintained outside this repository.

### Task-scoped path-lease foundation (Phase 2C1, LEASE-01)

The generic task-scoped path-lease authority is the strict, foundation-only
mechanism for task-scoped write authority over product-owned verification
surfaces.  The committed `factory-path-lease-policy/v1` config
(`.factory/path-lease-policy.json`) maps closed scope IDs (scripts, nix,
packaging, ci) to bounded repository-relative path prefixes/patterns and
declares absolute non-overridable deny zones: `.factory` security/control
machinery, `.factory-state`, `.git`, the product spec path, credential/key/
env authorities, and all goldens/approval/release/human-authority surfaces.
No carve-out/override field exists in the schema; a human-only override
mechanism is deliberately out of scope and must never be mintable by a
campaign/model.  Deny is dominant over allow, and the policy rejects
absolute/traversal/backslash/control/symlink-ambiguous patterns, duplicate
keys, unsafe globs, and overlapping deny escapes at load.

`factory-task-path-lease/v1` claims are strict DATA minted by the trusted
harness: they bind campaign ID, selected task ID, attempt, exact HEAD
commit, plan digest, policy digest, requested/granted scopes, the exact
deny-dominant expanded paths/patterns, issued/deadline bounds, and a unique
attempt nonce, sealed by a canonical claim digest.  Claims carry no
free-form commands, never resolve symlinks, and are never self-authorizing
(a same-UID workspace JSON cannot grant itself authority).  Replay
prevention fails closed on any campaign/task/attempt/commit/digest mismatch
and on expiry; security-sensitive scopes mark `audit_required` so the
independent audit is mandatory.  The optional plan `Write scopes:` request
field is closed-format and grants nothing by itself — the trusted policy
intersection decides.  Phase 2C1 does NOT wire leases into launch or
confinement behavior; Phase 2C2 binds claims into the existing signed launch
authority with exact no-follow path grants.

### Runtime-launch lease binding (Phase 2C2a, LEASE-01)

The Phase 2C2a runtime-launch foundation binds the claim into the existing
signed launch authority as an optional developer-only extension (no campaign
minting).  The exact canonical claim bytes are re-validated against the
committed policy and the trusted launch context before any prompt byte or
confinement rule is composed, immediately before spawn, and inside the
confined launcher; the claim digest alone is never authoritative — a
real-provider launch without the existing signed HMAC/FD launch token fails
closed.  The exact deny-dominant write candidates are granted as WRITE-only
workspace-confinement rules (no-follow, no symlink/hardlink/mount escape,
existing paths only).  CI/security-sensitive scopes set the immutable
`audit_required` signal on the verified binding and the launch result for
the Phase 2C2b scheduler; the model can never clear it.  Default behavior
without a lease is unchanged.

## Maintain one bug

Ordinary defects stay out of `docs/SPEC.md`. Canonical state is tracked in
`.factory/bugs/open.md` and `.factory/bugs/closed.md`, with optional manual references to GitHub,
Forgejo, or both. Validate and inspect it with `.factory/tools/bug-ledger.py`.

The legacy maintenance loops (`.factory/tools/ralph-maintenance-plan.sh` /
`.factory/tools/ralph-maintenance-run.sh`) are retired and not invocable under
the canonical factory loop: the forwarders are absent from the shipped tooling.
The fresh Python loop's complete role set is planner, developer, tester, and
auditor, so it has no maintenance role. Product defects are triaged by the
human and enter the canonical plan through a planning revision. See
[BUG_WORKFLOW.md](BUG_WORKFLOW.md) for the ledger contract.

## Concurrency

The four static roles are the complete model-role set of the fresh loop and
parallel model launches are forbidden: exactly one role process runs at a
time. The legacy `[concurrency]` block in `.factory/config.toml` is retained
only for policy checks (`mutating_workers = 1`, `integration_workers = 1`,
`allow_worktrees = false`); it does not describe adaptive subroles.

## Quota states

### Guard contract

The hidden standard-library guard `.factory/loop/usage.py` (and fetch child
`.factory/loop/usage_fetch.py`) implements the fixed §10 contract. Immediately
before every model invocation the Campaign runs `--check`; exit 1 or 3 runs a
campaign-deadline-bounded `--wait` followed by one final `--check`. Only final
exit 0 launches a model. Fatal, undocumented, wait, final-check, or signal
failure launches zero models and reaches a finite non-success. The visible
`scripts/ollama-usage-guard.sh` is only the canonical compatibility forwarder;
no quota/cookie parameter or credential enters the model surface. Cookie bytes
reach the fetch child only on a private stdin pipe and the workspace
`.ollama-usage-env` store is metadata-detected only, never a credential
authority.

### Allowed

The session and weekly percentages are below `OLLAMA_THRESHOLD`; the model invocation proceeds.

### Waiting

At or above the threshold, the guard sleeps for `OLLAMA_WAIT_INTERVAL_SECONDS`
and checks again, always capped by the campaign's remaining deadline and poll
bound. SIGINT, SIGTERM, and SIGHUP stop and reap the wait path.

### Transient failure

Network and server failures are retried in wait mode. Single-check mode returns status 3 so supervisors can distinguish them from quota and credential failures.

### Fatal failure

Missing/expired cookies or an unparseable settings page return status 2 and require operator action:

```bash
source .factory/tools/update-ollama-cookies.sh
```

## Quota waiting

The initial ordered registry does not run quota policy. An operator may invoke
the retained diagnostic directly, outside campaign/model launch authority:

```bash
./.factory/tools/ollama-usage-guard.sh --wait
```

It polls until usage resets below threshold; this command is not an automatic
per-model or pre-round campaign hook.

Useful settings in `.ollama-usage-env`:

```bash
OLLAMA_THRESHOLD=80
OLLAMA_WAIT_INTERVAL_SECONDS=300
OLLAMA_WAIT_MAX_SECONDS=0  # unlimited
```

The launch path does not check, wait, or retry quota policy.

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
An ambiguous live process, changed repository identity, changed branch, unsafe
file, stale specification binding, changed plan base, rewound counter, or
invalid state transition fails closed for human/operator inspection. Recovery
never resets Git and never starts a second writer.

The frozen legacy recovery path (`.factory/tools/ralph-recover.sh`) is retired
and not invocable under the canonical factory loop: the forwarder is absent from
the shipped tooling and the `FACTORY_RALPH_FREEZE_OVERRIDE=1` escape no longer
applies to a new launch.

## Specification changes

Never edit the specification during implementation. `check-plan-freshness.sh` compares both the latest spec commit and the exact Git blob against plan metadata. If they differ:

1. stop the campaign;
2. commit the revised canonical specification;
3. start a new campaign from a clean tree so the fresh planning phase replaces the plan and leaves the completed plan only in Git history;
4. inspect the replacement plan and confirm it contains only current pending gaps;
5. start a new implementation phase.

## Documentation gate

Every implementation plan ends with **Final documentation and specification audit**. `.factory/tools/validate-implementation-plan.py` requires the plan to contain a conformance matrix, interaction inventory, canonical task statuses, and a final audit depending on every other task. At implementation completion it rejects unfinished tasks and any matrix classification other than `verified`. The final gate also validates bug ledgers, rejects unresolved open bugs, runs project verification, and then requires commit-bound `test_installed_functional` evidence with zero skips. `.factory/tools/verify-project.sh` is **adopting-product only**: the generic boilerplate ships no product build and no `.factory/tools/verify-project.sh` of its own (`final-gate.sh` runs it only when an adopting product provides it). When an adopting product supplies it, it writes the local evidence only after that product's mandatory test and packaging gates pass; `.factory/tools/check-installed-functional-evidence.sh` invalidates it if production or acceptance inputs changed afterward. This prevents mocked or proxy-only coverage, fixture assembly without production dispatch, backend-less skips, or optional smoke skips from satisfying installed production behavior.

Read-only reviewers compare source, tests, configuration, README, operations, and the specification, specifically looking for tests that bypass production initialization/event dispatch or assert pixels without semantic behavior. The sole writer corrects documentation and runs final verification. If review finds a gap, the next planner appends remediation and the campaign continues; completion is forbidden until the complete definition of done passes.

## Harness footprint and packaging isolation

The harness is intentionally invisible to the adopting product: every
committed harness file lives under the hidden `.factory/` namespace, every
runtime artifact (state, locks, logs, receipts) lives under the ignored
`.factory-state/` namespace created mode 0700, and the Pi role definitions
live under `.pi/`.  The product root receives no new visible harness file,
and product source/test/packaging/build discovery never traverses the
hidden namespaces — a generated project can delete `.factory/`,
`.factory-state/`, and `.pi/` and lose nothing but the harness.

`.factory/loop/footprint.py` is the deterministic inventory authority behind
this boundary (HIDE-01, §3; Task 13). It enumerates every harness-installed
or harness-generated path (tracked content, present-on-disk content, and
external-prefix installs) and fails closed when a path escapes the allowed
namespaces or the trusted external executable prefix. It rejects absolute,
traversal, and control-character paths; case-fold, Unicode-normalization,
and trailing-dot/space namespace escapes (`.Factory/`, NFKC fullwidth
aliasing, `.factory.`/`.factory ` — Windows/macOS strip trailing dots and
spaces when creating entries) in *every* product-tree segment — tracked
and on disk — not only the first; root harness marker basenames (legacy
root factory files *and* harness config artifacts like
`factory-loop.json`) at the product root or at any depth; symlink and
gitlink modes on any
`.factory/`, `.pi/`, or `.ralph/` tracked entry; symlink escapes inside the
hidden namespaces (the legacy `.ralph/` namespace is scanned on disk too)
and product-side symlinks into the harness; hardlink aliasing between a
harness inode and a product path; FIFOs, sockets, and device nodes beneath
the hidden namespaces; entries whose device differs from the repository
root (a hidden namespace or `.ralph/` crossing a mount point); tracked
`.factory-state/` runtime state; `.factory-state/` that is not exactly mode
0700 and owned by the invoking user (the ownership check is never skipped
for root — root fails closed earlier through the pinned-Git resolver);
legacy root harness files (`PROMPT.md`, `IMPLEMENTATION_PLAN.md`,
`factory.toml`, ...); harness config artifacts and hidden-namespace entries
inside product source/test/packaging/build trees; and product install
prefixes that receive hidden-namespace content, harness markers, symlinks,
special inodes, or mount crossings at *any* depth of the staged tree. The
pinned external executables (the Git boundary binary) must resolve under
exactly the supported immutable executable roots — `/usr/bin`, `/bin`,
`/sbin`, `/run/current-system`, or the Nix store — never `/etc`, `/lib`,
or `/lib64` (configuration and shared libraries are not executable roots
and no pinned candidate ever lives there).

Removal and discovery are fail-closed too: `remove_harness()` verifies
every namespace root and descendant against the repository's device before
deleting anything, and refuses entirely when a root or descendant crosses a
filesystem/mount boundary; on-disk product discovery is derived from the
pinned `git ls-files -co --exclude-standard -z` set, so git-ignored
credentials, locks, caches, logs, and build artifacts can never reach a
packaging glob; and an external-prefix harness install must carry exactly
its operator manifest (no omitted manifest file, no extra product file or
symlink) plus only explicitly listed trusted executable entrypoints, under
a real (never symlinked) prefix that is outside the *resolved* product
tree (containment compares resolved paths, so a root or prefix reached
through a symlinked ancestor cannot hide an inside-the-tree prefix).

One `remove_harness()` limitation is accepted by design and deferred: the
function is a trusted-operator tool, and between its pre-deletion device
verification and the actual deletion a *privileged* attacker who can
bind-mount a filesystem over a harness namespace could still cause the
removal to cross into the mounted tree — a check-to-use (TOCTOU) window
that no ordinary permission walk closes without a descriptor-anchored
deletion primitive. That window requires root/bind-mount privileges the
operator already trusts, so it is recorded here and assigned to Task 16; an
invasive descriptor-anchored deletion rewrite is deliberately out of scope
for Task 13, and the harness keeps the documented fail-closed device
pre-check.

Two gates enforce the inventory:

```bash
./.factory/tests/test-factory-footprint.sh
python3 .factory/loop/footprint.py --root "$PWD" --json
```

The harness-owned suite `.factory/tests/test-factory-footprint.py`
plus its driver `.factory/tests/test-factory-footprint.sh` prove every escape
class with committed fixture repositories: containment of the tracked
harness, tracked-`.factory-state` rejection, role-def-only `.pi/`, legacy
`.ralph/` frozen to its migration allowlist (tracked and on disk),
symlink/gitlink modes across all three namespaces and legacy, root
forbidden files and harness config-artifact markers at the product root or
at any depth, case/Unicode/trailing-dot-and-space escapes in nested product
segments, symlink/hardlink escapes, special-inode rejection, simulated
mount/device crossings, exact-0700 and ownership of `.factory-state`,
discovery exclusion (including git-ignored secrets, build artifacts, root
harness markers, and namespace aliases), external-prefix manifest
completeness, symlinked-prefix and resolved-containment rejection, and
entrypoint hygiene, recursive product-prefix contamination (nested hidden
namespaces, markers, symlinks, special inodes, mount crossings), the
narrowed trusted executable roots (no `/etc`, `/lib`, `/lib64`), and
deletion of the hidden namespaces without touching a
single product byte. `verify-boilerplate.sh` runs the packaging gate; the
generic-leak gate (`.factory/tools/check-generic-leakage.sh`) remains the
product-neutrality scan.

The check is read-only and deterministic: it never writes to the
repository, never touches `.ralph/`, and creates no branch or worktree. It
matters because a harness file that leaks into the product tree — even an
ignored one — can be packaged, installed, or globbed by product discovery
and shipped to end users, which is exactly the isolation §3 forbids.

## Machine-readable acceptance evidence

Proxy evidence must not be promoted to production verification. Three tracked artifacts make acceptance machine-checked:

- `.factory/artifacts/conformance.json` (schema `ralph-conformance/v1`) is the only authority for `verified` claims. Each requirement row declares classification (`verified`/`partial`/`missing`/`ambiguous`/`blocked`/`not_applicable`), evidence tier (`unit`/`simulated`/`private_integration`/`installed`/`real_system`/`human`), required capabilities, the exact evidence commit, and receipt/artifact refs. `.factory/tools/validate-conformance.py planning|complete` checks the schema, cross-checks the plan matrix, and rejects `verified` rows that are below the normative required tier, unevidenced, or backed by an undeclared capability. `blocked` and `partial` rows always fail implementation completion; `not_applicable` requires a spec-scoped reason.
- `.factory/capability-contracts.json` (schema `ralph-capability-contract/v1`) defines one probe per declared/required capability: probe argv, must-execute marker, must-not-skip tokens, and deny-simulated markers. `.factory/tools/check-capability-contracts.py` rejects contracts for undeclared capabilities and declared capabilities without contracts; `.factory/tools/check-capability-evidence.py` requires a fresh exact-commit runner receipt whose probe section executed (no skip) and shows no simulated marker. Missing contract, probe, or receipt is unevidenced and never auto-reclassified. The generic repository keeps an empty contract instance; per-product contracts belong in the product repository.
- Audit reports must cite machine receipts: coordinator-executed commands are wrapped by `.factory/tools/machine-receipt.py --tag <tag> -- <argv...>` and recorded under `.factory-state/audit-receipts/`. `.factory/tools/check-audit-receipts.py` requires every executable-evidence line to carry PASS/FAIL/BLOCKED plus a `[receipt: ...]`/`[manifest: ...]` reference, PASS requires exit 0, and any BLOCKED evidence forces `result: findings`. Subagent prose cannot certify runtime.

  **Bounded supervision.** The receipt wrapper runs each command in a new
  session under the same bounded supervision contract as the launch
  authorities: a child subreaper, an identity-pinned baseline snapshot, a
  captured descendant scope, bounded pipe drains, and a bounded timeout.  On
  leader exit (or timeout/overflow) it keeps monitoring the original process
  group and every descendant reparented to it
  (`/proc/self/task/<tid>/children`) until the stdout/stderr pipes EOF **and**
  no owned descendant remains for a bounded stabilization window — so an
  escaped `setsid`/double-fork descendant that races reparenting, or holds the
  pipes open after the leader exits, is still detected and terminated.  Every
  owned PID is starttime-pinned: a reused PID is never signaled and
  baseline/foreign processes are never touched.  On timeout, overflow, or
  leader exit with descendants the wrapper TERM -> KILLs the original
  group **per-PID by starttime identity — never by the numeric group id
  (`killpg`)** (so a group id reused by a foreign process after the leader
  is reaped can never signal the foreign group), plus every
  identity-pinned escaped PID, then bounded-reaps all; a member forked
  during the grace is captured by the repeated `/proc` scan and killed
  while a pinned member still lives.  Any
  escaped descendant observed — even one cleaned up — fails the receipt with
  the exact PIDs named.  An overflow keeps a bounded, secret-free diagnostic
  on stderr and exits nonzero; a truncated or timed-out run is never recorded.

  **Receipt coverage is exactly the asserted command.** A machine receipt
  certifies only the concretely asserted argv it recorded (`argv == the
  allowlisted command`, exit 0, byte digests) — never other paths, other
  products, or prose claims in the same row.  A runtime receipt
  (`.factory-state/audit-receipts/<tag>.json`) covers only the command it
  ran, and each conformance row must carry its **own** receipt/artifact refs:
  a private/unit-tier row is never satisfied by an installed receipt for a
  different command, and an installed receipt is never read as evidence for
  a path outside its argv.  In particular, the `installed-harness-smoke`
  receipt certifies exactly `./.factory/tests/test-factory-installed.sh` —
  the installed harness suite — and nothing else.

  **Eligibility is machine-checked, not self-declared.** No conformance row
  is eligible for `verified` unless the evidence behind it actually ran at
  the required tier with its own exact-commit refs: VIS-01 stays unverified
  unless the visual provenance machinery was actually run (the boilerplate
  ships the visual-audit scaffold **disabled by default** with no vision
  model configured — a scaffold, not a run), RUNNER-01 stays `blocked`
  until a declared, provisioned, signed hardware runner produces an accepted
  exact-commit manifest (Task 24, FACT-020), and ACCEPT-01 stays `missing`
  until the final audit verifies every row.  A scaffold, a declaration, or a
  private/unit test run is never elevated to an installed/realtime tier.

Pixel/offscreen framebuffer checks are not real visual acceptance, private/session-scoped service instances are not the real system service, a synthetic test producer is not the target consumer, and an evidence declaration is not evidence. `final-gate.sh` `--implementation` and `--campaign-audit` run all three layers; `--planning` validates an existing sidecar so a fresh cycle stays pendable before migration.

## Installed-tier evidence for the generic harness (Task 20)

The `installed` evidence tier proves the *installed harness itself*: a clean
exact-commit copy of the generic harness is staged into test-owned external
and hidden prefixes and its production control-plane CLIs are executed from
that copy — never relabeled source-tree or private-unit runs, and never
`real_system`/`human`/product evidence. No external model, runner,
hardware, or human is invoked.

`.factory/loop/installer.py` is the trusted installer/stager. It stages
**committed harness content** — every file tracked under the hidden
`.factory/` and `.pi/` namespaces at the exact bound commit — from the exact
committed blob bytes through the pinned Git boundary (a batched
`git cat-file --batch` with a hard-capped, fairly drained capture, then a
batched `git hash-object --no-filters --stdin-paths` blob-exactness proof so
no content filter can rewrite the hashed bytes), preserves executable modes
(`100755 -> 0755`, `100644 -> 0644`), creates every installed directory as
private mode 0700, refuses symlink/gitlink/special-inode/device entries,
unsafe paths, secret/credential-looking path names (committed content
included — a committed tree is not an exemption), group/other-writable
modes, and any pre-existing or in-repository prefix, and writes a
machine-readable manifest (`factory-install-manifest/v1`) recording per
file: path, mode, size, sha256, the committed blob id, the pending flag,
and the exact root/prefix the installer bound (the manifest is written
outside the repository and never replaces an existing file).

The prefix is **descriptor-anchored**: it is created and opened with
`O_DIRECTORY|O_NOFOLLOW|O_CLOEXEC`, its `(st_dev, st_ino)` identity is
recorded from the anchored fd, the named path and the resolved containment
are verified against that identity *before* the private-0700 mode is pinned
with `fchmod` (never a pathname chmod), and every staged directory/file is
created through `openat` dirfd chains with `O_NOFOLLOW` (every created
directory private 0700) — no pathname write is ever performed.  The root
fd identity and containment are revalidated before every staged write and
before the post-staging manifest verify, so a prefix path swapped to a
symlink or bind-mount into the repository fails closed *before* any
repository write; the fds are closed on every exit path.  On any failure
after the prefix is created, the created prefix is rolled back
identity-safely (the exact directory inode recorded at creation is
removed).  Every malformed batched-Git transcript (a non-numeric
`cat-file` size, truncated record framing, non-UTF-8 text, a malformed
`hash-object` oid) fails closed as an `InstallerError`, never as a raw
parser exception.

Task-20-era additions that are not yet part of the bound commit are staged
from the working tree only when they appear on the **exact reviewer
allowlist** (`PENDING_ALLOWLIST` in the installer — the known Task-20
authorities) and live under the installed surface; any other pending path
under `.factory/`/`.pi/` fails closed, a visible-`.factory/tools/` worktree change
that is not a declared shared authority/entrypoint is never staged, and a
secret-named path is never staged.  They are recorded in the manifest with
`pending: true` so a reviewer sees exactly which installed bytes are newer
than the bound commit.  The declared shared authorities and operator
entrypoints must live under the allowlisted first segments
(`.factory`/`.pi`/`scripts`) and are staged **exactly once** — they are
excluded from the bulk committed staging so a clean committed install can
never double-stage an entrypoint.

The installed copy carries the shared authority the control plane loads at
runtime (`.factory/tools/factory_state_io.py`), the operator receipt wrapper
(`.factory/tools/machine-receipt.py`), and the external-prefix launcher entry
point `.factory/bin/factory-launch` (which imports the installed package
under the public name `factory` through a private alias directory and
forwards every argument to `python -m factory.loop.launch`).  The launcher
forwards INT/TERM/HUP/QUIT to the child, then **bounded-waits and reaps the
child before any cleanup**: a child that exits on the forwarded signal is
reaped with its actual status, a child that ignores the signal is escalated
to SIGKILL after a bounded grace, and the operator alias directory is
removed only after the child is truly gone — on every exit path (normal
completion, a signal, or an early failure) — while the child's actual exit
status is preserved, including the 128+signal convention.

`.factory/loop/gitutil.py`'s bounded capture drives stdin writes and the
stdout/stderr drains through **one fair select event loop** against one
shared deadline: a batched child whose request payload *and* transcript
both exceed a pipe buffer keeps making progress instead of deadlocking
into a spurious timeout, an over-bound stream fails closed, EPIPE/EOF end
the affected side, and a wedged capture terminates and reaps the child's
entire process group (no zombie).

`.factory/loop/footprint.py` gains the installed physical-file inventory:
`--installed-inventory PREFIX --manifest MANIFEST` captures every
installed file's path, mode, owner, link count, size, and sha256, asserts
the exact physical set equals the manifest + shared authorities + operator
entrypoints (never a product/foreign namespace — no product pollution),
rejects symlink/special-inode/device/mount crossings, foreign owners, and
hardlink aliases, and cross-checks every digest against the manifest.

```bash
python3 .factory/loop/installer.py install \
  --root "$PWD" --commit "$(git rev-parse HEAD)" \
  --prefix "$TMPDIR/external-prefix" --manifest-out "$TMPDIR/manifest.json"
python3 .factory/loop/footprint.py --root "$PWD" \
  --installed-inventory "$TMPDIR/external-prefix" --manifest "$TMPDIR/manifest.json"
./.factory/tests/test-factory-installed.sh
```

The harness-owned suite `.factory/tests/test-factory-installed.py` plus its
driver `.factory/tests/test-factory-installed.sh` build both the external
and the hidden dot-prefixed install, run the production CLIs from the
installed copy (launch help and excerpt, the external-prefix launcher entry
point, `factory-campaign`, the parser/selector/state CLIs, the receipt
wrapper, and the installed footprint inventory) under a sanitized
environment with no source-tree path and no `.factory-state`/credential/
legacy/Git/Ollama/campaign-binding surface, and mint every installed-tier
gate as an exact-commit machine receipt bound to the audit coordinator in
the fixture authority so `.factory/tools/check-audit-receipts.py` exits 0.  Every
module-form gate is wrapped in an **installed-root attestation**: the
certified argv/stdout bind the resolved module root of the installed
prefix, and a source-tree invocation resolves a different root and can
never mint an equivalent receipt.  A gate that exits nonzero or prints a
skip marker is never PASS.  The suite also proves the launcher's signal
contract (a delayed-termination child keeps the alias until it exits, the
child's actual or 128+signal status is preserved, and a signal-ignoring
child is KILLed within the bounded grace) and the installer's adversarial
prefix-swap rejection (a symlink/bind-mount swap of the prefix path fails
closed before any repository write).  The suite never mutates the live
`.factory-state/`: the driver snapshots every foreign runtime file's
digest, mode, and mtime plus the tracked/untracked Git state and the
ignored/bytecode inventory before and after and proves byte-for-byte
preservation.  A clean-commit simulation (the whole installed surface
committed in a fixture authority) proves the post-commit install never
double-stages a declared entrypoint and reports an empty pending set,
which catches the H1 regression before the real Task-20 commit lands.

Task 20 is **complete at exact commit `6b9c626`** as installed-harness
mechanics with fixture-authority receipts only: the installed suite builds
and proves the installed-tier machinery against the fixture authority, and
the live `.factory-state/` foreign evidence is never touched, replaced, or
relabeled.  The **live** installed-functional evidence — the fresh
generic-namespace evidence, the coordinator receipts at the audit base, and
the check-installed acceptance — is owned by pending Task 23 after Task 22
runs the live campaign; Task 20 itself never stages live evidence.

## Generic evidence-scope authority for foreign artifacts (Task 23)

The **live** installed-tier evidence for the generic harness is staged by a
trusted two-stage publisher — `.factory/loop/generic_evidence.py` with the
operator entrypoint `.factory/bin/publish-generic-evidence` — under a
dedicated generic evidence namespace `.factory-state/generic-evidence/<exact-commit>/`
(private 0700 directories, mode-0600 single-link no-replace artifacts).
The publisher is deterministic control-plane code: no model, runner,
hardware, or human is invoked, exactly one writer holds the exclusive
root-descriptor lock, and a failed or skipped suite leaves no artifacts.

- `prepare` (read-only stage): validates the `factory-state/v1` control
  state (hardened `state.load_state`) and the exact clean HEAD (strict
  40-hex, empty `git status --porcelain`), verifies the generic namespace,
  the audit coordinator path, and the receipt paths do not preexist,
  snapshots every pre-existing `.factory-state` file's bytes digest / mode /
  mtime, and writes a staging record (commit, control-state round, fresh
  nonce, state digest, before-snapshot) into a test-owned 0700 staging
  directory.  No `.factory-state` byte is written by `prepare`.
- `publish` (write stage): re-validates the exact clean HEAD and the staged
  binding, re-proves the before-snapshot still matches the live
  `.factory-state`, runs the installed harness suite
  (`./.factory/tests/test-factory-installed.sh`) with a sanitized bounded
  environment (exit 0, no skip marker, bounded transcript, clean tree after),
  and only then bridges the audit coordinator from the validated
  control state and the exact HEAD
  (`.factory-state/audit-coordinator.json`, no-overwrite, mode 0600),
  mints the installed-harness machine receipt through the trusted
  `.factory/tools/machine-receipt.py` authority under the allowlisted
  `installed-harness-smoke` category
  (`.factory/campaign-receipt-policy.json` argv
  `[./.factory/tests/test-factory-installed.sh]`), and only after the
  receipt is accepted publishes the installed-functional evidence record
  (schema `factory-generic-installed-functional/v1`) binding the receipt
  reference, the receipt byte digest, the exact commit, and the coordinator
  round/nonce, plus a preservation proof
  (`factory-generic-preservation/v1`) showing every pre-existing
  `.factory-state` file is byte/mode/mtime identical and the only additions
  are the new generic namespace, the coordinator path, and the receipt
  paths.

`.factory/tools/check-installed-functional-evidence.sh` is Task 23 scoped: by
default (boilerplate mode) it scans every hardened child of the dedicated
generic evidence root `.factory-state/generic-evidence/` and accepts the
**unique valid namespace** bound to the **exact** live audit coordinator
base — the strict invariant
``coordinator.base_commit == receipt.evidence_commit == record.commit ==
namespace name``.  The evidence commit must also be an ancestor of (or
equal to) the final HEAD and bind a
matching installed-harness receipt and the live audit coordinator (round
and nonce exactly), and have **every implementation/acceptance
authority path unchanged** between the evidence commit and the final HEAD
(the checker authority,
`generic_evidence.py`/`evidence.py`/`gitutil.py`/`state.py`,
`publish-generic-evidence`, the receipt policy, the installed suite, and
`machine-receipt.py`).  Later plan/conformance/audit **metadata** commits
are allowed; a changed authority path is stale and fails closed.  The
single live coordinator base can equal only one namespace name, so two
candidate paths can never both be valid under one coordinator: a planted
descendant, incomparable, or foreign namespace (a self-consistent record
at any commit other than the coordinator base) is cross-audit evidence,
is excluded from the candidate set before selection, and can never shadow
the unique valid namespace (a forged or stale namespace is excluded the
same way).  The legacy
foreign root file
`.factory-state/installed-functional-evidence.env` is never read (an
adopting-product artifact that is ignored and never rewritten).  `--namespace
PATH` selects an explicit safe-mode namespace for fixture authorities
(missing value, `..`/empty-component traversal, a symlink parent, or an
absolute path outside the repository root fail closed); even then the
record must be bound to a commit that is an ancestor of the final HEAD.
Accepting the generic evidence requires a **matching installed-harness
receipt**: the record binds the receipt reference, receipt digest, exact
commit, and coordinator round/nonce; the receipt must pass the hardened
hidden evidence validation (no-follow owner/mode/link-count/inode
identity, argv/digest/coordinator bindings), exit 0, carry exactly one of
the allowlisted `installed-harness-smoke` argv arrays, and its certified
suite stdout transcript must carry no skip marker.  The production/acceptance
inputs must be unchanged since the tested commit (clean working tree).

**Crash recovery (SIGKILL windows).** Every canonical write is atomic
no-replace and a crashed publication resumes deterministically on the next
`publish` with the same staging record.  The installed-harness receipt set
resumes only when it is complete and byte-exact to the staged artifacts (a
torn set fails closed with no deletion); the authorizing coordinator is
reused, never overwritten.  A canonical empty/partial evidence namespace is
finalized only when the exact staging record + the reused coordinator + the
validated receipt + the expected record bytes all match; a tampered or
foreign partial fails closed with **no deletion**.  A fresh namespace is
published as a privately complete 0700 temp namespace (record fsynced
inside) moved onto the canonical name with Linux `renameat2`
`RENAME_NOREPLACE`, so the namespace appears atomically and can never
clobber an existing one; the publisher's own `.partial-*` temp namespace
left by a SIGKILL is validated and resumed, or fails closed.  After the
installed suite the `.factory-state` snapshot must be **fully unchanged** —
no additions, mutations, or deletions at all — before the first
coordinator/receipt/evidence canonical write, so a hostile passing suite
that leaves an extra file can never produce canonical artifacts.

The hidden suite `.factory/tests/test-factory-generic-evidence.py` plus its
driver `.factory/tests/test-factory-generic-evidence.sh` prove in
test-owned fixture repositories that the foreign old root env is ignored
and preserved byte/mode/mtime-identically, symlink/hardlink/mode/commit/
receipt tampering fails closed, duplicate publication fails closed, stale
generic namespaces are never read, failed/skipped suites leave no
artifacts, and the real committed installed suite runs end-to-end at the
exact fixture commit with the checker accepting the publication.

## Machine visual-audit scaffold

A product-neutral, optional machine visual-audit framework ships in the
boilerplate as a scaffold (`.factory/visual-audit.toml`, the
`.factory/tools/visual-audit-*.py/.sh` and `.factory/tools/check-visual-audit.py` tools,
`.factory/tests/legacy/test-visual-audit.sh`, the review schema, and the frozen review prompt).
It is **disabled by default** and defaults **no vision model**: `vision_model`
is a consumer-configured placeholder that stays empty until a consumer sets it,
and `.factory/tools/visual-audit-probe.sh` fails closed unless the consumer configures
`VISUAL_AUDIT_VISION_MODEL`. The generic capture adapter
(`.factory/tools/visual-capture-driver.sh`) also fails closed with a clear message
until the consumer implements installed exact-commit capture; tests use only
explicit test-only mock drivers.

All mutable capture/review/calibration/probe state lives under the ignored
`.factory-state/visual-audit/` directory; nothing mutable is tracked. When a
consumer enables the framework it must first implement an installed
exact-commit capture driver, replace the placeholder inventory/calibration
templates with its own visual states, and prove a real non-skipping image
round-trip through `.factory/tools/visual-audit-probe.sh`.

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
  outage, and shared-session races fail closed (`.factory/tools/check-visual-audit.py`
  is the aggregate gate). A receipt proves invocation, not visual truth.

Machine vision is **supplemental falsification/findings-only**, never a
certification oracle:

- A machine PASS cannot certify visual truth, and it cannot elevate
  unit/simulated/private-integration/installed evidence to
  `real_system`/`human`. A clean review only falsifies nothing; it confirms
  nothing about real system behavior.
- Captures and review reports do **not** replace adopter-required physical,
  external-observer, or human evidence. A screenshot is not a compositor
  observation, a framebuffer grab is not a physical display, a synthetic
  producer is not an independently enrolled observer, and no machine report substitutes for
  a human judgement call.
- Machine-generated baselines cannot self-certify goldens: a baseline is not
  independent ground truth, and human/golden acceptance remains out-of-band
  (a separate, human-reviewed artifact and decision).
- When enabled, the completion gate validates an existing exact-commit report
  only; it never invokes the capture driver or vision model under the
  lifecycle lock. Gate validation reads a stored report and does not run any
  machine vision at completion time.

## Credential boundary guard

`.factory/tools/credential-guard.py` and the project-local Pi extension enforce a
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
./.factory/tools/verify-boilerplate.sh
```

The verifier checks shell syntax, ShellCheck when available, TOML/JSON configuration, read-only agent tools, single-writer settings, quota behavior, plan freshness, branch policy, removed product artifacts, and secret tracking.

## Release

After the campaign reports a terminal outcome, review the configured development branch. Release manually:

```bash
git switch main
git merge --no-ff <development-branch>
git tag vX.Y.Z
```

For the next release, update the same canonical specification
`docs/FACTORY-LOOP-SPEC.md` in a dedicated commit, run a new planning loop,
and execute a new implementation loop. Git retains prior specifications and
plans; the adopting-product placeholder is never the release update target.