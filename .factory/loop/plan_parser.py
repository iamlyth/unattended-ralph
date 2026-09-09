#!/usr/bin/env python3
"""Deterministic stdlib-only parser for the `factory-plan/v1` canonical plan.

This module implements the accepted-boundary grammar documented in the
committed schema ``.factory/schemas/factory-plan-v1.schema.md`` and the JSON
model contract in ``.factory/schemas/factory-plan-v1.schema.json``.  It uses
only the Python standard library.

Contract (see schema for the full grammar):

* front matter binds the exact spec path, spec commit/blob, base commit, and
  lifecycle status (``active`` | ``complete``), each key exactly once;
* exactly one ``# Implementation Plan`` title and exactly one occurrence of
  each canonical ``##`` section (goal, architecture, conformance matrix,
  interaction inventory), in order, before any ``## Task N:`` section;
* tasks are uniquely and contiguously numbered, each with exactly one
  ``Status``, ``Dependencies``, ``Scope``, ``Acceptance criteria``,
  ``Verification``, and ``Documentation impact`` field and optional
  ``Priority``, ``Evidence``, and ``Blocked on`` fields; unknown or repeated
  fields are rejected;
* statuses are limited to the documented lifecycle set; at most one task is
  ``in_progress``; a ``blocked`` task must name an exact unresolved reference;
* dependencies reference existing tasks only, never themselves, never a later
  task unless the task is the final documentation and specification audit,
  and never in a cycle;
* the conformance matrix header, classification values, and task references
  are machine-checked; the interaction inventory covers exactly the four
  documented boundaries.

The parser is a pure, deterministic function of the plan bytes: identical
bytes always produce the identical model, JSON dump, and canonical
serialization.  ``parse -> serialize -> parse`` reproduces the original bytes
exactly for any document that parses (``roundtrip`` without semantic loss),
including trailing blank lines; a UTF-8 byte order mark never parses.

Defect classes (each documented with an exact fixture in
``.factory/tests/fixtures/plan-*.md``) are rejected with ``PlanError``:
duplicate headings/keys/IDs, unknown lifecycle states, ambiguous task
sections, out-of-order or cyclic dependencies, non-contiguous IDs, invalid
front matter, malformed dependencies, unknown fields, invalid conformance
rows, and incomplete interaction inventories.  The hardened boundary (Task 18)
additionally rejects: BOM-prefixed input, ``verified`` rows with empty or
non-complete task references, ``verified`` rows in an ``active`` plan,
matrices that miss or exceed the committed \u00a724 requirement registry
(``factory-plan-v1.requirements.json``), a ``complete`` lifecycle with an
unfinished task, dependency or matrix ranges whose endpoints exceed the
parsed task count (and oversized endpoints in general), continuation lines on
structured lifecycle fields, empty interaction-boundary text, spec paths with
empty/``.``/``..`` segments, a misplaced or under-dependent final audit task,
non-verified matrix rows that reference only completed tasks, and missing or
duplicated plan titles.  Every rejected fixture raises a bounded ``PlanError``
without materializing an attacker-sized range.
"""

from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass, field as dataclass_field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

SCHEMA_NAME = "factory-plan/v1"
PLAN_TITLE = "# Implementation Plan"
FINAL_AUDIT_TITLE = "Final documentation and specification audit"

# Committed machine registry of the 26 stable FACTORY-LOOP-SPEC \u00a724
# normative requirement IDs (PLAN-01, Task 18 item 3). The parser loads it
# deterministically at parse time and fails closed when it is missing,
# malformed, or diverges from the stable set below.
REQUIREMENTS_REGISTRY = (
    Path(__file__).resolve().parent.parent
    / "schemas" / "factory-plan-v1.requirements.json"
)
STABLE_REQUIREMENT_IDS = (
    "AUTH-01", "CTX-01", "CTX-02", "ROLE-01", "PLAN-01", "TASK-01",
    "TASK-02", "QUOTA-01", "QUOTA-02", "STATE-01", "STATE-02",
    "LOCK-01", "PROC-01", "GIT-01", "PHASE-01", "COMPLETE-01",
    "FIND-01", "CRED-01", "EVID-01", "EVID-02", "VIS-01",
    "RUNNER-01", "HIDE-01", "MIG-01", "TEST-01", "ACCEPT-01",
    "LEASE-01",
)

# Structured lifecycle fields are machine-read as single lines; a continuation
# line on one of them must be rejected instead of silently ignored (Task 18
# item 6). ``Blocked on`` is intentionally excluded: its exact reference is
# prose that may span lines, and the parser joins them so nothing is silently
# truncated. ``Write scopes`` is a closed-format structured request list.
STRUCTURED_FIELDS = ("Status", "Dependencies", "Priority", "Write scopes")

# An endpoint larger than 10^40 can never reference a task in any plan; this
# cap keeps int() conversion (and the Python max-str-digits limit) out of the
# attack surface while remaining far above every real task count.
MAX_ENDPOINT_DIGITS = 40

# Front matter keys, in canonical order.
FRONT_KEYS = ("spec_path", "spec_commit", "spec_blob", "base_commit", "status")
LIFECYCLE_STATUSES = ("active", "complete")

# Allowed task lifecycle states (documented in schema statuses/transitions).
TASK_STATUSES = ("pending", "in_progress", "complete", "blocked")

# Allowed conformance classifications (mirrors the existing validator).
CLASSIFICATIONS = ("verified", "partial", "missing", "ambiguous", "blocked", "not_applicable")


def _reject_duplicate_keys(pairs):
    """JSON object-pairs hook: reject duplicate object keys in committed data.

    A duplicate key in any schema/config file (the \u00a724 registry included)
    silently overwrites its predecessor under a plain ``dict`` decode and can
    hide a drifted authority; the acceptance boundary rejects it instead.
    """
    result = {}
    for key, value in pairs:
        if key in result:
            raise PlanError(f"duplicate JSON object key in the \u00a724 registry: {key!r}")
        result[key] = value
    return result

# Task field grammar: required fields exactly once; optional fields at most
# once; any other field label is a parse error.
REQUIRED_FIELDS = (
    "Status",
    "Dependencies",
    "Scope",
    "Acceptance criteria",
    "Verification",
    "Documentation impact",
)
OPTIONAL_FIELDS = ("Priority", "Evidence", "Blocked on", "Write scopes")
ALL_FIELDS = REQUIRED_FIELDS + OPTIONAL_FIELDS

# The canonical sections, in canonical order, before any task section.
CANONICAL_SECTIONS = (
    "Goal and non-goals",
    "Architecture and constraints",
    "Specification conformance matrix",
    "Interaction acceptance inventory",
)
MATRIX_HEADER = ("ID", "Spec \u00a7", "Classification", "Evidence", "Task")
INTERACTION_BOUNDARIES = (
    "input boundary",
    "semantic boundary",
    "production boundary",
    "evidence boundary",
)

# Allowed task transitions, documented in schema statuses/transitions.  The
# parser enforces the statically checkable invariants of this table (allowed
# statuses, one in_progress, blocked must name a reference); the transition
# itself is enforced by the trusted selector/state machinery.
ALLOWED_TRANSITIONS: Dict[str, frozenset] = {
    "pending": frozenset({"in_progress"}),
    "in_progress": frozenset({"complete", "blocked", "pending"}),
    "blocked": frozenset({"pending"}),
    "complete": frozenset(),
}


class PlanError(Exception):
    """Raised when plan bytes violate the ``factory-plan/v1`` grammar."""


SHA_RE = re.compile(r"^[0-9a-f]{40}$")
MATRIX_ID_RE = re.compile(r"^[A-Z][A-Z0-9]*(?:[-_][A-Z0-9]+)+$")
TASK_HEADING_RE = re.compile(r"^## Task\s+(\d+):\s*(.+?)\s*$")
FIELD_RE = re.compile(r"^- ([A-Z][A-Za-z ]*?):\s*(.*?)\s*$")
FRONT_RE = re.compile(r"^([a-z_]+):\s*(\S.*?)\s*$")
DEP_ITEM_RE = re.compile(r"^Tasks?\s+(\d+)(?:\s*[-\u2013\u2014]\s*(\d+))?$", re.I)
PRIORITY_RE = re.compile(r"^\d+$")
BOUNDARY_RE = re.compile(r"^- ((?:input|semantic|production|evidence) boundary):\s*(.*?)\s*$")
# Closed-format write-scope ID (Phase 2C1): the same grammar the committed
# path-lease policy uses, so a plan can only request scopes the policy can
# name.  The parser enforces the closed format and duplicate rejection; the
# trusted policy intersection (``.factory/loop/path_lease.py``) decides
# whether a requested scope is known and grants anything.
WRITE_SCOPE_RE = re.compile(r"^[a-z][a-z0-9_-]*$")


def _parse_dep_spans(value: str, what: str, *, allow_empty: bool = False) -> List[Tuple[int, int]]:
    """Parse a dependency/reference list into ``(start, end)`` spans.

    Ranges are never materialized here: an attacker-sized range must not
    allocate memory proportional to its endpoint. ``_expand_spans`` bounds
    every endpoint to the parsed task count before expansion (Task 18 item 5).
    ``allow_empty`` is used by conformance rows, where an empty ``Task`` cell
    is a row-level defect handled by the caller, not a grammar error.
    """
    value = value.strip()
    if value.lower() == "none":
        return []
    if not value:
        if allow_empty:
            return []
        raise PlanError(f"{what} has an empty dependency list")
    spans: List[Tuple[int, int]] = []
    for item in (part.strip() for part in value.split(",")):
        if not item:
            raise PlanError(f"{what} has malformed dependencies: {value}")
        match = DEP_ITEM_RE.fullmatch(item)
        if not match:
            raise PlanError(f"{what} has malformed dependencies: {value}")
        raw_start, raw_end = match.group(1), (match.group(2) or match.group(1))
        if len(raw_start) > MAX_ENDPOINT_DIGITS or len(raw_end) > MAX_ENDPOINT_DIGITS:
            raise PlanError(f"{what} has an out-of-range dependency number: {item}")
        try:
            start = int(raw_start)
            end = int(raw_end)
        except (ValueError, OverflowError) as exc:
            raise PlanError(
                f"{what} has an out-of-range dependency number: {item}"
            ) from exc
        if start < 1 or end < 1:
            raise PlanError(f"{what} has a non-positive dependency number: {item}")
        if end < start:
            raise PlanError(f"{what} has a descending dependency range: {item}")
        spans.append((start, end))
    _check_no_overlapping_spans(spans, what)
    return spans


def _check_no_overlapping_spans(spans: List[Tuple[int, int]], what: str) -> None:
    """Reject overlapping spans (a repeated number) without materializing them."""
    ordered = sorted(spans)
    previous_end: Optional[int] = None
    for start, end in ordered:
        if previous_end is not None and start <= previous_end:
            raise PlanError(f"{what} repeats a dependency")
        if previous_end is None or end > previous_end:
            previous_end = end


def _expand_spans(
    spans: List[Tuple[int, int]], max_id: int, what: str, kind: str
) -> List[int]:
    """Expand validated spans, bounding every endpoint to the parsed count.

    Any endpoint beyond ``max_id`` is rejected before a range is materialized,
    so the allocated list can never grow past the number of parsed tasks.
    ``kind`` selects the documented error wording (``deps`` for dependency
    lists, ``matrix`` for conformance rows) so the parser and the legacy
    validator agree on every accepted fixture.
    """
    result: List[int] = []
    for start, end in spans:
        if end > max_id:
            if start == end:
                if kind == "deps":
                    raise PlanError(
                        f"{what} references unknown dependencies: Task {end}"
                    )
                raise PlanError(f"{what} references an unknown task: Task {end}")
            if kind == "deps":
                raise PlanError(
                    f"{what} has an oversized dependency range "
                    f"(endpoint {end} exceeds the {max_id} parsed tasks)"
                )
            raise PlanError(
                f"{what} has an oversized task range "
                f"(endpoint {end} exceeds the {max_id} parsed tasks)"
            )
        result.extend(range(start, end + 1))
    return result

def is_allowed_transition(current: str, next_status: str) -> bool:
    """Return whether ``current -> next_status`` is in the schema transition table."""
    return next_status in ALLOWED_TRANSITIONS.get(current, frozenset())


@dataclass
class Block:
    """A raw heading block: heading line (or None) and its raw lines."""

    heading: Optional[str]
    lines: List[str]


@dataclass
class Task:
    number: int
    title: str
    status: str
    dependencies: List[int]
    priority: int
    blocked_on: Optional[str]
    fields: Dict[str, str]
    field_order: List[str]
    # Closed-format ``Write scopes:`` request list (Phase 2C1).  The planner
    # request grants nothing by itself; the trusted policy intersection
    # (``.factory/loop/path_lease.py``) decides.  Empty when the field is
    # absent or ``None``.
    write_scopes: List[str] = dataclass_field(default_factory=list)
    # Unmaterialized ``(start, end)`` spans parsed from ``Dependencies``; they
    # are expanded against the parsed task count before validation so an
    # attacker-sized range can never be materialized (Task 18 item 5).
    dep_spans: List[Tuple[int, int]] = dataclass_field(default_factory=list, repr=False)


@dataclass
class MatrixRow:
    requirement_id: str
    spec_sections: str
    classification: str
    evidence: str
    tasks: List[int]


@dataclass
class Interaction:
    boundary: str
    text: str


@dataclass
class Plan:
    """Parsed canonical plan with deterministic model and serialization."""

    schema: str = SCHEMA_NAME
    spec_path: str = ""
    spec_commit: str = ""
    spec_blob: str = ""
    base_commit: str = ""
    status: str = ""
    tasks: List[Task] = dataclass_field(default_factory=list)
    matrix: List[MatrixRow] = dataclass_field(default_factory=list)
    interactions: List[Interaction] = dataclass_field(default_factory=list)
    _blocks: List[Block] = dataclass_field(default_factory=list, repr=False)

    @classmethod
    def from_bytes(cls, data: bytes) -> "Plan":
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise PlanError(f"plan is not valid UTF-8: {exc}") from exc
        return cls.from_text(text)

    @classmethod
    def from_text(cls, text: str) -> "Plan":
        return parse_plan(text)

    @classmethod
    def from_file(cls, path: Path) -> "Plan":
        try:
            data = Path(path).read_bytes()
        except OSError as exc:
            raise PlanError(f"cannot read plan {path}: {exc}") from exc
        return cls.from_bytes(data)

    def serialize(self) -> str:
        """Return the canonical Markdown serialization (byte-exact for parsed input)."""
        parts: List[str] = []
        for block in self._blocks:
            parts.extend(block.lines)
        return "\n".join(parts)

    def to_dict(self) -> Dict[str, object]:
        """Deterministic JSON-ready model of the parsed plan."""
        return {
            "schema": self.schema,
            "spec_path": self.spec_path,
            "spec_commit": self.spec_commit,
            "spec_blob": self.spec_blob,
            "base_commit": self.base_commit,
            "status": self.status,
            "tasks": [
                {
                    "number": task.number,
                    "title": task.title,
                    "status": task.status,
                    "priority": task.priority,
                    "dependencies": list(task.dependencies),
                    "blocked_on": task.blocked_on,
                    "write_scopes": list(task.write_scopes),
                    "fields": {key: task.fields[key] for key in task.field_order},
                }
                for task in self.tasks
            ],
            "matrix": [
                {
                    "requirement_id": row.requirement_id,
                    "spec_sections": row.spec_sections,
                    "classification": row.classification,
                    "evidence": row.evidence,
                    "tasks": list(row.tasks),
                }
                for row in self.matrix
            ],
            "interactions": [
                {"boundary": entry.boundary, "text": entry.text}
                for entry in self.interactions
            ],
        }

    def dump_json(self) -> str:
        """Deterministic JSON rendering (sorted keys, no whitespace variance)."""
        return json.dumps(self.to_dict(), sort_keys=True, ensure_ascii=False, separators=(",", ":"))


_REGISTRY_CACHE: Optional[List[str]] = None


def _load_requirement_registry() -> List[str]:
    """Load and validate the committed \u00a724 requirement-ID registry.

    The registry is part of the acceptance boundary (Task 18 item 3): a
    missing, malformed, or divergent registry fails closed so free-form
    matrices can never evade the stable ID set.
    """
    global _REGISTRY_CACHE
    if _REGISTRY_CACHE is not None:
        return _REGISTRY_CACHE
    try:
        data = json.loads(
            REQUIREMENTS_REGISTRY.read_text(encoding="utf-8"),
            object_pairs_hook=_reject_duplicate_keys,
        )
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise PlanError(
            f"cannot load the committed \u00a724 requirement registry "
            f"{REQUIREMENTS_REGISTRY}: {exc}"
        ) from exc
    if not isinstance(data, dict) or not isinstance(data.get("requirement_ids"), list):
        raise PlanError(
            "the \u00a724 requirement registry must be an object with a "
            "`requirement_ids` array"
        )
    ids = data["requirement_ids"]
    seen: set = set()
    for rid in ids:
        if not isinstance(rid, str) or not MATRIX_ID_RE.fullmatch(rid):
            raise PlanError(f"the \u00a724 requirement registry has an invalid ID: {rid!r}")
        if rid in seen:
            raise PlanError(f"the \u00a724 requirement registry has a duplicate ID: {rid}")
        seen.add(rid)
    if set(ids) != set(STABLE_REQUIREMENT_IDS):
        raise PlanError(
            "the \u00a724 requirement registry must contain exactly the stable "
            "FACTORY-LOOP-SPEC \u00a724 IDs"
        )
    _REGISTRY_CACHE = list(ids)
    return _REGISTRY_CACHE


def _validate_spec_path(spec_path: str) -> None:
    """Reject empty, absolute, dot-segment, and ``..`` traversal spec paths."""
    if not spec_path:
        raise PlanError("front matter spec_path must be non-empty and repository-relative")
    if spec_path.startswith("/"):
        raise PlanError("front matter spec_path must be repository-relative")
    if any(segment in ("", ".", "..") for segment in spec_path.split("/")):
        raise PlanError(
            "front matter spec_path must not contain empty, `.`, or `..` segments"
        )


def _table_cells(line: str) -> List[str]:
    return [cell.strip() for cell in line.strip().strip("|").split("|")]


def parse_plan(text: str) -> Plan:
    """Parse canonical plan bytes (UTF-8 text) and return the semantic model."""
    if not isinstance(text, str):
        raise TypeError("parse_plan expects a str")
    if text.startswith("\ufeff"):
        raise PlanError("plan must not start with a UTF-8 byte order mark")
    # Keep every line, including all trailing empty lines, so that
    # ``serialize`` reproduces the input bytes exactly (Task 18 item 1).
    lines = text.split("\n")

    # --- front matter ---------------------------------------------------
    if not lines or lines[0] != "---":
        raise PlanError("front matter must start on the first line")
    close = None
    for index, line in enumerate(lines[1:], start=1):
        if line == "---":
            close = index
            break
    if close is None:
        raise PlanError("front matter must be terminated by a closing `---` line")

    front_fields: List[Tuple[str, str]] = []
    seen_keys: set = set()
    for raw in lines[1:close]:
        match = FRONT_RE.fullmatch(raw)
        if not match:
            raise PlanError(f"front matter has a malformed field line: {raw!r}")
        key, value = match.group(1), match.group(2)
        if key in seen_keys:
            raise PlanError(f"front matter has duplicate key `{key}`")
        seen_keys.add(key)
        front_fields.append((key, value))

    missing_keys = [k for k in FRONT_KEYS if k not in seen_keys]
    unknown_keys = sorted(k for k in seen_keys if k not in FRONT_KEYS)
    if missing_keys or unknown_keys:
        detail = ""
        if missing_keys:
            detail += f"; missing {', '.join(missing_keys)}"
        if unknown_keys:
            detail += f"; unknown {', '.join(unknown_keys)}"
        raise PlanError(f"front matter must contain exactly `{', '.join(FRONT_KEYS)}`{detail}")

    front = dict(front_fields)
    spec_path = front["spec_path"]
    _validate_spec_path(spec_path)
    for key in ("spec_commit", "spec_blob", "base_commit"):
        if not SHA_RE.fullmatch(front[key]):
            raise PlanError(f"front matter {key} must be a 40-character Git object ID")
    if front["status"] not in LIFECYCLE_STATUSES:
        raise PlanError(
            f"front matter status must be one of `{'|'.join(LIFECYCLE_STATUSES)}`, "
            f"got `{front['status']}`"
        )

    # --- title ---------------------------------------------------------------
    body_start = close + 1
    title_indices = [
        index
        for index in range(body_start, len(lines))
        if re.match(r"^#(?!#)", lines[index])
    ]
    if len(title_indices) != 1:
        raise PlanError(f"plan requires exactly one `{PLAN_TITLE}` title")
    title_index = title_indices[0]
    if lines[title_index] != PLAN_TITLE:
        raise PlanError(f"plan title must be exactly `{PLAN_TITLE}`")

    first_heading = len(lines)
    for index in range(body_start, len(lines)):
        if lines[index].startswith("## "):
            first_heading = index
            break
    if title_index >= first_heading:
        raise PlanError(f"`{PLAN_TITLE}` must precede every `## ` section heading")

    # --- raw blocks ----------------------------------------------------------
    blocks: List[Block] = [
        Block(None, lines[:close + 1]),          # front matter incl. closing `---`
        Block(None, lines[body_start:first_heading]),  # title with separators
    ]
    index = first_heading
    while index < len(lines):
        end = index + 1
        while end < len(lines) and not lines[end].startswith("## "):
            end += 1
        blocks.append(Block(lines[index], lines[index:end]))
        index = end

    # --- canonical sections and task order -----------------------------------
    section_blocks: List[Block] = []
    task_blocks: List[Block] = []
    seen_sections: set = set()
    expected = 0
    task_started = False
    for block in blocks[2:]:
        heading = block.heading
        if heading is None or heading.startswith("## Task"):
            task_started = True
            if heading is not None:
                task_blocks.append(block)
            else:
                raise PlanError(f"unexpected content block: {block.lines!r}")
            continue
        if task_started:
            raise PlanError("task sections must follow all canonical sections")
        if heading in seen_sections:
            raise PlanError(f"duplicate canonical section `{heading}`")
        seen_sections.add(heading)
        if heading == "## " + CANONICAL_SECTIONS[expected]:
            section_blocks.append(block)
            expected += 1
            continue
        if heading in ("## " + name for name in CANONICAL_SECTIONS):
            raise PlanError(
                f"canonical sections out of order; expected `## {CANONICAL_SECTIONS[expected]}` "
                f"found `{heading}`"
            )
        raise PlanError(f"unknown section heading `{heading}`")
    if expected != len(CANONICAL_SECTIONS):
        missing = ", ".join(
            "## " + name for name in CANONICAL_SECTIONS[expected:]
        )
        raise PlanError(f"plan requires canonical section(s) `{missing}` before any task")
    if not task_blocks:
        raise PlanError("plan has no numbered tasks")

    # --- tasks ---------------------------------------------------------------
    tasks: List[Task] = []
    seen_numbers: set = set()
    seen_titles: set = set()
    for index, block in enumerate(task_blocks):
        heading = block.heading
        match = TASK_HEADING_RE.fullmatch(heading)
        if not match:
            if re.fullmatch(r"^## Task\s+\d+:\s*$", heading):
                raise PlanError(f"task heading `{heading}` has no title")
            raise PlanError(f"malformed task heading: `{heading}`")
        number = int(match.group(1))
        title = match.group(2).strip()
        if number in seen_numbers:
            raise PlanError(f"duplicate task id `{number}`")
        expected_number = index + 1
        if number != expected_number:
            raise PlanError(
                f"task numbers must be unique and contiguous "
                f"(expected Task {expected_number}, found Task {number})"
            )
        if title in seen_titles:
            raise PlanError(f"duplicate task title: `{title}`")
        seen_numbers.add(number)
        seen_titles.add(title)
        tasks.append(_parse_task_block(number, title, block))

    # Expand every dependency range against the parsed task count before any
    # validation or graph traversal; oversized ranges fail here without ever
    # being materialized (Task 18 item 5).
    for task in tasks:
        task.dependencies = _expand_spans(
            task.dep_spans, len(tasks), what=f"task {task.number}", kind="deps"
        )

    _validate_task_graph(tasks)
    _validate_status_invariants(tasks)
    finals = [task.number for task in tasks if task.title == FINAL_AUDIT_TITLE]
    if len(finals) != 1:
        raise PlanError(f"plan requires exactly one task titled `{FINAL_AUDIT_TITLE}`")
    final_number = finals[0]
    if tasks[-1].number != final_number:
        raise PlanError(f"`{FINAL_AUDIT_TITLE}` must be the last task")
    expected_deps = set(range(1, len(tasks) + 1))
    expected_deps.discard(final_number)
    if set(tasks[final_number - 1].dependencies) != expected_deps:
        raise PlanError(
            "the final audit task must depend on every other task and no others"
        )
    if front["status"] == "complete":
        unfinished = sorted(task.number for task in tasks if task.status != "complete")
        if unfinished:
            raise PlanError(
                "lifecycle `complete` requires every task `complete` "
                f"(found non-complete: {unfinished})"
            )

    # --- conformance matrix ---------------------------------------------------
    matrix_block = next(block for block in section_blocks
                        if block.heading == "## " + CANONICAL_SECTIONS[2])
    matrix = _parse_matrix(matrix_block, tasks, lifecycle_status=front["status"])

    # --- interaction inventory ------------------------------------------------
    interactions_block = next(block for block in section_blocks
                              if block.heading == "## " + CANONICAL_SECTIONS[3])
    interactions = _parse_interactions(interactions_block)

    return Plan(
        spec_path=spec_path,
        spec_commit=front["spec_commit"],
        spec_blob=front["spec_blob"],
        base_commit=front["base_commit"],
        status=front["status"],
        tasks=tasks,
        matrix=matrix,
        interactions=interactions,
        _blocks=blocks,
    )


def _parse_task_block(number: int, title: str, block: Block) -> Task:
    body = block.lines[1:]
    values: Dict[str, List[str]] = {}
    order: List[str] = []
    current: Optional[str] = None
    for line in body:
        if not line.strip():
            continue
        match = FIELD_RE.fullmatch(line)
        if match:
            label, first = match.group(1), match.group(2)
            if label in values:
                raise PlanError(f"task {number} has a duplicate field `{label}`")
            if label not in ALL_FIELDS:
                raise PlanError(f"task {number} has an unknown field `{label}`")
            values[label] = [first]
            order.append(label)
            current = label
            continue
        if line.startswith((" ", "\t")):
            if current is None:
                raise PlanError(
                    f"task {number} body has an indented line before any field: {line!r}"
                )
            values[current].append(line)
            continue
        raise PlanError(f"task {number} body contains an ambiguous line: {line!r}")

    for required in REQUIRED_FIELDS:
        if required not in values:
            raise PlanError(f"task {number} requires exactly one `- {required}:` field")
    for label in order:
        logical = "\n".join(part.lstrip(" \t") for part in values[label])
        if label == "Documentation impact":
            continue
        if not logical.strip():
            raise PlanError(f"task {number} has an empty `- {label}:` field")
    for label in order:
        if label in STRUCTURED_FIELDS and len(values[label]) > 1:
            raise PlanError(
                f"task {number} `- {label}:` is a structured field and must not "
                "have continuation lines"
            )

    status = values["Status"][0].strip()
    if status not in TASK_STATUSES:
        raise PlanError(f"task {number} has invalid status `{status}`")

    dep_spans = _parse_dep_spans(
        values["Dependencies"][0], what=f"task {number}"
    )
    if "Priority" in values:
        raw_priority = values["Priority"][0].strip()
        if not PRIORITY_RE.fullmatch(raw_priority) or int(raw_priority) < 1:
            raise PlanError(
                f"task {number} priority must be a positive integer, got `{raw_priority}`"
            )
        priority = int(raw_priority)
    else:
        priority = number

    blocked_on = (
        "\n".join(part.lstrip(" \t") for part in values["Blocked on"])
        if "Blocked on" in values else None
    )
    if status == "blocked" and not blocked_on:
        raise PlanError(
            f"blocked task {number} must name an exact unresolved reference in `- Blocked on:`"
        )

    if "Write scopes" in values:
        raw_scopes = values["Write scopes"][0].strip()
        if raw_scopes.lower() == "none":
            write_scopes: List[str] = []
        else:
            items = [part.strip() for part in raw_scopes.split(",")]
            if not items or any(not item for item in items):
                raise PlanError(
                    f"task {number} has a malformed `- Write scopes:` list: {raw_scopes!r}"
                )
            seen_scopes: set = set()
            for item in items:
                if not WRITE_SCOPE_RE.fullmatch(item):
                    raise PlanError(
                        f"task {number} has an invalid write scope `{item}` "
                        "(must match `^[a-z][a-z0-9_-]*$`)"
                    )
                if item in seen_scopes:
                    raise PlanError(
                        f"task {number} has a duplicate write scope `{item}`"
                    )
                seen_scopes.add(item)
            write_scopes = items
    else:
        write_scopes = []
    fields = {
        label: "\n".join(part.lstrip(" \t") for part in values[label])
        for label in order
    }
    return Task(
        number=number,
        title=title,
        status=status,
        dependencies=[],
        priority=priority,
        blocked_on=blocked_on,
        fields=fields,
        field_order=order,
        write_scopes=write_scopes,
        dep_spans=dep_spans,
    )


def _validate_task_graph(tasks: List[Task]) -> None:
    numbers = {task.number for task in tasks}
    finals = [task.number for task in tasks if task.title == FINAL_AUDIT_TITLE]
    final_number = finals[0] if finals else None
    for task in tasks:
        unknown = sorted(set(task.dependencies) - numbers)
        if unknown:
            raise PlanError(f"task {task.number} references unknown dependencies: {unknown}")
        if task.number in task.dependencies:
            raise PlanError(f"task {task.number} cannot depend on itself")
        for dependency in task.dependencies:
            if dependency > task.number and task.number != final_number:
                raise PlanError(
                    f"task {task.number} dependencies must refer to earlier tasks "
                    f"(found forward dependency on Task {dependency})"
                )

    visiting: set = set()
    visited: set = set()

    def visit(number: int) -> None:
        if number in visiting:
            raise PlanError("task dependency graph contains a cycle")
        if number in visited:
            return
        visiting.add(number)
        task = next(task for task in tasks if task.number == number)
        for dependency in task.dependencies:
            visit(dependency)
        visiting.discard(number)
        visited.add(number)

    for number in numbers:
        visit(number)


def _validate_status_invariants(tasks: List[Task]) -> None:
    in_progress = [task.number for task in tasks if task.status == "in_progress"]
    if len(in_progress) > 1:
        raise PlanError("at most one task may be `in_progress`")


def _parse_matrix(block: Block, tasks: List[Task], lifecycle_status: str) -> List[MatrixRow]:
    body = "\n".join(block.lines[1:])
    lines = [line.strip() for line in body.splitlines() if line.strip()]
    table_lines = [line for line in lines if line.startswith("|")]
    if len(table_lines) < 3:
        raise PlanError("conformance matrix has no machine-checkable requirement rows")
    header = _table_cells(table_lines[0])
    if header != list(MATRIX_HEADER):
        raise PlanError(
            f"conformance matrix header must be exactly `{' | '.join(MATRIX_HEADER)}`"
        )
    separator = _table_cells(table_lines[1])
    if len(separator) != 5 or any(
        not re.fullmatch(r":?-{3,}:?", cell) for cell in separator
    ):
        raise PlanError("conformance matrix separator is malformed")

    task_statuses = {task.number: task.status for task in tasks}
    max_id = len(tasks)

    rows: List[MatrixRow] = []
    seen_ids: set = set()
    for line in table_lines[2:]:
        cells = _table_cells(line)
        if len(cells) != 5:
            raise PlanError(f"conformance row must have exactly five cells: {line}")
        requirement_id, spec_sections, classification, evidence, task_cell = cells
        if not MATRIX_ID_RE.fullmatch(requirement_id):
            raise PlanError(f"invalid conformance requirement ID: `{requirement_id}`")
        if requirement_id in seen_ids:
            raise PlanError(f"duplicate conformance requirement ID: `{requirement_id}`")
        seen_ids.add(requirement_id)
        if not spec_sections or not evidence:
            raise PlanError(f"conformance row {requirement_id} requires spec and evidence cells")
        if classification not in CLASSIFICATIONS:
            raise PlanError(
                f"conformance row {requirement_id} has invalid classification `{classification}`"
            )
        spans = _parse_dep_spans(
            task_cell, what=f"conformance row {requirement_id}", allow_empty=True
        )
        refs = _expand_spans(
            spans, max_id, what=f"conformance row {requirement_id}", kind="matrix"
        )
        if not refs and classification != "verified":
            raise PlanError(
                f"non-verified conformance row {requirement_id} must reference an existing task"
            )
        if classification == "verified":
            if not refs:
                raise PlanError(
                    f"verified conformance row {requirement_id} must reference a "
                    "completed task"
                )
            non_complete = sorted(
                number for number in refs if task_statuses[number] != "complete"
            )
            if non_complete:
                raise PlanError(
                    f"verified conformance row {requirement_id} references a task "
                    f"that is not complete: {non_complete}"
                )
        elif refs:
            complete_refs = sorted(
                number for number in refs if task_statuses[number] == "complete"
            )
            if len(complete_refs) == len(refs):
                raise PlanError(
                    f"non-verified conformance row {requirement_id} references only "
                    f"completed tasks ({complete_refs}); a pending/in_progress/blocked "
                    "task must own it"
                )
        rows.append(MatrixRow(
            requirement_id=requirement_id,
            spec_sections=spec_sections,
            classification=classification,
            evidence=evidence,
            tasks=refs,
        ))
    if not rows:
        raise PlanError("conformance matrix has no requirement rows")
    if lifecycle_status == "active" and any(
        row.classification == "verified" for row in rows
    ):
        raise PlanError(
            "an `active` plan may not contain a `verified` conformance row"
        )
    registry = _load_requirement_registry()
    extra_ids = sorted(rid for rid in seen_ids if rid not in registry)
    if extra_ids:
        raise PlanError(
            "conformance matrix has a requirement ID outside the \u00a724 registry: "
            f"{', '.join(extra_ids)}"
        )
    missing_ids = sorted(rid for rid in registry if rid not in seen_ids)
    if missing_ids:
        raise PlanError(
            "conformance matrix must cover every ID in the \u00a724 registry "
            f"(missing: {', '.join(missing_ids)})"
        )
    if lifecycle_status == "complete" and any(
        row.classification != "verified" for row in rows
    ):
        raise PlanError(
            "a `complete` plan requires every conformance row `verified`"
        )
    return rows


def _parse_interactions(block: Block) -> List[Interaction]:
    found: Dict[str, List[str]] = {}
    order: List[str] = []
    current: Optional[str] = None
    for line in block.lines[1:]:
        match = BOUNDARY_RE.fullmatch(line)
        if match:
            label = match.group(1)
            if label in found:
                raise PlanError(f"interaction inventory has a duplicate `{label}`")
            if not match.group(2).strip():
                raise PlanError(
                    f"interaction inventory `{label}` text must not be empty"
                )
            found[label] = [match.group(2)]
            order.append(label)
            current = label
            continue
        if not line.strip():
            continue
        if line.startswith((" ", "\t")):
            if current is None:
                raise PlanError(
                    f"interaction inventory has an indented line before a boundary: {line!r}"
                )
            found[current].append(line)
            continue
        raise PlanError(f"interaction inventory contains an ambiguous line: {line!r}")
    missing = [boundary for boundary in INTERACTION_BOUNDARIES if boundary not in found]
    if missing:
        raise PlanError(f"interaction inventory must cover `{', '.join(missing)}`")
    return [
        Interaction(
            boundary=label,
            text="\n".join(part.lstrip(" \t") for part in found[label]),
        )
        for label in order
    ]


def round_trip(text: str) -> str:
    """Parse, serialize, and re-parse; returns the canonical serialization."""
    first = parse_plan(text)
    serialized = first.serialize()
    second = parse_plan(serialized)
    if first.to_dict() != second.to_dict():
        raise PlanError("round-trip changed the parsed plan semantics")
    return serialized


def main(argv: Optional[List[str]] = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        prog="plan_parser",
        description="Deterministic `factory-plan/v1` plan parser.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_parse = sub.add_parser("parse", help="validate and report the parsed model")
    p_parse.add_argument("path")

    p_dump = sub.add_parser("dump", help="emit the deterministic JSON model")
    p_dump.add_argument("path")

    p_serialize = sub.add_parser("serialize", help="emit the canonical Markdown")
    p_serialize.add_argument("path")

    p_roundtrip = sub.add_parser("roundtrip", help="parse -> serialize -> parse")
    p_roundtrip.add_argument("path")

    p_transitions = sub.add_parser("transitions", help="print the allowed status transitions")

    args = parser.parse_args(argv)

    if args.command == "transitions":
        for current in sorted(ALLOWED_TRANSITIONS):
            to = sorted(ALLOWED_TRANSITIONS[current])
            print(f"{current} -> {', '.join(to) if to else '<none>'}")
        return 0

    path = Path(args.path)
    try:
        data = path.read_bytes()
    except OSError as exc:
        print(f"factory-plan: cannot read {path}: {exc}", file=sys.stderr)
        return 2

    try:
        plan = Plan.from_bytes(data)
    except PlanError as exc:
        print(f"factory-plan: {exc}", file=sys.stderr)
        return 1

    if args.command == "parse":
        print(
            f"factory-plan: ok schema={SCHEMA_NAME} tasks={len(plan.tasks)} "
            f"matrix={len(plan.matrix)} interactions={len(plan.interactions)}"
        )
    elif args.command == "dump":
        print(plan.dump_json())
    elif args.command == "serialize":
        sys.stdout.write(plan.serialize())
    elif args.command == "roundtrip":
        serialized = plan.serialize()
        if serialized.encode("utf-8") != data:
            print("factory-plan: roundtrip changed the plan bytes", file=sys.stderr)
            return 1
        try:
            rechecked = parse_plan(serialized)
        except PlanError as exc:
            print(f"factory-plan: roundtrip failed: {exc}", file=sys.stderr)
            return 1
        if rechecked.to_dict() != plan.to_dict():
            print("factory-plan: roundtrip changed the plan semantics", file=sys.stderr)
            return 1
        if serialized != plan.serialize():
            print("factory-plan: serialization is not idempotent", file=sys.stderr)
            return 1
        print("factory-plan: roundtrip byte-exact")
    return 0


if __name__ == "__main__":
    sys.exit(main())
