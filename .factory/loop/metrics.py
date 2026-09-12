"""Per-round campaign metrics for feedback-driven prompt tuning (spec §4.6).

Tracks per-round statistics and writes them to a JSONL log file.  The
planner receives a summary of historical metrics so it can adjust roles
for the next round (skip cry-wolf auditors, narrow developer scopes,
change model tiers).

Metrics tracked per round:
  * Study: which subagents ran, success/failure, duration.
  * Implementation: proposals accepted/rejected by integration, per developer.
  * Audit: findings per auditor, BLOCKERs upheld vs overturned, conflicts.
  * Repair: cycles used, whether resolved.
  * Overall: round duration, task outcome.

Auditor precision = upheld BLOCKERs / total BLOCKERs reported.
Developer rejection rate = rejected proposals / total proposals.
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from pathlib import Path
import json
import time
from typing import Any


@dataclass
class StudyMetric:
    name: str
    success: bool
    duration_s: float = 0.0


@dataclass
class DeveloperMetric:
    name: str
    success: bool
    proposals_accepted: int = 0
    proposals_rejected: int = 0
    duration_s: float = 0.0


@dataclass
class AuditorMetric:
    name: str
    success: bool
    findings_count: int = 0
    blockers_count: int = 0
    blockers_overturned: int = 0   # downgraded by conflict resolution
    duration_s: float = 0.0

    @property
    def precision(self) -> float:
        """Fraction of BLOCKERs that were upheld (not overturned)."""
        total = self.blockers_count
        if total == 0:
            return 1.0  # no false positives if no findings
        upheld = total - self.blockers_overturned
        return upheld / total


@dataclass
class RoundMetrics:
    round_number: int
    campaign_id: str
    task_id: int = 0
    task_title: str = ""
    duration_s: float = 0.0
    studies: list[StudyMetric] = field(default_factory=list)
    developers: list[DeveloperMetric] = field(default_factory=list)
    auditors: list[AuditorMetric] = field(default_factory=list)
    verification_attempts: int = 0
    verification_passed: bool = False
    repair_cycles: int = 0
    repair_resolved: bool = False
    conflicts_count: int = 0
    outcome: str = ""               # completed, blocked, failed, etc.

    def to_dict(self) -> dict:
        return asdict(self)


class MetricsLog:
    """Append-only JSONL log of round metrics.

    File: ``.factory-state/metrics.jsonl`` — one JSON object per line.
    """

    def __init__(self, path: str | Path):
        self.path = Path(path)

    def append(self, metrics: RoundMetrics) -> None:
        """Append a round's metrics to the JSONL log."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(json.dumps(metrics.to_dict()) + "\n")

    def load_all(self) -> list[dict]:
        """Load all historical metrics from the log."""
        if not self.path.exists():
            return []
        records = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        return records

    def summary(self, last_n: int = 5) -> str:
        """Produce a markdown summary of recent metrics for the planner.

        Includes:
          - Auditor precision over recent rounds.
          - Developer rejection rates.
          - Average repair cycles.
          - Recommendations for role adjustments.
        """
        records = self.load_all()
        if not records:
            return "No historical metrics available (first round)."

        recent = records[-last_n:]
        parts = [f"## Campaign Metrics Summary (last {len(recent)} rounds)\n"]

        # ─── Auditor precision ───
        auditor_stats: dict[str, dict[str, int]] = {}
        for r in recent:
            for a in r.get("auditors", []):
                name = a.get("name", "?")
                stats = auditor_stats.setdefault(name, {
                    "total": 0, "overturned": 0, "rounds": 0,
                })
                stats["total"] += a.get("blockers_count", 0)
                stats["overturned"] += a.get("blockers_overturned", 0)
                stats["rounds"] += 1

        if auditor_stats:
            parts.append("### Auditor Precision\n")
            parts.append(
                "| Auditor | BLOCKERs reported | Overturned | Precision | "
                "Rounds |\n"
                "|---|---|---|---|---|\n"
            )
            for name, s in sorted(auditor_stats.items()):
                upheld = s["total"] - s["overturned"]
                precision = upheld / s["total"] if s["total"] > 0 else 1.0
                parts.append(
                    f"| {name} | {s['total']} | {s['overturned']} | "
                    f"{precision:.0%} | {s['rounds']} |\n"
                )
            # Flag cry-wolf auditors.
            for name, s in auditor_stats.items():
                if s["total"] > 0:
                    precision = (s["total"] - s["overturned"]) / s["total"]
                    if precision < 0.5:
                        parts.append(
                            f"\n> **⚠ {name}** has low precision ({precision:.0%}). "
                            f"Consider tightening its prompt or skipping it "
                            f"next round via `roles_override`.\n"
                        )
            parts.append("")

        # ─── Developer rejection rates ───
        dev_stats: dict[str, dict[str, int]] = {}
        for r in recent:
            for d in r.get("developers", []):
                name = d.get("name", "?")
                stats = dev_stats.setdefault(name, {
                    "accepted": 0, "rejected": 0, "rounds": 0,
                })
                stats["accepted"] += d.get("proposals_accepted", 0)
                stats["rejected"] += d.get("proposals_rejected", 0)
                stats["rounds"] += 1

        if dev_stats:
            parts.append("### Developer Proposal Acceptance\n")
            parts.append(
                "| Developer | Accepted | Rejected | Rejection rate | "
                "Rounds |\n"
                "|---|---|---|---|---|\n"
            )
            for name, s in sorted(dev_stats.items()):
                total = s["accepted"] + s["rejected"]
                rej_rate = s["rejected"] / total if total > 0 else 0.0
                parts.append(
                    f"| {name} | {s['accepted']} | {s['rejected']} | "
                    f"{rej_rate:.0%} | {s['rounds']} |\n"
                )
            for name, s in dev_stats.items():
                total = s["accepted"] + s["rejected"]
                if total > 0:
                    rej_rate = s["rejected"] / total
                    if rej_rate > 0.5:
                        parts.append(
                            f"\n> **⚠ {name}** has high rejection rate "
                            f"({rej_rate:.0%}). Consider narrowing scope or "
                            f"adding more specific instructions.\n"
                        )
            parts.append("")

        # ─── Repair cycle stats ───
        repair_rounds = [r for r in recent if r.get("repair_cycles", 0) > 0]
        if repair_rounds:
            avg_repair = sum(r["repair_cycles"] for r in repair_rounds) / len(repair_rounds)
            resolved = sum(1 for r in repair_rounds if r.get("repair_resolved"))
            parts.append("### Repair Cycle Stats\n")
            parts.append(
                f"- Rounds with repairs: {len(repair_rounds)}\n"
                f"- Average repair cycles: {avg_repair:.1f}\n"
                f"- Resolved by repair: {resolved}/{len(repair_rounds)}\n"
            )
            parts.append("")

        # ─── Round outcomes ───
        outcomes: dict[str, int] = {}
        for r in recent:
            o = r.get("outcome", "unknown")
            outcomes[o] = outcomes.get(o, 0) + 1
        parts.append("### Round Outcomes\n")
        for o, count in sorted(outcomes.items()):
            parts.append(f"- {o}: {count}\n")

        return "\n".join(parts)


# ─── Helper: build metrics from phase results ────────────────────────

def build_round_metrics(
    round_num: int,
    campaign_id: str,
    task: Any,
    start_time: float,
    study_results: list = None,
    dev_results: list = None,
    audit_report: Any = None,
    verification_attempts: int = 0,
    verification_passed: bool = False,
    repair_cycles: int = 0,
    repair_resolved: bool = False,
    outcome: str = "",
) -> RoundMetrics:
    """Build a RoundMetrics object from phase results."""
    from .parallel import SubagentResult, AuditReport

    metrics = RoundMetrics(
        round_number=round_num,
        campaign_id=campaign_id,
        task_id=getattr(task, "id", 0),
        task_title=getattr(task, "title", ""),
        duration_s=time.time() - start_time,
        verification_attempts=verification_attempts,
        verification_passed=verification_passed,
        repair_cycles=repair_cycles,
        repair_resolved=repair_resolved,
        outcome=outcome,
    )

    # Study metrics
    if study_results:
        for r in study_results:
            metrics.studies.append(StudyMetric(
                name=r.name, success=r.success,
            ))

    # Developer metrics (simplified — we don't track per-proposal yet)
    if dev_results:
        for r in dev_results:
            metrics.developers.append(DeveloperMetric(
                name=r.name, success=r.success,
            ))

    # Auditor metrics
    if audit_report and isinstance(audit_report, AuditReport):
        # Per-auditor stats from the findings
        auditor_findings: dict[str, dict[str, int]] = {}
        for f in audit_report.findings:
            stats = auditor_findings.setdefault(f.auditor, {
                "findings": 0, "blockers": 0, "overturned": 0,
            })
            stats["findings"] += 1
            if f.severity == "BLOCKER":
                stats["blockers"] += 1

        # Track overturned blockers from conflicts
        for c in audit_report.conflicts:
            if c.loser in auditor_findings:
                auditor_findings[c.loser]["overturned"] += 1

        for name, stats in auditor_findings.items():
            metrics.auditors.append(AuditorMetric(
                name=name,
                success=True,
                findings_count=stats["findings"],
                blockers_count=stats["blockers"],
                blockers_overturned=stats["overturned"],
            ))

        metrics.conflicts_count = len(audit_report.conflicts)

    return metrics