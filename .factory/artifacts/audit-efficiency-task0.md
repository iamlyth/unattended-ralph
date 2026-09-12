# Efficiency Audit — Task 0 (Final Audit)

Scope: performance, resource usage, algorithmic complexity across the current
codebase state (built from `develop`, verify run completed).

**BLOCKER findings: none.** The codebase is well-structured: text and icon
rasterization are cached behind bounded open-addressing hash maps (load factor
< 0.5), DBus connections/messages are unref'd, malloc'd reply strings on the
`ip_composite_*`/`ip_target_*` paths are freed, and grid/conflict loops are
bounded by `CBX_GRID_MAX_ROWS` (~tens of entries). No resource leaks or
unbounded algorithmic blowups were found in the render, poll, or save paths.

**WARN findings:**

1. **Unthrottled manager main loop — continuous full redraw when idle**
   `src/manager/manager.c` `cbx_manager_run` (loop around line 656)

   The loop does `SDL_PollEvent` + DBus drain + `cbx_manager_render()`
   (full clear + all widget draws) + `cbx_renderer_present()` on **every**
   iteration, with no `SDL_Delay`, no dirty/redraw flag, and the renderer is
   created without `SDL_RENDERER_PRESENTVSYNC`
   (`CBX_RENDERER_FLAGS_ACCEL = ACCELERATED | TARGETTEXTURE`,
   `src/ui/renderer.h`). On an idle static UI this spins at unbounded FPS,
   burning a full CPU core and doing needless present/blit work.

   Recommendation: mirror the overlay service (`overlay_service.c` step 7
   renders only when the surface is dirty, and the loop uses `SDL_Delay(10)`).
   Either gate render+present on a dirty flag and add a small `SDL_Delay`, or
   add `SDL_RENDERER_PRESENTVSYNC` to the accelerator flags.

2. **Redundant per-cell icon lookup in the grid render path**
   `src/overlay/grid_render.c` `cbx_select_grid_render`

   Inside the row loop, for every column `col > 0` the code calls
   `cbx_settings_icon_override(settings, dev_type)` then
   `cbx_icon_lookup(cache, map, dev_type, ...)`. Both `dev_type`
   (`g->cols[col].device_type`) and the resulting override are a function of
   the **column** only, yet the lookup is recomputed once per row for each
   column — i.e. the same icon/override is resolved `row_count` times. Each
   lookup does stack-string formatting plus a hash probe plus a
   `strcmp`/djb2 pass. With R rows × C columns this is R× redundant work in
   the per-frame path.

   Recommendation: hoist per-column device-type → icon-texture resolution out
   of the row loop (resolve once per column into a local array, then render
   the cached `SDL_Texture*` from it each row).

3. **Word-wrap is O(n²) on long lines**
   `src/ui/text.c` `wrap_line`

   In the wrapping loop, for every character position it `malloc`s a length-prefixed
   substring, `memcpy`s it, and calls `TTF_SizeUTF8(font, substr, ...)` before
   `free`. That is one allocation + one full re-measure per character of the
   current line → quadratic cost on long wrapped text. Not a per-frame hot
   path (layout/one-off), so severity is low, but it is wasteful for large
   description blobs.

   Recommendation: measure incrementally (advance the cursor and compare
   cumulative width against `max_w`) instead of re-measuring a fresh substring
   per character, or binary-search the break position between the last good and
   first overflowing indices.

**INFO observations:**

4. **Icon rasterization can occur mid-frame on cache miss**
   `src/overlay/grid_render.c` via `cbx_icon_lookup` / `src/icons/icon_lookup.c`
   and `src/icons/icon_cache.c`

   `cbx_icon_lookup` calls `cbx_icon_cache_load_one` (blocking disk read +
   malloc buffer + `nsvgRasterize` + `SDL_UpdateTexture`) as a side effect of a
   render when a requested icon is absent. Preloading from the icon map covers
   map entries, but a profile/device-type *icon override* referencing an
   uncached built-in or PNG would trigger a rasterize inside the frame path.
   It runs once (then cached), so impact is a single-frame hitch, but the
   render path would be cleaner if all render-visible icons were guaranteed
   preloaded or the cache-hit path were separated from the load path.

5. **Startup reconcile does synchronous DBus work in nested loops**
   `src/app/overlay_service.c` — `assigned_composite_for_slot`, and its callers
   in `cbx_reconcile_startup_targets`

   `assigned_composite_for_slot` is O(assignments × composites) and issues a
   blocking `ip_composite_get_persistent_id` DBus round-trip per composite,
   and it is called once per slot during startup/recovery. Counts are small and
   this is a one-time/restart path (never in the poll loop), so it is
   acceptable; worth revisiting only if physical controller or slot counts grow.
