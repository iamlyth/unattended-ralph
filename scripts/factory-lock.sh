#!/usr/bin/env bash
# Source-only helper for the cross-process single-writer lock.

FACTORY_LOCK_HELPER_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
FACTORY_LOCK_ROOT=

factory_lock_bootstrap() {
    local root=${1:?repository root required}
    shift
    (( $# > 0 )) || { echo "factory-lock: lifecycle command required" >&2; return 2; }
    FACTORY_LOCK_ROOT=$root

    if [[ ${FACTORY_LOCK_HELD:-0} == 1 ]]; then
        python3 "$FACTORY_LOCK_HELPER_DIR/factory-lock-exec.py" "$root" --check
        return $?
    fi
    exec python3 "$FACTORY_LOCK_HELPER_DIR/factory-lock-exec.py" "$root" -- "$@"
}

factory_lock_acquire() {
    local root=${1:-${FACTORY_LOCK_ROOT:?repository root required}}
    if [[ ${FACTORY_LOCK_HELD:-0} != 1 ]]; then
        echo "factory-lock: lifecycle did not bootstrap the factory lock" >&2
        return 1
    fi
    python3 "$FACTORY_LOCK_HELPER_DIR/factory-lock-exec.py" "$root" --check
}

factory_lock_assert_held() {
    local root=${1:?repository root required}
    if [[ ${FACTORY_LOCK_HELD:-0} != 1 ]]; then
        echo "factory-lock: trusted transition did not inherit the lifecycle lock" >&2
        return 1
    fi
    python3 "$FACTORY_LOCK_HELPER_DIR/factory-lock-exec.py" "$root" --check
}

# Run an untrusted leaf without any descriptor referring to the lock inode and
# without lock metadata in its environment. This is already-loaded shell logic:
# a subshell closes the dynamic repository-root lock descriptor and unsets all
# lock metadata BEFORE any mutable workspace executable runs, so a workspace
# helper is never executed while authority is live and background descendants
# of the untrusted command can retain, unlock, or claim nothing. The trusted
# caller retains its own descriptor outside the subshell.
factory_lock_run_untrusted() {
    local root=${FACTORY_LOCK_ROOT:?factory_lock_bootstrap must run first}
    (( $# > 0 )) || { echo "factory-lock: untrusted command required" >&2; return 2; }
    local fd=${FACTORY_LOCK_FD:-} child rc had_errexit=0
    [[ $- == *e* ]] && had_errexit=1
    local old_term old_int old_hup
    old_term=$(trap -p TERM || true)
    old_int=$(trap -p INT || true)
    old_hup=$(trap -p HUP || true)
    (
        if [[ ${FACTORY_LOCK_HELD:-0} == 1 && "$fd" =~ ^[0-9]+$ && $fd -ge 3 ]]; then
            eval "exec $fd>&-"
        fi
        unset FACTORY_LOCK_HELD FACTORY_LOCK_FD FACTORY_LOCK_ID FACTORY_LOCK_ROOT
        if command -v setsid >/dev/null 2>&1; then
            exec setsid -- "$@"
        fi
        exec "$@"
    ) &
    child=$!
    # A lifecycle supervisor may be terminated while this shell is waiting on
    # an untrusted Ralph/Pi leaf. Forward the signal, reap the exact child, and
    # exit with the conventional signal status so no orchestrator is orphaned.
    trap 'kill -TERM -- -"$child" 2>/dev/null || kill -TERM "$child" 2>/dev/null || true; wait "$child" 2>/dev/null || true; exit 143' TERM
    trap 'kill -INT -- -"$child" 2>/dev/null || kill -INT "$child" 2>/dev/null || true; wait "$child" 2>/dev/null || true; exit 130' INT
    trap 'kill -HUP -- -"$child" 2>/dev/null || kill -HUP "$child" 2>/dev/null || true; wait "$child" 2>/dev/null || true; exit 129' HUP
    set +e
    wait "$child"
    rc=$?
    (( had_errexit == 0 )) || set -e
    trap - TERM INT HUP
    [[ -z "$old_term" ]] || eval "$old_term"
    [[ -z "$old_int" ]] || eval "$old_int"
    [[ -z "$old_hup" ]] || eval "$old_hup"
    return "$rc"
}
