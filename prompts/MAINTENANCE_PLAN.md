# Maintenance Planning Loop

Plan one selected defect; do not implement it. The selected ID is in `.factory-state/maintenance-bug-id`. Canonical state is `open-bugs.md`; external GitHub/Forgejo issues are references only.

A new cycle intentionally starts from a minimal `MAINTENANCE_PLAN.md` skeleton. Do not retrieve, copy, summarize, or append tasks or evidence from an older maintenance plan in Git history. Plan only the selected bug from its current record and current repository state; Git history and `closed-bugs.md` archive prior cycles.

Validate the ledgers and inspect the selected `triaged` record with `scripts/bug-ledger.py`. A resumed planning run may already be `planned`. Stop with the task blocked if the bug has `contract_change: true`, requires a product decision, or cannot be resolved without editing `docs/SPEC.md`; a human must use the specification workflow. Never edit the specification.

You are the only writer. Read-only subagents may inspect code, tests, security, and documentation. Modify only `MAINTENANCE_PLAN.md` and `.ralph/agent/scratchpad.md`.

Replace the plan with this exact front-matter key set, using values obtained from Git and `bug-ledger.py fingerprint`:

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

Every task in a newly generated maintenance plan must start with exactly `pending`; planning completion rejects inherited `complete`, `in_progress`, or `blocked` tasks. Include tests with each change. The last task must be titled exactly **Maintenance verification and documentation audit**, depend on every prior task, verify the acceptance criteria, close only the selected bug with non-empty resolution and verification, run final checks, and update relevant docs. One maintenance cycle handles one bug.

The strict parser requires front matter at byte zero, exactly those seven unique keys, contiguous task numbering, and exactly one of every listed task field. `Status` must have its value on the same line. Other fields may continue on following indented lines, but each field must contain non-empty content. Do not add front-matter keys or omit task fields.

When the plan is coherent, fresh, and limited to an ordinary defect, end with `MAINTENANCE_PLAN_COMPLETE`. The launcher commits the planning checkpoint, transitions `triaged` to `planned`, and creates a separate ledger-only checkpoint. Otherwise record the next action in the scratchpad and continue another iteration.
