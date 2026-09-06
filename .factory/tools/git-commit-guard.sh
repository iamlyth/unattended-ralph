#!/usr/bin/env bash
# Fail-closed Ralph Git commit boundary.
#
# Installed by .factory/tools/install-git-commit-guard.sh as three Git hooks that all
# exec this script:
#
#   pre-commit        --hook pre-commit      (policy gate for `git commit`)
#   pre-merge-commit  --hook pre-merge-commit (policy gate for `git merge`)
#   commit-msg        --hook commit-msg      (one-shot consumption; runs for
#                                             every commit-creation path: git
#                                             commit, merge, cherry-pick,
#                                             revert, am, rebase, pull)
#
# Policy: an ordinary checkpoint never commits scratchpad-only state. A commit
# whose entire staged content is Ralph recovery metadata (everything under
# `.ralph/`) manufactures fake Git progress and is rejected. The single trusted
# exception is exactly one final-handoff commit per durable lifecycle cycle: a
# one-shot authorization token (bound to the durable cycle ID and lifecycle
# mode) is written by the lifecycle checkpoint path and the commit-msg hook
# validates and consumes it at the boundary, so the exception cannot be
# replayed. Substantive commits (any staged path outside `.ralph/`) remain
# allowed, including commits that also carry the scratchpad. All three hooks
# enforce the same content policy; only commit-msg consumes the token, so
# commit-creation paths that never run pre-commit (merge, cherry-pick, revert,
# am, rebase) are guarded identically.
set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
PROJECT_ROOT=$(cd -- "$SCRIPT_DIR/../.." && pwd)

HOOK=policy
if [[ ${1:-} == --hook ]]; then
    HOOK=${2:-policy}
fi

SCRATCHPAD=.ralph/agent/scratchpad.md
TOKEN_NAME=final-handoff-authorization.json
# shellcheck disable=SC2034 # documented in the python validator below
MODES="planning implementation campaign-audit maintenance-planning maintenance"

# A hook always runs inside the repository, but the guard must also work when
# invoked directly by tests from a nested cwd. Resolve the repo and refuse to
# guard a different repository than the one that owns this script.
REPO_ROOT=$(git rev-parse --show-toplevel 2>/dev/null || true)
if [[ -z "$REPO_ROOT" ]]; then
    echo "git-commit-guard: not inside a Git repository" >&2
    exit 1
fi
if [[ "$(realpath -e -- "$REPO_ROOT")" != "$(realpath -e -- "$PROJECT_ROOT")" ]]; then
    echo "git-commit-guard: guard repository mismatch ($REPO_ROOT)" >&2
    exit 1
fi
cd -- "$PROJECT_ROOT"

# Staged paths relative to the repository root, NUL-separated so that any path
# (spaces, newlines) is handled exactly.
mapfile -d '' -t STAGED < <(git diff --cached --name-only -z)

SUBSTANTIVE=false
METADATA_BEYOND_SCRATCHPAD=false
for path in "${STAGED[@]}"; do
    if [[ "$path" != .ralph/* ]]; then
        SUBSTANTIVE=true
    elif [[ "$path" != "$SCRATCHPAD" ]]; then
        METADATA_BEYOND_SCRATCHPAD=true
    fi
done

if [[ "$SUBSTANTIVE" == true ]]; then
    echo "git-commit-guard: substantive commit allowed (metadata may ride along)"
    exit 0
fi

# Everything staged is Ralph recovery metadata (or nothing at all). This is
# exactly the progress-forging case the boundary must reject unless the
# one-shot final-handoff authorization is present and valid.
if [[ ${#STAGED[@]} -eq 0 ]]; then
    echo "git-commit-guard: empty commits manufacture metadata-only progress" >&2
    exit 1
fi
if [[ "$METADATA_BEYOND_SCRATCHPAD" == true ]]; then
    echo "git-commit-guard: metadata-only commit beyond the final-handoff scratchpad is forbidden" >&2
    # Fail closed and burn any token so a mismatched staged set cannot reuse it.
    python3 - "$TOKEN_NAME" <<'PY' || true
import sys
from pathlib import Path
sys.path.insert(0, str((Path.cwd() / '.factory' / 'loop').resolve()))
from factory_state_io import remove
try:
    remove(Path.cwd(), sys.argv[1])
except SystemExit:
    pass
PY
    exit 1
fi

# The authorized scratchpad index entry must be a real regular file, not a
# gitlink (submodule trick) and not a symlink whose blob is committed instead
# of the guarded file.
entry=$(git ls-files --stage -- "$SCRATCHPAD" || true)
case "$entry" in
    100644\ *|100755\ *) ;;
    *)
        echo "git-commit-guard: scratchpad is not a trusted regular file in the index" >&2
        exit 1
        ;;
esac
status=$(git diff --cached --name-status -- "$SCRATCHPAD")
if [[ "$status" == R* || "$status" == C* ]]; then
    echo "git-commit-guard: final handoff must not rename or copy the scratchpad" >&2
    exit 1
fi

# Validate (and, at commit-msg, one-shot consume) the lifecycle authorization.
# The token is written only by the trusted final-handoff flow; the guard burns
# the token whether or not it is valid, so a stale or forged attempt cannot be
# replayed.
if python3 - "$TOKEN_NAME" "${FACTORY_RALPH_CYCLE_ID:-}" "$HOOK" <<'PY'
import re
import sys
from pathlib import Path

sys.path.insert(0, str((Path.cwd() / '.factory' / 'loop').resolve()))
from factory_state_io import StateIOError, consume_json, read_json, remove

name = sys.argv[1]
env_cycle = sys.argv[2] or ""
hook = sys.argv[3]
root = Path.cwd()

MODES = {"planning", "implementation", "campaign-audit", "maintenance-planning", "maintenance"}
CYCLE = re.compile(r"^[0-9a-f]{64}$")
NONCE = re.compile(r"^[0-9a-f]{64}$")


def fail(message: str) -> None:
    raise StateIOError(message)


def validate_token(data: object) -> dict:
    expected = {"schema", "mode", "cycle_id", "nonce"}
    if (
        not isinstance(data, dict)
        or set(data) != expected
        or data.get("schema") != "ralph-final-handoff/v1"
        or data.get("mode") not in MODES
        or not isinstance(data.get("cycle_id"), str)
        or not CYCLE.fullmatch(data["cycle_id"])
        or not isinstance(data.get("nonce"), str)
        or not NONCE.fullmatch(data["nonce"])
    ):
        fail("final-handoff authorization has an invalid schema")
    cycle = data["cycle_id"]
    if env_cycle:
        if env_cycle != cycle:
            fail("final-handoff authorization cycle does not match the launcher cycle")
    lifecycle = read_json(
        root,
        f"final-handoff-{data['mode']}.json",
        maximum=16384,
        missing_ok=True,
    )
    if isinstance(lifecycle, dict):
        if lifecycle.get("schema") != "ralph-final-state/v1" or lifecycle.get("cycle_id") != cycle:
            fail("final-handoff authorization cycle does not match durable lifecycle state")
    elif not env_cycle:
        fail("final-handoff authorization cannot be bound to any lifecycle state")
    return data


if hook == "commit-msg":
    try:
        consume_json(root, name, validate_token, maximum=16384)
    except Exception:
        # Burn the marker even when it is invalid so a stale or forged
        # authorization can never be replayed; consume_json only quarantines
        # validated markers, so removal is the guard's responsibility here.
        try:
            remove(root, name)
        except Exception:
            pass
        raise SystemExit(1)
else:
    # Policy hooks (pre-commit, pre-merge-commit) validate without consuming;
    # the commit-msg hook consumes the authorization once for every commit
    # path, so the at-most-one guarantee holds even for commits that never
    # run pre-commit. Invalid tokens are burned here too.
    try:
        validate_token(read_json(root, name, maximum=16384))
    except Exception:
        try:
            remove(root, name)
        except Exception:
            pass
        raise SystemExit(1)
PY
then
    echo "git-commit-guard: trusted final-handoff authorization accepted"
    exit 0
fi

echo "git-commit-guard: metadata-only commit requires a valid final-handoff authorization" >&2
exit 1
