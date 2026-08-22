#!/usr/bin/env bash
# Checkpoint the preceding Ralph iteration using lifecycle metadata from stdin.
set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
PROJECT_ROOT=$(cd -- "$SCRIPT_DIR/.." && pwd)
MODE=implementation
FINAL_HANDOFF=false
for arg in "$@"; do
    case "$arg" in
        --plan-only) MODE=planning ;;
        --maintenance-plan) MODE=maintenance-planning ;;
        --campaign-audit) MODE=campaign-audit ;;
        --maintenance) MODE=maintenance ;;
        --maintenance-ledger) MODE=maintenance-ledger ;;
        --final-handoff) FINAL_HANDOFF=true ;;
        *) echo "ralph-checkpoint: unknown argument '$arg'" >&2; exit 2 ;;
    esac
done
if [[ "$MODE" == maintenance-ledger && "$FINAL_HANDOFF" == true ]]; then
    echo "ralph-checkpoint: maintenance-ledger does not support final handoffs" >&2
    exit 2
fi

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
[[ $(realpath -e -- "$WORKSPACE") == "$(realpath -e -- "$PROJECT_ROOT")" ]] || {
    echo "ralph-checkpoint: hook workspace does not match this repository" >&2
    exit 2
}
cd -- "$PROJECT_ROOT"

SCRATCHPAD=.ralph/agent/scratchpad.md
if [[ "$FINAL_HANDOFF" == true ]]; then
    python3 - "$SCRATCHPAD" <<'PY'
import subprocess, sys
scratchpad = sys.argv[1]
raw = subprocess.check_output(
    ['git', 'status', '--porcelain=v1', '-z', '--untracked-files=normal']
)
records = raw.split(b'\0')
index = 0
while index < len(records):
    record = records[index]
    index += 1
    if not record:
        continue
    if len(record) < 4 or record[2:3] != b' ':
        raise SystemExit('ralph-checkpoint: cannot parse final-handoff Git status')
    status = record[:2].decode('ascii', 'strict')
    try:
        path = record[3:].decode('utf-8', 'surrogateescape')
    except UnicodeError:
        raise SystemExit('ralph-checkpoint: final-handoff path is not valid UTF-8')
    if 'R' in status or 'C' in status:
        raise SystemExit('ralph-checkpoint: final handoff must not rename or copy paths')
    if path != scratchpad:
        raise SystemExit(f'ralph-checkpoint: final handoff contains forbidden dirty path: {path}')
PY
    token=
    case "$MODE" in
        planning) token=PLAN_COMPLETE ;;
        implementation) token=LOOP_COMPLETE ;;
        campaign-audit) token=AUDIT_COMPLETE ;;
        maintenance-planning) token=MAINTENANCE_PLAN_COMPLETE ;;
        maintenance) token=MAINTENANCE_COMPLETE ;;
    esac
    ./scripts/check-scratchpad.sh "$token"
else
    git restore --staged -- .ralph 2>/dev/null || true
fi

case "$MODE" in
    planning) git add -- .factory/artifacts/implementation-plan.md ;;
    maintenance-planning) git add -- .factory/artifacts/maintenance-plan.md ;;
    campaign-audit) git add -- .factory/artifacts/campaign-audit.md ;;
    maintenance-ledger) git add -- .factory/bugs/open.md .factory/bugs/closed.md ;;
    implementation|maintenance)
        if [[ "$FINAL_HANDOFF" != true ]]; then
            git add -A -- . ':(exclude).ralph/**'
        fi
        ;;
esac
if [[ "$MODE" != maintenance-ledger && -f "$SCRATCHPAD" ]]; then
    git add -f -- "$SCRATCHPAD"
fi

mapfile -t STAGED < <(git diff --cached --name-only)
for path in "${STAGED[@]}"; do
    case "$MODE:$path" in
        planning:.factory/artifacts/implementation-plan.md|planning:.ralph/agent/scratchpad.md) ;;
        maintenance-planning:.factory/artifacts/maintenance-plan.md|maintenance-planning:.ralph/agent/scratchpad.md) ;;
        campaign-audit:.factory/artifacts/campaign-audit.md|campaign-audit:.ralph/agent/scratchpad.md) ;;
        maintenance-ledger:.factory/bugs/open.md|maintenance-ledger:.factory/bugs/closed.md) ;;
        implementation:*|maintenance:*) ;;
        *) echo "ralph-checkpoint: $MODE checkpoint contains forbidden staged path: $path" >&2; exit 1 ;;
    esac
    if [[ "$FINAL_HANDOFF" == true && "$path" != "$SCRATCHPAD" ]]; then
        echo "ralph-checkpoint: final handoff may stage only $SCRATCHPAD (found $path)" >&2
        exit 1
    fi
done

if git diff --cached --quiet; then
    if [[ "$FINAL_HANDOFF" == true ]]; then
        [[ ${FACTORY_RALPH_CYCLE_ID:-} =~ ^[0-9a-f]{64}$ ]] || {
            echo "ralph-checkpoint: final handoff requires a durable lifecycle cycle ID" >&2; exit 1;
        }
        ./scripts/ralph-final-state.py ensure-checkpoint "$MODE" "$(git rev-parse HEAD)" >/dev/null
    fi
    exit 0
fi

# An implementation checkpoint may never commit completion prose or stale
# lifecycle claims: the durable context summary must stay contamination-free
# while the plan advances during the active cycle.
if [[ "$MODE" == implementation && "$FINAL_HANDOFF" != true && -f .factory/artifacts/context-summary.md && -f scripts/check-context-summary.py ]]; then
    ./scripts/check-context-summary.py --contamination-only
fi

# An ordinary iteration checkpoint must not turn recovery metadata into Git
# progress. Keep the newest non-empty scratchpad in the worktree for --resume.
if [[ "$FINAL_HANDOFF" != true && ${#STAGED[@]} -eq 1 && ${STAGED[0]} == "$SCRATCHPAD" ]]; then
    git restore --staged -- "$SCRATCHPAD"
    echo "ralph-checkpoint: preserved scratchpad-only handoff without a commit"
    exit 0
fi

if [[ "$FINAL_HANDOFF" == true ]]; then
    [[ ${FACTORY_RALPH_CYCLE_ID:-} =~ ^[0-9a-f]{64}$ ]] || {
        echo "ralph-checkpoint: final handoff requires a durable lifecycle cycle ID" >&2; exit 1;
    }
    ./scripts/ralph-final-state.py allow-checkpoint-commit "$MODE" "$(git rev-parse HEAD)" >/dev/null
fi

CONTEXT=""
if [[ -f "$SCRATCHPAD" ]]; then
    CONTEXT=$(awk '/^##[[:space:]]+/{line=$0} END{sub(/^##[[:space:]]+/, "", line); print line}' "$SCRATCHPAD" | head -c 48)
fi
if [[ -z "$CONTEXT" ]]; then
    CONTEXT="$MODE checkpoint"
fi
SUBJECT=$(printf 'ralph %s iteration %s: %s' "$MODE" "$ITERATION" "$CONTEXT" | head -c 72)

# The one-shot final-handoff authorization: written only here, after
# allow-checkpoint-commit has enforced at-most-one per durable cycle, and
# consumed by the fail-closed pre-commit boundary (scripts/git-commit-guard.sh)
# at the actual commit. The EXIT trap removes a never-consumed token so a
# failed commit can never leave a reusable authorization behind.
TOKEN_NAME=final-handoff-authorization.json
write_final_handoff_token() {
    python3 - "$MODE" "$FACTORY_RALPH_CYCLE_ID" "$TOKEN_NAME" <<'PY'
import secrets, sys
from pathlib import Path
sys.path.insert(0, str((Path.cwd() / 'scripts').resolve()))
from factory_state_io import atomic_write_json
mode, cycle, name = sys.argv[1:]
atomic_write_json(Path.cwd(), name, {
    'schema': 'ralph-final-handoff/v1',
    'mode': mode,
    'cycle_id': cycle,
    'nonce': secrets.token_hex(32),
})
PY
}
remove_final_handoff_token() {
    python3 - "$TOKEN_NAME" <<'PY' || true
import sys
from pathlib import Path
sys.path.insert(0, str((Path.cwd() / 'scripts').resolve()))
from factory_state_io import remove
remove(Path.cwd(), sys.argv[1])
PY
}

if [[ "$FINAL_HANDOFF" == true ]]; then
    write_final_handoff_token
    trap remove_final_handoff_token EXIT
fi

if [[ "$FINAL_HANDOFF" == true ]]; then
    printf '%s\n\nLoop: %s\nMode: %s\nIteration: %s\nCycle: %s\n' \
        "$SUBJECT" "$LOOP_ID" "$MODE" "$ITERATION" "$FACTORY_RALPH_CYCLE_ID" | \
        git commit -F -
else
    printf '%s\n\nLoop: %s\nMode: %s\nIteration: %s\n' \
        "$SUBJECT" "$LOOP_ID" "$MODE" "$ITERATION" | git commit -F -
fi

if [[ "$FINAL_HANDOFF" == true ]]; then
    ./scripts/ralph-final-state.py ensure-checkpoint "$MODE" "$(git rev-parse HEAD)" >/dev/null
fi
