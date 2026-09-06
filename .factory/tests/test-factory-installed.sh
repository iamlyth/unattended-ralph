#!/usr/bin/env bash
# test-factory-installed.sh — hidden Task 20 installed-tier evidence driver.
#
# The specification (HIDE-01 §3, EVID-01 §19, TEST-01 §22) keeps harness-only
# tests out of the adopting product's visible `.factory/tests/legacy/` tree, so the installed
# smoke suite lives under the hidden `.factory/tests/` namespace like the
# Task 13 footprint and Task 16 adversarial series.  This driver:
#
#   0. runs in a controlled shell context (Git redirector families and
#      Ollama/campaign environment stripped) with one private temporary
#      directory removed on every exit path and bytecode writes disabled
#      (`PYTHONDONTWRITEBYTECODE=1`) so the suite can never create a
#      `__pycache__`/`*.pyc` artifact in the repository;
#   1. snapshots every live `.factory-state` file's digest, mode, and mtime,
#      the tracked/untracked Git state, and every ignored mutation
#      (`git status --porcelain --ignored` plus the `__pycache__`/`*.pyc`
#      inventory under the installed surface) *before* the suite;
#   2. runs `.factory/tests/test-factory-installed.py` warning-free under
#      `-W error::ResourceWarning` and requires a final `OK`;
#   3. re-snapshots the live `.factory-state`, the Git state, and the
#      ignored/bytecode inventory *after* the suite and proves byte-for-byte
#      preservation: no foreign runtime file was deleted, mutated, or
#      added, the repository gained no file, and no ignored bytecode
#      artifact appeared or changed.
#
# The suite itself stages installed copies only under its own test-owned
# temporary directory and mints receipts only inside its fixture authority,
# so the live repository's runtime state is never touched here either.
set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
ROOT=$(cd -- "$SCRIPT_DIR/../.." && pwd)
cd -- "$ROOT"

PY=${PYTHON:-python3}

fail() {
    echo "test-factory-installed: $*" >&2
    exit 1
}

# -- Controlled shell context -----------------------------------------------
# Strip the same Git redirector families the pinned boundary strips, plus
# Ollama/campaign/credential variables, so a caller environment can never
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

tmp=$(mktemp -d "${TMPDIR:-/tmp}/factory-installed.XXXXXX")
cleanup() {
    rm -rf -- "$tmp"
}
# shellcheck disable=SC2154 # rc is assigned inside the trap from $?
trap 'rc=$?; cleanup; trap - EXIT INT TERM; exit "$rc"' EXIT INT TERM

# Bytecode writes are disabled for the whole suite (the driver and every
# subprocess), so the installed copy and the repository can never gain a
# `__pycache__`/`*.pyc` artifact.
export PYTHONDONTWRITEBYTECODE=1

# -- 1. Foreign-runtime and Git-state snapshots ------------------------------
# The live `.factory-state/` holds foreign runtime evidence (for example the
# installed-functional evidence recorded against an adopting-product commit)
# that Task 20 must preserve byte-for-byte.  Snapshot every file's digest,
# mode, and mtime plus the complete file list; any deletion, mutation, or
# addition after the suite is a hard failure.
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

# Snapshot ignored/bytecode state under the installed surface: a suite that
# imported a module without `PYTHONDONTWRITEBYTECODE` would create or mutate
# a `__pycache__`/`*.pyc` artifact here, and an unexpected ignored file is a
# runtime leak.  Both channels must be byte-identical before and after.
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

# -- 2. The hidden Python suite runs warning-free ---------------------------
echo "test-factory-installed: running .factory/tests/test-factory-installed.py"
"$PY" -W error::ResourceWarning .factory/tests/test-factory-installed.py \
    >"$tmp/suite.log" 2>&1 || {
    echo "test-factory-installed: Python suite failed:" >&2
    tail -80 "$tmp/suite.log" >&2
    exit 1
}
if grep -qiE 'ResourceWarning|unclosed file|still running' "$tmp/suite.log"; then
    echo "test-factory-installed: ResourceWarning/subprocess leaked from the suite:" >&2
    grep -iE 'ResourceWarning|unclosed file|still running' "$tmp/suite.log" | head -20 >&2
    exit 1
fi
grep -q '^OK$' "$tmp/suite.log" \
    || fail "the Python suite must end OK (got: $(tail -3 "$tmp/suite.log"))"
grep -qE '^Ran [0-9]+ tests' "$tmp/suite.log" \
    || fail "the Python suite must report a non-zero test count"

# -- 3. Byte-for-byte preservation and repository stability -----------------
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
echo "test-factory-installed: live .factory-state preserved byte-for-byte; repository and ignored/bytecode state unchanged"

echo "test-factory-installed: all checks passed"
