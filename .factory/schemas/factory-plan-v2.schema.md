# `factory-plan/v2` concise active-plan schema (Phase 2D1)

Status: **canonical plan contract.** The human/model-facing plan document
(`.factory/artifacts/implementation-plan.md`) carries only the genuinely
unfinished tasks — active/pending/in_progress/blocked — with stable IDs,
titles, priorities, dependencies, concise Scope/Acceptance, the current
blocker, and the latest actionable failure. Completed/cancelled tasks and the
plan acceptance/evidence history live in the strict committed machine
sidecars (`factory-plan-archive/v1` and `factory-plan-history/v1`); the
conformance matrix lives in the machine conformance sidecar
(`.factory/artifacts/conformance.json`). The legacy `factory-plan/v1` format
remains readable until migrated; migration is an explicit deterministic tool
(`.factory/loop/plan_migration.py`), never a silent auto-mutation.

## 1. Front matter

The YAML front matter carries exactly:

- `spec_path`, `spec_commit`, `spec_blob`, `base_commit`, `status` — the
  unchanged PLAN-01 binding fields (the spec path/commit/blob and the cycle
  base commit never change during a campaign);
- `schema: factory-plan/v2` — the explicit schema marker;
- `sidecars: {"archive": <sha256>, "history": <sha256>}` — the content
  addresses of the two required committed sidecars. The composite plan
  binding digest is `sha256(sha256(plan) | 0x00 | sha256(archive) | 0x00 |
  sha256(history))`; a change to the active plan or to either sidecar changes
  the binding, so the campaign's `plan_digest` (state, launch binding, lease
  claims) provably binds the whole plan-sidecar pair.

## 2. Canonical sections

Exactly two canonical sections, in order:

1. `## Goal and non-goals`
2. `## Architecture and constraints`

The v1 conformance matrix and interaction inventory sections are removed from
the plan document; the machine conformance sidecar and the plan history
sidecar remain the authorities.

## 3. Tasks

- Task IDs are stable original numbers and may be non-contiguous (archived
  IDs are absent from the active plan).
- Each task carries `Status` (pending | in_progress | complete | blocked),
  `Dependencies` (task references, spans bounded by the larger of the parsed
  task count and the archived completed-ID index), `Priority`, concise
  `Scope`, `Acceptance criteria`, `Verification`, `Documentation impact`,
  optional `Blocked on` (exact unresolved reference), optional
  `Latest failure` (the latest actionable failure), and optional
  `Write scopes`.
- The final audit task (`Final documentation and specification audit`) must be
  last in document order and must depend on every other task — the active
  plan tasks plus the archived completed tasks from the bound archive sidecar
  — and no others. A v2 plan cannot be parsed without the bound archive
  records; a missing/conflicting/reopened ID fails closed.
- A v2 plan may transiently carry a task the developer just marked
  `complete`; the trusted campaign archives it after the independent
  verification + audit pass, so the canonical migrated/archived plan never
  does.

## 4. Bounds

The plan document is bounded (64 KiB ceiling enforced by the migration tool);
the sidecars are bounded (1 MiB file / 64 KiB record / 10000 records) and
reject duplicate keys. The parser never materializes an attacker-sized
dependency range.
