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
mkdir -p "$tmp/scripts" "$tmp/docs"
cp "$PROJECT_ROOT/scripts/check-plan-freshness.sh" "$tmp/scripts/"
printf '# Trial specification\n' > "$tmp/docs/SPEC.md"
cat > "$tmp/factory.toml" <<'EOF'
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
cat > "$tmp/IMPLEMENTATION_PLAN.md" <<EOF
---
spec_path: docs/SPEC.md
spec_commit: $spec_commit
spec_blob: $spec_blob
base_commit: $spec_commit
status: active
---
# Plan
EOF
git -C "$tmp" add IMPLEMENTATION_PLAN.md factory.toml scripts/check-plan-freshness.sh
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
with (root / 'factory.toml').open('rb') as stream:
    config = tomllib.load(stream)
assert config['concurrency']['mutating_workers'] == 1
assert config['concurrency']['integration_workers'] == 1
assert config['git']['allow_worktrees'] is False
assert config['verification']['maintenance_command'] == ['./scripts/verify-project.sh']
assert config['issues']['providers'] == ['github', 'forgejo']
assert config['issues']['external_sync'] == 'manual'
assert config['issues']['credentials'] is False
for path in ('AGENTS.md', 'open-bugs.md', 'closed-bugs.md', 'MAINTENANCE_PLAN.md',
             'ralph.maintenance.yml', 'ralph.maintenance-plan.yml',
             'scripts/bug-ledger.py', 'scripts/validate-maintenance-plan.py',
             'scripts/validate-implementation-plan.py',
             'scripts/ralph-maintenance-plan.sh',
             'scripts/ralph-maintenance-run.sh', 'docs/BUG_WORKFLOW.md'):
    assert (root / path).is_file(), f'missing maintenance artifact: {path}'
PY
for config in ralph.yml ralph.plan.yml ralph.maintenance.yml ralph.maintenance-plan.yml; do
    grep -q 'parallel: false' "$PROJECT_ROOT/$config"
done
python3 - "$PROJECT_ROOT" <<'PY'
import pathlib, sys
root = pathlib.Path(sys.argv[1])
for name, mode in {
    'ralph-plan.sh': 'planning', 'ralph-run.sh': 'implementation',
    'ralph-maintenance-plan.sh': 'maintenance-planning',
    'ralph-maintenance-run.sh': 'maintenance',
}.items():
    text = (root / 'scripts' / name).read_text(encoding='utf-8')
    lock = text.index('factory_lock_acquire')
    marker = text.index(f"printf '%s\\n' {mode} > .factory-state/loop-mode")
    assert marker > lock, f'{name}: loop-mode marker is not under factory lock'
maintenance = (root / 'scripts/ralph-maintenance-plan.sh').read_text(encoding='utf-8')
lock = maintenance.index('factory_lock_acquire')
selection = maintenance.index('> .factory-state/maintenance-bug-id')
clean = maintenance.index('git status --porcelain')
assert lock < clean < selection, 'maintenance selection/clean check is not serialized'
recover = (root / 'scripts/ralph-recover.sh').read_text(encoding='utf-8')
assert "does not match recorded loop mode" in recover
PY
"$PROJECT_ROOT/tests/test-bug-workflow.sh"
"$PROJECT_ROOT/tests/test-plan-cycle.sh"

echo "test: boilerplate integration checks passed"
