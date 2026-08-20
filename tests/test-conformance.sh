#!/usr/bin/env bash
# Adversarial conformance-sidecar validation (BUG-0016 + provenance hardening):
# free-text verified rows, lower-tier evidence for normative real-system/visual
# hardware rows, skipped probes, missing receipts, not_applicable misuse,
# uncommitted/stale refs (verified refs must be Git blobs at the evidence
# commit), self-declared required_tier drift from the requirement-policy map,
# and human-tier self-attestation must all fail.
set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
PROJECT_ROOT=$(cd -- "$SCRIPT_DIR/.." && pwd)
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT

VALIDATOR="$PROJECT_ROOT/scripts/validate-conformance.py"
EVIDENCE_CHECKER="$PROJECT_ROOT/scripts/check-capability-evidence.py"
CONTRACT_CHECKER="$PROJECT_ROOT/scripts/check-capability-contracts.py"
FACTS_VALIDATOR="$PROJECT_ROOT/scripts/validate-blocked-facts.py"

setup_repo() {
    local dir=$1
    mkdir -p "$dir/scripts" "$dir/.factory/artifacts" "$dir/.factory-state/runner-evidence/probe-runner" \
        "$dir/tests/fixtures" "$dir/docs"
    cp "$VALIDATOR" "$EVIDENCE_CHECKER" "$CONTRACT_CHECKER" "$FACTS_VALIDATOR" "$dir/scripts/"
    chmod +x "$dir/scripts/"*.py
    printf '#!/usr/bin/env bash\nexit 0\n' > "$dir/scripts/verify-boilerplate.sh"
    chmod +x "$dir/scripts/verify-boilerplate.sh"
    cat > "$dir/.factory/environment.toml" <<'EOF'
schema_version = 1
[[runners]]
name = "probe-runner"
transport = "ssh"
ssh_config_alias = "probe-runner"
working_directory = "/srv/dev-runner/workspaces/probe"
capabilities = ["probe-capability"]
verify_argv = ["./scripts/verify-boilerplate.sh"]
EOF
    cat > "$dir/.factory/capability-contracts.json" <<'CONTRACT'
{
  "schema": "ralph-capability-contract/v1",
  "capabilities": [
    {
      "name": "probe-capability",
      "probe_argv": ["./scripts/verify-boilerplate.sh"],
      "probe_marker": "--- probe-capability contract ---",
      "must_execute": true,
      "must_not_skip": ["Skipped", "Not Run", "skip"],
      "deny_simulated_markers": ["mock", "private service", "simulated"]
    }
  ]
}
CONTRACT
    printf '# Spec\n' > "$dir/docs/SPEC.md"
    printf '# Plan\n' > "$dir/.factory/artifacts/implementation-plan.md"
    printf '%s\n' ".factory-state/" > "$dir/.gitignore"
    git -C "$dir" init -q -b develop
    git -C "$dir" config user.name test
    git -C "$dir" config user.email test@example.invalid
    git -C "$dir" add .
    git -C "$dir" commit -qm base
    printf 'int probe(void){return 0;}\n' > "$dir/tests/probe.c"
    printf '%s\n' '{"schema":"factory-runner-receipt/v1","result":"pass","exit_code":0}' > \
        "$dir/tests/fixtures/runner-manifest.json"
    git -C "$dir" add tests/probe.c tests/fixtures/runner-manifest.json
    git -C "$dir" commit -qm fixture
    local head
    head=$(git -C "$dir" rev-parse HEAD)
    cat > "$dir/.factory/requirement-policy.json" <<'POLICY'
{
  "schema": "ralph-requirement-policy/v1",
  "requirements": [
    {"id": "REQ-01", "required_tier": "unit", "spec_sections": ["§1"]},
    {"id": "REQ-02", "required_tier": "unit", "spec_sections": ["§2"]},
    {"id": "REQ-03", "required_tier": "real_system", "spec_sections": ["§3"]}
  ]
}
POLICY
    git -C "$dir" add .factory/requirement-policy.json
    git -C "$dir" commit -qm policy
    head=$(git -C "$dir" rev-parse HEAD)
    mkdir -p "$dir/.factory-state/runner-evidence/probe-runner/$head"
    printf '%s\n' '{"schema":"factory-runner-receipt/v1","result":"pass","exit_code":0}' > \
        "$dir/.factory-state/runner-evidence/probe-runner/$head/manifest.json"
    cat > "$dir/.factory-state/runner-evidence/probe-runner/$head/stdout.log" <<'LOG'
--- probe-capability contract ---
100% tests passed, 0 tests failed out of 1
LOG
    : > "$dir/.factory-state/runner-evidence/probe-runner/$head/stderr.log"
    cat > "$dir/.factory-state/runner-evidence.json" <<AG
{
  "schema": "factory-runner-aggregate/v1",
  "commit": "$head",
  "runners": [
    {"name": "probe-runner", "manifest": ".factory-state/runner-evidence/probe-runner/$head/manifest.json", "capabilities": ["probe-capability"]}
  ]
}
AG
    echo "$head"
}

write_plan() {
    local dir=$1
    cat > "$dir/.factory/artifacts/implementation-plan.md" <<'PLAN'
---
status: active
---
# Implementation Plan
## Specification conformance matrix
| ID | Spec § | Classification | Evidence | Task |
|----|--------|--------------|----------|------|
| REQ-01 | §2 | verified | probe evidence | Task 1 |
| REQ-02 | §2 | partial | probe evidence | Task 1 |
| REQ-03 | §3 | partial | probe evidence | Task 1 |
PLAN
}

write_sidecar() {
    local dir=$1 head=$2
    cat > "$dir/.factory/artifacts/conformance.json" <<JSON
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
      "evidence_commit": "$head",
      "receipts": ["tests/fixtures/runner-manifest.json"],
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
      "evidence_commit": "$head",
      "receipts": [],
      "artifacts": [],
      "fact_refs": ["FACT-001"],
      "reason": "pending task"
    },
    {
      "id": "REQ-03",
      "spec_sections": ["§3"],
      "classification": "blocked",
      "evidence_tier": "private_integration",
      "required_tier": "real_system",
      "required_capabilities": ["probe-capability"],
      "evidence_commit": "$head",
      "receipts": [],
      "artifacts": [],
      "fact_refs": ["FACT-002"],
      "reason": "capability is not real-system evidenced"
    }
  ]
}
JSON
}

# Every blocked/partial row must reference an open fact; the fact ledger must
# stay bidirectional with the sidecar.
write_facts() {
    local dir=$1
    cat > "$dir/.factory/artifacts/blocked-facts.json" <<JSON
{
  "schema": "ralph-blocked-facts/v1",
  "facts": [
    {
      "id": "FACT-001",
      "title": "probe REQ-02 evidence unavailable",
      "status": "open",
      "capabilities": [],
      "requirements": ["REQ-02"],
      "blocking_evidence": "REQ-02 needs a real-system probe that the fixture lacks",
      "resolution": null
    },
    {
      "id": "FACT-002",
      "title": "probe REQ-03 evidence unavailable",
      "status": "open",
      "capabilities": ["probe-capability"],
      "requirements": ["REQ-03"],
      "blocking_evidence": "REQ-03 needs a real-system probe that the fixture lacks",
      "resolution": null
    }
  ]
}
JSON
}

# mutate <src> <dst> <mode>: copy the blessed repo and reclassify every
# requirement to verified with baseline refs, then apply the adversarial mode.
mutate() {
    local src=$1 dst=$2 mode=$3
    cp -a "$src" "$dst"
    local head
    head=$(git -C "$dst" rev-parse HEAD)
    python3 - "$dst/.factory/artifacts/conformance.json" "$head" "$mode" <<'PY'
import json, sys
path, head, mode = sys.argv[1], sys.argv[2], sys.argv[3]
data = json.load(open(path))
for req in data['requirements']:
    req['classification'] = 'verified'
    req['evidence_tier'] = 'unit'
    req['required_tier'] = 'unit'
    req['required_capabilities'] = []
    req['receipts'] = ["tests/fixtures/runner-manifest.json"]
    req['artifacts'] = ["tests/probe.c"]
    req['fact_refs'] = []
    req['reason'] = ''
if mode == 'free-text':
    for req in data['requirements']:
        if req['id'] == 'REQ-02':
            req['receipts'] = []
            req['artifacts'] = []
elif mode == 'private-dbus':
    for req in data['requirements']:
        if req['id'] == 'REQ-01':
            req['evidence_tier'] = 'private_integration'
            req['required_tier'] = 'real_system'
            req['required_capabilities'] = ['probe-capability']
elif mode == 'skipped-probe':
    for req in data['requirements']:
        if req['id'] == 'REQ-03':
            req['evidence_tier'] = 'real_system'
            req['required_tier'] = 'real_system'
            req['required_capabilities'] = ['probe-capability']
            req['receipts'] = []
elif mode == 'missing-receipt':
    for req in data['requirements']:
        if req['id'] == 'REQ-01':
            req['receipts'] = ['.factory-state/runner-evidence/probe-runner/missing-receipt.json']
elif mode == 'uncommitted-ref':
    # The ref exists only in the working tree, never as a blob at the commit.
    for req in data['requirements']:
        if req['id'] == 'REQ-01':
            req['receipts'] = ['tests/fixtures/working-tree-only.json']
elif mode == 'tampered-ref':
    # The ref was committed, then deleted from the working tree: the blob still
    # exists at the commit, so complete mode still accepts it; but a ref whose
    # committed blob differs from the working tree is detected by hashing.
    for req in data['requirements']:
        if req['id'] == 'REQ-01':
            req['receipts'] = ['tests/fixtures/runner-manifest.json']
elif mode == 'human-tier':
    for req in data['requirements']:
        if req['id'] == 'REQ-01':
            req['evidence_tier'] = 'human'
            req['required_tier'] = 'human'
elif mode == 'tier-drift':
    for req in data['requirements']:
        if req['id'] == 'REQ-01':
            req['required_tier'] = 'installed'
open(path, 'w').write(json.dumps(data))
PY
    sed -i -e 's/| REQ-02 | §2 | partial |/| REQ-02 | §2 | verified |/' \
           -e 's/| REQ-03 | §3 | partial |/| REQ-03 | §3 | verified |/' \
        "$dst/.factory/artifacts/implementation-plan.md"
    # All facts are resolved once every row is reclassified verified: an open
    # fact must stay referenced by a blocked/partial row, so the mutated
    # fixture carries an empty ledger.
    cat > "$dst/.factory/artifacts/blocked-facts.json" <<'LEDGER'
{
  "schema": "ralph-blocked-facts/v1",
  "facts": []
}
LEDGER
}

expect_fail() {
    local dir=$1 label=$2
    set +e
    (cd "$dir" && ./scripts/validate-conformance.py complete .factory/artifacts/conformance.json >/dev/null 2>&1)
    local rc=$?
    set -e
    [[ $rc -eq 1 ]] || { echo "test: conformance accepted $label (rc=$rc)" >&2; exit 1; }
}

# Blessed repo: planning valid, contract/receipt checkers pass.
head=$(setup_repo "$tmp/blessed")
write_plan "$tmp/blessed"
write_sidecar "$tmp/blessed" "$head"
write_facts "$tmp/blessed"
(cd "$tmp/blessed" && ./scripts/validate-conformance.py planning .factory/artifacts/conformance.json >/dev/null)
(cd "$tmp/blessed" && ./scripts/validate-conformance.py planning .factory/artifacts/conformance.json --facts .factory/artifacts/blocked-facts.json >/dev/null)
(cd "$tmp/blessed" && ./scripts/validate-blocked-facts.py planning .factory/artifacts/blocked-facts.json >/dev/null)
(cd "$tmp/blessed" && ./scripts/check-capability-contracts.py >/dev/null)
(cd "$tmp/blessed" && ./scripts/check-capability-evidence.py >/dev/null)

# A complete state with a non-verified row must fail (blocked fails
# implementation completion) and an open fact must keep completion failing.
sed -i 's/^status: active$/status: complete/' "$tmp/blessed/.factory/artifacts/implementation-plan.md"
set +e
(cd "$tmp/blessed" && ./scripts/validate-conformance.py complete .factory/artifacts/conformance.json >/dev/null 2>&1)
blocked_complete_rc=$?
set -e
[[ $blocked_complete_rc -eq 1 ]]
set +e
(cd "$tmp/blessed" && ./scripts/validate-blocked-facts.py complete .factory/artifacts/blocked-facts.json >/dev/null 2>&1)
open_fact_complete_rc=$?
set -e
[[ $open_fact_complete_rc -eq 1 ]]

# Free-text verified row: verified with no receipt/artifact refs is rejected.
mutate "$tmp/blessed" "$tmp/free-text" free-text
expect_fail "$tmp/free-text" "a free-text verified row"

# Private/session DBus for a system capability: real-system required tier
# cannot be satisfied by private-integration evidence.
mutate "$tmp/blessed" "$tmp/private-dbus" private-dbus
expect_fail "$tmp/private-dbus" "private DBus evidence for a system capability"

# Skipped probe evidence: a receipt whose probe section shows a skip must make
# the capability unevidenced.
mutate "$tmp/blessed" "$tmp/skipped-probe" skipped-probe
head2=$(git -C "$tmp/skipped-probe" rev-parse HEAD)
cat > "$tmp/skipped-probe/.factory-state/runner-evidence/probe-runner/$head2/stdout.log" <<'LOG'
--- probe-capability contract ---
100% tests passed, 0 tests failed out of 1
Test #1: kernel_probe ...................***Skipped   0.01 sec
LOG
expect_fail "$tmp/skipped-probe" "a skipped probe in the receipt"

# Missing receipt: a verified row that references a receipt that does not exist.
mutate "$tmp/blessed" "$tmp/missing-receipt" missing-receipt
expect_fail "$tmp/missing-receipt" "a missing receipt"

# Uncommitted ref: a verified row whose receipt exists only in the working tree
# (never committed at the evidence commit) must fail complete mode.
mutate "$tmp/blessed" "$tmp/uncommitted-ref" uncommitted-ref
printf '%s\n' '{"schema":"factory-runner-receipt/v1","result":"pass","exit_code":0}' > \
    "$tmp/uncommitted-ref/tests/fixtures/working-tree-only.json"
expect_fail "$tmp/uncommitted-ref" "an uncommitted receipt ref (working tree only)"

# Human-tier self-attestation: a verified row claiming human-tier evidence is
# out-of-band and rejected even though the policy map may assign human.
mutate "$tmp/blessed" "$tmp/human-tier" human-tier
python3 - "$tmp/human-tier/.factory/requirement-policy.json" <<'PY'
import json, sys
path = sys.argv[1]
data = json.load(open(path))
for entry in data['requirements']:
    if entry['id'] == 'REQ-01':
        entry['required_tier'] = 'human'
open(path, 'w').write(json.dumps(data))
PY
expect_fail "$tmp/human-tier" "human-tier self-attestation"

# required_tier drift: a sidecar self-declaring required_tier different from
# the requirement-policy map must fail.
mutate "$tmp/blessed" "$tmp/tier-drift" tier-drift
set +e
(cd "$tmp/tier-drift" && ./scripts/validate-conformance.py planning .factory/artifacts/conformance.json >/dev/null 2>&1)
tier_drift_rc=$?
set -e
[[ $tier_drift_rc -eq 1 ]] || { echo "test: required_tier drift was accepted" >&2; exit 1; }

# A requirement absent from the requirement-policy map must fail planning.
cp -a "$tmp/blessed" "$tmp/policy-missing"
python3 - "$tmp/policy-missing/.factory/artifacts/conformance.json" <<'PY'
import json, sys
path = sys.argv[1]
data = json.load(open(path))
data['requirements'].append(dict(data['requirements'][0], id='REQ-99'))
open(path, 'w').write(json.dumps(data))
PY
sed -i 's/| REQ-01 |/| REQ-99 |/' "$tmp/policy-missing/.factory/artifacts/implementation-plan.md"
set +e
(cd "$tmp/policy-missing" && ./scripts/validate-conformance.py planning .factory/artifacts/conformance.json >/dev/null 2>&1)
policy_missing_rc=$?
set -e
[[ $policy_missing_rc -eq 1 ]] || { echo "  test: requirement absent from policy map was accepted" >&2; exit 1; }

# not_applicable misuse: without a spec-scoped reason the sidecar is invalid in
# planning mode; with a scoped reason planning accepts it but completion fails.
cp -a "$tmp/blessed" "$tmp/na-misuse"
python3 - "$tmp/na-misuse/.factory/artifacts/conformance.json" \
        "$tmp/na-misuse/.factory/artifacts/blocked-facts.json" <<'PY'
import json, sys
sidecar_path, facts_path = sys.argv[1], sys.argv[2]
data = json.load(open(sidecar_path))
for req in data['requirements']:
    if req['id'] == 'REQ-02':
        req['classification'] = 'not_applicable'
        req['reason'] = 'convenience'
        req['fact_refs'] = []
open(sidecar_path, 'w').write(json.dumps(data))
ledger = json.load(open(facts_path))
ledger['facts'] = [fact for fact in ledger['facts'] if fact['id'] != 'FACT-001']
open(facts_path, 'w').write(json.dumps(ledger))
PY
set +e
(cd "$tmp/na-misuse" && ./scripts/validate-conformance.py planning .factory/artifacts/conformance.json >/dev/null 2>&1)
na_bad_rc=$?
set -e
[[ $na_bad_rc -eq 1 ]] || { echo "test: not_applicable without a spec-scoped reason was accepted" >&2; exit 1; }
python3 - "$tmp/na-misuse/.factory/artifacts/conformance.json" <<'PY'
import json, sys
path = sys.argv[1]
data = json.load(open(path))
for req in data['requirements']:
    if req['id'] == 'REQ-02':
        req['reason'] = 'excluded by §12 out-of-scope boundary'
open(path, 'w').write(json.dumps(data))
PY
(cd "$tmp/na-misuse" && ./scripts/validate-conformance.py planning .factory/artifacts/conformance.json >/dev/null)

# The sidecar and the plan matrix must agree on requirement IDs and claims.
cp -a "$tmp/blessed" "$tmp/mismatch"
python3 - "$tmp/mismatch/.factory/artifacts/conformance.json" <<'PY'
import json, sys
path = sys.argv[1]
data = json.load(open(path))
data['requirements'].append(dict(data['requirements'][0], id='REQ-98'))
open(path, 'w').write(json.dumps(data))
PY
set +e
(cd "$tmp/mismatch" && ./scripts/validate-conformance.py planning .factory/artifacts/conformance.json >/dev/null 2>&1)
mismatch_rc=$?
set -e
[[ $mismatch_rc -eq 1 ]] || { echo "test: sidecar/plan ID mismatch was accepted" >&2; exit 1; }

# A partial row that references an unknown fact must fail.
cp -a "$tmp/blessed" "$tmp/unknown-fact"
python3 - "$tmp/unknown-fact/.factory/artifacts/conformance.json" <<'PY'
import json, sys
path = sys.argv[1]
data = json.load(open(path))
for req in data['requirements']:
    if req['id'] == 'REQ-02':
        req['fact_refs'] = ['FACT-999']
open(path, 'w').write(json.dumps(data))
PY
set +e
(cd "$tmp/unknown-fact" && ./scripts/validate-conformance.py planning .factory/artifacts/conformance.json >/dev/null 2>&1)
unknown_fact_rc=$?
set -e
[[ $unknown_fact_rc -eq 1 ]] || { echo "FAIL: unknown fact reference was accepted" >&2; exit 1; }

# A partial row with no open fact reference must fail (unavailable evidence
# must be fact-bound).
cp -a "$tmp/blessed" "$tmp/no-fact-ref"
python3 - "$tmp/no-fact-ref/.factory/artifacts/conformance.json" <<'PY'
import json, sys
path = sys.argv[1]
data = json.load(open(path))
for req in data['requirements']:
    if req['id'] == 'REQ-02':
        req['fact_refs'] = []
open(path, 'w').write(json.dumps(data))
PY
set +e
(cd "$tmp/no-fact-ref" && ./scripts/validate-conformance.py planning .factory/artifacts/conformance.json >/dev/null 2>&1)
no_fact_rc=$?
set -e
[[ $no_fact_rc -eq 1 ]] || { echo "FAIL: partial row without a fact reference was accepted" >&2; exit 1; }

# A verified row may never reference a fact.
cp -a "$tmp/blessed" "$tmp/verified-fact"
python3 - "$tmp/verified-fact/.factory/artifacts/conformance.json" <<'PY'
import json, sys
path = sys.argv[1]
data = json.load(open(path))
for req in data['requirements']:
    if req['id'] == 'REQ-01':
        req['fact_refs'] = ['FACT-001']
open(path, 'w').write(json.dumps(data))
PY
set +e
(cd "$tmp/verified-fact" && ./scripts/validate-conformance.py planning .factory/artifacts/conformance.json >/dev/null 2>&1)
verified_fact_rc=$?
set -e
[[ $verified_fact_rc -eq 1 ]] || { echo "FAIL: verified row referencing a fact was accepted" >&2; exit 1; }

# A fact whose requirements drift from the sidecar must fail.
cp -a "$tmp/blessed" "$tmp/fact-drift"
python3 - "$tmp/fact-drift/.factory/artifacts/blocked-facts.json" <<'PY'
import json, sys
path = sys.argv[1]
data = json.load(open(path))
for fact in data['facts']:
    if fact['id'] == 'FACT-001':
        fact['requirements'] = ['REQ-03']
open(path, 'w').write(json.dumps(data))
PY
set +e
(cd "$tmp/fact-drift" && ./scripts/validate-conformance.py planning .factory/artifacts/conformance.json >/dev/null 2>&1)
fact_drift_rc=$?
set -e
[[ $fact_drift_rc -eq 1 ]] || { echo "FAIL: fact drift was accepted" >&2; exit 1; }

# A resolved fact may not remain referenced by a blocked/partial row.
cp -a "$tmp/blessed" "$tmp/resolved-referenced"
python3 - "$tmp/resolved-referenced/.factory/artifacts/blocked-facts.json" "$head" <<'PY'
import json, sys
path, head = sys.argv[1], sys.argv[2]
data = json.load(open(path))
for fact in data['facts']:
    if fact['id'] == 'FACT-001':
        fact['status'] = 'resolved'
        fact['resolution'] = {
            'type': 'receipt',
            'refs': ['tests/fixtures/runner-manifest.json'],
            'evidence_commit': head,
            'resolved_at': '2026-01-01',
            'reason': 'fixture resolution'
        }
open(path, 'w').write(json.dumps(data))
PY
set +e
(cd "$tmp/resolved-referenced" && ./scripts/validate-conformance.py planning .factory/artifacts/conformance.json >/dev/null 2>&1)
resolved_rc=$?
set -e
[[ $resolved_rc -eq 1 ]] || { echo "FAIL: resolved fact referenced by a partial row was accepted" >&2; exit 1; }

echo "test: conformance validator adversarial checks passed"
