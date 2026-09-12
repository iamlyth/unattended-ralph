## Spec Study Report

**Project:** Controller-Box — a controller-only SDL2 GUI wrapping InputPlumber.
**Spec source:** `docs/SPEC.md` (Ready-for-implementation, all 16 decisions + tickets #2–#10 resolved). Companion specs: `docs/DBus-API.md`, `docs/PROFILES.md`, `docs/PACKAGING.md`, `docs/REVIEW.md` (historical, not acceptance authority).
**Note on state:** Implementation is far progressed — git tree clean on `develop`, 92 test files, plan tasks 1–6 all completed, suite at "91/91 tests, 0 failures." This report covers the full normative spec (incl. what remains to be verified) for continued planning.

### Core requirements

- **One binary, two modes** (`controller-box`): `--overlay-service` (always-resident systemd user service) and `--manager` (launched on demand). Shared codebase, config dir, and DBus connection.
- **GUI is a control surface only.** Never touches input routing — every state change goes through InputPlumber's **system DBus** (`org.shadowblip.InputPlumber`) via **sd-bus** (zero new deps). InputPlumber is a separate package/service, never bundled.
- **Overlay** (fighting-game character-select): rows=physical controllers, columns=player slots (virtual controllers) + Unassigned. Left/Right move slot, Up/Down cycle per-controller profile (follows controller, not seat), R3 = host mode (exclusive), B closes. Single hotkey (Select+A, configurable). Conflict (2 controllers same slot) highlighted red, resolved deterministically to lowest free P slot on exit. Columns are dynamic (scale to instantiated virtual controllers). Player mode & Host mode.
- **Manager** tabs: **Controllers** (add/remove virtual controllers, per-slot type, mixed types, topology reconcile — manager-only for virtual connect/disconnect), **Profiles** (browse/create/edit/delete; built-in read-only default fallback), **Settings** (launch-at-boot, theme, opacity, startup controller count/types, trigger combo, icon overrides).
- **Profile editor** (two modes sharing one diagram): binding-list mode + sequential-binding mode (B skip, Start cancel, progress bar). **NES minimum validation:** A, B, D-pad Up/Down/Left/Right required; others optional. Profiles are standard InputPlumber `device_profile_v1` YAML, written directly to InputPlumber's profile dir — no duplicate format.
- **Multi-layered controller auto-assignment** keyed to strongest stable identity: BT MAC > USB serial > USB port path > connection order. Saves preferred slot/profile in `assignments.yaml`. ID-type prefix (BT:/USB:/USB:phys:) enables downgrade detection.
- **Icons = virtual device type** (what the game sees), via `DeviceType` string → `controller-icons.yaml` map. Sourced from Controllercons SVG (30 icons, OFL), custom fills for gaps (arcade stick, hitbox, Steam Deck, generic). Rasterized by vendored **nanosvg** at startup; textures cached.
- **Packaging:** Flatpak primary (experimental until published; perms: system-talk InputPlumber + read/write `~/.local/share/inputplumber` + read `/usr/share/inputplumber`; manager installs user systemd unit on first run), tarball fallback (x86_64+aarch64, `make install`).
- **Performance:** overlay ≤75 ms p99 / ≤100 ms max from button press; <10 ms p99 from ALL-detection to present; close → input back in <1 ms. Pre-built in-memory surface, ~50 ms InterceptMode poll (gap#1 workaround).
- **Heavy acceptance gates** (definition-of-done): full conformance matrix, production-path behavior, complete interaction traversal (every control: controller + pointer paths), visual + degraded-state acceptance via framebuffer pixel reads and golden images, regression/quality gates, known-defect accounting, independent review, reproducible docs, clean repo on `develop`.

### Constraints

- **Language/tooling:** C + CMake. SDL2, SDL2_ttf, SDL2_image, sd-bus (systemd), libyaml; vendored nanosvg (~500-line C). Build via Nix (`nix-shell`).
- **Platform:** Linux x86_64 + aarch64; **minimum hardware Raspberry Pi 4** (OpenGL ES 3.0). Any compositor (X11, Wayland, Gamescope). Bare DRM/framebuffer, Pi Zero/Pi 3 out of scope.
- **DBus type fidelity:** must read/write each member by its native signature (`u` for InterceptMode, `b` booleans, `as` string arrays). Converting everything through string getters is prohibited. Release tests run against a real/private native-signature service.
- **System/user service split:** user service must NOT declare `After=`/`Requires=` on the system `inputplumber.service` (separate managers). Runtime bus-name/readiness checks instead.
- **Identity strength** must be tracked (prefix scheme) to handle graceful downgrade.
- **Runners:** three declared in `.factory/environment.toml` (dev-runner-vm, iprunner, gpurunner); hardware-dependent tests skip with exit 77; unreachable runner = `blocked`, never faked.
- Work only on `develop`; orchestrator is sole Git writer; no worktrees; don't mutate the committed spec.

### Open questions / underspecified (deferred to implementation by spec §13)

- **Host Mode interior UX** — row navigation and edit affordances are unspecified (mode + R3 trigger specified only).
- **Theme/skinning details** — `theme` key exists; format TBD.
- **Console-only manager launch** path TBD (desktop entry + overlay "open manager" covers most).
- **Advanced profile mappings** (chord, delayed_chord, gamepad→mouse) valid/loadable but not exposed in v1 editor.
- Per-game profile auto-switching deferred (needs upstream).
- Post-v1 distro packages (.deb/.rpm/Nix/AUR/Batocera).

### Risk areas (hard to implement or verify)

1. **Visual/rendering acceptance** — the strictest gates. Tests must prove real framebuffer output (not just widget metadata/counts/no-crash) across manager tabs, overlay states, degraded modes; golden images with documented tolerance (never auto-regenerated); failure artifacts saved. Already addressed by tests (`test_golden`, `test_manager_visual`, `test_overlay_visual`) but highest regression surface.
2. **Interaction acceptance** — every enabled control must pass *both* controller and pointer activation paths via production SDL event dispatch, with semantic-outcome evidence (exact mock DBus request, filesystem mutation, persisted value), and disabled controls must reject both paths. Event-handler return value is not evidence.
3. **DBus native fidelity & the five gaps** — InterceptMode poll (50 ms, timeout state machine), GamepadOrder persistence/reapply, CreateCompositeDevice temp-file workaround, filesystem-directory profile enumeration, source-device add/remove not needed. Mock string-conversion is prohibited; tests must use native signatures.
4. **Hardware/kernel-backed verification** — controller acceptance needs a physical or kernel-backed synthetic gamepad (`/dev/uinput`, `kernel-uinput` capability) with InputPlumber running; string-only mock and keyboard events are explicitly *not* acceptable. Real InputPlumber service may not be present on all runners.
5. **Packaging** — Flatpak must pass installed functional gate (host profile paths visible, system DBus access verified, service install on first run) before it can be advertised; tarball needs clean X11 installed smoke test. Currently experimental per spec.
6. **Perf latency targets** on Pi 4 hardware — pre-built surface + 50 ms poll must hold ≤75/100 ms; needs real compositor measurement (gpu-compositor runner).
7. **Broad scope creep risk** in acceptance automation — the §11.1 seven-layer suite and §5.7 complete traversal are large; maintaining them across refactors is the main ongoing effort.
