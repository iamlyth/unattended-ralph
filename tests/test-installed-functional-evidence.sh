#!/usr/bin/env bash
set -euo pipefail
# Task 23: the generic installed-functional gate scans the dedicated generic
# evidence root (.factory-state/generic-evidence/) and accepts the unique
# valid namespace bound to the exact live audit coordinator base
# (coordinator.base_commit == receipt.evidence_commit == record.commit ==
# namespace name) and backed by a matching installed-harness
# machine receipt — and never the foreign root
# .factory-state/installed-functional-evidence.env.  This visible test
# builds a fixture authority, mints the installed-harness receipt through
# the real wrapper, publishes the generic evidence record, and proves every
# tamper class fails closed while the foreign root env is ignored and never
# rewritten.
source_root=${1:-$PWD}
root=$(mktemp -d)
sentinel=$(mktemp /tmp/factory-evidence-injection.XXXXXX)
rm -f -- "$sentinel"
trap 'rm -rf "$root"; rm -f -- "$sentinel"' EXIT
cd "$root"
git init -q
git config user.name test
git config user.email test@example.invalid

mkdir -p scripts .factory/loop .factory/tests .factory-state/audit-receipts \
    src tests data packaging
cp "$source_root/scripts/check-installed-functional-evidence.sh" scripts/
cp "$source_root/scripts/machine-receipt.py" scripts/
cp "$source_root/.factory/loop/evidence.py" .factory/loop/
cp "$source_root/.factory/loop/gitutil.py" .factory/loop/
cp "$source_root/.factory/loop/lock.py" .factory/loop/
cp "$source_root/.factory/campaign-receipt-policy.json" .factory/
printf '#!/usr/bin/env bash\nset -euo pipefail\necho "test-factory-installed: all checks passed"\n' \
    > .factory/tests/test-factory-installed.sh
chmod +x .factory/tests/test-factory-installed.sh
printf '.factory-state/\n' > .gitignore
printf 'source\n' > src/app.c
printf 'cmake\n' > CMakeLists.txt
printf 'config\n' > config.h.in
printf 'verify\n' > scripts/verify-project.sh
git add .
git commit -qm baseline
commit=$(git rev-parse HEAD)

# The fixture audit coordinator binds round 1 and the exact commit.
nonce=$(python3 -c "import hashlib; print(hashlib.sha256(b'fixture-installed-nonce').hexdigest())")
printf '{"schema": "ralph-audit-coordinator/v1", "round": 1, "base_commit": "%s", "nonce": "%s", "created_at": 1}\n' \
    "$commit" "$nonce" > .factory-state/audit-coordinator.json
chmod 600 .factory-state/audit-coordinator.json

# The installed-harness receipt is minted through the real wrapper (the stub
# suite exits 0) — never hand-fabricated.
./scripts/machine-receipt.py --root "$PWD" --tag installed-harness-smoke \
    --audit-round 1 --evidence-commit "$commit" --nonce "$nonce" \
    -- ./.factory/tests/test-factory-installed.sh >/dev/null

receipt_sha256=$(sha256sum .factory-state/audit-receipts/installed-harness-smoke.json | awk '{print $1}')
stdout_sha256=$(sha256sum .factory-state/audit-receipts/installed-harness-smoke.stdout | awk '{print $1}')
mkdir -p ".factory-state/generic-evidence/$commit"
chmod 700 ".factory-state/generic-evidence/$commit"
cat > ".factory-state/generic-evidence/$commit/installed-functional.json" <<EOF
{"schema": "factory-generic-installed-functional/v1", "commit": "$commit",
 "test": "test_installed_functional", "result": "PASS", "skipped": 0,
 "receipt": ".factory-state/audit-receipts/installed-harness-smoke.json",
 "receipt_sha256": "$receipt_sha256", "suite_stdout_sha256": "$stdout_sha256",
 "coordinator_round": 1, "coordinator_nonce": "$nonce"}
EOF
chmod 600 ".factory-state/generic-evidence/$commit/installed-functional.json"

./scripts/check-installed-functional-evidence.sh >/dev/null

# A foreign root env is ignored and never satisfies the exact-HEAD gate.
cat > .factory-state/installed-functional-evidence.env <<EOF
schema=factory-installed-functional/v1
commit=$commit
test=test_installed_functional
result=PASS
skipped=0
EOF
./scripts/check-installed-functional-evidence.sh >/dev/null

# Changed production input invalidates the evidence.
printf 'changed\n' >> src/app.c
if ./scripts/check-installed-functional-evidence.sh >/dev/null 2>&1; then
    echo 'evidence guard accepted changed production input' >&2
    exit 1
fi
git checkout -q -- src/app.c

# Skipped generic evidence is rejected.
sed -i 's/"skipped": 0/"skipped": 1/' ".factory-state/generic-evidence/$commit/installed-functional.json"
if ./scripts/check-installed-functional-evidence.sh >/dev/null 2>&1; then
    echo 'evidence guard accepted skipped functional test' >&2
    exit 1
fi

# Wrong result is rejected.
sed -i 's/"skipped": 1/"skipped": 0/' ".factory-state/generic-evidence/$commit/installed-functional.json"
sed -i 's/"result": "PASS"/"result": "FAIL"/' ".factory-state/generic-evidence/$commit/installed-functional.json"
if ./scripts/check-installed-functional-evidence.sh >/dev/null 2>&1; then
    echo 'evidence guard accepted failing result' >&2
    exit 1
fi

# Wrong test name is rejected.
sed -i 's/"result": "FAIL"/"result": "PASS"/' ".factory-state/generic-evidence/$commit/installed-functional.json"
sed -i 's/"test": "test_installed_functional"/"test": "test_other"/' ".factory-state/generic-evidence/$commit/installed-functional.json"
if ./scripts/check-installed-functional-evidence.sh >/dev/null 2>&1; then
    echo 'evidence guard accepted wrong test name' >&2
    exit 1
fi

# Wrong schema is rejected.
sed -i 's/"test": "test_other"/"test": "test_installed_functional"/' ".factory-state/generic-evidence/$commit/installed-functional.json"
sed -i 's/factory-generic-installed-functional\/v1/other/' ".factory-state/generic-evidence/$commit/installed-functional.json"
if ./scripts/check-installed-functional-evidence.sh >/dev/null 2>&1; then
    echo 'evidence guard accepted wrong schema' >&2
    exit 1
fi

# Evidence is parsed as data, never executed as shell code: a forged extra
# field (with an injected shell command value) makes the record schema
# invalid and the sentinel must never be created.
sed -i 's/other/factory-generic-installed-functional\/v1/' ".factory-state/generic-evidence/$commit/installed-functional.json"
python3 - ".factory-state/generic-evidence/$commit/installed-functional.json" "$sentinel" <<'PY'
import json, sys
record = json.load(open(sys.argv[1], encoding='utf-8'))
record['touch'] = "touch -- '" + sys.argv[2] + "'"
open(sys.argv[1], 'w', encoding='utf-8').write(json.dumps(record))
PY
if ./scripts/check-installed-functional-evidence.sh >/dev/null 2>&1 || [[ -e "$sentinel" ]]; then
    echo 'evidence guard accepted or executed injected content' >&2
    exit 1
fi

# A stale (non-HEAD) commit in the record is rejected even when the foreign
# root env remains valid.
python3 - ".factory-state/generic-evidence/$commit/installed-functional.json" <<'PY'
import json, sys
record = json.load(open(sys.argv[1], encoding='utf-8'))
record.pop('touch', None)
record['commit'] = '0' * 40
open(sys.argv[1], 'w', encoding='utf-8').write(json.dumps(record))
PY
if ./scripts/check-installed-functional-evidence.sh >/dev/null 2>&1; then
    echo 'evidence guard accepted a stale commit' >&2
    exit 1
fi

# A forged receipt (different exit code) is rejected through the digest
# binding and the hardened validation.
python3 - ".factory-state/audit-receipts/installed-harness-smoke.json" <<'PY'
import json, sys
receipt = json.load(open(sys.argv[1], encoding='utf-8'))
receipt['exit_code'] = 7
open(sys.argv[1], 'w', encoding='utf-8').write(json.dumps(receipt))
PY
if ./scripts/check-installed-functional-evidence.sh >/dev/null 2>&1; then
    echo 'evidence guard accepted a forged receipt' >&2
    exit 1
fi

# Symlinked and missing records are rejected entirely.
rm ".factory-state/generic-evidence/$commit/installed-functional.json"
ln -s /dev/null ".factory-state/generic-evidence/$commit/installed-functional.json"
if ./scripts/check-installed-functional-evidence.sh >/dev/null 2>&1; then
    echo 'evidence guard accepted a symlinked record' >&2
    exit 1
fi
rm ".factory-state/generic-evidence/$commit/installed-functional.json"
if ./scripts/check-installed-functional-evidence.sh >/dev/null 2>&1; then
    echo 'evidence guard accepted a missing record' >&2
    exit 1
fi
echo 'installed-functional-evidence tests passed'
