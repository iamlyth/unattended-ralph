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
RUNNER_EVIDENCE="$PROJECT_ROOT/scripts/check-factory-runner-evidence.py"
ENV_CHECKER="$PROJECT_ROOT/scripts/check-factory-environment.py"

ROUND=2

setup_repo() {
    local dir=$1
    mkdir -p "$dir/scripts" "$dir/.factory" "$dir/.factory/artifacts" "$dir/.ralph/agent" \
        "$dir/.factory-state/runner-evidence" "$dir/docs"
    chmod 700 "$dir/.factory-state"
    cp "$RECORDER" "$CHECKER" "$RUNNER_EVIDENCE" "$ENV_CHECKER" "$dir/scripts/"
    chmod +x "$dir/scripts/"*.py
    printf '# Spec\n' > "$dir/docs/SPEC.md"
    printf '# Plan\n' > "$dir/.factory/artifacts/implementation-plan.md"
    printf '# Audit\n' > "$dir/.factory/artifacts/campaign-audit.md"
    printf '%s\n' '.factory-state/' > "$dir/.gitignore"
    cat > "$dir/.factory/environment.toml" <<'EOF'
schema_version = 1
[[runners]]
name = "fake-runner"
transport = "ssh"
ssh_config_alias = "fake-runner"
working_directory = "/srv/dev-runner/workspaces/fake-project"
capabilities = ["runner-gate"]
verify_argv = ["./scripts/verify-boilerplate.sh"]
EOF
    # Ephemeral signer for fixture runner-receipt trust (never committed).
    ssh-keygen -q -t ed25519 -N '' -f "$tmp/signer-key"
    PUBLIC_KEY=$(cut -d' ' -f1,2 "$tmp/signer-key.pub")
    python3 - "$PUBLIC_KEY" <<'PY' > "$dir/.factory/signer-trust.json"
import json, sys
print(json.dumps({
    "schema": "ralph-runner-signer-trust/v1",
    "description": "ephemeral test fixture signer",
    "require_signature": True,
    "enabled": True,
    "namespace": "factory-runner-receipt",
    "public_keys": [{"principal": "factory-signer", "public_key": sys.argv[1]}],
    "allowed_principals": ["factory-signer"],
}))
PY
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

# write_signed_evidence <dir> <head>: a fully commit-bound signed aggregate
# fixture (tree/environment blob/verifier argv digest/git archive hash all
# recomputed) so aggregate membership and signature are the variables under test.
write_signed_evidence() {
    local dir=$1 head=$2
    local public_key
    public_key=$(cut -d' ' -f1,2 "$tmp/signer-key.pub")
    mkdir -p "$dir/.factory-state/runner-evidence/fake-runner/$head"
    python3 - "$dir" "$head" "$public_key" <<'PY'
import hashlib, json, pathlib, subprocess, sys, tomllib
root, head, public_key = pathlib.Path(sys.argv[1]), sys.argv[2], sys.argv[3]

def git(*args: str) -> str:
    result = subprocess.run(["git", *args], cwd=root, text=True, capture_output=True)
    if result.returncode:
        raise SystemExit(f"git {args} failed: {result.stderr}")
    return result.stdout.strip()

tree = git("rev-parse", f"{head}^{{tree}}")
environment_blob = git("rev-parse", f"{head}:.factory/environment.toml")
environment = tomllib.loads(git("show", f"{head}:.factory/environment.toml"))
declared = environment["runners"][0]
argv_digest = hashlib.sha256(json.dumps(declared["verify_argv"], separators=(",", ":")).encode()).hexdigest()
archive = subprocess.run(
    ["git", "archive", "--format=tar", "--output", str(root / "commit-archive.tar"), head],
    cwd=root, capture_output=True,
)
if archive.returncode:
    raise SystemExit("cannot archive fixture commit")
archive_sha256 = hashlib.sha256((root / "commit-archive.tar").read_bytes()).hexdigest()
(root / "commit-archive.tar").unlink()
empty = hashlib.sha256(b"").hexdigest()
capabilities = sorted(declared["capabilities"])
key_sha256 = hashlib.sha256(public_key.encode()).hexdigest()
manifest = {
    "schema": "factory-runner-receipt/v1", "result": "pass", "runner": "fake-runner",
    "commit": head, "tree": tree, "environment_blob": environment_blob,
    "verify_argv_sha256": argv_digest, "archive_sha256": archive_sha256, "nonce": "0" * 64,
    "capabilities": capabilities, "exit_code": 0, "timed_out": False,
    "started_at": 1, "finished_at": 2, "cleanup": True,
    "stdout_sha256": empty, "stderr_sha256": empty,
    "signer_principal": "factory-signer", "signer_key_sha256": key_sha256,
    "namespace": "factory-runner-receipt", "signature_algorithm": "ssh-ed25519",
}
raw = (json.dumps(manifest, sort_keys=True, indent=2) + "\n").encode()
manifest_path = root / f".factory-state/runner-evidence/fake-runner/{head}/manifest.json"
manifest_path.write_bytes(raw)
aggregate = {
    "schema": "factory-runner-aggregate/v1",
    "commit": head,
    "tree": tree,
    "environment_blob": environment_blob,
    "runners": [
        {"name": "fake-runner", "manifest": f".factory-state/runner-evidence/fake-runner/{head}/manifest.json",
         "manifest_sha256": hashlib.sha256(raw).hexdigest(), "capabilities": capabilities,
         "signer": {"principal": "factory-signer", "key_sha256": key_sha256,
                     "algorithm": "ssh-ed25519", "signature_sha256": ""}}
    ],
}
(root / ".factory-state/runner-evidence.json").write_text(json.dumps(aggregate, sort_keys=True, indent=2) + "\n")
(root / f".factory-state/runner-evidence/fake-runner/{head}/stdout.log").write_bytes(b"")
(root / f".factory-state/runner-evidence/fake-runner/{head}/stderr.log").write_bytes(b"")
PY
    cat "$dir/.factory-state/runner-evidence/fake-runner/$head/manifest.json" \
        | ssh-keygen -Y sign -f "$tmp/signer-key" -n factory-runner-receipt \
            > "$dir/.factory-state/runner-evidence/fake-runner/$head/manifest.sig" 2>/dev/null
    python3 - "$dir/.factory-state/runner-evidence/fake-runner/$head/manifest.sig" \
        "$dir/.factory-state/runner-evidence.json" <<'PY'
import hashlib, json, pathlib, sys
sig_path, aggregate_path = pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2])
aggregate = json.loads(aggregate_path.read_text())
aggregate["runners"][0]["signer"]["signature_sha256"] = hashlib.sha256(sig_path.read_bytes()).hexdigest()
aggregate_path.write_text(json.dumps(aggregate, sort_keys=True, indent=2) + "\n")
PY
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
write_signed_evidence "$tmp/audit" "$head"

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
write_report "$tmp/audit" pass "\`sh -c 'printf \"runtime output\\n\"'\` PASS [receipt: .factory-state/audit-receipts/probe.json]"
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
write_report "$tmp/audit" findings "\`true\` FAIL [receipt: .factory-state/audit-receipts/pass-again.json]"
must_fail "FAIL claim with an exit-0 receipt" \
    "cd '$tmp/audit' && ./scripts/check-audit-receipts.py"

# A tampered receipt (digest mismatch) is rejected.
(cd "$tmp/audit" && ./scripts/machine-receipt.py --tag tampered \
    --audit-round "$ROUND" --evidence-commit "$head" --nonce "$(printf 'a%.0s' {1..64})" \
    -- true >/dev/null)
printf 'intruder\n' >> "$tmp/audit/.factory-state/audit-receipts/tampered.stdout"
write_report "$tmp/audit" pass "\`true\` PASS [receipt: .factory-state/audit-receipts/tampered.json]"
must_fail "tampered receipt transcript" \
    "cd '$tmp/audit' && ./scripts/check-audit-receipts.py"

# A stale receipt (evidence_commit != campaign audit base) is rejected.
cat > "$tmp/audit/.factory-state/audit-receipts/stale.json" <<JSON
{"schema": "ralph-audit-receipt/v1", "tag": "stale", "argv": ["true"], "argv_sha256": "$(printf 'b%.0s' {1..64})", "exit_code": 0, "stdout_sha256": "$(printf 'c%.0s' {1..64})", "stderr_sha256": "$(printf 'd%.0s' {1..64})", "started_at": 1, "finished_at": 2, "evidence_commit": "$(printf '1%.0s' {1..40})", "coordinator_round": $((ROUND - 1)), "coordinator_nonce": "$(printf 'e%.0s' {1..64})"}
JSON
printf 'stale\n' > "$tmp/audit/.factory-state/audit-receipts/stale.stdout"
printf '\n' > "$tmp/audit/.factory-state/audit-receipts/stale.stderr"
write_report "$tmp/audit" pass "\`true\` PASS [receipt: .factory-state/audit-receipts/stale.json]"
must_fail "stale round receipt" \
    "cd '$tmp/audit' && ./scripts/check-audit-receipts.py"

# A receipt reused across rounds (evidence_commit from a previous round) fails.
cat > "$tmp/audit/.factory-state/audit-receipts/reused.json" <<JSON
{"schema": "ralph-audit-receipt/v1", "tag": "reused", "argv": ["true"], "argv_sha256": "$(printf 'b%.0s' {1..64})", "exit_code": 0, "stdout_sha256": "$(printf 'c%.0s' {1..64})", "stderr_sha256": "$(printf 'd%.0s' {1..64})", "started_at": 1, "finished_at": 2, "evidence_commit": "$head", "coordinator_round": $ROUND, "coordinator_nonce": "$(printf 'f%.0s' {1..64})"}
JSON
printf 'reused\n' > "$tmp/audit/.factory-state/audit-receipts/reused.stdout"
printf '\n' > "$tmp/audit/.factory-state/audit-receipts/reused.stderr"
write_report "$tmp/audit" pass "\`true\` PASS [receipt: .factory-state/audit-receipts/reused.json]"
must_fail "receipt reused with a stale nonce" \
    "cd '$tmp/audit' && ./scripts/check-audit-receipts.py"

# A legacy receipt without coordinator binding fields is rejected.
cat > "$tmp/audit/.factory-state/audit-receipts/legacy.json" <<JSON
{"schema": "ralph-audit-receipt/v1", "tag": "legacy", "argv": ["true"], "argv_sha256": "$(printf 'b%.0s' {1..64})", "exit_code": 0, "stdout_sha256": "$(printf 'c%.0s' {1..64})", "stderr_sha256": "$(printf 'd%.0s' {1..64})", "started_at": 1, "finished_at": 2, "evidence_commit": "$head"}
JSON
printf 'legacy\n' > "$tmp/audit/.factory-state/audit-receipts/legacy.stdout"
printf '\n' > "$tmp/audit/.factory-state/audit-receipts/legacy.stderr"
write_report "$tmp/audit" pass "\`true\` PASS [receipt: .factory-state/audit-receipts/legacy.json]"
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
write_report "$tmp/audit" findings "\`sh -c 'printf \"runtime output\\n\"'\` PASS [receipt: .factory-state/audit-receipts/probe.json]"
(cd "$tmp/audit" && ./scripts/check-audit-receipts.py >/dev/null)

# A `[manifest:]` reference must be an exact signed record in the runner-
# evidence aggregate bound to the audit base; an accepted manifest certifies a
# clean PASS through the strict runner-evidence helper.
write_report "$tmp/audit" pass "\`./verify-project\` PASS [manifest: .factory-state/runner-evidence/fake-runner/$head/manifest.json]"
(cd "$tmp/audit" && ./scripts/check-audit-receipts.py >/dev/null)

# A standalone/minimal manifest is never accepted: it is not an exact signed
# aggregate record, so the strict runner-evidence validation rejects it.
mkdir -p "$tmp/audit/.factory-state/runner-manifests/runner-evidence"
cat > "$tmp/audit/.factory-state/runner-manifests/runner-evidence/manifest.json" <<'JSON'
{"schema": "factory-runner-receipt/v1", "result": "pass", "exit_code": 0}
JSON
write_report "$tmp/audit" pass "\`cmd\` PASS [manifest: .factory-state/runner-manifests/runner-evidence/manifest.json]"
must_fail "standalone minimal manifest" \
    "cd '$tmp/audit' && ./scripts/check-audit-receipts.py"

# An unsigned manifest (no detached signature) fails even when every binding
# matches and it is an exact aggregate record.
cp -a "$tmp/audit" "$tmp/unsigned"
rm -f "$tmp/unsigned/.factory-state/runner-evidence/fake-runner/$head/manifest.sig"
write_report "$tmp/unsigned" pass "\`cmd\` PASS [manifest: .factory-state/runner-evidence/fake-runner/$head/manifest.json]"
must_fail "unsigned runner manifest" \
    "cd '$tmp/unsigned' && ./scripts/check-audit-receipts.py"

# A manifest that is not an exact record in the aggregate fails even if the
# path would otherwise look evidence-shaped.
write_report "$tmp/audit" pass "\`cmd\` PASS [manifest: .factory-state/runner-evidence/fake-runner/$(printf '0%.0s' {1..40})/manifest.json]"
must_fail "manifest not in the aggregate" \
    "cd '$tmp/audit' && ./scripts/check-audit-receipts.py"

# FAIL evidence semantics: a runner manifest certifies only a clean pass, so a
# FAIL claim can never cite a pass manifest.
write_report "$tmp/audit" findings "\`cmd\` FAIL [manifest: .factory-state/runner-evidence/fake-runner/$head/manifest.json]"
must_fail "FAIL claim citing a pass manifest" \
    "cd '$tmp/audit' && ./scripts/check-audit-receipts.py"

# A manifest cannot be cited without the campaign audit base binding.
rm -f "$tmp/audit/.factory-state/audit-coordinator.json"
write_report "$tmp/audit" pass "\`cmd\` PASS [manifest: .factory-state/runner-evidence/fake-runner/$head/manifest.json]"
set +e
(cd "$tmp/audit" && env -u FACTORY_CAMPAIGN_AUDIT_ROUND -u FACTORY_CAMPAIGN_AUDIT_BASE -u FACTORY_CAMPAIGN_AUDIT_NONCE \
    ./scripts/check-audit-receipts.py >/dev/null 2>&1)
no_base_rc=$?
set -e
[[ $no_base_rc -eq 1 ]] || { echo "test: manifest accepted without a base binding" >&2; exit 1; }

echo "test: machine audit receipt checks passed"
