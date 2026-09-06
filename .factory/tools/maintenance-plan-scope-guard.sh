#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
PROJECT_ROOT=$(cd -- "$SCRIPT_DIR/../.." && pwd)
cd -- "$PROJECT_ROOT"

mapfile -t CHANGED < <({
    git diff --name-only
    git diff --cached --name-only
    git ls-files --others --exclude-standard
} | sort -u)
for path in "${CHANGED[@]}"; do
    case "$path" in
        .factory/artifacts/maintenance-plan.md|.ralph/agent/scratchpad.md) ;;
        *) echo "maintenance-plan-scope: planning modified forbidden path: $path" >&2; exit 1 ;;
    esac
done
echo "maintenance-plan-scope: changes confined to maintenance plan and scratchpad"
