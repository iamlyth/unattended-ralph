#!/usr/bin/env bash
# Audited operator pathway for a legitimate campaign verifier migration.
#
# The campaign verifier binding pins the gate entrypoint, its config, and the
# tracked acceptance gate list (.factory/verifier-acceptance.json). A strict
# strengthening (entrypoint and config unchanged, gate list only grew)
# auto-rebinds on resume with a durable audit record and needs no operator.
# Any other verifier change (entrypoint/config edit, gate removal, or missing
# baseline contract) halts the campaign and must be migrated through this
# audited pathway: it validates the change, records an authorization receipt,
# archives the prior lifecycle state, creates the supervision migration marker,
# and promotes the binding. It is idempotent and never guesses about locks.
set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
PROJECT_ROOT=$(cd -- "$SCRIPT_DIR/.." && pwd)
STATE_HELPER="$SCRIPT_DIR/ralph-campaign-state.py"
MODE=implementation
AUTHORITY=""
REASON=""
ORIGINAL_ARGS=("$@")

usage() {
    cat <<'EOF'
Usage: scripts/ralph-verifier-migrate.sh --mode <planning|implementation|campaign-audit> [--authority <string>] [--reason <string>]

Records an audited operator authorization, archives the prior lifecycle state,
and promotes the campaign verifier binding to the current committed verifier.
--authority is required unless the change is a strict strengthening (which the
campaign auto-rebinds anyway); it names the human/operator authorizing the
migration and is recorded in the durable receipt.
EOF
}

die() { echo "ralph-verifier-migrate: $*" >&2; exit 1; }

while (( $# > 0 )); do
    case "$1" in
        --mode) (( $# >= 2 )) || die "--mode requires a value"; MODE=$2; shift 2 ;;
        --authority) (( $# >= 2 )) || die "--authority requires a value"; AUTHORITY=$2; shift 2 ;;
        --reason) (( $# >= 2 )) || die "--reason requires a value"; REASON=$2; shift 2 ;;
        -h|--help) usage; exit 0 ;;
        *) die "unknown option '$1'" ;;
    esac
done
case "$MODE" in
    planning|implementation|campaign-audit) ;;
    *) die "invalid mode '$MODE'" ;;
esac

cd -- "$PROJECT_ROOT"
# shellcheck source=scripts/factory-lock.sh
source "$SCRIPT_DIR/factory-lock.sh"
factory_lock_bootstrap "$PROJECT_ROOT" "$PROJECT_ROOT/scripts/ralph-verifier-migrate.sh" "${ORIGINAL_ARGS[@]}"
factory_lock_acquire "$PROJECT_ROOT"

# 1. Compute the current committed binding.
verification_binding=$(factory_lock_run_untrusted ./scripts/campaign-verifier-binding.py) || exit $?
verification_digest=$(python3 - "$verification_binding" <<'PY'
import json, sys
binding = json.loads(sys.argv[1])
if set(binding) != {'binding', 'sha256', 'helper'}:
    raise SystemExit('ralph-verifier-migrate: invalid verifier binding output')
print(binding['sha256'])
PY
) || exit $?

# 2. Load the saved digest; idempotent when already current.
saved_digest=$("$STATE_HELPER" get verification_command_sha256)
if [[ "$saved_digest" == "$verification_digest" ]]; then
    echo "ralph-verifier-migrate: verifier binding is already current ($verification_digest)"
    exit 0
fi

# 3. Classify the change.
classification=$("$STATE_HELPER" classify-verifier-change \
    --expected-old "$saved_digest" --new "$verification_digest") || exit $?
case "$classification" in
    auto-strengthening|weakening|ambiguous) ;;
    *) die "invalid verifier change classification '$classification'" ;;
esac

# 4. A strict strengthening auto-rebinds on resume; the operator pathway is only
#    required for weakening/ambiguous changes, which need explicit authority.
if [[ "$classification" != auto-strengthening && -z "$AUTHORITY" ]]; then
    die "verifier change is $classification; operator authorization (--authority) is required"
fi

# 5. Record the durable authorization receipt and archive the prior state.
archive_dir=".factory-state/operator-archive/verifier-migration-$(date -u +%Y%m%dT%H%M%SZ)"
mkdir -p "$archive_dir"
head=$(git rev-parse HEAD)
receipt="$archive_dir/authorization-receipt.json"
python3 - "$receipt" "$archive_dir" "$MODE" "$saved_digest" "$verification_digest" \
    "$classification" "$AUTHORITY" "$REASON" "$head" <<'PY'
import datetime, json, shutil, sys
from pathlib import Path
receipt, archive_dir, mode, old, new, classification, authority, reason, head = sys.argv[1:]
archive = Path(archive_dir)
Path(receipt).write_text(json.dumps({
    "schema": "factory-operator-verifier-authorization/v1",
    "authority": authority or "unattended-operator",
    "authorized_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    "head": head,
    "mode": mode,
    "old_binding": old,
    "new_binding": new,
    "classification": classification,
    "reason": reason,
}, sort_keys=True, indent=2) + "\n", encoding="utf-8")
for name in ("ralph-campaign.json", f"ralph-supervision-{mode}.json", f"ralph-supervision-migration-{mode}.json"):
    source = Path('.factory-state') / name
    if source.is_file():
        shutil.copy2(source, archive / name)
PY

# 6. Create the supervision migration marker (validates the campaign digest and
#    the old/new binding digests; a superseded previous marker is archived in the
#    operator archive before replacement, idempotent when the marker matches).
python3 - "$MODE" "$saved_digest" "$verification_digest" "$archive_dir" <<'PY'
import hashlib, json, os, re, sys
from pathlib import Path
sys.path.insert(0, str((Path.cwd() / 'scripts').resolve()))
from factory_state_io import StateIOError, atomic_write_json, read_json
mode, old, new, archive_dir = sys.argv[1:]
sha256 = re.compile(r'^[0-9a-f]{64}$')
if not sha256.fullmatch(old) or not sha256.fullmatch(new):
    raise SystemExit('ralph-verifier-migrate: invalid verifier binding digest')
raw = (Path('.factory-state') / 'ralph-campaign.json').read_bytes()
digest = hashlib.sha256(raw).hexdigest()
campaign = json.loads(raw.decode('utf-8'))
expected_phase = 'audit' if mode == 'campaign-audit' else mode
if (campaign.get('schema') != 'ralph-campaign/v2' or campaign.get('status') != 'active'
        or campaign.get('phase') != expected_phase
        or not isinstance(campaign.get('round'), int) or isinstance(campaign.get('round'), bool)):
    raise SystemExit('ralph-verifier-migrate: campaign is not stopped in the requested active phase')
cycle_material = json.dumps({
    'schema': 'ralph-supervision-migration-cycle/v1',
    'mode': mode,
    'campaign_state_sha256': digest,
    'legacy_verification_command_sha256': old,
    'verification_binding_sha256': new,
}, sort_keys=True, separators=(',', ':')).encode('utf-8')
cycle = hashlib.sha256(cycle_material).hexdigest()
marker_name = f'ralph-supervision-migration-{mode}.json'
try:
    existing = read_json(Path.cwd(), marker_name, maximum=16384, missing_ok=True)
except (OSError, StateIOError) as exc:
    raise SystemExit(f'ralph-verifier-migrate: cannot safely read migration marker: {exc}')
marker = {
    'schema': 'ralph-supervision-migration/v1',
    'mode': mode,
    'cycle_id': cycle,
    'campaign_state_sha256': digest,
    'round': campaign['round'],
    'legacy_verification_command_sha256': old,
    'verification_binding_sha256': new,
}
if existing is not None and existing != marker:
    # A superseded migration marker is preserved in the operator archive
    # before replacement; the lifecycle state was already archived in step 5.
    source = Path('.factory-state') / marker_name
    try:
        os.replace(source, Path(archive_dir) / marker_name)
    except OSError as exc:
        raise SystemExit(f'ralph-verifier-migrate: cannot archive stale migration marker: {exc}')
try:
    atomic_write_json(Path.cwd(), marker_name, marker)
except (OSError, StateIOError) as exc:
    raise SystemExit(f'ralph-verifier-migrate: cannot safely write migration marker: {exc}')
PY

# 7. Promote the binding (auto-rebinds on strict strengthening; otherwise the
#    marker authorizes the operator migration).
"$STATE_HELPER" promote-verifier-binding --mode "$MODE" \
    --expected-old "$saved_digest" --new "$verification_digest"

# 8. Record the new contract as the baseline for future classification.
"$STATE_HELPER" record-verifier-contract --digest "$verification_digest" --binding "$verification_binding"

echo "ralph-verifier-migrate: verifier binding migrated ($saved_digest -> $verification_digest, $classification)"
echo "ralph-verifier-migrate: authorization receipt: $receipt"
