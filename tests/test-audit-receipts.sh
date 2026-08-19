#!/usr/bin/env bash
# Adversarial machine audit receipt checks (BUG-0016): fabricated command
# prose, missing receipts, tampered digests, and BLOCKED-in-pass audits must
# all be rejected; only matching exit-0 receipts certify runtime.
set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
PROJECT_ROOT=$(cd -- "$SCRIPT_DIR/.." && pwd)
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT

RECORDER="$PROJECT_ROOT/scripts/machine-receipt.py"
CHECKER="$PROJECT_ROOT/scripts/check-audit-receipts.py"

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
}

write_report() {
    local dir=$1 result=$2 evidence=$3
    cat > "$dir/.factory/artifacts/campaign-audit.md" <<EOF
---
schema: ralph-campaign-audit/v1
round: 1
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

setup_repo "$tmp/audit"

# A coordinator-executed command records a receipt and its evidence line
# validates; a passing receipt with exit 0 certifies runtime.
(cd "$tmp/audit" && ./scripts/machine-receipt.py --tag probe -- sh -c 'printf "runtime output\n"' >receipt.line)
grep -q '^\[receipt: .factory-state/audit-receipts/probe.json\]$' "$tmp/audit/receipt.line"
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
(cd "$tmp/audit" && ./scripts/machine-receipt.py --tag pass-again -- true >/dev/null)
write_report "$tmp/audit" findings "\`cmd\` FAIL [receipt: .factory-state/audit-receipts/pass-again.json]"
must_fail "FAIL claim with an exit-0 receipt" \
    "cd '$tmp/audit' && ./scripts/check-audit-receipts.py"

# A tampered receipt (digest mismatch) is rejected.
(cd "$tmp/audit" && ./scripts/machine-receipt.py --tag tampered -- true >/dev/null)
printf 'intruder\n' >> "$tmp/audit/.factory-state/audit-receipts/tampered.stdout"
write_report "$tmp/audit" pass "\`cmd\` PASS [receipt: .factory-state/audit-receipts/tampered.json]"
must_fail "tampered receipt transcript" \
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
