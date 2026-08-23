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
- `last_outcome`, which is a trusted control-plane enum, not an evidence claim.

No wall-clock timestamp or additional field is accepted by the schema.

It MUST NOT contain model prose, task descriptions, memories, evidence claims, or copies of the plan.

All writes MUST be atomic, no-follow, ownership/mode/link-count checked, and validated against this transition table:

```text
planning -> implementation -> verification -> audit
planning --attempts-exhausted--> failed
implementation --dirty-attempts-exhausted--> interrupted
verification --infrastructure-failure--> infrastructure_failure
audit --nonfinal--> planning(next round)
audit --final--> success | findings | blocked
```

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
- `blocked`: every unfinished task is explicitly blocked on unavailable external or human evidence.

`work_exhausted` and `blocked` are not product acceptance. `task_failed` and `task_progress` retry the same plan task while its bounded attempt budget remains. If the budget expires with dirty work, the campaign terminates `interrupted` and verification does not run. If the budget expires cleanly with a reproducible task failure, the trusted harness records a finding and proceeds to verification/audit at the last coherent commit.

### 13.3 Verification

Outcomes:

- `pass`;
- `findings`;
- `blocked` when a required declared capability cannot execute;
- `infrastructure_failure` when the verifier itself cannot be trusted.

Verification `findings` or `blocked` do not prevent the independent audit from running. In a non-final round they advance to audit and then become next-round plan inputs. `blocked` means a required, correctly declared capability or human/external authority is unavailable while the verifier and binding remain trusted. `infrastructure_failure` means verifier identity, digest, execution, receipt publication, or control-plane trust is invalid; it fails closed and stops the campaign.

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

For the final round:

- complete product acceptance and audit pass produce campaign `success`;
- any unresolved software, test, documentation, security, or audit defect produces terminal nonzero `findings`;
- if there are no such defects and every unresolved mandatory item exclusively requires unavailable external, hardware, declared-capability, or human authority, the result is terminal nonzero `blocked`;
- if both categories exist, `findings` takes precedence;
- state and evidence are preserved for a later campaign after circumstances change.

Planning-attempt exhaustion produces `failed`; dirty implementation-attempt exhaustion or operator/process interruption produces `interrupted`; untrusted verifier/control-plane failure produces `infrastructure_failure`. A finite campaign therefore always terminates as success, findings, blocked, failed, infrastructure failure, or interruption. It never spins because there is no runnable task.

## 15. Completion predicates

The following predicates MUST remain distinct:

1. **Task completion:** one plan task and its acceptance checks are complete.
2. **Implementation work exhaustion:** no runnable plan task remains.
3. **Verification pass:** deterministic checks at the exact commit passed.
4. **Audit pass:** independent review found no acceptance finding within scope.
5. **Product acceptance:** every normative specification requirement has adequate evidence, including required real-system and human evidence.
6. **Campaign success:** final-round product acceptance and audit pass.

No lower predicate implies a higher predicate.

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
27. no new implementation depends on Ralph lifecycle events, tokens, shims, runtime tasks, or memories.

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
| PLAN-01 | §7 | `factory-plan/v1` binds spec/base/tasks/requirements/interactions/conformance and parses unambiguously | unit |
| TASK-01 | §7, §8 | Task transitions and deterministic priority-plus-ID selection are trusted and plan-derived | unit |
| TASK-02 | §9, §20 | Delivered task bytes and digest exactly match the committed plan | installed |
| QUOTA-01 | §10 | Ollama check/wait behavior runs before each invocation and fails closed by documented exit table | private_integration |
| QUOTA-02 | §10 | Ollama credentials appear in no child argv/environment/log and owned material is securely removed | private_integration |
| STATE-01 | §11, §17 | One minimal atomic state file enforces the explicit monotonic transition table and detects tampering | unit |
| LOCK-01 | §12 | Canonical root-descriptor lock enforces one writer and is not inherited or unlockable by untrusted children | private_integration |
| PROC-01 | §9, §12, §17 | Bounded process-session signaling, escaped-child detection, full reap, and dirty-work preservation hold | private_integration |
| GIT-01 | §12, §17 | Canonical repository/branch/spec/plan bindings and guarded commit boundary fail closed | installed |
| PHASE-01 | §13, §14 | All phase and campaign outcomes terminate or advance exactly as specified without no-task spin | private_integration |
| COMPLETE-01 | §15 | Task, work exhaustion, verification, audit, product acceptance, and campaign success remain distinct | unit |
| FIND-01 | §16 | Findings reach later developers only through a planner revision of the canonical plan | private_integration |
| CRED-01 | §18 | Existing Pi tool-call/tool-result credential enforcement and trusted SDK authority remain active | private_integration |
| EVID-01 | §19 | Evidence tiers, exact receipts/manifests, immutable verifier binding, and no-elevation rules remain authoritative | installed |
| VIS-01 | §19 | Visual evidence preserves exact-byte provenance and never substitutes machine review for human authority | installed |
| RUNNER-01 | §19 | Runner/capability evidence remains signed, exact-commit, non-skipped, and non-simulated | real_system |
| HIDE-01 | §3 | Harness files remain within hidden namespaces/external prefix and never pollute product/build/package paths | installed |
| MIG-01 | §21 | Generic-first migration preserves code, plan, evidence, blockers, and dirty work without importing Ralph control state | installed |
| TEST-01 | §22 | The full adversarial conformance suite and documentation synchronization pass | installed |
| ACCEPT-01 | §23 | Boilerplate acceptance requires all mapped requirements verified and an independent audit without critical findings | installed |
