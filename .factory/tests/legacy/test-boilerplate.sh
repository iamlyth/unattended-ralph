#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
PROJECT_ROOT=$(cd -- "$SCRIPT_DIR/../../.." && pwd)
GUARD="$PROJECT_ROOT/.factory/tools/ollama-usage-guard.sh"

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
mkdir -p "$tmp/scripts" "$tmp/docs" "$tmp/.factory/artifacts" "$tmp/.factory/loop"
cp "$PROJECT_ROOT/.factory/tools/check-plan-freshness.sh" "$tmp/scripts/"
cp "$PROJECT_ROOT/.factory/loop/gitutil.py" "$tmp/.factory/loop/"
# The Task-23 freshness scope requires the canonical policy authorities to
# be real tracked files at HEAD.
cp "$PROJECT_ROOT/.factory/campaign-receipt-policy.json" "$tmp/.factory/"
cp "$PROJECT_ROOT/.factory/requirement-policy.json" "$tmp/.factory/"
cp "$PROJECT_ROOT/.factory/capability-contracts.json" "$tmp/.factory/"
printf '# Trial specification\n' > "$tmp/docs/SPEC.md"
cat > "$tmp/.factory/config.toml" <<'EOF'
[project]
spec = "docs/SPEC.md"
EOF
git -C "$tmp" init -q
git -C "$tmp" config user.name test
git -C "$tmp" config user.email test@example.invalid
git -C "$tmp" add docs/SPEC.md .factory/campaign-receipt-policy.json \
    .factory/requirement-policy.json .factory/capability-contracts.json \
    .factory/loop/gitutil.py
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
git -C "$tmp" add .factory/artifacts/implementation-plan.md .factory/config.toml .factory/tools/check-plan-freshness.sh
git -C "$tmp" commit -qm plan
"$tmp/scripts/check-plan-freshness.sh" >/dev/null
printf '\nchanged\n' >> "$tmp/docs/SPEC.md"
set +e
"$tmp/scripts/check-plan-freshness.sh" >/dev/null 2>&1
stale_rc=$?
set -e
[[ $stale_rc -eq 1 ]] || { echo "test: dirty specification was not rejected" >&2; exit 1; }

FACTORY_ALLOW_TRIAL_BRANCH=1 "$PROJECT_ROOT/.factory/tools/branch-guard.sh" >/dev/null

"$PROJECT_ROOT/.factory/tools/bug-ledger.py" validate >/dev/null
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
assert config['verification']['maintenance_command'] == ['./.factory/tools/verify-boilerplate.sh']
assert isinstance(config['verification']['campaign_command'], list)
assert config['verification']['campaign_command']
assert all(isinstance(arg, str) and arg for arg in config['verification']['campaign_command'])
assert config['issues']['providers'] == ['github', 'forgejo']
assert config['issues']['external_sync'] == 'manual'
assert config['issues']['credentials'] is False
for path in ('AGENTS.md', '.factory/bugs/open.md', '.factory/bugs/closed.md', '.factory/artifacts/maintenance-plan.md',
             '.factory/ralph/maintenance.yml', '.factory/ralph/maintenance-plan.yml',
             '.factory/tools/bug-ledger.py', '.factory/tools/validate-maintenance-plan.py',
             '.factory/tools/validate-implementation-plan.py',
             '.factory/tools/check-scratchpad.sh', '.factory/tests/legacy/test-scratchpad-guard.sh',
             '.factory/tools/ralph-completion-gate.sh', '.factory/tools/ralph-supervision.sh',
             '.factory/tools/factory-lock.sh', '.factory/tools/factory-lock-exec.py',
             '.factory/tools/factory_lock.py', '.factory/tools/factory_state_io.py',
             '.factory/tools/factory-state-file.py', '.factory/tools/ralph_lock.py',
             '.factory/tools/ralph-lock-recover.py',
             '.factory/tools/campaign-verifier-binding.py', '.factory/tools/ralph-supervision-migrate.py',
             '.factory/tests/legacy/test-git-checkpoint.sh',
             '.factory/tests/legacy/test-ralph-completion-recovery.sh',
             '.factory/tests/legacy/test-maintenance-planning-completion.sh',
             '.factory/tools/ralph-maintenance-plan.sh',
             '.factory/tools/ralph-maintenance-run.sh', 'docs/BUG_WORKFLOW.md',
             '.factory/environment.toml', '.factory/artifacts/campaign-audit.md', '.factory/ralph/audit.yml',
             '.factory/prompts/audit.md', '.factory/tools/check-factory-environment.py',
             '.factory/tools/ralph-campaign-state.py', '.factory/tools/initialize-campaign-audit.py',
             '.factory/tools/validate-campaign-audit.py', '.factory/tools/campaign-audit-scope-guard.sh',
             '.factory/tools/ralph-audit.sh', '.factory/tools/ralph-campaign.sh',
             '.factory/tools/ralph-verifier-migrate.sh', '.factory/verifier-acceptance.json',
             '.factory/tests/legacy/test-factory-environment.sh', '.factory/tests/legacy/test-campaign-audit.sh',
             '.factory/tests/legacy/test-ralph-campaign.sh', '.factory/tests/legacy/test-ralph-campaign-state.py',
             '.factory/tests/legacy/test-factory-lock.py', '.factory/tests/legacy/test-orchestration-security.py',
             '.factory/tools/pi2-secure-exec.py',
             '.factory/tools/pi-cli-shims/ralph',
             '.factory/tests/legacy/test-pi2-ollama-wrapper.sh',
             '.factory/tests/legacy/test-visual-audit-sdk-authority.sh'):
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
    text = (root / '.factory' / 'tools' / name).read_text(encoding='utf-8')
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
    assert f'check-scratchpad.sh", "{token}"' in text, f'{name}: lifecycle token guard missing'
planning = (root / '.factory/ralph/plan.yml').read_text(encoding='utf-8')
assert 'check-plan-freshness.sh", "--planning' in planning, \
    '.factory/ralph/plan.yml: immutable planning metadata must be checked before checkpoint'
maintenance = (root / '.factory/tools/ralph-maintenance-plan.sh').read_text(encoding='utf-8')
lock = maintenance.index('factory_lock_acquire')
selection = maintenance.index('write maintenance-bug-id "$BUG_ID"')
clean = maintenance.index('git status --porcelain')
assert lock < clean < selection, 'maintenance selection/clean check is not serialized'
recover = (root / '.factory/tools/ralph-recover.sh').read_text(encoding='utf-8')
assert "does not match recorded loop mode" in recover
pi2_wrapper = (root / '.factory/tools/pi2-ollama.sh').read_text(encoding='utf-8')
assert 'pi2-secure-exec.py' in pi2_wrapper
assert 'pi-factory-guard-extension.mjs' in pi2_wrapper
pi2_shim = (root / '.factory/tools/pi-cli-shims/ralph').read_text(encoding='utf-8')
assert "${1:-} != emit" in pi2_shim
assert "s/^Event emitted:/Event published:/" in pi2_shim
for name in ('ralph-run.sh', 'ralph-plan.sh', 'ralph-audit.sh', 'ralph-maintenance-run.sh', 'ralph-maintenance-plan.sh'):
    launcher = (root / '.factory' / 'tools' / name).read_text(encoding='utf-8')
    assert 'factory_lock_bootstrap' in launcher
    assert 'ralph-supervision.sh' in launcher
    assert 'ralph_supervision_initialize' in launcher
    assert 'ralph_supervision_begin' in launcher
    assert 'ralph_supervision_consume_rejection' in launcher
    assert '--loop-id "$rejected_loop_id"' in launcher
audit = (root / '.factory/tools/ralph-audit.sh').read_text(encoding='utf-8')
assert 'ralph-campaign-state.py audit-binding' in audit
assert 'FACTORY_CAMPAIGN_RUNNER_EVIDENCE_SHA256=${saved_binding[2]}' in audit
maintenance_hooks = (root / '.factory/ralph/maintenance-plan.yml').read_text(encoding='utf-8')
# The untrusted completion hook may only validate unprivileged completion
# artifacts. No lock-needing finalizer or strict checkpoint may run in the
# hook chain; the trusted parent performs the ledger transition, final
# handoff, gate attestation, and final-state attestation under the lock.
assert '--final-handoff' not in maintenance_hooks
pre_complete = maintenance_hooks.split('pre.loop.complete:', 1)[1]
assert pre_complete.count('command: [') == 1
launcher = (root / '.factory/tools/ralph-maintenance-plan.sh').read_text(encoding='utf-8')
assert 'final-gate.sh --maintenance-planning' in launcher
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
