#!/usr/bin/env bash
# Supervise one independent campaign gap audit.
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
        -h|--help) echo "Usage: scripts/ralph-audit.sh [--resume] [--no-tui]"; exit 0 ;;
        *) echo "ralph-audit: unknown option '$1'" >&2; exit 2 ;;
    esac
done

cd -- "$PROJECT_ROOT"
# Task 15/16 migration: the legacy Ralph control plane is frozen by the
# tracked .factory/ralph-freeze marker; the hidden .factory/loop control
# plane is authoritative. The freeze decision is routed through the retained
# hidden authority (.factory/loop/migration.py freeze --guard), which re-stats
# the marker no-follow and fails closed on an unsafe marker, closing the
# deprecated shell-level local-writer check/launch race. The operator-only
# FACTORY_RALPH_FREEZE_OVERRIDE=1 escape remains solely for recovering an
# already in-flight legacy cycle.
if [[ ${FACTORY_RALPH_FREEZE_OVERRIDE:-0} != 1 ]]; then
    freeze_rc=1
    if python3 "$PROJECT_ROOT/.factory/loop/migration.py" \
        --root "$PROJECT_ROOT" freeze --guard 2>/dev/null; then
        freeze_rc=0
    else
        freeze_rc=$?
    fi
    if (( freeze_rc == 0 )); then
        echo "ralph-audit: frozen: the legacy Ralph control plane is deprecated (Task 15 migration)" >&2
        echo "ralph-audit: the hidden .factory/loop control plane replaces it" >&2
        echo "ralph-audit: set FACTORY_RALPH_FREEZE_OVERRIDE=1 only to recover an in-flight cycle" >&2
        exit 2
    fi
    if (( freeze_rc != 1 )); then
        echo "ralph-audit: frozen: the freeze marker check failed (status $freeze_rc); refusing a legacy launch" >&2
        echo "ralph-audit: inspect .factory/ralph-freeze; FACTORY_RALPH_FREEZE_OVERRIDE=1 is only for bounded recovery" >&2
        exit 2
    fi
fi
# shellcheck source=scripts/factory-lock.sh
source "$SCRIPT_DIR/factory-lock.sh"
factory_lock_bootstrap "$PROJECT_ROOT" "$PROJECT_ROOT/scripts/ralph-audit.sh" "${ORIGINAL_ARGS[@]}"
factory_lock_run_untrusted ./scripts/install-git-commit-guard.sh
command -v "$RALPH_BIN" >/dev/null || { echo "ralph-audit: Ralph executable not found: $RALPH_BIN" >&2; exit 2; }
command -v pi2 >/dev/null || { echo "ralph-audit: pi2 is not available in this shell" >&2; exit 2; }
factory_lock_run_untrusted ./scripts/branch-guard.sh
factory_lock_run_untrusted ./scripts/check-factory-environment.py
[[ -x scripts/ralph-campaign-state.py ]] || { echo "ralph-audit: campaign state helper is unavailable" >&2; exit 1; }
binding=$(./scripts/ralph-campaign-state.py audit-binding) || exit $?
mapfile -t saved_binding < <(python3 - "$binding" <<'PY'
import json, sys
data = json.loads(sys.argv[1])
expected = {'round', 'base', 'runner_evidence_sha256'}
if set(data) != expected:
    raise SystemExit('ralph-audit: invalid reconstructed audit binding')
print(data['round']); print(data['base']); print(data['runner_evidence_sha256'])
PY
)
(( ${#saved_binding[@]} == 3 )) || { echo "ralph-audit: incomplete reconstructed audit binding" >&2; exit 1; }
for pair in \
    "${FACTORY_CAMPAIGN_AUDIT_ROUND:-}:${saved_binding[0]}:round" \
    "${FACTORY_CAMPAIGN_AUDIT_BASE:-}:${saved_binding[1]}:base" \
    "${FACTORY_CAMPAIGN_RUNNER_EVIDENCE_SHA256:-}:${saved_binding[2]}:runner evidence"; do
    IFS=: read -r supplied saved label <<<"$pair"
    [[ -z "$supplied" || "$supplied" == "$saved" ]] || {
        echo "ralph-audit: supplied $label binding does not match campaign state" >&2; exit 1;
    }
done
FACTORY_CAMPAIGN_AUDIT_ROUND=${saved_binding[0]}
FACTORY_CAMPAIGN_AUDIT_BASE=${saved_binding[1]}
FACTORY_CAMPAIGN_RUNNER_EVIDENCE_SHA256=${saved_binding[2]}
export FACTORY_CAMPAIGN_AUDIT_ROUND FACTORY_CAMPAIGN_AUDIT_BASE FACTORY_CAMPAIGN_RUNNER_EVIDENCE_SHA256
[[ $FACTORY_CAMPAIGN_AUDIT_ROUND =~ ^[1-9][0-9]*$ \
    && $FACTORY_CAMPAIGN_AUDIT_BASE =~ ^[0-9a-f]{40}$ \
    && $FACTORY_CAMPAIGN_RUNNER_EVIDENCE_SHA256 =~ ^[0-9a-f]{64}$ ]] || {
    echo "ralph-audit: invalid campaign audit binding" >&2; exit 1;
}
# The protected audit coordinator binding authorizes machine-receipt minting
# only inside this audit's bounded invocation; a bare model receipt call has
# no nonce and fails. Validate the coordinator state against the campaign.
FACTORY_CAMPAIGN_AUDIT_NONCE=$(python3 - <<'PY' || exit 1
import json, os, re, sys
from pathlib import Path
state = Path('.factory-state/audit-coordinator.json')
if state.is_symlink() or not state.is_file():
    raise SystemExit('ralph-audit: audit coordinator state is missing')
try:
    data = json.loads(state.read_text(encoding='utf-8'))
except (OSError, UnicodeError, json.JSONDecodeError) as exc:
    raise SystemExit(f'ralph-audit: invalid audit coordinator state: {exc}')
expected = {'schema', 'round', 'base_commit', 'nonce', 'created_at'}
if (not isinstance(data, dict) or set(data) != expected
        or data.get('schema') != 'ralph-audit-coordinator/v1'
        or data.get('round') != int(os.environ['FACTORY_CAMPAIGN_AUDIT_ROUND'])
        or data.get('base_commit') != os.environ['FACTORY_CAMPAIGN_AUDIT_BASE']
        or not re.fullmatch(r'[0-9a-f]{64}', data.get('nonce') or '')):
    raise SystemExit('ralph-audit: audit coordinator binding does not match campaign state')
print(data['nonce'])
PY
)
export FACTORY_CAMPAIGN_AUDIT_NONCE
factory_lock_run_untrusted ./scripts/campaign-audit-scope-guard.sh

# shellcheck source=scripts/ralph-supervision.sh
source "$SCRIPT_DIR/ralph-supervision.sh"
factory_lock_acquire "$PROJECT_ROOT"
ralph_supervision_prepare_state_directory
"$SCRIPT_DIR/factory-state-file.py" write loop-mode campaign-audit
ralph_supervision_initialize campaign-audit "$RESUME"
CONTINUE=false
if $RESUME && ralph_supervision_should_continue campaign-audit; then CONTINUE=true; fi

finish_audit_cycle() {
    local payload head
    if ./scripts/ralph-final-state.py verify campaign-audit >/dev/null 2>&1; then
        echo "ralph-audit: independent audit completed"
        return 0
    fi
    payload=$(printf '{"loop":{"workspace":"%s","id":"campaign-audit-final"},"iteration":{"current":"final"}}' "$PROJECT_ROOT")
    if printf '%s' "$payload" | factory_lock_run_untrusted \
            ./scripts/git-commit-hook.sh --campaign-audit --final-handoff; then :; else return $?; fi
    if factory_lock_run_untrusted env FACTORY_FINAL_GATE_ATTEST=1 \
            FACTORY_CAMPAIGN_AUDIT_ROUND="$FACTORY_CAMPAIGN_AUDIT_ROUND" \
            FACTORY_CAMPAIGN_AUDIT_BASE="$FACTORY_CAMPAIGN_AUDIT_BASE" \
            FACTORY_CAMPAIGN_RUNNER_EVIDENCE_SHA256="$FACTORY_CAMPAIGN_RUNNER_EVIDENCE_SHA256" \
            ./scripts/final-gate.sh --campaign-audit; then :; else return $?; fi
    head=$(git rev-parse HEAD)
    ./scripts/ralph-final-state.py attest campaign-audit "$head" >/dev/null
    echo "ralph-audit: independent audit completed"
}

while true; do
    factory_lock_run_untrusted ./scripts/ollama-usage-guard.sh --wait
    CONTINUE=false
    if $RESUME && ralph_supervision_should_continue campaign-audit; then CONTINUE=true; fi
    ralph_supervision_begin campaign-audit
    command=("$RALPH_BIN" -c .factory/ralph/audit.yml run --exclusive)
    $CONTINUE && command+=(--continue)
    $TUI || command+=(--no-tui)
    set +e
    factory_lock_run_untrusted "${command[@]}"
    rc=$?
    ralph_supervision_finish_attempt campaign-audit
    boundary_rc=$?
    set -e
    if (( boundary_rc == 2 || (rc == 0 && boundary_rc != 0) )); then
        echo "ralph-audit: launch/event boundary validation failed" >&2
        exit 2
    fi

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
    factory_lock_run_untrusted ./scripts/ollama-usage-guard.sh --check
    quota_rc=$?
    set -e
    if (( quota_rc == 1 )); then
        factory_lock_run_untrusted ./scripts/ollama-usage-guard.sh --wait
        ./scripts/ralph-recover.sh --mode campaign-audit --prepare-only
        RESUME=true
        continue
    elif (( quota_rc != 0 )); then
        echo "ralph-audit: quota status check failed with status $quota_rc" >&2
        exit "$quota_rc"
    fi
    stale_diagnostics=$(ralph_supervision_diagnostics_file campaign-audit)
    set +e
    factory_lock_run_untrusted env \
        FACTORY_CAMPAIGN_AUDIT_ROUND="$FACTORY_CAMPAIGN_AUDIT_ROUND" \
        FACTORY_CAMPAIGN_AUDIT_BASE="$FACTORY_CAMPAIGN_AUDIT_BASE" \
        FACTORY_CAMPAIGN_RUNNER_EVIDENCE_SHA256="$FACTORY_CAMPAIGN_RUNNER_EVIDENCE_SHA256" \
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
