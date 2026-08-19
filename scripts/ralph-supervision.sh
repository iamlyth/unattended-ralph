#!/usr/bin/env bash
# Shared, attempt-bound classification for Ralph completion-gate rejections.

RALPH_COMPLETION_REJECTION_MARKER=${RALPH_COMPLETION_REJECTION_MARKER:-.factory-state/completion-rejected.json}
RALPH_HISTORY_FILE=${RALPH_HISTORY_FILE:-.ralph/history.jsonl}
RALPH_SUPERVISION_INITIALIZED=false
RALPH_SUPERVISION_STATE_MODE=
RALPH_SUPERVISION_STATE_FILE=

ralph_supervision_prepare_state_directory() {
    python3 - <<'PY'
import os, stat
from pathlib import Path
path = Path('.factory-state')
if path.exists() or path.is_symlink():
    info = path.lstat()
    if (not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode)
            or info.st_uid != os.getuid() or info.st_mode & 0o077):
        raise SystemExit('ralph-supervision: unsafe .factory-state directory')
else:
    path.mkdir(mode=0o700)
PY
}

ralph_supervision_initialize() {
    local mode=${1:?ralph_supervision_initialize requires a lifecycle mode}
    local resume=${2:-false}
    case "$mode" in
        implementation|planning|campaign-audit|maintenance-planning|maintenance) ;;
        *) echo "ralph-supervision: invalid lifecycle mode '$mode'" >&2; return 2 ;;
    esac
    [[ "$resume" == true || "$resume" == false ]] || {
        echo "ralph-supervision: resume flag must be true or false" >&2; return 2;
    }
    RALPH_SUPERVISION_STATE_MODE=$mode
    RALPH_SUPERVISION_STATE_FILE=${FACTORY_RALPH_SUPERVISION_STATE:-.factory-state/ralph-supervision-$mode.json}
    FACTORY_RALPH_CYCLE_ID=$(python3 - "$RALPH_SUPERVISION_STATE_FILE" "$mode" "$resume" <<'PY'
import json, os, secrets, stat, subprocess, sys
from pathlib import Path
sys.path.insert(0, str((Path.cwd() / 'scripts').resolve()))
from factory_state_io import StateIOError, atomic_write_json, read_json

name, mode, resume_text = sys.argv[1:]
resume = resume_text == 'true'
path = Path(name)
expected_parent = Path('.factory-state').absolute()
if not expected_parent.exists():
    expected_parent.mkdir(mode=0o700)
parent = path.parent.absolute()
if parent != expected_parent or path.name != f'ralph-supervision-{mode}.json':
    raise SystemExit('ralph-supervision: durable state must use its canonical state-local path')
parent_info = expected_parent.lstat()
if (not stat.S_ISDIR(parent_info.st_mode) or stat.S_ISLNK(parent_info.st_mode)
        or parent_info.st_uid != os.getuid() or parent_info.st_mode & 0o077):
    raise SystemExit('ralph-supervision: unsafe durable-state parent')

keys_v2 = {
    'schema', 'mode', 'cycle_id', 'stale_recoveries',
    'completion_recoveries', 'no_progress_recoveries',
}
def validate(data):
    if not isinstance(data, dict) or data.get('mode') != mode:
        raise SystemExit('ralph-supervision: durable recovery state has an invalid schema')
    if set(data) == keys_v2 and data.get('schema') == 'ralph-supervision/v2':
        cycle = data.get('cycle_id')
        if not isinstance(cycle, str) or len(cycle) != 64 or any(c not in '0123456789abcdef' for c in cycle):
            raise SystemExit('ralph-supervision: durable cycle ID is invalid')
    else:
        raise SystemExit('ralph-supervision: durable recovery state has an invalid schema')
    for key in ('stale_recoveries', 'completion_recoveries', 'no_progress_recoveries'):
        if not isinstance(data.get(key), int) or not 0 <= data[key] <= 64:
            raise SystemExit('ralph-supervision: durable recovery counter is invalid')
    return data

def read_json_file(candidate, label, maximum=16384):
    candidate = Path(candidate)
    if candidate.parent.absolute() != expected_parent or candidate.name != candidate.name.replace('/', ''):
        raise SystemExit(f'ralph-supervision: unsafe {label} path')
    try:
        return read_json(Path.cwd(), candidate.name, maximum=maximum)
    except (OSError, StateIOError) as exc:
        raise SystemExit(f'ralph-supervision: cannot safely read {label}: {exc}')

def write(data):
    try:
        atomic_write_json(Path.cwd(), path.name, data)
    except (OSError, StateIOError) as exc:
        raise SystemExit(f'ralph-supervision: cannot safely write durable recovery state: {exc}')

if path.exists() or path.is_symlink():
    info = path.lstat()
    if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
            or info.st_uid != os.getuid() or info.st_mode & 0o077):
        raise SystemExit('ralph-supervision: unsafe durable recovery state')
    existing_data = read_json_file(path, 'durable recovery state')
    if resume:
        if isinstance(existing_data, dict) and existing_data.get('schema') == 'ralph-supervision/v1':
            raise SystemExit(
                'ralph-supervision: legacy durable state requires explicit '
                'scripts/ralph-supervision-migrate.py migration'
            )
        data = validate(existing_data)
        print(data['cycle_id'])
        raise SystemExit(0)
    data = validate(existing_data)
    final_marker = expected_parent / f'final-handoff-{mode}.json'
    final = read_json_file(final_marker, 'prior final-state marker')
    final_keys = {'schema', 'mode', 'cycle_id', 'checkpoint_head', 'attested_head'}
    checkpoint = final.get('checkpoint_head') if isinstance(final, dict) else None
    attested = final.get('attested_head') if isinstance(final, dict) else None
    if (not isinstance(final, dict) or set(final) != final_keys
            or final.get('schema') != 'ralph-final-state/v1' or final.get('mode') != mode
            or final.get('cycle_id') != data['cycle_id']
            or not isinstance(checkpoint, str) or len(checkpoint) != 40
            or any(c not in '0123456789abcdef' for c in checkpoint)
            or not isinstance(attested, str) or len(attested) != 40
            or any(c not in '0123456789abcdef' for c in attested)):
        raise SystemExit('ralph-supervision: unfinished lifecycle state requires --resume')
    for commit in (checkpoint, attested):
        if subprocess.run(
            ['git', 'cat-file', '-e', f'{commit}^{{commit}}'],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        ).returncode:
            raise SystemExit('ralph-supervision: prior final-state commit is unavailable')
    if (subprocess.run(
            ['git', 'merge-base', '--is-ancestor', checkpoint, attested],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        ).returncode or subprocess.run(
            ['git', 'merge-base', '--is-ancestor', attested, 'HEAD'],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        ).returncode):
        raise SystemExit('ralph-supervision: prior final-state history was rewritten')
elif resume:
    raise SystemExit(
        'ralph-supervision: durable recovery state is missing; refuse budget reset. '
        'For the stopped legacy campaign run scripts/ralph-supervision-migrate.py explicitly.'
    )
data = {
    'schema': 'ralph-supervision/v2', 'mode': mode, 'cycle_id': secrets.token_hex(32),
    'stale_recoveries': 0, 'completion_recoveries': 0, 'no_progress_recoveries': 0,
}
write(data)
print(data['cycle_id'])
PY
) || return 1
    [[ "$FACTORY_RALPH_CYCLE_ID" =~ ^[0-9a-f]{64}$ ]] || {
        echo "ralph-supervision: failed to establish a durable cycle ID" >&2; return 1;
    }
    export FACTORY_RALPH_CYCLE_ID
    RALPH_SUPERVISION_INITIALIZED=true
}

ralph_supervision_should_continue() {
    local mode=${1:?ralph_supervision_should_continue requires a lifecycle mode}
    ralph_supervision_ensure_initialized "$mode" || return $?
    python3 - "$mode" "${FACTORY_RALPH_CYCLE_ID:?missing cycle ID}" <<'PY'
import sys
from pathlib import Path
sys.path.insert(0, str((Path.cwd() / 'scripts').resolve()))
from factory_state_io import read_json
mode, cycle = sys.argv[1:]
handshake = read_json(Path.cwd(), f'ralph-launch-handshake-{mode}.json', maximum=16384, missing_ok=True)
handshake_keys = {
    'schema', 'mode', 'cycle_id', 'attempt_id', 'campaign_round',
    'campaign_phase', 'campaign_state_sha256', 'loop_id', 'events',
    'events_dev', 'events_ino', 'events_offset', 'events_size', 'events_sha256',
    'events_delta_size', 'events_delta_sha256', 'start_record_size',
    'start_record_sha256', 'start_record_nonce',
}
if (isinstance(handshake, dict) and set(handshake) == handshake_keys
        and handshake.get('schema') == 'ralph-launch-handshake/v2'
        and handshake.get('mode') == mode and handshake.get('cycle_id') == cycle):
    raise SystemExit(0)
migration = read_json(
    Path.cwd(), f'ralph-supervision-migration-{mode}.json', maximum=16384, missing_ok=True,
)
migration_keys = {
    'schema', 'mode', 'cycle_id', 'campaign_state_sha256', 'round',
    'legacy_verification_command_sha256', 'verification_binding_sha256',
}
if (not isinstance(migration, dict) or set(migration) != migration_keys
        or migration.get('schema') != 'ralph-supervision-migration/v1'
        or migration.get('mode') != mode or migration.get('cycle_id') != cycle):
    raise SystemExit(1)
PY
}

ralph_supervision_ensure_initialized() {
    local mode=${1:?ralph_supervision_ensure_initialized requires a lifecycle mode}
    if [[ "$RALPH_SUPERVISION_INITIALIZED" != true || "$RALPH_SUPERVISION_STATE_MODE" != "$mode" ]]; then
        echo "ralph-supervision: lifecycle state was not explicitly initialized" >&2
        return 2
    fi
}

ralph_supervision_diagnostics_file() {
    local mode=${1:?ralph_supervision_diagnostics_file requires a lifecycle mode}
    ralph_supervision_ensure_initialized "$mode" || return $?
    mktemp ".factory-state/final-gate-$mode.XXXXXX"
}

ralph_supervision_claim_recovery() {
    local kind=${1:?ralph_supervision_claim_recovery requires a kind}
    local maximum=${2:?ralph_supervision_claim_recovery requires a kind limit}
    local no_progress_maximum=${FACTORY_RALPH_MAX_NO_PROGRESS_RECOVERIES:-8}
    [[ "$kind" == stale || "$kind" == completion ]] || return 2
    if [[ ! "$maximum" =~ ^[0-9]+$ || ! "$no_progress_maximum" =~ ^[0-9]+$ ]] \
            || (( maximum > 32 || no_progress_maximum > 32 )); then
        echo "ralph-supervision: invalid durable recovery limit" >&2
        return 2
    fi
    python3 - "$RALPH_SUPERVISION_STATE_FILE" "$RALPH_SUPERVISION_STATE_MODE" \
        "${FACTORY_RALPH_CYCLE_ID:?missing durable cycle ID}" \
        "$kind" "$maximum" "$no_progress_maximum" <<'PY'
import json, os, re, secrets, stat, sys
from pathlib import Path
sys.path.insert(0, str((Path.cwd() / 'scripts').resolve()))
from factory_state_io import StateIOError, atomic_write_json, read_json
path_text, mode, expected_cycle, kind, raw_maximum, raw_no_progress = sys.argv[1:]
maximum, no_progress_maximum = int(raw_maximum), int(raw_no_progress)
path = Path(path_text)
if path.parent.absolute() != Path('.factory-state').absolute() or path.name != f'ralph-supervision-{mode}.json':
    raise SystemExit(2)
try:
    data = read_json(Path.cwd(), path.name, maximum=16384)
except (OSError, StateIOError):
    raise SystemExit(2)
expected = {
    'schema', 'mode', 'cycle_id', 'stale_recoveries',
    'completion_recoveries', 'no_progress_recoveries',
}
cycle = data.get('cycle_id')
if (set(data) != expected or data.get('schema') != 'ralph-supervision/v2' or data.get('mode') != mode
        or cycle != expected_cycle or not isinstance(cycle, str) or not re.fullmatch(r'[0-9a-f]{64}', cycle)
        or any(not isinstance(data.get(key), int) or not 0 <= data[key] <= 64
               for key in ('stale_recoveries', 'completion_recoveries', 'no_progress_recoveries'))):
    raise SystemExit(2)
key = f'{kind}_recoveries'
if data[key] >= maximum or data['no_progress_recoveries'] >= no_progress_maximum:
    print(f'ralph-supervision: {kind} recovery limit reached '
          f'({data[key]}/{maximum}, no-progress {data["no_progress_recoveries"]}/{no_progress_maximum})',
          file=sys.stderr)
    raise SystemExit(1)
data[key] += 1
data['no_progress_recoveries'] += 1
try:
    atomic_write_json(Path.cwd(), path.name, data)
except (OSError, StateIOError):
    raise SystemExit(2)
print(f'ralph-supervision: {kind} recovery {data[key]}/{maximum}; '
      f'no-progress {data["no_progress_recoveries"]}/{no_progress_maximum}', file=sys.stderr)
PY
}

ralph_supervision_begin() {
    local mode=${1:?ralph_supervision_begin requires a lifecycle mode}
    case "$mode" in
        implementation|planning|campaign-audit|maintenance-planning|maintenance) ;;
        *) echo "ralph-supervision: invalid lifecycle mode '$mode'" >&2; return 2 ;;
    esac
    ralph_supervision_ensure_initialized "$mode" || return $?

    python3 - "$RALPH_COMPLETION_REJECTION_MARKER" <<'PY' || return 1
import sys
from pathlib import Path
sys.path.insert(0, str((Path.cwd() / 'scripts').resolve()))
from factory_state_io import StateIOError, remove
canonical = (Path.cwd() / '.factory-state/completion-rejected.json').absolute()
if Path(sys.argv[1]).absolute() != canonical:
    raise SystemExit('ralph-supervision: completion marker must use its canonical state-local path')
try:
    remove(Path.cwd(), 'completion-rejected.json', missing_ok=True)
except (OSError, StateIOError) as exc:
    raise SystemExit(f'ralph-supervision: cannot safely remove completion marker: {exc}')
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
    if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.getuid()
            or info.st_mode & 0o022):
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
    "$SCRIPT_DIR/ralph-event-boundary.py" begin "$mode"
}

ralph_supervision_finish_attempt() {
    local mode=${1:?ralph_supervision_finish_attempt requires a lifecycle mode}
    local boundary_rc
    set +e
    "$SCRIPT_DIR/ralph-event-boundary.py" finish "$mode"
    boundary_rc=$?
    set -e
    if (( boundary_rc == 0 )); then
        return 0
    elif (( boundary_rc == 3 )); then
        return 3
    fi
    echo "ralph-supervision: received event stream failed closed" >&2
    return 2
}

ralph_supervision_consume_rejection() {
    local mode=${1:?ralph_supervision_consume_rejection requires a lifecycle mode}
    local workspace=${2:?ralph_supervision_consume_rejection requires a workspace}
    python3 - "$RALPH_COMPLETION_REJECTION_MARKER" "$FACTORY_RALPH_ATTEMPT_ID" "$mode" "$workspace" <<'PY'
import re
import sys
from pathlib import Path
sys.path.insert(0, str((Path.cwd() / 'scripts').resolve()))
from factory_state_io import StateIOError, consume_json
marker, attempt, mode, workspace = sys.argv[1:]
canonical = (Path.cwd() / '.factory-state/completion-rejected.json').absolute()
if Path(marker).absolute() != canonical:
    print('ralph-supervision: completion marker must use its canonical state-local path', file=sys.stderr)
    raise SystemExit(2)
def validate(data):
    if not isinstance(data, dict) or set(data) != {'schema', 'attempt_id', 'mode', 'workspace', 'loop_id'}:
        raise StateIOError('completion-rejection marker has an invalid schema')
    expected = {
        'schema': 'ralph-completion-rejection/v1',
        'attempt_id': attempt,
        'mode': mode,
        'workspace': str(Path(workspace).resolve(strict=True)),
    }
    if any(data.get(key) != value for key, value in expected.items()):
        raise StateIOError('stale or mismatched completion-rejection marker')
    loop_id = data.get('loop_id')
    if not isinstance(loop_id, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,127}', loop_id):
        raise StateIOError('completion-rejection marker has an invalid loop ID')
    return loop_id
try:
    print(consume_json(Path.cwd(), 'completion-rejected.json', validate, maximum=16384))
except FileNotFoundError:
    raise SystemExit(1)
except (OSError, StateIOError) as exc:
    print(f'ralph-supervision: invalid completion-rejection marker: {exc}', file=sys.stderr)
    raise SystemExit(2)
PY
}

ralph_supervision_allow_completion_recovery() {
    local maximum=${FACTORY_RALPH_MAX_COMPLETION_RECOVERIES:-8}
    ralph_supervision_claim_recovery completion "$maximum"
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
    if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.getuid()
            or info.st_mode & 0o022):
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
    local claim_rc
    if ralph_supervision_claim_recovery stale "$maximum"; then
        claim_rc=0
    else
        claim_rc=$?
    fi
    if (( claim_rc == 1 )); then
        return 3
    elif (( claim_rc != 0 )); then
        return 2
    fi
    ralph_supervision_record_stale_feedback "$mode" "$workspace" || return 2
    return 0
}
