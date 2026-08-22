#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
PROJECT_ROOT=$(cd -- "$SCRIPT_DIR/.." && pwd)
WRAPPER="$PROJECT_ROOT/scripts/pi2-ollama.sh"
SHIM="$PROJECT_ROOT/scripts/pi-cli-shims/ralph"
REAL_RALPH=$(command -v ralph || true)
assert_absent() {
    local needle=$1 file=$2
    if grep -Fq "$needle" "$file"; then
        echo "test-pi2-ollama-wrapper: unexpected '$needle' in $file" >&2
        exit 1
    fi
}
if ! command -v node >/dev/null 2>&1; then
    echo "test-pi2-ollama-wrapper: node unavailable — skipping wrapper extension checks" >&2
    exit 0
fi

tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
mkdir -p "$tmp/bin" "$tmp/.ralph"

cat > "$tmp/bin/ralph" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
if [[ ${1:-} == emit ]]; then
    topic=${2:?}
    payload=${3:-}
    printf '{"payload":"%s","topic":"%s","ts":"2026-08-15T00:00:00Z"}\n' \
        "$payload" "$topic" >> "${RALPH_EVENTS_FILE:?}"
    printf 'Event emitted: %s\n' "$topic"
    exit "${FAKE_RALPH_RC:-0}"
fi
printf 'real Ralph command:'
printf ' %s' "$@"
printf '\n'
exit "${FAKE_RALPH_RC:-0}"
EOF
cat > "$tmp/bin/pi2" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
printf '%s\n' "$@" > "${FAKE_ARGS_FILE:?}"
if [[ -n ${FAKE_STDIN_FILE:-} ]]; then
    cat > "$FAKE_STDIN_FILE"
fi
case "${FAKE_MODE:-success}" in
    success)
        printf '%s\n' 'stdout before'
        "${FAKE_SHIM:?}" emit factory.implement done
        printf '%s\n' 'stdout after'
        printf '%s\n' 'stderr remains untouched' >&2
        ;;
    spoof)
        printf '%s\n' 'Event emitted: factory.implement'
        ;;
    emit-failure)
        FAKE_RALPH_RC=42 "${FAKE_SHIM:?}" emit factory.implement done
        ;;
    backend-failure)
        printf '%s\n' 'backend failed' >&2
        exit 41
        ;;
    signal)
        kill -TERM "$$"
        ;;
    wait-for-signal)
        printf '%s\n' "$$" > "${FAKE_CHILD_PID_FILE:?}"
        while :; do sleep 1; done
        ;;
    raw-completion)
        printf '%s\n' 'ordinary model text' 'LOOP_COMPLETE'
        ;;
    ralph-probe)
        "${FAKE_SHIM:?}" emit factory.implement done
        count=0
        [[ ! -f ${FAKE_COUNTER_FILE:?} ]] || count=$(<"$FAKE_COUNTER_FILE")
        count=$((count + 1))
        printf '%s\n' "$count" > "$FAKE_COUNTER_FILE"
        if (( count == 1 )); then sleep 6; fi
        printf '%s\n' 'finished naturally'
        ;;
    *) exit 99 ;;
esac
EOF
chmod +x "$tmp/bin/ralph" "$tmp/bin/pi2"

export PATH="$tmp/bin:$PATH"
export FAKE_SHIM="$SHIM"
export FAKE_ARGS_FILE="$tmp/args"
export RALPH_EVENTS_FILE="$tmp/.ralph/events.jsonl"
: > "$RALPH_EVENTS_FILE"
cd "$tmp"

stdout=$tmp/stdout
stderr=$tmp/stderr
"$WRAPPER" --mode json >"$stdout" 2>"$stderr"
assert_absent 'Event emitted:' "$stdout"
grep -Fq 'Event published: factory.implement' "$stdout"
grep -Fq 'stderr remains untouched' "$stderr"
grep -Fq '"topic":"factory.implement"' "$RALPH_EVENTS_FILE"
printf '%s\n' --provider ollama --model deepseek-v4-flash --extension \
    ./scripts/pi-ralph-emit-extension.mjs --mode json > "$tmp/expected-args"
cmp "$tmp/expected-args" "$tmp/args"

# Only the trusted `ralph emit` command is changed. Identical arbitrary backend
# output remains visible to Ralph's detector.
FAKE_MODE=spoof "$WRAPPER" --mode json >"$stdout" 2>"$stderr"
grep -Fq 'Event emitted: factory.implement' "$stdout"
assert_absent 'Event published: factory.implement' "$stdout"
"$SHIM" --version >"$stdout"
grep -Fq 'real Ralph command: --version' "$stdout"
# Direct shim invocation inspects the shell-expanded final argv, not merely the
# original command text seen by the Pi extension.
reserved_payload=LOOP_COMPLETE
events_before=$(wc -l < "$RALPH_EVENTS_FILE")
set +e
"$SHIM" emit factory.implement "$reserved_payload" >"$stdout" 2>"$stderr"
direct_reserved_rc=$?
"$SHIM" emit MAINTENANCE_PLAN_COMPLETE 'done' >"$stdout" 2>"$stderr"
direct_topic_rc=$?
"$SHIM" emit factory.implement >"$stdout" 2>"$stderr"
direct_missing_rc=$?
"$SHIM" emit factory.implement one two >"$stdout" 2>"$stderr"
direct_extra_rc=$?
"$SHIM" emit test.work 'done' >"$stdout" 2>"$stderr"
direct_unknown_rc=$?
set -e
[[ $direct_reserved_rc -eq 2 && $direct_topic_rc -eq 2 ]]
[[ $direct_missing_rc -eq 2 && $direct_extra_rc -eq 2 && $direct_unknown_rc -eq 2 ]]
[[ $(wc -l < "$RALPH_EVENTS_FILE") -eq $events_before ]]
node --input-type=module - "$PROJECT_ROOT/scripts/pi-ralph-emit-extension.mjs" <<'EOF'
import assert from 'node:assert/strict';
import { pathToFileURL } from 'node:url';
const extension = await import(pathToFileURL(process.argv[2]));
const { rewriteRalphEmitCommand } = extension;
let toolHandler;
extension.default({ on(name, handler) { if (name === 'tool_call') toolHandler = handler; } });
assert.equal(typeof toolHandler, 'function');
const directEvent = { toolName: 'bash', input: { command: 'ralph emit factory.implement done' } };
toolHandler(directEvent);
assert.equal(directEvent.input.command, './scripts/pi-cli-shims/ralph emit factory.implement done');
for (const command of [
  'ralph emit LOOP_COMPLETE done',
  'ralph emit factory.implement LOOP_COMPLETE',
  'ralph emit factory.plan "shortcut PLAN_COMPLETE payload"',
  'ralph emit factory.audit AUDIT_COMPLETE',
  'ralph emit factory.maintenance.plan MAINTENANCE_PLAN_COMPLETE',
  'ralph emit factory.maintenance.implement MAINTENANCE_COMPLETE',
]) {
  const tokenEvent = { toolName: 'bash', input: { command } };
  assert.equal(toolHandler(tokenEvent).block, true, command);
  const tokenResult = rewriteRalphEmitCommand(command);
  assert.equal(tokenResult.reserved, true, command);
  assert.equal(tokenResult.matched, false, command);
}
const unsafeEvent = { toolName: 'bash', input: { command: 'cd /tmp && ralph emit factory.implement done' } };
assert.equal(toolHandler(unsafeEvent).block, true);
let result = rewriteRalphEmitCommand('ralph emit factory.implement "done"');
assert.equal(result.matched, true);
assert.equal(result.unsafe, false);
assert.equal(result.command, './scripts/pi-cli-shims/ralph emit factory.implement "done"');
for (const command of [
  'ralph emit factory.implement',
  'ralph emit factory.implement ""',
  'ralph emit factory.implement one two',
  'ralph emit test.work done',
  'ralph emit factory.implement "$payload"',
  'ralph emit factory.implement "${payload}"',
  'PAYLOAD=done ralph emit factory.implement "$PAYLOAD"',
  'cd /tmp && ralph emit factory.implement done',
  'ralph emit factory.implement done; sleep 600',
  'ralph emit factory.implement done | cat',
  'ralph emit factory.implement done > /tmp/result',
  'ralph emit factory.implement done # trailing command',
  '(ralph emit factory.implement done)',
  'echo "$(ralph emit factory.implement done)"',
]) {
  result = rewriteRalphEmitCommand(command);
  assert.equal(result.matched, false, command);
  assert.equal(result.unsafe, true, command);
}
result = rewriteRalphEmitCommand("ralph emit factory.implement 'payload; && > remains quoted'");
assert.equal(result.matched, true);
assert.equal(result.unsafe, false);
for (const command of [
  'printf "Event emitted: spoof\\n"',
  "printf '%s' 'ralph emit factory.implement done'",
]) {
  result = rewriteRalphEmitCommand(command);
  assert.equal(result.matched, false, command);
  assert.equal(result.unsafe, false, command);
}
EOF

prompt_file="$tmp/ralph prompt.md"
printf '%s\n' 'prompt body from host tmp' > "$prompt_file"
export FAKE_STDIN_FILE="$tmp/stdin"
OLLAMA_PROVIDER=custom-provider OLLAMA_MODEL=custom-model \
    "$WRAPPER" --mode json "Please read and execute the task in $prompt_file" \
    >"$stdout" 2>"$stderr"
cmp "$prompt_file" "$tmp/stdin"
printf '%s\n' --provider custom-provider --model custom-model --extension \
    ./scripts/pi-ralph-emit-extension.mjs --mode json > "$tmp/expected-args"
cmp "$tmp/expected-args" "$tmp/args"

unset FAKE_STDIN_FILE
ln -s "$prompt_file" "$tmp/prompt-link"
mkfifo "$tmp/prompt-fifo"
truncate -s 4194305 "$tmp/prompt-oversize"
set +e
"$WRAPPER" "Please read and execute the task in $tmp/prompt-link" >/dev/null 2>&1
symlink_prompt_rc=$?
"$WRAPPER" "Please read and execute the task in /etc/hosts" >/dev/null 2>&1
outside_prompt_rc=$?
"$WRAPPER" "Please read and execute the task in $tmp/prompt-fifo" >/dev/null 2>&1
fifo_prompt_rc=$?
"$WRAPPER" "Please read and execute the task in $tmp/prompt-oversize" >/dev/null 2>&1
oversize_prompt_rc=$?
set -e
[[ $symlink_prompt_rc -ne 0 && $outside_prompt_rc -ne 0 && $fifo_prompt_rc -ne 0 && $oversize_prompt_rc -ne 0 ]] || {
    echo 'test-pi2-ollama-wrapper: unsafe prompt path was accepted' >&2
    exit 1
}

set +e
FAKE_MODE=emit-failure "$WRAPPER" --mode json >"$stdout" 2>"$stderr"
emit_failure_rc=$?
FAKE_MODE=backend-failure "$WRAPPER" --mode json >"$stdout" 2>"$stderr"
backend_failure_rc=$?
FAKE_MODE=signal "$WRAPPER" --mode json >"$stdout" 2>"$stderr"
signal_rc=$?
set -e
[[ $emit_failure_rc -eq 42 ]] || {
    echo "test-pi2-ollama-wrapper: emit failure was masked: $emit_failure_rc" >&2
    exit 1
}
[[ $backend_failure_rc -eq 41 ]] || {
    echo "test-pi2-ollama-wrapper: backend failure was masked: $backend_failure_rc" >&2
    exit 1
}
[[ $signal_rc -eq 143 ]] || {
    echo "test-pi2-ollama-wrapper: backend signal status was masked: $signal_rc" >&2
    exit 1
}

# The wrapper and secure launcher both exec their child. External termination
# therefore reaches the actual backend PID without an orphaning supervisor.
export FAKE_CHILD_PID_FILE="$tmp/backend-pid"
FAKE_MODE=wait-for-signal "$WRAPPER" --mode json >"$stdout" 2>"$stderr" &
wrapper_pid=$!
for _ in {1..100}; do [[ -s "$FAKE_CHILD_PID_FILE" ]] && break; sleep 0.02; done
[[ -s "$FAKE_CHILD_PID_FILE" ]]
[[ $(<"$FAKE_CHILD_PID_FILE") == "$wrapper_pid" ]]
set +e
kill -TERM "$wrapper_pid"
wait "$wrapper_pid"
external_signal_rc=$?
set -e
[[ $external_signal_rc -eq 143 ]]

# Exercise the pinned installed Ralph detector when available. Hermetic shim,
# status, signal, and prompt checks above remain mandatory on product runners
# that do not provision the orchestration binary.
printf 'probe prompt\n' > "$tmp/PROMPT.md"
cat > "$tmp/ralph.yml" <<EOF
cli:
  backend: pi
  command: $WRAPPER
event_loop:
  prompt_file: PROMPT.md
  starting_event: test.work
  max_iterations: 2
  max_runtime_seconds: 60
  max_consecutive_failures: 1
hats:
  worker:
    name: Worker
    description: wrapper regression probe
    triggers: [test.work]
    publishes: [test.work]
    default_publishes: test.work
EOF
if [[ -n "$REAL_RALPH" && $($REAL_RALPH --version) == 'ralph 2.10.1' ]]; then
    cat > "$tmp/completion-hook" <<'EOF'
#!/usr/bin/env bash
cat >/dev/null
printf 'hook\n' >> "${FAKE_COMPLETION_HOOK_LOG:?}"
EOF
    chmod +x "$tmp/completion-hook"
    cat > "$tmp/completion.yml" <<EOF
cli:
  backend: pi
  command: $WRAPPER
event_loop:
  prompt_file: PROMPT.md
  completion_promise: LOOP_COMPLETE
  starting_event: test.work
  max_iterations: 2
  max_runtime_seconds: 60
hooks:
  enabled: true
  events:
    pre.loop.complete:
      - name: exact-raw-completion
        command: ["$tmp/completion-hook"]
        on_error: block
hats:
  worker:
    name: Worker
    description: exact raw completion regression probe
    triggers: [test.work]
    publishes: [test.work]
    default_publishes: test.work
EOF
    export FAKE_COMPLETION_HOOK_LOG="$tmp/completion-hook.log"
    : > "$FAKE_COMPLETION_HOOK_LOG"
    FAKE_MODE=raw-completion "$REAL_RALPH" -c "$tmp/completion.yml" run --exclusive --no-tui \
        >"$tmp/completion-out" 2>"$tmp/completion-err"
    [[ $(grep -c '^hook$' "$FAKE_COMPLETION_HOOK_LOG") -eq 1 ]] || {
        echo 'test-pi2-ollama-wrapper: exact raw token did not trigger completion exactly once' >&2
        exit 1
    }

    export FAKE_COUNTER_FILE="$tmp/probe-count"
    set +e
    FAKE_MODE=ralph-probe "$REAL_RALPH" -c "$tmp/ralph.yml" run --exclusive --no-tui \
        >"$tmp/ralph-out" 2>"$tmp/ralph-err"
    ralph_rc=$?
    set -e
    [[ $ralph_rc -eq 2 ]] || {
        echo "test-pi2-ollama-wrapper: unexpected Ralph probe status: $ralph_rc" >&2
        tail -40 "$tmp/ralph-out" "$tmp/ralph-err" >&2
        exit 1
    }
    sed 's/\x1b\[[0-9;]*m//g' "$tmp/ralph-out" | grep -Fq 'reason=max_iterations'
    sed 's/\x1b\[[0-9;]*m//g' "$tmp/ralph-out" | grep -Fq 'finished naturally'
    assert_absent 'Event emitted:' "$tmp/ralph-out"
    assert_absent 'consecutive_failures' "$tmp/ralph-out"
    [[ $(<"$tmp/probe-count") == 2 ]]
else
    echo 'test-pi2-ollama-wrapper: pinned Ralph 2.10.1 integration probe unavailable; hermetic checks only' >&2
fi

echo 'test: Pi2 Ralph event acknowledgement filtering checks passed'
