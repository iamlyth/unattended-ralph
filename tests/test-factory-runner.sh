#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
PROJECT_ROOT=$(cd -- "$SCRIPT_DIR/.." && pwd)
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
mkdir -p "$tmp/repo/scripts" "$tmp/repo/docs" "$tmp/repo/.factory" \
    "$tmp/runner/workspaces/fake-project"
chmod 0700 "$tmp/runner/workspaces/fake-project"
cp "$PROJECT_ROOT/scripts/run-factory-runners.py" \
   "$PROJECT_ROOT/scripts/check-factory-runner-evidence.py" \
   "$PROJECT_ROOT/scripts/factory-runner-server.py" \
   "$PROJECT_ROOT/scripts/factory_runner_policy.py" "$tmp/repo/scripts/"
chmod +x "$tmp/repo/scripts/"*.py
cat > "$tmp/repo/.factory/environment.toml" <<EOF
schema_version = 1
[[runners]]
name = "fake-runner"
transport = "ssh"
ssh_config_alias = "fake-runner"
working_directory = "$tmp/runner/workspaces/fake-project"
capabilities = ["project-gate"]
verify_argv = ["./scripts/verify-boilerplate.sh"]
EOF
cat > "$tmp/repo/.factory/config.toml" <<'EOF'
[project]
development_branch = "develop"
EOF
cat > "$tmp/repo/.factory/capability-contracts.json" <<'EOF'
{
  "schema": "ralph-capability-contract/v1",
  "capabilities": [
    {
      "name": "project-gate",
      "status": "declared",
      "probe_argv": ["./scripts/verify-boilerplate.sh"],
      "probe_marker": "",
      "probe_stage": "post",
      "probe_stdout_contains": [],
      "probe_is_verify_run": true,
      "must_execute": true,
      "must_not_skip": [],
      "deny_simulated_markers": ["simulated", "fixture-only"]
    },
    {
      "name": "user-service",
      "status": "declared",
      "probe_argv": ["/usr/bin/true"],
      "probe_marker": "--- user-service capability contract ---",
      "probe_stage": "post",
      "probe_stdout_contains": ["factory-user-service-ok"],
      "probe_is_verify_run": false,
      "must_execute": true,
      "must_not_skip": ["Skipped", "Not Run", "skip"],
      "deny_simulated_markers": ["mock", "simulated"]
    }
  ]
}
EOF
cat > "$tmp/repo/scripts/verify-boilerplate.sh" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
[[ $(git rev-parse HEAD) =~ ^[0-9a-f]{40}$ ]]
[[ -z $(git status --porcelain --untracked-files=normal) ]]
exit 0
EOF
chmod +x "$tmp/repo/scripts/verify-boilerplate.sh"
printf '# Spec\n' > "$tmp/repo/docs/SPEC.md"
cat > "$tmp/repo/.gitignore" <<'EOF'
.factory-state/
EOF
# Ephemeral signer for runner-receipt trust (private key stays out-of-tree).
ssh-keygen -q -t ed25519 -N '' -f "$tmp/signer-key"
PUBLIC_KEY=$(cut -d' ' -f1,2 "$tmp/signer-key.pub")
python3 - "$PUBLIC_KEY" <<'PY' > "$tmp/repo/.factory/signer-trust.json"
import json, sys
print(json.dumps({
    "schema": "ralph-runner-signer-trust/v1",
    "description": "ephemeral test fixture signer",
    "require_signature": True,
    "enabled": True,
    "namespace": "factory-runner-receipt",
    "public_keys": [{"principal": "fake-runner", "public_key": sys.argv[1]}],
    "allowed_principals": ["fake-runner"],
}))
PY
# Root-configured runner class policy: the executing UID binds to exactly one
# class, whose workspace root, verifier argv, and capability allowlist the
# endpoint must honor exactly.
write_policy() {
    local capabilities=$1 verify_argv=${2:-'["./scripts/verify-boilerplate.sh"]'}
    python3 - "$tmp" "$capabilities" "$verify_argv" <<'PY' > "$tmp/policy.json"
import json, os, sys
tmp, capabilities, verify_argv = sys.argv[1], json.loads(sys.argv[2]), json.loads(sys.argv[3])
print(json.dumps({
    "schema": "factory-runner-policy/v1",
    "namespace": "factory-runner-receipt",
    "classes": [
        {
            "name": "fake-runner",
            "uid": os.getuid(),
            "workspace_root": f"{tmp}/runner/workspaces",
            "verify_argv": verify_argv,
            "allowed_capabilities": capabilities,
            "signer_helper": "/usr/local/libexec/factory-runner-signer",
            "signer_key": f"{tmp}/signer-key",
            "signer_principal_file": f"{tmp}/signer-principal",
        }
    ],
}))
PY
}
write_policy '["project-gate"]'
cat > "$tmp/fake-ssh" <<EOF
#!/usr/bin/env bash
set -euo pipefail
export SSH_ORIGINAL_COMMAND=factory-runner-v1
exec "$(command -v python3)" -I "$tmp/repo/scripts/factory-runner-server.py"
EOF
chmod +x "$tmp/fake-ssh"
mkdir -p "$tmp/client-home/.ssh"
ln -s "$tmp/fake-ssh" "$tmp/client-home/.ssh/factory-ssh"
export HOME="$tmp/client-home"
# The root-owned signer helper runs in this disposable harness as the test
# user with an ephemeral key; only the root-identity checks are relaxed in the
# copied helper, exactly as the workspace-root checks are relaxed below.
cp "$PROJECT_ROOT/scripts/factory-runner-signer.py" "$tmp/repo/scripts/factory-runner-signer.py"
chmod +x "$tmp/repo/scripts/factory-runner-signer.py"
sed -i \
    -e 's/if os.getuid() != os.geteuid() or os.geteuid() != 0:/if False:/' \
    -e 's/if key_stat.st_uid != 0 or key_stat.st_mode & 0o077:/if key_stat.st_mode \& 0o077:/' \
    -e 's/if principal_stat.st_uid != 0 or principal_stat.st_mode & 0o077:/if principal_stat.st_mode \& 0o077:/' \
    "$tmp/repo/scripts/factory-runner-signer.py"
printf 'fake-runner\n' > "$tmp/signer-principal"
chmod 0600 "$tmp/signer-principal" "$tmp/signer-key"
export FACTORY_SIGNER_KEY="$tmp/signer-key"
export FACTORY_SIGNER_PRINCIPAL_FILE="$tmp/signer-principal"
export FACTORY_SIGNER_CLASS=fake-runner
export FACTORY_RUNNER_POLICY="$tmp/policy.json"
# The production endpoint reaches the signer through a narrowly scoped sudoers
# entry; the disposable harness executes the copied helper directly instead.
sed -i "s#\[SUDO, \"-n\", signer_helper\]#[\"$(command -v python3)\", \"-I\", \"$tmp/repo/scripts/factory-runner-signer.py\"]#" "$tmp/repo/scripts/factory-runner-server.py"
# Validator bypass is confined to this disposable copied test harness because
# its temporary workspace intentionally does not use the production /srv root.
sed -i '0,/if subprocess.run(/s//if False and subprocess.run(/' "$tmp/repo/scripts/run-factory-runners.py"
sed -i '0,/if subprocess.run(/s//if False and subprocess.run(/' "$tmp/repo/scripts/check-factory-runner-evidence.py"
sed -i 's/root_stat.st_uid != 0/root_stat.st_uid != os.getuid()/' "$tmp/repo/scripts/factory-runner-server.py"
sed -i "s#/usr/bin/git#$(command -v git)#g" "$tmp/repo/scripts/factory-runner-server.py"
sed -i 's#"PATH": "/nix/var/nix/profiles/default/bin:/usr/local/bin:/usr/bin:/bin"#"PATH": os.environ.get("PATH", "")#' "$tmp/repo/scripts/factory-runner-server.py"
git -C "$tmp/repo" init -q -b develop
git -C "$tmp/repo" config user.name test
git -C "$tmp/repo" config user.email test@example.invalid
git -C "$tmp/repo" add .
git -C "$tmp/repo" commit -qm base
(
    cd "$tmp/repo"
    ./scripts/run-factory-runners.py >/dev/null
    head=$(git rev-parse HEAD)
    # The endpoint signs the manifest it generated; the client stores the
    # detached signature and the aggregate signer metadata verbatim.
    [[ -f ".factory-state/runner-evidence/fake-runner/$head/manifest.sig" ]]
    python3 - ".factory-state/runner-evidence/fake-runner/$head/manifest.json" \
        ".factory-state/runner-evidence/fake-runner/$head/manifest.sig" \
        ".factory-state/runner-evidence.json" \
        "$(cut -d' ' -f1,2 "$tmp/signer-key.pub")" <<'PY'
import hashlib, json, pathlib, subprocess, sys
manifest_path, sig_path, aggregate_path = map(pathlib.Path, sys.argv[1:4])
public_key = sys.argv[4]
manifest = json.loads(manifest_path.read_bytes())
assert manifest["result"] == "pass"
assert manifest["signer_principal"] == "fake-runner"
assert manifest["namespace"] == "factory-runner-receipt"
assert manifest["signature_algorithm"] == "ssh-ed25519"
assert manifest["signer_key_sha256"] == hashlib.sha256(public_key.encode()).hexdigest()
aggregate = json.loads(aggregate_path.read_bytes())
signer = aggregate["runners"][0]["signer"]
assert signer["principal"] == manifest["signer_principal"]
assert signer["key_sha256"] == manifest["signer_key_sha256"]
assert signer["signature_sha256"] == hashlib.sha256(sig_path.read_bytes()).hexdigest()
allowed = pathlib.Path(".factory-state/allowed-signers")
allowed.write_text(f"fake-runner {public_key}\n")
verified = subprocess.run(
    ["ssh-keygen", "-Y", "verify", "-f", str(allowed), "-I", "fake-runner",
     "-n", "factory-runner-receipt", "-s", str(sig_path)],
    input=manifest_path.read_bytes(), capture_output=True,
)
assert verified.returncode == 0, verified.stderr
PY
    ./scripts/check-factory-runner-evidence.py >/dev/null
    capabilities=$(./scripts/check-factory-runner-evidence.py --print-capabilities)
    grep -qx 'project-gate' <<<"$capabilities"
)
base=$(git -C "$tmp/repo" rev-parse HEAD)
# Historical validation must use the selected commit's declaration, not the
# current checkout's unrelated environment file.
sed -i 's/name = "fake-runner"/name = "other-runner"/' "$tmp/repo/.factory/environment.toml"
git -C "$tmp/repo" add .factory/environment.toml
git -C "$tmp/repo" commit -qm unrelated-environment
(cd "$tmp/repo" && ./scripts/check-factory-runner-evidence.py --expected-commit "$base" >/dev/null)
git -C "$tmp/repo" reset -q --hard "$base"

# A resource name is not evidence: unsupported hardware claims fail before the
# generic verifier can turn them into a passing receipt.
sed -i 's/\["project-gate"\]/["project-gate", "physical-controller"]/' \
    "$tmp/repo/.factory/environment.toml"
git -C "$tmp/repo" add .factory/environment.toml
git -C "$tmp/repo" commit -qm unsupported-physical-controller
set +e
(cd "$tmp/repo" && ./scripts/run-factory-runners.py >/dev/null 2>&1)
unsupported_capability_rc=$?
set -e
[[ $unsupported_capability_rc -eq 1 ]]
git -C "$tmp/repo" reset -q --hard "$base"

# Device presence alone cannot produce capability evidence: a fabricated alias
# (request runner/class not bound to the executing UID) is rejected by the
# root endpoint before any probe can run.
write_policy '["project-gate"]'
sed -i 's/name = "fake-runner"/name = "intruder-runner"/' "$tmp/repo/.factory/environment.toml"
sed -i 's/ssh_config_alias = "fake-runner"/ssh_config_alias = "intruder-runner"/' "$tmp/repo/.factory/environment.toml"
git -C "$tmp/repo" add .factory/environment.toml
git -C "$tmp/repo" commit -qm fabricated-alias
set +e
(cd "$tmp/repo" && ./scripts/run-factory-runners.py >/dev/null 2>&1)
alias_rc=$?
set -e
[[ $alias_rc -eq 1 ]]
git -C "$tmp/repo" reset -q --hard "$base"

# The exact requested set rule: a runner may not claim fewer capabilities than
# its class allowlist grants.
write_policy '["project-gate", "user-service"]'
git -C "$tmp/repo" add .
set +e
(cd "$tmp/repo" && ./scripts/run-factory-runners.py >/dev/null 2>&1)
subset_rc=$?
set -e
[[ $subset_rc -eq 1 ]]
git -C "$tmp/repo" reset -q --hard "$base"
write_policy '["project-gate"]'

# The verifier argv is root-configured: a policy/request mismatch is rejected.
write_policy '["project-gate"]' '["./scripts/other.sh"]'
set +e
(cd "$tmp/repo" && ./scripts/run-factory-runners.py >/dev/null 2>&1)
argv_rc=$?
set -e
[[ $argv_rc -eq 1 ]]
git -C "$tmp/repo" reset -q --hard "$base"
write_policy '["project-gate"]'

# A declared capability whose contract probe cannot pass is unevidenced, even
# with the class allowlist granting it.
write_policy '["project-gate", "user-service"]'
sed -i 's/\["project-gate"\]/["project-gate", "user-service"]/' \
    "$tmp/repo/.factory/environment.toml"
git -C "$tmp/repo" add .factory/environment.toml
git -C "$tmp/repo" commit -qm incomplete-user-service-contract
set +e
(cd "$tmp/repo" && ./scripts/run-factory-runners.py >/dev/null 2>&1)
user_service_contract_rc=$?
set -e
[[ $user_service_contract_rc -eq 1 ]]
git -C "$tmp/repo" reset -q --hard "$base"
write_policy '["project-gate"]'

# A skip marker inside a contract probe is never evidence.
write_policy '["project-gate", "user-service"]'
python3 - "$tmp/repo/.factory/capability-contracts.json" <<'PY'
import json, sys
path = sys.argv[1]
data = json.load(open(path))
for contract in data["capabilities"]:
    if contract["name"] == "user-service":
        contract["probe_argv"] = ["/usr/bin/printf", "factory-user-service-ok\nSkipped test\n"]
open(path, "w").write(json.dumps(data, indent=2) + "\n")
PY
sed -i 's/\["project-gate"\]/["project-gate", "user-service"]/' \
    "$tmp/repo/.factory/environment.toml"
git -C "$tmp/repo" add .
git -C "$tmp/repo" commit -qm skipped-contract-probe
set +e
(cd "$tmp/repo" && ./scripts/run-factory-runners.py >/dev/null 2>&1)
skip_probe_rc=$?
set -e
[[ $skip_probe_rc -eq 1 ]]
git -C "$tmp/repo" reset -q --hard "$base"
write_policy '["project-gate"]'

# Self-consistent local hashes cannot conceal a false archive binding.
python3 - "$tmp/repo" "$base" <<'PY'
import hashlib, json, pathlib, sys
root=pathlib.Path(sys.argv[1]); commit=sys.argv[2]
aggregate_path=root/'.factory-state/runner-evidence.json'
aggregate=json.loads(aggregate_path.read_text())
manifest_path=root/aggregate['runners'][0]['manifest']
manifest=json.loads(manifest_path.read_text()); manifest['archive_sha256']='0'*64
raw=(json.dumps(manifest,sort_keys=True,indent=2)+'\n').encode(); manifest_path.write_bytes(raw)
aggregate['runners'][0]['manifest_sha256']=hashlib.sha256(raw).hexdigest()
aggregate_path.write_text(json.dumps(aggregate,sort_keys=True,indent=2)+'\n')
PY
set +e
(cd "$tmp/repo" && ./scripts/check-factory-runner-evidence.py >/dev/null 2>&1)
archive_binding_rc=$?
set -e
[[ $archive_binding_rc -eq 1 ]]
(cd "$tmp/repo" && ./scripts/run-factory-runners.py >/dev/null)

# Evidence tampering must fail closed.
printf 'tamper\n' >> "$tmp/repo/.factory-state/runner-evidence/fake-runner/$(git -C "$tmp/repo" rev-parse HEAD)/stdout.log"
set +e
(cd "$tmp/repo" && ./scripts/check-factory-runner-evidence.py >/dev/null 2>&1)
tamper_rc=$?
set -e
[[ $tamper_rc -eq 1 ]]

# A dirty source tree cannot be transferred as if it were the recorded commit.
printf 'dirty\n' > "$tmp/repo/untracked"
set +e
(cd "$tmp/repo" && ./scripts/run-factory-runners.py >/dev/null 2>&1)
dirty_rc=$?
set -e
[[ $dirty_rc -eq 1 ]]
rm -f "$tmp/repo/untracked"

# Unsupported tracked modes fail locally instead of claiming exact transfer.
ln -s docs/SPEC.md "$tmp/repo/tracked-link"
git -C "$tmp/repo" add tracked-link
git -C "$tmp/repo" commit -qm tracked-symlink
set +e
(cd "$tmp/repo" && ./scripts/run-factory-runners.py >/dev/null 2>&1)
symlink_rc=$?
set -e
[[ $symlink_rc -eq 1 ]]
git -C "$tmp/repo" reset -q --hard HEAD^

# Excessive verifier output is terminated without unbounded buffering.
cat > "$tmp/repo/scripts/verify-boilerplate.sh" <<'EOF'
#!/usr/bin/env bash
python3 -c 'import sys; sys.stdout.write("x" * (5 * 1024 * 1024))'
EOF
chmod +x "$tmp/repo/scripts/verify-boilerplate.sh"
git -C "$tmp/repo" add scripts/verify-boilerplate.sh
git -C "$tmp/repo" commit -qm excessive-output
set +e
(cd "$tmp/repo" && ./scripts/run-factory-runners.py >/dev/null 2>&1)
output_rc=$?
set -e
[[ $output_rc -eq 1 ]]
[[ ! -e "$tmp/runner/workspaces/fake-project/job" ]]
git -C "$tmp/repo" reset -q --hard HEAD^

# A verifier failure must propagate through the real endpoint protocol.
sed -i 's/exit 0/exit 23/' "$tmp/repo/scripts/verify-boilerplate.sh"
git -C "$tmp/repo" add scripts/verify-boilerplate.sh
git -C "$tmp/repo" commit -qm failing-verifier
rm -rf "$tmp/repo/.factory-state"
set +e
(cd "$tmp/repo" && ./scripts/run-factory-runners.py >/dev/null 2>&1)
remote_rc=$?
set -e
[[ $remote_rc -eq 1 ]]
[[ ! -e "$tmp/runner/workspaces/fake-project/job" ]]

# A signer failure is fail-closed: with the private key unavailable the
# endpoint must refuse to emit any receipt, unsigned or otherwise.
sed -i 's/exit 23/exit 0/' "$tmp/repo/scripts/verify-boilerplate.sh"
git -C "$tmp/repo" add scripts/verify-boilerplate.sh
git -C "$tmp/repo" commit -qm restore-verifier
rm -rf "$tmp/repo/.factory-state" "$tmp/signer-key" "$tmp/signer-key.pub"
set +e
(cd "$tmp/repo" && ./scripts/run-factory-runners.py >/dev/null 2>&1)
signer_unavailable_rc=$?
set -e
[[ $signer_unavailable_rc -eq 1 ]]
[[ ! -e "$tmp/runner/workspaces/fake-project/job" ]]

echo "test: factory runner transfer and evidence checks passed"
