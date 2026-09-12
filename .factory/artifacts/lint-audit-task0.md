# Lint Audit — Task 0 (Final audit)

Scope: `src/**/*.c` (67 files) and `src/**/*.h` (59 files); Python factory tooling under `.factory/loop/`.

Focus: language conventions, readability, comments, dead code, consistency.

## Summary

The C codebase is in strong shape for a linting pass. Naming is consistently
`snake_case` with a uniform `cbx_`/`ip_` module prefix; header guards are present
everywhere (using a consistent `CBX_<MODULE>_H` convention); comments explain
*why* and cite spec sections (e.g. `/* SPEC §4.5 */`) rather than restating code;
in-line comments on magic behavior are concise and valuable. No TODO/FIXME/XXX/HACK
markers remain. Automated dead-code analysis found **no unused static or exported
functions** (the only flagged names were false-positive macros/constants that are
all used). No **BLOCKER** issues.

The findings below are **WARN** (readability/maintainability) and **INFO**
(observations). None must block task completion, but addressing the top WARNs will
improve maintainability.

---

## Findings

### WARN 1 — Several monolithic functions exceed recommended size (readability)

Files with functions > 120 lines that are genuine complexity/size concerns (YAML and
DBus parse routines are inherently verbose and are lower priority; the interactive
/render/init ones should be decomposed):

| File | Function | Lines | Notes |
|------|----------|-------|-------|
| `src/overlay/grid_render.c` | `cbx_select_grid_render` | 300 (334-633) | render loop + layout; could split layout/draw helpers |
| `src/app/overlay_service.c` | `run_overlay_service` | 272 (1380-1651) | linear init sequence with section comments; could extract setup stages |
| `src/manager/manager.c` | `cbx_manager_init_with_dbus` | 241 (334-574) | initialize + event wiring |
| `src/manager/manager.c` | `cbx_manager_handle_event` | 220 (766-985) | dispatch/event handler |
| `src/manager/profile_diagram.c` | `load_svg_texture` | 183 (183-365) | SVG raster path |
| `src/config/config_profile.c` | `parse_profile_events` | 172 (258-429) | YAML parser (accepted for domain) |

**Recommendation:** Decompose the interactive/render functions (grid render, manager
init/handle-event, `run_overlay_service`) into smaller named sub-steps matching their
existing `/* --- N. section --- */` markers. YAML/DBus parsing routines are acceptable
as-is but would benefit from a per-state helper if touched again.

### WARN 2 — No trailing newline at EOF in every C file (POSIX/consistency)

All 126 `src` `.c`/`.h` files end with `}` and **no terminating newline** (verified
with `tail -c1`). POSIX and the project's own C toolchain expect a final newline; its
absence can trigger warnings on some compilers/packaging and looks like a
templating/`sed` artifact applied uniformly.

**Recommendation:** Add a trailing newline to each source file (single bulk pass, e.g.
`find src \( -name '*.c' -o -name '*.h' \) -exec sed -i -e '$a\' {} +`), and fix the
generator/template that strips it so it does not regress. Low risk, cosmetic.

### WARN 3 — Duplicated `MAX_DOC_SIZE` macro across 5 files (maintainability/drift risk)

`#define MAX_DOC_SIZE (1024 * 1024)` is redefined identically (with *inconsistent*
formatting) in:

- `src/config/config_profile.c:45`
- `src/config/config_assignments.c:36`
- `src/config/config_settings.c:34`
- `src/config/config_profile_meta.c:35`  (note extra space + `/* 1 MB */`)
- `src/icons/icon_map.c:46`  (same drift)

The value is identical today, but duplicated constants drift independently and the
formatting inconsistency (`(1MB)` vs `( 1024*1024 )   /* comment */`) is noise.

**Recommendation:** Hoist `MAX_DOC_SIZE` into a shared config header (e.g.
`src/config/config_internal.h` or a common `internal.h`) and include it from the five
translations units. Also sweep for other repeated module-local layout constants
(e.g. the `HEADER_H`/`LABEL_W`/`PROFILE_W` used in the grid renderer) if they are
duplicated across `ui` modules.

---

### INFO 1 — Header-guard convention exception in `src/controllerbox.h`

Every header uses a `CBX_<MODULE>_H` guard except `src/controllerbox.h`, which uses
`CONTROLLER_BOX_H`. Minor inconsistency; align it to the `CBX_*` convention for
uniformity (or rename the guard regardless).

### INFO 2 — Python factory scripts exceed 100-char line length

`py_compile` passes for all `.factory/loop/*.py`, but several files contain lines
over 100 characters (`issues.py`, `campaign.py`, `selector.py`, `parallel.py`,
`metrics.py`). This is factory tooling, not product code, and only marginally outside
PEP 8's line-length guidance. Optional.

### INFO 3 — Verbose-comment pass came back clean

Sampled the largest files (`overlay_service.c`, `grid_render.c`, `dbus_client.c`,
`config_profile.c`): comments are concise, they explain *why* / invoke spec
references / note edge cases (`/* no SA_RESTART — interrupt SDL_PollEvent */`), and
do not restate the adjacent code. No action required.

---

## Conclusion

No blocking lint issues. The code follows its language and project conventions
consistently (snake_case + `cbx_` prefix, guards everywhere, purposeful comments,
no dead code, no leftover TODOs). The three **WARN**s are readability and
maintainability improvements (long function decomposition, EOF newlines, macro
hoisting); the **INFO** items are minor polish.
