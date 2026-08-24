# Factory Operational Guide

Keep this file brief. Progress/evidence live in
`.factory/artifacts/implementation-plan.md`; the crash handoff lives in
`.ralph/agent/scratchpad.md` (legacy, excluded from model context).

## Sources of truth

- Canonical specification: `docs/FACTORY-LOOP-SPEC.md` (`.factory/config.toml` `[project].spec`); `docs/SPEC.md` is the adopting placeholder, never planned.
- Canonical plan and sole task ledger: `.factory/artifacts/implementation-plan.md`
  (schema `factory-plan/v1`, parser `.factory/loop/plan_parser.py`).
- Single mutable control-state file: `.factory-state/factory-loop.json` (ignored).
  Capabilities: `.factory/environment.toml`; defects: `.factory/bugs/open.md`/`closed.md`.
- Work only on the configured development branch (`.factory/config.toml`);
  the human promotes to `main`. No worktrees; never edit the committed spec.

## Build

This boilerplate has no product build; the factory control plane is
stdlib-only Python under `.factory/loop/`; the gates below verify it.

## Immediate validation

```bash
./scripts/verify-boilerplate.sh              # complete generic factory gate
./scripts/check-docs-sync.sh                 # documentation sync gate
./.factory/tests/test-factory-adversarial.sh # hidden §22 conformance driver
python3 .factory/tests/test-factory-plan-parser.py
python3 .factory/tests/test-factory-selector.py
python3 .factory/tests/test-factory-state.py
./scripts/run-factory-runners.py             # exact-commit runner gate
./scripts/check-factory-runner-evidence.py   # (needs provisioning)
```

Run gates serially. Do not dismiss an unrelated failure as pre-existing:
determine its cause, fix it when safe, or append a remediation task.

## Run and inspect

```bash
# Finite campaign: planning -> implementation -> verification -> audit
python3 .factory/loop/campaign.py --root "$PWD" run \
  --campaign-id primary-YYYYMMDD-HHMMSS --rounds 3 \
  --branch boilerplate-develop --provider ollama --model <model> \
  --backend <absolute-model-backend>
python3 .factory/loop/campaign.py --root "$PWD" show
python3 .factory/loop/state.py --root "$PWD" show
python3 .factory/loop/migration.py --root "$PWD" status  # Ralph migration
```

A campaign always terminates with one of six outcomes: `success`, `findings`, `blocked`, `failed`, `infrastructure_failure`, `interrupted`.

## Code and test patterns

- Search before assuming behavior is missing; reuse the `.factory/loop/`
  stdlib authorities instead of ad-hoc copies.
- Production acceptance uses the real fresh-process launch, Landlock
  confinement, state, pinned-Git, receipt, and gate paths; synthetic seams
  are hidden test fixtures only, never acceptance evidence.
- A runner declaration is not evidence; accept only exact-commit runner
  receipts validated by the runner evidence checker. Findings reach the
  next developer only through a planner revision of the canonical plan.
- No placeholders, stubs, weakened assertions, unexplained skips, or
  test-only bypasses. Golden updates explicit and reviewed; docs record why.

## Git commit boundary

- Ordinary checkpoints never commit scratchpad-only state; the one trusted
  exception is a single final-handoff commit per durable cycle, authorized by
  a one-shot lifecycle token. Enforced by `scripts/git-commit-guard.sh`
  (hooks via `scripts/install-git-commit-guard.sh`), `scripts/pi-cli-shims/git`,
  and `scripts/pi-ralph-emit-extension.mjs`.
- Direct `git commit` of metadata-only state is rejected; substantive commits
  that also carry the scratchpad are allowed. Do not bypass hooks
  (`--no-verify`, `core.hooksPath`, `GIT_CONFIG_*`); only `git commit` may
  create commits from the model command boundary.

## Acceptance evidence (BUG-0016 machinery)

- Conformance rows are machine-checked from `.factory/artifacts/conformance.json`
  (`ralph-conformance/v1`) by `scripts/validate-conformance.py`; free-text
  matrix cells cannot prove acceptance; `blocked`/`partial`/`not_applicable`
  rows fail completion unless re-classified with evidence.
- Capability contracts live in `.factory/capability-contracts.json` checked by
  `scripts/check-capability-contracts.py`; `scripts/check-capability-evidence.py`
  needs a fresh exact-commit receipt with the probe executed, not skipped.
- Coordinator commands are recorded by `scripts/machine-receipt.py --tag
  <tag> -- <argv...>` under `.factory-state/audit-receipts/`; audits cite
  `[receipt: ...]`/`[manifest: ...]`, PASS requires exit 0, and any BLOCKED
  evidence forces `result: findings`.
- Pixel/offscreen checks are not real visual acceptance, private/session
  services are not the real system service, a synthetic producer is not the
  target consumer, and declaring evidence is not evidence; the conformance
  sidecar is the only authority for verified claims.
