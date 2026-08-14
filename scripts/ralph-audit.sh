#!/usr/bin/env bash
# Supervise one independent campaign gap audit.
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
        -h|--help) echo "Usage: scripts/ralph-audit.sh [--resume] [--no-tui]"; exit 0 ;;
        *) echo "ralph-audit: unknown option '$1'" >&2; exit 2 ;;
    esac
done

cd -- "$PROJECT_ROOT"
command -v "$RALPH_BIN" >/dev/null || { echo "ralph-audit: Ralph executable not found: $RALPH_BIN" >&2; exit 2; }
command -v pi2 >/dev/null || { echo "ralph-audit: pi2 is not available in this shell" >&2; exit 2; }
./scripts/branch-guard.sh
./scripts/check-factory-environment.py
if [[ -z ${FACTORY_CAMPAIGN_AUDIT_ROUND:-} || -z ${FACTORY_CAMPAIGN_AUDIT_BASE:-} ]]; then
    [[ -x scripts/ralph-campaign-state.py ]] || { echo "ralph-audit: campaign state helper is unavailable" >&2; exit 1; }
    [[ $(./scripts/ralph-campaign-state.py get status) == active && $(./scripts/ralph-campaign-state.py get phase) == audit ]] || {
        echo "ralph-audit: no active campaign audit binding" >&2; exit 1;
    }
    campaign_round=$(./scripts/ralph-campaign-state.py get round)
    FACTORY_CAMPAIGN_AUDIT_ROUND=$campaign_round
    FACTORY_CAMPAIGN_AUDIT_BASE=$(./scripts/ralph-campaign-state.py get "rounds.$((campaign_round - 1)).verification_commit")
    export FACTORY_CAMPAIGN_AUDIT_ROUND FACTORY_CAMPAIGN_AUDIT_BASE
fi
[[ $FACTORY_CAMPAIGN_AUDIT_ROUND =~ ^[1-9][0-9]*$ && $FACTORY_CAMPAIGN_AUDIT_BASE =~ ^[0-9a-f]{40}$ ]] || {
    echo "ralph-audit: invalid campaign audit binding" >&2; exit 1;
}
./scripts/campaign-audit-scope-guard.sh

# shellcheck source=scripts/factory-lock.sh
source "$SCRIPT_DIR/factory-lock.sh"
# shellcheck source=scripts/ralph-supervision.sh
source "$SCRIPT_DIR/ralph-supervision.sh"
factory_lock_acquire "$PROJECT_ROOT/.factory-lock"
mkdir -p .factory-state
printf '%s\n' campaign-audit > .factory-state/loop-mode

finish_audit_cycle() {
    if ./scripts/final-gate.sh --campaign-audit; then :; else return $?; fi
    local payload
    payload=$(printf '{"loop":{"workspace":"%s","id":"campaign-audit-final"},"iteration":{"current":"final"}}' "$PROJECT_ROOT")
    if printf '%s' "$payload" | ./scripts/git-commit-hook.sh --campaign-audit; then :; else return $?; fi
    [[ -z $(git status --porcelain --untracked-files=normal) ]] || {
        echo "ralph-audit: completion left a dirty Git tree" >&2
        return 1
    }
    echo "ralph-audit: independent audit completed"
}

while true; do
    ./scripts/ollama-usage-guard.sh --wait
    ralph_supervision_begin campaign-audit
    command=("$RALPH_BIN" -c .factory/ralph/audit.yml run --exclusive)
    $RESUME && command+=(--continue)
    $TUI || command+=(--no-tui)
    set +e
    "${command[@]}"
    rc=$?
    set -e

    if (( rc == 0 )); then
        finish_audit_cycle
        exit 0
    fi
    if (( rc >= 128 )); then
        echo "ralph-audit: interrupted; resume with ./scripts/ralph-recover.sh --mode campaign-audit" >&2
        exit "$rc"
    fi
    set +e
    rejected_loop_id=$(ralph_supervision_consume_rejection campaign-audit "$PROJECT_ROOT")
    rejection_rc=$?
    set -e
    if (( rejection_rc == 0 )); then
        ralph_supervision_allow_completion_recovery || { recovery_rc=$?; exit "$recovery_rc"; }
        echo "ralph-audit: final gate rejected premature completion; continuing audit" >&2
        ./scripts/ralph-recover.sh --mode campaign-audit --loop-id "$rejected_loop_id" --prepare-only
        RESUME=true
        continue
    elif (( rejection_rc != 1 )); then
        echo "ralph-audit: invalid completion-rejection marker; refusing automatic recovery" >&2
        exit 1
    fi
    set +e
    ./scripts/ollama-usage-guard.sh --check
    quota_rc=$?
    set -e
    if (( quota_rc == 1 )); then
        ./scripts/ollama-usage-guard.sh --wait
        ./scripts/ralph-recover.sh --mode campaign-audit --prepare-only
        RESUME=true
        continue
    elif (( quota_rc != 0 )); then
        echo "ralph-audit: quota status check failed with status $quota_rc" >&2
        exit "$quota_rc"
    fi
    stale_diagnostics=$(mktemp)
    set +e
    ./scripts/final-gate.sh --campaign-audit >"$stale_diagnostics" 2>&1
    gate_rc=$?
    set -e
    if (( $(wc -c < "$stale_diagnostics") > 65536 )); then
        rm -f -- "$stale_diagnostics"
        echo "ralph-audit: final-gate diagnostics exceeded the recovery limit" >&2
        exit 1
    fi
    cat "$stale_diagnostics" >&2
    if (( gate_rc == 0 )); then
        rm -f -- "$stale_diagnostics"
        if finish_audit_cycle; then :; else final_rc=$?; echo "ralph-audit: finalization failed after a passing gate" >&2; exit "$final_rc"; fi
        echo "ralph-audit: accepted valid audit artifacts after Ralph exited with status $rc" >&2
        exit 0
    elif (( gate_rc >= 128 )); then
        rm -f -- "$stale_diagnostics"
        exit "$gate_rc"
    elif (( gate_rc != 1 )); then
        rm -f -- "$stale_diagnostics"
        echo "ralph-audit: final gate failed with infrastructure status $gate_rc" >&2
        exit "$gate_rc"
    fi
    set +e
    ralph_supervision_recover_stale campaign-audit "$PROJECT_ROOT"
    stale_rc=$?
    set -e
    if (( stale_rc == 0 )); then
        rm -f -- "$stale_diagnostics"
        ./scripts/ralph-recover.sh --mode campaign-audit --prepare-only
        RESUME=true
        continue
    fi
    rm -f -- "$stale_diagnostics"
    (( stale_rc == 1 )) || { echo "ralph-audit: stale recovery was rejected" >&2; exit "$stale_rc"; }
    echo "ralph-audit: Ralph exited with status $rc for a non-quota failure" >&2
    exit "$rc"
done
