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
- `evidence`: string — the full v1 Evidence narrative preserved verbatim
  (bounded inert data, never authority; command-shaped prose stays in the
  narrative and is never a ref)
- `evidence_refs`: list of non-empty safe inert references extracted from
  the Evidence narrative (exact commit/blob IDs and repository-relative
  paths as data).  Each ref must satisfy the safe inert-reference grammar:
  non-empty, no whitespace/control characters, no backslash, not absolute,
  and no empty/`.`/`..` path segments — an unsafe ref fails closed at
  serialization and is never emitted by the migration tool
- `archived_commit`: 40-hex commit at which the task was verified
- `provenance`: `migration` | `campaign` | `reopen`

## 2a. Evidence narrative and refs (Phase 2D1 security remediation A)

The `evidence` field is the lossless audit narrative: the full v1 Evidence
paragraph is preserved byte-for-byte (bounded inert data).  `evidence_refs`
is the curated list of safe inert references extracted from it — backtick-
quoted tokens that are either a repository-relative path (contains `/` or
`.`) or a hex commit/blob ID (`[0-9a-f]{7,40}(…)?`).  Command-shaped prose
(e.g. ``git diff HEAD -- docs/SPEC.md``) stays in the narrative and is never
a ref.  Refs are data for audit display, never interpreted as task/command
authority; the safe grammar is enforced by `validate_evidence_ref` at
serialization and by the migration tool at extraction.

## 3. Trusted completed-ID index

`completed_ids(records)` is the pure-function index of archived task IDs. It
is bound to the archive sidecar digest by the plan's `sidecars:` front-matter
binding; a missing/conflicting/reopened ID fails closed at every caller that
binds the index to the digest. Only the trusted campaign archiver (or the
explicit migration tool) appends records; planner/developer roles can never
mutate the archive.  The trusted archiver anchors to the committed
archive/history blobs at the exact head and requires the worktree bytes to
equal them; a stale or forged worktree sidecar fails closed before any
mutation.
