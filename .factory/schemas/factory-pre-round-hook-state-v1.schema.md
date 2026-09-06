# `factory-pre-round-hook-state/v1` sidecar schema

Status: committed contract for the pre-round hook extension (EXT-PREROUND-01).
The canonical control-state file `.factory-state/factory-loop.json` carries
*exactly* the §11 field set of `factory-state/v1` (STATE-01). The pre-round
hook configuration digest, bound commit, chained result digest, and the
monotonic started/completed round cursors are campaign-bound,
coordinator-owned extension data that MUST NOT appear as fields, phases, or
outcomes in canonical state. They live in this strict sidecar document
`.factory-state/pre-round-hooks.json` under the ignored `.factory-state/`
namespace, implemented by `.factory/loop/sidecars.py`.

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

The object carries exactly these seven keys, each exactly once:

| Field | Type / invariant | Mutable by |
|-------|------------------|------------|
| `schema` | exactly the constant `"factory-pre-round-hook-state/v1"` | write-once |
| `campaign_id` | non-empty campaign identifier (`^[a-z0-9](?:[a-z0-9._-]{0,62}[a-z0-9])?$`) | write-once |
| `configuration_digest` | 64-character lowercase SHA-256 over the exact ordered registry, fixed implementation blobs, and bound commit | write-once |
| `commit` | 40-character lowercase Git object ID from which every registry/implementation blob is descriptor-anchored | write-once |
| `results_digest` | 64-character lowercase SHA-256 digest chain over canonical typed per-round results; initialized to zero | only `complete_pre_round_hooks` |
| `started_round` | non-negative monotonic round cursor written before hook execution | only `begin_pre_round_hooks` |
| `completed_round` | non-negative monotonic cursor, never above started; later phases require completion for the current round | only `complete_pre_round_hooks` |

### 2.1 Cursor invariants

`started_round` and `completed_round` are monotonic and never rewind.
`completed_round` may never exceed `started_round`, and the cursor may have at
most one ambiguous round (`started_round - completed_round <= 1`). A caller
that observes `started == current_round > completed` must terminate the
campaign for operator review; recovery never re-executes an ambiguous claimed
round. `begin_pre_round_hooks` durably claims the current round before any
hook side effect can occur, and `complete_pre_round_hooks` binds the canonical
ordered result chain before the planner starts.

## 3. Trusted CLI

The sidecar is written only by the trusted control-plane module
`.factory/loop/sidecars.py` (`empty_pre_round_hooks`,
`begin_pre_round_hooks`, `complete_pre_round_hooks`, `read_pre_round`,
`write_pre_round`); it is never invoked by a model role. A fail-closed path
raises `SidecarError`/`SidecarBindingError`/`SidecarTransitionError` and never
publishes a partial or forged sidecar.
