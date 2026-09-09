# `factory-plan-archive/v1` archive sidecar schema (Phase 2D1)

Status: **canonical committed machine sidecar.** One JSONL record per
archived completed/cancelled task, preserving every task/status/acceptance/
evidence datum losslessly for audit. Exact commit/evidence references are
treated as data, never interpreted as task/command authority.

## 1. File

- Path: `.factory/artifacts/plan-archive.jsonl` (committed history).
- One JSON object per line; empty input is a valid empty archive.
- Bounds: 1 MiB file / 64 KiB record / 10000 records; duplicate JSON object
  keys fail closed.
- Append-only: a completed task may be archived exactly once; reopening is an
  explicit semantic migration with provenance, never model prose.

## 2. Record fields (exact set; unknown or repeated keys fail closed)

- `schema`: `factory-plan-archive/v1`
- `task_id`: positive integer (stable original plan ID)
- `title`: non-empty string
- `priority`: positive integer
- `dependencies`: list of positive integers (no repeats)
- `status`: `complete` | `cancelled`
- `scope`, `acceptance`, `verification`, `documentation_impact`: strings
  (full v1 text preserved verbatim)
- `evidence_refs`: list of non-empty strings (exact commit/evidence
  references as data)
- `archived_commit`: 40-hex commit at which the task was verified
- `provenance`: `migration` | `campaign` | `reopen`

## 3. Trusted completed-ID index

`completed_ids(records)` is the pure-function index of archived task IDs. It
is bound to the archive sidecar digest by the plan's `sidecars:` front-matter
binding; a missing/conflicting/reopened ID fails closed at every caller that
binds the index to the digest. Only the trusted campaign archiver (or the
explicit migration tool) appends records; planner/developer roles can never
mutate the archive.
