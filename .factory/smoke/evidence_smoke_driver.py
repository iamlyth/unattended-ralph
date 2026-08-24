#!/usr/bin/env python3
"""Deterministic designated evidence-smoke role seam (Task 22, private label).

This committed executable is the *designated smoke-role seam*: when the
trusted operator runs the evidence-smoke lane, every untrusted campaign phase
(planner, developer, tester, auditor) launches this exact committed file
through the root-lock boundary as a fresh process with a
``FACTORY_LOOP_CAMPAIGN_*`` environment describing the phase.  It acts as a
deterministic synthetic "model" for exactly one full
planning -> implementation -> verification -> audit round of the *real*
production campaign control plane against the live repository:

* planner: writes the canonical byte-bound revision of the committed plan
  (the exact plan bytes plus one fixed marker line); ``Task 22`` stays
  ``pending`` in the planner output and only the developer role marks it
  ``complete`` (spec §6.2: the planner creates/revises tasks, the developer
  updates the selected task's plan status and evidence);
* developer: records the single bounded harness-owned tracked evidence
  artifact under ``.factory/artifacts/`` and marks the deterministically
  selected task (``Task 22``) complete in the plan revision;
* tester/auditor: write the exact ``factory-phase-result/v1`` ``pass``
  structured results (never findings, never blocked, never evidence
  elevation).

The seam is deterministic (a pure function of the committed plan bytes and
the phase environment), never runs Git (every commit is made by the trusted
orchestrator), and never touches a model, credential, cookie, runner, or
human.  It is explicitly *private source methodology evidence*: it is never
a real model or human outcome, never real confinement evidence, never
installed-tier evidence, never GIT-01 acceptance evidence, and never
acceptance-tier evidence.  The campaign CLI refuses to use it outside the
fail-closed ``--evidence-smoke`` mode, and the mode refuses any other
driver.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys

HERE = os.path.dirname(os.path.realpath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import evidence_smoke_common as common  # noqa: E402

PREFIX = "FACTORY_LOOP_CAMPAIGN_"


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def env(name: str, *, required: bool = True) -> str:
    value = os.environ.get(PREFIX + name, "")
    if required and not value:
        raise SystemExit(f"evidence-smoke driver: missing {PREFIX}{name}")
    return value


def read_plan(root: str, plan_rel: str) -> bytes:
    with open(os.path.join(root, plan_rel), "rb") as stream:
        return stream.read()


def write_file(root: str, rel: str, data: bytes) -> None:
    path = os.path.join(root, rel)
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    with open(path, "wb") as stream:
        stream.write(data)


def write_phase_result(result_file: str, root: str) -> None:
    if not result_file:
        raise SystemExit(
            "evidence-smoke driver: tester/auditor requires a result file"
        )
    payload = {"schema": "factory-phase-result/v1", "outcome": "pass"}
    write_file(
        root,
        result_file,
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        ),
    )


def main() -> int:
    root = env("ROOT")
    plan_rel = env("PLAN")
    role = env("ROLE")
    campaign_id = env("CAMPAIGN_ID")
    bound_commit = env("BOUND_COMMIT")
    round_no = int(env("ROUND"))
    attempt = int(env("ATTEMPT"))
    task_id_raw = os.environ.get(PREFIX + "TASK_ID", "")
    result_file = os.environ.get(PREFIX + "RESULT_FILE", "")
    evidence_rel = os.environ.get(PREFIX + "DEVELOPER_EVIDENCE", "")
    findings_env = os.environ.get(PREFIX + "FINDINGS", "")

    if role == "planner":
        # The canonical byte-bound revision: the committed plan plus the
        # fixed marker line.  Bindings, task statuses (Task 22 stays
        # pending), matrix, and inventory are byte-identical.
        plan_bytes = read_plan(root, plan_rel)
        revision = common.plan_with_smoke_marker(plan_bytes)
        write_file(root, plan_rel, revision)
        return 0

    if role == "developer":
        if not task_id_raw:
            raise SystemExit(
                "evidence-smoke driver: developer requires a selected task id"
            )
        task_id = int(task_id_raw)
        if task_id != common.EVIDENCE_TASK_ID:
            raise SystemExit(
                "evidence-smoke driver: the evidence round works exactly the "
                f"designated task {common.EVIDENCE_TASK_ID}, not {task_id}"
            )
        excerpt_digest = env("TASK_EXCERPT_DIGEST")
        if findings_env:
            raise SystemExit(
                "evidence-smoke driver: the developer must never receive a "
                "findings payload"
            )
        if not evidence_rel:
            raise SystemExit(
                "evidence-smoke driver: the designated evidence artifact path "
                "was not bound"
            )
        plan_bytes = read_plan(root, plan_rel)
        payload = common.smoke_evidence_bytes(
            campaign_id=campaign_id,
            bound_commit=bound_commit,
            task_id=task_id,
            round_no=round_no,
            attempt=attempt,
            task_excerpt_digest=excerpt_digest,
            plan_digest_worked=sha256(plan_bytes),
            findings_present=False,
        )
        write_file(root, evidence_rel, payload)
        revision = common.plan_with_task_complete(plan_bytes, task_id)
        write_file(root, plan_rel, revision)
        return 0

    if role == "tester":
        write_phase_result(result_file, root)
        return 0

    if role == "auditor":
        write_phase_result(result_file, root)
        return 0

    raise SystemExit(f"evidence-smoke driver: unknown role {role!r}")


if __name__ == "__main__":
    sys.exit(main())
