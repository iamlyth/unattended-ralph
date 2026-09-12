# Spec Compliance Audit — Task 0 (Final audit)

Auditor: spec-compliance auditor
Target: implementation vs. `docs/SPEC.md` (canonical binding = `HEAD` on `develop`)
Gate: `./scripts/verify.sh`

## Method

- Read `docs/SPEC.md` in full (§1–§13, Appendices).
- Read the task ledger `.factory/artifacts/implementation-plan.md` (all tasks 1–6 `completed`).
- Mapped normative spec sections to implementing modules and their tests.
- Ran the canonical acceptance gate `./scripts/verify.sh` from the current tree.
- Verified the repository-integrity conditions in SPEC §11.2 (definition of done).

## Acceptance-gate result

`./scripts/verify.sh` → **EXIT=0** from the current tree:

```
100% tests passed, 0 tests failed out of 91
Total Test time (real) = 170.37 sec
The following tests did not run:
      3 - test_kernel_controller (Skipped)   [no /dev/uinput on dev-runner-vm]
     81 - test_backend_smoke        (Skipped) [no GPU/compositor backend]
```

All four installed-package tests (`test_installed_smoke`, `test_installed_diagram`,
`test_installed_binary`, `test_installed_functional`) **passed** — the Xvfb
stale-display flakiness flagged in prior audits is resolved by the dynamic Xvfb
display-allocation fix. The two skips are legitimate missing-capability skips on
`dev-runner-vm`.

## Spec→implementation conformance (functional surface)

Spot-verified against the implementation; the shipped code is functionally
spec-compliant and the acceptance evidence is green:

- §2.3 single binary, two modes: `./build/controller-box --help` enumerates
  `--overlay-service` and `--manager`.
- §5.4 / §7.6 profile format + NES minimum: `data/profiles/default.yaml` is a valid
  InputPlumber `DeviceProfile` binding A, B, and D-Pad Up/Down/Left/Right with an
  exact `device_profile_v1` schema.
- §8.4 icon mapping table: `data/controller-icons.yaml` follows the `virtual_types`
  shape and unknown-type fallback documented in the spec.
- §2/§10 DBus, §4 Overlay, §5 Manager, §6 Identification, §7 Config, §9 Packaging,
  §11.1 evidence layers: present and exercised by the 91-test suite
  (golden/visual/framebuffer/native-signature/installed/interaction coverage), all green.

The findings below are **completion/hygiene** conditions the definition of done
makes mandatory, not defects in the shipped product code.

## Findings

### BLOCKER-1 — Git tree on `develop` is not clean (SPEC §11.2.9; task 6 acceptance) — RECURRING

- **Files**:
  - `.gitignore` (incomplete coverage)
  - Untracked, not-ignored artifacts currently in the tree:
    - `docs/security-audit-final.md`
    - `docs/subsystem-ui-report.md`
    - `src/identify/SUBSYSTEM-STUDY.md`
- **Severity**: BLOCKER
- **Description**: SPEC §11.2.9 (repository integrity) lists "the Git tree is clean
  on `develop`" as a completion prerequisite, and task 6's verification is literally
  `./scripts/verify.sh && git status --porcelain`. `git status --porcelain` is
  **non-empty** — three untracked artifacts are present and NOT-IGNORED
  (`git check-ignore` returns nothing for all three). The prior audit's BLOCKER-1
  was partially addressed by commit `be244b34` (which removed tracked study/audit
  files and added `.gitignore` patterns), but the ignore patterns only cover
  root-anchored `/*SUBSYSTEM*.md` / `/*subsystem*study*.md` and `docs/*subsystem*study*.md`
  (the latter requires the literal token `study` in the filename). Factory study and
  audit subagents write reports to additional uncovered locations:
  - per-subsystem `SUBSYSTEM-STUDY.md` inside source dirs (`src/*/SUBSYSTEM-STUDY.md`);
  - `docs/subsystem-<name>-report.md` (no `study` token);
  - `docs/*-audit-*.md` (e.g. `security-audit-final.md`).
  So the tree is dirty again after every campaign run; the "clean on develop"
  condition cannot currently be achieved. Task 6's recorded evidence ("Git tree
  clean on develop") is false.
- **Recommendation**: broaden `.gitignore` to cover the actual artifact locations, e.g.
  `src/*/SUBSYSTEM-STUDY.md`, `docs/subsystem-*-report.md`, `docs/*-audit-*.md`,
  `docs/SUBSYSTEM-STUDY.md` (and any remaining docs/ study outputs), then have the
  orchestrator commit the ignore change and confirm `git status --porcelain` is empty
  on `develop` before declaring task 0 complete.

### WARN-1 — Recorded final evidence overstates the terminal state (SPEC §11.2.8)

- **Files**: `.factory/artifacts/implementation-plan.md` (task 6 "Evidence")
- **Severity**: WARN
- **Description**: Task 6 evidence states "Git tree clean on develop", which is
  currently false (BLOCKER-1). Its numeric claim ("91/91 tests, 0 failures") is
  technically consistent with the gate ("100% passed, 0 failed" with 2 hardware
  skips), but the repository-integrity clause is not. SPEC §11.2.8 requires final
  evidence to record exact commands and results.
- **Recommendation**: after BLOCKER-1 is fixed, refresh the task-0/final evidence to
  state the exact result (e.g. "0 failures, 2 hardware skips, tree clean") and record
  the current hygiene state rather than re-asserting a universally-green clean tree.

### INFO-1 — Factory subagents emit untracked artifacts into the working tree

- **Files**: `.factory/prompts/study-subsystem.md`, `.factory/roles.toml`,
  `.factory/loop/campaign.py`
- **Severity**: INFO
- **Description**: Confirms BLOCKER-1 is systemic: planning/audit subagents (study
  per-subsystem and specialist-audit runs) write markdown reports into `docs/`,
  `.factory/artifacts/`, and the source subsystem directories during the active
  campaign (`e2e-final-audit`). Until `.gitignore` covers every output location, the
  clean-tree gate will keep regressing every round.
- **Recommendation**: treat the ignore/cleanup as a one-time hygiene fix (BLOCKER-1),
  optionally route report output into an already-ignored directory to avoid future
  regressions.

## Notes (improvements already landed since prior audits)

- **Resolved**: Xvfb stale-display test flakiness (prior WARN) — all four installed
  tests now pass via dynamic display allocation.
- The bug ledger (`.factory/bugs/open.md`) is empty; no open product defect blocks a
  v1 requirement.

## Conclusion

The production implementation is functionally sound and spec-conformant: the full
acceptance gate `./scripts/verify.sh` **passes** (0 failures; 2 legitimate hardware
skips), and the spec's functional surface (overlay, manager, DBus, identification,
config, icons, packaging, visual/interaction/evidence layers) is implemented and
green.

**However, the task cannot be declared complete** because an explicit definition-of-done
condition is unmet (BLOCKER-1: the Git tree on `develop` is not clean — a recurring
condition that the previous fix did not fully close). WARN-1 should be corrected once
it is.
