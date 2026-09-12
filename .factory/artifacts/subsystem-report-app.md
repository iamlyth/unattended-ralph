# Subsystem Study Report: src/app

The **app** subsystem is the thin application-layer coordinator of `controller-box`.
It is the integration hub that wires together the DBus layer (`src/dbus`), overlay
mode logic (`src/overlay`), UI resources (`src/ui`, `src/icons`), and config
(`src/config`) into two runnable entry points. It holds almost **no** business
logic of its own — its value is the orchestration order, the lifecycle callbacks it
registers, and the poll loop that drives everything at runtime.

## Files

### `src/app/main.c` (262 lines)
- **Purpose**: Binary entry point that parses CLI flags and dispatches to the
  overlay-service or manager run-path.
- **Key functions**:
  - `main()` — parses `--overlay-service` (default), `--manager`, `--dry-run`,
    `--version`, `-h/--help`. Unknown option → prints usage, returns `2`.
  - `run_manager(dry_run)` — banner, font discovery (`cbx_font_path()`), then
    `cbx_manager_init` / `cbx_manager_run` / `cbx_manager_shutdown`.
  - `mode_name()`, `print_version()`, `print_usage()` — formatting helpers.
- **Interfaces**: includes `manager/manager.h`, `app/overlay_service.h`,
  `config/config_paths.h`. **Entry point** of the whole process.
- **Note**: `main.c` is deliberately the ONLY file compiled into the executable
  (`add_executable(controller-box ... main.c)`); everything else lives in
  `libcontrollerbox`. No static state here.

### `src/app/overlay_service.h` (285 lines)
- **Purpose**: Public contract for the overlay-service run-path, exposing every
  testable seam. Declares structs, callbacks, and the step function.
- **Key structs**:
  - `cbx_poll_activation_ctx` — per-composite poll activation context (lifecycle
    ptr + `composite_path`) so *close* restores PASS on the triggering composite.
  - `cbx_overlay_input_ctx` — maps DBusDevice paths → grid row indices
    (max `CBX_MAX_DBUS_DEVICES`=64) so InputEvent signals route to the right row.
  - `cbx_overlay_service_ctx` — the **master context** (Task 10): renderer, DBus
    conn, device model, settings/assignments/profiles, text cache/font/theme/icon
    resources, composites, grid, surface, render ctx, lifecycle, pm/hm/conflicts,
    input events, intercept polls, hotplug handler, reconcile-status struct, and
    flags (`initialized`, `backend_ready`).
  - `reconcile_status` nest — phase/operation/kind/path/counts/rc/elapsed/deadline/
    cleanup_failures/originals_stopped/detail for diagnostics.
- **Key public functions** (all exposed for testing):
  - Input mapping: `cbx_overlay_input_add_mapping`, `cbx_overlay_input_build_map`,
    `cbx_overlay_input_find_row`, `cbx_ip_input_to_pm`, `cbx_ip_input_to_hm`,
    `cbx_overlay_input_cb`.
  - Loop: `cbx_overlay_service_step(svc)`.
  - Reconciliation: `cbx_overlay_rearm_polls`, `cbx_reconcile_startup_targets`.
  - Entry: `run_overlay_service(dry_run)`.
  - Signals: `cbx_overlay_service_install_signal_handlers`,
    `cbx_overlay_service_shutdown_requested`, `cbx_overlay_service_reset_shutdown`.
  - Callbacks: `cbx_overlay_on_save`, `cbx_overlay_on_slot_change`,
    `cbx_overlay_on_profile_change`.
  - Under `CBX_TESTING`: exported `on_intercept_activating/_deactivating/_error` so
    native tests wire the *exact* production callbacks.
- **Constants**: `CBX_RECONCILE_TIMEOUT_MS`=2000, `CBX_RECONCILE_POLL_MS`=10,
  `CBX_MAX_DBUS_DEVICES`=64.

### `src/app/overlay_service.c` (1650 lines)
- **Purpose**: Production init path + poll loop for overlay-service mode. The
  heart of the subsystem.
- **Static/internal state**: `static volatile sig_atomic_t g_running` — the 
  single global control flag (1=run, 0=shutdown). Set by signal handlers, cleared
  by poll loop. Only global in the subsystem.
- **Key functions & responsibilities**:
  - `signal_handler` / `cbx_overlay_service_install_signal_handlers` — SIGTERM/SIGINT
    without `SA_RESTART` so it interrupts `SDL_PollEvent`.
  - `on_intercept_activating/_deactivating/_error` — lifecycle activate on
    activation, `cbx_overlay_lifecycle_close` on deactivation, error logged.
  - `cbx_overlay_on_save` — **the big save path**: detect+resolve conflicts, build
    `GamepadOrder` string, Phase 1 exact-replacement routing (clear every composite,
    then attach with profile apply + singleton-verified TargetDevices), Phase 2
    `ip_manager_set_gamepad_order`, Phase 3 only-now-sync + persist assignments.
    Uses `wait_for_attachment` with exact-singleton CSV matching (stale/cross-slot
    detection).
  - `cbx_overlay_on_slot_change` / `cbx_overlay_on_profile_change` — re-detect
    conflicts, mark surface dirty; profile change applies via
    `cbx_profile_cycle_apply` + save.
  - `on_host_slot_change` — thin wrapper → `cbx_overlay_on_slot_change`.
  - `fill_composite_info` — path/persistent-id/model-name from DBus, best-effort.
  - `set_all_pass` — set InterceptMode PASS on all composites.
  - **Reconciliation internals**: `reconcile_now_ms`, `reconcile_error_category`,
    `reconcile_diag`, `csv_exact_path_count`/`csv_token_count`/
    `csv_is_exact_singleton`, `assigned_composite_for_slot`,
    `reconcile_enumerate`, `reconcile_order_targets`,
    `wait_for_exact_target`, `wait_for_attachment`.
  - `cbx_reconcile_startup_targets` — grow targets to `desired` using **retained
    CreateTargetDevice return paths** (never reply order), correct device types
    non-destructively, attach only persisted physical assignments, destructive
    shrink last; on failure rollback-stops all created paths with cleanup
    accounting. Sets/returns negative errno; fills `reconcile_status`.
  - `cbx_overlay_rearm_polls` — stop existing polls, re-create one per composite
    with per-composite activation context.
  - `cbx_overlay_reconcile_hotplug` — after hotplug: save profiles, update
    composite info, dynamic-columns-or-grid rebuild, restore profiles, re-detect
    conflicts, rebuild input map, re-register triggers + PASS, rearm polls.
  - `overlay_backend_degraded` / `overlay_backend_ready` — recovery callbacks:
    degraded force-closes lifecycle + hides window; ready re-enumerates, re-reconciles,
    rebuilds grid, restores topology via `cbx_overlay_on_save`, resubscribes
    input/hotplug.
  - `sdl_key_to_pm_input` / `sdl_key_to_hm_input` — SDL key → mode input.
  - `cbx_overlay_input_*` — DBusDevice↔row mapping + dispatch (player/host mode).
  - `cbx_overlay_service_step` — one poll-loop iteration: drain SDL events (poll
    ticks, QUIT, keydown), drain DBus (recovery path), process InputEvents,
    hotplug reconcile, lifecycle tick, show/hide window, re-arm idle polls, re-render
    dirty surface.
  - `run_overlay_service(dry_run)` — **entry point**: init SDL hidden renderer →
    connect system DBus → enumerate → load settings/assignments → startup reconcile →
    text/theme/icon resources → build grid → pre-render surface → restore topology →
    register triggers + PASS → lifecycle init → wire callbacks → input map/subscribe →
    rearm polls → hotplug subscribe → set degraded/ready cbs → `while (g_running)`
    loop with `SDL_Delay(10)` → clean shutdown sequence.

## Entry points
- `main()` (process start).
- `run_overlay_service(int dry_run)` — called from `main`, and tested directly.
- `cbx_overlay_service_step()` — called by the poll loop; called by tests to
  simulate iterations.
- `cbx_overlay_on_save/_on_slot_change/_on_profile_change` — registered as lifecycle
  and player-mode callbacks; called from `overlay/lifecycle.c` and
  `overlay/player_mode.c`.
- `cbx_reconcile_startup_targets`, `cbx_overlay_rearm_polls` — called internally and
  from tests during setup.

## Internal state
- `g_running` (static `sig_atomic_t`) — the only global. `0` requests shutdown.
  Exposed read/write via `cbx_overlay_service_shutdown_requested()` /
  `cbx_overlay_service_reset_shutdown()`.
- Everything else is per-`cbx_overlay_service_ctx` fields, allocated on the heap in
  `run_overlay_service` (`calloc`) because the struct exceeds 200 KB. **Lifecycle**:
  initialized sequentially in `run_overlay_service`, torn down in reverse order on
  both error and clean-shutdown paths. Error paths manually `ip_connection_disconnect`,
  `cbx_renderer_shutdown`, `free(svc)` — resource ownership is hand-managed.

## Error handling
- DBus calls return negative errno / `IP_ERR_*` codes; most init steps map `!= 0`
  to a stderr message + non-zero process exit after full resource cleanup.
- `run_overlay_service` returns: SDL init fail → 1; no system DBus → 1;
  reconcile fail → 1; no profiles → 1; surface init fail → 1; restore-assignments
  fail → 1; clean shutdown → 0. `dry_run` → 0 always.
- `cbx_overlay_on_save` returns negative errno (e.g. `-ENAMETOOLONG` on order
  overflow, `-ENODEV` on slot≥target_count, DBus codes) — **no engine state is
  persisted on failure** (Phase 3 ordering guarantees this).
- Reconcile failure path sets `reconcile_status.detail` and rolls back created
  paths, counting `cleanup_failures`.
- **Caution**: `overlay_backend_degraded` / `_ready` are used for live recovery;
  they can be re-entered and re-enumerate/rebuild state.

## Test coverage
Direct tests register via `tests/CMakeLists.txt` (all link `app/overlay_service.c`):
- **`test_overlay_service`** (tests/test_overlay_service.c): SDL init failure,
  dry-run, SIGTERM/SIGINT flags, multi-controller independent rows, unknown
  device-path dropped, wrong sender dropped (fail-closed), input-events-process
  mock no-op, `step` quit/keydown/empty-queue.
- **`test_overlay_interaction`** (1068 lines): exercises `cbx_overlay_service_step`
  through real SDL events.
- **`test_overlay_latency`** (656 lines): timing/latency of the step + reconcile.
- **`test_overlay_reconcile`** (676 lines): reconcile/enumeration + hotplug paths.
- **`test_overlay_native`** (1453 lines): wires the *exact production* intercept
  callbacks (`on_intercept_activating` etc. via `CBX_TESTING`).
- Plus `test_installed_functional`, `test_daemon_footprint` reference the service.
- Indirect: `test_overlay_visual` / `test_overlay_integration` / `test_overlay_lifecycle`.

## Potential issues / observations for the planner
1. **Long blocking startup**: `cbx_reconcile_startup_targets` does synchronous
   `SDL_Delay(poll)` polls with a **2000 ms deadline per operation**
   (`CBX_RECONCILE_TIMEOUT_MS`). With many grow/type/attach/shrink steps, startup /
   `cbx_overlay_on_save` restore can block for seconds. Bounded, but worth
   confirming acceptable UX and that `cbx_overlay_on_save` (called on every load
   and restore) doesn't exceed expected latency.
2. **Hand-managed cleanup**: every early-return error path in `run_overlay_service`
   duplicates the `disconnect → shutdown → free` sequence; a new init step must
   remember to extend all cleanup blocks. Refactor to a `goto cleanup` single-exit
   pattern would reduce risk.
3. **`g_running` shared global across tests**: tests mutate it via reset/shutdown;
   ordering matters and it is process-global (already handled by reset calls, but a
   future test could leak state).
4. **Best-effort degrade**: font, icon map, icon cache, device enumeration,
   `set_all_pass`, and input-map building are best-effort (`continue` / ignore
   errors). Acceptable by design but means failures here are silent.
5. **`cbx_overlay_input_cb` host-mode row**: host navigates via
   `cbx_host_mode_get_host_row` while player mode uses the device-path row; the
   keyboard path hardcodes row 0. Divergent row semantics between input sources is a
   subtle area for any multi-controller keyboard test.
6. **`wait_for_exact_target`/`wait_for_attachment` re-enumerate repeatedly** inside
   loop plus dispatch `backend->process`; cost is bounded by deadline but repeated.
7. **`on_save` clear-all-then-attach (Phase 1)**: momentarily empties TargetDevices
   on every composite before re-asserting — during this window routing is transiently
   empty. Intentional (authoritative replacement) but not atomic across composites.
8. **`CBX_MAX_COMPOSITES` vs `model.composites` indexing**: `set_all_pass` iterates
   `svc->comp_count` (clamped) over `svc->model.composites`; keep both arrays in
   sync or a composite beyond the clamp could be missed on PASS.
