#!/usr/bin/env bash
# Run a strict final gate and attest only its explicit rejection to the supervisor.
set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
PROJECT_ROOT=$(cd -- "$SCRIPT_DIR/.." && pwd)
MODE=${1:-}
MARKER=${RALPH_COMPLETION_REJECTION_MARKER:-$PROJECT_ROOT/.factory-state/completion-rejected.json}
ATTEMPT_ID=${FACTORY_RALPH_ATTEMPT_ID:-}

case "$MODE" in
    implementation|planning|campaign-audit|maintenance-planning|maintenance) ;;
    *) echo "ralph-completion-gate: invalid lifecycle mode '$MODE'" >&2; exit 2 ;;
esac
[[ "$ATTEMPT_ID" =~ ^[0-9a-f]{64}$ ]] || {
    echo "ralph-completion-gate: missing or invalid launch attempt ID" >&2
    exit 2
}
[[ ! -L "$MARKER" && ( ! -e "$MARKER" || -f "$MARKER" ) ]] || {
    echo "ralph-completion-gate: unsafe rejection marker" >&2
    exit 2
}
mkdir -p -- "$(dirname -- "$MARKER")"
payload_file=$(mktemp "$PROJECT_ROOT/.factory-state/completion-hook.XXXXXX")
trap 'rm -f -- "$payload_file"' EXIT
cat > "$payload_file"

python3 - "$payload_file" "$PROJECT_ROOT" <<'PY'
import json
import sys
from pathlib import Path

payload = json.loads(Path(sys.argv[1]).read_text(encoding='utf-8'))
if not isinstance(payload, dict):
    raise SystemExit('ralph-completion-gate: hook payload must be a JSON object')
expected = {
    'schema_version': 1,
    'phase': 'pre',
    'event': 'loop.complete',
    'phase_event': 'pre.loop.complete',
}
if any(payload.get(key) != value for key, value in expected.items()):
    raise SystemExit('ralph-completion-gate: payload is not a pre.loop.complete hook invocation')
loop = payload.get('loop')
if not isinstance(loop, dict):
    raise SystemExit('ralph-completion-gate: hook payload has no loop object')
loop_id = loop.get('id')
workspace = loop.get('workspace')
if not isinstance(loop_id, str) or not loop_id:
    raise SystemExit('ralph-completion-gate: hook payload has no loop ID')
if not isinstance(workspace, str) or Path(workspace).resolve() != Path(sys.argv[2]).resolve():
    raise SystemExit('ralph-completion-gate: hook workspace does not match this repository')
PY

set +e
"$SCRIPT_DIR/final-gate.sh" "--$MODE"
rc=$?
set -e
if (( rc == 0 )); then
    rm -f -- "$MARKER"
    exit 0
fi
if (( rc >= 128 )); then
    echo "ralph-completion-gate: final gate interrupted with status $rc; no continuation marker written" >&2
    exit "$rc"
fi

python3 - "$payload_file" "$MARKER" "$ATTEMPT_ID" "$MODE" "$PROJECT_ROOT" <<'PY'
import json
import os
import sys
from pathlib import Path

payload_path, marker_name, attempt, mode, workspace = sys.argv[1:]
payload = json.loads(Path(payload_path).read_text(encoding='utf-8'))
data = {
    'schema': 'ralph-completion-rejection/v1',
    'attempt_id': attempt,
    'mode': mode,
    'workspace': str(Path(workspace).resolve()),
    'loop_id': payload['loop']['id'],
}
marker = Path(marker_name)
temporary = marker.with_name(f'.{marker.name}.{os.getpid()}.tmp')
try:
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w', encoding='utf-8') as stream:
        json.dump(data, stream, sort_keys=True)
        stream.write('\n')
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, marker)
finally:
    try:
        temporary.unlink()
    except FileNotFoundError:
        pass
PY
exit "$rc"
