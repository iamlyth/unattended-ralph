"""Minimal plan parser for the Ralph factory.

Parses the canonical implementation plan (markdown) into a Plan object.
Fails closed on malformed input per spec §5.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import re

VALID_STATUSES = {"pending", "in_progress", "completed", "blocked"}
REQUIRED_FIELDS = ("Title", "Status", "Acceptance", "Verification")
FINAL_AUDIT_TITLE = "Final documentation and specification audit"


@dataclass
class Task:
    id: int
    title: str
    status: str
    dependencies: list[int] = field(default_factory=list)
    acceptance: str = ""
    verification: str = ""
    runner: str | None = None
    evidence: str | None = None


@dataclass
class Plan:
    spec_path: str = ""
    spec_commit: str = ""
    base_commit: str = ""
    status: str = "active"
    tasks: list[Task] = field(default_factory=list)

    @property
    def task_ids(self) -> set[int]:
        return {t.id for t in self.tasks}

    def get_task(self, task_id: int) -> Task | None:
        for t in self.tasks:
            if t.id == task_id:
                return t
        return None


def _parse_front_matter(text: str) -> tuple[dict, str]:
    """Extract YAML-like front matter between --- delimiters."""
    if not text.startswith("---"):
        return {}, text
    end = text.find("\n---", 3)
    if end == -1:
        raise ValueError("Plan has unclosed front matter (no closing ---)")
    fm_text = text[3:end].strip()
    body = text[end + 4:].lstrip("\n")
    fm: dict[str, str] = {}
    for line in fm_text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if ":" not in line:
            raise ValueError(f"Invalid front matter line: {line!r}")
        key, _, val = line.partition(":")
        fm[key.strip()] = val.strip()
    return fm, body


def _parse_field(line: str) -> tuple[str, str]:
    """Parse a 'Key: value' line, returning (key, value)."""
    key, _, val = line.partition(":")
    return key.strip(), val.strip()


def _parse_deps(val: str) -> list[int]:
    """Parse comma-separated dependency numbers."""
    val = val.strip()
    if not val or val.lower() in ("none", "n/a", "-"):
        return []
    parts = [p.strip() for p in val.split(",") if p.strip()]
    try:
        return [int(p) for p in parts]
    except ValueError:
        raise ValueError(f"Invalid dependencies (non-integer): {val!r}")


def parse(plan_path: str | Path) -> Plan:
    """Parse the plan file and return a Plan object. Raises ValueError on malformed input."""
    path = Path(plan_path)
    if not path.exists():
        raise ValueError(f"Plan file not found: {plan_path}")
    text = path.read_text(encoding="utf-8")
    fm, body = _parse_front_matter(text)

    plan = Plan(
        spec_path=fm.get("spec_path", ""),
        spec_commit=fm.get("spec_commit", ""),
        base_commit=fm.get("base_commit", ""),
        status=fm.get("status", "active"),
    )

    # Parse tasks: ## Task N: Title
    task_pattern = re.compile(r"^##\s+Task\s+(\d+)\s*:\s*(.*)$", re.MULTILINE)
    task_starts = list(task_pattern.finditer(body))

    if not task_starts:
        raise ValueError("Plan has no tasks (expected '## Task N: Title' headings)")

    for i, match in enumerate(task_starts):
        task_id = int(match.group(1))
        task_title = match.group(2).strip()

        # Field block is from after the heading to the next task heading or EOF
        start = match.end()
        end = task_starts[i + 1].start() if i + 1 < len(task_starts) else len(body)
        block = body[start:end].strip()

        # Parse fields
        fields: dict[str, str] = {}
        for line in block.splitlines():
            line = line.rstrip()
            if not line or line.startswith("#"):
                continue
            if ":" in line:
                k, v = _parse_field(line)
                if k in ("Title", "Status", "Dependencies", "Acceptance",
                          "Verification", "Runner", "Evidence"):
                    fields[k] = v

        # Validate required fields
        for req in REQUIRED_FIELDS:
            if req not in fields or not fields[req]:
                raise ValueError(
                    f"Task {task_id} missing required field: {req}")

        status = fields["Status"].lower()
        if status not in VALID_STATUSES:
            raise ValueError(
                f"Task {task_id} has invalid status: {status!r} "
                f"(valid: {VALID_STATUSES})")

        runner = fields.get("Runner", "").strip()
        if runner.lower() in ("none", "n/a", "-", ""):
            runner = None

        evidence = fields.get("Evidence", "").strip()
        if evidence.lower() in ("none", "n/a", "-", ""):
            evidence = None

        task = Task(
            id=task_id,
            title=task_title,
            status=status,
            dependencies=_parse_deps(fields.get("Dependencies", "")),
            acceptance=fields["Acceptance"],
            verification=fields["Verification"],
            runner=runner,
            evidence=evidence,
        )
        plan.tasks.append(task)

    _validate(plan)
    return plan


def _validate(plan: Plan) -> None:
    """Validate plan invariants per spec §5.3."""
    if not plan.tasks:
        raise ValueError("Plan has no tasks")

    ids = [t.id for t in plan.tasks]

    # Unique task numbers
    if len(ids) != len(set(ids)):
        seen: set[int] = set()
        for tid in ids:
            if tid in seen:
                raise ValueError(f"Duplicate task number: {tid}")
            seen.add(tid)

    # Contiguous and increasing
    for i, tid in enumerate(ids):
        if tid != i + 1:
            raise ValueError(
                f"Task numbers must be contiguous starting at 1; "
                f"found {tid} at position {i}")

    # Self-dependencies
    for t in plan.tasks:
        if t.id in t.dependencies:
            raise ValueError(f"Task {t.id} has self-dependency")

    # Unknown dependencies
    valid_ids = set(ids)
    for t in plan.tasks:
        for dep in t.dependencies:
            if dep not in valid_ids:
                raise ValueError(
                    f"Task {t.id} depends on unknown task {dep}")

    # Final audit task
    last = plan.tasks[-1]
    if FINAL_AUDIT_TITLE.lower() not in last.title.lower():
        raise ValueError(
            f"Final task must be '{FINAL_AUDIT_TITLE}', "
            f"got: {last.title!r}")

    # Final task depends on every other task
    expected_deps = set(ids[:-1])
    actual_deps = set(last.dependencies)
    if expected_deps != actual_deps:
        missing = expected_deps - actual_deps
        extra = actual_deps - expected_deps
        if missing:
            raise ValueError(
                f"Final audit task missing dependencies: "
                f"{sorted(missing)}")
        if extra:
            raise ValueError(
                f"Final audit task has unexpected dependencies: "
                f"{sorted(extra)}")


def dump(plan: Plan) -> str:
    """Render a Plan back to markdown. Round-trips with parse()."""
    lines: list[str] = []
    lines.append("---")
    lines.append(f"spec_path: {plan.spec_path}")
    lines.append(f"spec_commit: {plan.spec_commit}")
    lines.append(f"base_commit: {plan.base_commit}")
    lines.append(f"status: {plan.status}")
    lines.append("---")
    lines.append("")

    for task in plan.tasks:
        deps = ", ".join(str(d) for d in task.dependencies) if task.dependencies else "none"
        runner = task.runner or "none"
        evidence = task.evidence or "none"
        lines.append(f"## Task {task.id}: {task.title}")
        lines.append(f"Title: {task.title}")
        lines.append(f"Status: {task.status}")
        lines.append(f"Dependencies: {deps}")
        lines.append(f"Acceptance: {task.acceptance}")
        lines.append(f"Verification: {task.verification}")
        lines.append(f"Runner: {runner}")
        lines.append(f"Evidence: {evidence}")
        lines.append("")

    return "\n".join(lines) + "\n"