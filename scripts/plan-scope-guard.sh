#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
PROJECT_ROOT=$(cd -- "$SCRIPT_DIR/.." && pwd)
cd -- "$PROJECT_ROOT"

# Planning is hard-blocked until the canonical specification is bound and
# real: check-spec-provided.sh gates on `.factory/config.toml [project].spec`
# (this cycle: docs/FACTORY-LOOP-SPEC.md) and refuses any bound spec that is
# missing/unsafe or still carries the SPEC_PENDING_HUMAN_SUPPLY placeholder
# marker. The adopting-product placeholder docs/SPEC.md is never planned
# against.
./scripts/check-spec-provided.sh

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
