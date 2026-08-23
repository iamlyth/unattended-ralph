#!/usr/bin/env python3
"""Scratch multi-scenario runner for the Task 9 campaign smoke diagnostics.

Builds a fixture repo (like smoke_build.py) and runs one campaign per
scenario.  Not part of the hidden suite.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]  # /workspace/controller-box
GIT = "git"


def run(cmd, cwd, check=True, env=None):
    result = subprocess.run(cmd, cwd=cwd, text=True, capture_output=True, env=env)
    if check and result.returncode:
        raise SystemExit(
            f"cmd {cmd} failed rc={result.returncode}\n"
            f"stdout={result.stdout[-3000:]}\nstderr={result.stderr[-3000:]}"
        )
    return result


def gen_plan(ws: Path, out_rel: str, spec: dict, tasks: list):
    spec_path = ws / "spec.json"
    spec_path.write_text(
        json.dumps(
            {
                "spec_path": spec["spec_path"],
                "spec_commit": spec["spec_commit"],
                "spec_blob": spec["spec_blob"],
                "base_commit": spec["base_commit"],
                "lifecycle": spec["lifecycle"],
                "tasks": tasks,
            }
        ),
        encoding="utf-8",
    )
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


TASK_SPECS = [
    {"number": 1, "title": "Implement the fixture feature", "status": "pending",
     "priority": 10, "dependencies": [], "blocked_on": None,
     "scope": "initial scope statement.", "verification": "`src/work-1.md`"},
    {"number": 2, "title": "Implement the second feature", "status": "pending",
     "priority": 20, "dependencies": [], "blocked_on": None,
     "verification": "`src/work-2.md`"},
    {"number": 3, "title": "final", "status": "pending",
     "priority": 1, "dependencies": [1, 2], "blocked_on": None,
     "verification": "`src/final.md`"},
]


def build_workspace(tmp: Path) -> Path:
    ws = tmp / "ws"
    ws.mkdir(parents=True)
    for rel in ("docs", ".factory/prompts", ".factory/audit-objectives",
                ".factory/artifacts", ".factory/tests/fixtures",
                "fixture/templates", "src"):
        (ws / rel).mkdir(parents=True)
    (ws / "AGENTS.md").write_text("AGENTS.md operational policy\n", encoding="utf-8")
    (ws / "docs" / "SPEC.md").write_text("PRODUCT SPEC FIXTURE\n", encoding="utf-8")
    for role in ("planner", "developer", "tester", "auditor"):
        (ws / ".factory" / "prompts" / f"{role}.md").write_text(
            f"# {role} role prompt\n", encoding="utf-8")
    shutil.copy2(ROOT / ".factory" / "audit-objectives" / "registry.json",
                 ws / ".factory" / "audit-objectives" / "registry.json")
    run([GIT, "init", "-q", "-b", "fixture-main"], cwd=ws)
    run([GIT, "config", "user.email", "fixture@test"], cwd=ws)
    run([GIT, "config", "user.name", "fixture"], cwd=ws)
    run([GIT, "add", "-A"], cwd=ws)
    run([GIT, "commit", "-qm", "fixture base"], cwd=ws)
    base_head = run([GIT, "rev-parse", "HEAD"], cwd=ws).stdout.strip()
    spec_blob = run([GIT, "rev-parse", "HEAD:docs/SPEC.md"], cwd=ws).stdout.strip()
    common = {"spec_path": "docs/SPEC.md", "spec_commit": base_head,
              "spec_blob": spec_blob, "base_commit": base_head, "lifecycle": "active"}
    gen_plan(ws, ".factory/artifacts/implementation-plan.md", common, TASK_SPECS)
    revised = [
        {**t, "scope": t.get("scope", "fixture-scoped work only.") + " revised."}
        for t in TASK_SPECS
    ]
    gen_plan(ws, "fixture/templates/planner-1.md", common, revised)
    revised2 = [
        {**t, "scope": t.get("scope", "fixture-scoped work only.") + " revised again."}
        for t in TASK_SPECS
    ]
    gen_plan(ws, "fixture/templates/planner-2.md", common, revised2)
    revised3 = [
        {**t, "scope": t.get("scope", "fixture-scoped work only.") + " revised three."}
        for t in TASK_SPECS
    ]
    gen_plan(ws, "fixture/templates/planner-3.md", common, revised3)
    complete = [{**dict(t), "status": "complete"} for t in TASK_SPECS]
    gen_plan(ws, "fixture/templates/planner-complete.md",
             {**common, "lifecycle": "complete"}, complete)
    dev1 = [{**dict(TASK_SPECS[0]), "status": "complete"},
            TASK_SPECS[1], TASK_SPECS[2]]
    gen_plan(ws, "fixture/templates/dev-1.md", common, dev1)
    prog = [{**dict(TASK_SPECS[0]), "status": "in_progress"},
            TASK_SPECS[1], TASK_SPECS[2]]
    gen_plan(ws, "fixture/templates/dev-1-progress.md", common, prog)
    driver_dst = ws / ".factory/tests/fixtures/campaign_driver.py"
    shutil.copy2(ROOT / ".factory/tests/fixtures/campaign_driver.py", driver_dst)
    os.chmod(driver_dst, 0o755)
    run([GIT, "add", "-A"], cwd=ws)
    run([GIT, "commit", "-qm", "fixture plan and driver"], cwd=ws)
    return ws


def run_campaign(ws: Path, scenario: dict, *, rounds=1, campaign_id="smoke"):
    (ws / "scenario.json").write_text(json.dumps(scenario), encoding="utf-8")
    # The scenario is committed so it is never dirty role work (an untracked
    # fixture file inside the planner/implementation scope would fail the
    # campaign's exact-scope checks).
    run([GIT, "add", "scenario.json"], cwd=ws)
    run([GIT, "commit", "-qm", f"scenario {campaign_id}"], cwd=ws)
    cmd = [
        sys.executable, str(ROOT / ".factory/loop/campaign.py"),
        "--root", str(ws),
        "run",
        "--campaign-id", campaign_id,
        "--rounds", str(rounds),
        "--branch", "fixture-main",
        "--role-driver", ".factory/tests/fixtures/campaign_driver.py",
        "--scenario", "scenario.json",
        "--phase-result", ".factory-state/phase-result.json",
        "--audit-result", ".factory-state/audit-result.json",
    ]
    result = run(cmd, cwd=ROOT, check=False)
    try:
        data = json.loads(result.stdout)
    except ValueError:
        data = None
    return result, data


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="campaign-scenarios."))
    ws = build_workspace(tmp)
    scenarios = {
        "success": {"planner": {"behavior": "planned"},
                    "developer": {"behavior": "complete"},
                    "tester": {"behavior": "pass"},
                    "auditor": {"behavior": "pass"}},
        "final-findings": {"planner": {"behavior": "planned"},
                           "developer": {"behavior": "complete"},
                           "tester": {"behavior": "findings"},
                           "auditor": {"behavior": "findings"}},
        "audit-blocked": {"planner": {"behavior": "planned"},
                          "developer": {"behavior": "complete"},
                          "tester": {"behavior": "pass"},
                          "auditor": {"behavior": "blocked"}},
        "work-exhausted": {"planner": {"behavior": "planned-complete"},
                           "developer": {"behavior": "complete"},
                           "tester": {"behavior": "pass"},
                           "auditor": {"behavior": "pass"}},
        "planning-failed": {"planner": {"behavior": "no-change"},
                            "developer": {"behavior": "complete"},
                            "tester": {"behavior": "pass"},
                            "auditor": {"behavior": "pass"}},
        "dev-crash-once": {"planner": {"behavior": "planned"},
                           "developer": {"behavior": "crash-once", "resume": "complete"},
                           "tester": {"behavior": "pass"},
                           "auditor": {"behavior": "pass"}},
        "two-round-success": {"planner": {"behavior": "planned"},
                               "developer": {"behavior": "complete"},
                               "tester": {"behavior": {"1": "findings", "2": "pass", "default": "pass"}},
                               "auditor": {"behavior": {"1": "findings", "2": "pass", "default": "pass"}}},
        "verification-infra": {"planner": {"behavior": "planned"},
                                "developer": {"behavior": "complete"},
                                "tester": {"behavior": "dirty"},
                                "auditor": {"behavior": "pass"}},
        "planning-interrupted": {"planner": {"behavior": "crash"},
                                  "developer": {"behavior": "complete"},
                                  "tester": {"behavior": "pass"},
                                  "auditor": {"behavior": "pass"}},
        "dev-crash-always": {"planner": {"behavior": "planned"},
                              "developer": {"behavior": "crash"},
                              "tester": {"behavior": "pass"},
                              "auditor": {"behavior": "pass"}},
    }
    for name, scenario in scenarios.items():
        ws = build_workspace(tmp / name)
        rounds = 2 if name == "two-round-success" else 1
        result, data = run_campaign(ws, scenario, rounds=rounds, campaign_id=name)
        if data is None:
            print(f"{name}: rc={result.returncode} "
                  f"stdout={result.stdout[-1500:]!r} "
                  f"stderr={result.stderr[-1500:]!r}")
        else:
            print(f"{name}: terminal={data['terminal_phase']} "
                  f"outcome={data['terminal_outcome']} rc={result.returncode} "
                  f"rounds_completed={data['rounds_completed']}")
    shutil.rmtree(tmp, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
