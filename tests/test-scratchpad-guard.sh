#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
PROJECT_ROOT=$(cd -- "$SCRIPT_DIR/.." && pwd)
GUARD="$PROJECT_ROOT/scripts/check-scratchpad.sh"
tmp=$(mktemp)
trap 'rm -f "$tmp"' EXIT
export FACTORY_SCRATCHPAD_PATH=$tmp

cat > "$tmp" <<'EOF'
# Current handoff

## Work completed

- One concise recovery fact.

## Verification

### Targeted tests

- All targeted checks passed.
EOF
"$GUARD" PLAN_COMPLETE >/dev/null

rm -f "$tmp"
if "$GUARD" PLAN_COMPLETE >/dev/null 2>&1; then
    echo 'test-scratchpad-guard: strict mode accepted a missing scratchpad' >&2
    exit 1
fi
"$GUARD" PLAN_COMPLETE --allow-missing >/dev/null

touch "$tmp"
if "$GUARD" PLAN_COMPLETE --allow-missing >/dev/null 2>&1; then
    echo 'test-scratchpad-guard: allow-missing mode accepted an empty scratchpad' >&2
    exit 1
fi

printf '# First handoff\n\n## Details\n\n# Second handoff\n' > "$tmp"
if "$GUARD" PLAN_COMPLETE --allow-oversize >/dev/null 2>&1; then
    echo 'test-scratchpad-guard: appended handoff document was accepted' >&2
    exit 1
fi

printf '## Missing document title\n\n- Detail\n' > "$tmp"
if "$GUARD" PLAN_COMPLETE >/dev/null 2>&1; then
    echo 'test-scratchpad-guard: handoff without a level-one title was accepted' >&2
    exit 1
fi

printf '# Current handoff\n\n## Status\n\nPLAN_COMPLETE\n' > "$tmp"
if "$GUARD" PLAN_COMPLETE --allow-oversize >/dev/null 2>&1; then
    echo 'test-scratchpad-guard: reserved completion token was accepted by the final protocol check' >&2
    exit 1
fi
# Iteration checkpoints validate the handoff structure without turning a model
# protocol mistake into a terminal child failure. The strict completion gate
# above remains responsible for rejecting it and authorizing automatic recovery.
"$GUARD" --allow-oversize >/dev/null

{
    printf '# Scratchpad\n'
    for i in $(seq 1 81); do printf 'line %s\n' "$i"; done
} > "$tmp"
if "$GUARD" PLAN_COMPLETE >/dev/null 2>&1; then
    echo 'test-scratchpad-guard: strict mode accepted an oversized scratchpad' >&2
    exit 1
fi
"$GUARD" PLAN_COMPLETE --allow-oversize >/dev/null 2>&1

echo 'test: scratchpad guard checks passed'
