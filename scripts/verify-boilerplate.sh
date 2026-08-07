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
with open('factory.toml', 'rb') as stream:
    config = tomllib.load(stream)
assert config['concurrency']['mutating_workers'] == 1
assert config['concurrency']['integration_workers'] == 1
assert config['git']['allow_worktrees'] is False
assert config['verification']['maintenance_command'] == ['./scripts/verify-project.sh']
assert config['issues'] == {
    'schema': 'ralph-bug-ledger/v1',
    'open_ledger': 'open-bugs.md',
    'closed_ledger': 'closed-bugs.md',
    'maintenance_plan': 'MAINTENANCE_PLAN.md',
    'final_task_title': 'Maintenance verification and documentation audit',
    'providers': ['github', 'forgejo'],
    'external_sync': 'manual',
    'credentials': False,
}
required = [
    'open-bugs.md', 'closed-bugs.md', 'MAINTENANCE_PLAN.md',
    'ralph.maintenance.yml', 'ralph.maintenance-plan.yml',
    'prompts/MAINTENANCE.md', 'prompts/MAINTENANCE_PLAN.md',
    'scripts/bug-ledger.py', 'scripts/validate-maintenance-plan.py',
    'scripts/initialize-plan-cycle.py', 'scripts/check-maintenance-freshness.sh',
    'scripts/maintenance-plan-scope-guard.sh',
    'scripts/ralph-maintenance-plan.sh', 'scripts/ralph-maintenance-run.sh',
    'docs/BUG_WORKFLOW.md', 'tests/test-bug-workflow.sh',
    'tests/test-plan-cycle.sh',
]
for name in required:
    assert pathlib.Path(name).is_file(), f'missing {name}'
json.load(open('.pi/subagents.json', encoding='utf-8'))
for path in pathlib.Path('.pi/agents').glob('*.md'):
    text = path.read_text(encoding='utf-8')
    header = text.split('---', 2)[1]
    tools = next(line for line in header.splitlines() if line.startswith('tools:'))
    for forbidden in ('edit', 'write', 'bash'):
        assert forbidden not in tools, f'{path}: read-only agent exposes {forbidden}'
PY

for config in ralph.yml ralph.plan.yml ralph.maintenance.yml ralph.maintenance-plan.yml; do
    grep -q 'parallel: false' "$config"
done
grep -q 'Final documentation and specification audit' prompts/PLAN.md
grep -q 'Maintenance verification and documentation audit' prompts/MAINTENANCE_PLAN.md
grep -q 'LOOP_COMPLETE' PROMPT.md
grep -q 'MAINTENANCE_COMPLETE' prompts/MAINTENANCE.md
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

./tests/test-boilerplate.sh
echo "verify: boilerplate checks passed"
