#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
PROJECT_ROOT=$(cd -- "$SCRIPT_DIR/.." && pwd)
cd -- "$PROJECT_ROOT"

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
    'tests/test-ralph-stale-recovery.sh', 'tests/test-ralph-recover-safety.sh',
    'tests/test-pi2-ollama-wrapper.sh',
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
    grep -q 'ralph-supervision.sh' "$launcher"
    grep -q 'ralph_supervision_consume_rejection' "$launcher"
    grep -q 'ralph_supervision_recover_stale' "$launcher"
done
grep -q '^## Build' AGENTS.md
grep -q '^## Immediate validation' AGENTS.md
(( $(wc -l < AGENTS.md) <= 100 )) || { echo 'verify: AGENTS.md must remain concise (100 lines maximum)' >&2; exit 1; }
grep -q 'Do not assume functionality is missing or complete' .factory/prompts/implementation.md
grep -q 'Final documentation and specification audit' .factory/prompts/plan.md
grep -q 'Specification conformance matrix' .factory/prompts/plan.md
grep -q 'Interaction acceptance inventory' .factory/prompts/plan.md
grep -q 'definition of done' .factory/prompts/implementation.md
grep -q 'Maintenance verification and documentation audit' .factory/prompts/maintenance-plan.md
grep -q 'PLAN_COMPLETE.*final non-empty line outside every event tag' .factory/prompts/plan.md
grep -q 'LOOP_COMPLETE.*final non-empty line outside every event tag' .factory/prompts/implementation.md
grep -q 'MAINTENANCE_PLAN_COMPLETE.*final non-empty line outside every event tag' .factory/prompts/maintenance-plan.md
grep -q 'MAINTENANCE_COMPLETE.*final non-empty line outside every event tag' .factory/prompts/maintenance.md
grep -q 'AUDIT_COMPLETE.*final non-empty line' .factory/prompts/audit.md
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
for config in .factory/ralph/plan.yml .factory/ralph/implementation.yml \
        .factory/ralph/audit.yml .factory/ralph/maintenance-plan.yml \
        .factory/ralph/maintenance.yml; do
    checkpoint=$(grep 'command: \["./scripts/check-scratchpad.sh"' "$config")
    [[ $checkpoint != *'_COMPLETE"'* ]] || {
        echo "verify: iteration scratchpad hook must defer token rejection to the completion gate: $config" >&2
        exit 1
    }
done
grep -q '.factory/environment.toml' .factory/prompts/plan.md
grep -q '.factory/environment.toml' .factory/prompts/implementation.md
grep -q 'check-factory-runner-evidence.py' .factory/prompts/plan.md
grep -q 'check-factory-runner-evidence.py' .factory/prompts/implementation.md
grep -q 'check-factory-runner-evidence.py' .factory/prompts/audit.md
./scripts/check-factory-environment.py
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
./tests/test-ralph-completion-recovery.sh
./tests/test-installed-functional-evidence.sh "$PROJECT_ROOT"
./tests/test-factory-environment.sh
./tests/test-factory-runner.sh
./tests/test-campaign-audit.sh
./tests/test-ralph-campaign.sh
./tests/test-ralph-stale-recovery.sh
./tests/test-ralph-recover-safety.sh
./tests/test-pi2-ollama-wrapper.sh
./tests/test-boilerplate.sh
echo "verify: boilerplate checks passed"
