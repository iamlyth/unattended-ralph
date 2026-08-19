#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
PROJECT_ROOT=$(cd -- "$SCRIPT_DIR/.." && pwd)
RALPH_BIN=${RALPH_BIN:-ralph}
RESUME=false
TUI=true
ORIGINAL_ARGS=("$@")
while (( $# > 0 )); do
    case "$1" in
        --resume) RESUME=true; shift ;;
        --no-tui) TUI=false; shift ;;
        -h|--help) echo "Usage: scripts/ralph-maintenance-run.sh [--resume] [--no-tui]"; exit 0 ;;
        *) echo "ralph-maintenance-run: unknown option '$1'" >&2; exit 2 ;;
    esac
done
cd -- "$PROJECT_ROOT"
# shellcheck source=scripts/factory-lock.sh
source "$SCRIPT_DIR/factory-lock.sh"
factory_lock_bootstrap "$PROJECT_ROOT" "$PROJECT_ROOT/scripts/ralph-maintenance-run.sh" "${ORIGINAL_ARGS[@]}"
command -v "$RALPH_BIN" >/dev/null || { echo "ralph-maintenance-run: Ralph executable not found: $RALPH_BIN" >&2; exit 2; }
command -v pi2 >/dev/null || { echo "ralph-maintenance-run: pi2 is unavailable" >&2; exit 2; }
factory_lock_run_untrusted ./scripts/branch-guard.sh

# shellcheck source=scripts/ralph-supervision.sh
source "$SCRIPT_DIR/ralph-supervision.sh"
factory_lock_acquire "$PROJECT_ROOT"
ralph_supervision_prepare_state_directory
factory_lock_run_untrusted ./scripts/check-maintenance-freshness.sh
FACTORY_MAINTENANCE_BUG_ID=$("$SCRIPT_DIR/factory-state-file.py" read maintenance-bug-id)
export FACTORY_MAINTENANCE_BUG_ID
python3 - <<'PY'
import json, os, subprocess
selection = os.environ['FACTORY_MAINTENANCE_BUG_ID']
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
factory_lock_run_untrusted ./scripts/check-maintenance-freshness.sh >/dev/null
[[ -z $(git status --porcelain --untracked-files=normal) || "$RESUME" == true ]] || {
    echo "ralph-maintenance-run: tree changed before launch" >&2; exit 1;
}
"$SCRIPT_DIR/factory-state-file.py" write loop-mode maintenance
ralph_supervision_initialize maintenance "$RESUME"
CONTINUE=false
if $RESUME && ralph_supervision_should_continue maintenance; then CONTINUE=true; fi

finish_maintenance_cycle() {
    local payload head
    if ./scripts/ralph-final-state.py verify maintenance >/dev/null 2>&1; then
        echo "ralph-maintenance-run: maintenance cycle completed"
        return 0
    fi
    payload=$(printf '{"loop":{"workspace":"%s","id":"maintenance-final"},"iteration":{"current":"final"}}' "$PROJECT_ROOT")
    if printf '%s' "$payload" | factory_lock_run_untrusted \
            ./scripts/git-commit-hook.sh --maintenance --final-handoff; then :; else return $?; fi
    if factory_lock_run_untrusted env FACTORY_FINAL_GATE_ATTEST=1 ./scripts/final-gate.sh --maintenance; then :; else return $?; fi
    head=$(git rev-parse HEAD)
    ./scripts/ralph-final-state.py attest maintenance "$head" >/dev/null
    echo "ralph-maintenance-run: maintenance cycle completed"
}

while true; do
    factory_lock_run_untrusted ./scripts/ollama-usage-guard.sh --wait
    CONTINUE=false
    if $RESUME && ralph_supervision_should_continue maintenance; then CONTINUE=true; fi
    ralph_supervision_begin maintenance
    command=("$RALPH_BIN" -c .factory/ralph/maintenance.yml run --exclusive)
    $CONTINUE && command+=(--continue)
    $TUI || command+=(--no-tui)
    set +e
    factory_lock_run_untrusted "${command[@]}"
    rc=$?
    ralph_supervision_finish_attempt maintenance
    boundary_rc=$?
    set -e
    if (( boundary_rc == 2 || (rc == 0 && boundary_rc != 0) )); then
        echo "ralph-maintenance-run: launch/event boundary validation failed" >&2
        exit 2
    fi
    if (( rc == 0 )); then
        finish_maintenance_cycle
        exit 0
    fi
    if (( rc >= 128 )); then
        echo "ralph-maintenance-run: interrupted; recover with --mode maintenance" >&2
        exit "$rc"
    fi
    set +e
    rejected_loop_id=$(ralph_supervision_consume_rejection maintenance "$PROJECT_ROOT")
    rejection_rc=$?
    set -e
    if (( rejection_rc == 0 )); then
        ralph_supervision_allow_completion_recovery || { recovery_rc=$?; exit "$recovery_rc"; }
        echo "ralph-maintenance-run: final gate rejected premature completion; continuing maintenance" >&2
        ./scripts/ralph-recover.sh --mode maintenance --loop-id "$rejected_loop_id" --prepare-only
        RESUME=true
        continue
    elif (( rejection_rc != 1 )); then
        echo "ralph-maintenance-run: invalid completion-rejection marker; refusing automatic recovery" >&2
        exit 1
    fi
    set +e
    factory_lock_run_untrusted ./scripts/ollama-usage-guard.sh --check
    quota_rc=$?
    set -e
    if (( quota_rc == 1 )); then
        factory_lock_run_untrusted ./scripts/ollama-usage-guard.sh --wait
        ./scripts/ralph-recover.sh --mode maintenance --prepare-only
        RESUME=true
        continue
    elif (( quota_rc != 0 )); then
        echo "ralph-maintenance-run: quota status check failed with status $quota_rc" >&2
        exit "$quota_rc"
    fi
    stale_diagnostics=$(ralph_supervision_diagnostics_file maintenance)
    set +e
    factory_lock_run_untrusted ./scripts/final-gate.sh --maintenance >"$stale_diagnostics" 2>&1
    gate_rc=$?
    set -e
    if (( $(wc -c < "$stale_diagnostics") > 65536 )); then
        rm -f -- "$stale_diagnostics"
        echo "ralph-maintenance-run: final-gate diagnostics exceeded the recovery limit" >&2
        exit 1
    fi
    cat "$stale_diagnostics" >&2
    if (( gate_rc == 0 )); then
        rm -f -- "$stale_diagnostics"
        if finish_maintenance_cycle; then :; else final_rc=$?; echo "ralph-maintenance-run: finalization failed after a passing gate" >&2; exit "$final_rc"; fi
        echo "ralph-maintenance-run: accepted valid artifacts after Ralph exited with status $rc" >&2
        exit 0
    elif (( gate_rc >= 128 )); then
        rm -f -- "$stale_diagnostics"
        exit "$gate_rc"
    elif (( gate_rc != 1 )); then
        rm -f -- "$stale_diagnostics"
        echo "ralph-maintenance-run: final gate failed with infrastructure status $gate_rc" >&2
        exit "$gate_rc"
    fi
    set +e
    ralph_supervision_recover_stale maintenance "$PROJECT_ROOT"
    stale_rc=$?
    set -e
    if (( stale_rc == 0 )); then
        rm -f -- "$stale_diagnostics"
        ./scripts/ralph-recover.sh --mode maintenance --prepare-only
        RESUME=true
        continue
    fi
    rm -f -- "$stale_diagnostics"
    (( stale_rc == 1 )) || { echo "ralph-maintenance-run: stale recovery was rejected" >&2; exit "$stale_rc"; }
    echo "ralph-maintenance-run: Ralph failed with status $rc" >&2
    exit "$rc"
done
