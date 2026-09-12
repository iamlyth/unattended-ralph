## Architecture Study Report

**Project:** Controller-Box — a controller-only SDL2 GUI for Linux wrapping InputPlumber.
**Root:** `/workspace/project` (the task brief's `/workspace/controller-box` does not exist; the repo lives under `/workspace/project`, on `develop`).

Controller-Box is a single C11 binary, `controller-box`, with two modes:

- **Overlay service** (`--overlay-service`, default): an always-resident systemd user service showing a fighting-game-style "character select" overlay for per-controller slot assignment and profile cycling in-game.
- **Manager** (`--manager`): a tab-based configuration app (Controllers / Profiles / Settings) for virtual-controller creation, profile editing with a visual controller diagram, and settings.

Both modes share one codebase, one config directory, one DBus connection, and one `libcontrollerbox` static library.

---

### 1. Top-level source directories and purposes

| Directory | Purpose |
|-----------|---------|
| `src/app/` | Thin executable entry points: `main.c` (dual-mode dispatch + flag parsing), `overlay_service.c/.h` (the full overlay poll-loop service). |
| `src/ui/` | Reusable rendering + widget toolkit on top of SDL2: renderer, text, theme, focus, input map, animation, dirty-rect, and a widget family (button, label, image, panel, list, grid, tabbar, progress). |
| `src/config/` | File-backed configuration: paths, settings (YAML), assignments (slot auto-assignment + gamepad order), and profiles (profile file load/save/meta/list). |
| `src/dbus/` | InputPlumber DBus integration via an **abstraction vtable** (`ip_dbus_backend`) over sd-bus: connection, devices, composites, properties, signals, object-manager, intercept polling, target creation. |
| `src/icons/` | Controller icon subsystem: icon→file mapping (YAML), raster cache, lookup. |
| `src/identify/` | Controller identity extraction and persistence: strongest-identity selection (BT→USB→port→order), slot assignment, assignment persistence, gamepad-order restore, identity-downgrade fallback. |
| `src/manager/` | The manager application: `manager.c` (window+event loop), the three tabs (controllers, profiles, settings), profile editor (list + seq modes) and diagram, profile save/validate, service installation. |
| `src/overlay/` | The overlay feature domain, mode-agnostic (no SDL window here): surface pre-build, lifecycle state machine, grid rendering, player/host modes, conflict, profile cycle, dynamic columns, trigger, close. |
| `src/inputplumber-mediator.c` | Root helper binary (`inputplumber-mediator`): forwards InputPlumber mutation calls over its own system-bus connection. |

### 2. Major module breakdown

#### 2.1 Application layer (`src/app/`)
- **`main.c`** — sole C entrypoint. Parses `--overlay-service` / `--manager` / `--dry-run` / `--version` / `--help`. Dispatches to `run_overlay_service()` or `cbx_manager_init`+`cbx_manager_run`. `--dry-run` is headless-safe (banner + exit).
- **`overlay_service.c` (66 KB) / `.h`** — the overlay service. Defines the comprehensive `cbx_overlay_service_ctx` context struct holding all loop state (renderer, DBus connection, device model, settings, assignments, profiles, grid, surface, lifecycle, modes, intercept polls, hotplug handler). **Key API:** `cbx_overlay_service_step()` — one iteration of the poll loop, extracted so tests can inject events without an infinite loop. Also `run_overlay_service(dry_run)`, `cbx_reconcile_startup_targets()`, `cbx_overlay_rearm_polls()`, signal handlers, and production callbacks (`on_save`, `on_slot_change`, `on_profile_change`, intercept callbacks).

  This is the orchestration core of the whole product — it wires renderer, DBus, lifecycle, modes, polling, and reconciliation together.

#### 2.2 Manager (`src/manager/`)
- **`manager.c` / `manager.h`** — the manager window + event loop. `cbx_manager` struct owns renderer, text cache, theme, settings, tab bar, 3 panels, focus chain, DBus connection state, and real `SDL_GameController` array. **Key API:** `cbx_manager_init`, `cbx_manager_init_with_dbus` (backend injection for tests), `cbx_manager_run`, `cbx_manager_stop`, `cbx_manager_handle_event` (public for testing), `cbx_manager_render`, first-run service dialog, accessor getters.
- **`controllers_tab.c/.h`, `profiles_tab.c/.h`, `settings_tab.c/.h`** — the three tab modules, each owning a `cbx_panel`. Controllers tab is DBus-backed; Profiles tab is filesystem-backed; Settings tab is local YAML-backed.
- **`profile_diagram.c/.h`** — visual controller diagram; **`profile_editor_list.c`**, **`profile_editor_seq.c`** — binding editor (list + sequential capture modes); **`profile_validate.c`**, **`profile_save.c`**; **`service_install.c/.h`** — systemd user service install (has `CBX_TESTING`-gated mock overrides, kept out of release builds).

#### 2.3 Overlay domain (`src/overlay/`) — no SDL window, mode-agnostic logic
- `surface_build.c/.h` — pre-builds the overlay grid surface in memory at startup (perf: <10 ms render; icons pre-rasterized).
- `lifecycle.c/.h` — `cbx_overlay_lifecycle`, the overlay open/close state machine.
- `grid_render.c/.h` — grid (rows=controllers, columns=slots) rendering context.
- `player_mode.c/.h` + `host_mode.c/.h` — Player Mode (all controllers edit simultaneously) vs. Host Mode (first R3 = exclusive host).
- `conflict.c/.h`, `profile_cycle.c/.h`, `dynamic_columns.c/.h`, `trigger.c/.h` (Select+A combo), `close.c/.h` (restores PASS on activating composite).

#### 2.4 UI toolkit (`src/ui/`)
- `renderer.c/.h` — `cbx_renderer`: SDL2 window + renderer wrapper. `theme.c`, `text.c` (TTF text cache), `focus.c` (focus chain for controller nav), `input_map.c`, `animation.c`, `dirty_rect.c`.
- **Widget family** (`widget.h` + `widget_*.c`): `widget.c` (base `cbx_widget`), button, label, image, panel, list, grid, tabbar, progress.

#### 2.5 Config (`src/config/`)
- `config_paths.c` — runtime file paths. `config_settings.c/.h` — `cbx_settings` YAML. `config_assignments.c` — slot auto-assignment + gamepad order table. `config_profile.c/.h`, `config_profile_meta.c`, `config_profile_list.c` — profile file load/save/list + per-profile sidecar metadata.

#### 2.6 DBus layer (`src/dbus/`)
- **`dbus_interface.h`** — the critical abstraction. Defines InputPlumber DBus constants, signal payload structs (`ip_owner_changed`, `ip_interfaces_changed`, `ip_properties_changed`, `ip_input_event`), and the **`ip_dbus_backend` function-pointer vtable** (connect/disconnect, get_unique_name, get_connection_creds, call_method, get/set_property, get_managed_objects, subscribe_signal, inject_signal [test-only], process). Production impl `ip_dbus_sd_backend()` is in `dbus_client.c`; tests inject a mock (`tests/dbus_mock.c`).
- **`ip_*.c/.h`** per InputPlumber concept: `ip_connection`, `ip_device_model`, `ip_composite`, `ip_create_composite`, `ip_gamepad_order`, `ip_hotplug`, `ip_input_signal`, `ip_intercept_poll`, `ip_manager`, `ip_objectmanager`, `ip_properties`, `ip_source`, `ip_target`.

  **Key design pattern:** DBus is never called directly from product code — everything goes through the backend vtable so tests observe exact requests and inject signals without a live bus.

#### 2.7 Identity (`src/identify/`)
- `identity.c/.h` — strongest-identity extraction (4 layers: BT MAC → USB serial → USB port → connection order), with `CBX_IDENTITY_LAYER_*` enum and per-interface serial rules (evdev uses UniqueId, HIDRaw uses SerialNumber).
- `assign.c`, `assign_persist.c` — slot assignment + persistence. `gamepad_order_restore.c`. `identity_downgrade.c` — fallback when a strong identity disappears.

#### 2.8 Icons (`src/icons/`)
- `icon_map.c` (SVG→file mapping from `controller-icons.yaml`), `icon_cache.c` (raster cache via nanosvg), `icon_lookup.c`.

#### 2.9 Cross-cutting: `inputplumber-mediator.c`
- Separate small binary installed to `libexec`; makes InputPlumber mutation decisions before forwarding over its own system-bus connection.

---

### 3. Build system and test structure

**Build:** CMake (`CMakeLists.txt`, C11, `GNUInstallDirs`). Three declared runners in `.factory/environment.toml`: `dev-runner-vm`, `iprunner`, `gpurunner`, each running `./scripts/verify.sh`.

- **Targets:**
  - `nanosvg` — vendored static lib (`third_party/nanosvg`), zlib-licensed, suppressed warnings.
  - `controllerbox` — static lib aggregating **all** product sources (everything except `main.c`), linked against SDL2, SDL2_ttf, SDL2_image, systemd (sd-bus), yaml, nanosvg.
  - `controllerbox_testing` — a **second copy** of the same sources compiled with `CBX_TESTING`, so `service_install.c` mock overrides exist only in the test-instrumented build. Release lib/binary never contain mock symbols (F6 / security boundary).
  - `controller-box` — executable (`main.c`), links `controllerbox`.
  - `inputplumber-mediator` — executable, links systemd.
- **Deps:** SDL2, SDL2TTF, SDL2IMG, SYSTEMD, YAML (all REQUIRED via pkg-config); CMOCKA optional (drives unit tests only when available). Sanitizer option `CBX_ENABLE_SANITIZERS` (ASan+UBSan). `-Werror` gated to Debug. Compiled with `_GNU_SOURCE` (uses `open_memstream`, `mkstemp`, `O_NOFOLLOW`).
- **Install layout:** `/usr` default prefix; absolute paths compiled into generated `config.h` from `config.h.in`.
- **Docs:** build/install in `docs/PACKAGING.md`, `shell.nix`/`cross-shell.nix`/`cmake/aarch64-toolchain.cmake` for Nix-based builds.

**Tests** (all in `tests/`, registered via `tests/CMakeLists.txt`, `ctest`):
- **Smoke tests** — `smoke_test_sdl2`, `smoke_test_nanosvg` (always built, no cmocka).
- **cmocka unit tests** — linked against `controllerbox_testing` (mostly) and use the **mock DBus backend** (`dbus_mock.c/.h`) or the **native IP server** (`native_ip_server.c/.h`, a private InputPlumber-compatible DBus server). Test scaffolding: `test_harness`, `fb_assert.c` (framebuffer region assertions), `interaction_inventory.c`, `cmocka_compat.h`.
- **Layered visual acceptance** (README verification table):
  1. Deterministic framebuffer — `test_overlay_visual`, `test_manager_visual` (read back via `SDL_RenderReadPixels`).
  2. Region assertions — `fb_assert` (`fb_region_has_content`, `fb_region_has_color`, `fb_frames_differ`, `fb_golden_compare`).
  3. Golden images — `test_golden` (11 baselines in `tests/golden/`, ±3/channel, <2% tolerance; failure artifacts to `tests/golden-fail/`; regenerated only via explicit `scripts/generate-golden.sh`).
  4. Installed production smoke — `test_installed_smoke.sh` (Xvfb + xdotool subprocess), `test_installed_functional.c`, `test_installed_binary.sh`, `test_installed_diagram.sh`.
  5. Kernel-backed — `test_kernel_controller.c` + `run-kernel-controller-test.sh` (evdev via `/dev/uinput`; skips exit 77 if absent).
  6. Backend smoke — `test_backend_smoke` / `test_backend_smoke_sw` (accelerated vs software; skips headless).
- **Verification entrypoint:** `scripts/verify.sh` (self-heals stale CMake cache, builds under nix-shell, runs full CTest). Plus `scripts/verify-sanitizers.sh`. A hardware-hungry test skips with **exit 77** rather than fake-passing.

---

### 4. Recurring patterns

- **Single binary, dual mode:** one `main.c` dispatches to two run-paths; all logic shared behind `libcontrollerbox`.
- **DBus via vtable abstraction:** `ip_dbus_backend` function-pointer interface lets tests use `dbus_mock` (direct signal injection) or `native_ip_server` (private real-sd-bus server) interchangeably with production sd-bus. Product code never calls sd-bus directly. All names/ifaces/payload structs centralized in `dbus_interface.h`.
- **Extractable event-loop step for testability:** both `cbx_manager_handle_event` (manager) and `cbx_overlay_service_step` (overlay) are public single-iteration/drainable entry points so tests exercise the real SDL/DBus dispatch path rather than direct callbacks.
- **SDL2 rendering with pre-built surface:** overlay pre-rasterizes the grid surface in memory at startup (icons via **nanosvg**) for a <10 ms render target; rendering is compositor-agnostic (X11 via Xvfb, Wayland, Gamescope all work — zero compositor-specific calls).
- **Two-copy build for test instrumentation:** `CBX_TESTING` variant of the library gives mock override hooks (service install) that are guaranteed absent from the release artifact.
- **Dual transport tests:** DBus exercised through both a light-weight mock backend and a `native_ip_server` real DBus daemon, with pixel readback (`SDL_RenderReadPixels`) and golden comparison for visual acceptance.
- **Controller-first UI:** focus chains + `input_map` for controller navigation; keyboard is supplemental. GameController transport via `SDL_GameController`.
- **Identity fallback ladder:** strongest available identity wins (BT MAC → USB serial → USB port → connection order) with graceful downgrade.

---

### 5. Notes for the planner

- The overlay service (`src/app/overlay_service.c` + `src/overlay/`) is the product's busiest orchestration module and the most heavily tested; changes there should route through `cbx_overlay_service_step` and the `ip_dbus_backend` abstraction.
- New features should (a) add sources to **both** `controllerbox` and `controllerbox_testing` lists in the root `CMakeLists.txt`, and (b) route InputPlumber operations through `dbus_interface.h`/`ip_*` wrappers with DBus requests observable by tests.
- Prior subsystem studies already exist at `.factory/artifacts/` (`subsystem-manager-study.md`, `subsystem-report-app.md`) — consult them before duplicating module analysis.
