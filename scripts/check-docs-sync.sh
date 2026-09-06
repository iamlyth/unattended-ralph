#!/bin/sh
# Minimal documentation-gate forwarder; implementation is hidden.
set -eu
root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
exec "$root/.factory/tools/check-docs-sync.sh" "$@"
