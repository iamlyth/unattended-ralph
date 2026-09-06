#!/usr/bin/env python3
"""Deterministic scenario-driven role driver for the hidden campaign suite.

This executable is the *embedded/fixture role seam* of the Task 9 campaign
orchestrator: when the campaign CLI runs with ``--role-driver``, every
untrusted phase launches this committed repository file through the root
lock boundary as a fresh process with a ``FACTORY_LOOP_CAMPAIGN_*``
environment describing the phase.  It acts as a deterministic "model" for
synthetic fixture repos — writing the exact plan templates, code files, and
structured phase results the scenario prescribes, then exiting with the
scenario's exit status (or crashing, for interruption fixtures).  The Task 10
``findings-revised`` planner behavior additionally consumes the deterministic
receipt-backed ``factory-findings/v1`` payload of the previous round from its
environment (``FACTORY_LOOP_CAMPAIGN_FINDINGS``) and fails closed when the
payload is absent or malformed, so a fixture can prove that
verification/audit findings reached the next planner before any development
continued.  The Task 16 ``developer`` behavior likewise receives the exact
task-excerpt digest of the committed plan at the phase head (derived by the
real launch authority) and fails closed when it is absent, recording
evidence of the exact received bytes so the trusted suite can prove the
developer worked only the revised selected task and never a findings/
receipt payload.  It never runs Git: every commit in a campaign is made by
the trusted orchestrator.

It is not evidence of real model acceptance or real confinement; production
campaigns launch real roles through the launch authority.
"""

import json
import os
import shutil
import signal
import sys

PREFIX = "FACTORY_LOOP_CAMPAIGN_"


def sha256(data: bytes) -> str:
    import hashlib
    return hashlib.sha256(data).hexdigest()


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
        if behavior == "planned-exit1":
            copy_template(f"planner-{round_no}.md", plan_rel, root)
            return 1
        if behavior == "planned-complete":
            copy_template("planner-complete.md", plan_rel, root)
            return 0
        if behavior == "planned-blocked":
            copy_template("planner-blocked.md", plan_rel, root)
            return 0
        if behavior == "planned-unbound":
            copy_template("planner-unbound.md", plan_rel, root)
            return 0
        if behavior == "findings-revised":
            # Task 10 (FIND-01, §16): this is the *only* planner behavior that
            # consumes the previous round's findings.  The deterministic
            # receipt-backed ``factory-findings/v1`` payload must be present
            # in the planner's environment, must parse, and must carry at
            # least one structured entry (phase, outcome, exact phase-base
            # commit, phase tag, result digest, and the receipt digest); any
            # missing/malformed payload fails the planner closed so the
            # campaign never lets a planner that ignored its findings pass.
            # Task 16 §22.5: the exact payload bytes are digest-bound exactly
            # like the production planner prompt (:func:`launch.compose_prompt`
            # refuses substituted findings against ``findings_digest``).  The
            # campaign authority delivers the verbatim payload plus its
            # SHA-256; the driver refuses any substituted or tampered payload
            # whose bytes do not carry the delivered digest, and records the
            # exact digest of the bytes it consumed as fixture evidence for
            # the trusted suite (never an authority of any phase).
            findings = os.environ.get(PREFIX + "FINDINGS", "")
            expected_digest = os.environ.get(PREFIX + "FINDINGS_DIGEST", "")
            if not findings:
                raise SystemExit(
                    "campaign driver: findings-revised requires the "
                    "receipt-backed findings payload"
                )
            if not expected_digest:
                raise SystemExit(
                    "campaign driver: findings-revised requires the exact "
                    "payload digest"
                )
            if sha256(findings.encode("utf-8")) != expected_digest:
                raise SystemExit(
                    "campaign driver: findings payload digest mismatch; the "
                    "delivered payload bytes are substituted or tampered"
                )
            try:
                payload = json.loads(findings)
            except ValueError as exc:
                raise SystemExit(
                    f"campaign driver: findings payload is not JSON: {exc}"
                )
            if not isinstance(payload, dict) or payload.get("schema") != "factory-findings/v1":
                raise SystemExit(
                    "campaign driver: findings payload has the wrong schema"
                )
            entries = payload.get("entries")
            if not isinstance(entries, list) or not entries:
                raise SystemExit(
                    "campaign driver: findings payload carries no entries"
                )
            for entry in entries:
                if not isinstance(entry, dict) or entry.get("phase") not in (
                    "verification", "audit",
                ):
                    raise SystemExit(
                        "campaign driver: findings payload has an invalid entry"
                    )
                if not isinstance(entry.get("findings"), list) or not isinstance(
                    entry.get("blocked_on"), list
                ):
                    raise SystemExit(
                        "campaign driver: findings payload entry is not structured"
                    )
                # The deterministic-gate evidence fields (Task 10 REQ 2) are
                # part of the payload contract; a payload that omits them is
                # malformed and never reaches a planner.
                for field in ("gate_ran", "capability_ran"):
                    if not isinstance(entry.get(field), bool):
                        raise SystemExit(
                            "campaign driver: findings payload entry is "
                            f"missing the {field} gate field"
                        )
                for field in ("gate_exit", "capability_exit"):
                    value = entry.get(field)
                    if value is not None and (
                        isinstance(value, bool) or not isinstance(value, int)
                    ):
                        raise SystemExit(
                            "campaign driver: findings payload entry has an "
                            f"invalid {field} gate value"
                        )
            # The canonical revised plan template of this round incorporates
            # the accepted findings as new/revised tasks (the fixture seam
            # mirrors the production planner's digest-bound prompt section).
            # External blockers (blocked_on references) stay explicit in the
            # plan as blocked tasks; other findings revise the next task.
            has_blockers = any(
                entry.get("blocked_on") for entry in entries
            )
            if has_blockers:
                copy_template(
                    f"planner-findings-blocked-revised-{round_no}.md",
                    plan_rel, root,
                )
            else:
                copy_template(
                    f"planner-findings-revised-{round_no}.md", plan_rel, root
                )
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
        # Task 16 §22.5: the developer receives *only* the exact revised
        # selected-task bytes.  The campaign authority derives the task-
        # excerpt digest from the exact committed plan at the phase head
        # (mirroring the production launch child environment) and the driver
        # fails closed when that digest is absent — a developer that never
        # received its exact task bytes cannot proceed.  The developer
        # records evidence of the exact received bytes (the excerpt digest,
        # the digest of the plan it worked, and the fact that no findings
        # env ever reached it) under the fixture output namespace; the
        # trusted suite re-derives the expected excerpt from the committed
        # revised plan with the real authority.
        excerpt_digest = os.environ.get(PREFIX + "TASK_EXCERPT_DIGEST", "")
        findings_env = os.environ.get(PREFIX + "FINDINGS", "")
        if not excerpt_digest:
            raise SystemExit(
                "campaign driver: developer requires the exact task-excerpt "
                "digest of the committed plan"
            )
        if behavior == "complete":
            # The exact received bytes are recorded only for the coherent
            # completion behavior (the deterministic model working the exact
            # task): the file lives under the fixture output namespace and
            # the trusted orchestrator commits it with the task work.  Other
            # behaviors (crash/invalid/scope fixtures) must not add
            # preservable dirty paths that would change their failure
            # classification.
            with open(plan_path, "rb") as stream:
                plan_bytes = stream.read()
            evidence = {
                "schema": "factory-driver-developer-evidence/v1",
                "round": round_no,
                "task_id": task_id,
                "task_excerpt_digest": excerpt_digest,
                "plan_digest": sha256(plan_bytes),
                "findings_present": bool(findings_env),
            }
            evidence_rel = (
                f"src/.factory-test-output/developer-evidence-round-{round_no}.json"
            )
            evidence_path = os.path.join(root, evidence_rel)
            os.makedirs(os.path.dirname(evidence_path), exist_ok=True)
            with open(evidence_path, "w", encoding="utf-8") as stream:
                json.dump(evidence, stream, sort_keys=True, separators=(",", ":"))
        if behavior == "crash":
            crash_path = os.path.join(root, f"src/work-{task_id}.md")
            os.makedirs(os.path.dirname(crash_path), exist_ok=True)
            with open(crash_path, "a", encoding="utf-8") as stream:
                stream.write(f"crash-attempt-{attempt}\n")
            os.kill(os.getpid(), signal.SIGKILL)
        if behavior == "clean-crash":
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
        if behavior == "no-result":
            return 0
        if behavior == "malformed-result":
            path = os.path.join(root, result_file)
            with open(path, "w", encoding="utf-8") as stream:
                stream.write('{"schema":"factory-phase-result/v1","outcome":')
            return 0
        if behavior == "findings":
            write_result_file(result_file, root, "findings", findings=["fixture finding"])
            return 0
        if behavior == "pass-exit1":
            write_result_file(result_file, root, "pass")
            return 1
        if behavior == "secret-findings":
            # Task 23 (F): a free-text finding that embeds a raw credential-
            # shaped value.  The trusted orchestrator must redact it through
            # the exact-commit credential guard BEFORE any durable storage or
            # next-planner delivery; the raw value must never appear in the
            # preserved result, the receipt, the control state, or the next
            # planner's payload.
            write_result_file(
                result_file, root, "findings",
                findings=["api_token=super-secret-value-123 leak in fixture"],
            )
            return 0
        if behavior == "secret-blocked":
            write_result_file(
                result_file, root, "blocked",
                blocked_on=["GITHUB_TOKEN=ghp_secret_blocker external-capability"],
            )
            return 0
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
            return 0
        if behavior == "pass-exit1":
            write_result_file(result_file, root, "pass")
            return 1
        if behavior == "blocked":
            write_result_file(
                result_file, root, "blocked", blocked_on=["external-human-authority"]
            )
            return 0
        if behavior == "secret-blocked":
            write_result_file(
                result_file, root, "blocked",
                blocked_on=["GITHUB_TOKEN=ghp_secret_blocker external-capability"],
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
