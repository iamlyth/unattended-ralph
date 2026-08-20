#!/usr/bin/env bash
# test-production-path-bypass.sh
#
# Project-agnostic boilerplate check: detect tests that set an
# environment variable to override a *resource* path (image, SVG, font,
# texture, asset) so the resource loads in the test, masking a
# production-path failure.
#
# The failure mode this guards against: a test passes because it injects
# an env var (e.g. PRODUCT_ICON_DIR) or a source-tree path to make a resource
# load, while the real binary (no injection) cannot find the resource and
# silently renders without it. The test suite then reports green while the
# product is broken — "thinking issues are complete when they are not."
#
# This is distinct from legitimate test isolation (e.g. XDG_CONFIG_HOME /
# XDG_DATA_HOME overrides), which do not override a resource location and
# are not flagged.
set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
PROJECT_ROOT=$(cd -- "$SCRIPT_DIR/.." && pwd)
cd -- "$PROJECT_ROOT"

# Resource-path env var name fragments. Setting one of these in a test to
# make a resource load is a production-path bypass. XDG_* and other
# isolation vars are intentionally excluded.
RESOURCE_PATTERNS=(
    'ICON'
    'FONT'
    'SVG'
    'TEXTURE'
    'IMAGE'
    'ASSET'
    'RESOURCE'
)

# Test source files to scan.
mapfile -t TEST_FILES < <(find tests -type f \
    \( -name '*.c' -o -name '*.sh' -o -name '*.py' \) -print | sort)

violations=0
for file in "${TEST_FILES[@]}"; do
    # Only lines that call setenv/unsetenv with a string literal.
    while IFS= read -r line; do
        for pat in "${RESOURCE_PATTERNS[@]}"; do
            if echo "$line" | grep -qiE "(setenv|unsetenv)\(\s*\"[^\"]*${pat}[^\"]*\""; then
                echo "test-production-path-bypass: $file:$line" >&2
                echo "  sets a resource-path env var (${pat}) to make a resource load" >&2
                echo "  this masks production failures; the production path must work without it" >&2
                violations=$((violations + 1))
            fi
        done
    done < <(grep -nE '(setenv|unsetenv)' "$file" 2>/dev/null || true)
done

if (( violations > 0 )); then
    echo "test-production-path-bypass: FAIL: $violations resource-path env-var injection(s) found" >&2
    exit 1
fi

echo "test-production-path-bypass: no resource-path env-var injection found"
exit 0
