#!/usr/bin/env bash
# Adversarial regression (BUG-0013): the isolated boilerplate scenario suite
# must be immune to ambient lifecycle state. The implementation completion
# gate runs final-gate.sh with FACTORY_FINAL_GATE_ATTEST=1; before the fix
# that flag leaked into nested final-gate invocations inside the
# maintenance-planning completion chain, whose deliberately dirty root then
# failed the attestation clean-tree check and made every completion attempt
# bounce through bounded recovery with no semantic movement.
set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
PROJECT_ROOT=$(cd -- "$SCRIPT_DIR/../../.." && pwd)
cd -- "$PROJECT_ROOT"

# The suite runner must neutralize ambient lifecycle state before any test.
grep -q '^unset FACTORY_FINAL_GATE_ATTEST' .factory/tools/verify-boilerplate.sh || {
    echo "test: verify-boilerplate does not sanitize the ambient attestation flag" >&2
    exit 1
}
grep -q 'FACTORY_RALPH_CYCLE_ID' .factory/tools/verify-boilerplate.sh || {
    echo "test: verify-boilerplate does not sanitize ambient cycle state" >&2
    exit 1
}
grep -q '^unset FACTORY_FINAL_GATE_ATTEST' .factory/tests/legacy/test-maintenance-planning-completion.sh || {
    echo "test: maintenance-planning chain does not scope its own attestation state" >&2
    exit 1
}

# Recreate the exact completion-gate leak: the attestation flag set in the
# environment of the maintenance-planning chain. The chain must pass.
FACTORY_FINAL_GATE_ATTEST=1 ./.factory/tests/legacy/test-maintenance-planning-completion.sh || {
    echo "test: maintenance-planning chain failed under ambient attestation state" >&2
    exit 1
}

echo "test: boilerplate environment isolation checks passed"
