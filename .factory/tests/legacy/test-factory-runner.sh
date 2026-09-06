#!/usr/bin/env bash
set -euo pipefail
ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../../.." && pwd)
python3 "$ROOT/.factory/tests/legacy/test_runner_framework.py"
echo 'test: generic runner protocol fixtures passed (no live evidence claimed)'
