#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
PROJECT_ROOT=$(cd -- "$SCRIPT_DIR/.." && pwd)
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
mkdir -p "$tmp/scripts" "$tmp/.ralph/agent" "$tmp/.factory/artifacts"
cp "$PROJECT_ROOT/scripts/initialize-campaign-audit.py" \
   "$PROJECT_ROOT/scripts/validate-campaign-audit.py" \
   "$PROJECT_ROOT/scripts/run-factory-runners.py" \
   "$PROJECT_ROOT/scripts/check-factory-runner-evidence.py" \
   "$PROJECT_ROOT/scripts/check-factory-environment.py" "$tmp/scripts/"
chmod +x "$tmp/scripts/"*
cat > "$tmp/.factory/environment.toml" <<'EOF'
schema_version = 1
EOF
cat > "$tmp/.factory/config.toml" <<'EOF'
[project]
development_branch = "develop"
[campaign]
required_capabilities = []
EOF
cat > "$tmp/.factory/signer-trust.json" <<'EOF'
{
  "schema": "ralph-runner-signer-trust/v1",
  "description": "test fixture: no signer provisioned",
  "require_signature": true,
  "enabled": false,
  "namespace": "factory-runner-receipt",
  "public_keys": [],
  "allowed_principals": []
}
EOF
cat > "$tmp/.factory/artifacts/implementation-plan.md" <<'EOF'
---
status: complete
---
# Plan
EOF
printf '# Scratch\n' > "$tmp/.ralph/agent/scratchpad.md"
printf '# Audit\n' > "$tmp/.factory/artifacts/campaign-audit.md"
git -C "$tmp" init -q -b develop
git -C "$tmp" config user.name test
git -C "$tmp" config user.email test@example.invalid
git -C "$tmp" add .
git -C "$tmp" commit -qm base
base=$(git -C "$tmp" rev-parse HEAD)
(cd "$tmp" && ./scripts/run-factory-runners.py >/dev/null)
runner_digest=$(cd "$tmp" && ./scripts/check-factory-runner-evidence.py --print-digest)
(cd "$tmp" && ./scripts/initialize-campaign-audit.py --round 1 --base "$base" \
    --runner-evidence-sha256 "$runner_digest" >/dev/null)
python3 - "$tmp/.factory/artifacts/campaign-audit.md" <<'PY'
from pathlib import Path
import sys
p=Path(sys.argv[1]); s=p.read_text().replace('result: pending','result: pass')
front='---\n'+s.split('---\n',2)[1]+'---\n'
p.write_text(front+'# Audit\n\n## Evidence reviewed\n- Specification: `docs/SPEC.md §1` requirements\n- Production paths: `src/app.c:1` initialization through shutdown\n- Executable evidence: `./test-product` PASS with artifact hash\n- Environment limits: `.factory/environment.toml` declares no external runner\n\n## Findings\nNone.\n')
PY
git -C "$tmp" add .factory/artifacts/campaign-audit.md .ralph/agent/scratchpad.md
git -C "$tmp" commit -qm pass
(cd "$tmp" && ./scripts/validate-campaign-audit.py complete --expected-round 1 --expected-base "$base" \
    --expected-runner-evidence-sha256 "$runner_digest" >/dev/null)
set +e
(cd "$tmp" && ./scripts/validate-campaign-audit.py complete --expected-round 2 --expected-base "$base" \
    --expected-runner-evidence-sha256 "$runner_digest" >/dev/null 2>&1)
binding_rc=$?
set -e
[[ $binding_rc -eq 1 ]]
set +e
(cd "$tmp" && ./scripts/validate-campaign-audit.py complete --expected-round 1 --expected-base "$base" \
    --expected-runner-evidence-sha256 "$(printf '0%.0s' {1..64})" >/dev/null 2>&1)
evidence_binding_rc=$?
set -e
[[ $evidence_binding_rc -eq 1 ]]

# A pass cannot conceal a numbered finding.
printf '\n## Finding 1: hidden\n- Requirement: x\n- Production evidence: y\n- Required remediation: z\n' >> "$tmp/.factory/artifacts/campaign-audit.md"
set +e
(cd "$tmp" && ./scripts/validate-campaign-audit.py complete --expected-round 1 --expected-base "$base" \
    --expected-runner-evidence-sha256 "$runner_digest" >/dev/null 2>&1)
hidden_rc=$?
set -e
[[ $hidden_rc -eq 1 ]]
git -C "$tmp" restore .factory/artifacts/campaign-audit.md

# A findings result must provide all remediation fields.
sed -i 's/result: pass/result: findings/; /## Findings/,$c\## Finding 1: gap\n- Requirement: x\n- Production evidence: y' "$tmp/.factory/artifacts/campaign-audit.md"
set +e
(cd "$tmp" && ./scripts/validate-campaign-audit.py complete --expected-round 1 --expected-base "$base" \
    --expected-runner-evidence-sha256 "$runner_digest" >/dev/null 2>&1)
missing_rc=$?
set -e
[[ $missing_rc -eq 1 ]]
git -C "$tmp" restore .factory/artifacts/campaign-audit.md

# An unauthorized product commit remains visible even if a later commit reverts it.
printf 'unauthorized\n' > "$tmp/product.c"
git -C "$tmp" add product.c
git -C "$tmp" commit -qm unauthorized
git -C "$tmp" rm -q product.c
git -C "$tmp" commit -qm reverted
set +e
(cd "$tmp" && ./scripts/validate-campaign-audit.py complete --expected-round 1 --expected-base "$base" \
    --expected-runner-evidence-sha256 "$runner_digest" >/dev/null 2>&1)
scope_rc=$?
set -e
[[ $scope_rc -eq 1 ]]

echo "test: campaign audit validation checks passed"
