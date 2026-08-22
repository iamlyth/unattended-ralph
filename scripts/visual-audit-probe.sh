#!/usr/bin/env bash
# Real non-skipping image round-trip probe for the visual-audit vision model.
#
# Before a consumer may select a vision model for visual audit, this probe must
# pass: it builds a deterministic known image (a solid red rectangle), reads
# its bytes, computes the SHA-256, and delivers those exact bytes in-memory to
# the configured model through the SDK driver. The model must correctly
# describe the image (solid red rectangle). This proves actual image-byte
# delivery to the exact model — not prompt-only, text-description, golden, or
# fixture-stub behavior. On any model outage, unconfigured model, or wrong
# answer the probe fails closed and the consumer must not enable visual audit.
#
# The vision model is consumer-configured in the tracked
# `.factory/visual-audit.toml` (`vision_model`) and never defaulted in the
# generic scaffold. Test-only execution overrides — `VISUAL_AUDIT_VISION_MODEL`
# (a fake model), `VISUAL_AUDIT_SDK_DRIVER`, and `VISUAL_AUDIT_CONFIG` — are
# honored only with the explicit `RALPH_VISUAL_AUDIT_TESTING=1` marker;
# production never accepts caller-provided execution paths.
#
# The model subprocess runs under a HARD configured timeout
# (`VISUAL_AUDIT_MODEL_TIMEOUT`, default 120s, ceiling 300s) in its own
# process session/group: on timeout the whole group receives SIGTERM, is given
# a bounded grace, then SIGKILL, and is waited/reaped. No global pkill and no
# display assumptions exist here.
#
# Usage: scripts/visual-audit-probe.sh
set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
PROJECT_ROOT=$(cd -- "$SCRIPT_DIR/.." && pwd)

is_testing() { [[ "${RALPH_VISUAL_AUDIT_TESTING:-}" == "1" ]]; }

# Execution-path overrides require the explicit test marker.
if ! is_testing; then
    for var in VISUAL_AUDIT_CONFIG VISUAL_AUDIT_SDK_DRIVER VISUAL_AUDIT_VISION_MODEL; do
        if [[ -n "${!var:-}" ]]; then
            echo "visual-audit-probe: $var is a test-only execution override; set RALPH_VISUAL_AUDIT_TESTING=1 in tests" >&2
            exit 1
        fi
    done
fi

MODEL="${VISUAL_AUDIT_VISION_MODEL:-}"
if ! is_testing || [[ -z "$MODEL" ]]; then
    # Production (and testing without a fake model) reads the tracked config.
    MODEL=$(python3 - "$PROJECT_ROOT/.factory/visual-audit.toml" <<'PY'
import sys, tomllib
path = sys.argv[1]
try:
    with open(path, "rb") as fh:
        section = tomllib.load(fh).get("visual-audit", {})
    print(section.get("vision_model", "") or "")
except (OSError, tomllib.TOMLDecodeError):
    print("")
PY
)
fi
if [[ -z "$MODEL" ]]; then
    echo "visual-audit-probe: consumer must configure vision_model in .factory/visual-audit.toml (tests: set VISUAL_AUDIT_VISION_MODEL with RALPH_VISUAL_AUDIT_TESTING=1); the generic scaffold defaults no vision model" >&2
    exit 64
fi

MODEL_TIMEOUT="${VISUAL_AUDIT_MODEL_TIMEOUT:-120}"
if ! [[ "$MODEL_TIMEOUT" =~ ^[0-9]+$ ]] || (( MODEL_TIMEOUT < 1 || MODEL_TIMEOUT > 300 )); then
    echo "visual-audit-probe: VISUAL_AUDIT_MODEL_TIMEOUT must be an integer in [1, 300]" >&2
    exit 64
fi
MODEL_GRACE=5

mkdir -p "$PROJECT_ROOT/.factory-state/visual-audit"
work=$(mktemp -d "$PROJECT_ROOT/.factory-state/visual-audit/probe.XXXXXX")
trap 'rm -rf "$work"' EXIT
chmod 700 "$work"

# Deterministic known image: 64x32 solid red PNG (stdlib writer).
python3 - "$work/probe.png" <<'PY'
import struct, sys, zlib
path = sys.argv[1]
w, h = 64, 32
def chunk(t, d):
    c = t + d
    return struct.pack('>I', len(d)) + c + struct.pack('>I', zlib.crc32(c) & 0xffffffff)
raw = b''.join(b'\x00' + bytes((200, 30, 30)) * w for _ in range(h))
data = b'\x89PNG\r\n\x1a\n'
data += chunk(b'IHDR', struct.pack('>IIBBBBB', w, h, 8, 2, 0, 0, 0))
data += chunk(b'IDAT', zlib.compress(raw))
data += chunk(b'IEND', b'')
open(path, 'wb').write(data)
PY

expected_sha=$(sha256sum "$work/probe.png" | cut -d' ' -f1)
# Fresh 128-bit request nonce: the SDK driver seals it into the finding and the
# prompt demands the exact echo (the same protocol as visual-audit-review.py).
request_nonce=$(python3 -c 'import secrets; print(secrets.token_hex(16))')
cat > "$work/prompt.md" <<EOF
This image's bytes SHA-256 is $expected_sha. Reply with a single strict JSON object (no markdown fences):
{"schema":"ralph-visual-audit-review/v1","state_id":"probe","image_sha256":"$expected_sha","role":"probe","model":"{model}","prompt_sha256":"{prompt_sha256}","schema_sha256":"{schema_sha256}","request_nonce":"{request_nonce}","verdict":"pass","observations":[{"code":"PROBE_COLOR","severity":"info","description":"<color>"}]}
The request_nonce field MUST be echoed exactly as provided; do not invent or alter it.
EOF

# Build the SDK driver argv; VISUAL_AUDIT_SDK_DRIVER lets tests substitute a
# deterministic mock (same seam as visual-audit-review.py).
SDK_DRIVER="${VISUAL_AUDIT_SDK_DRIVER:-}"
if [[ -n "$SDK_DRIVER" ]]; then
    SDK_CMD=("$SDK_DRIVER")
else
    SDK_CMD=(node "$SCRIPT_DIR/visual-audit-review-sdk.mjs")
fi

# Run a subprocess under a hard configured timeout. The subprocess runs in its
# own process session/group (setsid when available) so TERM and KILL target
# the whole group, never a global pkill. Returns 124 when the timeout fires.
run_bounded() { # <limit-seconds> <label> <command...>
    local limit=$1 label=$2
    shift 2
    local pid rc waited
    if command -v setsid >/dev/null 2>&1; then
        setsid "$@" >/dev/null 2>&1 &
    else
        "$@" >/dev/null 2>&1 &
    fi
    pid=$!
    waited=0
    while :; do
        if ! kill -0 "$pid" 2>/dev/null; then
            wait "$pid"
            return $?
        fi
        if [[ -r "/proc/$pid/stat" ]] && [[ "$(awk '{print $3}' "/proc/$pid/stat")" == "Z" ]]; then
            wait "$pid"
            return $?
        fi
        if (( waited >= limit )); then
            kill -TERM -- "-$pid" 2>/dev/null || kill -TERM "$pid" 2>/dev/null
            sleep "$MODEL_GRACE"
            kill -KILL -- "-$pid" 2>/dev/null || kill -KILL "$pid" 2>/dev/null
            wait "$pid" 2>/dev/null || true
            echo "visual-audit-probe: $label exceeded the hard ${limit}s timeout; TERM then KILL applied to the whole group and it was reaped" >&2
            return 124
        fi
        sleep 1
        waited=$((waited + 1))
    done
}

# The SDK driver delivers the exact hashed bytes in-memory.
# Schema binding: the frozen committed review schema hash (the generic
# scaffold previously passed a literal "probe", which the real SDK correctly
# rejects; the secure probe receipt binds the real schema digest).
PROBE_SCHEMA_SHA=$(sha256sum "$PROJECT_ROOT/.factory/schemas/visual-audit-review.schema.json" | cut -d' ' -f1)
if run_bounded "$MODEL_TIMEOUT" "vision model" "${SDK_CMD[@]}" \
        --image "$work/probe.png" \
        --expected-sha256 "$expected_sha" \
        --state-id "probe" \
        --role "probe" \
        --prompt-file "$work/prompt.md" \
        --prompt-sha256 "$(sha256sum "$work/prompt.md" | cut -d' ' -f1)" \
        --schema-sha256 "$PROBE_SCHEMA_SHA" \
        --request-nonce "$request_nonce" \
        --model "$MODEL" \
        --out-dir "$work/out"; then
    :
else
    rc=$?
    echo "visual-audit-probe: model subprocess failed (rc=$rc)" >&2
    exit "$rc"
fi

finding="$work/out/finding-probe-probe.json"
[[ -f "$finding" ]] || { echo "visual-audit-probe: no finding produced" >&2; exit 1; }
description=$(python3 - "$finding" <<'PY'
import json, sys
f = json.load(open(sys.argv[1]))
text = " ".join(o.get("description", "") for o in f.get("observations", []))
print(text.lower())
PY
)
case "$description" in
    *red*|*rectangle*|*solid*)
        echo "visual-audit-probe: PASS — model described known image (bytes $expected_sha)"
        ;;
    *)
        echo "visual-audit-probe: FAIL — model did not describe the known image: $description" >&2
        exit 1
        ;;
esac

# Durable secure probe receipt (C3c): the enabled scaffold requires a real,
# non-skipping probe round-trip receipt BEFORE calibration is trusted. Only a
# PASS writes the receipt and its sealed finding artifact; both are regular
# 0600 files under the ignored .factory-state/visual-audit/ directory, written
# atomically, bound to the model, image/prompt/schema hashes, and the current
# framework commit/tree. Tests may only produce a valid receipt by running a
# real mock probe command (never a bypass boolean).
receipt_dir="$PROJECT_ROOT/.factory-state/visual-audit"
mkdir -p "$receipt_dir"
chmod 700 "$receipt_dir"
finding_store="$receipt_dir/probe-finding.json"
cp "$finding" "$finding_store"
chmod 600 "$finding_store"
PROMPT_SHA=$(sha256sum "$work/prompt.md" | cut -d' ' -f1)
COMMIT=$(git -C "$PROJECT_ROOT" rev-parse HEAD)
TREE=$(git -C "$PROJECT_ROOT" rev-parse 'HEAD^{tree}')
python3 - "$receipt_dir/probe-receipt.json" "$MODEL" "$expected_sha" "$PROMPT_SHA" "$PROBE_SCHEMA_SHA" "$finding_store" "$COMMIT" "$TREE" <<'PY'
import hashlib, json, os, sys
path, model, image_sha, prompt_sha, schema_sha, finding_path, commit, tree = sys.argv[1:]
receipt = {
    "schema": "ralph-visual-audit-probe-receipt/v1",
    "commit": commit,
    "tree": tree,
    "model": model,
    "image_sha256": image_sha,
    "prompt_sha256": prompt_sha,
    "schema_sha256": schema_sha,
    "finding_sha256": hashlib.sha256(open(finding_path, "rb").read()).hexdigest(),
    "ok": True,
}
raw = (json.dumps(receipt, indent=2, sort_keys=True) + "\n").encode("utf-8")
temporary = path + "." + str(os.getpid()) + ".tmp"
fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
try:
    os.write(fd, raw)
    os.fsync(fd)
finally:
    os.close(fd)
os.replace(temporary, path)
os.chmod(path, 0o600)
PY
echo "visual-audit-probe: probe receipt written to $receipt_dir/probe-receipt.json"
