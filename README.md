# Ralph Software Factory Boilerplate

A reusable, single-writer implementation of Geoffrey Huntley's Ralph Wiggum
development technique using a fresh-context Python control plane: planner,
developer, tester, and auditor run as separate fresh, confined model
processes with static digest-bound prompts; exactly one task is selected
deterministically from the canonical plan per attempt; one minimal
control-state file drives the lifecycle; real Landlock confinement jails each
model process; Git history and commits stay in the trusted orchestrator; and
campaigns are finite with exact terminal outcomes.

> **Migration status:** Ralph Orchestrator launches are frozen
> (`.factory/ralph-freeze`). The hidden Python control plane under
> `.factory/loop/` implements the canonical
> [Factory Loop Specification](docs/FACTORY-LOOP-SPEC.md) using
> `factory-plan/v1` and the single `factory-state/v2` control-state file; it
> does not import Ralph tasks, memories, events, completion tokens,
> scratchpads, or persisted context summaries. Visible `scripts/ralph-*`
> commands are frozen deprecated compatibility forwarders and are not
> required by the loop. The `FACTORY_RALPH_FREEZE_OVERRIDE=1` escape exists
> only to recover an already in-flight legacy cycle; see
> [Factory Operations](docs/OPERATIONS.md).

`docs/SPEC.md` remains the adopting-product placeholder and is never planned
against; this cycle plans `docs/FACTORY-LOOP-SPEC.md` only.

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
- Exactly one minimal mutable control-state file
  `.factory-state/factory-loop.json` (schema `factory-state/v2`, ignored)
  also carries mandatory round-zero readiness and the exact hook configuration/commit binding, ordered result
  digest chain, and durable start/completion cursor.
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
- The secure Pi wrapper (`scripts/pi2-secure-exec.py`) and a committed model
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
  --verification-command ./scripts/verify-boilerplate.sh \
  --acceptance-command ./scripts/verify-boilerplate.sh
```

Production accepts only the authenticated, immutable Pi2 launch contract;
`synthetic` and `--role-driver` are explicit fixture-only seams. Before model
execution, `.factory/readiness-policy.json` selects only fixed internal gate
adapters and binds the accepted commit/tree, configuration, environment,
specification, plan, contracts, install manifest, and external trust. The
boilerplate enrolls no production authority, so real-provider production is
explicitly blocked until an adopting project commits and provisions one.
Readiness-only terminates as `readiness_complete`, never campaign `success`;
each real role consumes a fresh campaign-only one-use authorization.

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

## Verify the boilerplate

```bash
./scripts/verify-boilerplate.sh
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
