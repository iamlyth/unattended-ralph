---
spec_path: docs/SPEC.md
spec_commit: 1111111111111111111111111111111111111111
spec_blob: 2222222222222222222222222222222222222222
base_commit: 3333333333333333333333333333333333333333
status: active
---

# Implementation Plan

## Goal and non-goals

Goal: exercise the `factory-plan/v1` grammar deterministically.

## Architecture and constraints

Plan: Python 3.11 standard library only; the parser never reads process state.

## Specification conformance matrix

| ID | Spec § | Classification | Evidence | Task |
|----|--------|--------------|----------|------|
| AUTH-01 | §5, §7 | missing | fixture exercises a registry-bound matrix | Task 1 |
| CTX-01 | §5, §9 | missing | fixture exercises a registry-bound matrix | Task 1 |
| CTX-02 | §5, §18 | missing | fixture exercises a registry-bound matrix | Task 1 |
| ROLE-01 | §6 | missing | fixture exercises a registry-bound matrix | Task 1 |
| PLAN-01 | §7 | missing | fixture exercises a registry-bound matrix | Task 1 |
| TASK-01 | §7, §8 | missing | fixture exercises a registry-bound matrix | Task 1 |
| TASK-02 | §9, §20 | missing | fixture exercises a registry-bound matrix | Task 1 |
| QUOTA-01 | §10 | missing | fixture exercises a registry-bound matrix | Task 1 |
| QUOTA-02 | §10 | missing | fixture exercises a registry-bound matrix | Task 1 |
| STATE-01 | §11, §17 | missing | fixture exercises a registry-bound matrix | Task 1 |
| STATE-02 | §11, §13, §14, §15 | missing | fixture exercises a registry-bound matrix | Task 1 |
| LOCK-01 | §12 | missing | fixture exercises a registry-bound matrix | Task 1 |
| PROC-01 | §9, §12, §17 | missing | fixture exercises a registry-bound matrix | Task 1 |
| GIT-01 | §12, §17 | missing | fixture exercises a registry-bound matrix | Task 1 |
| PHASE-01 | §13, §14 | missing | fixture exercises a registry-bound matrix | Task 1 |
| COMPLETE-01 | §15 | missing | fixture exercises a registry-bound matrix | Task 1 |
| FIND-01 | §16 | missing | fixture exercises a registry-bound matrix | Task 1 |
| CRED-01 | §18 | missing | fixture exercises a registry-bound matrix | Task 1 |
| EVID-01 | §19 | missing | fixture exercises a registry-bound matrix | Task 1 |
| EVID-02 | §19 | missing | fixture exercises a registry-bound matrix | Task 1 |
| VIS-01 | §19 | missing | fixture exercises a registry-bound matrix | Task 1 |
| RUNNER-01 | §19 | missing | fixture exercises a registry-bound matrix | Task 1 |
| HIDE-01 | §3 | missing | fixture exercises a registry-bound matrix | Task 1 |
| MIG-01 | §21 | missing | fixture exercises a registry-bound matrix | Task 1 |
| TEST-01 | §22 | missing | fixture exercises a registry-bound matrix | Task 1 |
| ACCEPT-01 | §23 | missing | fixture exercises a registry-bound matrix | Task 2 |

## Interaction acceptance inventory

- input boundary: only the committed plan bytes reach the parser.
- semantic boundary: no memory or runtime ledger is read or written.
- production boundary: the parser never writes product code.
- evidence boundary: the model is a deterministic function of the bytes.

## Task 1: Parse a canonical fixture plan

- Status: pending
- Dependencies: None
- Priority: 3
- Scope: one fixture plan parses and round-trips byte-exactly.
- Acceptance criteria: the parser accepts and the model is deterministic.
- Verification: `.factory/tests/test-factory-plan-parser.py`.
- Documentation impact: none.

## Task 2: Final documentation and specification audit

- Status: pending
- Dependencies: Task 1
- Scope: fixture audit task for the acceptance plan; the definition of done
  covers the conformance matrix, the interaction inventory, open findings,
  the final review, and a clean tree.
- Acceptance criteria: the fixture remains parseable.
- Verification: the fixture suite.
- Documentation impact: none.
