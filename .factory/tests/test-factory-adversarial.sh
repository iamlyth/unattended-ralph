#!/usr/bin/env bash
# test-factory-adversarial.sh — hidden-namespace Task 16 §22 adversarial
# conformance shell driver.
#
# The specification (HIDE-01, §3, TEST-01 §22) keeps harness-only tests out
# of the adopting product's visible `.factory/tests/legacy/` tree, so the §22 conformance
# suite lives under the hidden `.factory/tests/` namespace like the Task 13
# footprint and Task 15 migration series.  This driver:
#
#   0. hard-checks every §22 authority prerequisite (loop campaign/findings/
#      launch/plan-parser/selector/state modules, the committed fixture
#      driver, the findings campaign suite providing FindingsWorkspace, the
#      adversarial manifest, and the findings schemas) with a named
#      diagnostic before anything runs, so a missing or renamed authority
#      can never silently weaken a conformance case;
#   1. machine-checks the committed adversarial manifest
#      (`.factory/tests/adversarial-manifest.json`, schema
#      `factory-adversarial-manifest/v1`) as the single authority: exactly
#      27 well-formed, unique, numbered cases (1..27, no gaps/duplicates),
#      every declared case test registered and never skipped in the Python
#      suite, and no extra case test outside the manifest;
#   2. runs `.factory/tests/test-factory-adversarial.py` warning-free under
#      `-W error::ResourceWarning` (the full 27-case §22 suite drives the
#      real authorities in fresh subprocesses and test-owned repositories);
#
# Legacy launcher compatibility is outside this completed hidden-harness
# driver; migration behavior is exercised by canonical case 18.
#
# The driver never writes to the live repository and never touches `.ralph/`.
set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
ROOT=$(cd -- "$SCRIPT_DIR/../.." && pwd)
cd -- "$ROOT"

PY=${PYTHON:-python3}

fail() {
    echo "test-factory-adversarial: $*" >&2
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
unset OLLAMA_USAGE_ENV_FILE OLLAMA_COOKIE 2>/dev/null || true

# -- 0. Hard prerequisite diagnostics ---------------------------------------
# Case 05 (FIND-01, §22.5) drives the real findings/launch authorities and
# the committed fixture driver seam (FindingsWorkspace, consume_next_round_
# findings, the developer task-excerpt digest env), so every authority file
# must exist with the exact expected name before the suite starts.  A
# missing prerequisite fails the shell driver with a named diagnostic
# instead of a deep traceback or a silently weaker assertion.
required_prerequisites=(
    .factory/loop/campaign.py
    .factory/loop/findings.py
    .factory/loop/launch.py
    .factory/loop/plan_parser.py
    .factory/loop/selector.py
    .factory/loop/state.py
    .factory/tests/fixtures/campaign_driver.py
    .factory/tests/test-factory-campaign.py
    .factory/tests/test-factory-findings.py
    .factory/tests/test-factory-adversarial.py
    .factory/tests/adversarial-manifest.json
    .factory/schemas/factory-findings-v1.schema.json
    .factory/schemas/factory-findings-receipt-v1.schema.json
)
for prerequisite in "${required_prerequisites[@]}"; do
    [[ -f "$ROOT/$prerequisite" ]] || \
        fail "missing §22 case-05 authority prerequisite: $prerequisite"
done
echo "test-factory-adversarial: §22 case-05 authority prerequisites present"

tmp=$(mktemp -d "${TMPDIR:-/tmp}/factory-adversarial.XXXXXX")
cleanup() {
    rm -rf -- "$tmp"
}
# shellcheck disable=SC2154 # rc is assigned inside the trap from $?
trap 'rc=$?; cleanup; trap - EXIT INT TERM; exit "$rc"' EXIT INT TERM

# -- 1. Machine manifest checks ---------------------------------------------
# The manifest is the single authority for the 27 §22 cases.  The shell
# driver independently re-checks it (in addition to the Python
# ManifestCompletenessTest) so a broken manifest fails even before the
# Python suite starts.
echo "test-factory-adversarial: checking .factory/tests/adversarial-manifest.json"
"$PY" - "$ROOT/.factory/tests/adversarial-manifest.json" \
    "$ROOT/.factory/tests/test-factory-adversarial.py" <<'PY' || \
    fail "the adversarial manifest or its registered tests are inconsistent"
import json
import re
import sys

manifest_path, suite_path = sys.argv[1], sys.argv[2]
with open(manifest_path, encoding="utf-8") as stream:
    manifest = json.load(stream)

if not isinstance(manifest, dict) or manifest.get("schema") != \
        "factory-adversarial-manifest/v1":
    raise SystemExit("the adversarial manifest must declare "
                     "schema factory-adversarial-manifest/v1")
cases = manifest.get("cases")
if not isinstance(cases, list) or not cases:
    raise SystemExit("the adversarial manifest declares no cases")
if len(cases) != 27:
    raise SystemExit(f"the manifest must declare exactly 27 cases, got {len(cases)}")
numbers = [entry["case"] for entry in cases]
if numbers != list(range(1, 28)) or len(set(numbers)) != 27:
    raise SystemExit("case numbers must be exactly 1..27 with no gap or duplicate")
for entry in cases:
    if not isinstance(entry, dict) or set(entry) != {
        "case", "title", "requirement_ids", "test",
    }:
        raise SystemExit(f"case {entry.get('case')}: malformed manifest entry")
    if not isinstance(entry["case"], int) or not isinstance(entry["title"], str) \
            or not entry["title"] or not isinstance(entry["requirement_ids"], list) \
            or not entry["requirement_ids"] or not isinstance(entry["test"], str) \
            or not re.fullmatch(r"test_case_\d\d_.+", entry["test"]):
        raise SystemExit(f"case {entry.get('case')}: malformed manifest fields")

suite = open(suite_path, encoding="utf-8").read()
declared = [entry["test"] for entry in cases]
if len(set(declared)) != len(declared):
    raise SystemExit("the manifest must not repeat a case test method name")
for method in declared:
    if re.search(rf"def\s+{re.escape(method)}\s*\(", suite) is None:
        raise SystemExit(f"manifest case test {method!r} is not registered in the suite")
# No skipped markers: a skip/skipIf/skipUnless decorator attached to any
# case method (with or without its argument tuple) is rejected, and no case
# method body may call ``skipTest`` at all — a skipped §22 conformance case
# is a hard failure, never a skip.
skip_decorated = set(re.findall(
    r"@unittest\.skip(?:If|Unless)?(?:\s*\([^)]*\))?\s*\n"
    r"(?=\s*def\s+(test_case_\d\d_[A-Za-z0-9_]+)\s*\()",
    suite,
))
if skip_decorated:
    raise SystemExit("manifest case test(s) must never be skipped: "
                     + ", ".join(sorted(skip_decorated)))
# Slice each case method body (4-space class indentation) and reject any
# ``skipTest`` call inside it; a method that calls skipTest on any branch is
# a skipped case and must fail the manifest gate.
method_blocks = {}
current = None
start = None
for index, line in enumerate(suite.splitlines()):
    match = re.match(r"^    def (test_case_\d\d_[A-Za-z0-9_]+)\(", line)
    if match:
        if current is not None:
            method_blocks[current] = suite.splitlines()[start:index]
        current = match.group(1)
        start = index
if current is not None:
    method_blocks[current] = suite.splitlines()[start:]
for method in declared:
    body = "\n".join(method_blocks.get(method, []))
    if not body.strip():
        raise SystemExit(f"manifest case test {method!r} has no source body")
    if re.search(r"\bskipTest\s*\(", body):
        raise SystemExit(f"manifest case test {method!r} must never call "
                         "skipTest (a §22 case is a hard requirement)")
actual = set(re.findall(r"def\s+(test_case_\d\d_[A-Za-z0-9_]+)\s*\(", suite))
if actual != set(declared):
    raise SystemExit("the suite case tests and the manifest must be the exact "
                     "same set (extra or missing)")
PY
echo "test-factory-adversarial: manifest is the single authority for 27 registered cases"

# -- 2. The hidden Python suite runs warning-free ---------------------------
echo "test-factory-adversarial: running .factory/tests/test-factory-adversarial.py"
"$PY" -W error::ResourceWarning .factory/tests/test-factory-adversarial.py \
    >"$tmp/suite.log" 2>&1 || {
    echo "test-factory-adversarial: Python suite failed:" >&2
    tail -80 "$tmp/suite.log" >&2
    exit 1
}
if grep -qiE 'ResourceWarning|unclosed file|still running' "$tmp/suite.log"; then
    echo "test-factory-adversarial: ResourceWarning/subprocess leaked from the suite:" >&2
    grep -iE 'ResourceWarning|unclosed file|still running' "$tmp/suite.log" | head -20 >&2
    exit 1
fi
grep -q '^OK$' "$tmp/suite.log" \
    || fail "the Python suite must end OK (got: $(tail -3 "$tmp/suite.log"))"
echo "test-factory-adversarial: the 27-case §22 suite passes warning-free"

echo "test-factory-adversarial: all 27 canonical cases passed"
