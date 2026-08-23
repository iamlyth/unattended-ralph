#!/usr/bin/env bash
# test-factory-supervision.sh — hidden-namespace Task 6 supervision suite.
#
# This is the *hidden actual test path* for Task 6 (fresh-context execution,
# invocation contract, and supervision): the specification (HIDE-01, §3) keeps
# harness-only tests out of the adopting product's visible `tests/` tree, so
# there is deliberately no visible `tests/` runner for Task 6.  It drives
# `.factory/loop/launch.py` through its only exposed entry points:
#
#   * `python -m factory.loop.launch` (the module entrypoint, reachable
#     through the external-prefix alias: `factory` on `PYTHONPATH` resolving
#     to the canonical `.factory/` directory — no visible bare `scripts/`
#     wrapper);
#   * the real `scripts/pi2-secure-exec.py` wrapper (invoked, never
#     reimplemented) with a synthetic committed model backend.
#
# The suite re-derives every authoritative byte from the committed fixture
# blobs (F5), proves wrapper/backend bound-commit tamper is rejected before
# exec (F2), and requires the machine-readable launch result to conform to
# the committed `factory-launch-result/v1` schema.
set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
ROOT=$(cd -- "$SCRIPT_DIR/../.." && pwd)
cd -- "$ROOT"

PY=${PYTHON:-python3}
GIT=${GIT:-git}

fail() {
    echo "test-factory-supervision: $*" >&2
    exit 1
}

assert_rc() {
    local expected=$1 actual=$2 label=$3
    [[ $actual -eq $expected ]] || {
        echo "test-factory-supervision: $label: exit $actual, expected $expected" >&2
        exit 1
    }
}

tmp=$(mktemp -d "${TMPDIR:-/tmp}/factory-supervision.XXXXXX")
trap 'rm -rf "$tmp"' EXIT

# -- 1. The hidden Python suite runs warning-free under -W error --------------
echo "test-factory-supervision: running .factory/tests/test-factory-launch.py"
"$PY" -W error::ResourceWarning .factory/tests/test-factory-launch.py \
    >"$tmp/suite.log" 2>&1 || {
    echo "test-factory-supervision: Python suite failed:" >&2
    tail -40 "$tmp/suite.log" >&2
    exit 1
}
if grep -qiE 'ResourceWarning|unclosed file' "$tmp/suite.log"; then
    echo "test-factory-supervision: ResourceWarning leaked from the suite:" >&2
    grep -iE 'ResourceWarning|unclosed file' "$tmp/suite.log" | head -20 >&2
    exit 1
fi

# -- 2. The module entrypoint is reachable (external-prefix alias) -----------
mkdir -p "$tmp/alias"
ln -s "$ROOT/.factory" "$tmp/alias/factory"
if ! PYTHONPATH="$tmp/alias" "$PY" -m factory.loop.launch --help \
    >"$tmp/help.out" 2>&1; then
    echo "test-factory-supervision: python -m factory.loop.launch --help failed:" >&2
    tail -20 "$tmp/help.out" >&2
    exit 1
fi
grep -q 'factory-launch' "$tmp/help.out" \
    || fail "the module entrypoint help does not identify factory-launch"
if find scripts -maxdepth 1 -name '*launch*' | grep -q .; then
    fail "a visible scripts/ wrapper exposes the launcher (HIDE-01)"
fi

# -- 3. End-to-end launch through the committed fixture repo ------------------
repo="$tmp/repo"
mkdir -p "$repo/scripts"
mkdir -p "$repo/.factory/loop"
mkdir -p "$repo/.factory/schemas"
cp scripts/pi2-secure-exec.py "$repo/scripts/"
# Task 11: every fixture repo commits the exact credential guard so the
# launch authority can verify the guard source before any child output
# channel is redacted.
cp scripts/credential-guard.py "$repo/scripts/"
cp .factory/loop/confine_launcher.py "$repo/.factory/loop/"
cp .factory/schemas/factory-confinement-v1.schema.json "$repo/.factory/schemas/"
cp .factory/tests/fixtures/plan-valid-base.md "$repo/plan.md"
printf 'spec\n' > "$repo/spec.md"
printf 'policy\n' > "$repo/policy.md"
printf 'role\n' > "$repo/role.md"
cat > "$repo/backend.py" <<'PY'
#!/usr/bin/env python3
import json, os, sys
sys.stdin.buffer.read()
print("hello from fixture backend")
print("fixture stderr", file=sys.stderr)
PY
chmod 700 "$repo/backend.py"
(
    cd "$repo"
    "$GIT" init -q
    "$GIT" config user.email factory@test
    "$GIT" config user.name factory
    "$GIT" add -A
    "$GIT" commit -qm init
)
head=$("$GIT" -C "$repo" rev-parse HEAD)
plan_digest=$(sha256sum "$repo/plan.md" | cut -d' ' -f1)
printf '{"mode":"record"}\n' > "$repo/behavior.json"

run_launch() {
    PYTHONPATH="$tmp/alias" "$PY" -m factory.loop.launch launch \
        --root "$repo" \
        --role developer \
        --model synthetic-model \
        --provider synthetic \
        --backend "$repo/backend.py" \
        --role-prompt "$repo/role.md" \
        --role-prompt-digest "$(printf 'role\n' | sha256sum | cut -d' ' -f1)" \
        --prompt-set-digest "$(printf 'set' | sha256sum | cut -d' ' -f1)" \
        --policy "$repo/policy.md" \
        --policy-digest "$(printf 'policy\n' | sha256sum | cut -d' ' -f1)" \
        --spec "$repo/spec.md" \
        --spec-digest "$(printf 'spec\n' | sha256sum | cut -d' ' -f1)" \
        --plan "$repo/plan.md" \
        --plan-digest "$plan_digest" \
        --bound-commit "$head" \
        --allowed-tools read,bash \
        --runtime-limit 30 \
        --inactivity-limit 20 \
        --task-id 1 \
        --task-excerpt-digest "$(
            PYTHONPATH="$tmp/alias" "$PY" -m factory.loop.launch \
                excerpt --plan "$repo/plan.md" --task-id 1 |
                "$PY" -c 'import json,sys; print(json.load(sys.stdin)["digest"])'
        )"
}

run_launch > "$tmp/launch.json" 2> "$tmp/launch.err" \
    || fail "the launch CLI did not complete: $(tail -5 "$tmp/launch.err")"
"$PY" - "$tmp/launch.json" <<'PY'
import json, sys
payload = json.load(open(sys.argv[1], encoding="utf-8"))
assert payload["outcome"] == "completed", payload
assert payload["returncode"] == 0, payload
assert payload["role"] == "developer", payload
required = {"role","model","provider","outcome","returncode","signal",
            "reason","terminated_by","elapsed","stdout","stderr",
            "descendants_snapshot","live_descendants","invariants"}
assert required <= set(payload), set(payload)
for stream in ("stdout", "stderr"):
    assert set(payload[stream]) == {"bytes", "digest", "tail", "truncated"}
PY
echo "test-factory-supervision: CLI launch result schema fields verified"

# -- 4. Tampered workspace wrapper/backend vs the bound commit is rejected ---
# A working-tree mutation of the committed backend must fail closed before
# exec (F2); the CLI re-derives the exact bound-commit blob itself (F5).
printf 'tampered\n' >> "$repo/backend.py"
if run_launch >"$tmp/tamper.out" 2>"$tmp/tamper.err"; then
    fail "tampered backend was not rejected before exec"
fi
grep -qi 'committed blob' "$tmp/tamper.err" \
    || fail "tamper rejection did not cite the committed-blob boundary"

# The tampered wrapper (the file that must run from its exact bound-commit
# bytes) is rejected the same way, before any backend read or spawn.
printf '\n# tampered\n' >> "$repo/scripts/pi2-secure-exec.py"
if run_launch >"$tmp/wraptamper.out" 2>"$tmp/wraptamper.err"; then
    fail "tampered wrapper was not rejected before exec"
fi
grep -qi 'wrapper' "$tmp/wraptamper.err" \
    || fail "tamper rejection did not cite the secure wrapper"

echo "test-factory-supervision: all checks passed"
