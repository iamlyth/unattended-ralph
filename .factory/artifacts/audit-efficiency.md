# Efficiency Audit — Final Audit (Task 0)

Scope: performance, resource usage, algorithmic complexity, hot paths,
redundant work, I/O patterns, and build efficiency across `src/`.

No **BLOCKER** findings. Two **WARN** and two **INFO** findings below.

---

## WARN 1 — Manager renders in an unbounded busy loop (no frame pacing, no vsync, no dirty gating)

**Files:** `src/manager/manager.c` (main loop ~lines 656–677, `cbx_manager_render` line 1173), `src/ui/renderer.h` (`CBX_RENDERER_FLAGS_ACCEL` ~line 27).

The Manager's `while (mgr->running)` loop unconditionally calls
`cbx_manager_render(mgr)` then `cbx_renderer_present(&mgr->rend)` every
iteration. There is **no `SDL_Delay`** and the renderer is created with
`SDL_RENDERER_ACCELERATED | SDL_RENDERER_TARGETTEXTURE` (no
`SDL_RENDERER_PRESENTVSYNC`), so `SDL_RenderPresent` does not block on vblank.

Consequences when idle (no input, no DBus traffic):
- The full widget tree (`cbx_tabbar` draw + the active panel's entire child
  subtree) is redrawn **every frame**, including a `cbx_text_render` cache
  lookup + `SDL_QueryTexture` + `SDL_RenderCopy` per label, plus all
  `SDL_RenderFillRect`/`SDL_RenderDrawRect` primitives.
- Frame rate is unbounded (hundreds of FPS), spinning the GPU/CPU at 100%
  on an idle desktop/Pi 4 and draining power.
- The DBus drain (`for i<64 process()`) and `cbx_manager_refresh_controllers_if_due`
  are also re-tried at that frequency; the former still issues a `process()`
  call each pass even when idle.

Contrast: the overlay service already gates rendering on
`cbx_overlay_surface_is_dirty()` and paces the loop with `SDL_Delay(10)`
(`src/app/overlay_service.c:1368`, `:1632`), so the Manager path is the
inconsistent one here.

**Recommendation:** pace the Manager loop. Cheapest fix: add
`SDL_RENDERER_PRESENTVSYNC` to `CBX_RENDERER_FLAGS_ACCEL` (falls back
cleanly to the existing `vsync_enabled` detection), or add a small
`SDL_Delay` (e.g. 16ms) to the idle path. Better: track a dirty flag and
skip `cbx_manager_render`/`present` when nothing changed, as the overlay does.

---

## WARN 2 — Per-render cell work recomputes column-constant lookups repeatedly

**File:** `src/overlay/grid_render.c` (`cbx_select_grid_render`, row/column loops).

Inside the per-row `for (row ...)` / `for (col ...)` double loop, for every
cell `col > 0` the code calls `cbx_settings_icon_override(ctx->settings,
dev_type)` and then `cbx_icon_lookup(cache, map, dev_type, ...)` with a
`memset(&icon_res, ...)` per call. The `dev_type` (and therefore the icon and
its override) is constant across **all rows for a given column**, so this is
recomputing identical hash lookups ~`row_count` times per column.

The overlay surface is only re-rendered when dirty and bounds are small
(`CBX_GRID_MAX_ROWS` / `CBX_MAX_CONTROLLERS`), so the absolute cost is low —
this is not a frame-rate blocker.

**Recommendation:** hoist the per-column icon/override lookup out of the row
loop; pre-resolve the texture + dims once per column into a small
`struct column_icon { SDL_Texture *tex; int w, h; }` array and reuse it for
every row. Both the override scan and icon cache lookup are O(1)–O(cols), so
this is a straightforward constant-factor win that also removes the
redundant `memset`s.

---

## INFO 1 — `cbx_dirty_rect_merge` is worst-case O(n³)

**File:** `src/ui/dirty_rect.c` (`cbx_dirty_rect_merge`).

The outer `do { … } while (changed)` loop performs up to `count` passes, and
each pass does an O(count²) nested-overlap scan, giving worst-case O(n³).
This is inherently bounded by `CBX_DIRTY_RECT_MAX = 64`, is only invoked on
state changes (never per-frame), and merges typically converge in 1–2 passes,
so it is not a practical problem today.

**Recommendation:** no action required. If dirty regions ever grow, an
incremental single-pass union (or a sweep-line merge) would reduce it to
O(n log n); document the bound so a future change doesn't hit the pathological
case.

---

## INFO 2 — `cbx_conflict_detect` / `cbx_conflict_is_row_conflicted` are O(n²)-style scans

**Files:** `src/overlay/conflict.c`, `src/overlay/grid_render.c`.

`cbx_conflict_detect` is O(rows²) and `cbx_conflict_is_row_conflicted` is
O(conflict_count) per row called from `cbx_select_grid_render`. Bounds are tiny
(`CBX_GRID_MAX_ROWS`), and detection runs on slot/profile-change and on dirty
render, not per frame. Not worth optimizing; noting as an observation.

---

## Summary

- **BLOCKER:** 0
- **WARN:** 2
  1. `src/manager/manager.c` / `src/ui/renderer.h` — unbounded render busy loop (no vsync/delay, no dirty gating).
  2. `src/overlay/grid_render.c` — column-constant icon/override lookups recomputed per row.
- **INFO:** 2
  1. `src/ui/dirty_rect.c` — worst-case O(n³) merge, bounded to 64 entries.
  2. `src/overlay/conflict.c` — small O(rows²) scans, bounded.

Resource lifecycle (SDL renderer/window, text/icon texture caches, `sd_bus`
unrefs, intercept-poll timers, DBus messages, config/profile atomic file
writes) was reviewed across `src/dbus`, `src/ui`, `src/manager`, `src/app`
and found clean: all cached textures and allocations are freed on the
corresponding `*_cleanup`/`*_shutdown`/`*_destroy` paths, and DBus bus/message
refs are balanced.
