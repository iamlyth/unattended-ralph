#!/usr/bin/env bash
set -euo pipefail
source_root=${1:-$PWD}
root=$(mktemp -d)
trap 'rm -rf "$root"' EXIT
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
echo 'installed-functional-evidence tests passed'
