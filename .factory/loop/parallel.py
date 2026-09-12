"""Parallel subagent execution for factory campaign phases (spec §7).

Each subagent is a ``pi2`` subprocess invoked with a role prompt and
optional context.  Subagents run concurrently via
``ThreadPoolExecutor``; their outputs are collected and returned to the
orchestrator for the next phase.

Three patterns:
  * **Study** — read-only subagents that analyse the codebase and produce
    reports.  Fully parallel, no side effects.
  * **Development** — subagents that edit files within their assigned
    paths.  Parallel as long as areas don't overlap; the integration
    developer commits after all developers finish.
  * **Audit** — read-only subagents that review the final codebase.
    Fully parallel, no side effects.  Findings are structured into an
    ``AuditReport`` with conflict resolution and repair instructions.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import re
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

# ─── Data structures ──────────────────────────────────────────────────

@dataclass
class SubagentResult:
    """Output of a single subagent invocation."""
    name: str
    success: bool
    stdout: str
    stderr: str
    exit_code: int
    prompt_path: str = ""


@dataclass
class StudyConfig:
    """A study subagent definition from roles.toml."""
    name: str
    prompt: str
    description: str = ""
    path: str = ""      # source directory to focus on (optional)


@dataclass
class DeveloperConfig:
    """A developer subagent definition from roles.toml."""
    name: str
    prompt: str
    description: str = ""
    paths: list[str] = field(default_factory=list)


@dataclass
class AuditorConfig:
    """An auditor subagent definition from roles.toml."""
    name: str
    prompt: str
    description: str = ""


# ─── Single subagent invocation ──────────────────────────────────────

def invoke_subagent(
    prompt_path: str,
    context: str,
    provider: str,
    model: str,
    timeout: int,
    cwd: str | Path | None = None,
    approve: bool = True,
) -> tuple[str, int, str, str]:
    """Invoke a single subagent via pi2.

    Returns ``(name, exit_code, stdout, stderr)``.
    """
    name = Path(prompt_path).stem
    try:
        prompt_text = Path(prompt_path).read_text(encoding="utf-8")
    except OSError as exc:
        return name, 127, "", f"cannot read prompt {prompt_path}: {exc}"

    full_input = prompt_text
    if context:
        full_input += "\n\n---\n\n## Context\n\n" + context

    cmd = [
        "pi2", "--provider", provider, "--model", model,
        "--print", "--no-session",
    ]
    if approve:
        cmd.append("--approve")

    try:
        proc = subprocess.run(
            cmd,
            input=full_input,
            capture_output=True,
            text=True,
            cwd=str(cwd) if cwd else None,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return name, 124, "", f"subagent {name} timed out after {timeout}s"
    except OSError as exc:
        return name, 127, "", f"subagent {name} failed: {exc}"

    return name, proc.returncode, proc.stdout or "", proc.stderr or ""


# ─── Parallel execution ──────────────────────────────────────────────

def _resolve_model(sa: dict, default_model: str) -> str:
    """Resolve which model to use for a subagent.

    Priority: subagent's ``model`` field > ``default_model``.
    """
    return sa.get("model") or default_model


def _resolve_timeout(sa: dict, default_timeout: int) -> int:
    """Resolve timeout for a subagent."""
    return int(sa.get("timeout", default_timeout))


def run_parallel(
    subagents: list[dict[str, Any]],
    context_fn,           # callable(name, sa) -> str  (context per subagent)
    provider: str,
    model: str,
    timeout: int,
    cwd: str | Path | None = None,
    approve: bool = True,
    max_workers: int = 6,
) -> list[SubagentResult]:
    """Launch multiple subagents in parallel.

    ``context_fn(name, sa)`` is called for each subagent to produce its
    context string.  Each subagent may specify its own ``model`` and
    ``timeout`` in its dict; these override the defaults.  Returns
    results in completion order.
    """
    results: list[SubagentResult] = []

    if not subagents:
        return results

    workers = min(max_workers, len(subagents))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        future_map = {}
        for sa in subagents:
            name = sa.get("name", "unnamed")
            prompt = sa.get("prompt", "")
            ctx = context_fn(name, sa) if context_fn else ""
            sa_model = _resolve_model(sa, model)
            sa_timeout = _resolve_timeout(sa, timeout)
            fut = pool.submit(
                invoke_subagent,
                prompt, ctx, provider, sa_model, sa_timeout, cwd, approve,
            )
            future_map[fut] = name

        for fut in as_completed(future_map):
            name = future_map[fut]
            try:
                _name, exit_code, stdout, stderr = fut.result()
            except Exception as exc:
                results.append(SubagentResult(
                    name=name, success=False, stdout="",
                    stderr=str(exc), exit_code=1,
                ))
            else:
                results.append(SubagentResult(
                    name=name,
                    success=exit_code == 0,
                    stdout=stdout,
                    stderr=stderr,
                    exit_code=exit_code,
                ))

    return results


# ─── Report assembly ──────────────────────────────────────────────────

def assemble_reports(results: list[SubagentResult]) -> str:
    """Concatenate subagent outputs into a single report string."""
    parts = []
    for r in results:
        header = f"## {r.name} Report"
        if r.success:
            parts.append(f"{header}\n\n{r.stdout}")
        else:
            parts.append(
                f"{header}\n\n"
                f"**Subagent failed (exit {r.exit_code})**\n\n"
                f"stdout:\n{r.stdout}\n\n"
                f"stderr:\n{r.stderr}\n"
            )
    return "\n\n---\n\n".join(parts)


def assemble_developer_outputs(results: list[SubagentResult]) -> str:
    """Concatenate developer proposals for the integration developer."""
    parts = []
    for r in results:
        header = f"### Developer: {r.name}"
        if r.success:
            parts.append(f"{header}\n\n{r.stdout}")
        else:
            parts.append(
                f"{header}\n\n"
                f"**Developer failed (exit {r.exit_code})**\n\n"
                f"stderr:\n{r.stderr}\n"
            )
    return "\n\n---\n\n".join(parts)


# ─── Audit structures ─────────────────────────────────────────────────

# Priority order: higher index = higher priority.  When two auditors
# conflict on the same file, the higher-priority auditor's finding wins.
# Security and functional correctness are never sacrificed for efficiency
# or style.
AUDITOR_PRIORITY = [
    "linting",           # 0 — lowest
    "efficiency",        # 1
    "compatibility",     # 2
    "spec-compliance",   # 3
    "functional",        # 4
    "security",          # 5 — highest
]


def _auditor_priority(name: str) -> int:
    """Return priority index for an auditor name (higher = higher priority)."""
    name_lower = name.lower().replace("_", "-")
    for i, p in enumerate(AUDITOR_PRIORITY):
        if p in name_lower:
            return i
    return 0  # unknown auditors get lowest priority


@dataclass
class AuditFinding:
    """A single finding from one auditor."""
    auditor: str
    severity: str           # BLOCKER, WARN, INFO
    file_refs: list[str]    # file paths mentioned in the finding
    text: str               # the finding text (may be multi-line)


@dataclass
class AuditConflict:
    """A conflict between two auditors on the same file area."""
    file_ref: str
    winner: str             # auditor name (higher priority)
    loser: str              # auditor name (lower priority)
    note: str               # explanation


@dataclass
class AuditReport:
    """Structured audit report with conflict resolution."""
    findings: list[AuditFinding] = field(default_factory=list)
    blockers: list[AuditFinding] = field(default_factory=list)
    conflicts: list[AuditConflict] = field(default_factory=list)
    has_blockers: bool = False
    raw_report: str = ""           # assembled markdown for human reading

    @property
    def repair_instructions(self) -> str:
        """Markdown listing only BLOCKER findings for the developer."""
        if not self.blockers:
            return ""
        parts = ["## BLOCKER Findings Requiring Repair\n"]
        for f in self.blockers:
            parts.append(f"### {f.auditor} (BLOCKER)\n")
            if f.file_refs:
                parts.append(f"**Files:** {', '.join(f.file_refs)}\n")
            parts.append(f"{f.text}\n")
        if self.conflicts:
            parts.append("### Conflict Resolution Notes\n")
            for c in self.conflicts:
                parts.append(
                    f"- `{c.file_ref}`: {c.winner} overrode {c.loser}. "
                    f"{c.note}\n"
                )
        return "\n".join(parts)


# ─── Audit parsing ───────────────────────────────────────────────────

# Regex for file-like references in auditor output.  Single capture group
# so re.findall returns strings, not tuples.
_FILE_RE = re.compile(
    r'(?<!\w)(?:(?:src|tests|scripts|data|include|lib|bin|docs)/'
    r'[\w/]+\.(?:c|h|py|sh|md|toml|yaml|yml|json|txt)'
    r'|CMakeLists\.txt|AGENTS\.md)'
)


def _extract_file_refs(text: str) -> list[str]:
    """Extract file path references from a block of text."""
    return list(dict.fromkeys(_FILE_RE.findall(text)))  # dedup, preserve order


def _extract_findings(auditor_name: str, output: str) -> list[AuditFinding]:
    """Parse an auditor's output into structured findings.

    Heuristic: split on blank-line-separated blocks that contain
    BLOCKER, WARN, or INFO keywords.  Each block becomes one finding.
    """
    findings = []
    # Split on headers or severity markers.
    blocks = re.split(r'\n(?=#{1,4}\s|\*\*?(?:BLOCKER|WARN|INFO))', output)
    for block in blocks:
        block = block.strip()
        if not block:
            continue
        upper = block.upper()
        if "BLOCKER" in upper:
            severity = "BLOCKER"
        elif "WARN" in upper:
            severity = "WARN"
        elif "INFO" in upper or "NOTE" in upper:
            severity = "INFO"
        else:
            # No explicit severity marker — treat the whole output as one INFO.
            if not findings and len(blocks) <= 2:
                severity = "INFO"
            else:
                continue
        file_refs = _extract_file_refs(block)
        findings.append(AuditFinding(
            auditor=auditor_name,
            severity=severity,
            file_refs=file_refs,
            text=block[:2000],  # cap individual finding size
        ))
    return findings


def _detect_and_resolve_conflicts(
    findings: list[AuditFinding],
) -> tuple[list[AuditFinding], list[AuditConflict]]:
    """Detect conflicts between auditors on the same file and resolve by priority.

    When two BLOCKER findings from different auditors reference the same
    file, the lower-priority auditor's finding is downgraded to WARN and
    a conflict note is recorded.

    Returns ``(resolved_findings, conflicts)``.
    """
    conflicts = []
    blockers_by_file: dict[str, list[AuditFinding]] = {}

    for f in findings:
        if f.severity != "BLOCKER":
            continue
        for ref in f.file_refs:
            blockers_by_file.setdefault(ref, []).append(f)

    for file_ref, refs in blockers_by_file.items():
        if len(refs) < 2:
            continue
        # Sort by priority (highest first).
        ranked = sorted(refs, key=lambda f: _auditor_priority(f.auditor),
                        reverse=True)
        winner = ranked[0]
        for loser in ranked[1:]:
            # Downgrade the loser's finding from BLOCKER to WARN.
            loser.severity = "WARN"
            conflicts.append(AuditConflict(
                file_ref=file_ref,
                winner=winner.auditor,
                loser=loser.auditor,
                note=(
                    f"{winner.auditor} (priority "
                    f"{_auditor_priority(winner.auditor)}) overrode "
                    f"{loser.auditor} (priority "
                    f"{_auditor_priority(loser.auditor)}) on {file_ref}"
                ),
            ))

    # Rebuild blockers list (only findings still at BLOCKER severity).
    resolved_blockers = [f for f in findings if f.severity == "BLOCKER"]
    return resolved_blockers, conflicts


def assemble_audit_findings(results: list[SubagentResult]) -> AuditReport:
    """Assemble auditor results into a structured AuditReport.

    Parses each auditor's output for findings, detects cross-auditor
    conflicts, resolves them by priority, and returns a report with
    repair instructions for the developer.
    """
    all_findings: list[AuditFinding] = []
    raw_parts = []

    for r in results:
        header = f"## {r.name} Audit"
        body = r.stdout if r.success else (
            f"**Auditor failed (exit {r.exit_code})**\n\n{r.stderr}"
        )
        raw_parts.append(f"{header}\n\n{body}")

        # If the auditor process exited non-zero, treat as BLOCKER.
        if r.exit_code != 0 and not body.upper().count("BLOCKER"):
            all_findings.append(AuditFinding(
                auditor=r.name, severity="BLOCKER",
                file_refs=[],
                text=f"Auditor exited with code {r.exit_code}.\n{body[:1000]}",
            ))
        else:
            all_findings.extend(_extract_findings(r.name, body))

    # Resolve conflicts and get final blockers list.
    blockers, conflicts = _detect_and_resolve_conflicts(all_findings)

    return AuditReport(
        findings=all_findings,
        blockers=blockers,
        conflicts=conflicts,
        has_blockers=len(blockers) > 0,
        raw_report="\n\n---\n\n".join(raw_parts),
    )


def build_repair_context(
    report: AuditReport,
    verification_output: str = "",
    repair_attempt: int = 1,
) -> str:
    """Build context string for a repair-cycle developer invocation.

    Includes BLOCKER findings, conflict resolution notes, and the
    verification output from the previous attempt so the developer can
    diagnose what went wrong.
    """
    parts = [f"## Repair Cycle {repair_attempt}\n"]
    parts.append(
        "The previous implementation attempt was audited and BLOCKER "
        "issues were found. You must fix these issues.\n"
    )
    parts.append(report.repair_instructions)

    if verification_output:
        parts.append("\n## Verification Output (Previous Attempt)\n")
        parts.append(
            "This is the stdout/stderr from the verification command that "
            "ran after the previous implementation. Use it to diagnose "
            "integration issues.\n"
        )
        parts.append(f"```\n{verification_output[:8000]}\n```\n")

    if report.conflicts:
        parts.append("\n## Auditor Conflict Resolution\n")
        parts.append(
            "Some auditors disagreed. The following conflicts were "
            "resolved by priority (security > functional > spec > "
            "compatibility > efficiency > linting). Lower-priority "
            "BLOCKERs were downgraded to WARN. Review these to ensure "
            "the resolution was correct.\n"
        )
        for c in report.conflicts:
            parts.append(f"- {c.note}\n")

    return "\n".join(parts)


# ─── Auto-discovery ──────────────────────────────────────────────────

def discover_subsystems(root: str | Path, src_dirs: list[str] | None = None,
                        min_files: int = 3) -> list[dict]:
    """Scan source directories and generate study subagent configs.

    Looks for directories under ``src/`` (or the given ``src_dirs``) that
    contain at least ``min_files`` source files.  Returns a list of
    subagent dicts suitable for ``run_parallel``.
    """
    root = Path(root)
    if src_dirs is None:
        src_dirs = ["src"]
    src_paths = [root / d for d in src_dirs if (root / d).is_dir()]

    subsystems = []
    for src_path in src_paths:
        for child in sorted(src_path.iterdir()):
            if not child.is_dir():
                continue
            # Count source files in this subsystem.
            source_files = list(child.rglob("*.c")) + \
                           list(child.rglob("*.h")) + \
                           list(child.rglob("*.py"))
            if len(source_files) >= min_files:
                rel = child.relative_to(root)
                subsystems.append({
                    "name": child.name,
                    "prompt": ".factory/prompts/study-subsystem.md",
                    "path": str(rel) + "/",
                    "description": f"Study the {child.name} subsystem ({rel})",
                })
    return subsystems


# ─── Roles.toml loader ───────────────────────────────────────────────

def load_roles(root: str | Path) -> dict:
    """Load .factory/roles.toml and return the parsed structure.

    Falls back to built-in defaults if the file doesn't exist.
    """
    root = Path(root)
    roles_path = root / ".factory" / "roles.toml"
    if roles_path.is_file():
        import tomllib
        with open(roles_path, "rb") as f:
            return tomllib.load(f)

    # Built-in defaults matching the user's described structure.
    return {
        "planning": {
            "planner_prompt": ".factory/prompts/planner.md",
            "timeout": 900,
            "studies": [
                {"name": "spec", "prompt": ".factory/prompts/study-spec.md",
                 "description": "Study the project specification"},
                {"name": "architecture", "prompt": ".factory/prompts/study-architecture.md",
                 "description": "Study the codebase architecture"},
                {"name": "bugs", "prompt": ".factory/prompts/study-bugs.md",
                 "description": "Study current bugs and test failures"},
            ],
        },
        "implementation": {
            "integration_prompt": ".factory/prompts/integration-developer.md",
            "timeout": 900,
            "developers": [
                {"name": "default", "prompt": ".factory/prompts/developer.md",
                 "description": "Full-stack developer"},
            ],
        },
        "audit": {
            "timeout": 600,
            "auditors": [
                {"name": "linting", "prompt": ".factory/prompts/auditor-linting.md",
                 "description": "Linting and readability"},
                {"name": "efficiency", "prompt": ".factory/prompts/auditor-efficiency.md",
                 "description": "Performance and efficiency"},
                {"name": "security", "prompt": ".factory/prompts/auditor-security.md",
                 "description": "Security vulnerabilities"},
                {"name": "functional", "prompt": ".factory/prompts/auditor-functional.md",
                 "description": "Functional correctness"},
                {"name": "spec-compliance", "prompt": ".factory/prompts/auditor-spec.md",
                 "description": "Spec compliance"},
                {"name": "compatibility", "prompt": ".factory/prompts/auditor-compatibility.md",
                 "description": "Platform and API compatibility"},
            ],
        },
    }