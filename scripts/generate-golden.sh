#!/usr/bin/env bash
# generate-golden.sh — Regenerate golden image baselines for visual tests.
#
# Protected golden policy (`.factory/golden-policy.json`): generation can
# never overwrite active (committed) goldens as a side effect. This script
# refuses to run unless the operator supplies an explicit review manifest
# path, a human reviewer identity, and a review date. After regeneration it
# writes the reviewed manifest entries (before = HEAD hash, after = new hash)
# so `scripts/check-golden-policy.py` can validate the change before commit.
# The goldens and the manifest are reviewed and committed together.
#
# Usage:
#   CBX_GOLDEN_REVIEW_MANIFEST=.factory/golden-review.json \
#   CBX_GOLDEN_REVIEWER='<human name>' \
#   CBX_GOLDEN_REVIEWED_AT=2026-08-19 \
#   scripts/generate-golden.sh
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SOURCE_DIR="$(dirname "$SCRIPT_DIR")"

cd "$SOURCE_DIR"

[[ -n "${CBX_GOLDEN_REVIEW_MANIFEST:-}" ]] || {
    echo "generate-golden: refused: generation cannot overwrite active goldens without a review manifest (CBX_GOLDEN_REVIEW_MANIFEST)" >&2
    exit 1
}
[[ -n "${CBX_GOLDEN_REVIEWER:-}" ]] || {
    echo "generate-golden: refused: a human reviewer identity is required (CBX_GOLDEN_REVIEWER)" >&2
    exit 1
}
[[ "${CBX_GOLDEN_REVIEWED_AT:-}" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}$ ]] || {
    echo "generate-golden: refused: an ISO-8601 review date is required (CBX_GOLDEN_REVIEWED_AT)" >&2
    exit 1
}
MANIFEST="$CBX_GOLDEN_REVIEW_MANIFEST"
[[ "$MANIFEST" != /* ]] || { echo "generate-golden: manifest path must be repository-relative" >&2; exit 1; }
[[ -f "$MANIFEST" ]] || { echo "generate-golden: review manifest does not exist: $MANIFEST" >&2; exit 1; }
python3 - "$MANIFEST" <<'PY' || exit 1
import json, sys
path = sys.argv[1]
data = json.load(open(path, encoding='utf-8'))
if data.get('schema') != 'ralph-golden-review/v1' or not isinstance(data.get('entries'), list):
    raise SystemExit('generate-golden: invalid review manifest schema')
if data['entries']:
    raise SystemExit('generate-golden: review manifest already has entries; resolve or commit the previous review first')
PY

echo "Building test_golden..."
nix-shell --run "cmake --build build-maintenance-verify --target test_golden"

echo ""
echo "Generating golden baselines..."
CBX_GENERATE_GOLDEN=1 SDL_VIDEODRIVER=dummy \
  ctest --test-dir build-maintenance-verify -R test_golden --output-on-failure

python3 - "$MANIFEST" "$CBX_GOLDEN_REVIEWER" "$CBX_GOLDEN_REVIEWED_AT" "${CBX_GOLDEN_REASON:-reviewed regeneration}" <<'PY'
import hashlib, json, subprocess, sys
from pathlib import Path
manifest, reviewer, reviewed_at, reason = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]
policy = json.load(open('.factory/golden-policy.json', encoding='utf-8'))
directories = policy.get('golden_directories', [])
entries = []
def head_hash(ref):
    result = subprocess.run(['git', 'show', f'HEAD:{ref}'], capture_output=True)
    return hashlib.sha256(result.stdout).hexdigest() if result.returncode == 0 else None
def work_hash(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()
for directory in directories:
    base = Path(directory)
    if not base.is_dir():
        continue
    for path in sorted(base.rglob('*')):
        if not path.is_file() or path.is_symlink():
            continue
        relative = str(path)
        before = head_hash(relative)
        after = work_hash(path)
        if before == after:
            continue
        entries.append({
            'path': relative,
            'before_sha256': before,
            'after_sha256': after,
            'reason': reason,
            'reviewer': reviewer,
            'human': True,
            'reviewed_at': reviewed_at,
        })
if not entries:
    raise SystemExit('golden-generate: no golden file changed; refusing to record an empty review')
json.dump({'schema': 'ralph-golden-review/v1', 'entries': entries}, open(manifest, 'w'), indent=2)
print(f'golden-generate: wrote {len(entries)} reviewed entry/entries to {manifest}')
print('golden-generate: review the images, then commit goldens + manifest together')
PY
