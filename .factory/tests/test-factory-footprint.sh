#!/usr/bin/env bash
# test-factory-footprint.sh — hidden-namespace Task 13 footprint suite driver.
#
# The specification (HIDE-01, §3) keeps harness-only tests out of the adopting
# product's visible `.factory/tests/legacy/` tree, so the Task 13 inventory suite lives under
# the hidden `.factory/tests/` namespace exactly like the Task 6 supervision
# series.  This driver runs inside a strict shell context manager:
#
#   * it sanitizes every Git override variable (``GIT_DIR``, the complete
#     ``GIT_CONFIG*`` family, index/work-tree/object-store redirectors) so
#     the suite's pinned-Git fixture work is deterministic and can never be
#     pointed at a different repository or configuration by the caller;
#   * it creates one private temporary directory and removes it on every
#     exit path (normal, failure, SIGINT, SIGTERM);
#   * it runs `.factory/tests/test-factory-footprint.py` warning-free under
#     `-W error::ResourceWarning`;
#   * it asserts the trusted CLI (`python3 .factory/loop/footprint.py`)
#     reports a clean inventory on the live boilerplate repository (a
#     harness path that escapes the hidden namespaces, a tracked
#     `.factory-state` file, a legacy root harness file, a symlink/hardlink/
#     case/unicode/mount escape, or a product discovery that yields a hidden
#     artifact fails closed with a machine-readable report);
#   * it asserts the product-discovery CLI never yields `.factory/`,
#     `.factory-state/`, or `.pi/` artifacts.
#
# The suite never writes to the live repository and never touches `.ralph/`.
set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
ROOT=$(cd -- "$SCRIPT_DIR/../.." && pwd)
cd -- "$ROOT"

PY=${PYTHON:-python3}

fail() {
    echo "test-factory-footprint: $*" >&2
    exit 1
}

# -- Controlled shell context -----------------------------------------------
# Strip the same Git redirector families the pinned boundary strips
# (GIT-01/F5), so a caller environment cannot steer the fixture commands at
# a different object store, index, work tree, or config set.
unset GIT_DIR GIT_WORK_TREE GIT_INDEX_FILE GIT_OBJECT_DIRECTORY \
    GIT_ALTERNATE_OBJECT_DIRECTORIES GIT_COMMON_DIR GIT_NAMESPACE \
    GIT_CEILING_DIRECTORIES GIT_SSH GIT_SSH_COMMAND GIT_ASKPASS \
    GIT_TERMINAL_PROMPT GIT_EXEC_PATH GIT_TEMPLATE_DIR \
    GIT_CONFIG_PARAMETERS GIT_CONFIG GIT_CONFIG_SYSTEM GIT_CONFIG_GLOBAL \
    GIT_CONFIG_NOSYSTEM GIT_CONFIG_COUNT 2>/dev/null || true
unset "${!GIT_CONFIG_KEY_@}" "${!GIT_CONFIG_VALUE_@}" 2>/dev/null || true

# Private temporary directory: the driver's context-manager state.  Removed
# on every exit path (normal return, `fail`, SIGINT, SIGTERM), so a broken
# run can never leave fixture repositories behind.
tmp=$(mktemp -d "${TMPDIR:-/tmp}/factory-footprint.XXXXXX")
cleanup() {
    rm -rf -- "$tmp"
}
# shellcheck disable=SC2154 # rc is assigned inside the trap from $?
trap 'rc=$?; cleanup; trap - EXIT INT TERM; exit "$rc"' EXIT INT TERM

# -- 1. The hidden Python suite runs warning-free under -W error -------------
echo "test-factory-footprint: running .factory/tests/test-factory-footprint.py"
"$PY" -W error::ResourceWarning .factory/tests/test-factory-footprint.py \
    >"$tmp/suite.log" 2>&1 || {
    echo "test-factory-footprint: Python suite failed:" >&2
    tail -40 "$tmp/suite.log" >&2
    exit 1
}
if grep -qiE 'ResourceWarning|unclosed file' "$tmp/suite.log"; then
    echo "test-factory-footprint: ResourceWarning leaked from the suite:" >&2
    grep -iE 'ResourceWarning|unclosed file' "$tmp/suite.log" | head -20 >&2
    exit 1
fi

# -- 2. The inventory CLI passes on the live repository ----------------------
"$PY" .factory/loop/footprint.py --root "$ROOT" --json >"$tmp/report.json" 2>&1 \
    || fail "the live footprint inventory failed closed: $(tail -5 "$tmp/report.json")"
"$PY" - "$tmp/report.json" <<'PY'
import json, sys
payload = json.load(open(sys.argv[1], encoding="utf-8"))
assert payload.get("schema") == "factory-footprint/v1", payload
assert not payload["tracked_errors"], payload["tracked_errors"]
assert not payload["on_disk_errors"], payload["on_disk_errors"]
assert not payload["external_errors"], payload["external_errors"]
PY
echo "test-factory-footprint: live inventory clean (tracked, on-disk, external)"

# -- 3. Product discovery yields no hidden-namespace artifact ----------------
"$PY" .factory/loop/footprint.py --root "$ROOT" --product-discovery \
    >"$tmp/discovery.txt" 2>&1 \
    || fail "product discovery failed"
if awk -F/ '{print $1}' "$tmp/discovery.txt" | grep -qxE '\.factory(-state)?|\.pi'; then
    fail "product discovery yielded a hidden-namespace artifact"
fi
echo "test-factory-footprint: product discovery excludes the hidden namespaces"

# -- 4. Product-install inventory rejects hidden namespace contamination -----
"$PY" - "$tmp/product-prefix" <<'PY'
import sys
from pathlib import Path
sys.path.insert(0, str(Path('.factory/loop').absolute()))
import footprint
prefix = Path(sys.argv[1])
prefix.mkdir()
assert footprint.verify_product_install(prefix) == []
(prefix / '.factory').mkdir()
errors = footprint.verify_product_install(prefix)
assert errors and any('.factory' in error for error in errors), errors
PY
echo "test-factory-footprint: product-install contamination gate exercised"

echo "test-factory-footprint: all checks passed"
