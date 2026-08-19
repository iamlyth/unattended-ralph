#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
PROJECT_ROOT=$(cd -- "$SCRIPT_DIR/.." && pwd)
RALPH_BIN=${RALPH_BIN:-ralph}
RESUME=false
TUI=true
ORIGINAL_ARGS=("$@")
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
# shellcheck source=scripts/factory-lock.sh
source "$SCRIPT_DIR/factory-lock.sh"
factory_lock_bootstrap "$PROJECT_ROOT" "$PROJECT_ROOT/scripts/ralph-maintenance-plan.sh" "${ORIGINAL_ARGS[@]}"
command -v "$RALPH_BIN" >/dev/null || { echo "ralph-maintenance-plan: Ralph executable not found: $RALPH_BIN" >&2; exit 2; }
command -v pi2 >/dev/null || { echo "ralph-maintenance-plan: pi2 is unavailable" >&2; exit 2; }
factory_lock_run_untrusted ./scripts/branch-guard.sh

# Select and validate the cycle only while holding the single-writer lock. This
# closes the race between clean-tree inspection and writing volatile selection.
# shellcheck source=scripts/ralph-supervision.sh
source "$SCRIPT_DIR/ralph-supervision.sh"
factory_lock_acquire "$PROJECT_ROOT"
factory_lock_run_untrusted ./scripts/branch-guard.sh
ralph_supervision_prepare_state_directory
STATE_FILE_HELPER="$SCRIPT_DIR/factory-state-file.py"
if [[ "$RESUME" == false ]]; then
    FACTORY_MAINTENANCE_BASE_COMMIT=$(git rev-parse HEAD)
    "$STATE_FILE_HELPER" write loop-mode maintenance-planning
    "$STATE_FILE_HELPER" write maintenance-base-commit "$FACTORY_MAINTENANCE_BASE_COMMIT"
else
    FACTORY_MAINTENANCE_BASE_COMMIT=$("$STATE_FILE_HELPER" read maintenance-base-commit) || {
        echo "ralph-maintenance-plan: missing or unsafe cycle base marker for resume" >&2; exit 1;
    }
    [[ $("$STATE_FILE_HELPER" read loop-mode) == maintenance-planning ]] || {
        echo "ralph-maintenance-plan: saved lifecycle is not maintenance planning" >&2; exit 1;
    }
    [[ $("$STATE_FILE_HELPER" read maintenance-bug-id) == "$BUG_ID" ]] || {
        echo "ralph-maintenance-plan: saved maintenance selection does not match $BUG_ID" >&2; exit 1;
    }
    [[ -s .factory/artifacts/maintenance-plan.md ]] || {
        echo "ralph-maintenance-plan: missing maintenance draft for resume" >&2; exit 1;
    }
fi
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
"$STATE_FILE_HELPER" write maintenance-bug-id "$BUG_ID"
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
ralph_supervision_initialize maintenance-planning "$RESUME"
CONTINUE=false
if $RESUME && ralph_supervision_should_continue maintenance-planning; then CONTINUE=true; fi

finish_maintenance_planning_cycle() {
    local payload head
    if ./scripts/ralph-final-state.py verify maintenance-planning >/dev/null 2>&1; then
        echo "ralph-maintenance-plan: plan committed and bug marked planned for $BUG_ID"
        return 0
    fi
    # The ledger transition precedes the single final checkpoint. It cannot
    # become a later metadata-only commit that authorizes another checkpoint.
    if ./scripts/finalize-maintenance-planning.sh; then :; else return $?; fi
    payload=$(printf '{"loop":{"workspace":"%s","id":"maintenance-planning-final"},"iteration":{"current":"final"}}' "$PROJECT_ROOT")
    if printf '%s' "$payload" | factory_lock_run_untrusted \
            ./scripts/git-commit-hook.sh --maintenance-plan --final-handoff; then :; else return $?; fi
    if factory_lock_run_untrusted env FACTORY_FINAL_GATE_ATTEST=1 ./scripts/final-gate.sh --maintenance-planning; then :; else return $?; fi
    head=$(git rev-parse HEAD)
    ./scripts/ralph-final-state.py attest maintenance-planning "$head" >/dev/null
    echo "ralph-maintenance-plan: plan committed and bug marked planned for $BUG_ID"
}

while true; do
    factory_lock_run_untrusted ./scripts/ollama-usage-guard.sh --wait
    CONTINUE=false
    if $RESUME && ralph_supervision_should_continue maintenance-planning; then CONTINUE=true; fi
    ralph_supervision_begin maintenance-planning
    command=("$RALPH_BIN" -c .factory/ralph/maintenance-plan.yml run --exclusive)
    $CONTINUE && command+=(--continue)
    $TUI || command+=(--no-tui)
    set +e
    factory_lock_run_untrusted "${command[@]}"
    rc=$?
    ralph_supervision_finish_attempt maintenance-planning
    boundary_rc=$?
    set -e
    if (( boundary_rc == 2 || (rc == 0 && boundary_rc != 0) )); then
        echo "ralph-maintenance-plan: launch/event boundary validation failed" >&2
        exit 2
    fi
    if (( rc == 0 )); then
        finish_maintenance_planning_cycle
        exit 0
    fi
    if (( rc >= 128 )); then
        echo "ralph-maintenance-plan: interrupted; recover with --mode maintenance-planning" >&2
        exit "$rc"
    fi
    set +e
    rejected_loop_id=$(ralph_supervision_consume_rejection maintenance-planning "$PROJECT_ROOT")
    rejection_rc=$?
    set -e
    if (( rejection_rc == 0 )); then
        ralph_supervision_allow_completion_recovery || { recovery_rc=$?; exit "$recovery_rc"; }
        echo "ralph-maintenance-plan: final gate rejected premature completion; continuing planning" >&2
        ./scripts/ralph-recover.sh --mode maintenance-planning --loop-id "$rejected_loop_id" --prepare-only
        RESUME=true
        continue
    elif (( rejection_rc != 1 )); then
        echo "ralph-maintenance-plan: invalid completion-rejection marker; refusing automatic recovery" >&2
        exit 1
    fi
    set +e
    factory_lock_run_untrusted ./scripts/ollama-usage-guard.sh --check
    quota_rc=$?
    set -e
    if (( quota_rc == 1 )); then
        factory_lock_run_untrusted ./scripts/ollama-usage-guard.sh --wait
        ./scripts/ralph-recover.sh --mode maintenance-planning --prepare-only
        RESUME=true
        continue
    elif (( quota_rc != 0 )); then
        echo "ralph-maintenance-plan: quota status check failed with status $quota_rc" >&2
        exit "$quota_rc"
    fi
    stale_diagnostics=$(ralph_supervision_diagnostics_file maintenance-planning)
    set +e
    factory_lock_run_untrusted ./scripts/final-gate.sh --maintenance-planning >"$stale_diagnostics" 2>&1
    gate_rc=$?
    set -e
    if (( $(wc -c < "$stale_diagnostics") > 65536 )); then
        rm -f -- "$stale_diagnostics"
        echo "ralph-maintenance-plan: final-gate diagnostics exceeded the recovery limit" >&2
        exit 1
    fi
    cat "$stale_diagnostics" >&2
    if (( gate_rc == 0 )); then
        rm -f -- "$stale_diagnostics"
        if finish_maintenance_planning_cycle; then :; else final_rc=$?; echo "ralph-maintenance-plan: finalization failed after a passing gate" >&2; exit "$final_rc"; fi
        echo "ralph-maintenance-plan: accepted valid planning artifacts after Ralph exited with status $rc" >&2
        exit 0
    elif (( gate_rc >= 128 )); then
        rm -f -- "$stale_diagnostics"
        exit "$gate_rc"
    elif (( gate_rc != 1 )); then
        rm -f -- "$stale_diagnostics"
        echo "ralph-maintenance-plan: final gate failed with infrastructure status $gate_rc" >&2
        exit "$gate_rc"
    fi
    set +e
    ralph_supervision_recover_stale maintenance-planning "$PROJECT_ROOT"
    stale_rc=$?
    set -e
    if (( stale_rc == 0 )); then
        rm -f -- "$stale_diagnostics"
        ./scripts/ralph-recover.sh --mode maintenance-planning --prepare-only
        RESUME=true
        continue
    fi
    rm -f -- "$stale_diagnostics"
    (( stale_rc == 1 )) || { echo "ralph-maintenance-plan: stale recovery was rejected" >&2; exit "$stale_rc"; }
    echo "ralph-maintenance-plan: Ralph failed with status $rc" >&2
    exit "$rc"
done
