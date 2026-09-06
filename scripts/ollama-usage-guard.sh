#!/bin/sh
# Canonical §10 compatibility entrypoint. Credentials remain in the trusted
# hidden guard and never cross into model argv, environment, prompts, or tools.
set -eu
root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
exec /usr/bin/python3 -I "$root/.factory/loop/usage.py" "$@"
