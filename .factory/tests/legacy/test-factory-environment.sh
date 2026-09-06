#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
PROJECT_ROOT=$(cd -- "$SCRIPT_DIR/../../.." && pwd)
CHECK="$PROJECT_ROOT/.factory/tools/check-factory-environment.py"

"$CHECK" "$PROJECT_ROOT/.factory/environment.toml" >/dev/null
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
cat > "$tmp/valid.toml" <<'EOF'
schema_version = 1
[[tools]]
name = "example-tool"
command = "example-tool"
capabilities = ["example-capability"]
[[runners]]
name = "hardware"
transport = "ssh"
ssh_config_alias = "boilerplate-vm"
working_directory = "/srv/factory-runner/workspaces/hardware"
capabilities = ["example-capability", "other-capability"]
verify_argv = ["./.factory/tools/verify-boilerplate.sh"]
EOF
"$CHECK" "$tmp/valid.toml" >/dev/null
for field in 'password = "bad"' 'host = "10.0.0.2"' 'private_key = "/tmp/key"'; do
    cp "$tmp/valid.toml" "$tmp/bad.toml"
    printf '\n%s\n' "$field" >> "$tmp/bad.toml"
    if "$CHECK" "$tmp/bad.toml" >/dev/null 2>&1; then
        echo "test: environment validator accepted forbidden field: $field" >&2
        exit 1
    fi
done
cat > "$tmp/sensitive-argv.toml" <<'EOF'
schema_version = 1
[[runners]]
name = "bad"
transport = "ssh"
ssh_config_alias = "boilerplate-vm"
working_directory = "/srv/dev-runner/workspaces/bad"
capabilities = ["test"]
verify_argv = ["runner", "--token", "secret-value"]
EOF
if "$CHECK" "$tmp/sensitive-argv.toml" >/dev/null 2>&1; then
    echo "test: environment validator accepted credential-bearing runner argv" >&2
    exit 1
fi
cat > "$tmp/url.toml" <<'EOF'
schema_version = 1
[[tools]]
name = "bad"
command = "https://user:pass@example.invalid/tool"
capabilities = ["test"]
EOF
if "$CHECK" "$tmp/url.toml" >/dev/null 2>&1; then
    echo "test: environment validator accepted embedded URL credentials" >&2
    exit 1
fi
for mutation in bad-workdir bad-alias duplicate-capability shell-argv; do
    cp "$tmp/valid.toml" "$tmp/mutation.toml"
    case "$mutation" in
        bad-workdir) sed -i 's#/srv/factory-runner/workspaces/hardware#/srv/factory-runner/workspaces/../escape#' "$tmp/mutation.toml" ;;
        bad-alias) sed -i 's/ssh_config_alias = "boilerplate-vm"/ssh_config_alias = "10.0.0.2"/' "$tmp/mutation.toml" ;;
        duplicate-capability) sed -i 's/\["example-capability", "other-capability"\]/["other-capability", "other-capability"]/' "$tmp/mutation.toml" ;;
        shell-argv) sed -i 's#\["./.factory/tools/verify-boilerplate.sh"\]#["./.factory/tools/verify-boilerplate.sh", "; touch /tmp/pwned"]#' "$tmp/mutation.toml" ;;
    esac
    if "$CHECK" "$tmp/mutation.toml" >/dev/null 2>&1; then
        echo "test: environment validator accepted $mutation" >&2
        exit 1
    fi
done
echo "test: factory environment declaration checks passed"
