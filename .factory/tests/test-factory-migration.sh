#!/usr/bin/env bash
# test-factory-migration.sh — hidden-namespace Task 15 migration/deprecation
# shell driver.
#
# The specification (HIDE-01, §3) keeps harness-only tests out of the adopting
# product's visible `tests/` tree, so the migration suite lives under the
# hidden `.factory/tests/` namespace like the Task 13 footprint series.  This
# driver:
#
#   1. runs `.factory/tests/test-factory-migration.py` warning-free under
#      `-W error::ResourceWarning` (read isolation, exact preservation, no
#      runtime imports, single-authority migrate, freeze surface,
#      context-summary unwiring, legacy-store metadata-only);
#   2. verifies the tracked freeze marker exists as a regular file and every
#      frozen legacy launcher both keeps working help (`--help` exits 0 while
#      frozen) and refuses a new launch (exit 2 with the frozen message);
#   3. verifies legacy recovery stays available (``ralph-recover.sh --help``
#      works and prints the deprecation note) and the deprecated visible
#      context-summary generator/verifier/suite fail closed as marked,
#      optional legacy entry points that no new-path control step invokes;
#   4. verifies the stale ``.factory/artifacts/context-summary.md`` mirror is
#      absent and that ``verify-boilerplate.sh``/``final-gate.sh``/
#      ``git-commit-hook.sh``/``ralph-run.sh`` never invoke the deprecated
#      authorities;
#   5. verifies the shell credential guard resolves the external operator
#      store and only *detects* the legacy workspace store with metadata-only
#      checks (never sources, reads, or overwrites it);
#   6. proves the exact override escape, the missing-marker legacy semantics,
#      and the all-six fail-closed refusal on unsafe markers (symlink/FIFO/
#      socket/device) in private fixtures (never touching the live
#      repository);
#   7. asserts the docs name the flat report path and the exact override.
#
# The driver never writes to the live repository, never reads or moves the
# real ``.ollama-usage-env``, and never touches ``.ralph/``.
set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
ROOT=$(cd -- "$SCRIPT_DIR/../.." && pwd)
cd -- "$ROOT"

PY=${PYTHON:-python3}

fail() {
    echo "test-factory-migration: $*" >&2
    exit 1
}

# -- Controlled shell context -----------------------------------------------
# Strip the same Git redirector families the pinned boundary strips, so a
# caller environment cannot steer the fixture commands at a different object
# store, index, work tree, or config set.
unset GIT_DIR GIT_WORK_TREE GIT_INDEX_FILE GIT_OBJECT_DIRECTORY \
    GIT_ALTERNATE_OBJECT_DIRECTORIES GIT_COMMON_DIR GIT_NAMESPACE \
    GIT_CEILING_DIRECTORIES GIT_SSH GIT_SSH_COMMAND GIT_ASKPASS \
    GIT_TERMINAL_PROMPT GIT_EXEC_PATH GIT_TEMPLATE_DIR \
    GIT_CONFIG_PARAMETERS GIT_CONFIG GIT_CONFIG_SYSTEM GIT_CONFIG_GLOBAL \
    GIT_CONFIG_NOSYSTEM GIT_CONFIG_COUNT 2>/dev/null || true
unset "${!GIT_CONFIG_KEY_@}" "${!GIT_CONFIG_VALUE_@}" 2>/dev/null || true
# The operator store tests must never consult a real ambient store.
unset OLLAMA_USAGE_ENV_FILE OLLAMA_COOKIE 2>/dev/null || true

tmp=$(mktemp -d "${TMPDIR:-/tmp}/factory-migration.XXXXXX")
cleanup() {
    rm -rf -- "$tmp"
}
# shellcheck disable=SC2154 # rc is assigned inside the trap from $?
trap 'rc=$?; cleanup; trap - EXIT INT TERM; exit "$rc"' EXIT INT TERM

# -- 1. The hidden Python suite runs warning-free ---------------------------
echo "test-factory-migration: running .factory/tests/test-factory-migration.py"
"$PY" -W error::ResourceWarning .factory/tests/test-factory-migration.py \
    >"$tmp/suite.log" 2>&1 || {
    echo "test-factory-migration: Python suite failed:" >&2
    tail -60 "$tmp/suite.log" >&2
    exit 1
}
if grep -qiE 'ResourceWarning|unclosed file' "$tmp/suite.log"; then
    echo "test-factory-migration: ResourceWarning leaked from the suite:" >&2
    grep -iE 'ResourceWarning|unclosed file' "$tmp/suite.log" | head -20 >&2
    exit 1
fi

# -- 2. Freeze marker and launcher freeze/help behavior ---------------------
[[ -f .factory/ralph-freeze && ! -L .factory/ralph-freeze ]] \
    || fail "the tracked .factory/ralph-freeze marker must be a regular file"
git ls-files --error-unmatch .factory/ralph-freeze >/dev/null 2>&1 \
    || fail "the freeze marker must be tracked"

# Help behavior is preserved while frozen: every frozen launcher prints its
# usage and exits 0 on -h/--help, and a benign new-launch invocation is
# refused with exit 2 and the frozen message.
for launcher in \
    scripts/ralph-campaign.sh scripts/ralph-plan.sh scripts/ralph-run.sh \
    scripts/ralph-audit.sh scripts/ralph-maintenance-plan.sh \
    scripts/ralph-maintenance-run.sh; do
    [[ -f "$launcher" ]] || fail "missing frozen launcher $launcher"
    grep -q '.factory/ralph-freeze' "$launcher" \
        || fail "$launcher does not implement the freeze gate"
    grep -q 'FACTORY_RALPH_FREEZE_OVERRIDE' "$launcher" \
        || fail "$launcher does not document the override escape"
    grep -q 'ralph.*frozen: the legacy Ralph control plane is deprecated' \
        "$launcher" || fail "$launcher lacks the frozen deprecation message"
    if ! "$launcher" --help >"$tmp/help.out" 2>"$tmp/help.err"; then
        fail "$launcher --help must stay available while frozen"
    fi
    grep -qi 'usage' "$tmp/help.out" || grep -qi 'usage' "$tmp/help.err" \
        || fail "$launcher --help must print usage"
done

check_frozen_refusal() {
    local launcher=$1; shift
    if "$launcher" "$@" >"$tmp/refuse.out" 2>"$tmp/refuse.err"; then
        fail "$launcher $* must refuse a new launch while frozen (exit 2)"
    fi
    grep -q 'frozen' "$tmp/refuse.err" \
        || fail "$launcher $* refusal must name the freeze: $(cat "$tmp/refuse.err")"
}
check_frozen_refusal scripts/ralph-campaign.sh --rounds 1
check_frozen_refusal scripts/ralph-plan.sh
check_frozen_refusal scripts/ralph-run.sh
check_frozen_refusal scripts/ralph-audit.sh
check_frozen_refusal scripts/ralph-maintenance-plan.sh BUG-1
check_frozen_refusal scripts/ralph-maintenance-run.sh
echo "test-factory-migration: all new legacy launchers are frozen; help preserved"

# -- 3. Legacy recovery exception and deprecated entry points ---------------
# Recovery of an already in-flight legacy cycle is NOT a new launch: it stays
# usable and prints the deprecation note.
if ! ./scripts/ralph-recover.sh --help >"$tmp/recover.out" 2>"$tmp/recover.err"; then
    fail "ralph-recover.sh --help must stay usable during the freeze"
fi
grep -q 'ralph-recover: note: the legacy Ralph control plane is deprecated' \
    "$tmp/recover.err" \
    || fail "ralph-recover.sh must print the deprecation note"

# The deprecated visible context-summary generator/verifier fail closed and
# the deprecated suite marks itself; none of them is a new-path dependency.
for script in scripts/ralph-context-summary.py scripts/check-context-summary.py; do
    if "$PY" "$script" >"$tmp/dep.out" 2>"$tmp/dep.err"; then
        fail "$script must fail closed (exit nonzero)"
    fi
    grep -q 'DEPRECATED' "$tmp/dep.err" \
        || fail "$script must mark itself deprecated"
    grep -q 'Task 15 migration' "$tmp/dep.err" \
        || fail "$script must name the Task 15 migration"
done
if ! ./tests/test-context-summary.sh >"$tmp/legacy.out" 2>"$tmp/legacy.err"; then
    fail "the marked legacy context-summary suite must remain an optional no-op"
fi
grep -q 'DEPRECATED' "$tmp/legacy.err" \
    || fail "test-context-summary.sh must mark itself deprecated"

# -- 4. no context summary absent and unwired from the new path -----------------
[[ ! -e .factory/artifacts/context-summary.md ]] \
    || fail "the stale context-summary mirror must not exist in the tree"
# The actual new-path control steps never invoke the deprecated authority.
# verify-boilerplate.sh names the deprecated paths only inside its own
# rejection assertions (it is the enforcement, not a caller), so it is
# checked separately for that enforcement.
for file in scripts/final-gate.sh scripts/git-commit-hook.sh scripts/ralph-run.sh; do
    grep -q 'check-context-summary.py\|ralph-context-summary.py' "$file" \
        && fail "$file still invokes a deprecated context-summary authority"
    grep -q 'test-context-summary.sh' "$file" \
        && fail "$file still invokes the deprecated context-summary suite"
done
grep -q 'still wires the deprecated' scripts/verify-boilerplate.sh \
    || fail "verify-boilerplate.sh must enforce the context-summary unwiring"
if "$PY" - <<'PY'
import json, pathlib
gates = json.load(open('.factory/verifier-acceptance.json', encoding='utf-8'))['gates']
assert all(g['name'] != 'test-context-summary.sh' for g in gates), gates
PY
then
    :
else
    fail "the verifier gate list must not run the deprecated context-summary suite"
fi
echo "test-factory-migration: context-summary authority absent from the new path"

# -- 5. Legacy workspace credential store is metadata-only ------------------
# The retained shell guard resolves the external operator store and only
# *detects* the legacy workspace store: it never sources/reads it, and the
# legacy bytes are untouched.
grep -q 'OLLAMA_USAGE_ENV_FILE\|XDG_CONFIG_HOME/unattended-ralph' \
    scripts/ollama-usage-guard.sh \
    || fail "the shell guard must resolve the external operator store"
grep -q 'DEPRECATED legacy store' scripts/ollama-usage-guard.sh \
    || fail "the shell guard must warn on the legacy store"
# The legacy path appears in the guard only inside metadata-only presence
# checks (`-e`/`-L`), never as a source/read authority.
if grep -nE 'source[[:space:]]+[$]PROJECT_ROOT/\.ollama-usage-env|cat[[:space:]]+[$]PROJECT_ROOT/\.ollama-usage-env' \
    scripts/ollama-usage-guard.sh scripts/update-ollama-cookies.sh; then
    fail "a retained shell script still sources/reads the legacy credential store"
fi

# Drive the interactive cookie updater against a private fixture: the legacy
# workspace store (with synthetic secret bytes) is only warned about, the new
# cookie lands in the external operator store, and the legacy file is
# byte-for-byte untouched.
legacy_fixture="$tmp/legacy-repo"
mkdir -p "$legacy_fixture"
printf 'export __Secure_session=LEGACY-SECRET\n' > "$legacy_fixture/.ollama-usage-env"
external_store="$tmp/operator-store/ollama-usage-env"
mkdir -p "$tmp/operator-store"
legacy_before=$(sha256sum "$legacy_fixture/.ollama-usage-env" | awk '{print $1}')
if ! (cd "$legacy_fixture" && \
      printf 'sess-fixture\naid-fixture\ncf-fixture\n\n\n\n' | \
      OLLAMA_USAGE_ENV_FILE="$external_store" \
      HOME="$tmp/home" XDG_CONFIG_HOME="$tmp/xdg" \
      bash "$ROOT/scripts/update-ollama-cookies.sh" \
      >"$tmp/cookies.out" 2>"$tmp/cookies.err"); then
    fail "update-ollama-cookies.sh must complete against the external store: $(tail -3 "$tmp/cookies.err")"
fi
grep -q 'DEPRECATED legacy store' "$tmp/cookies.err" \
    || fail "update-ollama-cookies.sh must warn on the legacy store"
grep -q "Saved owner-only credentials to $external_store" "$tmp/cookies.out" \
    || fail "update-ollama-cookies.sh must save to the external operator store"
[[ "$(sha256sum "$legacy_fixture/.ollama-usage-env" | awk '{print $1}')" == "$legacy_before" ]] \
    || fail "the legacy store bytes must never be modified"
grep -q 'LEGACY-SECRET' "$external_store" \
    && fail "a legacy credential byte leaked into the external store"
echo "test-factory-migration: legacy store detected metadata-only; external store honored"

# -- 6. Freeze override, missing-marker, and unsafe-marker guards ------------
# A fixture copy of a launcher proves the operator-only *exact* override
# bypasses the freeze (the launch then proceeds to a deterministic later
# failure), a missing marker preserves the legacy not-frozen semantics, and a
# present but unsafe marker fails closed instead of silently unfreezing.
# Task 16: the launcher routes the freeze decision through the retained
# hidden authority, so every freeze fixture provisions the committed loop
# modules (and the established state I/O authority the loop loads by path)
# exactly like a real deployment would.
provision_loop_authority() {
    local fixture="$1"
    mkdir -p "$fixture/.factory/loop" "$fixture/scripts"
    for module in migration gitutil plan_parser state; do
        cp "$ROOT/.factory/loop/$module.py" "$fixture/.factory/loop/$module.py"
    done
    cp "$ROOT/scripts/factory_state_io.py" "$fixture/scripts/factory_state_io.py"
}

fixture="$tmp/freeze-fixture"
mkdir -p "$fixture/scripts" "$fixture/.factory"
cp "$ROOT/scripts/ralph-plan.sh" "$fixture/scripts/ralph-plan.sh"
chmod +x "$fixture/scripts/ralph-plan.sh"
provision_loop_authority "$fixture"
printf '# frozen fixture\n' > "$fixture/.factory/ralph-freeze"

# A regular tracked marker freezes the launch (fail closed with the message).
if "$fixture/scripts/ralph-plan.sh" >"$tmp/f1.out" 2>"$tmp/f1.err"; then
    fail "frozen fixture launcher must refuse"
fi
grep -q 'frozen' "$tmp/f1.err" \
    || fail "fixture launcher refusal must name the freeze"

# Only the exact override value 1 bypasses; every other value stays frozen.
for bad_override in 0 2 yes true; do
    if FACTORY_RALPH_FREEZE_OVERRIDE="$bad_override" "$fixture/scripts/ralph-plan.sh" \
        >"$tmp/fb.out" 2>"$tmp/fb.err"; then
        fail "FACTORY_RALPH_FREEZE_OVERRIDE=$bad_override must not bypass the freeze"
    fi
    grep -q 'frozen' "$tmp/fb.err" \
        || fail "FACTORY_RALPH_FREEZE_OVERRIDE=$bad_override must stay frozen"
done

if FACTORY_RALPH_FREEZE_OVERRIDE=1 "$fixture/scripts/ralph-plan.sh" \
    >"$tmp/f2.out" 2>"$tmp/f2.err"; then
    fail "override launcher must still stop deterministically (missing lock helper)"
fi
grep -q 'frozen' "$tmp/f2.err" \
    && fail "FACTORY_RALPH_FREEZE_OVERRIDE=1 must bypass the freeze message"
# The launcher proceeded past the freeze gate and failed at the next step.
grep -q 'factory-lock.sh' "$tmp/f2.err" \
    || fail "override launcher must proceed past the freeze gate"

# A missing marker preserves the legacy semantics: the launch is not frozen
# and proceeds to its next deterministic failure (the missing lock helper).
rm -f "$fixture/.factory/ralph-freeze"
if "$fixture/scripts/ralph-plan.sh" >"$tmp/f0.out" 2>"$tmp/f0.err"; then
    fail "marker-less launcher must still stop deterministically (missing lock helper)"
fi
grep -q 'frozen' "$tmp/f0.err" \
    && fail "a missing freeze marker must never freeze"
grep -q 'factory-lock.sh' "$tmp/f0.err" \
    || fail "marker-less launcher must proceed past the absent freeze gate"
echo "test-factory-migration: exact override, missing-marker, and regular-marker semantics verified"

# -- 6b. All six frozen launchers fail closed on unsafe markers ----------------
# A present but non-regular marker (FIFO/socket/symlink, device when the
# filesystem permits) must never silently unfreeze a launch: every frozen
# launcher refuses and names the freeze without reaching the lock helper.
fixture="$tmp/freeze-all-fixture"
mkdir -p "$fixture/scripts" "$fixture/.factory"
provision_loop_authority "$fixture"
for launcher in scripts/ralph-campaign.sh scripts/ralph-plan.sh \
    scripts/ralph-run.sh scripts/ralph-audit.sh \
    scripts/ralph-maintenance-plan.sh scripts/ralph-maintenance-run.sh; do
    cp "$ROOT/$launcher" "$fixture/$launcher"
    chmod +x "$fixture/$launcher"
done

check_all_six_refuse() {
    local label="$1"
    # shellcheck disable=SC2086
    for entry in \
        "scripts/ralph-campaign.sh:--rounds 1" \
        "scripts/ralph-plan.sh:" \
        "scripts/ralph-run.sh:" \
        "scripts/ralph-audit.sh:" \
        "scripts/ralph-maintenance-plan.sh:BUG-1" \
        "scripts/ralph-maintenance-run.sh:"; do
        local script=${entry%%:*} args=${entry#*:}
        if "$fixture/$script" $args >"$tmp/u.out" 2>"$tmp/u.err"; then
            fail "$label: $script must refuse while the marker is unsafe"
        fi
        grep -q 'frozen' "$tmp/u.err" \
            || fail "$label: $script refusal must name the freeze"
        ! grep -q 'factory-lock.sh' "$tmp/u.err" \
            || fail "$label: $script must stop at the freeze gate, not proceed"
    done
}

mkfifo "$fixture/.factory/ralph-freeze"
check_all_six_refuse "fifo-marker"

rm -f "$fixture/.factory/ralph-freeze"
ln -s "../ralph-freeze-target" "$fixture/.factory/ralph-freeze"
printf 'target\n' > "$fixture/.factory/ralph-freeze-target"
check_all_six_refuse "symlink-marker"

rm -f "$fixture/.factory/ralph-freeze"
python3 - "$fixture/.factory/ralph-freeze" <<'PY' || fail "cannot create the socket marker"
import socket
import sys
sock = socket.socket(socket.AF_UNIX)
sock.bind(sys.argv[1])
PY
check_all_six_refuse "socket-marker"

rm -f "$fixture/.factory/ralph-freeze"
if mknod "$fixture/.factory/ralph-freeze" c 1 3 2>/dev/null; then
    check_all_six_refuse "device-marker"
fi
echo "test-factory-migration: all six launchers fail closed on unsafe markers"

# -- 6c. Docs name the flat report path and the exact recovery override ------
grep -q '\.factory-state/migration\.json' "$ROOT/docs/OPERATIONS.md" \
    || fail "docs must name the flat report path .factory-state/migration.json"
if grep -q '\.factory-state/migration/' "$ROOT/docs/OPERATIONS.md"; then
    fail "docs must not name a .factory-state/migration/ subdirectory (flat report only)"
fi
grep -q 'FACTORY_RALPH_FREEZE_OVERRIDE=1' "$ROOT/docs/OPERATIONS.md" \
    || fail "docs must document the exact recovery override FACTORY_RALPH_FREEZE_OVERRIDE=1"
echo "test-factory-migration: docs name the flat report and the exact override"

echo "test-factory-migration: all checks passed"
