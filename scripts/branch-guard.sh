#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
PROJECT_ROOT=$(cd -- "$SCRIPT_DIR/.." && pwd)
cd -- "$PROJECT_ROOT"

EXPECTED_BRANCH=${FACTORY_DEVELOPMENT_BRANCH:-$(python3 - <<'PY'
import tomllib
with open('.factory/config.toml', 'rb') as stream:
    print(tomllib.load(stream)['project']['development_branch'])
PY
)}
CURRENT_BRANCH=$(git branch --show-current)

if [[ "$CURRENT_BRANCH" == "main" ]]; then
    echo "branch-guard: refusing autonomous work on protected release branch 'main'" >&2
    exit 1
fi

if [[ "$CURRENT_BRANCH" != "$EXPECTED_BRANCH" ]]; then
    if [[ ${FACTORY_ALLOW_TRIAL_BRANCH:-0} == 1 && "$CURRENT_BRANCH" == factory/* ]]; then
        echo "branch-guard: warning: allowing explicit factory trial branch '$CURRENT_BRANCH'" >&2
    else
        echo "branch-guard: expected '$EXPECTED_BRANCH', found '$CURRENT_BRANCH'" >&2
        echo "branch-guard: merge the boilerplate into '$EXPECTED_BRANCH' before autonomous work" >&2
        exit 1
    fi
fi

WORKTREE_COUNT=$(git worktree list --porcelain | grep -c '^worktree ' || true)
if (( WORKTREE_COUNT != 1 )); then
    echo "branch-guard: exactly one working tree is allowed; found $WORKTREE_COUNT" >&2
    exit 1
fi

echo "branch-guard: branch=$CURRENT_BRANCH worktrees=$WORKTREE_COUNT"
