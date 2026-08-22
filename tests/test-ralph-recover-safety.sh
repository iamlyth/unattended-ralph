#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
PROJECT_ROOT=$(cd -- "$SCRIPT_DIR/.." && pwd)
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
mkdir -p "$tmp/scripts" "$tmp/.ralph/agent" "$tmp/.factory-state"
cp "$PROJECT_ROOT/scripts/ralph-recover.sh" "$PROJECT_ROOT/scripts/repair-scratchpad-handoffs.py" \
    "$PROJECT_ROOT/scripts/factory-lock.sh" "$PROJECT_ROOT/scripts/factory-lock-exec.py" \
    "$PROJECT_ROOT/scripts/factory_lock.py" \
    "$PROJECT_ROOT/scripts/factory_state_io.py" "$PROJECT_ROOT/scripts/factory-state-file.py" \
    "$PROJECT_ROOT/scripts/ralph_lock.py" "$PROJECT_ROOT/scripts/ralph-lock-recover.py" \
    "$PROJECT_ROOT/scripts/git-commit-guard.sh" "$PROJECT_ROOT/scripts/install-git-commit-guard.sh" \
    "$tmp/scripts/"
chmod +x "$tmp/scripts/"*
chmod 700 "$tmp/.factory-state"
cat > "$tmp/.ralph/agent/scratchpad.md" <<'EOF'
# Handoff: earlier

- Earlier fact must survive.

# Handoff: latest

- Latest fact remains current.
EOF
printf '{"pid":99999999}\n' > "$tmp/.ralph/loop.lock"
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
[[ ! -e "$tmp/.ralph/loop.lock" ]]
[[ $(<"$tmp/.ralph/current-loop-id") == primary-20260814-120000 ]]
[[ $(<"$tmp/.ralph/current-events") == .ralph/events-20260814-120000.jsonl ]]
[[ $(grep -c '^# ' "$tmp/.ralph/agent/scratchpad.md") -eq 1 ]]
grep -q '^## Handoff: earlier$' "$tmp/.ralph/agent/scratchpad.md"
grep -q 'Earlier fact must survive.' "$tmp/.ralph/agent/scratchpad.md"
grep -q '^# Handoff: latest$' "$tmp/.ralph/agent/scratchpad.md"
cp "$tmp/.ralph/agent/scratchpad.md" "$tmp/idempotent-scratchpad"
(cd "$tmp" && ./scripts/ralph-recover.sh --mode planning --prepare-only >/dev/null)
cmp "$tmp/idempotent-scratchpad" "$tmp/.ralph/agent/scratchpad.md"

printf '{broken\n' > "$tmp/.ralph/loop.lock"
set +e
(cd "$tmp" && ./scripts/ralph-recover.sh --mode planning --prepare-only >/dev/null 2>&1)
ambiguous_lock_rc=$?
set -e
[[ $ambiguous_lock_rc -ne 0 && -f "$tmp/.ralph/loop.lock" ]] || {
    echo 'test-ralph-recover-safety: ambiguous loop lock was removed' >&2; exit 1;
}
printf '{"pid":%s}\n' "$$" > "$tmp/.ralph/loop.lock"
set +e
(cd "$tmp" && ./scripts/ralph-recover.sh --mode planning --prepare-only >/dev/null 2>&1)
live_lock_rc=$?
set -e
[[ $live_lock_rc -ne 0 && -f "$tmp/.ralph/loop.lock" ]] || {
    echo 'test-ralph-recover-safety: live-PID loop lock was removed' >&2; exit 1;
}
printf '{"pid":99999999}\n' > "$tmp/.ralph/loop.lock"
(cd "$tmp" && ./scripts/ralph-recover.sh --mode planning --prepare-only >/dev/null 2>&1)
[[ ! -e "$tmp/.ralph/loop.lock" ]]

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
