#!/usr/bin/env bash
# test-factory-generic-evidence.sh — hidden Task 23 generic evidence-scope driver.
#
# The hidden Python suite exercises the trusted generic evidence publisher
# (prepare/publish), the installed-harness receipt, and the exact-HEAD
# generic checker entirely inside test-owned temporary fixture repositories.
# This driver runs it warning-free and proves the **live** repository's
# `.factory-state/`, Git state, and ignored/bytecode inventory are preserved
# byte-for-byte around the whole suite (exactly like the Task 20 installed
# driver), so the tests can never mutate, delete, or relabel a foreign
# runtime file.
set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
ROOT=$(cd -- "$SCRIPT_DIR/../.." && pwd)
cd -- "$ROOT"

PY=${PYTHON:-python3}

fail() {
    echo "test-factory-generic-evidence: $*" >&2
    exit 1
}

# Controlled shell context: strip Git redirector families and
# Ollama/campaign/credential variables so a caller environment can never
# steer the fixture commands at a different object store, index, work tree,
# configuration set, or model backend.
unset GIT_DIR GIT_WORK_TREE GIT_INDEX_FILE GIT_OBJECT_DIRECTORY \
    GIT_ALTERNATE_OBJECT_DIRECTORIES GIT_COMMON_DIR GIT_NAMESPACE \
    GIT_CEILING_DIRECTORIES GIT_SSH GIT_SSH_COMMAND GIT_ASKPASS \
    GIT_TERMINAL_PROMPT GIT_EXEC_PATH GIT_TEMPLATE_DIR \
    GIT_CONFIG_PARAMETERS GIT_CONFIG GIT_CONFIG_SYSTEM GIT_CONFIG_GLOBAL \
    GIT_CONFIG_NOSYSTEM GIT_CONFIG_COUNT 2>/dev/null || true
unset "${!GIT_CONFIG_KEY_@}" "${!GIT_CONFIG_VALUE_@}" 2>/dev/null || true
unset OLLAMA_USAGE_ENV_FILE OLLAMA_COOKIE OLLAMA_HOST 2>/dev/null || true
unset FACTORY_CAMPAIGN_AUDIT_ROUND FACTORY_CAMPAIGN_AUDIT_BASE \
    FACTORY_CAMPAIGN_AUDIT_NONCE FACTORY_VERIFIER_ROOT 2>/dev/null || true

tmp=$(mktemp -d "${TMPDIR:-/tmp}/factory-generic-evidence.XXXXXX")
cleanup() {
    rm -rf -- "$tmp"
}
# shellcheck disable=SC2154 # rc is assigned inside the trap from $?
trap 'rc=$?; cleanup; trap - EXIT INT TERM; exit "$rc"' EXIT INT TERM

export PYTHONDONTWRITEBYTECODE=1

snapshot_state() {
    local dir="$1"
    if [[ -d "$ROOT/.factory-state" ]]; then
        (
            cd "$ROOT/.factory-state"
            find . -type f -print0 | sort -z | while IFS= read -r -d '' f; do
                printf '%s\t%s\t%s\t%s\n' \
                    "$f" \
                    "$(sha256sum "$f" | awk '{print $1}')" \
                    "$(stat -c '%a' "$f")" \
                    "$(stat -c '%Y' "$f")"
            done
        ) > "$dir"
    else
        : > "$dir"
    fi
}

snapshot_ignored() {
    local dir="$1"
    {
        git status --porcelain --ignored -- .factory .pi scripts
        find .factory .pi scripts -type d -name __pycache__ -print 2>/dev/null | sort
        find .factory .pi scripts -type f -name '*.py[co]' -printf '%p %s\n' 2>/dev/null | sort
    } > "$dir"
}

before_state="$tmp/factory-state.before"
after_state="$tmp/factory-state.after"
before_git="$tmp/git-status.before"
after_git="$tmp/git-status.after"
before_ignored="$tmp/ignored.before"
after_ignored="$tmp/ignored.after"

snapshot_state "$before_state"
snapshot_ignored "$before_ignored"
git status --porcelain > "$before_git"

echo "test-factory-generic-evidence: running .factory/tests/test-factory-generic-evidence.py"
# Bounded: a hung fixture publication (stuck suite, wedged lock, leaked child)
# must fail the driver instead of hanging CI forever.
if ! timeout 1800 "$PY" -W error::ResourceWarning .factory/tests/test-factory-generic-evidence.py \
    >"$tmp/suite.log" 2>&1; then
    echo "test-factory-generic-evidence: Python suite failed or timed out:" >&2
    tail -80 "$tmp/suite.log" >&2
    exit 1
fi
if grep -qiE 'ResourceWarning|unclosed file|still running' "$tmp/suite.log"; then
    echo "test-factory-generic-evidence: ResourceWarning/subprocess leaked from the suite:" >&2
    grep -iE 'ResourceWarning|unclosed file|still running' "$tmp/suite.log" | head -20 >&2
    exit 1
fi
grep -q '^OK$' "$tmp/suite.log" \
    || fail "the Python suite must end OK (got: $(tail -3 "$tmp/suite.log"))"
grep -qE '^Ran [0-9]+ tests' "$tmp/suite.log" \
    || fail "the Python suite must report a non-zero test count"

snapshot_state "$after_state"
snapshot_ignored "$after_ignored"
git status --porcelain > "$after_git"

if ! cmp -s "$before_state" "$after_state"; then
    fail "the live .factory-state changed during the suite (foreign runtime files must be preserved byte-for-byte)"
fi
if ! cmp -s "$before_git" "$after_git"; then
    fail "the repository working tree changed during the suite"
fi
if ! cmp -s "$before_ignored" "$after_ignored"; then
    fail "the ignored/bytecode inventory changed during the suite (a __pycache__/*.pyc artifact or an ignored runtime file leaked)"
fi
echo "test-factory-generic-evidence: live .factory-state preserved byte-for-byte; repository and ignored/bytecode state unchanged"

echo "test-factory-generic-evidence: all checks passed"
