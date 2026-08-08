# Maintenance Planning Loop

Plan one selected defect; do not implement it. The selected ID is in `.factory-state/maintenance-bug-id`. Canonical state is `open-bugs.md`; external GitHub/Forgejo issues are references only.

A new cycle intentionally starts from a minimal `MAINTENANCE_PLAN.md` skeleton. Do not retrieve, copy, summarize, or append tasks or evidence from an older maintenance plan in Git history. Plan only the selected bug from its current record and current repository state; Git history and `closed-bugs.md` archive prior cycles.

Validate the ledgers and study `AGENTS.md` plus the selected `triaged` record with `scripts/bug-ledger.py`. A resumed planning run may already be `planned`. Search relevant production source, tests, shared utilities, TODOs, placeholders, skipped/flaky tests, and inconsistent patterns before accepting the reported diagnosis—do not assume functionality is missing or the stated cause is correct. Stop with the task blocked if the bug has `contract_change: true`, requires a product decision, or cannot be resolved without editing `docs/SPEC.md`; a human must use the specification workflow. Never edit the specification.

You are the only writer. Read-only subagents may inspect code, tests, security, and documentation. Modify only `MAINTENANCE_PLAN.md` and `.ralph/agent/scratchpad.md`.

Preserve the launcher-provided front-matter values from the fresh skeleton throughout the cycle, especially the immutable `base_commit`; do not recompute it from planning-checkpoint `HEAD`. Replace the plan body while retaining this exact front-matter key set:

```yaml
---
bug_id: BUG-0001
bug_fingerprint: <sha256>
spec_path: docs/SPEC.md
spec_commit: <latest commit changing the spec>
spec_blob: <HEAD spec blob>
base_commit: <HEAD before plan commit>
status: active
---
```

Include goal, non-goals, defect analysis, and numbered bounded tasks. Every task uses:

```markdown
## Task N: Short title
- Status: pending
- Dependencies: none
- Scope: bounded files and behavior
- Acceptance criteria: objective result
- Verification: exact commands/evidence
- Documentation impact: files or none
```

Every task in a newly generated maintenance plan must start with exactly `pending`; planning completion rejects inherited `complete`, `in_progress`, or `blocked` tasks. Include tests with each change, derived from the record's observable expected behavior, reproduction, edge cases, and acceptance criteria—state what must be verified, not how to implement it. The last task must be titled exactly **Maintenance verification and documentation audit**, depend on every prior task, verify the acceptance criteria, close only the selected bug with non-empty resolution and verification, run final checks, and update relevant docs. One maintenance cycle handles one bug.

The strict parser requires front matter at byte zero, exactly those seven unique keys, contiguous task numbering, and exactly one of every listed task field. `Status` must have its value on the same line. Other fields may continue on following indented lines, but each field must contain non-empty content. Do not add front-matter keys or omit task fields.

When the plan is not yet coherent, fresh, and limited to one ordinary defect, replace rather than append to the scratchpad with one concise next action and continue another iteration. The launcher commits an accepted planning checkpoint, transitions `triaged` to `planned`, and creates a separate ledger-only checkpoint.

## Completion protocol

`MAINTENANCE_PLAN_COMPLETE` is a reserved protocol token. Never write it into the maintenance plan, scratchpad, an event topic or payload, a summary, or explanatory prose. If planning still needs another iteration, finish the normal event and exit without emitting the token. When and only when planning is complete, publish any required `factory.maintenance.plan` summary without that token, close the event tag, and then output exactly `MAINTENANCE_PLAN_COMPLETE` as the final non-empty line outside every event tag. Do not add a colon, punctuation, Markdown fencing, or text after it. If the final gate rejects completion, the supervisor resumes the same draft; repair the reported deficiency rather than repeating the completion request.
