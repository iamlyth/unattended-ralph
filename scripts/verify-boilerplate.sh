#!/bin/sh
# Minimal compatibility/project-gate forwarder; implementation is hidden.
set -eu
root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
exec "$root/.factory/tools/verify-boilerplate.sh" "$@"
