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

while true; do
    ./scripts/ollama-usage-guard.sh --wait
    ralph_supervision_begin campaign-audit
    command=("$RALPH_BIN" -c ralph.audit.yml run --exclusive)
    $RESUME && command+=(--continue)
    $TUI || command+=(--no-tui)
    set +e
    "${command[@]}"
    rc=$?
    set -e

    if (( rc == 0 )); then
        ./scripts/final-gate.sh --campaign-audit
        payload=$(printf '{"loop":{"workspace":"%s","id":"campaign-audit-final"},"iteration":{"current":"final"}}' "$PROJECT_ROOT")
        printf '%s' "$payload" | ./scripts/git-commit-hook.sh --campaign-audit
        [[ -z $(git status --porcelain --untracked-files=normal) ]] || {
            echo "ralph-audit: completion left a dirty Git tree" >&2; exit 1;
        }
        echo "ralph-audit: independent audit completed"
        exit 0
    fi
    if (( rc == 130 || rc == 143 )); then
        echo "ralph-audit: interrupted; resume with ./scripts/ralph-recover.sh --mode campaign-audit" >&2
        exit "$rc"
    fi
    set +e
    rejected_loop_id=$(ralph_supervision_consume_rejection campaign-audit "$PROJECT_ROOT")
    rejection_rc=$?
    set -e
    if (( rejection_rc == 0 )); then
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
    fi
    echo "ralph-audit: Ralph exited with status $rc for a non-quota failure" >&2
    exit "$rc"
done
