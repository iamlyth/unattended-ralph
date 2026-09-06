#!/usr/bin/env -S python3 -I
"""Validate the independent, explicitly non-canonical extension registry."""
import json
from pathlib import Path
import re
import sys
root=Path(__file__).resolve().parents[2]
try:
    registry=json.loads((root/'.factory/extension-requirements.json').read_bytes())
    sidecar=json.loads((root/'.factory/artifacts/extension-conformance.json').read_bytes())
    ids=[x['id'] for x in registry['requirements']]
    rows=[x['id'] for x in sidecar['requirements']]
    assert registry['schema']=='factory-extension-requirements/v1'
    assert sidecar['schema']=='factory-extension-conformance/v1'
    assert ids==rows and len(ids)==len(set(ids))
    assert all(re.fullmatch(r'EXT-[A-Z0-9-]+-\d\d',x) for x in ids)
    assert all(x['classification'] in {'verified','partial','blocked'} for x in sidecar['requirements'])
except (OSError,ValueError,KeyError,AssertionError,TypeError) as exc:
    print(f'extension-conformance: invalid: {exc}',file=sys.stderr);raise SystemExit(1)
print('extension-conformance: registry and sidecar agree')
