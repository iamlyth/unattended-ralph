# Security Audit — Task 0: Final audit

**Focus:** Vulnerabilities, attack surface, input validation, buffer safety,
privilege boundaries, DBus security, filesystem safety, dependency security.

**Scope reviewed:**
- DBus authorization / reference monitor — `src/inputplumber-mediator.c`
- DBus client & sender verification — `src/dbus/dbus_client.c`, `src/dbus/ip_connection.c`
- Systemd service installation — `src/manager/service_install.c`
- Path resolution — `src/config/config_paths.c`
- Profile save/load/delete (traversal) — `src/manager/profile_save.c`,
  `src/manager/profiles_tab.c`, `src/config/config_profile_list.c`
- Atomic-write paths — `src/config/config_profile_meta.c`, `config_settings.c`,
  `config_assignments.c`, `config_profile.c`, `src/manager/service_install.c`
- Temp-file handling — `src/dbus/ip_create_composite.c`
- Trigger parsing / overlay — `src/overlay/trigger.c`, `src/overlay/lifecycle.c`

**Verification:** Full configure + build succeeded in `build-audit/`.
`ctest` — 91/91 passed, 0 failed (tests `test_kernel_controller` and
`test_backend_smoke` skipped because the hardware they need is not present on
this runner, which is the documented expected behavior for this environment).

---

## Summary

**No BLOCKER findings.**

The codebase is in an exceptionally strong security posture. It has clearly
undergone repeated hardening. Notable strengths confirmed during review:

- **No unbounded string operations.** Zero uses of `sprintf`, `strcat`,
  `gets`, `system`, or `alloca`. All writes are bounds-checked
  (`snprintf`/`strncpy` with explicit NUL-termination); the handful of
  `strcpy` calls (only in `inputplumber-mediator.c`) copy into fixed buffers
  whose source lengths are verified *before* the copy.
- **No shell invocation.** `service_install.c` uses `fork`+`execvp` with
  absolute tool paths and fixed-argv construction (comment explicitly
  documents the elimination of `popen`/`system` to prevent metacharacter
  injection).
- **No privileged execution.** No `setuid`/`setgid`/`seteuid` anywhere.
- **DBus reference monitor is well-designed.** A method-call allow-list with
  signature validation, path allow-list, property-name allow-list, argument
  allow-list (`xb360`, `Guide`), mutation gating, and per-request
  credential-verified sender policy. Unauthorized calls are denied before a
  system-bus forward.
- **Filesystem hardening is thorough.** `O_NOFOLLOW` on profile/sidecar
  opens; `lstat` skips symlinks during directory scans; `realpath`
  canonicalization + base-directory containment checks on every safe path;
  `mkstemp`+`rename` atomic writes (no `mktemp`/`tmpnam`); restrictive
  modes (`0700` dirs, `0600`/`0400`/`0444` files); temp-file TOCTOU
  (dev/inode) verification in `ip_create_composite.c`.
- **Config parsing bounds are enforced.** Array indices (`type_count`,
  `gamepad_order_count`, `slot`) are checked against their `CBX_MAX_*` limits
  before writes; numeric fields are validated with `strtol` end-pointer checks.

The following are **WARN** and **INFO** items — none block completion, listed
for ongoing hardening.

---

## Findings

### WARN — IPC socket path handling assumes a trusted broker-owned directory
**Files:** `src/inputplumber-mediator.c` (`listen_socket`, line ~170)

`listen_socket()` performs an unconditional `unlink(path)` before `bind()` and
does not re-verify file ownership of the socket after binding. This is correct
and safe *today* because the socket path is supplied by the root-held broker
and its containing directory is expected to be root-owned/non-writable by the
mediation client. It becomes a risk if the layout ever places the socket in a
directory writable by a lower-privileged process (e.g. `/tmp`).

**Recommendation:** Keep the guarantee explicit — create the socket in a
broker-owned directory (e.g. `/run/controller-box`) with mode `0755`, and as
defense-in-depth, `fstat` the bound socket and reject it if `st_uid` is not
the expected broker UID. Add a comment pinning this invariant near
`listen_socket`.

### WARN — `/tmp` fallback temp-file race window (defense-in-depth)
**Files:** `src/dbus/ip_create_composite.c` (`ip_create_composite_device`)

When `XDG_RUNTIME_DIR` is unset, `resolve_temp_dir()` falls back to world
readable `/tmp`. The code correctly creates the file `0600` and, *after*
`close(fd)`, re-verifies the path's dev/inode before handing it to the DBus
`CreateCompositeDevice` call. This is good, but the check-then-use leaves a
small window between the `lstat` verification and InputPlumber's server-side
open. Practical risk is low: the file is `0600`, so only the owning user (the
app itself) or root can read it, meaning no privilege boundary is crossed
against a different principal.

**Recommendation:** When `XDG_RUNTIME_DIR` is unavailable, prefer writing the
payload to a private subdirectory under the user's own config dir (`0700`)
instead of `/tmp`, or pass the YAML content to InputPlumber via a DBus variant
rather than a filesystem path, eliminating the filesystem race entirely.

### INFO — `fsync()` performed on every mediated decision
**Files:** `src/inputplumber-mediator.c` (`log_decision`)

`log_decision()` calls `fsync(fileno(s->audit))` on every DBus decision,
including each denied call. A client able to hammer the mediation socket can
amplify I/O stalls. This is an availability/performance consideration, not a
correctness or confidentiality issue.

**Recommendation:** Consider coalescing durability — flush on a bounded batch
or on `SIGTERM`/teardown rather than per message — while keeping the audit
file `0400` and fail-closed on write errors.

### INFO — `cbx_ensure_dir` follows symlinks in parent components
**Files:** `src/config/config_paths.c` (`cbx_ensure_dir`, ~line 100)

Recursive directory creation uses `mkdir` + `stat` (which follows symlinks in
intermediate path components). Because all resolved paths are the user's *own*
XDG directories created with `0700`, the only actor able to plant a symlink is
the user themselves, so no privilege boundary is crossed. This is safe in the
current usage.

**Recommendation:** If these helpers are ever reused to create directories
outside the user's own XDG tree (e.g. system or shared writable locations),
convert to `mkdirat`/`openat`-style component traversal with `O_NOFOLLOW`.

### INFO — Audit-log arguments are allow-list validated
**Files:** `src/inputplumber-mediator.c` (`log_decision`, `authorize`)

`log_decision` writes `s->arguments` into a JSON record via `fprintf`. The
contents of `s->arguments` are always one of: allow-list-derived constants
(`xb360`, `[Guide],Guide`, `[xb360-or-empty]`), a validated owner string
(`:`, < `sizeof owner`), or the validated interface/property allow-list. No
untrusted raw payload ends up in the log, so there is no log-injection vector.
This is a positive finding, noted for completeness.

---

## Conclusion

No BLOCKER issues. The two WARN findings are defense-in-depth improvements
that should be scheduled but do not prevent this task (final audit) from being
considered complete. The INFO items are observations for the hardening
backlog. Overall this is a clean security posture for the reviewed surface.
