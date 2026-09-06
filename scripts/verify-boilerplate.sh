#!/usr/bin/env bash
set -euo pipefail

# Task 16 F1 (EVID-01, §19): the deterministic verifier entrypoint is
# executed through the retained descriptor authority (the bound inode), so
# the kernel's shebang dispatch replaces the script argument with
# ``/proc/self/fd/N`` and ``BASH_SOURCE[0]`` never names the repository.
# The trusted authority pins the repository root into the child environment
# as FACTORY_VERIFIER_ROOT; when that is absent (direct invocation) the
# legacy ``$0``-derived resolution is used, and when neither resolves the
# verifier fails closed instead of resolving the wrong root.
if [[ -n "${FACTORY_VERIFIER_ROOT:-}" ]]; then
    PROJECT_ROOT=${FACTORY_VERIFIER_ROOT%/}
    SCRIPT_DIR=$PROJECT_ROOT/scripts
else
    SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
    PROJECT_ROOT=$(cd -- "$SCRIPT_DIR/.." && pwd)
fi
for required_marker in scripts/verify-boilerplate.sh .factory/config.toml; do
    [[ -e "$PROJECT_ROOT/$required_marker" ]] || {
        echo "verify: cannot resolve the canonical repository root from " \
            "FACTORY_VERIFIER_ROOT/BASH_SOURCE (missing " \
            "$PROJECT_ROOT/$required_marker)" >&2
        exit 1
    }
done
cd -- "$PROJECT_ROOT"

# The scenario suite exercises isolated temporary repositories and must not
# inherit ambient lifecycle state. The implementation completion gate runs
# this suite with FACTORY_FINAL_GATE_ATTEST=1; leaking the attestation flag,
# attempt/cycle bindings, campaign bindings, or recovery ceilings into nested
# final-gate invocations would make scenario tests attest dirty trees and fail
# spuriously (BUG-0013 regression).
unset FACTORY_FINAL_GATE_ATTEST FACTORY_RALPH_CYCLE_ID FACTORY_RALPH_ATTEMPT_ID \
      FACTORY_RALPH_HISTORY_ID FACTORY_RALPH_HISTORY_OFFSET \
      FACTORY_CAMPAIGN_PHASE FACTORY_CAMPAIGN_ROUND FACTORY_CAMPAIGN_AUDIT_ROUND \
      FACTORY_CAMPAIGN_AUDIT_BASE FACTORY_CAMPAIGN_RUNNER_EVIDENCE_SHA256 \
      FACTORY_CAMPAIGN_OBJECTIVE \
      FACTORY_PLANNING_BASE_COMMIT FACTORY_MAINTENANCE_BASE_COMMIT \
      FACTORY_RALPH_MAX_COMPLETION_RECOVERIES FACTORY_RALPH_MAX_NO_PROGRESS_RECOVERIES \
      FACTORY_RALPH_MAX_STALE_RECOVERIES

mapfile -t SHELL_FILES < <(find scripts tests -type f -name '*.sh' -print | sort)
for file in "${SHELL_FILES[@]}"; do
    bash -n "$file"
done
if command -v shellcheck >/dev/null; then
    shellcheck "${SHELL_FILES[@]}"
else
    echo "verify: warning: shellcheck unavailable" >&2
fi

python3 - <<'PY'
import json, pathlib, subprocess, tomllib
with open('.factory/config.toml', 'rb') as stream:
    config = tomllib.load(stream)
assert config['concurrency']['mutating_workers'] == 1
assert config['concurrency']['integration_workers'] == 1
assert config['git']['allow_worktrees'] is False
assert isinstance(config.get('campaign', {}).get('required_capabilities'), list)
assert all(isinstance(item, str) and item for item in config['campaign']['required_capabilities'])
assert config['verification']['maintenance_command'] == ['./scripts/verify-boilerplate.sh']
assert isinstance(config['verification']['campaign_command'], list)
assert config['verification']['campaign_command']
assert all(isinstance(arg, str) and arg for arg in config['verification']['campaign_command'])
assert config['issues'] == {
    'schema': 'ralph-bug-ledger/v1',
    'open_ledger': '.factory/bugs/open.md',
    'closed_ledger': '.factory/bugs/closed.md',
    'maintenance_plan': '.factory/artifacts/maintenance-plan.md',
    'final_task_title': 'Maintenance verification and documentation audit',
    'providers': ['github', 'forgejo'],
    'external_sync': 'manual',
    'credentials': False,
}
# The tracked test-discovery contract (.factory/verifier-acceptance.json,
# schema ralph-verifier-acceptance/v1) names exactly the gates the verifier
# entrypoint runs. The campaign binding covers the manifest, so adding a gate
# (strict strengthening) auto-rebinds with an audit record instead of halting
# the campaign, while removing a gate or changing the entrypoint requires the
# audited operator pathway (scripts/ralph-verifier-migrate.sh).
manifest = json.load(open('.factory/verifier-acceptance.json', encoding='utf-8'))
assert manifest.get('schema') == 'ralph-verifier-acceptance/v1'
gates = manifest.get('gates')
assert isinstance(gates, list) and gates
for gate in gates:
    assert isinstance(gate, dict) and set(gate) == {'name', 'args'}
    assert isinstance(gate['name'], str) and gate['name'] and '/' not in gate['name']
    assert isinstance(gate['args'], list) and all(isinstance(a, str) and a for a in gate['args'])
    assert (pathlib.Path('tests') / gate['name']).is_file(), f'missing gate {gate["name"]}'
required = [
    'AGENTS.md', '.factory/config.toml', '.factory/environment.toml',
    '.factory/artifacts/implementation-plan.md', '.factory/artifacts/maintenance-plan.md',
    '.factory/artifacts/campaign-audit.md', '.factory/bugs/open.md', '.factory/bugs/closed.md',
    '.factory/prompts/implementation.md', '.factory/prompts/plan.md',
    '.factory/ralph/maintenance.yml', '.factory/ralph/maintenance-plan.yml',
    '.factory/prompts/maintenance.md', '.factory/prompts/maintenance-plan.md',
    'scripts/bug-ledger.py', 'scripts/validate-maintenance-plan.py',
    'scripts/validate-implementation-plan.py', 'scripts/check-scratchpad.sh',
    'scripts/ralph-completion-gate.sh', 'scripts/ralph-supervision.sh',
    'scripts/factory-lock.sh', 'scripts/factory-lock-exec.py',
    'scripts/factory_lock.py', 'scripts/factory_state_io.py',
    'scripts/factory-state-file.py', 'scripts/ralph_lock.py',
    'scripts/ralph-lock-recover.py', 'scripts/repair-scratchpad-handoffs.py',
    'scripts/campaign-verifier-binding.py', 'scripts/ralph-supervision-migrate.py',
    'scripts/ralph-final-state.py', 'scripts/finalize-maintenance-planning.sh',
    'tests/test-git-checkpoint.sh',
    'scripts/git-commit-guard.sh', 'scripts/install-git-commit-guard.sh',
    'scripts/pi-cli-shims/git', 'tests/test-git-commit-guard.sh',
    'scripts/check-installed-functional-evidence.sh',
    'scripts/initialize-plan-cycle.py', 'scripts/check-maintenance-freshness.sh',
    'scripts/maintenance-plan-scope-guard.sh',
    'scripts/ralph-maintenance-plan.sh', 'scripts/ralph-maintenance-run.sh',
    'docs/BUG_WORKFLOW.md', 'tests/test-bug-workflow.sh',
    'tests/test-plan-cycle.sh', 'tests/test-scratchpad-guard.sh',
    'tests/test-ralph-completion-recovery.sh',
    'tests/test-installed-functional-evidence.sh',
    '.factory/environment.toml', '.factory/artifacts/campaign-audit.md', '.factory/ralph/audit.yml',
    '.factory/prompts/audit.md', 'scripts/check-factory-environment.py',
    'scripts/ralph-campaign-state.py', 'scripts/initialize-campaign-audit.py',
    'scripts/validate-campaign-audit.py', 'scripts/campaign-audit-scope-guard.sh',
    'scripts/ralph-audit.sh', 'scripts/ralph-campaign.sh',
    'scripts/ralph-verifier-migrate.sh', '.factory/verifier-acceptance.json',
    'scripts/run-factory-runners.py', 'scripts/check-factory-runner-evidence.py',
    'scripts/factory-runner-server.py', 'scripts/factory_runner_policy.py',
    'scripts/pi2-secure-exec.py',
    'scripts/pi-cli-shims/ralph',
    'tests/test-factory-environment.sh', 'tests/test-factory-runner.sh',
    'tests/test-campaign-audit.sh', 'tests/test-ralph-campaign.sh',
    'tests/test-ralph-campaign-state.py', 'tests/test-factory-lock.py',
    'tests/test-orchestration-security.py',
    'tests/test-ralph-stale-recovery.sh', 'tests/test-ralph-recover-safety.sh',
    'tests/test-scratchpad-recovery-repair.sh',
    'tests/test-maintenance-planning-completion.sh',
    'tests/test-boilerplate-env-isolation.sh',
    'tests/test-pi2-ollama-wrapper.sh', 'tests/test-production-path-bypass.sh',
    'tests/test-visual-audit-sdk-authority.sh',
    '.factory/schemas/conformance.schema.json', '.factory/capability-contracts.json',
    'scripts/validate-conformance.py', 'scripts/check-capability-contracts.py',
    'scripts/check-capability-evidence.py', 'scripts/machine-receipt.py',
    'scripts/check-audit-receipts.py',
    'scripts/validate-blocked-facts.py', 'scripts/check-campaign-objectives.py',
    'scripts/check-golden-policy.py',
    '.factory/visual-audit.toml', '.factory/visual-audit-inventory.json',
    '.factory/visual-audit-calibration.json',
    '.factory/schemas/visual-audit-review.schema.json',
    '.factory/prompts/visual-audit.md',
    'scripts/visual-audit-provenance.py', 'scripts/visual-audit-lease.py',
    'scripts/visual-audit-review.py', 'scripts/visual-audit-review-sdk.mjs',
    'scripts/visual-audit-capture.sh', 'scripts/visual-audit-probe.sh',
    'scripts/visual-capture-driver.sh', 'scripts/check-visual-audit.py',
    'scripts/visual-audit-gate.sh',
    'tests/test-visual-audit.sh',
    'scripts/credential-guard.py',
    'tests/test-credential-guard.sh',
    'tests/test-credential-extension.sh',
    '.factory/artifacts/blocked-facts.json', '.factory/artifacts/conformance.json',
    '.factory/campaign-objectives.json', '.factory/golden-policy.json',
    '.factory/golden-review.json', '.factory/schemas/blocked-facts.schema.json',
    '.factory/schemas/golden-review.schema.json',
    'tests/test-conformance.sh', 'tests/test-capability-contracts.sh',
    'tests/test-audit-receipts.sh',
    'tests/test-blocked-facts.sh', 'tests/test-campaign-objectives.sh',
    'tests/test-golden-policy.sh',
    'tests/test-runner-signer.sh', 'scripts/check-spec-provided.sh',
    'scripts/check-generic-leakage.sh', '.factory/generic-leak-allowlist',
    '.factory/signer-trust.json', '.factory/requirement-policy.json',
    '.factory/campaign-receipt-policy.json',
    '.factory/loop/migration.py', '.factory/ralph-freeze',
    '.factory/tests/test-factory-migration.py',
    '.factory/tests/test-factory-migration.sh',
    '.factory/tests/adversarial-manifest.json',
    '.factory/pre-round-hooks.json',
    '.factory/readiness-policy.json',
    '.factory/schemas/factory-readiness-policy-v1.schema.json',
    '.factory/schemas/factory-readiness-result-v2.schema.json',
    '.factory/schemas/factory-campaign-launch-authority-v1.schema.json',
    '.factory/schemas/factory-state-v2.schema.md',
    '.factory/loop/pre_round.py',
    '.factory/loop/readiness.py',
    '.factory/tests/test-factory-pre-round.py',
    '.factory/tests/test-factory-readiness.py',
    '.factory/tests/test-factory-adversarial.py',
    '.factory/tests/test-factory-adversarial.sh',
    '.factory/tests/test-factory-confinement-order.sh',
    '.factory/loop/installer.py',
    '.factory/loop/pi2_backend.py',
    '.factory/bin/factory-campaign',
    '.factory/bin/factory-launch',
    '.factory/tests/test-factory-installed.py',
    '.factory/tests/test-factory-installed.sh',
    '.factory/loop/generic_evidence.py',
    '.factory/bin/publish-generic-evidence',
    '.factory/tests/test-factory-generic-evidence.py',
    '.factory/tests/test-factory-generic-evidence.sh',
    '.factory/smoke/evidence_smoke_common.py',
    '.factory/smoke/evidence_smoke_driver.py',
    '.factory/smoke/evidence_smoke_gate.py',
    '.factory/smoke/evidence_smoke.py',
    '.factory/tests/test-factory-smoke.py',
    '.factory/tests/test-factory-smoke.sh',
]
for name in required:
    assert pathlib.Path(name).is_file(), f'missing {name}'
# The designated smoke seam, gate, and operator command are tracked
# executables (100755): they execute only from their bound committed
# descriptors through the pinned interpreter, never a PATH-resolved name.
for name in (
    '.factory/smoke/evidence_smoke_driver.py',
    '.factory/smoke/evidence_smoke_gate.py',
    '.factory/smoke/evidence_smoke.py',
):
    entry = subprocess.check_output(
        ['git', 'ls-files', '-s', '--', name], text=True
    ).strip()
    if entry:
        assert entry.split()[0] == '100755', \
            f'{name} must be tracked executable 100755 (got {entry.split()[0]})'
    else:
        mode = pathlib.Path(name).stat().st_mode
        assert mode & 0o111 and not mode & 0o022, \
            f'{name} must be a 0755 executable on disk'
# Task 15 migration: the persisted context-summary authority is removed from
# the tracked tree and unwired from every new-path control step, so the stale
# mirror can never compete with the canonical plan as a task authority.  The
# deprecated visible forwarders may remain (marked, optional) but are never a
# new-path dependency.
assert not pathlib.Path('.factory/artifacts/context-summary.md').exists(), \
    'the stale context-summary mirror must not be tracked'
for script in ('scripts/final-gate.sh', 'scripts/git-commit-hook.sh',
               'scripts/ralph-run.sh'):
    text = pathlib.Path(script).read_text(encoding='utf-8')
    for token in ('check-context-summary', 'ralph-context-summary'):
        assert token not in text, f'{script} still wires the deprecated {token} authority'
for gate in gates:
    assert gate['name'] != 'test-context-summary.sh', \
        'the verifier gate list must not run the deprecated context-summary suite'
# The new migration authority must exist and be stdlib-only; a Ralph runtime
# import in the hidden control plane fails the generic suite.
assert pathlib.Path('.factory/loop/migration.py').is_file()
# Task 15 freeze surface: the tracked marker is a regular file (a symlink is
# never trusted) and every frozen legacy launcher implements the freeze gate
# with the operator-only recovery escape.
migration_text = pathlib.Path('.factory/loop/migration.py').read_text(encoding='utf-8')
assert pathlib.Path('.factory/ralph-freeze').is_file()
marker_info = pathlib.Path('.factory/ralph-freeze').stat()
assert pathlib.Path('.factory/ralph-freeze').is_symlink() is False
for launcher in (
    'scripts/ralph-campaign.sh', 'scripts/ralph-plan.sh',
    'scripts/ralph-run.sh', 'scripts/ralph-audit.sh',
    'scripts/ralph-maintenance-plan.sh', 'scripts/ralph-maintenance-run.sh',
):
    text = pathlib.Path(launcher).read_text(encoding='utf-8')
    assert '.factory/ralph-freeze' in text, f'{launcher} lacks the freeze gate'
    assert 'FACTORY_RALPH_FREEZE_OVERRIDE' in text, \
        f'{launcher} lacks the recovery override escape'
    assert launcher in migration_text, \
        f'the migration freeze authority does not name {launcher}'
# Recovery of an already in-flight legacy cycle is the documented exception,
# not a new launch: ralph-recover.sh must not be frozen.
recover_text = pathlib.Path('scripts/ralph-recover.sh').read_text(encoding='utf-8')
assert 'ralph-recover: note: the legacy Ralph control plane is deprecated' in recover_text
assert '.factory/ralph-freeze' not in recover_text, \
    'ralph-recover.sh must not implement the freeze gate as a new launch'
# The deprecated visible context-summary forwarders fail closed and the stale
# suite marks itself; neither is ever a new-path dependency.
for script in ('scripts/ralph-context-summary.py',
               'scripts/check-context-summary.py'):
    text = pathlib.Path(script).read_text(encoding='utf-8')
    assert 'DEPRECATED' in text and 'Task 15 migration' in text
assert 'DEPRECATED' in pathlib.Path('tests/test-context-summary.sh').read_text(encoding='utf-8')
forbidden_root_factory_files = {
    'PROMPT.md', 'IMPLEMENTATION_PLAN.md', 'MAINTENANCE_PLAN.md',
    'CAMPAIGN_AUDIT.md', 'factory.toml', 'factory-environment.toml',
    'open-bugs.md', 'closed-bugs.md', 'ralph.yml', 'ralph.plan.yml',
    'ralph.audit.yml', 'ralph.maintenance.yml', 'ralph.maintenance-plan.yml',
}
root_files = {path.name for path in pathlib.Path('.').iterdir() if path.is_file()}
assert not (root_files & forbidden_root_factory_files), 'factory files leaked back into repository root'
json.load(open('.pi/subagents.json', encoding='utf-8'))
for path in pathlib.Path('.pi/agents').glob('*.md'):
    text = path.read_text(encoding='utf-8')
    header = text.split('---', 2)[1]
    tools = next(line for line in header.splitlines() if line.startswith('tools:'))
    for forbidden in ('edit', 'write', 'bash'):
        assert forbidden not in tools, f'{path}: read-only agent exposes {forbidden}'
PY

for config in .factory/ralph/implementation.yml .factory/ralph/plan.yml .factory/ralph/audit.yml .factory/ralph/maintenance.yml .factory/ralph/maintenance-plan.yml; do
    grep -q 'parallel: false' "$config"
    grep -q -- '--allow-oversize' "$config"
    grep -q 'ralph-completion-gate.sh' "$config"
done
for launcher in scripts/ralph-run.sh scripts/ralph-plan.sh scripts/ralph-audit.sh scripts/ralph-maintenance-run.sh scripts/ralph-maintenance-plan.sh; do
    grep -q 'factory_lock_bootstrap' "$launcher"
    grep -q 'install-git-commit-guard.sh' "$launcher"
    grep -q 'ralph-supervision.sh' "$launcher"
    grep -q 'ralph_supervision_consume_rejection' "$launcher"
    grep -q 'ralph_supervision_recover_stale' "$launcher"
done
grep -q 'install-git-commit-guard.sh' scripts/ralph-campaign.sh
grep -q 'install-git-commit-guard.sh' scripts/ralph-recover.sh
grep -q '^## Build' AGENTS.md
grep -q '^## Immediate validation' AGENTS.md
(( $(wc -l < AGENTS.md) <= 94 )) || { echo 'verify: AGENTS.md must remain concise (94 lines maximum)' >&2; exit 1; }
grep -q 'Do not assume functionality is missing or complete' .factory/prompts/implementation.md
# False-positive-acceptance redesign: prompts must distinguish real acceptance
# from proxy evidence and require machine-readable conformance evidence.
grep -q 'Pixel/offscreen framebuffer checks are not real visual acceptance' .factory/prompts/implementation.md
grep -q 'not the real system service' .factory/prompts/implementation.md
grep -q 'synthetic producer' .factory/prompts/implementation.md
grep -q 'declaring or asserting evidence is not evidence' .factory/prompts/implementation.md
grep -q 'conformance.json' .factory/prompts/implementation.md
grep -q 'machine-receipt.py --tag' .factory/prompts/implementation.md
grep -q 'requirement-policy.json' .factory/prompts/implementation.md
grep -q 'signer-trust.json' .factory/prompts/implementation.md
grep -q 'out-of-band and non-automatable' .factory/prompts/implementation.md
# Unavailable evidence must be fact-bound; the canonical plan is the sole
# task authority (no persisted context summary competes with it); golden
# baselines are protected by review manifests.
grep -q 'blocked-facts.json' .factory/prompts/implementation.md
grep -q 'golden-policy' .factory/prompts/implementation.md
for role in visual-reviewer runner-reviewer evidence-reviewer spec-reviewer; do
    grep -q 'no runtime-certification authority' ".pi/agents/$role.md"
done
grep -q 'Final documentation and specification audit' .factory/prompts/plan.md
grep -q 'Specification conformance matrix' .factory/prompts/plan.md
grep -q 'conformance.json' .factory/prompts/plan.md
grep -q 'evidence tier' .factory/prompts/plan.md
grep -q 'requirement-policy.json' .factory/prompts/plan.md
grep -q 'out-of-band and non-automatable' .factory/prompts/plan.md
grep -q 'Pixel/offscreen framebuffer checks are not real visual acceptance' .factory/prompts/plan.md
grep -q 'capability-contracts.json' .factory/prompts/plan.md
grep -q 'blocked-facts.json' .factory/prompts/plan.md
grep -q 'Interaction acceptance inventory' .factory/prompts/plan.md
grep -q 'definition of done' .factory/prompts/implementation.md
grep -q 'Maintenance verification and documentation audit' .factory/prompts/maintenance-plan.md
grep -q 'PLAN_COMPLETE.*final non-empty line outside every event tag' .factory/prompts/plan.md
grep -q 'LOOP_COMPLETE.*final non-empty line outside every event tag' .factory/prompts/implementation.md
grep -q 'MAINTENANCE_PLAN_COMPLETE.*final non-empty line outside every event tag' .factory/prompts/maintenance-plan.md
grep -q 'MAINTENANCE_COMPLETE.*final non-empty line outside every event tag' .factory/prompts/maintenance.md
grep -q 'AUDIT_COMPLETE.*final non-empty line' .factory/prompts/audit.md
grep -q 'machine-receipt.py --tag' .factory/prompts/audit.md
grep -q '\[receipt:' .factory/prompts/audit.md
grep -q 'BLOCKED evidence forces' .factory/prompts/audit.md
grep -q 'check-campaign-objectives.py' .factory/prompts/audit.md
grep -q 'campaign-receipt-policy.json' .factory/prompts/audit.md
grep -q 'FACTORY_CAMPAIGN_AUDIT_NONCE' .factory/prompts/audit.md
grep -q 'out-of-band and non-automatable' .factory/prompts/audit.md
grep -q 'signer-trust.json' .factory/prompts/audit.md
grep -q 'blocked-facts.json' .factory/prompts/audit.md
grep -q 'Pixel/offscreen framebuffer checks are not real visual acceptance' .factory/prompts/audit.md
grep -q 'conformance.json' .factory/prompts/audit.md
grep -q 'final-gate.sh --planning' .factory/prompts/plan.md
grep -q 'final-gate.sh --implementation' .factory/prompts/implementation.md
grep -q 'final-gate.sh --campaign-audit' .factory/prompts/audit.md
grep -q 'final-gate.sh --maintenance-planning' .factory/prompts/maintenance-plan.md
grep -q 'final-gate.sh --maintenance' .factory/prompts/maintenance.md
for prompt in .factory/prompts/plan.md .factory/prompts/implementation.md \
        .factory/prompts/audit.md .factory/prompts/maintenance-plan.md \
        .factory/prompts/maintenance.md; do
    grep -q 'emit the completion token' "$prompt"
done
grep -q '^TUI=false$' scripts/ralph-campaign.sh
grep -q -- '--tui)' scripts/ralph-campaign.sh
grep -q 'factory_lock_bootstrap' scripts/ralph-campaign.sh
grep -q 'factory_lock_bootstrap' scripts/ralph-recover.sh
for config in .factory/ralph/plan.yml .factory/ralph/implementation.yml \
        .factory/ralph/audit.yml .factory/ralph/maintenance.yml; do
    checkpoint=$(grep 'command: \["./scripts/check-scratchpad.sh"' "$config")
    [[ $checkpoint == *'_COMPLETE"'* ]] || {
        echo "verify: iteration scratchpad hook must reject its lifecycle token: $config" >&2
        exit 1
    }
    grep -q -- '--final-handoff' "$config"
done
# Maintenance planning defers the strict final handoff to its trusted parent
# launcher, which performs the ledger transition and final checkpoint under
# the retained factory lock; its completion hook validates only.
checkpoint=$(grep 'command: \["./scripts/check-scratchpad.sh"' .factory/ralph/maintenance-plan.yml)
[[ $checkpoint == *'MAINTENANCE_PLAN_COMPLETE"'* ]] || {
    echo "verify: maintenance-planning scratchpad hook must reject its lifecycle token" >&2
    exit 1
}
grep -q -- '--final-handoff' scripts/ralph-maintenance-plan.sh
grep -q 'finalize-maintenance-planning.sh' scripts/ralph-maintenance-plan.sh
grep -q '.factory/environment.toml' .factory/prompts/plan.md
grep -q '.factory/environment.toml' .factory/prompts/implementation.md
grep -q 'check-factory-runner-evidence.py' .factory/prompts/plan.md
grep -q 'check-factory-runner-evidence.py' .factory/prompts/implementation.md
grep -q 'check-factory-runner-evidence.py' .factory/prompts/audit.md
grep -q 'check-spec-provided.sh' scripts/plan-scope-guard.sh
./scripts/check-generic-leakage.sh
./scripts/check-docs-sync.sh
./.factory/tests/test-factory-footprint.sh
./.factory/tests/test-factory-installed.sh
./.factory/tests/test-factory-generic-evidence.sh
./.factory/tests/test-factory-migration.sh
python3 .factory/tests/test-factory-pre-round.py
python3 .factory/tests/test-factory-readiness.py
./.factory/tests/test-factory-confinement-order.sh
./.factory/tests/test-factory-adversarial.sh
./.factory/tests/test-factory-smoke.sh
# Machine visual-audit scaffold invariants: the generic scaffold is disabled by
# default, defaults no vision model (consumer-configured placeholder), and keeps
# every mutable capture/review/calibration/probe path under the ignored
# .factory-state/visual-audit/ directory.
grep -q '^enabled = false' .factory/visual-audit.toml
grep -q '^vision_model = ""' .factory/visual-audit.toml
for va_key in lease_file capture_dir review_dir; do
    grep -q "^$va_key = \".factory-state/visual-audit/" .factory/visual-audit.toml
done
git check-ignore -q .factory-state/visual-audit/captures/good-main.png
git check-ignore -q .factory-state/visual-audit/reviews/report.json
git check-ignore -q .factory-state/visual-audit/lease
# The visual-audit completion gate is a check-only mechanical step: the
# implementation final gate invokes it, and the gate itself only runs the
# aggregate checker against existing review evidence -- it never invokes the
# capture/probe paths or the review SDK driver / vision model.
grep -q 'scripts/visual-audit-gate.sh' scripts/final-gate.sh
grep -q 'check-visual-audit.py' scripts/visual-audit-gate.sh
if grep -Eq 'visual-audit-(capture|probe)|review-sdk' scripts/visual-audit-gate.sh; then
    echo "verify: visual-audit-gate.sh must never invoke capture/review-sdk/probe" >&2
    exit 1
fi
./scripts/check-factory-environment.py
./scripts/check-capability-contracts.py
./scripts/validate-blocked-facts.py planning .factory/artifacts/blocked-facts.json
./scripts/check-golden-policy.py
cmp -s .github/ISSUE_TEMPLATE/bug_report.md .forgejo/ISSUE_TEMPLATE/bug_report.md
./scripts/bug-ledger.py validate

if git ls-files | grep -E '(^|/)(\.ollama-usage-env|\.env)$' >/dev/null; then
    echo "verify: secret environment file is tracked" >&2
    exit 1
fi
python3 - <<'PY'
import subprocess, urllib.parse
for name in subprocess.check_output(['git', 'remote'], text=True).split():
    url = subprocess.check_output(['git', 'remote', 'get-url', name], text=True).strip()
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme in {'http', 'https'} and (parsed.username or parsed.password):
        raise SystemExit(f'verify: remote {name} embeds credentials; use SSH or a credential helper')
PY

./tests/test-scratchpad-guard.sh
./tests/test-scratchpad-recovery-repair.sh
./tests/test-git-checkpoint.sh
./tests/test-git-commit-guard.sh
./tests/test-ralph-completion-recovery.sh
./tests/test-installed-functional-evidence.sh "$PROJECT_ROOT"
./tests/test-factory-environment.sh
./tests/test-factory-runner.sh
./tests/test-campaign-audit.sh
./tests/test-ralph-campaign.sh
./tests/test-ralph-campaign-state.py
./tests/test-factory-lock.py
./tests/test-orchestration-security.py
./tests/test-maintenance-planning-completion.sh
./tests/test-boilerplate-env-isolation.sh
./tests/test-ralph-stale-recovery.sh
./tests/test-ralph-recover-safety.sh
./tests/test-pi2-ollama-wrapper.sh
./tests/test-production-path-bypass.sh
./tests/test-conformance.sh
./tests/test-capability-contracts.sh
./tests/test-audit-receipts.sh
./tests/test-blocked-facts.sh
./tests/test-campaign-objectives.sh
./tests/test-golden-policy.sh
./tests/test-visual-audit.sh
./tests/test-visual-audit-sdk-authority.sh
./tests/test-credential-guard.sh
./tests/test-credential-extension.sh
./tests/test-runner-signer.sh
./tests/test-boilerplate.sh
echo "verify: boilerplate checks passed"
