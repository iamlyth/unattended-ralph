#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
PROJECT_ROOT=$(cd -- "$SCRIPT_DIR/.." && pwd)
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
mkdir -p "$tmp/scripts" "$tmp/bin" "$tmp/docs" "$tmp/.factory" \
    "$tmp/.factory/artifacts" "$tmp/.factory/prompts" "$tmp/.ralph/agent" \
    "$tmp/.factory-state" "$tmp/.factory/loop"
cp "$PROJECT_ROOT/scripts/ralph-plan.sh" "$PROJECT_ROOT/scripts/ralph-supervision.sh" \
    "$PROJECT_ROOT/scripts/factory-lock.sh" "$PROJECT_ROOT/scripts/factory-lock-exec.py" \
    "$PROJECT_ROOT/scripts/factory_lock.py" "$PROJECT_ROOT/scripts/factory_state_io.py" \
    "$PROJECT_ROOT/scripts/factory-state-file.py" "$PROJECT_ROOT/scripts/ralph-event-boundary.py" \
    "$PROJECT_ROOT/scripts/ralph-final-state.py" "$PROJECT_ROOT/scripts/git-commit-guard.sh" \
    "$PROJECT_ROOT/scripts/install-git-commit-guard.sh" "$tmp/scripts/"
# Task 15/16: the deprecated ralph-plan launcher routes its freeze decision
# through the retained hidden authority (.factory/loop/migration.py freeze
# --guard), so a realistic deployment fixture carries the hidden loop modules
# exactly like the product tree; factory_state_io.py is already copied above.
cp "$PROJECT_ROOT/.factory/loop/migration.py" \
   "$PROJECT_ROOT/.factory/loop/gitutil.py" \
   "$PROJECT_ROOT/.factory/loop/plan_parser.py" \
   "$PROJECT_ROOT/.factory/loop/state.py" "$tmp/.factory/loop/"
chmod 700 "$tmp/.factory-state"

cat > "$tmp/.factory/config.toml" <<'EOF'
[project]
spec = "docs/SPEC.md"
EOF
printf '# Specification\n' > "$tmp/docs/SPEC.md"
printf 'fake planning prompt' > "$tmp/.factory/prompts/plan.md"
printf '# Initial handoff\n' > "$tmp/.ralph/agent/scratchpad.md"
printf '# Initial plan\n' > "$tmp/.factory/artifacts/implementation-plan.md"
cat > "$tmp/.gitignore" <<'EOF'
.factory-state/
.factory-lock
__pycache__/
.ralph/*
!.ralph/agent/
.ralph/agent/*
!.ralph/agent/scratchpad.md
EOF

cat > "$tmp/scripts/branch-guard.sh" <<'EOF'
#!/usr/bin/env bash
[[ $(git branch --show-current) == develop ]]
EOF
cat > "$tmp/scripts/check-factory-environment.py" <<'EOF'
#!/usr/bin/env python3
raise SystemExit(0)
EOF
cat > "$tmp/scripts/initialize-plan-cycle.py" <<'EOF'
#!/usr/bin/env python3
from pathlib import Path
Path('.factory/artifacts/implementation-plan.md').write_text('# Draft plan\n')
Path('.ralph/agent/scratchpad.md').write_text('# Planning handoff\n\n- Draft initialized.\n')
EOF
cat > "$tmp/scripts/ollama-usage-guard.sh" <<'EOF'
#!/usr/bin/env bash
exit 0
EOF
cat > "$tmp/scripts/final-gate.sh" <<'EOF'
#!/usr/bin/env bash
if [[ -e .factory-state/fake-gate-pass ]]; then
    exit 0
fi
echo 'implementation-plan: final audit must explicitly cover definition of done' >&2
exit 1
EOF
cat > "$tmp/scripts/git-commit-hook.sh" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
(( ${FAKE_FINALIZE_RC:-0} == 0 )) || exit "$FAKE_FINALIZE_RC"
if [[ " $* " == *' --final-handoff '* ]]; then
    ./scripts/ralph-final-state.py ensure-checkpoint planning "$(git rev-parse HEAD)" >/dev/null
fi
EOF
cat > "$tmp/scripts/check-plan-freshness.sh" <<'EOF'
#!/usr/bin/env bash
exit 0
EOF
cat > "$tmp/scripts/ralph-recover.sh" <<'EOF'
#!/usr/bin/env bash
printf 'recover %s\n' "$*" >> .factory-state/fake-calls
rm -f .ralph/loop.lock
exit 0
EOF
cat > "$tmp/bin/pi2" <<'EOF'
#!/usr/bin/env bash
exit 0
EOF
cat > "$tmp/bin/ralph" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
printf 'ralph %s\n' "$*" >> .factory-state/fake-calls
mkdir -p .ralph
printf '%s\n' '.ralph/events.jsonl' > .ralph/current-events
printf '%s\n' 'fake-planning-loop' > .ralph/current-loop-id
printf '%s\n' \
    '{"ts":"2026-08-14T00:00:00Z","iteration":0,"hat":"loop","topic":"factory.plan","triggered":"planner","payload":"fake planning prompt"}' \
    >> .ralph/events.jsonl
printf '%s\n' '{"ts":"2026-08-14T00:00:00Z","type":{"kind":"loop_started","prompt":"fake planning"}}' >> .ralph/history.jsonl
if [[ -n ${FAKE_NOOP_SUCCESS:-} ]]; then
    printf '%s\n' '{"ts":"2026-08-14T00:00:01Z","type":{"kind":"loop_completed","reason":"completed"}}' >> .ralph/history.jsonl
    printf '%s\n' '{"ts":"2026-08-14T00:00:01Z","iteration":1,"hat":"loop","topic":"iteration.summary","payload":"{\"cache_read_tokens\":0,\"cache_write_tokens\":0,\"context_pct\":0,\"context_tokens\":0,\"context_window\":0,\"cost_usd\":0,\"duration_ms\":0,\"input_tokens\":0,\"num_turns\":0,\"output_tokens\":0}"}' >> .ralph/events.jsonl
    exit 0
fi
if [[ ! -e .factory-state/fake-stale-seen ]]; then
    : > .factory-state/fake-stale-seen
    cat > .factory/artifacts/implementation-plan.md <<'PLAN'
# Plan

## Task 1: Incomplete final audit
- Status: pending
PLAN
    printf '# Planning handoff\n\n- Plan appears complete.\n' > .ralph/agent/scratchpad.md
    git add .factory/artifacts/implementation-plan.md .ralph/agent/scratchpad.md
    git commit -qm 'fake stale planning checkpoint'
    printf '%s\n' '{"ts":"2026-08-14T00:00:01Z","type":{"kind":"loop_completed","reason":"loop_stale"}}' >> .ralph/history.jsonl
    printf '%s\n' '{"ts":"2026-08-14T00:00:01Z","iteration":1,"hat":"loop","topic":"iteration.summary","payload":"{\"cache_read_tokens\":0,\"cache_write_tokens\":0,\"context_pct\":0,\"context_tokens\":0,\"context_window\":0,\"cost_usd\":0,\"duration_ms\":0,\"input_tokens\":0,\"num_turns\":0,\"output_tokens\":0}"}' >> .ralph/events.jsonl
    exit 1
fi
grep -q 'Supervisor recovery feedback' .ralph/agent/scratchpad.md
grep -q 'final-gate.sh --planning' .ralph/agent/scratchpad.md
cat > .factory/artifacts/implementation-plan.md <<'PLAN'
# Plan

## Task 1: Final documentation and specification audit
- Status: pending
- Acceptance criteria: execute the definition of done
PLAN
printf '# Planning handoff\n\n- Strict gate now passes.\n' > .ralph/agent/scratchpad.md
: > .factory-state/fake-gate-pass
git add .factory/artifacts/implementation-plan.md .ralph/agent/scratchpad.md
git commit -qm 'fake recovered planning checkpoint'
printf '%s\n' '{"ts":"2026-08-14T00:00:02Z","type":{"kind":"loop_completed","reason":"completed"}}' >> .ralph/history.jsonl
printf '%s\n' '{"ts":"2026-08-14T00:00:02Z","iteration":1,"hat":"loop","topic":"iteration.summary","payload":"{\"cache_read_tokens\":0,\"cache_write_tokens\":0,\"context_pct\":0,\"context_tokens\":0,\"context_window\":0,\"cost_usd\":0,\"duration_ms\":0,\"input_tokens\":0,\"num_turns\":0,\"output_tokens\":0}"}' >> .ralph/events.jsonl
exit 0
EOF
chmod +x "$tmp/scripts/"* "$tmp/bin/"*

git -C "$tmp" init -q -b develop
git -C "$tmp" config user.name test
git -C "$tmp" config user.email test@example.invalid
git -C "$tmp" add .
git -C "$tmp" commit -qm initial

(
    cd "$tmp"
    PATH="$tmp/bin:$PATH" RALPH_BIN="$tmp/bin/ralph" ./scripts/ralph-plan.sh --no-tui >/dev/null
)
grep -q '^recover --mode planning --prepare-only$' "$tmp/.factory-state/fake-calls"
grep -q '^ralph .*--continue.*--no-tui' "$tmp/.factory-state/fake-calls"
[[ $(grep -c '^ralph ' "$tmp/.factory-state/fake-calls") -eq 2 ]]
[[ -z $(git -C "$tmp" status --porcelain --untracked-files=normal) ]]
if grep -q 'factory-stale-recovery' "$tmp/.ralph/agent/scratchpad.md"; then
    echo 'test-ralph-stale-recovery: supervisor feedback survived the recovered handoff' >&2
    exit 1
fi

# A post-gate finalization failure is terminal and preserves its exact status;
# it must never be reclassified as another stale recovery.
set +e
(
    cd "$tmp"
    PATH="$tmp/bin:$PATH" RALPH_BIN="$tmp/bin/ralph" FAKE_NOOP_SUCCESS=1 \
        FAKE_FINALIZE_RC=42 ./scripts/ralph-plan.sh --no-tui >/dev/null 2>&1
)
finalization_rc=$?
set -e
[[ $finalization_rc -eq 42 ]] || {
    echo "test-ralph-stale-recovery: finalization status was masked: $finalization_rc" >&2
    exit 1
}

echo 'test: Ralph stale-loop launcher recovery checks passed'
