#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
PROJECT_ROOT=$(cd -- "$SCRIPT_DIR/.." && pwd)
RALPH_BIN=${RALPH_BIN:-ralph}
RESUME=false
TUI=true
if [[ ${1:-} == -h || ${1:-} == --help ]]; then
    echo "Usage: scripts/ralph-maintenance-plan.sh BUG-ID [--resume] [--no-tui]"
    exit 0
fi
BUG_ID=${1:-}
[[ -n "$BUG_ID" && "$BUG_ID" != -* ]] || { echo "Usage: scripts/ralph-maintenance-plan.sh BUG-ID [--resume] [--no-tui]" >&2; exit 2; }
shift
while (( $# > 0 )); do
    case "$1" in
        --resume) RESUME=true; shift ;;
        --no-tui) TUI=false; shift ;;
        -h|--help) echo "Usage: scripts/ralph-maintenance-plan.sh BUG-ID [--resume] [--no-tui]"; exit 0 ;;
        *) echo "ralph-maintenance-plan: unknown option '$1'" >&2; exit 2 ;;
    esac
done

cd -- "$PROJECT_ROOT"
command -v "$RALPH_BIN" >/dev/null || { echo "ralph-maintenance-plan: Ralph executable not found: $RALPH_BIN" >&2; exit 2; }
command -v pi2 >/dev/null || { echo "ralph-maintenance-plan: pi2 is unavailable" >&2; exit 2; }
./scripts/branch-guard.sh

# Select and validate the cycle only while holding the single-writer lock. This
# closes the race between clean-tree inspection and writing volatile selection.
# shellcheck source=scripts/factory-lock.sh
source "$SCRIPT_DIR/factory-lock.sh"
# shellcheck source=scripts/ralph-supervision.sh
source "$SCRIPT_DIR/ralph-supervision.sh"
factory_lock_acquire "$PROJECT_ROOT/.factory-lock"
./scripts/branch-guard.sh
mkdir -p .factory-state
BASE_MARKER=.factory-state/maintenance-base-commit
if [[ "$RESUME" == false ]]; then
    printf '%s\n' maintenance-planning > .factory-state/loop-mode
    git rev-parse HEAD > "$BASE_MARKER"
else
    [[ -s "$BASE_MARKER" ]] || {
        echo "ralph-maintenance-plan: missing cycle base marker for resume" >&2; exit 1;
    }
    [[ $(cat .factory-state/loop-mode 2>/dev/null) == maintenance-planning ]] || {
        echo "ralph-maintenance-plan: saved lifecycle is not maintenance planning" >&2; exit 1;
    }
    [[ $(cat .factory-state/maintenance-bug-id 2>/dev/null) == "$BUG_ID" ]] || {
        echo "ralph-maintenance-plan: saved maintenance selection does not match $BUG_ID" >&2; exit 1;
    }
    [[ -s .factory/artifacts/maintenance-plan.md ]] || {
        echo "ralph-maintenance-plan: missing maintenance draft for resume" >&2; exit 1;
    }
fi
FACTORY_MAINTENANCE_BASE_COMMIT=$(tr -d '[:space:]' < "$BASE_MARKER")
export FACTORY_MAINTENANCE_BASE_COMMIT
./scripts/bug-ledger.py validate >/dev/null
BUG_STATUS=$(python3 - "$BUG_ID" "$RESUME" <<'PY'
import json, subprocess, sys
bug_id, resume = sys.argv[1], sys.argv[2] == 'true'
try:
    record = json.loads(subprocess.check_output(['./scripts/bug-ledger.py', 'show', bug_id], text=True).split('\nfingerprint:', 1)[0])
except subprocess.CalledProcessError as exc:
    raise SystemExit(exc.returncode)
allowed = {'triaged', 'planned'} if resume else {'triaged'}
if record['status'] not in allowed:
    expected = 'triaged (or planned when resuming)' if resume else 'triaged'
    raise SystemExit(f'ralph-maintenance-plan: selected bug must be {expected}, not {record["status"]}')
if record['contract_change']:
    raise SystemExit('ralph-maintenance-plan: contract-change bug requires human specification workflow')
print(record['status'])
PY
)
SPEC=$(python3 - <<'PY'
import tomllib
with open('.factory/config.toml', 'rb') as stream: print(tomllib.load(stream)['project']['spec'])
PY
)
[[ -f "$SPEC" ]] || { echo "ralph-maintenance-plan: missing specification '$SPEC'" >&2; exit 1; }
if ! git diff --quiet -- "$SPEC" || ! git diff --cached --quiet -- "$SPEC"; then
    echo "ralph-maintenance-plan: commit the specification before planning" >&2
    exit 1
fi
if [[ "$RESUME" == false && -n $(git status --porcelain --untracked-files=normal) ]]; then
    echo "ralph-maintenance-plan: start from a clean Git tree" >&2
    exit 1
fi
printf '%s\n' "$BUG_ID" > .factory-state/maintenance-bug-id
# Repeat all mutable preconditions after selection while the same lock remains held.
./scripts/bug-ledger.py validate >/dev/null
RECHECK_STATUS=$(python3 - "$BUG_ID" <<'PY'
import json, subprocess, sys
record = json.loads(subprocess.check_output(
    ['./scripts/bug-ledger.py', 'show', sys.argv[1]], text=True,
).split('\nfingerprint:', 1)[0])
if record['status'] not in {'triaged', 'planned'} or record['contract_change']:
    raise SystemExit('ralph-maintenance-plan: selected bug changed during locked validation')
print(record['status'])
PY
)
[[ "$RECHECK_STATUS" == "$BUG_STATUS" ]] || { echo "ralph-maintenance-plan: selected bug status changed during validation" >&2; exit 1; }
[[ -z $(git status --porcelain --untracked-files=normal) || "$RESUME" == true ]] || {
    echo "ralph-maintenance-plan: tree changed while selecting bug" >&2; exit 1;
}
if ! git diff --quiet -- "$SPEC" || ! git diff --cached --quiet -- "$SPEC"; then
    echo "ralph-maintenance-plan: specification changed while selecting bug" >&2; exit 1
fi
if [[ "$RESUME" == false ]]; then
    ./scripts/initialize-plan-cycle.py maintenance \
        --base "$FACTORY_MAINTENANCE_BASE_COMMIT" --bug-id "$BUG_ID"
fi

finish_maintenance_planning_cycle() {
    ./scripts/final-gate.sh --maintenance-planning || return 1
    local payload ledger_payload
    payload=$(printf '{"loop":{"workspace":"%s","id":"maintenance-planning-final"},"iteration":{"current":"final"}}' "$PROJECT_ROOT")
    printf '%s' "$payload" | ./scripts/git-commit-hook.sh --maintenance-plan || return 1
    [[ -z $(git status --porcelain --untracked-files=normal) ]] || {
        echo "ralph-maintenance-plan: completion left a dirty tree" >&2; return 1;
    }
    ./scripts/check-maintenance-freshness.sh || return 1
    if [[ "$BUG_STATUS" == triaged ]]; then
        ./scripts/bug-ledger.py set-status "$BUG_ID" planned || return 1
        ledger_payload=$(printf '{"loop":{"workspace":"%s","id":"maintenance-planning-ledger"},"iteration":{"current":"planned"}}' "$PROJECT_ROOT")
        printf '%s' "$ledger_payload" | ./scripts/git-commit-hook.sh --maintenance-ledger || return 1
    fi
    [[ -z $(git status --porcelain --untracked-files=normal) ]] || {
        echo "ralph-maintenance-plan: ledger checkpoint left a dirty tree" >&2; return 1;
    }
    ./scripts/check-maintenance-freshness.sh || return 1
    echo "ralph-maintenance-plan: plan committed and bug marked planned for $BUG_ID"
}

while true; do
    ./scripts/ollama-usage-guard.sh --wait
    ralph_supervision_begin maintenance-planning
    command=("$RALPH_BIN" -c .factory/ralph/maintenance-plan.yml run --exclusive)
    $RESUME && command+=(--continue)
    $TUI || command+=(--no-tui)
    set +e
    "${command[@]}"
    rc=$?
    set -e
    if (( rc == 0 )); then
        finish_maintenance_planning_cycle
        exit 0
    fi
    if (( rc == 130 || rc == 143 )); then
        echo "ralph-maintenance-plan: interrupted; recover with --mode maintenance-planning" >&2
        exit "$rc"
    fi
    set +e
    rejected_loop_id=$(ralph_supervision_consume_rejection maintenance-planning "$PROJECT_ROOT")
    rejection_rc=$?
    set -e
    if (( rejection_rc == 0 )); then
        echo "ralph-maintenance-plan: final gate rejected premature completion; continuing planning" >&2
        ./scripts/ralph-recover.sh --mode maintenance-planning --loop-id "$rejected_loop_id" --prepare-only
        RESUME=true
        continue
    elif (( rejection_rc != 1 )); then
        echo "ralph-maintenance-plan: invalid completion-rejection marker; refusing automatic recovery" >&2
        exit 1
    fi
    set +e
    ./scripts/ollama-usage-guard.sh --check
    quota_rc=$?
    set -e
    if (( quota_rc == 1 )); then
        ./scripts/ollama-usage-guard.sh --wait
        ./scripts/ralph-recover.sh --mode maintenance-planning --prepare-only
        RESUME=true
        continue
    fi
    if finish_maintenance_planning_cycle; then
        echo "ralph-maintenance-plan: accepted valid planning artifacts after Ralph exited with status $rc" >&2
        exit 0
    fi
    echo "ralph-maintenance-plan: Ralph failed with status $rc" >&2
    exit "$rc"
done
