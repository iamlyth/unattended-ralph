## Subsystem Study Report: `src/ui/`

`src/ui/` is the SDL2 widget toolkit and rendering layer for Controller-Box. It
provides renderer init, theming, text caching, the widget base class + concrete
widgets, spatial focus navigation, controller-input-to-SDL mapping, animation
tweens, and dirty-rect tracking. It is a leaf-ish library consumed by the
manager (`src/manager/`), the overlay (`src/overlay/`), and the app entry
point (`src/app/overlay_service.c`). All UI files are Task-anchored (Tasks
19–24).

---

### Files & responsibilities

**Core infrastructure**

- `renderer.h/.c` — Central `cbx_renderer` struct (SDL window+renderer pair).
  `cbx_renderer_init()` creates a hidden window and an accelerated renderer
  with `SDL_RENDERER_TARGETTEXTURE`, falling back to a software renderer (sets
  `is_gles=true`) when target textures are unsupported (Pi 4 GLES). Verifies
  target-texture support (`cbx_renderer_check_target_texture`) and alpha
  blending via a read-back probe (`cbx_renderer_verify_blending`, non-fatal).
  `show/hide/present/clear/shutdown` are thin SDL wrappers. Shared by both the
  manager and the overlay service.

- `theme.h/.c` — `cbx_theme` colour struct. `cbx_theme_load()` fills it from
  `cbx_settings` (theme name + `overlay_opacity`). Only `"default"` is known
  (`cbx_theme_is_known`); unknown names fall back. `cbx_theme_default()`
  defines the dark palette. `cbx_theme_apply_opacity()` clamps 0–1 and sets the
  `overlay_bg.a` channel. Exposed on the manager as `mgr->theme`.

- `text.h/.c` — `cbx_text_cache` font + texture cache via SDL2_ttf. Fonts
  loaded by path→ID (`cbx_text_load_font`, max 8). Texture cache is an
  open-addressing hash map (512 slots, ≤256 entries) keyed by djb2 hash of
  (font_id, text, RGB — alpha excluded). `cbx_text_render()` returns cached
  texture or renders via `TTF_RenderUTF8_Blended`. `cbx_text_render_wrapped()`
  word-wraps (`wrap_line`), renders multi-line. `cbx_text_measure`,
  `cbx_text_line_height`, `cbx_text_get_dims`, `cbx_text_cache_clear` (theme
  change), `cbx_text_cache_cleanup` (frees fonts). Tombstone/hash scheme for
  probing: hash 0 = empty, 1 = tombstone.

- `dirty_rect.h/.c` — `cbx_dirty_rect` tracks up to 64 regions.
  `cbx_dirty_rect_add` clamps to screen, unions into slot 0 when full.
  `cbx_dirty_rect_merge` collapses overlapping/adjacent rects.
  `cbx_dirty_rect_render` iterates clip-rect render passes
  (`SDL_RenderSetClipRect`); empty list ⇒ full-screen first paint.
  Used for incremental overlay re-render (surface_build).

- `animation.h/.c` — `cbx_ease_eval()` pure easing (linear/in/out/in-out);
  `cbx_anim` `SDL_GetTicks()`-driven alpha tween state machine (IDLE/RUNNING/
  COMPLETE). `cbx_anim_start/update/stop`, `fade_in/fade_out` conveniences.
  Used for overlay fade (lifecycle).

**Widget base**

- `widget.h` — `cbx_widget` base struct (first member of every concrete
  widget, C-inheritance) with `cbx_widget_vtable` (draw, handle_event,
  focus, blur, get_rect, set_rect, destroy). All concrete widget structs
  declared here: Button, Label, Image, Panel, List, Grid, TabBar, Progress.
  `widget.c` — NULL-safe generic dispatchers (`cbx_widget_draw`, ...) that
  forward through the vtable and skip when `!visible`.

**Concrete widgets**

- `widget_button.c` — Button. States normal/focused/pressed. Handles mouse
  click + keyboard Return/Space/`a`. `render_label()` re-renders label texture
  in focus colour. `cbx_button_init/set_label/set_press_cb/set_pressed`.
  Label texture borrowed from text cache (not owned).
- `widget_label.c` — Label, non-interactive; single-line or multiline
  (`strtok_r` on `\n`). `cbx_label_init/set_text/set_color/set_multiline`.
- `widget_image.c` — Image: scale modes FIT/FILL/CENTER; optional texture
  ownership (`owns_texture` ⇒ destroy() frees it).
- `widget_panel.c` — Panel container (≤32 children, borrowed). Draws bg/border
  and children; forwards events to `focused_child`; focus chain helpers
  `cbx_panel_focus_first/next/prev`. Does NOT own children.
- `widget_list.c` — Scrollable List (≤64 items): up/down nav, scroll offset,
  selected highlight, optional icon, keyboard + mouse wheel + click.
  `on_select` fires on activate (KEYUP). `compute_visible`,
  `ensure_scroll_visible` helpers.
- `widget_grid.c` — Grid (≤256 cells, rows×cols): independent row/col cursor
  nav, draws cells + focus highlight. Does NOT own cells.
- `widget_tabbar.c` — Horizontal TabBar (≤16 tabs): left/right, mouse click,
  active-tab accent + underline, `on_change` callback.
- `widget_progress.c` — ProgressBar (0–1 fill), configurable colours,
  non-interactive.

**Navigation & input bridging**

- `focus.h/.c` — `cbx_focus_chain` spatial focus manager (≤64 entries with
  row groups). `cbx_focus_chain_add/focus/navigate`. `find_neighbor` scores
  candidates by axis-weighted distance. Modes: PLAYER (up/down restricted to
  current row) vs HOST (left/right restricted to row, up/down crosses rows).
- `input_map.h/.c` — Bridges InputPlumber `ip_input_id`/`ip_input_category`
  → synthetic SDL events. Static tables `s_button_map` (button→SDLK) and
  `s_axis_map` (axis→directional id). `cbx_input_map_to_sdl_event`,
  `cbx_input_map_keycode`, `cbx_input_map_axis_direction`. Uses
  `CBX_CONTROLLER_EVENT_WINDOW_ID` (`UINT32_MAX`) to mark controller-sourced
  key events.

---

### Entry points (called from outside `src/ui/`)

- `cbx_renderer_init/show/hide/present/clear/shutdown` — `src/manager/manager.c`
  (`cbx_manager_init_with_dbus`), `src/app/overlay_service.c`
  (overlay activate/deactivate paths).
- `cbx_text_load_font/render/render_wrapped/line_height/...` — 14 files across
  manager/overlay/app (labels, buttons, list rows, profiles tab).
- `cbx_theme_load` — `src/manager/manager.c`.
- Widget constructors + vtable dispatchers (`cbx_button_init`, `cbx_label_init`,
  ... `cbx_widget_draw`) — `manager.c`, `overlay/surface_build.c`,
  `profiles_tab.c`, `controllers_tab.c`, etc.
- `cbx_focus_chain_*` — **only** `src/manager/manager.c` (single production
  consumer).
- `cbx_anim_*` + `cbx_dirty_rect_*` — `src/overlay/surface_build.c`,
  `src/overlay/lifecycle.c` (pre-built overlay render + fade).
- `cbx_input_map_to_sdl_event` — **no production consumer outside tests**;
  consumed only by `tests/test_input_map.c`. It is the declared bridge (Task
  22) but controller→navigation currently flows through a different path in
  the overlay player/host mode modules.

---

### Internal state

- `renderer.c`: `static const char *default_title = "Controller-Box"`.
- `text.c`: static only (hash helpers); cache state lives in the
  caller-owned `cbx_text_cache` (renderer pointer, font table, entry array,
  `entry_count`, `font_count`). Cache never evicts automatically.
- `input_map.c`: `static const` tables `s_button_map`, `s_axis_map`.
- All widget structs hold borrowed pointers (`text_cache`, `theme`) — widgets
  do not own the cache/theme (caller keeps alive until cleanup). Panels/grids
  do not own children (caller destroys). Buttons/labels/list items borrow their
  textures from the text cache; only `cbx_image` optionally owns its texture.

Lifecycle order (from `cbx_manager_init_with_dbus`, manager.c):
`cbx_renderer_init` → `cbx_text_cache_init` → `cbx_theme_load` → `cbx_text_load_font` →
build widgets → show/present/draw; teardown reverses with
`cbx_text_cache_cleanup` + `cbx_renderer_shutdown`.

---

### Error handling

Functions return errno-style codes (`0`, `-EINVAL`, `-ENOMEM`, `-EIO`,
`-ENOENT`, `-ENOTSUP`) with NULL/empty-input guards at every public entry.
Vtable dispatchers are NULL-safe (draw no-op, handle_event returns false).
SDL/ttf failures are logged to stderr with `SDL_GetError()`/`TTF_GetError()`.
Blending verification failure is non-fatal (warning only). Cache-full path in
`cbx_text_render` falls back to uncached render rather than failing (but that
texture is never freed → leak risk).

---

### Test coverage (tests/CMakeLists.txt, all cmocka, most with `SDL_VIDEODRIVER=dummy`)

These tests target `src/ui/` directly (test → function counts via
`cmocka_unit_test` above):

- `test_renderer_init` (16) — renderer init, target-texture / blending checks.
- `test_text` (34) + `test_font_init` — font path + text cache + wrapping.
- `test_widgets` (49) — Button/Label/Image/Panel base behavior.
- `test_widget_list` (24), `test_widget_grid` (16), `test_widget_tabbar` (14),
  `test_widget_progress` (12) — per-widget behaviors.
- `test_focus` (39) — focus chain add/navigate/mode/row restrictions.
- `test_input_map` (30) — button/axis → SDL event mapping.
- `test_animation` (45) — easing + tween state machine.

Higher-level integration coverage through the ui layer:
`test_overlay_visual`, `test_grid_render`, `test_player_mode`, `test_host_mode`,
`test_conflict`, `test_overlay_lifecycle`, `test_overlay_integration`,
`test_surface_build`, manager tabs/visual tests. UI primitives are also
exercised indirectly by the manager production/visual tests.

---

### Potential issues / gaps (for planner tasks)

1. **`input_map.c` never emits KEYUP for return-to-center** — header documents
   "Return-to-center (|value| ≤ threshold) → SDL_KEYUP for the last direction",
   but the axis branch of `cbx_input_map_to_sdl_event` returns `false` inside
   the deadzone and produces no SDL_KEYUP. A consumer holding state for the
   last direction would get stuck pressed. Implemented-vs-documented mismatch.
   Also the button branch has an unreachable `else` (`value>=0.5` / `<0.5`
   cover all cases).

2. **`cbx_input_map_to_sdl_event` has no production caller** — it is the
   declared input bridge (Task 22) yet controller→overlay navigation doesn't
   route through it. Either wire it into the overlay input path or confirm the
   alternate path and mark it intentionally-unused; otherwise dead code with
   drift risk.

3. **`animation.c` tick-wrap handling** — `if (now < start_ticks) elapsed = 0;`
   discards the correct unsigned-difference elapsed (~49-day wrap) in favour of
   zero; benign for short fades but incorrect in principle.

4. **`text.c` unbounded uncached textures on cache-full / long-text path** —
   when the cache is full (`find_free_slot < 0`) or text ≥
   `CBX_TEXT_MAX_LEN`, `render_to_texture` returns a texture that is **not**
   cached and **not** tracked, so it leaks. Long-running overlays with many
   unique strings could leak textures.

5. **Button activates on `SDLK_a`** — `button_handle_event` treats `SDLK_a` as
   a press toggle key alongside Return/Space. `SDLK_a` may collide with text
   entry; the `CBX_CONTROLLER_EVENT_WINDOW_ID` marker is not checked here, so a
   real keyboard 'a' activates focused buttons.

6. **`list_handle_event` MOUSEBUTTONUP divides by `lst->item_h` unguarded** —
   a 0 `item_h` would divide-by-zero. Currently safe (`DEFAULT_ITEM_H=32`,
   no setter), but unprotected.

7. **`focus.c` spatial edge cases** — if an entry's rect is zero-size
   (`w/h==0`), centroids degenerate and navigation may skip it; `update_rect`
   is the only way to fix after layout change. Also duplicate/additions of the
   same widget aren't deduped (two entries → self-navigation loops).

8. **Panels/grids borrow children; list icons are borrowed** — any caller that
   frees a child/icon without removing it first leaves dangling pointers in
   the parent (draw/event paths dereference `children[i]`/`items[i].icon`
   without ownership checks). The base structs don't track which children were
   stack-allocated.

9. **`dirty_rect_merge` O(n²) and `cbx_dirty_rect_add` full-list fallback** —
   when over `CBX_DIRTY_RECT_MAX`, extra rects union into slot 0 (correct but
   can over-invalidate); acceptable for a ≤64-region overlay but worth noting
   if region counts grow.

10. **Renderer `vsync_enabled`/`is_gles` are best-effort diagnostics** — GLES
    detection by `strstr(info.name, "gles")` is heuristic; not an error, just
    fragile for diagnostics/tests that assert on these flags.
