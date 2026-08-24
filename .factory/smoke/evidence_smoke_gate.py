#!/usr/bin/env python3
"""Deterministic acceptance/verification gate of the evidence-smoke lane.

This committed executable is the deterministic gate the trusted campaign
orchestrator runs for the evidence-smoke round:

* ``--mode acceptance`` (the campaign's acceptance command): fails unless the
  worktree plan parses, the designated task is ``complete``, and the
  designated developer evidence artifact exists and is well-formed;
* ``--mode verify`` (the campaign's deterministic verification command):
  re-checks the committed state after the task commit and additionally fails
  unless the plan still parses, the canonical planner marker is present, and
  the final audit task stays pending (the round proves one full phase cycle,
  not acceptance).

The gate is deterministic, reads only bounded files under its ``--root``, and
never claims evidence elevation: a passing gate is a private source
methodology result, never a real model/human outcome, never installed-tier
evidence, never GIT-01 acceptance evidence, and never acceptance-tier
evidence.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Dict, Optional

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import evidence_smoke_common as common  # noqa: E402

PLAN_BLOB_MAX = 4 * 1024 * 1024


def _bounded_read(root: Path, relpath: str, label: str, maximum: int) -> bytes:
    """Anchored nofollow bounded read of ``root/relpath``.

    The read is pinned to a retained ``O_NOFOLLOW`` descriptor whose
    owner/mode/link-count/inode identity is validated both before and after
    the read and re-validated against the pathname at both ends — a symlink,
    mode, owner, link-count, inode, or size substitution fails closed (B2
    bounded-read contract).
    """
    return common.secure_read_bytes(
        root / relpath, maximum=maximum, what=label
    )


def _parse_plan(root: Path, plan_rel: str):
    data = _bounded_read(root, plan_rel, "plan", PLAN_BLOB_MAX)
    loop = root / ".factory" / "loop"
    if str(loop) not in sys.path:
        sys.path.insert(0, str(loop))
    try:
        import plan_parser  # noqa: PLC0415

        plan = plan_parser.Plan.from_bytes(data)
    except Exception as exc:  # plan_parser.PlanError plus import failures
        raise SystemExit(f"evidence-smoke gate: plan does not parse: {exc}")
    return plan, data


def _task_status(plan, task_id: int) -> Optional[str]:
    for task in plan.tasks:
        if task.number == task_id:
            return task.status
    return None


def _check_evidence(root: Path, evidence_rel: str, task_id: int,
                    campaign_id: Optional[str]) -> Dict[str, object]:
    data = _bounded_read(root, evidence_rel, "evidence", common.EVIDENCE_MAX)
    return common.validate_smoke_evidence(
        data, task_id=task_id, campaign_id=campaign_id
    )


def _run(mode: str, root: Path, plan_rel: str, evidence: str,
         task_id: int, campaign_id: str) -> int:
    plan, plan_bytes = _parse_plan(root, plan_rel)
    status = _task_status(plan, task_id)
    if status != "complete":
        raise SystemExit(
            f"evidence-smoke gate: task {task_id} status is {status!r}, "
            "expected complete"
        )
    if common.SMOKE_MARKER not in plan_bytes.decode("utf-8"):
        raise SystemExit(
            "evidence-smoke gate: the planner's canonical marker is missing "
            "from the plan"
        )
    final_status = _task_status(plan, common.FINAL_AUDIT_TASK_ID)
    if final_status == "complete":
        raise SystemExit(
            "evidence-smoke gate: the final audit task must stay pending "
            "(the evidence round is not acceptance)"
        )
    payload = _check_evidence(root, evidence, task_id, campaign_id)
    if mode == "verify":
        # The verification gate additionally proves the round did not
        # elevate any evidence: the evidence artifact stays the private seam
        # label and no findings/blocked channel was minted.
        if payload.get("findings_present"):
            raise SystemExit(
                "evidence-smoke gate: the developer evidence claims a "
                "findings channel"
            )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(prog="evidence_smoke_gate")
    parser.add_argument("--root", required=True)
    parser.add_argument("--evidence", default=common.DESIGNATED_EVIDENCE_REL)
    parser.add_argument("--task", type=int, default=common.EVIDENCE_TASK_ID)
    parser.add_argument("--campaign-id", default="")
    parser.add_argument("--mode", required=True, choices=("acceptance", "verify"))
    args = parser.parse_args(sys.argv[1:])
    root = Path(args.root).absolute()
    try:
        return _run(
            args.mode,
            root,
            common.PLAN_REL,
            args.evidence,
            args.task,
            args.campaign_id,
        )
    except SystemExit:
        raise
    except common.EvidenceSmokeError as exc:
        raise SystemExit(f"evidence-smoke gate: {exc}")


if __name__ == "__main__":
    sys.exit(main())
