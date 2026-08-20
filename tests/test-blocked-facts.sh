#!/usr/bin/env bash
# Adversarial blocked-facts ledger validation: append-only IDs, open/resolved
# discipline, documentation-only resolutions, receipt/artifact refs, decision
# resolutions, and completion refusal while any fact stays open.
set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
PROJECT_ROOT=$(cd -- "$SCRIPT_DIR/.." && pwd)
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT

VALIDATOR="$PROJECT_ROOT/scripts/validate-blocked-facts.py"
CONFORMANCE="$PROJECT_ROOT/scripts/validate-conformance.py"
EVIDENCE_CHECKER="$PROJECT_ROOT/scripts/check-capability-evidence.py"
CONTRACT_CHECKER="$PROJECT_ROOT/scripts/check-capability-contracts.py"

setup_repo() {
    local dir=$1
    mkdir -p "$dir/scripts" "$dir/.factory/artifacts" "$dir/.factory-state/runner-evidence/probe-runner" \
        "$dir/tests" "$dir/docs"
    cp "$VALIDATOR" "$CONFORMANCE" "$EVIDENCE_CHECKER" "$CONTRACT_CHECKER" "$dir/scripts/"
    chmod +x "$dir/scripts/"*.py
    cat > "$dir/.factory/environment.toml" <<'EOF'
schema_version = 1
[[runners]]
name = "probe-runner"
transport = "ssh"
ssh_config_alias = "probe-runner"
working_directory = "/srv/dev-runner/workspaces/probe"
capabilities = ["probe-capability"]
verify_argv = ["./scripts/verify-project.sh"]
EOF
    printf '# Spec\n' > "$dir/docs/SPEC.md"
    printf '# Plan\n' > "$dir/.factory/artifacts/implementation-plan.md"
    printf '%s\n' ".factory-state/" > "$dir/.gitignore"
    git -C "$dir" init -q -b develop
    git -C "$dir" config user.name test
    git -C "$dir" config user.email test@example.invalid
    git -C "$dir" add .
    git -C "$dir" commit -qm base
    printf 'int probe(void){return 0;}\n' > "$dir/tests/probe.c"
    git -C "$dir" add tests/probe.c
    git -C "$dir" commit -qm fixture
    local head
    head=$(git -C "$dir" rev-parse HEAD)
    mkdir -p "$dir/.factory-state/runner-evidence/probe-runner/$head"
    printf '%s\n' '{"schema":"factory-runner-receipt/v1","result":"pass","exit_code":0}' > \
        "$dir/.factory-state/runner-evidence/probe-runner/$head/manifest.json"
    printf '%s' "$head" > "$tmp/head"
}

write_ledger() {
    local dir=$1 head=$2
    cat > "$dir/.factory/artifacts/blocked-facts.json" <<JSON
{
  "schema": "ralph-blocked-facts/v1",
  "facts": [
    {
      "id": "FACT-001",
      "title": "real-system probe evidence unavailable",
      "status": "open",
      "capabilities": ["probe-capability"],
      "requirements": ["REQ-02"],
      "blocking_evidence": "no declared real-system probe for the fixture",
      "resolution": null
    },
    {
      "id": "FACT-002",
      "title": "packaged artifact evidence unavailable",
      "status": "resolved",
      "capabilities": [],
      "requirements": ["REQ-03"],
      "blocking_evidence": "fixture baseline gap",
      "resolution": {
        "type": "artifact",
        "refs": ["tests/probe.c"],
        "evidence_commit": "$head",
        "resolved_at": "2026-01-01",
        "reason": "fixture artifact proves the packaged payload"
      }
    }
  ]
}
JSON
}

expect() {
    local dir=$1 mode=$2 expected=$3 label=$4
    set +e
    (cd "$dir" && ./scripts/validate-blocked-facts.py "$mode" .factory/artifacts/blocked-facts.json >/dev/null 2>&1)
    local rc=$?
    set -e
    [[ $rc -eq $expected ]] || {
        echo "test: blocked-facts $label (expected rc=$expected, got rc=$rc)" >&2
        exit 1
    }
}

setup_repo "$tmp/blessed"
head=$(cat "$tmp/head")
write_ledger "$tmp/blessed" "$head"

# Valid ledger passes planning; completion fails while any fact is open.
expect "$tmp/blessed" planning 0 "blessed planning"
expect "$tmp/blessed" complete 1 "completion with an open fact"

# Duplicate fact IDs are rejected.
cp -a "$tmp/blessed" "$tmp/duplicate"
python3 - "$tmp/duplicate/.factory/artifacts/blocked-facts.json" <<'PY'
import json, sys
path = sys.argv[1]
data = json.load(open(path))
data['facts'].append(dict(data['facts'][0]))
open(path, 'w').write(json.dumps(data))
PY
expect "$tmp/duplicate" planning 1 "duplicate fact IDs"

# Reordering (renumbering) facts violates append-only ordering.
cp -a "$tmp/blessed" "$tmp/reordered"
python3 - "$tmp/reordered/.factory/artifacts/blocked-facts.json" <<'PY'
import json, sys
path = sys.argv[1]
data = json.load(open(path))
first = dict(data['facts'][0], id='FACT-002')
second = dict(data['facts'][1], id='FACT-001')
data['facts'] = [first, second]
open(path, 'w').write(json.dumps(data))
PY
expect "$tmp/reordered" planning 1 "renumbered facts"

# An open fact may not carry a resolution.
cp -a "$tmp/blessed" "$tmp/open-resolved"
python3 - "$tmp/open-resolved/.factory/artifacts/blocked-facts.json" <<'PY'
import json, sys
path = sys.argv[1]
data = json.load(open(path))
for fact in data['facts']:
    if fact['id'] == 'FACT-001':
        fact['resolution'] = {'type': 'artifact', 'refs': ['tests/probe.c'],
                              'evidence_commit': fact['id'], 'resolved_at': '2026-01-01',
                              'reason': 'x'}
open(path, 'w').write(json.dumps(data))
PY
expect "$tmp/open-resolved" planning 1 "open fact with a resolution"

# Documentation alone can never resolve a normative requirement.
cp -a "$tmp/blessed" "$tmp/doc-resolution"
python3 - "$tmp/doc-resolution/.factory/artifacts/blocked-facts.json" <<'PY'
import json, sys
path = sys.argv[1]
data = json.load(open(path))
for fact in data['facts']:
    if fact['id'] == 'FACT-001':
        fact['status'] = 'resolved'
        fact['resolution'] = {'type': 'artifact', 'refs': ['docs/SPEC.md'],
                              'evidence_commit': data['facts'][1]['resolution']['evidence_commit'],
                              'resolved_at': '2026-01-01', 'reason': 'docs explain the behavior'}
open(path, 'w').write(json.dumps(data))
PY
expect "$tmp/doc-resolution" planning 1 "documentation-only resolution"

# A receipt resolution must be a real clean-pass machine receipt.
cp -a "$tmp/blessed" "$tmp/receipt-resolution"
python3 - "$tmp/receipt-resolution/.factory/artifacts/blocked-facts.json" <<'PY'
import json, sys
path = sys.argv[1]
data = json.load(open(path))
for fact in data['facts']:
    if fact['id'] == 'FACT-001':
        fact['status'] = 'resolved'
        fact['resolution'] = {'type': 'receipt',
                              'refs': ['docs/SPEC.md'],
                              'evidence_commit': data['facts'][1]['resolution']['evidence_commit'],
                              'resolved_at': '2026-01-01', 'reason': 'receipt'}
open(path, 'w').write(json.dumps(data))
PY
expect "$tmp/receipt-resolution" planning 1 "receipt resolution pointing at documentation"

# A decision resolution requires a human reviewer identity.
cp -a "$tmp/blessed" "$tmp/decision-anon"
python3 - "$tmp/decision-anon/.factory/artifacts/blocked-facts.json" <<'PY'
import json, sys
path = sys.argv[1]
data = json.load(open(path))
for fact in data['facts']:
    if fact['id'] == 'FACT-001':
        fact['status'] = 'resolved'
        fact['resolution'] = {'type': 'decision', 'refs': [], 'reason': 'human deferral',
                              'evidence_commit': data['facts'][1]['resolution']['evidence_commit'],
                              'resolved_at': '2026-01-01'}
open(path, 'w').write(json.dumps(data))
PY
expect "$tmp/decision-anon" planning 1 "decision without a human reviewer"

# A decision resolution requires a spec-permitted location.
cp -a "$tmp/blessed" "$tmp/decision-unspecified"
python3 - "$tmp/decision-unspecified/.factory/artifacts/blocked-facts.json" <<'PY'
import json, sys
path = sys.argv[1]
data = json.load(open(path))
for fact in data['facts']:
    if fact['id'] == 'FACT-001':
        fact['status'] = 'resolved'
        fact['resolution'] = {'type': 'decision', 'refs': [], 'reason': 'human deferral',
                              'evidence_commit': data['facts'][1]['resolution']['evidence_commit'],
                              'resolved_at': '2026-01-01',
                              'reviewer': 'A. Human', 'human': True}
open(path, 'w').write(json.dumps(data))
PY
expect "$tmp/decision-unspecified" planning 1 "decision without a spec-permitted location"

# An open fact requires explicit blocking evidence.
cp -a "$tmp/blessed" "$tmp/no-blocking"
python3 - "$tmp/no-blocking/.factory/artifacts/blocked-facts.json" <<'PY'
import json, sys
path = sys.argv[1]
data = json.load(open(path))
for fact in data['facts']:
    if fact['id'] == 'FACT-001':
        fact['blocking_evidence'] = '   '
open(path, 'w').write(json.dumps(data))
PY
expect "$tmp/no-blocking" planning 1 "open fact without blocking evidence"

echo "test: blocked-facts ledger adversarial checks passed"
