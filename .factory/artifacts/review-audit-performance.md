# Performance / Resource / Complexity Audit — Task 0 (Final audit)

Scope: hot paths reviewed — overlay render loop, overlay surface + dirty-rect,
icon cache, text cache, grid render, conflict/host-mode detection, and the
manager render loop. Correctness, security, and packaging were out of scope.

## Findings

### 1. Manager main loop is an unthrottled busy render loop — **WARN**
**Files:** `src/manager/manager.c:656–673`, `src/manager/manager.c:1173–1178`, `src/ui/renderer.h:27`

`cbx_manager_run` spins `while (mgr->running) { PollEvent; process DBus; cbx_manager_refresh_controllers_if_due; cbx_manager_render(mgr); cbx_renderer_present(&mgr->rend); }` with **no `SDL_Delay`, no `SDL_WaitEvent`, and no `SDL_RENDERER_PRESENTVSYNC`** flag (`CBX_RENDERER_FLAGS_ACCEL = SDL_RENDERER_ACCELERATED | SDL_RENDERER_TARGETTEXTURE`). `cbx_manager_render` unconditionally clears and redraws the entire tab bar + active panel every iteration, even when there were zero events.

Consequence: while the manager is up (it also owns a live DBus connection and does dispatch every iteration), it re-renders the full frame and pegs a CPU core at maximum rate even when nothing changed. On a Pi 4 / GLES where vsync is not guaranteed, both code paths leave the loop unthrottled.

Recommendation:
- Add `SDL_RENDERER_PRESENTVSYNC` to `CBX_RENDERER_FLAGS_ACCEL`, **or** pace the loop with a small `SDL_Delay` (or switch to blocking event wait when idle).
- Add a dirty/needs-render flag so `cbx_manager_render` + `SDL_RenderPresent` only run when a widget/event actually invalidates the frame, instead of clearing + redrawing + presenting on every spin. This mirrors the overlay path, which already gates re-render on `cbx_overlay_surface_is_dirty`.

Note: the overlay service main loop (`src/app/overlay_service.c:1630–1633`, `step` at ~1256) is correctly gated by `SDL_Delay(10)` and dirty-rect `is_dirty()`; it is not affected.

### 2. `wrap_line` is quadratic with a per-character heap allocation — **WARN**
**Files:** `src/ui/text.c:289–380` (esp. `:326–336`), used via `cbx_text_render_wrapped` (`src/ui/text.c:430`)

`wrap_line` walks every character; at each position it `malloc`s a substring covering `start..cursor` and calls `TTF_SizeUTF8` over that whole growing substring. That is O(n) allocations + O(n) text-measurement per position → O(n²) in line length, and it happens for every wrapped call. Not currently on the overlay hot path (only exercised through profile-editor/diagram and `tests/test_text.c`), so severity is not blocking, but the algorithm is wasteful.

Recommendation: measure once per word boundary instead of per character, or track the incremental width; allocate/measure at candidate break points only. Keep the hard-break fallback for over-long words, but avoid a fresh `malloc`/`memcpy` + `TTF_SizeUTF8` for every cursor position.

### 3. Dirty-rect merge worst case is O(n³), bounded by a small cap — **INFO**
**Files:** `src/ui/dirty_rect.c:172–205` (`cbx_dirty_rect_merge`)

The merge loop restarts from the top (`do { … } while (changed)`) and does a full pairwise scan (`rects_overlap_or_adjacent` + union) after each successful merge, giving worst-case O(n³). It is bounded by `CBX_DIRTY_RECT_MAX = 64` (`src/ui/dirty_rect.h:28`), so per-frame cost is small and acceptable. Worth a note only: a single-pass sweep or index would remove the cubic worst case, but it is not a practical bottleneck at this cap.

### 4. Per-frame re-derivation in grid render — **INFO**
**Files:** `src/overlay/grid_render.c:369–630`

On each dirty render the row label is rebuilt with `snprintf` and re-hashed via `cbx_text_render` (`src/overlay/grid_render.c:454`), `SDL_QueryTexture` is called for every cached texture, and `cbx_icon_lookup` (string map lookup + label `snprintf`) runs per cell. All of these hit the caches (text/icon caches keep the actual rasterization out of the frame), so each per-frame operation is cheap constant work. No allocation is introduced in the frame path (stack buffers + cached textures). Only observation: with small grids this is fine; if grids grow, hoisting per-texture dimensions (cache stores `width`/`height` already — `src/ui/text.c` `cbx_text_get_dims`) would skip the `SDL_QueryTexture` calls.

## No blocking findings

No correctness-affecting or leak-class performance defects found. Memory ownership in the render/cache/DBus paths in scope is balanced (surface textures, icon rasterizer, text textures, wrapped-line arrays are all freed on the documented cleanup paths). Caches have proper size caps.

Exit code: 0.
