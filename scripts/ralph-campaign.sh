#!/usr/bin/env bash
# Run a finite sequence of fresh planning, implementation, verification, and audit rounds.
set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
PROJECT_ROOT=$(cd -- "$SCRIPT_DIR/.." && pwd)
STATE_HELPER="$SCRIPT_DIR/ralph-campaign-state.py"
ROUNDS=""
RESUME=false
RESTART=false
TUI=false
TUI_EXPLICIT=false
TUI_OPTION=
ORIGINAL_ARGS=("$@")

usage() {
    cat <<'EOF'
Usage: scripts/ralph-campaign.sh --rounds N [--resume|--restart] [--tui|--no-tui]

A new campaign requires a clean develop branch and runs unattended by default.
--tui opts into an attended diagnostic display. --resume continues the exact
saved round and phase; --restart explicitly replaces only a terminal saved
campaign. Rounds and TUI mode must match when resuming.
EOF
}
while (( $# > 0 )); do
    case "$1" in
        --rounds) (( $# >= 2 )) || { echo "ralph-campaign: --rounds requires a value" >&2; exit 2; }; ROUNDS=$2; shift 2 ;;
        --resume) RESUME=true; shift ;;
        --restart) RESTART=true; shift ;;
        --tui)
            [[ -z "$TUI_OPTION" || "$TUI_OPTION" == tui ]] || {
                echo "ralph-campaign: --tui and --no-tui are mutually exclusive" >&2; exit 2;
            }
            TUI=true; TUI_EXPLICIT=true; TUI_OPTION=tui; shift
            ;;
        --no-tui)
            [[ -z "$TUI_OPTION" || "$TUI_OPTION" == no-tui ]] || {
                echo "ralph-campaign: --tui and --no-tui are mutually exclusive" >&2; exit 2;
            }
            TUI=false; TUI_EXPLICIT=true; TUI_OPTION=no-tui; shift
            ;;
        -h|--help) usage; exit 0 ;;
        *) echo "ralph-campaign: unknown option '$1'" >&2; exit 2 ;;
    esac
done
[[ "$ROUNDS" =~ ^[1-9][0-9]*$ ]] || { echo "ralph-campaign: --rounds must be a positive integer" >&2; exit 2; }
$RESUME && $RESTART && { echo "ralph-campaign: --resume and --restart are mutually exclusive" >&2; exit 2; }

cd -- "$PROJECT_ROOT"
# shellcheck source=scripts/factory-lock.sh
source "$SCRIPT_DIR/factory-lock.sh"
factory_lock_bootstrap "$PROJECT_ROOT" "$PROJECT_ROOT/scripts/ralph-campaign.sh" "${ORIGINAL_ARGS[@]}"

# Open and retain an immutable descriptor to the binding helper before any
# untrusted phase. Every later helper invocation executes this exact opened
# inode through /proc/self/fd/N, so a workspace pathname swap can never
# substitute helper code between binding and verification. The helper binds
# its own committed blob/mode through that retained descriptor.
exec {verifier_helper_fd}<"$SCRIPT_DIR/campaign-verifier-binding.py"
verification_binding=$(factory_lock_run_untrusted "/proc/self/fd/$verifier_helper_fd") || exit $?
verification_digest=$(python3 - "$verification_binding" <<'PY'
import json, sys
binding = json.loads(sys.argv[1])
if set(binding) != {'binding', 'sha256', 'helper'}:
    raise SystemExit('ralph-campaign: invalid verifier binding output')
if binding['binding'].get('schema') != 'campaign-verifier-binding/v1':
    raise SystemExit('ralph-campaign: invalid verifier binding schema')
helper = binding.get('helper')
if (not isinstance(helper, dict) or set(helper) != {'path', 'sha256', 'blob', 'mode'}
        or helper.get('path') != 'scripts/campaign-verifier-binding.py'
        or helper.get('mode') != '0755'):
    raise SystemExit('ralph-campaign: invalid retained helper binding')
print(binding['sha256'])
PY
) || exit $?

factory_lock_run_untrusted ./scripts/branch-guard.sh
factory_lock_run_untrusted ./scripts/check-factory-environment.py
factory_lock_acquire "$PROJECT_ROOT"

# Campaign orchestration never guesses whether Ralph's own exclusive lock is
# stale. Recovery validates and repairs that lock explicitly; ambiguous, live,
# and dead-but-unreconciled lock records all remain untouched here.
if [[ -e .ralph/loop.lock || -L .ralph/loop.lock ]]; then
    echo "ralph-campaign: Ralph loop lock exists; reconcile it with ralph-recover before campaign resume" >&2
    exit 1
fi

python3 - <<'PY'
import os, stat
from pathlib import Path
path = Path('.factory-state')
if path.exists() or path.is_symlink():
    info = path.lstat()
    if (not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode)
            or info.st_uid != os.getuid() or info.st_mode & 0o077):
        raise SystemExit('ralph-campaign: unsafe .factory-state directory')
else:
    path.mkdir(mode=0o700)
PY
STATE_FILE=$PROJECT_ROOT/.factory-state/ralph-campaign.json
export FACTORY_CAMPAIGN_STATE="$STATE_FILE"

if $RESUME; then
    [[ -f "$STATE_FILE" && ! -L "$STATE_FILE" ]] || { echo "ralph-campaign: no safe saved campaign to resume" >&2; exit 1; }
    saved_rounds=$($STATE_HELPER get rounds_requested)
    saved_tui=$($STATE_HELPER get tui)
    saved_digest=$($STATE_HELPER get verification_command_sha256)
    [[ "$saved_rounds" == "$ROUNDS" ]] || { echo "ralph-campaign: requested rounds do not match saved campaign ($saved_rounds)" >&2; exit 1; }
    if [[ "$saved_digest" != "$verification_digest" ]]; then
        migration_mode=$($STATE_HELPER get phase)
        [[ "$migration_mode" != audit ]] || migration_mode=campaign-audit
        if ! "$STATE_HELPER" promote-verifier-binding \
                --mode "$migration_mode" --expected-old "$saved_digest" \
                --new "$verification_digest"; then
            echo "ralph-campaign: verification executable/config binding changed during the campaign" >&2
            exit 1
        fi
    fi
    requested_tui=$($TUI && echo true || echo false)
    if $TUI_EXPLICIT && [[ "$saved_tui" != "$requested_tui" ]]; then
        echo "ralph-campaign: requested TUI mode does not match saved campaign" >&2; exit 1
    fi
    [[ "$saved_tui" == true ]] && TUI=true || TUI=false
    saved_status=$($STATE_HELPER get status)
    [[ "$saved_status" == active ]] || { echo "ralph-campaign: saved campaign is $saved_status, not resumable" >&2; exit 1; }
else
    [[ -z $(git status --porcelain --untracked-files=normal) ]] || { echo "ralph-campaign: start from a clean Git tree" >&2; exit 1; }
    initial_base=$(git rev-parse HEAD)
    start_args=(start --rounds "$ROUNDS" --tui "$($TUI && echo true || echo false)" \
        --verification-digest "$verification_digest" --base "$initial_base")
    $RESTART && start_args+=(--replace-terminal)
    "$STATE_HELPER" "${start_args[@]}"
fi

trap 'echo "ralph-campaign: interrupted; resume with ./scripts/ralph-campaign.sh --rounds '"$ROUNDS"' --resume" >&2' INT TERM

state_get() { "$STATE_HELPER" get "$1"; }
round_field() { state_get "rounds.$(($(state_get round) - 1)).$1"; }
json_string() { python3 -c 'import json,sys; print(json.dumps(sys.argv[1]))' "$1"; }
record_field() {
    local phase=$1 key=$2 value=$3
    "$STATE_HELPER" update --expect-phase "$phase" --round-field "$key=$value"
}
launcher_args=()
$TUI || launcher_args+=(--no-tui)

# Leaf launchers own quota waits and their persisted bounded recoveries. Any
# other nonzero result stops this active campaign at the same resumable phase.
run_phase() {
    local label=$1; shift
    local rc
    if "$@"; then
        return 0
    else
        rc=$?
    fi
    echo "ralph-campaign: $label exited with status $rc; campaign remains active at this phase" >&2
    return "$rc"
}

confirm_prepared_launch() {
    local phase=$1 rc
    if "$STATE_HELPER" confirm-launch --expect-phase "$phase" --missing-ok; then
        return 0
    else
        rc=$?
    fi
    (( rc == 3 )) && return 3
    echo "ralph-campaign: saved $phase launch handshake is invalid" >&2
    return 2
}

run_leaf_phase() {
    local phase=$1 label=$2 rc confirm_rc
    shift 2
    if "$@"; then rc=0; else rc=$?; fi
    if "$STATE_HELPER" confirm-launch --expect-phase "$phase" --missing-ok; then
        confirm_rc=0
    else
        confirm_rc=$?
    fi
    if (( confirm_rc != 0 && confirm_rc != 3 )); then
        echo "ralph-campaign: $label launch handshake validation failed" >&2
        return 2
    fi
    if (( rc == 0 && confirm_rc == 3 )); then
        echo "ralph-campaign: $label exited successfully without receiver-side launch evidence" >&2
        return 2
    fi
    if (( rc != 0 )); then
        echo "ralph-campaign: $label exited with status $rc; campaign remains active at this phase" >&2
        return "$rc"
    fi
}

while [[ $(state_get status) == active ]]; do
    round=$(state_get round)
    phase=$(state_get phase)
    export FACTORY_CAMPAIGN_ROUND=$round FACTORY_CAMPAIGN_PHASE=$phase
    echo "ralph-campaign: round $round/$ROUNDS phase $phase"
    case "$phase" in
        planning)
            base=$(round_field base_commit)
            started=$(round_field planning_started)
            if $RESUME && [[ "$started" != true ]]; then
                set +e
                confirm_prepared_launch planning
                prepared_rc=$?
                set -e
                (( prepared_rc == 0 || prepared_rc == 3 )) || exit "$prepared_rc"
                [[ $prepared_rc -eq 0 ]] && started=true
            fi
            if [[ -z "$base" ]]; then
                [[ -z $(git status --porcelain --untracked-files=normal) ]] || { echo "ralph-campaign: planning round requires a clean tree" >&2; exit 1; }
                base=$(git rev-parse HEAD)
                record_field planning base_commit "$(json_string "$base")"
            fi
            planning_ok=false
            if [[ "$started" == true ]]; then
                set +e
                factory_lock_run_untrusted env FACTORY_FINAL_GATE_ATTEST=1 \
                    FACTORY_PLANNING_BASE_COMMIT="$base" ./scripts/final-gate.sh --planning >/dev/null 2>&1
                gate_rc=$?
                set -e
                if (( gate_rc == 0 )); then
                    planning_ok=true
                elif (( gate_rc != 1 )); then
                    echo "ralph-campaign: planning gate failed with infrastructure status $gate_rc" >&2
                    exit "$gate_rc"
                fi
            fi
            if ! $planning_ok; then
                if [[ "$started" == true ]]; then
                    saved_planning_base=$("$SCRIPT_DIR/factory-state-file.py" \
                        read planning-base-commit --missing-ok) || {
                        echo "ralph-campaign: planning resume marker is unsafe" >&2
                        exit 1
                    }
                    [[ "$saved_planning_base" == "$base" ]] || {
                        echo "ralph-campaign: planning resume marker does not match the round base" >&2
                        exit 1
                    }
                    run_leaf_phase planning "planning" "$SCRIPT_DIR/ralph-plan.sh" --resume "${launcher_args[@]}"
                elif [[ $(git rev-parse HEAD) == "$base" ]] && [[ -z $(git status --porcelain --untracked-files=normal) ]]; then
                    run_leaf_phase planning "planning" "$SCRIPT_DIR/ralph-plan.sh" "${launcher_args[@]}"
                else
                    echo "ralph-campaign: planning state cannot be reconciled safely" >&2
                    exit 1
                fi
            fi
            plan_base=$(python3 - <<'PY'
import re
text=open('.factory/artifacts/implementation-plan.md', encoding='utf-8').read()
match=re.search(r'^base_commit: ([0-9a-f]{40})$', text, re.M)
print(match.group(1) if match else '')
PY
)
            [[ "$plan_base" == "$base" ]] || { echo "ralph-campaign: plan base does not match round base" >&2; exit 1; }
            factory_lock_run_untrusted env FACTORY_FINAL_GATE_ATTEST=1 \
                ./scripts/final-gate.sh --planning
            plan_commit=$(git rev-parse HEAD)
            "$STATE_HELPER" update --expect-phase planning --phase implementation --round-field "plan_commit=$(json_string "$plan_commit")"
            ;;
        implementation)
            started=$(round_field implementation_started)
            if $RESUME && [[ "$started" != true ]]; then
                set +e
                confirm_prepared_launch implementation
                prepared_rc=$?
                set -e
                (( prepared_rc == 0 || prepared_rc == 3 )) || exit "$prepared_rc"
                [[ $prepared_rc -eq 0 ]] && started=true
            fi
            implementation_ok=false
            if [[ "$started" == true ]]; then
                precheck_diagnostics=$(mktemp "$PROJECT_ROOT/.factory-state/implementation-precheck.XXXXXX")
                set +e
                factory_lock_run_untrusted env FACTORY_FINAL_GATE_ATTEST=1 \
                    ./scripts/final-gate.sh --implementation >"$precheck_diagnostics" 2>&1
                gate_rc=$?
                set -e
                rm -f -- "$precheck_diagnostics"
                if (( gate_rc == 0 )); then
                    implementation_ok=true
                elif (( gate_rc != 1 )); then
                    echo "ralph-campaign: implementation pre-check failed with infrastructure status $gate_rc" >&2
                    exit "$gate_rc"
                fi
            fi
            if ! $implementation_ok; then
                # Preserve legitimate uncommitted leaf work after interruption.
                # A clean checkpoint starts a fresh context; a dirty interrupted
                # lifecycle must use the launcher's validated recovery path.
                if [[ "$started" == true ]]; then
                    run_leaf_phase implementation "implementation" "$SCRIPT_DIR/ralph-run.sh" --resume "${launcher_args[@]}"
                else
                    run_leaf_phase implementation "implementation" "$SCRIPT_DIR/ralph-run.sh" "${launcher_args[@]}"
                fi
                run_phase "implementation-gate" factory_lock_run_untrusted env \
                    FACTORY_FINAL_GATE_ATTEST=1 ./scripts/final-gate.sh --implementation
            fi
            implementation_commit=$(git rev-parse HEAD)
            "$STATE_HELPER" update --expect-phase implementation --phase verification --round-field "implementation_commit=$(json_string "$implementation_commit")"
            ;;
        verification)
            saved_verification_digest=$(state_get verification_command_sha256)
            [[ "$saved_verification_digest" == "$verification_digest" ]] || {
                echo "ralph-campaign: saved verifier binding changed before verification" >&2
                exit 1
            }
            factory_lock_run_untrusted "/proc/self/fd/$verifier_helper_fd" \
                --expected-digest "$saved_verification_digest" --exec
            if [[ -x ./scripts/check-installed-functional-evidence.sh ]]; then
                factory_lock_run_untrusted ./scripts/check-installed-functional-evidence.sh
            fi
            factory_lock_run_untrusted ./scripts/run-factory-runners.py
            runner_evidence_sha256=$(factory_lock_run_untrusted \
                ./scripts/check-factory-runner-evidence.py --print-digest)
            # Capability evidence is only valid when every declared capability
            # has a tracked contract and a fresh exact-commit receipt with no
            # skipped or simulated probe output.
            factory_lock_run_untrusted ./scripts/check-capability-contracts.py
            factory_lock_run_untrusted ./scripts/check-capability-evidence.py
            [[ -z $(git status --porcelain --untracked-files=normal) ]] || { echo "ralph-campaign: verification left a dirty tree" >&2; exit 1; }
            verification_commit=$(git rev-parse HEAD)
            "$STATE_HELPER" update --expect-phase verification --phase audit \
                --round-field "verification_commit=$(json_string "$verification_commit")" \
                --round-field "runner_evidence_sha256=$(json_string "$runner_evidence_sha256")"
            ;;
        audit)
            started=$(round_field audit_started)
            if $RESUME && [[ "$started" != true ]]; then
                set +e
                confirm_prepared_launch audit
                prepared_rc=$?
                set -e
                (( prepared_rc == 0 || prepared_rc == 3 )) || exit "$prepared_rc"
                [[ $prepared_rc -eq 0 ]] && started=true
            fi
            expected_audit_base=$(round_field verification_commit)
            expected_runner_evidence=$(round_field runner_evidence_sha256)
            export FACTORY_CAMPAIGN_AUDIT_ROUND=$round
            export FACTORY_CAMPAIGN_AUDIT_BASE=$expected_audit_base
            export FACTORY_CAMPAIGN_RUNNER_EVIDENCE_SHA256=$expected_runner_evidence
            if [[ "$started" != true ]]; then
                [[ $(git rev-parse HEAD) == "$expected_audit_base" ]] || { echo "ralph-campaign: audit HEAD does not match verified implementation" >&2; exit 1; }
                ./scripts/initialize-campaign-audit.py --round "$round" --base "$expected_audit_base" \
                    --runner-evidence-sha256 "$expected_runner_evidence"
                run_leaf_phase audit "audit" "$SCRIPT_DIR/ralph-audit.sh" "${launcher_args[@]}"
            else
                set +e
                factory_lock_run_untrusted env FACTORY_FINAL_GATE_ATTEST=1 \
                    ./scripts/final-gate.sh --campaign-audit >/dev/null 2>&1
                audit_gate_rc=$?
                set -e
                if (( audit_gate_rc == 1 )); then
                    run_leaf_phase audit "audit" "$SCRIPT_DIR/ralph-audit.sh" --resume "${launcher_args[@]}"
                elif (( audit_gate_rc != 0 )); then
                    echo "ralph-campaign: audit gate failed with infrastructure status $audit_gate_rc" >&2
                    exit "$audit_gate_rc"
                fi
            fi
            factory_lock_run_untrusted env FACTORY_FINAL_GATE_ATTEST=1 \
                ./scripts/final-gate.sh --campaign-audit
            audit_result=$(python3 - <<'PY'
import re
text=open('.factory/artifacts/campaign-audit.md', encoding='utf-8').read()
match=re.search(r'^result: (pass|findings)$', text, re.M)
print(match.group(1) if match else '')
PY
)
            audit_commit=$(git rev-parse HEAD)
            "$STATE_HELPER" update --expect-phase audit \
                --round-field "audit_commit=$(json_string "$audit_commit")" \
                --round-field "audit_result=$(json_string "$audit_result")"
            if (( round < ROUNDS )); then
                "$STATE_HELPER" advance --expect-phase audit
            else
                "$STATE_HELPER" finish --result "$audit_result"
            fi
            ;;
        *) echo "ralph-campaign: impossible active phase '$phase'" >&2; exit 1 ;;
    esac
done

status=$(state_get status)
if [[ "$status" == complete ]]; then
    echo "ralph-campaign: all $ROUNDS rounds completed with a clean final audit"
    exit 0
fi
echo "ralph-campaign: final round found unresolved gaps; start another campaign after review" >&2
exit 1
