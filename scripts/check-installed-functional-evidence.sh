#!/usr/bin/env bash
set -euo pipefail

root=$(git rev-parse --show-toplevel)
evidence=$root/.factory-state/installed-functional-evidence.env
[[ ! -L $evidence && -f $evidence ]] || {
    echo "installed-functional-evidence: missing or unsafe; run ./scripts/verify-project.sh" >&2
    exit 1
}
mapfile -d '' -t EVIDENCE_FIELDS < <(python3 - "$evidence" <<'PY'
import os, re, sys
from pathlib import Path
expected = {'schema', 'commit', 'test', 'result', 'skipped'}
values = {}
for line in Path(sys.argv[1]).read_text(encoding='utf-8').splitlines():
    if not line or '=' not in line:
        raise SystemExit(1)
    key, value = line.split('=', 1)
    if key not in expected or key in values or not value:
        raise SystemExit(1)
    values[key] = value
if set(values) != expected:
    raise SystemExit(1)
checks = {
    'schema': 'factory-installed-functional/v1',
    'test': 'test_installed_functional',
    'result': 'PASS',
    'skipped': '0',
}
if any(values[key] != value for key, value in checks.items()) or not re.fullmatch(r'[0-9a-f]{40}', values['commit']):
    raise SystemExit(1)
for key in ('schema', 'commit', 'test', 'result', 'skipped'):
    os.write(1, values[key].encode() + b'\0')
PY
)
if (( ${#EVIDENCE_FIELDS[@]} != 5 )); then
    echo "installed-functional-evidence: malformed or non-passing evidence" >&2
    exit 1
fi
commit=${EVIDENCE_FIELDS[1]}
git merge-base --is-ancestor "$commit" HEAD || {
    echo "installed-functional-evidence: tested commit is not an ancestor of HEAD" >&2
    exit 1
}
if ! git diff --quiet "$commit" -- CMakeLists.txt config.h.in data packaging src tests \
        scripts/verify-project.sh; then
    echo "installed-functional-evidence: production or acceptance inputs changed since $commit" >&2
    exit 1
fi
echo "installed-functional-evidence: PASS at $commit with zero skips"
