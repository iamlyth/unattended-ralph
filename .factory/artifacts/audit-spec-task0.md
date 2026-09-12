# Spec Compliance Audit — Task 0 (Final audit)

Auditor: spec-compliance-auditor
Target: implementation vs. `docs/SPEC.md` (canonical spec binding = `HEAD`, per `.factory/artifacts/implementation-plan.md`)
Gate: `./scripts/verify.sh` (task verification + `[project].spec` acceptance)

## Method

- Read `docs/SPEC.md` in full (§1–§A).
- Read `.factory/artifacts/implementation-plan.md` (task ledger, status `active`).
- Mapped spec requirement areas to implementing source modules (`src/`, `tests/`).
- Ran the canonical acceptance gate `./scripts/verify.sh` from a clean environment.
- Ran targeted reproductions of the installed-package tests to separate genuine
  defects from environment/state artifacts.

## 1. Acceptance-gate result

From a **clean** environment (no stale Xvfb, locks, or staging dirs):

```
nix-shell --run './scripts/verify.sh'   →   EXIT=0
100% tests passed, 0 tests failed out of 91
did not run: 3 - test_kernel_controller (Skipped)   [/dev/uinput absent]
           81 - test_backend_smoke      (Skipped)   [no GPU/compositor backend]
```

Both skips are legitimate missing-capability skips (dev-runner-vm lacks
`/dev/uinput`; `test_backend_smoke_sw` supplies software-renderer evidence).

I separately reproduced that the three installed-binary tests
(`test_installed_smoke`, `test_installed_diagram`, `test_installed_binary`)
**fail** when the environment carries leftover X-server state on a display
(e.g. a stray Xvfb abstract socket on `:90`), and **pass** once that state is
cleared. See WARN-1.

## 2. Spec-to-implementation coverage

Spot-mapped the major normative areas; each is present and exercised by the
suite (`tests/CMakeLists.txt`, 91 tests incl. golden/visual/native/installed):

- §2 / §10 DBus architecture: `src/dbus/` (`ip_objectmanager.c`, `ip_properties.c`,
  `ip_connection.c`, `ip_composite.c`, `dbus_interface.h` vtable + mock backend in
  `tests/dbus_mock.c`, native-signature server in `tests/native_ip_server.c`).
- §4 Overlay (grid, trigger Select+A, player/host mode, conflict, profiles,
  dynamic columns, pre-built surface): `src/overlay/*`, `src/app/overlay_service.c`.
- §4.9/§11 latency: `tests/test_overlay_latency.c`.
- §5 Manager (Controllers/Profiles/Settings tabs, editor list/seq, validation,
  interaction inventory, pointer+controller paths): `src/manager/*`,
  `tests/test_manager_*`, `tests/interaction_inventory.c`.
- §6 Identification: `src/identify/*`.
- §7 Config layer incl. `settings.yaml`/`assignments.yaml`, profile-metadata sidecars,
  InputPlumber `device_profile_v1` YAML: `src/config/*`.
- §8 Icons / mapping table: `src/icons/*`, `data/controller-icons.yaml`,
  installed `controller-icons.yaml` (asserted in `test_installed_diagram.sh`).
- §9 Packaging: `packaging/`, installed-desktop/service files asserted by
  installed-package tests; `tests/test_packaging*.sh`, `test_installed_*.sh`.
- §11.1 rendering/evidence layers: golden (`test_golden`), framebuffer
  (`fb_assert`), backend smoke (`test_backend_smoke`, `_sw`), installed
  functional (`test_installed_functional`), interaction/controller paths.

The production code builds cleanly, and the conformance of the functional
spec surface is supported by passing acceptance evidence. The findings below do
**not** call into doubt the functional correctness of the shipped code; they are
completion/hygiene conditions that the definition-of-done explicitly requires and
that are currently **not satisfied**.

## Findings

### BLOCKER-1 — Git tree on `develop` is not clean (task acceptance + SPEC §11.2.9 not met)

- **Files**:
  - `.factory/artifacts/study-bugs.md` (modified)
  - `.factory/artifacts/subsystem-study-dbus.md` (untracked)
  - `docs/subsystem-overlay.md` (untracked)
  - `.factory/artifacts/audit-task0-functional.md` (untracked, generated during the audit campaign)
- **Severity**: BLOCKER
- **Description**: Task 6's acceptance and the final-audit task 0 acceptance
  require "the Git tree is clean on develop" (task 6 verification is literally
  `./scripts/verify.sh && git status --porcelain`). SPEC §11.2.9 (repository
  integrity) lists "the Git tree is clean on `develop`" as a completion
  prerequisite. `git status --porcelain` is currently **non-empty**, so the
  stated completion criterion is false. Task 6's recorded evidence
  ("Git tree clean on develop") does not match the current tree.
- **Recommendation**: before declaring task 0 complete, either commit the
  study/audit artifacts or add `.gitignore` patterns that cover these **nested**
  paths (existing patterns `/*SUBSYSTEM*.md` and `/*subsystem*study*.md` only
  match repo-root paths, so they do not ignore `.factory/artifacts/subsystem-study-dbus.md`,
  `docs/subsystem-overlay.md`, or the audit output). Confirm `git status --porcelain` is empty on `develop`.

### WARN-1 — Installed-package Xvfb tests are flaky under stale X-server state (SPEC §11.1.5 / §11.2.5)

- **Files**: `tests/test_installed_smoke.sh`, `tests/test_installed_binary.sh`,
  `tests/test_installed_diagram.sh` (shared display allocator pattern).
- **Severity**: WARN
- **Description**: The display allocator selects `:NN` by checking only the
  **filesystem** socket `/tmp/.X11-unix/XNN` and lock file. Xvfb also binds a
  kernel **abstract** socket (`@/tmp/.X11-unix/XNN`). When an earlier/interrupted
  run leaves a server whose abstract socket is still being released, the
  allocator deems `:90` free, launches Xvfb, and Xvfb aborts with
  "server already running" — failing the test. Reproduced during this audit:
  with a stray `:90` server present, these 3 tests FAIL; from a clean tree
  they PASS and the full gate is green. This is a "flaky rerun dependency"
  prohibited by SPEC §11.2.5 and undermines the §11.1.5 installed-smoke
  determinism gate.
- **Recommendation**: on Xvfb start failure, advance to the next display and
  retry instead of failing; and/or confirm the server survived past bind
  (`xdpyinfo -display :NN`) before proceeding. Low functional impact (harness
  only), but it makes the acceptance gate sensitive to prior run state.

### WARN-2 — Recorded evidence overstates the gate result vs. observed state (SPEC §11.2.8 / §11.2.6)

- **Files**: `.factory/artifacts/implementation-plan.md` (task 6 "Evidence"),
  `.factory/artifacts/study-bugs.md`.
- **Severity**: WARN
- **Description**: Task 6 evidence states "91/91 tests, 0 failures … Git tree
  clean on develop." The actual clean run is **0 failures with 2 legitimate
  hardware skips** (90 passed + 2 skipped), and the tree is currently unclean
  (BLOCKER-1). SPEC §11.2.8 requires final evidence to record exact commands
  and results; §11.2.6 requires known-defect accounting. The recorded prose does
  not match the terminal state.
- **Recommendation**: refresh task-0 audit evidence to state the exact clean-run
  result (e.g. "90 passed, 0 failed, 2 hardware skips") and record WARN-1 in
  the open-defect ledger rather than claiming a universally-green run.

## Conclusion

The production implementation is functionally sound: it builds cleanly, the full
acceptance gate `./scripts/verify.sh` **passes from a clean environment**
(0 failures; 2 legitimate hardware skips), and the spec's functional surface
(overlay, manager, DBus, identification, config, icons, packaging, visual and
interaction evidence) is implemented and exercised.

**However, the task cannot yet be declared complete** because an explicit
completion acceptance is unmet (BLOCKER-1: the Git tree on `develop` is not clean,
which both the task acceptance and SPEC §11.2.9 require). WARN-1 and WARN-2
should be addressed for determinism and evidence fidelity.
