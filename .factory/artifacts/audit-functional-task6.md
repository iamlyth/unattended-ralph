# Functional Audit — Task 6: Resolve orphaned study-report files

**Date:** 2026-09-12
**Branch:** `develop`
**Scoped commits:** `6cdf52da` … `f4d66d96` (`git status` clean at audit time)

## Verdict

One **BLOCKER**, two **WARN** items, no fatal build/test breakage. The
primary acceptance (clean `git status --porcelain`, no untracked files at
repo root, study artifacts relocated into `.factory/artifacts/`) **is met**,
the build is clean, and the full suite passes (91/91, 2 per-runner skips).
However, task 6's own mandate — *"keep git status clean"* and *"resolve
orphaned files"* — was not fully carried out: the task left behind a
committed orphaned test file that references a deliberately-reverted feature,
and the task's own evidence falsely claims that file was cleaned. This is
squarely in scope for the task and must be fixed before task 6 is considered
complete.

---

## What I verified (real commands, real exit codes)

### Build
`nix-shell --run 'rm -rf build && cmake -B build -S . -DCMAKE_BUILD_TYPE=Debug && cmake --build build -j$(nproc)'`
→ **exit 0**, **0 warnings**, **0 errors** in the full build log.

- The `sysprof-capture-4 / glib-2.0 not found` message during configure is a
  benign side-effect of SDL pkg-config probing; glib is **not** referenced in
  `CMakeLists.txt` or any `src/` source. Not an issue.

### Tests
`ctest --test-dir build --output-on-failure --timeout 120`
→ first run **3 failures** (`test_installed_smoke`, `test_installed_diagram`,
`test_installed_binary`); on an isolated run **4/4 passed**; on a clean full
re-run **100% passed, 0 failed of 91** (exit 0). See WARN-2.

### Clean tree / orphan check
- `git status --porcelain -uall` → empty (exit 0). ✔
- `git ls-files --others --exclude-standard` → **0** untracked files. ✔
- `git ls-files --others --exclude-standard | grep -v '^\.factory/'` → empty
  (no untracked files at repo root). ✔
- The two study reports `architecture-study.md` and `subsystem-ui-report.md`
  are tracked under `.factory/artifacts/` and absent from the repo root.
  Moved, not discarded; content preserved. ✔

---

## Findings

### 1. **BLOCKER** — Orphaned, committed, never-built test file left in the tree

**Files:** `tests/test_settings_controllers_sync.c`

**Description:** Task 6 was created to resolve orphaned files, yet it leaves
behind exactly the class of artifact it was meant to eliminate. A 161-line
regression test ("Eliminates the stale dual-settings-copy problem…") was
written in `6cdf52da` against the `src/manager` settings-tab save-path fix.
That `src/manager` fix was **deliberately reverted** in `db20d2bd`
(`cbx_manager_settings_saved`, `cbx_settings_tab_set_saved_callback`, the
`on_saved` fields, and its CTest registration in `tests/CMakeLists.txt` were
all removed). But the test **source file itself was never reverted**.

Confirmed state:
- `git ls-files` → `tests/test_settings_controllers_sync.c` is **tracked**.
- `grep -n "test_settings_controllers_sync" tests/CMakeLists.txt` → **no
  matches**; there is no `add_executable`/`add_test` anywhere for it. All test
  registrations are explicit (no `file(GLOB)`), so this is not a glob miss.
- `find build -name '*settings_controllers*'` → **nothing**; it is never
  compiled, linked, or run. CTest's 91-test count confirms it contributes zero
  coverage.
- Its assertions depend on behavior that no longer exists: it drives the
  Settings-tab save and asserts `mgr.settings.virtual_controllers.count == 2`
  immediately after save, which only holds because the reverted `on_saved` hook
  re-loaded `mgr->settings`. With that hook gone, `cbx_settings_tab_save` no
  longer reloads the manager's object ($ the only `cbx_settings_load` calls in
  the save path reload `tab->settings`, not `mgr->settings`). **If this test
  were wired into the build today it would FAIL** — it asserts a feature that
  was deliberately removed.

**Why it is blocking:** The task's own acceptance is *"resolve orphaned
files"*; AGENTS.md forbids leaving "placeholders, stubs… or test-only
production bypasses" and requires "Do not silently delete valuable evidence;
preserve…". This is a silent dead artifact referencing a reverted, nonexistent
behavior, and task 6's repair evidence **falsely claims** it was cleaned —
`.factory/artifacts/audit-findings.md` line 169 says *"no residual
test_settings_controllers_sync … remains"*, but the file is present and
tracked. The task cannot be considered complete while it ships a committed
unbuilt test that would fail if integrated.

**Recommendation:** Delete the file (`git rm tests/test_settings_controllers_sync.c`).
It documents behavior that was deliberately reverted, so option (a) — rewiring
it — would mean re-implementing the reverted feature, which the task already
decided against. Deletion is the correct, in-scope resolution and matches the
task's clean-tree mandate. If any future task re-introduces the
settings-save-propagation feature, the test can be restored alongside it.
Correct the audit evidence to say the test was deleted, not that it was absent.

---

### 2. **WARN** — Installed-path integration tests are flaky under a full serialized run

**Files:** `tests/CMakeLists.txt` (lines ~80–135), `tests/test_installed_smoke.sh`,
`tests/test_installed_diagram.sh`, `tests/test_installed_binary.sh`

**Description:** One full `ctest` run reported all three `test_installed_*`
tests as **Failed**, yet each passes in isolation (4/4) and on a clean full
re-run the whole suite passes (exit 0). The CMakeLists already calibrates
`TIMEOUT 240` for these (SPEC §11.2.5) citing cold-cache-install-under-load as
the cause of spurious first-run TIME OUTs. This is a genuine reliability
defect in the `./scripts/verify.sh` gate (which runs the full suite): the
`test_installed_*` tests are order/load-sensitive (they perform real installs
to staging prefixes, then exercise the installed binary under Xvfb), so a
loaded or cold-cache run can produce a false negative.

This is **not introduced by task 6** (task 6's only Net change to
`tests/CMakeLists.txt` removed the orphaned test; the installed tests' source,
scripts, and CMake blocks are untouched by task 6), which is why it is a WARN
and not a BLOCKER for this task.

**Recommendation:** Track this independently (it pre-dates task 6). Make the
installed tests robust to first-run load — e.g. verify they only fail on the
*first* serialized invocation, add a retry inside the `.sh` harness, or pin
`RUN_SERIAL` ordering so the build/install heavy tests run when the machine is
quiet. Do not weaken assertions to hide it; the flakiness is environmental.

---

### 3. **INFO** — Rework churn / the "restore tree" revert decision is not recorded in the plan

**Files:** `.factory/artifacts/implementation-plan.md` (Task 6 entry),
commit history (`6cdf52da` adding source+tests, `db20d2bd` reverting them)

**Description:** Task 6's "implementation" commit added a full source feature
(settings-save propagation) plus a regression test, which a later commit then
wholly reverted. The final plan evidence only describes the relocation
outcome; it does not record the explicit "feature was implemented then
deliberately reverted" decision. That missing record is the root cause of
finding 1 (the reverted implementation's test was dropped from the build
instead of deliberately deleted) and makes the state look inconsistent to any
future reader or task.

**Recommendation:** Add a short "reverted in scope" note to the Task 6 evidence
so future tasks do not re-synthesize the reverted change, and run an explicit
orphan-audit (any committed file with no build registration) as part of the
clean-tree gate.

---

## Not in scope / verified clean

- **No logic errors, off-by-ones, null-derefs, or use-after-free** in the task's
  net change: the only production change (manager/settings-tab) was fully
  reverted, and the final resolution touches `.md` artifacts only. `gcc
  -fsyntax-only` on the dead test reaches SDL includes (no logic defect to
  report beyond finding 1).
- **Integration:** No DBus/header contract changed by the final resolution; no
  dangling references to the old root-level study-report filenames in `src/`,
  `tests/`, `CMakeLists.txt`, `scripts/`, or `docs/`.
- **Edge cases:** N/A — final change is documentation relocation.
- glib/sysprof configure message is benign (see build section).

## Summary

| # | Severity | Finding |
|---|----------|---------|
| 1 | **BLOCKER** | `tests/test_settings_controllers_sync.c` — committed, never built/run, asserts a deliberately-reverted feature; would fail if wired in; task's evidence falsely claims it was cleaned. Delete the file. |
| 2 | WARN | `test_installed_*` flaky under full serialized run (pass in isolation & re-run); environmental, pre-dates task 6. |
| 3 | INFO | Revert decision in task 6 not recorded in plan; root cause of finding 1. |

**Exit code:** 1 (one BLOCKER must be resolved before task 6 is complete).
