# Provider-neutral bug maintenance

## Fresh loop defect flow (planner-only)

The fresh Python loop has no maintenance role: its complete role set is
planner, developer, tester, and auditor. Tester and auditor outcomes are
evidence, never runtime tasks. When verification or the independent audit
produces `findings` or `blocked`, the trusted campaign publishes a
write-once `factory-findings-receipt/v1` and delivers a canonical
`factory-findings/v1` payload to the **next round's fresh planner only**;
no receipt, result file, event stream, or memory selects work. The planner
must convert the findings into bounded `pending` tasks in the canonical plan
(`.factory/artifacts/implementation-plan.md`), and developers receive only
the exact selected task bytes from that revised plan.

Ordinary product defects stay in the canonical ledgers
(`.factory/bugs/open.md` / `.factory/bugs/closed.md`) and are triaged by the
human. They enter the loop the same way every other gap does: a planning
revision converts them into canonical plan tasks.

## Canonical state and external references

`.factory/bugs/open.md` and `.factory/bugs/closed.md` are the portable canonical workflow state. Each contains one JSON array under schema `ralph-bug-ledger/v1`. A bug may reference a GitHub issue, a Forgejo issue, both, or neither. External synchronization is manual: issue state never overrides the local ledgers.

External references must be HTTPS issue URLs with a parsed hostname and valid optional port, without user information, query strings, or fragments. GitHub and root-hosted Forgejo links use `https://HOST/OWNER/REPO/issues/N` (the legacy singular `issue` route remains accepted). A Forgejo installation hosted below a URL path must expose/copy a canonical issue URL in that expected owner/repository route shape. Never put PATs, passwords, cookies, or other credentials in the repository or in issue URLs. This workflow makes no network calls and uses no provider API clients.

## Intake and states

IDs are allocated monotonically as `BUG-0001+`. Open ticket states are `open`, `triaged`, `planned`, `in_progress`, and `blocked`; `closed` records live only in `.factory/bugs/closed.md`. Severity is `low`, `medium`, `high`, or `critical`.

A defect restores behavior already required by the committed specification. If `contract_change` is true, expected behavior needs a product decision, or the fix would edit `docs/SPEC.md`, stop and use the human specification workflow: approve and commit the spec, then run the ordinary planning/implementation lifecycle. Maintenance must never decide or silently change the product contract.

Provider templates are byte-identical at `.github/ISSUE_TEMPLATE/bug_report.md` and `.forgejo/ISSUE_TEMPLATE/bug_report.md`. Copy issue details into the local ledger and retain manual URLs as references.

## Ledger commands

```bash
./.factory/tools/bug-ledger.py validate
./.factory/tools/bug-ledger.py list [--status triaged]
./.factory/tools/bug-ledger.py show BUG-0001
./.factory/tools/bug-ledger.py fingerprint BUG-0001
./.factory/tools/bug-ledger.py add --title "Failure" --severity high \
  --reproduction "steps" --expected "result" --actual "failure" \
  --acceptance "regression passes" \
  --external github=https://github.com/ORG/REPO/issues/123 \
  --external forgejo=https://forge.example/ORG/REPO/issues/456
./.factory/tools/bug-ledger.py link BUG-0001 github https://github.com/ORG/REPO/issues/123
./.factory/tools/bug-ledger.py unlink BUG-0001 github
./.factory/tools/bug-ledger.py set-status BUG-0001 triaged
./.factory/tools/bug-ledger.py set-status BUG-0001 planned
./.factory/tools/bug-ledger.py set-status BUG-0001 in_progress
./.factory/tools/bug-ledger.py close BUG-0001 \
  --resolution "implemented correction" --verification "test command and result"
./.factory/tools/bug-ledger.py recover
```

Each ledger-file replacement is individually atomic and deterministic; moving a record between two ledgers is not transactionally atomic. All read-modify-write commands serialize on ignored `.bug-ledger.lock`. If closure is interrupted after writing the closed destination, normal validation reports the duplicate and `recover` removes the open duplicate only when immutable fingerprints match and the closed record has valid evidence. Intake fingerprints cover immutable problem/acceptance fields, not workflow status or external URLs. Links cannot duplicate a provider, closed records are immutable, and closure requires `in_progress` plus resolution and verification evidence.

