# Implementation Loop

Implement the committed specification in `docs/SPEC.md` by following `IMPLEMENTATION_PLAN.md` on the autonomous `develop` branch.

## Non-negotiable operating model

1. `docs/SPEC.md` is the source of truth. Never change it during implementation. A spec change requires a separate human commit and a new planning loop.
2. Validate plan freshness with `scripts/check-plan-freshness.sh` before work.
3. Select exactly one highest-priority `pending` task whose dependencies are complete. Mark it `in_progress`.
4. Keep the primary context focused. Adaptively launch read-only subagents in parallel for source study, research, security, tests, and documentation. Respect `factory.toml` ceilings.
5. You are the only repository writer. Subagents report findings and must not edit, write, commit, or run mutating commands.
6. Implement only the selected task. Run its acceptance checks and relevant regression tests.
7. Update nearby documentation as behavior changes. Record concise evidence in the plan.
8. Mark the task `complete` only with objective evidence; otherwise mark it `blocked` with the exact reason. Never prune, renumber, replace, or recycle planned tasks during an active implementation cycle; the final gate requires the complete cycle ledger.
9. Commit a coherent checkpoint to `develop`, then update `.ralph/agent/scratchpad.md` with a short handoff and exit. One task per fresh context.

## Adaptive subagent use

- `planner-scout`: locate relevant code/spec areas before editing.
- `researcher`: resolve technical uncertainty.
- `reviewer`: inspect correctness and tests after implementation.
- `security-reviewer`: use for trust boundaries, credentials, parsing, process execution, or network behavior.
- `docs-reviewer`: use for public behavior, setup, configuration, operations, and final documentation audit.

Use only the agents needed for the task. Launch independent read-only investigations together. The global model-request ceiling is configured in `.pi/subagents.json` and `factory.toml`.

## Final documentation and verification gate

The final plan task must run only after every implementation task is complete. It must:

- compare the implementation and tests against every applicable specification requirement;
- launch parallel read-only correctness, security, test, and documentation reviews;
- update `README.md` and `docs/` so commands, configuration, recovery, limitations, and behavior are accurate;
- run `scripts/verify-boilerplate.sh` plus project-specific verification added by the implementation plan;
- set the plan front-matter `status: complete` only after every task and gate passes;
- ensure the Git tree is clean after its documentation commit.

Do not claim completion while documentation is stale, tests fail, the specification is unmet, or plan tasks remain. Only after the final audit passes may the final response end with `LOOP_COMPLETE`.
