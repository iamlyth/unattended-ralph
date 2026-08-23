#!/usr/bin/env bash
# Ollama Cloud usage guard with optional wait-until-reset behavior.
set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
PROJECT_ROOT=$(cd -- "$SCRIPT_DIR/.." && pwd)
# Task 15 migration: the operator store lives OUTSIDE the model workspace
# (Task 7 review) — $OLLAMA_USAGE_ENV_FILE when set by the operator,
# otherwise $XDG_CONFIG_HOME/unattended-ralph/ollama-usage-env or
# ~/.config/unattended-ralph/ollama-usage-env.  The legacy workspace store
# (<repository root>/.ollama-usage-env) is detected below with metadata only
# and is never read, sourced, or parsed as a credential authority.
if [[ -n "${OLLAMA_USAGE_ENV_FILE:-}" ]]; then
    ENV_FILE=${OLLAMA_USAGE_ENV_FILE}
elif [[ -n "${XDG_CONFIG_HOME:-}" ]]; then
    ENV_FILE="$XDG_CONFIG_HOME/unattended-ralph/ollama-usage-env"
else
    ENV_FILE="${HOME:-}/.config/unattended-ralph/ollama-usage-env"
fi

# Legacy workspace-scoped store detection (metadata only; never a credential
# authority).  The operator must migrate it to the external operator store;
# the guard does not read a single byte of the legacy store.
if [[ -e "$PROJECT_ROOT/.ollama-usage-env" || -L "$PROJECT_ROOT/.ollama-usage-env" ]]; then
    echo "ollama-guard: DEPRECATED legacy store $PROJECT_ROOT/.ollama-usage-env;" >&2
    echo "ollama-guard: the operator credential store lives outside the model workspace" >&2
    echo "ollama-guard: (migrate it; the legacy store is never read as a credential authority)" >&2
fi

# Resolve every setting into a *private non-exported* shell variable and scrub
# the complete OLLAMA/credential environment (QUOTA-02, §10): the operator
# store is parsed, never sourced, and any ambient OLLAMA_*/credential key is
# unset before any child (curl, python) is spawned, so the cookie never
# appears in a child argv or child environment.
COOKIE=${OLLAMA_COOKIE:-}
SETTINGS_URL=${OLLAMA_SETTINGS_URL:-https://ollama.com/settings}

if [[ -z $COOKIE && -r "$ENV_FILE" ]]; then
    # Read the store into private shell variables without exporting anything.
    while IFS= read -r _line; do
        _line=${_line#export }
        case "$_line" in
            OLLAMA_COOKIE=*|OLLAMA_THRESHOLD=*|OLLAMA_WAIT_INTERVAL_SECONDS=*|OLLAMA_WAIT_MAX_SECONDS=*|OLLAMA_WAIT_MAX_POLLS=*|OLLAMA_SETTINGS_URL=*)
                _key=${_line%%=*}
                _val=${_line#*=}
                case "$_val" in
                    \'*\') _val=${_val#\'}; _val=${_val%\'} ;;
                    \"*\") _val=${_val#\"}; _val=${_val%\"} ;;
                esac
                printf -v "$_key" '%s' "$_val"
                ;;
        esac
    done < "$ENV_FILE"
    unset _line _key _val
fi

COOKIE=${OLLAMA_COOKIE:-$COOKIE}
SETTINGS_URL=${OLLAMA_SETTINGS_URL:-$SETTINGS_URL}
THRESHOLD=${OLLAMA_THRESHOLD:-80}
POLL_INTERVAL=${OLLAMA_WAIT_INTERVAL_SECONDS:-300}
MAX_WAIT=${OLLAMA_WAIT_MAX_SECONDS:-0}
MAX_POLLS=${OLLAMA_WAIT_MAX_POLLS:-0}

# Scrub every exported OLLAMA_* and the individual cookie-component keys so no
# child (curl, python) can inherit a credential (QUOTA-02).
for _k in $(env | sed -n 's/^\(OLLAMA_[A-Za-z0-9_]*\)=.*/\1/p'); do
    unset "$_k" 2>/dev/null || true
done
unset OLLAMA_COOKIE OLLAMA_THRESHOLD OLLAMA_WAIT_INTERVAL_SECONDS \
      OLLAMA_WAIT_MAX_SECONDS OLLAMA_WAIT_MAX_POLLS OLLAMA_SETTINGS_URL \
      __Secure_session aid cf_clearance

MODE=check
JSON_OUTPUT=false
HTML_FILE=${OLLAMA_USAGE_HTML_FILE:-}

usage() {
    cat <<'EOF'
Usage: scripts/ollama-usage-guard.sh [--check|--wait] [--json] [--html-file FILE]

--check       Check once (default). Exit 0=allowed, 1=quota threshold, 2=fatal, 3=transient.
--wait        Poll until usage falls below threshold; fatal authentication/parse errors stop.
--json        Emit machine-readable status for a single check.
--html-file   Parse a saved settings page instead of using the network (tests/diagnostics).
EOF
}

while (( $# > 0 )); do
    case "$1" in
        --check) MODE=check; shift ;;
        --wait) MODE='wait'; shift ;;
        --json) JSON_OUTPUT=true; shift ;;
        --html-file)
            (( $# >= 2 )) || { echo "ollama-guard: --html-file requires a path" >&2; exit 2; }
            HTML_FILE=$2
            shift 2
            ;;
        -h|--help) usage; exit 0 ;;
        *) echo "ollama-guard: unknown option '$1'" >&2; usage >&2; exit 2 ;;
    esac
done

[[ "$THRESHOLD" =~ ^[0-9]+([.][0-9]+)?$ ]] || { echo "ollama-guard: invalid threshold '$THRESHOLD'" >&2; exit 2; }
[[ "$POLL_INTERVAL" =~ ^[0-9]+$ ]] || { echo "ollama-guard: invalid poll interval '$POLL_INTERVAL'" >&2; exit 2; }
[[ "$MAX_WAIT" =~ ^[0-9]+$ ]] || { echo "ollama-guard: invalid max wait '$MAX_WAIT'" >&2; exit 2; }

fetch_html() {
    if [[ -n "$HTML_FILE" ]]; then
        [[ -r "$HTML_FILE" ]] || return 2
        cat -- "$HTML_FILE"
        return 0
    fi

    [[ -n ${COOKIE:-} ]] || return 2
    # The cookie travels to curl only through its private stdin config pipe
    # (-K -), never through argv or environment (QUOTA-02 / §22 test 24).
    printf 'header = "Cookie: %s"\n' "$COOKIE" \
        | curl -fsSL --max-time 20 -K - \
              -H "User-Agent: Mozilla/5.0 (X11; Linux x86_64) Gecko/20100101 Firefox/140.0" \
              "$SETTINGS_URL"
}

# Prints: classification<TAB>session<TAB>weekly<TAB>reset_hint
inspect_html() {
    python3 -c '
import html as html_module
import re
import sys
raw = sys.stdin.read()
plain = html_module.unescape(re.sub(r"<[^>]+>", " ", raw))
plain = re.sub(r"\s+", " ", plain)
if re.search(r"\bSign in\b", plain, re.I) and not re.search(r"Usage\s*[·|-]?\s*Settings", plain, re.I):
    print("auth\t\t\tlogin page returned")
    raise SystemExit
patterns = {
    "session": [r"aria-label=[\"\x27]Session usage\s+([0-9]+(?:\.[0-9]+)?)%\s*used", r"Session usage.*?([0-9]+(?:\.[0-9]+)?)\s*%\s*used"],
    "weekly": [r"aria-label=[\"\x27]Weekly usage\s+([0-9]+(?:\.[0-9]+)?)%\s*used", r"Weekly usage.*?([0-9]+(?:\.[0-9]+)?)\s*%\s*used"],
}
values = {}
for name, options in patterns.items():
    match = next((m for pattern in options if (m := re.search(pattern, raw, re.I | re.S))), None)
    values[name] = match.group(1) if match else ""
if not values["session"] or not values["weekly"]:
    print("parse\t{}\t{}\tusage fields not found".format(values["session"], values["weekly"]))
    raise SystemExit
hint_match = re.search(r"((?:session|weekly)?\s*(?:usage\s*)?resets?\s+(?:in|at|on)?\s*[^<]{1,80})", plain, re.I)
hint = hint_match.group(1).strip() if hint_match else ""
print("usage\t{}\t{}\t{}".format(values["session"], values["weekly"], hint))
'
}

check_once() {
    local html rc classification session weekly reset_hint blocked
    set +e
    html=$(fetch_html 2>/dev/null)
    rc=$?
    set -e
    if (( rc == 2 )); then
        if [[ -n "$HTML_FILE" ]]; then
            echo "ollama-guard: cannot read HTML fixture '$HTML_FILE'" >&2
        else
            echo "ollama-guard: OLLAMA_COOKIE is missing; run source scripts/update-ollama-cookies.sh" >&2
        fi
        return 2
    elif (( rc != 0 )); then
        echo "ollama-guard: transient failure fetching $SETTINGS_URL" >&2
        return 3
    fi

    IFS=$'\t' read -r classification session weekly reset_hint < <(printf '%s' "$html" | inspect_html)
    case "$classification" in
        auth)
            echo "ollama-guard: authentication expired; refresh cookies with source scripts/update-ollama-cookies.sh" >&2
            return 2
            ;;
        parse)
            echo "ollama-guard: settings page format changed; session='${session:-?}' weekly='${weekly:-?}'" >&2
            return 2
            ;;
        usage) ;;
        *) echo "ollama-guard: invalid parser result" >&2; return 2 ;;
    esac

    blocked=false
    if awk "BEGIN {exit !($session >= $THRESHOLD || $weekly >= $THRESHOLD)}"; then
        blocked=true
    fi

    if $JSON_OUTPUT; then
        python3 - "$session" "$weekly" "$THRESHOLD" "$blocked" "$reset_hint" <<'PY'
import json, sys
print(json.dumps({
    "session_percent": float(sys.argv[1]),
    "weekly_percent": float(sys.argv[2]),
    "threshold_percent": float(sys.argv[3]),
    "blocked": sys.argv[4] == "true",
    "reset_hint": sys.argv[5] or None,
}, separators=(",", ":")))
PY
    else
        printf 'ollama-guard: session=%s%% weekly=%s%% threshold=%s%%\n' "$session" "$weekly" "$THRESHOLD"
        [[ -z "$reset_hint" ]] || printf 'ollama-guard: %s\n' "$reset_hint"
    fi

    $blocked && return 1
    return 0
}

if [[ "$MODE" == check ]]; then
    check_once
    exit $?
fi

$JSON_OUTPUT && { echo "ollama-guard: --json cannot be combined with --wait" >&2; exit 2; }
started=$SECONDS
polls=0
while true; do
    polls=$((polls + 1))
    set +e
    check_once
    rc=$?
    set -e
    case "$rc" in
        0)
            if (( polls > 1 )); then
                echo "ollama-guard: quota available again; continuing Ralph"
            fi
            exit 0
            ;;
        1) echo "ollama-guard: quota threshold reached; waiting ${POLL_INTERVAL}s before recheck" >&2 ;;
        3) echo "ollama-guard: network failure while waiting; retrying in ${POLL_INTERVAL}s" >&2 ;;
        *) exit "$rc" ;;
    esac

    elapsed=$((SECONDS - started))
    if (( MAX_POLLS > 0 && polls >= MAX_POLLS )); then
        echo "ollama-guard: maximum poll count reached" >&2
        exit 1
    fi
    if (( MAX_WAIT > 0 && elapsed + POLL_INTERVAL > MAX_WAIT )); then
        echo "ollama-guard: maximum wait time reached" >&2
        exit 1
    fi
    sleep "$POLL_INTERVAL"
done
