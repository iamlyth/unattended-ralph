"""Accumulating issue tracker for cross-round audit finding deduplication.

Maintains ``.factory/issues.json`` — a persistent record of audit findings
across rounds.  When an auditor flags the same issue in multiple rounds,
the tracker increments its ``repeat_count``.  If an issue recurs more than
``escalation_threshold`` times, it is marked ``escalated`` and the campaign
should stop rather than re-report.

The tracker is also used by the stale-round detection: if the same issues
keep recurring across rounds, the repair mechanism isn't working.

Issue matching heuristic: two findings match if they come from the same
auditor AND share at least one file reference.  This is conservative —
it may miss some duplicates — but it avoids false matches across
unrelated code areas.
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from pathlib import Path
import json
import os
import tempfile
from typing import Any


@dataclass
class Issue:
    """A single tracked issue across rounds."""
    id: str                         # issue-001, issue-002, etc.
    first_round: int
    last_round: int
    repeat_count: int = 1
    auditor: str = ""
    severity: str = "BLOCKER"
    file_refs: list[str] = field(default_factory=list)
    description: str = ""           # first 200 chars of finding text
    status: str = "open"            # open, resolved, escalated
    resolved_round: int | None = None

    def matches(self, auditor: str, file_refs: list[str]) -> bool:
        """Check if a new finding matches this issue."""
        if self.auditor != auditor:
            return False
        # Match if any file ref overlaps.
        if not self.file_refs and not file_refs:
            return True  # both empty — match by auditor only
        if not self.file_refs or not file_refs:
            return False
        return bool(set(self.file_refs) & set(file_refs))


class IssueTracker:
    """Persistent issue tracker backed by ``.factory/issues.json``."""

    def __init__(self, path: str | Path, escalation_threshold: int = 3):
        self.path = Path(path)
        self.escalation_threshold = escalation_threshold
        self.issues: list[Issue] = []
        self._next_id = 1
        self._load()

    def _load(self) -> None:
        """Load issues from JSON file."""
        if not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return
        for issue_data in data.get("issues", []):
            self.issues.append(Issue(**issue_data))
        # Determine next ID.
        max_num = 0
        for issue in self.issues:
            try:
                num = int(issue.id.split("-")[-1])
                max_num = max(max_num, num)
            except (ValueError, IndexError):
                pass
        self._next_id = max_num + 1

    def save(self) -> None:
        """Save issues to JSON file atomically."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        data = {"issues": [asdict(i) for i in self.issues]}
        fd, tmp = tempfile.mkstemp(
            dir=str(self.path.parent), prefix=".issues-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
                f.write("\n")
            os.replace(tmp, str(self.path))
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def add_findings(
        self,
        findings: list[Any],
        round_num: int,
    ) -> list[Issue]:
        """Add audit findings from a round to the tracker.

        For each BLOCKER finding:
          - If it matches an existing open issue, increment repeat_count.
          - If it's new, create a new issue.
          - If repeat_count >= escalation_threshold, mark as escalated.

        Findings that are NOT present in this round but were open are
        marked as resolved.

        Returns the list of issues that were escalated this round.
        """
        escalated = []
        new_issues = []

        # Track which issues were seen this round.
        seen_issue_ids: set[str] = set()

        for finding in findings:
            if not hasattr(finding, "auditor"):
                continue
            if finding.severity != "BLOCKER":
                continue

            # Try to match an existing open issue.
            matched = None
            for issue in self.issues:
                if issue.status != "open":
                    continue
                if issue.matches(finding.auditor, finding.file_refs):
                    matched = issue
                    break

            if matched:
                matched.repeat_count += 1
                matched.last_round = round_num
                seen_issue_ids.add(matched.id)
                if (matched.repeat_count >= self.escalation_threshold
                        and matched.status != "escalated"):
                    matched.status = "escalated"
                    escalated.append(matched)
            else:
                # New issue.
                issue_id = f"issue-{self._next_id:03d}"
                self._next_id += 1
                new_issue = Issue(
                    id=issue_id,
                    first_round=round_num,
                    last_round=round_num,
                    repeat_count=1,
                    auditor=finding.auditor,
                    severity="BLOCKER",
                    file_refs=finding.file_refs,
                    description=finding.text[:200],
                )
                self.issues.append(new_issue)
                new_issues.append(new_issue)
                seen_issue_ids.add(issue_id)

        # Mark open issues not seen this round as resolved.
        for issue in self.issues:
            if (issue.status == "open"
                    and issue.id not in seen_issue_ids
                    and issue.last_round < round_num):
                issue.status = "resolved"
                issue.resolved_round = round_num

        self.save()
        return escalated

    @property
    def open_issues(self) -> list[Issue]:
        """Issues that are still open."""
        return [i for i in self.issues if i.status == "open"]

    @property
    def escalated_issues(self) -> list[Issue]:
        """Issues that have been escalated."""
        return [i for i in self.issues if i.status == "escalated"]

    @property
    def has_escalations(self) -> bool:
        """Whether any issues have been escalated."""
        return len(self.escalated_issues) > 0

    def summary(self) -> str:
        """Produce a markdown summary for the planner."""
        if not self.issues:
            return ""

        parts = ["## Issue Tracker\n"]
        parts.append(f"Total issues tracked: {len(self.issues)}\n")

        # Open issues
        open_issues = self.open_issues
        if open_issues:
            parts.append(f"\n### Open Issues ({len(open_issues)})\n")
            for issue in open_issues:
                parts.append(
                    f"- **{issue.id}** ({issue.auditor}, "
                    f"rounds {issue.first_round}–{issue.last_round}, "
                    f"repeated {issue.repeat_count}×): "
                    f"{issue.description[:100]}\n"
                    f"  Files: {', '.join(issue.file_refs) or 'none'}\n"
                )

        # Escalated issues
        escalated = self.escalated_issues
        if escalated:
            parts.append(f"\n### ⚠ Escalated Issues ({len(escalated)})\n")
            parts.append(
                "These issues have recurred across multiple rounds and "
                "the repair mechanism has not resolved them. Consider "
                "splitting the task, changing the approach, or escalating "
                "to human review.\n"
            )
            for issue in escalated:
                parts.append(
                    f"- **{issue.id}** ({issue.auditor}): "
                    f"repeated {issue.repeat_count}× across rounds "
                    f"{issue.first_round}–{issue.last_round}\n"
                    f"  {issue.description[:150]}\n"
                )

        # Recently resolved
        resolved = [i for i in self.issues if i.status == "resolved"]
        if resolved:
            recent_resolved = resolved[-3:]
            parts.append(f"\n### Recently Resolved ({len(recent_resolved)})\n")
            for issue in recent_resolved:
                parts.append(
                    f"- **{issue.id}** ({issue.auditor}): "
                    f"resolved in round {issue.resolved_round} "
                    f"(was open rounds {issue.first_round}–{issue.last_round})\n"
                )

        return "\n".join(parts)


# ─── Round scratchpad ─────────────────────────────────────────────────

def write_round_scratchpad(
    root: str | Path,
    round_num: int,
    campaign_id: str,
    task: Any,
    plan_summary: str = "",
    implementation_summary: str = "",
    verification_summary: str = "",
    audit_summary: str = "",
    repair_summary: str = "",
    outcome: str = "",
    issues_summary: str = "",
) -> Path:
    """Write a structured round summary to ``.factory/rounds/N.md``.

    This file is read by the next round's study agents and planner to
    provide iteration continuity across fresh-context invocations.
    """
    root = Path(root)
    rounds_dir = root / ".factory" / "rounds"
    rounds_dir.mkdir(parents=True, exist_ok=True)
    path = rounds_dir / f"{round_num}.md"

    parts = [
        f"# Round {round_num} Summary",
        f"",
        f"Campaign: {campaign_id}",
        f"Task: {getattr(task, 'id', '?')} — {getattr(task, 'title', '?')}",
        f"Outcome: {outcome}",
        f"",
        f"## Planning",
        f"{plan_summary or 'No planning summary recorded.'}",
        f"",
        f"## Implementation",
        f"{implementation_summary or 'No implementation summary recorded.'}",
        f"",
        f"## Verification",
        f"{verification_summary or 'No verification summary recorded.'}",
        f"",
        f"## Audit",
        f"{audit_summary or 'No audit summary recorded.'}",
        f"",
    ]

    if repair_summary:
        parts.append(f"## Repair\n{repair_summary}\n")

    if issues_summary:
        parts.append(f"## Issues\n{issues_summary}\n")

    path.write_text("\n".join(parts), encoding="utf-8")
    return path


def read_round_scratchpads(
    root: str | Path,
    last_n: int = 3,
    min_round: int = 1,
) -> str:
    """Read recent round scratchpads and format them for the planner.

    Returns a markdown string with the last ``last_n`` round summaries.
    Returns empty string if no scratchpads exist.
    """
    root = Path(root)
    rounds_dir = root / ".factory" / "rounds"
    if not rounds_dir.exists():
        return ""

    # Find round files.
    round_files = sorted(rounds_dir.glob("*.md"))
    if not round_files:
        return ""

    # Read the last N.
    recent = round_files[-last_n:]
    parts = []
    for path in recent:
        content = path.read_text(encoding="utf-8")
        parts.append(content)

    if not parts:
        return ""

    return "\n\n---\n\n".join(parts)