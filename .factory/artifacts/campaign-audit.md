---
schema: ralph-campaign-audit/v1
round: 1
audit_base_commit: 02a1de45239e9124a79d59c51659fa832900170d
plan_commit: 02a1de45239e9124a79d59c51659fa832900170d
plan_blob: 5c91e353866806099d9155ba08e112db628f11d9
environment_blob: 9b2d9614e0ec4843c41650a04fbce2d994fd2630
runner_evidence_sha256: 0e431408bd9fdf646ec94cd7e1f39e2acb7d838209329da6331125558b0388b6
result: findings
---

# Campaign Audit — Task 20 Final Documentation and Specification Audit

## Audit identity

- Role: independent final auditor (Task 20), fresh process, distinct static
  audit prompt, no developer/tester conversation.
- Repository: canonical repository root, branch `boilerplate-develop`.
- Audit commit (HEAD): `02a1de45239e9124a79d59c51659fa832900170d` (tree
  `f0cc85ae0c50117d1145e949eb7988c5fa8f7cbf`), working tree clean
  (`git status --porcelain` empty), branch `boilerplate-develop`.
- Plan binding: `base_commit 2d6a4fd1bd70866f7ff47c2128c8f7e850c40760`,
  spec `docs/FACTORY-LOOP-SPEC.md` commit `2d6a4fd1bd70866f7ff47c2128c8f7e850c40760`
  blob `ca2334abf18a6557eb09c9baeb4b03bb3df523a4`.
- History integrity: 34 commits between plan base and HEAD, zero merge
  commits, zero Git replacement objects, Git commit-guard hooks installed
  (pre-commit, prepare-commit-msg, pre-merge-commit, applypatch-msg,
  commit-msg, pre-applypatch); changes since base confined to
  `.factory/`, `docs/`, `scripts/`, `tests/`, `README.md`, `AGENTS.md`.
- Audit objective (round 1, campaign objectives `[(1-1) mod 5]`):
  `runner-capability` — runner capability falsification (receipt categories
  `runner-evidence`, `project-verify`). Registry objective (round 1,
  `[(1-1) mod 8]`): `AUD-01` Fresh-context isolation. The runner-capability
  receipt categories cannot be covered at this audit base because no
  runner capability is declared and no runner evidence or coordinator
  receipts exist (see Finding 4).

## Evidence reviewed

- Specification: `docs/FACTORY-LOOP-SPEC.md` at commit
  `2d6a4fd1bd70866f7ff47c2128c8f7e850c40760`, blob
  `ca2334abf18a6557eb09c9baeb4b03bb3df523a4`; all 24 §24 normative
  requirement IDs present in the committed registry
  (`.factory/schemas/factory-plan-v1.requirements.json`, schema
  `factory-plan/v1/requirements`) and in the requirement policy
  (`.factory/requirement-policy.json`, schema
  `ralph-requirement-policy/v1`); plan front matter, conformance matrix
  (§24), and interaction acceptance inventory (input, semantic,
  production, evidence) read and cross-checked.
- Production paths: `.factory/loop/` (state.py, lock.py, launch.py,
  campaign.py, plan_parser.py, selector.py, usage.py, findings.py,
  evidence.py, migration.py, footprint.py, workspace_confinement.py,
  confinement.py, confine_launcher.py, promptset.py,
  audit_objectives.py, redaction.py, usage_fetch.py, gitutil.py,
  `__init__.py`), `.factory/schemas/`, `.factory/prompts/`,
  `.factory/tests/`, `scripts/` (verify-boilerplate.sh, final-gate.sh,
  validate-conformance.py, validate-implementation-plan.py,
  validate-blocked-facts.py, validate-campaign-audit.py,
  check-docs-sync.sh, check-capability-contracts.py,
  check-capability-evidence.py, check-factory-environment.py,
  check-factory-runner-evidence.py, run-factory-runners.py,
  check-audit-receipts.py, check-campaign-objectives.py,
  check-installed-functional-evidence.sh, visual-audit-gate.sh,
  machine-receipt.py, git-commit-guard.sh, pi2-secure-exec.py,
  credential-guard.py, ollama-usage-guard.sh), `tests/`, `docs/`
  (README.md, docs/FACTORY.md, docs/OPERATIONS.md, docs/BUG_WORKFLOW.md),
  `AGENTS.md`, `.factory/artifacts/` (implementation-plan.md,
  conformance.json, blocked-facts.json, campaign-audit.md),
  `.factory/capability-contracts.json`, `.factory/environment.toml`,
  `.factory/signer-trust.json`, `.factory/campaign-objectives.json`,
  `.factory/campaign-receipt-policy.json`, `.factory/golden-policy.json`,
  `.factory/verifier-acceptance.json`, `.factory/ralph-freeze`.
- Gates executed (prose, not receipts): every gate listed below was run at
  the audit commit in this fresh audit process (serial, deterministic) and
  its exit status observed. No exact-commit machine receipt or signed
  runner manifest exists at the audit base (`.factory-state/audit-receipts/`
  is empty and zero runner capabilities are evidenced), so no evidence line
  can carry a genuine `[receipt: …]`/`[manifest: …]` reference; citing
  fabricated references would violate §19. Every receipt-bound evidence line
  below is therefore marked BLOCKED for the receipt-bound tier — the
  observed exit status is recorded in parentheses, and receipt publication
  is the coordinator's trusted step (Findings 2 and 4). Any BLOCKED
  evidence forces result `findings`.
- Executable evidence: `scripts/validate-implementation-plan.py planning .factory/artifacts/implementation-plan.md` BLOCKED (observed exit 0)
- Executable evidence: `scripts/validate-conformance.py planning` BLOCKED (observed exit 0, 24 requirements)
- Executable evidence: `scripts/validate-conformance.py complete` BLOCKED (observed exit 1: AUTH-01 partial)
- Executable evidence: `scripts/validate-blocked-facts.py planning .factory/artifacts/blocked-facts.json` BLOCKED (observed exit 0, 23 facts)
- Executable evidence: `scripts/check-docs-sync.sh` BLOCKED (observed exit 0)
- Executable evidence: `scripts/check-capability-contracts.py` BLOCKED (observed exit 0, 0 contracts)
- Executable evidence: `scripts/check-capability-evidence.py` BLOCKED (observed exit 0, no declared capabilities)
- Executable evidence: `scripts/check-factory-environment.py .factory/environment.toml` BLOCKED (observed exit 0, 0 tools, 0 runners)
- Executable evidence: `scripts/check-golden-policy.py` BLOCKED (observed exit 0)
- Executable evidence: `scripts/check-factory-runner-evidence.py --print-digest` BLOCKED (observed exit 0, digest `0e431408…` stable over repeated runs)
- Executable evidence: `scripts/check-factory-runner-evidence.py --print-capabilities` BLOCKED (observed exit 0, zero evidenced capabilities)
- Executable evidence: `scripts/run-factory-runners.py` BLOCKED (observed exit 0, 0 runner(s) passed)
- Executable evidence: `.factory/tests/test-factory-adversarial.sh` BLOCKED (observed exit 0, 27/27 §22 cases)
- Executable evidence: `.factory/tests/test-factory-footprint.sh` BLOCKED (observed exit 0)
- Executable evidence: `scripts/check-generic-leakage.sh` BLOCKED (observed exit 0)
- Executable evidence: `scripts/verify-boilerplate.sh` BLOCKED (observed exit 0, full gate)
- Executable evidence: `scripts/visual-audit-gate.sh` BLOCKED (observed exit 0, visual audit disabled)
- Executable evidence: `scripts/check-installed-functional-evidence.sh` BLOCKED (observed exit 1: tested commit `61356a0…` not an ancestor of HEAD)
- Executable evidence: `scripts/check-audit-receipts.py` BLOCKED (observed exit 1: zero receipts at base)
- Executable evidence: `scripts/check-campaign-objectives.py` BLOCKED (observed exit 1: round-1 categories uncovered)
- Executable evidence: `scripts/check-plan-freshness.sh` BLOCKED (observed exit 0)
- Executable evidence: `.factory/loop/plan_parser.py roundtrip` BLOCKED (observed exit 0, byte-exact round-trip; serialize byte-identical to committed plan)
- Executable evidence: `.factory/loop/selector.py select .factory/artifacts/implementation-plan.md` BLOCKED (observed exit 0, deterministic task 20)
- Environment limits: `.factory/environment.toml` declares zero tools and
  zero runners (no `[[runners]]` entries); `.factory/signer-trust.json`
  has `enabled=false` and empty `public_keys` (no signer provisioned);
  `.factory/capability-contracts.json` declares `capabilities: []`;
  `.factory/campaign-receipt-policy.json` binds the round-1 objective
  categories `runner-evidence` (`./scripts/run-factory-runners.py`,
  allow_manifest=true) and `project-verify`
  (`./scripts/verify-boilerplate.sh`, allow_manifest=false) — none can be
  satisfied without a provisioned runner or coordinator receipts.

## Requirement verdicts (all 24 §24 rows, conformance sidecar at audit commit)

| ID | Required tier | Sidecar classification | Verdict | Evidence at audit commit |
|----|--------------|------------------------|---------|--------------------------|
| AUTH-01 | installed | partial | NOT VERIFIED | private_integration fixture evidence only; no installed receipt |
| CTX-01 | installed | partial | NOT VERIFIED | private_integration fixture evidence only |
| CTX-02 | installed | partial | NOT VERIFIED | private_integration fixture evidence only |
| ROLE-01 | installed | partial | NOT VERIFIED | private_integration fixture evidence only |
| PLAN-01 | unit | partial | NOT VERIFIED | parser fixture evidence; byte-exact round-trip confirmed |
| TASK-01 | unit | partial | NOT VERIFIED | selector fixture evidence; deterministic selection confirmed |
| TASK-02 | installed | partial | NOT VERIFIED | private_integration fixture evidence only |
| QUOTA-01 | private_integration | partial | NOT VERIFIED | synthetic decision-table tests; no campaign-wired receipt |
| QUOTA-02 | private_integration | partial | NOT VERIFIED | synthetic cookie tests; no campaign-wired receipt |
| STATE-01 | unit | partial | NOT VERIFIED | fixture evidence only; `.factory-state/factory-loop.json` absent |
| LOCK-01 | private_integration | partial | NOT VERIFIED | fixture evidence only; no campaign-level concurrency receipt |
| PROC-01 | private_integration | partial | NOT VERIFIED | fixture evidence only |
| GIT-01 | installed | partial | NOT VERIFIED | guarded-commit tests; no installed receipt |
| PHASE-01 | private_integration | partial | NOT VERIFIED | campaign fixtures; live campaign not run at this base |
| COMPLETE-01 | unit | partial | NOT VERIFIED | fixture evidence only |
| FIND-01 | private_integration | partial | NOT VERIFIED | fixture evidence only |
| CRED-01 | private_integration | partial | NOT VERIFIED | credential tests; no campaign receipt |
| EVID-01 | installed | partial | NOT VERIFIED | receipt/manifest machinery fixture-proven; zero receipts at base |
| VIS-01 | installed | partial | NOT VERIFIED | visual machinery fixture-proven; gate disabled, no evidence |
| RUNNER-01 | real_system | blocked | BLOCKED (external) | no hardware-runner capability declared/provisioned; no signer; FACT-020 open |
| HIDE-01 | installed | partial | NOT VERIFIED | footprint suite passes; no installed-tier receipt |
| MIG-01 | installed | partial | NOT VERIFIED | private_integration migration tests pass; no installed receipt |
| TEST-01 | installed | partial | NOT VERIFIED | 27-case adversarial suite passes; no installed receipt |
| ACCEPT-01 | installed | missing | NOT VERIFIED | no acceptance evidence; boilerplate acceptance criteria unmet |

Summary: 0 verified, 22 partial, 1 blocked (external capability), 1 missing.

## Findings

## Finding 1: No §24 conformance row is verified; acceptance and campaign success are impossible

- Severity: Critical (blocking).
- Requirement: ACCEPT-01, TEST-01, AUTH-01, CTX-01, CTX-02, ROLE-01,
  PLAN-01, TASK-01, TASK-02, QUOTA-01, QUOTA-02, STATE-01, LOCK-01,
  PROC-01, GIT-01, PHASE-01, COMPLETE-01, FIND-01, CRED-01, EVID-01,
  VIS-01, HIDE-01, MIG-01.
- Production evidence: `.factory/artifacts/conformance.json` classifies
  22 rows `partial`, RUNNER-01 `blocked`, ACCEPT-01 `missing`, zero
  `verified`; `scripts/validate-conformance.py complete` exits 1 (AUTH-01
  partial); `scripts/validate-blocked-facts.py complete` exits 1 (FACT-001
  open); plan matrix agrees with the sidecar for every row.
- Required remediation: complete every row to `verified` with exact-commit
  evidence at the required tier (receipts at the audit base, RUNNER-01 via
  a provisioned signed hardware runner or explicit external resolution),
  then re-run `scripts/validate-conformance.py complete` and
  `scripts/validate-blocked-facts.py complete` to exit 0 before any
  success claim. The campaign must remain non-final (result findings)
  until then.

## Finding 2: No exact-commit installed-tier receipts exist at the audit base

- Severity: High.
- Requirement: EVID-01, AUTH-01, CTX-01, CTX-02, ROLE-01, TASK-02, GIT-01,
  VIS-01, HIDE-01, MIG-01, TEST-01, ACCEPT-01.
- Production evidence: `.factory-state/audit-receipts/` is empty (0
  receipts); every conformance row's `receipts` array is empty; all
  installed-required rows carry only `private_integration` evidence tier;
  `scripts/check-audit-receipts.py` exits 1.
- Required remediation: run the trusted receipt wrapper
  (`scripts/machine-receipt.py`) for each installed-tier gate at the audit
  base under the campaign coordinator binding, publish receipts under
  `.factory-state/audit-receipts/`, and cite `[receipt: …]` references so
  `scripts/check-audit-receipts.py` exits 0. A model assertion or
  fixture transcript is not a receipt.

## Finding 3: RUNNER-01 real_system evidence is blocked on an undeclared, unprovisioned hardware runner

- Severity: High (external authority; does not by itself make the audit
  blocked because software/evidence findings exist and take precedence).
- Requirement: RUNNER-01.
- Production evidence: `.factory/environment.toml` declares zero
  `[[runners]]`; `.factory/signer-trust.json` has `enabled=false` and
  `public_keys: []`; `.factory/capability-contracts.json` declares no
  capabilities; `scripts/check-factory-runner-evidence.py --print-capabilities`
  returns empty (exit 0); `scripts/run-factory-runners.py` reports 0
  runner(s); FACT-020 is open in `.factory/artifacts/blocked-facts.json`.
- Required remediation: a human must provision the hardware runner,
  declare it in `.factory/environment.toml`, provision the signer trust,
  run the runner against the audit base, and have the signed manifest
  accepted by `scripts/check-factory-runner-evidence.py` before RUNNER-01
  can move from `blocked` to `verified`. Never elevate
  private/synthetic evidence to real_system.

## Finding 4: The round-1 campaign audit objective cannot be satisfied by any receipt category

- Severity: High.
- Requirement: EVID-01, TEST-01, ACCEPT-01.
- Production evidence: `.factory/campaign-objectives.json` round 1 selects
  `runner-capability` (categories `runner-evidence`, `project-verify`);
  `.factory/campaign-receipt-policy.json` allows a runner manifest for
  `runner-evidence` only via an accepted exact-commit signed record in the
  runner-evidence aggregate, which cannot exist with zero declared
  runners; `scripts/check-campaign-objectives.py --round 1 --base
  02a1de4` exits 1. With no coordinator receipts and no manifests, no
  audit round can pass in this environment.
- Required remediation: either provision the runner capability and mint
  the round's objective receipts, or ensure the campaign's audit round is
  assigned an objective whose receipt categories can be covered by real
  evidence at the audit base; the audit must report findings while the
  categories are uncovered. Replaying generic suites across rounds is
  explicitly rejected by the policy.

## Finding 5: The minimal control-state authority is not instantiated at the audit base

- Severity: High.
- Requirement: STATE-01, PHASE-01, GIT-01.
- Production evidence: `.factory-state/factory-loop.json` does not exist;
  `.factory/loop/state.py --root "$PWD" show` reports "lifecycle marker is
  missing: factory-loop.json"; no live campaign has been driven by
  `.factory/loop/campaign.py` at this base, so the §11 transition table,
  write-once bindings, and tamper detection have fixture-only (not
  campaign) evidence.
- Required remediation: run the trusted campaign
  (`python3 .factory/loop/campaign.py --root "$PWD" run …`) through at
  least one full round so the single control-state file exists and binds
  the round; re-validate STATE-01/PHASE-01 with the live state file and
  its digest ledger, keeping the state file outside Git.

## Finding 6: Installed-functional evidence is stale and foreign to the boilerplate audit base

- Severity: High.
- Requirement: EVID-01, TEST-01.
- Production evidence: `.factory-state/installed-functional-evidence.env`
  records `commit=61356a0af6d26a8207ccfc2fc25c86f27c982c4b` (`factory:
  root-owned runner-receipt signer…`), which exists only in the foreign adopting-product environment
  `develop` and is not an ancestor of the boilerplate audit base;
  `scripts/check-installed-functional-evidence.sh` exits 1 ("tested commit
  is not an ancestor of HEAD").
- Required remediation: produce fresh `test_installed_functional` PASS
  evidence at a boilerplate commit that is an ancestor of the audit base
  (or at the audit base itself), then re-run
  `scripts/check-installed-functional-evidence.sh` to exit 0 with zero
  skips. Legacy product evidence must never be imported as boilerplate
  installed-tier acceptance.

## Finding 7: Plan prose and table disagree on MIG-01/TEST-01 classification

- Severity: Low (documentation).
- Requirement: TEST-01, MIG-01, ACCEPT-01.
- Production evidence: plan prose (`.factory/artifacts/implementation-plan.md`
  §"Specification conformance matrix" preamble) states MIG-01, TEST-01,
  and ACCEPT-01 are `missing`, while the plan table and the conformance
  sidecar classify MIG-01 and TEST-01 as `partial` (only ACCEPT-01 is
  `missing`). Machine gates (planning mode) pass because the sidecar is
  authoritative, but the prose is inaccurate.
- Required remediation: correct the preamble prose to state MIG-01 and
  TEST-01 are `partial` and ACCEPT-01 is `missing`, matching the table and
  sidecar, and re-run `scripts/check-docs-sync.sh`.

## Observations (non-blocking)

- The runner-evidence checker reported a transient "aggregate
  Git/environment binding is stale" on its first invocation, then returned
  the identical digest `0e431408…` (exit 0) on five subsequent runs and
  the aggregate mtime was unchanged. No persistent defect was
  reproducible; a concurrent writer during receipt regeneration is the
  suspected cause. Remediation: the coordinator should publish the
  runner-evidence aggregate only under the writer lock and re-run the
  checker to confirm a stable digest before the audit.
- `scripts/verify-boilerplate.sh`, the 27-case adversarial suite, the
  footprint suite, the migration suite, docs sync, capability checks, and
  plan/conformance planning gates all pass at the audit commit; the
  machinery is coherent and the failure to pass is entirely the absence of
  campaign-level, installed-tier, and real_system evidence, not a
  mechanical regression.
- No open or closed bug-ledger entries exist
  (`.factory/bugs/open.md` and `.factory/bugs/closed.md` both `[]`); 23
  blocked facts (FACT-001…FACT-023) are open and unresolved.

## Overall result

- Overall verdict: `findings`.
- Rationale: 0 of 24 §24 requirements are `verified`; the conformance
  completion gate and the blocked-facts completion gate both fail; no
  exact-commit installed-tier receipts, no live control-state file, and
  no runner evidence exist at the audit base; RUNNER-01 is blocked on an
  external capability. Because software/evidence findings remain, §14
  precedence makes this `findings`, not `blocked`.
- The campaign MUST NOT claim success. The next planner revision must
  convert these findings into plan tasks (evidence receipt publication,
  live campaign state instantiation, fresh installed-functional evidence,
  runner provisioning or explicit external resolution, and documentation
  prose fix) before any re-audit.
