#!/usr/bin/env bash
# Adversarial machine audit receipt checks (BUG-0016 + provenance hardening):
# fabricated command prose, missing receipts, tampered digests, BLOCKED-in-pass
# audits, bare (unauthorized) receipt minting, stale round receipts, and
# receipts reused across rounds must all be rejected; only coordinator-bound
# matching receipts certify runtime.
set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
PROJECT_ROOT=$(cd -- "$SCRIPT_DIR/.." && pwd)
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT

RECORDER="$PROJECT_ROOT/scripts/machine-receipt.py"
CHECKER="$PROJECT_ROOT/scripts/check-audit-receipts.py"

ROUND=2

setup_repo() {
    local dir=$1
    mkdir -p "$dir/scripts" "$dir/.factory/artifacts" "$dir/.ralph/agent" "$dir/.factory-state" "$dir/docs"
    chmod 700 "$dir/.factory-state"
    cp "$RECORDER" "$CHECKER" "$dir/scripts/"
    chmod +x "$dir/scripts/"*.py
    printf '# Spec\n' > "$dir/docs/SPEC.md"
    printf '# Plan\n' > "$dir/.factory/artifacts/implementation-plan.md"
    printf '# Audit\n' > "$dir/.factory/artifacts/campaign-audit.md"
    printf '%s\n' '.factory-state/' > "$dir/.gitignore"
    mkdir -p "$dir/.factory"
    cat > "$dir/.factory/environment.toml" <<'EOF'
schema_version = 1
EOF
    git -C "$dir" init -q -b develop
    git -C "$dir" config user.name test
    git -C "$dir" config user.email test@example.invalid
    git -C "$dir" add .
    git -C "$dir" commit -qm base
    local head
    head=$(git -C "$dir" rev-parse HEAD)
    cat > "$dir/.factory-state/audit-coordinator.json" <<JSON
{"schema": "ralph-audit-coordinator/v1", "round": $ROUND, "base_commit": "$head", "nonce": "$(printf 'a%.0s' {1..64})", "created_at": 1}
JSON
    chmod 600 "$dir/.factory-state/audit-coordinator.json"
    echo "$head"
}

write_report() {
    local dir=$1 result=$2 evidence=$3
    cat > "$dir/.factory/artifacts/campaign-audit.md" <<EOF
---
schema: ralph-campaign-audit/v1
round: $ROUND
audit_base_commit: $(git -C "$dir" rev-parse HEAD)
plan_commit: $(git -C "$dir" rev-parse HEAD)
plan_blob: $(git -C "$dir" rev-parse HEAD:.factory/artifacts/implementation-plan.md)
environment_blob: $(git -C "$dir" rev-parse HEAD:.factory/environment.toml)
runner_evidence_sha256: $(printf '0%.0s' {1..64})
result: $result
---
# Audit
## Evidence reviewed
- Specification: \`docs/SPEC.md §1\` requirements
- Production paths: \`src/app.c:1\` initialization through shutdown
- Executable evidence: $evidence
- Environment limits: \`.factory/environment.toml\` declares no external runner
EOF
}

must_fail() {
    local label=$1 cmd=$2
    set +e
    bash -c "$cmd" >/dev/null 2>&1
    local rc=$?
    set -e
    [[ $rc -eq 1 ]] || { echo "test: $label returned rc=$rc (expected 1)" >&2; exit 1; }
}

head=$(setup_repo "$tmp/audit")
ROUND=2

# A bare model receipt call (no coordinator binding) must fail: receipt minting
# outside the audit coordinator's bounded invocation is rejected.
set +e
(cd "$tmp/audit" && env -u FACTORY_CAMPAIGN_AUDIT_ROUND -u FACTORY_CAMPAIGN_AUDIT_BASE -u FACTORY_CAMPAIGN_AUDIT_NONCE \
    ./scripts/machine-receipt.py --tag bare -- true >/dev/null 2>&1)
bare_rc=$?
set -e
[[ $bare_rc -eq 1 ]] || { echo "test: bare receipt minting was allowed (rc=$bare_rc)" >&2; exit 1; }

# A coordinator-executed command records a receipt and its evidence line
# validates; a passing receipt with exit 0 certifies runtime. The receipt's
# evidence_commit equals the campaign audit base and carries the round/nonce.
(cd "$tmp/audit" && ./scripts/machine-receipt.py --tag probe \
    --audit-round "$ROUND" --evidence-commit "$head" --nonce "$(printf 'a%.0s' {1..64})" \
    -- sh -c 'printf "runtime output\n"' >receipt.line)
grep -q '^\[receipt: .factory-state/audit-receipts/probe.json\]$' "$tmp/audit/receipt.line"
python3 - "$tmp/audit/.factory-state/audit-receipts/probe.json" "$head" <<'PY'
import json, sys
path, head = sys.argv[1], sys.argv[2]
data = json.load(open(path))
assert data['evidence_commit'] == head, data['evidence_commit']
assert data['coordinator_round'] == 2
assert len(data['coordinator_nonce']) == 64
PY
write_report "$tmp/audit" pass "\`sh -c 'printf ...'\` PASS [receipt: .factory-state/audit-receipts/probe.json]"
(cd "$tmp/audit" && ./scripts/check-audit-receipts.py >/dev/null)

# Fabricated command prose without a receipt cannot certify runtime.
write_report "$tmp/audit" pass "\`./verify-project\` PASS with all checks green"
must_fail "fabricated command prose" \
    "cd '$tmp/audit' && ./scripts/check-audit-receipts.py"

# A missing receipt path is rejected.
write_report "$tmp/audit" pass "\`cmd\` PASS [receipt: .factory-state/audit-receipts/ghost.json]"
must_fail "missing receipt" \
    "cd '$tmp/audit' && ./scripts/check-audit-receipts.py"

# A FAIL claim with an exit-0 receipt is contradictory.
(cd "$tmp/audit" && ./scripts/machine-receipt.py --tag pass-again \
    --audit-round "$ROUND" --evidence-commit "$head" --nonce "$(printf 'a%.0s' {1..64})" \
    -- true >/dev/null)
write_report "$tmp/audit" findings "\`cmd\` FAIL [receipt: .factory-state/audit-receipts/pass-again.json]"
must_fail "FAIL claim with an exit-0 receipt" \
    "cd '$tmp/audit' && ./scripts/check-audit-receipts.py"

# A tampered receipt (digest mismatch) is rejected.
(cd "$tmp/audit" && ./scripts/machine-receipt.py --tag tampered \
    --audit-round "$ROUND" --evidence-commit "$head" --nonce "$(printf 'a%.0s' {1..64})" \
    -- true >/dev/null)
printf 'intruder\n' >> "$tmp/audit/.factory-state/audit-receipts/tampered.stdout"
write_report "$tmp/audit" pass "\`cmd\` PASS [receipt: .factory-state/audit-receipts/tampered.json]"
must_fail "tampered receipt transcript" \
    "cd '$tmp/audit' && ./scripts/check-audit-receipts.py"

# A stale receipt (evidence_commit != campaign audit base) is rejected.
cat > "$tmp/audit/.factory-state/audit-receipts/stale.json" <<JSON
{"schema": "ralph-audit-receipt/v1", "tag": "stale", "argv": ["true"], "argv_sha256": "$(printf 'b%.0s' {1..64})", "exit_code": 0, "stdout_sha256": "$(printf 'c%.0s' {1..64})", "stderr_sha256": "$(printf 'd%.0s' {1..64})", "started_at": 1, "finished_at": 2, "evidence_commit": "$(printf '1%.0s' {1..40})", "coordinator_round": $((ROUND - 1)), "coordinator_nonce": "$(printf 'e%.0s' {1..64})"}
JSON
printf 'stale\n' > "$tmp/audit/.factory-state/audit-receipts/stale.stdout"
printf '\n' > "$tmp/audit/.factory-state/audit-receipts/stale.stderr"
write_report "$tmp/audit" pass "\`cmd\` PASS [receipt: .factory-state/audit-receipts/stale.json]"
must_fail "stale round receipt" \
    "cd '$tmp/audit' && ./scripts/check-audit-receipts.py"

# A receipt reused across rounds (evidence_commit from a previous round) fails.
cat > "$tmp/audit/.factory-state/audit-receipts/reused.json" <<JSON
{"schema": "ralph-audit-receipt/v1", "tag": "reused", "argv": ["true"], "argv_sha256": "$(printf 'b%.0s' {1..64})", "exit_code": 0, "stdout_sha256": "$(printf 'c%.0s' {1..64})", "stderr_sha256": "$(printf 'd%.0s' {1..64})", "started_at": 1, "finished_at": 2, "evidence_commit": "$head", "coordinator_round": $ROUND, "coordinator_nonce": "$(printf 'f%.0s' {1..64})"}
JSON
printf 'reused\n' > "$tmp/audit/.factory-state/audit-receipts/reused.stdout"
printf '\n' > "$tmp/audit/.factory-state/audit-receipts/reused.stderr"
write_report "$tmp/audit" pass "\`cmd [receipt: .factory-state/audit-receipts/reused.json]\` PASS"
must_fail "receipt reused with a stale nonce" \
    "cd '$tmp/audit' && ./scripts/check-audit-receipts.py"

# A legacy receipt without coordinator binding fields is rejected.
cat > "$tmp/audit/.factory-state/audit-receipts/legacy.json" <<JSON
{"schema": "ralph-audit-receipt/v1", "tag": "legacy", "argv": ["true"], "argv_sha256": "$(printf 'b%.0s' {1..64})", "exit_code": 0, "stdout_sha256": "$(printf 'c%.0s' {1..64})", "stderr_sha256": "$(printf 'd%.0s' {1..64})", "started_at": 1, "finished_at": 2, "evidence_commit": "$head"}
JSON
printf 'legacy\n' > "$tmp/audit/.factory-state/audit-receipts/legacy.stdout"
printf '\n' > "$tmp/audit/.factory-state/audit-receipts/legacy.stderr"
write_report "$tmp/audit" pass "\`cmd [receipt: .factory-state/audit-receipts/legacy.json]\` PASS"
must_fail "legacy receipt without coordinator binding" \
    "cd '$tmp/audit' && ./scripts/check-audit-receipts.py"

# BLOCKED evidence forces result: findings; a pass report with BLOCKED fails.
write_report "$tmp/audit" pass "\`real system probe\` BLOCKED (no real system service available)"
must_fail "BLOCKED evidence in a pass audit" \
    "cd '$tmp/audit' && ./scripts/check-audit-receipts.py"

# A findings report with a BLOCKED evidence line is accepted and drives the
# next round rather than claiming completion.
write_report "$tmp/audit" findings "\`real system probe\` BLOCKED (no real system service available)"
(cd "$tmp/audit" && ./scripts/check-audit-receipts.py >/dev/null)

# A findings report citing a passing receipt is valid (runtime certified by
# the machine receipt, not prose).
write_report "$tmp/audit" findings "\`sh -c 'printf ...'\` PASS [receipt: .factory-state/audit-receipts/probe.json]"
(cd "$tmp/audit" && ./scripts/check-audit-receipts.py >/dev/null)

echo "test: machine audit receipt checks passed"
