#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
PROJECT_ROOT=$(cd -- "$SCRIPT_DIR/.." && pwd)
cd -- "$PROJECT_ROOT"

mapfile -t CHANGED < <({
    git diff --name-only
    git diff --cached --name-only
    git ls-files --others --exclude-standard
} | sort -u)

for path in "${CHANGED[@]}"; do
    case "$path" in
        .factory/artifacts/implementation-plan.md|.ralph/agent/scratchpad.md) ;;
        *)
            echo "plan-scope: planning loop modified forbidden path: $path" >&2
            exit 1
            ;;
    esac
done

echo "plan-scope: planning changes are confined to the plan and recovery scratchpad"
