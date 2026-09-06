#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
PROVIDER=${OLLAMA_PROVIDER:-ollama}
MODEL=${OLLAMA_MODEL:-deepseek-v4-flash}
args=("$@")
prompt_prefix="Please read and execute the task in "

# This explicit extension runs inside Pi's jail and rewrites only a direct
# `ralph emit` bash tool call to the repository shim. The real Ralph binary
# remains the one resolved by the jail's trusted PATH.
args=(--extension ./.factory/tools/pi-factory-guard-extension.mjs "${args[@]}")

launcher=(python3 "$SCRIPT_DIR/pi2-secure-exec.py")
if (( ${#args[@]} > 0 )); then
    last_index=$(( ${#args[@]} - 1 ))
    prompt_arg=${args[$last_index]}
    if [[ "$prompt_arg" == "$prompt_prefix"* ]]; then
        prompt_file=${prompt_arg#"$prompt_prefix"}
        unset "args[$last_index]"
        launcher+=(--prompt-file "$prompt_file")
    fi
fi

exec "${launcher[@]}" -- pi2 --provider "$PROVIDER" --model "$MODEL" "${args[@]}"
