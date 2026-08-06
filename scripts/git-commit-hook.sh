#!/usr/bin/env bash
# Checkpoint the preceding Ralph iteration using lifecycle metadata from stdin.
set -euo pipefail

MODE=implementation
case ${1:-} in
    "") ;;
    --plan-only) MODE=planning ;;
    --maintenance-plan) MODE=maintenance-planning ;;
    --maintenance) MODE=maintenance ;;
    --maintenance-ledger) MODE=maintenance-ledger ;;
    *) echo "ralph-checkpoint: unknown argument '$1'" >&2; exit 2 ;;
esac

HOOK_PAYLOAD=$(cat)
mapfile -t HOOK_META < <(python3 -c '
import json, os, sys
payload = json.load(sys.stdin)
loop = payload.get("loop", {})
iteration = payload.get("iteration", {})
print(loop.get("workspace") or os.getcwd())
print(loop.get("id") or "unknown-loop")
print(iteration.get("current", "unknown"))
' <<< "$HOOK_PAYLOAD")

(( ${#HOOK_META[@]} == 3 )) || { echo "ralph-checkpoint: invalid hook payload" >&2; exit 2; }
WORKSPACE=${HOOK_META[0]}
LOOP_ID=${HOOK_META[1]}
ITERATION=${HOOK_META[2]}
cd -- "$WORKSPACE"

SCRATCHPAD=.ralph/agent/scratchpad.md
git restore --staged -- .ralph 2>/dev/null || true

case "$MODE" in
    planning) git add -- IMPLEMENTATION_PLAN.md ;;
    maintenance-planning) git add -- MAINTENANCE_PLAN.md ;;
    maintenance-ledger) git add -- open-bugs.md closed-bugs.md ;;
    implementation|maintenance) git add -A -- . ':(exclude).ralph/**' ;;
esac
if [[ "$MODE" != maintenance-ledger && -f "$SCRATCHPAD" ]]; then
    git add -f -- "$SCRATCHPAD"
fi

if git diff --cached --quiet; then
    exit 0
fi

CONTEXT=""
if [[ -f "$SCRATCHPAD" ]]; then
    CONTEXT=$(awk '/^##[[:space:]]+/{line=$0} END{sub(/^##[[:space:]]+/, "", line); print line}' "$SCRATCHPAD" | head -c 48)
fi
if [[ -z "$CONTEXT" ]]; then
    CONTEXT="$MODE checkpoint"
fi
SUBJECT=$(printf 'ralph %s iteration %s: %s' "$MODE" "$ITERATION" "$CONTEXT" | head -c 72)

printf '%s\n\nLoop: %s\nMode: %s\nIteration: %s\n' \
    "$SUBJECT" "$LOOP_ID" "$MODE" "$ITERATION" | git commit -F - --no-verify
