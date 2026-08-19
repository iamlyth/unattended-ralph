#!/usr/bin/env bash
# Supervise the single-writer implementation loop and resume after quota resets.
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
        -h|--help)
            echo "Usage: scripts/ralph-run.sh [--resume] [--no-tui]"
            exit 0
            ;;
        *) echo "ralph-run: unknown option '$1'" >&2; exit 2 ;;
    esac
done

cd -- "$PROJECT_ROOT"
# shellcheck source=scripts/factory-lock.sh
source "$SCRIPT_DIR/factory-lock.sh"
factory_lock_bootstrap "$PROJECT_ROOT" "$PROJECT_ROOT/scripts/ralph-run.sh" "${ORIGINAL_ARGS[@]}"
command -v "$RALPH_BIN" >/dev/null || { echo "ralph-run: Ralph executable not found: $RALPH_BIN" >&2; exit 2; }
command -v pi2 >/dev/null || { echo "ralph-run: pi2 is not available in this shell" >&2; exit 2; }
factory_lock_run_untrusted ./scripts/branch-guard.sh
factory_lock_run_untrusted ./scripts/check-factory-environment.py
factory_lock_run_untrusted ./scripts/check-plan-freshness.sh

if [[ "$RESUME" == false ]] && [[ -n $(git status --porcelain --untracked-files=normal) ]]; then
    echo "ralph-run: start from a clean Git tree; commit the spec and implementation plan first" >&2
    exit 1
fi

# shellcheck source=scripts/ralph-supervision.sh
source "$SCRIPT_DIR/ralph-supervision.sh"
factory_lock_acquire "$PROJECT_ROOT"
ralph_supervision_prepare_state_directory
"$SCRIPT_DIR/factory-state-file.py" write loop-mode implementation
ralph_supervision_initialize implementation "$RESUME"
CONTINUE=false
if $RESUME && ralph_supervision_should_continue implementation; then CONTINUE=true; fi

finish_implementation_cycle() {
    local payload head
    if ./scripts/ralph-final-state.py verify implementation >/dev/null 2>&1; then
        echo "ralph-run: implementation loop completed"
        return 0
    fi
    payload=$(printf '{"loop":{"workspace":"%s","id":"implementation-final"},"iteration":{"current":"final"}}' "$PROJECT_ROOT")
    if printf '%s' "$payload" | factory_lock_run_untrusted \
            ./scripts/git-commit-hook.sh --final-handoff; then :; else return $?; fi
    if factory_lock_run_untrusted env FACTORY_FINAL_GATE_ATTEST=1 ./scripts/final-gate.sh --implementation; then :; else return $?; fi
    head=$(git rev-parse HEAD)
    ./scripts/ralph-final-state.py attest implementation "$head" >/dev/null
    echo "ralph-run: implementation loop completed"
}

while true; do
    factory_lock_run_untrusted ./scripts/ollama-usage-guard.sh --wait
    CONTINUE=false
    if $RESUME && ralph_supervision_should_continue implementation; then CONTINUE=true; fi
    ralph_supervision_begin implementation

    command=("$RALPH_BIN" -c .factory/ralph/implementation.yml run --exclusive)
    $CONTINUE && command+=(--continue)
    $TUI || command+=(--no-tui)

    set +e
    factory_lock_run_untrusted "${command[@]}"
    rc=$?
    ralph_supervision_finish_attempt implementation
    boundary_rc=$?
    set -e
    if (( boundary_rc == 2 || (rc == 0 && boundary_rc != 0) )); then
        echo "ralph-run: launch/event boundary validation failed" >&2
        exit 2
    fi

    if (( rc == 0 )); then
        finish_implementation_cycle
        exit 0
    fi
    if (( rc >= 128 )); then
        echo "ralph-run: interrupted; resume later with ./scripts/ralph-recover.sh" >&2
        exit "$rc"
    fi
    set +e
    rejected_loop_id=$(ralph_supervision_consume_rejection implementation "$PROJECT_ROOT")
    rejection_rc=$?
    set -e
    if (( rejection_rc == 0 )); then
        ralph_supervision_allow_completion_recovery || { recovery_rc=$?; exit "$recovery_rc"; }
        echo "ralph-run: final gate rejected premature completion; continuing the active cycle" >&2
        ./scripts/ralph-recover.sh --mode implementation --loop-id "$rejected_loop_id" --prepare-only
        RESUME=true
        continue
    elif (( rejection_rc != 1 )); then
        echo "ralph-run: invalid completion-rejection marker; refusing automatic recovery" >&2
        exit 1
    fi

    set +e
    factory_lock_run_untrusted ./scripts/ollama-usage-guard.sh --check
    quota_rc=$?
    set -e
    if (( quota_rc == 1 )); then
        echo "ralph-run: backend stopped while quota is blocked; waiting before automatic continuation" >&2
        factory_lock_run_untrusted ./scripts/ollama-usage-guard.sh --wait
        ./scripts/ralph-recover.sh --prepare-only
        RESUME=true
        continue
    elif (( quota_rc != 0 )); then
        echo "ralph-run: quota status check failed with status $quota_rc" >&2
        exit "$quota_rc"
    fi

    stale_diagnostics=$(ralph_supervision_diagnostics_file implementation)
    set +e
    factory_lock_run_untrusted ./scripts/final-gate.sh --implementation >"$stale_diagnostics" 2>&1
    gate_rc=$?
    set -e
    if (( $(wc -c < "$stale_diagnostics") > 65536 )); then
        rm -f -- "$stale_diagnostics"
        echo "ralph-run: final-gate diagnostics exceeded the recovery limit" >&2
        exit 1
    fi
    cat "$stale_diagnostics" >&2
    if (( gate_rc == 0 )); then
        rm -f -- "$stale_diagnostics"
        if finish_implementation_cycle; then :; else final_rc=$?; echo "ralph-run: finalization failed after a passing gate" >&2; exit "$final_rc"; fi
        echo "ralph-run: accepted valid implementation artifacts after Ralph exited with status $rc" >&2
        exit 0
    elif (( gate_rc >= 128 )); then
        rm -f -- "$stale_diagnostics"
        exit "$gate_rc"
    elif (( gate_rc != 1 )); then
        rm -f -- "$stale_diagnostics"
        echo "ralph-run: final gate failed with infrastructure status $gate_rc" >&2
        exit "$gate_rc"
    fi
    set +e
    ralph_supervision_recover_stale implementation "$PROJECT_ROOT"
    stale_rc=$?
    set -e
    if (( stale_rc == 0 )); then
        rm -f -- "$stale_diagnostics"
        ./scripts/ralph-recover.sh --mode implementation --prepare-only
        RESUME=true
        continue
    fi
    rm -f -- "$stale_diagnostics"
    (( stale_rc == 1 )) || { echo "ralph-run: stale recovery was rejected" >&2; exit "$stale_rc"; }
    echo "ralph-run: Ralph exited with status $rc for a non-quota failure" >&2
    exit "$rc"
done
