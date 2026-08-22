#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
PROJECT_ROOT=$(cd -- "$SCRIPT_DIR/.." && pwd)
GUARD="$PROJECT_ROOT/scripts/ollama-usage-guard.sh"

json=$($GUARD --check --json --html-file "$SCRIPT_DIR/fixtures/usage-ok.html")
python3 -c 'import json,sys; d=json.load(sys.stdin); assert d["blocked"] is False; assert d["session_percent"] == 12.5' <<< "$json"

set +e
$GUARD --check --html-file "$SCRIPT_DIR/fixtures/usage-blocked.html" >/dev/null
blocked_rc=$?
OLLAMA_WAIT_MAX_POLLS=1 OLLAMA_WAIT_INTERVAL_SECONDS=0 \
    $GUARD --wait --html-file "$SCRIPT_DIR/fixtures/usage-blocked.html" >/dev/null 2>&1
wait_rc=$?
$GUARD --check --html-file "$SCRIPT_DIR/fixtures/login.html" >/dev/null 2>&1
login_rc=$?
set -e
[[ $blocked_rc -eq 1 ]] || { echo "test: blocked usage returned $blocked_rc" >&2; exit 1; }
[[ $wait_rc -eq 1 ]] || { echo "test: bounded wait returned $wait_rc" >&2; exit 1; }
[[ $login_rc -eq 2 ]] || { echo "test: login page returned $login_rc" >&2; exit 1; }

tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
mkdir -p "$tmp/scripts" "$tmp/docs" "$tmp/.factory/artifacts"
cp "$PROJECT_ROOT/scripts/check-plan-freshness.sh" "$tmp/scripts/"
printf '# Trial specification\n' > "$tmp/docs/SPEC.md"
cat > "$tmp/.factory/config.toml" <<'EOF'
[project]
spec = "docs/SPEC.md"
EOF
git -C "$tmp" init -q
git -C "$tmp" config user.name test
git -C "$tmp" config user.email test@example.invalid
git -C "$tmp" add docs/SPEC.md
git -C "$tmp" commit -qm spec
spec_commit=$(git -C "$tmp" rev-parse HEAD)
spec_blob=$(git -C "$tmp" rev-parse HEAD:docs/SPEC.md)
cat > "$tmp/.factory/artifacts/implementation-plan.md" <<EOF
---
spec_path: docs/SPEC.md
spec_commit: $spec_commit
spec_blob: $spec_blob
base_commit: $spec_commit
status: active
---
# Plan
EOF
git -C "$tmp" add .factory/artifacts/implementation-plan.md .factory/config.toml scripts/check-plan-freshness.sh
git -C "$tmp" commit -qm plan
"$tmp/scripts/check-plan-freshness.sh" >/dev/null
printf '\nchanged\n' >> "$tmp/docs/SPEC.md"
set +e
"$tmp/scripts/check-plan-freshness.sh" >/dev/null 2>&1
stale_rc=$?
set -e
[[ $stale_rc -eq 1 ]] || { echo "test: dirty specification was not rejected" >&2; exit 1; }

FACTORY_ALLOW_TRIAL_BRANCH=1 "$PROJECT_ROOT/scripts/branch-guard.sh" >/dev/null

"$PROJECT_ROOT/scripts/bug-ledger.py" validate >/dev/null
cmp -s "$PROJECT_ROOT/.github/ISSUE_TEMPLATE/bug_report.md" "$PROJECT_ROOT/.forgejo/ISSUE_TEMPLATE/bug_report.md"
python3 - "$PROJECT_ROOT" <<'PY'
import pathlib, sys, tomllib
root = pathlib.Path(sys.argv[1])
with (root / '.factory/config.toml').open('rb') as stream:
    config = tomllib.load(stream)
assert config['concurrency']['mutating_workers'] == 1
assert config['concurrency']['integration_workers'] == 1
assert config['git']['allow_worktrees'] is False
assert isinstance(config.get('campaign', {}).get('required_capabilities'), list)
assert all(isinstance(item, str) and item for item in config['campaign']['required_capabilities'])
assert config['verification']['maintenance_command'] == ['./scripts/verify-boilerplate.sh']
assert isinstance(config['verification']['campaign_command'], list)
assert config['verification']['campaign_command']
assert all(isinstance(arg, str) and arg for arg in config['verification']['campaign_command'])
assert config['issues']['providers'] == ['github', 'forgejo']
assert config['issues']['external_sync'] == 'manual'
assert config['issues']['credentials'] is False
for path in ('AGENTS.md', '.factory/bugs/open.md', '.factory/bugs/closed.md', '.factory/artifacts/maintenance-plan.md',
             '.factory/ralph/maintenance.yml', '.factory/ralph/maintenance-plan.yml',
             'scripts/bug-ledger.py', 'scripts/validate-maintenance-plan.py',
             'scripts/validate-implementation-plan.py',
             'scripts/check-scratchpad.sh', 'tests/test-scratchpad-guard.sh',
             'scripts/ralph-completion-gate.sh', 'scripts/ralph-supervision.sh',
             'scripts/factory-lock.sh', 'scripts/factory-lock-exec.py',
             'scripts/factory_lock.py', 'scripts/factory_state_io.py',
             'scripts/factory-state-file.py', 'scripts/ralph_lock.py',
             'scripts/ralph-lock-recover.py', 'scripts/ralph-event-boundary.py',
             'scripts/campaign-verifier-binding.py', 'scripts/ralph-supervision-migrate.py',
             'scripts/ralph-final-state.py', 'scripts/finalize-maintenance-planning.sh',
             'tests/test-git-checkpoint.sh',
             'tests/test-ralph-completion-recovery.sh',
             'tests/test-maintenance-planning-completion.sh',
             'scripts/ralph-maintenance-plan.sh',
             'scripts/ralph-maintenance-run.sh', 'docs/BUG_WORKFLOW.md',
             '.factory/environment.toml', '.factory/artifacts/campaign-audit.md', '.factory/ralph/audit.yml',
             '.factory/prompts/audit.md', 'scripts/check-factory-environment.py',
             'scripts/ralph-campaign-state.py', 'scripts/initialize-campaign-audit.py',
             'scripts/validate-campaign-audit.py', 'scripts/campaign-audit-scope-guard.sh',
             'scripts/ralph-audit.sh', 'scripts/ralph-campaign.sh',
             'scripts/ralph-verifier-migrate.sh', '.factory/verifier-acceptance.json',
             'tests/test-factory-environment.sh', 'tests/test-campaign-audit.sh',
             'tests/test-ralph-campaign.sh', 'tests/test-ralph-campaign-state.py',
             'tests/test-factory-lock.py', 'tests/test-orchestration-security.py',
             'scripts/pi2-secure-exec.py',
             'scripts/pi-cli-shims/ralph', 'scripts/pi-ralph-emit-extension.mjs',
             'tests/test-pi2-ollama-wrapper.sh'):
    assert (root / path).is_file(), f'missing maintenance artifact: {path}'
PY
for config in .factory/ralph/implementation.yml .factory/ralph/plan.yml .factory/ralph/audit.yml .factory/ralph/maintenance.yml .factory/ralph/maintenance-plan.yml; do
    grep -q 'parallel: false' "$PROJECT_ROOT/$config"
    grep -q 'check-scratchpad.sh' "$PROJECT_ROOT/$config"
    grep -q -- '--allow-oversize' "$PROJECT_ROOT/$config"
    grep -q 'ralph-completion-gate.sh' "$PROJECT_ROOT/$config"
done
python3 - "$PROJECT_ROOT" <<'PY'
import pathlib, sys
root = pathlib.Path(sys.argv[1])
for name, mode in {
    'ralph-plan.sh': 'planning', 'ralph-run.sh': 'implementation',
    'ralph-audit.sh': 'campaign-audit',
    'ralph-maintenance-plan.sh': 'maintenance-planning',
    'ralph-maintenance-run.sh': 'maintenance',
}.items():
    text = (root / 'scripts' / name).read_text(encoding='utf-8')
    lock = text.index('factory_lock_acquire')
    marker = text.index(f'write loop-mode {mode}')
    assert marker > lock, f'{name}: loop-mode marker is not under factory lock'
tokens = {
    '.factory/ralph/implementation.yml': 'LOOP_COMPLETE',
    '.factory/ralph/plan.yml': 'PLAN_COMPLETE',
    '.factory/ralph/audit.yml': 'AUDIT_COMPLETE',
    '.factory/ralph/maintenance.yml': 'MAINTENANCE_COMPLETE',
    '.factory/ralph/maintenance-plan.yml': 'MAINTENANCE_PLAN_COMPLETE',
}
for name, token in tokens.items():
    text = (root / name).read_text(encoding='utf-8')
    assert text.index('check-scratchpad.sh') < text.index('git-commit-hook.sh'), \
        f'{name}: scratchpad guard must run before checkpoint'
    assert f'check-scratchpad.sh", "{token}"' in text, f'{name}: lifecycle token guard missing'
    if name != '.factory/ralph/maintenance-plan.yml':
        final_checkpoint = text.rindex('git-commit-hook.sh')
        final_gate = text.rindex('ralph-completion-gate.sh')
        assert final_checkpoint < final_gate, f'{name}: completion gate must attest after final checkpoint'
planning = (root / '.factory/ralph/plan.yml').read_text(encoding='utf-8')
assert planning.index('check-plan-freshness.sh", "--planning') < planning.index('git-commit-hook.sh'), \
    '.factory/ralph/plan.yml: immutable planning metadata must be checked before checkpoint'
maintenance = (root / 'scripts/ralph-maintenance-plan.sh').read_text(encoding='utf-8')
lock = maintenance.index('factory_lock_acquire')
selection = maintenance.index('write maintenance-bug-id "$BUG_ID"')
clean = maintenance.index('git status --porcelain')
assert lock < clean < selection, 'maintenance selection/clean check is not serialized'
recover = (root / 'scripts/ralph-recover.sh').read_text(encoding='utf-8')
assert "does not match recorded loop mode" in recover
pi2_wrapper = (root / 'scripts/pi2-ollama.sh').read_text(encoding='utf-8')
assert 'pi2-secure-exec.py' in pi2_wrapper
assert 'pi-ralph-emit-extension.mjs' in pi2_wrapper
pi2_shim = (root / 'scripts/pi-cli-shims/ralph').read_text(encoding='utf-8')
assert "${1:-} != emit" in pi2_shim
assert "s/^Event emitted:/Event published:/" in pi2_shim
for name in ('ralph-run.sh', 'ralph-plan.sh', 'ralph-audit.sh', 'ralph-maintenance-run.sh', 'ralph-maintenance-plan.sh'):
    launcher = (root / 'scripts' / name).read_text(encoding='utf-8')
    assert 'factory_lock_bootstrap' in launcher
    assert 'ralph-supervision.sh' in launcher
    assert 'ralph_supervision_initialize' in launcher
    assert 'ralph_supervision_begin' in launcher
    assert 'ralph_supervision_consume_rejection' in launcher
    assert '--loop-id "$rejected_loop_id"' in launcher
audit = (root / 'scripts/ralph-audit.sh').read_text(encoding='utf-8')
assert 'ralph-campaign-state.py audit-binding' in audit
assert 'FACTORY_CAMPAIGN_RUNNER_EVIDENCE_SHA256=${saved_binding[2]}' in audit
maintenance_hooks = (root / '.factory/ralph/maintenance-plan.yml').read_text(encoding='utf-8')
# The untrusted completion hook may only validate unprivileged completion
# artifacts. No lock-needing finalizer or strict checkpoint may run in the
# hook chain; the trusted parent performs the ledger transition, final
# handoff, gate attestation, and final-state attestation under the lock.
assert 'finalize-maintenance-planning.sh' not in maintenance_hooks
assert '--final-handoff' not in maintenance_hooks
pre_complete = maintenance_hooks.split('pre.loop.complete:', 1)[1]
assert 'git-commit-hook.sh' not in pre_complete
assert pre_complete.count('command: [') == 1
assert 'ralph-completion-gate.sh", "maintenance-planning"' in pre_complete
launcher = (root / 'scripts/ralph-maintenance-plan.sh').read_text(encoding='utf-8')
assert 'finalize-maintenance-planning.sh' in launcher
assert 'git-commit-hook.sh --maintenance-plan --final-handoff' in launcher
assert 'final-gate.sh --maintenance-planning' in launcher
assert 'ralph-final-state.py attest maintenance-planning' in launcher
completion_gate = (root / 'scripts/ralph-completion-gate.sh').read_text(encoding='utf-8')
assert "mode == 'maintenance-planning'" in completion_gate
PY
"$PROJECT_ROOT/tests/test-git-checkpoint.sh"
"$PROJECT_ROOT/tests/test-bug-workflow.sh"
"$PROJECT_ROOT/tests/test-plan-cycle.sh"
"$PROJECT_ROOT/tests/test-ralph-completion-recovery.sh"
"$PROJECT_ROOT/tests/test-factory-environment.sh"
"$PROJECT_ROOT/tests/test-campaign-audit.sh"
"$PROJECT_ROOT/tests/test-ralph-campaign.sh"
"$PROJECT_ROOT/tests/test-ralph-campaign-state.py"
"$PROJECT_ROOT/tests/test-factory-lock.py"
"$PROJECT_ROOT/tests/test-orchestration-security.py"

echo "test: boilerplate integration checks passed"
