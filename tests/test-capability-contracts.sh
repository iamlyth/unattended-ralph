#!/usr/bin/env bash
# Adversarial capability-contract checks (BUG-0016): contracts claiming
# undeclared capabilities, declared capabilities without contracts, missing
# receipts, skipped probes, simulated markers, and stale receipts must all be
# unevidenced and rejected.
set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
PROJECT_ROOT=$(cd -- "$SCRIPT_DIR/.." && pwd)
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT

CONTRACT_CHECKER="$PROJECT_ROOT/scripts/check-capability-contracts.py"
EVIDENCE_CHECKER="$PROJECT_ROOT/scripts/check-capability-evidence.py"

must_fail() {
    local label=$1 cmd=$2
    set +e
    bash -c "$cmd" >/dev/null 2>&1
    local rc=$?
    set -e
    [[ $rc -eq 1 ]] || { echo "test: $label returned rc=$rc (expected 1)" >&2; exit 1; }
}

setup_repo() {
    local dir=$1 capability=$2
    mkdir -p "$dir/scripts" "$dir/.factory/artifacts" "$dir/docs" \
        "$dir/.factory-state/runner-evidence/probe-runner"
    cp "$CONTRACT_CHECKER" "$EVIDENCE_CHECKER" "$dir/scripts/"
    chmod +x "$dir/scripts/"*.py
    printf '#!/usr/bin/env bash\nexit 0\n' > "$dir/scripts/verify-project.sh"
    chmod +x "$dir/scripts/verify-project.sh"
    cat > "$dir/.factory/environment.toml" <<EOF
schema_version = 1
[[runners]]
name = "probe-runner"
transport = "ssh"
ssh_config_alias = "probe-runner"
working_directory = "/srv/dev-runner/workspaces/probe"
capabilities = ["$capability"]
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
}

write_contract() {
    local dir=$1 capability=$2 marker=$3
    cat > "$dir/.factory/capability-contracts.json" <<CONTRACT
{
  "schema": "ralph-capability-contract/v1",
  "capabilities": [
    {
      "name": "$capability",
      "probe_argv": ["./scripts/verify-project.sh"],
      "probe_marker": "$marker",
      "must_execute": true,
      "must_not_skip": ["Skipped", "Not Run", "skip"],
      "deny_simulated_markers": ["mock", "private service", "simulated"]
    }
  ]
}
CONTRACT
}

write_receipt() {
    local dir=$1 mode=$2
    local head
    head=$(git -C "$dir" rev-parse HEAD)
    mkdir -p "$dir/.factory-state/runner-evidence/probe-runner/$head"
    printf '%s\n' '{"schema":"factory-runner-receipt/v1","result":"pass","exit_code":0}' > \
        "$dir/.factory-state/runner-evidence/probe-runner/$head/manifest.json"
    case "$mode" in
        clean)
            cat > "$dir/.factory-state/runner-evidence/probe-runner/$head/stdout.log" <<'LOG'
--- probe-capability contract ---
100% tests passed, 0 tests failed out of 1
LOG
            ;;
        skipped)
            cat > "$dir/.factory-state/runner-evidence/probe-runner/$head/stdout.log" <<'LOG'
--- probe-capability contract ---
Test #1: probe ...................***Skipped   0.01 sec
LOG
            ;;
        simulated)
            cat > "$dir/.factory-state/runner-evidence/probe-runner/$head/stdout.log" <<'LOG'
--- probe-capability contract ---
100% tests passed (private service simulation)
LOG
            ;;
        missing-marker)
            printf '%s\n' '100% tests passed, 0 tests failed out of 1' > \
                "$dir/.factory-state/runner-evidence/probe-runner/$head/stdout.log"
            ;;
    esac
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
}

# A contract for an undeclared/unavailable capability is a forbidden claim.
setup_repo "$tmp/undeclared-contract" probe-capability
write_contract "$tmp/undeclared-contract" demo-system-service "--- demo-system-service contract ---"
must_fail "contract claiming an undeclared capability" \
    "cd '$tmp/undeclared-contract' && ./scripts/check-capability-contracts.py"

# A declared capability without a contract is unevidenced.
setup_repo "$tmp/missing-contract" probe-capability
must_fail "declared capability without a contract" \
    "cd '$tmp/missing-contract' && ./scripts/check-capability-contracts.py"

# A valid contract plus a clean exact-commit receipt is evidenced.
setup_repo "$tmp/valid" probe-capability
write_contract "$tmp/valid" probe-capability "--- probe-capability contract ---"
write_receipt "$tmp/valid" clean
(cd "$tmp/valid" && ./scripts/check-capability-contracts.py >/dev/null)
(cd "$tmp/valid" && ./scripts/check-capability-evidence.py >/dev/null)

# runner_class is optional candidate metadata (a candidate binds itself to its
# root-configured runner class before provisioning): a valid lowercase class
# name passes, an invalid one is rejected.
setup_repo "$tmp/runner-class-ok" probe-capability
write_contract "$tmp/runner-class-ok" probe-capability "--- probe-capability contract ---"
python3 - "$tmp/runner-class-ok/.factory/capability-contracts.json" <<'PY'
import json, sys
path = sys.argv[1]
data = json.load(open(path, encoding="utf-8"))
data['capabilities'][0]['runner_class'] = 'runner'
open(path, 'w', encoding="utf-8").write(json.dumps(data, indent=2))
PY
(cd "$tmp/runner-class-ok" && ./scripts/check-capability-contracts.py >/dev/null)

runner_class_bad=0
for bad in 'Runner' '' 'two words' 'runner/class' 'R'; do
    runner_class_bad=$((runner_class_bad + 1))
    setup_repo "$tmp/runner-class-bad-$runner_class_bad" probe-capability
    write_contract "$tmp/runner-class-bad-$runner_class_bad" probe-capability "--- probe-capability contract ---"
    python3 - "$tmp/runner-class-bad-$runner_class_bad/.factory/capability-contracts.json" <<PY
import json, sys
path = sys.argv[1]
data = json.load(open(path, encoding="utf-8"))
data['capabilities'][0]['runner_class'] = '''$bad'''
open(path, 'w', encoding="utf-8").write(json.dumps(data, indent=2))
PY
    must_fail "invalid runner_class '$bad'" \
        "cd '$tmp/runner-class-bad-$runner_class_bad' && ./scripts/check-capability-contracts.py"
done

# Structural fail-closed: a committed contract probe argv may never carry a
# token that equals or is prefixed by a fixture/simulation option, because the
# exact probe command could then switch into fixture mode and fabricate a pass.
fixture_token_bad=0
for token in '--fixture' '--fixture=/tmp/facts' '--fixture-dir' '--fixture-dir=/tmp/f' '--fixture-facts' '--fixture-facts=/tmp/f.json'; do
    fixture_token_bad=$((fixture_token_bad + 1))
    setup_repo "$tmp/fixture-token-$fixture_token_bad" probe-capability
    write_contract "$tmp/fixture-token-$fixture_token_bad" probe-capability "--- probe-capability contract ---"
    python3 - "$tmp/fixture-token-$fixture_token_bad/.factory/capability-contracts.json" "$token" <<PY
import json, sys
path, token = sys.argv[1], sys.argv[2]
data = json.load(open(path, encoding="utf-8"))
data['capabilities'][0]['probe_argv'] = ["nix-shell", "--run", "bash scripts/probe.sh", token]
open(path, 'w', encoding="utf-8").write(json.dumps(data, indent=2))
PY
    must_fail "probe argv fixture token '$token'" \
        "cd '$tmp/fixture-token-$fixture_token_bad' && ./scripts/check-capability-contracts.py"
done

# A missing receipt (no aggregate) is unevidenced.
cp -a "$tmp/valid" "$tmp/missing-receipt"
rm -f "$tmp/missing-receipt/.factory-state/runner-evidence.json"
rm -rf "$tmp/missing-receipt/.factory-state/runner-evidence/probe-runner"
must_fail "missing runner receipt" \
    "cd '$tmp/missing-receipt' && ./scripts/check-capability-evidence.py"

# A skipped probe in the receipt log is unevidenced.
cp -a "$tmp/valid" "$tmp/skipped-probe"
write_receipt "$tmp/skipped-probe" skipped
must_fail "skipped probe in the receipt" \
    "cd '$tmp/skipped-probe' && ./scripts/check-capability-evidence.py"

# Simulated/denied markers in the probe section are unevidenced.
cp -a "$tmp/valid" "$tmp/simulated"
write_receipt "$tmp/simulated" simulated
must_fail "simulated marker in the receipt" \
    "cd '$tmp/simulated' && ./scripts/check-capability-evidence.py"

# A receipt that never executed the probe marker is unevidenced (must-execute).
cp -a "$tmp/valid" "$tmp/missing-marker"
write_receipt "$tmp/missing-marker" missing-marker
must_fail "receipt without the probe marker" \
    "cd '$tmp/missing-marker' && ./scripts/check-capability-evidence.py"

# A receipt bound to a different commit is stale and unevidenced.
cp -a "$tmp/valid" "$tmp/stale"
python3 - "$tmp/stale/.factory-state/runner-evidence.json" <<'PY'
import json, sys
path = sys.argv[1]
data = json.load(open(path))
data['commit'] = '0' * 40
open(path, 'w').write(json.dumps(data))
PY
must_fail "stale receipt not bound to HEAD" \
    "cd '$tmp/stale' && ./scripts/check-capability-evidence.py"

# A runner manifest that does not prove a clean pass is unevidenced.
cp -a "$tmp/valid" "$tmp/fail-manifest"
head=$(git -C "$tmp/fail-manifest" rev-parse HEAD)
printf '%s\n' '{"schema":"factory-runner-receipt/v1","result":"fail","exit_code":1}' > \
    "$tmp/fail-manifest/.factory-state/runner-evidence/probe-runner/$head/manifest.json"
must_fail "failed runner manifest" \
    "cd '$tmp/fail-manifest' && ./scripts/check-capability-evidence.py"

# An explicit empty capability request must not pass a gate.
must_fail "empty explicit capability set" \
    "cd '$tmp/valid' && ./scripts/check-capability-evidence.py --capabilities ''"

echo "test: capability contract and receipt checks passed"
