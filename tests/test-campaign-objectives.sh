#!/usr/bin/env bash
# Adversarial campaign-objective validation: every audit round is bound to one
# product-neutral falsification objective with its own receipt categories;
# replaying one generic receipt suite cannot satisfy all rounds, BLOCKED
# evidence without machine receipts satisfies nothing, and receipt categories
# are authorized by the tracked receipt policy (allowlisted argv, evidence
# tier, coordinator round/base/nonce bindings) so arbitrary commands, tag/path
# spoofing, stale round receipts, and cross-round reuse are rejected.
set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
PROJECT_ROOT=$(cd -- "$SCRIPT_DIR/.." && pwd)
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT

CHECKER="$PROJECT_ROOT/scripts/check-campaign-objectives.py"

NONCE=$(printf 'a%.0s' {1..64})

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
    cat > "$dir/.factory/campaign-receipt-policy.json" <<'POLICY'
{
  "schema": "ralph-receipt-policy/v1",
  "categories": [
    {"name": "runner-evidence", "tier": "installed", "allow_manifest": true, "argv": [["./scripts/run-factory-runners.py"]]},
    {"name": "project-verify", "tier": "installed", "allow_manifest": false, "argv": [["./scripts/verify-boilerplate.sh"]]},
    {"name": "installed-visual", "tier": "installed", "allow_manifest": false, "argv": [["./scripts/probe-visual.sh"]]},
    {"name": "visual-render", "tier": "installed", "allow_manifest": false, "argv": [["./scripts/probe-visual.sh"]]},
    {"name": "real-system-service", "tier": "real_system", "allow_manifest": false, "argv": [["./scripts/probe-system.sh"]]},
    {"name": "target-consumer", "tier": "real_system", "allow_manifest": false, "argv": [["./scripts/probe-system.sh"]]}
  ]
}
POLICY
    git -C "$dir" init -q -b develop
    git -C "$dir" config user.name test
    git -C "$dir" config user.email test@example.invalid
    git -C "$dir" add .
    git -C "$dir" commit -qm base
}

# receipt <dir> <round> <tag> <exit_code> <argv...>
receipt() {
    local dir=$1 round=$2 tag=$3 exit_code=$4
    shift 4
    python3 - "$dir" "$round" "$tag" "$exit_code" "$@" <<'PY'
import hashlib, json, subprocess, sys
root, round_number, tag, exit_code = sys.argv[1], int(sys.argv[2]), sys.argv[3], int(sys.argv[4])
argv = sys.argv[5:]
head = subprocess.run(['git', 'rev-parse', 'HEAD'], cwd=root, capture_output=True, text=True).stdout.strip()
data = {
    "schema": "ralph-audit-receipt/v1", "tag": tag, "argv": argv,
    "argv_sha256": hashlib.sha256(json.dumps(argv, separators=(",", ":")).encode()).hexdigest(),
    "exit_code": exit_code,
    "stdout_sha256": hashlib.sha256(b"").hexdigest(),
    "stderr_sha256": hashlib.sha256(b"").hexdigest(),
    "started_at": 1, "finished_at": 2,
    "evidence_commit": head,
    "coordinator_round": round_number,
    "coordinator_nonce": "a" * 64,
}
open(f"{root}/.factory-state/audit-receipts/{tag}.json", "w").write(json.dumps(data))
PY
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
- Executable evidence: ./scripts/run-factory-runners.py PASS [receipt: .factory-state/audit-receipts/runner-evidence.json]
- Executable evidence: ./scripts/verify-boilerplate.sh PASS [receipt: .factory-state/audit-receipts/project-verify.json]
- Executable evidence: ./scripts/probe-visual.sh FAIL [receipt: .factory-state/audit-receipts/visual-render.json]

## Findings
None.
REPORT
}

expect_rc() {
    local dir=$1 round=$2 base=$3 expected=$4 label=$5
    set +e
    (cd "$dir" && ./scripts/check-campaign-objectives.py --round "$round" --base "$base" >/dev/null 2>&1)
    local rc=$?
    set -e
    [[ $rc -eq $expected ]] || {
        echo "test: campaign-objectives $label (expected rc=$expected, got rc=$rc)" >&2
        exit 1
    }
}

setup_repo "$tmp/blessed"
head=$(git -C "$tmp/blessed" rev-parse HEAD)
receipt "$tmp/blessed" 1 runner-evidence 0 ./scripts/run-factory-runners.py
receipt "$tmp/blessed" 1 project-verify 0 ./scripts/verify-boilerplate.sh
receipt "$tmp/blessed" 1 visual-render 1 ./scripts/probe-visual.sh
write_report "$tmp/blessed"

# Round 1 (runner-capability) is satisfied by the runner suite.
expect_rc "$tmp/blessed" 1 "$head" 0 "round 1 runner suite"

# Round 2 requires the visual-installed categories: replaying round 1's suite
# must fail, and the FAIL receipt for visual-render cannot cover the category.
expect_rc "$tmp/blessed" 2 "$head" 1 "round 2 replay of round 1 suite"

# Round 3 requires real-system categories; the generic suite cannot cover them.
expect_rc "$tmp/blessed" 3 "$head" 1 "round 3 replay of round 1 suite"

# A complete real-system suite (round-bound receipts) passes round 3.
receipt "$tmp/blessed" 3 real-system-service 0 ./scripts/probe-system.sh
receipt "$tmp/blessed" 3 target-consumer 0 ./scripts/probe-system.sh
cat > "$tmp/blessed/.factory/artifacts/campaign-audit.md" <<'REPORT'
---
result: pass
---
# Campaign Audit
## Evidence reviewed
- Specification: docs/SPEC.md §2
- Executable evidence: ./scripts/probe-system.sh PASS [receipt: .factory-state/audit-receipts/real-system-service.json]
- Executable evidence: ./scripts/probe-system.sh PASS [receipt: .factory-state/audit-receipts/target-consumer.json]

## Findings
None.
REPORT
expect_rc "$tmp/blessed" 3 "$head" 0 "round 3 real-system suite"

# BLOCKED evidence lines carry no receipt and satisfy nothing.
cat > "$tmp/blessed/.factory/artifacts/campaign-audit.md" <<'REPORT'
---
result: findings
---
# Campaign Audit
## Evidence reviewed
- Specification: docs/SPEC.md §2
- Executable evidence: ./scripts/probe.sh BLOCKED no real system service available
- Executable evidence: ./scripts/probe-system.sh PASS [receipt: .factory-state/audit-receipts/target-consumer.json]

## Findings
- Hardware unavailable
REPORT
expect_rc "$tmp/blessed" 3 "$head" 1 "BLOCKED evidence without receipts"

# A receipt with an unrelated tag does not cover a required category.
cat > "$tmp/blessed/.factory/artifacts/campaign-audit.md" <<'REPORT'
---
result: pass
---
# Campaign Audit
## Evidence reviewed
- Executable evidence: ./scripts/run-factory-runners.py PASS [receipt: .factory-state/audit-receipts/runner-evidence.json]
- Executable evidence: ./scripts/run-factory-runners.py PASS [receipt: .factory-state/audit-receipts/runner-evidence.json]

## Findings
None.
REPORT
expect_rc "$tmp/blessed" 3 "$head" 1 "unrelated receipt tags"

# Tag/path spoofing: a receipt tagged project-verify but running an arbitrary
# command (a bare `true`) cannot satisfy the category (argv allowlist).
cat > "$tmp/blessed/.factory-state/audit-receipts/project-verify.json" <<JSON
{"schema": "ralph-audit-receipt/v1", "tag": "project-verify", "argv": ["true"], "argv_sha256": "$(printf 'b%.0s' {1..64})", "exit_code": 0, "stdout_sha256": "$(printf 'c%.0s' {1..64})", "stderr_sha256": "$(printf 'd%.0s' {1..64})", "started_at": 1, "finished_at": 2, "evidence_commit": "$head", "coordinator_round": 1, "coordinator_nonce": "$NONCE"}
JSON
printf '\n' > "$tmp/blessed/.factory-state/audit-receipts/project-verify.stdout"
printf '\n' > "$tmp/blessed/.factory-state/audit-receipts/project-verify.stderr"
cat > "$tmp/blessed/.factory/artifacts/campaign-audit.md" <<'REPORT'
---
result: pass
---
# Campaign Audit
## Evidence reviewed
- Executable evidence: ./scripts/run-factory-runners.py PASS [receipt: .factory-state/audit-receipts/runner-evidence.json]
- Executable evidence: true PASS [receipt: .factory-state/audit-receipts/project-verify.json]

pass
REPORT
expect_rc "$tmp/blessed" 1 "$head" 1 "arbitrary command with a spoofed tag"

# A stale receipt (evidence_commit != audit base) cannot cover a category.
cat > "$tmp/blessed/.factory-state/audit-receipts/project-verify.json" <<JSON
{"schema": "ralph-audit-receipt/v1", "tag": "project-verify", "argv": ["./scripts/verify-boilerplate.sh"], "argv_sha256": "$(printf 'e%.0s' {1..64})", "exit_code": 0, "stdout_sha256": "$(printf 'c%.0s' {1..64})", "stderr_sha256": "$(printf 'd%.0s' {1..64})", "started_at": 1, "finished_at": 2, "evidence_commit": "$(printf '2%.0s' {1..40})", "coordinator_round": 1, "coordinator_nonce": "$NONCE"}
JSON
cat > "$tmp/blessed/.factory/artifacts/campaign-audit.md" <<'REPORT'
---
result: pass
---
# Campaign Audit
## Evidence reviewed
- Executable evidence: ./scripts/run-factory-runners.py PASS [receipt: .factory-state/audit-receipts/runner-evidence.json]
- Executable evidence: ./scripts/verify-boilerplate.sh PASS [receipt: .factory-state/audit-receipts/project-verify.json]

pass
REPORT
expect_rc "$tmp/blessed" 1 "$head" 1 "stale round receipt cannot cover a category"

# A runner manifest satisfies a category only when the category allows
# manifests: real-system categories here require machine receipts.
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
- Executable evidence: ./scripts/probe.sh PASS [manifest: .factory-state/runner-manifests/target-consumer/manifest.json]
- Executable evidence: ./scripts/probe.sh PASS [manifest: .factory-state/runner-manifests/real-system-service/manifest.json]

pass
REPORT
expect_rc "$tmp/blessed" 3 "$head" 1 "manifests cannot cover a non-manifest category"

# A manifest DOES satisfy a manifest-allowed category (runner-evidence).
receipt "$tmp/blessed" 1 project-verify 0 ./scripts/verify-boilerplate.sh
mkdir -p "$tmp/blessed/.factory-state/runner-manifests/runner-evidence"
cat > "$tmp/blessed/.factory-state/runner-manifests/runner-evidence/manifest.json" <<'JSON'
{"schema": "factory-runner-receipt/v1", "result": "pass", "exit_code": 0}
JSON
cat > "$tmp/blessed/.factory/artifacts/campaign-audit.md" <<'REPORT'
---
result: pass
---
# Campaign Audit
## Evidence reviewed
- Executable evidence: ./scripts/verify-boilerplate.sh PASS [receipt: .factory-state/audit-receipts/project-verify.json]
- Executable evidence: ./scripts/probe.sh PASS [manifest: .factory-state/runner-manifests/runner-evidence/manifest.json]

pass
REPORT
expect_rc "$tmp/blessed" 1 "$head" 0 "manifest coverage for a manifest-allowed category"

# Without an objective map the check fails.
cp -a "$tmp/blessed" "$tmp/no-objectives"
rm -f "$tmp/no-objectives/.factory/campaign-objectives.json"
expect_rc "$tmp/no-objectives" 1 "$head" 1 "missing objectives map"

# Without a receipt policy the check fails.
cp -a "$tmp/blessed" "$tmp/no-policy"
rm -f "$tmp/no-policy/.factory/campaign-receipt-policy.json"
expect_rc "$tmp/no-policy" 1 "$head" 1 "missing receipt policy"

# A round is required: the environment variable path also fails when absent.
set +e
(cd "$tmp/blessed" && env -u FACTORY_CAMPAIGN_AUDIT_ROUND \
    ./scripts/check-campaign-objectives.py --base "$head" >/dev/null 2>&1)
env_missing_rc=$?
set -e
[[ $env_missing_rc -eq 1 ]] || { echo "test: missing audit round was accepted" >&2; exit 1; }

echo "test: campaign-objectives adversarial checks passed"
