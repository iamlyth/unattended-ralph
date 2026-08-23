#!/usr/bin/env python3
"""Deterministic scenario-driven role driver for the hidden campaign suite.

This executable is the *embedded/fixture role seam* of the Task 9 campaign
orchestrator: when the campaign CLI runs with ``--role-driver``, every
untrusted phase launches this committed repository file through the root
lock boundary as a fresh process with a ``FACTORY_LOOP_CAMPAIGN_*``
environment describing the phase.  It acts as a deterministic "model" for
synthetic fixture repos — writing the exact plan templates, code files, and
structured phase results the scenario prescribes, then exiting with the
scenario's exit status (or crashing, for interruption fixtures).  It never
runs Git: every commit in a campaign is made by the trusted orchestrator.

It is not evidence of real model acceptance or real confinement; production
campaigns launch real roles through the launch authority.
"""

import json
import os
import shutil
import signal
import sys

PREFIX = "FACTORY_LOOP_CAMPAIGN_"


def env(name: str) -> str:
    value = os.environ.get(PREFIX + name, "")
    if not value:
        raise SystemExit(f"campaign driver: missing {PREFIX}{name}")
    return value


def pick(section: dict, round_no: int, attempt_no: int, default: str) -> str:
    """Deterministic per-round/per-attempt behavior selection.

    ``section["behavior"]`` may be a plain string (the same behavior every
    run), or a mapping keyed by ``"<round>.<attempt>"``, ``"<round>"``, or
    ``"default"`` (first match wins).  Multi-round and multi-attempt fixtures
    can therefore revise behaviors deterministically (e.g. findings in round
    1 and a clean pass in round 2; a planner that fails once then succeeds).
    """
    behavior = section.get("behavior", default)
    if isinstance(behavior, dict):
        return behavior.get(
            f"{round_no}.{attempt_no}",
            behavior.get(str(round_no), behavior.get("default", default)),
        )
    return behavior


def copy_template(source_rel: str, target_rel: str, root: str) -> None:
    source = os.path.join(root, "fixture", "templates", source_rel)
    target = os.path.join(root, target_rel)
    with open(source, "rb") as src, open(target, "wb") as dst:
        shutil.copyfileobj(src, dst)


def write_result_file(result_file: str, root: str, outcome: str, findings=None, blocked_on=None) -> None:
    if not result_file:
        return
    data = {"schema": "factory-phase-result/v1", "outcome": outcome}
    if findings:
        data["findings"] = findings
    if blocked_on:
        data["blocked_on"] = blocked_on
    path = os.path.join(root, result_file)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as stream:
        json.dump(data, stream, sort_keys=True, separators=(",", ":"))


def touch(root: str, rel: str) -> None:
    path = os.path.join(root, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as stream:
        stream.write("dirty fixture work\n")


def main() -> int:
    root = env("ROOT")
    plan_rel = env("PLAN")
    role = env("ROLE")
    round_no = int(env("ROUND"))
    attempt = int(env("ATTEMPT"))
    task_id = os.environ.get(PREFIX + "TASK_ID", "")
    scenario_rel = env("SCENARIO")
    result_file = os.environ.get(PREFIX + "RESULT_FILE", "")

    plan_path = os.path.join(root, plan_rel)
    with open(os.path.join(root, scenario_rel), encoding="utf-8") as stream:
        scenario = json.load(stream)

    if role == "planner":
        behavior = pick(scenario.get("planner", {}), round_no, attempt, "planned")
        if behavior == "planned":
            copy_template(f"planner-{round_no}.md", plan_rel, root)
            return 0
        if behavior == "planned-complete":
            copy_template("planner-complete.md", plan_rel, root)
            return 0
        if behavior == "planned-blocked":
            copy_template("planner-blocked.md", plan_rel, root)
            return 0
        if behavior == "planned-unbound":
            copy_template("planner-unbound.md", plan_rel, root)
            return 0
        if behavior == "no-change":
            return 0
        if behavior == "invalid":
            with open(plan_path, "w", encoding="utf-8") as stream:
                stream.write("this is not a factory-plan/v1 plan\n")
            return 0
        if behavior == "crash":
            os.kill(os.getpid(), signal.SIGKILL)
        if behavior == "scope":
            copy_template(f"planner-{round_no}.md", plan_rel, root)
            touch(root, "src/planner-touched.py")
            return 1
        if behavior == "exit1":
            return 1
        raise SystemExit(f"campaign driver: unknown planner behavior {behavior!r}")

    if role == "developer":
        if not task_id:
            raise SystemExit("campaign driver: developer requires a task id")
        behavior = pick(scenario.get("developer", {}), round_no, attempt, "complete")
        if behavior == "crash":
            touch(root, f"src/work-{task_id}.md")
            os.kill(os.getpid(), signal.SIGKILL)
        if behavior == "crash-once":
            if attempt == 1:
                touch(root, f"src/work-{task_id}.md")
                os.kill(os.getpid(), signal.SIGKILL)
            behavior = scenario.get("developer", {}).get("resume", "complete")
        if behavior == "complete":
            copy_template(f"dev-{task_id}.md", plan_rel, root)
            touch(root, f"src/work-{task_id}.md")
            return 0
        if behavior == "complete-no-file":
            copy_template(f"dev-{task_id}.md", plan_rel, root)
            return 0
        if behavior == "complete-exit1":
            # Deterministic L3 fixture: the work is coherent and complete, but
            # the role process ends with a nonzero machine-readable exit
            # status — the §13 model-process-failed signal.  The orchestrator
            # must classify the attempt task_failed and preserve the work for
            # the next attempt.
            copy_template(f"dev-{task_id}.md", plan_rel, root)
            touch(root, f"src/work-{task_id}.md")
            return 1
        if behavior == "progress":
            copy_template(f"dev-{task_id}-progress.md", plan_rel, root)
            touch(root, f"src/work-{task_id}.md")
            return 0
        if behavior == "exit1":
            return 1
        if behavior == "invalid":
            with open(plan_path, "w", encoding="utf-8") as stream:
                stream.write("not a plan\n")
            return 0
        if behavior == "scope":
            copy_template(f"dev-{task_id}.md", plan_rel, root)
            touch(root, f"src/work-{task_id}.md")
            touch(root, ".factory/config.toml")
            return 0
        if behavior == "scope-policy":
            # Task 9 review HIGH: the untrusted developer tampers with the
            # trusted policy/harness surface (operational policy, harness
            # docs, legacy security scripts).  The orchestrator must refuse
            # to commit any of it and preserve the dirty work.
            copy_template(f"dev-{task_id}.md", plan_rel, root)
            touch(root, f"src/work-{task_id}.md")
            for rel in (
                "AGENTS.md", "docs/FACTORY.md", "scripts/guard.sh",
            ):
                touch(root, rel)
            return 0
        raise SystemExit(f"campaign driver: unknown developer behavior {behavior!r}")

    if role == "tester":
        behavior = pick(scenario.get("tester", {}), round_no, attempt, "pass")
        if behavior == "pass":
            write_result_file(result_file, root, "pass")
            return 0
        if behavior == "findings":
            write_result_file(result_file, root, "findings", findings=["fixture finding"])
            return 1
        if behavior == "blocked":
            write_result_file(
                result_file, root, "blocked", blocked_on=["external-capability-required"]
            )
            return 0
        if behavior == "crash":
            os.kill(os.getpid(), signal.SIGKILL)
        if behavior == "dirty":
            touch(root, "src/tester-touched.py")
            return 1
        if behavior == "no-result":
            return 0
        raise SystemExit(f"campaign driver: unknown tester behavior {behavior!r}")

    if role == "auditor":
        behavior = pick(scenario.get("auditor", {}), round_no, attempt, "pass")
        if behavior == "pass":
            write_result_file(result_file, root, "pass")
            return 0
        if behavior == "findings":
            write_result_file(
                result_file, root, "findings", findings=["fixture audit finding"]
            )
            return 1
        if behavior == "blocked":
            write_result_file(
                result_file, root, "blocked", blocked_on=["external-human-authority"]
            )
            return 0
        if behavior == "crash":
            os.kill(os.getpid(), signal.SIGKILL)
        if behavior == "dirty":
            touch(root, "src/auditor-touched.py")
            return 1
        if behavior == "no-result":
            return 0
        raise SystemExit(f"campaign driver: unknown auditor behavior {behavior!r}")

    raise SystemExit(f"campaign driver: unknown role {role!r}")


if __name__ == "__main__":
    sys.exit(main())
