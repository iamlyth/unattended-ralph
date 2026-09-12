# Final Audit Findings

## efficiency Audit

I've completed the efficiency audit of the codebase. Report written to `.factory/artifacts/audit-efficiency.md`.

## Summary

**No BLOCKER findings.** The codebase is generally well-structured on resource management.

### WARN (2)
1. **Unbounded render busy loop in the Manager** (`src/manager/manager.c`, `src/ui/renderer.h`)
   The Manager's main loop calls `cbx_manager_render` + `cbx_renderer_present` every iteration with **no `SDL_Delay`** and no `SDL_RENDERER_PRESENTVSYNC`, and redraws the entire widget tree unconditionally. It runs at hundreds of FPS when idle, spinning CPU/GPU. Note: the overlay service *does* gate on dirty + `SDL_Delay(10)`, so the Manager is the inconsistent/inefficient path. Fix: add vsync flag or a frame pacing delay, ideally a dirty-flag gate.

2. **Per-cell column-constant lookups recomputed per row** (`src/overlay/grid_render.c`)
   `cbx_settings_icon_override` + `cbx_icon_lookup` for a column's icon run for every row×cell even though the value is constant per column. Low absolute cost but a redundant-work/hot-path smell; recommend hoisting column icon lookups out of the row loop.

### INFO (2)
- `src/ui/dirty_rect.c` — `cbx_dirty_rect_merge` worst-case O(n³), bounded to 64 entries, state-change only. Not practical.
- `src/overlay/conflict.c` — small O(rows²) scans, tightly bounded.

I verified resource lifecycle across the DBus layer (`sd_bus`/message unrefs, slot cleanup), text/icon texture caches, intercept-poll timers, and config/profile atomic writes — all balanced/cleaned correctly on their teardown paths.


---

## linting Audit

Lint audit complete. Report saved to `.factory/artifacts/lint-audit-task0.md`.

## Summary

**No BLOCKER findings.** The C codebase is in strong shape for conventions:

- **Naming**: consistent `snake_case` with uniform `cbx_`/`ip_` module prefixes (the only camelCase hits are external DBus/SDK identifiers like `InputPlumber`, `GamepadOrder` — correct).
- **Header guards**: present in all headers using a consistent `CBX_<MODULE>_H` convention.
- **Comments**: clean — concise, explain *why*, cite spec sections (`/* SPEC §4.5 */`), no restating-the-code noise.
- **Dead code**: automated scan (including multi-line signatures and a project-wide call-site check) found **no** unused static or exported functions — the only flagged names were false-positive macros that are all used.
- **No TODO/FIXME/XXX/HACK** markers remain.

### Findings (all WARN/INFO)

1. **WARN — Monolithic functions**: `cbx_select_grid_render` (300L), `run_overlay_service` (272L), `cbx_manager_init_with_dbus` (241L), `cbx_manager_handle_event` (220L), `load_svg_texture` (183L), `parse_profile_events` (172L), plus YAML/DBus parse routines. Interactive/render ones should be decomposed.
2. **WARN — No trailing newline at EOF in all 126 `.c`/`.h` files** (POSIX compliance, likely a strip artifact).
3. **WARN — `MAX_DOC_SIZE` duplicated across 5 files** (`config_profile.c:45`, `config_assignments.c:36`, `config_settings.c:34`, `config_profile_meta.c:35`, `icon_map.c:46`) with formatting drift — should be hoisted to a shared header.
4. **INFO** — `controllerbox.h` guard `CONTROLLER_BOX_H` breaks the `CBX_*` convention.
5. **INFO** — Python factory scripts have a few >100-char lines (tooling only).
6. **INFO** — Verbose-comment pass came back clean.

Each finding references exact file paths and line numbers for the developer. No action is required to consider Task 0 complete.


---

## security Audit

# Security Audit — Task 0: Final audit

I reviewed the full attack surface: DBus authorization, buffer safety, privilege boundaries, filesystem/temp-file handling, and config parsing. I also built the project fresh and ran the gate.

**Verification:** clean configure+build; `ctest` **91/91 passed** (2 skips are the documented hardware-gated tests `test_kernel_controller` and `test_backend_smoke`).

## Verdict: **No BLOCKER findings.**

This codebase is in an exceptionally strong security posture (clearly the product of the per-task security review loop). Confirmed strengths:
- **No** `sprintf`/`strcat`/`gets`/`system`/`alloca`; all writes bounds-checked; the isolated `strcpy`s in the mediator copy only pre-length-verified allow-list values.
- **No shell**: `service_install.c` uses `fork`+`execvp` with absolute paths, fixed Flatpak-ID allow-list, and atomic `mkstemp`+`rename`.
- **No `setuid`/`setgid`** anywhere.
- **DBus reference monitor** (`inputplumber-mediator.c`) is a signed method-call allow-list with signature/path/property/argument validation, mutation gating, and credential-verified sender policy — denies before system-bus forward.
- **Filesystem**: `O_NOFOLLOW`, `lstat` symlink-skip in directory scans, `realpath` + base-dir containment, dev/inode TOCTOU verification on the `/tmp` temp file, restrictive modes.
- **Config parsing** bounds-checks every `CBX_MAX_*` index before writes.

Report written to `docs/audit-security-task0-final.md`. Non-blocking findings:

- **WARN** — `src/inputplumber-mediator.c` (`listen_socket`): unconditional `unlink` before `bind` on the IPC socket. Safe today only because the path is broker/root-owned; pin that with an explicit invariant comment + post-bind ownership `fstat`.
- **WARN** — `src/dbus/ip_create_composite.c`: when `XDG_RUNTIME_DIR` is unset the temp file falls back to `/tmp`; the dev/inode check-then-use has a small residual window. Low practical risk (file is `0600`), but passing content via DBus variant or a private `0700` dir would eliminate it.
- **INFO** — `fsync()` per mediated decision (I/O DoS amplification, not correctness).
- **INFO** — `config_paths.c` `cbx_ensure_dir` follows parent symlinks (safe — user-owned `0700` dirs only).
- **INFO** — audit-log arguments are allow-list-validated, so no log injection (positive).

No changes required to deem task 0 complete.

