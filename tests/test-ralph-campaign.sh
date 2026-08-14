#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
PROJECT_ROOT=$(cd -- "$SCRIPT_DIR/.." && pwd)
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
mkdir -p "$tmp/scripts" "$tmp/docs" "$tmp/.ralph/agent" "$tmp/.factory-state" \
    "$tmp/.factory/artifacts"
cp "$PROJECT_ROOT/scripts/ralph-campaign.sh" \
   "$PROJECT_ROOT/scripts/ralph-campaign-state.py" \
   "$PROJECT_ROOT/scripts/initialize-campaign-audit.py" \
   "$PROJECT_ROOT/scripts/check-factory-environment.py" \
   "$PROJECT_ROOT/scripts/run-factory-runners.py" \
   "$PROJECT_ROOT/scripts/check-factory-runner-evidence.py" "$tmp/scripts/"
chmod +x "$tmp/scripts/"*
cat > "$tmp/.factory/environment.toml" <<'EOF'
schema_version = 1
EOF
printf '# Spec\n' > "$tmp/docs/SPEC.md"
printf '# Initial plan\n' > "$tmp/.factory/artifacts/implementation-plan.md"
printf '# Campaign Audit\n' > "$tmp/.factory/artifacts/campaign-audit.md"
printf '# Initial scratchpad\n' > "$tmp/.ralph/agent/scratchpad.md"
printf 'base\n' > "$tmp/product.txt"
cat > "$tmp/.factory/config.toml" <<'EOF'
[project]
spec = "docs/SPEC.md"
plan = ".factory/artifacts/implementation-plan.md"
development_branch = "develop"
[verification]
campaign_command = ["./scripts/verify-project.sh"]
[git]
allow_worktrees = false
EOF
cat > "$tmp/.gitignore" <<'EOF'
.factory-state/
.factory-lock
.ralph/*
!.ralph/agent/
.ralph/agent/*
!.ralph/agent/scratchpad.md
EOF
cat > "$tmp/scripts/branch-guard.sh" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
[[ $(git branch --show-current) == develop ]]
EOF
cat > "$tmp/scripts/factory-lock.sh" <<'EOF'
#!/usr/bin/env bash
factory_lock_acquire() { export FACTORY_LOCK_HELD=1; }
EOF
cat > "$tmp/scripts/ralph-plan.sh" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
printf 'plan %s\n' "$*" >> .factory-state/calls
base=$(git rev-parse HEAD)
if [[ ${1:-} == --resume ]]; then base=$(cat .factory-state/planning-base-commit); else printf '%s\n' "$base" > .factory-state/planning-base-commit; fi
cat > .factory/artifacts/implementation-plan.md <<PLAN
---
spec_path: docs/SPEC.md
spec_commit: $(git log -1 --format=%H -- docs/SPEC.md)
spec_blob: $(git rev-parse HEAD:docs/SPEC.md)
base_commit: $base
status: active
---
# Plan
## Specification conformance matrix
| ID | Status |
| --- | --- |
| FR-1 | verified |
## Interaction acceptance inventory
| ID | Evidence |
| --- | --- |
| I-1 | fake |
## Task 1: Final documentation and specification audit
- Status: pending
- Dependencies: none
- Scope: fake
- Acceptance criteria: fake
- Verification: fake
- Documentation impact: none
PLAN
printf '# Planning handoff\n- complete\n' > .ralph/agent/scratchpad.md
git add .factory/artifacts/implementation-plan.md .ralph/agent/scratchpad.md
git commit -qm "fake plan"
EOF
cat > "$tmp/scripts/ralph-run.sh" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
printf 'implement %s\n' "$*" >> .factory-state/calls
if [[ -n ${FAKE_FAIL_IMPL_ONCE:-} && ! -e .factory-state/failed-implementation ]]; then
    : > .factory-state/failed-implementation
    [[ -z ${FAKE_DIRTY_IMPL:-} ]] || printf 'interrupted work\n' > product.txt
    exit 42
fi
sed -i 's/status: active/status: complete/; s/- Status: pending/- Status: complete/' .factory/artifacts/implementation-plan.md
printf '# Implementation handoff\n- complete\n' > .ralph/agent/scratchpad.md
git add .factory/artifacts/implementation-plan.md .ralph/agent/scratchpad.md product.txt
git commit -qm "fake implementation"
EOF
cat > "$tmp/scripts/ralph-audit.sh" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
printf 'audit %s\n' "$*" >> .factory-state/calls
python3 - <<'PY'
from pathlib import Path
p=Path('.factory/artifacts/campaign-audit.md')
s=p.read_text().replace('result: pending', 'result: findings' if __import__('os').environ.get('FAKE_AUDIT_FINDINGS') else 'result: pass')
head=s.split('---\n',2)[:2]
front='---\n'+head[1]+'---\n'
evidence='## Evidence reviewed\n- Specification: fake requirement\n- Production paths: fake trace\n- Executable evidence: fake command\n- Environment limits: no runner\n\n'
if __import__('os').environ.get('FAKE_AUDIT_FINDINGS'):
 body='# Audit\n\n'+evidence+'## Finding 1: Gap\n- Requirement: real behavior\n- Production evidence: missing\n- Required remediation: implement it\n'
else:
 body='# Audit\n\n'+evidence+'## Findings\nNone.\n'
p.write_text(front+body)
PY
printf '# Audit handoff\n- complete\n' > .ralph/agent/scratchpad.md
git add .factory/artifacts/campaign-audit.md .ralph/agent/scratchpad.md
git commit -qm "fake audit"
EOF
cat > "$tmp/scripts/final-gate.sh" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
case ${1:-} in
 --planning) grep -q '^status: active$' .factory/artifacts/implementation-plan.md ;;
 --implementation) grep -q '^status: complete$' .factory/artifacts/implementation-plan.md ;;
 --campaign-audit) grep -Eq '^result: (pass|findings)$' .factory/artifacts/campaign-audit.md ;;
 *) exit 2 ;;
esac
EOF
cat > "$tmp/scripts/verify-project.sh" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
printf 'verify\n' >> .factory-state/calls
EOF
cat > "$tmp/scripts/check-installed-functional-evidence.sh" <<'EOF'
#!/usr/bin/env bash
exit 0
EOF
chmod +x "$tmp/scripts/"*.sh

git -C "$tmp" init -q -b develop
git -C "$tmp" config user.name test
git -C "$tmp" config user.email test@example.invalid
git -C "$tmp" add .
git -C "$tmp" commit -qm initial

(cd "$tmp" && ./scripts/ralph-campaign.sh --rounds 3 --no-tui >/dev/null)
python3 - "$tmp" <<'PY'
import json, pathlib, sys
root=pathlib.Path(sys.argv[1])
state=json.loads((root/'.factory-state/ralph-campaign.json').read_text())
assert state['status']=='complete' and state['round']==3
bases=[r['base_commit'] for r in state['rounds']]
assert len(set(bases))==3, bases
assert all(r['audit_result']=='pass' for r in state['rounds'])
calls=(root/'.factory-state/calls').read_text().splitlines()
assert [c.split()[0] for c in calls] == ['plan','implement','verify','audit']*3, calls
assert all('--no-tui' in c for c in calls if c.startswith(('plan ','implement ','audit ')))
PY

set +e
(cd "$tmp" && FAKE_FAIL_IMPL_ONCE=1 FAKE_DIRTY_IMPL=1 ./scripts/ralph-campaign.sh --rounds 1 --restart --no-tui >/dev/null 2>&1)
fail_rc=$?
set -e
[[ $fail_rc -eq 42 ]]
set +e
(cd "$tmp" && ./scripts/ralph-campaign.sh --rounds 1 --restart --no-tui >/dev/null 2>&1)
overwrite_rc=$?
set -e
[[ $overwrite_rc -eq 1 ]]
(cd "$tmp" && FAKE_FAIL_IMPL_ONCE=1 FAKE_DIRTY_IMPL=1 ./scripts/ralph-campaign.sh --rounds 1 --resume --no-tui >/dev/null)
grep -q '^implement --resume --no-tui$' "$tmp/.factory-state/calls"
python3 - "$tmp" <<'PY'
import json, pathlib, sys
state=json.loads((pathlib.Path(sys.argv[1])/'.factory-state/ralph-campaign.json').read_text())
assert state['status']=='complete'
PY

set +e
(cd "$tmp" && FAKE_AUDIT_FINDINGS=1 ./scripts/ralph-campaign.sh --rounds 1 --restart --no-tui >/dev/null 2>&1)
findings_rc=$?
(cd "$tmp" && ./scripts/ralph-campaign.sh --rounds 0 >/dev/null 2>&1)
invalid_rc=$?
set -e
[[ $findings_rc -eq 1 ]]
[[ $invalid_rc -eq 2 ]]
python3 - "$tmp" <<'PY'
import json, pathlib, sys
state=json.loads((pathlib.Path(sys.argv[1])/'.factory-state/ralph-campaign.json').read_text())
assert state['status']=='blocked' and state['phase']=='blocked-findings'
assert state['rounds'][-1]['audit_result']=='findings'
PY

# Invalid verification configuration must fail closed before any phase runs.
cp "$tmp/.factory/config.toml" "$tmp/.factory/config.toml.good"
sed -i '/campaign_command/d' "$tmp/.factory/config.toml"
set +e
(cd "$tmp" && ./scripts/ralph-campaign.sh --rounds 1 --no-tui >/dev/null 2>&1)
config_rc=$?
set -e
mv "$tmp/.factory/config.toml.good" "$tmp/.factory/config.toml"
[[ $config_rc -eq 1 ]]

# The real inherited descriptor lock is re-entrant for children and excludes a
# competing non-inheriting writer when flock is available.
if command -v flock >/dev/null; then
    lock_tmp=$(mktemp -d)
    cp "$PROJECT_ROOT/scripts/factory-lock.sh" "$lock_tmp/"
    bash -c 'source "$1/factory-lock.sh"; factory_lock_acquire "$1/lock"; bash -c '\''source "$1/factory-lock.sh"; factory_lock_acquire "$1/lock"'\'' _ "$1"; if env -u FACTORY_LOCK_HELD bash -c '\''source "$1/factory-lock.sh"; factory_lock_acquire "$1/lock"'\'' _ "$1"; then exit 1; fi' _ "$lock_tmp"
    rm -rf "$lock_tmp"
fi

echo "test: Ralph campaign sequencing and resume checks passed"
