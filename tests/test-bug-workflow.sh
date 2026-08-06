#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
PROJECT_ROOT=$(cd -- "$SCRIPT_DIR/.." && pwd)
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
mkdir -p "$tmp/scripts" "$tmp/docs" "$tmp/.factory-state"
cp "$PROJECT_ROOT/scripts/bug-ledger.py" "$PROJECT_ROOT/scripts/check-maintenance-freshness.sh" \
    "$PROJECT_ROOT/scripts/validate-maintenance-plan.py" "$PROJECT_ROOT/scripts/final-gate.sh" "$tmp/scripts/"
chmod +x "$tmp/scripts/"*
cd "$tmp"

reset_ledgers() {
    cat > open-bugs.md <<'EOF'
# Open Bugs

Canonical queue of defects awaiting maintenance.

Schema: `ralph-bug-ledger/v1`

```json
[]
```
EOF
    cat > closed-bugs.md <<'EOF'
# Closed Bugs

Completed defects and their verification evidence.

Schema: `ralph-bug-ledger/v1`

```json
[]
```
EOF
}
reset_ledgers
./scripts/bug-ledger.py validate >/dev/null
add_bug() {
    ./scripts/bug-ledger.py add --title "$1" --severity high --reported 2026-08-06 \
        --reproduction "Run the reproducer" --expected "Successful result" \
        --actual "Failure result" --acceptance "Regression test passes" "${@:2}"
}
add_bug "GitHub bug" --external github=https://github.com/example/project/issues/1 >/dev/null
add_bug "Forgejo bug" --external forgejo=https://code.example.org/team/project/issues/2 >/dev/null
add_bug "Dual bug" --external github=https://github.com/example/project/issues/3 \
    --external forgejo=https://forge.example.org/team/project/issues/9 >/dev/null
./scripts/bug-ledger.py validate >/dev/null
[[ $(./scripts/bug-ledger.py list | wc -l) -eq 3 ]]

# Deterministic add and transitions; closure requires evidence and moves the record.
cp open-bugs.md snapshot.md
./scripts/bug-ledger.py set-status BUG-0001 triaged
./scripts/bug-ledger.py set-status BUG-0001 planned
set +e
./scripts/bug-ledger.py close BUG-0001 --resolution fixed --verification tested --closed 2026-08-07 >/dev/null 2>&1
not_in_progress_close=$?
set -e
[[ $not_in_progress_close -eq 1 ]]
./scripts/bug-ledger.py set-status BUG-0001 in_progress
set +e
./scripts/bug-ledger.py set-status BUG-0001 open >/dev/null 2>&1
bad_transition=$?
./scripts/bug-ledger.py close BUG-0001 --resolution '' --verification evidence --closed 2026-08-07 >/dev/null 2>&1
bad_close=$?
set -e
[[ $bad_transition -eq 1 && $bad_close -eq 1 ]]
./scripts/bug-ledger.py close BUG-0001 --resolution "Corrected parser" --verification "Regression test passed" --closed 2026-08-07
./scripts/bug-ledger.py validate >/dev/null
./scripts/bug-ledger.py show BUG-0001 | grep -q '"status": "closed"'

# Link/unlink are validated mutations and do not alter immutable fingerprints.
before=$(./scripts/bug-ledger.py fingerprint BUG-0002)
./scripts/bug-ledger.py set-status BUG-0002 triaged
after_status=$(./scripts/bug-ledger.py fingerprint BUG-0002)
./scripts/bug-ledger.py link BUG-0002 github https://github.com/example/project/issues/22
set +e
./scripts/bug-ledger.py link BUG-0002 github https://github.com/example/project/issues/23 >/dev/null 2>&1
duplicate_provider_rc=$?
set -e
[[ $duplicate_provider_rc -eq 1 ]]
after_external=$(./scripts/bug-ledger.py fingerprint BUG-0002)
./scripts/bug-ledger.py unlink BUG-0002 github
[[ "$before" == "$after_status" && "$before" == "$after_external" ]]
python3 - <<'PY'
import json, re
path = 'open-bugs.md'; text = open(path, encoding='utf-8').read()
data = json.loads(re.search(r'```json\n(.*?)\n```', text, re.S).group(1))
data[0]['title'] += ' changed'
payload = json.dumps(data, indent=2, ensure_ascii=False) + '\n'
open(path, 'w', encoding='utf-8').write(re.sub(r'```json\n.*?\n```', '```json\n' + payload + '```', text, flags=re.S))
PY
[[ "$before" != "$(./scripts/bug-ledger.py fingerprint BUG-0002)" ]]

# Reject malformed JSON, duplicate IDs, invalid states, and unsafe/non-issue URLs.
cp open-bugs.md valid-open.md
reject_mutation() {
    cp valid-open.md open-bugs.md
    python3 - "$1" <<'PY'
import json, re, sys
path='open-bugs.md'; text=open(path, encoding='utf-8').read(); mode=sys.argv[1]
data=json.loads(re.search(r'```json\n(.*?)\n```', text, re.S).group(1))
if mode == 'duplicate': data.append(dict(data[0]))
elif mode == 'state': data[0]['status']='closed'
elif mode == 'credential': data[0]['external']=[{'provider':'github','url':'https://user:token@github.com/a/b/issues/1'}]
elif mode == 'query': data[0]['external']=[{'provider':'forgejo','url':'https://forge.example/a/b/issues/1?token=x'}]
elif mode == 'badport': data[0]['external']=[{'provider':'forgejo','url':'https://forge.example:bad/a/b/issues/1'}]
elif mode == 'badhost': data[0]['external']=[{'provider':'github','url':'https://[broken/a/b/issues/1'}]
payload=json.dumps(data, indent=2, ensure_ascii=False)+'\n'
open(path,'w',encoding='utf-8').write(re.sub(r'```json\n.*?\n```','```json\n'+payload+'```',text,flags=re.S))
PY
    set +e
    ./scripts/bug-ledger.py validate >/dev/null 2>&1
    rc=$?
    set -e
    [[ $rc -eq 1 ]] || { echo "test: accepted invalid ledger mutation $1" >&2; exit 1; }
}
for mutation in duplicate state credential query badport badhost; do reject_mutation "$mutation"; done
cp valid-open.md open-bugs.md
python3 - <<'PY'
p = 'open-bugs.md'
text = open(p, encoding='utf-8').read()
start = text.index('```json\n') + len('```json\n')
end = text.index('\n```', start)
open(p, 'w', encoding='utf-8').write(text[:start] + '[broken]' + text[end:])
PY
set +e
./scripts/bug-ledger.py validate >/dev/null 2>&1
malformed_rc=$?
set -e
[[ $malformed_rc -eq 1 ]]

# Concurrent adds serialize and allocate distinct IDs.
cp valid-open.md open-bugs.md
add_bug "Concurrent one" > add-one.out & first_pid=$!
add_bug "Concurrent two" > add-two.out & second_pid=$!
wait "$first_pid" "$second_pid"
[[ $(sort -u add-one.out add-two.out | wc -l) -eq 2 ]]
./scripts/bug-ledger.py validate >/dev/null

# Numeric ordering remains correct when canonical IDs exceed four digits.
python3 - <<'PY'
import json, re
path='open-bugs.md'; text=open(path, encoding='utf-8').read()
data=json.loads(re.search(r'```json\n(.*?)\n```', text, re.S).group(1))
data[-1]['id']='BUG-9999'
data=sorted(data, key=lambda item: int(item['id'][4:]))
payload=json.dumps(data, indent=2, ensure_ascii=False)+'\n'
open(path,'w',encoding='utf-8').write(re.sub(r'```json\n.*?\n```','```json\n'+payload+'```',text,flags=re.S))
PY
[[ $(add_bug "Five digit bug") == BUG-10000 ]]
[[ $(./scripts/bug-ledger.py list | tail -1 | cut -f1) == BUG-10000 ]]

# Recover only the detectable destination-first interrupted-close duplicate.
./scripts/bug-ledger.py set-status BUG-0003 triaged
./scripts/bug-ledger.py set-status BUG-0003 planned
./scripts/bug-ledger.py set-status BUG-0003 in_progress
cp open-bugs.md before-close.md
./scripts/bug-ledger.py close BUG-0003 --resolution fixed --verification tested --closed 2026-08-07
python3 - <<'PY'
import json, re
old=open('before-close.md', encoding='utf-8').read(); current=open('open-bugs.md', encoding='utf-8').read()
def records(text): return json.loads(re.search(r'```json\n(.*?)\n```', text, re.S).group(1))
data=records(current); data.append(next(r for r in records(old) if r['id']=='BUG-0003'))
data.sort(key=lambda r:int(r['id'][4:]))
payload=json.dumps(data, indent=2, ensure_ascii=False)+'\n'
open('open-bugs.md','w',encoding='utf-8').write(re.sub(r'```json\n.*?\n```','```json\n'+payload+'```',current,flags=re.S))
PY
set +e; ./scripts/bug-ledger.py validate >/dev/null 2>&1; interrupted_rc=$?; set -e
[[ $interrupted_rc -eq 1 ]]
./scripts/bug-ledger.py recover >/dev/null
./scripts/bug-ledger.py validate >/dev/null

# Freshness succeeds for an open bug and after closure only when the final audit is complete.
reset_ledgers
add_bug "Freshness bug" >/dev/null
printf '# Trial spec\n' > docs/SPEC.md
cat > factory.toml <<'EOF'
[project]
spec = "docs/SPEC.md"
[verification]
maintenance_command = ["./scripts/verify-project.sh"]
EOF
git init -q
git config user.name test
git config user.email test@example.invalid
git add .
git commit -qm base
base=$(git rev-parse HEAD)
spec_commit=$(git log -1 --format=%H -- docs/SPEC.md)
spec_blob=$(git rev-parse HEAD:docs/SPEC.md)
fingerprint=$(./scripts/bug-ledger.py fingerprint BUG-0001)
printf 'BUG-0001\n' > .factory-state/maintenance-bug-id
cat > MAINTENANCE_PLAN.md <<EOF
---
bug_id: BUG-0001
bug_fingerprint: $fingerprint
spec_path: docs/SPEC.md
spec_commit: $spec_commit
spec_blob: $spec_blob
base_commit: $base
status: active
---
# Maintenance Plan
## Task 1: Maintenance verification and documentation audit
- Status: pending
- Dependencies: none
- Scope: verify the selected defect
- Acceptance criteria: defect is fixed
- Verification: run project checks
- Documentation impact: none
EOF
./scripts/validate-maintenance-plan.py planning MAINTENANCE_PLAN.md >/dev/null
printf '%s\n' "$base" > .factory-state/maintenance-base-commit
./scripts/check-maintenance-freshness.sh --planning >/dev/null
# A malformed draft lifecycle checkpoint must not deadlock a corrected plan.
cp MAINTENANCE_PLAN.md valid-planning-plan.md
printf '\n## Tasks\n' >> MAINTENANCE_PLAN.md
git add MAINTENANCE_PLAN.md
git commit -qm draft-plan -m 'Mode: maintenance-planning'
cp valid-planning-plan.md MAINTENANCE_PLAN.md
./scripts/check-maintenance-freshness.sh --planning >/dev/null
git add MAINTENANCE_PLAN.md
git commit -qm plan -m 'Mode: maintenance-planning'
./scripts/check-maintenance-freshness.sh >/dev/null
# A prose lookalike cannot override strictly parsed front metadata.
printf '\nbase_commit: not-a-front-matter-override\n' >> MAINTENANCE_PLAN.md
./scripts/check-maintenance-freshness.sh >/dev/null
git restore -- MAINTENANCE_PLAN.md
printf '\ndirty contract\n' >> docs/SPEC.md
set +e
./scripts/check-maintenance-freshness.sh >/dev/null 2>&1
dirty_spec_rc=$?
set -e
[[ $dirty_spec_rc -eq 1 ]]
git restore -- docs/SPEC.md
./scripts/bug-ledger.py set-status BUG-0001 triaged
./scripts/bug-ledger.py set-status BUG-0001 planned
./scripts/bug-ledger.py set-status BUG-0001 in_progress
./scripts/bug-ledger.py close BUG-0001 --resolution fixed --verification tested --closed 2026-08-07
set +e
./scripts/check-maintenance-freshness.sh >/dev/null 2>&1
premature_rc=$?
set -e
[[ $premature_rc -eq 1 ]]
python3 - <<'PY'
p='MAINTENANCE_PLAN.md'; text=open(p,encoding='utf-8').read()
text=text.replace('status: active','status: complete').replace('- Status: pending','- Status: complete')
open(p,'w',encoding='utf-8').write(text)
PY
./scripts/check-maintenance-freshness.sh >/dev/null

# Strict parser rejects status and metadata bypasses.
cp MAINTENANCE_PLAN.md valid-plan.md
reject_plan() {
    cp valid-plan.md MAINTENANCE_PLAN.md
    python3 - "$1" <<'PY'
import sys
p='MAINTENANCE_PLAN.md'; text=open(p, encoding='utf-8').read(); mode=sys.argv[1]
if mode == 'missing-status': text=text.replace('- Status: complete\n', '')
elif mode == 'duplicate-status': text=text.replace('- Status: complete\n', '- Status: complete\n- Status: complete\n')
elif mode == 'duplicate-meta': text=text.replace('bug_id: BUG-0001\n', 'bug_id: BUG-0001\nbug_id: BUG-0001\n')
elif mode == 'unknown-meta': text=text.replace('status: complete\n', 'status: complete\nextra: value\n')
open(p, 'w', encoding='utf-8').write(text)
PY
    set +e; ./scripts/validate-maintenance-plan.py complete MAINTENANCE_PLAN.md >/dev/null 2>&1; rc=$?; set -e
    [[ $rc -eq 1 ]] || { echo "test: strict parser accepted $1" >&2; exit 1; }
}
for mutation in missing-status duplicate-status duplicate-meta unknown-meta; do reject_plan "$mutation"; done
cp valid-plan.md MAINTENANCE_PLAN.md

# Immutable metadata remains bound after task evidence/status edits.
python3 - <<'PY'
p='MAINTENANCE_PLAN.md'; text=open(p, encoding='utf-8').read()
text=text.replace('- Verification: run project checks', '- Verification: run project checks; evidence recorded')
open(p, 'w', encoding='utf-8').write(text)
PY
./scripts/check-maintenance-freshness.sh >/dev/null
cp MAINTENANCE_PLAN.md bound-plan.md
wrong_base=$(git rev-parse HEAD)
python3 - "$wrong_base" <<'PY'
import re, sys
p='MAINTENANCE_PLAN.md'; text=open(p, encoding='utf-8').read()
text=re.sub(r'^base_commit: .*$', 'base_commit: '+sys.argv[1], text, flags=re.M)
open(p, 'w', encoding='utf-8').write(text)
PY
set +e; ./scripts/check-maintenance-freshness.sh >/dev/null 2>&1; base_binding_rc=$?; set -e
[[ $base_binding_rc -eq 1 ]]
cp bound-plan.md MAINTENANCE_PLAN.md

# The configured verifier fails closed when its executable is absent.
printf '#!/usr/bin/env bash\nexit 0\n' > scripts/verify-boilerplate.sh
chmod +x scripts/verify-boilerplate.sh
set +e; ./scripts/final-gate.sh --maintenance > verifier.out 2>&1; verifier_rc=$?; set -e
[[ $verifier_rc -eq 1 ]]
grep -q 'configured maintenance verifier is missing or not executable' verifier.out

cmp -s "$PROJECT_ROOT/.github/ISSUE_TEMPLATE/bug_report.md" "$PROJECT_ROOT/.forgejo/ISSUE_TEMPLATE/bug_report.md"
echo "test: provider-neutral bug workflow checks passed"
