#!/usr/bin/env bash
# test-factory-smoke.sh — hidden evidence-smoke lane shell driver (Task 22).
#
# The evidence-smoke lane (``.factory/smoke/``) drives the real production
# campaign control plane for exactly one planning -> implementation ->
# verification -> audit round with the designated deterministic smoke seam.
# It is private/installed harness methodology evidence, never a real model or
# human outcome.  This driver:
#
#   0. hard-checks every authority prerequisite (the loop campaign/state/
#      plan-parser modules, the four committed smoke-lane files, the
#      canonical plan with Task 22 pending, and the smoke Python suite) with
#      a named diagnostic before anything runs, so a missing or renamed
#      authority can never silently weaken the lane;
#   1. runs the hidden Python suite warning-free under
#      `-W error::ResourceWarning` (the full suite drives the real campaign
#      CLI and the trusted operator command in fresh subprocesses against
#      committed fixture repositories and asserts the exact phase history,
#      state/digest-ledger contracts, byte-bound planner revision, committed
#      evidence artifact, foreign-state preservation, process reap, and
#      fail-closed refusals);
#   2. proves the campaign CLI exposes the fail-closed ``--evidence-smoke``
#      mode in its own --help text (the installed interface contract).
#
# The driver never writes to the live repository and never touches
# ``.factory-state/`` or ``.ralph/``.
set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
ROOT=$(cd -- "$SCRIPT_DIR/../.." && pwd)
cd -- "$ROOT"

PY=${PYTHON:-python3}

fail() {
    echo "test-factory-smoke: $*" >&2
    exit 1
}

# -- 0. Hard prerequisite diagnostics ----------------------------------------
required_prerequisites=(
    .factory/loop/campaign.py
    .factory/loop/plan_parser.py
    .factory/loop/selector.py
    .factory/loop/state.py
    .factory/loop/gitutil.py
    .factory/smoke/evidence_smoke_common.py
    .factory/smoke/evidence_smoke_driver.py
    .factory/smoke/evidence_smoke_gate.py
    .factory/smoke/evidence_smoke.py
    .factory/tests/test-factory-smoke.py
    .factory/tests/fixtures/fixture_plan_tool.py
    .factory/schemas/factory-plan-v1.requirements.json
)
for prerequisite in "${required_prerequisites[@]}"; do
    [[ -f "$ROOT/$prerequisite" ]] || \
        fail "missing evidence-smoke authority prerequisite: $prerequisite"
done
# The canonical plan must still carry Task 22 pending for the evidence round.
grep -q '^## Task 22: Live campaign and control-state instantiation' \
    "$ROOT/.factory/artifacts/implementation-plan.md" || \
    fail "the canonical plan no longer names the evidence task Task 22"
grep -q '^- Status: pending' "$ROOT/.factory/artifacts/implementation-plan.md" || \
    fail "the canonical plan no longer carries a pending task"
echo "test-factory-smoke: evidence-smoke authority prerequisites present"

tmp=$(mktemp -d "${TMPDIR:-/tmp}/factory-smoke.XXXXXX")
cleanup() {
    rm -rf -- "$tmp"
}
# shellcheck disable=SC2154 # rc is assigned inside the trap from $?
trap 'rc=$?; cleanup; trap - EXIT INT TERM; exit "$rc"' EXIT INT TERM

# -- 1. The hidden Python suite runs warning-free ---------------------------
echo "test-factory-smoke: running .factory/tests/test-factory-smoke.py"
"$PY" -W error::ResourceWarning .factory/tests/test-factory-smoke.py \
    >"$tmp/suite.log" 2>&1 || {
    echo "test-factory-smoke: Python suite failed:" >&2
    tail -80 "$tmp/suite.log" >&2
    exit 1
}
if grep -qiE 'ResourceWarning|unclosed file|still running' "$tmp/suite.log"; then
    echo "test-factory-smoke: ResourceWarning/subprocess leaked from the suite:" >&2
    grep -iE 'ResourceWarning|unclosed file|still running' "$tmp/suite.log" \
        | head -20 >&2
    exit 1
fi
grep -q '^OK$' "$tmp/suite.log" \
    || fail "the Python suite must end OK (got: $(tail -3 "$tmp/suite.log"))"
echo "test-factory-smoke: the evidence-smoke suite passes warning-free"

# -- 2. The installed CLI exposes the fail-closed mode ----------------------
"$PY" .factory/loop/campaign.py run --help | grep -q -- '--evidence-smoke' \
    || fail "campaign.py run --help must expose the --evidence-smoke mode"
"$PY" .factory/loop/campaign.py run --help | grep -q -- '--developer-evidence-path' \
    || fail "campaign.py run --help must expose the developer evidence binding"
# The trusted operator command makes the exact bound commit mandatory (a live
# smoke round never runs against an unverified HEAD).
"$PY" .factory/smoke/evidence_smoke.py run --help \
    | grep -q -- '--expect-commit' \
    || fail "evidence_smoke.py run --help must expose the mandatory --expect-commit"
"$PY" .factory/smoke/evidence_smoke.py run --help \
    | head -2 | grep -q -- '--expect-commit SHA40' \
    || fail "evidence_smoke.py run --help must mark --expect-commit required"
"$PY" .factory/smoke/evidence_smoke.py run --help \
    | grep -q -- 'mandatory' \
    || fail "evidence_smoke.py --expect-commit help must document the exact bound commit"
echo "test-factory-smoke: campaign CLI exposes the fail-closed evidence-smoke mode"

echo "test-factory-smoke: all checks passed"
