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
    mkdir -p "$dir/scripts" "$dir/.factory/artifacts" "$dir/.factory/schemas" "$dir/.factory/loop" \
        "$dir/.factory-state/runner-evidence/probe-runner" "$dir/tests/fixtures" "$dir/docs"
    cp "$VALIDATOR" "$EVIDENCE_CHECKER" "$CONTRACT_CHECKER" "$FACTS_VALIDATOR" "$dir/scripts/"
    chmod +x "$dir/scripts/"*.py
    # The validators run every trusted Git call through the committed
    # pinned-Git authority; the fixture receives the exact committed module
    # (never a weakened stub) so fake PATH/GIT_DIR/GIT_CONFIG/replace fixtures
    # cannot redirect it.
    cp "$PROJECT_ROOT/.factory/loop/gitutil.py" "$dir/.factory/loop/gitutil.py"
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
    cat > "$dir/.factory/config.toml" <<'CONFIG'
[project]
development_branch = "develop"
[campaign]
required_capabilities = ["probe-capability"]
CONFIG
    # The committed section-24 requirement registry is part of the acceptance
    # boundary: sidecar, plan matrix, and policy must each carry exactly these
    # IDs (Task 14 registry exact-set binding).
    cat > "$dir/.factory/schemas/factory-plan-v1.requirements.json" <<'REGISTRY'
{
  "schema": "factory-plan/v1/requirements",
  "requirement_ids": ["REQ-01", "REQ-02", "REQ-03"]
}
REGISTRY
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
    {"id": "REQ-01", "required_tier": "unit", "spec_sections": ["§1"], "required_capabilities": []},
    {"id": "REQ-02", "required_tier": "unit", "spec_sections": ["§2"], "required_capabilities": []},
    {"id": "REQ-03", "required_tier": "real_system", "spec_sections": ["§3"], "required_capabilities": ["probe-capability"]}
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
| REQ-03 | §3 | blocked | probe evidence | Task 1 |
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
            req['receipts'] = ['tests/fixtures/missing-receipt.json']
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
elif mode == 'proxy-elevation':
    # The policy map raises REQ-01 to installed; the sidecar claims verified
    # at unit, so the verified claim is below the required tier even though
    # tier names match the (drifted) policy -- the policy file is also
    # adjusted, isolating the tier-ordering violation.
    for req in data['requirements']:
        if req['id'] == 'REQ-01':
            req['evidence_tier'] = 'unit'
            req['required_tier'] = 'installed'
elif mode == 'capability-relax':
    # The sidecar drops the policy-mandated capability: self-declared
    # capability relaxation must fail.
    for req in data['requirements']:
        if req['id'] == 'REQ-03':
            req['required_capabilities'] = []
elif mode == 'traversal-ref':
    for req in data['requirements']:
        if req['id'] == 'REQ-01':
            req['artifacts'] = ['tests/../../escape']
elif mode == 'prefix-alias-ref':
    for req in data['requirements']:
        if req['id'] == 'REQ-01':
            req['artifacts'] = ['.factoryx/escape']
elif mode == 'absolute-ref':
    for req in data['requirements']:
        if req['id'] == 'REQ-01':
            req['artifacts'] = ['/etc/passwd']
elif mode == 'stale-ref-planning':
    # A ref that exists only in the working tree is stale in planning mode
    # too: evidence refs must be Git blobs at the evidence commit.
    for req in data['requirements']:
        if req['id'] == 'REQ-01':
            req['artifacts'] = ['tests/fixtures/working-tree-only.json']
elif mode == 'missing-row-refs':
    for req in data['requirements']:
        if req['id'] == 'REQ-02':
            req['classification'] = 'missing'
            req['fact_refs'] = []
            req['artifacts'] = ['tests/probe.c']
elif mode == 'human-planning':
    for req in data['requirements']:
        if req['id'] == 'REQ-01':
            req['evidence_tier'] = 'human'
            req['required_tier'] = 'human'
open(path, 'w').write(json.dumps(data))
PY
    sed -i -e 's/| REQ-02 | §2 | partial |/| REQ-02 | §2 | verified |/' \
           -e 's/| REQ-03 | §3 | blocked |/| REQ-03 | §3 | verified |/' \
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
if (cd "$tmp/blessed" && ./scripts/check-capability-evidence.py >/dev/null 2>&1); then
    echo "test: legacy unnamespaced capability fixture was accepted" >&2
    exit 1
fi

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
sed -i 's/| REQ-02 | §2 | partial |/| REQ-02 | §2 | not_applicable |/' \
    "$tmp/na-misuse/.factory/artifacts/implementation-plan.md"
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
# A spec-scoped not_applicable row stays representable in planning but fails
# implementation completion (blocked/not_applicable fail complete).
set +e
(cd "$tmp/na-misuse" && ./scripts/validate-conformance.py complete .factory/artifacts/conformance.json >/dev/null 2>&1)
na_complete_rc=$?
set -e
[[ $na_complete_rc -eq 1 ]] || { echo "FAIL: not_applicable row passed implementation completion" >&2; exit 1; }

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

# ---------------------------------------------------------------------------
# Task 14 hardening: registry exact-set, capability match, stale refs in
# planning mode, path traversal, and proxy elevation.  Each adversarial case
# below starts from the blessed (valid) repository and mutates exactly one
# authority so the intended check is the failing one.
# ---------------------------------------------------------------------------

edit_sidecar() {
    local dir=$1
    shift
    python3 - "$dir/.factory/artifacts/conformance.json" "$@"
}

expect_planning_fail() {
    local dir=$1 label=$2
    set +e
    (cd "$dir" && ./scripts/validate-conformance.py planning .factory/artifacts/conformance.json >/dev/null 2>&1)
    local rc=$?
    set -e
    [[ $rc -eq 1 ]] || { echo "test: conformance planning accepted $label (rc=$rc)" >&2; exit 1; }
}

# The §24 registry is the acceptance boundary: an extra or missing registry
# ID must fail even when sidecar/plan/policy agree with each other.
cp -a "$tmp/blessed" "$tmp/registry-extra"
python3 - "$tmp/registry-extra/.factory/schemas/factory-plan-v1.requirements.json" <<'PY'
import json, sys
path = sys.argv[1]
data = json.load(open(path))
data['requirement_ids'].append('REQ-99')
open(path, 'w').write(json.dumps(data))
PY
expect_planning_fail "$tmp/registry-extra" "a registry ID outside the sidecar/plan/policy set"

cp -a "$tmp/blessed" "$tmp/registry-missing"
python3 - "$tmp/registry-missing/.factory/schemas/factory-plan-v1.requirements.json" <<'PY'
import json, sys
path = sys.argv[1]
data = json.load(open(path))
data['requirement_ids'] = [rid for rid in data['requirement_ids'] if rid != 'REQ-02']
open(path, 'w').write(json.dumps(data))
PY
expect_planning_fail "$tmp/registry-missing" "a registry ID missing from the sidecar/plan/policy set"

# The policy map may not silently drop or add an ID either.
cp -a "$tmp/blessed" "$tmp/policy-extra"
python3 - "$tmp/policy-extra/.factory/requirement-policy.json" <<'PY'
import json, sys
path = sys.argv[1]
data = json.load(open(path))
data['requirements'].append({'id': 'REQ-04', 'required_tier': 'unit', 'spec_sections': ['§4'], 'required_capabilities': []})
open(path, 'w').write(json.dumps(data))
PY
expect_planning_fail "$tmp/policy-extra" "a policy ID outside the registry set"

# The sidecar may not relax a policy-mandated capability.
cp -a "$tmp/blessed" "$tmp/capability-relax"
python3 - "$tmp/capability-relax/.factory/artifacts/conformance.json" <<'PY'
import json, sys
path = sys.argv[1]
data = json.load(open(path))
for req in data['requirements']:
    if req['id'] == 'REQ-03':
        req['required_capabilities'] = []
open(path, 'w').write(json.dumps(data))
PY
expect_planning_fail "$tmp/capability-relax" "a self-declared capability relaxation"

# Proxy elevation: a verified claim below the policy-assigned required tier.
cp -a "$tmp/blessed" "$tmp/proxy-elevation"
python3 - "$tmp/proxy-elevation/.factory/artifacts/conformance.json" <<'PY'
import json, sys
path = sys.argv[1]
data = json.load(open(path))
for req in data['requirements']:
    if req['id'] == 'REQ-01':
        req['required_tier'] = 'installed'
open(path, 'w').write(json.dumps(data))
PY
python3 - "$tmp/proxy-elevation/.factory/requirement-policy.json" <<'PY'
import json, sys
path = sys.argv[1]
data = json.load(open(path))
for entry in data['requirements']:
    if entry['id'] == 'REQ-01':
        entry['required_tier'] = 'installed'
open(path, 'w').write(json.dumps(data))
PY
expect_planning_fail "$tmp/proxy-elevation" "verified evidence below the policy-required tier"

# Path traversal and prefix-boundary aliases are rejected in planning mode.
cp -a "$tmp/blessed" "$tmp/traversal-ref"
python3 - "$tmp/traversal-ref/.factory/artifacts/conformance.json" <<'PY'
import json, sys
path = sys.argv[1]
data = json.load(open(path))
for req in data['requirements']:
    if req['id'] == 'REQ-01':
        req['artifacts'] = ['tests/../../escape']
open(path, 'w').write(json.dumps(data))
PY
expect_planning_fail "$tmp/traversal-ref" "a traversal receipt/artifact ref"

cp -a "$tmp/blessed" "$tmp/prefix-alias-ref"
python3 - "$tmp/prefix-alias-ref/.factory/artifacts/conformance.json" <<'PY'
import json, sys
path = sys.argv[1]
data = json.load(open(path))
for req in data['requirements']:
    if req['id'] == 'REQ-01':
        req['artifacts'] = ['.factoryx/escape']
open(path, 'w').write(json.dumps(data))
PY
expect_planning_fail "$tmp/prefix-alias-ref" "a prefix-boundary alias ref"

cp -a "$tmp/blessed" "$tmp/absolute-ref"
python3 - "$tmp/absolute-ref/.factory/artifacts/conformance.json" <<'PY'
import json, sys
path = sys.argv[1]
data = json.load(open(path))
for req in data['requirements']:
    if req['id'] == 'REQ-01':
        req['artifacts'] = ['/etc/passwd']
open(path, 'w').write(json.dumps(data))
PY
expect_planning_fail "$tmp/absolute-ref" "an absolute receipt/artifact ref"

# Stale refs are rejected in planning mode too: a working-tree-only ref is
# not a Git blob at the evidence commit.
cp -a "$tmp/blessed" "$tmp/stale-planning"
python3 - "$tmp/stale-planning/.factory/artifacts/conformance.json" <<'PY'
import json, sys
path = sys.argv[1]
data = json.load(open(path))
for req in data['requirements']:
    if req['id'] == 'REQ-01':
        req['artifacts'] = ['tests/fixtures/working-tree-only.json']
open(path, 'w').write(json.dumps(data))
PY
printf '%s\n' '{}' > "$tmp/stale-planning/tests/fixtures/working-tree-only.json"
expect_planning_fail "$tmp/stale-planning" "a stale working-tree-only ref in planning mode"

# Human-tier evidence is rejected even before completion (planning mode).
cp -a "$tmp/blessed" "$tmp/human-planning"
python3 - "$tmp/human-planning/.factory/artifacts/conformance.json" <<'PY'
import json, sys
path = sys.argv[1]
data = json.load(open(path))
for req in data['requirements']:
    if req['id'] == 'REQ-01':
        req['evidence_tier'] = 'human'
open(path, 'w').write(json.dumps(data))
PY
expect_planning_fail "$tmp/human-planning" "human-tier evidence in planning mode"

# A missing row may never carry evidence refs (no proxy acceptance).
cp -a "$tmp/blessed" "$tmp/missing-refs"
python3 - "$tmp/missing-refs/.factory/artifacts/conformance.json" "$tmp/missing-refs/.factory/artifacts/blocked-facts.json" <<'PY'
import json, sys
sidecar_path, facts_path = sys.argv[1], sys.argv[2]
data = json.load(open(sidecar_path))
for req in data['requirements']:
    if req['id'] == 'REQ-02':
        req['classification'] = 'missing'
        req['fact_refs'] = []
        req['artifacts'] = ['tests/probe.c']
open(sidecar_path, 'w').write(json.dumps(data))
ledger = json.load(open(facts_path))
ledger['facts'] = [fact for fact in ledger['facts'] if fact['id'] != 'FACT-001']
open(facts_path, 'w').write(json.dumps(ledger))
PY
expect_planning_fail "$tmp/missing-refs" "a missing row carrying evidence refs"

# ---------------------------------------------------------------------------
# Task 14 hardening, part 2: a verified row can never rest on an undeclared
# or unevidenced capability (planning gate), duplicate JSON keys / duplicate
# IDs fail closed in every authority, fake PATH/GIT_DIR/GIT_CONFIG cannot
# redirect the pinned Git boundary, and refs/replace objects are rejected or
# ignored.
# ---------------------------------------------------------------------------

# A verified row requiring a capability the environment does not declare fails
# in planning mode, not only at completion.
cp -a "$tmp/blessed" "$tmp/verified-undeclared-cap"
python3 - "$tmp/verified-undeclared-cap/.factory/artifacts/conformance.json" \
        "$tmp/verified-undeclared-cap/.factory/requirement-policy.json" <<'PY'
import json, sys
sidecar_path, policy_path = sys.argv[1], sys.argv[2]
data = json.load(open(sidecar_path))
for req in data['requirements']:
    if req['id'] == 'REQ-01':
        req['required_capabilities'] = ['hardware-runner']
open(sidecar_path, 'w').write(json.dumps(data))
policy = json.load(open(policy_path))
for entry in policy['requirements']:
    if entry['id'] == 'REQ-01':
        entry['required_capabilities'] = ['hardware-runner']
open(policy_path, 'w').write(json.dumps(policy))
PY
expect_planning_fail "$tmp/verified-undeclared-cap" "a verified row requiring an undeclared capability"

# A declared capability with no accepted runner receipt is unevidenced too.
cp -a "$tmp/blessed" "$tmp/verified-unevidenced-cap"
python3 - "$tmp/verified-unevidenced-cap/.factory/artifacts/conformance.json" \
        "$tmp/verified-unevidenced-cap/.factory/requirement-policy.json" <<'PY'
import json, sys
sidecar_path, policy_path = sys.argv[1], sys.argv[2]
data = json.load(open(sidecar_path))
for req in data['requirements']:
    if req['id'] == 'REQ-01':
        req['required_capabilities'] = ['probe-capability']
open(sidecar_path, 'w').write(json.dumps(data))
policy = json.load(open(policy_path))
for entry in policy['requirements']:
    if entry['id'] == 'REQ-01':
        entry['required_capabilities'] = ['probe-capability']
open(policy_path, 'w').write(json.dumps(policy))
PY
rm -rf "$tmp/verified-unevidenced-cap/.factory-state/runner-evidence" \
       "$tmp/verified-unevidenced-cap/.factory-state/runner-evidence.json"
expect_planning_fail "$tmp/verified-unevidenced-cap" "a verified row requiring an unevidenced capability"

# Duplicate JSON object keys fail closed in every authority (the duplicate
# silently overwrites its predecessor under a plain decode).
for label in sidecar policy registry facts contracts; do
    cp -a "$tmp/blessed" "$tmp/dup-key-$label"
    case "$label" in
        sidecar)   file=".factory/artifacts/conformance.json"; schema="ralph-conformance/v1"; key="requirements";;
        policy)   file=".factory/requirement-policy.json"; schema="ralph-requirement-policy/v1"; key="requirements";;
        registry) file=".factory/schemas/factory-plan-v1.requirements.json"; schema="factory-plan/v1/requirements"; key="requirement_ids";;
        facts)    file=".factory/artifacts/blocked-facts.json"; schema="ralph-blocked-facts/v1"; key="facts";;
        contracts) file=".factory/capability-contracts.json"; schema="ralph-capability-contract/v1"; key="capabilities";;
    esac
    printf '{"schema": "%s", "%s": [], "%s": []}\n' "$schema" "$key" "$key" > "$tmp/dup-key-$label/$file"
    case "$label" in
        sidecar|policy|registry)
            set +e
            (cd "$tmp/dup-key-$label" && ./scripts/validate-conformance.py planning .factory/artifacts/conformance.json >/dev/null 2>&1)
            dup_rc=$?
            set -e
            ;;
        facts)
            set +e
            (cd "$tmp/dup-key-facts" && ./scripts/validate-blocked-facts.py planning .factory/artifacts/blocked-facts.json >/dev/null 2>&1)
            dup_rc=$?
            set -e
            ;;
        contracts)
            set +e
            (cd "$tmp/dup-key-contracts" && ./scripts/check-capability-contracts.py >/dev/null 2>&1)
            dup_rc=$?
            set -e
            ;;
    esac
    [[ $dup_rc -eq 1 ]] || { echo "FAIL: duplicate JSON key accepted in $label" >&2; exit 1; }
done

# Duplicate IDs fail closed in every authority.
cp -a "$tmp/blessed" "$tmp/dup-id-sidecar"
python3 - "$tmp/dup-id-sidecar/.factory/artifacts/conformance.json" <<'PY'
import json, sys
path = sys.argv[1]
data = json.load(open(path))
data['requirements'].append(dict(data['requirements'][0]))
open(path, 'w').write(json.dumps(data))
PY
expect_planning_fail "$tmp/dup-id-sidecar" "a duplicate sidecar requirement ID"

cp -a "$tmp/blessed" "$tmp/dup-id-policy"
python3 - "$tmp/dup-id-policy/.factory/requirement-policy.json" <<'PY'
import json, sys
path = sys.argv[1]
data = json.load(open(path))
data['requirements'].append(dict(data['requirements'][0]))
open(path, 'w').write(json.dumps(data))
PY
expect_planning_fail "$tmp/dup-id-policy" "a duplicate policy requirement ID"

cp -a "$tmp/blessed" "$tmp/dup-id-registry"
python3 - "$tmp/dup-id-registry/.factory/schemas/factory-plan-v1.requirements.json" <<'PY'
import json, sys
path = sys.argv[1]
data = json.load(open(path))
data['requirement_ids'].append('REQ-01')
open(path, 'w').write(json.dumps(data))
PY
expect_planning_fail "$tmp/dup-id-registry" "a duplicate registry requirement ID"

cp -a "$tmp/blessed" "$tmp/dup-id-facts"
python3 - "$tmp/dup-id-facts/.factory/artifacts/blocked-facts.json" <<'PY'
import json, sys
path = sys.argv[1]
data = json.load(open(path))
data['facts'].append(dict(data['facts'][0]))
open(path, 'w').write(json.dumps(data))
PY
set +e
(cd "$tmp/dup-id-facts" && ./scripts/validate-blocked-facts.py planning .factory/artifacts/blocked-facts.json >/dev/null 2>&1)
dup_facts_rc=$?
set -e
[[ $dup_facts_rc -eq 1 ]] || { echo "FAIL: duplicate fact ID accepted" >&2; exit 1; }

cp -a "$tmp/blessed" "$tmp/dup-id-contracts"
python3 - "$tmp/dup-id-contracts/.factory/capability-contracts.json" <<'PY'
import json, sys
path = sys.argv[1]
data = json.load(open(path))
data['capabilities'].append(dict(data['capabilities'][0]))
open(path, 'w').write(json.dumps(data))
PY
set +e
(cd "$tmp/dup-id-contracts" && ./scripts/check-capability-contracts.py >/dev/null 2>&1)
dup_contracts_rc=$?
set -e
[[ $dup_contracts_rc -eq 1 ]] || { echo "FAIL: duplicate contract name accepted" >&2; exit 1; }

# Fake PATH / GIT_DIR / GIT_CONFIG / object-store redirectors cannot redirect
# the trusted Git boundary: the blessed repo still validates under a hostile
# environment that would poison any unqualified `git`.
PYTHON_BIN=$(command -v python3)
FAKE_BIN="$tmp/fake-bin"
mkdir -p "$FAKE_BIN"
cat > "$FAKE_BIN/git" <<'FAKEGIT'
#!/usr/bin/env bash
printf 'fake git executed\n' >&2
exit 42
FAKEGIT
chmod +x "$FAKE_BIN/git"
set +e
(cd "$tmp/blessed" && env \
    PATH="$FAKE_BIN" \
    GIT_DIR="$tmp/not-a-repo" \
    GIT_WORK_TREE="$tmp/nowhere" \
    GIT_OBJECT_DIRECTORY="$tmp/objects" \
    GIT_ALTERNATE_OBJECT_DIRECTORIES="$tmp/alts" \
    GIT_INDEX_FILE="$tmp/index" \
    GIT_CONFIG="$tmp/evil-config" \
    GIT_CONFIG_GLOBAL="$tmp/evil-global" \
    GIT_CONFIG_SYSTEM="$tmp/evil-system" \
    GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=core.bare GIT_CONFIG_VALUE_0=true \
    "$PYTHON_BIN" ./scripts/validate-conformance.py planning .factory/artifacts/conformance.json >/dev/null 2>&1)
hardened_env_rc=$?
set -e
[[ $hardened_env_rc -eq 0 ]] || { echo "FAIL: fake PATH/GIT_DIR/GIT_CONFIG redirected a trusted Git call" >&2; exit 1; }

# Only full 40-hex commit IDs are accepted as the evidence commit; a ref name
# is refused.
cp -a "$tmp/blessed" "$tmp/ref-name-commit"
python3 - "$tmp/ref-name-commit/.factory/artifacts/conformance.json" <<'PY'
import json, sys
path = sys.argv[1]
data = json.load(open(path))
for req in data['requirements']:
    if req['id'] == 'REQ-01':
        req['evidence_commit'] = 'HEAD'
open(path, 'w').write(json.dumps(data))
PY
expect_planning_fail "$tmp/ref-name-commit" "a ref name as the evidence commit"

# A replace ref cannot substitute the evidence object: GIT_NO_REPLACE_OBJECTS=1
# makes object resolution ignore refs/replace/*.
cp -a "$tmp/blessed" "$tmp/replace-ref"
head3=$(git -C "$tmp/replace-ref" rev-parse HEAD)
rm "$tmp/replace-ref/tests/probe.c"
git -C "$tmp/replace-ref" add -A
git -C "$tmp/replace-ref" commit -qm "tamper: drop probe.c"
tamper3=$(git -C "$tmp/replace-ref" rev-parse HEAD)
git -C "$tmp/replace-ref" replace "$head3" "$tamper3"
(cd "$tmp/replace-ref" && ./scripts/validate-conformance.py planning .factory/artifacts/conformance.json >/dev/null)
set +e
git -C "$tmp/replace-ref" cat-file -e "$head3:tests/probe.c" >/dev/null 2>&1
replace_control_rc=$?
set -e
[[ $replace_control_rc -ne 0 ]] || { echo "FAIL: replace ref was honored, not disabled" >&2; exit 1; }

echo "test: conformance validator adversarial checks passed"
