#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
PROJECT_ROOT=$(cd -- "$SCRIPT_DIR/.." && pwd)
GUARD="$PROJECT_ROOT/scripts/check-scratchpad.sh"
tmp=$(mktemp)
trap 'rm -f "$tmp"' EXIT
export FACTORY_SCRATCHPAD_PATH=$tmp

cat > "$tmp" <<'EOF'
# Scratchpad

## Current handoff

- One concise recovery fact.
EOF
"$GUARD" PLAN_COMPLETE >/dev/null

printf '# Scratchpad\n\n## First\n\n## Second\n' > "$tmp"
if "$GUARD" PLAN_COMPLETE >/dev/null 2>&1; then
    echo 'test-scratchpad-guard: appended handoffs were accepted' >&2
    exit 1
fi

printf '# Scratchpad\n\n### Historical iteration\n' > "$tmp"
if "$GUARD" PLAN_COMPLETE >/dev/null 2>&1; then
    echo 'test-scratchpad-guard: historical subsection was accepted' >&2
    exit 1
fi

printf '# Scratchpad\n\n## Current handoff\n\nPLAN_COMPLETE\n' > "$tmp"
if "$GUARD" PLAN_COMPLETE >/dev/null 2>&1; then
    echo 'test-scratchpad-guard: reserved completion token was accepted' >&2
    exit 1
fi

{
    printf '# Scratchpad\n'
    for i in $(seq 1 81); do printf 'line %s\n' "$i"; done
} > "$tmp"
if "$GUARD" PLAN_COMPLETE >/dev/null 2>&1; then
    echo 'test-scratchpad-guard: oversized scratchpad was accepted' >&2
    exit 1
fi

echo 'test: scratchpad guard checks passed'
