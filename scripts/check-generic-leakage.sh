#!/usr/bin/env bash
# check-generic-leakage.sh — product-neutrality scan for the generic boilerplate.
#
# The generic boilerplate must never leak the adopting product's identity:
# Controller/InputPlumber/SDL/uinput/product capability names and product path
# structures are forbidden in tracked content. The committed allowlist
# (.factory/generic-leak-allowlist) exempts only a narrow set of exact tracked
# paths; everything else is scanned and any hit fails the gate.
set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
if [[ -n "${FACTORY_VERIFIER_ROOT:-}" ]]; then
    # The gate child executes the committed script through a retained
    # descriptor (`/proc/self/fd/<fd>`), so `BASH_SOURCE[0]` names the fd
    # path, never the canonical repository path.  The trusted parent pins
    # the canonical root instead.
    PROJECT_ROOT=$(realpath -e -- "$FACTORY_VERIFIER_ROOT")
else
    PROJECT_ROOT=$(cd -- "$SCRIPT_DIR/.." && pwd)
fi
cd -- "$PROJECT_ROOT"

ALLOWLIST="$PROJECT_ROOT/.factory/generic-leak-allowlist"

# Product identity terms that must never appear in generic tracked content.
# `sdl`, `uinput`, `controller-box`, `inputplumber`, `shadowblip`, `wayfinder`,
# the CBX_ environment prefix, and the upstream product capability/test names
# are Controller-Box product tokens; `test_installed_functional` is documented
# boilerplate mechanism vocabulary and is intentionally not scanned.
PATTERN=$(python3 - <<'PY'
import re
terms = [
    r"controller[-_ ]?box", r"inputplumber", r"org\.shadowblip", r"wayfinder",
    r"CBX_[A-Z_]*", r"controller-box-vm", r"remote-project-gate",
    r"systemd-user", r"kernel-uinput", r"installed-package",
    r"physical-controller", r"target-consumer", r"controller-production-routing",
    r"gpu-compositor", r"installed-licensed-diagram", r"licensed-diagram",
    r"\bsdl2?\b", r"\buinput\b", r"test_packaging",
]
print("|".join(terms))
PY
)
if [[ -z "$PATTERN" ]]; then
    echo "check-generic-leakage: cannot build the product-term pattern" >&2
    exit 1
fi

[[ -f "$ALLOWLIST" && ! -L "$ALLOWLIST" ]] || {
    echo "check-generic-leakage: tracked allowlist is missing or unsafe: $ALLOWLIST" >&2
    exit 1
}
mapfile -t ALLOWED < <(grep -v '^\s*#' "$ALLOWLIST" | grep -v '^\s*$' | sed 's/^[[:space:]]*//; s/[[:space:]]*$//' || true)

failed=0
while IFS= read -r tracked; do
    [[ -n "$tracked" ]] || continue
    case "$tracked" in
        scripts/check-generic-leakage.sh|.factory/generic-leak-allowlist)
            continue
            ;;
    esac
    if grep -qiE "$PATTERN" "$tracked" 2>/dev/null; then
        if printf '%s\n' "${ALLOWED[@]}" | grep -qxF "$tracked"; then
            continue
        fi
        echo "check-generic-leakage: product-term leak in tracked file: $tracked" >&2
        failed=1
    fi
done < <(git ls-files)

# No tracked product-path skeleton may exist (src/, packaging/, data/, and the
# upstream build files are product layout, not boilerplate).
for product_path in 'src/' 'packaging/' 'data/' 'CMakeLists.txt' 'config.h.in'; do
    if git ls-files | grep -qxF "$product_path" \
        || git ls-files | grep -q "^$product_path"; then
        echo "check-generic-leakage: product path skeleton is tracked: $product_path" >&2
        failed=1
    fi
done

# No production credential/evidence material may ship with the template.
while IFS= read -r tracked; do
    base=${tracked##*/}
    case "$tracked" in
        .factory/tests/fixtures/*|tests/*|scripts/check-generic-leakage.sh) continue ;;
    esac
    if printf '%s\n' "${ALLOWED[@]}" | grep -qxF "$tracked"; then
        continue
    fi
    if [[ $base =~ ^(id_(rsa|ed25519)|authorized_keys|.*\.(pem|key|p12|pfx))$ ]] \
       || grep -qE -- '-----BEGIN (OPENSSH |RSA |EC )?PRIVATE KEY-----|AKIA[0-9A-Z]{16}' "$tracked" 2>/dev/null; then
        echo "check-generic-leakage: credential/key material in tracked file: $tracked" >&2
        failed=1
    fi
done < <(git ls-files)

# Runtime evidence is never enrolled source material.
if git ls-files | grep -qE '^\.factory-state/|(^|/)(manifest\.sig|runner-evidence\.json)$'; then
    echo "check-generic-leakage: tracked runtime credential/evidence material" >&2
    failed=1
fi

[[ $failed -eq 0 ]] || {
    echo "check-generic-leakage: generic boilerplate leaked product content; neutralize or allowlist exactly" >&2
    exit 1
}
echo "check-generic-leakage: no Controller/InputPlumber/SDL/product-path leakage (${#ALLOWED[@]} allowlisted paths)"
