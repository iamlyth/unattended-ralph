# Project Operational Guide

Keep this file brief and operational. Progress, task status, and verification evidence belong in `.factory/artifacts/implementation-plan.md`; only the current recovery handoff belongs in `.ralph/agent/scratchpad.md`.

## Sources of truth

- Product contract: `docs/SPEC.md`
- Declared factory capabilities: `.factory/environment.toml` (never invent undeclared runners)
- Active work and evidence: `.factory/artifacts/implementation-plan.md`
- Ordinary defects: `.factory/bugs/open.md` and `.factory/bugs/closed.md`
- Work only on the configured development branch (`.factory/config.toml` `development_branch`); the human promotes to `main`.
- Do not use Git worktrees or edit the committed specification during implementation.

## Build

Replace these placeholders with deterministic project commands before the first planning cycle:

```bash
[configure command]
[build command]
```

Document required runtimes, dependency environments, and separate build variants here.

## Immediate validation

```bash
# Smallest relevant test/backpressure command
[target test command]

# Complete clean project gate
[full verification command]

# Ralph/factory policy and orchestration
./scripts/verify-boilerplate.sh

# Exact-commit declared runner gate and evidence check
./scripts/run-factory-runners.py
./scripts/check-factory-runner-evidence.py

# Finite fresh-plan/implementation/audit campaign
./scripts/ralph-campaign.sh --rounds 3
```

Run shared build/test commands serially. Do not dismiss an unrelated failure as pre-existing: determine its cause, fix it when safe, or append a remediation task with evidence.

## Run and inspect

```bash
[headless/dry-run command]
[installed production smoke command]
```

List exact artifact paths and inspection commands needed for immediate loopback.

## Code and test patterns

- Search before assuming behavior is missing; reuse established shared utilities instead of ad-hoc copies.
- Production acceptance must use real initialization, dispatch, backend, persistence, output, and shutdown paths.
- Tests must assert semantic outcomes; direct callback/unit tests are supplemental when they bypass production routing.
- Scope temporary resources to the test and clean them on success and failure.
- Do not leave placeholders, stubs, weakened assertions, unexplained skips, or test-only production bypasses.
- Golden/baseline updates, if used, must be explicit and reviewed rather than automatic test side effects.
- A runner declaration is not evidence; accept only exact-commit receipts validated by the runner evidence checker.
- Documentation records why a constraint or test matters, not iteration history.

## Git commit boundary

- Ordinary checkpoints never commit scratchpad-only state; the one trusted exception is a single final-handoff commit per durable cycle, authorized by a one-shot lifecycle token. The boundary is enforced at Git level by `scripts/git-commit-guard.sh` (installed as `pre-commit`, `prepare-commit-msg`, `pre-merge-commit`, `applypatch-msg`, `pre-applypatch`, `commit-msg` hooks by `scripts/install-git-commit-guard.sh`, which launchers run on every launch) and at the command layer by `scripts/pi-cli-shims/git` and `scripts/pi-ralph-emit-extension.mjs`.
- Direct `git commit` of metadata-only state is rejected; substantive commits that also carry the scratchpad are allowed. Do not bypass hooks (`--no-verify`, `core.hooksPath`, `GIT_CONFIG_*`); only `git commit` may create commits from the model command boundary.

## Acceptance evidence (BUG-0016 machinery)

- Conformance rows are machine-checked from `.factory/artifacts/conformance.json` (`ralph-conformance/v1`) by `scripts/validate-conformance.py`; free-text matrix cells cannot prove acceptance, and `blocked`/`partial`/`not_applicable` rows fail implementation completion unless re-classified with evidence.
- Capability contracts live in `.factory/capability-contracts.json` (declared capabilities only; never claim undeclared/unavailable ones) and are checked by `scripts/check-capability-contracts.py`; `scripts/check-capability-evidence.py` requires a fresh exact-commit receipt with the probe executed, not skipped, and no simulated markers.
- Coordinator commands are recorded by `scripts/machine-receipt.py --tag <tag> -- <argv...>` under `.factory-state/audit-receipts/`; audits must cite `[receipt: ...]`/`[manifest: ...]`, PASS requires exit 0, and any BLOCKED evidence forces `result: findings`.
- Pixel/offscreen checks are not real visual acceptance, private/session-scoped services are not the real system service, a synthetic producer is not the target consumer, and declaring evidence is not evidence. The conformance sidecar is the only authority for verified claims.
