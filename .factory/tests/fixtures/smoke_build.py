#!/usr/bin/env python3
"""Scratch smoke builder: build a fixture repo and run the campaign CLI.

Not part of the hidden suite; used to diagnose the Task 9 smoke failure.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]  # canonical project root
GIT = "git"


def run(cmd, cwd, check=True, env=None):
    result = subprocess.run(cmd, cwd=cwd, text=True, capture_output=True, env=env)
    if check and result.returncode:
        raise SystemExit(
            f"cmd {cmd} failed rc={result.returncode}\nstdout={result.stdout[-3000:]}\nstderr={result.stderr[-3000:]}"
        )
    return result


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="campaign-smoke."))
    ws = tmp / "ws"
    ws.mkdir()
    print(f"fixture root: {ws}")

    (ws / "docs").mkdir(parents=True)
    (ws / ".factory" / "prompts").mkdir(parents=True)
    (ws / ".factory" / "audit-objectives").mkdir(parents=True)
    (ws / ".factory" / "artifacts").mkdir(parents=True)
    (ws / ".factory" / "tests" / "fixtures").mkdir(parents=True)
    (ws / "fixture" / "templates").mkdir(parents=True)
    (ws / "src").mkdir(parents=True)

    (ws / "AGENTS.md").write_text("AGENTS.md operational policy\n", encoding="utf-8")
    (ws / "docs" / "SPEC.md").write_text("PRODUCT SPEC FIXTURE\n", encoding="utf-8")
    for role in ("planner", "developer", "tester", "auditor"):
        (ws / ".factory" / "prompts" / f"{role}.md").write_text(
            f"# {role} role prompt\n", encoding="utf-8"
        )
    shutil.copy2(
        ROOT / ".factory" / "audit-objectives" / "registry.json",
        ws / ".factory" / "audit-objectives" / "registry.json",
    )

    # ---- commit base files ----
    run([GIT, "init", "-q", "-b", "fixture-main"], cwd=ws)
    run([GIT, "config", "user.email", "fixture@test"], cwd=ws)
    run([GIT, "config", "user.name", "fixture"], cwd=ws)
    run([GIT, "add", "-A"], cwd=ws)
    run([GIT, "commit", "-qm", "fixture base"], cwd=ws)
    base_head = run([GIT, "rev-parse", "HEAD"], cwd=ws).stdout.strip()
    spec_blob = run([GIT, "rev-parse", f"HEAD:docs/SPEC.md"], cwd=ws).stdout.strip()

    # ---- generate plans via the fixture plan tool ----
    def gen_plan(out_rel: str, spec: dict):
        spec_path = ws / "spec.json"
        spec_path.write_text(json.dumps(spec), encoding="utf-8")
        run(
            [
                sys.executable,
                str(ROOT / ".factory/tests/fixtures/fixture_plan_tool.py"),
                "--spec", str(spec_path),
                "--registry", str(ROOT / ".factory/schemas/factory-plan-v1.requirements.json"),
                "--out", str(ws / out_rel),
            ],
            cwd=ws,
        )

    common = {
        "spec_path": "docs/SPEC.md",
        "spec_commit": base_head,
        "spec_blob": spec_blob,
        "base_commit": base_head,
        "lifecycle": "active",
    }
    # initial plan: 3 tasks, all pending
    initial_tasks = [
        {"number": 1, "title": "Implement the fixture feature", "status": "pending",
         "priority": 10, "dependencies": [], "blocked_on": None,
         "scope": "initial scope statement.",
         "verification": "`src/work-1.md`"},
        {"number": 2, "title": "Implement the second feature", "status": "pending",
         "priority": 20, "dependencies": [], "blocked_on": None,
         "verification": "`src/work-2.md`"},
        {"number": 3, "title": "final", "status": "pending",
         "priority": 1, "dependencies": [1, 2], "blocked_on": None,
         "verification": "`src/final.md`"},
    ]
    gen_plan(".factory/artifacts/implementation-plan.md",
             {**common, "tasks": initial_tasks})

    # planner-1: revised plan (different scope text so the planner made a change)
    revised_tasks = [
        {**t, "scope": t.get("scope", "fixture-scoped work only.") + " revised."}
        for t in initial_tasks
    ]
    gen_plan("fixture/templates/planner-1.md", {**common, "tasks": revised_tasks})
    # planner-complete: all complete
    complete_tasks = [
        {**t, "status": "complete"} for t in initial_tasks
    ]
    gen_plan("fixture/templates/planner-complete.md",
             {**common, "lifecycle": "complete", "tasks": complete_tasks})
    # dev-1: task 1 complete
    dev1_tasks = [
        {**initial_tasks[0], "status": "complete"},
        initial_tasks[1],
        initial_tasks[2],
    ]
    gen_plan("fixture/templates/dev-1.md", {**common, "tasks": dev1_tasks})
    # dev-1-progress: task 1 in_progress
    prog_tasks = [
        {**initial_tasks[0], "status": "in_progress"},
        initial_tasks[1],
        initial_tasks[2],
    ]
    gen_plan("fixture/templates/dev-1-progress.md", {**common, "tasks": prog_tasks})

    # ---- commit the driver ----
    driver_dst = ws / ".factory/tests/fixtures/campaign_driver.py"
    shutil.copy2(ROOT / ".factory/tests/fixtures/campaign_driver.py", driver_dst)
    os.chmod(driver_dst, 0o755)

    # scenario
    scenario = {
        "planner": {"behavior": "planned"},
        "developer": {"behavior": "complete"},
        "tester": {"behavior": "pass"},
        "auditor": {"behavior": "pass"},
    }
    (ws / "scenario.json").write_text(json.dumps(scenario), encoding="utf-8")

    run([GIT, "add", "-A"], cwd=ws)
    run([GIT, "commit", "-qm", "fixture plan and driver"], cwd=ws)
    head = run([GIT, "rev-parse", "HEAD"], cwd=ws).stdout.strip()

    # ---- run the campaign ----
    cmd = [
        sys.executable, str(ROOT / ".factory/loop/campaign.py"),
        "--root", str(ws),
        "run",
        "--campaign-id", "smoke-1",
        "--rounds", "1",
        "--branch", "fixture-main",
        "--role-driver", ".factory/tests/fixtures/campaign_driver.py",
        "--scenario", "scenario.json",
        "--phase-result", ".factory-state/phase-result.json",
        "--audit-result", ".factory-state/audit-result.json",
    ]
    result = run(cmd, cwd=ROOT, check=False)
    print("returncode:", result.returncode)
    print("stdout:", result.stdout[-4000:])
    print("stderr:", result.stderr[-4000:])
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
