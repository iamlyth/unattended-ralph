# Factory Operational Guide
Progress/evidence live in `.factory/artifacts/implementation-plan.md`.
## Sources of truth

- Canonical specification: `docs/FACTORY-LOOP-SPEC.md` (`.factory/config.toml` `[project].spec`); `docs/SPEC.md` is the adopting placeholder, never planned.
- Canonical plan and sole task ledger: `.factory/artifacts/implementation-plan.md`
  (schema `factory-plan/v1`, parser `.factory/loop/plan_parser.py`).
- Single mutable control-state file: `.factory-state/factory-loop.json` (ignored).
  Canonical state-v1 is extended explicitly by `factory-state/v2` under
  `EXT-STATE-V2-01`; it is not claimed as the exact §11 field set.
  Readiness policy: `.factory/readiness-policy.json`; capabilities: `.factory/environment.toml`; defects: `.factory/bugs/open.md`/`closed.md`.
- Work only on the configured development branch (`.factory/config.toml`);
  the human promotes to `main`. No worktrees; never edit the committed spec.

## Build
This boilerplate has no product build; the factory control plane is
stdlib-only Python under `.factory/loop/`; the gates below verify it.

## Immediate validation
```bash
./.factory/tools/verify-boilerplate.sh              # complete generic factory gate
./.factory/tools/check-docs-sync.sh                 # documentation sync gate
./.factory/tests/test-factory-adversarial.sh # hidden §22 conformance driver
python3 .factory/tests/test-factory-plan-parser.py
python3 .factory/tests/test-factory-selector.py
python3 .factory/tests/test-factory-state.py
python3 .factory/tests/test-factory-readiness.py
./.factory/tests/legacy/test-factory-runner.sh               # rootless runner/adversarial fixtures
./.factory/tools/run-factory-runners.py             # campaign-scoped external acquisition only
./.factory/tools/check-factory-runner-evidence.py   # needs explicit campaign/readiness namespace
```

Run gates serially. Do not dismiss an unrelated failure as pre-existing:
determine its cause, fix it when safe, or append a remediation task.

## Run and inspect

```bash
# Finite production campaign. The neutral policy is intentionally BLOCKED;
# an adopting project must commit and provision its generic readiness authorities.
"${INSTALL_PREFIX:?verified install}/.factory/bin/factory-campaign" --root "$PWD" run \
  --campaign-id "${CAMPAIGN_ID:?unique}" --rounds "${ROUNDS:?finite}" --branch boilerplate-develop \
  --provider "${PI_PROVIDER:?real}" --model "${PI_MODEL:?model}" --backend "${PI2_BACKEND:?pi2}" \
  --accepted-commit "${ACCEPTED_COMMIT:?clean HEAD}" --install-manifest "${INSTALL_MANIFEST:?verified}" \
  --campaign-timeout "${CAMPAIGN_TIMEOUT:-21600}" --verification-command ./.factory/tools/verify-boilerplate.sh \
  --acceptance-command ./.factory/tools/verify-boilerplate.sh
python3 .factory/loop/state.py --root "$PWD" show
python3 .factory/loop/migration.py --root "$PWD" status  # Ralph migration
```

A campaign always terminates with one of six outcomes: `success`, `findings`, `blocked`, `failed`, `infrastructure_failure`, `interrupted`. Verification may also report `software_verified_external_acceptance_blocked` (software fully verified while external release acceptance remains blocked); it advances to the independent audit and can never produce `success`.

## Code and test patterns

- Search before assuming behavior is missing; reuse the `.factory/loop/`
  stdlib authorities instead of ad-hoc copies.
- Production acceptance uses the real fresh-process launch, Landlock
  confinement, state, pinned-Git, receipt, and gate paths; synthetic seams
  are hidden test fixtures only, never acceptance evidence.
- A runner declaration is not evidence; accept only v3 signed exact-commit
  receipts from an externally enrolled root authority, validated under the
  exact campaign/readiness namespace. The shipped fixture is never evidence.
  Findings reach the
  next developer only through a planner revision of the canonical plan.
- No placeholders, stubs, weakened assertions, unexplained skips, or
  test-only bypasses. Golden updates explicit and reviewed; docs record why.

## Git commit boundary

- Checkpoints require substantive tracked changes and are enforced by
  `.factory/tools/git-commit-guard.sh` (hooks via
  `.factory/tools/install-git-commit-guard.sh`) and the hidden Git shim.
- Direct metadata-only commits are rejected. Do not bypass hooks
  (`--no-verify`, `core.hooksPath`, `GIT_CONFIG_*`); only `git commit` may
  create commits from the model command boundary.

## Acceptance evidence (BUG-0016 machinery)

- Conformance rows are machine-checked from `.factory/artifacts/conformance.json`
  (`ralph-conformance/v1`) by `.factory/tools/validate-conformance.py`; free-text
  matrix cells cannot prove acceptance; `blocked`/`partial`/`not_applicable`
  rows fail completion unless re-classified with evidence.
- Capability contracts live in `.factory/capability-contracts.json` checked by
  `.factory/tools/check-capability-contracts.py`; `.factory/tools/check-capability-evidence.py`
  needs a fresh exact-commit receipt with the probe executed, not skipped.
- Coordinator commands are recorded by `.factory/tools/machine-receipt.py --tag
  <tag> -- <argv...>` under `.factory-state/audit-receipts/`; audits cite
  `[receipt: ...]`/`[manifest: ...]`, PASS requires exit 0, and any BLOCKED
  evidence forces `result: findings`.
- Pixel/offscreen checks are not real visual acceptance, private/session
  services are not the real system service, a synthetic producer is not the
  target consumer, and declaring evidence is not evidence; the conformance
  sidecar is the only authority for verified claims.
