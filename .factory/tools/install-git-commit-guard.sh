#!/usr/bin/env bash
# Fail-closed Ralph Git commit boundary.
#
# Installs six launcher hooks in .git/hooks, each executing the tracked
# .factory/tools/git-commit-guard.sh boundary:
#
#   pre-commit          policy gate for `git commit`
#   prepare-commit-msg  policy gate for `git commit` and `git rebase`
#   pre-merge-commit    policy gate for `git merge`
#   applypatch-msg      policy gate for `git am`
#   pre-applypatch      policy gate for `git am`
#   commit-msg          one-shot token consumption; runs for `git commit` and
#                       `git merge`
#
# commit-msg is the single consumption point, so the at-most-one final-handoff
# guarantee holds across every hook-coverable commit path. Commit-creation
# verbs with no hook coverage (cherry-pick, revert) and the patch/replay verbs
# are refused by .factory/tools/pi-cli-shims/git and .factory/tools/pi-ralph-emit-extension.mjs
# at the model's command boundary; the harness only ever creates commits via
# `git commit`. Idempotent and safe to run before every Ralph launch and at
# verify time; --check reports whether the installed hooks match the tracked
# guard without writing anything.
set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
PROJECT_ROOT=$(cd -- "$SCRIPT_DIR/../.." && pwd)
GUARD="$PROJECT_ROOT/.factory/tools/git-commit-guard.sh"
HOOK_NAMES=(pre-commit prepare-commit-msg pre-merge-commit applypatch-msg pre-applypatch commit-msg)
CHECK=false
for arg in "$@"; do
    case "$arg" in
        --check) CHECK=true ;;
        *) echo "install-git-commit-guard: unknown argument '$arg'" >&2; exit 2 ;;
    esac
done

cd -- "$PROJECT_ROOT"
[[ -d .git && ! -L .git ]] || {
    echo "install-git-commit-guard: not a Git repository" >&2
    exit 2
}
[[ -f "$GUARD" && ! -L "$GUARD" && -x "$GUARD" ]] || {
    echo "install-git-commit-guard: tracked guard is missing or not executable: $GUARD" >&2
    exit 2
}

HOOK_DIR=$(git rev-parse --git-path hooks)
if [[ "$HOOK_DIR" != /* ]]; then
    HOOK_DIR=$(realpath -e -- "$HOOK_DIR")
fi
mkdir -p "$HOOK_DIR"

launcher() {
    local hook=$1
    cat <<EOF
#!/usr/bin/env bash
set -euo pipefail
HOOK_DIR=\$(cd -- "\$(dirname -- "\${BASH_SOURCE[0]}")" && pwd)
PROJECT_ROOT=\$(cd -- "\$HOOK_DIR/../.." && pwd)
exec "\$PROJECT_ROOT/.factory/tools/git-commit-guard.sh" --hook $hook
EOF
}

failures=0
for hook_name in "${HOOK_NAMES[@]}"; do
    HOOK="$HOOK_DIR/$hook_name"
    installed_matches() {
        [[ -f "$HOOK" && ! -L "$HOOK" && -x "$HOOK" ]] || return 1
        cmp -s "$HOOK" <(launcher "$hook_name")
    }

    if [[ "$CHECK" == true ]]; then
        if installed_matches; then
            echo "install-git-commit-guard: $hook_name boundary installed"
            continue
        fi
        echo "install-git-commit-guard: $hook_name boundary missing or does not match the tracked guard" >&2
        failures=1
        continue
    fi

    if [[ -e "$HOOK" || -L "$HOOK" ]] && ! installed_matches; then
        echo "install-git-commit-guard: refusing to replace a foreign $hook_name hook: $HOOK" >&2
        exit 2
    fi

    temporary="$HOOK.tmp.$$"
    launcher "$hook_name" > "$temporary"
    chmod 0755 "$temporary"
    mv -f "$temporary" "$HOOK"
    installed_matches || {
        echo "install-git-commit-guard: $hook_name installation verification failed" >&2
        exit 1
    }
    echo "install-git-commit-guard: $hook_name boundary installed"
done

exit "$failures"
