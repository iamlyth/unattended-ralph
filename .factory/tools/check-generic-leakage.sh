#!/bin/sh
# Reject adopting-product vocabulary everywhere except the canonical spec's
# three explicitly historical migration statements.
set -eu
# The trusted verifier runs this gate through the retained descriptor
# authority (``/proc/self/fd/<fd>``), so ``$0`` names the fd path, never the
# canonical repository path.  The trusted parent pins the canonical root into
# the child environment as FACTORY_VERIFIER_ROOT exactly like the sibling
# documentation gates; when that is absent (direct invocation) the legacy
# ``$0``-derived resolution is used, and when neither resolves the gate fails
# closed instead of resolving the wrong root.
if [ -n "${FACTORY_VERIFIER_ROOT:-}" ]; then
    root=${FACTORY_VERIFIER_ROOT%/}
else
    root=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)
fi
cd "$root"
python3 - <<'PY'
from pathlib import Path
import re, subprocess, sys
terms = [
    "con"+"troller[-_ ]?box", "input"+"plumber", "org\\."+"shadowblip",
    "way"+"finder", "C"+"BX_[A-Z_]*", "systemd"+"-user", "kernel-"+"u"+"input",
    "physical-"+"controller", "target-"+"consumer", "gpu-"+"compositor",
    "licensed-"+"diagram", r"\bs"+r"dl2?\b", r"\bu"+r"input\b",
]
rx=re.compile("|".join(terms),re.I)
allowed_spec_lines={35,858,971}
failures=[]
names=[x.decode() for x in subprocess.check_output(["git","ls-files","-z"]).split(b"\0") if x]
allowed_forwarders={"scripts/verify-boilerplate.sh","scripts/check-docs-sync.sh","scripts/check-generic-leakage.sh","scripts/ollama-usage-guard.sh"}
visible={x for x in names if x.startswith("scripts/")}
if visible != allowed_forwarders:
    failures.extend(sorted(visible ^ allowed_forwarders))
if any(x.startswith("tests/") for x in names):
    failures.append("visible tests/ contains harness-owned files")
for forwarder in sorted(allowed_forwarders & visible):
    raw=Path(forwarder).read_bytes()
    if len(raw)>1024 or b".factory/" not in raw or b"exec " not in raw:
        failures.append(forwarder+": not a minimal hidden forwarder")
for item in names:
    path=Path(item)
    try: text=path.read_text(encoding="utf-8")
    except (UnicodeError,OSError): continue
    for number,line in enumerate(text.splitlines(),1):
        if rx.search(line) and not (str(path)=="docs/FACTORY-LOOP-SPEC.md" and number in allowed_spec_lines):
            failures.append(f"{path}:{number}")
if failures:
    print("generic-leakage: hidden-boundary or vocabulary violations:",file=sys.stderr)
    print("\n".join(failures[:100]),file=sys.stderr)
    raise SystemExit(1)
print("check-generic-leakage: generic vocabulary boundary passed")
PY
