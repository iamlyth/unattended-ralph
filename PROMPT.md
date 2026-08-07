# Implementation Loop

Implement the committed specification in `docs/SPEC.md` by following `IMPLEMENTATION_PLAN.md` on the autonomous `develop` branch.

## Non-negotiable operating model

1. `docs/SPEC.md` is the source of truth. Never change it during implementation. A spec change requires a separate human commit and a new planning loop.
2. Validate plan freshness with `scripts/check-plan-freshness.sh` before work.
3. Select exactly one highest-priority `pending` task whose dependencies are complete. Mark it `in_progress`.
4. Keep the primary context focused. Adaptively launch read-only subagents in parallel for source study, research, security, tests, and documentation. Respect `factory.toml` ceilings.
5. You are the only repository writer. Subagents report findings and must not edit, write, commit, or run mutating commands.
6. Implement only the selected task. Fix root causes rather than masking symptoms; do not weaken assertions, bypass production paths, or substitute direct callbacks for user-visible interaction. Run its acceptance checks and relevant regression tests.
7. Update nearby documentation as behavior changes. Record concise evidence in the plan, including the exact command, result, production path exercised, and semantic outcome—not merely compilation or event consumption.
8. Mark the task `complete` only with objective evidence; otherwise mark it `blocked` with the exact reason. Never prune, renumber, replace, or recycle planned tasks during an active implementation cycle; the final gate requires the complete cycle ledger.
9. If implementation or final verification discovers an unplanned specification, interaction, quality, or regression gap, do not declare completion. Preserve every existing task, append a uniquely numbered `pending` remediation task, add it to the dependencies of the final audit, set that audit back to `pending`, and continue in later fresh iterations.
10. Commit a coherent checkpoint to `develop`, then update `.ralph/agent/scratchpad.md` with a short handoff and exit. One task per fresh context.

## Adaptive subagent use

- `planner-scout`: locate relevant code/spec areas before editing.
- `researcher`: resolve technical uncertainty.
- `reviewer`: inspect correctness and tests after implementation.
- `security-reviewer`: use for trust boundaries, credentials, parsing, process execution, or network behavior.
- `docs-reviewer`: use for public behavior, setup, configuration, operations, and final documentation audit.

Use only the agents needed for the task. Launch independent read-only investigations together. The global model-request ceiling is configured in `.pi/subagents.json` and `factory.toml`.

## Final documentation and verification gate

The final audit task may run only after every implementation and appended remediation task is complete. It must apply the canonical specification's full definition of done (plus the factory defaults below), not infer completion from task count or prior green tests. It must:

- update the plan's specification conformance matrix so every normative requirement is `verified` with source and executable evidence; no `partial`, `missing`, or `ambiguous` classification may remain;
- execute the complete interaction/API/CLI inventory through normal production dispatch, proving semantic outcomes rather than only handler return values, object existence, output shape, or no-crash behavior;
- run installed end-to-end workflows, required degraded/error-state acceptance, clean-build regression, packaging, and project verification—not only tests changed by the cycle;
- validate bug ledgers and resolve every open defect that contradicts the release scope; only an explicit human-approved specification/release decision can defer one;
- launch parallel read-only correctness, security, test-quality, and documentation reviews designed to find false-positive tests and production-path gaps;
- update `README.md` and `docs/` so commands, configuration, recovery, limitations, and behavior are accurate;
- run `scripts/verify-boilerplate.sh` plus project-specific verification added by the implementation plan;
- set the plan front-matter `status: complete` only after every task and gate passes;
- ensure the Git tree is clean after its documentation commit.

A final audit that finds a gap is successful discovery, not completion: apply operating-model step 9 and keep looping. There is no minimum iteration count, but there is also no early completion based on apparent progress. Only objective satisfaction of the canonical definition of done permits `LOOP_COMPLETE`. If Ralph reaches its configured iteration/runtime limit or an external session limit first, leave the plan `active` or `blocked`, write an exact recovery handoff, and do not emit `LOOP_COMPLETE`.
