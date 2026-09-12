## Subsystem Study Report: `src/ui`

A self-contained SDL2 widget toolkit plus rendering, theming, text, focus, input-mapping, animation, and dirty-rect primitives. It is the UI layer for both the **manager** app and the **overlay** service. Everything is built into the `controllerbox` library (all 16 `.c` files are listed in `CMakeLists.txt` lines 134–148).

### Architecture overview

- `widget.h` defines a base `cbx_widget` with a **function-pointer vtable** (`cbx_widget_vtable`: draw / handle_event / focus / blur / get_rect / set_rect / destroy). Every concrete widget embeds `cbx_widget` as its first member (C struct inheritance). `widget.c` provides NULL-safe generic dispatchers.
- Concrete widgets: Button, Label, Image, Panel, List, Grid, TabBar, ProgressBar.
- Supporting modules: `renderer`, `theme`, `text`, `focus`, `input_map`, `animation`, `dirty_rect`.

### Files + roles

**Widget framework**
- `widget.h` / `widget.c` — Base struct, `cbx_widget_vtable`, generic dispatchers, visibility/focus accessors. No state beyond per-widget fields. Entry point for all consumers.
- `widget_button.c` — `cbx_button`: label texture borrowed from text cache; focused/pressed visual states; handles mouse click + Return/Space/**A**. `render_label()` re-renders on focus colour change.
- `widget_label.c` — `cbx_label`: static text, multiline via `strtok_r` on `'\n'`. Non-interactive.
- `widget_image.c` — `cbx_image`: wraps an `SDL_Texture` with scale modes FIT/FILL/CENTER. Optionally owns the texture (destroy frees it).
- `widget_panel.c` — container (`CBX_PANEL_MAX_CHILDREN=32`), draws bg/border and children. **Does not own children** (caller frees). Only forwards events to the **focused child**.
- `widget_list.c` — `cbx_list` (64 items max), up/down nav, scroll_offset/visible_count, optional icon per item, mouse wheel + mouse click. Activate fires `on_select`.
- `widget_grid.c` — `cbx_grid` (256 cells), independent row/col cursor nav, highlight around current cell.
- `widget_tabbar.c` — `cbx_tabbar` (16 tabs), left/right + mouse, `on_change` callback on active switch.
- `widget_progress.c` — `cbx_progress`, fraction 0.0–1.0, configurable fill/bg colours, non-interactive.

**Supporting modules**
- `renderer.c` / `renderer.h` (Task 19) — SDL window+renderer init, `SDL_RENDERER_TARGETTEXTURE` verification, software/GLES fallback for Pi 4, `cbx_renderer_verify_blending()` alpha check, show/hide/present/clear/shutdown.
- `theme.c` / `theme.h` (Task 19) — dark colour palette. `cbx_theme_load()` reads theme name + `overlay_opacity` from `cbx_settings` (`config/config_settings.h`), applies opacity to `overlay_bg.a`. Only a "default" theme exists; unknown names fall back.
- `text.c` / `text.h` (Task 19) — font loading (`SDL2_ttf`, 8 fonts max) + open-addressing text-texture cache (512 slots, 256 entries) keyed by djb2 hash of `(font_id, text, r,g,b)`. `cbx_text_render()`, `cbx_text_render_wrapped()` (word wrapping), measure/line-height/clear/cleanup.
- `focus.c` / `focus.h` (Task 22) — `cbx_focus_chain` (64 entries, spatial nav). PLAYER mode restricts up/down to same row; HOST mode restricts left/right to same row. `cbx_focus_chain_navigate()` finds nearest centre in a direction.
- `input_map.c` / `input_map.h` (Task 22) — maps InputPlumber `ip_input_id`/`ip_input_category` to synthetic SDL key events (`cbx_input_map_to_sdl_event`), button tables and axis→directional mapping. Uses `CBX_CONTROLLER_EVENT_WINDOW_ID = UINT32_MAX` to tag controller inputs. **Not yet consumed by any runner code** (see integration gaps).
- `animation.c` (Task 23) — `cbx_anim` alpha tween (SDL_GetTicks based), easing types, fade in/out helpers. Stateless-per-call; caller polls `cbx_anim_update()`.
- `dirty_rect.c` (Task 23) — `cbx_dirty_rect` (64 rects) for incremental re-render; add/clamp, merge overlapping/adjacent, `cbx_dirty_rect_render()` applies `SDL_RenderSetClipRect`.

### Entry points (called from outside `src/ui`)

- `src/app/overlay_service.c/.h` — includes `renderer.h`, `theme.h`, `text.h`.
- `src/manager/manager.h/.c` — includes `renderer.h`, `widget.h`, `text.h`, `theme.h`, `focus.h`, `input_map.h`. Calls `cbx_focus_chain_navigate` (manager.c:910/919/926) and `cbx_focus_chain_focus_widget` (1056, 1163, 1165, 1278).
- `src/manager/*tabs*` — `settings_tab`, `controllers_tab`, `profiles_tab`, `profile_diagram`, `profile_editor_list` all consume widgets/text/theme/focus.
- `src/overlay/` — `grid_render.h` (text/theme), `lifecycle.h` (animation), `surface_build.h/.c` (dirty_rect; calls `cbx_dirty_rect_init/add/add_all/merge/render/clear`).

### Internal state / lifecycle

- No true global state; all state lives in caller-owned structs. Notable lifecycle pairing:
  - `cbx_renderer_init` ↔ `cbx_renderer_shutdown` (safe on zeroed struct).
  - `cbx_text_cache_init` ↔ `cbx_text_cache_cleanup`; `cbx_text_cache_clear` on theme change.
  - `cbx_anim_init` / `cbx_anim_start`; overlay `lifecycle` owns the anim.
  - Widget `*_init` functions zero the struct and assign the vtable before returning.
- Borrowed-pointer convention spans the whole subsystem: widgets borrow `theme`, `text_cache`, and label/icon `SDL_Texture`s — they never free them (except `cbx_image.owns_texture`).

### Error handling

- Convention: `0` on success, negative errno (`-EINVAL`, `-ENOMEM`, `-EIO`, `-ENOENT`, `-ENOTSUP`).
- Widget `init` functions return int; setters are void and NULL-safe no-ops.
- `cbx_text_render` returns NULL on failure (widgets silently skip the texture in draw).
- `cbx_renderer_verify_blending` failure is **non-fatal** (warning only) — overlay continues anyway.
- **Tautological / silent-failure risk (auditor-relevant):** several vtable draw methods (`button_draw`, `list_draw`, label/image/tabbar/progress) do not check `SDL_RenderCopy` / `SDL_QueryTexture` / `TTF_Render` return values — a failed glyph upload silently renders nothing, which is hard to distinguish from a legitimately empty widget.

### Test coverage (in `tests/`, all wired in `tests/CMakeLists.txt`)

Direct unit test binaries:
- `test_renderer_init.c` — renderer init + blending + target-texture checks (also used by `test_backend_smoke[-sw]`).
- `test_text.c` — text cache, fonts, wrapping, dims (also references theme).
- `test_widgets.c` — base dispatchers + Button/Label/Image/Panel.
- `test_widget_list.c`, `test_widget_grid.c`, `test_widget_tabbar.c`, `test_widget_progress.c` — dedicated per-widget.
- `test_focus.c` — focus chain modes + spatial nav.
- `test_input_map.c` — button/axis mapping (the ONLY consumer of input_map).
- `test_animation.c` — anim states/easing + dirty-rect API.
- Integration consumers: `test_surface_build.c` (dirty_rect), `test_overlay_lifecycle.c` (anim), `test_manager_*`, `test_overlay_*`, `test_grid_render.c`, `test_profile_diagram.c`, `test_golden.c`.

Visual/offscreen validation lives in `test_overlay_visual.c`, `test_manager_visual.c`, `test_golden.c` (framebuffer assertions).

### Potential issues / gaps for the planner

1. **`input_map` is not integrated.** `cbx_input_map_to_sdl_event` / `cbx_input_map_keycode` / `cbx_input_map_axis_direction` are referenced nowhere in `src/` outside `ui/input_map.c`, nor in any test but `test_input_map.c`. The manager event loop still maps controller→key or synthetic events through another path. If controller input is meant to drive the UI, this module is dead code pending wiring.
2. **Axis release never emitted.** `input_map.c` returns `false` (no event) for axes inside the deadzone and never synthesizes the `SDL_KEYUP` that the header doc (`input_map.h`) describes ("return-to-center → SDL_KEYUP for the last direction"). The mapping is stateless, so it cannot track "last direction". Callers must synthesize key-up themselves.
3. **Panel only routes events to the focused child.** `panel_handle_event` (widget_panel.c) forwards to `children[focused_child]` only — mouse clicks on a non-focused child (e.g. clicking a different Button) will not register unless that child is first focused. Fine for pure-controller nav; a bug for mixed mouse use inside a multi-widget panel.
4. **Uncached long-text texture leak.** `cbx_text_render` renders text `>= CBX_TEXT_MAX_LEN` (256) or when the cache is full *without caching*; the returned texture is not owned by the cache, but widgets store it as if cache-owned and their `destroy` never frees it → leak + per-frame re-render cost for long/full-cache text.
5. **Unused tombstone design in text cache.** `text.c` documents tombstones (hash==1) but only `cbx_text_cache_clear` resets entries (to hash=0); no deletion path creates tombstones. Dead design branch — `find_free_slot`'s tombstone check is effectively unreachable-in-practice.
6. **Theme load path barely tested.** `cbx_theme_load` (settings-driven, opacity application), `cbx_theme_apply_opacity`, and `cbx_theme_is_known` appear only as `cbx_theme_default(...)` calls in tests — there is **no `test_theme.c`** exercising load-from-settings, opacity clamping, or unknown-name fallback.
7. **Silent vtable draw failures** (see Error handling) — SDL render errors are unchecked in most widget draw paths.
8. **`cbx_dirty_rect_add_all` / `cbx_dirty_rect_render` full-screen path** do not clamp to non-zero screen dims; if init with 0×0 the clip rect/`add_all` rect is degenerate.
9. **Focus entries are borrowed and not invalidated.** `cbx_focus_chain` holds raw widget pointers (no ownership, no removal API besides `clear`); if a widget is destroyed while in the chain (e.g. panel child replaced), the chain keeps a dangling pointer — there is no `cbx_focus_chain_remove`.
10. **`cbx_tabbar_set_active` fires `on_change` on programmatic set too** (and guards `index == active`); `grid_set_dims` clears all cells on dim change without notifying, and `progress` clamps but `cbx_progress_get_fraction`/setters are consistent. Minor behavioral notes for tests.
