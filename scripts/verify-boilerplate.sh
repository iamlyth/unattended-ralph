#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
PROJECT_ROOT=$(cd -- "$SCRIPT_DIR/.." && pwd)
cd -- "$PROJECT_ROOT"

# The scenario suite exercises isolated temporary repositories and must not
# inherit ambient lifecycle state. The implementation completion gate runs
# this suite with FACTORY_FINAL_GATE_ATTEST=1; leaking the attestation flag,
# attempt/cycle bindings, campaign bindings, or recovery ceilings into nested
# final-gate invocations would make scenario tests attest dirty trees and fail
# spuriously (BUG-0013 regression).
unset FACTORY_FINAL_GATE_ATTEST FACTORY_RALPH_CYCLE_ID FACTORY_RALPH_ATTEMPT_ID \
      FACTORY_RALPH_HISTORY_ID FACTORY_RALPH_HISTORY_OFFSET \
      FACTORY_CAMPAIGN_PHASE FACTORY_CAMPAIGN_ROUND FACTORY_CAMPAIGN_AUDIT_ROUND \
      FACTORY_CAMPAIGN_AUDIT_BASE FACTORY_CAMPAIGN_RUNNER_EVIDENCE_SHA256 \
      FACTORY_CAMPAIGN_OBJECTIVE \
      FACTORY_PLANNING_BASE_COMMIT FACTORY_MAINTENANCE_BASE_COMMIT \
      FACTORY_RALPH_MAX_COMPLETION_RECOVERIES FACTORY_RALPH_MAX_NO_PROGRESS_RECOVERIES \
      FACTORY_RALPH_MAX_STALE_RECOVERIES

mapfile -t SHELL_FILES < <(find scripts tests -type f -name '*.sh' -print | sort)
for file in "${SHELL_FILES[@]}"; do
    bash -n "$file"
done
if command -v shellcheck >/dev/null; then
    shellcheck "${SHELL_FILES[@]}"
else
    echo "verify: warning: shellcheck unavailable" >&2
fi

python3 - <<'PY'
import json, pathlib, tomllib
with open('.factory/config.toml', 'rb') as stream:
    config = tomllib.load(stream)
assert config['concurrency']['mutating_workers'] == 1
assert config['concurrency']['integration_workers'] == 1
assert config['git']['allow_worktrees'] is False
assert isinstance(config.get('campaign', {}).get('required_capabilities'), list)
assert all(isinstance(item, str) and item for item in config['campaign']['required_capabilities'])
assert config['verification']['maintenance_command'] == ['./scripts/verify-project.sh']
assert isinstance(config['verification']['campaign_command'], list)
assert config['verification']['campaign_command']
assert all(isinstance(arg, str) and arg for arg in config['verification']['campaign_command'])
assert config['issues'] == {
    'schema': 'ralph-bug-ledger/v1',
    'open_ledger': '.factory/bugs/open.md',
    'closed_ledger': '.factory/bugs/closed.md',
    'maintenance_plan': '.factory/artifacts/maintenance-plan.md',
    'final_task_title': 'Maintenance verification and documentation audit',
    'providers': ['github', 'forgejo'],
    'external_sync': 'manual',
    'credentials': False,
}
required = [
    'AGENTS.md', '.factory/config.toml', '.factory/environment.toml',
    '.factory/artifacts/implementation-plan.md', '.factory/artifacts/maintenance-plan.md',
    '.factory/artifacts/campaign-audit.md', '.factory/bugs/open.md', '.factory/bugs/closed.md',
    '.factory/prompts/implementation.md', '.factory/prompts/plan.md',
    '.factory/ralph/maintenance.yml', '.factory/ralph/maintenance-plan.yml',
    '.factory/prompts/maintenance.md', '.factory/prompts/maintenance-plan.md',
    'scripts/bug-ledger.py', 'scripts/validate-maintenance-plan.py',
    'scripts/validate-implementation-plan.py', 'scripts/check-scratchpad.sh',
    'scripts/ralph-completion-gate.sh', 'scripts/ralph-supervision.sh',
    'scripts/factory-lock.sh', 'scripts/factory-lock-exec.py',
    'scripts/factory_lock.py', 'scripts/factory_state_io.py',
    'scripts/factory-state-file.py', 'scripts/ralph_lock.py',
    'scripts/ralph-lock-recover.py', 'scripts/ralph-event-boundary.py',
    'scripts/campaign-verifier-binding.py', 'scripts/ralph-supervision-migrate.py',
    'scripts/ralph-final-state.py', 'scripts/finalize-maintenance-planning.sh',
    'tests/test-git-checkpoint.sh',
    'scripts/check-installed-functional-evidence.sh',
    'scripts/initialize-plan-cycle.py', 'scripts/check-maintenance-freshness.sh',
    'scripts/maintenance-plan-scope-guard.sh',
    'scripts/ralph-maintenance-plan.sh', 'scripts/ralph-maintenance-run.sh',
    'docs/BUG_WORKFLOW.md', 'tests/test-bug-workflow.sh',
    'tests/test-plan-cycle.sh', 'tests/test-scratchpad-guard.sh',
    'tests/test-ralph-completion-recovery.sh',
    'tests/test-installed-functional-evidence.sh',
    '.factory/environment.toml', '.factory/artifacts/campaign-audit.md', '.factory/ralph/audit.yml',
    '.factory/prompts/audit.md', 'scripts/check-factory-environment.py',
    'scripts/ralph-campaign-state.py', 'scripts/initialize-campaign-audit.py',
    'scripts/validate-campaign-audit.py', 'scripts/campaign-audit-scope-guard.sh',
    'scripts/ralph-audit.sh', 'scripts/ralph-campaign.sh',
    'scripts/run-factory-runners.py', 'scripts/check-factory-runner-evidence.py',
    'scripts/factory-runner-server.py', 'scripts/pi2-secure-exec.py',
    'scripts/pi-cli-shims/ralph', 'scripts/pi-ralph-emit-extension.mjs',
    'tests/test-factory-environment.sh', 'tests/test-factory-runner.sh',
    'tests/test-campaign-audit.sh', 'tests/test-ralph-campaign.sh',
    'tests/test-ralph-campaign-state.py', 'tests/test-factory-lock.py',
    'tests/test-orchestration-security.py',
    'tests/test-ralph-stale-recovery.sh', 'tests/test-ralph-recover-safety.sh',
    'tests/test-maintenance-planning-completion.sh',
    'tests/test-boilerplate-env-isolation.sh',
    'tests/test-pi2-ollama-wrapper.sh', 'tests/test-production-path-bypass.sh',
    '.factory/schemas/conformance.schema.json', '.factory/capability-contracts.json',
    'scripts/validate-conformance.py', 'scripts/check-capability-contracts.py',
    'scripts/check-capability-evidence.py', 'scripts/machine-receipt.py',
    'scripts/check-audit-receipts.py',
    'scripts/validate-blocked-facts.py', 'scripts/check-context-summary.py',
    'scripts/ralph-context-summary.py', 'scripts/check-campaign-objectives.py',
    'scripts/check-golden-policy.py',
    '.factory/artifacts/blocked-facts.json', '.factory/artifacts/conformance.json',
    '.factory/artifacts/context-summary.md',
    '.factory/campaign-objectives.json', '.factory/golden-policy.json',
    '.factory/golden-review.json', '.factory/schemas/blocked-facts.schema.json',
    '.factory/schemas/golden-review.schema.json',
    'tests/test-conformance.sh', 'tests/test-capability-contracts.sh',
    'tests/test-audit-receipts.sh',
    'tests/test-blocked-facts.sh', 'tests/test-campaign-objectives.sh',
    'tests/test-context-summary.sh', 'tests/test-golden-policy.sh',
]
for name in required:
    assert pathlib.Path(name).is_file(), f'missing {name}'
forbidden_root_factory_files = {
    'PROMPT.md', 'IMPLEMENTATION_PLAN.md', 'MAINTENANCE_PLAN.md',
    'CAMPAIGN_AUDIT.md', 'factory.toml', 'factory-environment.toml',
    'open-bugs.md', 'closed-bugs.md', 'ralph.yml', 'ralph.plan.yml',
    'ralph.audit.yml', 'ralph.maintenance.yml', 'ralph.maintenance-plan.yml',
}
root_files = {path.name for path in pathlib.Path('.').iterdir() if path.is_file()}
assert not (root_files & forbidden_root_factory_files), 'factory files leaked back into repository root'
json.load(open('.pi/subagents.json', encoding='utf-8'))
for path in pathlib.Path('.pi/agents').glob('*.md'):
    text = path.read_text(encoding='utf-8')
    header = text.split('---', 2)[1]
    tools = next(line for line in header.splitlines() if line.startswith('tools:'))
    for forbidden in ('edit', 'write', 'bash'):
        assert forbidden not in tools, f'{path}: read-only agent exposes {forbidden}'
PY

for config in .factory/ralph/implementation.yml .factory/ralph/plan.yml .factory/ralph/audit.yml .factory/ralph/maintenance.yml .factory/ralph/maintenance-plan.yml; do
    grep -q 'parallel: false' "$config"
    grep -q -- '--allow-oversize' "$config"
    grep -q 'ralph-completion-gate.sh' "$config"
done
for launcher in scripts/ralph-run.sh scripts/ralph-plan.sh scripts/ralph-audit.sh scripts/ralph-maintenance-run.sh scripts/ralph-maintenance-plan.sh; do
    grep -q 'factory_lock_bootstrap' "$launcher"
    grep -q 'ralph-supervision.sh' "$launcher"
    grep -q 'ralph_supervision_consume_rejection' "$launcher"
    grep -q 'ralph_supervision_recover_stale' "$launcher"
done
grep -q '^## Build' AGENTS.md
grep -q '^## Immediate validation' AGENTS.md
(( $(wc -l < AGENTS.md) <= 100 )) || { echo 'verify: AGENTS.md must remain concise (100 lines maximum)' >&2; exit 1; }
grep -q 'Do not assume functionality is missing or complete' .factory/prompts/implementation.md
# False-positive-acceptance redesign: prompts must distinguish real acceptance
# from proxy evidence and require machine-readable conformance evidence.
grep -q 'Pixel/offscreen framebuffer checks are not real visual acceptance' .factory/prompts/implementation.md
grep -q 'not the real system service' .factory/prompts/implementation.md
grep -q 'synthetic producer' .factory/prompts/implementation.md
grep -q 'declaring or asserting evidence is not evidence' .factory/prompts/implementation.md
grep -q 'conformance.json' .factory/prompts/implementation.md
grep -q 'machine-receipt.py --tag' .factory/prompts/implementation.md
# Unavailable evidence must be fact-bound; fresh contexts receive only the
# durable context summary; golden baselines are protected by review manifests.
grep -q 'blocked-facts.json' .factory/prompts/implementation.md
grep -q 'context-summary' .factory/prompts/implementation.md
grep -q 'golden-policy' .factory/prompts/implementation.md
for role in visual-reviewer runner-reviewer evidence-reviewer spec-reviewer; do
    grep -q 'no runtime-certification authority' ".pi/agents/$role.md"
done
grep -q 'Final documentation and specification audit' .factory/prompts/plan.md
grep -q 'Specification conformance matrix' .factory/prompts/plan.md
grep -q 'conformance.json' .factory/prompts/plan.md
grep -q 'evidence tier' .factory/prompts/plan.md
grep -q 'Pixel/offscreen framebuffer checks are not real visual acceptance' .factory/prompts/plan.md
grep -q 'capability-contracts.json' .factory/prompts/plan.md
grep -q 'blocked-facts.json' .factory/prompts/plan.md
grep -q 'Interaction acceptance inventory' .factory/prompts/plan.md
grep -q 'definition of done' .factory/prompts/implementation.md
grep -q 'Maintenance verification and documentation audit' .factory/prompts/maintenance-plan.md
grep -q 'PLAN_COMPLETE.*final non-empty line outside every event tag' .factory/prompts/plan.md
grep -q 'LOOP_COMPLETE.*final non-empty line outside every event tag' .factory/prompts/implementation.md
grep -q 'MAINTENANCE_PLAN_COMPLETE.*final non-empty line outside every event tag' .factory/prompts/maintenance-plan.md
grep -q 'MAINTENANCE_COMPLETE.*final non-empty line outside every event tag' .factory/prompts/maintenance.md
grep -q 'AUDIT_COMPLETE.*final non-empty line' .factory/prompts/audit.md
grep -q 'machine-receipt.py --tag' .factory/prompts/audit.md
grep -q '\[receipt:' .factory/prompts/audit.md
grep -q 'BLOCKED evidence forces' .factory/prompts/audit.md
grep -q 'check-campaign-objectives.py' .factory/prompts/audit.md
grep -q 'blocked-facts.json' .factory/prompts/audit.md
grep -q 'Pixel/offscreen framebuffer checks are not real visual acceptance' .factory/prompts/audit.md
grep -q 'conformance.json' .factory/prompts/audit.md
grep -q 'final-gate.sh --planning' .factory/prompts/plan.md
grep -q 'final-gate.sh --implementation' .factory/prompts/implementation.md
grep -q 'final-gate.sh --campaign-audit' .factory/prompts/audit.md
grep -q 'final-gate.sh --maintenance-planning' .factory/prompts/maintenance-plan.md
grep -q 'final-gate.sh --maintenance' .factory/prompts/maintenance.md
for prompt in .factory/prompts/plan.md .factory/prompts/implementation.md \
        .factory/prompts/audit.md .factory/prompts/maintenance-plan.md \
        .factory/prompts/maintenance.md; do
    grep -q 'emit the completion token' "$prompt"
done
grep -q '^TUI=false$' scripts/ralph-campaign.sh
grep -q -- '--tui)' scripts/ralph-campaign.sh
grep -q 'factory_lock_bootstrap' scripts/ralph-campaign.sh
grep -q 'factory_lock_bootstrap' scripts/ralph-recover.sh
for config in .factory/ralph/plan.yml .factory/ralph/implementation.yml \
        .factory/ralph/audit.yml .factory/ralph/maintenance.yml; do
    checkpoint=$(grep 'command: \["./scripts/check-scratchpad.sh"' "$config")
    [[ $checkpoint == *'_COMPLETE"'* ]] || {
        echo "verify: iteration scratchpad hook must reject its lifecycle token: $config" >&2
        exit 1
    }
    grep -q -- '--final-handoff' "$config"
done
# Maintenance planning defers the strict final handoff to its trusted parent
# launcher, which performs the ledger transition and final checkpoint under
# the retained factory lock; its completion hook validates only.
checkpoint=$(grep 'command: \["./scripts/check-scratchpad.sh"' .factory/ralph/maintenance-plan.yml)
[[ $checkpoint == *'MAINTENANCE_PLAN_COMPLETE"'* ]] || {
    echo "verify: maintenance-planning scratchpad hook must reject its lifecycle token" >&2
    exit 1
}
grep -q -- '--final-handoff' scripts/ralph-maintenance-plan.sh
grep -q 'finalize-maintenance-planning.sh' scripts/ralph-maintenance-plan.sh
grep -q '.factory/environment.toml' .factory/prompts/plan.md
grep -q '.factory/environment.toml' .factory/prompts/implementation.md
grep -q 'check-factory-runner-evidence.py' .factory/prompts/plan.md
grep -q 'check-factory-runner-evidence.py' .factory/prompts/implementation.md
grep -q 'check-factory-runner-evidence.py' .factory/prompts/audit.md
./scripts/check-factory-environment.py
./scripts/check-capability-contracts.py
./scripts/validate-blocked-facts.py planning .factory/artifacts/blocked-facts.json
./scripts/check-golden-policy.py
./scripts/check-context-summary.py
cmp -s .github/ISSUE_TEMPLATE/bug_report.md .forgejo/ISSUE_TEMPLATE/bug_report.md
./scripts/bug-ledger.py validate

if git ls-files | grep -E '(^|/)(\.ollama-usage-env|\.env)$' >/dev/null; then
    echo "verify: secret environment file is tracked" >&2
    exit 1
fi
python3 - <<'PY'
import subprocess, urllib.parse
for name in subprocess.check_output(['git', 'remote'], text=True).split():
    url = subprocess.check_output(['git', 'remote', 'get-url', name], text=True).strip()
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme in {'http', 'https'} and (parsed.username or parsed.password):
        raise SystemExit(f'verify: remote {name} embeds credentials; use SSH or a credential helper')
PY

./tests/test-scratchpad-guard.sh
./tests/test-git-checkpoint.sh
./tests/test-ralph-completion-recovery.sh
./tests/test-installed-functional-evidence.sh "$PROJECT_ROOT"
./tests/test-factory-environment.sh
./tests/test-factory-runner.sh
./tests/test-campaign-audit.sh
./tests/test-ralph-campaign.sh
./tests/test-ralph-campaign-state.py
./tests/test-factory-lock.py
./tests/test-orchestration-security.py
./tests/test-maintenance-planning-completion.sh
./tests/test-boilerplate-env-isolation.sh
./tests/test-ralph-stale-recovery.sh
./tests/test-ralph-recover-safety.sh
./tests/test-pi2-ollama-wrapper.sh
./tests/test-production-path-bypass.sh
./tests/test-conformance.sh
./tests/test-capability-contracts.sh
./tests/test-audit-receipts.sh
./tests/test-blocked-facts.sh
./tests/test-campaign-objectives.sh
./tests/test-context-summary.sh
./tests/test-golden-policy.sh
./tests/test-boilerplate.sh
echo "verify: boilerplate checks passed"
