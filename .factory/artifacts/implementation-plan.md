---
spec_path: docs/FACTORY-LOOP-SPEC.md
spec_commit: 2d6a4fd1bd70866f7ff47c2128c8f7e850c40760
spec_blob: ca2334abf18a6557eb09c9baeb4b03bb3df523a4
base_commit: 2d6a4fd1bd70866f7ff47c2128c8f7e850c40760
status: active
---

# Implementation Plan

## Goal and non-goals

Goal: implement the minimal fresh-context software-factory loop specified by
`docs/FACTORY-LOOP-SPEC.md` as a new Python control plane living only under the
hidden `.factory/` namespace with runtime state under ignored `.factory-state/`.
The redesign retains the existing Ollama usage guard, the Git commit boundary,
the credential tool-call/tool-result enforcement, and the exact-commit
evidence/runner/visual machinery, hardens the Ollama credential transport, and
migrates off the Ralph Orchestrator control plane with visible `scripts/ralph-*`
entrypoints reduced to deprecated forwarders (or removed) only after parity is
proven.

Non-goals:

- no durable semantic memory, runtime task ledger, event stream, or loop lock;
- no `main`-branch promotion, no autonomous release, no product-spec invention
  (`docs/SPEC.md` remains the adopting-product placeholder and is not planned
  against);
- no adaptive model subroles, no parallel model launches, no Rust toolchain;
- no new orchestration added to the visible product `scripts/` directory beyond
  adaptation of existing configuration readers and deprecation forwarders;
- this boilerplate cycle does not plan, build, or test the Controller product.

## Architecture and constraints

- Control-plane implementation is Python 3.11+ standard library under
  `.factory/loop/` (plan parser, selector, state, locking, launch/supervision,
  phase machine, conformance helpers). POSIX shell is limited to small operator
  entry points. The existing secure Pi wrapper (`scripts/pi2-secure-exec.py`)
  is invoked, never reimplemented.
- Committed schema `factory-plan/v1` (Markdown + schema files under
  `.factory/schemas/`) is the plan contract; the deterministic parser is part
  of the acceptance boundary, round-trips without semantic loss, and rejects
  any plan it cannot bind exactly: a UTF-8 BOM prefix never parses; a
  `verified` conformance row must reference only `complete` tasks and may
  not appear in an `active` lifecycle plan; the conformance matrix must
  cover every ID in the committed §24 machine registry
  (`.factory/schemas/factory-plan-v1.requirements.json`); and the plan
  lifecycle status must be consistent with the task statuses (Task 2,
  Task 18).
- Exactly one minimal mutable control-state file
  `.factory-state/factory-loop.json` (schema `factory-state/v1`) carries only
  the §11 fields and enforces the §11 transition table; append-only evidence
  artifacts are never orchestration state.
- Locking is an exclusive `flock` on the canonical Git top-level directory
  descriptor (`O_DIRECTORY|O_NOFOLLOW|O_CLOEXEC`); the descriptor and lock
  metadata are never inherited by model processes, and no second writer,
  worktree, or parallel lifecycle mutation is permitted.
- Every phase runs a fresh model process with the static role prompt digest
  bound at campaign start, deterministic audit-objective digest, exact bound
  commit, and a selected task excerpt whose bytes are re-derived from the
  committed plan and digest-matched before launch.
- Ollama `--check`/`--wait` runs before every model invocation under the
  retained decision table; credential material appears in no child argv,
  environ, log, or repository state and is transported via a mode-0600
  mechanism, with owned temporary material erased.
- The existing evidence machinery (exact-commit receipts/manifests, capability
  contracts, immutable verifier binding, visual provenance, installed and
  runner evidence) stays authoritative and is re-verified on the new path.
- Migration is generic-first: the Ralph control plane is frozen, `.ralph/` is
  archived read-only outside the model-visible workspace, and the completed
  design must not depend on `ralph emit`, completion tokens, Ralph event
  streams, runtime task stores, or Ralph memories.
- The redesign is accepted only when `verify-boilerplate.sh`, the §2 §2
  conformance suite, the conformance sidecar for every §24 requirement ID, and
  an independent final audit pass on the boilerplate.

## Specification conformance matrix

Every normative requirement in `docs/FACTORY-LOOP-SPEC.md` §24 is mapped to
the bounded tasks below. Classification in this fresh plan is `missing` when
the behavior does not exist yet and `partial` when existing machinery is
retained but must be re-bound/hardened; no row is `verified` until its task
completes with evidence.

| ID | Spec § | Classification | Evidence | Task |
|----|--------|--------------|----------|------|
| AUTH-01 | §5, §7 | partial | existing spec/plan authority retained; new loop binds the config spec to the FACTORY-LOOP-SPEC and keeps the plan as the sole task ledger | Task 1, Task 8 |
| CTX-01 | §5, §9 | missing | fresh process per role with disabled session resume/memory injection implemented | Task 6 |
| CTX-02 | §5, §18 | missing | legacy `.ralph/`, `.factory-state/`, scratchpad, task, and memory paths unavailable to model tools | Task 8 |
| ROLE-01 | §6 | missing | four distinct static roles (planner/developer/tester/auditor) with no adaptive model roles | Task 8 |
| PLAN-01 | §7 | missing | `factory-plan/v1` schema and parser binding spec/base/tasks/requirements/interactions/conformance unambiguously, with byte-exact round-trip, §24 registry coverage, and range-bounds, lifecycle-field, traversal, and final-audit invariants closed by exact adversarial fixtures | Task 2, Task 14, Task 18 |
| TASK-01 | §7, §8 | missing | trusted task transitions and deterministic priority-then-ID selection | Task 3, Task 9 |
| TASK-02 | §9, §20 | missing | delivered task bytes and digest exactly match the committed plan | Task 6 |
| QUOTA-01 | §10 | partial | existing `scripts/ollama-usage-guard.sh` `--check`/`--wait` contract retained and wired into every invocation | Task 7 |
| QUOTA-02 | §10 | missing | Ollama credentials absent from child argv/environ/log and owned material securely erased | Task 7 |
| STATE-01 | §11, §17 | missing | one minimal atomic control-state file enforcing the monotonic transition table and tamper detection | Task 4, Task 19, Task 9 |
| LOCK-01 | §12 | missing | canonical root-descriptor flock, one writer, non-inheritance and non-unlockable-by-second-descriptor | Task 5 |
| PROC-01 | §9, §12, §17 | missing | bounded process-session signaling, escaped-child detection, full reap, dirty-work preservation | Task 6 |
| GIT-01 | §12, §17 | partial | canonical repository/branch/spec/plan bindings and guarded commit boundary enforced in the new launcher | Task 5 |
| PHASE-01 | §13, §14 | missing | phase/campaign outcome machine with exact advance/terminate behavior and no no-task spin | Task 9 |
| COMPLETE-01 | §15 | missing | task, work-exhaustion, verification, audit, product-acceptance, and campaign-success predicates stay distinct | Task 9 |
| FIND-01 | §16 | missing | findings reach later developers only through a planner revision of the canonical plan | Task 10 |
| CRED-01 | §18 | partial | existing Pi credential tool-call/tool-result enforcement and trusted SDK authority retained | Task 11 |
| EVID-01 | §19 | partial | existing exact-commit receipts/manifests and immutable verifier binding retained | Task 12 |
| VIS-01 | §19 | partial | existing visual provenance machinery retained with exact-byte provenance | Task 12 |
| RUNNER-01 | §19 | partial | existing runner/capability receipt machinery retained | Task 12 |
| HIDE-01 | §3 | missing | harness-footprint conformance test inventories every installed file and fails on escapes | Task 13 |
| MIG-01 | §21 | missing | generic-first migration preserves code/plan/evidence/blockers without importing Ralph control state | Task 15 |
| TEST-01 | §22 | missing | full adversarial conformance suite (§22 tests 1-27) and documentation synchronization | Task 16, Task 17 |
| ACCEPT-01 | §23 | missing | boilerplate acceptance criteria, all §24 requirements mapped and verified, independent audit clean | Task 14, Task 19, Task 20 |

## Interaction acceptance inventory

- input boundary: each role receives only its static prompt, the concise
  `AGENTS.md`, the canonical specification, the canonical plan, and the current
  code/tests at the bound Git state; the selected task is byte- and
  digest-bound to the committed plan; the plan's machine gate is the
  committed parser, whose byte-exact round-trip, §24 registry coverage,
  `verified`-binding, and lifecycle checks make free-form prose unable to
  alter lifecycle fields (AUTH-01, CTX-01, TASK-02, PLAN-01).
- semantic boundary: no durable semantic memory, scratchpad prose, prior
  conversations, context summaries, or completion claims are injected or read
  as authority; findings reach later developers only through a planner
  revision of the plan (CTX-02, FIND-01).
- production boundary: the developer role is the only writer of product code;
  harness and runtime files stay confined to `.factory/`, `.factory-state/`,
  and `.pi/` and never touch product/build/package paths (HIDE-01, ROLE-01).
- evidence boundary: deterministic verification produces exact-commit
  receipts and manifests; visual and runner evidence retain byte provenance;
  a model completion token can never bypass a deterministic gate (EVID-01,
  VIS-01, RUNNER-01).

## Task 1: Bind the canonical specification and baseline harness config

- Status: complete
- Dependencies: None
- Scope: Point `.factory/config.toml [project].spec` at
  `docs/FACTORY-LOOP-SPEC.md` so plan freshness and planning gates resolve the
  redesign specification (commit `2d6a4fd`, blob `ca2334ab…`); adapt
  `scripts/check-spec-provided.sh` so it gates on the new canonical spec and
  never plans against `docs/SPEC.md` (which remains the adopting product
  placeholder, untouched and unplanned); adapt `scripts/check-plan-freshness.sh`
  and `scripts/plan-scope-guard.sh` to the new spec path without adding new
  orchestration to the visible `scripts/` tree.
- Acceptance criteria: `scripts/check-plan-freshness.sh` resolves the committed
  plan's spec binding to `docs/FACTORY-LOOP-SPEC.md` at commit `2d6a4fd` and
  blob `ca2334ab…` and exits 0; `scripts/check-spec-provided.sh` exits 0 while
  the new canonical spec is present; `docs/SPEC.md` is byte-unchanged and not
  planned against.
- Verification: `scripts/check-plan-freshness.sh` and
  `scripts/check-spec-provided.sh` run from a clean tree; `git diff HEAD -- docs/SPEC.md` is empty.
- Evidence: `.factory/config.toml [project].spec` now binds the redesign
  contract `docs/FACTORY-LOOP-SPEC.md` (commit `2d6a4fd`, blob `ca2334ab…`);
  `scripts/check-spec-provided.sh` gates on the bound canonical spec and
  hard-blocks any bound placeholder (missing/unsafe path or
  `SPEC_PENDING_HUMAN_SUPPLY` marker), so the adopting-product placeholder
  `docs/SPEC.md` is never planned against; `scripts/check-plan-freshness.sh`
  resolves the committed plan's binding to `docs/FACTORY-LOOP-SPEC.md` at
  commit `2d6a4fd` / blob `ca2334ab…` and exits 0 (committed and planning
  phases), and `scripts/plan-scope-guard.sh` still confines planning writes;
  `git diff HEAD -- docs/SPEC.md` is empty and the placeholder is byte-
  unchanged.
- Documentation impact: `docs/FACTORY.md`, `docs/OPERATIONS.md`.

## Task 2: `factory-plan/v1` schema and deterministic parser

- Status: complete
- Dependencies: Task 1
- Scope: Commit `factory-plan/v1` schema and the stdlib-only deterministic
  parser in `.factory/loop/plan_parser.py` per §7: canonical front matter
  (spec path/commit/blob, base commit, lifecycle status), unique task IDs,
  allowed statuses and transitions, dependency/priority fields, scope,
  acceptance, verification, documentation impact, conformance matrix rows, and
  interaction inventory. The parser rejects duplicate headings, unknown
  lifecycle states, ambiguous task sections, out-of-order or cyclic
  dependencies, and non-contiguous IDs, and round-trips without semantic loss.
  `scripts/validate-implementation-plan.py` remains a passing gate for the
  owned plan file.
- Acceptance criteria: the parser and the existing validator agree on the
  canonical committed plan; each documented defect class has an exact
  fixture; output is a deterministic function of the plan bytes.
- Verification: `.factory/tests/test-factory-plan-parser.py`; run
  `scripts/validate-implementation-plan.py planning .factory/artifacts/implementation-plan.md`.
- Evidence: `.factory/loop/plan_parser.py` is a stdlib-only deterministic
  `factory-plan/v1` parser with byte-exact `parse -> serialize -> parse`
  round-trip, deterministic JSON dump, the documented status transition
  table, and `PlanError` rejection of every documented defect class; the
  committed schema is `.factory/schemas/factory-plan-v1.schema.md` with the
  machine-readable model contract `.factory/schemas/factory-plan-v1.schema.json`.
  The harness-owned suite `.factory/tests/test-factory-plan-parser.py`
  (12 tests, all passing) proves agreement with `scripts/validate-implementation-plan.py`
  on the committed canonical plan, exact-fixture rejection for every defect
  class (`.factory/tests/fixtures/plan-*.md`), and byte-exact/deterministic
  round-trip; `scripts/validate-implementation-plan.py planning
  .factory/artifacts/implementation-plan.md` still exits 0.
- Documentation impact: `docs/FACTORY.md`.

## Task 3: Deterministic plan-derived task selection

- Status: complete
- Dependencies: Task 2
- Scope: implement `.factory/loop/selector.py` for §8: reject an invalid,
  stale, or ambiguously parsed plan; resume the sole `in_progress` task;
  otherwise sort runnable `pending` tasks (dependencies complete) by explicit
  numeric priority then lexicographic task ID; select exactly one; classify
  `work_exhausted` or `blocked` when none are runnable. The selector runs only
  inside the trusted control plane and never consults a runtime task ledger.
- Acceptance criteria: fixture plans and harness prove the exact selection
  order, single-task guarantee, deterministic tie-breaks, and the empty-work
  classifications; the selection is a pure function of the plan and state.
- Verification: `.factory/tests/test-factory-selector.py`; fixture corpus
  under `.factory/tests/fixtures/plan-select-*.md`.
- Evidence: `.factory/loop/selector.py` implements the §8 deterministic
  selection step as a pure, stdlib-only function of the parsed
  `factory-plan/v1` plan: it rejects a stale plan (front-matter base commit
  differs from the bound commit) and an ambiguous plan (an `in_progress`
  task whose dependencies are not all complete) before selecting; resumes
  the sole `in_progress` task; otherwise sorts runnable `pending` tasks
  (every dependency `complete`) by explicit numeric priority then
  lexicographic task identifier and selects exactly one; when none are
  runnable it classifies the phase `work_exhausted` (no pending or
  `in_progress` task remains) or `blocked` (unfinished tasks remain but none
  can run, each blocked directly or transitively through a blocked
  dependency). The selector performs no I/O and never reads a runtime task
  ledger, a control-state file, the environment, or process state, so the
  outcome is a deterministic function of the plan and the bound base commit.
  The hidden harness-owned suite `.factory/tests/test-factory-selector.py`
  (28 tests, all passing) proves the exact selection order, the
  single-task guarantee, deterministic tie-breaks (lexicographic task
  identifier, where `"10" < "2"`), dependency gating, stale/ambiguous
  rejection, and both empty-work classifications against the committed
  corpus `.factory/tests/fixtures/plan-select-*.md` (8 fixtures, all
  round-tripping byte-exactly); the canonical plan deterministically
  selects its first runnable pending task (Task 4 at the evidence commit);
  `scripts/validate-implementation-plan.py planning` and
  `scripts/check-plan-freshness.sh` still exit 0. The verification path
  stays under the hidden `.factory/` namespace per HIDE-01 (the visible
  `tests/` tree is product-owned).
- Documentation impact: `docs/FACTORY.md`.

## Task 4: Minimal mutable control state

- Status: complete
- Dependencies: Task 1
- Scope: Implement the single mutable control-state file
  `.factory-state/factory-loop.json` under schema `factory-state/v1` with
  exactly the §11 field set: schema, repository identity, branch, campaign id,
  round counters, phase, spec/plan/prompt-set digests, base commit, selected
  task id, attempt counters, monotonic phase/attempt start markers, and a
  trusted `last_outcome` enum. All writes are atomic, no-follow, and
  ownership/mode/link-count checked (reuse `scripts/factory_state_io.py`);
  the §11 transition table is enforced; completed-phase bindings are
  write-once; counters are monotonic; the state digest is recorded before each
  untrusted phase and re-validated after.
- Acceptance criteria: every tamper class (forged field, mode/owner/link
  change, counter rewind, wrong path identity, illegal transition) fails
  closed; every legitimate transition advances exactly as the §11 table
  specifies; the state file is the only mutable lifecycle file.
- Verification: `.factory/tests/test-factory-state.py`; adversarial fixture
  files under `.factory/tests/fixtures/state-*`.
- Evidence: `.factory/loop/state.py` implements the single `factory-state/v1`
  authority `.factory-state/factory-loop.json` with exactly the §11 field set
  (rejecting both extra and missing fields), the §11 transition table edge
  for edge, write-once campaign/plan bindings, monotonic round/attempt and
  monotonic phase/attempt start counters, a trusted `last_outcome` enum, a
  deterministic canonical state digest, and a before/after untrusted-phase
  digest ledger (`.factory-state/state-digest-ledger.jsonl`, evidence only).
  All file I/O reuses the established no-follow authority
  `scripts/factory_state_io.py` (atomic publication through a mode-0600
  temporary and `linkat`, with ownership/mode/link-count and (dev, inode)
  identity checks), and loading re-validates `repository_identity` against the
  canonical root descriptor and any expected campaign binding, so forged,
  moved, symlinked, oversized, wrong-mode, or wrong-owner state fails closed.
  The hidden harness-owned suite `.factory/tests/test-factory-state.py` (91
  tests, all passing; the owner-tamper test skips unless run as root) proves
  the committed `state-*.json`/`state-*.jsonl` corpus, the exact §11
  transition edges, retry/attempt budgets, round finality, write-once
  bindings, digest determinism, secure atomic I/O, the append-only ledger,
  and the trusted control-plane CLI (`init`/`show`/`digest`/`advance`/
  `begin-attempt`/`record-retry`/`record-phase-digest`/`verify-phase-digest`)
  printing one machine-readable outcome and failing closed on every
  documented defect class. The public `factory-state/v1` API is exported from
  the hidden `.factory/loop/__init__.py` control-plane package. STATE-01
  remains co-owned by pending Task 9 (phase/campaign orchestration) so the §11
  transition machinery is exercised by the trusted control plane, not only by
  the unit suite; `scripts/validate-implementation-plan.py planning`,
  `scripts/check-plan-freshness.sh`, `scripts/check-generic-leakage.sh`, and
  `scripts/check-docs-sync.sh` still exit 0.
- Documentation impact: `docs/OPERATIONS.md`.

## Task 5: Root-descriptor lock and Git writer boundary

- Status: pending
- Dependencies: Task 4
- Scope: Implement the lock authority: exclusive `flock` on the already-open
  canonical Git top-level directory descriptor opened with
  `O_DIRECTORY|O_NOFOLLOW|O_CLOEXEC`; validate canonical repository identity,
  required branch, and spec/plan bindings before launch; close the lock
  descriptor in every child before exec and strip lock metadata from the
  child environment; start a new process session; make a separately opened
  repository descriptor unable to unlock the holder; detect double-fork or
  `setsid` escape and fail closed for operator inspection. Preserve the Git
  command-boundary guard so `--no-verify`, hook-path override,
  `GIT_CONFIG_*`, worktrees, amend/merge/rebase bypasses, and forged handoffs
  remain rejected. Invoke the Git binary through a PATH-pinned absolute
  executable (never an unqualified `git` resolved from a caller-controlled
  PATH) so an attacker-controlled PATH cannot substitute a different `git`
  behind the guarded commit boundary.
- Acceptance criteria: concurrent launcher probes prove exactly one writer;
  an untrusted leaf inherits no lock descriptor and no lock environment; the
  escaped-descendant case blocks recovery; commit-boundary bypass tests stay
  rejected.
- Verification: `tests/test-factory-lock.py` extended with inheritance and
  escape fixtures; `tests/test-git-commit-guard.sh` still passes.
- Documentation impact: `docs/OPERATIONS.md`.

## Task 6: Fresh-context execution, invocation contract, and supervision

- Status: pending
- Dependencies: Task 3, Task 5
- Scope: Implement `.factory/loop/launch.py`: every role starts in a new
  fresh process via the existing secure wrapper (`scripts/pi2-secure-exec.py`)
  in one-shot mode with disabled session resume and memory injection; the
  invocation binds exact model/provider, static role prompt digest,
  campaign-bound prompt-set digest, deterministic audit-objective digest,
  canonical workspace and bound commit, selected task ID with an excerpt whose
  bytes are re-derived from the committed plan blob and digest-matched (fail
  closed on substitution/paraphrase), allowed tools, and runtime/inactivity
  bounds. Supervision delivers TERM, INT, and HUP to the full process group,
  then reaps with a bounded grace escalating to KILL; escaped children are
  detected; dirty or interrupted work is preserved and never silently
  overwritten; the machine-readable exit status is the only completion signal.
- Acceptance criteria: process invariants (new session, no inherited lock/env,
  no resume) verified per launch; excerpt digest match/mismatch fixtures pass;
  signal delivery and reap fixtures pass; a crashed attempt leaves its dirty
  work intact.
- Verification: `tests/test-factory-launch.py`;
  `tests/test-factory-supervision.sh`.
- Documentation impact: `docs/OPERATIONS.md`.

## Task 7: Ollama usage guard retention and credential hardening

- Status: pending
- Dependencies: Task 6
- Scope: Retain the `scripts/ollama-usage-guard.sh` `--check`/`--wait`
  contract and the §10 decision table, wired into the control plane before
  every model invocation. Harden the credential transport: cookies and
  credentials never appear in child argv, child environments, logs, or
  repository state; transport via a bounded stdin or a mode-0600 descriptor or
  file; erase all owned temporary material; expose only redacted status;
  signals received while waiting terminate the wait and campaign cleanly.
  Add a conformance test that inspects a live synthetic child's
  `/proc/<pid>/cmdline` and `/proc/<pid>/environ` and fails if the synthetic
  cookie name or value appears.
- Acceptance criteria: the exit table is enforced by fixtures; the
  synthetic-secret probe never leaks into cmdline/environ; waiting aborts
  cleanly on signal.
- Verification: `tests/test-pi2-ollama-wrapper.sh` extended with the proc
  probe; `tests/fixtures/usage-ok.html` and `usage-blocked.html` still drive
  the parse path.
- Documentation impact: `docs/OPERATIONS.md`.

## Task 8: Role prompts, prompt-set binding, and workspace confinement

- Status: pending
- Dependencies: Task 6
- Scope: Commit distinct static role prompts for planner, developer, tester,
  and auditor under `.factory/prompts/` with campaign-bound digests; commit
  the audit-objective registry bound at campaign start and selected
  deterministically per round. Implement model workspace confinement so
  `.ralph/`, `.factory-state/`, prior scratchpads and handoffs, runtime task
  stores, memory stores, and migration archives are unavailable through model
  tools; the plan, spec, code/tests, and allowlisted `.factory/` inputs are
  readable, and role write allowlists are honored. No memory, conversation, or
  context-summary authority is injected.
- Acceptance criteria: each role launch proves it can read the allowlisted
  inputs and cannot read any forbidden path; the role-prompt digests match the
  campaign binding; no completion claim from a previous attempt is present in
  the fresh context.
- Verification: `tests/test-factory-confinement.sh`.
- Documentation impact: `docs/FACTORY.md`, `docs/OPERATIONS.md`.

## Task 9: Phase and campaign state machine with outcomes

- Status: pending
- Dependencies: Task 4, Task 7, Task 8
- Scope: Implement the phase/campaign orchestration: `planning ->
  implementation -> verification -> audit` with the §11 transition table,
  §13 phase-outcome classification (planned/failed/interrupted;
  task_completed/progress/failed/interrupted/work_exhausted/blocked; pass/
  findings/blocked/infrastructure_failure; pass/findings/blocked), §14 finite
  round semantics, and the §15 predicate ladder kept distinct. Rounds advance
  only on non-final audit; phase never moves backward; counters are
  monotonic; a finite campaign always terminates (success, findings, blocked,
  failed, infrastructure_failure, interrupted) and never spins on empty work.
- Acceptance criteria: fixture campaigns for each outcome and the success/
  blocked/findings/failed/interrupted combinations terminate within the
  configured bounds; no phase transition violates the state machine.
- Verification: `tests/test-factory-phase.py`;
  `tests/test-factory-campaign.sh`.
- Documentation impact: `docs/FACTORY.md`.

## Task 10: Findings flow

- Status: pending
- Dependencies: Task 9
- Scope: Structure tester/auditor findings as exact-commit, receipt-backed
  findings that are incorporated into the canonical plan only by the next
  planner revision. No independent runtime task queue may exist; evidence
  ledgers stay out of task/memory authority; verification `findings`,
  `blocked`, and audit results become next-round planner inputs through the
  plan, not through memory injection.
- Acceptance criteria: a fixture finding reaches the next developer only via
  a revised plan task; no code path reads a separate task ledger; receipts are
  the only acceptance evidence.
- Verification: `tests/test-factory-findings.sh`.
- Documentation impact: `docs/FACTORY.md`.

## Task 11: Credential and security boundary retention

- Status: pending
- Dependencies: Task 6, Task 8
- Scope: Preserve the existing Pi tool-call/tool-result credential guard and
  trusted SDK authority; the retained extension contains only required
  credential enforcement and guarded Git boundary behavior. No model tool can
  dump environment, authentication files, private keys, or secrets; tool-result
  redaction, stdin bounded command checks, and sanitized logs remain active.
  Ralph lifecycle topics, `ralph emit`, completion-token handling, event
  snapshots, launch handshakes, and Ralph CLI shims are removed from the new
  path and never reimplemented.
- Acceptance criteria: adversarial fixture attempts to exfiltrate secrets
  through tools, results, logs, argv, or environment all fail closed; the
  secure wrapper and credential-guard behavior remain unchanged in contract.
- Verification: `tests/test-credential-extension.sh`;
  `tests/test-credential-guard.sh`.
- Documentation impact: `docs/OPERATIONS.md`.

## Task 12: Evidence, verifier, and runner machinery retention

- Status: pending
- Dependencies: Task 5, Task 9
- Scope: Retain exact-commit signed runner receipts, capability contracts,
  visual provenance, atomic publication, installed and human evidence tiers,
  and the receipt wrapper (`scripts/machine-receipt.py`). The verifier
  entrypoint is opened and bound to its committed blob/identity before
  untrusted execution, and later pathname substitution fails closed. PASS
  requires exit 0 and verified identity/commit/digests; any BLOCKED evidence
  forces an audit `findings` result; audits cite `[receipt: …]` /
  `[manifest: …]` exact references. Receipt publication happens only through
  the trusted control plane.
- Acceptance criteria: fixtures prove the receipt wrapper remains
  authoritative, the immutable verifier binding rejects path substitution,
  and no model assertion can elevate evidence.
- Verification: `tests/test-factory-receipts.sh`;
  `tests/test-runner-signer.sh`.
- Documentation impact: `docs/OPERATIONS.md`.

## Task 13: Harness isolation and installed-footprint inventory

- Status: pending
- Dependencies: Task 1
- Scope: A conformance test inventories every file the harness installs or
  generates and fails when a harness-owned path escapes the hidden
  `.factory/`, runtime `.factory-state/`, or `.pi/` namespaces or the external
  executable prefix. Product source, test, packaging, and build discovery must
  exclude the hidden namespaces; deleting the hidden namespaces must remove
  the harness without deleting product code. The generic-leak check and the
  forbidden-root-file check keep product root clean.
- Acceptance criteria: the installed-file inventory test passes; a fixture
  harness file placed in a product path fails the gate; build/packaging
  discovery yields no `.factory/` artifacts.
- Verification: `tests/test-factory-footprint.sh`;
  `scripts/check-generic-leakage.sh`.
- Documentation impact: `docs/FACTORY.md`.

## Task 14: Conformance sidecar and requirement policy for the §24 registry

- Status: pending
- Dependencies: Task 12, Task 13
- Scope: Populate the existing machine-readable conformance sidecar
  (`.factory/artifacts/conformance.json`) and requirement policy
  (`.factory/requirement-policy.json`) with every §24 requirement ID, its
  minimum evidence tier, required capability annotations, exact evidence
  commit references, and receipt/artifact references. Classifications retain
  fail-closed semantics; only all-verified can produce campaign success;
  `blocked`/`partial`/`not_applicable` keep their blocking behavior.
- Acceptance criteria: `scripts/validate-conformance.py` passes with the
  populated sidecar; every §24 ID appears in both the sidecar and the policy
  with matching required tiers; no requirement is self-declared.
- Verification: `scripts/validate-conformance.py`;
  `scripts/check-capability-evidence.py`.
- Documentation impact: none (sidecar is machine data).

## Task 15: Migration and deprecation of the Ralph control plane

- Status: pending
- Dependencies: Task 9, Task 10
- Scope: Freeze new Ralph Orchestrator campaign launches; preserve existing
  `.ralph/` and campaign artifacts as read-only recovery history outside the
  model-visible workspace; migrate the active plan and campaign cursor into
  the single minimal state file; do not translate runtime tasks, memories, or
  completion tokens into the new authorities. Existing visible
  `scripts/ralph-*` entry points become deprecated forwarders only, and the
  completed design must not require them; new orchestration implementation
  never lands in the visible `scripts/` directory. New-path source and tests
  reject any dependency on `ralph emit`, completion tokens, Ralph event
  streams, runtime task stores, or Ralph memories. Port to the Controller
  product occurs only after generic verification, and this task stays
  generic-only. Legacy mutable control-state files (pre-existing lifecycle
  state) must coexist with or migrate into the single `factory-state/v1`
  authority without conflict, and no second mutable control-state authority
  may be created. Explicitly remove and deprecate the legacy persisted
  context-summary authority from new control flow: because the
  FACTORY-LOOP-SPEC forbids persisted context summaries and any competing
  task authority, the new path must never generate, read, inject, or validate
  `.factory/artifacts/context-summary.md`, nor invoke or depend on
  `scripts/ralph-context-summary.py` (generator) or
  `scripts/check-context-summary.py` (verifier), and must strip the
  context-summary wiring out of the new orchestration/launch/commit path
  (the `verify-boilerplate.sh`, `final-gate.sh`, `git-commit-hook.sh`, and
  `ralph-run.sh` invocation/checkpoint lines) so the stale mirror never
  competes with the canonical plan as a task authority. The visible scripts
  remain present only as deprecated, non-wired legacy entry points until
  removed; they are never a new-path dependency.
- Acceptance criteria: migration fixtures prove plan/commits/dirty-work/
  evidence/blockers survive while no `.ralph/` runtime state is imported;
  deprecation forwarders are marked and optional; the generic suite has no
  Ralph dependency; the new control flow generates, checks, or reads no
  context summary, and the stale
  `.factory/artifacts/context-summary.md` (with its
  `scripts/ralph-context-summary.py` / `scripts/check-context-summary.py`
  verifier and `tests/test-context-summary.sh` wiring) is removed/deprecated
  from every new-path control step, so a plan-mirror drift cannot surface as
  an acceptance failure.
- Verification: `tests/test-factory-migration.sh` proves the new path has no
  context-summary dependency (no new-path code generates or reads
  `.factory/artifacts/context-summary.md`, and
  `scripts/ralph-context-summary.py` / `scripts/check-context-summary.py` /
  `tests/test-context-summary.sh` are absent from or unreachable in the new
  control flow); the legacy `tests/test-context-summary.sh` suite is not
  invoked by the new loop.
- Documentation impact: `docs/OPERATIONS.md`, `README.md`.

## Task 16: Adversarial conformance suite and verification gate

- Status: pending
- Dependencies: Task 3, Task 4, Task 5, Task 6, Task 7, Task 8, Task 9, Task 10, Task 11, Task 12, Task 13, Task 14, Task 15
- Scope: Implement the full §22 conformance suite (tests 1-27: fresh roles
  and allowed inputs, memory/session disabled, deterministic selection, no
  runtime ledger, findings only via plan, empty work reaches verification/
  audit, external blockers end nonzero without elevation, pass impossible with
  unresolved findings, one-writer lock concurrency, full process-group signal
  delivery and reap, timeout/crash dirty-work preservation, tamper state fail
  closed, Ollama check/wait before invocation with quota errors blocking,
  credential enforcement and redaction active, exact-commit receipt/manifest
  trust, no completion-token bypass, finite termination fixtures for every
  outcome, migration without Ralph imports, lock non-inheritance, setsid
  escape, task-excerpt byte binding, context confinement, mid-phase mutation
  fail closed, synthetic Ollama cookie argv/environ, immutable verifier
  descriptor, Git commit-boundary rejection, no Ralph lifecycle dependency).
  Rework `scripts/verify-boilerplate.sh` so it runs the new-suite and the
  generic implementation acceptance; a synthetic five-round campaign completes
  with both success and findings fixtures.
- Acceptance criteria: every §22 test passes deterministically on clean
  trees; verify-boilerplate.sh fails on any of the adversarial fixtures.
- Verification: `./scripts/verify-boilerplate.sh`;
  `tests/test-factory-adversarial.sh`.
- Documentation impact: `AGENTS.md` validation commands.

## Task 17: Documentation synchronization

- Status: pending
- Dependencies: Task 16
- Scope: Synchronize README, `docs/FACTORY.md`, `docs/OPERATIONS.md`, the
  concise `AGENTS.md`, and the installed help/usage text to the new Python
  factory loop; document the canonical specification path, the single state
  file, role prompts, lock/security/evidence boundaries, and the migration/
  deprecation status. The documentation checker and `check-docs-sync.sh` must
  pass.
- Acceptance criteria: all listed documents reflect the implemented loop and
  pass the doc gates; `AGENTS.md` stays at or under the concise length limit
  and names deterministic commands.
- Verification: `scripts/check-docs-sync.sh`; the docs gate inside
  `scripts/verify-boilerplate.sh`.
- Documentation impact: README.md, `docs/FACTORY.md`, `docs/OPERATIONS.md`,
  `AGENTS.md`, help text.

## Task 18: Close `factory-plan/v1` untrusted-plan acceptance gaps

- Status: complete
- Dependencies: Task 2
- Scope: Harden the Task 2 parser boundary with exact adversarial fixtures
  under `.factory/tests/fixtures/`:
  1. reject UTF-8 BOM input and make `roundtrip`/`serialize` compare actual
     input and output bytes, including trailing blank lines
     (`plan-bom.md`, `plan-trailing-blank-line.md`);
  2. reject `verified` rows with empty/`None` task refs or any non-complete
     referenced task (`plan-verified-empty-refs.md`,
     `plan-verified-pending.md`), and reject `active` plans containing a
     `verified` row;
  3. commit `.factory/schemas/factory-plan-v1.requirements.json` from the 24
     stable §24 IDs and reject missing, extra, or duplicate matrix IDs
     (`plan-matrix-missing-id.md`, `plan-matrix-extra-id.md`);
  4. reject lifecycle `complete` unless every task is complete
     (`plan-lifecycle-inconsistent.md`);
  5. parse dependency and matrix task references without materializing an
     attacker-sized range; bound every endpoint to the parsed task count and
     convert oversized integers/`OverflowError` to `PlanError`
     (`plan-dependency-range-oversize.md`, `plan-matrix-range-oversize.md`,
     `plan-range-overflow.md`);
  6. reject continuation lines on structured lifecycle fields rather than
     silently interpreting only their first line
     (`plan-structured-field-continuation.md`);
  7. reject empty interaction-boundary text so parser output always conforms
     to the JSON schema (`plan-empty-interaction.md`);
  8. reject absolute, empty, dot-segment, and `..` traversal specification
     paths (`plan-front-matter-traversal-path.md`);
  9. require the final-audit task to be last and depend on every other task,
     and enforce the existing validator's matrix/task consistency invariants
     (`plan-final-audit-misplaced.md`, `plan-final-audit-missing-dependency.md`,
     `plan-matrix-complete-only-pending.md`);
  10. add exact empty-required-value, missing-title, and duplicate-title
      fixtures rather than relying on neighboring error branches.
  Update the Markdown/JSON schema contracts and the hidden parser suite. The
  parser and existing validator must agree on every accepted fixture, not only
  the canonical plan.
- Acceptance criteria: every named invalid fixture raises a bounded documented
  `PlanError` without traceback, excessive allocation, or schema divergence;
  every accepted fixture serializes byte-identically and deterministically;
  the canonical matrix exactly equals the committed §24 registry; lifecycle,
  final-audit, path, interaction, and matrix invariants agree with the legacy
  validator; repeated oversized-range probes remain within a fixed memory/time
  ceiling.
- Verification: `.factory/tests/test-factory-plan-parser.py`;
  `scripts/validate-implementation-plan.py planning
  .factory/artifacts/implementation-plan.md`; bounded resource probe for range
  fixtures; `scripts/check-generic-leakage.sh`; `scripts/check-docs-sync.sh`.
- Evidence: `.factory/tests/test-factory-plan-parser.py` passes 13/13,
  including every named invalid fixture, byte-exact accepted fixtures,
  parser/legacy-validator agreement, deterministic transitions, and 600
  repeated oversized-range parses under enforced 10-second CPU and 32 MiB RSS
  ceilings. The §24 registry SHA-256 is
  `7d9f502995a7af00c0153093bddb38e2cb948fbe717742ba3d2b6fba9539b402`.
  `scripts/validate-implementation-plan.py planning`,
  `scripts/check-generic-leakage.sh`, and `scripts/check-docs-sync.sh` exit 0;
  standalone parser round-trip is byte-exact. The planner applied Task 3's
  `blocked -> pending` transition in commit `21eafd7` after this evidence passed.
- Documentation impact: `docs/FACTORY.md`,
  `.factory/schemas/factory-plan-v1.schema.md`.

## Task 19: Harden factory-state/v1 authority (Task 4 review findings)

- Status: complete
- Dependencies: Task 4
- Priority: 1
- Scope: Review and harden the Task 4 `factory-state/v1` authority against the
  documented findings from the Task 4 review: S1 make `init` atomic with
  no-replace semantics so it never clobbers existing state or an existing
  campaign binding; S2 add crash-window and orphan recovery so a torn write
  or an orphaned temporary/leftover state artifact is recovered
  deterministically without data loss and without creating a second
  authority, failing closed when the existing state directory is unsafe,
  re-validating the canonical state after linking it into place and before
  deleting the quarantine, and checking the recovered state against the
  latest recorded digest-ledger entry when a ledger exists; S3 reject a
  zeroed `now=0`/epoch-zero monotonic start marker as
  tamper and fail closed; S6 add independent transition and state-digest
  fixtures authored separately from the code path they exercise (not derived
  by the same implementation they test); S7 make the owner-tamper probe
  always exercise the actual owner-check branch with a deterministic
  real-stat expected-UID mismatch that requires no `chown`, while
  separately reporting and exercising a genuine ownership tamper only when
  the kernel capability exists; the probe never skips and never claims
  real-system owner-tamper coverage when that capability is unavailable;
  S8 document the `plan_digest` field, its derivation, and its write-once
  binding in the state schema and OPERATIONS; S9 validate `phase`/`outcome`
  enum values and enforce the `attempt >= phase` monotonic coupling in the
  state machine so an attempt can never precede the phase that owns it, and
  enforce the inverse marker invariant that an attempt marker is zero
  whenever no attempt is active (no attempt => marker zero).
- Acceptance criteria: every finding has a fail-closed fixture and is
  exercised by the trusted control plane, not only the unit suite; init is
  atomic and no-replace; crash-window/orphan recovery is deterministic and
  fails closed on an unsafe existing state directory, re-validates the
  canonical state after linking and before deleting the quarantine, and
  matches the latest recorded ledger digest when a ledger exists; `now=0`
  is rejected; the owner probe always exercises the owner-check branch via
  a deterministic real-stat expected-UID mismatch without `chown`, and only
  additionally exercises a genuine ownership tamper when the kernel
  capability exists, never skipping and never claiming unavailable
  real-system evidence; `plan_digest` is documented and write-once bound;
  `phase`/`outcome`, `attempt >= phase`, and the inverse marker invariant
  (no attempt => attempt zero) are validated with exact adversarial
  fixtures.
- Verification: `.factory/tests/test-factory-state.py` extended with the
  independent fixtures; new `.factory/tests/fixtures/state-*` files;
  `scripts/validate-implementation-plan.py planning`;
  `scripts/check-plan-freshness.sh`.
- Evidence: every Task 4 review finding is closed with an exact fail-closed
  fixture exercised by the trusted control plane, not only the unit suite.
  S1 `init` is atomic and no-replace (never clobbers existing state or a
  campaign binding); S2 crash-window and orphan recovery is deterministic,
  fails closed on an unsafe existing state directory, re-validates the
  canonical state after linking and before deleting the quarantine, and
  matches the latest recorded digest-ledger entry when a ledger exists;
  S3 a zeroed `now=0`/epoch-zero monotonic start marker is rejected as
  tamper; S6 independent transition and state-digest fixtures are authored
  separately from the code path they exercise; S7 the owner-tamper probe
  always exercises the actual owner-check branch via a deterministic
  real-stat expected-UID mismatch requiring no `chown`, with a genuine
  ownership tamper reported and exercised only when the kernel capability
  exists (never skipping, never claiming unavailable real-system evidence);
  S8 `plan_digest` is documented, derived, and write-once bound in
  `docs/OPERATIONS.md` and the state schema; S9 `phase`/`outcome` enum
  values are validated, the `attempt >= phase` monotonic coupling is
  enforced so an attempt can never precede its owning phase, and the inverse
  marker invariant holds (no active attempt => marker zero). The hidden
  harness-owned suite `.factory/tests/test-factory-state.py` (125 tests,
  all passing) proves the exact §11 transitions, retry/attempt budgets,
  write-once bindings, secure atomic no-follow I/O, crash-window/quarantine
  recovery, digest determinism, the append-only ledger, and the trusted
  control-plane CLI across the committed `state-*.json`/`state-*.jsonl`
  corpus; `.factory/tests/test-factory-plan-parser.py` (13 tests) and
  `.factory/tests/test-factory-selector.py` (32 tests) also pass, and
  `scripts/validate-implementation-plan.py planning`,
  `scripts/check-plan-freshness.sh`, `scripts/check-generic-leakage.sh`, and
  `scripts/check-docs-sync.sh` all exit 0 with no `docs/SPEC.md` change.
  The independent security review of the hardened authority is acceptable.
  The complete `verify-boilerplate.sh` gate still exits 1 solely from the
  legacy context-summary authority drift (`scripts/check-context-summary.py`
  reports the open-task set drifts from the plan against the stale
  `.factory/artifacts/context-summary.md`); that legacy authority removal is
  assigned to pending Task 15 and is not a Task 19 defect.
- Documentation impact: `docs/OPERATIONS.md`,
  `.factory/schemas/factory-state-v1.schema.md`.

## Task 20: Final documentation and specification audit

- Status: pending
- Dependencies: Tasks 1-19
- Scope: Independent read-only audit and review at the final committed
  revision verifies the definition of done: every conformance row in the
  matrix and the sidecar is `verified` with exact evidence, the interaction
  inventory covers input/semantic/production/evidence, all open findings and
  defects are closed or explicitly documented, the repository is clean at
  the audit commit, and the documentation (README, FACTORY, OPERATIONS,
  AGENTS) is in sync. The auditor is a separate fresh process with a
  distinct static prompt, no developer/tester conversation, and reads only
  authoritative inputs at the exact commit. An audit finding becomes a
  next-round planner task; the audit itself never edits product code or the
  plan.
- Acceptance criteria: audit report records every §24 requirement verified
  or an explicit finding; no acceptance-critical audit finding remains; the
  campaign does not claim success unless the final audit is clean and the
  conformance sidecar shows all verified.
- Verification: `scripts/validate-conformance.py`;
  `scripts/check-docs-sync.sh`; independent audit evidence appended to
  `.factory/artifacts/campaign-audit.md`.
- Documentation impact: `.factory/artifacts/campaign-audit.md`.
