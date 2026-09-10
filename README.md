# Ralph Software Factory — Minimal

A minimal, autonomous software factory based on the Ralph Wiggum technique.
The factory builds software from a canonical specification using a fresh-context
loop with deterministic task selection, independent verification, and awareness
of external test runners. It is human-out-of-the-loop: once a campaign starts,
it runs to a terminal outcome without human intervention.

The control plane is stdlib-only Python under `.factory/loop/`. Four static
roles — planner, developer, tester, auditor — each run as a separate fresh CLI
invocation with no memory or session resume. Exactly one task is selected
deterministically from the canonical plan per attempt, and campaigns are finite
with honest terminal outcomes.

## Prerequisites

- Python 3.11+ (the control plane is stdlib-only)
- Git
- curl
- An Ollama server (the model provider)
- SSH access to runners (optional, only if you declare external runners)

## Quick start

```bash
# 1. Write your product spec
$EDITOR docs/SPEC.md

# 2. Declare runners (if any)
$EDITOR .factory/environment.toml

# 3. Set your verification command
$EDITOR .factory/config.toml

# 4. Run a campaign
python3 .factory/bin/factory-campaign run \
  --campaign-id my-project-v1 --rounds 20 --branch develop \
  --provider ollama --model qwen3.6:27b
```

## How it works

Each round runs `planning -> implementation -> verification -> audit`:

1. **Spec** — one canonical specification (`docs/SPEC.md`) is the source of
   truth and is never edited during implementation.
2. **Plan** — the planner creates or revises the canonical plan
   (`.factory/artifacts/implementation-plan.md`), the sole task ledger.
3. **Select** — the trusted selector deterministically picks exactly one
   runnable task per attempt; the model never chooses among tasks.
4. **Implement** — the developer implements only the selected task.
5. **Verify** — the tester independently runs the verification command and
   reports the actual exit code.
6. **Audit** — the auditor performs a read-only audit for weakened assertions,
   skipped tests, or fake passes.
7. **Commit** — the orchestrator (the sole Git writer) commits the checkpoint.
8. **Repeat** — until all tasks complete or a budget is exhausted.

A campaign always terminates with one of six outcomes: `success`, `findings`,
`blocked`, `failed`, `infrastructure_failure`, `interrupted`.

## Runner setup

External runners are declared in `.factory/environment.toml` with an SSH
transport, a working directory, and the capabilities they provide. SSH aliases,
credentials, and provisioning stay outside the repository. An empty runners
section means all verification runs locally. Runner-dependent tasks run on the
declared runner; an unreachable runner marks the task `blocked`, never a silent
skip or fake pass.

## Release

Release is manual. After the campaign reports a terminal outcome, review the
configured development branch and promote to `main`:

```bash
git switch main
git merge --no-ff <development-branch>
git tag vX.Y.Z
```

There is no automatic push, tag, or promotion.
