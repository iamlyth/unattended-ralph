#!/usr/bin/env bash
# check-spec-provided.sh — Hard-block planning until a human supplies a real
# canonical specification.
#
# The generic boilerplate ships docs/SPEC.md as a neutral committed placeholder
# that contains the marker `SPEC_PENDING_HUMAN_SUPPLY`. Planning (and every
# completion gate) refuses to run while that marker is present: an autonomous
# worker must never plan against a placeholder or invent the product contract.
# A human replaces the placeholder with the real canonical spec and commits it;
# the marker is then absent and this gate passes.
set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
PROJECT_ROOT=$(cd -- "$SCRIPT_DIR/.." && pwd)
cd -- "$PROJECT_ROOT"

SPEC_PATH=$(python3 - <<'PY'
import tomllib
with open('.factory/config.toml', 'rb') as stream:
    spec = tomllib.load(stream)['project']['spec']
if not isinstance(spec, str) or not spec:
    raise SystemExit('check-spec-provided: project.spec is not a path')
print(spec)
PY
)
[[ -n "$SPEC_PATH" && -f "$SPEC_PATH" && ! -L "$SPEC_PATH" ]] || {
    echo "check-spec-provided: canonical spec '$SPEC_PATH' is missing or unsafe" >&2
    exit 1
}
if grep -q 'SPEC_PENDING_HUMAN_SUPPLY' "$SPEC_PATH"; then
    echo "check-spec-provided: docs/SPEC.md is still the neutral placeholder." >&2
    echo "check-spec-provided: planning is hard-blocked until a human supplies and commits a real canonical specification." >&2
    exit 1
fi
echo "check-spec-provided: canonical spec $SPEC_PATH is present"
