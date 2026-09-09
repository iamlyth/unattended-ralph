# `factory-plan-history/v1` history sidecar schema (Phase 2D1)

Status: **canonical committed machine sidecar.** One JSONL record per plan
acceptance/evidence event (migration, archive, reopen, acceptance, plan
revision). The history is append-only and content-addressed by the plan's
`sidecars:` front-matter binding.

## 1. File

- Path: `.factory/artifacts/plan-history.jsonl` (committed history).
- One JSON object per line; empty input is a valid empty history.
- Bounds: 1 MiB file / 64 KiB record / 10000 records; duplicate JSON object
  keys fail closed.

## 2. Record fields (exact set; unknown or repeated keys fail closed)

- `schema`: `factory-plan-history/v1`
- `event`: `migrated` | `archived` | `reopened` | `accepted` | `plan_revised`
- `task_id`: positive integer or null
- `commit`: 40-hex commit
- `plan_digest`: 64-hex SHA-256 of the plan bytes at the event
- `detail`: string

## 3. Authority

The history records plan lifecycle events as data; it is never interpreted as
task/command authority. The existing `.factory/artifacts/conformance.json`,
blocked facts, and audit receipts remain the authorities. The history is
queryable by trusted tools but excluded from routine role prompts.
