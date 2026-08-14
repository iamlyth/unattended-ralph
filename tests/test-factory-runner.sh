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
   "$PROJECT_ROOT/scripts/factory-runner-server.py" "$tmp/repo/scripts/"
chmod +x "$tmp/repo/scripts/"*.py
cat > "$tmp/repo/.factory/environment.toml" <<EOF
schema_version = 1
[[runners]]
name = "fake-runner"
transport = "ssh"
ssh_config_alias = "fake-runner"
working_directory = "$tmp/runner/workspaces/fake-project"
capabilities = ["remote-project-gate"]
verify_argv = ["./scripts/verify-project.sh"]
EOF
cat > "$tmp/repo/scripts/verify-project.sh" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
[[ $(git rev-parse HEAD) =~ ^[0-9a-f]{40}$ ]]
[[ -z $(git status --porcelain --untracked-files=normal) ]]
exit 0
EOF
chmod +x "$tmp/repo/scripts/verify-project.sh"
printf '# Spec\n' > "$tmp/repo/docs/SPEC.md"
cat > "$tmp/repo/.gitignore" <<'EOF'
.factory-state/
EOF
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
# Validator bypass is confined to this disposable copied test harness because
# its temporary workspace intentionally does not use the production /srv root.
sed -i '0,/if subprocess.run(/s//if False and subprocess.run(/' "$tmp/repo/scripts/run-factory-runners.py"
sed -i '0,/if subprocess.run(/s//if False and subprocess.run(/' "$tmp/repo/scripts/check-factory-runner-evidence.py"
sed -i "s#ROOT = Path(\"/srv/dev-runner/workspaces\")#ROOT = Path(\"$tmp/runner/workspaces\")#" "$tmp/repo/scripts/factory-runner-server.py"
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
    ./scripts/check-factory-runner-evidence.py >/dev/null
    capabilities=$(./scripts/check-factory-runner-evidence.py --print-capabilities)
    grep -qx 'remote-project-gate' <<<"$capabilities"
)
base=$(git -C "$tmp/repo" rev-parse HEAD)
# Historical validation must use the selected commit's declaration, not the
# current checkout's unrelated environment file.
sed -i 's/name = "fake-runner"/name = "other-runner"/' "$tmp/repo/.factory/environment.toml"
git -C "$tmp/repo" add .factory/environment.toml
git -C "$tmp/repo" commit -qm unrelated-environment
(cd "$tmp/repo" && ./scripts/check-factory-runner-evidence.py --expected-commit "$base" >/dev/null)
git -C "$tmp/repo" reset -q --hard "$base"

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
cat > "$tmp/repo/scripts/verify-project.sh" <<'EOF'
#!/usr/bin/env bash
python3 -c 'import sys; sys.stdout.write("x" * (5 * 1024 * 1024))'
EOF
chmod +x "$tmp/repo/scripts/verify-project.sh"
git -C "$tmp/repo" add scripts/verify-project.sh
git -C "$tmp/repo" commit -qm excessive-output
set +e
(cd "$tmp/repo" && ./scripts/run-factory-runners.py >/dev/null 2>&1)
output_rc=$?
set -e
[[ $output_rc -eq 1 ]]
[[ ! -e "$tmp/runner/workspaces/fake-project/job" ]]
git -C "$tmp/repo" reset -q --hard HEAD^

# A verifier failure must propagate through the real endpoint protocol.
sed -i 's/exit 0/exit 23/' "$tmp/repo/scripts/verify-project.sh"
git -C "$tmp/repo" add scripts/verify-project.sh
git -C "$tmp/repo" commit -qm failing-verifier
rm -rf "$tmp/repo/.factory-state"
set +e
(cd "$tmp/repo" && ./scripts/run-factory-runners.py >/dev/null 2>&1)
remote_rc=$?
set -e
[[ $remote_rc -eq 1 ]]
[[ ! -e "$tmp/runner/workspaces/fake-project/job" ]]

echo "test: factory runner transfer and evidence checks passed"
