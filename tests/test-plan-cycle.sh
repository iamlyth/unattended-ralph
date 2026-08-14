#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
PROJECT_ROOT=$(cd -- "$SCRIPT_DIR/.." && pwd)
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
mkdir -p "$tmp/scripts" "$tmp/docs" "$tmp/.ralph/agent" "$tmp/.factory-state" \
    "$tmp/.factory/artifacts" "$tmp/.factory/bugs"
cp "$PROJECT_ROOT/scripts/initialize-plan-cycle.py" \
   "$PROJECT_ROOT/scripts/bug-ledger.py" \
   "$PROJECT_ROOT/scripts/check-plan-freshness.sh" \
   "$PROJECT_ROOT/scripts/plan-scope-guard.sh" \
   "$PROJECT_ROOT/scripts/final-gate.sh" \
   "$PROJECT_ROOT/scripts/check-scratchpad.sh" \
   "$PROJECT_ROOT/scripts/validate-implementation-plan.py" \
   "$PROJECT_ROOT/scripts/validate-maintenance-plan.py" "$tmp/scripts/"
chmod +x "$tmp/scripts/"*
cat > "$tmp/.factory/config.toml" <<'EOF'
[project]
spec = "docs/SPEC.md"
plan = ".factory/artifacts/implementation-plan.md"
[issues]
maintenance_plan = ".factory/artifacts/maintenance-plan.md"
EOF
printf '# Current specification\n' > "$tmp/docs/SPEC.md"
cat > "$tmp/.factory/bugs/open.md" <<'EOF'
# Open Bugs

Canonical queue of defects awaiting maintenance.

Schema: `ralph-bug-ledger/v1`

```json
[
  {
    "id": "BUG-0001",
    "title": "Fresh maintenance cycle",
    "status": "triaged",
    "severity": "medium",
    "reported": "2026-08-07",
    "external": [],
    "contract_change": false,
    "reproduction": "run it",
    "expected": "works",
    "actual": "fails",
    "acceptance": "passes",
    "resolution": "",
    "verification": "",
    "closed": null
  }
]
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
printf '.factory-state/\n' > "$tmp/.gitignore"
printf 'OLD IMPLEMENTATION TASKS MUST DISAPPEAR\n' > "$tmp/.factory/artifacts/implementation-plan.md"
printf 'OLD MAINTENANCE TASKS MUST DISAPPEAR\n' > "$tmp/.factory/artifacts/maintenance-plan.md"
printf 'OLD SCRATCHPAD MUST DISAPPEAR\n' > "$tmp/.ralph/agent/scratchpad.md"
cd "$tmp"
git init -q
git config user.name test
git config user.email test@example.invalid
git add .
git commit -qm baseline
base=$(git rev-parse HEAD)

./scripts/initialize-plan-cycle.py specification --base "$base" >/dev/null
if grep -q 'OLD IMPLEMENTATION' .factory/artifacts/implementation-plan.md; then exit 1; fi
if grep -q 'OLD SCRATCHPAD' .ralph/agent/scratchpad.md; then exit 1; fi
grep -q "base_commit: $base" .factory/artifacts/implementation-plan.md
grep -q 'Git history is their archive' .factory/artifacts/implementation-plan.md
git show HEAD:.factory/artifacts/implementation-plan.md | grep -q 'OLD IMPLEMENTATION TASKS'

spec_commit=$(git log -1 --format=%H -- docs/SPEC.md)
spec_blob=$(git rev-parse HEAD:docs/SPEC.md)
cat > .factory/artifacts/implementation-plan.md <<EOF
---
spec_path: docs/SPEC.md
spec_commit: $spec_commit
spec_blob: $spec_blob
base_commit: $base
status: active
---
# Implementation Plan
## Specification conformance matrix
| Requirement | Spec section | Classification | Evidence | Task |
|---|---|---|---|---|
| REQ-1 | §1 | missing | no implementation | Task 1 |
## Interaction acceptance inventory
| Operation | Input paths | Semantic outcome | Production dispatch | Evidence |
|---|---|---|---|---|
| Example | normal event | state changes | application event loop | integration test |
## Task 1: Implement current gap
- Status: pending
- Dependencies: none
- Scope: bounded work
- Acceptance criteria: behavior works
- Verification: run tests
- Documentation impact: none
## Task 2: Final documentation and specification audit
- Status: pending
- Dependencies: Task 1
- Scope: canonical definition of done, conformance, and interaction audit
- Acceptance criteria: conformance verified; interaction complete; open bugs resolved; independent review passes; clean tree
- Verification: run final checks
- Documentation impact: README
EOF
printf '%s\n' "$base" > .factory-state/planning-base-commit
FACTORY_PLANNING_BASE_COMMIT=$base ./scripts/final-gate.sh --planning >/dev/null
cp .factory/artifacts/implementation-plan.md "$tmp/valid-plan.md"
sed -i '0,/- Status: pending/s//- Status: complete/' .factory/artifacts/implementation-plan.md
set +e
FACTORY_PLANNING_BASE_COMMIT=$base ./scripts/final-gate.sh --planning >/dev/null 2>&1
non_pending_rc=$?
set -e
[[ $non_pending_rc -eq 1 ]]

cp "$tmp/valid-plan.md" .factory/artifacts/implementation-plan.md
sed -i 's/status: active/status: complete/; s/- Status: pending/- Status: complete/g; s/| missing |/| verified |/' .factory/artifacts/implementation-plan.md
./scripts/validate-implementation-plan.py complete .factory/artifacts/implementation-plan.md >/dev/null
set +e
./scripts/final-gate.sh --implementation >/dev/null 2>&1
open_bug_rc=$?
set -e
[[ $open_bug_rc -eq 1 ]]

git restore -- .factory/artifacts/implementation-plan.md .ralph/agent/scratchpad.md

./scripts/initialize-plan-cycle.py maintenance --base "$base" --bug-id BUG-0001 >/dev/null
if grep -q 'OLD MAINTENANCE' .factory/artifacts/maintenance-plan.md; then exit 1; fi
if grep -q 'OLD SCRATCHPAD' .ralph/agent/scratchpad.md; then exit 1; fi
grep -q 'bug_id: BUG-0001' .factory/artifacts/maintenance-plan.md
git show HEAD:.factory/artifacts/maintenance-plan.md | grep -q 'OLD MAINTENANCE TASKS'

fingerprint=$(./scripts/bug-ledger.py fingerprint BUG-0001)
cat > .factory/artifacts/maintenance-plan.md <<EOF
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
- Scope: verify
- Acceptance criteria: fixed
- Verification:
  run checks
- Documentation impact: none
EOF
./scripts/validate-maintenance-plan.py planning .factory/artifacts/maintenance-plan.md >/dev/null
sed -i 's/- Status: pending/- Status: complete/' .factory/artifacts/maintenance-plan.md
set +e
./scripts/validate-maintenance-plan.py planning .factory/artifacts/maintenance-plan.md >/dev/null 2>&1
maintenance_non_pending_rc=$?
set -e
[[ $maintenance_non_pending_rc -eq 1 ]]

echo "test: fresh planning cycles discard completed task context"
