#!/usr/bin/env bash
# Adversarial runner-receipt signer verification: capability evidence that
# requires runner trust must reject unsigned legacy/local manifests. Public
# keys/config live in the repository; private signing stays out-of-tree.
# Signed fixtures are accepted with ephemeral test keys; unsigned and
# fabricated signatures are rejected.
set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
PROJECT_ROOT=$(cd -- "$SCRIPT_DIR/.." && pwd)
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT

CHECKER="$PROJECT_ROOT/scripts/check-factory-runner-evidence.py"

# Ephemeral test keys (never committed; private key is out-of-tree).
ssh-keygen -q -t ed25519 -N '' -f "$tmp/signer-key"
PUBLIC_KEY=$(cut -d' ' -f1,2 "$tmp/signer-key.pub")

DISABLED=$(python3 - <<'PY'
import json
print(json.dumps({
    "schema": "ralph-runner-signer-trust/v1",
    "description": "test fixture",
    "require_signature": True,
    "enabled": False,
    "namespace": "factory-runner-receipt",
    "public_keys": [],
    "allowed_principals": [],
}))
PY
)
ENABLED=$(python3 - "$PUBLIC_KEY" <<'PY'
import json, sys
public_key = sys.argv[1]
print(json.dumps({
    "schema": "ralph-runner-signer-trust/v1",
    "description": "test fixture",
    "require_signature": True,
    "enabled": True,
    "namespace": "factory-runner-receipt",
    "public_keys": [{"principal": "factory-signer", "public_key": public_key}],
    "allowed_principals": ["factory-signer"],
}))
PY
)

setup_repo() {
    local dir=$1 trust=$2
    mkdir -p "$dir/scripts" "$dir/docs" "$dir/.factory" "$dir/.factory-state"
    cp "$CHECKER" "$dir/scripts/"
    cp "$PROJECT_ROOT/scripts/check-factory-environment.py" "$dir/scripts/"
    chmod +x "$dir/scripts/"*.py
    cat > "$dir/.factory/environment.toml" <<'EOF'
schema_version = 1
[[runners]]
name = "fake-runner"
transport = "ssh"
ssh_config_alias = "fake-runner"
working_directory = "/srv/dev-runner/workspaces/fake-project"
capabilities = ["project-gate"]
verify_argv = ["./scripts/verify-boilerplate.sh"]
EOF
    printf '# Spec\n' > "$dir/docs/SPEC.md"
    printf '%s\n' ".factory-state/" > "$dir/.gitignore"
    printf '%s\n' "$trust" > "$dir/.factory/signer-trust.json"
    git -C "$dir" init -q -b develop
    git -C "$dir" config user.name test
    git -C "$dir" config user.email test@example.invalid
    git -C "$dir" add .
    git -C "$dir" commit -qm base
}

# write_evidence <dir> <head>: a manifest with the exact commit-bound bindings the
# checker recomputes (tree, environment blob, verifier argv digest, git archive
# hash) so signature verification is the only variable under test.
write_evidence() {
    local dir=$1 head=$2
    mkdir -p "$dir/.factory-state/runner-evidence/fake-runner/$head"
    python3 - "$dir" "$head" <<'PY'
import hashlib, json, pathlib, subprocess, sys
root, head = pathlib.Path(sys.argv[1]), sys.argv[2]

def git(*args: str) -> str:
    result = subprocess.run(["git", *args], cwd=root, text=True, capture_output=True)
    if result.returncode:
        raise SystemExit(f"git {args} failed: {result.stderr}")
    return result.stdout.strip()

tree = git("rev-parse", f"{head}^{{tree}}")
environment_blob = git("rev-parse", f"{head}:.factory/environment.toml")
argv_digest = hashlib.sha256(json.dumps(["./scripts/verify-boilerplate.sh"], separators=(",", ":")).encode()).hexdigest()
archive = subprocess.run(
    ["git", "archive", "--format=tar", "--output", str(root / "commit-archive.tar"), head],
    cwd=root, capture_output=True,
)
if archive.returncode:
    raise SystemExit("cannot archive fixture commit")
archive_sha256 = hashlib.sha256((root / "commit-archive.tar").read_bytes()).hexdigest()
(root / "commit-archive.tar").unlink()
empty = hashlib.sha256(b"").hexdigest()
manifest = {
    "schema": "factory-runner-receipt/v1", "result": "pass", "runner": "fake-runner",
    "commit": head, "tree": tree, "environment_blob": environment_blob,
    "verify_argv_sha256": argv_digest, "archive_sha256": archive_sha256, "nonce": "0" * 64,
    "capabilities": ["project-gate"], "exit_code": 0, "timed_out": False,
    "started_at": 1, "finished_at": 2, "cleanup": True,
    "stdout_sha256": empty, "stderr_sha256": empty,
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
         "manifest_sha256": hashlib.sha256(raw).hexdigest(), "capabilities": ["project-gate"]}
    ],
}
(root / ".factory-state/runner-evidence.json").write_text(json.dumps(aggregate, sort_keys=True, indent=2) + "\n")
(root / f".factory-state/runner-evidence/fake-runner/{head}/stdout.log").write_bytes(b"")
(root / f".factory-state/runner-evidence/fake-runner/{head}/stderr.log").write_bytes(b"")
PY
}

sign() {
    local dir=$1 head=$2 key=$3
    cat "$dir/.factory-state/runner-evidence/fake-runner/$head/manifest.json" \
        | ssh-keygen -Y sign -f "$key" -n factory-runner-receipt \
            > "$dir/.factory-state/runner-evidence/fake-runner/$head/manifest.sig"
}

expect() {
    local dir=$1 expected=$2 label=$3
    set +e
    (cd "$dir" && ./scripts/check-factory-runner-evidence.py >/dev/null 2>&1)
    local rc=$?
    set -e
    [[ $rc -eq $expected ]] || {
        echo "test: runner-signer $label (expected rc=$expected, got rc=$rc)" >&2
        exit 1
    }
}

# Unsigned legacy/local manifest with no signer provisioned: rejected.
setup_repo "$tmp/unsigned" "$DISABLED"
head=$(git -C "$tmp/unsigned" rev-parse HEAD)
write_evidence "$tmp/unsigned" "$head"
expect "$tmp/unsigned" 1 "unsigned manifest without a provisioned signer"

# Unsigned manifest while a signer IS provisioned: missing signature rejected.
setup_repo "$tmp/missing-sig" "$ENABLED"
head=$(git -C "$tmp/missing-sig" rev-parse HEAD)
write_evidence "$tmp/missing-sig" "$head"
expect "$tmp/missing-sig" 1 "unsigned manifest with signer provisioned"

# Fabricated signature (garbage .sig): rejected.
setup_repo "$tmp/fabricated" "$ENABLED"
head=$(git -C "$tmp/fabricated" rev-parse HEAD)
write_evidence "$tmp/fabricated" "$head"
printf 'not-a-real-signature\n' > "$tmp/fabricated/.factory-state/runner-evidence/fake-runner/$head/manifest.sig"
expect "$tmp/fabricated" 1 "fabricated signature"

# A signature from an unknown key (not in the trust store): rejected.
setup_repo "$tmp/unknown-key" "$ENABLED"
head=$(git -C "$tmp/unknown-key" rev-parse HEAD)
write_evidence "$tmp/unknown-key" "$head"
ssh-keygen -q -t ed25519 -N '' -f "$tmp/other-key"
sign "$tmp/unknown-key" "$head" "$tmp/other-key"
expect "$tmp/unknown-key" 1 "signature from an unknown key"

# Valid signed manifest with the provisioned ephemeral key: accepted.
setup_repo "$tmp/signed" "$ENABLED"
head=$(git -C "$tmp/signed" rev-parse HEAD)
write_evidence "$tmp/signed" "$head"
sign "$tmp/signed" "$head" "$tmp/signer-key"
expect "$tmp/signed" 0 "valid signed runner manifest"

# Tampering the manifest after signing invalidates the signature.
setup_repo "$tmp/tampered" "$ENABLED"
head=$(git -C "$tmp/tampered" rev-parse HEAD)
write_evidence "$tmp/tampered" "$head"
sign "$tmp/tampered" "$head" "$tmp/signer-key"
printf 'tamper\n' >> "$tmp/tampered/.factory-state/runner-evidence/fake-runner/$head/manifest.json"
expect "$tmp/tampered" 1 "tampered manifest after signing"

echo "test: runner signer verification checks passed"
