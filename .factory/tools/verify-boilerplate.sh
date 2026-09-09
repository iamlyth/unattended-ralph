#!/bin/sh
# Complete generic hidden-harness verification gate.
set -eu
root=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)
cd "$root"

./.factory/tools/check-generic-leakage.sh
./.factory/tools/check-docs-sync.sh
./.factory/tools/validate-extension-conformance.py
python3 -m compileall -q .factory/loop .factory/tools .factory/tests
python3 .factory/tests/test-factory-plan-parser.py
python3 .factory/tests/test-factory-selector.py
python3 .factory/tests/test-factory-state.py
python3 .factory/tests/test-factory-readiness.py
python3 .factory/tests/test-factory-pre-round.py
python3 .factory/tests/test-factory-usage.py
python3 .factory/tests/test-factory-lock.py
python3 .factory/tests/test-factory-launch.py
python3 .factory/tests/test-factory-task-budget.py
python3 .factory/tests/test-factory-scheduler.py
python3 .factory/tests/test-factory-campaign.py
python3 .factory/tests/test-factory-findings.py
python3 .factory/tests/test-factory-verifier-failure.py
python3 .factory/tests/test-factory-evidence.py
./.factory/tests/test-factory-footprint.sh
./.factory/tests/test-factory-installed.sh
./.factory/tests/test-factory-migration.sh
./.factory/tests/test-factory-adversarial.sh
./.factory/tests/legacy/test-factory-runner.sh
./.factory/tests/legacy/test-runner-signer.sh
./.factory/tools/check-capability-contracts.py
./.factory/tools/validate-conformance.py planning .factory/artifacts/conformance.json
# A verification run must not leave Python caches in the repository.
find .factory -type d -name __pycache__ -prune -exec rm -rf {} +
echo "verify-boilerplate: all generic gates passed"
