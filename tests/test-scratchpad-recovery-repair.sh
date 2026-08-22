#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
PROJECT_ROOT=$(cd -- "$SCRIPT_DIR/.." && pwd)
REPAIR="$PROJECT_ROOT/scripts/repair-scratchpad-handoffs.py"
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
mkdir -p "$tmp/.ralph/agent"
scratch="$tmp/.ralph/agent/scratchpad.md"

expect_failure() {
    local label=$1
    if "$REPAIR" --project-root "$tmp" >/dev/null 2>&1; then
        echo "test-scratchpad-recovery-repair: accepted $label" >&2
        exit 1
    fi
}

cat > "$scratch" <<'EOF'
# Handoff: first

- Preserve first body exactly: []{} / punctuation.

## First details

Paragraph from the older handoff.
# Handoff: second

- Preserve second body.

# Handoff: current

- Preserve latest body and title.
EOF
cat > "$tmp/expected" <<'EOF'
## Handoff: first

- Preserve first body exactly: []{} / punctuation.

## First details

Paragraph from the older handoff.
## Handoff: second

- Preserve second body.

# Handoff: current

- Preserve latest body and title.
EOF
"$REPAIR" --project-root "$tmp" >/dev/null
cmp "$tmp/expected" "$scratch"
[[ $(grep -c '^# ' "$scratch") -eq 1 ]]
cp "$scratch" "$tmp/idempotent"
"$REPAIR" --project-root "$tmp" >/dev/null
cmp "$tmp/idempotent" "$scratch"
[[ -z $(find "$tmp/.ralph/agent" -maxdepth 1 -name '.scratchpad.md.repair-*' -print -quit) ]]

cat > "$scratch" <<'EOF'
# One current handoff

- A valid single document is unchanged.
EOF
cp "$scratch" "$tmp/single"
"$REPAIR" --project-root "$tmp" >/dev/null
cmp "$tmp/single" "$scratch"

printf '# External handoff\n\n- Must not change.\n' > "$tmp/external"
rm -f "$scratch"
ln -s "$tmp/external" "$scratch"
expect_failure 'a symlink scratchpad'
grep -q 'Must not change.' "$tmp/external"
rm -f "$scratch"

python3 - "$scratch" <<'PY'
import sys
with open(sys.argv[1], 'wb') as out:
    out.write(b'# Oversized handoff\n\n- ' + b'x' * (1024 * 1024))
PY
cp "$scratch" "$tmp/oversized"
expect_failure 'an oversized scratchpad'
cmp "$tmp/oversized" "$scratch"

printf '## No level-one document\n\n- Fact.\n' > "$scratch"
cp "$scratch" "$tmp/malformed"
expect_failure 'a scratchpad without a level-one document'
cmp "$tmp/malformed" "$scratch"

printf '#Malformed title\n\n- Fact.\n' > "$scratch"
expect_failure 'a malformed level-one heading'

cat > "$scratch" <<'EOF'
# Handoff with content

- Fact.

# Truncated handoff

## Empty section
EOF
expect_failure 'a truncated handoff document'

: > "$scratch"
expect_failure 'a scratchpad with no content'

printf '# Invalid UTF-8\n\n- \377\n' > "$scratch"
expect_failure 'a non-UTF-8 scratchpad'

echo 'test: scratchpad recovery repair checks passed'
