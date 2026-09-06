#!/usr/bin/env bash
# C3 completion gate for the machine visual-audit methodology.
#
# Check-only: this gate never captures, never probes, never drives a vision
# model, and never runs the review SDK orchestrator. It parses the tracked
# .factory/visual-audit.toml with stdlib tomllib, and when the methodology is
# enabled it runs the aggregated fail-closed checker
# (scripts/check-visual-audit.py) against the configured review report bound
# to the exact current HEAD. When the methodology is disabled (the generic
# scaffold default) it exits 0 without touching anything.
#
# The gate is production-grade: test-only execution overrides
# (VISUAL_AUDIT_CONFIG, VISUAL_AUDIT_SDK_DRIVER, VISUAL_AUDIT_VISION_MODEL) and
# the test marker RALPH_VISUAL_AUDIT_TESTING are all rejected outright, because
# the completion gate is never a test task and must always inspect the tracked
# production configuration.
set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
PROJECT_ROOT=$(cd -- "$SCRIPT_DIR/../.." && pwd)
cd -- "$PROJECT_ROOT"

# Production gate: no test-only execution overrides, including the marker that
# would otherwise enable them.
for var in VISUAL_AUDIT_CONFIG VISUAL_AUDIT_SDK_DRIVER VISUAL_AUDIT_VISION_MODEL \
        RALPH_VISUAL_AUDIT_TESTING; do
    if [[ -n ${!var:-} ]]; then
        echo "visual-audit-gate: $var is a test-only execution override; forbidden in the production completion gate" >&2
        exit 1
    fi
done

# Resolve the enabled state and report path from the tracked config. Emits
# either "disabled" (one line) or "enabled" followed by the absolute repo
# report path. Fails closed (non-zero) on any config/report/path problem.
set +e
GATE_OUTPUT=$(python3 - "$PROJECT_ROOT" <<'PY'
import pathlib
import subprocess
import sys
import tomllib

root = pathlib.Path(sys.argv[1])
config_path = root / ".factory/visual-audit.toml"
if not config_path.is_file() or config_path.is_symlink():
    sys.stderr.write("visual-audit-gate: tracked .factory/visual-audit.toml is missing or a symlink\n")
    raise SystemExit(2)

# The config must be the tracked repo file, never an out-of-tree substitute.
try:
    tracked = subprocess.run(
        ["git", "-C", str(root), "ls-files", "--error-unmatch", "--",
         ".factory/visual-audit.toml"],
        capture_output=True, check=False).returncode == 0
except OSError:
    tracked = False
if not tracked:
    sys.stderr.write("visual-audit-gate: .factory/visual-audit.toml is not a tracked file\n")
    raise SystemExit(2)

with open(config_path, "rb") as stream:
    config = tomllib.load(stream)
section = config.get("visual-audit")
if not isinstance(section, dict):
    sys.stderr.write("visual-audit-gate: visual-audit config section missing\n")
    raise SystemExit(2)
if section.get("enabled") is not True:
    print("disabled")
    raise SystemExit(0)

review_dir = section.get("review_dir")
if not isinstance(review_dir, str) or not review_dir:
    sys.stderr.write("visual-audit-gate: review_dir must be configured when enabled\n")
    raise SystemExit(2)
report_rel = pathlib.Path(review_dir) / "report.json"
# The review report must remain repo-relative under .factory-state/visual-audit/
# with no traversal and no symlink.
if report_rel.is_absolute() or any(part == ".." for part in report_rel.parts):
    sys.stderr.write("visual-audit-gate: review_dir must be repo-relative with no traversal\n")
    raise SystemExit(2)
try:
    rel = report_rel.relative_to(".factory-state/visual-audit")
except ValueError:
    sys.stderr.write("visual-audit-gate: review report must live under .factory-state/visual-audit/\n")
    raise SystemExit(2)
report_path = root / report_rel
# Reject a symlink on the report and on every existing parent from
# .factory-state through the review directory; the report and its container
# chain must stay strictly inside the repo root (root containment).
cur = report_path
while cur != root:
    if cur.is_symlink():
        sys.stderr.write("visual-audit-gate: review report path must not traverse a symlink\n")
        raise SystemExit(2)
    cur = cur.parent
if not report_path.is_file():
    sys.stderr.write("visual-audit-gate: required review report missing: %s\n" % report_path)
    raise SystemExit(2)
print("enabled")
print(report_path)
PY
)
GATE_STATUS=$?
set -e
if (( GATE_STATUS != 0 )); then
    echo "visual-audit-gate: could not resolve the visual-audit configuration" >&2
    exit 1
fi

if [[ "$GATE_OUTPUT" == "disabled" ]]; then
    echo "visual-audit-gate: visual audit disabled; nothing to check"
    exit 0
fi
REPORT_PATH=$(printf '%s\n' "$GATE_OUTPUT" | sed -n '2p')
if [[ -z "$REPORT_PATH" ]]; then
    echo "visual-audit-gate: failed to resolve the review report path" >&2
    exit 1
fi

HEAD_COMMIT=$(git rev-parse HEAD)
exec python3 .factory/tools/check-visual-audit.py \
    --config .factory/visual-audit.toml \
    --report "$REPORT_PATH" \
    --current-commit "$HEAD_COMMIT"
