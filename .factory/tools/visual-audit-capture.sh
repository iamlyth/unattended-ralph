#!/usr/bin/env bash
# Thin wrapper: deterministic exact-commit visual capture (serial/isolated).
# All lease/driver/provenance logic lives in visual-audit-capture.py so the
# lease fd is held with CLOEXEC in the process that runs the untrusted
# capture driver and never leaks to it or to the vision model.
set -euo pipefail
SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
PROJECT_ROOT=$(cd -- "$SCRIPT_DIR/../.." && pwd)
cd -- "$PROJECT_ROOT"
exec python3 "$SCRIPT_DIR/visual-audit-capture.py" "$@"
