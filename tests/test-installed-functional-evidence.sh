#!/usr/bin/env bash
set -euo pipefail
source_root=${1:-$PWD}
root=$(mktemp -d)
sentinel=$(mktemp /tmp/factory-evidence-injection.XXXXXX)
rm -f -- "$sentinel"
trap 'rm -rf "$root"; rm -f -- "$sentinel"' EXIT
cd "$root"
git init -q
git config user.name test
git config user.email test@example.invalid
mkdir -p scripts .factory-state src tests data packaging
cp "$source_root/scripts/check-installed-functional-evidence.sh" scripts/
printf 'source\n' > src/app.c
printf 'cmake\n' > CMakeLists.txt
printf 'config\n' > config.h.in
printf 'verify\n' > scripts/verify-project.sh
git add .
git commit -qm baseline
commit=$(git rev-parse HEAD)
cat > .factory-state/installed-functional-evidence.env <<EOF
schema=factory-installed-functional/v1
commit=$commit
test=test_installed_functional
result=PASS
skipped=0
EOF
./scripts/check-installed-functional-evidence.sh >/dev/null
printf 'changed\n' >> src/app.c
if ./scripts/check-installed-functional-evidence.sh >/dev/null 2>&1; then
    echo 'evidence guard accepted changed production input' >&2
    exit 1
fi
git checkout -q -- src/app.c
sed -i 's/skipped=0/skipped=1/' .factory-state/installed-functional-evidence.env
if ./scripts/check-installed-functional-evidence.sh >/dev/null 2>&1; then
    echo 'evidence guard accepted skipped functional test' >&2
    exit 1
fi
# Reject wrong result
sed -i 's/skipped=1/skipped=0/' .factory-state/installed-functional-evidence.env
sed -i 's/result=PASS/result=FAIL/' .factory-state/installed-functional-evidence.env
if ./scripts/check-installed-functional-evidence.sh >/dev/null 2>&1; then
    echo 'evidence guard accepted failing result' >&2
    exit 1
fi
# Reject wrong test name
sed -i 's/result=FAIL/result=PASS/' .factory-state/installed-functional-evidence.env
sed -i 's/test=test_installed_functional/test=test_other/' .factory-state/installed-functional-evidence.env
if ./scripts/check-installed-functional-evidence.sh >/dev/null 2>&1; then
    echo 'evidence guard accepted wrong test name' >&2
    exit 1
fi
# Reject missing schema
sed -i 's/test=test_other/test=test_installed_functional/' .factory-state/installed-functional-evidence.env
sed -i 's/schema=factory-installed-functional\/v1/schema=other/' .factory-state/installed-functional-evidence.env
if ./scripts/check-installed-functional-evidence.sh >/dev/null 2>&1; then
    echo 'evidence guard accepted wrong schema' >&2
    exit 1
fi
# Evidence is parsed as data, never sourced as shell code.
cat > .factory-state/installed-functional-evidence.env <<EOF
schema=factory-installed-functional/v1
commit=$commit
test=test_installed_functional
result=PASS
skipped=0
touch -- '$sentinel'
EOF
if ./scripts/check-installed-functional-evidence.sh >/dev/null 2>&1 || [[ -e "$sentinel" ]]; then
    echo 'evidence guard accepted or executed injected shell content' >&2
    exit 1
fi
# Reject symlinked and missing evidence files entirely.
rm .factory-state/installed-functional-evidence.env
ln -s /dev/null .factory-state/installed-functional-evidence.env
if ./scripts/check-installed-functional-evidence.sh >/dev/null 2>&1; then
    echo 'evidence guard accepted symlinked evidence file' >&2
    exit 1
fi
rm .factory-state/installed-functional-evidence.env
if ./scripts/check-installed-functional-evidence.sh >/dev/null 2>&1; then
    echo 'evidence guard accepted missing evidence file' >&2
    exit 1
fi
echo 'installed-functional-evidence tests passed'
