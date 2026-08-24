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

- Smoke evidence round (evidence-smoke): deterministic designated harness seam; no external model, cookies, credentials, runner, or human.
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
the bounded tasks below. These classifications are reconciled exactly to the
machine-readable conformance sidecar (`.factory/artifacts/conformance.json`,
which `scripts/validate-conformance.py` cross-checks row-by-row): the
retained-and-rebound machinery rows AUTH-01 through VIS-01 are `partial`
(existing harness authority is preserved and re-bound to the new path;
Task 20 completes the installed-harness mechanics with fixture-authority
receipts only at its exact commit, while live installed-tier receipts and
live campaign evidence are still owed by Tasks 22/23), RUNNER-01 is
`blocked` (real-system evidence requires a
provisioned, signed hardware runner the generic environment does not declare;
external-human remediation is Task 24, FACT-020), HIDE-01, MIG-01, and
TEST-01 are `partial` (footprint inventory, generic-first migration, and the
§22 adversarial suite exist but their installed/checked evidence is still
owed by Task 23), and ACCEPT-01 is `missing` (no boilerplate acceptance
evidence exists yet); no row is `verified` until its task completes with
exact-commit evidence at the required tier.

Matrix ownership: the round-1 independent audit
(`.factory/artifacts/campaign-audit.md` at commit `02a1de4`) reported
`findings` with 0 of 24 rows verified. Every non-verified row is therefore
owned by its referenced remediation tasks — Tasks 20-24 (Task 20's
installed-harness mechanics are complete at `6b9c626` with fixture-authority
receipts only; round-1 objective coverage, live campaign control state,
live generic-namespace/coordinator receipts and check-installed acceptance,
and external runner provisioning remain pending) — and remains NOT
VERIFIED under the pending final audit (Task 25), which may only report
clean when every row is verified with exact-commit receipts at its required
tier. RUNNER-01's row is owned by blocked Tasks 21/24 and stays `blocked`
until the external human resolves FACT-020; no row is reclassified or
elevated by prose.

| ID | Spec § | Classification | Evidence | Task |
|----|--------|--------------|----------|------|
| AUTH-01 | §5, §7 | partial | existing spec/plan authority retained; new loop binds the config spec to the FACTORY-LOOP-SPEC and keeps the plan as the sole task ledger | Task 1, Task 8, Task 9, Task 16, Task 20, Task 22, Task 23, Task 25 |
| CTX-01 | §5, §9 | partial | fresh process per role with disabled session/resume/memory injection implemented | Task 6, Task 8, Task 16, Task 20, Task 22, Task 23, Task 25 |
| CTX-02 | §5, §18 | partial | legacy `.ralph/`, `.factory-state/`, scratchpad, task, and memory paths unavailable to model tools; generic checker read authority scoped to the exact commit and dedicated generic namespace | Task 8, Task 15, Task 16, Task 20, Task 23, Task 25 |
| ROLE-01 | §6 | partial | four distinct static roles (planner/developer/tester/auditor) with no adaptive model roles | Task 8, Task 16, Task 20, Task 22, Task 23, Task 25 |
| PLAN-01 | §7 | partial | `factory-plan/v1` schema and parser binding spec/base/tasks/requirements/interactions/conformance unambiguously, with byte-exact round-trip, §24 registry coverage, and range-bounds, lifecycle-field, traversal, and final-audit invariants closed by exact adversarial fixtures | Task 2, Task 14, Task 16, Task 18, Task 25 |
| TASK-01 | §7, §8 | partial | trusted task transitions and deterministic priority-then-ID selection | Task 3, Task 9, Task 16, Task 22, Task 25 |
| TASK-02 | §9, §20 | partial | delivered task bytes and digest exactly match the committed plan | Task 6, Task 9, Task 16, Task 20, Task 22, Task 23, Task 25 |
| QUOTA-01 | §10 | partial | existing `scripts/ollama-usage-guard.sh` `--check`/`--wait` contract retained and wired into every invocation | Task 7, Task 9, Task 11, Task 16, Task 22, Task 25 |
| QUOTA-02 | §10 | partial | Ollama credentials absent from child argv/environ/log and owned material securely erased | Task 7, Task 8, Task 11, Task 16, Task 22, Task 25 |
| STATE-01 | §11, §17 | partial | one minimal atomic control-state file enforcing the monotonic transition table and tamper detection | Task 4, Task 9, Task 16, Task 19, Task 22, Task 25 |
| LOCK-01 | §12 | partial | canonical root-descriptor flock, one writer, non-inheritance and non-unlockable-by-second-descriptor | Task 5, Task 6, Task 16, Task 22, Task 25 |
| PROC-01 | §9, §12, §17 | partial | bounded process-session signaling, escaped-child detection, full reap, dirty-work preservation | Task 6, Task 16, Task 22, Task 25 |
| GIT-01 | §12, §17 | partial | canonical repository/branch/spec/plan bindings and guarded commit boundary enforced in the new launcher | Task 5, Task 11, Task 16, Task 20, Task 23, Task 25 |
| PHASE-01 | §13, §14 | partial | phase/campaign outcome machine with exact advance/terminate behavior and no no-task spin | Task 9, Task 16, Task 22, Task 25 |
| COMPLETE-01 | §15 | partial | task, work-exhaustion, verification, audit, product-acceptance, and campaign-success predicates stay distinct | Task 9, Task 16, Task 22, Task 25 |
| FIND-01 | §16 | partial | findings reach later developers only through a planner revision of the canonical plan | Task 10, Task 16, Task 25 |
| CRED-01 | §18 | partial | existing Pi credential tool-call/tool-result enforcement and trusted SDK authority retained | Task 11, Task 16, Task 22, Task 25 |
| EVID-01 | §19 | partial | existing exact-commit receipts/manifests and immutable verifier binding retained; installed-tier receipts minted from the installed copy | Task 12, Task 16, Task 20, Task 23, Task 25 |
| VIS-01 | §19 | partial | existing visual provenance machinery retained with exact-byte provenance | Task 12, Task 16, Task 20, Task 23, Task 25 |
| RUNNER-01 | §19 | blocked | existing runner/capability receipt machinery retained; real_system evidence requires a declared, provisioned, signed hardware runner the generic environment does not provide | Task 12, Task 16, Task 21, Task 24, Task 25 |
| HIDE-01 | §3 | partial | harness-footprint conformance test inventories every installed file and fails on escapes; installed physical-file inventory from the installed copy | Task 13, Task 16, Task 20, Task 23, Task 25 |
| MIG-01 | §21 | partial | generic-first migration preserves code/plan/evidence/blockers without importing Ralph control state | Task 15, Task 16, Task 20, Task 23, Task 25 |
| TEST-01 | §22 | partial | full adversarial conformance suite (§22 tests 1-27) and documentation synchronization; production gates executed from the installed copy | Task 16, Task 17, Task 20, Task 23, Task 25 |
| ACCEPT-01 | §23 | missing | boilerplate acceptance criteria, all §24 requirements mapped and verified, independent audit clean | Task 14, Task 19, Task 20, Task 21, Task 22, Task 23, Task 24, Task 25 |

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

- Status: complete
- Dependencies: Task 4
- Scope: Reconcile the Task 5 security review (findings F1-F10): implement
  the lock authority: exclusive `flock` on the already-open canonical Git
  top-level directory descriptor opened with
  `O_DIRECTORY|O_NOFOLLOW|O_CLOEXEC`. At acquisition, bind the descriptor to
  its canonical path by matching device+inode and make canonical repository
  identity, required branch, and spec/plan bindings mandatory and validated
  before the lock is granted, failing closed on any mismatch (F2). Detect a
  double-fork/`setsid` escape or any holder descendant outside the session
  via a full `/proc` ancestor walk (F1) and fail closed for operator
  inspection; the detection API accepts and reports against the captured
  descendant scope supplied by supervision (F6 contract) so descendant
  accounting is never stale. Reject any passed or inherited fd that aliases
  the root lock file before exec (F8) and route every lock/authority failure
  through one unified fail-closed exception contract (F9). Close the lock
  descriptor in every child before exec and strip every `GIT_CONFIG*`
  variable (F5) and the entire lock-key environment prefix, including legacy
  keys (F10), from the child environment; start a new process session; make a
  separately opened repository descriptor unable to unlock the holder. Enforce
  a bounded timeout that kills and reaps the holder's full process group (F3).
  Preserve the Git command-boundary guard so `--no-verify`, hook-path
  override, `GIT_CONFIG*`, worktrees, amend/merge/rebase bypasses, and forged
  handoffs remain rejected. Invoke Git through fixed trusted absolute
  candidates or a root-owned immutable Nix-store-validated path (F4), never an
  unqualified `git` resolved from a caller-controlled PATH, so an
  attacker-controlled PATH cannot substitute a different `git` behind the
  guarded commit boundary.
- Acceptance criteria: concurrent launcher probes prove exactly one writer;
  an untrusted leaf inherits no lock descriptor and no lock environment; a
  full `/proc` ancestor walk proves the escaped/`setsid` descendant is
  detected against the captured descendant scope (F1); the acquired
  descriptor's device+inode matches its canonical path and identity/branch/
  spec/plan are bound before the lock is granted, failing closed on any
  mismatch before any write (F2); a bounded timeout kills and reaps the
  holder's full process group (F3); no caller-controlled PATH can substitute
  `git` (F4); every `GIT_CONFIG*` and legacy/prefix lock key is absent from
  the child environment (F5, F10); a passed or inherited fd aliasing the root
  lock is rejected before exec (F8); every lock failure surfaces through the
  unified fail-closed exception contract (F9); the escaped-descendant case
  blocks recovery; commit-boundary bypass tests stay rejected.
- Verification: `tests/test-factory-lock.py` extended with inheritance,
  escape, `/proc` ancestor-walk, inode-binding, `GIT_CONFIG*`/legacy lock-key
  strip, alias-fd, unified-exception, and timeout fixtures;
  `tests/test-git-commit-guard.sh` still passes.
- Evidence: `.factory/loop/lock.py` implements the root-descriptor lock
  authority with `flock` on the already-open canonical Git top-level
  directory descriptor opened with `O_DIRECTORY|O_NOFOLLOW|O_CLOEXEC`, binds
  the descriptor to its canonical path by device+inode, and validates
  repository identity, required branch, and spec/plan bindings before
  granting the lock (F2). The full `/proc` ancestor walk detects a
  double-fork/`setsid` escape and any holder descendant outside the session,
  reporting against the captured descendant scope supplied by supervision
  (F1, F6). Passed/inherited fds aliasing the root lock are rejected before
  exec (F8); every lock/authority failure routes through the unified
  fail-closed exception contract (F9). The lock descriptor is closed in every
  child before exec, a new process session is started, every `GIT_CONFIG*`
  variable and the entire lock-key environment prefix including legacy keys
  is stripped from the child environment (F5, F10), and a separately opened
  descriptor cannot unlock the holder; a bounded timeout kills and reaps the
  holder's full process group (F3). Git is invoked through the PATH-pinned
  absolute executable in `.factory/loop/gitutil.py` (F4), never an
  unqualified `git`, and the `git-commit-guard.sh` boundary rejects
  `--no-verify`, hook-path override, `GIT_CONFIG*`, worktrees,
  amend/merge/rebase bypasses, and forged handoffs. Evidence from the
  accepted Task 5 report: `.factory/tests/test-factory-lock.py` passes
  37/37, `.factory/tests/test-factory-state.py` passes 125/125,
  `.factory/tests/test-factory-selector.py` passes 32/32, and
  `.factory/tests/test-factory-plan-parser.py` passes 13/13; legacy lock,
  git-commit-guard, plan-freshness, and docs checks pass; the security
  review of the implementation is acceptable; `verify-boilerplate.sh` exits
  1 solely because the legacy `check-context-summary.py` authority is
  assigned to Task 15 (context-summary migration/deprecation) and is not
  part of this task's scope. Documentation impact in `docs/OPERATIONS.md`.
- Documentation impact: `docs/OPERATIONS.md`.

## Task 6: Fresh-context execution, invocation contract, and supervision

- Status: complete
- Dependencies: Task 3, Task 5
- Scope: Reconcile the Task 6 supervisor review (findings F1-F5): implement
  `.factory/loop/launch.py` as a hidden control-plane module exposed only via
  `python -m factory.loop.launch` and, when installed, the external-prefix
  launcher entry point (no visible bare `scripts/` wrapper); the launch API
  and result types are exported from the hidden `.factory.loop` package
  surface. Every role starts in a new fresh process via the existing secure
  wrapper (`scripts/pi2-secure-exec.py`) in one-shot mode with disabled
  session resume and memory injection; the invocation binds exact
  model/provider, static role prompt digest, campaign-bound prompt-set digest,
  optional audit-objective digest, canonical workspace and bound commit,
  selected task ID with an excerpt whose bytes are re-derived from the
  committed spec and plan blobs and digest-matched (fail closed on
  substitution/paraphrase), allowed tools, and runtime/inactivity bounds. The
  CLI re-derives every authoritative byte for the backend, spec/plan, role
  prompt, policy, and wrapper through fd-anchored, nofollow, size-bounded
  reads of the committed blobs and accepts no operator-claimed or
  caller-supplied path, blob, digest, or binding (F5). The secure wrapper and
  model backend run from their exact bound-commit blobs (digest-verified) or
  an external trusted executable; an operator-claimed or caller-controlled
  path never qualifies (F2). Every PID is pinned to its `/proc` starttime and
  parent identity before any group signal, so a reused PID is never signaled
  or reaped (F4). Any exception, `KeyboardInterrupt`, or signal received after
  spawn still takes the bounded terminate-then-reap path, so no path can
  leave a child running (F1). Supervision delivers TERM, INT, and HUP to the
  full process group (F3), then reaps with a bounded grace escalating to
  KILL, guarding the launch snapshot against PID reuse and
  crash-before-snapshot windows and installing a subreaper whose lifetime is
  scoped to the active attempt, so an escaped descendant cannot orphan and is
  always reaped (F7); orphan/reap handling is scoped to children spawned
  after the snapshot and explicitly excludes pre-existing children. The
  supervisor maintains a descendant-scoped handle scan (F6): it snapshots and
  re-enumerates only the role's own live descendants and supplies that
  captured descendant scope to the Task 5 lock-detector API so descendant
  accounting is never stale; escaped children are detected; dirty or
  interrupted work is preserved and never silently overwritten. The only
  completion signal is a machine-readable exit-status whose field set is a
  fixed result schema (role, model/provider, outcome, returncode, signal,
  reason, terminated-by signals, bounded per-stream digest/tail, snapshot
  counts). Output content redaction of child/tool output is assigned to
  Task 11, and HOME/XDG/filesystem confinement is assigned to Tasks 8/11;
  Task 6 retains its own parent-secret boundary (no credentials, cookies, or
  legacy lock tokens in child argv/environment, no inherited descriptors).
  Emergency cleanup always terminates the full process group even if the
  leader has already exited, so a group kill is never skipped because its
  leader is gone. TERM, INT, and HUP are blocked on the launch thread from
  before the child is spawned until its identity is recorded, and any signal
  received in that window, or any launch attempted off the main thread, fails
  loudly rather than silently leaving an unrecorded child running. Runtime
  and inactivity are bounded by finite authoritative limits with no unbounded
  wait. The verify-to-exec path is freed of its TOCTOU by executing only the
  private mode-0500 staged exact committed wrapper/backend bytes or a
  verified immutable fd; an external trusted executable is accepted only
  after every directory on its resolved path (including the containing
  directories of any symlink target) is validated. The exported programmatic
  launch API cannot bypass F2/F5: it requires an unforgeable verified
  binding/token or stays private, and the production CLI is the sole public
  launch authority. Interpreter resolution is bounded to a trusted, verified
  set and never follows an attacker-controlled path.
- Acceptance criteria: the CLI is reachable only via `python -m
  factory.loop.launch` or the external-prefix launcher, and no visible bare
  script exposes it; the CLI re-derives every authoritative byte from bound
  blobs with nofollow bounded fd-anchored reads and rejects any
  operator-claimed override (F5); wrapper/backend run only from the bound
  commit or an external trusted executable (F2); starttime identity is pinned
  before any group signal so a reused PID is never signaled or reaped (F4); a
  synthetic exception/`KeyboardInterrupt`/signal raised after spawn still
  bounded-terminates and reaps with no survivor (F1); TERM/INT/HUP reach the
  full group and escalate to KILL within a bound (F3); process invariants
  (new session, no inherited lock/env, no resume) are verified per launch;
  excerpt digest match/mismatch fixtures pass; the descendant-scoped scan
  covers exactly the launch snapshot with no stale handles and excludes
  pre-existing children (F6); PID-reuse, crash-before-snapshot, and subreaper
  fixtures pass and an escaped orphan is reaped within the bounded subreaper
  lifetime (F7); the machine-result schema is validated; the launch and
  result exports appear on the hidden package surface; a crashed attempt
  leaves its dirty work intact; the emergency cleanup path terminates the
  full group even when the leader already exited and never skips the group
  kill; TERM/INT/HUP are blocked through child-identity recording and any
  off-main-thread launch fails loudly; finite runtime/inactivity limits bound
  every attempt; the verify-to-exec path is TOCTOU-free (mode-0500 staged
  exact committed bytes or a verified immutable fd); an external trusted
  executable is accepted only after its resolved containing directories
  (including symlink-target directories) are validated; an exported
  programmatic launch API cannot bypass F2/F5 or displace the CLI as the sole
  public launch authority, and interpreter resolution is bounded to the
  trusted set.
- Verification: `.factory/tests/test-factory-launch.py`;
  `.factory/tests/test-factory-supervision.sh` (hidden-namespace actual test
  path; no visible `tests/` runner for Task 6) plus added tests covering
  the terminal emergency full-group kill after leader exit, TERM/INT/HUP
  blocking and off-main-thread rejection, finite runtime/inactivity
  enforcement, verify-to-exec TOCTOU (mode-0500 staged bytes / verified
  immutable fd), external trusted-path directory and symlink validation,
  programmatic-API F2/F5 bypass rejection, and bounded interpreter
  resolution.
- Evidence: `.factory/tests/test-factory-launch.py` passes 76/76 under
  `-W error::ResourceWarning`; `.factory/tests/test-factory-supervision.sh`
  passes its module-entrypoint, result-schema, exact committed executable,
  and tamper controls; Task 5 lock tests pass 37/37, state tests pass 125/125,
  selector tests pass 32/32, and parser tests pass 13/13. The final
  independent security review found no Medium or High issue and accepted the
  checkpoint after verifying emergency group cleanup, signal-safe spawn,
  finite bounds, private mode-0500 staged committed executables, immutable
  external-path checks, and mandatory launch authority. The complete
  `verify-boilerplate.sh` remains exit 1 solely on the legacy persisted
  context-summary authority already assigned to pending Task 15; no full-gate
  pass is claimed here.
- Documentation impact: `docs/OPERATIONS.md`.

## Task 7: Ollama usage guard retention and credential hardening

- Status: complete
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
  Reconcile the Task 7 security review:
  1. **Operator store default is outside the model workspace.** The guard's
     default credential store is never `<workspace>/.ollama-usage-env` or
     `<repository root>/.ollama-usage-env`; it defaults to a canonical
     operator-owned path outside the model-visible workspace and is read
     strictly nofollow with mode exactly `0600` (owner rw only), owned by the
     operator, single-link, and size-bounded — a mode other than `0600`, a
     symlink, a wrong owner, a multi-link, or an oversized store fails closed.
     The launch authority must not scope the store into the model workspace
     the way the WIP `env_file_path(workspace)` does.
  2. **Production Ollama launch fails closed until Task 8 proves
     confinement.** An `ollama`-provider invocation does not reach the model
     until the Task 8 confinement authority proves `.factory/` and the
     operator credential store(s) are inaccessible/read-only to model tools.
     Until that proof, the production launch gate fails closed (no model
     invocation) rather than relying on the store location alone.
  3. **Guard source binding is jointly owned by Task 8 and Task 11.** The
     guard modules (`usage.py`, `usage_fetch.py`) are staged and executed
     only from their exact-commit blob or the trusted external executable
     prefix; an operator-claimed or caller-controlled path never qualifies.
     Task 8 owns source/executable confinement, Task 11 owns the credential/
     source authority.
  4. **Remove `html-file` from the production launch API/CLI.** The
     `--usage-guard-html-file` option is removed from `python -m
     factory.loop.launch` and from the `authorize_launch` production surface;
     saved-page parsing is diagnostics/test-only, reachable only through the
     hidden `.factory/` test suite, never through the production launch CLI or
     API.
  5. **Strict known-provider validation and per-policy gating.**
     `verify_invocation` validates the provider against the supported known
     set, and every supported model invocation (each provider/model pair) is
     gated per the retained policy — no provider/model bypasses the guard.
  6. **Bounded same-origin HTTPS redirects; 3xx/401/403 auth fatal.** The
     fetch child follows only bounded (finite hop, same-origin, HTTPS)
     redirects; any unresolvable redirect and any 3xx/401/403 response is
     classified as a fatal authentication failure (exit 2), never as a
     transient retry.
  7. **Restore the `re.I | re.S` parse path.** The retained session/weekly
     classification patterns use exactly `re.I | re.S` as the legacy shell
     guard does, so `usage-ok.html` / `usage-blocked.html` / `login.html`
     drive the identical parse contract.
  8. **Ambient `OLLAMA_COOKIE` is ignored and scrubbed, not a crash.** The
     ambient environment is never a cookie source and never spawns a child
     with a credential; the guard scrubs every ambient credential-shaped key
     from the child environment and continues (failing closed only on a
     genuinely missing private cookie at the fetch boundary), and every
     exception maps to a documented exit (0/1/2/3 or `128+signum`) with no
     undocumented exit and no traceback.
  9. **HTTPS only, except an explicit loopback test seam.** Network fetches
     accept `https://` only; `http://` is rejected in production and permitted
     only for an explicit loopback (`127.0.0.1`/`localhost`) seam used
     exclusively by the hermetic hidden suite.
  10. **No zero busy loop.** A zero poll interval (unbounded zero-sleep busy
      loop) is rejected; every `--wait` bound is finite.
  11. **Reject CRLF / embedded-control legacy cookies.** A cookie value
      containing CR, LF, or other control characters (the header-injection
      class) is fatal and never passed to the HTTP request.
  12. **Signals cover the initial and final checks too.** TERM/INT/HUP during
      the initial `--check`, the `--wait`, and the final `--check` all
      terminate and reap the credential-holding fetch child and exit
      `128+signum`; no `require_quota` path is signal-unsafe.
  13. **Provider/store tamper tests.** A tampered provider binding and a
      tampered credential store (mode/owner/link/content/size) fail closed
      with exact fixtures.
  14. **Loopback HTTP settings seam is diagnostics/private-test only.** The
      explicit loopback HTTP test seam (item 9) is a diagnostics/private-test
      settings seam reachable only through the hidden `.factory/` test suite
      and is absent from the production launch CLI/API unless an explicit
      trusted diagnostics authority is present; no production invocation may
      enable `http://` transport.
- Acceptance criteria: the exit table is enforced by fixtures; the
  synthetic-secret probe never leaks into cmdline/environ; waiting aborts
  cleanly on a signal at the initial check, the wait, or the final check;
  the production launch surface has no `html-file` option; provider/model
  gating is per policy with strict known-provider validation; bounded
  same-origin HTTPS redirects with 3xx/401/403 fatal; the `re.I | re.S`
  parser drives the retained fixtures; ambient `OLLAMA_COOKIE` is scrubbed
  without crashing; HTTPS-only with an explicit loopback seam, and that
  loopback HTTP settings seam is diagnostics/private-test only and absent
  from the production CLI unless an explicit trusted diagnostics authority
  is present; a zero poll interval and a CRLF/control legacy cookie are
  rejected; every exception is a documented exit; an `ollama`-provider
  launch fails closed until the Task 8 confinement proof is present.
- Verification: the hidden `.factory/tests/test-factory-usage.py` extended
  with redirect/3xx/401/403, loopback-only HTTPS, ambient-scrub, CRLF, zero
  poll, provider/store tamper, and initial/final-check signal fixtures;
  `tests/test-pi2-ollama-wrapper.sh` extended with the proc probe;
  `tests/fixtures/usage-ok.html` and `usage-blocked.html` still drive the
  parse path; the production `python -m factory.loop.launch` help exposes no
  `--usage-guard-html-file`.
- Evidence: `.factory/tests/test-factory-usage.py` passes 106/106 under a
  90-second outer bound with one honest root-only ownership-tamper skip;
  `tests/test-pi2-ollama-wrapper.sh` passes the live synthetic curl
  cmdline/environ probe; launch regressions pass 76/76. The retained shell
  and hidden Python guards preserve the exact `--check`/`--wait` exit table,
  transport cookies only through bounded private stdin/config channels, and
  reject unsafe stores, providers, redirects, parser inputs, control bytes,
  unbounded polling, and production loopback transport. Independent security
  review accepted the checkpoint and confirmed production Ollama launch stays
  fail-closed until Task 8 supplies real confinement proof for every effective
  credential channel and exact guard source. `verify-boilerplate.sh` remains
  exit 1 solely on the legacy persisted context-summary authority assigned to
  pending Task 15; no full-gate pass is claimed.
- Documentation impact: `docs/OPERATIONS.md`.

## Task 8: Role prompts, prompt-set binding, and workspace confinement

- Status: complete
- Dependencies: Task 6
- Scope: Commit distinct static role prompts for planner, developer, tester,
  and auditor under `.factory/prompts/` with campaign-bound digests (no
  adaptive model roles); the role prompts are not yet committed, and Task 8
  remains pending until this scope and the reconciled confinement below land.
  Commit the audit-objective registry bound at campaign start and selected
  deterministically per round. Implement model workspace and tool confinement
  so `.ralph/`, `.factory-state/`, prior scratchpads and handoffs, runtime
  task stores, memory stores, migration archives, host credentials, and the
  operator Ollama credential store(s) are unavailable through model tools;
  the plan, spec, code/tests, and allowlisted `.factory/` inputs are readable,
  and role write allowlists are honored. No memory, conversation, or
  context-summary authority is injected.
  Reconcile the Task 8 confinement review:
  1. **Reject symlinks in every allowlist component and containment-check
     resolved targets.** Every path in the model read/write/execute
     allowlists (workspace inputs, allowlisted `.factory/` inputs, the
     sanitized private HOME, per-launch scratch/staging paths, and any
     system-path component) is resolved with a bounded no-follow read; a
     symlink in any component of any allowlisted path is rejected and the
     confinement fails closed. The containment check validates the fully
     resolved target against the allowed namespace, so a symlink whose
     resolved target escapes the allowed subtree or aliases a forbidden path
     is never permitted.
  2. **Remove `.git` from the model read allowlist.** The model read
     allowlist contains no `.git` entry, so the model cannot read repository
     history, `.git/` objects, reflog, hooks, config, or any old secrets or
     historical content reachable through Git history or the `.factory/`
     commit history. Git operations, history, and commit are owned entirely
     by the trusted Task 9 orchestrator (descriptor-anchored, outside the
     model) and are never performed by model tools; the model-facing git
     shim and bare-git selection (Task 11) route through the trusted path and
     never grant the model direct repository-history read access.
  3. **Remove `/proc` from the model Landlock allowlist and prove proc
     credential reads denied.** `/proc` is not granted to model tools; the
     confinement proof includes a probe asserting a model-tool read of
     `/proc/<pid>/environ`, `/proc/<pid>/cmdline`, `/proc/*/fd`, `/proc/*/maps`,
     or any proc-derived credential path is denied by the *effective*
     confinement, and the proof fails closed if any such read succeeds.
  4. **Replace any broad `/tmp` grant with exact per-launch private
     home/scratch/staging paths.** The model receives no broad `/tmp` access.
     Each launch is granted only its own private, mode-restricted
     home/scratch/staging directories under the per-launch private namespace
     (exact per-launch paths); sibling-launch or cross-launch access to
     another launch's private directories is denied, and shared or
     predictable `/tmp` scratch, staging, or credential-carrier locations are
     never granted.
  5. **Real confinement is mandatory for every provider and every public
     authorize API, not CLI-only.** The confinement authority and its real
     proof gate every model invocation and every public
     `authorize_launch`/authorize API path for every provider (Ollama and
     external backends alike); confinement is never optional for, or
     bypassed by, a non-CLI or programmatic authorize entry point. A launch
     that cannot apply real confinement fails closed regardless of how it was
     invoked.
  6. **Clean up all private directories on authorization failure.** Any
     failure to authorize or confine a launch (or any rejected probe) removes
     and cleans every per-launch private home, scratch, staging, and
     credential-carrier directory the launch created, so no private or
     credential material survives a failed authorization.
  7. **Narrow and document the `/run`/`dev`/`var`/`etc` system-path
     allowlist and test socket/host-config denial.** Any system-path
     components the model may read are enumerated as the narrowest explicit
     allowlist with each entry justified and documented; a model read of
     `/run/...` sockets, `/dev/...` device nodes, host config under
     `/etc/...`, or `/var/...` state that is not explicitly allowlisted is
     denied, and a probe asserts socket and host-config denial under the
     effective confinement.
  Own HOME/XDG and broader filesystem confinement (assigned from the Task 6
  review, shared with Task 11): the fresh process runs under a sanitized
  HOME and an explicit `XDG_*` set so model tools cannot reach host
  credentials, the operator's real home or caches, `.ollama-usage-env`,
  secrets, or any path outside the allowlist, while the role read/write
  allowlists remain honored. Own the Task 7 review's confinement proof and
  guard-source binding jointly with Task 11: the confinement authority
  *proves* `.factory/` and the operator Ollama credential store(s) are
  inaccessible/read-only to model tools, and the Ollama usage-guard source
  (`usage.py`, `usage_fetch.py`) is staged and executed only from its
  exact-commit blob or the trusted external executable prefix — a proof that
  the Task 7 production Ollama launch gate requires before any
  `ollama`-provider invocation proceeds. The confinement proof is a real,
  effective proof, not a synthetic one: it binds every effective credential
  channel the guard actually consumes — the default operator env store, any
  explicitly specified cookie file, and any stdin-provided credential
  provenance — and proves each such channel, together with the exact-commit
  guard source, is outside or inaccessible/read-only to model tools at the
  bound exact commit; a synthetic or simulated probe is never evidence, and
  only a probe exercising the real production launch path against the real
  consumed channels can evidence confinement. No implementation claim for the
  role prompts or the confinement authority is made in any documentation
  while Task 8 is pending.
- Acceptance criteria: each role launch proves it can read the allowlisted
  inputs and cannot read any forbidden path; every allowlist component is
  no-follow and any symlink in any component, or any resolved-target escape,
  fails the containment check; `.git` and repository history are not readable
  by the model and no model tool performs Git operations; `/proc` is not
  granted and a proc credential read is denied by the effective confinement;
  the model has no broad `/tmp` grant and only its own exact per-launch
  private home/scratch/staging paths with no sibling-launch access; real
  confinement is applied and proven for every provider and every public
  authorize API (not CLI-only); all private directories are cleaned up on
  authorization failure; the `/run`/`dev`/`var`/`etc` system-path allowlist
  is narrow and documented and socket/host-config reads are denied; the
  role-prompt digests match the campaign binding; no completion claim from a
  previous attempt is present in the fresh context; the process's
  HOME/XDG/filesystem scope is confined and cannot reach host credentials or
  any path outside the allowlist; the confinement authority proves `.factory/`
  and the operator Ollama credential store(s) are inaccessible/read-only, and
  the Ollama usage-guard source runs only from its exact-commit blob or the
  trusted external executable prefix; the real confinement proof binds every
  effective credential channel actually consumed (default env store, explicit
  cookie file, stdin provenance) and the exact-commit guard source, proving
  each is outside or inaccessible to model tools, and synthetic proof is never
  evidence.
- Verification: `.factory/tests/test-factory-confinement.py` and
  `.factory/tests/test-factory-supervision.sh`, including a real
  production-launch confinement probe that exercises the default operator env
  store, an explicitly specified cookie file, and stdin-provided credential
  provenance against the bound exact commit (no synthetic-only evidence), plus
  symlink-in-every-allowlist-component and resolved-target containment
  fixtures, a `.git`/history-read denial fixture, a `/proc` credential-read
  denial fixture, an exact per-launch private home/scratch/staging
  no-sibling-access fixture, a programmatic (non-CLI) authorize-API
  confinement-required fixture, a private-directory
  cleanup-on-authorization-failure fixture, and socket/host-config denial
  fixtures.
- Documentation impact: `docs/FACTORY.md`, `docs/OPERATIONS.md` (documenting
  the no-`.git` model read boundary, the no-`/proc` grant, the exact
  per-launch private home/scratch/staging paths, the narrow documented
  `/run`/`dev`/`var`/`etc` system-path allowlist, and that Git
  operations/history/commit are owned by the trusted Task 9 orchestrator).
- Evidence: `.factory/tests/test-factory-confinement.py` passes 72/72 with
  real kernel Landlock enforcement and non-vacuous unconfined controls;
  `.factory/tests/test-factory-launch.py` passes 76/76 warning-clean with no
  surviving model process; `.factory/tests/test-factory-usage.py` passes
  106/106 with one honest root-only ownership skip. The hidden shell launch
  integration passes. Static role prompts, prompt-set digest binding, and
  deterministic audit-objective selection are covered by the confinement
  suite. Independent security review accepted the checkpoint after verifying
  symlink containment, no model `.git` or `/proc` access, exact per-launch
  private paths, mandatory confinement for every provider/API, cleanup on
  failure, role write boundaries, and proof binding of every credential
  channel and guard source. `verify-boilerplate.sh` remains exit 1 solely on
  the legacy persisted context-summary authority assigned to Task 15; no
  full-gate pass is claimed.

## Task 9: Phase and campaign state machine with outcomes

- Status: complete
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
  Own the trusted, descriptor-anchored Git/history/commit authority **outside
  the model** (reconciled from the Task 8 review, which removed `.git` from
  the model read allowlist): all Git operations, repository-history access,
  reflog/object/config reads, staging, and the guarded commit boundary are
  performed by the trusted Task 9 orchestrator through the
  root-descriptor-anchored authority (Task 5 `lock.py`/`gitutil.py`), never
  by a model tool. The model receives no direct `.git` read access and no Git
  history; the orchestrator stages and commits trusted plan/code/evidence, and
  the model-facing git shim and bare-git selection (Task 11) route every Git
  request through the trusted path. The orchestrator's commit boundary
  preserves the Git commit guard so `--no-verify`, hook-path override,
  `GIT_CONFIG*`, worktrees, and amend/merge/rebase bypasses remain rejected.
- Acceptance criteria: fixture campaigns for each outcome and the success/
  blocked/findings/failed/interrupted combinations terminate within the
  configured bounds; no phase transition violates the state machine; all Git
  operations, history, and commit for the campaign are performed by the
  trusted orchestrator through the descriptor-anchored authority and no model
  tool has direct `.git` read or Git-history access.
- Verification: `.factory/tests/test-factory-campaign.py`, including fixtures
  asserting the trusted orchestrator (not the model) performs every commit,
  model tools hold no direct `.git`/history read, all six terminals persist,
  empty work reaches deterministic verification/audit, recovery fails closed,
  and exact task/objective bytes are bound on the production launch path.
- Documentation impact: `docs/FACTORY.md`, `docs/OPERATIONS.md`.
- Evidence: `.factory/tests/test-factory-campaign.py` passes 66/66
  warning-clean; state 129/129, lock 38/38, launch 76/76, confinement 72/72,
  usage 106/106 (one honest root-only ownership skip), selector 32/32, parser
  13/13, and hidden shell supervision pass. Independent adversarial review
  accepted the final checkpoint after remediation of persisted audit-abort
  terminals, exact production task/objective binding, new-plan acceptance,
  authoritative stale-base rejection, bounded Git, deterministic-gate
  requirements, and identical Landlock/commit denial of trusted policy paths.
  No full boilerplate pass is claimed: the known legacy context-summary drift
  remains assigned to Task 15.

## Task 10: Findings flow

- Status: complete
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
- Verification: `.factory/tests/test-factory-findings.py` and the real
  Landlock result-handoff cases in
  `.factory/tests/test-factory-confinement.py`.
- Documentation impact: `docs/FACTORY.md`, `docs/OPERATIONS.md`.
- Evidence: findings 61/61, campaign 66/66, real confinement 74/74,
  launch 76/76, state 129/129, lock 38/38, selector 32/32, parser 13/13,
  and hidden shell supervision pass warning-clean. Fixtures prove exact-byte
  idempotent receipt recovery for verification/audit crash windows, preserved
  result authentication, strict ledger and receipt bindings, actionable gate
  evidence, planner-only digest-bound delivery, no selector authority, and
  real exact-file tester/auditor writes with sibling `.factory-state` denial.
  Independent security review accepted the checkpoint after those production
  and crash-recovery remediations.

## Task 11: Credential and security boundary retention

- Status: complete
- Dependencies: Task 6, Task 8
- Scope: Preserve the existing Pi tool-call/tool-result credential guard and
  trusted SDK authority; the retained extension contains only required
  credential enforcement and guarded Git boundary behavior, including the
  model-facing git shim and bare-git selection pinned to the trusted fixed
  absolute candidates or a root-owned immutable Nix-store-validated path so a
  caller-controlled PATH cannot redirect `git` behind the model boundary. No
  model tool can
  dump environment, authentication files, private keys, or secrets; tool-result
  redaction, stdin bounded command checks, and sanitized logs remain active.
  Own output content redaction (assigned from the Task 6 review): every
  child/tool output channel — tool results, command output, deterministic
  gate output, and bounded captures — is redacted so credentials or secrets
  never appear in results, logs, receipts, or repository state; deterministic
  gates and acceptance commands receive a stripped allowlisted environment
  rather than the full parent environment. This complements, without
  replacing, Task 6's parent-secret environment/argv/descriptor boundary. With Task 8,
  retain HOME/XDG/filesystem confinement so the credential guard cannot be
  bypassed through host files visible to model tools. Co-own the Task 7
  review's guard-source binding jointly with Task 8: the Ollama usage-guard
  source (`usage.py`, `usage_fetch.py`) is executed only from its exact-commit
  blob or the trusted external executable prefix (never an operator-claimed
  or caller-controlled path), and the operator Ollama credential store
  confinement (inaccessible/read-only to model tools, strict nofollow `0600`)
  is retained as part of the credential/source authority.
  The external backend configuration authority remains pending within this
  task's scope: configuration for external (non-Ollama) model backends and
  trusted external executables is not finalized and remains pending Task 11
  work; any external backend config must be confined and transported under the
  same mandatory real-confinement and credential/source boundary as the Ollama
  provider, and no external backend config is accepted while Task 8
  confinement is unproven.
  Ralph lifecycle topics, `ralph emit`, completion-token handling, event
  snapshots, launch handshakes, and Ralph CLI shims are removed from the new
  path and never reimplemented. Close the accepted Task 9 defense-in-depth
  residuals: reject a bare `.factory` dirty path and unsafe/control-character
  porcelain paths at the trusted commit-scope layer even though Landlock
  already prevents model creation, and map `GitBoundaryError` to a clean
  launch-CLI failure rather than a traceback.
- Acceptance criteria: adversarial fixture attempts to exfiltrate secrets
  through tools, results, logs, argv, or environment all fail closed; a
  synthetic credential rendered into child/tool output is redacted from
  results, logs, receipts, and repository state; the secure wrapper and
  credential-guard behavior remain unchanged in contract; a caller-controlled
  PATH cannot redirect the model-facing git shim or bare-git selection; HOME/
  XDG/filesystem confinement keeps host credentials out of model reach.
- Verification: `.factory/tests/test-factory-redaction.py`,
  `tests/test-credential-extension.sh`, `tests/test-credential-guard.sh`, and
  the launch/campaign/usage/lock/confinement regressions.
- Documentation impact: `docs/FACTORY.md`, `docs/OPERATIONS.md`.
- Evidence: redaction 67 tests with two honest root-only skips; launch 77/77,
  campaign 66/66, usage 106/106 (one honest ownership skip), lock 39/39,
  confinement 74/74, state 129/129, findings 61/61, selector 32/32, hidden
  shell supervision, and visible credential guard/extension suites pass.
  Synthetic-secret fixtures verify exact-commit guard binding, Pi tool-call/
  tool-result masking, bounded split-marker and mid-line stream handling,
  sanitized model/gate/fixture environments, bounded gate pipes, URL-userinfo
  rejection, pinned interpreter/Git resolution, and real-confinement-only
  external backends. Independent security review accepted the remediated
  checkpoint with no blockers.

## Task 12: Evidence, verifier, and runner machinery retention

- Status: complete
- Dependencies: Task 5, Task 9
- Scope: Retain exact-commit signed runner receipts, capability contracts,
  visual provenance, atomic publication, installed and human evidence tiers,
  and the receipt wrapper (`scripts/machine-receipt.py`). Harden adjacent
  stdout/stderr artifacts with owner, mode, link-count, and inode checks, and
  require same-tag coordinator receipt publication to fail closed rather than
  silently replace an existing receipt. The verifier
  entrypoint is opened and bound to its committed blob/identity before
  untrusted execution, and later pathname substitution fails closed. PASS
  requires exit 0 and verified identity/commit/digests; any BLOCKED evidence
  forces an audit `findings` result; audits cite `[receipt: …]` /
  `[manifest: …]` exact references. Receipt publication happens only through
  the trusted control plane.
- Acceptance criteria: fixtures prove the receipt wrapper remains
  authoritative, the immutable verifier binding rejects path substitution,
  and no model assertion can elevate evidence.
- Verification: `.factory/tests/test-factory-evidence.py`,
  `tests/test-audit-receipts.sh`, `tests/test-runner-signer.sh`, and
  `tests/test-campaign-audit.sh`.
- Documentation impact: `docs/FACTORY.md`, `docs/OPERATIONS.md`.
- Evidence: hidden evidence 116/116, campaign 66/66, lock 39/39, and the
  visible receipt/signer/campaign-audit/factory-lock gates pass. Tests prove
  committed verifier execution through a retained read-only descriptor after
  pathname substitution, no root-lock descriptor inheritance, pinned and
  sanitized Git/ssh-keygen authority, no-follow receipt/transcript handling,
  atomic same-tag no-replace publication, protected coordinator-only minting,
  exact command/argv and status grammar, BLOCKED→findings, and machine tier/
  human non-elevation. Independent security review accepted the final
  remediated checkpoint with no blockers. Legacy maintenance verifier routing
  remains migration work under Tasks 15/16; no full boilerplate or real-runner
  pass is claimed.

## Task 13: Harness isolation and installed-footprint inventory

- Status: complete
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
- Verification: `.factory/tests/test-factory-footprint.sh` and
  `scripts/check-generic-leakage.sh`.
- Documentation impact: `docs/FACTORY.md`.
- Evidence: hidden footprint 97/97 and its shell driver pass warning-clean;
  the live tracked/on-disk/external inventory is clean, product discovery
  excludes hidden/ignored credential and build artifacts, product-install
  contamination fails, and docs/leakage/diff gates pass. Adversarial fixtures
  cover NUL-safe tracked modes, whitespace filenames, `.pi`/`.ralph` links,
  every-segment case/NFKC/trailing-dot aliases, root/nested markers, special
  inodes and device crossings, deletion refusal, exact 0700 ownership, and
  complete external manifests. Independent security review accepted the
  remediated checkpoint with no blockers. `verify-boilerplate.sh` remains
  nonzero only at the legacy context-summary authority assigned to Task 15.

## Task 14: Conformance sidecar and requirement policy for the §24 registry

- Status: complete
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
- Verification: `.factory/tests/test-factory-conformance.py`,
  `.factory/tests/test-factory-plan-parser.py`,
  `tests/test-conformance.sh`, `tests/test-blocked-facts.sh`,
  `scripts/validate-conformance.py`, and
  `scripts/check-capability-evidence.py`.
- Documentation impact: none (sidecar is machine data).
- Evidence: hidden conformance 37/37, parser 17/17, selector 32/32,
  visible conformance and blocked-facts suites pass; planning mode validates
  all 24 requirements while complete mode honestly exits 1 on `partial` /
  `blocked` rows. Policy, sidecar, plan matrix, and committed §24 registry
  match exactly; only RUNNER-01 requires the undeclared `hardware-runner` and
  remains blocked, no row is verified, and no unavailable runner/system/human
  evidence is elevated. Duplicate keys/IDs, stale/traversing refs, proxy tiers,
  unevidenced verified capabilities, PATH/Git-config redirects, and replace
  objects fail closed. Independent evidence review accepted the checkpoint;
  stale ignored runner aggregate state is not evidence and is not committed.

## Task 15: Migration and deprecation of the Ralph control plane

- Status: complete
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
  Migrate and deprecate the legacy workspace `.ollama-usage-env` store: the
  new path never reads `<workspace>/.ollama-usage-env` or
  `<repository root>/.ollama-usage-env` (assigned from the Task 7 review,
  which moved the operator store default outside the model workspace), and
  any pre-existing workspace-scoped legacy store is migrated/deprecated to
  the operator store outside the model workspace without being read as a
  credential authority.
- Acceptance criteria: migration fixtures prove plan/commits/dirty-work/
  evidence/blockers survive while no `.ralph/` runtime state is imported;
  deprecation forwarders are marked and optional; the generic suite has no
  Ralph dependency; the new control flow generates, checks, or reads no
  context summary, and the stale
  `.factory/artifacts/context-summary.md` (with its
  `scripts/ralph-context-summary.py` / `scripts/check-context-summary.py`
  verifier and `tests/test-context-summary.sh` wiring) is removed/deprecated
  from every new-path control step, so a plan-mirror drift cannot surface as
  an acceptance failure; the legacy workspace `.ollama-usage-env` store is
  migrated/deprecated and no new-path code reads a workspace- or
  repository-scoped Ollama credential store.
- Verification: `.factory/tests/test-factory-migration.py`,
  `.factory/tests/test-factory-migration.sh`, and the complete
  `scripts/verify-boilerplate.sh` gate. The suites prove the new path has no
  context-summary dependency, imports no Ralph runtime authority, and reads no
  workspace/repository-scoped `.ollama-usage-env` credential bytes.
- Documentation impact: `docs/FACTORY.md`, `docs/OPERATIONS.md`, `README.md`.
- Evidence: migration 67/67, state 129/129, and every retained hidden factory
  regression passes; `scripts/verify-boilerplate.sh` exits 0. The migration
  binds plan/HEAD/dirty/evidence/blocker metadata, publishes only the single
  no-replace `factory-state/v1` authority, freezes all six Ralph launchers,
  removes and unwires the persisted context-summary authority, and detects
  legacy credential stores metadata-only. Blob reads are pre-sized and bounded,
  blocker reads are anchored no-follow identity-checked, and state descriptors
  are close-on-exec. Independent security review accepted the final checkpoint
  with no blockers; the shell freeze-marker local-writer race and legacy
  maintenance verifier routing remain Task 16 adversarial/deletion work.

## Task 16: Adversarial conformance suite and verification gate

- Status: complete
- Dependencies: Task 3, Task 4, Task 5, Task 6, Task 7, Task 8, Task 9, Task 10, Task 11, Task 12, Task 13, Task 14, Task 15
- Scope: Implement the full §22 conformance suite (tests 1-27: fresh roles
  and allowed inputs, memory/session disabled, deterministic selection, no
  runtime ledger, findings only via plan, empty work reaches verification/
  audit, external blockers end nonzero without elevation, pass impossible with
  unresolved findings, one-writer lock concurrency, full process-group signal
  delivery and reap, timeout/crash dirty-work preservation, tamper state fail
  closed, Ollama check/wait before invocation with quota errors blocking,
  credential enforcement and redaction active, exact-commit receipt/manifest
  trust, harness-removal privileged bind-mount swap/TOCTOU refusal (the Task 13
  accepted residual), deprecated shell freeze-marker local-writer races and
  legacy maintenance verifier routing through the retained descriptor authority
  (Task 15 residuals), no completion-token bypass, finite termination fixtures
  for every
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
  `.factory/tests/test-factory-adversarial.sh`.
- Documentation impact: `docs/FACTORY.md`; Task 17 completes synchronized
  operator and template documentation.
- Evidence: exact commit `11a4f8e98497fefcff1c5917568e663c1ab48350`
  contains the machine-complete 27-case manifest, warning-free adversarial
  suite, five-round success/findings fixtures, atomic freeze guard,
  descriptor-bound maintenance verifier, and deletion revalidation. The hidden
  adversarial gate and complete `scripts/verify-boilerplate.sh` both exit 0.
  Independent specification, security, code-quality, and focused findings-flow
  reviews accepted the checkpoint after case 5 was made non-vacuous, case 15
  exercised every retained receipt family, case 22 became a hard Landlock
  failure, and case 27 scanned the full new control plane. Installed-tier
  campaign evidence remains honestly open as FACT-022/FACT-023.

## Task 17: Documentation synchronization

- Status: complete
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
  `docs/BUG_WORKFLOW.md`, `AGENTS.md`, help text.
- Evidence: the documentation gate checks canonical spec/plan/state bindings,
  every documented CLI/path/help surface, frozen Ralph framing, planner-only
  findings, finite outcomes, migration and recovery behavior, Linux/Landlock/
  `/proc`/OpenSSH prerequisites, and adopting-product verifier boundaries.
  `AGENTS.md` is 94 lines and the gate enforces that limit. Docs sync, plan
  freshness, plan-cycle, bug-workflow, hidden parser/selector/state, and the
  complete boilerplate gate exit 0. Independent documentation review found no
  stale operative Ralph/context-summary/resume/task-ledger claim and no runner,
  visual, installed, real-system, or human evidence overclaim.

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

## Task 20: Installed-tier evidence for the generic harness

- Status: complete
- Dependencies: Tasks 1-19
- Scope: Remediate campaign-audit Findings 2 and 6: build the installed-
  harness **mechanics** — a clean exact-commit installation of the generic
  harness into test-owned external and hidden prefixes (per-launch
  test-owned directories outside the model workspace — a custom
  external-prefix install plus a hidden dot-prefixed install under the
  test-owned namespace), the physical installed-file inventory, and
  execution of the production control-plane CLIs and deterministic gates
  from the installed copy. Source-tree runs and private unit-test runs are
  never relabeled as installed-tier evidence. Production paths executed
  from the installed copy include `python -m factory.loop.launch` (help and
  CLI surface plus the external-prefix launcher entry point),
  `factory-campaign`, the parser/selector/state CLIs,
  `scripts/machine-receipt.py`, and the installed footprint inventory.
  Every installed-tier gate is minted as an exact-commit receipt via
  `scripts/machine-receipt.py`; at this commit the receipts are
  fixture-authority only (minted under the fixture authority's audit
  coordinator, never against the live `.factory-state/`), so
  `scripts/check-audit-receipts.py` exits 0; the physical installed-file
  inventory (path, mode, owner, link count) is captured and asserted to
  stay inside the test-owned external/hidden prefixes and the hidden
  `.factory/`/`.factory-state/`/`.pi/` namespaces (HIDE-01). The live
  generic-namespace evidence, coordinator receipts at the audit base, and
  the fresh check-installed acceptance are owned by pending Task 23 after
  Task 22.
- Acceptance criteria: the installed copy is a fresh build from the exact
  bound commit `6b9c626` into test-owned external/hidden prefixes; the
  physical installed-file inventory is complete and confined; every
  installed-tier production CLI/gate runs from the installed copy with its
  documented exit status; every installed-tier gate has an exact-commit
  fixture-authority receipt and `scripts/check-audit-receipts.py` exits 0;
  no source-tree or private-test run is claimed as installed-tier evidence,
  and no live installed-functional evidence is staged by this task (the
  live `.factory-state/` is never touched).
- Verification: `scripts/verify-boilerplate.sh`;
  `scripts/check-audit-receipts.py`;
  `scripts/check-installed-functional-evidence.sh`;
  `.factory/tests/test-factory-footprint.sh` installed inventory;
  `scripts/validate-conformance.py planning`; `scripts/check-docs-sync.sh`.
- Documentation impact: `docs/FACTORY.md`, `docs/OPERATIONS.md`.
- Evidence: exact commit `6b9c626` completes the installed-harness
  mechanics with fixture-authority receipts only — no live
  installed-functional evidence is staged (the live `.factory-state/`
  is never touched, replaced, or relabeled). The installed-tier machinery
  is implemented and proven by the hidden suite
  `.factory/tests/test-factory-installed.py/.sh` — a descriptor-anchored
  trusted installer
  (`.factory/loop/installer.py`, prefix opened `O_DIRECTORY|O_NOFOLLOW|
  O_CLOEXEC` with fd identity/containment revalidated per stage and
  `openat` staging), a single fair select event loop in
  `.factory/loop/gitutil.py` (stdin writes + stdout/stderr drains against
  one shared deadline, EPIPE/EOF handled, >128 KiB stdin and >128 KiB
  output regression), the external-prefix launcher
  `.factory/bin/factory-launch` (forwards INT/TERM/HUP/QUIT, bounded
  wait/reap before alias cleanup, actual or 128+signal status), the
  installed physical-file inventory in `.factory/loop/footprint.py`, and
  the installed-root-attested receipt gates.  The
  `.factory/tests/test-factory-migration.py` `GitBytesBoundedTests`
  regressions (fair single-loop drain, bounded group termination, the
  batched-stdin and >128 KiB deadlock tests) are part of the committed
  Task-20 change and pass at `6b9c626`; the installed suite derives the
  actual pending set independently so the allowlist is not fragile.  Live installed-functional evidence is **not** staged at `6b9c626`: the
  fresh generic-namespace evidence, the coordinator receipts at the audit
  base, and the check-installed acceptance are owned by pending Task 23
  after Task 22.

## Task 21: Round-1 campaign audit objective coverage

- Status: blocked
- Dependencies: Task 20
- Blocked on: FACT-020 and Task 24 — the round-1 audit objective
  `runner-capability` (receipt categories `runner-evidence` and
  `project-verify`) cannot be covered at the boilerplate audit base: no
  runner capability is declared, the signer trust is disabled, and no
  coordinator receipts exist (campaign-audit.md Finding 4). The
  `runner-evidence` category can be satisfied only by an accepted
  exact-commit signed runner manifest, which requires the external human
  provisioning of Task 24; until then this task stays blocked and must never
  pretend PASS.
- Scope: Remediate campaign-audit Finding 4 by covering the round-1
  objective's receipt categories with genuine evidence. `project-verify`
  becomes coverable by a fresh exact-commit receipt of
  `./scripts/verify-boilerplate.sh` once Tasks 20/22/23 pass; `runner-evidence`
  remains coverable only by an exact signed runner manifest accepted by
  `scripts/check-factory-runner-evidence.py` after the external human
  completes Task 24. While any category is uncovered,
  `scripts/check-campaign-objectives.py` exits 1 and the campaign audit keeps
  reporting `findings`; partial or fabricated coverage is never claimed and
  no private/synthetic evidence is elevated to the runner-evidence category.
- Acceptance criteria: the round-1 objective is either fully covered by
  genuine receipts/manifests (only after Task 24 provisions the runner) or
  explicitly reported uncovered with the audit result `findings`; no PASS is
  pretended while `runner-evidence` is uncovered;
  `scripts/check-campaign-objectives.py` reflects the true covered/uncovered
  state at the audit base.
- Verification: `scripts/check-campaign-objectives.py --round 1 --base <exact-commit>`;
  `scripts/check-audit-receipts.py`;
  `scripts/check-factory-runner-evidence.py --print-capabilities`.
- Documentation impact: `docs/FACTORY.md`,
  `.factory/artifacts/campaign-audit.md`.

## Task 22: Live campaign and control-state instantiation

- Status: pending
- Dependencies: Task 20
- Scope: Remediate campaign-audit Finding 5: drive the real production
  campaign machinery at the bound commit — `.factory/loop/campaign.py run`
  against the real production plan and `.factory-state` paths — so the single
  `factory-state/v1` control-state file `.factory-state/factory-loop.json` is
  instantiated and exercised through the §11 transition table, §13 phase
  outcomes, write-once bindings, and the before/after untrusted-phase digest
  ledger. Methodology evidence is produced through a deterministic,
  designated smoke-role seam: the designated smoke role runs the real launch
  CLI against the real campaign state with a deterministic synthetic task so
  the fresh-process, lock, supervision, selection, and phase paths are
  exercised end-to-end. The seam is explicitly labeled private source
  methodology evidence — never a real model or human outcome, never
  installed-tier evidence, never GIT-01 acceptance evidence, and never
  acceptance-tier evidence; no external model, credentials, or cookies are
  used. The planner output keeps Task 22 `pending`; per spec §6.2 only the
  developer role may mark the selected task `complete`. The campaign
  terminates in a documented finite terminal (success/findings/blocked/
  failed/interrupted/infrastructure_failure) with no orphaned process;
  STATE-01, LOCK-01, PROC-01, PHASE-01, TASK-01, TASK-02, and the guarded
  Git boundary are exercised at campaign level without claiming
  installed-tier or GIT-01 acceptance evidence from the smoke seam.
- Acceptance criteria: a live campaign at the bound commit instantiates
  `.factory-state/factory-loop.json` with exactly the §11 field set and
  drives at least one full phase cycle through the trusted control plane to a
  finite terminal; the smoke seam is deterministic and labeled private
  source methodology (never installed-tier, never GIT-01); no external
  model/cookies/credentials are invoked; state digest verification,
  `scripts/check-plan-freshness.sh`, and
  `scripts/check-generic-leakage.sh` pass.
- Verification: `.factory/tests/test-factory-campaign.py`;
  `.factory/loop/campaign.py run` live at the bound commit;
  `.factory/loop/state.py show`; `scripts/check-plan-freshness.sh`;
  `scripts/check-generic-leakage.sh`.
- Documentation impact: `docs/OPERATIONS.md`.

## Task 23: Generic evidence-scope authority for foreign artifacts

- Status: pending
- Dependencies: Task 20, Task 22
- Scope: Remediate campaign-audit Finding 6 without touching foreign runtime
  evidence. The pre-existing `.factory-state/` content (for example
  `.factory-state/installed-functional-evidence.env`, which records a
  foreign adopting-product commit `61356a0` that is not an ancestor of the
  boilerplate audit base) is foreign runtime evidence and MUST be
  preserved byte-for-byte: no delete, quarantine, or mutation of any existing
  `.factory-state` file. This task owns the **live** installed-tier
  evidence for the generic harness after Task 22: it runs the installed
  suite (Task 20 machinery) at the bound Task-23 commit, stages the fresh
  live evidence under a dedicated generic evidence namespace (for example
  `.factory-state/generic-evidence/`), mints the installed-tier coordinator
  receipts at the audit base under `.factory-state/audit-receipts/`, and
  produces the fresh check-installed acceptance
  (`scripts/check-installed-functional-evidence.sh` exit 0). It scopes
  every generic checker's and generic read authority to the exact bound
  commit and the dedicated generic evidence namespace:
  `scripts/check-installed-functional-evidence.sh` and sibling generic
  readers ignore foreign artifacts outside that namespace, so a foreign
  artifact causes no error and is never rewritten. Preservation is proven
  by recording the foreign files' byte digests before and after the task
  and verifying the digests, mode, and mtime are unchanged.
- Acceptance criteria: the pre-existing foreign `.factory-state` bytes are
  byte-identical after the task (digest snapshot proves untouched); the
  installed suite (Task 20 machinery) runs from the installed copy at the
  bound Task-23 commit and mints the live generic-namespace evidence and
  coordinator receipts at the audit base so `scripts/check-audit-receipts.py`
  exits 0; generic checkers read only the exact-commit dedicated generic
  namespace and ignore the foreign artifact; no generic tooling deletes,
  quarantines, or mutates a foreign artifact;
  `scripts/check-installed-functional-evidence.sh` exits 0 on the fresh
  generic evidence and `scripts/check-generic-leakage.sh` passes.
- Verification: byte-digest before/after snapshot of the foreign
  `.factory-state` files (digest, mode, and mtime recorded and re-verified);
  `scripts/check-installed-functional-evidence.sh`;
  `scripts/check-audit-receipts.py`;
  `scripts/check-generic-leakage.sh`; `scripts/check-docs-sync.sh`.
- Documentation impact: `docs/FACTORY.md`, `docs/OPERATIONS.md`.

## Task 24: External human runner provisioning (RUNNER-01)

- Status: blocked
- Dependencies: Task 20, Task 23
- Blocked on: FACT-020 — RUNNER-01 requires real_system evidence from a
  declared, provisioned, signed hardware runner, an external human action
  this environment cannot perform: declare the runner in
  `.factory/environment.toml`, provision the signer trust in
  `.factory/signer-trust.json`, run the runner against the exact audit base,
  and have the signed manifest accepted by
  `scripts/check-factory-runner-evidence.py`.
- Scope: Remediate campaign-audit Finding 3 (RUNNER-01 real_system evidence
  blocked on an undeclared, unprovisioned hardware runner). While the human
  action is outstanding, the task stays blocked, RUNNER-01 stays `blocked`,
  and FACT-020 stays open; private/synthetic evidence is never elevated to
  the real_system tier. When the human completes provisioning, the accepted
  exact-commit signed manifest (capabilities non-empty) is the only evidence
  that lets RUNNER-01 move toward `verified`, and the round-1
  `runner-evidence` category (Task 21) becomes coverable.
- Acceptance criteria: without the human action the task remains blocked and
  the conformance row remains `blocked`; after the human action an
  exact-commit signed runner manifest is accepted by
  `scripts/check-factory-runner-evidence.py` (exit 0, capabilities non-empty)
  and `scripts/check-capability-evidence.py` passes with a fresh exact-commit
  probe; no fake or simulated runner evidence is ever recorded.
- Verification: `scripts/check-factory-runner-evidence.py --print-capabilities`;
  `scripts/run-factory-runners.py`; `scripts/check-capability-evidence.py`;
  `scripts/check-factory-environment.py .factory/environment.toml`.
- Documentation impact: none beyond the evidence receipts and blocked-facts
  resolution.

## Task 25: Final documentation and specification audit

- Status: pending
- Dependencies: Tasks 1-24
- Scope: Independent read-only audit and review at the final committed
  revision verifies the definition of done: every conformance row in the
  matrix and the sidecar is `verified` with exact-commit evidence at the
  required tier (installed-tier receipts minted from the installed copy,
  live campaign control state, and runner evidence only as the Task 24
  external-human resolution permits), the interaction inventory covers
  input/semantic/production/evidence, all open findings and blocked facts
  are closed or explicitly documented, the repository is clean at the audit
  commit, and the documentation (README, FACTORY, OPERATIONS, AGENTS) is in
  sync. The auditor is a separate fresh process with a distinct static
  prompt, no developer/tester conversation, and reads only authoritative
  inputs at the exact commit. An audit finding becomes a next-round planner
  task; the audit itself never edits product code or the plan. The audit
  report cites `[receipt: …]`/`[manifest: …]` exact references, and any
  BLOCKED evidence forces result `findings`.
- Acceptance criteria: audit report records every §24 requirement verified
  or an explicit finding; no acceptance-critical audit finding remains; the
  campaign does not claim success unless the final audit is clean, the
  conformance sidecar shows all verified, and the `complete` mode of
  `scripts/validate-conformance.py` exits 0.
- Verification: `scripts/validate-conformance.py`;
  `scripts/check-docs-sync.sh`; `scripts/check-audit-receipts.py`;
  `scripts/check-campaign-objectives.py`; independent audit evidence
  appended to `.factory/artifacts/campaign-audit.md`.
- Documentation impact: `.factory/artifacts/campaign-audit.md`.
