# Subsystem Study Report: src/icons

Runtime icon resolution subsystem: maps InputPlumber `DeviceType` strings to SVG/PNG
textures and human-readable labels, applying profile overrides (SPEC §8.3–§8.5).

## Files

**`src/icons/icon_map.h` / `icon_map.c`** — DeviceType → {icon, asset, name} mapping.
- Parses `data/controller-icons.yaml` (`virtual_types` + `custom_icons`) with **libyaml
  event-based** parsing. Security: max doc 1 MB, max depth 50, rejects all YAML tags
  (`check_event_tags` → `-EPERM`), bounded field/entry counts, bounded key buffer.
- Key functions:
  - `cbx_icon_map_init(map)` — zeroes struct.
  - `cbx_icon_map_load(map, path)` / `cbx_icon_map_parse(map, yaml, len)` — parse into
    `map->entries[]`; sets `map->loaded=1` on success only. `load` opens with
    `O_RDONLY|O_NOFOLLOW`, enforces 1 MB via fseek/ftell, reads whole file.
  - `cbx_icon_map_lookup(map, type, out_icon, out_name)` — linear scan for exact type
    match; unknown/NULL-map → `CBX_ICON_DEFAULT_ICON` ("generic-gamepad") + raw type as
    name. Returns 0 (always succeeds).
  - `cbx_icon_map_default_path(out_path)` — install `DATA_DIR/controller-icons.yaml`,
    falls back to `SOURCE_DATA_DIR`; both must be non-symlink regular files (lstat checks).
- State: `cbx_icon_map{ entries[64], count, loaded, yaml_path }`. No global state.

**`src/icons/icon_cache.h` / `icon_cache.c`** — SVG→SDL2 texture rasterization cache.
- One reusable `NSVGrasterizer` (nanosvg); open-addressing djb2 hash table
  (256 slots, linear probe, max 128 entries). Textures use `SDL_BLENDMODE_BLEND`;
  recolourable via `SDL_SetTextureColorMod` (§8.3 theme).
- Key functions:
  - `cbx_icon_cache_init(cache, renderer, icon_dir, target_size)` — creates rasterizer;
    **calls cleanup first if already initialized** (re-init safe). Returns -EINVAL/-ENOMEM.
  - `cbx_icon_cache_load(cache, map)` — batch-rasterizes all mapped icons, skips
    already-cached (idempotent). Individual failures logged, not fatal.
  - `cbx_icon_cache_load_one(cache, icon_name)` — derives filename: strips `cc-` prefix,
    appends `.svg`. Validates no `/`, `..`, leading `.` in key.
  - `cbx_icon_cache_load_asset(cache, icon_name, filename)` — strict variant used by the
    profile-editor diagram; `filename` must be a simple `.svg` basename; rejects symlinks
    & non-regular files via `lstat`.
  - `rasterize_svg_file` — validates dims finite/>0, guards float→int & buffer-size
    overflow (`-EOVERFLOW`), allocates RGBA, **rejects fully-transparent assets**,
    creates texture, `insert_entry`.
  - `cbx_icon_cache_get` / `get_dims` / `insert` / `cleanup`.
- `rasterize_svg_file` returns -EINVAL for transparent/oversized/malformed assets
  (defensive: profile-editor diagram adopts these textures directly).

**`src/icons/icon_lookup.h` / `icon_lookup.c`** — single runtime entry point.
- `cbx_icon_lookup(cache, map, device_type, icon_override, cbx_icon_result*)` resolves
  texture+label per SPEC §8.5 order: (1) absolute-path override → PNG via `SDL2_image`,
  (2) built-in icon-name override, (3) map lookup, (4) generic-gamepad fallback.
  Result texture is **owned by the cache** (caller must not destroy). `label` always set
  even if texture is NULL.
- `cbx_icon_validate_path(abs_path, resolved, size)` — security gate for absolute PNG
  overrides: must be absolute, `path_is_safe` (reject `..` components), `realpath()` must
  succeed, canonical path must be within user config dir / user profiles dir / system data
  dir / system InputPlumber dir (`path_within` boundary check). Returns -EINVAL/-EACCES/
  -ENAMETOOLONG. Backed by config dir helpers (`cbx_resolve_config_dir`,
  `cbx_resolve_user_profiles_dir`, `cbx_data_dir`, `cbx_system_inputplumber_dir`).
- Internal `load_png` stores PNG texture under the raw (non-canonicalized) override path
  as cache key.

## Entry points (consumed outside subsystem)
- `cbx_icon_map_init/load/lookup/default_path/parse` — used by `src/app/overlay_service.c`
  and `src/manager/profile_editor_list.c`.
- `cbx_icon_cache_init/load/load_one/load_asset/get/get_dims/cleanup/insert` — used by
  overlay_service (default grid 48px) and profile_editor_list (diagram raster).
- `cbx_icon_lookup` + `cbx_icon_validate_path` — used by `src/overlay/grid_render.c`
  (per-cell `DeviceType`→icon in the selection grid).

## Callers / interface
- **`src/app/overlay_service.c`** (init ~L1473, cleanup L1515/1546/1645): 
  `cbx_icon_map_default_path` → `cbx_icon_map_load` → `cbx_icon_cache_init(...,cbx_icon_dir(),48)` →
  `cbx_icon_cache_load`. Best-effort (failures logged, not fatal to startup). Render context
  carries `&icon_cache`/`&icon_map` to `grid_render`.
- **`src/overlay/grid_render.c`** (L522–557): calls `cbx_icon_lookup` per cell, honors
  settings-level override (`cbx_settings_icon_override`), scales icon to fit cell, renders.
- **`src/manager/profile_editor_list.c`** (L280–502): loads map + cache for diagram; uses
  explicit `cbx_icon_cache_load_asset` with catalog asset filenames (strict path), applies
  provenance tracking for the diagram.

## Internal state & lifecycle
- Single-threaded consumers (SDL event loop) — the cache/map structs are plain data;
  **no locking** in the subsystem. Correct only because all callers run on one thread.
- Cache owns textures + rasterizer; `cleanup()` destroys all and is re-init-safe.
- Renderer is borrowed (caller keeps alive until cleanup).
- YAML `asset` field is read into map but **not used by batch `cbx_icon_cache_load`**;
  batch relies on the `cc-` prefix-strip convention in `load_one`.

## Error handling
- Negative errno returns throughout (-EINVAL/-ENOMEM/-ENOENT/-EACCES/-EFBIG/-EPERM/-EIO/-ENAMETOOLONG/-EOVERFLOW); callers log best-effort failures and continue.
- `icon_lookup` never fails on a missing icon: returns label with `texture=NULL`; falls
  back through override→map→generic-gamepad.

## Test coverage (all pass: `ctest -R test_icon`, Test #29–31)
- **`test_icon_map.c`** (854 lines, ~40 tests): init, parse basic/custom/empty,
  lookup known/unknown/ds5/NULL-map/unloaded/NULL-type/NULL-outputs/small-buffer,
  default-path, load nonexistent/null/tempfile, tag rejection (-EPERM), too-large
  (-EFBIG), many-entries, full YAML, unknown→raw-type, icon-only/name-only lookups,
  missing icon/type, long-type truncation, reparse reset, `test_svg_*` mapping to installed SVGs.
- **`test_icon_cache.c`** (552 lines, ~25 tests): init/basic/null, load all/texture/
  dims/aspect/idempotent/null, get known/unknown/null/empty, dims, load_one new/cached/
  nonexistent/traversal (`/`, `..`, leading `.`), recolour (color mod), blend mode,
  cleanup/null, shared-icon dedup, target size, hash-collision lookup, production-path load.
- **`test_icon_lookup.c`** (713 lines, ~40 tests): known/unknown/NULL/empty device type,
  NULL map, builtin override (incl. nonexistent), empty override, PNG override (cached,
  nonexistent, traversal, relative), validate-path (absolute safe, not-absolute, empty,
  NULL, traversal, `..` mid, `..` prefix, `..` only), NULL cache/result, labels, on-demand
  loads, dims, PNG dims, PNG in user-config-dir, cache insert/replace/null-args.
- Cross-tests touching icons indirectly: `test_overlay_visual`, `test_golden`,
  `test_manager_visual`, `test_profile_diagram`, `test_editor_list_mode`,
  `test_installed_diagram.sh`, `test_installed_functional`.

## Potential issues / gaps
1. **Batch load ignores the `asset` field** (icon_map.c ↔ icon_cache.c). `cbx_icon_cache_load`
   uses `cc-`-strip derivation; the profile-editor strict path (load_asset via catalog)
   honours `asset`. Currently the YAML is internally consistent (icon name↔filename
   convention holds), so no live bug — but the two paths can diverge if a future YAML entry
   names an icon whose file doesn't match the `cc-` convention. No test asserts asset-aware
   batch loading. *Potential planner task: have batch load honour `map->entries[i].asset`.*
2. **PNG override cache key not canonicalized** (`icon_lookup.c`). `load_png` validates with
   `realpath()` but caches under the raw `icon_override` string and the pre-load
   `cbx_icon_cache_get` uses the raw string. Two equivalent paths (`/x/./y.png` vs
   `/x/y.png`) validate fine but produce duplicate cached textures (minor memory waste; not
   a correctness or security bug).
3. **`path_is_safe` allows `.` components** (only `..` rejected); safe because `realpath()`
   canonicalizes and `path_within` is a boundary-checked prefix match. Verified hardening is
   tested (`test_validate_path_*`).
4. **No thread-safety** on the cache/map. Safe for current single-threaded consumers, but
   a race would corrupt the hash table if ever accessed concurrently. Worth a comment/
   invariant assertion if multi-threaded rendering is contemplated.
5. **Dead tombstone branch** in `get`/`get_dims`: a slot with `name` set but `texture==NULL`
   ("tombstone") never occurs in practice because there is no delete-path and `insert_entry`
   always sets the texture and `cleanup` always resets names. Harmless, but the branch
   signals an intent (deletion) that isn't implemented — if deletion is ever added, the
   incremental-probe logic must be revisited.
6. **`cbx_icon_cache_init` destructive re-init**: calling init on an already-initialized
   cache destroys existing textures/rasterizer and, if the rasterizer alloc then fails,
   leaves the cache zeroed (renderer NULL). Callers should treat double-init as
   reset-then-fail; current call sites only init once.
7. **`cbx_icon_map_load` reads into an unbounded fd with `fdopen(f,"rb")`** — the FILE* is
   closed via `fclose` in all paths; fd is leaked-safe. File-size cap prevents `ftell`/malloc
   surprises on huge files. Sound.

## Summary for planner
The subsystem is well-structured, secure (YAML tag/size/depth limits; PNG path validation;
symlink rejection for assets), and thoroughly tested (3 dedicated suites, ~105 tests, all
green). The main actionable improvement is **option (1): make batch `cbx_icon_cache_load`
honour each map entry's explicit `asset` filename** so icon-name→SVG-file resolution is
data-driven rather than convention-dependent — this is the only genuine divergence risk
between the grid-render path and the profile-editor diagram path. Optional hardening:
canonicalize the PNG cache key (2) and document the single-thread ownership invariant (4).
