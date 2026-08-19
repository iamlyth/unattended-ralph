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
        -h|--help)
            echo "Usage: scripts/ralph-plan.sh [--resume] [--no-tui]"
            exit 0
            ;;
        *) echo "ralph-plan: unknown option '$1'" >&2; exit 2 ;;
    esac
done

cd -- "$PROJECT_ROOT"
# shellcheck source=scripts/factory-lock.sh
source "$SCRIPT_DIR/factory-lock.sh"
factory_lock_bootstrap "$PROJECT_ROOT" "$PROJECT_ROOT/scripts/ralph-plan.sh" "${ORIGINAL_ARGS[@]}"
command -v "$RALPH_BIN" >/dev/null || { echo "ralph-plan: Ralph executable not found: $RALPH_BIN" >&2; exit 2; }
command -v pi2 >/dev/null || { echo "ralph-plan: pi2 is not available in this shell" >&2; exit 2; }
factory_lock_run_untrusted ./scripts/branch-guard.sh
factory_lock_run_untrusted ./scripts/check-factory-environment.py

SPEC=$(python3 - <<'PY'
import tomllib
with open('.factory/config.toml', 'rb') as stream:
    print(tomllib.load(stream)['project']['spec'])
PY
)
[[ -f "$SPEC" ]] || { echo "ralph-plan: missing specification '$SPEC'" >&2; exit 1; }
if ! git diff --quiet -- "$SPEC" || ! git diff --cached --quiet -- "$SPEC"; then
    echo "ralph-plan: commit '$SPEC' before planning" >&2
    exit 1
fi
if [[ "$RESUME" == false && -n $(git status --porcelain --untracked-files=normal) ]]; then
    echo "ralph-plan: start a fresh planning cycle from a clean Git tree" >&2
    exit 1
fi

# shellcheck source=scripts/ralph-supervision.sh
source "$SCRIPT_DIR/ralph-supervision.sh"
factory_lock_acquire "$PROJECT_ROOT"
factory_lock_run_untrusted ./scripts/branch-guard.sh
ralph_supervision_prepare_state_directory
STATE_FILE_HELPER="$SCRIPT_DIR/factory-state-file.py"
if ! git diff --quiet -- "$SPEC" || ! git diff --cached --quiet -- "$SPEC"; then
    echo "ralph-plan: specification changed while acquiring the planning lock" >&2
    exit 1
fi
if [[ "$RESUME" == false ]]; then
    [[ -z $(git status --porcelain --untracked-files=normal) ]] || {
        echo "ralph-plan: tree changed while starting the planning cycle" >&2; exit 1;
    }
    FACTORY_PLANNING_BASE_COMMIT=$(git rev-parse HEAD)
    "$STATE_FILE_HELPER" write planning-base-commit "$FACTORY_PLANNING_BASE_COMMIT"
    "$STATE_FILE_HELPER" write loop-mode planning
    ./scripts/initialize-plan-cycle.py specification --base "$FACTORY_PLANNING_BASE_COMMIT"
else
    FACTORY_PLANNING_BASE_COMMIT=$("$STATE_FILE_HELPER" read planning-base-commit) || {
        echo "ralph-plan: missing or unsafe planning base marker for resume" >&2; exit 1;
    }
    [[ $("$STATE_FILE_HELPER" read loop-mode) == planning ]] || {
        echo "ralph-plan: saved lifecycle is not specification planning" >&2; exit 1;
    }
    [[ -s .factory/artifacts/implementation-plan.md ]] || { echo "ralph-plan: missing planning draft for resume" >&2; exit 1; }
fi
export FACTORY_PLANNING_BASE_COMMIT
ralph_supervision_initialize planning "$RESUME"
CONTINUE=false
if $RESUME && ralph_supervision_should_continue planning; then CONTINUE=true; fi

finish_planning_cycle() {
    local payload head
    if ./scripts/ralph-final-state.py verify planning >/dev/null 2>&1; then
        printf 'ralph-plan: plan is committed and fresh for %s\n' "$SPEC"
        return 0
    fi
    payload=$(printf '{"loop":{"workspace":"%s","id":"planning-final"},"iteration":{"current":"final"}}' "$PROJECT_ROOT")
    if printf '%s' "$payload" | factory_lock_run_untrusted \
            ./scripts/git-commit-hook.sh --plan-only --final-handoff; then :; else return $?; fi
    if factory_lock_run_untrusted env FACTORY_FINAL_GATE_ATTEST=1 ./scripts/final-gate.sh --planning; then :; else return $?; fi
    head=$(git rev-parse HEAD)
    ./scripts/ralph-final-state.py attest planning "$head" >/dev/null
    printf 'ralph-plan: plan is committed and fresh for %s\n' "$SPEC"
}

while true; do
    factory_lock_run_untrusted ./scripts/ollama-usage-guard.sh --wait
    CONTINUE=false
    if $RESUME && ralph_supervision_should_continue planning; then CONTINUE=true; fi
    ralph_supervision_begin planning

    command=("$RALPH_BIN" -c .factory/ralph/plan.yml run --exclusive)
    $CONTINUE && command+=(--continue)
    $TUI || command+=(--no-tui)
    set +e
    factory_lock_run_untrusted "${command[@]}"
    rc=$?
    ralph_supervision_finish_attempt planning
    boundary_rc=$?
    set -e
    if (( boundary_rc == 2 || (rc == 0 && boundary_rc != 0) )); then
        echo "ralph-plan: launch/event boundary validation failed" >&2
        exit 2
    fi

    if (( rc == 0 )); then
        finish_planning_cycle
        exit 0
    fi
    if (( rc >= 128 )); then
        echo "ralph-plan: interrupted; resume with ./scripts/ralph-recover.sh --mode planning" >&2
        exit "$rc"
    fi
    set +e
    rejected_loop_id=$(ralph_supervision_consume_rejection planning "$PROJECT_ROOT")
    rejection_rc=$?
    set -e
    if (( rejection_rc == 0 )); then
        ralph_supervision_allow_completion_recovery || { recovery_rc=$?; exit "$recovery_rc"; }
        echo "ralph-plan: final gate rejected premature completion; continuing planning" >&2
        ./scripts/ralph-recover.sh --mode planning --loop-id "$rejected_loop_id" --prepare-only
        RESUME=true
        continue
    elif (( rejection_rc != 1 )); then
        echo "ralph-plan: invalid completion-rejection marker; refusing automatic recovery" >&2
        exit 1
    fi

    set +e
    factory_lock_run_untrusted ./scripts/ollama-usage-guard.sh --check
    quota_rc=$?
    set -e
    if (( quota_rc == 1 )); then
        echo "ralph-plan: quota blocked during planning; waiting before automatic continuation" >&2
        factory_lock_run_untrusted ./scripts/ollama-usage-guard.sh --wait
        ./scripts/ralph-recover.sh --mode planning --prepare-only
        RESUME=true
        continue
    elif (( quota_rc != 0 )); then
        echo "ralph-plan: quota status check failed with status $quota_rc" >&2
        exit "$quota_rc"
    fi
    stale_diagnostics=$(ralph_supervision_diagnostics_file planning)
    set +e
    factory_lock_run_untrusted ./scripts/final-gate.sh --planning >"$stale_diagnostics" 2>&1
    gate_rc=$?
    set -e
    if (( $(wc -c < "$stale_diagnostics") > 65536 )); then
        rm -f -- "$stale_diagnostics"
        echo "ralph-plan: final-gate diagnostics exceeded the recovery limit" >&2
        exit 1
    fi
    cat "$stale_diagnostics" >&2
    if (( gate_rc == 0 )); then
        rm -f -- "$stale_diagnostics"
        if finish_planning_cycle; then :; else final_rc=$?; echo "ralph-plan: finalization failed after a passing gate" >&2; exit "$final_rc"; fi
        echo "ralph-plan: accepted valid planning artifacts after Ralph exited with status $rc" >&2
        exit 0
    elif (( gate_rc >= 128 )); then
        rm -f -- "$stale_diagnostics"
        exit "$gate_rc"
    elif (( gate_rc != 1 )); then
        rm -f -- "$stale_diagnostics"
        echo "ralph-plan: final gate failed with infrastructure status $gate_rc" >&2
        exit "$gate_rc"
    fi
    set +e
    ralph_supervision_recover_stale planning "$PROJECT_ROOT"
    stale_rc=$?
    set -e
    if (( stale_rc == 0 )); then
        rm -f -- "$stale_diagnostics"
        ./scripts/ralph-recover.sh --mode planning --prepare-only
        RESUME=true
        continue
    fi
    rm -f -- "$stale_diagnostics"
    (( stale_rc == 1 )) || { echo "ralph-plan: stale recovery was rejected" >&2; exit "$stale_rc"; }
    echo "ralph-plan: Ralph exited with status $rc for a non-quota failure" >&2
    exit "$rc"
done
