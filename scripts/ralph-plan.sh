#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
PROJECT_ROOT=$(cd -- "$SCRIPT_DIR/.." && pwd)
RALPH_BIN=${RALPH_BIN:-ralph}
RESUME=false
TUI=true

while (( $# > 0 )); do
    case "$1" in
        --resume) RESUME=true; shift ;;
        --no-tui) TUI=false; shift ;;
        -h|--help)
            echo "Usage: scripts/ralph-plan.sh [--resume] [--no-tui]"
            exit 0
            ;;
        *) echo "ralph-plan: unknown option '$1'" >&2; exit 2 ;;
    esac
done

cd -- "$PROJECT_ROOT"
command -v "$RALPH_BIN" >/dev/null || { echo "ralph-plan: Ralph executable not found: $RALPH_BIN" >&2; exit 2; }
command -v pi2 >/dev/null || { echo "ralph-plan: pi2 is not available in this shell" >&2; exit 2; }
./scripts/branch-guard.sh

SPEC=$(python3 - <<'PY'
import tomllib
with open('factory.toml', 'rb') as stream:
    print(tomllib.load(stream)['project']['spec'])
PY
)
[[ -f "$SPEC" ]] || { echo "ralph-plan: missing specification '$SPEC'" >&2; exit 1; }
if ! git diff --quiet -- "$SPEC" || ! git diff --cached --quiet -- "$SPEC"; then
    echo "ralph-plan: commit '$SPEC' before planning" >&2
    exit 1
fi

# shellcheck source=scripts/factory-lock.sh
source "$SCRIPT_DIR/factory-lock.sh"
factory_lock_acquire "$PROJECT_ROOT/.factory-lock"
mkdir -p .factory-state
printf '%s\n' planning > .factory-state/loop-mode
while true; do
    ./scripts/ollama-usage-guard.sh --wait

    command=("$RALPH_BIN" -c ralph.plan.yml run --exclusive)
    $RESUME && command+=(--continue)
    $TUI || command+=(--no-tui)
    set +e
    "${command[@]}"
    rc=$?
    set -e

    if (( rc == 0 )); then
        ./scripts/final-gate.sh --planning
        payload=$(printf '{"loop":{"workspace":"%s","id":"planning-final"},"iteration":{"current":"final"}}' "$PROJECT_ROOT")
        printf '%s' "$payload" | ./scripts/git-commit-hook.sh --plan-only
        [[ -z $(git status --porcelain --untracked-files=normal) ]] || {
            echo "ralph-plan: completion left a dirty Git tree" >&2
            exit 1
        }
        ./scripts/check-plan-freshness.sh
        printf 'ralph-plan: plan is committed and fresh for %s\n' "$SPEC"
        exit 0
    fi
    if (( rc == 130 || rc == 143 )); then
        echo "ralph-plan: interrupted; resume with ./scripts/ralph-recover.sh --mode planning" >&2
        exit "$rc"
    fi

    set +e
    ./scripts/ollama-usage-guard.sh --check
    quota_rc=$?
    set -e
    if (( quota_rc == 1 )); then
        echo "ralph-plan: quota blocked during planning; waiting before automatic continuation" >&2
        ./scripts/ollama-usage-guard.sh --wait
        ./scripts/ralph-recover.sh --mode planning --prepare-only
        RESUME=true
        continue
    fi
    echo "ralph-plan: Ralph exited with status $rc for a non-quota failure" >&2
    exit "$rc"
done
