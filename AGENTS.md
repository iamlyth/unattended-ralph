# Project Operational Guide

Keep this file brief and operational. Progress, task status, and verification evidence belong in `IMPLEMENTATION_PLAN.md`; only the current recovery handoff belongs in `.ralph/agent/scratchpad.md`.

## Sources of truth

- Product contract: `docs/SPEC.md`
- Active work and evidence: `IMPLEMENTATION_PLAN.md`
- Ordinary defects: `open-bugs.md` and `closed-bugs.md`
- Work only on `develop`; the human promotes to `main`.
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
- Documentation records why a constraint or test matters, not iteration history.
