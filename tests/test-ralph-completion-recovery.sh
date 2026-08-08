#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
PROJECT_ROOT=$(cd -- "$SCRIPT_DIR/.." && pwd)
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
mkdir -p "$tmp/scripts" "$tmp/.factory-state"
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

export FAKE_GATE_RC=130
ralph_supervision_begin implementation
set +e
printf '%s' "$payload" | "$tmp/scripts/ralph-completion-gate.sh" implementation >/dev/null 2>&1
signal_rc=$?
set -e
[[ $signal_rc -eq 130 && ! -e "$RALPH_COMPLETION_REJECTION_MARKER" ]] || {
    echo 'test-completion-recovery: interrupted gate created a marker' >&2; exit 1;
}

echo 'test: Ralph completion rejection recovery checks passed'
