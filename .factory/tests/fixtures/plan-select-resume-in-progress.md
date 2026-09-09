---
spec_path: docs/FACTORY-LOOP-SPEC.md
spec_commit: 1111111111111111111111111111111111111111
spec_blob: 2222222222222222222222222222222222222222
base_commit: 3333333333333333333333333333333333333333
status: active
---

# Implementation Plan

## Goal and non-goals

Goal: exercise the deterministic §8 selector resume rule.

## Architecture and constraints

Plan: Python 3.11 standard library only; selection is a pure function of the
parsed plan and never consults a runtime task ledger.

## Specification conformance matrix

| ID | Spec § | Classification | Evidence | Task |
|----|--------|--------------|----------|------|
| AUTH-01 | §5, §7 | missing | fixture exercises selector determinism | Task 1 |
| CTX-01 | §5, §9 | missing | fixture exercises selector determinism | Task 1 |
| CTX-02 | §5, §18 | missing | fixture exercises selector determinism | Task 1 |
| ROLE-01 | §6 | missing | fixture exercises selector determinism | Task 1 |
| PLAN-01 | §7 | missing | fixture exercises selector determinism | Task 1 |
| TASK-01 | §7, §8 | missing | fixture exercises selector determinism | Task 1 |
| TASK-02 | §9, §20 | missing | fixture exercises selector determinism | Task 1 |
| QUOTA-01 | §10 | missing | fixture exercises selector determinism | Task 1 |
| QUOTA-02 | §10 | missing | fixture exercises selector determinism | Task 1 |
| STATE-01 | §11, §17 | missing | fixture exercises selector determinism | Task 1 |
| STATE-02 | §11, §13, §14, §15 | missing | fixture exercises a registry-bound matrix | Task 1 |
| LOCK-01 | §12 | missing | fixture exercises selector determinism | Task 1 |
| PROC-01 | §9, §12, §17 | missing | fixture exercises selector determinism | Task 1 |
| GIT-01 | §12, §17 | missing | fixture exercises selector determinism | Task 1 |
| PHASE-01 | §13, §14 | missing | fixture exercises selector determinism | Task 1 |
| COMPLETE-01 | §15 | missing | fixture exercises selector determinism | Task 1 |
| FIND-01 | §16 | missing | fixture exercises selector determinism | Task 1 |
| CRED-01 | §18 | missing | fixture exercises selector determinism | Task 1 |
| EVID-01 | §19 | missing | fixture exercises selector determinism | Task 1 |
| EVID-02 | §19 | missing | fixture exercises a registry-bound matrix | Task 1 |
| VIS-01 | §19 | missing | fixture exercises selector determinism | Task 1 |
| RUNNER-01 | §19 | missing | fixture exercises selector determinism | Task 1 |
| HIDE-01 | §3 | missing | fixture exercises selector determinism | Task 1 |
| MIG-01 | §21 | missing | fixture exercises selector determinism | Task 1 |
| TEST-01 | §22 | missing | fixture exercises selector determinism | Task 1 |
| ACCEPT-01 | §23 | missing | fixture exercises selector determinism | Task 1 |
| LEASE-01 | §14.2, §22, §24 | missing | fixture exercises selector determinism | Task 1 |

## Interaction acceptance inventory

- input boundary: only the committed plan model reaches the selector.
- semantic boundary: no memory or runtime ledger is read or written.
- production boundary: the selector never writes product code.
- evidence boundary: selection is a deterministic function of plan and bytes.

## Task 1: In-progress task to resume

- Status: in_progress
- Dependencies: None
- Priority: 5
- Scope: the sole in-progress task must be resumed regardless of priority.
- Acceptance criteria: the selector resumes Task 1.
- Verification: the hidden selector fixture suite.
- Documentation impact: none.

## Task 2: Final documentation and specification audit

- Status: pending
- Dependencies: Task 1
- Scope: fixture audit task for the acceptance plan; the definition of done
  covers the conformance matrix, the interaction inventory, open findings,
  the final review, and a clean tree.
- Acceptance criteria: the fixture remains parseable and the selector never
  selects the audit while Task 1 is in progress.
- Verification: the fixture suite.
- Documentation impact: none.
