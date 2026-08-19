# Provider-neutral bug maintenance

## Canonical state and external references

`.factory/bugs/open.md` and `.factory/bugs/closed.md` are the portable canonical workflow state. Each contains one JSON array under schema `ralph-bug-ledger/v1`. A bug may reference a GitHub issue, a Forgejo issue, both, or neither. External synchronization is manual: issue state never overrides the local ledgers.

External references must be HTTPS issue URLs with a parsed hostname and valid optional port, without user information, query strings, or fragments. GitHub and root-hosted Forgejo links use `https://HOST/OWNER/REPO/issues/N` (the legacy singular `issue` route remains accepted). A Forgejo installation hosted below a URL path must expose/copy a canonical issue URL in that expected owner/repository route shape. Never put PATs, passwords, cookies, or other credentials in the repository or in issue URLs. This workflow makes no network calls and uses no provider API clients.

## Intake and states

IDs are allocated monotonically as `BUG-0001+`. Open ticket states are `open`, `triaged`, `planned`, `in_progress`, and `blocked`; `closed` records live only in `.factory/bugs/closed.md`. Severity is `low`, `medium`, `high`, or `critical`.

A defect restores behavior already required by the committed specification. If `contract_change` is true, expected behavior needs a product decision, or the fix would edit `docs/SPEC.md`, stop and use the human specification workflow: approve and commit the spec, then run the ordinary planning/implementation lifecycle. Maintenance must never decide or silently change the product contract.

Provider templates are byte-identical at `.github/ISSUE_TEMPLATE/bug_report.md` and `.forgejo/ISSUE_TEMPLATE/bug_report.md`. Copy issue details into the local ledger and retain manual URLs as references.

## Ledger commands

```bash
./scripts/bug-ledger.py validate
./scripts/bug-ledger.py list [--status triaged]
./scripts/bug-ledger.py show BUG-0001
./scripts/bug-ledger.py fingerprint BUG-0001
./scripts/bug-ledger.py add --title "Failure" --severity high \
  --reproduction "steps" --expected "result" --actual "failure" \
  --acceptance "regression passes" \
  --external github=https://github.com/ORG/REPO/issues/123 \
  --external forgejo=https://forge.example/ORG/REPO/issues/456
./scripts/bug-ledger.py link BUG-0001 github https://github.com/ORG/REPO/issues/123
./scripts/bug-ledger.py unlink BUG-0001 github
./scripts/bug-ledger.py set-status BUG-0001 triaged
./scripts/bug-ledger.py set-status BUG-0001 planned
./scripts/bug-ledger.py set-status BUG-0001 in_progress
./scripts/bug-ledger.py close BUG-0001 \
  --resolution "implemented correction" --verification "test command and result"
./scripts/bug-ledger.py recover
```

Each ledger-file replacement is individually atomic and deterministic; moving a record between two ledgers is not transactionally atomic. All read-modify-write commands serialize on ignored `.bug-ledger.lock`. If closure is interrupted after writing the closed destination, normal validation reports the duplicate and `recover` removes the open duplicate only when immutable fingerprints match and the closed record has valid evidence. Intake fingerprints cover immutable problem/acceptance fields, not workflow status or external URLs. Links cannot duplicate a provider, closed records are immutable, and closure requires `in_progress` plus resolution and verification evidence.

## One-bug maintenance cycle

Triage an ordinary defect, then start from a clean tree:

```bash
./scripts/ralph-maintenance-plan.sh BUG-0001
# Review the committed .factory/artifacts/maintenance-plan.md
./scripts/ralph-maintenance-run.sh
```

The selected ID, selected-cycle base commit, and loop mode are volatile local state in ignored `.factory-state/`. A fresh cycle atomically replaces the previous maintenance plan and scratchpad with a minimal selected-bug skeleton; old plans remain only in Git history and the closed ledger, while `--resume` preserves the current draft. The planning gate requires every new task to be `pending`. Planning accepts `triaged` (or `planned` only when resuming), commits the strict plan, and commits the ledger-only `planned` transition before final completion attestation. Draft checkpoints may be incomplete; scratchpad-only drafts stay uncommitted for recovery. Completion binds immutable metadata to a clean unchanged HEAD, and no tracked commit follows that attestation. Maintenance run accepts only `planned` or `in_progress`; its first implementation task transitions `planned` to `in_progress`. The plan ends with **Maintenance verification and documentation audit**. Only that completed final task may close the selected record.

Maintenance fails closed unless `[verification].maintenance_command` is a non-empty argv array whose first element exists and is executable. The generic boilerplate intentionally does not include `scripts/verify-project.sh`; each project must supply that executable before maintenance can complete. The argv is executed directly, without shell evaluation.

Headless and continuation options are `--no-tui` and `--resume`. Recovery modes are:

```bash
./scripts/ralph-recover.sh --mode maintenance-planning
./scripts/ralph-recover.sh --mode maintenance
```

Recovery retains the same selected bug and uses the factory lock, checkpoint hooks, clean-tree checks, and quota waiting. If either maintenance worker requests completion before its strict final gate passes, an attempt-bound one-shot rejection marker authorizes the supervisor to repair volatile Ralph state and continue that same lifecycle with `--continue`; arbitrary failures, stale markers, and mismatched modes do not trigger retries. If freshness reports changed intake/specification or a contract change, do not bypass it; return to human triage/specification workflow.
