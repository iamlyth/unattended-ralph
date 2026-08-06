# Maintenance Implementation Loop

Implement exactly the bug selected in `.factory-state/maintenance-bug-id` according to `MAINTENANCE_PLAN.md`.

Non-negotiable rules:

1. Run `scripts/check-maintenance-freshness.sh` before work.
2. Never edit `docs/SPEC.md`. If a contract change or product decision is discovered, mark the current task blocked and require the human specification workflow.
3. On the first implementation task, transition the selected bug from `planned` to `in_progress` with `bug-ledger.py` before changing product code. Then select one ready pending task, mark it in_progress, and implement only that bounded task.
4. You are the sole writer. Subagents are read-only.
5. Test against the selected record's reproduction, expected behavior, and acceptance criteria; record objective evidence.
6. Mark tasks complete only after verification. Keep the front status active until all tasks complete.
7. Do not change intake fields of the bug. External URLs and workflow status are mutable and excluded from its fingerprint.
8. Move the selected `in_progress` bug from `open-bugs.md` to `closed-bugs.md` only in the final task, using `bug-ledger.py close` with non-empty resolution and verification. Do not close any other bug.
9. The final task is exactly **Maintenance verification and documentation audit**. It runs boilerplate verification and the configured `[verification].maintenance_command`, audits docs and acceptance evidence, then sets front status complete. The configured argv must name an executable project verifier.
10. Commit one coherent checkpoint and update the recovery scratchpad each iteration.

Only after the final gate passes may the response end with `MAINTENANCE_COMPLETE`.
