#!/usr/bin/env bash
# Adversarial campaign-objective validation: every audit round is bound to one
# product-neutral falsification objective with its own receipt categories;
# replaying one generic receipt suite cannot satisfy all rounds, BLOCKED
# evidence without machine receipts satisfies nothing, receipt categories are
# authorized by the tracked receipt policy (allowlisted argv, evidence tier,
# coordinator round/base/nonce bindings), and `[manifest:]` references must be
# exact signed records in the protected runner-evidence aggregate bound to the
# audit base. Standalone/minimal, unsigned, fabricated, and path-category-only
# manifests are rejected via the strict runner-evidence helper; receipts with
# the wrong coordinator nonce are rejected.
set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
PROJECT_ROOT=$(cd -- "$SCRIPT_DIR/.." && pwd)
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT

CHECKER="$PROJECT_ROOT/scripts/check-campaign-objectives.py"
RUNNER_EVIDENCE="$PROJECT_ROOT/scripts/check-factory-runner-evidence.py"
ENV_CHECKER="$PROJECT_ROOT/scripts/check-factory-environment.py"

NONCE=$(printf 'a%.0s' {1..64})

setup_repo() {
    local dir=$1
    mkdir -p "$dir/scripts" "$dir/.factory" "$dir/.factory/artifacts" "$dir/docs" \
        "$dir/.factory-state/audit-receipts" \
        "$dir/.factory-state/runner-evidence"
    chmod 700 "$dir/.factory-state"
    cp "$CHECKER" "$RUNNER_EVIDENCE" "$ENV_CHECKER" "$dir/scripts/"
    chmod +x "$dir/scripts/"*.py
    cat > "$dir/.factory/environment.toml" <<'EOF'
schema_version = 1
[[runners]]
name = "fake-runner"
transport = "ssh"
ssh_config_alias = "fake-runner"
working_directory = "/srv/dev-runner/workspaces/fake-project"
capabilities = ["runner-gate"]
verify_argv = ["./scripts/verify-boilerplate.sh"]
[[runners]]
name = "real-system-service"
transport = "ssh"
ssh_config_alias = "real-system-service"
working_directory = "/srv/dev-runner/workspaces/system-project"
capabilities = ["system-gate"]
verify_argv = ["./scripts/verify-boilerplate.sh"]
[[runners]]
name = "verify-runner"
transport = "ssh"
ssh_config_alias = "verify-runner"
working_directory = "/srv/dev-runner/workspaces/verify-project"
capabilities = ["verify-gate"]
verify_argv = ["./scripts/verify-boilerplate.sh"]
EOF
    cat > "$dir/.factory/capability-contracts.json" <<'CONTRACTS'
{
  "schema": "ralph-capability-contract/v1",
  "capabilities": [
    {
      "name": "runner-gate",
      "probe_argv": ["./scripts/run-factory-runners.py"],
      "probe_marker": "",
      "must_execute": true,
      "must_not_skip": [],
      "deny_simulated_markers": []
    },
    {
      "name": "system-gate",
      "probe_argv": ["./scripts/probe-system.sh"],
      "probe_marker": "",
      "must_execute": true,
      "must_not_skip": [],
      "deny_simulated_markers": []
    },
    {
      "name": "verify-gate",
      "probe_argv": ["./scripts/verify-project.sh"],
      "probe_marker": "",
      "must_execute": true,
      "must_not_skip": [],
      "deny_simulated_markers": []
    }
  ]
}
CONTRACTS
    cat > "$dir/.factory/campaign-objectives.json" <<'OBJECTIVES'
{
  "schema": "ralph-campaign-objectives/v1",
  "objectives": [
    {"key": "runner-capability", "label": "runner", "receipt_categories": ["runner-evidence", "project-verify"]},
    {"key": "visual-installed", "label": "visual", "receipt_categories": ["installed-visual", "visual-render"]},
    {"key": "real-system", "label": "real", "receipt_categories": ["real-system-service", "external-observer"]}
  ]
}
OBJECTIVES
    cat > "$dir/.factory/campaign-receipt-policy.json" <<'POLICY'
{
  "schema": "ralph-receipt-policy/v1",
  "categories": [
    {"name": "runner-evidence", "tier": "installed", "allow_manifest": true, "argv": [["./scripts/run-factory-runners.py"]]},
    {"name": "project-verify", "tier": "installed", "allow_manifest": false, "argv": [["./scripts/verify-project.sh"]]},
    {"name": "installed-visual", "tier": "installed", "allow_manifest": false, "argv": [["./scripts/probe-visual.sh"]]},
    {"name": "visual-render", "tier": "installed", "allow_manifest": false, "argv": [["./scripts/probe-visual.sh"]]},
    {"name": "real-system-service", "tier": "real_system", "allow_manifest": true, "argv": [["./scripts/probe-system.sh"]]},
    {"name": "external-observer", "tier": "real_system", "allow_manifest": true, "argv": [["./scripts/probe-system.sh"]]}
  ]
}
POLICY
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
    printf '# Spec\n' > "$dir/docs/SPEC.md"
    printf '%s\n' '.factory-state/' > "$dir/.gitignore"
    git -C "$dir" init -q -b develop
    git -C "$dir" config user.name test
    git -C "$dir" config user.email test@example.invalid
    git -C "$dir" add .
    git -C "$dir" commit -qm base
}

# write_evidence <dir> <runner>: a fully commit-bound signed aggregate fixture
# (tree/environment blob/verifier argv digest/git archive hash all recomputed)
# so signature and aggregate membership are the variables under test.
write_evidence() {
    local dir=$1 runner=$2
    local head
    head=$(git -C "$dir" rev-parse HEAD)
    mkdir -p "$dir/.factory-state/runner-evidence/$runner/$head"
    python3 - "$dir" "$runner" "$head" "$PUBLIC_KEY" <<'PY'
import hashlib, json, pathlib, subprocess, sys, tomllib
root, runner, head, public_key = pathlib.Path(sys.argv[1]), sys.argv[2], sys.argv[3], sys.argv[4]

def git(*args: str) -> str:
    result = subprocess.run(["git", *args], cwd=root, text=True, capture_output=True)
    if result.returncode:
        raise SystemExit(f"git {args} failed: {result.stderr}")
    return result.stdout.strip()

tree = git("rev-parse", f"{head}^{{tree}}")
environment_blob = git("rev-parse", f"{head}:.factory/environment.toml")
environment = tomllib.loads(git("show", f"{head}:.factory/environment.toml"))
declared = next(item for item in environment["runners"] if item["name"] == runner)
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
    "schema": "factory-runner-receipt/v1", "result": "pass", "runner": runner,
    "commit": head, "tree": tree, "environment_blob": environment_blob,
    "verify_argv_sha256": argv_digest, "archive_sha256": archive_sha256, "nonce": "0" * 64,
    "capabilities": capabilities, "exit_code": 0, "timed_out": False,
    "started_at": 1, "finished_at": 2, "cleanup": True,
    "stdout_sha256": empty, "stderr_sha256": empty,
    "signer_principal": "factory-signer", "signer_key_sha256": key_sha256,
    "namespace": "factory-runner-receipt", "signature_algorithm": "ssh-ed25519",
}
raw = (json.dumps(manifest, sort_keys=True, indent=2) + "\n").encode()
manifest_path = root / f".factory-state/runner-evidence/{runner}/{head}/manifest.json"
manifest_path.write_bytes(raw)
aggregate_path = root / ".factory-state/runner-evidence.json"
if aggregate_path.exists():
    aggregate = json.loads(aggregate_path.read_text())
else:
    aggregate = {"schema": "factory-runner-aggregate/v1", "commit": head,
                 "tree": tree, "environment_blob": environment_blob, "runners": []}
aggregate["runners"] = [
    item for item in aggregate["runners"] if item["name"] != runner
] + [{"name": runner, "manifest": f".factory-state/runner-evidence/{runner}/{head}/manifest.json",
      "manifest_sha256": hashlib.sha256(raw).hexdigest(), "capabilities": capabilities,
      "signer": {"principal": "factory-signer", "key_sha256": key_sha256,
                  "algorithm": "ssh-ed25519", "signature_sha256": ""}}]
aggregate_path.write_text(json.dumps(aggregate, sort_keys=True, indent=2) + "\n")
(root / f".factory-state/runner-evidence/{runner}/{head}/stdout.log").write_bytes(b"")
(root / f".factory-state/runner-evidence/{runner}/{head}/stderr.log").write_bytes(b"")
PY
    cat "$dir/.factory-state/runner-evidence/$runner/$head/manifest.json" \
        | ssh-keygen -Y sign -f "$tmp/signer-key" -n factory-runner-receipt \
            > "$dir/.factory-state/runner-evidence/$runner/$head/manifest.sig" 2>/dev/null
    python3 - "$dir/.factory-state/runner-evidence/$runner/$head/manifest.sig" \
        "$dir/.factory-state/runner-evidence.json" "$runner" <<'PY'
import hashlib, json, pathlib, sys
sig_path, aggregate_path, runner = pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2]), sys.argv[3]
aggregate = json.loads(aggregate_path.read_text())
for record in aggregate["runners"]:
    if record["name"] == runner:
        record["signer"]["signature_sha256"] = hashlib.sha256(sig_path.read_bytes()).hexdigest()
aggregate_path.write_text(json.dumps(aggregate, sort_keys=True, indent=2) + "\n")
PY
}

# mint_state <dir> <round>: authoritative protected coordinator state for the
# active audit round (base = fixture head, nonce = fixture NONCE).
mint_state() {
    local dir=$1 round=$2
    local head
    head=$(git -C "$dir" rev-parse HEAD)
    cat > "$dir/.factory-state/audit-coordinator.json" <<JSON
{"schema": "ralph-audit-coordinator/v1", "round": $round, "base_commit": "$head", "nonce": "$NONCE", "created_at": 1}
JSON
    chmod 600 "$dir/.factory-state/audit-coordinator.json"
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
- Executable evidence: ./scripts/verify-project.sh PASS [receipt: .factory-state/audit-receipts/project-verify.json]
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
write_evidence "$tmp/blessed" fake-runner
write_evidence "$tmp/blessed" real-system-service
write_evidence "$tmp/blessed" verify-runner
receipt "$tmp/blessed" 1 runner-evidence 0 ./scripts/run-factory-runners.py
receipt "$tmp/blessed" 1 project-verify 0 ./scripts/verify-project.sh
receipt "$tmp/blessed" 1 visual-render 1 ./scripts/probe-visual.sh
write_report "$tmp/blessed"

# Round 1 (runner-capability) is satisfied by the runner suite.
mint_state "$tmp/blessed" 1
expect_rc "$tmp/blessed" 1 "$head" 0 "round 1 runner suite"

# Round 2 requires the visual-installed categories: replaying round 1's suite
# must fail (round binding), and the FAIL receipt for visual-render cannot
# cover the category either.
mint_state "$tmp/blessed" 2
expect_rc "$tmp/blessed" 2 "$head" 1 "round 2 replay of round 1 suite"

# Round 3 requires real-system categories; the generic suite cannot cover them.
mint_state "$tmp/blessed" 3
expect_rc "$tmp/blessed" 3 "$head" 1 "round 3 replay of round 1 suite"

# A complete real-system suite (round-bound receipts) passes round 3.
receipt "$tmp/blessed" 3 real-system-service 0 ./scripts/probe-system.sh
receipt "$tmp/blessed" 3 external-observer 0 ./scripts/probe-system.sh
cat > "$tmp/blessed/.factory/artifacts/campaign-audit.md" <<'REPORT'
---
result: pass
---
# Campaign Audit
## Evidence reviewed
- Service: docs/SPEC.md §2
- Executable evidence: ./scripts/probe-system.sh PASS [receipt: .factory-state/audit-receipts/real-system-service.json]
- Executable evidence: ./scripts/probe-system.sh PASS [receipt: .factory-state/audit-receipts/external-observer.json]

## Findings
None.
REPORT
mint_state "$tmp/blessed" 3
expect_rc "$tmp/blessed" 3 "$head" 0 "round 3 real-system suite"

# BLOCKED evidence lines carry no receipt and satisfy nothing.
cat > "$tmp/blessed/.factory/artifacts/campaign-audit.md" <<'REPORT'
---
result: findings
---
# Campaign Audit
## Evidence reviewed
- Executable evidence: ./scripts/probe.sh BLOCKED no real system service available
- Executable evidence: ./scripts/probe-system.sh PASS [receipt: .factory-state/audit-receipts/external-observer.json]

## Findings
- Hardware unavailable
REPORT
mint_state "$tmp/blessed" 3
expect_rc "$tmp/blessed" 3 "$head" 1 "BLOCKED evidence without receipts"

# A receipt with an unrelated tag does not cover a required category (round-1
# receipts replayed at round 3 fail the round binding and cover nothing).
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
mint_state "$tmp/blessed" 3
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
mint_state "$tmp/blessed" 1
expect_rc "$tmp/blessed" 1 "$head" 1 "arbitrary command with a spoofed tag"

# A stale receipt (evidence_commit != audit base) cannot cover a category.
cat > "$tmp/blessed/.factory-state/audit-receipts/project-verify.json" <<JSON
{"schema": "ralph-audit-receipt/v1", "tag": "project-verify", "argv": ["./scripts/verify-project.sh"], "argv_sha256": "$(printf 'e%.0s' {1..64})", "exit_code": 0, "stdout_sha256": "$(printf 'c%.0s' {1..64})", "stderr_sha256": "$(printf 'd%.0s' {1..64})", "started_at": 1, "finished_at": 2, "evidence_commit": "$(printf '2%.0s' {1..40})", "coordinator_round": 1, "coordinator_nonce": "$NONCE"}
JSON
cat > "$tmp/blessed/.factory/artifacts/campaign-audit.md" <<'REPORT'
---
result: pass
---
# Campaign Audit
## Evidence reviewed
- Executable evidence: ./scripts/run-factory-runners.py PASS [receipt: .factory-state/audit-receipts/runner-evidence.json]
- Executable evidence: ./scripts/verify-project.sh PASS [receipt: .factory-state/audit-receipts/project-verify.json]

pass
REPORT
mint_state "$tmp/blessed" 1
expect_rc "$tmp/blessed" 1 "$head" 1 "stale round receipt cannot cover a category"

# A standalone/minimal manifest at a path-matching category is rejected: it is
# not an exact signed record in the runner-evidence aggregate (path-category
# manifests are never accepted).
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
- Executable evidence: ./scripts/probe.sh PASS [manifest: .factory-state/runner-manifests/runner-evidence/manifest.json]
- Executable evidence: ./scripts/verify-project.sh PASS [receipt: .factory-state/audit-receipts/project-verify.json]

pass
REPORT
mint_state "$tmp/blessed" 1
expect_rc "$tmp/blessed" 1 "$head" 1 "minimal path-category manifest"

# A signed, fully bound exact aggregate fixture DOES satisfy a manifest-allowed
# category (runner-evidence) at the audit base.
receipt "$tmp/blessed" 1 project-verify 0 ./scripts/verify-project.sh
cat > "$tmp/blessed/.factory/artifacts/campaign-audit.md" <<REPORT
---
result: pass
---
# Campaign Audit
## Evidence reviewed
- Executable evidence: ./scripts/verify-project.sh PASS [receipt: .factory-state/audit-receipts/project-verify.json]
- Executable evidence: ./scripts/probe.sh PASS [manifest: .factory-state/runner-evidence/fake-runner/$head/manifest.json]

pass
REPORT
mint_state "$tmp/blessed" 1
expect_rc "$tmp/blessed" 1 "$head" 1 "legacy unnamespaced aggregate is rejected"

# Manifest matching is exact capability evidence, never a path-substring
# proxy: the real-system-service manifest's path carries the runner-evidence
# evidence-root segment, but its recorded capabilities (system-gate) do not
# evidence the runner-evidence category, so the category stays uncovered.
cat > "$tmp/blessed/.factory/artifacts/campaign-audit.md" <<REPORT
---
result: pass
---
# Campaign Audit
## Evidence reviewed
- Executable evidence: ./scripts/verify-project.sh PASS [receipt: .factory-state/audit-receipts/project-verify.json]
- Executable evidence: ./scripts/probe.sh PASS [manifest: .factory-state/runner-evidence/real-system-service/$head/manifest.json]

pass
REPORT
mint_state "$tmp/blessed" 1
expect_rc "$tmp/blessed" 1 "$head" 1 "path-substring manifest is not capability evidence"

# A signed, fully bound real-system manifest DOES satisfy the manifest-allowed
# real-system categories: the real-system-service manifest's recorded
# capabilities (system-gate, whose contract probe argv equals the allowlisted
# probe-system.sh) cover real-system-service AND external-observer exactly.
cat > "$tmp/blessed/.factory/artifacts/campaign-audit.md" <<REPORT
---
result: pass
---
# Campaign Audit
## Evidence reviewed
- Executable evidence: ./scripts/probe.sh PASS [manifest: .factory-state/runner-evidence/real-system-service/$head/manifest.json]

pass
REPORT
mint_state "$tmp/blessed" 3
expect_rc "$tmp/blessed" 3 "$head" 1 "legacy real-system manifest lacks v3 namespace bindings"

# A signed, fully bound manifest whose capabilities evidence a NON-manifest
# category cannot cover it: verify-gate's contract probe argv matches the
# project-verify category, which forbids manifest coverage, so a machine
# receipt with the allowlisted argv is required.
cat > "$tmp/blessed/.factory/artifacts/campaign-audit.md" <<REPORT
---
result: pass
---
# Campaign Audit
## Evidence reviewed
- Executable evidence: ./scripts/probe.sh PASS [manifest: .factory-state/runner-evidence/verify-runner/$head/manifest.json]

pass
REPORT
mint_state "$tmp/blessed" 1
expect_rc "$tmp/blessed" 1 "$head" 1 "signed manifest cannot cover a non-manifest category"

# A receipt with the wrong coordinator nonce is rejected even when every other
# binding matches.
python3 - "$tmp/blessed" <<'PY'
import json, sys
root = sys.argv[1]
path = root + "/.factory-state/audit-receipts/external-observer.json"
data = json.load(open(path))
data["coordinator_nonce"] = "b" * 64
open(path, "w").write(json.dumps(data))
PY
cat > "$tmp/blessed/.factory/artifacts/campaign-audit.md" <<'REPORT'
---
result: pass
---
# Campaign Audit
## Evidence reviewed
- Executable evidence: ./scripts/probe-system.sh PASS [receipt: .factory-state/audit-receipts/real-system-service.json]
- Executable evidence: ./scripts/probe-system.sh PASS [receipt: .factory-state/audit-receipts/external-observer.json]

pass
REPORT
mint_state "$tmp/blessed" 3
expect_rc "$tmp/blessed" 3 "$head" 1 "wrong coordinator nonce"

# Without an objectives map the check fails.
cp -a "$tmp/blessed" "$tmp/no-objectives"
rm -f "$tmp/no-objectives/.factory/campaign-objectives.json"
mint_state "$tmp/no-objectives" 1
expect_rc "$tmp/no-objectives" 1 "$head" 1 "missing objectives map"

# Without a receipt policy the check fails.
cp -a "$tmp/blessed" "$tmp/no-policy"
rm -f "$tmp/no-policy/.factory/campaign-receipt-policy.json"
mint_state "$tmp/no-policy" 1
expect_rc "$tmp/no-policy" 1 "$head" 1 "missing receipt policy"

# A round is required when neither --round nor the protected state supplies one.
rm -f "$tmp/blessed/.factory-state/audit-coordinator.json"
set +e
(cd "$tmp/blessed" && env -u FACTORY_CAMPAIGN_AUDIT_ROUND \
    ./scripts/check-campaign-objectives.py --base "$head" >/dev/null 2>&1)
env_missing_rc=$?
set -e
[[ $env_missing_rc -eq 1 ]] || { echo "test: missing audit round was accepted" >&2; exit 1; }

echo "test: campaign-objectives adversarial checks passed"
