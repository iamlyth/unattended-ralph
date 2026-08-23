#!/usr/bin/env python3
"""DEPRECATED legacy context-summary verifier (Task 15 migration).

The persisted context-summary authority is forbidden by FACTORY-LOOP-SPEC
§5.2, so the new control flow generates, checks, or reads no context
summary and a plan-mirror drift can never surface as an acceptance failure
through this mirror.  This visible script remains only as a marked, optional
deprecated legacy entry point until later removal; it is never a new-path
dependency and it fails closed when invoked so it cannot be mistaken for a
validation authority.
"""

from __future__ import annotations

import sys


def main() -> int:
    print(
        "check-context-summary: DEPRECATED (Task 15 migration): the persisted "
        "context summary is forbidden by FACTORY-LOOP-SPEC §5.2; the new "
        "control plane never generates, reads, or validates "
        ".factory/artifacts/context-summary.md and the canonical plan is the "
        "sole task authority.",
        file=sys.stderr,
    )
    print(
        "check-context-summary: nothing was validated; this script fails "
        "closed and will be removed with the other legacy Ralph entry "
        "points.",
        file=sys.stderr,
    )
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
