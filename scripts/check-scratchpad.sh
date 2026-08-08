#!/usr/bin/env bash
# Keep the tracked Ralph handoff concise and free of reserved completion tokens.
set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
PROJECT_ROOT=$(cd -- "$SCRIPT_DIR/.." && pwd)
SCRATCHPAD=${FACTORY_SCRATCHPAD_PATH:-$PROJECT_ROOT/.ralph/agent/scratchpad.md}
TOKEN=
ALLOW_MISSING=false
MAX_LINES=${FACTORY_SCRATCHPAD_MAX_LINES:-80}
MAX_BYTES=${FACTORY_SCRATCHPAD_MAX_BYTES:-8192}

for arg in "$@"; do
    case "$arg" in
        --allow-missing)
            ALLOW_MISSING=true
            ;;
        --*)
            echo "scratchpad-guard: unknown option: $arg" >&2
            exit 2
            ;;
        *)
            if [[ -n "$TOKEN" ]]; then
                echo "scratchpad-guard: expected at most one completion token" >&2
                exit 2
            fi
            TOKEN=$arg
            ;;
    esac
done

[[ "$MAX_LINES" =~ ^[1-9][0-9]*$ ]] || { echo "scratchpad-guard: invalid line limit" >&2; exit 2; }
[[ "$MAX_BYTES" =~ ^[1-9][0-9]*$ ]] || { echo "scratchpad-guard: invalid byte limit" >&2; exit 2; }
if [[ ! -e "$SCRATCHPAD" && "$ALLOW_MISSING" == true ]]; then
    echo "scratchpad-guard: scratchpad absent at fresh-loop boundary; deferred until the next checkpoint"
    exit 0
fi
[[ -s "$SCRATCHPAD" ]] || { echo "scratchpad-guard: missing or empty scratchpad: $SCRATCHPAD" >&2; exit 1; }

lines=$(wc -l < "$SCRATCHPAD")
bytes=$(wc -c < "$SCRATCHPAD")
(( lines <= MAX_LINES )) || {
    echo "scratchpad-guard: scratchpad has $lines lines; replace it with one handoff of at most $MAX_LINES lines" >&2
    exit 1
}
(( bytes <= MAX_BYTES )) || {
    echo "scratchpad-guard: scratchpad has $bytes bytes; limit is $MAX_BYTES" >&2
    exit 1
}

documents=$(grep -Ec '^#[[:space:]]+' "$SCRATCHPAD" || true)
(( documents == 1 )) || {
    echo "scratchpad-guard: scratchpad must contain exactly one level-one handoff document; found $documents" >&2
    exit 1
}
if [[ -n "$TOKEN" ]] && grep -Fq -- "$TOKEN" "$SCRATCHPAD"; then
    echo "scratchpad-guard: reserved completion token '$TOKEN' must not appear in the scratchpad" >&2
    exit 1
fi

echo "scratchpad-guard: concise current handoff accepted ($lines lines, $bytes bytes)"
