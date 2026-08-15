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
./scripts/branch-guard.sh
./scripts/check-factory-environment.py
# shellcheck source=scripts/factory-lock.sh
source "$SCRIPT_DIR/factory-lock.sh"
factory_lock_acquire "$PROJECT_ROOT/.factory-lock"

# A campaign owns leaf-lifecycle recovery. Refuse a live Ralph owner, but remove
# an abandoned exclusive lock before starting or reconciling the saved phase.
if [[ -e .ralph/loop.lock ]]; then
    [[ ! -L .ralph/loop.lock && -f .ralph/loop.lock ]] || { echo "ralph-campaign: unsafe Ralph loop lock" >&2; exit 1; }
    lock_pid=$(python3 - <<'PY'
import json
try:
    value=json.load(open('.ralph/loop.lock', encoding='utf-8')).get('pid')
    print(value if isinstance(value, int) and value > 0 else '')
except Exception:
    print('')
PY
)
    if [[ -n "$lock_pid" && -d "/proc/$lock_pid" ]]; then
        lock_command=$(tr '\0' ' ' < "/proc/$lock_pid/cmdline" 2>/dev/null || true)
        [[ "$lock_command" != *ralph* ]] || { echo "ralph-campaign: live Ralph process $lock_pid owns the loop lock" >&2; exit 1; }
    fi
    echo "ralph-campaign: removing abandoned Ralph loop lock${lock_pid:+ for PID $lock_pid}" >&2
    rm -f -- .ralph/loop.lock
fi

[[ ! -L .factory-state && ( ! -e .factory-state || -d .factory-state ) ]] || {
    echo "ralph-campaign: unsafe .factory-state path" >&2; exit 1;
}
mkdir -p .factory-state
chmod 700 .factory-state
STATE_FILE=$PROJECT_ROOT/.factory-state/ralph-campaign.json
export FACTORY_CAMPAIGN_STATE="$STATE_FILE"
verify_argv_file=$(mktemp "$PROJECT_ROOT/.factory-state/campaign-verify.XXXXXX")
trap 'rm -f -- "$verify_argv_file"' EXIT
if ! python3 - > "$verify_argv_file" <<'PY'
import os, tomllib
with open('.factory/config.toml', 'rb') as stream:
    command = tomllib.load(stream).get('verification', {}).get('campaign_command')
if not isinstance(command, list) or not command or not all(isinstance(arg, str) and arg and '\0' not in arg for arg in command):
    raise SystemExit('ralph-campaign: verification.campaign_command must be a non-empty argv array')
if command[0] in {'bash', 'sh', 'zsh', 'env'} or command[:2] in (["python", "-c"], ["python3", "-c"]):
    raise SystemExit('ralph-campaign: campaign verifier must not be an inline interpreter command')
for arg in command:
    os.write(1, arg.encode() + b'\0')
PY
then
    rm -f -- "$verify_argv_file"
    exit 1
fi
mapfile -d '' -t CAMPAIGN_VERIFY_COMMAND < "$verify_argv_file"
rm -f -- "$verify_argv_file"
(( ${#CAMPAIGN_VERIFY_COMMAND[@]} > 0 )) || { echo "ralph-campaign: empty verification command" >&2; exit 1; }
if [[ ${CAMPAIGN_VERIFY_COMMAND[0]} == */* ]]; then
    [[ -x ${CAMPAIGN_VERIFY_COMMAND[0]} ]] || { echo "ralph-campaign: verifier is not executable: ${CAMPAIGN_VERIFY_COMMAND[0]}" >&2; exit 1; }
else
    command -v "${CAMPAIGN_VERIFY_COMMAND[0]}" >/dev/null || { echo "ralph-campaign: verifier is unavailable: ${CAMPAIGN_VERIFY_COMMAND[0]}" >&2; exit 1; }
fi
verification_digest=$(python3 - "${CAMPAIGN_VERIFY_COMMAND[@]}" <<'PY'
import hashlib, json, sys
print(hashlib.sha256(json.dumps(sys.argv[1:], separators=(',', ':')).encode()).hexdigest())
PY
)

if $RESUME; then
    [[ -f "$STATE_FILE" && ! -L "$STATE_FILE" ]] || { echo "ralph-campaign: no safe saved campaign to resume" >&2; exit 1; }
    saved_rounds=$($STATE_HELPER get rounds_requested)
    saved_tui=$($STATE_HELPER get tui)
    saved_digest=$($STATE_HELPER get verification_command_sha256)
    [[ "$saved_rounds" == "$ROUNDS" ]] || { echo "ralph-campaign: requested rounds do not match saved campaign ($saved_rounds)" >&2; exit 1; }
    [[ "$saved_digest" == "$verification_digest" ]] || { echo "ralph-campaign: verification command changed during the campaign" >&2; exit 1; }
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

# Run a phase script (ralph-plan/run/audit) with campaign-level resilience.
# If the script exits non-zero (e.g. infrastructure failure), clean up the
# factory lock and signal the caller to retry via the while loop.
run_phase() {
    local label=$1; shift
    set +e
    "$@"
    local rc=$?
    set -e
    if (( rc != 0 )); then
        rm -f "$PROJECT_ROOT/.factory-lock"
        echo "ralph-campaign: $label exited with status $rc; retrying in ${CAMPAIGN_RETRY_DELAY:-30}s" >&2
        sleep "${CAMPAIGN_RETRY_DELAY:-30}"
        return 1
    fi
    return 0
}

while [[ $(state_get status) == active ]]; do
    round=$(state_get round)
    phase=$(state_get phase)
    echo "ralph-campaign: round $round/$ROUNDS phase $phase"
    case "$phase" in
        planning)
            base=$(round_field base_commit)
            started=$(round_field planning_started)
            if [[ -z "$base" ]]; then
                [[ -z $(git status --porcelain --untracked-files=normal) ]] || { echo "ralph-campaign: planning round requires a clean tree" >&2; exit 1; }
                base=$(git rev-parse HEAD)
                record_field planning base_commit "$(json_string "$base")"
            fi
            planning_ok=false
            if [[ "$started" == true ]]; then
                set +e
                FACTORY_PLANNING_BASE_COMMIT=$base ./scripts/final-gate.sh --planning >/dev/null 2>&1
                gate_rc=$?
                set -e
                (( gate_rc == 0 )) && [[ -z $(git status --porcelain --untracked-files=normal) ]] && planning_ok=true
            fi
            if ! $planning_ok; then
                if [[ "$started" != true ]]; then
                    record_field planning planning_started true
                fi
                if [[ -s .factory-state/planning-base-commit ]] && [[ $(tr -d '[:space:]' < .factory-state/planning-base-commit) == "$base" ]]; then
                    run_phase "planning" "$SCRIPT_DIR/ralph-plan.sh" --resume "${launcher_args[@]}" || continue
                elif [[ $(git rev-parse HEAD) == "$base" ]] && [[ -z $(git status --porcelain --untracked-files=normal) ]]; then
                    run_phase "planning" "$SCRIPT_DIR/ralph-plan.sh" "${launcher_args[@]}" || continue
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
            ./scripts/final-gate.sh --planning
            plan_commit=$(git rev-parse HEAD)
            "$STATE_HELPER" update --expect-phase planning --phase implementation --round-field "plan_commit=$(json_string "$plan_commit")"
            ;;
        implementation)
            started=$(round_field implementation_started)
            implementation_ok=false
            if [[ "$started" == true ]]; then
                set +e
                ./scripts/final-gate.sh --implementation >/dev/null 2>&1
                gate_rc=$?
                set -e
                (( gate_rc == 0 )) && [[ -z $(git status --porcelain --untracked-files=normal) ]] && implementation_ok=true
            fi
            if ! $implementation_ok; then
                if [[ "$started" != true ]]; then
                    record_field implementation implementation_started true
                fi
                # Preserve legitimate uncommitted leaf work after interruption.
                # A clean checkpoint starts a fresh context; a dirty interrupted
                # lifecycle must use the launcher's validated recovery path.
                if [[ "$started" == true && -n $(git status --porcelain --untracked-files=normal) ]]; then
                    run_phase "implementation" "$SCRIPT_DIR/ralph-run.sh" --resume "${launcher_args[@]}" || continue
                else
                    run_phase "implementation" "$SCRIPT_DIR/ralph-run.sh" "${launcher_args[@]}" || continue
                fi
            fi
            ./scripts/final-gate.sh --implementation
            implementation_commit=$(git rev-parse HEAD)
            "$STATE_HELPER" update --expect-phase implementation --phase verification --round-field "implementation_commit=$(json_string "$implementation_commit")"
            ;;
        verification)
            "${CAMPAIGN_VERIFY_COMMAND[@]}"
            if [[ -x ./scripts/check-installed-functional-evidence.sh ]]; then
                ./scripts/check-installed-functional-evidence.sh
            fi
            ./scripts/run-factory-runners.py
            runner_evidence_sha256=$(./scripts/check-factory-runner-evidence.py --print-digest)
            [[ -z $(git status --porcelain --untracked-files=normal) ]] || { echo "ralph-campaign: verification left a dirty tree" >&2; exit 1; }
            verification_commit=$(git rev-parse HEAD)
            "$STATE_HELPER" update --expect-phase verification --phase audit \
                --round-field "verification_commit=$(json_string "$verification_commit")" \
                --round-field "runner_evidence_sha256=$(json_string "$runner_evidence_sha256")"
            ;;
        audit)
            started=$(round_field audit_started)
            expected_audit_base=$(round_field verification_commit)
            expected_runner_evidence=$(round_field runner_evidence_sha256)
            export FACTORY_CAMPAIGN_AUDIT_ROUND=$round
            export FACTORY_CAMPAIGN_AUDIT_BASE=$expected_audit_base
            export FACTORY_CAMPAIGN_RUNNER_EVIDENCE_SHA256=$expected_runner_evidence
            if [[ "$started" != true ]]; then
                [[ $(git rev-parse HEAD) == "$expected_audit_base" ]] || { echo "ralph-campaign: audit HEAD does not match verified implementation" >&2; exit 1; }
                ./scripts/initialize-campaign-audit.py --round "$round" --base "$expected_audit_base" \
                    --runner-evidence-sha256 "$expected_runner_evidence"
                record_field audit audit_started true
                run_phase "audit" "$SCRIPT_DIR/ralph-audit.sh" "${launcher_args[@]}" || continue
            else
                set +e
                ./scripts/final-gate.sh --campaign-audit >/dev/null 2>&1
                audit_gate_rc=$?
                set -e
                if (( audit_gate_rc != 0 )); then
                    # The report and Git binding persist; use a fresh audit context
                    # instead of continuing an unrelated volatile event stream.
                    run_phase "audit" "$SCRIPT_DIR/ralph-audit.sh" "${launcher_args[@]}" || continue
                fi
            fi
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
