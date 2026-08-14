#!/usr/bin/env bash
# Enforce planning or implementation completion before Ralph may terminate.
set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
PROJECT_ROOT=$(cd -- "$SCRIPT_DIR/.." && pwd)
MODE=${1:-}
cd -- "$PROJECT_ROOT"

case "$MODE" in
    --planning)
        ./scripts/plan-scope-guard.sh
        ./scripts/check-plan-freshness.sh --planning
        ./scripts/check-scratchpad.sh PLAN_COMPLETE
        ./scripts/validate-implementation-plan.py planning .factory/artifacts/implementation-plan.md
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
        ./scripts/check-plan-freshness.sh
        ./scripts/validate-implementation-plan.py complete .factory/artifacts/implementation-plan.md
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
        ./scripts/verify-boilerplate.sh
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
        "${MAINTENANCE_COMMAND[@]}"
        echo "final-gate: maintenance implementation and audit accepted"
        ;;
    *)
        echo "Usage: scripts/final-gate.sh --planning|--implementation|--campaign-audit|--maintenance-planning|--maintenance" >&2
        exit 2
        ;;
esac
