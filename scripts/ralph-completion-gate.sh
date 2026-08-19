#!/usr/bin/env bash
# Validate one completion hook payload in-memory and attest its strict gate result.
set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
PROJECT_ROOT=$(cd -- "$SCRIPT_DIR/.." && pwd)
MODE=${1:-}

PYTHON_CODE=
IFS= read -r -d '' PYTHON_CODE <<'PY' || true
import json
import os
from pathlib import Path
import re
import subprocess
import sys

root = Path(sys.argv[1])
mode = sys.argv[2]
if mode not in {
    'implementation', 'planning', 'campaign-audit',
    'maintenance-planning', 'maintenance',
}:
    raise SystemExit(f'ralph-completion-gate: invalid lifecycle mode {mode!r}')
sys.path.insert(0, str(root / 'scripts'))
from factory_state_io import StateIOError, atomic_write_json, remove, require_linux_primitives

try:
    require_linux_primitives()
except StateIOError as exc:
    raise SystemExit(f'ralph-completion-gate: {exc}') from exc
canonical_marker = root / '.factory-state/completion-rejected.json'
configured_marker = Path(os.environ.get('RALPH_COMPLETION_REJECTION_MARKER', canonical_marker)).absolute()
if configured_marker != canonical_marker:
    raise SystemExit('ralph-completion-gate: rejection marker must use its canonical state-local path')
attempt = os.environ.get('FACTORY_RALPH_ATTEMPT_ID', '')
cycle = os.environ.get('FACTORY_RALPH_CYCLE_ID', '')
if not re.fullmatch(r'[0-9a-f]{64}', attempt):
    raise SystemExit('ralph-completion-gate: missing or invalid launch attempt ID')
if not re.fullmatch(r'[0-9a-f]{64}', cycle):
    raise SystemExit('ralph-completion-gate: missing or invalid durable cycle ID')

# The payload has one authority: these exact bytes retained in memory. No
# pathname is created, truncated, closed, or reopened between validation and
# rejection-marker construction.
raw = sys.stdin.buffer.read(65537)
if len(raw) > 65536:
    raise SystemExit('ralph-completion-gate: hook payload exceeds 64 KiB')
try:
    payload = json.loads(raw.decode('utf-8'))
except (UnicodeError, json.JSONDecodeError) as exc:
    raise SystemExit(f'ralph-completion-gate: invalid hook payload: {exc}') from exc
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
if not isinstance(loop_id, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,127}', loop_id):
    raise SystemExit('ralph-completion-gate: hook payload has no valid loop ID')
try:
    workspace_path = Path(workspace).resolve(strict=True) if isinstance(workspace, str) else None
except OSError:
    workspace_path = None
if workspace_path != root.resolve(strict=True):
    raise SystemExit('ralph-completion-gate: hook workspace does not match this repository')

child_env = {**os.environ}
if mode != 'maintenance-planning':
    # Completion attestation belongs to the trusted hook/leaf authority for
    # these modes. Maintenance planning delegates finalization to its trusted
    # parent (see below) and the uncommitted scratchpad still needs the
    # parent's final-handoff checkpoint, so its gate runs validate-only.
    child_env['FACTORY_FINAL_GATE_ATTEST'] = '1'
result = subprocess.run([str(root / 'scripts/final-gate.sh'), f'--{mode}'], cwd=root, env=child_env)
rc = result.returncode
if rc == 0:
    try:
        remove(root, 'completion-rejected.json', missing_ok=True)
    except (OSError, StateIOError) as exc:
        raise SystemExit(f'ralph-completion-gate: cannot safely remove rejection marker: {exc}') from exc
    if mode == 'maintenance-planning':
        # The completion hook validates only unprivileged completion artifacts.
        # The trusted parent shell performs the ledger/finalization transition
        # under the retained factory lock, commits the strict final handoff,
        # attests the clean unchanged HEAD, and only then marks final state;
        # attesting here would authorize completion without the lock and let a
        # later ledger-only commit follow the attested handoff.
        raise SystemExit(0)
    head_result = subprocess.run(
        ['git', 'rev-parse', 'HEAD'], cwd=root, text=True, capture_output=True,
        env={**os.environ, 'GIT_NO_REPLACE_OBJECTS': '1'},
    )
    if head_result.returncode:
        raise SystemExit('ralph-completion-gate: cannot resolve final HEAD')
    attest = subprocess.run(
        [str(root / 'scripts/ralph-final-state.py'), 'attest', mode, head_result.stdout.strip()],
        cwd=root,
    )
    raise SystemExit(attest.returncode)
if rc < 0:
    raise SystemExit(128 + -rc)
if rc >= 128:
    print(f'ralph-completion-gate: final gate interrupted with status {rc}; no continuation marker written', file=sys.stderr)
    raise SystemExit(rc)
if rc != 1:
    print(f'ralph-completion-gate: final gate failed with infrastructure status {rc}; no continuation marker written', file=sys.stderr)
    raise SystemExit(rc)
data = {
    'schema': 'ralph-completion-rejection/v1',
    'attempt_id': attempt,
    'mode': mode,
    'workspace': str(root.resolve(strict=True)),
    'loop_id': loop_id,
}
try:
    atomic_write_json(root, 'completion-rejected.json', data)
except (OSError, StateIOError) as exc:
    raise SystemExit(f'ralph-completion-gate: cannot safely write rejection marker: {exc}') from exc
raise SystemExit(1)
PY
exec python3 -c "$PYTHON_CODE" "$PROJECT_ROOT" "$MODE"
