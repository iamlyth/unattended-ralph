#!/usr/bin/env bash
# Adversarial context-summary validation: the durable summary handed to a fresh
# implementation context must carry only the active task, open tasks, unresolved
# facts, blocked/partial rows, and exact receipt refs — no completion prose, no
# stale claims, and no drift from the plan/sidecar/facts.
set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
PROJECT_ROOT=$(cd -- "$SCRIPT_DIR/.." && pwd)
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT

GENERATOR="$PROJECT_ROOT/scripts/ralph-context-summary.py"
CHECKER="$PROJECT_ROOT/scripts/check-context-summary.py"

setup_repo() {
    local dir=$1
    mkdir -p "$dir/scripts" "$dir/.factory/artifacts" "$dir/docs"
    cp "$GENERATOR" "$CHECKER" "$dir/scripts/"
    chmod +x "$dir/scripts/ralph-context-summary.py" "$dir/scripts/check-context-summary.py"
    cat > "$dir/.factory/artifacts/implementation-plan.md" <<'PLAN'
---
status: active
spec_path: docs/SPEC.md
spec_commit: 0123456789012345678901234567890123456789
spec_blob: 0123456789012345678901234567890123456789
base_commit: 0123456789012345678901234567890123456789
---
# Implementation Plan
## Specification conformance matrix
| ID | Spec § | Classification | Evidence | Task |
|----|--------|--------------|----------|------|
| REQ-01 | §2 | verified | unit | Task 1 |
| REQ-02 | §2 | partial | unit | Task 2 |

## Task 1: First task
- Status: complete
- Dependencies: none
- Scope: fixture scope
- Acceptance criteria: objective outcomes
- Verification: exact commands
- Documentation impact: none

## Task 2: Second task
- Status: in_progress
- Dependencies: Task 1
- Scope: fixture scope
- Acceptance criteria: objective outcomes
- Verification: exact commands
- Documentation impact: none

## Task 3: Final audit
- Status: pending
- Dependencies: Tasks 1-2
- Scope: fixture scope
- Acceptance criteria: objective outcomes
- Verification: exact commands
- Documentation impact: none
PLAN
    cat > "$dir/.factory/artifacts/conformance.json" <<'JSON'
{
  "schema": "ralph-conformance/v1",
  "requirements": [
    {
      "id": "REQ-01",
      "spec_sections": ["§1"],
      "classification": "verified",
      "evidence_tier": "unit",
      "required_tier": "unit",
      "required_capabilities": [],
      "evidence_commit": "0123456789012345678901234567890123456789",
      "receipts": ["tests/probe.c"],
      "artifacts": ["tests/probe.c"],
      "fact_refs": [],
      "reason": ""
    },
    {
      "id": "REQ-02",
      "spec_sections": ["§2"],
      "classification": "partial",
      "evidence_tier": "unit",
      "required_tier": "unit",
      "required_capabilities": [],
      "evidence_commit": "0123456789012345678901234567890123456789",
      "receipts": [".factory-state/runner-evidence/probe-runner/f8f8f8f8f8f8f8f8f8f8f8f8f8f8f8f8f8f8f8f8/manifest.json"],
      "artifacts": [],
      "fact_refs": ["FACT-001"],
      "reason": "pending task"
    }
  ]
}
JSON
    cat > "$dir/.factory/artifacts/blocked-facts.json" <<'JSON'
{
  "schema": "ralph-blocked-facts/v1",
  "facts": [
    {
      "id": "FACT-001",
      "title": "fixture evidence unavailable",
      "status": "open",
      "capabilities": [],
      "requirements": ["REQ-02"],
      "blocking_evidence": "fixture has no real probe",
      "resolution": null
    }
  ]
}
JSON
}

expect_check() {
    local dir=$1 mode=$2 expected=$3 label=$4
    set +e
    (cd "$dir" && ./scripts/check-context-summary.py ${mode:+"$mode"} >/dev/null 2>&1)
    local rc=$?
    set -e
    [[ $rc -eq $expected ]] || {
        echo "test: context-summary $label (expected rc=$expected, got rc=$rc)" >&2
        exit 1
    }
}

setup_repo "$tmp/blessed"
(cd "$tmp/blessed" && ./scripts/ralph-context-summary.py >/dev/null)

# The generated summary is contamination-free and truthful.
expect_check "$tmp/blessed" "" 0 "blessed full check"
expect_check "$tmp/blessed" "--contamination-only" 0 "blessed contamination-only"

# Reserved lifecycle tokens are contamination.
cp -a "$tmp/blessed" "$tmp/token"
printf '\nLOOP_COMPLETE\n' >> "$tmp/token/.factory/artifacts/context-summary.md"
expect_check "$tmp/token" "" 1 "reserved token contamination"
expect_check "$tmp/token" "--contamination-only" 1 "reserved token contamination (hook)"

# Completion prose is contamination, even when the task list itself is fresh.
cp -a "$tmp/blessed" "$tmp/prose"
printf '\nThe definition of done is satisfied and all requirements are verified.\n' \
    >> "$tmp/prose/.factory/artifacts/context-summary.md"
expect_check "$tmp/prose" "" 1 "completion prose contamination"

# A status-complete claim is contamination.
cp -a "$tmp/blessed" "$tmp/status"
printf '\nstatus: complete\n' >> "$tmp/status/.factory/artifacts/context-summary.md"
expect_check "$tmp/status" "" 1 "status complete contamination"

# A missing summary fails.
cp -a "$tmp/blessed" "$tmp/missing"
rm -f "$tmp/missing/.factory/artifacts/context-summary.md"
expect_check "$tmp/missing" "" 1 "missing summary"

# Stale task claims: the plan advances but the summary does not.
cp -a "$tmp/blessed" "$tmp/stale-tasks"
sed -i 's/## Task 3: Final audit/## Task 3: Final audit\n\n## Task 4: Appended remediation/' \
    "$tmp/stale-tasks/.factory/artifacts/implementation-plan.md"
printf '\n## Task 4: Appended remediation\n- Status: pending\n- Dependencies: Tasks 1-3\n' \
    >> "$tmp/stale-tasks/.factory/artifacts/context-summary.md"
expect_check "$tmp/stale-tasks" "" 1 "stale open-task set"

# Resolved-fact drift: the ledger resolves a fact but the summary still lists it.
cp -a "$tmp/blessed" "$tmp/stale-fact"
python3 - "$tmp/stale-fact/.factory/artifacts/blocked-facts.json" <<'PY'
import json, sys
path = sys.argv[1]
data = json.load(open(path))
for fact in data['facts']:
    if fact['id'] == 'FACT-001':
        fact['status'] = 'resolved'
        fact['resolution'] = {
            'type': 'artifact', 'refs': ['tests/probe.c'],
            'evidence_commit': '0' * 40, 'resolved_at': '2026-01-01', 'reason': 'fixture'
        }
open(path, 'w').write(json.dumps(data))
PY
expect_check "$tmp/stale-fact" "" 1 "resolved-fact drift"

# Wrong active task is caught.
cp -a "$tmp/blessed" "$tmp/wrong-active"
sed -i 's/^- Task 2 (in_progress): Second task$/- Task 1 (in_progress): First task/' \
    "$tmp/wrong-active/.factory/artifacts/context-summary.md"
expect_check "$tmp/wrong-active" "" 1 "wrong active task"

# The contamination-only mode stays usable when the plan legitimately advances
# during a run (the checkpoint hook must not fail on fresh progress).
cp -a "$tmp/blessed" "$tmp/stale-tasks"
expect_check "$tmp/stale-tasks" "--contamination-only" 0 "stale plan accepted by contamination-only hook"

echo "test: context-summary adversarial checks passed"
