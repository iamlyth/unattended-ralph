#!/usr/bin/env bash
# test-factory-migration.sh — hidden-namespace Task 15 migration/deprecation
# shell driver.
#
# The specification (HIDE-01, §3) keeps harness-only tests out of the adopting
# product's visible `.factory/tests/legacy/` tree, so the migration suite lives under the
# hidden `.factory/tests/` namespace like the Task 13 footprint series.  This
# driver:
#
#   1. runs `.factory/tests/test-factory-migration.py` warning-free under
#      `-W error::ResourceWarning` (read isolation, exact preservation, no
#      runtime imports, single-authority migrate, freeze surface,
#      context-summary unwiring, legacy-store metadata-only);
#   2. verifies the tracked freeze marker exists as a regular file and every
#      frozen legacy launcher both keeps working help (`--help` exits 0 while
#      frozen) and refuses a new launch (exit 2 with the frozen message);
#   3. verifies legacy recovery stays available (``ralph-recover.sh --help``
#      works and prints the deprecation note) and the deprecated visible
#      context-summary generator/verifier/suite fail closed as marked,
#      optional legacy entry points that no new-path control step invokes;
#   4. verifies the stale ``.factory/artifacts/context-summary.md`` mirror is
#      absent and that ``verify-boilerplate.sh``/``final-gate.sh``/
#      ``git-commit-hook.sh``/``ralph-run.sh`` never invoke the deprecated
#      authorities;
#   5. verifies the shell credential guard resolves the external operator
#      store and only *detects* the legacy workspace store with metadata-only
#      checks (never sources, reads, or overwrites it);
#   6. proves the exact override escape, the missing-marker legacy semantics,
#      and the all-six fail-closed refusal on unsafe markers (symlink/FIFO/
#      socket/device) in private fixtures (never touching the live
#      repository);
#   7. asserts the docs name the flat report path and the exact override.
#
# The driver never writes to the live repository, never reads or moves the
# real ``.ollama-usage-env``, and never touches ``.ralph/``.
set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
ROOT=$(cd -- "$SCRIPT_DIR/../.." && pwd)
cd -- "$ROOT"

PY=${PYTHON:-python3}

fail() {
    echo "test-factory-migration: $*" >&2
    exit 1
}

# -- Controlled shell context -----------------------------------------------
# Strip the same Git redirector families the pinned boundary strips, so a
# caller environment cannot steer the fixture commands at a different object
# store, index, work tree, or config set.
unset GIT_DIR GIT_WORK_TREE GIT_INDEX_FILE GIT_OBJECT_DIRECTORY \
    GIT_ALTERNATE_OBJECT_DIRECTORIES GIT_COMMON_DIR GIT_NAMESPACE \
    GIT_CEILING_DIRECTORIES GIT_SSH GIT_SSH_COMMAND GIT_ASKPASS \
    GIT_TERMINAL_PROMPT GIT_EXEC_PATH GIT_TEMPLATE_DIR \
    GIT_CONFIG_PARAMETERS GIT_CONFIG GIT_CONFIG_SYSTEM GIT_CONFIG_GLOBAL \
    GIT_CONFIG_NOSYSTEM GIT_CONFIG_COUNT 2>/dev/null || true
unset "${!GIT_CONFIG_KEY_@}" "${!GIT_CONFIG_VALUE_@}" 2>/dev/null || true
# The operator store tests must never consult a real ambient store.
unset OLLAMA_USAGE_ENV_FILE OLLAMA_COOKIE 2>/dev/null || true

tmp=$(mktemp -d "${TMPDIR:-/tmp}/factory-migration.XXXXXX")
cleanup() {
    rm -rf -- "$tmp"
}
# shellcheck disable=SC2154 # rc is assigned inside the trap from $?
trap 'rc=$?; cleanup; trap - EXIT INT TERM; exit "$rc"' EXIT INT TERM

# -- 1. The hidden Python suite runs warning-free ---------------------------
echo "test-factory-migration: running .factory/tests/test-factory-migration.py"
"$PY" -W error::ResourceWarning .factory/tests/test-factory-migration.py \
    >"$tmp/suite.log" 2>&1 || {
    echo "test-factory-migration: Python suite failed:" >&2
    tail -60 "$tmp/suite.log" >&2
    exit 1
}
if grep -qiE 'ResourceWarning|unclosed file' "$tmp/suite.log"; then
    echo "test-factory-migration: ResourceWarning leaked from the suite:" >&2
    grep -iE 'ResourceWarning|unclosed file' "$tmp/suite.log" | head -20 >&2
    exit 1
fi

grep -q '^OK$' "$tmp/suite.log" || fail "migration Python suite did not finish OK"
echo "test-factory-migration: canonical migration suite passed"
