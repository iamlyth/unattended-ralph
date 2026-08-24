#!/usr/bin/env bash
# Enforce planning or implementation completion before Ralph may terminate.
set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
PROJECT_ROOT=$(cd -- "$SCRIPT_DIR/.." && pwd)
MODE=${1:-}
cd -- "$PROJECT_ROOT"
ATTEST=false
ATTEST_HEAD=
if [[ ${FACTORY_FINAL_GATE_ATTEST:-0} == 1 ]]; then
    ATTEST=true
    ATTEST_HEAD=$(git rev-parse HEAD)
    [[ -z $(git status --porcelain --untracked-files=normal) ]] || {
        echo "final-gate: completion attestation requires a clean Git tree" >&2
        exit 1
    }
fi

case "$MODE" in
    --planning)
        ./scripts/plan-scope-guard.sh
        ./scripts/check-plan-freshness.sh --planning
        ./scripts/check-scratchpad.sh PLAN_COMPLETE
        ./scripts/validate-implementation-plan.py planning .factory/artifacts/implementation-plan.md
        if [[ -f .factory/artifacts/blocked-facts.json ]]; then
            ./scripts/validate-blocked-facts.py planning .factory/artifacts/blocked-facts.json
        fi
        if [[ -f .factory/artifacts/conformance.json ]]; then
            ./scripts/validate-conformance.py planning .factory/artifacts/conformance.json
        fi
        echo "final-gate: planning completion accepted"
        ;;
    --maintenance-planning)
        ./scripts/maintenance-plan-scope-guard.sh
        ./scripts/validate-maintenance-plan.py planning .factory/artifacts/maintenance-plan.md >/dev/null
        ./scripts/check-maintenance-freshness.sh --planning
        ./scripts/check-scratchpad.sh MAINTENANCE_PLAN_COMPLETE
        echo "final-gate: maintenance planning completion accepted"
        ;;
    --implementation)
        ./scripts/check-plan-freshness.sh
        ./scripts/check-scratchpad.sh LOOP_COMPLETE
        ./scripts/validate-implementation-plan.py complete .factory/artifacts/implementation-plan.md
        if [[ -x scripts/bug-ledger.py && -f .factory/bugs/open.md ]]; then
            ./scripts/bug-ledger.py validate
            python3 - <<'PY'
import json
import re
from pathlib import Path

text = Path('.factory/bugs/open.md').read_text(encoding='utf-8')
match = re.search(r'```json\s*\n(.*?)\n```', text, re.S)
if not match:
    raise SystemExit('final-gate: .factory/bugs/open.md has no JSON ledger')
records = json.loads(match.group(1))
if records:
    ids = ', '.join(str(record.get('id', '<unknown>')) for record in records)
    raise SystemExit(f'final-gate: autonomous definition of done rejects unresolved open bugs: {ids}')
PY
        fi
        # The machine-readable conformance sidecar is the only authority for
        # verified claims: free-text matrix cells cannot prove acceptance.
        # Blocked/unevidenced requirements fail implementation completion,
        # and every blocked-facts entry must be resolved by an exact
        # receipt/artifact or an explicit human decision.
        ./scripts/validate-conformance.py complete .factory/artifacts/conformance.json
        ./scripts/validate-blocked-facts.py complete .factory/artifacts/blocked-facts.json
        ./scripts/check-golden-policy.py
        ./scripts/check-capability-contracts.py
        ./scripts/check-capability-evidence.py
        # Mechanical visual-audit methodology gate: check-only, never invokes
        # capture/review-sdk/probe/model.
        ./scripts/visual-audit-gate.sh
        ./scripts/check-docs-sync.sh
        ./scripts/verify-boilerplate.sh
        if [[ -x scripts/verify-project.sh ]]; then
            ./scripts/verify-project.sh
        fi
        ./scripts/check-installed-functional-evidence.sh
        echo "final-gate: implementation, specification, tests, and documentation accepted"
        ;;
    --campaign-audit)
        ./scripts/campaign-audit-scope-guard.sh
        ./scripts/check-factory-environment.py
        ./scripts/check-capability-contracts.py
        ./scripts/check-capability-evidence.py
        ./scripts/check-plan-freshness.sh
        ./scripts/validate-implementation-plan.py complete .factory/artifacts/implementation-plan.md
        if [[ -f .factory/artifacts/conformance.json ]]; then
            ./scripts/validate-conformance.py complete .factory/artifacts/conformance.json
        fi
        if [[ -f .factory/artifacts/blocked-facts.json ]]; then
            ./scripts/validate-blocked-facts.py complete .factory/artifacts/blocked-facts.json
        fi
        ./scripts/check-golden-policy.py
        # Coordinator-executed commands are only runtime evidence when a machine
        # receipt matches; BLOCKED evidence forces result: findings.
        ./scripts/check-audit-receipts.py
        # Each audit round carries one falsification objective; the round
        # cannot pass without objective-specific receipt categories.
        ./scripts/check-campaign-objectives.py
        [[ ${FACTORY_CAMPAIGN_AUDIT_ROUND:-} =~ ^[1-9][0-9]*$ \
            && ${FACTORY_CAMPAIGN_AUDIT_BASE:-} =~ ^[0-9a-f]{40}$ \
            && ${FACTORY_CAMPAIGN_RUNNER_EVIDENCE_SHA256:-} =~ ^[0-9a-f]{64}$ ]] || {
            echo "final-gate: missing campaign-owned audit binding" >&2; exit 1;
        }
        ./scripts/check-scratchpad.sh AUDIT_COMPLETE
        ./scripts/validate-campaign-audit.py complete .factory/artifacts/campaign-audit.md \
            --expected-round "$FACTORY_CAMPAIGN_AUDIT_ROUND" \
            --expected-base "$FACTORY_CAMPAIGN_AUDIT_BASE" \
            --expected-runner-evidence-sha256 "$FACTORY_CAMPAIGN_RUNNER_EVIDENCE_SHA256"
        echo "final-gate: independent campaign audit accepted"
        ;;
    --maintenance)
        ./scripts/validate-maintenance-plan.py complete .factory/artifacts/maintenance-plan.md >/dev/null
        ./scripts/check-maintenance-freshness.sh
        ./scripts/check-scratchpad.sh MAINTENANCE_COMPLETE
        ./scripts/bug-ledger.py validate
        python3 - <<'PY'
import json, re, subprocess
meta = json.loads(subprocess.check_output(
    ['./scripts/validate-maintenance-plan.py', 'metadata', '.factory/artifacts/maintenance-plan.md'], text=True,
))
bug_id, base = meta['bug_id'], meta['base_commit']
shown = subprocess.check_output(['./scripts/bug-ledger.py', 'show', bug_id], text=True)
record = json.loads(shown.split('\nfingerprint:', 1)[0])
if record['status'] != 'closed' or not record['resolution'].strip() or not record['verification'].strip():
    raise SystemExit('final-gate: selected bug is not closed with resolution and verification')
def records(text):
    return {r['id']: r for r in json.loads(re.search(r'```json\s*\n(.*?)\n```', text, re.S).group(1))}
before = {}
for ledger in ('.factory/bugs/open.md', '.factory/bugs/closed.md'):
    before.update(records(subprocess.check_output(['git', 'show', f'{base}:{ledger}'], text=True)))
after = {}
for ledger in ('.factory/bugs/open.md', '.factory/bugs/closed.md'):
    after.update(records(open(ledger, encoding='utf-8').read()))
if set(before) != set(after) or bug_id not in before:
    raise SystemExit('final-gate: maintenance must not add or remove unrelated bug IDs')
changed = {key for key in before if before[key] != after[key]}
if changed != {bug_id}:
    raise SystemExit(f'final-gate: only selected bug may change in a maintenance cycle (changed: {sorted(changed)})')
PY
        mapfile -d '' -t MAINTENANCE_COMMAND < <(python3 - <<'PY'
import os, tomllib
with open('.factory/config.toml', 'rb') as stream:
    command = tomllib.load(stream).get('verification', {}).get('maintenance_command')
if not isinstance(command, list) or not command or not all(isinstance(arg, str) and arg for arg in command):
    raise SystemExit('final-gate: verification.maintenance_command must be a non-empty argv array')
for arg in command:
    os.write(1, arg.encode() + b'\0')
PY
        )
        (( ${#MAINTENANCE_COMMAND[@]} > 0 )) || { echo "final-gate: maintenance verifier is not configured" >&2; exit 1; }
        if [[ "${MAINTENANCE_COMMAND[0]}" == */* ]]; then
            [[ -x "${MAINTENANCE_COMMAND[0]}" ]] || {
                echo "final-gate: configured maintenance verifier is missing or not executable: ${MAINTENANCE_COMMAND[0]}" >&2
                exit 1
            }
        else
            command -v "${MAINTENANCE_COMMAND[0]}" >/dev/null || {
                echo "final-gate: configured maintenance verifier is missing or not executable: ${MAINTENANCE_COMMAND[0]}" >&2
                exit 1
            }
        fi
        # Task 16 (EVID-01 §19): the maintenance verifier is routed through
        # the retained descriptor authority exactly like the campaign
        # verifier.  The binding helper is opened before the verifier step
        # and the configured maintenance_command is bound to its committed
        # blob/identity; the helper then re-validates and executes the exact
        # bound inode through the retained descriptor, so a workspace
        # pathname or byte substitution can never substitute the maintenance
        # verifier that runs.
        exec {verifier_helper_fd}<"$SCRIPT_DIR/campaign-verifier-binding.py" || {
            echo "final-gate: the retained verifier binding helper is unavailable" >&2
            exit 1
        }
        maintenance_binding=$("/proc/self/fd/$verifier_helper_fd" --mode maintenance) || exit $?
        maintenance_digest=$(python3 - "$maintenance_binding" <<'PY'
import json, sys
binding = json.loads(sys.argv[1])
if set(binding) != {'binding', 'sha256', 'helper'}:
    raise SystemExit('final-gate: invalid verifier binding output')
if binding['binding'].get('schema') != 'campaign-verifier-binding/v1':
    raise SystemExit('final-gate: invalid verifier binding schema')
print(binding['sha256'])
PY
        ) || exit $?
        "/proc/self/fd/$verifier_helper_fd" \
            --mode maintenance --expected-digest "$maintenance_digest" --exec
        echo "final-gate: maintenance implementation and audit accepted"
        ;;
    *)
        echo "Usage: scripts/final-gate.sh --planning|--implementation|--campaign-audit|--maintenance-planning|--maintenance" >&2
        exit 2
        ;;
esac

if [[ "$ATTEST" == true ]]; then
    [[ $(git rev-parse HEAD) == "$ATTEST_HEAD" ]] || {
        echo "final-gate: HEAD changed while completion was being attested" >&2
        exit 1
    }
    [[ -z $(git status --porcelain --untracked-files=normal) ]] || {
        echo "final-gate: completion gate changed the tracked Git tree" >&2
        exit 1
    }
    printf 'final-gate: attested clean unchanged HEAD %s\n' "$ATTEST_HEAD"
fi
