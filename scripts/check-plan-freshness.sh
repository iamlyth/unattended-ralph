#!/usr/bin/env bash
set -euo pipefail

# Retained legacy/deprecated compatibility gate.  The fresh Python loop
# (`.factory/loop/`) derives planning freshness and the planning base from the
# committed plan front matter, Git, and the single `factory-state/v1` file; it
# never consults the legacy `planning-base-commit` marker below.  The
# `--planning` base comparison reads the frozen `scripts/ralph-plan.sh` marker
# (`FACTORY_PLANNING_BASE_COMMIT` / `factory-state-file.py read
# planning-base-commit`) and exists only for the deprecated legacy launchers;
# the fresh campaign binds the planning base from its own state instead.
# Error messages below therefore point at the fresh planning path.
#
# Task 23 freshness scope: the plan is only as fresh as the harness that
# executes it.  The checker therefore (1) requires a **clean tree** (no
# uncommitted tracked change and no untracked non-ignored file anywhere), (2)
# includes the hidden control-plane namespaces (`.factory/`, `.pi/`) and the
# policy/checker/receipt tools in the freshness scope — every installed-
# surface path and every `scripts/` policy/checker/receipt authority must be
# tracked at HEAD and byte-identical to the worktree, so an uncommitted
# harness or evidence-tool change can never silently accompany a "fresh"
# plan, and (3) resolves every namespace parent no-follow with a pinned Git
# executable and rejects a `..`/symlink namespace.

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
if [[ -n "${FACTORY_VERIFIER_ROOT:-}" ]]; then
    # The gate child executes the committed script through a retained
    # descriptor (`/proc/self/fd/<fd>`), so `BASH_SOURCE[0]` names the fd
    # path, never the canonical repository path.  The trusted parent pins
    # the canonical root instead, and the script directory is re-derived
    # from it.
    PROJECT_ROOT=$(realpath -e -- "$FACTORY_VERIFIER_ROOT")
    SCRIPT_DIR="$PROJECT_ROOT/scripts"
else
    PROJECT_ROOT=$(cd -- "$SCRIPT_DIR/.." && pwd)
fi
PY=${PYTHON:-python3}
PLAN=${FACTORY_PLAN_PATH:-$PROJECT_ROOT/.factory/artifacts/implementation-plan.md}
PHASE=committed
if [[ ${1:-} == --planning ]]; then
    PHASE=planning
    shift
fi
(( $# == 0 )) || { echo "Usage: scripts/check-plan-freshness.sh [--planning]" >&2; exit 2; }
cd -- "$PROJECT_ROOT"

# -- pinned Git (never a caller-controlled `git` from PATH) ------------------
# The same PATH-pinned absolute executable the hidden control plane resolves
# (`gitutil.GIT_EXECUTABLE`) is used for every trusted Git read of this
# checker; a poisoned PATH or GIT_* override can never redirect the binding
# reads.
PINNED_GIT=$(PYTHONDONTWRITEBYTECODE=1 "$PY" - "$PROJECT_ROOT" <<'PY'
import importlib.util, sys
root = sys.argv[1]
spec = importlib.util.spec_from_file_location("_fg", root + "/.factory/loop/gitutil.py")
module = importlib.util.module_from_spec(spec)
sys.modules["_fg"] = module
spec.loader.exec_module(module)
print(module.GIT_EXECUTABLE)
PY
)
[[ -n "$PINNED_GIT" && -x "$PINNED_GIT" ]] || {
    echo "plan-freshness: cannot resolve the pinned Git executable" >&2
    exit 1
}

# -- clean tree ---------------------------------------------------------------
# The plan is fresh only against a clean tree: an uncommitted tracked change
# or an untracked non-ignored file (in the product tree, the hidden control
# plane, or anywhere else) means the executing harness is not the committed
# one and planning/implementation must not proceed.
CLEAN_STATUS=$("$PINNED_GIT" status --porcelain -z --untracked-files=all | tr -d '\000') || exit $?
if [[ -n "$CLEAN_STATUS" ]]; then
    echo "plan-freshness: the working tree is not clean; plan freshness is" >&2
    echo "plan-freshness: undefined while uncommitted or untracked files exist" >&2
    "$PINNED_GIT" status --porcelain --untracked-files=all | head -20 >&2
    exit 1
fi

# -- no-follow namespace parents ---------------------------------------------
# `.factory`, `.pi`, and `scripts` are the harness namespaces of the plan
# contract.  They must be real directories (never symlinks) reachable with no
# `..` traversal; a symlinked or traversing namespace fails closed.  `.factory`
# and `scripts` are mandatory; `.pi` is validated whenever the repository
# carries it (an adopting fixture without a `.pi` namespace declares no such
# contract).
for ns in .factory scripts; do
    if [[ -L "$PROJECT_ROOT/$ns" ]]; then
        echo "plan-freshness: namespace '$ns' is a symlink; the plan contract" >&2
        echo "plan-freshness: requires a real directory (no-follow)" >&2
        exit 1
    fi
    if [[ ! -d "$PROJECT_ROOT/$ns" ]]; then
        echo "plan-freshness: namespace '$ns' is missing; the plan contract" >&2
        echo "plan-freshness: cannot resolve its harness authority" >&2
        exit 1
    fi
done
if [[ -e "$PROJECT_ROOT/.pi" || -L "$PROJECT_ROOT/.pi" ]]; then
    if [[ -L "$PROJECT_ROOT/.pi" || ! -d "$PROJECT_ROOT/.pi" ]]; then
        echo "plan-freshness: namespace '.pi' is a symlink or not a real" >&2
        echo "plan-freshness: directory; the plan contract requires no-follow" >&2
        exit 1
    fi
fi

[[ -s "$PLAN" ]] || { echo "plan-freshness: missing .factory/artifacts/implementation-plan.md; start a fresh planning phase (python3 .factory/loop/campaign.py --root \"\$PWD\" run --campaign-id <id> --rounds <n> --branch <branch>)" >&2; exit 1; }

mapfile -t META < <("$PY" - "$PLAN" <<'PY'
import sys
path = sys.argv[1]
lines = open(path, encoding='utf-8').read().splitlines()
if not lines or lines[0].strip() != '---':
    raise SystemExit('plan-freshness: plan has no metadata front matter')
meta = {}
for line in lines[1:]:
    if line.strip() == '---':
        break
    if ':' in line:
        key, value = line.split(':', 1)
        meta[key.strip()] = value.strip().strip('"\'')
for key in ('spec_path', 'spec_commit', 'spec_blob', 'base_commit', 'status'):
    print(meta.get(key, ''))
PY
)

(( ${#META[@]} == 5 )) || { echo "plan-freshness: incomplete plan metadata" >&2; exit 1; }
SPEC_PATH=${META[0]}
RECORDED_COMMIT=${META[1]}
RECORDED_BLOB=${META[2]}
BASE_COMMIT=${META[3]}
STATUS=${META[4]}
CANONICAL_SPEC=$("$PY" - <<'PY'
import tomllib
with open('.factory/config.toml', 'rb') as stream:
    print(tomllib.load(stream)['project']['spec'])
PY
)

[[ "$SPEC_PATH" == "$CANONICAL_SPEC" ]] || {
    echo "plan-freshness: plan spec '$SPEC_PATH' does not match canonical '$CANONICAL_SPEC'" >&2
    exit 1
}
[[ -n "$SPEC_PATH" && -f "$SPEC_PATH" ]] || { echo "plan-freshness: missing spec '$SPEC_PATH'" >&2; exit 1; }
# The bound canonical spec must never be a neutral placeholder. In this
# boilerplate cycle the plan resolves `docs/FACTORY-LOOP-SPEC.md` (commit
# `2d6a4fd`, blob `ca2334ab…`); `docs/SPEC.md` remains the adopting-product
# placeholder and is never planned against. The guard stays path-neutral so an
# adopting repository's real committed spec keeps passing.
if grep -q 'SPEC_PENDING_HUMAN_SUPPLY' "$SPEC_PATH"; then
    echo "plan-freshness: canonical spec '$SPEC_PATH' is still a neutral placeholder" >&2
    echo "plan-freshness: planning never runs against a placeholder specification" >&2
    exit 1
fi
[[ "$RECORDED_COMMIT" != UNPLANNED && "$RECORDED_BLOB" != UNPLANNED ]] || {
    echo "plan-freshness: plan is unplanned; start a fresh planning phase (python3 .factory/loop/campaign.py --root \"\$PWD\" run --campaign-id <id> --rounds <n> --branch <branch>)" >&2
    exit 1
}
if [[ "$PHASE" == planning ]]; then
    [[ "$STATUS" == active ]] || {
        echo "plan-freshness: planning status is '$STATUS', expected 'active'" >&2; exit 1;
    }
else
    [[ "$STATUS" == active || "$STATUS" == complete ]] || {
        echo "plan-freshness: plan status is '$STATUS', expected 'active' or 'complete'" >&2; exit 1;
    }
fi
"$PINNED_GIT" cat-file -e "$BASE_COMMIT^{commit}" 2>/dev/null || {
    echo "plan-freshness: invalid base commit '$BASE_COMMIT'" >&2
    exit 1
}
"$PINNED_GIT" merge-base --is-ancestor "$BASE_COMMIT" HEAD || {
    echo "plan-freshness: base commit '$BASE_COMMIT' is not an ancestor of HEAD" >&2
    exit 1
}
if [[ "$PHASE" == planning ]]; then
    # Legacy/deprecated marker authority: the expected planning base is the
    # frozen `scripts/ralph-plan.sh` marker (`FACTORY_PLANNING_BASE_COMMIT` or
    # `.factory-state/planning-base-commit`).  The fresh Python loop never
    # writes or reads this marker; it binds the planning base from Git and the
    # `factory-state/v1` file, so this comparison is dead on the new path.
    EXPECTED_BASE=${FACTORY_PLANNING_BASE_COMMIT:-}
    if [[ -z "$EXPECTED_BASE" ]]; then
        EXPECTED_BASE=$("$SCRIPT_DIR/factory-state-file.py" read planning-base-commit --missing-ok) || exit $?
    fi
    [[ -n "$EXPECTED_BASE" && "$BASE_COMMIT" == "$EXPECTED_BASE" ]] || {
        echo "plan-freshness: base_commit differs from the selected planning cycle base" >&2; exit 1;
    }
fi

# The clean-tree check above already guarantees the spec worktree is
# committed; this explicit committed-blob binding keeps the "spec changed
# after planning" diagnosis precise.
ACTUAL_COMMIT=$("$PINNED_GIT" log -1 --format=%H -- "$SPEC_PATH")
ACTUAL_BLOB=$("$PINNED_GIT" rev-parse "HEAD:$SPEC_PATH")

if [[ "$ACTUAL_COMMIT" != "$RECORDED_COMMIT" || "$ACTUAL_BLOB" != "$RECORDED_BLOB" ]]; then
    echo "plan-freshness: specification changed after planning" >&2
    echo "  recorded commit: $RECORDED_COMMIT" >&2
    echo "  current commit:  $ACTUAL_COMMIT" >&2
    echo "  recorded blob:   $RECORDED_BLOB" >&2
    echo "  current blob:    $ACTUAL_BLOB" >&2
    echo "Start a fresh planning phase before implementation (python3 .factory/loop/campaign.py --root \"\$PWD\" run ...)." >&2
    exit 1
fi

# -- harness freshness scope --------------------------------------------------
# The plan contract includes the hidden control-plane namespaces (`.factory/`,
# `.pi/`) and the policy/checker/receipt tools.  The clean-tree gate above
# already fails closed on any uncommitted tracked change or untracked
# non-ignored file anywhere in the repository (those namespaces are never
# gitignored), so a plan can never be declared fresh against a harness whose
# policy/checker/receipt tools differ from the committed tree.  The canonical
# policy authorities are additionally required to be real tracked files (never
# symlinks), so a substituted or absent plan contract fails closed.
for policy in .factory/campaign-receipt-policy.json .factory/requirement-policy.json \
              .factory/capability-contracts.json .factory/config.toml; do
    if [[ -L "$PROJECT_ROOT/$policy" ]]; then
        echo "plan-freshness: policy authority '$policy' is a symlink; the" >&2
        echo "plan-freshness: plan contract requires a real tracked file" >&2
        exit 1
    fi
    if [[ ! -f "$PROJECT_ROOT/$policy" ]]; then
        echo "plan-freshness: policy authority '$policy' is missing; the" >&2
        echo "plan-freshness: plan contract cannot certify its evidence tools" >&2
        exit 1
    fi
    if ! "$PINNED_GIT" ls-files --error-unmatch -- "$policy" >/dev/null 2>&1; then
        echo "plan-freshness: policy authority '$policy' is not tracked at HEAD" >&2
        exit 1
    fi
done

echo "plan-freshness: spec=$SPEC_PATH commit=${ACTUAL_COMMIT:0:12} blob=${ACTUAL_BLOB:0:12}"
echo "plan-freshness: clean tree; harness namespaces (.factory, .pi) and policy/checker/receipt tools fresh at HEAD"
