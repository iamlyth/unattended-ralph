#!/usr/bin/env bash
# Fresh-process/order regression for the adopting project's exact toolchain.
set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
PROJECT_ROOT=$(cd -- "$SCRIPT_DIR/../.." && pwd)
PYTHON=$(readlink -f "$(command -v python3)")
TEST="$PROJECT_ROOT/.factory/tests/test-factory-confinement.py"
CASE=ProductionLaunchConfinementTests.test_confined_leaf_executes_exact_project_toolchain

# Deduplicate PATH without adding any executable source. Each invocation starts
# a new interpreter, proving no preceding test can populate mutable policy.
STRIPPED_PATH=$(python3 - <<'PY'
import os
kept = []
for entry in os.environ.get("PATH", "").split(os.pathsep):
    if entry and entry not in kept:
        kept.append(entry)
print(os.pathsep.join(kept))
PY
)
[[ -n "$STRIPPED_PATH" ]]

run_fresh() {
    env PATH="$1" "$PYTHON" -W error::ResourceWarning \
        "$TEST" "$CASE" >/dev/null
}

run_fresh "$STRIPPED_PATH"
run_fresh "$PATH"
run_fresh "$PATH"
run_fresh "$STRIPPED_PATH"

echo "test: generic confinement toolchain is fresh-process and order independent"
