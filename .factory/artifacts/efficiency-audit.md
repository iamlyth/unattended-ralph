# Efficiency Audit — Task 0 (Final Audit)

Scope: performance, resource usage, algorithmic complexity across the controller-box
codebase (overlay service, manager, render path, icon/text caches, DBus layer).

Overall, the architecture is sound for its workload: the overlay service is
event-driven (100 Hz poll, renders only when its surface is dirty), the icon and
text caches are proper open-addressing hash maps with sane load factors (icon
128/256, text 256/512), and the DBus sd-bus layer correctly unrefs every message,
reply, slot, and error and frees heap strings on all paths including failures.

No findings block completion; all findings below are WARN/INFO.

---

## WARN — Texture leak when the text cache is full

**Files:** `src/ui/text.c` (`cbx_text_render`, `render_to_texture`)

When `cbx_text_render` cannot find a free cache slot (`find_free_slot` returns
-1, table full/256 entries) or the text exceeds `CBX_TEXT_MAX_LEN`, it returns a
fresh texture from `render_to_texture()` that is *not* stored in the cache and
is *never* freed:

```c
idx = find_free_slot(cache, h);
if (idx < 0) {
    /* Cache full — render without caching. */
    return render_to_texture(cache, font, text, color);   /* leaked */
}
```

Callers (`grid_render.c`, `widget_list.c`, `widget_label.c`, `widget_button.c`,
`widget_tabbar.c`) only `SDL_RenderCopy()` the returned texture and never destroy
it, because they assume all textures are owned by the cache. So under cache-full
pressure, every subsequent render of a not-yet-cached string leaks an SDL texture.

**Recommendation:** Cache-own only, and on cache-miss/full return NULL (callers
already tolerate a NULL texture — they check it before copying). Alternatively,
return a reference-counted or caller-owned texture with an explicit destroy, or
evict an LRU entry instead of rendering-uncached-and-leaking.

---

## INFO — `wrap_line` is O(n²) with a malloc per character

**Files:** `src/ui/text.c` (`wrap_line`, `cbx_text_render_wrapped`)

The wrapping loop allocates a substring and calls `TTF_SizeUTF8` for every cursor
position to find the break point:

```c
int len = (int)(cursor - start) + 1;
char *substr = malloc(len + 1);
...
TTF_SizeUTF8(font, substr, &w, &h);
free(substr);
```

For a paragraph of length L this is O(L²) font measurements plus O(L) temporary
allocations. Note: no production caller currently uses `cbx_text_render_wrapped`
(only unit tests), so the practical impact is currently nil, but this is the
obvious future hot spot for profile/description text.

**Recommendation:** Approximate the break point by estimating width per glyph
(or binary search) and only measure the candidate, or grow the substring
incrementally reusing a buffer instead of a fresh malloc per char.

---

## INFO — `cbx_dirty_rect_merge` O(n²·passes) and the full-buffer fallback union

**Files:** `src/ui/dirty_rect.c`, `src/ui/dirty_rect.h` (`CBX_DIRTY_RECT_MAX 64`)

`cbx_dirty_rect_merge` is a repeated two-pass pairwise overlap test with a
`do { changed } while (changed)` loop — worst case roughly O(max² · passes ≈ 64³)
cheap rect ops per surface render. That is acceptable at N=64, but note that
`cbx_dirty_rect_add` (used a lot for the overlay) when full silently unions new
rects into entry 0, which grows toward full-screen and then defeats clipping.

More importantly, the whole mechanism is used for the grid which is small and
event-driven, so the cost is minor. This is a maintenance/design note, not a
visible bottleneck.

**Recommendation:** If it stays, replace the quadratic merge with a single pass
that unions greedily using an area-gain heuristic during `add` (merging a new
rect only when the union-cost is bounded), which removes the need for the full
merge pass.

---

## INFO — Grid render relies on SDL clipping rather than skipping work; override/icon lookups repeated per cell

**Files:** `src/overlay/grid_render.c`, `src/icons/icon_lookup.c`,
`src/config/config_settings.c`

`cbx_select_grid_render` always iterates every row and every column and issues
full-area background fill and cell draws even when the dirty region is a small
sub-rect. It relies on `SDL_RenderSetClipRect` to reject overdraw rather than
skipping the per-cell work (icon lookup, text render, indicator draws). For the
small grid (controllers × slots) and event-driven redraws this is cheap, so it's
only an observation:

- `cbx_settings_icon_override()` (linear scan over the overrides list) and
  `cbx_icon_lookup()` are recomputed per cell on every render rebuild, even
  though the same device type recurs across rows. The override result could be
  hoisted per-column (cached in the render ctx or the grid build).
- On the absolute-PNG override path, `cbx_icon_lookup` recomputes full path
  validation on cache miss via `cbx_icon_validate_path` (multiple `realpath`
  calls plus config/user dir resolution). This only runs once per PNG key (then
  cached), so it is startup-ish — fine, but worth caching if icons are ever
  toggled frequently.

---

## INFO — Linear scans in assignment resolution during rebuild

**Files:** `src/identify/assign.c` (`cbx_assign_find_index`,
`cbx_assign_lowest_free_slot`)

`cbx_assign_lookup` is linear per row (`find_index`), and
`cbx_assign_lowest_free_slot` is O(slots × assignments) because it re-scans via
`cbx_assign_slot_occupied` for each slot. Grid build loops these per composite →
O(rows × assignments) overall. This runs only on reconcile/hotplug/restore, not
per frame, and counts are small (≤ 8 controllers, few assignments).

**Recommendation:** If assignment counts grow, sort-by-slot or maintain a slot
occupancy bitmap; not worth it at current sizes.

---

## Summary

No correctness or blocking performance issues. The only genuine resource concern
is the WARN-level texture leak in `cbx_text_render`'s cache-full path; everything
else is minor algorithmic/design polish on small-N, event-driven paths. Overall,
resource handling (especially the DBus layer) is careful and correct.
