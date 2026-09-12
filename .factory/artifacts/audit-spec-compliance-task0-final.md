# Spec Compliance Audit — Task 0 (Final Audit)

**Spec audited:** `docs/SPEC.md` (§2–§12)
**State audited:** `develop` @ `47857b73`, working tree clean.
**Verification gate:** `./scripts/verify.sh` — **passes** (91/91 tests, 0 failures; `test_kernel_controller`, `test_backend_smoke` correctly skipped — they are gated to their declared hardware runners dev-runner-vm / gpurunner).

The audit was performed with read-only spec-reviewer sub-agents covering §4/§5/§6–§10 plus direct verification of the headline finding in production source.

---

## BLOCKER

### B-1 — Host Mode "freeze" is bypassed on the production DBus input path

- **Files:** `src/app/overlay_service.c` (`cbx_overlay_input_cb`, ~line 1215–1239); affected test `tests/test_overlay_native.c` (`test_o06b_host_freezes_non_host`, ~line 705).
- **Spec:** §4.4 — "The first controller to press R3 becomes the exclusive host. All other controllers freeze."
- **Description:** When Host Mode is active, the production input callback computes `row_idx` (the row of the controller that actually sent the DBus `InputEvent`) but then dispatches **every** event with `host_row`, not the sender's `row_idx`:

  ```c
  int row_idx = cbx_overlay_input_find_row(device_path, ...);   // sender's row
  ...
  int host_row = cbx_host_mode_get_host_row(ctx->hm);
  int result = cbx_host_mode_handle(ctx->hm, host_row, hm_in, ctx->grid);
  ```

  `cbx_host_mode_handle` implements the freeze gate as `if (row_idx != hm->host_row) return CBX_HM_RESULT_FROZEN;` (see `src/overlay/host_mode.c` lines 72–75). Because `host_row` is always passed, the gate is **never taken in production** for a non-host controller. Consequences in the running overlay:
  - A frozen (non-host) controller's Left/Right moves the host's currently-selected row.
  - A frozen controller pressing R3 **exits Host Mode** (`CBX_HM_RESULT_EXIT`).
  - A frozen controller pressing B **closes the overlay** (`CBX_HM_RESULT_CLOSE`).

  The same pattern is used on the keyboard path (`overlay_service.c` ~line 1282), so both transports are affected.
- **Why tests pass:** `test_o06b_host_freezes_non_host` (and the interaction O08 block) only assert that the *frozen controller's own row column is unchanged* (`get_cur_col(grid, 1) == 1`). In production the mis-dispatched input moves the *host's* row instead, so that row-1 assertion holds trivially and the test never detects the violation. The test asserts an invariant the bug does not break.
- **Recommendation:** Pass the sender's `row_idx` into `cbx_host_mode_handle` in `cbx_overlay_input_cb` (i.e. call `cbx_host_mode_handle(ctx->hm, row_idx, hm_in, ctx->grid)`), so the internal FROZEN gate is exercised. Strengthen the integration assertions so the test proves the freeze: after a non-host controller sends R3/B/direction, assert the host's selected row/slot are unchanged, Host Mode is still active (R3 from a frozen controller must be ignored), and the overlay is still open (B from a frozen controller must be ignored).

---

## WARN

### W-1 — Profile-editor target list is not scoped to the virtual device's capabilities in the running manager
- **Files:** `src/manager/profiles_tab.c` (`cbx_profiles_tab_open_editor`, ~line 1173–1177); `src/manager/profile_editor_list.c` (`cbx_profile_editor_set_dbus`, `cbx_profile_editor_load_capabilities`).
- **Spec:** §5.4 — "Profiles map against the virtual device's capabilities, not the physical controller"; §10.2 lists `Capabilities` for "editor scope".
- **Description:** `cbx_profiles_tab_open_editor` calls `cbx_profile_editor_set_dbus(&tab->editor, dbus_backend, dbus_bus, NULL)` — the `composite_path` argument is `NULL`. `cbx_profile_editor_load_capabilities` only reads device capabilities when `ed->composite_path[0]` is set; with NULL it falls back to the hardcoded `default_targets[]` (keyboard/mouse). So the "Pick Target" list is never capability-scoped in production. The diagram is device-type-mapped (good), but the capability model is not exercised, and there is no production-path evidence for it.
- **Recommendation:** Wire the editor to a valid composite/target path so the target list is populated from the device's capability map, and add a production-path test showing the target list is scoped to (e.g.) gamepad capabilities.

### W-2 — Conflicting `settings.yaml`/controllers tab: virtual-controller types only user-editable for slots 0–3 (of up to 16)
- **Files:** `src/manager/settings_tab.c` (`CBX_ST_SET_VC_TYPE_0..3`, `cbx_settings_tab_edit_up`).
- **Spec:** §5.5 / §7.3 — "number of virtual controllers on startup (**and their types**)".
- **Description:** The type picker is exposed for up to 4 slots while the VC count can be raised to 16; slots 4–15 are silently padded to `xb360` and cannot be edited. This under-serves the stated "count **and types**" requirement at higher counts.
- **Recommendation:** Either raise the per-slot type controls to the full addressable range or explicitly document the 4-slot type-edit limit.

---

## INFO

### I-1 — Stale references to a non-existent conformance sidecar/harness
- **Files:** `.pi/agents/evidence-reviewer.md` (references `.factory/artifacts/conformance.json`, `scripts/validate-conformance.py`, `scripts/final-gate.sh`); such references also persist in `.ralph/*`.
- **Description:** The current factory uses `.factory/artifacts/implementation-plan.md` + `./scripts/verify.sh` (spec §11.2's "complete conformance matrix / no requirement remains partial/missing" is fulfilled via the plan's completed tasks and evidence lines, plus the 91-test suite). The `conformance.json`/validator-script names are leftovers from an earlier harness and do not exist in the tree. Not a functional gap, but the stale docs should be cleaned so audit tooling does not chase nonexistent artifacts.

### I-2 — Flatpak profiles filesystem permission is narrower than the spec's literal text
- **Files:** `packaging/org.shadowblip.ControllerBox.yaml` (~line 52); `tests/test_flatpak_manifest.py` (~line 158).
- **Description:** §9.1 shows `--filesystem=~/.local/share/inputplumber`; the manifest grants only `--filesystem=~/.local/share/inputplumber/profiles`. Functionally sufficient (profiles is the only user-writable subdir; consistent with docs/PACKAGING.md), but a literal §9.1 conformance check would flag it. Optional: document the deliberate narrowing next to the manifest line.

### I-3 — README contains literal `flatpak install flathub ...` commands
- **Files:** `README.md` (~lines 42–43).
- **Description:** §9.1 says do not advertise a Flathub install command for the **app** before publication. The README's `flatpak install flathub org.freedesktop.Sdk//24.08` / `Platform//24.08` are runtime/SDK **prerequisites**, not the app, so §9.1 intent is satisfied and test-enforced. Literal-wording only; no action required unless read strictly.

### I-4 — Desktop-entry install path differs from §9.3 literal text
- **Files:** `CMakeLists.txt` (~line 332); §9.3 lists `~/.local/share/applications/controller-box-manager.desktop`, install puts it in the system `share/applications`.
- **Description:** Standard system-wide placement; not a functional blocker.

---

## Confirmations (no issues found)

- **§4.2 overlay trigger** — default Select+A, registered via `SetInterceptActivation` with `InterceptMode=1` (PASS): implemented + mock-tested in `src/overlay/trigger.c`, `tests/test_trigger.c`. ✅
- **§4.3 Player Mode independence** — per-row callbacks; `tests/test_player_mode.c` `test_independence` verified. ✅
- **§4.5 conflict** — red highlight (`{220,40,40}`) + deterministic lowest-unoccupied-P-slot resolution on exit; real pixel readback in `test_overlay_visual.c`; unit coverage in `test_conflict.c`. ✅
- **§4.9 / §4.10 visual acceptance** — pre-built surface, no on-demand construction; framebuffer `SDL_RenderReadPixels` readback for Player Mode grid, Host Mode, conflict, Unassigned+≥2 columns, model/profile text, and virtual icons; golden comparisons in `test_golden.c`. ✅
- **§2.5** — B closes overlay and sets PASS; overlay hidden, not destroyed; `test_close.c` + `test_overlay_lifecycle.c` (re-activate proved). ✅
- **§10.1 native type fidelity** — production `dbus_client.c` uses typed sd-bus accessors (`u` for InterceptMode, `b`, `as`, `s`); the string-mock is test-only; `tests/test_native_dbus.c` runs the real backend against a private typed sd-bus server. ✅
- **§10.1 operational readiness** — owner check, `Version` read, GetManagedObjects enumeration, typed-property validation, NameOwnerChanged re-enumeration ≤2 s; `test_connection.c`/`test_native_dbus.c`. ✅
- **§6 identification** — `BT:`/`USB:`/`USB:phys:`/`ORDER:` ID prefixes, 4-layer precedence, downgrade handling; `test_identity*.c`. ✅
- **§8 icons** — `DeviceType`→icon mapping from `data/controller-icons.yaml` (no VID:PID table); required custom gap icons (arcade stick, hitbox, steam deck, generic) present; unknown-type fallback = generic + raw label. ✅
- **§9 packaging** — Flatpak manifest permissions, flatpak-run overlay-service install (hostile-ID validated), tarball unit with bounded restart backoff, no app Flathub advertisement; `test_flatpak_manifest.py`, `test_service_install.c`. ✅
- **§11.1 InterceptMode ~50 ms poll** — `IP_INTERCEPT_POLL_INTERVAL_MS 50`; `test_overlay_latency.c`/`test_intercept_poll.c`. ✅

---

## Bottom line

The implementation is generally strong and the full verification gate passes, but **Task 0 cannot be considered complete** until **B-1** (the Host Mode freeze bypass on the production DBus input path) is fixed and the freeze invariant is asserted in an integration test. Fixing B-1 requires a production change in `src/app/overlay_service.c` plus strengthened test assertions in `tests/test_overlay_native.c`; after fixing, re-run `./scripts/verify.sh` and re-run the targeted `test_overlay_native` / `test_overlay_interaction` tests.
