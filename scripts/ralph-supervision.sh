#!/usr/bin/env bash
# Shared, attempt-bound classification for Ralph completion-gate rejections.

RALPH_COMPLETION_REJECTION_MARKER=${RALPH_COMPLETION_REJECTION_MARKER:-.factory-state/completion-rejected.json}

ralph_supervision_begin() {
    local mode=${1:?ralph_supervision_begin requires a lifecycle mode}
    case "$mode" in
        implementation|planning|campaign-audit|maintenance-planning|maintenance) ;;
        *) echo "ralph-supervision: invalid lifecycle mode '$mode'" >&2; return 2 ;;
    esac

    mkdir -p -- "$(dirname -- "$RALPH_COMPLETION_REJECTION_MARKER")"
    if [[ -L "$RALPH_COMPLETION_REJECTION_MARKER" || ( -e "$RALPH_COMPLETION_REJECTION_MARKER" && ! -f "$RALPH_COMPLETION_REJECTION_MARKER" ) ]]; then
        echo "ralph-supervision: unsafe completion-rejection marker" >&2
        return 1
    fi
    rm -f -- "$RALPH_COMPLETION_REJECTION_MARKER"
    FACTORY_RALPH_ATTEMPT_ID=$(python3 - <<'PY'
import secrets
print(secrets.token_hex(32))
PY
)
    export FACTORY_RALPH_ATTEMPT_ID
}

ralph_supervision_consume_rejection() {
    local mode=${1:?ralph_supervision_consume_rejection requires a lifecycle mode}
    local workspace=${2:?ralph_supervision_consume_rejection requires a workspace}
    python3 - "$RALPH_COMPLETION_REJECTION_MARKER" "$FACTORY_RALPH_ATTEMPT_ID" "$mode" "$workspace" <<'PY'
import json
import os
import stat
import sys
from pathlib import Path

marker, attempt, mode, workspace = sys.argv[1:]
try:
    info = os.lstat(marker)
except FileNotFoundError:
    raise SystemExit(1)
if not stat.S_ISREG(info.st_mode):
    print('ralph-supervision: completion-rejection marker is not a regular file', file=sys.stderr)
    raise SystemExit(2)
try:
    data = json.loads(Path(marker).read_text(encoding='utf-8'))
except (OSError, UnicodeError, json.JSONDecodeError) as exc:
    print(f'ralph-supervision: invalid completion-rejection marker: {exc}', file=sys.stderr)
    raise SystemExit(2)
if not isinstance(data, dict):
    print('ralph-supervision: completion-rejection marker must be a JSON object', file=sys.stderr)
    raise SystemExit(2)
expected = {
    'schema': 'ralph-completion-rejection/v1',
    'attempt_id': attempt,
    'mode': mode,
    'workspace': str(Path(workspace).resolve()),
}
if any(data.get(key) != value for key, value in expected.items()):
    print('ralph-supervision: stale or mismatched completion-rejection marker', file=sys.stderr)
    raise SystemExit(2)
if not isinstance(data.get('loop_id'), str) or not data['loop_id']:
    print('ralph-supervision: completion-rejection marker has no loop ID', file=sys.stderr)
    raise SystemExit(2)
loop_id = data['loop_id']
Path(marker).unlink()
print(loop_id)
PY
}
