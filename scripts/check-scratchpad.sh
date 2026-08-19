#!/usr/bin/env bash
# Keep the tracked Ralph handoff concise and free of reserved completion tokens.
set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
PROJECT_ROOT=$(cd -- "$SCRIPT_DIR/.." && pwd)
SCRATCHPAD=${FACTORY_SCRATCHPAD_PATH:-$PROJECT_ROOT/.ralph/agent/scratchpad.md}
TOKEN=
ALLOW_MISSING=false
ALLOW_OVERSIZE=false
MAX_LINES=${FACTORY_SCRATCHPAD_MAX_LINES:-80}
MAX_BYTES=${FACTORY_SCRATCHPAD_MAX_BYTES:-8192}

for arg in "$@"; do
    case "$arg" in
        --allow-missing) ALLOW_MISSING=true ;;
        --allow-oversize) ALLOW_OVERSIZE=true ;;
        --*) echo "scratchpad-guard: unknown option: $arg" >&2; exit 2 ;;
        *)
            [[ -z "$TOKEN" ]] || {
                echo "scratchpad-guard: expected at most one completion token" >&2; exit 2;
            }
            TOKEN=$arg
            ;;
    esac
done

[[ "$MAX_LINES" =~ ^[1-9][0-9]*$ ]] || { echo "scratchpad-guard: invalid line limit" >&2; exit 2; }
[[ "$MAX_BYTES" =~ ^[1-9][0-9]*$ ]] || { echo "scratchpad-guard: invalid byte limit" >&2; exit 2; }

python3 - "$SCRATCHPAD" "$TOKEN" "$ALLOW_MISSING" "$ALLOW_OVERSIZE" "$MAX_LINES" "$MAX_BYTES" <<'PY'
import os
from pathlib import Path
import re
import stat
import sys

name, token, allow_missing_text, allow_oversize_text, raw_lines, raw_bytes = sys.argv[1:]
allow_missing = allow_missing_text == 'true'
allow_oversize = allow_oversize_text == 'true'
max_lines, max_bytes = int(raw_lines), int(raw_bytes)
path = Path(name).absolute()

# Traverse every ancestor without following symlinks. The scratchpad may be
# relocated by a test, but the same trust rule applies to that custom path.
parts = path.parts
fd = os.open(parts[0], os.O_RDONLY | os.O_DIRECTORY)
try:
    for component in parts[1:-1]:
        try:
            next_fd = os.open(
                component,
                os.O_RDONLY | os.O_DIRECTORY | getattr(os, 'O_NOFOLLOW', 0),
                dir_fd=fd,
            )
        except OSError as exc:
            print(f'scratchpad-guard: unsafe scratchpad directory: {exc}', file=sys.stderr)
            raise SystemExit(1)
        os.close(fd)
        fd = next_fd
    try:
        scratch_fd = os.open(path.name, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0), dir_fd=fd)
    except FileNotFoundError:
        if allow_missing:
            print('scratchpad-guard: scratchpad absent at fresh-loop boundary; deferred until the next checkpoint')
            raise SystemExit(0)
        print(f'scratchpad-guard: missing or empty scratchpad: {path}', file=sys.stderr)
        raise SystemExit(1)
    except OSError as exc:
        print(f'scratchpad-guard: cannot safely open scratchpad: {exc}', file=sys.stderr)
        raise SystemExit(1)
    try:
        info = os.fstat(scratch_fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                or info.st_uid != os.getuid() or info.st_mode & 0o022):
            print('scratchpad-guard: scratchpad is not a trusted regular file', file=sys.stderr)
            raise SystemExit(1)
        if info.st_size == 0:
            print(f'scratchpad-guard: missing or empty scratchpad: {path}', file=sys.stderr)
            raise SystemExit(1)
        if info.st_size > 1024 * 1024:
            print('scratchpad-guard: scratchpad exceeds the absolute read limit', file=sys.stderr)
            raise SystemExit(1)
        raw = bytearray()
        while True:
            chunk = os.read(scratch_fd, 65536)
            if not chunk:
                break
            raw.extend(chunk)
    finally:
        os.close(scratch_fd)
finally:
    os.close(fd)

try:
    text = bytes(raw).decode('utf-8')
except UnicodeError as exc:
    print(f'scratchpad-guard: scratchpad is not UTF-8: {exc}', file=sys.stderr)
    raise SystemExit(1)
lines = len(text.splitlines())
size = len(raw)
oversize = False
if lines > max_lines:
    if not allow_oversize:
        print(f'scratchpad-guard: scratchpad has {lines} lines; replace it with one handoff of at most {max_lines} lines', file=sys.stderr)
        raise SystemExit(1)
    print(f'scratchpad-guard: warning: checkpoint handoff has {lines} lines; final limit is {max_lines}', file=sys.stderr)
    oversize = True
if size > max_bytes:
    if not allow_oversize:
        print(f'scratchpad-guard: scratchpad has {size} bytes; limit is {max_bytes}', file=sys.stderr)
        raise SystemExit(1)
    print(f'scratchpad-guard: warning: checkpoint handoff has {size} bytes; final limit is {max_bytes}', file=sys.stderr)
    oversize = True

documents = len(re.findall(r'^#[ \t]+', text, re.M))
if documents != 1:
    print(f'scratchpad-guard: scratchpad must contain exactly one level-one handoff document; found {documents}', file=sys.stderr)
    raise SystemExit(1)
if token and token in text:
    print(f"scratchpad-guard: reserved completion token '{token}' must not appear in the scratchpad", file=sys.stderr)
    raise SystemExit(1)
if oversize:
    print('scratchpad-guard: structurally valid checkpoint handoff accepted; worker must shorten it before final completion')
else:
    print(f'scratchpad-guard: concise current handoff accepted ({lines} lines, {size} bytes)')
PY
