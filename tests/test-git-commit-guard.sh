#!/usr/bin/env bash
# Adversarial coverage for the fail-closed Git commit boundary.
#
# Reproduces the dogfooding regression that produced five consecutive
# scratchpad-only commits in the adopting product: a blocked cycle whose worker
# refreshed .ralph/agent/scratchpad.md and committed it directly with git each
# iteration. Every direct-commit vector must be rejected at the boundary with
# no metadata commit reaching history and the dirty handoff preserved in the
# worktree, while substantive commits (including those that carry the
# scratchpad) and exactly one trusted final-handoff commit per cycle (bound to
# the lifecycle nonce/cycle state) remain allowed.
set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
PROJECT_ROOT=$(cd -- "$SCRIPT_DIR/.." && pwd)
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
mkdir -p "$tmp/scripts/pi-cli-shims" "$tmp/.ralph/agent" "$tmp/.factory-state" "$tmp/sub"
for name in git-commit-guard.sh install-git-commit-guard.sh git-commit-hook.sh \
        check-scratchpad.sh ralph-final-state.py factory_state_io.py; do
    cp "$PROJECT_ROOT/scripts/$name" "$tmp/scripts/"
done
cp "$PROJECT_ROOT/scripts/pi-cli-shims/git" "$tmp/scripts/pi-cli-shims/git"
chmod +x "$tmp/scripts/"*
chmod 700 "$tmp/.factory-state"
SHIM="$tmp/scripts/pi-cli-shims/git"
CYCLE=$(printf 'a%.0s' {1..64})

printf '.factory-state/\n.factory-lock\n__pycache__/\n.ralph/*\n!.ralph/agent/\n.ralph/agent/*\n!.ralph/agent/scratchpad.md\n' > "$tmp/.gitignore"
printf 'base\n' > "$tmp/source.txt"
printf '# Initial handoff\n\n## Next\n\n- Start.\n' > "$tmp/.ralph/agent/scratchpad.md"
git -C "$tmp" init -q -b develop
git -C "$tmp" config user.name test
git -C "$tmp" config user.email test@example.invalid
git -C "$tmp" add .
git -C "$tmp" commit -qm initial
initial_head=$(git -C "$tmp" rev-parse HEAD)

# A side branch carrying only a scratchpad change, created before the boundary
# is installed, feeds the merge/cherry-pick/revert vectors that never run
# pre-commit.
git -C "$tmp" checkout -qb side
printf '# Side handoff\n\n## Next\n\n- Side scratchpad only.\n' > "$tmp/.ralph/agent/scratchpad.md"
git -C "$tmp" add -f .ralph/agent/scratchpad.md
git -C "$tmp" commit -qm side
side_head=$(git -C "$tmp" rev-parse HEAD)
git -C "$tmp" checkout -q develop

# Installation is idempotent and verifiable; a foreign hook is refused.
# The boundary spans pre-commit, pre-merge-commit and commit-msg so commit
# paths that never run pre-commit stay guarded.
(cd "$tmp" && ./scripts/install-git-commit-guard.sh >/dev/null)
(cd "$tmp" && ./scripts/install-git-commit-guard.sh --check >/dev/null)
(cd "$tmp" && ./scripts/install-git-commit-guard.sh >/dev/null)
for hook_name in pre-commit prepare-commit-msg pre-merge-commit applypatch-msg pre-applypatch commit-msg; do
    [[ -x "$tmp/.git/hooks/$hook_name" ]] || { echo "test-git-commit-guard: missing hook $hook_name" >&2; exit 1; }
done
printf '#!/usr/bin/env bash\n# foreign\n' > "$tmp/.git/hooks/pre-commit"
set +e
(cd "$tmp" && ./scripts/install-git-commit-guard.sh >/dev/null 2>&1)
foreign_rc=$?
set -e
[[ $foreign_rc -eq 2 ]] || { echo "test-git-commit-guard: foreign hook was not refused" >&2; exit 1; }
rm -f "$tmp/.git/hooks/pre-commit"
(cd "$tmp" && ./scripts/install-git-commit-guard.sh >/dev/null)
(cd "$tmp" && ./scripts/install-git-commit-guard.sh --check >/dev/null)

# Repeated ordinary scratchpad refreshes committed directly must manufacture no
# Git progress, exactly like the blocked-cycle regression: five attempts.
for iteration in 1 2 3 4 5; do
    printf '# Current handoff\n\n## Next\n\n- Resume update %s.\n' "$iteration" > "$tmp/.ralph/agent/scratchpad.md"
    git -C "$tmp" add -f .ralph/agent/scratchpad.md
    set +e
    (cd "$tmp" && git commit -m "factory: refresh scratchpad handoff (cycle blocked on external facts)" >/dev/null 2>&1)
    rc=$?
    set -e
    [[ $rc -eq 1 ]] || { echo "test-git-commit-guard: direct scratchpad commit $iteration was accepted" >&2; exit 1; }
    [[ $(git -C "$tmp" rev-parse HEAD) == "$initial_head" ]]
    grep -q "Resume update $iteration" "$tmp/.ralph/agent/scratchpad.md"
done
[[ $(git -C "$tmp" rev-list --count "$initial_head..HEAD") -eq 0 ]]
[[ $(git -C "$tmp" log --format=%s | grep -c 'scratchpad') -eq 0 ]]
[[ -n $(git -C "$tmp" status --porcelain --untracked-files=normal) ]]

# Every direct-commit vector must be rejected without history progress.
assert_blocked() {
    local label=$1 expected_head=$2
    shift 2
    set +e
    "$@" >/dev/null 2>&1
    local rc=$?
    set -e
    [[ $rc -eq 1 ]] || { echo "test-git-commit-guard: vector '$label' returned $rc" >&2; exit 1; }
    [[ $(git -C "$tmp" rev-parse HEAD) == "$expected_head" ]] || {
        echo "test-git-commit-guard: vector '$label' advanced history" >&2; exit 1; }
}

printf '# Pathspec\n\n## Next\n\n- pathspec.\n' > "$tmp/.ralph/agent/scratchpad.md"
assert_blocked "pathspec commit" "$initial_head" \
    git -C "$tmp" commit -m pathspec -- .ralph/agent/scratchpad.md
assert_blocked "git -C commit" "$initial_head" \
    git -C "$tmp" commit -m gitslashC
assert_blocked "alternate cwd commit" "$initial_head" \
    bash -c "cd '$tmp/sub' && git commit -m altcwd"
assert_blocked "command/env prefix" "$initial_head" \
    bash -c "cd '$tmp' && env git commit -m envprefix"
printf '# Amend\n\n## Next\n\n- amend.\n' > "$tmp/.ralph/agent/scratchpad.md"
git -C "$tmp" add -f .ralph/agent/scratchpad.md
assert_blocked "amend scratchpad-only" "$initial_head" \
    git -C "$tmp" commit --amend -m amended
assert_blocked "empty commit" "$initial_head" \
    git -C "$tmp" commit --allow-empty -m empty
assert_blocked "gitlink scratchpad" "$initial_head" bash -c "
    git -C '$tmp' rm -q --cached .ralph/agent/scratchpad.md
    git -C '$tmp' update-index --add --cacheinfo 160000,$(printf 'd%.0s' {1..40}),.ralph/agent/scratchpad.md
    git -C '$tmp' commit -m gitlink
"
git -C "$tmp" reset -q
printf '# Symlink\n\n## Next\n\n- symlink.\n' > "$tmp/.ralph/agent/scratchpad.md"
printf 'outside\n' > "$tmp/outside.txt"
rm "$tmp/.ralph/agent/scratchpad.md"
ln -s ../outside.txt "$tmp/.ralph/agent/scratchpad.md"
git -C "$tmp" add -f .ralph/agent/scratchpad.md
assert_blocked "symlink scratchpad" "$initial_head" git -C "$tmp" commit -m symlink
git -C "$tmp" reset -q
rm "$tmp/.ralph/agent/scratchpad.md" "$tmp/outside.txt"
printf '# Restored\n\n## Next\n\n- restored.\n' > "$tmp/.ralph/agent/scratchpad.md"

# Commit-creation paths that never run pre-commit are still guarded: merge
# runs pre-merge-commit + commit-msg (both installed), and the argv shim
# refuses cherry-pick/revert/am/rebase/pull (no hook coverage) so the model
# can never create commits through them. A branch whose only change is a
# scratchpad refresh must not reach history through any path.
git -C "$tmp" merge --no-ff side >/dev/null 2>&1 || true
[[ $(git -C "$tmp" rev-parse HEAD) == "$initial_head" ]]
git -C "$tmp" merge --abort 2>/dev/null || true
git -C "$tmp" reset -q --hard "$initial_head" >/dev/null
set +e
(cd "$tmp" && "$SHIM" cherry-pick "$side_head" >/dev/null 2>&1)
cherry_shim_rc=$?
(cd "$tmp" && "$SHIM" revert "$side_head" >/dev/null 2>&1)
revert_shim_rc=$?
(cd "$tmp" && "$SHIM" am /tmp/nonexistent.patch >/dev/null 2>&1)
am_shim_rc=$?
(cd "$tmp" && "$SHIM" rebase "$side_head" >/dev/null 2>&1)
rebase_shim_rc=$?
set -e
[[ $cherry_shim_rc -eq 1 && $revert_shim_rc -eq 1 && $am_shim_rc -eq 1 && $rebase_shim_rc -eq 1 ]]
[[ $(git -C "$tmp" rev-parse HEAD) == "$initial_head" ]]
git -C "$tmp" reset -q --hard "$initial_head" >/dev/null
printf '# Restored after merge vectors\n\n## Next\n\n- clean.\n' > "$tmp/.ralph/agent/scratchpad.md"
git -C "$tmp" status --porcelain --untracked-files=normal | grep -q . || \
    { echo "test-git-commit-guard: dirty handoff was lost after merge vectors" >&2; exit 1; }

# Untracked files are never part of a commit and cannot smuggle metadata in;
# a substantive commit that also carries the scratchpad is allowed.
printf 'untracked-handoff\n' > "$tmp/.ralph/agent/untracked.md"
printf '# Source handoff\n\n## Verification\n\n- Source changed.\n' > "$tmp/.ralph/agent/scratchpad.md"
printf 'changed\n' >> "$tmp/source.txt"
git -C "$tmp" add -A
(cd "$tmp" && git commit -qm "implement: source change with handoff")
source_head=$(git -C "$tmp" rev-parse HEAD)
[[ "$source_head" != "$initial_head" ]]
mapfile -t changed < <(git -C "$tmp" diff-tree --no-commit-id --name-only -r HEAD | sort)
[[ "${changed[*]}" == '.ralph/agent/scratchpad.md source.txt' ]]
[[ -z $(git -C "$tmp" status --porcelain --untracked-files=normal) ]]

# The trusted final-handoff path permits exactly one scratchpad-only commit,
# authorized by the lifecycle cycle and consumed one-shot at the boundary.
printf '# Final handoff\n\n## Verification\n\n- Complete and token-free.\n' > "$tmp/.ralph/agent/scratchpad.md"
payload=$(printf '{"loop":{"workspace":"%s","id":"checkpoint-test"},"iteration":{"current":"1"}}' "$tmp")
(cd "$tmp" && FACTORY_RALPH_CYCLE_ID=$CYCLE printf '%s' "$payload" \
    | FACTORY_RALPH_CYCLE_ID=$CYCLE ./scripts/git-commit-hook.sh --final-handoff >/dev/null)
final_head=$(git -C "$tmp" rev-parse HEAD)
[[ "$final_head" != "$source_head" ]]
[[ ! -e "$tmp/.factory-state/final-handoff-authorization.json" ]]
[[ -z $(git -C "$tmp" status --porcelain --untracked-files=normal) ]]

# A second final handoff in the same cycle is rejected with no lingering token.
printf '# Revised final handoff\n\n## Verification\n\n- Metadata churn.\n' > "$tmp/.ralph/agent/scratchpad.md"
set +e
(cd "$tmp" && FACTORY_RALPH_CYCLE_ID=$CYCLE printf '%s' "$payload" \
    | FACTORY_RALPH_CYCLE_ID=$CYCLE ./scripts/git-commit-hook.sh --final-handoff >/dev/null 2>&1)
repeat_rc=$?
set -e
[[ $repeat_rc -eq 1 && $(git -C "$tmp" rev-parse HEAD) == "$final_head" ]]
[[ ! -e "$tmp/.factory-state/final-handoff-authorization.json" ]]
git -C "$tmp" restore -- .ralph/agent/scratchpad.md

# A valid-schema token is one-shot: the first use is consumed at the boundary
# and a subsequent scratchpad-only commit is rejected again.
printf '# Token reuse\n\n## Next\n\n- reuse.\n' > "$tmp/.ralph/agent/scratchpad.md"
python3 - "$tmp" "$CYCLE" <<'PY'
import json, sys
from pathlib import Path
root = Path(sys.argv[1])
(root / '.factory-state/final-handoff-authorization.json').write_text(json.dumps({
    'schema': 'ralph-final-handoff/v1', 'mode': 'implementation',
    'cycle_id': sys.argv[2], 'nonce': 'c' * 64,
}) + '\n')
PY
git -C "$tmp" add -f .ralph/agent/scratchpad.md
set +e
(cd "$tmp" && FACTORY_RALPH_CYCLE_ID=$CYCLE git commit -m "token-once" >/dev/null 2>&1)
once_rc=$?
set -e
[[ $once_rc -eq 0 && $(git -C "$tmp" rev-parse HEAD) != "$final_head" ]]
[[ ! -e "$tmp/.factory-state/final-handoff-authorization.json" ]]
printf '# Token reuse again\n\n## Next\n\n- reuse again.\n' > "$tmp/.ralph/agent/scratchpad.md"
git -C "$tmp" add -f .ralph/agent/scratchpad.md
set +e
(cd "$tmp" && FACTORY_RALPH_CYCLE_ID=$CYCLE git commit -m "token-again" >/dev/null 2>&1)
again_rc=$?
set -e
[[ $again_rc -eq 1 ]]

# A token bound to the wrong cycle is rejected and consumed (no replay).
printf '# Forged token\n\n## Next\n\n- forged.\n' > "$tmp/.ralph/agent/scratchpad.md"
python3 - "$tmp" <<'PY'
import json, sys
from pathlib import Path
root = Path(sys.argv[1])
(root / '.factory-state/final-handoff-authorization.json').write_text(json.dumps({
    'schema': 'ralph-final-handoff/v1', 'mode': 'implementation',
    'cycle_id': 'b' * 64, 'nonce': 'c' * 64,
}) + '\n')
PY
git -C "$tmp" add -f .ralph/agent/scratchpad.md
set +e
(cd "$tmp" && FACTORY_RALPH_CYCLE_ID=$CYCLE git commit -m "forged" >/dev/null 2>&1)
forged_rc=$?
set -e
[[ $forged_rc -eq 1 ]]
[[ ! -e "$tmp/.factory-state/final-handoff-authorization.json" ]]

# A scratchpad-only commit with no .factory-state at all stays fail-closed.
rm -rf "$tmp/.factory-state"
git -C "$tmp" restore --staged .ralph 2>/dev/null || true
printf '# No state\n\n## Next\n\n- nostate.\n' > "$tmp/.ralph/agent/scratchpad.md"
git -C "$tmp" add -f .ralph/agent/scratchpad.md
set +e
(cd "$tmp" && git commit -m nostate >/dev/null 2>&1)
nostate_rc=$?
set -e
[[ $nostate_rc -eq 1 ]]

# The argv-level shim rejects hook-bypass attempts and missing guards, and
# delegates non-commit and substantive operations transparently.
for bypass in "--no-verify" "-n"; do
    set +e
    (cd "$tmp" && "$SHIM" commit "$bypass" -m "bypass-$bypass" >/dev/null 2>&1)
    rc=$?
    set -e
    [[ $rc -eq 1 ]] || { echo "test-git-commit-guard: shim accepted $bypass" >&2; exit 1; }
done
for argument in "-c core.hooksPath=/tmp/x commit -m hooks" \
        "--config core.hooksPath=/tmp/x commit -m hooks"; do
    set +e
    # shellcheck disable=SC2086 # intended word splitting: each item is a full argv fragment
    (cd "$tmp" && "$SHIM" $argument >/dev/null 2>&1)
    rc=$?
    set -e
    [[ $rc -eq 1 ]] || { echo "test-git-commit-guard: shim accepted hooksPath redirect" >&2; exit 1; }
done
set +e
(cd "$tmp" && GIT_CONFIG_COUNT=1 "$SHIM" commit -m gconfig >/dev/null 2>&1)
rc=$?
set -e
[[ $rc -eq 1 ]]
git -C "$tmp" config core.hooksPath /tmp/other-hooks
set +e
(cd "$tmp" && "$SHIM" commit -m hooked >/dev/null 2>&1)
rc=$?
set -e
[[ $rc -eq 1 ]]
git -C "$tmp" config --unset core.hooksPath
mv "$tmp/.git/hooks/pre-commit" "$tmp/.git/hooks/pre-commit.bak"
set +e
(cd "$tmp" && "$SHIM" commit -m nohook >/dev/null 2>&1)
rc=$?
set -e
[[ $rc -eq 1 ]]
mv "$tmp/.git/hooks/pre-commit.bak" "$tmp/.git/hooks/pre-commit"
printf '# Shim handoff\n\n## Verification\n\n- shim works.\n' > "$tmp/.ralph/agent/scratchpad.md"
printf 'shim\n' >> "$tmp/source.txt"
git -C "$tmp" add -A
set +e
(cd "$tmp" && "$SHIM" commit -m "substantive via shim" >/dev/null 2>&1)
rc=$?
set -e
[[ $rc -eq 0 ]]
(cd "$tmp" && "$SHIM" status --porcelain >/dev/null)
(cd "$tmp" && "$SHIM" log --oneline -1 >/dev/null)

# The tool-call extension rewrites simple direct commit verbs to the shim and
# blocks hook-bypass markers in any git-invoking command.
if command -v node >/dev/null 2>&1; then
    extension="$PROJECT_ROOT/scripts/pi-ralph-emit-extension.mjs"
    node - "$extension" <<'JS' || { echo 'test-git-commit-guard: extension git-boundary checks failed' >&2; exit 1; }
const extension = process.argv[2];
const module = await import(extension);
const { rewriteGitCommitCommand } = module;
const shim = "./scripts/pi-cli-shims/git";
const checks = [
    ["git commit -m 'factory: refresh scratchpad handoff'",
        (r) => r.matched && r.command.startsWith(shim) && !r.blocked],
    ["cd /tmp/x && git commit -am done",
        (r) => r.matched && r.command.includes(shim) && !r.blocked],
    ["git -C /tmp/x commit -m done",
        (r) => r.matched && r.command.startsWith(shim) && !r.blocked],
    ["git add .ralph/agent/scratchpad.md && git commit -m x",
        (r) => !r.matched && !r.blocked],
    ["git commit --no-verify -m x", (r) => r.blocked],
    ["/usr/bin/git commit --no-verify -m x", (r) => r.blocked],
    ["git -c core.hooksPath=/dev/null commit -m x", (r) => r.blocked],
    ["GIT_CONFIG_COUNT=1 git commit -m x", (r) => r.blocked],
    ["echo ok && git cherry-pick abc123", (r) => r.blocked],
    ["git revert HEAD", (r) => r.blocked],
    ["git rebase main", (r) => r.blocked],
    ["git pull origin main", (r) => r.blocked],
    ["git am fix.patch", (r) => r.blocked],
    ["git commit -m 'revert the broken pull request'", (r) => r.matched && !r.blocked],
    ["git status --short", (r) => !r.matched && !r.blocked],
    ["git log --oneline -5", (r) => !r.matched && !r.blocked],
    ["git add src/foo.c && git commit -m 'implement'", (r) => !r.matched && !r.blocked],
];
for (const [command, accept] of checks) {
    const result = rewriteGitCommitCommand(command);
    if (!accept(result)) {
        console.error(`extension check failed: ${command}`);
        console.error(JSON.stringify(result));
        process.exit(1);
    }
}
JS
fi

echo 'test: fail-closed Git commit boundary passed'
