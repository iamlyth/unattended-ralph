#!/usr/bin/env bash
# check-spec-provided.sh — Hard-block planning until a real canonical
# specification is bound in `.factory/config.toml [project].spec`.
#
# This boilerplate cycle's canonical specification is the committed redesign
# contract `docs/FACTORY-LOOP-SPEC.md` (bound in `.factory/config.toml`
# `[project].spec` at commit `2d6a4fd`, blob `ca2334ab…`). `docs/SPEC.md`
# remains the adopting-product placeholder: it carries the marker
# `SPEC_PENDING_HUMAN_SUPPLY` and is never planned against. Planning (and
# every completion gate) refuses to run while the bound canonical spec is
# missing/unsafe or is still a neutral placeholder carrying that marker — an
# autonomous worker must never plan against a placeholder or invent the
# product contract. The gate stays path-neutral so an adopting repository can
# bind its own real `docs/SPEC.md` once a human supplies and commits it.
set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
PROJECT_ROOT=$(cd -- "$SCRIPT_DIR/.." && pwd)
cd -- "$PROJECT_ROOT"

SPEC_PATH=$(python3 - <<'PY'
import tomllib
with open('.factory/config.toml', 'rb') as stream:
    spec = tomllib.load(stream)['project']['spec']
if not isinstance(spec, str) or not spec:
    raise SystemExit('check-spec-provided: project.spec is not a path')
print(spec)
PY
)
[[ -n "$SPEC_PATH" && -f "$SPEC_PATH" && ! -L "$SPEC_PATH" ]] || {
    echo "check-spec-provided: canonical spec '$SPEC_PATH' is missing or unsafe" >&2
    exit 1
}
if grep -q 'SPEC_PENDING_HUMAN_SUPPLY' "$SPEC_PATH"; then
    echo "check-spec-provided: canonical spec '$SPEC_PATH' is still a neutral placeholder." >&2
    echo "check-spec-provided: in this boilerplate cycle '$SPEC_PATH' must be the committed redesign" >&2
    echo "check-spec-provided: spec docs/FACTORY-LOOP-SPEC.md; planning never runs against the" >&2
    echo "check-spec-provided: adopting-product placeholder docs/SPEC.md." >&2
    exit 1
fi
echo "check-spec-provided: canonical spec $SPEC_PATH is present"
