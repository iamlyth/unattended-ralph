#!/usr/bin/env bash
set -euo pipefail

root=$(git rev-parse --show-toplevel)
evidence=$root/.factory-state/installed-functional-evidence.env
[[ -f $evidence ]] || {
    echo "installed-functional-evidence: missing; run ./scripts/verify-project.sh" >&2
    exit 1
}
# shellcheck disable=SC1090
source "$evidence"
[[ ${schema:-} == factory-installed-functional/v1 &&
   ${test:-} == test_installed_functional && ${result:-} == PASS &&
   ${skipped:-1} == 0 && ${commit:-} =~ ^[0-9a-f]{40}$ ]] || {
    echo "installed-functional-evidence: malformed or non-passing evidence" >&2
    exit 1
}
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
