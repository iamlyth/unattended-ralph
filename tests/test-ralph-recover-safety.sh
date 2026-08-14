#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
PROJECT_ROOT=$(cd -- "$SCRIPT_DIR/.." && pwd)
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
mkdir -p "$tmp/scripts" "$tmp/.ralph/agent" "$tmp/.factory-state"
cp "$PROJECT_ROOT/scripts/ralph-recover.sh" "$PROJECT_ROOT/scripts/factory-lock.sh" "$tmp/scripts/"
chmod +x "$tmp/scripts/"*
printf '# Recovery handoff\n\n- Safe.\n' > "$tmp/.ralph/agent/scratchpad.md"
printf '{}\n' > "$tmp/.ralph/events-20260814-120000.jsonl"
printf 'planning\n' > "$tmp/.factory-state/loop-mode"
printf 'tracked\n' > "$tmp/product.txt"
cat > "$tmp/.gitignore" <<'EOF'
.factory-state/
.factory-lock
.ralph/*
!.ralph/agent/
.ralph/agent/*
!.ralph/agent/scratchpad.md
EOF
git -C "$tmp" init -q -b develop
git -C "$tmp" config user.name test
git -C "$tmp" config user.email test@example.invalid
git -C "$tmp" add .
git -C "$tmp" commit -qm initial

(cd "$tmp" && ./scripts/ralph-recover.sh --mode planning --prepare-only >/dev/null)
[[ $(<"$tmp/.ralph/current-loop-id") == primary-20260814-120000 ]]
[[ $(<"$tmp/.ralph/current-events") == .ralph/events-20260814-120000.jsonl ]]

printf 'external-loop\n' > "$tmp/external-loop"
rm -f "$tmp/.ralph/current-loop-id"
ln -s "$tmp/external-loop" "$tmp/.ralph/current-loop-id"
set +e
(cd "$tmp" && ./scripts/ralph-recover.sh --mode planning --prepare-only >/dev/null 2>&1)
marker_rc=$?
set -e
[[ $marker_rc -ne 0 && $(<"$tmp/external-loop") == external-loop ]] || {
    echo 'test-ralph-recover-safety: symlink marker was followed or accepted' >&2
    exit 1
}
rm -f "$tmp/.ralph/current-loop-id"

printf '# External handoff\n' > "$tmp/external-scratchpad"
rm -f "$tmp/.ralph/agent/scratchpad.md"
ln -s "$tmp/external-scratchpad" "$tmp/.ralph/agent/scratchpad.md"
set +e
(cd "$tmp" && ./scripts/ralph-recover.sh --mode planning --prepare-only >/dev/null 2>&1)
scratch_rc=$?
set -e
[[ $scratch_rc -ne 0 && $(<"$tmp/external-scratchpad") == '# External handoff' ]] || {
    echo 'test-ralph-recover-safety: symlink scratchpad was followed or accepted' >&2
    exit 1
}

echo 'test: Ralph recovery path safety checks passed'
