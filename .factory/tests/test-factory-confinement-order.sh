#!/usr/bin/env bash
# Fresh-process/order regression for the exact project-shell display toolchain.
set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
PROJECT_ROOT=$(cd -- "$SCRIPT_DIR/../.." && pwd)
PYTHON=$(readlink -f "$(command -v python3)")
TEST="$PROJECT_ROOT/.factory/tests/test-factory-confinement.py"
CASE=ProductionLaunchConfinementTests.test_confined_leaf_executes_exact_nix_python_and_display_toolchain

# Remove every PATH component that directly exposes xdotool. This reproduces
# verify-boilerplate's outer process even when this regression itself was
# entered from nix-shell. The exact project shell closure must supply the tool;
# no prior test/process may populate mutable module state for it.
STRIPPED_PATH=$(python3 - <<'PY'
import os
from pathlib import Path
kept = []
for entry in os.environ.get("PATH", "").split(os.pathsep):
    if not entry or entry in kept:
        continue
    if (Path(entry) / "xdotool").exists():
        continue
    kept.append(entry)
print(os.pathsep.join(kept))
PY
)
[[ -n "$STRIPPED_PATH" ]]

run_stripped() {
    env PATH="$STRIPPED_PATH" "$PYTHON" -W error::ResourceWarning \
        "$TEST" "$CASE" >/dev/null
}

run_declared_nix() {
    local command
    printf -v command '%q -W error::ResourceWarning %q %q >/dev/null' \
        "$PYTHON" "$TEST" "$CASE"
    nix-shell --run "$command"
}

# Each call is a fresh interpreter. Run both environment orders so neither the
# test module nor a preceding Nix-rich process can leak executable discovery.
run_stripped
run_declared_nix
run_declared_nix
run_stripped

echo "test: confinement toolchain is fresh-process and order independent"
