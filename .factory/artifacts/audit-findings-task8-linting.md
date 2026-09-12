# Linting Audit — Task 8 (Final documentation and specification audit)

**Auditor role:** linting (language conventions, readability, comments, dead code, consistency)
**Tree audited:** `develop` @ 491ce1d8 (`factory: task 8 implementation`)
**Scope notes:** Task 8 changed no production code (only the plan artifact). All findings below are
pre-existing code-quality issues in modules from earlier tasks. No new lint regressions were
introduced by Task 8. No BLOCKERs.

---

## WARN

### W1. `verify_sender` dereferences `conn` before its null guard
- **Files:** `src/dbus/ip_connection.c:99–103`
- Lines 99–101 write `conn->sender_verified`, `conn->expected_pid`, `conn->expected_uid`
  **before** the `if (!conn || ...)` guard on line 103. If `conn` is ever NULL the function
  crashes before reaching the guard, making the guard ineffective. cppcheck flags this as a
  real `nullPointerRedundantCheck` warning.
- Both call sites (`:187`, `:328`) currently pass a known-live `conn`, so it is not an active
  crash, but the guard is misplaced/misleading defensive code.
- **Recommendation:** hoist `if (!conn) return false;` (and the `!backend`/`!unique_name`
  checks) above the three assignments, or drop the redundant `!conn` term and document that
  `conn` is required non-NULL.

### W2. Tautological ternary in profile emission
- **Files:** `src/config/config_assignments.c:570–572`
- Both arms of the ternary are identical:
  ```c
  asgn->profile[0] == '\0' ? YAML_PLAIN_SCALAR_STYLE
                            : YAML_PLAIN_SCALAR_STYLE
  ```
  The branch conveys no information — empty vs. non-empty profile are emitted identically.
  Emitted YAML is still valid (functionally harmless), but the decision is dead/misleading and
  hides the likely intent (quoting a non-empty profile string). cppcheck:
  `duplicateExpressionTernary`.
- **Recommendation:** apply the desired per-case style (e.g. `YAML_QUOTED_SCALAR_STYLE` when
  non-empty) or collapse the ternary to a single `YAML_PLAIN_SCALAR_STYLE` with a clear comment.

### W3. Audit log file not flushed/closed on fatal-exit paths in the mediator
- **Files:** `src/inputplumber-mediator.c` (`main`, ~lines 176–186)
- `s.audit = fopen(...)` is opened early, but the early-return paths skip cleanup:
  `if (listener < 0) return 67;`, the poll/accept `return 68;` paths, and
  `if (client < 0) { unlink(argv[3]); return 0; }`. Only the `done:` label closes/flushes the
  audit FILE. cppcheck reports an `error: resourceLeak` for the `return` paths.
- The `return 0` no-client path is a normal clean exit and really should flush `s.audit`
  for an audit-logging process; the fatal-error paths are minor since the OS reaps fds.
- **Recommendation:** route all early exits through a common cleanup (fclose+fsync the audit
  log) or at minimum flush before the clean early `return 0`.

### W4. Readability: extremely long, statement-crammed lines
- **Files:** `src/inputplumber-mediator.c` (dozens of lines >150 cols, worst 335 cols, e.g.
  lines 60–177). Secondary: `src/app/overlay_service.c:460` (131), `src/manager/profiles_tab.c:1166`
  (120), `src/dbus/ip_connection.h:69`, `src/dbus/dbus_interface.h:84`.
- The mediator packs multiple independent statements and conditions onto a single line. This
  hurts greppability and diff readability, and is inconsistent with the rest of the tree
  (4-space indented, one statement per line, `cbx_`-prefixed API).
- **Recommendation:** split compound statements onto separate lines in the mediator (and wrap
  the >100-col stragglers elsewhere). No requirement to rewrite the security logic.

---

## INFO

### I1. Dead variable initializer for `last_rc`
- **Files:** `src/app/overlay_service.c:578`, `:621`
- `int last_rc = 0;` is immediately overwritten before its first read inside each loop, so the
  `= 0` initializer is never used (`unreadVariable`). Harmless; drop the initializer or use the
  value in the loop condition.

### I2. Header/source parameter-name drift
- **Files:** `src/config/config_settings.c:61,151`, `src/dbus/ip_connection.c:292,302`,
  `src/dbus/ip_input_signal.c:230`, `src/dbus/ip_intercept_poll.c:80–84`,
  `src/dbus/ip_properties.c:137`
- Header declarations use unnamed parameters while definitions name them (or name the same
  parameter differently, e.g. `settings`/`s`, `<unnamed>`/`userdata`). cppcheck
  (`funcArgNamesDifferent`). Conventional and harmless for self-documentation, but naming the
  parameters consistently in headers would document the API.

### I3. Git tree not clean
- `git status --porcelain` reports ` M .factory/artifacts/implementation-plan.md`.
- This is the factory plan artifact (sole-Git-writer responsibility), not product source, so it
  is not a lint violation; noted because Task 8's acceptance mentions a clean tree.

---

## Summary

No BLOCKERs. Task 8 introduced no lint regressions. Four WARNs (one real null-deref ordering
hazard, one dead/tautological branch, one resource-leak-on-exit in the mediator, and broad
readability issues in `inputplumber-mediator.c`) and three INFO notes are worth addressing in a
follow-up cycle; none block Task 8's documentation/verification acceptance.
