# Factory Operational Guide

Progress and evidence live in `.factory/artifacts/implementation-plan.md`.

## Sources of truth

- Canonical specification: `docs/SPEC.md` (`.factory/config.toml` `[project].spec`).
  It is the source of truth and is never edited during implementation.
- Canonical plan and sole task ledger: `.factory/artifacts/implementation-plan.md`.
  Bugs, findings, and new work become plan tasks through a planner revision —
  never an independent task queue.
- This guide (`AGENTS.md`): build/test commands and code patterns.
- Control state (`.factory-state/factory-loop.json`) is orchestrator-only and
  never an input to any role.
- Work only on the configured development branch (`.factory/config.toml`);
  the human promotes to `main`. No worktrees.

## Build

The control plane is stdlib-only Python under `.factory/loop/`. The product
build is defined by the adopting project; placeholder commands:

```bash
./scripts/build.sh
```

## Immediate validation

```bash
./scripts/verify-project.sh
python3 .factory/loop/plan_parser.py parse .factory/artifacts/implementation-plan.md
python3 .factory/loop/selector.py select .factory/artifacts/implementation-plan.md
```

Run gates serially. Do not dismiss an unrelated failure as pre-existing:
determine its cause, fix it when safe, or append a remediation task.

## Run and inspect

```bash
python3 .factory/bin/factory-campaign run \
  --campaign-id my-project-v1 --rounds 20 --branch develop \
  --provider ollama --model qwen3.6:27b
python3 .factory/loop/state.py --root "$PWD" show
```

A campaign always terminates with one of six outcomes: `success`, `findings`,
`blocked`, `failed`, `infrastructure_failure`, `interrupted`. Reaching a
budget ceiling is never success.

## Code and test patterns

- Search before assuming behavior is missing; reuse the `.factory/loop/`
  stdlib authorities instead of ad-hoc copies.
- The tester independently runs verification and reports the actual exit code;
  the orchestrator never takes the developer's word that tests passed.
- Runner-dependent tasks run on the declared runner; an unreachable runner
  marks the task `blocked`, never a silent skip or fake pass.
- No placeholders, stubs, weakened assertions, unexplained skips, or
  test-only bypasses.

## Git commit boundary

- The orchestrator is the sole Git writer. Roles never run `git commit`.
- After each implementation+verification cycle the orchestrator commits
  `git add -A && git commit -m "factory: task {N} round {R}"`.
- Do not bypass hooks (`--no-verify`, `core.hooksPath`, `GIT_CONFIG_*`).

## Acceptance evidence

- Tests must actually pass: the tester runs the real verification command and
  records the real exit code. Declaring evidence is not evidence.
- The auditor checks for weakened assertions, removed or skipped tests,
  tautological passes, and verification commands that do not test the
  implementation. Findings become plan tasks in the next planning round.
