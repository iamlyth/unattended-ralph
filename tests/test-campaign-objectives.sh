#!/usr/bin/env bash
# Adversarial campaign-objective validation: every audit round is bound to one
# product-neutral falsification objective with its own receipt categories;
# replaying one generic receipt suite cannot satisfy all rounds, and BLOCKED
# evidence without machine receipts satisfies nothing.
set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
PROJECT_ROOT=$(cd -- "$SCRIPT_DIR/.." && pwd)
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT

CHECKER="$PROJECT_ROOT/scripts/check-campaign-objectives.py"

setup_repo() {
    local dir=$1
    mkdir -p "$dir/scripts" "$dir/.factory/artifacts" "$dir/.factory-state/audit-receipts" \
        "$dir/.factory-state/runner-evidence/verify-runner" \
        "$dir/.factory-state/runner-receipt/verify-runner"
    cp "$CHECKER" "$dir/scripts/"
    chmod +x "$dir/scripts/check-campaign-objectives.py"
    cat > "$dir/.factory/campaign-objectives.json" <<'OBJECTIVES'
{
  "schema": "ralph-campaign-objectives/v1",
  "objectives": [
    {"key": "runner-capability", "label": "runner", "receipt_categories": ["runner-evidence", "project-verify"]},
    {"key": "visual-installed", "label": "visual", "receipt_categories": ["installed-visual", "visual-render"]},
    {"key": "real-system", "label": "real", "receipt_categories": ["real-system-service", "target-consumer"]}
  ]
}
OBJECTIVES
}

# receipt <dir> <tag> <exit_code>
receipt() {
    local dir=$1 tag=$2 exit_code=$3
    cat > "$dir/.factory-state/audit-receipts/$tag.json" <<JSON
{"schema": "ralph-audit-receipt/v1", "tag": "$tag", "argv": ["probe"], "exit_code": $exit_code}
JSON
}

write_report() {
    local dir=$1
    cat > "$dir/.factory/artifacts/campaign-audit.md" <<'REPORT'
---
result: pass
---
# Campaign Audit
## Evidence reviewed
- Specification: docs/SPEC.md §2
- Production paths: src/main.c:100
- Executable evidence: ./scripts/verify-project.sh PASS [receipt: .factory-state/audit-receipts/runner-evidence.json]
- Executable evidence: ./scripts/verify-project.sh PASS [receipt: .factory-state/audit-receipts/project-verify.json]
- Executable evidence: ./scripts/verify-project.sh FAIL [receipt: .factory-state/audit-receipts/visual-render.json]
- Executable evidence: ./scripts/verify-project.sh PASS [manifest: .factory-state/runner-receipt/verify-runner/runner-evidence]

## Findings
None.
REPORT
}

expect_rc() {
    local dir=$1 round=$2 expected=$3 label=$4
    set +e
    (cd "$dir" && ./scripts/check-campaign-objectives.py --round "$round" >/dev/null 2>&1)
    local rc=$?
    set -e
    [[ $rc -eq $expected ]] || {
        echo "test: campaign-objectives $label (expected rc=$expected, got rc=$rc)" >&2
        exit 1
    }
}

setup_repo "$tmp/blessed"
receipt "$tmp/blessed" runner-evidence 0
receipt "$tmp/blessed" project-verify 0
receipt "$tmp/blessed" visual-render 1
write_report "$tmp/blessed"

# Round 1 (runner-capability) is satisfied by the runner suite.
expect_rc "$tmp/blessed" 1 0 "round 1 runner suite"

# Round 2 requires the visual-installed categories: replaying round 1's suite
# must fail, and the FAIL receipt for visual-render cannot cover the category.
expect_rc "$tmp/blessed" 2 1 "round 2 replay of round 1 suite"

# Round 3 requires real-system categories; the generic suite cannot cover them.
expect_rc "$tmp/blessed" 3 1 "round 3 replay of round 1 suite"

# A complete real-system suite passes round 3.
receipt "$tmp/blessed" real-system-service 0
receipt "$tmp/blessed" target-consumer 0
cat > "$tmp/blessed/.factory/artifacts/campaign-audit.md" <<'REPORT'
---
result: pass
---
# Campaign Audit
## Evidence reviewed
- Specification: docs/SPEC.md §2
- Executable evidence: ./scripts/probe.sh PASS [receipt: .factory-state/audit-receipts/real-system-service.json]
- Executable evidence: ./scripts/probe.sh PASS [receipt: .factory-state/audit-receipts/target-consumer.json]

## Findings
None.
REPORT
expect_rc "$tmp/blessed" 3 0 "round 3 real-system suite"

# BLOCKED evidence lines carry no receipt and satisfy nothing.
cat > "$tmp/blessed/.factory/artifacts/campaign-audit.md" <<'REPORT'
---
result: findings
---
# Campaign Audit
## Evidence reviewed
- Specification: docs/SPEC.md §2
- Executable evidence: ./scripts/probe.sh BLOCKED no real system service available
- Executable evidence: ./scripts/probe.sh PASS [receipt: .factory-state/audit-receipts/target-consumer.json]

## Findings
- Hardware unavailable
REPORT
expect_rc "$tmp/blessed" 3 1 "BLOCKED evidence without receipts"

# A receipt with an unrelated tag does not cover a required category.
cat > "$tmp/blessed/.factory/artifacts/campaign-audit.md" <<'REPORT'
---
result: pass
---
# Campaign Audit
## Evidence reviewed
- Specification: docs/SPEC.md §2
- Executable evidence: ./scripts/probe.sh PASS [receipt: .factory-state/audit-receipts/runner-evidence.json]
- Executable evidence: ./scripts/probe.sh PASS [receipt: .factory-state/audit-receipts/runner-evidence.json]

## Findings
None.
REPORT
expect_rc "$tmp/blessed" 3 1 "unrelated receipt tags"

# A runner manifest satisfies a category through its path segments.
mkdir -p "$tmp/blessed/.factory-state/runner-manifests/target-consumer" \
    "$tmp/blessed/.factory-state/runner-manifests/real-system-service"
cat > "$tmp/blessed/.factory-state/runner-manifests/target-consumer/manifest.json" <<'JSON'
{"schema": "factory-runner-receipt/v1", "result": "pass", "exit_code": 0}
JSON
cat > "$tmp/blessed/.factory-state/runner-manifests/real-system-service/manifest.json" <<'JSON'
{"schema": "factory-runner-receipt/v1", "result": "pass", "exit_code": 0}
JSON
cat > "$tmp/blessed/.factory/artifacts/campaign-audit.md" <<'REPORT'
---
result: pass
---
# Campaign Audit
## Evidence reviewed
- Specification: .factory/environment.toml
- Executable evidence: ./scripts/probe.sh PASS [manifest: .factory-state/runner-manifests/target-consumer/manifest.json]
- Executable evidence: ./scripts/probe.sh PASS [manifest: .factory-state/runner-manifests/real-system-service/manifest.json]

## Findings
None.
REPORT
expect_rc "$tmp/blessed" 3 0 "runner manifest path segment coverage"

# Without an objective map the check fails.
cp -a "$tmp/blessed" "$tmp/no-objectives"
rm -f "$tmp/no-objectives/.factory/campaign-objectives.json"
expect_rc "$tmp/no-objectives" 1 1 "missing objectives map"

# A round is required: the environment variable path also fails when absent.
set +e
(cd "$tmp/blessed" && env -u FACTORY_CAMPAIGN_AUDIT_ROUND ./scripts/check-campaign-objectives.py >/dev/null 2>&1)
env_missing_rc=$?
set -e
[[ $env_missing_rc -eq 1 ]] || { echo "test: missing audit round was accepted" >&2; exit 1; }

echo "test: campaign-objectives adversarial checks passed"
