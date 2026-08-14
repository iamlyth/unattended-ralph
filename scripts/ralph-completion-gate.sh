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

remove_marker_safely() {
    python3 - "$MARKER" <<'PY'
import os
import stat
import sys
from pathlib import Path
path=Path(sys.argv[1])
parent=path.parent if str(path.parent) else Path('.')
try:
    parent_fd=os.open(parent, os.O_RDONLY | os.O_DIRECTORY | getattr(os, 'O_NOFOLLOW', 0))
except OSError as exc:
    raise SystemExit(f'ralph-completion-gate: unsafe marker parent: {exc}')
try:
    try:
        info=os.stat(path.name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        raise SystemExit(0)
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.getuid():
        raise SystemExit('ralph-completion-gate: unsafe existing marker')
    os.unlink(path.name, dir_fd=parent_fd)
    os.fsync(parent_fd)
finally:
    os.close(parent_fd)
PY
}
[[ "$ATTEMPT_ID" =~ ^[0-9a-f]{64}$ ]] || {
    echo "ralph-completion-gate: missing or invalid launch attempt ID" >&2
    exit 2
}
[[ -d "$PROJECT_ROOT/.factory-state" && ! -L "$PROJECT_ROOT/.factory-state" ]] || {
    echo "ralph-completion-gate: unsafe factory state directory" >&2
    exit 2
}
payload_file=$(mktemp "$PROJECT_ROOT/.factory-state/completion-hook.XXXXXX")
trap 'rm -f -- "$payload_file"' EXIT
python3 -c '
import os, sys
payload=sys.stdin.buffer.read(65537)
if len(payload) > 65536:
    raise SystemExit("ralph-completion-gate: hook payload exceeds 64 KiB")
fd=os.open(sys.argv[1], os.O_WRONLY | os.O_TRUNC)
try:
    view=memoryview(payload)
    while view:
        written=os.write(fd, view)
        view=view[written:]
    os.fsync(fd)
finally:
    os.close(fd)
' "$payload_file"

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
    remove_marker_safely
    exit 0
fi
if (( rc >= 128 )); then
    echo "ralph-completion-gate: final gate interrupted with status $rc; no continuation marker written" >&2
    exit "$rc"
fi
if (( rc != 1 )); then
    echo "ralph-completion-gate: final gate failed with infrastructure status $rc; no continuation marker written" >&2
    exit "$rc"
fi

python3 - "$payload_file" "$MARKER" "$ATTEMPT_ID" "$MODE" "$PROJECT_ROOT" <<'PY'
import json
import os
import stat
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
parent = marker.parent if str(marker.parent) else Path('.')
try:
    parent_fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY | getattr(os, 'O_NOFOLLOW', 0))
except OSError as exc:
    raise SystemExit(f'ralph-completion-gate: unsafe marker parent: {exc}')
temporary = f'.{marker.name}.{os.getpid()}.tmp'
try:
    try:
        existing = os.stat(marker.name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        existing = None
    if existing is not None and (not stat.S_ISREG(existing.st_mode) or existing.st_nlink != 1 or existing.st_uid != os.getuid()):
        raise SystemExit('ralph-completion-gate: unsafe existing marker')
    fd = os.open(
        temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0),
        0o600, dir_fd=parent_fd,
    )
    with os.fdopen(fd, 'w', encoding='utf-8') as stream:
        json.dump(data, stream, sort_keys=True)
        stream.write('\n')
        stream.flush()
        os.fsync(stream.fileno())
    os.rename(temporary, marker.name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
    temporary = None
    os.fsync(parent_fd)
finally:
    if temporary is not None:
        try:
            os.unlink(temporary, dir_fd=parent_fd)
        except FileNotFoundError:
            pass
    os.close(parent_fd)
PY
exit "$rc"
