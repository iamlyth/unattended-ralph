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
   "$PROJECT_ROOT/scripts/check-factory-runner-evidence.py" \
   "$PROJECT_ROOT/scripts/factory-lock.sh" \
   "$PROJECT_ROOT/scripts/factory-lock-exec.py" \
   "$PROJECT_ROOT/scripts/factory_lock.py" \
   "$PROJECT_ROOT/scripts/factory_state_io.py" \
   "$PROJECT_ROOT/scripts/campaign-verifier-binding.py" \
   "$PROJECT_ROOT/scripts/check-capability-contracts.py" \
   "$PROJECT_ROOT/scripts/check-capability-evidence.py" "$tmp/scripts/"
cp "$PROJECT_ROOT/.factory/campaign-objectives.json" "$tmp/.factory/"
cat > "$tmp/scripts/assert-no-factory-lock.py" <<'PY'
#!/usr/bin/env python3
import os
from pathlib import Path
keys=('FACTORY_LOCK_HELD','FACTORY_LOCK_FD','FACTORY_LOCK_ID','FACTORY_LOCK_ROOT')
assert all(key not in os.environ for key in keys), {key:os.environ.get(key) for key in keys}
root=Path.cwd().stat()
for item in Path('/proc/self/fd').iterdir():
    try: info=os.stat(item)
    except OSError: continue
    assert (info.st_dev, info.st_ino) != (root.st_dev, root.st_ino)
PY
for helper in check-factory-environment.py run-factory-runners.py check-factory-runner-evidence.py \
    check-capability-contracts.py check-capability-evidence.py; do
    mv "$tmp/scripts/$helper" "$tmp/scripts/$helper.real"
    cat > "$tmp/scripts/$helper" <<EOF
#!/usr/bin/env bash
set -euo pipefail
./scripts/assert-no-factory-lock.py
exec python3 "\$0.real" "\$@"
EOF
done
chmod +x "$tmp/scripts/"*
chmod 700 "$tmp/.factory-state"
cat > "$tmp/.factory/environment.toml" <<'EOF'
schema_version = 1
EOF
cat > "$tmp/.factory/capability-contracts.json" <<'EOF'
{
  "schema": "ralph-capability-contract/v1",
  "capabilities": []
}
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
__pycache__/
.ralph/*
!.ralph/agent/
.ralph/agent/*
!.ralph/agent/scratchpad.md
EOF
cat > "$tmp/scripts/fake-launch-handshake.py" <<'PY'
#!/usr/bin/env python3
import hashlib, json, os, pathlib, sys
root=pathlib.Path.cwd(); mode=sys.argv[1]
cycle=hashlib.sha256((mode+'-cycle').encode()).hexdigest()
attempt=hashlib.sha256((mode+'-attempt').encode()).hexdigest()
events=root/'.ralph/events.jsonl'
events.parent.mkdir(exist_ok=True)
offset=events.stat().st_size if events.exists() else 0
start_line=(json.dumps({
    'ts':'2026-08-19T00:00:00+00:00', 'iteration':0, 'hat':'loop',
    'topic':{'planning':'factory.plan','implementation':'factory.implement','campaign-audit':'factory.audit'}[mode],
    'triggered':'planner', 'payload':'trusted start prompt',
})+'\n').encode()
with events.open('ab') as stream:
    stream.write(start_line)
info=events.stat(); loop_id=f'fake-{mode}'
(root/'.ralph/current-events').write_text('.ralph/events.jsonl\n')
(root/'.ralph/current-loop-id').write_text(loop_id+'\n')
(root/'.factory-state/loop-mode').write_text(mode+'\n')
(root/f'.factory-state/ralph-supervision-{mode}.json').write_text(json.dumps({
    'schema':'ralph-supervision/v2','mode':mode,'cycle_id':cycle,
    'stale_recoveries':0,'completion_recoveries':0,'no_progress_recoveries':0,
})+'\n')
raw=(root/'.factory-state/ralph-campaign.json').read_bytes()
phase=os.environ['FACTORY_CAMPAIGN_PHASE']
handshake={
    'schema':'ralph-launch-handshake/v2','mode':mode,'cycle_id':cycle,
    'attempt_id':attempt,'campaign_round':os.environ['FACTORY_CAMPAIGN_ROUND'],
    'campaign_phase':phase,'campaign_state_sha256':hashlib.sha256(raw).hexdigest(),
    'loop_id':loop_id,'events':'.ralph/events.jsonl','events_dev':info.st_dev,
    'events_ino':info.st_ino,'events_offset':offset,'events_size':info.st_size,
    'events_sha256':hashlib.sha256(events.read_bytes()).hexdigest(),
    'events_delta_size':len(start_line),'events_delta_sha256':hashlib.sha256(start_line).hexdigest(),
    'start_record_size':len(start_line),'start_record_sha256':hashlib.sha256(start_line).hexdigest(),
    'start_record_nonce':attempt,
}
(root/f'.factory-state/ralph-launch-handshake-{mode}.json').write_text(json.dumps(handshake)+'\n')
PY
cat > "$tmp/scripts/branch-guard.sh" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
./scripts/assert-no-factory-lock.py
[[ $(git branch --show-current) == develop ]]
EOF
cat > "$tmp/scripts/ralph-plan.sh" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
printf 'plan %s\n' "$*" >> .factory-state/calls
./scripts/fake-launch-handshake.py planning
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
./scripts/fake-launch-handshake.py implementation
if [[ -n ${FAKE_CRASH_CAMPAIGN:-} ]]; then
    kill -KILL "$PPID"
    exit 97
fi
if [[ -n ${FAKE_FAIL_IMPL_ONCE:-} && ! -e .factory-state/failed-implementation ]]; then
    : > .factory-state/failed-implementation
    [[ -z ${FAKE_DIRTY_IMPL:-} ]] || printf '%s\n' "${FAKE_DIRTY_CONTENT:-interrupted work}" > product.txt
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
./scripts/fake-launch-handshake.py campaign-audit
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
./scripts/assert-no-factory-lock.py
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
./scripts/assert-no-factory-lock.py
printf 'verify\n' >> .factory-state/calls
EOF
cat > "$tmp/scripts/check-installed-functional-evidence.sh" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
./scripts/assert-no-factory-lock.py
exit 0
EOF
chmod +x "$tmp/scripts/"*.sh "$tmp/scripts/"*.py

git -C "$tmp" init -q -b develop
git -C "$tmp" config user.name test
git -C "$tmp" config user.email test@example.invalid
git -C "$tmp" add .
git -C "$tmp" commit -qm initial

# Finite campaigns are unattended by default.
(cd "$tmp" && ./scripts/ralph-campaign.sh --rounds 3 >/dev/null)
python3 - "$tmp" <<'PY'
import json, pathlib, sys
root=pathlib.Path(sys.argv[1])
state=json.loads((root/'.factory-state/ralph-campaign.json').read_text())
assert state['status']=='complete' and state['round']==3
assert state['tui'] is False
bases=[r['base_commit'] for r in state['rounds']]
assert len(set(bases))==3, bases
assert all(r['audit_result']=='pass' for r in state['rounds'])
calls=(root/'.factory-state/calls').read_text().splitlines()
assert [c.split()[0] for c in calls] == ['plan','implement','verify','audit']*3, calls
assert all('--no-tui' in c for c in calls if c.startswith(('plan ','implement ','audit ')))
PY

# An attended diagnostic campaign remains an explicit opt-in.
(cd "$tmp" && ./scripts/ralph-campaign.sh --rounds 1 --restart --tui >/dev/null)
python3 - "$tmp" <<'PY'
import json, pathlib, sys
root=pathlib.Path(sys.argv[1])
state=json.loads((root/'.factory-state/ralph-campaign.json').read_text())
assert state['status']=='complete' and state['tui'] is True
calls=(root/'.factory-state/calls').read_text().splitlines()
latest=calls[-4:]
assert [c.split()[0] for c in latest] == ['plan','implement','verify','audit'], latest
assert all('--no-tui' not in c for c in latest), latest
PY

# An arbitrary nonzero leaf result is invoked once and stops at the same active,
# resumable phase. The campaign uses no replaceable lock pathname.
rm -f "$tmp/.factory-state/failed-implementation"
implementation_calls_before=$(grep -c '^implement ' "$tmp/.factory-state/calls")
set +e
(cd "$tmp" && FAKE_FAIL_IMPL_ONCE=1 FAKE_DIRTY_IMPL=1 ./scripts/ralph-campaign.sh --rounds 1 --restart >/dev/null 2>&1)
fail_rc=$?
set -e
[[ $fail_rc -eq 42 ]]
[[ ! -e "$tmp/.factory-lock" ]]
[[ $(grep -c '^implement ' "$tmp/.factory-state/calls") -eq $((implementation_calls_before + 1)) ]]
python3 - "$tmp" <<'PY'
import json, pathlib, sys
root=pathlib.Path(sys.argv[1])
state=json.loads((root/'.factory-state/ralph-campaign.json').read_text())
assert state['status']=='active' and state['phase']=='implementation'
assert state['rounds'][-1]['implementation_started'] is True
assert state['rounds'][-1]['implementation_commit'] is None
assert (root/'product.txt').read_text().strip() == 'interrupted work'
PY
(cd "$tmp" && ./scripts/ralph-campaign.sh --rounds 1 --resume >/dev/null)
grep -q '^implement --resume --no-tui$' "$tmp/.factory-state/calls"
python3 - "$tmp" <<'PY'
import json, pathlib, sys
state=json.loads((pathlib.Path(sys.argv[1])/'.factory-state/ralph-campaign.json').read_text())
assert state['status']=='complete'
PY

# Attended mode also stops once, then preserves its mode on explicit resume.
rm -f "$tmp/.factory-state/failed-implementation"
set +e
(cd "$tmp" && FAKE_FAIL_IMPL_ONCE=1 FAKE_DIRTY_IMPL=1 FAKE_DIRTY_CONTENT='attended interrupted work' ./scripts/ralph-campaign.sh --rounds 1 --restart --tui >/dev/null 2>&1)
attended_fail_rc=$?
set -e
[[ $attended_fail_rc -eq 42 ]]
(cd "$tmp" && ./scripts/ralph-campaign.sh --rounds 1 --resume --tui >/dev/null)
grep -q '^implement --resume$' "$tmp/.factory-state/calls"
python3 - "$tmp" <<'PY'
import json, pathlib, sys
state=json.loads((pathlib.Path(sys.argv[1])/'.factory-state/ralph-campaign.json').read_text())
assert state['status']=='complete' and state['tui'] is True
PY

# If the campaign process dies after the receiver handshake but before the
# started transition, resume validates that exact state digest/cycle handshake
# and continues rather than resetting the phase or relaunching it as fresh.
set +e
(cd "$tmp" && FAKE_CRASH_CAMPAIGN=1 ./scripts/ralph-campaign.sh --rounds 1 --restart --no-tui >/dev/null 2>&1)
crash_rc=$?
set -e
[[ $crash_rc -ne 0 ]]
python3 - "$tmp" <<'PY'
import json, pathlib, sys
root=pathlib.Path(sys.argv[1])
state=json.loads((root/'.factory-state/ralph-campaign.json').read_text())
assert state['status']=='active' and state['phase']=='implementation'
assert state['rounds'][-1]['implementation_started'] is False
assert (root/'.factory-state/ralph-launch-handshake-implementation.json').is_file()
(root/'.factory-state/event-before-tamper').write_bytes((root/'.ralph/events.jsonl').read_bytes())
PY
# The receiver has completed its final check. Any later append or same-size
# rewrite invalidates the exact byte/size/start-record handshake and leaves
# campaign state unchanged.
state_before_event_tamper=$(sha256sum "$tmp/.factory-state/ralph-campaign.json" | cut -d' ' -f1)
printf '{"ts":"2026-08-19T00:00:01+00:00","topic":"factory.implement","payload":"late"}\n' >> "$tmp/.ralph/events.jsonl"
set +e
(cd "$tmp" && ./scripts/ralph-campaign.sh --rounds 1 --resume --no-tui >/dev/null 2>&1)
append_handshake_rc=$?
set -e
[[ $append_handshake_rc -ne 0 ]]
[[ $(sha256sum "$tmp/.factory-state/ralph-campaign.json" | cut -d' ' -f1) == "$state_before_event_tamper" ]]
cp "$tmp/.factory-state/event-before-tamper" "$tmp/.ralph/events.jsonl"
python3 - "$tmp" <<'PY'
import json, pathlib, sys
root=pathlib.Path(sys.argv[1]); path=root/'.ralph/events.jsonl'
handshake=json.loads((root/'.factory-state/ralph-launch-handshake-implementation.json').read_text())
raw=bytearray(path.read_bytes()); offset=handshake['events_offset']
raw[offset] = ord('[') if raw[offset] != ord('[') else ord('{')
path.write_bytes(raw)
PY
set +e
(cd "$tmp" && ./scripts/ralph-campaign.sh --rounds 1 --resume --no-tui >/dev/null 2>&1)
rewrite_handshake_rc=$?
set -e
[[ $rewrite_handshake_rc -ne 0 ]]
[[ $(sha256sum "$tmp/.factory-state/ralph-campaign.json" | cut -d' ' -f1) == "$state_before_event_tamper" ]]
cp "$tmp/.factory-state/event-before-tamper" "$tmp/.ralph/events.jsonl"
(cd "$tmp" && ./scripts/ralph-campaign.sh --rounds 1 --resume --no-tui >/dev/null)
grep -q '^implement --resume --no-tui$' "$tmp/.factory-state/calls"

set +e
(cd "$tmp" && FAKE_AUDIT_FINDINGS=1 ./scripts/ralph-campaign.sh --rounds 1 --restart --no-tui >/dev/null 2>&1)
findings_rc=$?
(cd "$tmp" && ./scripts/ralph-campaign.sh --rounds 0 >/dev/null 2>&1)
invalid_rc=$?
(cd "$tmp" && ./scripts/ralph-campaign.sh --rounds 1 --tui --no-tui >/dev/null 2>&1)
contradictory_a_rc=$?
(cd "$tmp" && ./scripts/ralph-campaign.sh --rounds 1 --no-tui --tui >/dev/null 2>&1)
contradictory_b_rc=$?
set -e
[[ $findings_rc -eq 1 ]]
[[ $invalid_rc -eq 2 ]]
[[ $contradictory_a_rc -eq 2 ]]
[[ $contradictory_b_rc -eq 2 ]]
python3 - "$tmp" <<'PY'
import json, pathlib, sys
state=json.loads((pathlib.Path(sys.argv[1])/'.factory-state/ralph-campaign.json').read_text())
assert state['status']=='blocked' and state['phase']=='blocked-findings'
assert state['rounds'][-1]['audit_result']=='findings'
PY

# Campaign startup never guesses about or unlinks Ralph's exclusive lock.
printf '{broken\n' > "$tmp/.ralph/loop.lock"
state_before_lock_rejection=$(sha256sum "$tmp/.factory-state/ralph-campaign.json" | cut -d' ' -f1)
set +e
(cd "$tmp" && ./scripts/ralph-campaign.sh --rounds 1 --resume >/dev/null 2>&1)
loop_lock_rc=$?
set -e
[[ $loop_lock_rc -ne 0 && -f "$tmp/.ralph/loop.lock" ]]
[[ $(sha256sum "$tmp/.factory-state/ralph-campaign.json" | cut -d' ' -f1) == "$state_before_lock_rejection" ]]
rm -f "$tmp/.ralph/loop.lock"

# Invalid verification configuration must fail closed before any phase runs.
cp "$tmp/.factory/config.toml" "$tmp/.factory/config.toml.good"
sed -i '/campaign_command/d' "$tmp/.factory/config.toml"
set +e
(cd "$tmp" && ./scripts/ralph-campaign.sh --rounds 1 --no-tui >/dev/null 2>&1)
config_rc=$?
set -e
mv "$tmp/.factory/config.toml.good" "$tmp/.factory/config.toml"
[[ $config_rc -eq 1 ]]

# A symlinked state directory is rejected before campaign state is read or
# replaced.
mv "$tmp/.factory-state" "$tmp/.factory-state.real"
mkdir "$tmp/external-state"
ln -s "$tmp/external-state" "$tmp/.factory-state"
set +e
(cd "$tmp" && ./scripts/ralph-campaign.sh --rounds 1 --restart >/dev/null 2>&1)
symlink_state_rc=$?
set -e
[[ $symlink_state_rc -ne 0 && -z $(find "$tmp/external-state" -mindepth 1 -print -quit) ]]
rm "$tmp/.factory-state"
mv "$tmp/.factory-state.real" "$tmp/.factory-state"

# Dedicated descriptor-drop and legacy migration coverage lives in
# test-factory-lock.py; campaign sequencing creates no lock-file authority.
[[ ! -e "$tmp/.factory-lock" ]]

echo "test: Ralph campaign sequencing and resume checks passed"
