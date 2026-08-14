# Maintenance Implementation Loop

Implement exactly the bug selected in `.factory-state/maintenance-bug-id` according to `.factory/artifacts/maintenance-plan.md`.

Before selecting work, study `AGENTS.md`, the selected canonical bug record, the complete maintenance plan, the latest scratchpad handoff, and relevant source/tests. Search and trace the production path before changing code—do not assume the reported cause is correct or functionality is absent merely from names, TODOs, or prior evidence. Use `AGENTS.md` for serialized build/run/validation commands.

Non-negotiable rules:

1. Run `scripts/check-maintenance-freshness.sh` before work.
2. Never edit `docs/SPEC.md`. If a contract change or product decision is discovered, mark the current task blocked and require the human specification workflow.
3. On the first implementation task, transition the selected bug from `planned` to `in_progress` with `bug-ledger.py` before changing product code. Then select one ready pending task, mark it in_progress, and implement only that bounded task.
4. You are the sole writer. Subagents are read-only.
5. Fix the root cause completely; do not leave placeholders/stubs, weaken assertions, or bypass production behavior. Derive tests from the selected record's behavioral acceptance criteria and cover relevant failure/edge cases. Record exact commands and semantic outcomes.
6. Mark tasks complete only after verification. Task status values are exact: `pending`, `in_progress`, `complete`, or `blocked`; write `- Status: complete`, never `done`. Never prune, renumber, replace, or recycle tasks during the active maintenance cycle. Keep the front status active until all tasks complete.
7. Do not change intake fields of the bug. External URLs and workflow status are mutable and excluded from its fingerprint.
8. Move the selected `in_progress` bug from `.factory/bugs/open.md` to `.factory/bugs/closed.md` only in the final task, using `bug-ledger.py close` with non-empty resolution and verification. Do not close any other bug.
9. The final task is exactly **Maintenance verification and documentation audit**. It runs boilerplate verification and the configured `[verification].maintenance_command`, audits docs and acceptance evidence, then sets front status complete. The configured argv must name an executable project verifier.
10. Capture why a regression test or operational constraint matters in nearby documentation. If a durable build/run/validation fact is learned, update `AGENTS.md` but keep it concise and free of progress history.
11. Commit one coherent checkpoint and replace rather than append to the recovery scratchpad with one short current handoff. Use one level-one title plus concise bullets for outcome, exact verification, commit, and next task; stay below 80 lines and 8 KiB, omit detailed change history, and never include the reserved completion token.

## Completion protocol

`MAINTENANCE_COMPLETE` is a reserved protocol token. Never write it into the maintenance plan, scratchpad, an event topic or payload, a summary, or explanatory prose. If any non-final task remains, finish the normal event and exit without emitting the token. Only after the final gate passes, publish any required `factory.maintenance.implement` summary without that token, close the event tag, and then output exactly `MAINTENANCE_COMPLETE` as the final non-empty line outside every event tag. Do not add a colon, punctuation, Markdown fencing, or text after it. If the final gate rejects completion, the supervisor resumes the active cycle; repair the reported deficiency rather than repeating the completion request.
