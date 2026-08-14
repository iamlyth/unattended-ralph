#!/usr/bin/env bash
# Shared, attempt-bound classification for Ralph completion-gate rejections.

RALPH_COMPLETION_REJECTION_MARKER=${RALPH_COMPLETION_REJECTION_MARKER:-.factory-state/completion-rejected.json}
RALPH_HISTORY_FILE=${RALPH_HISTORY_FILE:-.ralph/history.jsonl}
RALPH_SUPERVISION_STALE_RECOVERIES=0
RALPH_SUPERVISION_COMPLETION_RECOVERIES=0

ralph_supervision_begin() {
    local mode=${1:?ralph_supervision_begin requires a lifecycle mode}
    case "$mode" in
        implementation|planning|campaign-audit|maintenance-planning|maintenance) ;;
        *) echo "ralph-supervision: invalid lifecycle mode '$mode'" >&2; return 2 ;;
    esac

    python3 - "$RALPH_COMPLETION_REJECTION_MARKER" <<'PY' || return 1
import os
import stat
import sys
from pathlib import Path

marker = Path(sys.argv[1])
parent = marker.parent if str(marker.parent) else Path('.')
try:
    parent_fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY | getattr(os, 'O_NOFOLLOW', 0))
except OSError as exc:
    print(f'ralph-supervision: unsafe completion-marker parent: {exc}', file=sys.stderr)
    raise SystemExit(1)
try:
    try:
        info = os.stat(marker.name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        raise SystemExit(0)
    if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.getuid()):
        print('ralph-supervision: unsafe completion-rejection marker', file=sys.stderr)
        raise SystemExit(1)
    os.unlink(marker.name, dir_fd=parent_fd)
    os.fsync(parent_fd)
finally:
    os.close(parent_fd)
PY
    FACTORY_RALPH_ATTEMPT_ID=$(python3 - <<'PY'
import secrets
print(secrets.token_hex(32))
PY
)
    export FACTORY_RALPH_ATTEMPT_ID

    local history_snapshot
    history_snapshot=$(python3 - "$RALPH_HISTORY_FILE" <<'PY'
import os
import stat
import sys

path = sys.argv[1]
try:
    fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
except FileNotFoundError:
    print('missing 0')
    raise SystemExit(0)
except OSError as exc:
    print(f'ralph-supervision: cannot safely open history: {exc}', file=sys.stderr)
    raise SystemExit(1)
try:
    info = os.fstat(fd)
    if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.getuid()):
        print('ralph-supervision: history is not a safe regular file', file=sys.stderr)
        raise SystemExit(1)
    size = info.st_size
    if size > 64 * 1024 * 1024:
        print('ralph-supervision: history exceeds the supervisor limit', file=sys.stderr)
        raise SystemExit(1)
    if size and os.pread(fd, 1, size - 1) != b'\n':
        print('ralph-supervision: history does not end at a record boundary', file=sys.stderr)
        raise SystemExit(1)
    print(f'{info.st_dev}:{info.st_ino} {size}')
finally:
    os.close(fd)
PY
) || return 1
    read -r FACTORY_RALPH_HISTORY_ID FACTORY_RALPH_HISTORY_OFFSET <<<"$history_snapshot"
    export FACTORY_RALPH_HISTORY_ID FACTORY_RALPH_HISTORY_OFFSET
}

ralph_supervision_consume_rejection() {
    local mode=${1:?ralph_supervision_consume_rejection requires a lifecycle mode}
    local workspace=${2:?ralph_supervision_consume_rejection requires a workspace}
    python3 - "$RALPH_COMPLETION_REJECTION_MARKER" "$FACTORY_RALPH_ATTEMPT_ID" "$mode" "$workspace" <<'PY'
import json
import os
import re
import stat
import sys
from pathlib import Path

marker, attempt, mode, workspace = sys.argv[1:]
path = Path(marker)
parent = path.parent if str(path.parent) else Path('.')
try:
    parent_fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY | getattr(os, 'O_NOFOLLOW', 0))
except OSError as exc:
    print(f'ralph-supervision: unsafe completion-marker parent: {exc}', file=sys.stderr)
    raise SystemExit(2)
fd = None
try:
    try:
        fd = os.open(path.name, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0), dir_fd=parent_fd)
    except FileNotFoundError:
        raise SystemExit(1)
    info = os.fstat(fd)
    if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.getuid()
            or info.st_size > 16384):
        print('ralph-supervision: unsafe completion-rejection marker', file=sys.stderr)
        raise SystemExit(2)
    raw = os.read(fd, 16385)
    try:
        data = json.loads(raw.decode('utf-8'))
    except (UnicodeError, json.JSONDecodeError) as exc:
        print(f'ralph-supervision: invalid completion-rejection marker: {exc}', file=sys.stderr)
        raise SystemExit(2)
    if not isinstance(data, dict) or set(data) != {'schema', 'attempt_id', 'mode', 'workspace', 'loop_id'}:
        print('ralph-supervision: completion-rejection marker has an invalid schema', file=sys.stderr)
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
    loop_id = data.get('loop_id')
    if not isinstance(loop_id, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,127}', loop_id):
        print('ralph-supervision: completion-rejection marker has an invalid loop ID', file=sys.stderr)
        raise SystemExit(2)
    os.unlink(path.name, dir_fd=parent_fd)
    os.fsync(parent_fd)
    print(loop_id)
finally:
    if fd is not None:
        os.close(fd)
    os.close(parent_fd)
PY
}

ralph_supervision_allow_completion_recovery() {
    local maximum=${FACTORY_RALPH_MAX_COMPLETION_RECOVERIES:-8}
    if [[ ! "$maximum" =~ ^[0-9]+$ ]] || (( maximum > 32 )); then
        echo "ralph-supervision: invalid completion recovery limit" >&2
        return 2
    fi
    if (( RALPH_SUPERVISION_COMPLETION_RECOVERIES >= maximum )); then
        echo "ralph-supervision: completion recovery limit reached ($maximum)" >&2
        return 1
    fi
    (( RALPH_SUPERVISION_COMPLETION_RECOVERIES += 1 ))
    echo "ralph-supervision: completion recovery $RALPH_SUPERVISION_COMPLETION_RECOVERIES/$maximum" >&2
}

ralph_supervision_attempt_was_stale() {
    python3 - "$RALPH_HISTORY_FILE" "${FACTORY_RALPH_HISTORY_ID:?missing history identity}" \
        "${FACTORY_RALPH_HISTORY_OFFSET:?missing history offset}" <<'PY'
import json
import os
import re
import stat
import sys

path, expected_id, raw_offset = sys.argv[1:]
try:
    offset = int(raw_offset)
except ValueError:
    print('ralph-supervision: invalid history offset', file=sys.stderr)
    raise SystemExit(2)
try:
    fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
except FileNotFoundError:
    raise SystemExit(1)
except OSError as exc:
    print(f'ralph-supervision: cannot safely open history: {exc}', file=sys.stderr)
    raise SystemExit(2)
try:
    info = os.fstat(fd)
    if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.getuid()):
        print('ralph-supervision: history is not a safe regular file', file=sys.stderr)
        raise SystemExit(2)
    identity = f'{info.st_dev}:{info.st_ino}'
    if expected_id != 'missing' and identity != expected_id:
        print('ralph-supervision: history identity changed during the attempt', file=sys.stderr)
        raise SystemExit(2)
    if info.st_size < offset:
        print('ralph-supervision: history shrank during the attempt', file=sys.stderr)
        raise SystemExit(2)
    delta = info.st_size - offset
    if delta > 1024 * 1024:
        print('ralph-supervision: appended history exceeds the attempt limit', file=sys.stderr)
        raise SystemExit(2)
    try:
        appended = os.pread(fd, delta, offset).decode('utf-8')
    except UnicodeError as exc:
        print(f'ralph-supervision: cannot decode appended history: {exc}', file=sys.stderr)
        raise SystemExit(2)
finally:
    os.close(fd)
if not appended or not appended.endswith('\n'):
    raise SystemExit(1)
records = []
try:
    for line in appended.splitlines():
        if line:
            records.append(json.loads(line))
except json.JSONDecodeError as exc:
    print(f'ralph-supervision: malformed appended history: {exc}', file=sys.stderr)
    raise SystemExit(2)
if not records:
    raise SystemExit(1)
timestamp = re.compile(r'^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z$')
kinds = []
for record in records:
    if (not isinstance(record, dict) or set(record) != {'ts', 'type'}
            or not isinstance(record.get('ts'), str) or not timestamp.fullmatch(record['ts'])
            or not isinstance(record.get('type'), dict)):
        print('ralph-supervision: appended history record has an invalid schema', file=sys.stderr)
        raise SystemExit(2)
    item = record['type']
    kind = item.get('kind')
    if kind == 'loop_started':
        if (set(item) != {'kind', 'prompt'} or not isinstance(item.get('prompt'), str)
                or not item['prompt'] or len(item['prompt'].encode('utf-8')) > 512 * 1024):
            print('ralph-supervision: loop_started record has an invalid schema', file=sys.stderr)
            raise SystemExit(2)
    elif kind == 'loop_completed':
        if set(item) != {'kind', 'reason'} or not isinstance(item.get('reason'), str) or not item['reason']:
            print('ralph-supervision: loop_completed record has an invalid schema', file=sys.stderr)
            raise SystemExit(2)
    else:
        print('ralph-supervision: appended history record has an unknown kind', file=sys.stderr)
        raise SystemExit(2)
    kinds.append(item)
if len(kinds) != 2 or kinds[0]['kind'] != 'loop_started' or kinds[1]['kind'] != 'loop_completed':
    raise SystemExit(1)
raise SystemExit(0 if kinds[1]['reason'] == 'loop_stale' else 1)
PY
}

ralph_supervision_record_stale_feedback() {
    local mode=${1:?ralph_supervision_record_stale_feedback requires a lifecycle mode}
    local workspace=${2:?ralph_supervision_record_stale_feedback requires a workspace}
    python3 - "$mode" "$workspace" <<'PY'
import os
import re
import secrets
import stat
import sys
from pathlib import Path

mode, workspace = sys.argv[1:]
gates = {
    'planning': './scripts/final-gate.sh --planning',
    'implementation': './scripts/final-gate.sh --implementation',
    'campaign-audit': './scripts/final-gate.sh --campaign-audit',
    'maintenance-planning': './scripts/final-gate.sh --maintenance-planning',
    'maintenance': './scripts/final-gate.sh --maintenance',
}
if mode not in gates:
    print('ralph-supervision: invalid feedback mode', file=sys.stderr)
    raise SystemExit(1)
flags = os.O_RDONLY | os.O_DIRECTORY | getattr(os, 'O_NOFOLLOW', 0)
root = Path(workspace).resolve(strict=True)
root_fd = os.open(root, flags)
ralph_fd = agent_fd = scratch_fd = None
temporary = None
try:
    ralph_fd = os.open('.ralph', flags, dir_fd=root_fd)
    agent_fd = os.open('agent', flags, dir_fd=ralph_fd)
    scratch_fd = os.open('scratchpad.md', os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0), dir_fd=agent_fd)
    info = os.fstat(scratch_fd)
    if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.getuid()
            or info.st_mode & 0o022):
        print('ralph-supervision: unsafe scratchpad', file=sys.stderr)
        raise SystemExit(1)
    chunks = []
    total = 0
    while True:
        chunk = os.read(scratch_fd, 4096)
        if not chunk:
            break
        total += len(chunk)
        if total > 16384:
            print('ralph-supervision: scratchpad exceeds the recovery input limit', file=sys.stderr)
            raise SystemExit(1)
        chunks.append(chunk)
    try:
        original = b''.join(chunks).decode('utf-8')
    except UnicodeError as exc:
        print(f'ralph-supervision: scratchpad is not UTF-8: {exc}', file=sys.stderr)
        raise SystemExit(1)
    if len(re.findall(r'^#[ \t]+', original, re.M)) != 1:
        print('ralph-supervision: scratchpad must have one level-one document', file=sys.stderr)
        raise SystemExit(1)
    start = '<!-- factory-stale-recovery:start -->'
    end = '<!-- factory-stale-recovery:end -->'
    original = re.sub(rf'\n?{re.escape(start)}.*?{re.escape(end)}\n?', '\n', original, flags=re.S).rstrip()
    feedback = (
        f'\n\n{start}\n'
        '## Supervisor recovery feedback\n\n'
        f'- The previous `{mode}` Ralph attempt terminated as a stale loop.\n'
        f'- Run `{gates[mode]}` yourself and fix every reported failure.\n'
        '- Do not repeat a completion summary until that command passes. Replace this section '
        'in the next scratchpad handoff before requesting completion.\n'
        f'{end}\n'
    )
    updated = (original + feedback).encode('utf-8')
    if len(updated) > 16384:
        print('ralph-supervision: recovery feedback would make the scratchpad unsafe', file=sys.stderr)
        raise SystemExit(1)
    temporary = f'.scratchpad-recovery.{secrets.token_hex(16)}'
    out_fd = os.open(
        temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0),
        0o600, dir_fd=agent_fd,
    )
    try:
        view = memoryview(updated)
        while view:
            written = os.write(out_fd, view)
            view = view[written:]
        os.fchmod(out_fd, info.st_mode & 0o777)
        os.fsync(out_fd)
    finally:
        os.close(out_fd)
    os.rename(temporary, 'scratchpad.md', src_dir_fd=agent_fd, dst_dir_fd=agent_fd)
    temporary = None
    os.fsync(agent_fd)
finally:
    if temporary is not None and agent_fd is not None:
        try: os.unlink(temporary, dir_fd=agent_fd)
        except FileNotFoundError: pass
    for fd in (scratch_fd, agent_fd, ralph_fd, root_fd):
        if fd is not None:
            os.close(fd)
PY
}

ralph_supervision_recover_stale() {
    local mode=${1:?ralph_supervision_recover_stale requires a lifecycle mode}
    local workspace=${2:?ralph_supervision_recover_stale requires a workspace}
    local maximum=${FACTORY_RALPH_MAX_STALE_RECOVERIES:-2}
    if [[ ! "$maximum" =~ ^[0-9]+$ ]] || (( maximum > 10 )); then
        echo "ralph-supervision: invalid stale recovery limit" >&2
        return 2
    fi
    local stale_rc
    if ralph_supervision_attempt_was_stale; then
        stale_rc=0
    else
        stale_rc=$?
    fi
    (( stale_rc == 0 )) || return "$stale_rc"
    if (( RALPH_SUPERVISION_STALE_RECOVERIES >= maximum )); then
        echo "ralph-supervision: stale recovery limit reached ($maximum)" >&2
        return 3
    fi
    ralph_supervision_record_stale_feedback "$mode" "$workspace" || return 2
    (( RALPH_SUPERVISION_STALE_RECOVERIES += 1 ))
    echo "ralph-supervision: recovering stale attempt $RALPH_SUPERVISION_STALE_RECOVERIES/$maximum" >&2
    return 0
}
