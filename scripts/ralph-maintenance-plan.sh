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
factory_lock_acquire "$PROJECT_ROOT/.factory-lock"
mkdir -p .factory-state
printf '%s\n' maintenance-planning > .factory-state/loop-mode
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
with open('factory.toml', 'rb') as stream: print(tomllib.load(stream)['project']['spec'])
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
while true; do
    ./scripts/ollama-usage-guard.sh --wait
    command=("$RALPH_BIN" -c ralph.maintenance-plan.yml run --exclusive)
    $RESUME && command+=(--continue)
    $TUI || command+=(--no-tui)
    set +e
    "${command[@]}"
    rc=$?
    set -e
    if (( rc == 0 )); then
        ./scripts/final-gate.sh --maintenance-planning
        payload=$(printf '{"loop":{"workspace":"%s","id":"maintenance-planning-final"},"iteration":{"current":"final"}}' "$PROJECT_ROOT")
        printf '%s' "$payload" | ./scripts/git-commit-hook.sh --maintenance-plan
        [[ -z $(git status --porcelain --untracked-files=normal) ]] || { echo "ralph-maintenance-plan: completion left a dirty tree" >&2; exit 1; }
        ./scripts/check-maintenance-freshness.sh
        if [[ "$BUG_STATUS" == triaged ]]; then
            ./scripts/bug-ledger.py set-status "$BUG_ID" planned
            ledger_payload=$(printf '{"loop":{"workspace":"%s","id":"maintenance-planning-ledger"},"iteration":{"current":"planned"}}' "$PROJECT_ROOT")
            printf '%s' "$ledger_payload" | ./scripts/git-commit-hook.sh --maintenance-ledger
        fi
        [[ -z $(git status --porcelain --untracked-files=normal) ]] || { echo "ralph-maintenance-plan: ledger checkpoint left a dirty tree" >&2; exit 1; }
        ./scripts/check-maintenance-freshness.sh
        echo "ralph-maintenance-plan: plan committed and bug marked planned for $BUG_ID"
        exit 0
    fi
    if (( rc == 130 || rc == 143 )); then
        echo "ralph-maintenance-plan: interrupted; recover with --mode maintenance-planning" >&2
        exit "$rc"
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
    echo "ralph-maintenance-plan: Ralph failed with status $rc" >&2
    exit "$rc"
done
