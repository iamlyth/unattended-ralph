#!/usr/bin/env bash
set -euo pipefail
ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../../.." && pwd)
python3 "$ROOT/.factory/tests/legacy/test_runner_framework.py" RunnerFrameworkTests.test_signer_has_no_direct_oracle RunnerFrameworkTests.test_forced_command_and_bootstrap_are_exact
echo 'test: broker-only signer fixture checks passed'
