#!/usr/bin/env bash
# Commit the selected bug's planned transition before final-gate attestation.
set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
PROJECT_ROOT=$(cd -- "$SCRIPT_DIR/.." && pwd)
cd -- "$PROJECT_ROOT"
# This finalizer is a hook child of the locked maintenance-planning lifecycle;
# it must never create a standalone ledger writer.
# shellcheck source=scripts/factory-lock.sh
source "$SCRIPT_DIR/factory-lock.sh"
factory_lock_assert_held "$PROJECT_ROOT"
bug_id=$("$SCRIPT_DIR/factory-state-file.py" read maintenance-bug-id) || {
    echo "maintenance-planning-finalize: missing or unsafe selected bug" >&2
    exit 1
}
[[ "$bug_id" =~ ^BUG-[0-9]{4,}$ ]] || {
    echo "maintenance-planning-finalize: invalid selected bug ID" >&2
    exit 1
}
status=$(python3 - "$bug_id" <<'PY'
import json, subprocess, sys
record = json.loads(subprocess.check_output(
    ['./scripts/bug-ledger.py', 'show', sys.argv[1]], text=True,
).split('\nfingerprint:', 1)[0])
print(record['status'])
PY
)
case "$status" in
    planned) ;;
    triaged)
        ./scripts/bug-ledger.py set-status "$bug_id" planned
        payload=$(printf '{"loop":{"workspace":"%s","id":"maintenance-planning-ledger"},"iteration":{"current":"planned"}}' "$PROJECT_ROOT")
        printf '%s' "$payload" | ./scripts/git-commit-hook.sh --maintenance-ledger
        ;;
    *)
        echo "maintenance-planning-finalize: selected bug must be triaged or planned, not $status" >&2
        exit 1
        ;;
esac
# The ledger transition must leave no unexpected dirty files. The recovery
# scratchpad is the sole permitted exception: the parent's strict final-handoff
# checkpoint commits exactly that file after this finalizer returns.
leftover=$(git status --porcelain --untracked-files=normal \
    | grep -v '^ M \.ralph/agent/scratchpad\.md$' || true)
[[ -z "$leftover" ]] || {
    echo "maintenance-planning-finalize: ledger transition left unexpected dirty files" >&2
    exit 1
}
./scripts/check-maintenance-freshness.sh >/dev/null
