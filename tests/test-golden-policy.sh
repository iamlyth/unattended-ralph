#!/usr/bin/env bash
# Adversarial golden-policy validation (out-of-band hardening): golden
# baselines are active acceptance assets; golden approval is out-of-band and
# non-automatable. Unattended gates never accept agent-authored `human: true`,
# reviewer strings, or environment reviewer identity. Every golden change and
# every review-manifest entry is rejected until a verifiable external
# attestation mechanism exists; generation refuses to run in unattended mode.
set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
PROJECT_ROOT=$(cd -- "$SCRIPT_DIR/.." && pwd)
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT

CHECKER="$PROJECT_ROOT/scripts/check-golden-policy.py"
GENERATOR="$PROJECT_ROOT/scripts/generate-golden.sh"

setup_repo() {
    local dir=$1
    mkdir -p "$dir/scripts" "$dir/.factory/schemas" "$dir/.factory/artifacts" "$dir/tests/golden" \
        "$dir/docs"
    cp "$CHECKER" "$GENERATOR" "$dir/scripts/"
    chmod +x "$dir/scripts/check-golden-policy.py" "$dir/scripts/generate-golden.sh"
    cat > "$dir/.factory/golden-policy.json" <<'POLICY'
{
  "schema": "ralph-golden-policy/v1",
  "golden_directories": ["tests/golden"],
  "review_manifest": ".factory/golden-review.json",
  "generation": {"allow_overwrite_of_active": false}
}
POLICY
    cat > "$dir/.factory/golden-review.json" <<'REVIEW'
{"schema": "ralph-golden-review/v1", "entries": []}
REVIEW
    printf 'PNG-GOLDEN-V1\n' > "$dir/tests/golden/baseline.png"
    printf '# Spec\n' > "$dir/docs/SPEC.md"
    printf '%s\n' ".factory-state/" > "$dir/.gitignore"
    git -C "$dir" init -q -b develop
    git -C "$dir" config user.name test
    git -C "$dir" config user.email test@example.invalid
    git -C "$dir" add .
    git -C "$dir" commit -qm base
}

expect() {
    local dir=$1 expected=$2 label=$3
    set +e
    (cd "$dir" && ./scripts/check-golden-policy.py >/dev/null 2>&1)
    local rc=$?
    set -e
    [[ $rc -eq $expected ]] || {
        echo "test: golden-policy $label (expected rc=$expected, got rc=$rc)" >&2
        exit 1
    }
}

setup_repo "$tmp/blessed"

# A clean tree with an empty manifest passes.
expect "$tmp/blessed" 0 "clean baselines"

# The generator refuses to run in unattended mode (out-of-band attestation).
set +e
(cd "$tmp/blessed" && ./scripts/generate-golden.sh >/dev/null 2>&1)
gen_rc=$?
set -e
[[ $gen_rc -eq 1 ]] || { echo "test: unattended generation was allowed" >&2; exit 1; }

# Modifying a golden is rejected outright: no manifest can authorize a golden
# change in unattended mode.
cp -a "$tmp/blessed" "$tmp/dirty"
printf 'PNG-GOLDEN-V2\n' > "$tmp/dirty/tests/golden/baseline.png"
expect "$tmp/dirty" 1 "modified golden without out-of-band attestation"

# Agent-authored self-attestation (human: true + reviewer string) is rejected:
# a review manifest entry is never accepted by an unattended gate.
cp -a "$tmp/blessed" "$tmp/self-approved"
printf 'PNG-GOLDEN-V2\n' > "$tmp/self-approved/tests/golden/baseline.png"
cat > "$tmp/self-approved/.factory/golden-review.json" <<'REVIEW'
{
  "schema": "ralph-golden-review/v1",
  "entries": [
    {"path": "tests/golden/baseline.png", "before_sha256": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
     "after_sha256": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
     "reason": "self-approved regeneration", "reviewer": "Ralph Agent", "human": true,
     "reviewed_at": "2026-08-19"}
  ]
}
REVIEW
expect "$tmp/self-approved" 1 "agent-authored human:true self-approval"

# An empty-manifest golden change is also rejected (no out-of-band mechanism).
cp -a "$tmp/blessed" "$tmp/dirty-empty-manifest"
printf 'PNG-GOLDEN-V2\n' > "$tmp/dirty-empty-manifest/tests/golden/baseline.png"
expect "$tmp/dirty-empty-manifest" 1 "golden change with an empty manifest"

# An environment reviewer identity does not authorize anything: the checker
# never reads reviewer identity from the environment.
set +e
(cd "$tmp/blessed" && GOLDEN_REVIEWER='env reviewer' \
    ./scripts/check-golden-policy.py >/dev/null 2>&1)
env_reviewer_rc=$?
set -e
[[ $env_reviewer_rc -eq 0 ]] || { echo "test: env reviewer identity broke clean baselines" >&2; exit 1; }

# A forged manifest entry on a clean tree is rejected (self-attestation).
cp -a "$tmp/blessed" "$tmp/forged"
cat > "$tmp/forged/.factory/golden-review.json" <<'REVIEW'
{
  "schema": "ralph-golden-review/v1",
  "entries": [
    {"path": "tests/golden/baseline.png", "before_sha256": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
     "after_sha256": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
     "reason": "forged", "reviewer": "A. Reviewer", "human": true, "reviewed_at": "2026-08-19"}
  ]
}
REVIEW
expect "$tmp/forged" 1 "forged manifest entry on a clean tree"

# The policy itself must forbid overwriting active goldens.
cp -a "$tmp/blessed" "$tmp/loose-policy"
python3 - "$tmp/loose-policy/.factory/golden-policy.json" <<'PY'
import json, sys
path = sys.argv[1]
data = json.load(open(path))
data['generation']['allow_overwrite_of_active'] = True
open(path, 'w').write(json.dumps(data))
PY
expect "$tmp/loose-policy" 1 "policy allowing overwrite"

echo "test: golden-policy adversarial checks passed"
