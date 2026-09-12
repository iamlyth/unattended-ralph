## Bug Study Report

**Scope:** `/workspace/project` (branch `develop`, 34 commits ahead of origin; tree clean).
**Method:** Ran the canonical gate and inspected the code paths most recently changed (tasks 6/7).
**Bottom line:** **No failing tests, no open bug tickets, no real TODO/FIXME markers, and no code defects found in the recently-touched code.** One operational/environmental defect was found (`build/` stale CMake cache) — already self-healed and guarded by `verify.sh`.

---

### Failing tests

**None.** All 91 tests pass via two independent paths:

- `ctest --test-dir build-planner --output-on-failure --timeout 120` → **91/91 pass**, exit 0.
- `./scripts/verify.sh` (canonical gate) → **91/91 pass** (exit 0), after it rebuilt `build/` from scratch.

### Skipped tests (expected, NOT bugs)

Two tests skip (exit 77) because they require hardware this runner lacks. They are declared capabilities in `.factory/environment.toml`, so this is correct gate behavior, not a regression:

- `#3 test_kernel_controller` — requires `kernel-uinput` (dev-runner-vm).
- `#81 test_backend_smoke` — requires `gpu-compositor` (gpurunner).

Both were verified green on their respective runners earlier in the factory (tasks 3 and 4, recorded as completed in `implementation-plan.md`).

---

### Environmental / operational defect (the one real finding)

**Stale CMake cache pinned to an old project root breaks manual `ctest --test-dir build`.**

- The canonical `build/` directory was originally configured at `/workspace/controller-box`, but the repo now lives at `/workspace/project`. `build/CMakeCache.txt` and `build/CTestTestfile.cmake` still had absolute paths to `/workspace/controller-box` (e.g. `controller-box_SOURCE_DIR:STATIC=/workspace/controller-box`, `CMAKE_HOME_DIRECTORY:STATIC=/workspace/controller-box`).
- Consequence: `ctest --test-dir build` failed en masse with **"Could not find executable /workspace/controller-box/build/<test>"** → `Errors while running CTest` (exit 8). Running `cmake -S . -B build` directly aborts with *"directory ... is different than the directory ... where CMakeCache.txt was created"*.
- This is already **handled by design**: `scripts/verify.sh` (lines 16–32) detects the stale root via `CMAKE_HOME_DIRECTORY` and drops/rebuilds `build/` from scratch. After running verify.sh, `build/CMakeCache.txt` now correctly points at `/workspace/project`, and the tree is clean.
- **Actionable for the planner:** the safe, canonical entry point is `./scripts/verify.sh`, not raw `cmake`/`ctest` against `build/`. If a raw `ctest --test-dir build` must be used, drop `build/` (or reconfigure) first. Avoid committing build directories so a moved checkout doesn't leave stale absolute paths.

---

### Known issues / markers

- **`grep -rn 'TODO\|FIXME\|HACK\|XXX' src/ tests/`** returned only false positives: every hit is `XXXXXX` inside `mkstemp`/`mkstemps` temp-file templates
  (`src/config/config_settings.c:747`, `src/config/config_profile.c:791`, `src/config/config_assignments.c:630`, `src/dbus/ip_create_composite.c:61,72`, `src/manager/service_install.c:398`). **No real TODOs/FIXMEs.**
- `.factory/bugs/open.md` and `.factory/bugs/closed.md` **do not exist** — no open or closed bug tickets on record.

---

### Recently fixed (git log)

Latest work is the closing of an 8-task plan; tasks 1–7 complete, task 8 (final docs/spec audit) **pending**:

- `7ec9295a` factory: task 7 repair — replaced magic numbers with `CBX_OVERLAY_OPACITY_DEFAULT/MIN/MAX` in `src/config/config_settings.{c,h}` (`config_settings.h` adds the constants; `config_settings.c` uses them in defaults/validate/clamp). Added `<math.h>` `isfinite` coverage. No logic change — pure refactor.
- `300daee1` factory: task 7 implementation — added non-finite (NaN/±inf) validation for `overlay_opacity` in `cbx_settings_validate` + `tests/test_settings.c` tests.
- `6d3b7e1f` / `36b24e12` factory: task 6 (repair + implementation) — fixed lossy `"%.2f"` → `"%.17g"` export so opacity round-trips losslessly (`src/config/config_settings.c:627`).
- Earlier: `test_golden` golden files fixed (task 1), flaky acceptance stabilization (task 2), kernel controller D-pad `ABS_HAT0X/ABS_HAT0Y` + first-run-dialog skip (task 3), cross-runner build compat + backend smoke tests (task 4), `test_icon_map` install-state independence (task 5), plus several `verify.sh`/audit efficiency fixes.

---

### Suspected bugs

No concrete code defects found. The most recently changed surface — `overlay_opacity` load/validate/clamp/parse — is checked and is robust:

- `cbx_settings_validate` (`src/config/config_settings.c:157–163`): rejects `!isfinite` and out-of-[MIN,MAX] (returns `-EINVAL`).
- Parse path (`:286–293`): `strtod` with `*endp` validation; **malformed and non-finite values are rejected at parse, leaving the safe default** — so crafted YAML cannot smuggle NaN into the overlay surface alpha (SPEC §7.3).
- Load path (`cbx_settings_load`, `:506–550`): after parse, `clamp_settings` resets non-finite to `CBX_OVERLAY_OPACITY_DEFAULT` and clamps out-of-range finite values to [MIN,MAX].
- Export (`emit_settings_yaml`, `:623–629`): `"%.17g"` is lossless for round-trip.

The two `isfinite`-relevant edge cases in the parse path were both rejected before reaching validation, and the load path clamps defensively — no NaN-persistence hole found. Nothing here warrants a bug ticket.

---

### Recommended action for the planner

1. **Task 8 (final docs/spec audit)** is the only remaining item; it is ready to run with the correct gate `./scripts/verify.sh && git status --porcelain` (verified green here: exit 0, clean tree).
2. **No new bug tasks are warranted** from this study.
3. **Document/commune the operational note** that manual `cmake`/`ctest` against `build/` can break if the checkout is moved; `verify.sh` is the canonical gate and self-heals stale caches. Optionally add this caveat to `AGENTS.md` so roles prefer `verify.sh`.
4. The two hardware-gated tests (`test_kernel_controller`, `test_backend_smoke`) should be exercised on `dev-runner-vm`/`gpurunner` during task 8 for full acceptance; they are correctly skipped on this runner.
