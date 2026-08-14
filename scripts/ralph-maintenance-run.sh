#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
PROJECT_ROOT=$(cd -- "$SCRIPT_DIR/.." && pwd)
RALPH_BIN=${RALPH_BIN:-ralph}
RESUME=false
TUI=true
while (( $# > 0 )); do
    case "$1" in
        --resume) RESUME=true; shift ;;
        --no-tui) TUI=false; shift ;;
        -h|--help) echo "Usage: scripts/ralph-maintenance-run.sh [--resume] [--no-tui]"; exit 0 ;;
        *) echo "ralph-maintenance-run: unknown option '$1'" >&2; exit 2 ;;
    esac
done
cd -- "$PROJECT_ROOT"
command -v "$RALPH_BIN" >/dev/null || { echo "ralph-maintenance-run: Ralph executable not found: $RALPH_BIN" >&2; exit 2; }
command -v pi2 >/dev/null || { echo "ralph-maintenance-run: pi2 is unavailable" >&2; exit 2; }
./scripts/branch-guard.sh

# shellcheck source=scripts/factory-lock.sh
source "$SCRIPT_DIR/factory-lock.sh"
# shellcheck source=scripts/ralph-supervision.sh
source "$SCRIPT_DIR/ralph-supervision.sh"
factory_lock_acquire "$PROJECT_ROOT/.factory-lock"
mkdir -p .factory-state
./scripts/check-maintenance-freshness.sh
python3 - <<'PY'
import json, pathlib, subprocess
selection = pathlib.Path('.factory-state/maintenance-bug-id').read_text(encoding='utf-8').strip()
record = json.loads(subprocess.check_output(
    ['./scripts/bug-ledger.py', 'show', selection], text=True,
).split('\nfingerprint:', 1)[0])
if record['status'] not in {'planned', 'in_progress'}:
    raise SystemExit(f'ralph-maintenance-run: selected bug must be planned or in_progress, not {record["status"]}')
PY
if [[ "$RESUME" == false && -n $(git status --porcelain --untracked-files=normal) ]]; then
    echo "ralph-maintenance-run: start from a clean Git tree with a committed maintenance plan" >&2
    exit 1
fi
# Repeat freshness and cleanliness under the held lock immediately before launch.
./scripts/check-maintenance-freshness.sh >/dev/null
[[ -z $(git status --porcelain --untracked-files=normal) || "$RESUME" == true ]] || {
    echo "ralph-maintenance-run: tree changed before launch" >&2; exit 1;
}
printf '%s\n' maintenance > .factory-state/loop-mode
while true; do
    ./scripts/ollama-usage-guard.sh --wait
    ralph_supervision_begin maintenance
    command=("$RALPH_BIN" -c .factory/ralph/maintenance.yml run --exclusive)
    $RESUME && command+=(--continue)
    $TUI || command+=(--no-tui)
    set +e
    "${command[@]}"
    rc=$?
    set -e
    if (( rc == 0 )); then
        ./scripts/final-gate.sh --maintenance
        payload=$(printf '{"loop":{"workspace":"%s","id":"maintenance-final"},"iteration":{"current":"final"}}' "$PROJECT_ROOT")
        printf '%s' "$payload" | ./scripts/git-commit-hook.sh --maintenance
        [[ -z $(git status --porcelain --untracked-files=normal) ]] || { echo "ralph-maintenance-run: completion left a dirty tree" >&2; exit 1; }
        echo "ralph-maintenance-run: maintenance cycle completed"
        exit 0
    fi
    if (( rc == 130 || rc == 143 )); then
        echo "ralph-maintenance-run: interrupted; recover with --mode maintenance" >&2
        exit "$rc"
    fi
    set +e
    rejected_loop_id=$(ralph_supervision_consume_rejection maintenance "$PROJECT_ROOT")
    rejection_rc=$?
    set -e
    if (( rejection_rc == 0 )); then
        echo "ralph-maintenance-run: final gate rejected premature completion; continuing maintenance" >&2
        ./scripts/ralph-recover.sh --mode maintenance --loop-id "$rejected_loop_id" --prepare-only
        RESUME=true
        continue
    elif (( rejection_rc != 1 )); then
        echo "ralph-maintenance-run: invalid completion-rejection marker; refusing automatic recovery" >&2
        exit 1
    fi
    set +e
    ./scripts/ollama-usage-guard.sh --check
    quota_rc=$?
    set -e
    if (( quota_rc == 1 )); then
        ./scripts/ollama-usage-guard.sh --wait
        ./scripts/ralph-recover.sh --mode maintenance --prepare-only
        RESUME=true
        continue
    fi
    echo "ralph-maintenance-run: Ralph failed with status $rc" >&2
    exit "$rc"
done
