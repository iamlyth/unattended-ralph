#!/usr/bin/env bash
# Supervise the single-writer implementation loop and resume after quota resets.
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
            echo "Usage: scripts/ralph-run.sh [--resume] [--no-tui]"
            exit 0
            ;;
        *) echo "ralph-run: unknown option '$1'" >&2; exit 2 ;;
    esac
done

cd -- "$PROJECT_ROOT"
command -v "$RALPH_BIN" >/dev/null || { echo "ralph-run: Ralph executable not found: $RALPH_BIN" >&2; exit 2; }
command -v pi2 >/dev/null || { echo "ralph-run: pi2 is not available in this shell" >&2; exit 2; }
./scripts/branch-guard.sh
./scripts/check-factory-environment.py
./scripts/check-plan-freshness.sh

if [[ "$RESUME" == false ]] && [[ -n $(git status --porcelain --untracked-files=normal) ]]; then
    echo "ralph-run: start from a clean Git tree; commit the spec and implementation plan first" >&2
    exit 1
fi

# shellcheck source=scripts/factory-lock.sh
source "$SCRIPT_DIR/factory-lock.sh"
# shellcheck source=scripts/ralph-supervision.sh
source "$SCRIPT_DIR/ralph-supervision.sh"
factory_lock_acquire "$PROJECT_ROOT/.factory-lock"
mkdir -p .factory-state
printf '%s\n' implementation > .factory-state/loop-mode

while true; do
    ./scripts/ollama-usage-guard.sh --wait
    ralph_supervision_begin implementation

    command=("$RALPH_BIN" -c ralph.yml run --exclusive)
    $RESUME && command+=(--continue)
    $TUI || command+=(--no-tui)

    set +e
    "${command[@]}"
    rc=$?
    set -e

    if (( rc == 0 )); then
        ./scripts/final-gate.sh --implementation
        payload=$(printf '{"loop":{"workspace":"%s","id":"implementation-final"},"iteration":{"current":"final"}}' "$PROJECT_ROOT")
        printf '%s' "$payload" | ./scripts/git-commit-hook.sh
        [[ -z $(git status --porcelain --untracked-files=normal) ]] || {
            echo "ralph-run: completion left a dirty Git tree" >&2
            exit 1
        }
        echo "ralph-run: implementation loop completed"
        exit 0
    fi
    if (( rc == 130 || rc == 143 )); then
        echo "ralph-run: interrupted; resume later with ./scripts/ralph-recover.sh" >&2
        exit "$rc"
    fi
    set +e
    rejected_loop_id=$(ralph_supervision_consume_rejection implementation "$PROJECT_ROOT")
    rejection_rc=$?
    set -e
    if (( rejection_rc == 0 )); then
        echo "ralph-run: final gate rejected premature completion; continuing the active cycle" >&2
        ./scripts/ralph-recover.sh --mode implementation --loop-id "$rejected_loop_id" --prepare-only
        RESUME=true
        continue
    elif (( rejection_rc != 1 )); then
        echo "ralph-run: invalid completion-rejection marker; refusing automatic recovery" >&2
        exit 1
    fi

    set +e
    ./scripts/ollama-usage-guard.sh --check
    quota_rc=$?
    set -e
    if (( quota_rc == 1 )); then
        echo "ralph-run: backend stopped while quota is blocked; waiting before automatic continuation" >&2
        ./scripts/ollama-usage-guard.sh --wait
        ./scripts/ralph-recover.sh --prepare-only
        RESUME=true
        continue
    fi

    echo "ralph-run: Ralph exited with status $rc for a non-quota failure" >&2
    exit "$rc"
done
