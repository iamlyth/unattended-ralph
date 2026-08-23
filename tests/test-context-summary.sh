#!/usr/bin/env bash
# DEPRECATED legacy context-summary adversarial suite (Task 15 migration).
#
# The persisted context-summary authority is forbidden by FACTORY-LOOP-SPEC
# §5.2, so the new control loop never invokes this suite and the stale
# `.factory/artifacts/context-summary.md` mirror is removed from the tracked
# authority.  This visible test remains only as a marked, optional
# deprecated legacy artifact until later removal; it is never a new-path
# dependency and never fails the new loop.
set -euo pipefail
echo "test-context-summary: DEPRECATED (Task 15 migration): the persisted context summary" >&2
echo "test-context-summary: is forbidden by FACTORY-LOOP-SPEC §5.2; the new control loop" >&2
echo "test-context-summary: never generates, reads, or validates context summaries." >&2
exit 0
