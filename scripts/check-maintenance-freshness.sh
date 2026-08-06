#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
PROJECT_ROOT=$(cd -- "$SCRIPT_DIR/.." && pwd)
PLAN=${FACTORY_MAINTENANCE_PLAN_PATH:-$PROJECT_ROOT/MAINTENANCE_PLAN.md}
SELECTION="$PROJECT_ROOT/.factory-state/maintenance-bug-id"
PHASE=committed
if [[ ${1:-} == --planning ]]; then
    PHASE=planning
    shift
fi
(( $# == 0 )) || { echo "Usage: scripts/check-maintenance-freshness.sh [--planning]" >&2; exit 2; }
cd -- "$PROJECT_ROOT"

[[ -s "$SELECTION" ]] || { echo "maintenance-freshness: no selected bug; run ralph-maintenance-plan.sh BUG-ID" >&2; exit 1; }
BUG_ID=$(tr -d '[:space:]' < "$SELECTION")
[[ "$BUG_ID" =~ ^BUG-[0-9]{4,}$ ]] || { echo "maintenance-freshness: invalid selected bug ID" >&2; exit 1; }
[[ -s "$PLAN" ]] || { echo "maintenance-freshness: missing MAINTENANCE_PLAN.md" >&2; exit 1; }
./scripts/bug-ledger.py validate >/dev/null
./scripts/validate-maintenance-plan.py current "$PLAN" >/dev/null

mapfile -t META < <(python3 - "$PLAN" "$PHASE" <<'PY'
import importlib.util, os, pathlib, subprocess, sys
plan_path, phase = pathlib.Path(sys.argv[1]), sys.argv[2]
module_path = pathlib.Path('scripts/validate-maintenance-plan.py')
spec = importlib.util.spec_from_file_location('maintenance_plan_validator', module_path)
module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
current, _ = module.parse(plan_path.read_text(encoding='utf-8'))
immutable = tuple(key for key in module.KEYS if key != 'status')

def git(*args):
    return subprocess.check_output(['git', *args], text=True).strip()

if phase == 'planning':
    expected_base = os.environ.get('FACTORY_MAINTENANCE_BASE_COMMIT', '').strip()
    if not expected_base:
        marker = pathlib.Path('.factory-state/maintenance-base-commit')
        expected_base = marker.read_text(encoding='utf-8').strip() if marker.is_file() else ''
    if not expected_base or current['base_commit'] != expected_base:
        raise SystemExit('maintenance-freshness: planning base_commit differs from the selected cycle base')
    try:
        git('cat-file', '-e', f'{expected_base}^{{commit}}')
    except subprocess.CalledProcessError:
        raise SystemExit('maintenance-freshness: selected cycle base is not a commit')
else:
    checkpoint_meta = None
    for commit in git('log', '--format=%H', '--', 'MAINTENANCE_PLAN.md').splitlines():
        body = git('show', '-s', '--format=%B', commit)
        if not any(line == 'Mode: maintenance-planning' for line in body.splitlines()):
            continue
        try:
            checkpoint_text = subprocess.check_output(
                ['git', 'show', f'{commit}:MAINTENANCE_PLAN.md'], text=True,
                stderr=subprocess.DEVNULL,
            )
            checkpoint_meta, _ = module.parse(checkpoint_text)
            break
        except (subprocess.CalledProcessError, ValueError):
            # A draft checkpoint may be structurally invalid. The final wrapper
            # creates a newer valid lifecycle checkpoint after the planning gate.
            continue
    if checkpoint_meta is None:
        raise SystemExit('maintenance-freshness: no valid committed maintenance planning checkpoint')
    if any(current[key] != checkpoint_meta[key] for key in immutable):
        raise SystemExit('maintenance-freshness: immutable plan metadata differs from planning checkpoint')

for key in module.KEYS:
    print(current[key])
PY
)
(( ${#META[@]} == 7 )) || { echo "maintenance-freshness: incomplete plan metadata" >&2; exit 1; }
[[ "${META[0]}" == "$BUG_ID" ]] || { echo "maintenance-freshness: selected bug does not match plan" >&2; exit 1; }

CANONICAL_SPEC=$(python3 - <<'PY'
import tomllib
with open('factory.toml', 'rb') as stream:
    print(tomllib.load(stream)['project']['spec'])
PY
)
[[ "${META[2]}" == "$CANONICAL_SPEC" && -f "$CANONICAL_SPEC" ]] || { echo "maintenance-freshness: non-canonical or missing spec" >&2; exit 1; }
if ! git diff --quiet -- "$CANONICAL_SPEC" || ! git diff --cached --quiet -- "$CANONICAL_SPEC"; then
    echo "maintenance-freshness: specification has uncommitted changes" >&2
    exit 1
fi
git cat-file -e "${META[5]}^{commit}" 2>/dev/null || { echo "maintenance-freshness: invalid base commit" >&2; exit 1; }
git merge-base --is-ancestor "${META[5]}" HEAD || { echo "maintenance-freshness: base is not an ancestor of HEAD" >&2; exit 1; }
ACTUAL_COMMIT=$(git log -1 --format=%H -- "$CANONICAL_SPEC")
ACTUAL_BLOB=$(git rev-parse "HEAD:$CANONICAL_SPEC")
[[ "$ACTUAL_COMMIT" == "${META[3]}" && "$ACTUAL_BLOB" == "${META[4]}" ]] || {
    echo "maintenance-freshness: committed specification changed after planning" >&2
    exit 1
}

mapfile -t BUG_META < <(python3 - "$BUG_ID" <<'PY'
import json, re, sys
bug_id = sys.argv[1]
for path in ('open-bugs.md', 'closed-bugs.md'):
    text = open(path, encoding='utf-8').read()
    data = json.loads(re.search(r'```json\s*\n(.*?)\n```', text, re.S).group(1))
    for record in data:
        if record['id'] == bug_id:
            print(path)
            print(record['contract_change'])
            raise SystemExit
raise SystemExit(f'maintenance-freshness: selected bug {bug_id} does not exist')
PY
)
(( ${#BUG_META[@]} == 2 )) || { echo "maintenance-freshness: selected bug lookup failed" >&2; exit 1; }
[[ "${BUG_META[1]}" == False ]] || { echo "maintenance-freshness: contract-change bugs require the human spec workflow" >&2; exit 1; }
ACTUAL_FINGERPRINT=$(./scripts/bug-ledger.py fingerprint "$BUG_ID")
[[ "$ACTUAL_FINGERPRINT" == "${META[1]}" ]] || { echo "maintenance-freshness: immutable bug intake changed after planning" >&2; exit 1; }

if [[ "${BUG_META[0]}" == closed-bugs.md ]]; then
    ./scripts/validate-maintenance-plan.py complete "$PLAN" >/dev/null || {
        echo "maintenance-freshness: selected bug may close only during a completed maintenance plan" >&2
        exit 1
    }
fi
printf 'maintenance-freshness: bug=%s spec=%s fingerprint=%s\n' "$BUG_ID" "$CANONICAL_SPEC" "${ACTUAL_FINGERPRINT:0:12}"
