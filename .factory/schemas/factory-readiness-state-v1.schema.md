# `factory-readiness-state/v1` sidecar schema

Status: committed contract for the round-zero readiness extension
(EXT-READINESS-01). The canonical control-state file
`.factory-state/factory-loop.json` carries *exactly* the §11 field set of
`factory-state/v1` (STATE-01). The round-zero readiness applicability, campaign
nonce, attempt/cursor/status, accepted commit/tree/environment, the five
separate result digests, and the terminal outcome are campaign-bound,
coordinator-owned extension data that MUST NOT appear as fields, phases, or
outcomes in canonical state. They live in this strict sidecar document
`.factory-state/readiness.json` under the ignored `.factory-state/` namespace,
implemented by `.factory/loop/sidecars.py`.

Readiness runs *before* canonical state initialization, so canonical
`current_phase` is never `readiness` and `current_round` starts at 1 per §11.
A readiness-only campaign publishes only the separate readiness result schema
(`factory-readiness-result/v2`) and never initializes canonical state.

## 1. Contract

- The file is a single JSON object carrying **exactly** the field set of
  section 2 — no wall-clock timestamp, model prose, evidence claim, or copy of
  the plan is accepted. Parsing rejects both extra and missing fields.
- Every parse re-validates every structural invariant; a sidecar that fails
  any invariant is a tamper (`SidecarError`) and never reaches a cursor
  transition or a write.
- All file I/O is atomic, no-follow, and ownership/mode/link-count checked
  through the established dirfd authority `.factory/loop/factory_state_io.py`
  (`state_dir`/`read_bytes`/`atomic_write_json`), exactly like the canonical
  state file. Loading re-validates the recorded `campaign_id` against the
  expected campaign binding.
- The sidecar is evidence of coordinator-owned extension bookkeeping only; it
  is never a field, phase, or outcome in canonical state.

## 2. Field set

The object carries exactly these three keys, each exactly once:

| Field | Type / invariant | Mutable by |
|-------|------------------|------------|
| `schema` | exactly the constant `"factory-readiness-state/v1"` | write-once |
| `campaign_id` | non-empty campaign identifier (`^[a-z0-9](?:[a-z0-9._-]{0,62}[a-z0-9])?$`) | write-once |
| `readiness` | mandatory exact bounded object (section 2.1) | round-zero coordinator only |

### 2.1 `readiness` object

The `readiness` object carries exactly these keys, each exactly once:

| Field | Type / invariant |
|-------|------------------|
| `required` | boolean; production load requires `true` |
| `nonce` | 64-character lowercase SHA-256 campaign nonce (nonzero when `required`) |
| `attempt` | non-negative integer; monotonic |
| `cursor` | integer in `0..6`; monotonic |
| `status` | one of `not_required, pending, acquiring, complete, findings, human_blocked, infrastructure_failure` |
| `accepted_commit` | 40-character lowercase Git object ID |
| `tree` | 40-character lowercase Git tree object ID |
| `environment_blob` | 40-character lowercase Git blob object ID |
| `specification_sha256` | 64-character lowercase SHA-256 |
| `plan_sha256` | 64-character lowercase SHA-256 |
| `conformance_sha256` | 64-character lowercase SHA-256 |
| `policy_sha256` | 64-character lowercase SHA-256 |
| `contracts_sha256` | 64-character lowercase SHA-256 |
| `install_manifest_sha256` | 64-character lowercase SHA-256 |
| `command_authority_sha256` | 64-character lowercase SHA-256 |
| `human_authority_sha256` | 64-character lowercase SHA-256 |
| `trust_authority_sha256` | 64-character lowercase SHA-256 |
| `aggregate_sha256` | 64-character lowercase SHA-256 (runner aggregate result digest) |
| `capability_result_sha256` | 64-character lowercase SHA-256 |
| `core_result_sha256` | 64-character lowercase SHA-256 |
| `conformance_result_sha256` | 64-character lowercase SHA-256 |
| `human_result_sha256` | 64-character lowercase SHA-256 |
| `result_sha256` | 64-character lowercase SHA-256 (combined readiness result digest) |
| `terminal_outcome` | one of `not_required, pending, pass, findings, blocked, infrastructure_failure` |

The five separate result digests are `aggregate_sha256`,
`capability_result_sha256`, `core_result_sha256`, `conformance_result_sha256`,
and `human_result_sha256`; `result_sha256` is the combined readiness result
digest. A completed readiness (`status == "complete"`) must record
`terminal_outcome == "pass"`, `cursor == 6`, and every required result digest
nonzero.

### 2.2 Cursor and binding invariants

`attempt` and `cursor` are monotonic and never rewind. The immutable
applicability/binding fields (`required`, `nonce`, `accepted_commit`, `tree`,
`environment_blob`, and the authority/`_sha256` bindings) may never change on
a transition. A required readiness must carry a campaign nonce; a completed
readiness must carry every nonzero bound result digest.

## 3. Trusted CLI

The sidecar is written only by the trusted control-plane module
`.factory/loop/sidecars.py` (`empty_readiness`, `update_readiness`,
`read_readiness`, `write_readiness`); it is never invoked by a model role. A
fail-closed path raises `SidecarError`/`SidecarBindingError`/
`SidecarTransitionError` and never publishes a partial or forged sidecar.
