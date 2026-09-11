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
    Fully parallel, no side effects.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
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
    context string.  Returns results in completion order.
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
            fut = pool.submit(
                invoke_subagent,
                prompt, ctx, provider, model, timeout, cwd, approve,
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


def assemble_audit_findings(results: list[SubagentResult]) -> tuple[str, bool]:
    """Concatenate auditor findings and determine if any found blockers.

    Returns ``(report, has_blockers)``.
    """
    parts = []
    has_blockers = False
    for r in results:
        header = f"## {r.name} Audit"
        body = r.stdout if r.success else (
            f"**Auditor failed (exit {r.exit_code})**\n\n{r.stderr}"
        )
        parts.append(f"{header}\n\n{body}")
        # An auditor that exits non-zero is signalling findings/blockers.
        if r.exit_code != 0:
            has_blockers = True
        # Also check for BLOCKER severity in the output.
        if "BLOCKER" in (r.stdout or "").upper():
            has_blockers = True
    return "\n\n---\n\n".join(parts), has_blockers


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