#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
PROJECT_ROOT=$(cd -- "$SCRIPT_DIR/.." && pwd)
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
mkdir -p "$tmp/scripts" "$tmp/.ralph/agent" "$tmp/.factory-state"
cp "$PROJECT_ROOT/scripts/git-commit-hook.sh" \
   "$PROJECT_ROOT/scripts/check-scratchpad.sh" \
   "$PROJECT_ROOT/scripts/git-commit-guard.sh" \
   "$PROJECT_ROOT/scripts/install-git-commit-guard.sh" \
   "$PROJECT_ROOT/scripts/ralph-recover.sh" \
   "$PROJECT_ROOT/scripts/factory-lock.sh" \
   "$PROJECT_ROOT/scripts/factory-lock-exec.py" \
   "$PROJECT_ROOT/scripts/factory_lock.py" \
   "$PROJECT_ROOT/scripts/factory_state_io.py" \
   "$PROJECT_ROOT/scripts/factory-state-file.py" \
   "$PROJECT_ROOT/scripts/ralph_lock.py" \
   "$PROJECT_ROOT/scripts/ralph-lock-recover.py" \
   "$PROJECT_ROOT/scripts/ralph-final-state.py" "$tmp/scripts/"
chmod +x "$tmp/scripts/"*
chmod 700 "$tmp/.factory-state"
FACTORY_RALPH_CYCLE_ID=$(printf 'a%.0s' {1..64})
export FACTORY_RALPH_CYCLE_ID
printf '.factory-state/\n.factory-lock\n__pycache__/\n.ralph/*\n!.ralph/agent/\n.ralph/agent/*\n!.ralph/agent/scratchpad.md\n' > "$tmp/.gitignore"
printf 'base\n' > "$tmp/source.txt"
printf '# Initial handoff\n\n## Next\n\n- Start.\n' > "$tmp/.ralph/agent/scratchpad.md"
git -C "$tmp" init -q -b develop
git -C "$tmp" config user.name test
git -C "$tmp" config user.email test@example.invalid
git -C "$tmp" add .
git -C "$tmp" commit -qm initial
(cd "$tmp" && ./scripts/install-git-commit-guard.sh >/dev/null)
(cd "$tmp" && ./scripts/install-git-commit-guard.sh --check >/dev/null)
payload=$(printf '{"loop":{"workspace":"%s","id":"checkpoint-test"},"iteration":{"current":"1"}}' "$tmp")

# Repeated ordinary scratchpad updates remain recoverable worktree state and do
# not manufacture Git progress.
initial_head=$(git -C "$tmp" rev-parse HEAD)
for iteration in 1 2 3 4; do
    printf '# Current handoff\n\n## Next\n\n- Resume update %s.\n' "$iteration" > "$tmp/.ralph/agent/scratchpad.md"
    (cd "$tmp" && printf '%s' "$payload" | ./scripts/git-commit-hook.sh >/dev/null)
    [[ $(git -C "$tmp" rev-parse HEAD) == "$initial_head" ]]
    grep -q "Resume update $iteration" "$tmp/.ralph/agent/scratchpad.md"
done
[[ $(git -C "$tmp" rev-list --count "$initial_head..HEAD") -eq 0 ]]

# Prepare-only recovery must retain the latest non-empty uncommitted handoff.
printf '%s\n' implementation > "$tmp/.factory-state/loop-mode"
printf '%s\n' '{"topic":"factory.implement","payload":"continue"}' > "$tmp/.ralph/events.jsonl"
printf '%s\n' .ralph/events.jsonl > "$tmp/.ralph/current-events"
printf '%s\n' checkpoint-test > "$tmp/.ralph/current-loop-id"
cp "$tmp/.ralph/agent/scratchpad.md" "$tmp/.factory-state/expected-handoff"
(cd "$tmp" && ./scripts/ralph-recover.sh --mode implementation --loop-id checkpoint-test --prepare-only >/dev/null)
cmp "$tmp/.factory-state/expected-handoff" "$tmp/.ralph/agent/scratchpad.md"

# Substantive source and scratchpad changes commit together.
printf 'changed\n' >> "$tmp/source.txt"
printf '# Source handoff\n\n## Verification\n\n- Source changed.\n' > "$tmp/.ralph/agent/scratchpad.md"
(cd "$tmp" && printf '%s' "$payload" | ./scripts/git-commit-hook.sh >/dev/null)
source_head=$(git -C "$tmp" rev-parse HEAD)
[[ "$source_head" != "$initial_head" ]]
mapfile -t changed < <(git -C "$tmp" diff-tree --no-commit-id --name-only -r HEAD | sort)
[[ "${changed[*]}" == '.ralph/agent/scratchpad.md source.txt' ]]
[[ -z $(git -C "$tmp" status --porcelain --untracked-files=normal) ]]

# The explicit final path permits exactly one scratchpad-only handoff commit.
printf '# Final handoff\n\n## Verification\n\n- Complete and token-free.\n' > "$tmp/.ralph/agent/scratchpad.md"
(cd "$tmp" && printf '%s' "$payload" | ./scripts/git-commit-hook.sh --final-handoff >/dev/null)
final_head=$(git -C "$tmp" rev-parse HEAD)
[[ "$final_head" != "$source_head" ]]
[[ -z $(git -C "$tmp" status --porcelain --untracked-files=normal) ]]
(cd "$tmp" && ./scripts/ralph-final-state.py attest implementation "$final_head" >/dev/null)
(cd "$tmp" && ./scripts/ralph-final-state.py verify implementation >/dev/null)
if (cd "$tmp" && ./scripts/ralph-final-state.py attest implementation "$final_head" >/dev/null 2>&1); then
    echo 'test-git-checkpoint: repeated successful attestation was accepted' >&2
    exit 1
fi

printf '# Revised final handoff\n\n## Verification\n\n- Metadata churn.\n' > "$tmp/.ralph/agent/scratchpad.md"
set +e
(cd "$tmp" && printf '%s' "$payload" | ./scripts/git-commit-hook.sh --final-handoff >/dev/null 2>&1)
repeat_rc=$?
set -e
[[ $repeat_rc -eq 1 && $(git -C "$tmp" rev-parse HEAD) == "$final_head" ]]
git -C "$tmp" restore -- .ralph/agent/scratchpad.md

printf 'forbidden\n' >> "$tmp/source.txt"
printf '# Boundary handoff\n\n## Verification\n\n- Dirty source must fail.\n' > "$tmp/.ralph/agent/scratchpad.md"
set +e
(cd "$tmp" && printf '%s' "$payload" | ./scripts/git-commit-hook.sh --final-handoff >/dev/null 2>&1)
boundary_rc=$?
set -e
[[ $boundary_rc -eq 1 && $(git -C "$tmp" rev-parse HEAD) == "$final_head" ]]
git -C "$tmp" restore -- source.txt .ralph/agent/scratchpad.md

printf '# Contaminated handoff\n\n## Status\n\nLOOP_COMPLETE\n' > "$tmp/.ralph/agent/scratchpad.md"
set +e
(cd "$tmp" && printf '%s' "$payload" | ./scripts/git-commit-hook.sh --final-handoff >/dev/null 2>&1)
token_rc=$?
set -e
[[ $token_rc -eq 1 && $(git -C "$tmp" rev-parse HEAD) == "$final_head" ]]

echo 'test: Ralph checkpoint progress and final-handoff boundaries passed'
