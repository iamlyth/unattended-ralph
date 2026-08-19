#!/usr/bin/env bash
# Real maintenance-planning lifecycle chain: untrusted completion hook validates
# only, then the trusted parent performs the ledger transition, strict final
# handoff, gate attestation, and final-state attestation under the retained
# factory lock. No lock descriptor or metadata may reach the hook child, and no
# commit may follow the attestation.
set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
PROJECT_ROOT=$(cd -- "$SCRIPT_DIR/.." && pwd)
tmp=$(mktemp -d)

# The scenario test drives the maintenance-planning completion chain through
# its own trusted-parent finalization, which sets the attestation flag
# explicitly where required (BUG-0013). An ambient flag from a wrapping
# completion gate must not leak into the chain's nested validate-only gates.
unset FACTORY_FINAL_GATE_ATTEST

trap 'rm -rf "$tmp"' EXIT
mkdir -p "$tmp/scripts" "$tmp/.factory/artifacts" "$tmp/.factory/bugs" \
    "$tmp/.factory-state" "$tmp/.ralph/agent" "$tmp/docs"
chmod 700 "$tmp/.factory-state"

for name in \
    factory-lock.sh factory-lock-exec.py factory_lock.py factory_state_io.py \
    factory-state-file.py ralph-completion-gate.sh ralph-final-state.py \
    final-gate.sh maintenance-plan-scope-guard.sh check-maintenance-freshness.sh \
    check-scratchpad.sh validate-maintenance-plan.py bug-ledger.py \
    finalize-maintenance-planning.sh git-commit-hook.sh initialize-plan-cycle.py; do
    cp "$PROJECT_ROOT/scripts/$name" "$tmp/scripts/"
done
chmod +x "$tmp/scripts/"*.py "$tmp/scripts/"*.sh 2>/dev/null || true
cp "$PROJECT_ROOT/scripts/ralph-final-state.py" "$tmp/scripts/"

cat > "$tmp/.gitignore" <<'EOF'
.factory-state/
.bug-ledger.lock
__pycache__/
driver.sh
probe-gate.sh
EOF
printf '# Trial specification\n' > "$tmp/docs/SPEC.md"
cat > "$tmp/.factory/config.toml" <<'EOF'
[project]
spec = "docs/SPEC.md"

[issues]
schema = "ralph-bug-ledger/v1"
open_ledger = ".factory/bugs/open.md"
closed_ledger = ".factory/bugs/closed.md"
maintenance_plan = ".factory/artifacts/maintenance-plan.md"
final_task_title = "Maintenance verification and documentation audit"
providers = ["github", "forgejo"]
external_sync = "manual"
credentials = false
EOF
cat > "$tmp/.factory/bugs/open.md" <<'EOF'
# Open Bugs

Canonical queue of defects awaiting maintenance.

Schema: `ralph-bug-ledger/v1`

```json
[]
```
EOF
cat > "$tmp/.factory/bugs/closed.md" <<'EOF'
# Closed Bugs

Completed defects and their verification evidence.

Schema: `ralph-bug-ledger/v1`

```json
[]
```
EOF

git -C "$tmp" init -q -b develop
git -C "$tmp" config user.name test
git -C "$tmp" config user.email test@example.invalid
git -C "$tmp" add .
git -C "$tmp" commit -qm base
BASE=$(git -C "$tmp" rev-parse HEAD)

# Seed the maintenance-planning cycle exactly as the launcher does.
(cd "$tmp" && ./scripts/bug-ledger.py add \
    --title "Test maintenance defect" --severity high \
    --reproduction "Reproduce the defect." \
    --expected "Expected behavior." \
    --actual "Observed behavior." \
    --acceptance "Acceptance criteria." >/dev/null)
(cd "$tmp" && ./scripts/bug-ledger.py set-status BUG-0001 triaged)
git -C "$tmp" add .factory/bugs/open.md .factory/bugs/closed.md
if git -C "$tmp" diff --cached --quiet; then :; else
    git -C "$tmp" commit -qm "ledger: triage BUG-0001"
fi
(cd "$tmp" && ./scripts/initialize-plan-cycle.py maintenance --base "$BASE" --bug-id BUG-0001 >/dev/null)

FINGERPRINT=$(cd "$tmp" && ./scripts/bug-ledger.py fingerprint BUG-0001)
SPEC_COMMIT=$(git -C "$tmp" log -1 --format=%H -- docs/SPEC.md)
SPEC_BLOB=$(git -C "$tmp" rev-parse HEAD:docs/SPEC.md)
cat > "$tmp/.factory/artifacts/maintenance-plan.md" <<EOF
---
bug_id: BUG-0001
bug_fingerprint: $FINGERPRINT
spec_path: docs/SPEC.md
spec_commit: $SPEC_COMMIT
spec_blob: $SPEC_BLOB
base_commit: $BASE
status: active
---

# Maintenance Plan: BUG-0001 — Test maintenance defect

## Task 1: Plan the fix
- Status: pending
- Dependencies: None
- Scope: Nothing
- Acceptance criteria: Nothing
- Verification: Nothing
- Documentation impact: Nothing

## Task 2: Maintenance verification and documentation audit
- Status: pending
- Dependencies: Task 1
- Scope: Nothing
- Acceptance criteria: Nothing
- Verification: Nothing
- Documentation impact: Nothing
EOF
cat > "$tmp/.ralph/agent/scratchpad.md" <<'EOF'
# Maintenance BUG-0001 — Draft

Draft planning checkpoint.
EOF

# A draft checkpoint (as the loop's post.iteration.start hook commits one) is
# the committed planning checkpoint the finalizer's freshness check requires.
(cd "$tmp" && printf '%s' \
    "{\"loop\":{\"workspace\":\"$tmp\",\"id\":\"draft-loop\"},\"iteration\":{\"current\":1}}" \
    | ./scripts/git-commit-hook.sh --maintenance-plan >/dev/null)
[[ -n $(git -C "$tmp" log --format=%H -- .factory/artifacts/maintenance-plan.md) ]] || {
    echo "test-maintenance-planning: draft checkpoint was not committed" >&2; exit 1;
}

# The final iteration leaves only the scratchpad dirty.
cat > "$tmp/.ralph/agent/scratchpad.md" <<'EOF'
# Maintenance BUG-0001 — Final handoff

Planning is complete; the plan is coherent, fresh, and limited to BUG-0001.
EOF
[[ -n $(git -C "$tmp" status --porcelain) ]] || {
    echo "test-maintenance-planning: final scratchpad handoff was not left dirty" >&2; exit 1;
}

# The trusted parent is exec'd under the retained factory lock, exactly as the
# launcher is. It then drives the completion hook at the untrusted boundary.
cat > "$tmp/driver.sh" <<EOF
#!/usr/bin/env bash
set -euo pipefail
cd -- "$tmp"
# shellcheck source=scripts/factory-lock.sh
source "$tmp/scripts/factory-lock.sh"
factory_lock_bootstrap "$tmp" bash "$tmp/driver.sh"
export FACTORY_MAINTENANCE_BASE_COMMIT=$BASE
export FACTORY_RALPH_ATTEMPT_ID=\$(printf 'a%.0s' {1..64})
export FACTORY_RALPH_CYCLE_ID=\$(printf 'c%.0s' {1..64})
"$tmp/scripts/factory-state-file.py" write maintenance-bug-id BUG-0001 >/dev/null

# --- Hook phase: the completion hook must validate without lock authority. ---
cat > "$tmp/probe-gate.sh" <<'PROBE'
#!/usr/bin/env bash
set -euo pipefail
for key in FACTORY_LOCK_HELD FACTORY_LOCK_FD FACTORY_LOCK_ID FACTORY_LOCK_ROOT; do
    [[ -z \${!key:-} ]] || {
        echo "test-maintenance-planning: completion hook inherited lock env \$key" >&2; exit 42;
    }
done
python3 - "$tmp" <<'PY'
import os, pathlib, sys
root = pathlib.Path(sys.argv[1])
root_info = root.stat()
for item in pathlib.Path('/proc/self/fd').iterdir():
    try:
        info = os.stat(item)
    except OSError:
        continue
    if (info.st_dev, info.st_ino) == (root_info.st_dev, root_info.st_ino):
        print(f'test-maintenance-planning: completion hook inherited root descriptor {item}', file=sys.stderr)
        raise SystemExit(43)
PY
exec "$tmp/scripts/ralph-completion-gate.sh" maintenance-planning
PROBE
chmod +x "$tmp/probe-gate.sh"

hook_payload=\$(printf '{"schema_version":1,"phase":"pre","event":"loop.complete","phase_event":"pre.loop.complete","loop":{"workspace":"%s","id":"final-loop"},"iteration":{"current":"final"}}' "$tmp")

# 1. A valid completion is validated only: no attestation, no rejection marker.
printf '%s' "\$hook_payload" | factory_lock_run_untrusted "$tmp/probe-gate.sh" >/dev/null
[[ ! -e "$tmp/.factory-state/final-handoff-maintenance-planning.json" ]] || {
    echo "test-maintenance-planning: completion hook attested without the parent transition" >&2; exit 1;
}
[[ ! -e "$tmp/.factory-state/completion-rejected.json" ]] || {
    echo "test-maintenance-planning: passing completion hook left a rejection marker" >&2; exit 1;
}
[[ \$(./scripts/bug-ledger.py show BUG-0001 | python3 -c 'import json,sys; print(json.loads(sys.stdin.read().split(chr(10)+"fingerprint:",1)[0])["status"])') == triaged ]] || {
    echo "test-maintenance-planning: completion hook changed the bug ledger" >&2; exit 1;
}

# 2. An invalid completion writes only the rejection marker; still no attest.
cat > .ralph/agent/scratchpad.md <<'BAD'
# One handoff
# Second handoff
BAD
set +e
printf '%s' "\$hook_payload" | factory_lock_run_untrusted "$tmp/probe-gate.sh" >/dev/null 2>&1
hook_rc=\$?
set -e
[[ \$hook_rc -eq 1 ]] || {
    echo "test-maintenance-planning: invalid completion hook returned \$hook_rc" >&2; exit 1;
}
[[ -f "$tmp/.factory-state/completion-rejected.json" ]] || {
    echo "test-maintenance-planning: rejected completion wrote no continuation marker" >&2; exit 1;
}
[[ ! -e "$tmp/.factory-state/final-handoff-maintenance-planning.json" ]] || {
    echo "test-maintenance-planning: rejected completion was attested" >&2; exit 1;
}
grep -q '"loop_id":"final-loop"' "$tmp/.factory-state/completion-rejected.json"
# The next loop iteration clears the rejection marker before relaunching.
rm -f "$tmp/.factory-state/completion-rejected.json"
cat > .ralph/agent/scratchpad.md <<'GOOD'
# Maintenance BUG-0001 — Final handoff

Planning is complete; the plan is coherent, fresh, and limited to BUG-0001.
GOOD

# --- Parent transition under the retained lock (finish_maintenance_planning_cycle). ---
./scripts/finalize-maintenance-planning.sh >/dev/null
[[ \$(./scripts/bug-ledger.py show BUG-0001 | python3 -c 'import json,sys; print(json.loads(sys.stdin.read().split(chr(10)+"fingerprint:",1)[0])["status"])') == planned ]] || {
    echo "test-maintenance-planning: parent finalizer did not transition the bug to planned" >&2; exit 1;
}
[[ -n \$(git status --porcelain --untracked-files=normal) ]] || {
    echo "test-maintenance-planning: finalizer committed the scratchpad before the final handoff" >&2; exit 1;
}

printf '%s' "\$hook_payload" | factory_lock_run_untrusted \
    ./scripts/git-commit-hook.sh --maintenance-plan --final-handoff >/dev/null
checkpoint_commit=\$(git rev-parse HEAD)
[[ \$(git show --name-only --format= "\$checkpoint_commit" | sed '/^$/d') == .ralph/agent/scratchpad.md ]] || {
    echo "test-maintenance-planning: final handoff committed more than the scratchpad" >&2; exit 1;
}

factory_lock_run_untrusted env FACTORY_FINAL_GATE_ATTEST=1 \
    ./scripts/final-gate.sh --maintenance-planning >/dev/null
head=\$(git rev-parse HEAD)
./scripts/ralph-final-state.py attest maintenance-planning "\$head" >/dev/null
[[ \$(git rev-parse HEAD) == "\$head" ]] || {
    echo "test-maintenance-planning: a commit followed the attestation" >&2; exit 1;
}
[[ -z \$(git status --porcelain --untracked-files=normal) ]] || {
    echo "test-maintenance-planning: tree is dirty after attestation" >&2; exit 1;
}
./scripts/ralph-final-state.py verify maintenance-planning >/dev/null
grep -q "\"attested_head\":\"\$head\"" "$tmp/.factory-state/final-handoff-maintenance-planning.json"

# The untrusted boundary must not have leaked the lock to the parent either:
# the parent still owns the retained descriptor after all transitions.
factory_lock_run_untrusted true
python3 "$tmp/scripts/factory-lock-exec.py" "$tmp" --check >/dev/null
echo "test: maintenance-planning completion lifecycle chain passed"
EOF
chmod +x "$tmp/driver.sh"

python3 "$tmp/scripts/factory-lock-exec.py" "$tmp" -- bash "$tmp/driver.sh"
