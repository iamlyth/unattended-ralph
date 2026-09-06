#!/bin/sh
# Minimal generic-leakage gate forwarder; implementation is hidden.
set -eu
root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
exec "$root/.factory/tools/check-generic-leakage.sh" "$@"
