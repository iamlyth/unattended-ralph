#!/usr/bin/env python3
"""DEPRECATED legacy context-summary generator (Task 15 migration).

The persisted context-summary authority is forbidden by FACTORY-LOOP-SPEC
§5.2 (no persisted context summaries and no competing task authority), so
the new control plane never generates, reads, injects, or validates
``.factory/artifacts/context-summary.md``.  This visible script remains only
as a marked, optional deprecated legacy entry point until later removal; it
is never a new-path dependency and it fails closed when invoked so it cannot
manufacture a stale context-summary authority.
"""

from __future__ import annotations

import sys


def main() -> int:
    print(
        "ralph-context-summary: DEPRECATED (Task 15 migration): the persisted "
        "context summary is forbidden by FACTORY-LOOP-SPEC §5.2; the new "
        "control plane never generates or reads "
        ".factory/artifacts/context-summary.md and the canonical plan is the "
        "sole task authority.",
        file=sys.stderr,
    )
    print(
        "ralph-context-summary: no context summary was written; this script "
        "fails closed and will be removed with the other legacy Ralph entry "
        "points.",
        file=sys.stderr,
    )
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
