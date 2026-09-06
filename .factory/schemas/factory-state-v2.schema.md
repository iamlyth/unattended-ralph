# `factory-state/v2` control-state schema (legacy migration input)

Status: **legacy pre-migration format only.** This document records the former
`factory-state/v2` extension format that carried the pre-round hook and
round-zero readiness fields inline in the control-state file. It is no longer
the canonical STATE-01 contract and is never produced by the trusted harness.
The canonical mutable control-state authority is now `factory-state/v1`
(`.factory/schemas/factory-state-v1.schema.md`) with exactly the §11 field
set; the pre-round hook and readiness extension data live in strict
campaign-bound sidecars (`factory-pre-round-hook-state/v1` and
`factory-readiness-state/v1`, `.factory/loop/sidecars.py`), never as fields,
phases, or outcomes in canonical state.

A legacy `factory-state/v2` document is accepted **only** as a deterministic
migration input by the explicit offline helper
(`.factory/loop/state.py` `migrate_offline_state`), which strips the extension
fields into the strict sidecars and returns the canonical `factory-state/v1`
§11 field set. Production loading never calls this helper and never
synthesizes a readiness or pre-round authority at the old version; a legacy v2
document can never satisfy a real campaign's expected exact-commit hook
binding.

## 1. Contract

- The file is a single JSON object carrying the former §11 field set plus the
  inline pre-round hook and readiness extension fields of section 2. Runtime
  parsing performs no synthesis. Legacy `factory-state/v2` input is accepted
  only by the explicit offline/fixture migration helper and can never migrate
  into production readiness.
- Every parse re-validates every structural invariant; a model that fails any
  invariant is a tamper (`StateTamperError`) and never reaches a transition,
  a digest, or a write.
- All file I/O is atomic, no-follow, and ownership/mode/link-count checked
  through the established dirfd authority `.factory/tools/factory_state_io.py`
  (`state_dir`/`read_bytes`/`atomic_write_json`). The file is published
  through a mode-0600 temporary inode and `linkat`, so a raced pathname is
  never silently replaced, and every open re-validates the recorded
  `repository_identity` against the canonical root directory descriptor.
- A digest is recorded before every untrusted phase in the append-only
  evidence ledger `.factory-state/state-digest-ledger.jsonl` (evidence, never
  orchestration state) and re-validated after the phase; any same-UID
  semantic mutation not produced by the trusted transition fails closed.
- The state file is the *only* mutable lifecycle file; the ledger is
  append-only evidence.

## 2. Field set (legacy)

The legacy object carried exactly these twenty-three keys, each exactly once,
with the former §11 type and invariant. This is the historical pre-migration
field set; the canonical `factory-state/v1` field set is the seventeen §11
fields of `.factory/schemas/factory-state-v1.schema.md`.

| Field | Type / invariant | Mutable by |
|-------|------------------|------------|
| `schema` | exactly the constant `"factory-state/v2"` | write-once |
| `repository_identity` | `dev:inode` hex pair (`^[0-9a-f]+:[0-9a-f]+$`) of the canonical root directory | write-once |
| `branch` | non-empty Git branch name | write-once |
| `campaign_id` | non-empty campaign identifier | write-once |
| `rounds_requested` | positive integer (campaign round budget) | write-once |
| `current_round` | positive integer, `1 <= current_round <= rounds_requested`; monotonic, 1-based | only `audit --nonfinal` increments it |
| `current_phase` | one of `planning, implementation, verification, audit, success, findings, blocked, failed, interrupted, infrastructure_failure` | only §4 transitions |
| `specification_digest` | 64-character lowercase SHA-256 hex | write-once |
| `plan_digest` | 64-character lowercase SHA-256 hex; binds a completed planning phase (see §2.1 for its derivation) | rebind only on `planning -> implementation` |
| `role_prompt_digests` | non-empty JSON object mapping each role name to a 64-hex SHA-256 digest | write-once |
| `audit_objectives_digest` | 64-character lowercase SHA-256 hex | write-once |
| `pre_round_hook_configuration_digest` | 64-character lowercase SHA-256 over the exact ordered registry, fixed implementation blobs, and bound commit | write-once |
| `pre_round_hook_commit` | 40-character exact campaign-start commit from which every registry/implementation blob is descriptor-anchored | write-once |
| `pre_round_hook_results_digest` | 64-character lowercase SHA-256 digest chain over canonical typed per-round results; initialized to zero | only `complete_pre_round_hooks` |
| `pre_round_hook_started_round` | non-negative monotonic round cursor written before hook execution | only `begin_pre_round_hooks` |
| `pre_round_hook_completed_round` | non-negative monotonic cursor, never above started; later phases require completion for the current round | only `complete_pre_round_hooks` |
| `phase_base_commit` | 40-character lowercase Git object ID; binds a completed planning phase | rebind only on `planning -> implementation` |
| `selected_task_id` | positive integer or `null`; present only during `implementation` with `attempt_number >= 1` | only `begin_attempt` |
| `attempt_number` | non-negative integer; monotonic within the current task, reset to zero only on a trusted task/phase transition | only `begin_attempt` / phase transitions |
| `phase_started_at_monotonic` | positive integer (`time.monotonic_ns`); a zeroed `now=0` epoch marker is rejected as tamper (Task 19 S3) | only phase transitions |
| `attempt_started_at_monotonic` | non-negative integer; positive exactly while an attempt is active and `>= phase_started_at_monotonic` (an attempt can never precede the phase that owns it, S9); the inverse holds too — when no attempt is active (`attempt_number == 0`) the marker must be zero (S9) | only `begin_attempt` / phase transitions |
| `last_outcome` | `null` only during round-zero readiness or fresh `planning`, otherwise exactly one trusted outcome of the owning phase; mismatch fails closed | trusted harness only |
| `readiness` | mandatory exact bounded object with immutable applicability, campaign nonce, attempt/cursor/status, accepted commit/tree/environment, command/human/trust authorities, five separate result digests, and terminal outcome; production load requires `required=true` | round-zero coordinator only |

### 2.1 Write-once bindings

`schema`, `repository_identity`, `branch`, `campaign_id`, `rounds_requested`,
`specification_digest`, `role_prompt_digests`, `audit_objectives_digest`, and
`pre_round_hook_configuration_digest` and `pre_round_hook_commit` are bound by `init_state` and can never change on a
transition. `plan_digest` and `phase_base_commit` bind a completed planning
round and are write-once until the next trusted `planning -> implementation`
transition.

`plan_digest` (Task 19 S8) is the SHA-256 of the exact bytes of the committed
`factory-plan/v1` plan document — the canonical
`.factory/artifacts/implementation-plan.md` at the bound `phase_base_commit`
— as accepted by the deterministic plan parser. It is bound only by the
trusted `planning -> implementation` edge and is write-once until the next
such edge of the next round, so a later plan cannot silently change the plan a
round was planned against.

`load_state` additionally fails closed when the recorded identity
does not match the canonical root directory or when any expected campaign
binding differs. The state-file and private-directory owner checks compare
real `stat` metadata against an internal expected owner UID (default: the
current user), so the exact owner-rejection branch is always exercisable with
real stat metadata and a wrong expected UID, with no `chown` required (S7).

## 3. Transition table and outcomes

`last_outcome` is exactly one of the trusted control-plane outcome enum
`OUTCOMES`; model completion tokens are never control protocol. The §11 edge
set is enforced exactly as follows (source phase, trusted outcome) → target
phase; a *terminal* target accepts no further transition:

```text
readiness      pass            -> planning round 1
readiness      findings        -> findings          (terminal round 0)
readiness      blocked         -> blocked           (terminal round 0)
readiness      infrastructure_failure -> infrastructure_failure (terminal round 0)
planning       planned         -> implementation
planning       failed          -> failed            (terminal)
planning       interrupted     -> interrupted       (terminal)
planning       infrastructure_failure -> infrastructure_failure (terminal)
implementation task_completed  -> verification
implementation work_exhausted  -> verification
implementation blocked         -> verification
implementation task_failed     -> verification
implementation interrupted     -> interrupted       (terminal)
verification   pass            -> audit
verification   findings        -> audit
verification   blocked         -> audit
verification   infrastructure_failure -> infrastructure_failure (terminal)
audit          pass            -> planning (next round) | success      (final)
audit          findings        -> planning (next round) | findings     (final)
audit          blocked         -> planning (next round) | blocked      (final)
audit          interrupted     -> interrupted        (terminal)
audit          infrastructure_failure -> infrastructure_failure (terminal)
```

An interrupted audit and an untrusted audit (`infrastructure_failure`) are
the two terminal fail-closed closes that have no nonfinal `audit ->
planning(next round)` edge: they always end the campaign (Task 9 review
B1), never advance the round, and are persisted in the authoritative
control state so a later run refuses to re-execute them.

Round finality resolves at the `audit` phase from `rounds_requested`:
`current_round < rounds_requested` advances to the next round's `planning`
and increments `current_round` (the only counter increment); the final round
ends the campaign in the terminal state named by the outcome. Phase never
moves backward within a round.

Retry outcomes that keep the same phase and attempt budget are recorded
through `record_retry` without claiming a transition:

- `planning`: `interrupted` (a step interrupted before a valid checkpoint,
  §13.1);
- `implementation`: `task_progress`, `task_failed`, `interrupted` while the
  same task's bounded attempt budget remains (§13.2).

Attempts begin through `begin_attempt` (implementation phase only):
`attempt_number` increments while the same `selected_task_id` is selected and
restarts at 1 on a trusted task transition; `attempt_started_at_monotonic`
restarts for timeout recovery and must never precede
`phase_started_at_monotonic` (S9). `advance` resets the task/attempt fields to
their inactive form on every phase change. Every persisted `last_outcome` is
a §13 outcome of its owning phase (S9); a `null` `last_outcome` is valid only
in a fresh planning phase.

## 4. Determinism and digest

`state_digest` is the SHA-256 of the canonical JSON encoding
(`json.dumps(to_dict(), sort_keys=True, separators=(",", ":")).encode()`),
the same bytes `atomic_write_json` writes without the trailing newline, so
the digest is a deterministic function of the model and any semantic mutation
(forged field, rewound counter, changed binding, changed phase, drifted
task/attempt) changes it. File-metadata mutations (mode, owner, link count,
pathname identity) are caught by the no-follow dirfd checks before or during
any open.

The harness records the digest before each untrusted phase and re-validates
afterwards:

- `record_phase_digest(root, tag)` appends one newline-terminated JSON line
  `{"tag": ..., "digest": ...}` to the append-only ledger (mode 0600, bounded
  1 MiB, single link, same-UID); a repeated tag, unsafe tag, malformed line,
  or substituted file fails closed. The duplicate-tag check and the append
  happen inside one validated directory scope bound to the exact checked
  inode: a ledger swapped to a different inode between the check and the
  append fails closed, and a concurrent writer that lands the *same* tag on
  the same inode in that window is detected on the re-read before the append
  (Task 19 L5), so at most one same-tag writer can ever succeed and the
  ledger is never left with a repeated tag.
- `verify_phase_digest(root, tag)` reopens/re-validates the state, recomputes
  the digest, and fails closed on any mismatch or missing/malformed record.

## 5. Secure file I/O

- `.factory-state` is a private (0700) owned directory; a world-accessible,
  foreign-owned, or symlinked directory fails closed. The owner checks
  compare real `stat` metadata against an internal expected owner UID
  (default: the current user), so the exact owner-rejection branch is always
  exercisable with real stat metadata and a wrong expected UID, with no
  `chown` required (S7).
- The state file is a regular same-UID file, mode 0600, link count 1, size
  bounded (`STATE_FILE_MAX`); reads hold one descriptor and re-validate the
  (dev, inode) and size before/after reading.
- Writes publish through a mode-0600 temporary inode and `linkat`; a raced
  pathname is never silently replaced; the previous validated inode is
  quarantined on failure, never destroyed silently.
- `init` (Task 19 S1) runs deterministic crash-window recovery first, refuses
  a prior campaign binding recorded in the digest ledger, and then publishes
  atomically with **no-replace** semantics (`atomic_write_json(...,
  no_replace=True)`): the existence check and the `linkat` publication happen
  inside one locked directory scope, so a fresh campaign can never clobber
  existing state, an existing binding, or a raced pathname.
- Crash-window/orphan recovery (Task 19 S2) is deterministic: `recover_state`
  removes validated orphaned temporaries (`.{marker}.{32-hex}`) and
  quarantines (`.{marker}.quarantine-{32-hex}`) of the established atomic
  writer, and when the canonical file is absent restores the single last
  validated quarantine atomically with no-replace — never creating a second
  authority. Recovery only ever deletes a file it can prove is an exact
  mode-0600, same-UID, single-link regular marker (`_validate_orphan`), fails
  closed on ambiguous (multiple quarantines) or foreign artifacts, and
  preserves any unknown name untouched. Operator entrypoint: `recover`.
  Recovery additionally:
  - reports `clean` *only* when the private directory is truly absent; an
    existing symlinked, filed, wrong-mode, or foreign-owned directory fails
    closed instead of being treated as clean;
  - reports a distinct `existing-empty` outcome when the private directory is
    present, valid, and completely empty (for example one left behind by an
    owner probe) — never confused with a truly absent (`clean`) directory,
    and recovery creates or deletes nothing for it;
  - when a digest ledger exists, requires the recovered state's digest to
    match the *latest* recorded ledger entry before restoring a quarantine,
    so a quarantined state tampered after it was recorded — or one that only
    matches an earlier (superseded) ledger entry — fails closed with the
    quarantine preserved. A present-but-zero-byte ledger is ambiguous torn
    evidence of an interrupted first append and likewise blocks the restore
    (hardening L4) rather than being silently treated as “no evidence”;
  - after linking the quarantine into the canonical name, re-validates the
    canonical state in place (tolerating the transient two-link window)
    *before* deleting the quarantine, so a raced, substituted, or forged
    canonical fails closed with the quarantine preserved for inspection, and
    verifies the canonical and quarantine are still one inode before deleting
    the quarantine so an inode swap of the quarantine itself fails closed.
- The owner-tamper probe (Task 19 S7) never skips: `owner_tamper_gate`
  performs a real `chown(2)` on a probe file and declares kernel-level
  availability. A privileged run genuinely exercises the owner tamper
  fail-closed path; an unprivileged run reports the owner check unavailable
  with a fail-closed reason and never falsely claims owner-tamper coverage.

## 6. Defect classes and fixtures

Every static defect class below is rejected with `StateTamperError` and has
an exact fixture under `.factory/tests/fixtures/state-*.json`; unsafe-I/O and
ledger tamper classes have runtime probes plus seeded content fixtures
(`state-unsafe-*.json`, `state-ledger-*.jsonl`). The committed corpus is
inventoried verbatim by the hidden conformance suite.

| Defect class | Fixture |
| ------------ | ------- |
| wrong schema constant | `state-schema-wrong.json` |
| missing `schema` | `state-schema-missing.json` |
| extra (non-§11) field | `state-field-extra.json` |
| missing §11 field | `state-field-missing.json` |
| empty control-state object | `state-empty-object.json` |
| malformed repository identity | `state-identity-malformed.json` |
| empty repository identity | `state-identity-empty.json` |
| empty branch / campaign | `state-branch-empty.json`, `state-campaign-empty.json` |
| `rounds_requested` zero/negative/boolean | `state-rounds-requested-zero.json`, `state-rounds-requested-negative.json`, `state-rounds-requested-bool.json` |
| `current_round` zero/boolean/exceeds budget | `state-current-round-zero.json`, `state-current-round-bool.json`, `state-current-round-exceeds-requested.json` |
| unknown phase | `state-phase-unknown.json` |
| negative attempt or monotonic marker | `state-attempt-number-negative.json`, `state-phase-monotonic-negative.json`, `state-attempt-monotonic-negative.json` |
| zeroed epoch monotonic marker (S3) | `state-phase-monotonic-zero.json` |
| attempt preceding its owning phase (S9) | `state-attempt-before-phase.json` |
| attempt marker without an active attempt (S9 inverse: no attempt => marker zero) | `state-attempt-marker-without-attempt.json` |
| boolean attempt number | `state-attempt-number-bool.json` |
| terminal state without matching outcome | `state-terminal-outcome-mismatch.json`, `state-terminal-outcome-null.json` |
| task without begun attempt | `state-task-without-attempt.json` |
| attempt without selected task | `state-attempt-without-task.json` |
| task/attempt outside implementation | `state-task-outside-implementation.json` |
| non-positive task id | `state-task-id-zero.json`, `state-task-id-bool.json` |
| active attempt without timestamp | `state-attempt-without-timestamp.json` |
| untrusted `last_outcome` | `state-outcome-unknown.json`, `state-outcome-number.json` |
| phase/outcome mismatch (S9) | `state-outcome-phase-mismatch.json` |
| invalid digest shape / role digests | `state-spec-digest-invalid.json`, `state-plan-digest-invalid.json`, `state-audit-digest-invalid.json`, `state-role-digest-invalid.json`, `state-role-digest-empty-role.json`, `state-role-digests-empty.json`, `state-role-digests-not-object.json`, `state-digest-uppercase.json` |
| invalid / uppercase phase base commit | `state-phase-base-commit-invalid.json`, `state-phase-base-commit-uppercase.json` |
| unsafe I/O content | `state-unsafe-not-json.json`, `state-unsafe-binary.json`, `state-unsafe-oversized.json` |
| malformed/repeated/bad ledger lines | `state-ledger-malformed.jsonl`, `state-ledger-repeated-tag.jsonl`, `state-ledger-bad-tag.jsonl`, `state-ledger-bad-digest.jsonl`, `state-ledger-extra-key.jsonl`, `state-ledger-empty-line.jsonl` |

Accepted fixtures (must parse with the full invariant set, round-trip through
`to_dict` without semantic loss, and be write/load-able through the real
no-follow I/O in a test-owned temporary repository) are the
`state-valid-*.json` files covering every lifecycle phase, a final-round
audit, a recorded planning retry, and every terminal state.

## 7. Trusted CLI

`python3 .factory/loop/state.py --root ROOT <command>` is the operator
entrypoint (never invoked by a model role): `init`, `show`, `digest`,
`recover`, `advance OUTCOME [--plan-digest ...] [--base-commit ...]`,
`begin-attempt TASK_ID`, `record-retry OUTCOME`, `record-phase-digest TAG`,
`verify-phase-digest TAG`. `recover` runs the deterministic crash-window/orphan
recovery of §5 and prints `recovered status=... removed=... restored=...`. A
fail-closed path prints a single `factory-state: <error>` line to stderr and
exits 1; argparse misuse exits 2.
