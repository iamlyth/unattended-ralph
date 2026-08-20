#!/usr/bin/env bash
# Adversarial runner-receipt signer verification: capability evidence that
# requires runner trust must reject unsigned legacy/local manifests, fabricated
# signatures, unknown or rotated keys, wrong namespaces/principals, stale
# commit bindings, and failure/skip receipts. The root-owned signer (a separate
# out-of-tree helper) never signs caller-provided bytes: it rebuilds and signs
# only manifests whose fields prove a clean pass, and it fails closed on
# unsafe/absent private-key state. Public keys/config live in the repository;
# private signing stays out-of-tree.
set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
PROJECT_ROOT=$(cd -- "$SCRIPT_DIR/.." && pwd)
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT

CHECKER="$PROJECT_ROOT/scripts/check-factory-runner-evidence.py"
SIGNER="$PROJECT_ROOT/scripts/factory-runner-signer.py"

# Ephemeral test keys (never committed; private keys are out-of-tree).
ssh-keygen -q -t ed25519 -N '' -f "$tmp/signer-key"
PUBLIC_KEY=$(cut -d' ' -f1,2 "$tmp/signer-key.pub")
ssh-keygen -q -t ed25519 -N '' -f "$tmp/other-key"
OTHER_PUBLIC_KEY=$(cut -d' ' -f1,2 "$tmp/other-key.pub")

PUBKEY_SHA256=$(python3 - "$PUBLIC_KEY" <<'PY'
import hashlib, sys
print(hashlib.sha256(sys.argv[1].encode()).hexdigest())
PY
)

trust_json() {
    python3 - "$@" <<'PY'
import json, sys
print(json.dumps({
    "schema": "ralph-runner-signer-trust/v1",
    "description": "test fixture",
    "require_signature": True,
    "enabled": json.loads(sys.argv[3]),
    "namespace": "factory-runner-receipt",
    "public_keys": json.loads(sys.argv[1]),
    "allowed_principals": json.loads(sys.argv[2]),
}))
PY
}

DISABLED=$(trust_json '[]' '[]' false)
ENABLED=$(trust_json "[{\"principal\":\"factory-signer\",\"public_key\":\"$PUBLIC_KEY\"}]" '["factory-signer"]' true)
UNKNOWN=$(trust_json "[{\"principal\":\"factory-signer\",\"public_key\":\"$OTHER_PUBLIC_KEY\"}]" '["factory-signer"]' true)
WRONG_PRINCIPAL=$(trust_json "[{\"principal\":\"factory-signer\",\"public_key\":\"$PUBLIC_KEY\"}]" '["other-signer"]' true)
ROTATED_BOTH=$(trust_json "[{\"principal\":\"factory-signer\",\"public_key\":\"$PUBLIC_KEY\"},{\"principal\":\"factory-signer\",\"public_key\":\"$OTHER_PUBLIC_KEY\"}]" '["factory-signer"]' true)

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

# write_evidence <dir> <head>: a manifest with the exact commit-bound bindings
# the checker recomputes (tree, environment blob, verifier argv digest, git
# archive hash) plus the signer identity fields the root signer would bind, so
# signature/principal/namespace/rotation are the variables under test.
write_evidence() {
    local dir=$1 head=$2
    mkdir -p "$dir/.factory-state/runner-evidence/fake-runner/$head"
    python3 - "$dir" "$head" "$PUBKEY_SHA256" <<'PY'
import hashlib, json, pathlib, subprocess, sys
root, head, key_sha256 = pathlib.Path(sys.argv[1]), sys.argv[2], sys.argv[3]

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
         "manifest_sha256": hashlib.sha256(raw).hexdigest(), "capabilities": ["project-gate"],
         "signer": {"principal": "factory-signer", "key_sha256": key_sha256,
                    "algorithm": "ssh-ed25519", "signature_sha256": ""}}
    ],
}
(root / ".factory-state/runner-evidence.json").write_text(json.dumps(aggregate, sort_keys=True, indent=2) + "\n")
(root / f".factory-state/runner-evidence/fake-runner/{head}/stdout.log").write_bytes(b"")
(root / f".factory-state/runner-evidence/fake-runner/{head}/stderr.log").write_bytes(b"")
PY
}

# sign <dir> <head> <key> [namespace]: sign the manifest and register the
# detached signature (and its digest) in the aggregate metadata.
sign() {
    local dir=$1 head=$2 key=$3 namespace=${4:-factory-runner-receipt}
    local sig="$dir/.factory-state/runner-evidence/fake-runner/$head/manifest.sig"
    cat "$dir/.factory-state/runner-evidence/fake-runner/$head/manifest.json" \
        | ssh-keygen -Y sign -f "$key" -n "$namespace" \
            > "$sig" 2>/dev/null
    python3 - "$dir/.factory-state/runner-evidence.json" "$sig" <<'PY'
import hashlib, json, pathlib, sys
aggregate_path, sig_path = pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2])
aggregate = json.loads(aggregate_path.read_text())
aggregate["runners"][0]["signer"]["signature_sha256"] = hashlib.sha256(sig_path.read_bytes()).hexdigest()
aggregate_path.write_text(json.dumps(aggregate, sort_keys=True, indent=2) + "\n")
PY
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

# --- Checker adversarial cases -------------------------------------------------

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
python3 - "$tmp/fabricated/.factory-state/runner-evidence.json" <<'PY'
import hashlib, json, pathlib, sys
path = pathlib.Path(sys.argv[1])
aggregate = json.loads(path.read_text())
aggregate["runners"][0]["signer"]["signature_sha256"] = hashlib.sha256(b"not-a-real-signature\n").hexdigest()
path.write_text(json.dumps(aggregate, sort_keys=True, indent=2) + "\n")
PY
expect "$tmp/fabricated" 1 "fabricated signature"

# A signature from an unknown key (not in the trust store): rejected.
setup_repo "$tmp/unknown-key" "$UNKNOWN"
head=$(git -C "$tmp/unknown-key" rev-parse HEAD)
write_evidence "$tmp/unknown-key" "$head"
sign "$tmp/unknown-key" "$head" "$tmp/other-key"
expect "$tmp/unknown-key" 1 "signature from an unknown key"

# A manifest claiming a foreign signer key digest: rejected even with a valid
# signature from the provisioned key.
setup_repo "$tmp/wrong-key-digest" "$ENABLED"
head=$(git -C "$tmp/wrong-key-digest" rev-parse HEAD)
write_evidence "$tmp/wrong-key-digest" "$head"
sign "$tmp/wrong-key-digest" "$head" "$tmp/signer-key"
python3 - "$tmp/wrong-key-digest" "$head" <<'PY'
import json, pathlib, sys
root, head = pathlib.Path(sys.argv[1]), sys.argv[2]
manifest_path = root / f".factory-state/runner-evidence/fake-runner/{head}/manifest.json"
aggregate_path = root / ".factory-state/runner-evidence.json"
manifest = json.loads(manifest_path.read_text())
manifest["signer_key_sha256"] = "1" * 64
manifest_path.write_text(json.dumps(manifest, sort_keys=True, indent=2) + "\n")
aggregate = json.loads(aggregate_path.read_text())
aggregate["runners"][0]["signer"]["key_sha256"] = "1" * 64
aggregate_path.write_text(json.dumps(aggregate, sort_keys=True, indent=2) + "\n")
PY
expect "$tmp/wrong-key-digest" 1 "wrong signer key digest"

# A manifest signed by the provisioned key but claiming a wrong principal:
# rejected (principal binding).
setup_repo "$tmp/wrong-principal" "$WRONG_PRINCIPAL"
head=$(git -C "$tmp/wrong-principal" rev-parse HEAD)
write_evidence "$tmp/wrong-principal" "$head"
sign "$tmp/wrong-principal" "$head" "$tmp/signer-key"
python3 - "$tmp/wrong-principal" "$head" <<'PY'
import json, pathlib, sys
root, head = pathlib.Path(sys.argv[1]), sys.argv[2]
manifest_path = root / f".factory-state/runner-evidence/fake-runner/{head}/manifest.json"
aggregate_path = root / ".factory-state/runner-evidence.json"
manifest = json.loads(manifest_path.read_text())
manifest["signer_principal"] = "other-signer"
manifest_path.write_text(json.dumps(manifest, sort_keys=True, indent=2) + "\n")
aggregate = json.loads(aggregate_path.read_text())
aggregate["runners"][0]["signer"]["principal"] = "other-signer"
aggregate_path.write_text(json.dumps(aggregate, sort_keys=True, indent=2) + "\n")
PY
expect "$tmp/wrong-principal" 1 "wrong signer principal"

# A signature over the wrong namespace: rejected.
setup_repo "$tmp/wrong-namespace" "$ENABLED"
head=$(git -C "$tmp/wrong-namespace" rev-parse HEAD)
write_evidence "$tmp/wrong-namespace" "$head"
sign "$tmp/wrong-namespace" "$head" "$tmp/signer-key" other-namespace
expect "$tmp/wrong-namespace" 1 "wrong signature namespace"

# Tampering the manifest after signing invalidates the signature.
setup_repo "$tmp/tampered" "$ENABLED"
head=$(git -C "$tmp/tampered" rev-parse HEAD)
write_evidence "$tmp/tampered" "$head"
sign "$tmp/tampered" "$head" "$tmp/signer-key"
printf 'tamper\n' >> "$tmp/tampered/.factory-state/runner-evidence/fake-runner/$head/manifest.json"
expect "$tmp/tampered" 1 "tampered manifest after signing"

# A manifest bound to a stale commit (the aggregate claims a newer commit while
# the manifest still names the old one): rejected.
setup_repo "$tmp/stale-commit" "$ENABLED"
head=$(git -C "$tmp/stale-commit" rev-parse HEAD)
write_evidence "$tmp/stale-commit" "$head"
sign "$tmp/stale-commit" "$head" "$tmp/signer-key"
printf 'change\n' > "$tmp/stale-commit/extra.txt"
git -C "$tmp/stale-commit" add extra.txt
git -C "$tmp/stale-commit" commit -qm newer-commit
newhead=$(git -C "$tmp/stale-commit" rev-parse HEAD)
python3 - "$tmp/stale-commit" "$newhead" <<'PY'
import json, pathlib, subprocess, sys
root, newhead = pathlib.Path(sys.argv[1]), sys.argv[2]
tree = subprocess.run(["git", "rev-parse", f"{newhead}^{{tree}}"], cwd=root,
                      capture_output=True, text=True).stdout.strip()
environment_blob = subprocess.run(["git", "rev-parse", f"{newhead}:.factory/environment.toml"], cwd=root,
                                  capture_output=True, text=True).stdout.strip()
path = root / ".factory-state/runner-evidence.json"
aggregate = json.loads(path.read_text())
aggregate["commit"] = newhead
aggregate["tree"] = tree
aggregate["environment_blob"] = environment_blob
path.write_text(json.dumps(aggregate, sort_keys=True, indent=2) + "\n")
PY
expect "$tmp/stale-commit" 1 "stale-commit manifest"

# Failure/skip receipts can never be evidence: result=fail, timed out, and
# cleanup=false manifests are all rejected even when signed.
for variant in fail-result timed-out cleanup; do
    setup_repo "$tmp/$variant" "$ENABLED"
    head=$(git -C "$tmp/$variant" rev-parse HEAD)
    write_evidence "$tmp/$variant" "$head"
    sign "$tmp/$variant" "$head" "$tmp/signer-key"
    python3 - "$tmp/$variant" "$head" "$variant" <<'PY'
import json, pathlib, sys
root, head, variant = pathlib.Path(sys.argv[1]), sys.argv[2], sys.argv[3]
path = root / f".factory-state/runner-evidence/fake-runner/{head}/manifest.json"
manifest = json.loads(path.read_text())
if variant == "fail-result":
    manifest["result"] = "fail"
elif variant == "timed-out":
    manifest["timed_out"] = True
else:
    manifest["cleanup"] = False
path.write_text(json.dumps(manifest, sort_keys=True, indent=2) + "\n")
PY
    expect "$tmp/$variant" 1 "failure/skip variant $variant cannot be evidence"
done

# Signer rotation is fail-closed: a key still in the trust store verifies;
# once rotated out, the same receipt is rejected.
setup_repo "$tmp/rotation-old" "$ROTATED_BOTH"
head=$(git -C "$tmp/rotation-old" rev-parse HEAD)
write_evidence "$tmp/rotation-old" "$head"
sign "$tmp/rotation-old" "$head" "$tmp/signer-key"
expect "$tmp/rotation-old" 0 "old key while rotation window is open"
python3 - "$tmp/rotation-old" "$PUBLIC_KEY" <<'PY'
import json, pathlib, sys
root = pathlib.Path(sys.argv[1])
public_key = sys.argv[2]
path = root / ".factory/signer-trust.json"
trust = json.loads(path.read_text())
trust["public_keys"] = [entry for entry in trust["public_keys"] if entry["public_key"] != public_key]
path.write_text(json.dumps(trust, sort_keys=True, indent=2) + "\n")
PY
expect "$tmp/rotation-old" 1 "rotated-out key is rejected"

# Valid signed manifest with the provisioned ephemeral key: accepted.
setup_repo "$tmp/signed" "$ENABLED"
head=$(git -C "$tmp/signed" rev-parse HEAD)
write_evidence "$tmp/signed" "$head"
sign "$tmp/signed" "$head" "$tmp/signer-key"
expect "$tmp/signed" 0 "valid signed runner manifest"

# --- Root-owned signer helper adversarial cases (never signs caller bytes) ---

helper_dir="$tmp/helper"
mkdir -p "$helper_dir"
cp "$SIGNER" "$helper_dir/factory-runner-signer.py"
cp "$PROJECT_ROOT/scripts/factory_runner_policy.py" "$helper_dir/"
chmod +x "$helper_dir/factory-runner-signer.py"
# The disposable harness relaxes only the root-identity checks (the fixture
# runs unprivileged) and points the private key/principal paths at ephemeral
# out-of-tree state. Mode/symlink/missing fail-closed checks stay verbatim.
sed -i \
    -e 's/if os.getuid() != os.geteuid() or os.geteuid() != 0:/if False:/' \
    -e 's/if key_stat.st_uid != 0 or key_stat.st_mode & 0o077:/if key_stat.st_mode \& 0o077:/' \
    -e 's/if principal_stat.st_uid != 0 or principal_stat.st_mode & 0o077:/if principal_stat.st_mode \& 0o077:/' \
    "$helper_dir/factory-runner-signer.py"

printf 'factory-signer\n' > "$tmp/signer-principal"
chmod 0600 "$tmp/signer-principal"
chmod 0600 "$tmp/signer-key"
# The signer's capability policy is root-configured: the harness installs a
# fixture policy whose class grants exactly the capability the valid request
# claims, so the signer enforces the class allowlist even in harness mode.
python3 - "$tmp" <<'PY' > "$tmp/policy.json"
import json, os, sys
print(json.dumps({
    "schema": "factory-runner-policy/v1",
    "namespace": "factory-runner-receipt",
    "classes": [
        {
            "name": "factory-signer",
            "uid": os.getuid(),
            "workspace_root": "/srv/dev-runner/workspaces",
            "verify_argv": ["./scripts/verify-boilerplate.sh"],
            "allowed_capabilities": ["project-gate"],
            "signer_helper": "/usr/local/libexec/factory-runner-signer",
            "signer_key": f"{sys.argv[1]}/signer-key",
            "signer_principal_file": f"{sys.argv[1]}/signer-principal",
        }
    ],
}))
PY

valid_request() {
    python3 - "$PUBKEY_SHA256" <<'PY'
import hashlib, json, sys
manifest = {
    "schema": "factory-runner-receipt/v1", "result": "pass", "runner": "factory-signer",
    "commit": "a" * 40, "tree": "b" * 40, "environment_blob": "c" * 40,
    "verify_argv_sha256": hashlib.sha256(b"x").hexdigest(),
    "archive_sha256": hashlib.sha256(b"y").hexdigest(), "nonce": "0" * 64,
    "capabilities": ["project-gate"], "exit_code": 0, "timed_out": False,
    "started_at": 1, "finished_at": 2, "cleanup": True,
    "stdout_sha256": hashlib.sha256(b"").hexdigest(),
    "stderr_sha256": hashlib.sha256(b"").hexdigest(),
}
print(json.dumps({"schema": "factory-runner-sign-request/v1", "manifest": manifest}))
PY
}

helper_run() {
    local key_path=$1 principal_path=$2
    shift 2
    set +e
    FACTORY_SIGNER_KEY="$key_path" FACTORY_SIGNER_PRINCIPAL_FILE="$principal_path" \
        FACTORY_SIGNER_CLASS=factory-signer FACTORY_RUNNER_POLICY="$tmp/policy.json" \
        python3 -I "$helper_dir/factory-runner-signer.py" \
        >"$tmp/helper-out" 2>"$tmp/helper-err"
    local rc=$?
    set -e
    echo $rc
}

# A valid clean-pass request under a healthy key is signed; the returned
# detached signature verifies under the provisioned public key and the signed
# manifest carries the signer identity binding. The private key never appears
# in the response.
rc=$(valid_request | helper_run "$tmp/signer-key" "$tmp/signer-principal")
[[ $rc -eq 0 ]] || { echo "test: runner-signer helper rejected a valid request (rc=$rc)" >&2; exit 1; }
python3 - "$PUBLIC_KEY" "$tmp/helper-out" <<'PY'
import base64, hashlib, json, pathlib, subprocess, sys
public_key, out_path = sys.argv[1], pathlib.Path(sys.argv[2])
response = json.loads(out_path.read_text())
assert response["result"] == "signed"
manifest = json.loads(base64.b64decode(response["manifest_b64"]))
assert manifest["signer_principal"] == "factory-signer"
assert manifest["signer_key_sha256"] == hashlib.sha256(public_key.encode()).hexdigest()
assert manifest["namespace"] == "factory-runner-receipt"
assert manifest["signature_algorithm"] == "ssh-ed25519"
assert response["signature_sha256"] == hashlib.sha256(base64.b64decode(response["signature_b64"])).hexdigest()
allowed = pathlib.Path(out_path.parent) / "allowed-signers"
allowed.write_text(f"factory-signer {public_key}\n")
with open(out_path.parent / "signature.sig", "wb") as stream:
    stream.write(base64.b64decode(response["signature_b64"]))
with open(out_path.parent / "manifest.json", "wb") as stream:
    stream.write(base64.b64decode(response["manifest_b64"]))
verified = subprocess.run(
    ["ssh-keygen", "-Y", "verify", "-f", str(allowed), "-I", "factory-signer",
     "-n", "factory-runner-receipt", "-s", str(out_path.parent / "signature.sig")],
    input=base64.b64decode(response["manifest_b64"]), capture_output=True,
)
assert verified.returncode == 0, verified.stderr
assert b"OPENSSH PRIVATE KEY" not in out_path.read_bytes()
PY

# A caller-supplied manifest blob is never signed: an opaque request, a wrong
# schema, an extra caller field, and caller-supplied signer identity are all
# rejected without producing a signature. A capability outside the class
# allowlist (even one the old hardcoded list happened to reject differently)
# is also never signed.
rc=$(printf '%s\n' '{"schema":"factory-runner-sign-request/v1","manifest":"not-a-dict"}' \
    | helper_run "$tmp/signer-key" "$tmp/signer-principal")
[[ $rc -eq 1 ]] || { echo "test: signer signed an opaque caller manifest" >&2; exit 1; }
rc=$(valid_request | sed 's/"schema": "factory-runner-sign-request\/v1"/"schema": "wrong-schema"/' \
    | helper_run "$tmp/signer-key" "$tmp/signer-principal")
[[ $rc -eq 1 ]] || { echo "test: signer accepted a wrong request schema" >&2; exit 1; }
rc=$(valid_request | sed 's/"capabilities": \["project-gate"\]/"capabilities": ["project-gate"], "signer_principal": "intruder"/' \
    | helper_run "$tmp/signer-key" "$tmp/signer-principal")
[[ $rc -eq 1 ]] || { echo "test: signer accepted caller-supplied signer identity" >&2; exit 1; }

# Failures, skips, and capability claims outside the class allowlist can never
# be signed.
for broken in \
    '"result": "fail"' \
    '"exit_code": 3' \
    '"timed_out": true' \
    '"cleanup": false' \
    '"capabilities": ["installed-runtime"]' \
    '"runner": "intruder-runner"'; do
    rc=$(valid_request | sed "s/\"result\": \"pass\"/$broken/" \
        | helper_run "$tmp/signer-key" "$tmp/signer-principal")
    [[ $rc -eq 1 ]] || { echo "test: signer signed a rejected claim ($broken)" >&2; exit 1; }
done

# Unprivileged key state is fail-closed: a symlinked key, a world-readable
# key, a missing key, a missing principal, and a world-readable principal are
# all refused, and no signature is produced.
ln -s "$tmp/signer-key" "$tmp/key-link"
rc=$(valid_request | helper_run "$tmp/key-link" "$tmp/signer-principal")
[[ $rc -eq 1 ]] || { echo "test: signer accepted a symlinked key" >&2; exit 1; }
chmod 0644 "$tmp/signer-key"
rc=$(valid_request | helper_run "$tmp/signer-key" "$tmp/signer-principal")
[[ $rc -eq 1 ]] || { echo "test: signer accepted a world-readable key" >&2; exit 1; }
chmod 0600 "$tmp/signer-key"
rc=$(valid_request | helper_run "$tmp/missing-key" "$tmp/signer-principal")
[[ $rc -eq 1 ]] || { echo "test: signer accepted a missing key" >&2; exit 1; }
rc=$(valid_request | helper_run "$tmp/signer-key" "$tmp/missing-principal")
[[ $rc -eq 1 ]] || { echo "test: signer accepted a missing principal" >&2; exit 1; }
chmod 0644 "$tmp/signer-principal"
rc=$(valid_request | helper_run "$tmp/signer-key" "$tmp/signer-principal")
[[ $rc -eq 1 ]] || { echo "test: signer accepted a world-readable principal" >&2; exit 1; }
chmod 0600 "$tmp/signer-principal"
if grep -q 'OPENSSH PRIVATE KEY' "$tmp/helper-out" "$tmp/helper-err" 2>/dev/null; then
    echo "test: signer leaked private key material" >&2
    exit 1
fi

echo "test: runner signer adversarial verification checks passed"
