#!/usr/bin/env bash
set -euo pipefail

# The trusted verifier runs this gate through the retained descriptor
# authority (``/proc/self/fd/<fd>``), so ``BASH_SOURCE[0]`` names the fd
# path, never the canonical repository path.  The trusted parent pins the
# canonical root into the child environment as FACTORY_VERIFIER_ROOT exactly
# like verify-boilerplate.sh; when that is absent (direct invocation) the
# legacy ``$0``-derived resolution is used, and when neither resolves the
# gate fails closed instead of resolving the wrong root.
if [[ -n "${FACTORY_VERIFIER_ROOT:-}" ]]; then
    PROJECT_ROOT=${FACTORY_VERIFIER_ROOT%/}
    SCRIPT_DIR=$PROJECT_ROOT/scripts
else
    SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
    PROJECT_ROOT=$(cd -- "$SCRIPT_DIR/../.." && pwd)
fi
for required_marker in .factory/tools/check-docs-sync.sh .factory/config.toml; do
    [[ -e "$PROJECT_ROOT/$required_marker" ]] || {
        echo "docs-sync: cannot resolve the canonical repository root from " \
            "FACTORY_VERIFIER_ROOT/BASH_SOURCE (missing " \
            "$PROJECT_ROOT/$required_marker)" >&2
        exit 1
    }
done
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

# Task 17: machine-checked documentation claims. The operator-facing documents
# must describe the implemented fresh-context Python loop (canonical spec/plan,
# one state file, four role prompts, selection/campaign commands, lock/Landlock/
# process/Git/credential/evidence boundaries, exact finite outcomes, findings
# planner-only, hidden footprint, frozen Ralph migration) and must never
# document a removed authority or a frozen legacy launcher as an operative
# command. The docs gate fails closed when a claim drifts from the code.
python3 - <<'PY'
import pathlib
import re
import subprocess
import sys

ROOT = pathlib.Path('.')
DOCS = {
    'README.md': ROOT / 'README.md',
    'docs/FACTORY.md': ROOT / 'docs/FACTORY.md',
    'docs/OPERATIONS.md': ROOT / 'docs/OPERATIONS.md',
    'docs/BUG_WORKFLOW.md': ROOT / 'docs/BUG_WORKFLOW.md',
    'AGENTS.md': ROOT / 'AGENTS.md',
}
for name in DOCS:
    if not DOCS[name].is_file():
        print(f'docs-sync: missing {name}', file=sys.stderr)
        sys.exit(1)
texts = {name: DOCS[name].read_text(encoding='utf-8') for name in DOCS}


def code_lines(name):
    """Yield non-empty lines inside fenced code blocks."""
    in_block = False
    for line in texts[name].splitlines():
        if line.lstrip().startswith('```'):
            in_block = not in_block
            continue
        if in_block and line.strip():
            yield line


# 1. The removed persisted context-summary authority is never referenced.
for name, text in texts.items():
    for token in ('context-summary.md', 'check-context-summary',
                  'ralph-context-summary'):
        if token in text:
            print(f'docs-sync: {name} references removed authority {token}',
                  file=sys.stderr)
            sys.exit(1)

# 2. No fenced code block documents a frozen legacy Ralph launcher or a
#    removed resume/TUI/continue flag.
LEGACY_LAUNCHERS = (
    'ralph-plan.sh', 'ralph-run.sh', 'ralph-recover.sh', 'ralph-campaign.sh',
    'ralph-audit.sh', 'ralph-maintenance-plan.sh', 'ralph-maintenance-run.sh',
    'ralph-completion-gate.sh', 'ralph-supervision.sh',
)
REMOVED_FLAGS = ('--resume', '--restart', '--tui', '--continue', '--no-tui')
for name in DOCS:
    for line in code_lines(name):
        for token in LEGACY_LAUNCHERS:
            if token in line:
                print(f'docs-sync: {name} documents a deprecated launcher in a '
                      f'command block: {line.strip()}', file=sys.stderr)
                sys.exit(1)
        for flag in REMOVED_FLAGS:
            if flag in line:
                print(f'docs-sync: {name} documents a removed flag in a command '
                      f'block: {line.strip()}', file=sys.stderr)
                sys.exit(1)

# 3. Every repository-path command documented in a code block resolves to a
#    real file (exact test/inspection commands; only fenced command blocks are
#    parsed, so prose mentions of paths are never false-flagged).
PATH_RE = re.compile(r'(?:scripts|\.factory|tests|docs)/[\w./-]+')
for name in DOCS:
    for line in code_lines(name):
        for token in PATH_RE.findall(line):
            token = token.rstrip('.,;:)')
            if not (ROOT / token).exists():
                print(f'docs-sync: {name} documents a missing path '
                      f'{token!r} in a command block', file=sys.stderr)
                sys.exit(1)

# 4. Critical claims are present in the designated documents.
facts = texts['docs/FACTORY.md']
ops = texts['docs/OPERATIONS.md']
bugs = texts['docs/BUG_WORKFLOW.md']
spec = texts['README.md'] + facts
campaign_cli = ('factory.loop.campaign' in spec + ops
                or '.factory/loop/campaign.py' in spec + ops)
launch_cli = 'factory.loop.launch' in facts + ops
release_factory = re.search(r'^## Release\b(.*?)(?=^## |\Z)', facts, re.M | re.S)
claims = (
    ('canonical specification path',
     'docs/FACTORY-LOOP-SPEC.md' in texts['README.md']),
    ('canonical plan path', '.factory/artifacts/implementation-plan.md' in facts + ops),
    ('single control-state file', '.factory-state/factory-loop.json' in facts + ops),
    ('four role prompts', all(f'.factory/prompts/{role}.md' in facts for role in
                              ('planner', 'developer', 'tester', 'auditor'))),
    ('campaign CLI', campaign_cli),
    ('launch CLI', launch_cli),
    ('plan parser authority', '.factory/loop/plan_parser.py' in facts + ops),
    ('deterministic selector', '.factory/loop/selector.py' in facts + ops),
    ('Landlock prerequisite', 'Landlock' in spec),
    ('/proc prerequisite', '/proc' in facts + ops),
    ('openssh prerequisite', 'ssh-keygen' in spec + ops),
    ('freeze marker', '.factory/ralph-freeze' in facts + ops),
    ('freeze override recovery-only', 'FACTORY_RALPH_FREEZE_OVERRIDE' in facts + ops),
    ('findings planner-only payload', 'factory-findings/v1' in ops),
    ('findings planner-only flow', 'planner' in ops and 'findings' in ops),
    ('BUG_WORKFLOW documented', 'docs/BUG_WORKFLOW.md' in facts + ops),
    ('BUG_WORKFLOW fresh planner-only flow',
     "next round's fresh planner only" in bugs and 'factory-findings/v1' in bugs),
    ('BUG_WORKFLOW legacy maintenance frozen',
     'frozen legacy' in bugs and 'ralph-maintenance-run.sh' in bugs),
    ('BUG_WORKFLOW ledger contract',
     '.factory/bugs/open.md' in bugs and '.factory/bugs/closed.md' in bugs),
    ('BUG_WORKFLOW project-supplied verifier',
     'adopting-product supplied' in bugs and '.factory/tools/verify-project.sh' in bugs),
    ('canonical release target',
     'docs/FACTORY-LOOP-SPEC.md' in (release_factory.group(1) if release_factory else '')),
    ('clean-tree state note', 'lifecycle marker is missing' in ops),
    ('verification gate in AGENTS', 'verify-boilerplate.sh' in texts['AGENTS.md']),
    ('docs gate in AGENTS', 'check-docs-sync.sh' in texts['AGENTS.md']),
    ('adversarial suite in AGENTS', 'test-factory-adversarial.sh' in texts['AGENTS.md']),
    ('exact finite campaign outcomes', all(o in facts + ops for o in (
        'success', 'findings', 'blocked', 'failed',
        'infrastructure_failure', 'interrupted'))),
)
for label, ok in claims:
    if not ok:
        print(f'docs-sync: missing documentation claim: {label}', file=sys.stderr)
        sys.exit(1)

# 5. The canonical release instructions never name the adopting-product
#    placeholder (`docs/SPEC.md`) as the update target: the release section
#    of every document must stay consistent with the bound canonical
#    specification (`docs/FACTORY-LOOP-SPEC.md`).
for name in DOCS:
    match = re.search(r'^## Release\b(.*?)(?=^## |\Z)', texts[name], re.M | re.S)
    if match and 'docs/SPEC.md' in match.group(1):
        print(f'docs-sync: {name} release section contradicts the canonical '
              f'specification: docs/SPEC.md is the adopting placeholder and '
              f'never the release update target (docs/FACTORY-LOOP-SPEC.md '
              f'is)', file=sys.stderr)
        sys.exit(1)

# 6. Full gate: the boilerplate verifier runs this documentation gate.
verify = (ROOT / '.factory/tools/verify-boilerplate.sh').read_text(encoding='utf-8')
if 'check-docs-sync.sh' not in verify:
    print('docs-sync: .factory/tools/verify-boilerplate.sh does not run '
          'check-docs-sync.sh', file=sys.stderr)
    sys.exit(1)

# 7. Canonical plan: parseable under the committed parser, base an ancestor
#    of HEAD (ancestry), and recorded spec blob equal to the committed spec
#    at HEAD (spec blob).
plan_text = (ROOT / '.factory/artifacts/implementation-plan.md').read_text(encoding='utf-8')
front = {}
plan_lines = plan_text.splitlines()
if not plan_lines or plan_lines[0].strip() != '---':
    print('docs-sync: canonical plan has no metadata front matter', file=sys.stderr)
    sys.exit(1)
for line in plan_lines[1:]:
    if line.strip() == '---':
        break
    if ':' in line:
        key, value = line.split(':', 1)
        front[key.strip()] = value.strip().strip('"\'')
for key in ('spec_path', 'spec_blob', 'base_commit'):
    if not front.get(key):
        print(f'docs-sync: canonical plan lacks front-matter {key}', file=sys.stderr)
        sys.exit(1)
try:
    subprocess.run(
        [sys.executable, '.factory/loop/plan_parser.py', 'parse',
         str(ROOT / '.factory/artifacts/implementation-plan.md')],
        capture_output=True, text=True, timeout=60, check=True)
except (subprocess.CalledProcessError, OSError) as exc:
    print(f'docs-sync: canonical plan does not parse: {exc}', file=sys.stderr)
    sys.exit(1)
ancestor = subprocess.run(
    ['git', 'merge-base', '--is-ancestor', front['base_commit'], 'HEAD'],
    capture_output=True, text=True, timeout=30)
if ancestor.returncode != 0:
    print('docs-sync: plan base_commit is not an ancestor of HEAD', file=sys.stderr)
    sys.exit(1)
blob = subprocess.run(
    ['git', 'rev-parse', f'HEAD:{front["spec_path"]}'],
    capture_output=True, text=True, timeout=30)
if blob.returncode != 0 or blob.stdout.strip() != front['spec_blob']:
    print('docs-sync: plan spec_blob does not match the committed '
          'specification at HEAD', file=sys.stderr)
    sys.exit(1)

# 8. State inspection on a clean tree is honestly reported as no lifecycle
#    state (never a traceback), and the docs say so.
state_show = subprocess.run(
    [sys.executable, '.factory/loop/state.py', '--root', str(ROOT), 'show'],
    capture_output=True, text=True, timeout=30)
if state_show.returncode != 0:
    if 'Traceback' in state_show.stderr or not state_show.stderr.strip():
        print('docs-sync: state.py show fails without an honest no-state '
              'message', file=sys.stderr)
        sys.exit(1)

# 9. Installed help/usage: every operator entrypoint exposes the documented
#    subcommands in its own --help output (the help text is the installed
#    interface the docs must stay synchronized with).
HELP_SUBCOMMANDS = {
    '.factory/loop/campaign.py': ('run', 'show'),
    '.factory/loop/launch.py': ('launch', 'excerpt'),
    '.factory/loop/state.py': (
        'init', 'show', 'digest', 'recover', 'advance', 'begin-attempt',
        'record-retry', 'record-phase-digest', 'verify-phase-digest',
    ),
    '.factory/loop/migration.py': ('derive', 'status', 'freeze', 'migrate'),
    '.factory/loop/plan_parser.py': (
        'parse', 'dump', 'serialize', 'roundtrip', 'transitions',
    ),
    '.factory/loop/selector.py': ('select',),
}
for script, subcommands in HELP_SUBCOMMANDS.items():
    try:
        help_text = subprocess.run(
            [sys.executable, script, '--help'], capture_output=True,
            text=True, timeout=30, check=True,
        ).stdout
    except (subprocess.CalledProcessError, OSError) as exc:
        print(f'docs-sync: cannot read {script} --help: {exc}', file=sys.stderr)
        sys.exit(1)
    for subcommand in subcommands:
        if subcommand not in help_text:
            print(f'docs-sync: {script} --help lacks documented subcommand '
                  f'{subcommand!r}', file=sys.stderr)
            sys.exit(1)

print('docs-sync: documentation change gate passed')
PY
