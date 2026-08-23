# `factory-plan/v1` canonical plan schema

Status: committed contract (normative for PLAN-01). The canonical plan is the
Markdown document `.factory/artifacts/implementation-plan.md`; the deterministic
parser is `.factory/loop/plan_parser.py`. The JSON model produced by the parser
is described by `.factory/schemas/factory-plan-v1.schema.json`.

## 1. Contract

The canonical implementation plan is UTF-8 Markdown conforming to this schema.
The parser accepts exactly the one documented heading/field grammar below.
Free-form prose cannot alter lifecycle fields: every normative field is parsed
and machine-checked. The parser is a pure, deterministic function of the plan
bytes — identical bytes always produce the identical model, JSON dump, and
canonical serialization — and `parse -> serialize -> parse` reproduces the
input bytes exactly (round-trip without semantic loss).

## 2. Front matter

The document starts with a YAML-like front matter block on the first line:

```text
---
spec_path: <repository-relative path, no leading slash>
spec_commit: <40 hex characters>
spec_blob: <40 hex characters>
base_commit: <40 hex characters>
status: active|complete
---
```

- exactly the five keys `spec_path`, `spec_commit`, `spec_blob`,
  `base_commit`, `status`, each exactly once, in any order;
- `spec_path` must be non-empty and repository-relative (never absolute);
- `spec_commit`, `spec_blob`, and `base_commit` must be 40-character Git
  object IDs;
- `status` is the plan lifecycle status: `active` (planning/implementation) or
  `complete` (ledger closed for acceptance).

## 3. Title and section headings

The document contains exactly one top-level title:

```text
# Implementation Plan
```

followed by the canonical `##` sections in this exact order, each exactly once:

```text
## Goal and non-goals
## Architecture and constraints
## Specification conformance matrix
## Interaction acceptance inventory
```

Every task follows after all canonical sections. A heading that is not one of
the canonical sections and not a task heading is rejected. The four canonical
section bodies are parsed for the conformance matrix and interaction inventory
below; goal/architecture prose round-trips verbatim.

## 4. Task sections

Tasks use exactly this heading grammar, numbered contiguously from 1:

```text
## Task <N>: <title>
```

- task identifiers must be unique and contiguous (1, 2, 3, …);
- task titles must be unique;
- a heading that begins `## Task` but does not match the grammar is a
  malformed/ambiguous task section.

Each task body is a sequence of `- <Key>: <value>` bullet fields. A field value
may continue on following lines indented with at least one space. Field labels
and cardinality are fixed:

| Field | Required | Notes |
|-------|----------|-------|
| `Status` | yes | exactly one; see statuses below |
| `Dependencies` | yes | `None` or a comma-separated list of `Task N` / `Tasks N-M` items |
| `Scope` | yes | non-empty |
| `Acceptance criteria` | yes | non-empty |
| `Verification` | yes | non-empty |
| `Documentation impact` | yes | may be empty |
| `Priority` | no | positive integer; defaults to the task number |
| `Evidence` | no | acceptance/evidence references recorded by the developer |
| `Blocked on` | no | required when `Status: blocked`; names the exact unresolved requirement/fact reference |

Duplicate field labels, unknown field labels, missing required fields, and
empty required values are parse errors. Body lines that are neither a field, a
field continuation, nor blank make the task section ambiguous and are rejected.

## 5. Statuses and transitions

Allowed task states: `pending`, `in_progress`, `complete`, `blocked`.

Allowed transitions (no other transition is allowed):

- `pending -> in_progress` by the trusted selector at developer launch;
- `in_progress -> complete` only after the task checkpoint and acceptance
  checks validate;
- `in_progress -> blocked` only with an exact unresolved requirement/fact
  reference (`- Blocked on:`);
- `blocked -> pending` only in a new planner commit after the blocker changed;
- `in_progress -> pending` only through explicit interruption recovery that
  preserves work and records no completion claim.

`complete` is write-once within a campaign. Statically checkable invariants
enforced by the parser: a status outside the allowed set is rejected, at most
one task may be `in_progress`, and a `blocked` task must carry a non-empty
`Blocked on` reference.

## 6. Dependencies and priorities

- `Dependencies: None` means no dependencies; otherwise each item is `Task N`
  or `Task(s) N-M` (ranges expand; descending ranges are rejected; repeats are
  rejected).
- Dependencies must reference existing tasks, must not reference the task
  itself, and must not reference a later task unless the referencing task is
  the final documentation and specification audit (which may also reference
  appended remediation tasks that follow it).
- The dependency graph must be acyclic.
- `Priority` is an optional positive integer; when absent it defaults to the
  task number, so §8 deterministic selection still sorts by ID.

## 7. Conformance matrix

`## Specification conformance matrix` contains a Markdown table with exactly
the header

```text
| ID | Spec § | Classification | Evidence | Task |
```

and one row per normative requirement:

- `ID` matches `^[A-Z][A-Z0-9]*(?:[-_][A-Z0-9]+)+$` and is unique;
- `Spec §` and `Evidence` are non-empty;
- `Classification` is one of `verified | partial | missing | ambiguous`;
- `Task` references existing tasks (same item grammar as dependencies) and is
  non-empty for non-`verified` rows.

## 8. Interaction acceptance inventory

`## Interaction acceptance inventory` must cover exactly the four boundary
bullets, each exactly once, in any order:

```text
- input boundary: …
- semantic boundary: …
- production boundary: …
- evidence boundary: …
```

## 9. Determinism and round-trip

- `parse_plan(bytes)` -> model; `Plan.dump_json()` -> deterministic JSON
  (sorted keys, no whitespace variance); `Plan.serialize()` -> canonical
  Markdown.
- For any document that parses, `serialize(parse(text)) == text` exactly
  (byte-for-byte), and `parse(serialize(parse(text)))` is identical to
  `parse(text)`.

## 10. Defect classes and fixtures

Each defect class below is rejected with `PlanError` and has an exact fixture
under `.factory/tests/fixtures/plan-*.md`:

| Defect class | Fixture |
|--------------|---------|
| duplicate canonical heading | `plan-duplicate-heading.md` |
| unknown task lifecycle status | `plan-unknown-status.md` |
| unknown front-matter lifecycle status | `plan-unknown-lifecycle-status.md` |
| ambiguous/malformed task section | `plan-ambiguous-task-section.md` |
| out-of-order dependency | `plan-out-of-order-dependency.md` |
| cyclic dependency | `plan-cyclic-dependency.md` |
| non-contiguous task IDs | `plan-noncontiguous-ids.md` |
| duplicate task ID | `plan-duplicate-task-id.md` |
| duplicate task title | `plan-duplicate-task-title.md` |
| duplicate task field | `plan-duplicate-field.md` |
| missing required task field | `plan-missing-field.md` |
| unknown task field | `plan-unknown-field.md` |
| malformed dependencies | `plan-malformed-dependencies.md` |
| self-dependency | `plan-self-dependency.md` |
| unknown dependency target | `plan-unknown-dependency.md` |
| more than one `in_progress` task | `plan-two-in-progress.md` |
| blocked task without reference | `plan-blocked-without-reference.md` |
| non-numeric priority | `plan-bad-priority.md` |
| duplicate matrix requirement ID | `plan-matrix-duplicate-id.md` |
| invalid matrix classification | `plan-matrix-bad-classification.md` |
| malformed matrix header | `plan-matrix-bad-header.md` |
| matrix row referencing unknown task | `plan-matrix-unknown-task.md` |
| missing interaction inventory | `plan-missing-interactions.md` |
| incomplete interaction boundaries | `plan-missing-interaction-boundary.md` |
| missing front-matter key | `plan-front-matter-missing.md` |
| duplicate front-matter key | `plan-front-matter-duplicate-key.md` |
| malformed front-matter commit | `plan-front-matter-bad-sha.md` |
| absolute front-matter spec path | `plan-front-matter-absolute-path.md` |
| unrecognized section heading | `plan-unrecognized-heading.md` |
| plan without tasks | `plan-no-tasks.md` |
| missing/duplicated title | `plan-no-title.md` |
| UTF-8 byte order mark prefix | `plan-bom.md` |
| verified row with empty task reference | `plan-verified-empty-refs.md` |
| verified row referencing a non-complete task | `plan-verified-pending.md` |
| verified row in an `active` plan | `plan-verified-in-active-plan.md` |
| matrix missing a §24 registry ID | `plan-matrix-missing-id.md` |
| matrix ID outside the §24 registry | `plan-matrix-extra-id.md` |
| lifecycle `complete` with an unfinished task | `plan-lifecycle-inconsistent.md` |
| oversized dependency range endpoint | `plan-dependency-range-oversize.md` |
| oversized matrix task range endpoint | `plan-matrix-range-oversize.md` |
| oversized/overflowing dependency number | `plan-range-overflow.md` |
| continuation lines on a structured field | `plan-structured-field-continuation.md` |
| empty interaction-boundary text | `plan-empty-interaction.md` |
| empty/dot-segment/`..` traversal spec path | `plan-front-matter-traversal-path.md` |
| final-audit task not last | `plan-final-audit-misplaced.md` |
| final-audit task missing a dependency | `plan-final-audit-missing-dependency.md` |
| non-verified row referencing only complete tasks | `plan-matrix-complete-only-pending.md` |
| empty required field value | `plan-empty-required-value.md` |
| missing plan title | `plan-missing-title.md` |
| duplicated plan title | `plan-duplicate-title.md` |

Accepted fixtures (must parse, serialize byte-identically, and stay accepted
by the legacy validator) are `plan-valid-base.md` (a two-task registry-bound
fixture plan exercising an explicit `Priority` and a final-audit dependency)
and `plan-trailing-blank-line.md` (a valid plan whose final trailing blank
line proves `serialize` reproduces the input bytes exactly).

### 10.1 §24 requirement registry

The conformance matrix is machine-bounded to the stable set of 24 normative
requirement IDs (`docs/FACTORY-LOOP-SPEC.md` §24) recorded in
`.factory/schemas/factory-plan-v1.requirements.json`. The parser loads this
registry deterministically at parse time and fails closed when it is missing,
malformed, duplicated, or divergent from the committed stable set. A matrix
that misses, duplicates, or exceeds these IDs is rejected, so free-form prose
can never smuggle an unregistered requirement past the gate.

### 10.2 Range and endpoint bounds

Dependency and matrix task references are parsed into `(start, end)` spans
and never materialized; every endpoint is bounded to the parsed task count
before expansion, so an attacker-sized range can never allocate memory or CPU
proportional to its endpoint. A digit string longer than the documented cap
(`MAX_ENDPOINT_DIGITS`) or an integer conversion `OverflowError` raises a
bounded `PlanError`.

### 10.3 Structured-field, path, and lifecycle hardening

- A UTF-8 BOM prefix never parses; `serialize` compares the actual input bytes
  with the output bytes, preserving trailing blank lines.
- `Status`, `Dependencies`, and `Priority` are structured single-line
  fields: any continuation line on them is rejected rather than silently
  interpreting only the first line. `Blocked on` is intentionally prose that
  may span lines and is joined, never truncated.
- The interaction inventory boundary text must be non-empty.
- `spec_path` must be non-empty, repository-relative, and free of empty,
  `.`, and `..` segments.
- The final-audit task must be the last task and depend on every other task
  and no others.
- A non-verified conformance row must be owned by a task that is not complete;
  a row referencing only completed tasks must be `verified`.
- A `complete` lifecycle plan requires every task `complete`; an `active` plan
  may not contain a `verified` conformance row.
