#!/usr/bin/env bash
# Generic visual-capture driver scaffold — fails closed by design.
#
# Contract (from the generic visual-audit-capture runner, .factory/tools/visual-audit-capture.py):
#   <driver> <state_id> <output.png> <commit>
#
# The generic boilerplate has no installed product binary and no display
# harness, so this adapter CANNOT capture anything. Every invocation fails
# closed with a clear message. A consumer must replace this adapter with an
# installed exact-commit capture driver for its product before enabling
# visual audit: launch the INSTALLED binary built from the exact commit,
# navigate semantically to the requested state (see
# .factory/visual-audit-inventory.json), capture the window, and exit 0 only
# when the screenshot exists. Tests exercise this fail-closed contract with
# explicit test-only mock drivers, never with this adapter.
set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
PROJECT_ROOT=$(cd -- "$SCRIPT_DIR/../.." && pwd)
cd -- "$PROJECT_ROOT"

[[ $# -eq 3 ]] || { echo "usage: visual-capture-driver.sh <state_id> <output.png> <commit>" >&2; exit 64; }
STATE=$1
OUTPUT=$2
COMMIT=$3
: "$OUTPUT" # Reserved for the consumer adapter's required output path.

# No generic capture exists: there is no consumer product binary, display
# harness, or visual state implementation here. Fail closed rather than emit
# a placeholder image that could be mistaken for real capture evidence.
echo "visual-capture-driver: consumer must implement installed exact-commit capture; the generic scaffold cannot capture state '$STATE'" >&2
echo "visual-capture-driver: implement the <driver> <state_id> <output.png> <commit> contract against your installed product build (commit $COMMIT) and re-run" >&2
exit 1
