# Ralph Software Factory Boilerplate

A reusable, single-writer implementation of Geoffrey Huntley's Ralph Wiggum
development technique using a fresh-context Python control plane: planner,
developer, tester, and auditor run as separate fresh, confined model
processes with static digest-bound prompts; exactly one task is selected
deterministically from the canonical plan per attempt; one minimal
control-state file drives the lifecycle; real Landlock confinement jails each
model process; Git history and commits stay in the trusted orchestrator; and
campaigns are finite with exact terminal outcomes.

> **Current path:** The hidden Python control plane under `.factory/loop/`
> implements the [Factory Loop Specification](docs/FACTORY-LOOP-SPEC.md).
> Legacy orchestration namespaces and launchers are not part of this checkout
> or installed surface. `factory-state/v1` is the one root canonical STATE-01
> state with exactly the §11 field set; `factory-state/v2` is the legacy
> pre-migration format accepted only by the offline migration helper. The
> readiness and pre-round extension data lives in strict campaign-bound
> sidecars (`factory-pre-round-hook-state/v1`,
> `factory-readiness-state/v1`), never as fields, phases, or outcomes in
> canonical state.

`docs/SPEC.md` remains the adopting-product placeholder and is never planned
against; this cycle plans `docs/FACTORY-LOOP-SPEC.md` only.

## Documentation index

- [Canonical factory loop specification](docs/FACTORY-LOOP-SPEC.md)
- [Factory design and contracts](docs/FACTORY.md)
- [Operations and recovery](docs/OPERATIONS.md)
- [Generic defect workflow](docs/BUG_WORKFLOW.md)
- [Adopting-product specification placeholder](docs/SPEC.md)

## Operating model

- `main` is the human-controlled release branch. The autonomous lifecycle
  runs only on the configured development branch (`.factory/config.toml`
  `development_branch`); the human reviews and manually promotes to `main`.
  There is no automatic push, tag, or promotion.
- One committed canonical specification is the source of truth; Git versions
  it. The specification is never edited during implementation.
- One canonical plan (`.factory/artifacts/implementation-plan.md`, schema
  `factory-plan/v1`) is the sole task ledger. The trusted selector
  (`.factory/loop/selector.py`) deterministically picks exactly one runnable
  task per implementation attempt; the model never chooses among tasks.
- Four static roles — planner, developer, tester, auditor — each start as a
  separate fresh process with a digest-bound static prompt
  (`.factory/prompts/planner.md`, `developer.md`, `tester.md`, `auditor.md`)
  and no resumed session or injected memory. Parallel model launches are
  forbidden.
- Exactly one repository writer: an exclusive `flock` on the already-open
  canonical Git top-level directory descriptor. Git history, staging, and
  commits run only in the trusted orchestrator, never through model tools.
- Before each round's planner, the trusted campaign runs the committed
  `.factory/pre-round-hooks.json` registry exactly once in declared order.
  The registry accepts only fixed internal implementations and initially
  contains the mandatory `branch_guard`; it cannot carry commands or argv.
- Exactly one root canonical mutable control-state file
  `.factory-state/factory-loop.json` (schema `factory-state/v1`, the exact
  §11 field set). Round-zero readiness and pre-round hook extension data are
  coordinator-owned and live in strict campaign-bound sidecars
  (`.factory-state/readiness.json` `factory-readiness-state/v1` and
  `.factory-state/pre-round-hooks.json`
  `factory-pre-round-hook-state/v1`), never as fields, phases, or outcomes in
  canonical state. Readiness is not a phase or outcome; a readiness-only
  campaign publishes the separate `factory-readiness-result/v2` result and
  never initializes canonical state. Extension requirements and truth are in
  `.factory/extension-requirements.json` and
  `.factory/artifacts/extension-conformance.json`.
- Tests, documentation, machine evidence, and an independent audit are
  completion gates. A campaign always terminates with one of six outcomes:
  `success`, `findings`, `blocked`, `failed`, `infrastructure_failure`,
  `interrupted`; it never spins on empty work.

## Prerequisites

- Linux with the Landlock LSM (path-beneath rules), `/proc`, and
  `flock`/`O_NOFOLLOW` primitives. The lifecycle exits fail-closed when any
  required primitive is unavailable.
- Python 3.11+ (the control plane is stdlib-only), Git, curl, flock, and
  optionally ShellCheck.
- OpenSSH (`ssh-keygen -Y verify`) for signed runner evidence.
  `.factory/environment.toml` declares tools and runners without endpoints or
  credentials; SSH aliases, provisioning, and credentials stay outside the
  repository.
- The secure Pi wrapper (`.factory/tools/pi2-secure-exec.py`) and a committed model
  backend. The `ollama` provider additionally requires the retained usage
  guard and a real Landlock confinement proof.
- A clean configured development branch with at least one commit. No Git
  worktrees are used.

## Quick start

Run a finite fresh campaign (planning -> implementation -> verification ->
audit):

```bash
"${INSTALL_PREFIX:?verified install}/.factory/bin/factory-campaign" --root "$PWD" run \
  --campaign-id "${CAMPAIGN_ID:?unique id}" --rounds "${ROUNDS:-3}" \
  --branch boilerplate-develop --provider "${PI_PROVIDER:?real provider}" \
  --model "${PI_MODEL:?model}" --backend "${PI2_BACKEND:?immutable pi2 path}" \
  --accepted-commit "${ACCEPTED_COMMIT:?clean HEAD}" \
  --install-manifest "${INSTALL_MANIFEST:?verified manifest}" \
  --campaign-timeout "${CAMPAIGN_TIMEOUT:-21600}" \
  --verification-command ./.factory/tools/verify-boilerplate.sh \
  --acceptance-command ./.factory/tools/verify-boilerplate.sh
```

Production accepts only the authenticated, immutable Pi2 launch contract;
`synthetic` and `--role-driver` are explicit fixture-only seams. Before model
execution, `.factory/readiness-policy.json` selects only fixed internal gate
adapters and binds the accepted commit/tree, configuration, environment,
specification, plan, contracts, install manifest, and external trust. The
boilerplate enrolls no production authority, so real-provider production is
explicitly blocked until an adopting project commits and provisions one.
Readiness-only terminates as `readiness_complete`, never campaign `success`.
Immediately before every model invocation the trusted parent runs the fixed
§10 `--check`, conditional bounded `--wait`, and final `--check`; any failure
launches no model. Each real role consumes a campaign-lock-bound, durable
one-use authorization.

Inspect the lifecycle:

```bash
python3 .factory/loop/state.py --root "$PWD" show
```

## Plan

The planner is one of the four fresh roles; its output is the canonical
`factory-plan/v1` document parsed by `.factory/loop/plan_parser.py`. The
parser is part of the acceptance boundary: it binds the specification
path/commit/blob, the base commit, unique tasks, dependencies, the
conformance matrix, and the interaction inventory, and round-trips without
semantic loss.

```bash
python3 .factory/loop/plan_parser.py parse .factory/artifacts/implementation-plan.md
python3 .factory/loop/plan_parser.py dump .factory/artifacts/implementation-plan.md
python3 .factory/loop/selector.py select .factory/artifacts/implementation-plan.md
```

## Implement and verify

The developer implements exactly the deterministically selected task and
commits one coherent checkpoint; the trusted campaign then runs verification
(`[verification].campaign_command`), validates installed-functional and
runner evidence, and starts an independent adversarial audit. Findings from
the tester or auditor become structured, digest-bound `factory-findings/v1`
payloads that only the next round's fresh planner may convert into plan
tasks; no receipt, result file, event stream, or memory participates in task
selection.

## Adopt the external runner authority

The boilerplate enrolls no production runner, signer, key, capability, or
host resource. Its neutral readiness policy therefore blocks production by
default. An adopter supplies, reviews, signs, and root-installs an external
`factory-runner-policy/v3` plus a versioned `factory-probe-authority/v1`.
Classes and capabilities have no fixed names or count. Each capability binds
an immutable probe, a pinned semantic-analyzer ID, exact artifacts, executable
pins, and optional default-deny device, D-Bus-proxy, host-fact, and dedicated-
host declarations. `.factory/tests/fixtures/runner-authority/` is harmless
stdlib test material and is never production authority or evidence.

Receipts use `factory-runner-receipt/v3`, aggregates use
`factory-runner-aggregate/v4`, and retained artifacts use
`factory-runner-artifacts/v1`. Evidence is scoped by campaign and readiness
nonce and remains invalid without exact commit/tree/archive/environment,
policy, contracts, authority, trust, and current-revocation bindings.
Provisioning examples and the authenticated bootstrap are documented in
`docs/OPERATIONS.md`.

## Verify the boilerplate

```bash
./.factory/tools/verify-boilerplate.sh
```

The verifier checks shell syntax, ShellCheck when available, TOML/JSON
configuration, read-only agent tools, single-writer settings, the hidden
§22 adversarial conformance suite, plan freshness, branch policy, harness
footprint isolation, and secret tracking.

## Release

After the campaign reports a terminal, review the configured development
branch. Release manually:

```bash
git switch main
git merge --no-ff <development-branch>
git tag vX.Y.Z
```

For the next release, update the same canonical specification in a dedicated
commit, start a new planning phase, and run a new campaign. Git retains prior
specifications and plans.
