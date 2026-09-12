## Subsystem Study Report: manager

Scope: `/workspace/project/src/manager/` (the SDL2 Manager application, SPEC §5).
The manager is one of several window modes; it is a controller-navigated console
settings menu with three tabs: **Controllers** (§5.2), **Profiles** (§5.3), and
**Settings** (§5.5). It owns its own renderer/window, text cache, theme, focus
chain, all three tabs, and the first-run service-install dialog (§9.1). It is
also the sole entry point to the profile-editor sub-UI and to systemd service
installation.

### File inventory

| File | Role |
|---|---|
| `manager.c` / `manager.h` (51 KB / 8 KB) | Orchestrator: lifecycle, event dispatch, focus, rendering, first-run dialog. |
| `controllers_tab.c/.h` (40 KB) | Controllers tab. DBus-backed device (virtual controller) list + Add/Remove/Change-Type. |
| `profiles_tab.c/.h` (46 KB) | Profiles tab. Filesystem profile list + create/delete + editor launch; mode machine incl. confirm-quit. |
| `settings_tab.c/.h` (22 KB) | Settings tab. Editable setting rows + Save button (settings.yaml). |
| `service_install.c/.h` (22 KB) | systemd user service install/uninstall (first-run §9.1). Fork/exec hardened. |
| `profile_editor_list.c/.h` (41 KB) | Profile editor (list mode): diagram + binding list, target-pick, capture. Owns shared `cbx_profile_editor`. |
| `profile_editor_seq.c/.h` (11 KB) | Editor sequential ("all buttons") binding mode. |
| `profile_diagram.c/.h` (29 KB) | SVG controller diagram widget with highlightable buttons + device-mapped catalogs (BUG-0018). |
| `profile_save.c/.h` (8 KB) | Security+validation wrapper around low-level profile save. |
| `profile_validate.c/.h` (4 KB) | NES-minimum binding validation gate. |

### 1. Entry points (called from outside the subsystem)

All public functions are declared in the `.h` files; callers are `src/app`
(manager mode entry) and tests. The key external surface:

- **Lifecycle**: `cbx_manager_init(mgr, font_path)`,
  `cbx_manager_init_with_dbus(mgr, font_path, backend, bus)` (test backend
  injection), `cbx_manager_run`, `cbx_manager_stop`, `cbx_manager_shutdown`,
  `cbx_manager_check_first_run`, `cbx_manager_first_run_active`,
  `cbx_manager_handle_event`, `cbx_manager_render`,
  `cbx_manager_refresh_controllers_if_due`, `cbx_manager_backend_ready`,
  `cbx_manager_backend_degraded`.
- Accessors: `active_tab`, `tab_bar`, `panel`, `focus`,
  `controllers_tab`/`profiles_tab`/`settings_tab` (used heavily by tests).
- Each tab exposes `init/layout/refresh/shutdown`, action functions, and the
  manager-facing `*_handle_key` / `*_activate` / `*_cancel`.
- `service_install`: `cbx_service_install`, `cbx_service_uninstall`,
  `cbx_service_is_active`, `cbx_service_unit_path`, `cbx_service_unit_content`,
  `cbx_service_write_unit`, `cbx_service_check_group`,
  `cbx_service_systemd_available` (+ `CBX_TESTING` mock overrides).
- Profile: `cbx_profile_save_named`/`cbx_profile_save_to_dir`/
  `cbx_profile_save_meta_to_dir`; `cbx_profile_validate_nes_minimum`/
  `cbx_nes_minimum_buttons`; editor API (in `profile_editor_list.h`).

External dependencies (borrowed, not owned): `ui/renderer`, `ui/widget`,
`ui/text`, `ui/theme`, `ui/focus`, `ui/input_map`, `config/config_settings`,
`config/config_profile*`, `dbus/dbus_interface`, `dbus/ip_connection`,
`dbus/ip_device_model`, `dbus/ip_input_signal`, `dbus/ip_manager`,
`dbus/ip_target`, `dbus/ip_composite`, `icons/icon_map`, `icons/icon_cache`.

### 2. Internal state

- `cbx_manager` struct holds: window/renderer/text-cache/theme/settings/font;
  tabbar + 3 panels + `active_tab`; a `cbx_focus_chain`; the three embedded tab
  structs (`ct`, `pt`, `st`); DBus state (`dbus_backend` vtable, `dbus_bus`,
  `dbus_connected`, `owns_dbus_connection`, `dbus_init_rc`, `connection` +
  `last_controller_refresh_ms`); an array of up to `CBX_MGR_MAX_GAMECONTROLLERS`
  (16) open SDL game-controllers; first-run dialog widgets; `running` flag.
- **No static/global mutable state** in any manager file — state is fully
  struct-injected (good for testability).
- Per-tab persistent/owned state: `services_tab` none static; tab structs hold
  widget lists, selection indices, and mode enums (`cbx_ct_mode`, `cbx_pt_mode`
  LIST/CONFIRM_DELETE/NAME_INPUT/CREATE_PICK/EDITOR/CONFIRM_QUIT, `cbx_st_mode`,
  `cbx_editor_mode` LIST/TARGET_PICK/CAPTURE/SEQUENTIAL/BINDING_EDIT).
  `cbx_profile_editor` carries the live `cbx_profile` + dirty flag + capture and
  sequential sub-state.
- Persistent on-disk state managed by tabs: `~/.config/controller-box/
  settings.yaml` (settings tab via `cbx_settings_save`), `assignments.yaml`
  + `settings.yaml` (controllers tab topology), user/system profile `*.yaml`
  + sidecar `*.meta.yaml` (profiles tab), and the systemd user unit
  `controller-box.service` (service install).

### 3. Error handling

- Convention: `0` success, **negative errno** (`-EINVAL`, `-EEXIST`, `-ENOENT`,
  `-ENOSPC`, `-ENAMETOOLONG`, `-EIO`, `-EACCES`, …) plus raw backend `IP_ERR_*`
  codes. Tab-level `handle_key`/`cancel` return `bool`.
- `service_install` uses its own `CBX_SVC_*` result codes (0,1,2,-1…-5) and
  writes a human-readable status string.
- DBus failures propagate as return codes; user-visible errors are surfaced to
  the tab `status_lbl` (e.g. `show_action_error`, "Missing: …" from NES
  validation, "Save failed!").
- Degraded mode: `cbx_manager_backend_degraded` disables backend actions and
  shows a reason; `cbx_manager_backend_ready` re-enables + re-enumerates.
  Tab init never hard-fails on absent DBus (controllers tab degrades to empty
  enabled-but-disabled list).
- **Weakness**: several `cbx_list_add_item` return values and `systemctl`
  exits are ignored; NES validation error messages are only displayed, some
  code paths silently swallow failures (see issues below).

### 4. Test coverage

From `tests/CMakeLists.txt`, the manager and its sub-modules are covered by
(linked against `controllerbox` + `cbx_test_support` + CMOCKA unless noted):

- `test_manager_tabs` — lifecycle, tab switching, focus.
- `test_controllers_tab` — Controllers tab incl. `cbx_manager_init_with_dbus`
  (mock backend). Uses `cbx_test_support`.
- `test_profiles_tab` — Profiles tab incl. editor save. Uses `cbx_test_support`.
- `test_profile_diagram` — diagram widget + device-mapped catalogs.
- `test_editor_list_mode` — profile editor list mode.
- `test_editor_seq_mode` — sequential binding mode.
- `test_profile_validate` — NES-minimum validation.
- `test_profile_save` — save security/validation (no test_support).
- `test_settings_tab` — Settings tab.
- `test_service_install` — linked against `controllerbox_testing` with
  `CBX_TESTING` (mock systemctl/group/username).
- `test_manager_integration` — `controllerbox_testing` + `CBX_TESTING`;
  production-path first-run dialog + service install.
- `test_manager_calls` — manager DBus call routing.
- `test_manager_dbus_inject`, `test_manager_production`, `test_manager_native`,
  `test_manager_visual`, `test_manager_interaction_ctrl`,
  `test_manager_interaction_prof`, `test_manager_native_prof` — additional
  manager-level integration/visual/interaction coverage.
- `test_profile_cycle`, `test_installed_diagram.sh`, `test_installed_smoke.sh`.

These depend on `dbus_mock.c` (mock `ip_dbus_backend`) and `native_ip_server.c`
(native-input test path). The manager is well covered, but the visual tests
require SDL/Xvfb and skip (exit 77) where not available.

### 5. Potential issues (planner action items)

Prioritised, cross-referenced to their files. These come from a full read of the
subsystem and are the highest-value implementation tasks:

1. **[manager] UI-blocking retry loops** (`controllers_tab.c`): `refresh_until_path`
   and `wait_exact_attachment` spin `SDL_Delay(10ms)` + `backend->process` on the
   UI thread up to 2000 ms during Add/Remove/Change — blocks the whole window, and
   burns CPU if `backend->process` is NULL.
2. **[controllers_tab] `set_available` vs TYPE_PICK half-state**: toggling
   availability while a type-picker is open doesn't reconcile list/picker
   visibility, leaving an inconsistent view; a later `cancel_type_pick`
   restores unconditionally.
3. **[controllers_tab] silent `cbx_list_add_item` failures**: types/rows
   silently dropped if `supported_type_count`/`target_count` exceed
   `CBX_LIST_MAX_ITEMS`.
4. **[controllers_tab] `add` limit desync**: checks both `model.target_count`
   and `settings.virtual_controllers.count` against 16; a desync causes an
   errant push/refusal (with `-EINVAL` round-trip) instead of a single
   authoritative check.
5. **[controllers_tab] `change_type`/`refresh` correctness**: `change_type`
   overwrites `model.targets[idx]` before stop is confirmed and persists
   settings before creating the replacement (rollback exists); a single
   unreadable DeviceType sets `type_rc=-EIO` and upstream marks the whole tab
   "unavailable" (read-failure vs backend-down conflation, SPEC §2.4).
6. **[profiles_tab] confirm-quit "Save & Quit" ignores save failure**
   (`handle_key` CONFIRM_QUIT A path): if `save_editor` fails (e.g. NES
   validation) the app still quits, silently discarding changes.
7. **[profiles_tab] divergent/impossible create paths**: `cbx_profiles_tab_create`
   writes immediately and therefore cannot create an EMPTY profile (empty
   mappings fail NES minimum) — possibly dead/legacy vs the
   `name_input_confirm`→editor path. `cbx_profile_save_to_dir` does not
   re-check `-EEXIST` (only checked earlier against the enumerated list) — an
   external writer could be clobbered.
8. **[editor] duplicate InputEvent subscription**: `begin_capture` and
   `begin_sequential` call `ip_input_events_subscribe()` on every entry and
   **never unsubscribe** — repeated capture sessions stack subscriptions → one
   physical press fires the handler multiple times (multi-advance/capture).
9. **[editor] composite capabilities never loaded in the real path**:
   `cbx_profiles_tab_open_editor` calls `cbx_profile_editor_set_dbus` with a
   NULL composite_path, so DBus composite capability loading is dead in
   production — the target picker only ever uses hard-coded defaults.
10. **[editor] dead `cap_maps` field** (`cbx_file_list`) in `cbx_profile_editor`
    — declared, never used ("filesystem capability maps" feature).
11. **[editor] axes captured as buttons**: `on_input_event` /
    `seq_on_input` only check `value == 1.0`, no category check, so a stick
    event at max can write e.g. "LeftStickX" into a button slot.
12. **[editor] silent profile-full / props-full**: `find_or_create_mapping`
    returns -1 and prop-table-full returns silently — no user feedback, no
    progress, no `-ENOSPC` handling.
13. **[editor/seq] completion message never shown**: on the final capture,
    `cancel_sequential` (which clears the status + refreshes) is invoked right
    after setting the "complete" label.
14. **[diagram] marker drift**: `diag_draw` highlight uses `(int)` truncation
    where `content_rect` rounds — ~1px offset risk.
15. **[service_install] uninitialized buffer bug**: `cbx_service_is_active`
    uses `char buf[128]` (uninitialized); on `run_command` failure it reads it
    → UB / false "active" positives. Fix: `= {0}` + check exit code.
16. **[service_install] buffer truncations**: `ExecStart` `exec_buf[512]`
    truncates a long `CBX_BINARY_PATH`; `cbx_service_write_unit`/`write_atomic`
    truncate a `PATH_MAX`-length path for the parent-dir computation while
    rename/mkstemp use the untruncated original (wrong dir / tempname risk).
17. **[service_install] `run_command` has no timeout** — a hung `systemctl`
    stalls the UI indefinitely; also **no fsync before rename** (crash
    durability), **mkdir errors ignored**, and group-check errors are treated
    as "not in group" (polkit warning instead of surfacing the error).
18. **[service_install] `uninstall` swallows `systemctl disable` failure** —
    unit file is unlinked even if disable fails, leaving an inconsistent
    enabled-but-unitless service. `build_argv` silently drops args past
    its cap.
19. **[settings_tab] settings load failure silently falls back to defaults**
    without user feedback; dead `#include "config/config_paths.h"`.
20. **[profiles_tab] minor UX gaps**: editor-open failure in the create path
    gives no feedback; editor leave-inconsistent on open failure; icon meta
    write failure after profile is already on disk leaves the list unrefreshed;
    status label from a prior failure can linger; `on_delete_pressed` /
    `on_edit_pressed` lack a `mode == LIST` guard.

### Security & robustness positives

Service install is already hardened (no `system()/popen()`, absolute systemctl
paths, exact `FLATPAK_ID == org.shadowblip.ControllerBox` binding + charset
validation, no user interpolation into the unit, atomic mkstemp+rename, mock
overrides gated behind `CBX_TESTING`). Profiles-tab name validation is
`^[a-zA-Z0-9_-]+$`; saves canonicalize paths and gate on NES-minimum. Array
bounds in controllers_tab (`CBX_CT_MAX_DEVICES`/`CBX_CT_MAX_TYPES`/`parse_csv`)
are respected.
