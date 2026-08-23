#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
PROJECT_ROOT=$(cd -- "$SCRIPT_DIR/.." && pwd)
PLAN=${FACTORY_PLAN_PATH:-$PROJECT_ROOT/.factory/artifacts/implementation-plan.md}
PHASE=committed
if [[ ${1:-} == --planning ]]; then
    PHASE=planning
    shift
fi
(( $# == 0 )) || { echo "Usage: scripts/check-plan-freshness.sh [--planning]" >&2; exit 2; }
cd -- "$PROJECT_ROOT"

[[ -s "$PLAN" ]] || { echo "plan-freshness: missing .factory/artifacts/implementation-plan.md; run ./scripts/ralph-plan.sh" >&2; exit 1; }

mapfile -t META < <(python3 - "$PLAN" <<'PY'
import sys
path = sys.argv[1]
lines = open(path, encoding='utf-8').read().splitlines()
if not lines or lines[0].strip() != '---':
    raise SystemExit('plan-freshness: plan has no metadata front matter')
meta = {}
for line in lines[1:]:
    if line.strip() == '---':
        break
    if ':' in line:
        key, value = line.split(':', 1)
        meta[key.strip()] = value.strip().strip('"\'')
for key in ('spec_path', 'spec_commit', 'spec_blob', 'base_commit', 'status'):
    print(meta.get(key, ''))
PY
)

(( ${#META[@]} == 5 )) || { echo "plan-freshness: incomplete plan metadata" >&2; exit 1; }
SPEC_PATH=${META[0]}
RECORDED_COMMIT=${META[1]}
RECORDED_BLOB=${META[2]}
BASE_COMMIT=${META[3]}
STATUS=${META[4]}
CANONICAL_SPEC=$(python3 - <<'PY'
import tomllib
with open('.factory/config.toml', 'rb') as stream:
    print(tomllib.load(stream)['project']['spec'])
PY
)

[[ "$SPEC_PATH" == "$CANONICAL_SPEC" ]] || {
    echo "plan-freshness: plan spec '$SPEC_PATH' does not match canonical '$CANONICAL_SPEC'" >&2
    exit 1
}
[[ -n "$SPEC_PATH" && -f "$SPEC_PATH" ]] || { echo "plan-freshness: missing spec '$SPEC_PATH'" >&2; exit 1; }
# The bound canonical spec must never be a neutral placeholder. In this
# boilerplate cycle the plan resolves `docs/FACTORY-LOOP-SPEC.md` (commit
# `2d6a4fd`, blob `ca2334ab…`); `docs/SPEC.md` remains the adopting-product
# placeholder and is never planned against. The guard stays path-neutral so an
# adopting repository's real committed spec keeps passing.
if grep -q 'SPEC_PENDING_HUMAN_SUPPLY' "$SPEC_PATH"; then
    echo "plan-freshness: canonical spec '$SPEC_PATH' is still a neutral placeholder" >&2
    echo "plan-freshness: planning never runs against a placeholder specification" >&2
    exit 1
fi
[[ "$RECORDED_COMMIT" != UNPLANNED && "$RECORDED_BLOB" != UNPLANNED ]] || {
    echo "plan-freshness: plan is unplanned; run ./scripts/ralph-plan.sh" >&2
    exit 1
}
if [[ "$PHASE" == planning ]]; then
    [[ "$STATUS" == active ]] || {
        echo "plan-freshness: planning status is '$STATUS', expected 'active'" >&2; exit 1;
    }
else
    [[ "$STATUS" == active || "$STATUS" == complete ]] || {
        echo "plan-freshness: plan status is '$STATUS', expected 'active' or 'complete'" >&2; exit 1;
    }
fi
git cat-file -e "$BASE_COMMIT^{commit}" 2>/dev/null || {
    echo "plan-freshness: invalid base commit '$BASE_COMMIT'" >&2
    exit 1
}
git merge-base --is-ancestor "$BASE_COMMIT" HEAD || {
    echo "plan-freshness: base commit '$BASE_COMMIT' is not an ancestor of HEAD" >&2
    exit 1
}
if [[ "$PHASE" == planning ]]; then
    EXPECTED_BASE=${FACTORY_PLANNING_BASE_COMMIT:-}
    if [[ -z "$EXPECTED_BASE" ]]; then
        EXPECTED_BASE=$("$SCRIPT_DIR/factory-state-file.py" read planning-base-commit --missing-ok) || exit $?
    fi
    [[ -n "$EXPECTED_BASE" && "$BASE_COMMIT" == "$EXPECTED_BASE" ]] || {
        echo "plan-freshness: base_commit differs from the selected planning cycle base" >&2; exit 1;
    }
fi

if ! git diff --quiet -- "$SPEC_PATH" || ! git diff --cached --quiet -- "$SPEC_PATH"; then
    echo "plan-freshness: '$SPEC_PATH' has uncommitted changes; commit the spec and replan" >&2
    exit 1
fi

ACTUAL_COMMIT=$(git log -1 --format=%H -- "$SPEC_PATH")
ACTUAL_BLOB=$(git rev-parse "HEAD:$SPEC_PATH")

if [[ "$ACTUAL_COMMIT" != "$RECORDED_COMMIT" || "$ACTUAL_BLOB" != "$RECORDED_BLOB" ]]; then
    echo "plan-freshness: specification changed after planning" >&2
    echo "  recorded commit: $RECORDED_COMMIT" >&2
    echo "  current commit:  $ACTUAL_COMMIT" >&2
    echo "  recorded blob:   $RECORDED_BLOB" >&2
    echo "  current blob:    $ACTUAL_BLOB" >&2
    echo "Run ./scripts/ralph-plan.sh before implementation." >&2
    exit 1
fi

echo "plan-freshness: spec=$SPEC_PATH commit=${ACTUAL_COMMIT:0:12} blob=${ACTUAL_BLOB:0:12}"
