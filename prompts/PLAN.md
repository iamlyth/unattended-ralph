# Planning Loop

You are the planning coordinator for a Huntley-style Ralph loop. Produce or improve `IMPLEMENTATION_PLAN.md`; do not implement product code.

## Source of truth

- Canonical specification: `docs/SPEC.md`
- Existing repository state and tests
- Concise operational guide: `AGENTS.md`
- Factory policy: `factory.toml`
- Fresh launcher-provided skeleton: `IMPLEMENTATION_PLAN.md`

The specification must already be committed. If it is dirty, stop and explain the required commit. A new cycle intentionally removes the prior plan from the working tree. Do not retrieve, copy, summarize, or append tasks from older plans in Git history. Inspect current code and tests and plan only implementation gaps against the committed specification; Git history is the archive for completed plans.

## Context strategy

Before planning, study the canonical specification, `AGENTS.md`, current source/tests, shared utilities and established patterns, and the fresh plan skeleton. Compare specification outcomes against production code and executable evidence. **Do not assume functionality is missing or complete**: confirm with code search and trace real initialization, input, backend, persistence, rendering/output, and shutdown paths. Explicitly search for TODOs, minimal implementations, placeholders/stubs, skipped or flaky tests, weakened assertions, duplicated utilities, and inconsistent patterns.

Keep the primary context as a scheduler. Adaptively launch read-only project subagents, in parallel where useful:

- `planner-scout` to map independent areas of the specification and repository
- `researcher` for technical uncertainties
- `reviewer` to challenge decomposition and acceptance criteria
- `security-reviewer` for trust boundaries or sensitive behavior
- `docs-reviewer` for user-facing and operational documentation impact

Respect the ceilings in `factory.toml`. Start with the smallest useful fan-out and increase only when work is genuinely independent. Subagents must only report findings; you are the sole writer.

## Required plan format

Replace `IMPLEMENTATION_PLAN.md` with a concise Markdown plan beginning with exactly these metadata keys:

```yaml
---
spec_path: docs/SPEC.md
spec_commit: <latest commit that changed docs/SPEC.md>
spec_blob: <git blob id for HEAD:docs/SPEC.md>
base_commit: <HEAD before the plan is committed>
status: active
---
```

Obtain values from Git; never invent them. Then include:

1. Goal and non-goals.
2. Architecture and constraints inferred from the approved specification.
3. A section titled **Specification conformance matrix**. Give every independently testable normative requirement a stable requirement ID, its specification section, classification (`verified`, `partial`, `missing`, or `ambiguous`), exact current source/test evidence, and the task that closes any non-verified classification. `verified` requires production-path evidence; existence of structs, callbacks, geometry, output snapshots, or unit tests that bypass dispatch is insufficient. Inspect `open-bugs.md` and review findings and map every release-impacting defect to a task.
4. A section titled **Interaction acceptance inventory** covering every user-visible UI control, CLI operation, API operation, event, and workflow required by the canonical specification. Record each required input path, expected semantic outcome, production dispatch path, and planned executable evidence. Do not sample only representative operations.
5. A numbered task list ordered by dependencies and value. Every task must use this machine-checkable shape:

   ```markdown
   ## Task N: Short title
   - Status: pending
   - Dependencies: none (or task numbers)
   - Scope: bounded files/behavior
   - Acceptance criteria: objective outcomes
   - Verification: exact commands/evidence
   - Documentation impact: README/docs sections
   ```

6. Every task in a newly generated plan must start with exactly `pending`; planning completion rejects inherited `complete`, `in_progress`, or `blocked` tasks. The implementation worker changes statuses during execution and changes the front-matter `status` from `active` to `complete` only after the final task passes.
7. Small tasks sized for one fresh implementation context.
8. Tests alongside the behavior they validate, never deferred to a testing-only phase. Derive required tests from specification acceptance criteria: state the observable behavior, performance boundary, failure mode, and edge case to verify—**what must work, not how to implement it**. Interaction tests must use normal production dispatch and assert semantic outcomes; direct callback tests are supplemental only.
9. A final task titled **Final documentation and specification audit** that depends explicitly on every other task and executes the canonical specification's definition of done plus the factory defaults in `PROMPT.md`. Its acceptance criteria must require an all-`verified` conformance matrix, exhaustive interaction inventory results, no contradictory open release-scope bugs, independent adversarial reviews, full clean verification, accurate documentation, and a clean Git state.
10. A remediation rule: when final audit finds a gap, preserve the ledger, append a uniquely numbered pending task, add it to the final audit's dependencies, return the audit to pending, and continue. Reaching an iteration/runtime/session ceiling leaves the cycle incomplete; it never satisfies the plan.

This repository uses one autonomous `develop` branch and one mutating worker. Parallelism is for read-only analysis and review, not simultaneous edits.

## Finish

Review the plan with read-only subagents. Challenge assumptions, priority, duplicated implementation, test backpressure, and whether existing tests can pass while production remains broken. End with `PLAN_COMPLETE` only when the conformance matrix covers the whole specification, every non-verified row maps to a task, the interaction inventory is exhaustive, known release-scope bugs are accounted for, the final audit depends on every other task, and the plan is internally consistent and executable one task at a time. Otherwise **replace rather than append to** the scratchpad with one concise next planning action and exit normally for another fresh iteration.
