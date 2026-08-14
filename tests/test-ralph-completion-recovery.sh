#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
PROJECT_ROOT=$(cd -- "$SCRIPT_DIR/.." && pwd)
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
mkdir -p "$tmp/scripts" "$tmp/.factory-state" "$tmp/.ralph/agent"
cp "$PROJECT_ROOT/scripts/ralph-completion-gate.sh" "$tmp/scripts/"
cp "$PROJECT_ROOT/scripts/ralph-supervision.sh" "$tmp/scripts/"
cat > "$tmp/scripts/final-gate.sh" <<'EOF'
#!/usr/bin/env bash
printf '%s\n' "$*" >> "${FAKE_GATE_LOG:?}"
exit "${FAKE_GATE_RC:-0}"
EOF
chmod +x "$tmp/scripts/final-gate.sh" "$tmp/scripts/ralph-completion-gate.sh"

cd -- "$tmp"
export RALPH_COMPLETION_REJECTION_MARKER="$tmp/.factory-state/completion-rejected.json"
export FAKE_GATE_LOG="$tmp/gate.log"
# shellcheck source=scripts/ralph-supervision.sh
source "$tmp/scripts/ralph-supervision.sh"
payload=$(printf '{"schema_version":1,"phase":"pre","event":"loop.complete","phase_event":"pre.loop.complete","loop":{"workspace":"%s","id":"test-loop"},"iteration":{"current":1}}' "$tmp")

export FAKE_GATE_RC=1
ralph_supervision_begin implementation
set +e
printf '%s' "$payload" | "$tmp/scripts/ralph-completion-gate.sh" implementation >/dev/null 2>&1
gate_rc=$?
set -e
[[ $gate_rc -eq 1 && -f "$RALPH_COMPLETION_REJECTION_MARKER" ]] || {
    echo 'test-completion-recovery: rejected gate did not create a marker' >&2; exit 1;
}
consumed_loop=$(ralph_supervision_consume_rejection implementation "$tmp")
[[ "$consumed_loop" == test-loop ]] || {
    echo 'test-completion-recovery: consumed marker returned the wrong loop ID' >&2; exit 1;
}
if ralph_supervision_consume_rejection implementation "$tmp" >/dev/null 2>&1; then
    echo 'test-completion-recovery: rejection marker was reusable' >&2
    exit 1
fi
export FACTORY_RALPH_MAX_COMPLETION_RECOVERIES=1
RALPH_SUPERVISION_COMPLETION_RECOVERIES=0
ralph_supervision_allow_completion_recovery >/dev/null
if ralph_supervision_allow_completion_recovery >/dev/null 2>&1; then
    echo 'test-completion-recovery: completion recovery ceiling was not enforced' >&2
    exit 1
fi
unset FACTORY_RALPH_MAX_COMPLETION_RECOVERIES
RALPH_SUPERVISION_COMPLETION_RECOVERIES=0

ralph_supervision_begin implementation
printf '%s' "$payload" | "$tmp/scripts/ralph-completion-gate.sh" implementation >/dev/null 2>&1 || true
FACTORY_RALPH_ATTEMPT_ID=$(printf '0%.0s' {1..64})
export FACTORY_RALPH_ATTEMPT_ID
if ralph_supervision_consume_rejection implementation "$tmp" >/dev/null 2>&1; then
    echo 'test-completion-recovery: stale attempt marker was accepted' >&2
    exit 1
fi
ralph_supervision_begin implementation
[[ ! -e "$RALPH_COMPLETION_REJECTION_MARKER" ]] || {
    echo 'test-completion-recovery: stale marker was not cleared before launch' >&2; exit 1;
}

printf '%s' "$payload" | "$tmp/scripts/ralph-completion-gate.sh" implementation >/dev/null 2>&1 || true
if ralph_supervision_consume_rejection planning "$tmp" >/dev/null 2>&1; then
    echo 'test-completion-recovery: wrong lifecycle mode was accepted' >&2
    exit 1
fi
printf '{broken\n' > "$RALPH_COMPLETION_REJECTION_MARKER"
if ralph_supervision_consume_rejection implementation "$tmp" >/dev/null 2>&1; then
    echo 'test-completion-recovery: malformed marker was accepted' >&2
    exit 1
fi
rm -f "$RALPH_COMPLETION_REJECTION_MARKER"
ln -s "$tmp/elsewhere" "$RALPH_COMPLETION_REJECTION_MARKER"
if ralph_supervision_begin implementation >/dev/null 2>&1; then
    echo 'test-completion-recovery: symlink marker was accepted' >&2
    exit 1
fi
rm -f "$RALPH_COMPLETION_REJECTION_MARKER"
mkdir -p "$tmp/external-state"
ln -s "$tmp/external-state" "$tmp/symlink-state"
safe_marker=$RALPH_COMPLETION_REJECTION_MARKER
export RALPH_COMPLETION_REJECTION_MARKER="$tmp/symlink-state/rejected.json"
if ralph_supervision_begin implementation >/dev/null 2>&1; then
    echo 'test-completion-recovery: symlinked marker parent was accepted by supervision' >&2
    exit 1
fi
FACTORY_RALPH_ATTEMPT_ID=$(printf 'a%.0s' {1..64})
export FACTORY_RALPH_ATTEMPT_ID
if printf '%s' "$payload" | "$tmp/scripts/ralph-completion-gate.sh" implementation >/dev/null 2>&1; then
    echo 'test-completion-recovery: symlinked marker parent was accepted by completion gate' >&2
    exit 1
fi
export RALPH_COMPLETION_REJECTION_MARKER=$safe_marker

export FAKE_GATE_RC=0
ralph_supervision_begin implementation
printf '%s' "$payload" | "$tmp/scripts/ralph-completion-gate.sh" implementation >/dev/null
[[ ! -e "$RALPH_COMPLETION_REJECTION_MARKER" ]] || {
    echo 'test-completion-recovery: successful gate left a marker' >&2; exit 1;
}
if ralph_supervision_consume_rejection implementation "$tmp" >/dev/null 2>&1; then
    echo 'test-completion-recovery: arbitrary failure without marker was recoverable' >&2
    exit 1
fi

export FAKE_GATE_RC=1
ralph_supervision_begin implementation
set +e
printf '{"loop":{}}' | "$tmp/scripts/ralph-completion-gate.sh" implementation >/dev/null 2>&1
payload_rc=$?
set -e
[[ $payload_rc -ne 0 && ! -e "$RALPH_COMPLETION_REJECTION_MARKER" ]] || {
    echo 'test-completion-recovery: malformed hook payload created a marker' >&2; exit 1;
}
set +e
printf '%s' "${payload/\"pre.loop.complete\"/\"post.iteration.start\"}" | \
    "$tmp/scripts/ralph-completion-gate.sh" implementation >/dev/null 2>&1
phase_rc=$?
set -e
[[ $phase_rc -ne 0 && ! -e "$RALPH_COMPLETION_REJECTION_MARKER" ]] || {
    echo 'test-completion-recovery: non-completion hook created a marker' >&2; exit 1;
}

export FAKE_GATE_RC=2
ralph_supervision_begin implementation
set +e
printf '%s' "$payload" | "$tmp/scripts/ralph-completion-gate.sh" implementation >/dev/null 2>&1
infrastructure_rc=$?
set -e
[[ $infrastructure_rc -eq 2 && ! -e "$RALPH_COMPLETION_REJECTION_MARKER" ]] || {
    echo 'test-completion-recovery: infrastructure failure created a continuation marker' >&2; exit 1;
}

export FAKE_GATE_RC=130
ralph_supervision_begin implementation
set +e
printf '%s' "$payload" | "$tmp/scripts/ralph-completion-gate.sh" implementation >/dev/null 2>&1
signal_rc=$?
set -e
[[ $signal_rc -eq 130 && ! -e "$RALPH_COMPLETION_REJECTION_MARKER" ]] || {
    echo 'test-completion-recovery: interrupted gate created a marker' >&2; exit 1;
}

# A stale-loop classification is bound to history appended after the current
# supervision attempt began. The strict gate diagnostic is injected into the
# recovery handoff, and retries remain finite.
export RALPH_HISTORY_FILE="$tmp/.ralph/history.jsonl"
printf '# Recovery handoff\n\n- Current work is preserved.\n' > "$tmp/.ralph/agent/scratchpad.md"
printf 'implementation-plan: final audit must cover definition of done\n' > "$tmp/stale-diagnostics"
ralph_supervision_begin planning
printf '%s\n' \
    '{"ts":"2026-08-14T00:00:00Z","type":{"kind":"loop_started","prompt":"test planning"}}' \
    '{"ts":"2026-08-14T00:00:01Z","type":{"kind":"loop_completed","reason":"loop_stale"}}' \
    >> "$RALPH_HISTORY_FILE"
ralph_supervision_recover_stale planning "$tmp"
grep -q 'Supervisor recovery feedback' "$tmp/.ralph/agent/scratchpad.md"
grep -q 'final-gate.sh --planning' "$tmp/.ralph/agent/scratchpad.md"
if grep -q 'definition of done' "$tmp/.ralph/agent/scratchpad.md"; then
    echo 'test-completion-recovery: raw gate diagnostics entered the model handoff' >&2
    exit 1
fi

ralph_supervision_begin planning
set +e
ralph_supervision_attempt_was_stale >/dev/null 2>&1
old_stale_rc=$?
printf '%s\n' \
    '{"ts":"2026-08-14T00:01:00Z","type":{"kind":"loop_started","prompt":"test planning"}}' \
    '{"ts":"2026-08-14T00:01:01Z","type":{"kind":"loop_completed","reason":"completed"}}' \
    >> "$RALPH_HISTORY_FILE"
ralph_supervision_attempt_was_stale >/dev/null 2>&1
normal_completion_rc=$?
set -e
[[ $old_stale_rc -eq 1 && $normal_completion_rc -eq 1 ]] || {
    echo 'test-completion-recovery: stale classifier accepted old or normal history' >&2; exit 1;
}

ralph_supervision_begin planning
printf '{broken\n' >> "$RALPH_HISTORY_FILE"
set +e
ralph_supervision_attempt_was_stale >/dev/null 2>&1
malformed_history_rc=$?
set -e
[[ $malformed_history_rc -eq 2 ]] || {
    echo 'test-completion-recovery: malformed appended history was not rejected' >&2; exit 1;
}
# Restore a valid history boundary after the deliberate malformed record.
python3 - "$RALPH_HISTORY_FILE" <<'PY'
from pathlib import Path
import sys
path=Path(sys.argv[1])
lines=path.read_text().splitlines()
path.write_text('\n'.join(lines[:-1])+'\n')
PY
export FACTORY_RALPH_MAX_STALE_RECOVERIES=0
ralph_supervision_begin planning
printf '%s\n' \
    '{"ts":"2026-08-14T00:02:00Z","type":{"kind":"loop_started","prompt":"test planning"}}' \
    '{"ts":"2026-08-14T00:02:01Z","type":{"kind":"loop_completed","reason":"loop_stale"}}' \
    >> "$RALPH_HISTORY_FILE"
set +e
ralph_supervision_recover_stale planning "$tmp" >/dev/null 2>&1
bounded_rc=$?
set -e
[[ $bounded_rc -eq 3 ]] || {
    echo 'test-completion-recovery: stale retry ceiling was not enforced' >&2; exit 1;
}
unset FACTORY_RALPH_MAX_STALE_RECOVERIES

# Schema-invalid JSON records and symlinked scratchpad ancestors fail closed.
ralph_supervision_begin planning
printf '%s\n' \
    '{"ts":"2026-08-14T00:03:00Z","type":{"kind":"loop_started","prompt":"test planning"}}' \
    '[]' \
    '{"ts":"2026-08-14T00:03:01Z","type":{"kind":"loop_completed","reason":"loop_stale"}}' \
    >> "$RALPH_HISTORY_FILE"
set +e
ralph_supervision_attempt_was_stale >/dev/null 2>&1
schema_rc=$?
set -e
[[ $schema_rc -eq 2 ]] || {
    echo 'test-completion-recovery: schema-invalid history authorized recovery' >&2; exit 1;
}
ralph_supervision_begin planning
printf '%s\n' \
    '{"type":{"kind":"loop_started","prompt":"missing timestamp"}}' \
    '{"ts":"2026-08-14T00:04:01Z","type":{"kind":"loop_completed","reason":"loop_stale"}}' \
    >> "$RALPH_HISTORY_FILE"
set +e
ralph_supervision_attempt_was_stale >/dev/null 2>&1
missing_timestamp_rc=$?
set -e
[[ $missing_timestamp_rc -eq 2 ]] || {
    echo 'test-completion-recovery: history missing a timestamp authorized recovery' >&2; exit 1;
}
mkdir -p "$tmp/unsafe"
ln -s "$tmp/.ralph" "$tmp/unsafe/.ralph"
if ralph_supervision_record_stale_feedback planning "$tmp/unsafe" >/dev/null 2>&1; then
    echo 'test-completion-recovery: symlinked scratchpad ancestor was accepted' >&2
    exit 1
fi

rm -f "$RALPH_HISTORY_FILE"
ln -s "$tmp/elsewhere" "$RALPH_HISTORY_FILE"
if ralph_supervision_begin planning >/dev/null 2>&1; then
    echo 'test-completion-recovery: symlink history was accepted' >&2
    exit 1
fi

echo 'test: Ralph completion and stale-loop recovery checks passed'
