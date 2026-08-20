#!/usr/bin/env bash
# Adversarial golden-policy validation: golden baselines are active acceptance
# assets. Working-tree changes require a reviewed manifest entry with exact
# before/after hashes and a human identity; generation cannot overwrite active
# goldens; after commit, every manifest entry must match a real Git transition.
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

write_manifest() {
    local dir=$1
    python3 - "$dir" <<'PY'
import hashlib, json, os, subprocess, sys
root = sys.argv[1]
def head_hash(ref):
    result = subprocess.run(['git', 'show', f'HEAD:{ref}'], cwd=root, capture_output=True)
    return hashlib.sha256(result.stdout).hexdigest() if result.returncode == 0 else None
def read_hash(path):
    try:
        return hashlib.sha256(open(path, 'rb').read()).hexdigest()
    except OSError:
        return None
entries = []
paths = set()
listed = subprocess.run(['git', 'ls-tree', '-r', '--name-only', 'HEAD', '--', 'tests/golden'],
                        cwd=root, text=True, capture_output=True)
for line in listed.stdout.splitlines():
    paths.add(line)
for base, _, files in os.walk(root + '/tests/golden'):
    for name in sorted(files):
        path = os.path.join(base, name)
        paths.add(os.path.relpath(path, root))
for rel in sorted(paths):
    before = head_hash(rel)
    after = read_hash(os.path.join(root, rel))
    if before != after:
        entries.append({
            'path': rel, 'before_sha256': before, 'after_sha256': after,
            'reason': 'reviewed baseline update', 'reviewer': 'A. Reviewer',
            'human': True, 'reviewed_at': '2026-08-19',
        })
json.dump({'schema': 'ralph-golden-review/v1', 'entries': entries},
          open(root + '/.factory/golden-review.json', 'w'), indent=2)
PY
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

# The generator refuses to overwrite active goldens without review identity.
set +e
(cd "$tmp/blessed" && ./scripts/generate-golden.sh >/dev/null 2>&1)
gen_rc=$?
set -e
[[ $gen_rc -eq 1 ]] || { echo "test: unguarded generation was allowed" >&2; exit 1; }

# Modifying a golden without a review manifest is rejected.
cp -a "$tmp/blessed" "$tmp/dirty-no-manifest"
printf 'PNG-GOLDEN-V2\n' > "$tmp/dirty-no-manifest/tests/golden/baseline.png"
expect "$tmp/dirty-no-manifest" 1 "modified golden without manifest"

# A matching review manifest (exact before/after hashes + reviewer) passes.
cp -a "$tmp/blessed" "$tmp/dirty-reviewed"
printf 'PNG-GOLDEN-V2\n' > "$tmp/dirty-reviewed/tests/golden/baseline.png"
write_manifest "$tmp/dirty-reviewed"
expect "$tmp/dirty-reviewed" 0 "modified golden with manifest"

# A wrong before hash (forged against HEAD) is rejected.
cp -a "$tmp/blessed" "$tmp/wrong-before"
printf 'PNG-GOLDEN-V2\n' > "$tmp/wrong-before/tests/golden/baseline.png"
write_manifest "$tmp/wrong-before"
python3 - "$tmp/wrong-before/.factory/golden-review.json" <<'PY'
import json, sys
path = sys.argv[1]
data = json.load(open(path))
data['entries'][0]['before_sha256'] = '0' * 64
open(path, 'w').write(json.dumps(data))
PY
expect "$tmp/wrong-before" 1 "wrong before hash"

# A wrong after hash (does not match the working tree) is rejected.
cp -a "$tmp/blessed" "$tmp/wrong-after"
printf 'PNG-GOLDEN-V2\n' > "$tmp/wrong-after/tests/golden/baseline.png"
write_manifest "$tmp/wrong-after"
python3 - "$tmp/wrong-after/.factory/golden-review.json" <<'PY'
import json, sys
path = sys.argv[1]
data = json.load(open(path))
data['entries'][0]['after_sha256'] = '0' * 64
open(path, 'w').write(json.dumps(data))
PY
expect "$tmp/wrong-after" 1 "wrong after hash"

# An automated (non-human) reviewer is rejected.
cp -a "$tmp/blessed" "$tmp/auto-review"
printf 'PNG-GOLDEN-V2\n' > "$tmp/auto-review/tests/golden/baseline.png"
write_manifest "$tmp/auto-review"
python3 - "$tmp/auto-review/.factory/golden-review.json" <<'PY'
import json, sys
path = sys.argv[1]
data = json.load(open(path))
data['entries'][0]['human'] = False
open(path, 'w').write(json.dumps(data))
PY
expect "$tmp/auto-review" 1 "non-human reviewer"

# An empty reviewer identity is rejected.
cp -a "$tmp/blessed" "$tmp/anon-review"
printf 'PNG-GOLDEN-V2\n' > "$tmp/anon-review/tests/golden/baseline.png"
write_manifest "$tmp/anon-review"
python3 - "$tmp/anon-review/.factory/golden-review.json" <<'PY'
import json, sys
path = sys.argv[1]
data = json.load(open(path))
data['entries'][0]['reviewer'] = '   '
open(path, 'w').write(json.dumps(data))
PY
expect "$tmp/anon-review" 1 "empty reviewer identity"

# A stale manifest entry (for a path that did not change) is rejected.
cp -a "$tmp/blessed" "$tmp/stale-manifest"
printf 'PNG-GOLDEN-V2\n' > "$tmp/stale-manifest/tests/golden/baseline.png"
write_manifest "$tmp/stale-manifest"
python3 - "$tmp/stale-manifest/.factory/golden-review.json" <<'PY'
import json, sys
path = sys.argv[1]
data = json.load(open(path))
data['entries'].append({
    'path': 'tests/golden/never-changed.png', 'before_sha256': '0' * 64,
    'after_sha256': '0' * 64, 'reason': 'stale', 'reviewer': 'A. Reviewer',
    'human': True, 'reviewed_at': '2026-08-19'})
open(path, 'w').write(json.dumps(data))
PY
expect "$tmp/stale-manifest" 1 "stale manifest entries"

# A new golden file also needs a reviewed manifest entry.
cp -a "$tmp/blessed" "$tmp/new-golden"
printf 'PNG-GOLDEN-NEW\n' > "$tmp/new-golden/tests/golden/new.png"
write_manifest "$tmp/new-golden"
expect "$tmp/new-golden" 0 "new golden with manifest review"

# A deleted golden needs a manifest entry with after_sha256 null.
cp -a "$tmp/blessed" "$tmp/deleted-golden"
rm -f "$tmp/deleted-golden/tests/golden/baseline.png"
write_manifest "$tmp/deleted-golden"
python3 - "$tmp/deleted-golden/.factory/golden-review.json" <<'PY'
import json, sys
path = sys.argv[1]
data = json.load(open(path))
assert len(data['entries']) == 1
assert data['entries'][0]['path'] == 'tests/golden/baseline.png'
assert data['entries'][0]['after_sha256'] is None
open(path, 'w').write(json.dumps(data))
PY
expect "$tmp/deleted-golden" 0 "deleted golden with manifest review"

# After committing goldens + manifest, the review stays valid and durable.
cp -a "$tmp/blessed" "$tmp/committed"
printf 'PNG-GOLDEN-V2\n' > "$tmp/committed/tests/golden/baseline.png"
write_manifest "$tmp/committed"
git -C "$tmp/committed" add .
git -C "$tmp/committed" commit -qm "reviewed golden update"
expect "$tmp/committed" 0 "committed golden transition"

# A forged manifest entry on a clean tree matches no real transition.
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
