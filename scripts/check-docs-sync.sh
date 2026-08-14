#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
PROJECT_ROOT=$(cd -- "$SCRIPT_DIR/.." && pwd)
cd -- "$PROJECT_ROOT"

BASE=$(python3 - <<'PY'
lines = open('.factory/artifacts/implementation-plan.md', encoding='utf-8').read().splitlines()
for line in lines:
    if line.startswith('base_commit:'):
        print(line.split(':', 1)[1].strip().strip('"\''))
        break
PY
)
[[ -n "$BASE" && "$BASE" != UNPLANNED ]] || { echo "docs-sync: plan has no valid base_commit" >&2; exit 1; }
git cat-file -e "$BASE^{commit}" 2>/dev/null || { echo "docs-sync: unknown base commit '$BASE'" >&2; exit 1; }

mapfile -t CHANGED < <({
    git diff --name-only "$BASE"
    git ls-files --others --exclude-standard
} | sort -u)
product_changed=false
docs_changed=false
for path in "${CHANGED[@]}"; do
    case "$path" in
        README.md|docs/*) docs_changed=true ;;
        .factory/artifacts/implementation-plan.md|.ralph/*|.pi/*) ;;
        *) product_changed=true ;;
    esac
done

if $product_changed && ! $docs_changed; then
    echo "docs-sync: implementation changed since planning but README/docs did not" >&2
    exit 1
fi

echo "docs-sync: documentation change gate passed"
