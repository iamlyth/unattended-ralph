# Minimal Fresh-Context Software Factory Specification

Status: draft canonical specification for the boilerplate redesign

## 1. Purpose

This specification defines a small, secure, unattended software-factory loop based on the Ralph Wiggum method without using Ralph Orchestrator as the control plane.

The factory MUST preserve the useful properties of the simple loop:

- one canonical specification;
- one canonical implementation plan;
- one repository writer;
- one deterministically selected task per implementation attempt;
- a fresh model context for every attempt;
- deterministic verification outside the model;
- independent testing and audit;
- bounded execution and honest non-success when acceptance remains unresolved.

Security, evidence, runner, visual, and human-acceptance controls remain mandatory. They MUST operate at trust boundaries and MUST NOT become competing task or memory authorities.

## 2. Non-goals

The factory is not:

- a conversational agent with durable semantic memory;
- a distributed multi-writer scheduler;
- a replacement for the product specification;
- an authority for promoting evidence tiers;
- an autonomous release or `main`-branch promotion system;
- a mechanism for treating unavailable hardware or human review as passing.

## 3. Repository footprint and harness isolation

The generic implementation MUST be developed and verified only in the dedicated boilerplate repository on `boilerplate-develop`. Controller-Box remains on `develop` and MUST NOT be used as the build or test workspace for this redesign. Porting occurs only after the generic implementation, adversarial tests, and independent audit pass.

The harness MUST remain visually and operationally out of the product's way:

- committed harness configuration, prompts, implementation, and harness-only tests live under the hidden `.factory/` namespace;
- runtime state, locks, temporary files, logs, and receipts live under ignored `.factory-state/`, created mode 0700;
- Pi role definitions MAY live under hidden `.pi/` when the Pi runtime requires that location;
- no `.ralph/` directory, Ralph runtime task ledger, Ralph memory store, event stream, or loop lock is created by the new system;
- no harness Python package, shell library, generated report, or test fixture is placed in the product source, product test, packaging, or build directories;
- the product repository root receives no new visible harness files or directories;
- one small command installed outside the product tree, or one hidden `.factory/bin/` entrypoint, is the operator interface;
- product build systems and packaging discovery MUST exclude `.factory/`, `.factory-state/`, and `.pi/`;
- generated projects MUST be able to remove the harness by deleting the hidden factory namespaces without deleting product source or product tests.

During migration, existing visible `scripts/ralph-*` compatibility entrypoints MAY remain only as deprecated forwarders. The completed redesign MUST NOT require them, and new orchestration implementation MUST NOT be added to the visible product `scripts/` directory.

A conformance test MUST inventory every file installed by the harness and fail if a harness-owned path escapes the allowed hidden namespaces or the external executable installation prefix.

## 4. Implementation language

The control plane SHOULD be implemented in Python 3.11 or newer using only the standard library for its core orchestration.

Reasons:

- the boilerplate already uses Python for state validation, secure file handling, receipts, and tests;
- Python provides direct process-group, signal, `fcntl`, `dir_fd`, hashing, JSON, and atomic-file APIs;
- it is substantially easier to audit and modify than a large shell state machine;
- it avoids introducing a Rust or Go build toolchain into every generated project.

POSIX shell MUST be limited to small operator entrypoints and environment activation. Node.js MAY remain where required by the Pi SDK or existing credential-boundary extension. Rust is not required for the first implementation. A later single-binary rewrite MAY be considered only after the Python behavior and conformance suite are stable.

## 5. Sources of truth

### 5.1 Authoritative model inputs

Every role receives a fresh context containing only:

1. its committed, static role prompt;
2. the concise operational policy in `AGENTS.md`;
3. the canonical product specification, normally `docs/SPEC.md`;
4. the canonical implementation plan;
5. the current repository code and tests at the bound Git state;
6. for implementation only, the exact selected task copied verbatim from the plan.

Git status, Git diff, and deterministic command output observed during the attempt are part of the current repository observation, not durable model memory.

The model workspace MUST enforce this boundary, not merely describe it in the prompt. `.ralph/`, `.factory-state/`, prior scratch/handoff files, runtime task or memory stores, and legacy campaign/event state MUST be unavailable through model tools. The current specification, plan, code, tests, and role-allowed `.factory/` implementation are readable. Role-specific write permissions are allowlisted. Migration archives remain outside the model-visible workspace. A model-selected read of Git history MAY inspect historical product code when needed for diagnosis, but historical plans, conversations, memories, and lifecycle artifacts MUST NOT be automatically injected or treated as current authority.

### 5.2 Forbidden context authorities

The factory MUST NOT inject:

- a runtime task queue independent of the implementation plan;
- model memories from previous attempts or sessions;
- prior conversations;
- accumulated scratchpad prose;
- supervisor-authored recovery narratives;
- persisted context summaries;
- bug, fact, conformance, or audit prose duplicated outside the plan as task-selection input;
- completion claims from a previous model.

Evidence ledgers and receipts MAY exist for deterministic verification, but they are not task or memory authorities. A role accesses them only through the committed plan or a deterministic checker it runs during its own attempt.

### 5.3 Canonical task authority

The implementation plan is the sole task ledger. Bugs, test findings, audit findings, and newly discovered work MUST become plan tasks through a planning revision. They MUST NOT create an independent campaign-level task queue.

## 6. Roles and hats

Each role runs in a separate, fresh process and receives a separate static prompt.

### 6.1 Planner

Inputs:

- planner prompt;
- `AGENTS.md`;
- product specification;
- current implementation plan, if one exists;
- current code and tests.

Responsibilities:

- inspect rather than assume missing behavior;
- create or revise the canonical plan;
- translate verified findings into bounded tasks;
- define dependencies and acceptance checks;
- preserve unresolved external requirements as explicit findings;
- never modify product code or the product specification.

The planner is the only role that creates, removes, splits, or reorders plan tasks. A fixture-backed planning check MUST prove that existing behavior is discovered rather than duplicated as new work.

### 6.2 Developer

Inputs:

- developer prompt;
- `AGENTS.md`;
- product specification;
- canonical plan;
- current code and tests;
- one deterministically selected plan task.

Responsibilities:

- implement only the selected task;
- perform its own bounded investigation within the selected task;
- run focused backpressure;
- update that task's plan status and evidence;
- commit one coherent checkpoint;
- never claim final product acceptance.

There is exactly one repository writer. The four factory roles are the complete model-role set for the first implementation; adaptive specialist subagents are not part of the control plane. Parallel model launches are forbidden.

### 6.3 Tester

Inputs:

- tester prompt;
- `AGENTS.md`;
- product specification;
- canonical plan;
- exact committed code and tests.

Responsibilities:

- remain independent from developer reasoning and memory;
- run deterministic focused and project verification;
- inspect installed and production paths required by the specification;
- produce exact command receipts and concrete findings;
- never edit product code;
- never elevate unavailable evidence.

### 6.4 Auditor

The auditor MUST be a separate fresh process with a distinct static prompt and no tester or developer conversation. It receives the same authoritative inputs at an exact commit. Audit objectives MAY rotate by round, but the objective list MUST be committed, digest-bound at campaign start, immutable during a campaign, product-neutral, and selected deterministically from the round number. The selected objective is the only additional role input permitted beyond §5.1.

Audit objectives are falsification lenses, not implementation tasks or project memory. Audit findings feed the next planner revision.

## 7. Plan contract

The canonical plan is Markdown conforming to committed schema `factory-plan/v1` and a deterministic parser. It MUST bind to:

- the exact specification path, commit, and blob digest;
- the cycle base commit;
- a unique task identifier for every task;
- task priority, status, dependencies, scope, acceptance checks, and documentation impact;
- the stable specification requirement IDs covered by every task;
- the interaction inventory affected by every task;
- the conformance classification and evidence references for every normative requirement.

The parser accepts only one documented heading/field grammar, rejects duplicate keys/IDs, unknown lifecycle states, ambiguous task sections, and out-of-order or cyclic dependencies, and round-trips without semantic loss. The committed schema and parser are part of the acceptance boundary; free-form prose cannot alter lifecycle fields.

Allowed task states and transitions are:

- `pending -> in_progress` by the trusted selector at developer launch;
- `in_progress -> complete` only after the task checkpoint and acceptance checks validate;
- `in_progress -> blocked` only with an exact unresolved requirement/fact reference;
- `blocked -> pending` only in a new planner commit after the blocker changed;
- `in_progress -> pending` only through explicit interruption recovery that preserves work and records no completion claim.

No other transition is allowed. Exactly zero or one task may be `in_progress`; `complete` is write-once within a campaign unless a later tester/auditor finding causes a planner to create a new remediation task rather than reopening history.

A `blocked` task MUST name the unresolved fact or required human/external evidence. Blocked tasks do not become passing merely because no model can execute them.

Newly discovered work MUST be recorded by the next planner revision. A developer MAY record a finding against the selected task, but MUST NOT independently create a second task ledger.

## 7.1 Concise active plan and committed sidecars (Phase 2D1)

The canonical plan may be `factory-plan/v1` (legacy, readable until migrated) or `factory-plan/v2` (concise active plan). A v2 plan carries only the genuinely unfinished tasks — active/pending/in_progress/blocked — with stable IDs, titles, priorities, dependencies, concise Scope/Acceptance, the current blocker, and the latest actionable failure. Completed/cancelled tasks and the plan acceptance/evidence history live in two strict committed machine sidecars:

- `.factory/artifacts/plan-archive.jsonl` (`factory-plan-archive/v1`) — one JSONL record per archived completed/cancelled task, preserving every task/status/acceptance/evidence datum losslessly for audit; the full v1 Evidence narrative is preserved verbatim in the `evidence` field (bounded inert data, never authority) and the curated safe inert references extracted from it live in `evidence_refs` (non-empty, no whitespace/control/backslash, not absolute, no empty/`.`/`..` segments; command-shaped prose stays in the narrative and is never a ref);
- `.factory/artifacts/plan-history.jsonl` (`factory-plan-history/v1`) — one JSONL record per plan acceptance/evidence event.

Both sidecars are content-addressed: the plan front matter `sidecars:` binding carries their exact SHA-256 digests, and the composite plan binding digest `sha256(sha256(plan) | 0x00 | sha256(archive) | 0x00 | sha256(history))` binds the active plan plus both required sidecar digests. The campaign's `plan_digest` (state, launch binding, lease claims) is the composite digest for a v2 plan, so a change to the active plan or to either sidecar changes the binding and same-commit tamper is detected. v1/v2 is detected by the parsed front-matter `schema:` key — never a substring heuristic — so a v1 plan whose body merely mentions the v2 schema string stays v1, and a forged `schema:` value fails closed. Sidecars are bounded (1 MiB file / 64 KiB record / 10000 records), reject duplicate keys, and are append-only (or deterministically migrated); they are queryable by trusted tools but excluded from routine role prompts and never interpreted as task/command authority. The existing `.factory/artifacts/conformance.json`, blocked facts, and audit receipts remain the authorities.

Archived dependency satisfaction uses the trusted completed-ID index bound to the archive sidecar digest: a missing/conflicting/reopened ID fails closed. Reopening an archived task is an explicit semantic migration with provenance, never model prose; the current factory does not support reopening and rejects it unambiguously (a reopened task must be reverified and rearchived by a future explicit tool). The final audit task must be last in document order and must depend on every other task — active and archived — and no others; a v2 plan cannot be parsed without the bound archive records.

Migration v1→v2 is an explicit deterministic tool (`.factory/loop/plan_migration.py`): lossless for audit, idempotent, atomic (sidecars written before the plan, so any partial pair fails closed on the plan's binding), crash-safe, and never a silent auto-mutation. Legacy v1 plans remain readable until migrated. The trusted campaign archives a completed task only after the independent verification + audit pass (provenance `campaign`); planner/developer roles can never mutate the sidecars. The trusted archiver anchors to the committed archive/history blobs at the exact head and requires the worktree bytes to equal them — a stale or forged worktree sidecar fails closed before any mutation. The plan-sidecar binding is verified against the actual sidecar bytes at every planning worktree validation and at crash reconciliation, so a forged declared digest fails closed. The injected plan text is the concise v2 plan plus the current latest failure — never the archive/history/matrix.

## 8. Deterministic task selection

The trusted control plane selects the next task from the plan. The model does not choose among multiple tasks.

Selection order:

1. reject an invalid, stale, or ambiguously parsed plan;
2. resume the sole `in_progress` task, if present;
3. otherwise consider `pending` tasks whose dependencies are `complete`;
4. sort by explicit numeric priority, then lexicographic task identifier;
5. select exactly one;
6. if none are runnable, classify the implementation phase as `work_exhausted` or `blocked`, as defined below.

Selection MUST be deterministic and covered by fixtures. No `.ralph/agent/tasks.jsonl` or equivalent runtime queue participates.

For a v2 plan, the selector binds the trusted completed-ID index from the archive sidecar: a dependency on an archived completed task is satisfied through the index, archived tasks are leaves of the plan graph, and a conflict (an archived ID that reappears active) or a reopened ID fails closed. The selector never selects an archived task.

## 9. Fresh-context execution

Every model attempt MUST:

- start a new Pi/Pi2 process;
- use no resumed model session or previous loop identity;
- disable automatic memory injection;
- receive the static role prompt and authoritative inputs only;
- run under a hard runtime limit;
- run in its own process session/group;
- receive TERM, INT, and HUP through the trusted supervisor;
- be fully reaped, escalating to KILL only after a bounded grace period.

A selected task excerpt is derived from the plan and does not constitute another source of truth. Before launch, the harness re-derives its exact bytes from the committed plan blob, records the excerpt digest in the invocation binding, and fails closed if the delivered bytes differ, are paraphrased, or come from another plan revision.

## 9.1 Task-resource budget (cumulative)

Each selected implementation task is bounded by a cumulative resource budget across every fresh developer attempt of that task.  The committed `.factory/task-budget.json` document (closed schema `factory-task-budget/v1`, bounded integers, duplicate-key rejection) supplies the limits; an absent config uses the documented defaults.  The budget bounds:

- cumulative wall-clock time of the attempts;
- cumulative process-tree CPU time: the authoritative per-attempt value is the `RUSAGE_CHILDREN` delta of the supervisor (baseline pinned immediately before spawn, read after the confinement/exec broker is reaped), which is kernel accounting that folds every reaped descendant — including short-lived burners that never appear in a live sample — into the attempt total without double counting (live CPU is read before the delta, so a process that exits between the two reads is counted exactly once); fast live `/proc` sampling of the identity-pinned descendant closure is kept only for early termination and as the last-known bounded fallback;
- cumulative combined captured stdout+stderr bytes;
- the live/descendant process peak of one attempt;
- a per-command timeout kept as defense-in-depth (there is no small command-count limit: focused inspect/edit/test/diagnose cycles continue while the resource budgets remain).

The trusted supervisor enforces the budget around every fresh developer attempt: it derives the remaining limits before spawn, refuses a task whose cumulative budget is already exhausted (never launched, never accepted), terminates the attempt on overflow through the existing TERM→INT→HUP→KILL group machinery, and records the measured usage monotonically into the cumulative per-task ledger (`factory-task-budget-ledger/v1`) bound to (campaign, selected task) under the ignored private `.factory-state/` namespace, outside every model-writable path.  The ledger is published with no-replace/monotonic tamper handling: a foreign, malformed, or non-monotonic ledger fails closed and is never silently replaced.  The recorded CPU is the authoritative per-attempt `RUSAGE_CHILDREN` delta (including short-lived reaped descendants); when exact accounting cannot be established the attempt fails closed as `accounting_untrusted` and the last-known bounded usage is preserved — never a trusted zero.  A descendant-capture overflow is reported as the capture bound (the live-process budget is then exhausted, never an incomplete snapshot mistaken for a small tree) while preserving the last-known CPU; the live-process count is a sampled peak, not an exact instantaneous count, and the bounded TERM→INT→HUP→KILL termination plus the CPU/wall budgets bound any fork-explosion damage between samples.

Exhaustion is a bounded non-success outcome and can never produce acceptance: dirty work is preserved (terminal `interrupted`), a clean exhaustion is a deterministic `task_failed` that proceeds to verification/audit at the last coherent commit, and the campaign never retries an exhausted task.  The developer prompt states the budget and that its own runs are diagnostic while acceptance remains an independently bound exact-commit verifier.

## 10. Ollama usage hook

The existing `scripts/ollama-usage-guard.sh` contract MUST be retained.

Before every model invocation, the control plane runs:

```sh
./scripts/ollama-usage-guard.sh --check
```

The exact decision table is:

- `--check` exit 0: invoke the model;
- `--check` exit 1 (quota threshold) or 3 (transient status failure): run `--wait`;
- `--wait` exit 0: run one final `--check`, which MUST exit 0 before invocation;
- `--check` exit 2 (fatal), any undocumented exit, or any nonzero `--wait` exit: terminate the campaign without invoking the model.

The existing usage-status behavior is retained, but its credential transport MUST be hardened before the new loop is accepted. The guard MUST NOT export cookies or credentials to child environments or place them in child argv. It MUST use a bounded stdin or secure mode-0600 descriptor/file mechanism, erase owned temporary material, and expose only redacted status. A conformance test inspects a live synthetic child's `/proc/<pid>/cmdline` and `/proc/<pid>/environ` and fails if the synthetic cookie name or value appears.

The guard remains outside the model context. Credential material, cookies, private endpoints, and raw usage responses MUST NOT be added to prompts, logs, command arguments, child environments, or repository state. Signals received while waiting MUST terminate the wait and campaign cleanly.

## 11. Minimal mutable control state

The factory MUST have one mutable control-state file, stored outside Git, for example `.factory-state/factory-loop.json`.

It contains exactly:

- `schema`;
- `repository_identity`;
- `branch`;
- `campaign_id`;
- `rounds_requested`;
- `current_round` (monotonic, 1-based);
- `current_phase`;
- `specification_digest`;
- `plan_digest`;
- `role_prompt_digests` and `audit_objectives_digest` bound at campaign start;
- `phase_base_commit`;
- `selected_task_id`, if any;
- `attempt_number` (monotonic within the current task and reset to zero only on a trusted task/phase transition);
- `phase_started_at_monotonic` and `attempt_started_at_monotonic` for timeout recovery;
- `last_outcome`, which is a trusted control-plane enum, not an evidence claim;
- `convergence_task_id`, `verifier_failure_digest`, `convergence_retries`, and
  `last_failure_fingerprint` (Phase 2B1 convergence-extension fields, present
  only while an inner same-task convergence cycle is active; a state with an
  inactive cycle serializes byte-identically to a pre-Phase-2B1 state).

No wall-clock timestamp or additional field is accepted by the schema.

It MUST NOT contain model prose, task descriptions, memories, evidence claims, or copies of the plan.

All writes MUST be atomic, no-follow, ownership/mode/link-count checked, and validated against this transition table:

```text
planning -> implementation -> verification -> audit
planning --attempts-exhausted--> failed
implementation --dirty-attempts-exhausted--> interrupted
verification --infrastructure-failure--> infrastructure_failure
verification --software_verified_external_acceptance_blocked--> audit
verification --verifier_failure--> implementation
audit --nonfinal--> planning(next round)
audit --final--> success | findings | blocked
```

`software_verified_external_acceptance_blocked` is the verification outcome
that records software fully verified while external release acceptance
remains blocked (human approval, real-system evidence, or an unavailable
external release authority).  It advances to the independent `audit` exactly
like `pass`/`findings`/`blocked`, but it can never produce campaign success:
when the audit phase was entered with this verification outcome, an audit
`pass` resolves to the terminal `blocked` state in the final round (never
`success`) and to the next round's `planning` in a non-final round.  The
outcome never weakens round-zero readiness, the `infrastructure_failure`
fail-closed closes, or human authority.

`verifier_failure` is the Phase 2B1 inner same-task convergence outcome: a
trusted deterministic software verifier failure returns to implementation
for the SAME task (`convergence_task_id`) with the validated
`factory-verifier-failure/v1` artifact, without planner/tester/auditor
ceremony.  The edge is conditional and bounded: the campaign takes it only
when the deterministic gate actually ran and returned an ordinary nonzero
status (never 126/127 or a negative supervisor status), the tester passed
with no findings, the declared capability is available and ran clean, no
scope violation occurred, the task is bound, the retry budget remains, the
failure is not a byte-identical repeat, the task resource budget is not
exhausted, and the campaign deadline remains.  Infrastructure, capability,
human/external, and tester-finding failures never converge: they keep the
existing `findings`/`blocked`/`infrastructure_failure` flow.  The campaign
performs at most two same-task convergence retries per task
(`MAX_CONVERGENCE_RETRIES = 2`); a repeated identical failure (same
fingerprint) and a consumed task resource budget terminate the loop honestly
before that bound.  The convergence edge never weakens round-zero
readiness, the `infrastructure_failure` fail-closed closes, or human
authority.

Round advances only on `audit --nonfinal`; phase never moves backward within a round. State fields that bind a completed phase are write-once; round and attempt counters are monotonic. The harness records the state digest before every untrusted phase and reopens and validates the file after the phase. Any same-UID mutation not produced by the trusted transition, including content, mode, owner, link-count, or pathname identity changes, fails closed.

Append-only receipts, audit reports, captures, and runner evidence are evidence artifacts, not orchestration state.

## 12. Locking and writer boundary

The trusted control plane MUST acquire an exclusive `flock` on an already-open canonical Git-top-level directory descriptor obtained with `O_DIRECTORY`, `O_NOFOLLOW`, and close-on-exec semantics. A replaceable lockfile pathname is not the authority. It MUST validate canonical repository identity, required branch, specification binding, and plan base before launch.

The lock authority MUST NOT be inherited by untrusted model processes. Before child exec, the harness closes the lock descriptor in the child, removes all lock metadata from its environment, and starts a new process session. An untrusted process MUST be unable to unlock the holder through a separately opened root descriptor. No second tracked writer, worktree, concurrent build, or parallel lifecycle mutation is allowed.

After interruption, the supervisor verifies that the model process group is gone and that no escaped descendant retains the repository root/lock inode or a model workspace handle. A double-fork or `setsid` escape that survives bounded termination fails closed for operator inspection before recovery can reacquire the writer boundary.

Only the developer role may modify product code. The planner may modify only the plan. The tester and auditor are read-only except for trusted receipt publication performed by the control plane. The control plane preserves the Git command-boundary guard: `--no-verify`, hook-path override, `GIT_CONFIG_*`, alternate git/work trees, amend/merge/rebase bypasses, and forged handoffs remain rejected.

## 13. Phase outcomes

Model completion tokens are not control protocol. The trusted harness derives outcomes from plan state, Git state, exit status, and deterministic gates.

### 13.1 Planning

Outcomes:

- `planned`: valid fresh plan committed;
- `failed`: no valid plan commit after the configured bounded planning attempts; campaign terminates nonzero `failed`;
- `interrupted`: process ended before a valid checkpoint; retry while budget remains, otherwise terminate nonzero `interrupted`.

### 13.2 Implementation attempt

Outcomes:

- `task_completed`: selected task is complete, acceptance checks pass, and a coherent commit exists;
- `task_progress`: selected task remains `in_progress` with preserved committed or dirty work;
- `task_failed`: deterministic check or model process failed;
- `interrupted`: bounded process interruption;
- `work_exhausted`: no pending or in-progress tasks remain;
- `blocked`: every unfinished task is explicitly blocked on unavailable external or human evidence;
- `verifier_failure`: a Phase 2B1 inner same-task convergence retry returned to
  implementation for the SAME task with the validated verifier-failure
  artifact (see §13.3/§14).

`work_exhausted` and `blocked` are not product acceptance. `task_failed` and `task_progress` retry the same plan task while its bounded attempt budget remains. If the budget expires with dirty work, the campaign terminates `interrupted` and verification does not run. If the budget expires cleanly with a reproducible task failure, the trusted harness records a finding and proceeds to verification/audit at the last coherent commit.

### 13.3 Verification

Outcomes:

- `pass`;
- `findings`;
- `blocked` when a required declared capability cannot execute;
- `software_verified_external_acceptance_blocked` when the deterministic
gate passed, no finding remains, the declared capability is available, and
the tester cited exact blocked references — software is fully verified while
external release acceptance (human approval, real-system evidence, or an
unavailable external release authority) remains blocked;
- `infrastructure_failure` when the verifier itself cannot be trusted;
- `verifier_failure` when the deterministic software verifier actually ran and
  returned an ordinary nonzero status while the tester passed with no
  findings, the declared capability is available and ran clean, no scope
  violation occurred, the task is bound, the retry budget remains, the
  failure is not a byte-identical repeat, the task resource budget is not
  exhausted, and the campaign deadline remains.  It returns to implementation
  for the SAME task (at most two retries per task, `MAX_CONVERGENCE_RETRIES =
  2`); a repeated identical failure (same fingerprint) and a consumed task
  resource budget terminate the loop honestly before that bound.

Verification `findings` or `blocked` do not prevent the independent audit from running. In a non-final round they advance to audit and then become next-round plan inputs. `blocked` means a required, correctly declared capability or human/external authority is unavailable while the verifier and binding remain trusted. `software_verified_external_acceptance_blocked` also advances to the independent audit, but it can never produce campaign success: an audit `pass` entered from it resolves to the terminal `blocked` state in the final round (never `success`) and to the next round's `planning` in a non-final round. `infrastructure_failure` means verifier identity, digest, execution, receipt publication, or control-plane trust is invalid; it fails closed and stops the campaign.

### 13.4 Audit

Outcomes:

- `pass`;
- `findings`;
- `blocked` with exact unavailable-evidence references.

An audit cannot pass with a failed receipt, fabricated command, unresolved mandatory finding, or evidence below the required tier. A non-final audit `blocked` advances to the next planner exactly like findings; the blocker remains explicit in the plan. A final audit `blocked` produces terminal `blocked` only when no software/test finding remains and every unresolved item requires unavailable external, hardware, capability, or human authority.

## 14. Campaign semantics

A campaign is a finite number of rounds. Each round consists of:

```text
planning -> implementation attempts -> verification -> audit
```

The campaign MUST NOT remain indefinitely in implementation merely because external acceptance is unavailable.

When implementation reports `work_exhausted` or `blocked`, verification and audit still run. Their findings are inputs to the next round's planner through the canonical plan revision, not through memory injection.

For non-final rounds:

- audit `pass` or `findings` advances to the next round;
- audit findings MUST be represented in the next plan before development starts.

Phase 2B1 inner same-task convergence: when a trusted deterministic software
verifier failure meets every convergence condition (§13.3), the campaign
returns to implementation for the SAME task with the validated
`factory-verifier-failure/v1` artifact, bypassing planner/tester/auditor
ceremony.  The convergence cycle is task-bound and monotonic: the state binds
`convergence_task_id`, the artifact digest, a monotonic `convergence_retries`
count, and the last failure fingerprint, and the cycle is cleared on every
transition except the two inner-loop edges (`implementation --task_completed-->
verification` binds the task being verified; `verification --verifier_failure-->
implementation` carries the cycle forward).  The campaign performs at most two
same-task convergence retries per task (`MAX_CONVERGENCE_RETRIES = 2`); a
repeated identical failure (same fingerprint) and a consumed task resource
budget terminate the loop honestly before that bound.  Infrastructure,
capability, human/external, and tester-finding failures never converge: they
keep the existing `findings`/`blocked`/`infrastructure_failure` flow with no
planner/tester/auditor ceremony skipped.

For the final round (or the scheduler-resolved terminal boundary, §14.1):

- complete product acceptance and audit pass produce campaign `success`
  (verified completion may terminate the campaign early, before the maximum
  round budget);
- any unresolved software, test, documentation, security, or audit defect produces terminal nonzero `findings`;
- if there are no such defects and every unresolved mandatory item exclusively requires unavailable external, hardware, declared-capability, or human authority, the result is terminal nonzero `blocked`;
- a verification outcome of `software_verified_external_acceptance_blocked` (software fully verified while external release acceptance remains blocked) can never produce `success`: even a final-round audit `pass` resolves to the terminal `blocked` state;
- if both categories exist, `findings` takes precedence;
- repeated/no meaningful progress (consecutive audits reproducing the same
  progress fingerprint) terminates honestly as `no_progress`;
- the maximum round/checkpoint budget without verified completion
  terminates honestly as `budget_exhausted`;
- state and evidence are preserved for a later campaign after circumstances change.

Planning-attempt exhaustion produces `failed`; dirty implementation-attempt exhaustion or operator/process interruption produces `interrupted`; untrusted verifier/control-plane failure produces `infrastructure_failure`. A finite campaign therefore always terminates as success, findings, blocked, failed, infrastructure failure, interruption, no progress, or budget exhaustion. It never spins because there is no runnable task.

### 14.1 Campaign budget (adaptive scheduler authority)

The campaign/audit scheduler is a pure generic authority: every milestone,
objective-coverage, no-progress, and terminal decision is a deterministic
function of the committed campaign budget, the plan state, the control state,
and the trusted phase outcomes — never of model prose, wall-clock time, or
runtime randomness.  The committed `.factory/campaign-budget.json` document
(closed schema `factory-campaign-budget/v1`, bounded values, duplicate-key
rejection) supplies the budget; an absent config uses the documented defaults.
The budget bounds:

- `max_rounds` — the maximum number of planning/implementation/
  verification/audit cycles.  It is a maximum budget, never an exact count
  (there is no exactly-five rejection); verified completion may terminate the
  campaign early, before the maximum;
- `max_checkpoints` — the maximum number of coherent task checkpoints
  (committed task completions) the campaign may produce;
- `max_wall_seconds` — the committed wall-clock budget.  The effective
  campaign deadline is the tighter of the operator `campaign_timeout` and
  this committed `max_wall_seconds` (`min`), applied consistently on start
  and resume, so neither a larger operator timeout nor a larger committed
  budget can silently extend the wall-clock bound;
- `max_task_attempts` — the per-task attempt budget, enforced by the existing
  task-resource-budget authority;
- `audit_interval` — the coherent-checkpoint interval at which the
  independent tester/auditor run (milestone-boundary roles, not mandatory
  after every patch);
- `security_sensitive_paths` — trusted closed-config repository-relative path
  prefixes that force an independent audit when a checkpoint touches them
  (never plan prose; absolute paths, traversal segments, and glob characters
  are rejected);
- `mandatory_audit_objectives` — the audit objective IDs that must all be
  covered before release success, regardless of checkpoint count;
- `no_progress_limit` — the number of consecutive audits that reproduce the
  same progress fingerprint before the campaign terminates honestly as
  `no_progress`.

The progress fingerprint binds only trusted monotonic evidence: the
coherent checkpoint count (newly independently verified exact-commit
software checkpoint/task completion) and the set of PASSed mandatory audit
objectives.  It deliberately excludes planner-authored task statuses,
findings/outcome alternation, and mere objective rotation, so repeated
activity without a new checkpoint or a newly PASSed mandatory objective
reproduces the same fingerprint and terminates honestly as `no_progress`
even if the planner toggles statuses.  Only an audit outcome of `pass` adds
objective coverage; findings/blocked never cover an objective.  A changed
Git symlink is always security-sensitive and forces an independent audit,
recognized from trusted `git diff --raw` mode metadata (mode `120000`)
without resolving/following the target, so a benign-named symlink that
redirects outside the intended namespace is still detected.

The scheduler continues only while meaningful progress is possible and
terminates deterministically on verified completion,
`software_verified_external_acceptance_blocked`, a persistent
external/infrastructure blocker, repeated/no meaningful progress,
task/campaign budget exhaustion, or interruption.  The terminal reason is a
closed enum (`success`, `findings`, `blocked`, `failed`, `interrupted`,
`infrastructure_failure`, `budget_exhausted`, `no_progress`,
`software_verified_external_acceptance_blocked`) recorded in the control
state and the published campaign result so operators can distinguish success,
findings, infrastructure failure, interrupted, budget exhausted/no progress,
and software-verified-external-acceptance-blocked without weakening the
existing bounded public reasons or the exact-commit evidence chain.  The
scheduler never selects audit authority from task/plan prose: the mandatory
objective set and the security-sensitive path prefixes come only from the
committed closed config, and the audit objective itself is selected
deterministically from the round number by the committed registry authority.

### 14.2 Task-scoped path-lease foundation (Phase 2C1, LEASE-01)

The generic task-scoped path-lease authority is the strict, foundation-only
mechanism for task-scoped write authority over product-owned verification
surfaces.  It is implemented by the committed `factory-path-lease-policy/v1`
config (`.factory/path-lease-policy.json`) and the strict
`factory-task-path-lease/v1` claim schema, with the pure authority in
`.factory/loop/path_lease.py`.  Phase 2C1 deliberately does NOT wire leases
into launch/confinement behavior: no Landlock/workspace-confinement write
candidate is altered and no claim is self-authorizing.  Phase 2C2 binds
claims into the existing signed launch authority and applies exact no-follow
path grants.

Policy (closed config, deny-dominant):

- scope IDs are closed (`^[a-z][a-z0-9_-]*$`) and map to bounded
  repository-relative path prefixes/patterns for product-owned verification
  surfaces (scripts, build environment/Nix files, packaging, CI/forge files);
- deny zones are absolute and non-overridable: `.factory` security/control
  machinery, `.factory-state`, `.git`, the product spec path, credential/key/
  env authorities, and ALL goldens/approval/release/human-authority surfaces
  are immutable deny zones.  No carve-out/override field exists in the
  schema; a human-only override mechanism is deliberately out of scope and
  must never be mintable by a campaign/model;
- deny is dominant over allow: a requested scope's paths inside a deny zone
  are removed, and a scope that grants nothing after expansion is forbidden
  (the request fails closed);
- the policy rejects absolute/traversal/backslash/control/symlink-ambiguous
  patterns, duplicate keys, unsafe globs, and overlapping deny escapes at
  load; grant patterns never contain `**` (deny patterns may, conservatively);
- a planner `Write scopes:` request is a REQUEST, never a grant: the trusted
  policy intersection decides whether a requested scope is known and grants
  anything, and unknown/duplicate/forbidden scopes fail closed.  `Scope:`
  prose is never authority.

Claims (strict DATA, never self-authorizing):

- a `factory-task-path-lease/v1` claim binds campaign ID, selected task ID,
  attempt, exact HEAD commit, plan digest, policy digest, requested/granted
  scopes, the exact deny-dominant expanded paths/patterns, issued/deadline
  bounds, and a unique attempt nonce, sealed by a canonical claim digest;
- the claim digest is an UNKEYED SHA-256 over the canonical claim bytes: a
  deterministic integrity check against accidental corruption and
  forgery-by-a-non-writer, but NOT authoritative on its own (any same-UID
  writer can recompute it, so it never proves provenance or authorization).
  Only the trusted harness mints and re-validates claims, and Phase 2C2
  binds the claim into the existing signed launch token (HMAC/FD authority);
  until that binding exists the digest is non-authoritative integrity data,
  never a grant;
- claims carry no free-form commands and never resolve symlinks; every
  granted path/pattern is a bounded repository-relative prefix/pattern;
- replay prevention fails closed on any campaign/task/attempt/commit/digest
  mismatch and on expiry; a same-UID workspace JSON can never self-
  authorize;
- security-sensitive leases (a scope marked `audit_required`) mark
  `audit_required` so the independent audit is mandatory.

### 14.2a Runtime-launch lease binding (Phase 2C2a, LEASE-01)

The Phase 2C2a runtime-launch foundation binds the Phase 2C1 claim into the
existing signed launch authority as an optional developer-only extension;
no campaign minting happens here.  The claim remains DATA: the claim digest
alone is never authoritative.

Launch binding (`InvocationBinding` / `authorize_launch`):

- the binding carries optional `campaign_id`, `attempt`, `lease_digest`,
  `lease_bytes` (the exact canonical claim document), and the immutable
  `audit_required` signal — all defaulted, so the campaign launch path is
  byte-identical without a lease;
- `authorize_launch` re-validates the exact canonical claim bytes against
  the committed policy (schema, deny-dominant expansion, policy digest) and
  the trusted launch context (campaign/task/attempt/HEAD/plan/policy
  digest, issued/deadline) before any prompt byte or confinement rule is
  composed; a stale, tampered, replayed, or foreign claim fails closed;
- a real-provider launch still requires the existing signed HMAC/FD launch
  token (the locked readiness store): a claim without that signed authority
  never grants, and a consumed/replayed token fails closed;
- the exact deny-dominant write candidates (`lease_write_candidates`:
  granted paths plus the static directory prefixes of granted patterns,
  deny re-checked, fail closed when empty) are the only paths the workspace
  confinement may grant, as WRITE-only rules — the lease is write-path
  scope only and never grants read/execute/commands/credentials beyond the
  existing static authority;
- each candidate is validated dirfd/O_NOFOLLOW: no symlink component,
  resolved containment, same-device mount-escape check, single-link
  hardlink check, and the path must exist (a nonexistent path cannot be
  granted without overgranting its parent);
- CI/security-sensitive scopes set the immutable `audit_required` signal on
  the verified binding and the launch result for the Phase 2C2b scheduler;
  the model can never clear it (a binding that claims `audit_required`
  without an authenticated lease is rejected).

Confinement and revalidation:

- the exact claim/context travels in the `factory-confinement/v1`
  specification (`lease` section) and the launch result carries
  `audit_required`;
- the lease is re-validated immediately before spawn (supervisor) and
  inside the confined launcher (which loads the committed policy through
  the staged exact-commit `path_lease` module, F2), so a stale/tampered/
  replayed/foreign claim and a worktree policy tamper fail closed at every
  trust edge;
- default behavior without a lease is unchanged: no lease section, no
  lease write rules, no `audit_required`.

### 14.2b Campaign-controlled lease minting and delivery (Phase 2C2b-A, LEASE-01)

The Phase 2C2b-A campaign slice mints and delivers the task path-lease for
selected developer attempts; the Phase 2C2b scheduler audit consumption of
the `audit_required` signal is the next slice (2C2b-B) and is explicitly
out of scope here.

Minting (trusted campaign, per selected developer attempt):

- the optional plan `Write scopes:` request of the selected task is read
  from the exact committed plan blob at the phase head (never the mutable
  worktree) and is treated only as a REQUEST — `Scope:` prose is never
  parsed and never authority;
- the committed path-lease policy (`.factory/path-lease-policy.json`) is
  loaded from the exact committed HEAD blob (never the worktree) and its
  digest is bound into the claim; the request is validated through the
  trusted deny-dominant policy intersection/expansion;
- one unique canonical `factory-task-path-lease/v1` claim is minted per
  campaign/task/attempt/exact HEAD/plan digest/policy digest with a
  trusted nonce and a deadline no later than the remaining
  attempt/task/campaign budget (the campaign wall-clock deadline is the
  outer bound; the per-task resource budget's remaining wall time is the
  tighter inner bound when a ledger exists);
- unknown/forbidden/unavailable/nonexistent expanded path requests fail
  closed with a bounded campaign error — never a silent fallback and never
  a broad write; a committed plan scope change forces plan/HEAD
  revalidation (the claim binds the exact committed plan digest and
  HEAD); a convergence retry mints a fresh nonce/claim lease and a
  replayed claim fails closed;
- no requested scopes means no lease: the exact previous default
  confinement applies unchanged.

Delivery:

- the exact canonical claim bytes plus digest travel through the existing
  `authorize_launch`/LaunchSupervision path (production) and the
  digest-bound driver channel (fixture seam), which re-validates schema,
  committed-policy expansion, and context/expiry before any prompt byte
  or confinement rule is composed;
- the campaign persists no authority secret/token; bounded non-secret
  result fields may include scope IDs, the claim digest, and the immutable
  `audit_required` signal;
- no command allowlist or verifier binding changes; the product-path
  lease remains write-only and cannot self-certify.

### 14.2c Scheduler/state wiring of the immutable lease audit_required signal (Phase 2C2b-B, LEASE-01)

The Phase 2C2b-B slice wires the immutable lease `audit_required` signal
into the adaptive scheduler/state and final acceptance.  Every leased
verification surface (scripts, nix, packaging, ci) is security-sensitive —
each can alter verification/build/release behavior — so the committed
path-lease policy marks all four scopes `audit_required`; this grants no
release authority (the lease remains write-path scope only).

Pending lease-audit state (trusted control state, `pending_lease_audits`):

- when any developer attempt's authenticated task path-lease returns
  `audit_required`, the campaign records one sticky trusted pending
  lease-audit trigger bound to the campaign/task/attempt/lease digest and
  the exact resulting candidate commit containing the leased changes;
- the trigger ORs across retries/attempts and the model can never clear
  it; it carries no secret/nonce/claim bytes — only bounded non-secret
  digests and identifiers;
- the set is optional in parse and serialized only when non-empty, so a
  pre-Phase-2C2b-B state round-trips byte-identically (migration
  compatibility); every record is validated (closed five-field shape,
  positive task/attempt, 64-hex lease digest, 40-hex commit, sorted,
  duplicate-free, bound to the exact campaign).

Forced audit milestone and consumption:

- the trusted scheduler forces the independent tester/auditor milestone
  whenever any trigger is pending, regardless of the configured
  `audit_interval` (the model can never defer or clear it);
- only a passing independent audit at that exact candidate commit
  consumes the matching trigger; findings/blocked/skipped/infrastructure/
  interrupted/stale/foreign/replay audits leave every trigger pending and
  route honestly;
- a same-task trigger whose bound commit is a verified ancestor of a new
  commit is superseded (the new commit's independent audit covers the
  ancestor's changes), so a convergence retry or a crashed-attempt resume
  never deadlocks on a commit the campaign can never revisit; a trigger
  whose commit is not a verified ancestor is never discarded;
- the trusted verifier still runs independently at each candidate exact
  commit; leased modifications to scripts/nix/packaging/ci cannot
  self-certify current acceptance.

Success blocking and the success validator:

- any pending trigger blocks success: the scheduler routes honestly to
  planning/implementation/budget-exhausted/no-progress instead of ever
  succeeding with an unaudited sensitive lease;
- the success validator independently rejects a pending trigger that
  remains after the passing audit at the exact commit (a stale/foreign
  trigger can never be silently dropped by a passing audit at a different
  commit);
- the no-progress fingerprint treats a passing required lease audit as
  trusted progress only when a matching trigger was actually consumed.

## 15. Completion predicates

The following predicates MUST remain distinct:

1. **Task completion:** one plan task and its acceptance checks are complete.
2. **Implementation work exhaustion:** no runnable plan task remains.
3. **Verification pass:** deterministic checks at the exact commit passed.
4. **Software verified, external acceptance blocked:** the deterministic gate passed, no finding remains, the declared capability is available, and the tester cited exact blocked references; the verification outcome is `software_verified_external_acceptance_blocked` and the campaign can never reach `success` while it holds.
5. **Audit pass:** independent review found no acceptance finding within scope.
6. **Product acceptance:** every normative specification requirement has adequate evidence, including required real-system and human evidence.
7. **Campaign success:** final-round product acceptance and audit pass.

No lower predicate implies a higher predicate. In particular, software fully verified while external acceptance remains blocked is never product acceptance and never campaign success.

## 16. Findings flow

Tester and auditor outputs MUST be structured, exact-commit-bound findings. Before another development attempt, the planner incorporates accepted findings into the canonical plan as new or revised tasks.

The next developer sees only the revised plan, specification, and code. It does not receive tester conversation, developer conversation, semantic memory, or recovery prose.

## 17. Recovery

Recovery is derived from Git, the canonical plan, the one control-state file, and process liveness.

- A clean committed task resumes from the next deterministic task.
- An `in_progress` task resumes from current code and Git diff in a fresh context.
- Tests from an interrupted attempt are rerun; their untrusted prose is not preserved as memory.
- An ambiguous live process, changed repository identity, changed branch, unsafe file, stale specification binding, changed plan base, rewound counter, or invalid state transition fails closed for human/operator inspection.
- Recovery MUST NOT reset, discard, or silently overwrite dirty work.

A scratchpad is not required for ordinary operation. If retained for compatibility, it MUST be an optional single crash note, excluded from normal model context, non-authoritative, size bounded, and removable after successful recovery.

## 18. Security and credential boundary

Existing credential protections remain mandatory at the actual Pi tool-call and tool-result boundary.

The control plane MUST preserve:

- model workspace confinement that makes `.factory-state/`, `.ralph/`, legacy handoffs, task stores, and memory stores unavailable to model tools;
- immutable production credential-guard selection;
- bounded command/path checks through stdin;
- structured tool-result redaction;
- secure log sanitization;
- trusted Pi authentication/model configuration;
- no model tools or extensions beyond the explicitly allowed set;
- no environment, authentication-file, private-key, or secret dumping.

The new loop MUST call the existing secure Pi wrapper rather than reproducing authentication logic. The Pi extension retained by the redesign contains only required credential tool-call/tool-result enforcement and guarded Git command-boundary behavior. Ralph lifecycle topics, `ralph emit`, completion-token handling, event snapshots, launch handshakes, and Ralph CLI shims are removed from the new path and MUST NOT be reimplemented for parity.

## 19. Evidence and acceptance

Evidence tiers remain distinct:

- `unit`;
- `simulated`;
- `private_integration`;
- `installed`;
- `real_system`;
- `human`.

The factory MUST preserve exact-commit signed runner receipts, capability contracts, visual provenance, atomic publication, installed verification, and human-only acceptance where specified.

These systems answer whether a claim is proven. They MUST NOT decide which implementation task the developer performs.

Coordinator commands that support acceptance MUST run through the trusted receipt wrapper. PASS requires the bound command to exit 0; command, working identity, commit, digest, timestamps, and output digests are verified; any BLOCKED evidence forces an audit result of findings. Audits cite exact `[receipt: ...]` and `[manifest: ...]` references. A model assertion or free-text command transcript is not a receipt.

The verifier entrypoint MUST be opened and bound to its committed blob and secure identity before untrusted execution; later pathname substitution fails closed.

Every deterministic verifier failure MUST be recorded as a strict structured verifier-failure artifact (schema `factory-verifier-failure/v1`, EVID-02) carrying the exact command, the exact exit status, expected vs observed, a bounded relevant output tail and/or a bounded output reference, changed files, artifact references, an environment/capability classification, and a rerun scope.  Strings in the artifact are data, never executable path/argv authority: the exact command is an argv record of what the trusted control plane invoked and no consumer may re-execute it from the artifact.  Sizes are bounded, enums are closed, and duplicate JSON keys are rejected at parse time.  The artifact is the structured-handoff foundation for the implementer-owned inspect/edit/test/diagnose loop and never weakens round-zero readiness, infrastructure-failure fail-closed closes, or human authority.

Phase 2B1: a trusted deterministic software verifier failure that meets every convergence condition (§13.3) returns to implementation for the SAME task with the validated artifact.  The artifact is published write-once under the private `.factory-state/` namespace by its content-addressed digest and is re-validated at consumption against the exact commit, campaign, task, and digest; a stale commit, a foreign campaign/task, an altered digest, a missing artifact, or a replay across task/campaign/commit fails closed before any consumption.  The artifact's structured fields enter the developer's sealed prompt as inert quoted data — the exact command argv is a record of what the trusted control plane invoked, never an authority to re-execute, and the output tail is bounded diagnostic text.  The campaign performs at most two same-task convergence retries per task (`MAX_CONVERGENCE_RETRIES = 2`); a repeated identical failure (same fingerprint) and a consumed task resource budget terminate the loop honestly before that bound.

Missing evidence remains a finding. A receipt declaration is not a receipt; a model assertion is not evidence; private integration is not real-system acceptance; machine vision is not human approval.

## 20. Direct Pi execution

The factory SHOULD invoke the existing secure Pi/Pi2 wrapper directly in one-shot mode rather than running Ralph Orchestrator.

The invocation contract MUST specify:

- exact model/provider;
- static role prompt digest and campaign-bound prompt-set digest;
- deterministic audit-objective digest when applicable;
- canonical workspace and exact bound commit;
- selected task identifier, exact task-excerpt digest, and plan digest;
- disabled session resume and memory injection;
- allowed read/write tools for the role;
- runtime and inactivity bounds;
- machine-readable process exit status.

The trusted harness, not model output, determines whether a phase checkpoint is acceptable.

## 21. Migration from Ralph Orchestrator

Migration occurs generic-first on `boilerplate-develop`.

1. Freeze new Ralph Orchestrator campaign launches.
2. Preserve existing `.ralph/` and campaign artifacts as read-only recovery history until the active product task is safely checkpointed.
3. Implement the new Python control plane and conformance tests without modifying product repositories.
4. Prove behavior using synthetic fixture repositories and synthetic secrets.
5. Run adversarial interruption, timeout, dirty-work, stale-state, task-selection, and findings campaigns.
6. Port the verified generic implementation into Controller-Box on `develop` without changing its product specification.
7. Translate the active plan and campaign cursor into the one minimal state file; do not translate runtime tasks or semantic memories into new authorities.
8. Retire Ralph launchers only after parity and recovery tests pass.

Historical `.ralph` data MAY remain archived outside the model-visible workspace, but MUST NOT be injected into or readable from new model contexts. New-path source and tests reject any dependency on `ralph emit`, completion tokens, Ralph event streams, runtime task stores, or Ralph memories.

## 22. Required conformance tests

Every normative requirement in this specification has a stable ID in §24. The generic implementation MUST populate the existing machine-readable conformance sidecar and requirement policy with those IDs, required evidence tiers, required capabilities, exact evidence commits, and receipt/artifact references. `verified`, `partial`, `blocked`, and narrowly justified spec-scoped `not_applicable` classifications retain existing fail-closed semantics; only all-verified requirements may produce campaign success.

The generic implementation is not acceptable until tests prove:

1. planner, developer, tester, and auditor receive fresh processes and only allowed authoritative inputs;
2. automatic memory/session resume is disabled;
3. plan-derived task selection is deterministic;
4. no runtime task ledger is read or created;
5. a discovered finding reaches the next developer only through a revised plan;
6. empty runnable work reaches verification and audit instead of spinning;
7. external blockers terminate the final round nonzero without evidence elevation;
8. pass is impossible with unresolved mandatory findings;
9. one-writer locking and serialized builds hold under concurrency attempts;
10. TERM, INT, and HUP reach and reap the full model process group;
11. timeout and crash recovery preserve dirty work;
12. stale, forged, symlinked, oversized, wrong-owner, wrong-mode, or mismatched state fails closed;
13. Ollama `--check` and `--wait` run before every model invocation and quota-guard errors prevent launch;
14. credential tool-call blocking and tool-result redaction remain active;
15. exact-commit runner, visual, installed, and audit receipts retain their existing trust semantics;
16. no model completion token can bypass deterministic gates;
17. finite campaigns terminate within configured round and attempt bounds, including planning failure, clean task failure, dirty interruption, verification blocked, verification infrastructure failure, audit findings, audit blocked, and success fixtures;
18. migration preserves the active plan, Git commits, dirty work, evidence, and external blockers without importing Ralph memories or runtime tasks;
19. an untrusted leaf inherits no lock descriptor or lock environment, and a separately opened repository descriptor cannot unlock the holder;
20. a double-fork or `setsid` descendant is terminated or detected, and recovery cannot proceed while it survives;
21. the delivered task excerpt is byte-bound to the committed plan and substitution or paraphrase fails closed;
22. the model can read current authoritative inputs but cannot read `.ralph/`, `.factory-state/`, prior scratchpads, runtime tasks, memories, or legacy campaign state;
23. a mid-phase control-state mutation, mode/owner/link change, counter rewind, campaign-option mismatch, branch mismatch, or specification/plan-base change fails closed;
24. synthetic Ollama credentials appear in neither child argv nor child environments during `--check` and `--wait`;
25. the immutable verifier descriptor rejects pathname replacement;
26. Git commit-boundary bypass attempts remain rejected;
27. no new implementation depends on Ralph lifecycle events, tokens, shims, runtime tasks, or memories;
28. a trusted deterministic software verifier failure converges to the SAME task
    at most twice per task (`MAX_CONVERGENCE_RETRIES = 2`), the validated
    verifier-failure artifact is digest-bound and rendered as inert data, a
    repeated identical failure and a consumed task resource budget terminate
    the loop honestly, and infrastructure/capability/tester-finding failures
    never converge;
29. the adaptive scheduler budget (`factory-campaign-budget/v1`) rejects
    duplicate/unknown/overflow/path-traversal/unsafe-glob config, requires
    every mandatory audit objective to be covered before success, detects
    repeated no-progress, and terminates honestly on verified completion,
    external-acceptance-blocked, and round/checkpoint budget exhaustion;
30. the path-lease authority (`factory-path-lease-policy/v1` and
    `factory-task-path-lease/v1`) rejects every documented defect class
    (absolute/traversal/backslash/control/unsafe-glob/symlink-ambiguous
    patterns, duplicate keys, overlapping deny escapes, unknown fields),
    keeps deny zones absolute and non-overridable (no carve-out exists;
    goldens/approval/release/human-authority paths are never grantable),
    expands requests deny-dominantly with unknown/duplicate/forbidden scopes
    failing closed, binds claims to campaign/task/attempt/HEAD/plan/policy/
    nonce/deadline with canonical digests, fails closed on forgery, drift,
    replay, and expiry, marks security-sensitive scopes `audit_required`,
    and loads the committed policy no-follow with bounded size; the Phase
    2C2b-A campaign mints one unique claim per selected developer attempt
    from the committed plan `Write scopes:` request and the committed HEAD
    policy blob (never the worktree), clamps the deadline to the
    attempt/task/campaign budget, fails closed on unknown/forbidden/
    unavailable/nonexistent requests (never a silent fallback or broad
    write), and delivers the exact bytes+digest through the existing
    launch path without persisting any authority secret/token; the Phase
    2C2b-B scheduler/state wiring records a sticky pending lease-audit
    trigger bound to the campaign/task/attempt/lease digest and the exact
    resulting candidate commit whenever a developer attempt's lease
    returns `audit_required`, forces the independent tester/auditor
    milestone regardless of the configured interval, consumes a trigger
    only on a passing audit at that exact commit (findings/blocked/
    skipped/infrastructure/interrupted/stale/foreign/replay cannot clear;
    a same-task trigger whose bound commit is a verified ancestor of a
    new commit is superseded), blocks success while any trigger is
    pending, and keeps the trusted verifier independent so leased
    modifications to scripts/nix/packaging/ci cannot self-certify.
31. the concise active plan and committed sidecars (Phase 2D1) hold: v1
    plans stay readable until migrated; migration is lossless for audit,
    idempotent, atomic (sidecars-then-plan ordering makes every partial
    pair fail closed), and never a silent auto-mutation; the archive/
    history sidecars reject duplicate keys, unknown fields, malformed
    lines, and size/count/record overflow; the completed-ID index binds to
    the archive digest and missing/conflicting/reopened IDs fail closed;
    the composite plan binding digest changes when the plan or either
    sidecar changes (same-commit tamper detected); the prompt excludes
    archive/history/matrix content; the parser stays bounded; the selector
    never selects archived tasks; the plan+sidecar binding is verified at
    launch/state/freshness; administrative-only commits require a genuine
    semantic planning change (task add/remove/reorder or a title/priority/
    dependencies/Scope/Acceptance/blocker/latest-failure edit) while
    evidence/status/prose edits are metadata-only; no task is lost in
    migration; malformed/injection inputs are inert; and the plan size
    ceiling holds.

## 23. Acceptance criteria for the redesign

The redesign is complete in the boilerplate when:

- the Python control plane implements this specification;
- static role prompts exist for planner, developer, tester, and auditor;
- the implementation plan is the sole task authority;
- each role demonstrably starts with a fresh context;
- only one minimal control-state file exists;
- the existing Ollama usage guard and security boundaries are retained;
- generic adversarial tests and `verify-boilerplate.sh` pass;
- a five-round synthetic campaign completes with both success and final-findings fixtures;
- no Ralph Orchestrator process, runtime task ledger, memory store, event protocol, or resumed loop identity is required;
- an independent read-only audit reports no acceptance-critical findings;
- README, `docs/FACTORY.md`, `docs/OPERATIONS.md`, concise `AGENTS.md`, and installed help are synchronized and pass the documentation checker;
- the conformance sidecar maps every §24 requirement ID to exact evidence;
- the change is committed on `boilerplate-develop` before any Controller-Box port.

## 24. Normative requirement registry

The stable IDs below are the conformance authority for this specification. Section prose refines each requirement but MUST NOT introduce an unmapped normative obligation. The requirement policy records the exact required tier and any capability for each ID.

| ID | Sections | Requirement summary | Minimum evidence |
|---|---|---|---|
| AUTH-01 | §5, §7 | Specification, plan, and current code/tests are authoritative; the plan is the sole task ledger | installed |
| CTX-01 | §5, §9 | Every role uses a fresh process with only allowlisted current inputs and no semantic memory/session resume | installed |
| CTX-02 | §5, §18 | Legacy task, memory, scratch, event, and state paths are unavailable to model tools | installed |
| ROLE-01 | §6 | Planner, developer, tester, and auditor are distinct static roles; tester/auditor are independent and no adaptive model roles run | installed |
| PLAN-01 | §7, §7.1 | `factory-plan/v1` binds spec/base/tasks/requirements/interactions/conformance and parses unambiguously; `factory-plan/v2` is the concise active plan (unfinished tasks only) bound to the committed archive/history sidecars by the composite plan binding digest, with the final audit closing over active + archived tasks and archived dependency satisfaction through the trusted completed-ID index | unit |
| TASK-01 | §7, §8 | Task transitions and deterministic priority-plus-ID selection are trusted and plan-derived | unit |
| TASK-02 | §9, §20 | Delivered task bytes and digest exactly match the committed plan | installed |
| QUOTA-01 | §10 | Ollama check/wait behavior runs before each invocation and fails closed by documented exit table | private_integration |
| QUOTA-02 | §10 | Ollama credentials appear in no child argv/environment/log and owned material is securely removed | private_integration |
| STATE-01 | §11, §17 | One minimal atomic state file enforces the explicit monotonic transition table and detects tampering | unit |
| STATE-02 | §11, §13, §14, §15 | The verification outcome `software_verified_external_acceptance_blocked` records software fully verified while external release acceptance remains blocked; it advances to the independent audit, can never produce campaign success, and never weakens readiness or human authority | unit |
| BUDGET-01 | §14.1 | The adaptive campaign/audit scheduler is a pure generic authority: every milestone, objective-coverage, no-progress, and terminal decision is a deterministic function of the committed `factory-campaign-budget/v1` budget and the trusted plan/state/outcome inputs; the budget is closed-config with bounded values and duplicate-key rejection, security-sensitive paths are trusted closed config (never plan prose) and reject absolute/traversal/glob paths, mandatory audit objectives must all be covered before success, and the campaign terminates honestly on verified completion, external-acceptance-blocked, no progress, and round/checkpoint budget exhaustion | unit |
| LOCK-01 | §12 | Canonical root-descriptor lock enforces one writer and is not inherited or unlockable by untrusted children | private_integration |
| PROC-01 | §9, §12, §17 | Bounded process-session signaling, escaped-child detection, full reap, and dirty-work preservation hold | private_integration |
| GIT-01 | §12, §17 | Canonical repository/branch/spec/plan bindings and guarded commit boundary fail closed | installed |
| PHASE-01 | §13, §14 | All phase and campaign outcomes terminate or advance exactly as specified without no-task spin | private_integration |
| COMPLETE-01 | §15 | Task, work exhaustion, verification, audit, product acceptance, and campaign success remain distinct | unit |
| FIND-01 | §16 | Findings reach later developers only through a planner revision of the canonical plan | private_integration |
| CRED-01 | §18 | Existing Pi tool-call/tool-result credential enforcement and trusted SDK authority remain active | private_integration |
| EVID-01 | §19 | Evidence tiers, exact receipts/manifests, immutable verifier binding, and no-elevation rules remain authoritative | installed |
| EVID-02 | §19 | Verifier failures are recorded as strict structured artifacts (exact command as data, exit status, expected vs observed, bounded output tail/reference, changed files, artifact refs, environment/capability classification, rerun scope) with bounded sizes, closed enums, and duplicate-key rejection; a trusted deterministic software verifier failure converges to the SAME task at most twice per task (`MAX_CONVERGENCE_RETRIES = 2`) with the artifact digest-bound and rendered as inert data, and a repeated identical failure or consumed task resource budget terminates the loop honestly | unit |
| VIS-01 | §19 | Visual evidence preserves exact-byte provenance and never substitutes machine review for human authority | installed |
| RUNNER-01 | §19 | Runner/capability evidence remains signed, exact-commit, non-skipped, and non-simulated | real_system |
| HIDE-01 | §3 | Harness files remain within hidden namespaces/external prefix and never pollute product/build/package paths | installed |
| MIG-01 | §21 | Generic-first migration preserves code, plan, evidence, blockers, and dirty work without importing Ralph control state | installed |
| TEST-01 | §22 | The full adversarial conformance suite and documentation synchronization pass | installed |
| ACCEPT-01 | §23 | Boilerplate acceptance requires all mapped requirements verified and an independent audit without critical findings | installed |
| LEASE-01 | §14.2, §14.2a, §14.2b, §14.2c, §22, §24 | The task-scoped path-lease authority is strict and foundation-only: the committed `factory-path-lease-policy/v1` config maps closed scope IDs to bounded repository-relative path prefixes/patterns for product-owned verification surfaces with absolute non-overridable deny zones (`.factory`, `.factory-state`, `.git`, product spec, credential/key/env authorities, and all goldens/approval/release/human-authority surfaces; no carve-out exists in the schema), deny is dominant over allow, the policy rejects absolute/traversal/backslash/control/symlink-ambiguous patterns, duplicate keys, unsafe globs, and overlapping deny escapes, and `factory-task-path-lease/v1` claims bind campaign/task/attempt/HEAD/plan digest/policy digest/scopes/expanded paths/issued-deadline/nonce with canonical digests, replay/expiry/context fail-closed, no free-form commands, no symlink resolution, and `audit_required` for security-sensitive scopes; the optional plan `Write scopes:` request field is closed-format and grants nothing by itself. The Phase 2C2a runtime-launch foundation binds the claim into the existing signed launch authority as an optional developer-only extension: the exact canonical claim bytes are re-validated against the committed policy and the trusted context before spawn and inside the confined launcher, the claim digest alone is never authoritative (a real-provider launch without the signed HMAC/FD launch token fails closed), the exact deny-dominant write candidates are granted as WRITE-only workspace-confinement rules (no-follow, no symlink/hardlink/mount escape, existing paths only), CI/security scopes set the immutable `audit_required` signal on the binding and launch result, and default behavior without a lease is unchanged. The Phase 2C2b-A campaign mints one unique claim per selected developer attempt from the committed plan `Write scopes:` request and the committed HEAD policy blob (never the worktree), clamps the deadline to the attempt/task/campaign budget, fails closed on unknown/forbidden/unavailable/nonexistent requests (never a silent fallback or broad write), and delivers the exact bytes+digest through the existing launch path without persisting any authority secret/token. The Phase 2C2b-B scheduler/state wiring records a sticky pending lease-audit trigger bound to the campaign/task/attempt/lease digest and the exact resulting candidate commit whenever a developer attempt's lease returns `audit_required`, forces the independent tester/auditor milestone regardless of the configured interval, consumes a trigger only on a passing audit at that exact commit (findings/blocked/skipped/infrastructure/interrupted/stale/foreign/replay cannot clear; a same-task trigger whose bound commit is a verified ancestor of a new commit is superseded), blocks success while any trigger is pending, and keeps the trusted verifier independent so leased modifications to scripts/nix/packaging/ci cannot self-certify | unit |
