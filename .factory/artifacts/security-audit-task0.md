# Task 0 — Final Security Audit

**Auditor role:** Security / attack surface / input validation
**Scope:** Current `develop` tree at `/workspace/project`
**Verification state:** `./scripts/verify.sh` equivalent build — 100% tests passed (91 tests, 0 failed; 2 hardware-bound skips: `test_kernel_controller`, `test_backend_smoke`).

---

## Summary

The main application (`controller-box`) is notably well hardened. Highlights verified
in source:

- All string formatting uses `snprintf`; no unbounded `sprintf`/`strcat`/`gets` anywhere
  in `src/`. (`grep` found zero non-`snprintf` uses in `.c` sources.)
- Every `strcpy`/`memcpy` in `src/inputplumber-mediator.c` is length-gated by an
  adjacent `strlen() < BOUND` check (see per-call analysis below).
- Atomic persistence everywhere: `mkstemp` + `fchmod(0600)` + `fsync` + `rename`
  (`config_assignments.c`, `config_profile.c`, `config_profile_meta.c`,
  `config_settings.c`, `manager/service_install.c`).
- All reads of external files open with `O_NOFOLLOW` (anti-symlink-attack):
  `config_profile_list.c`, `config_profile.c`, `config_profile_meta.c`,
  `config_settings.c`, `config_assignments.c`, `manager/profile_diagram.c`.
- No shell invocation: `service_install.c` uses `fork`/`execvp` with **absolute**
  tool paths (`/usr/bin/systemctl`, `/usr/bin/flatpak-spawn`); `FLATPAK_ID` is bound
  to the exact expected app ID to defeat injection into the generated unit file.
- External identifier/name input is strictly validated before use as filenames
  (`cbx_validate_filename` allows only `[a-z0-9_-]`), as map keys
  (`cbx_validate_id`) and as identities (`identity.c` MAC/serial/bus-type checks).
- DBus signal authenticity enforced: `dbus_client.c` verifies the sender's unique
  bus name and a captured PID fingerprint (`expected_sender`/`expected_pid`) before
  trusting any signal.
- YAML parsing is constrained: max doc 1 MB, max depth 50, custom tags rejected.
- The application does **not** expose its own DBus service (`sd_bus_request_name`/
  `sd_bus_add_object` absent) — arbitrary clients have no in-app method surface.

No BLOCKER (must-fix before completion) findings were identified in the product
application.

---

## Findings

### 1. Root helper `inputplumber-mediator` does not authenticate its connecting peer

- **Files:** `src/inputplumber-mediator.c` (`listen_socket`, `main`)
- **Severity:** **WARN**

The mediator is built and installed as a root-side helper
(`add_executable(inputplumber-mediator ...)`, `install(TARGETS inputplumber-mediator
RUNTIME DESTINATION libexecdir)` in `CMakeLists.txt`). It:
- binds a UNIX socket and `chmod`s it **0660** (group writable),
- advertises `sd_bus_set_anonymous` (anonymous client), and
- accepts **exactly one** client connection with **no peer credential check**
  (no `getpeereid`/`SO_PEERCRED`, no comparison of the connecting uid/comm against
  an allowlist).

Consequently any local process that can reach the socket can drive the mediator.
In `--mutations yes` mode the allowlisted set includes InputPlumber mutations
`CreateTargetDevice`, `StopTargetDevice`, `SetTargetDevices`,
`SetInterceptActivation`, and `Properties.Set InterceptMode`, forwarded over a
root-owned system-bus connection.

Mitigating factors that keep this below BLOCKER:
- Every method is gated by the `authorize()` allowlist, so blast radius is limited
  to a fixed set of InputPlumber operations — not arbitrary DBus.
- Nothing currently launches the mediator (see Finding 4), so the surface is not
  reachable in normal operation.

**Recommendation:** Bind peer credentials (`getpeereid`/`SO_PEERCRED`) at accept
time and refuse the connection unless the client uid/gid/comm matches the brokered
identity, or drop socket permissions to 0600 and rely on the requesting process
being the root-held broker. Add the check before processing any DBus message.

### 2. `unlink(path)` before `bind()` opens a small local race / pre-created-socket denial

- **Files:** `src/inputplumber-mediator.c` (`listen_socket`)
- **Severity:** **INFO** (WARN if socket directory is attacker-writable)

`listen_socket()` does `unlink(path); bind(...); chmod(path,0660);`. The socket
path comes from `argv[3]` (root-held), so in the intended deployment this is fine.
But if the parent directory is ever writable by a lower-privileged user, there is a
classic TOCTOU: the `unlink` then `bind` sequence is not race-free, and a
pre-created symlink/file at `path` is removed by `unlink`. Combined with Finding 1,
group-writable socket mode widens the local attack surface.

**Recommendation:** `open(path, O_CREAT|O_EXCL|O_NOFOLLOW)` a placeholder to claim
the path, then `unlink` only if it is still the file we created; and consider
0600 instead of 0660.

### 3. Mediator answers `Hello` with a hardcoded unique name `:1.1`

- **Files:** `src/inputplumber-mediator.c` (`filter`)
- **Severity:** **INFO**

The mediator replies to `org.freedesktop.DBus` `Hello` with the fixed unique name
`:1.1` and answers `GetNameOwner` for `org.shadowblip.InputPlumber`. This is
intentional for a narrow peer, but it means the client is handed a name it can rely
on as if it were the real daemon. Acceptable given the single-client design, but the
name should be derived from the broker topology rather than hardcoded to guard
against accidental impersonation semantics if the design evolves.

### 4. Mediator is shipped but not wired to any launcher (incomplete integration)

- **Files:** `src/inputplumber-mediator.c`, `CMakeLists.txt`,
  `data/inputplumber-runner-dbus.conf`
- **Severity:** **INFO**

The mediator binary is built/installed and `data/inputplumber-runner-dbus.conf`
enacts a deny policy so candidate users cannot reach InputPlumber directly, yet no
service unit, systemd unit, or script in the tree invokes `inputplumber-mediator`
with socket/snapshot/audit paths. The root-held "pre-forward" path it implements is
therefore currently dead code, and the DBus deny policy would break InputPlumber
access for those users if the mediator were not running. This is a deployment
consistency concern, not a product-input vulnerability.

**Recommendation:** Either wire the mediator into the packaging (a `.service` unit
and a broker launcher that supplies the topology snapshot + socket + audit paths)
together with the deny policy, or drop the install/deny-policy pair until the
component is complete, so the tree does not ship a partially integrated root
helper.

---

## Verified-safe / no finding (positive confirmations for the record)

- **Fixed-size write into `arguments[1024]`:** every `strcpy(s->arguments, ...)` in
  `authorize()` writes a compile-time constant or a value already bounded by
  `learned()` (max 511 chars for a learned target); `Get`/`GetAll` use `snprintf`.
- **`strcpy(s->targets[...], target)`** guarded by `strlen(target) >= MAX_PATH`
  (512) before copy — fits the 512-byte slot.
- **`strcpy(s->owner, owner)`** guarded by `strlen(owner) >= sizeof s->owner` (64).
- **`strcpy(sa.sun_path, path)`** guarded by `strlen(path) >= sizeof sa.sun_path`.
- **DBus string-array assembly** (`dbus_client.c` `sd_read_string_array_csv`) uses
  `realloc` with incremental size, no fixed-buffer overflow.
- **Composite path / device path copies** (`ip_device_model.c`, `ip_composite.c`,
  `overlay_service.c`) all bounded via `snprintf`/explicit length checks.
- **Rate limiting + NaN/range validation** on input signal values
  (`ip_input_signal.c`).

## Conclusion

No findings rise to **BLOCKER** for marking Task 0 complete. The shipped product
application (`controller-box`) is well hardened across input validation, buffer
safety, file handling, and DBus trust boundaries. The only material attack-surface
concern lives in the `inputplumber-mediator` root helper (peer authentication and
socket handling), which is currently dead/incompletely integrated; it should be
addressed before that component is wired into a real deployment.
