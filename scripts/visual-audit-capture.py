#!/usr/bin/env python3
"""Serial/isolated visual capture runner (stdlib only).

Holds the dedicated visual-audit lease (flock, O_CLOEXEC) in THIS process
across every capture-driver invocation, so:
  - captures are strictly serial; a concurrent capture is refused (no
    shared-session race)
  - the untrusted capture driver and the vision model never inherit the
    lease fd (CLOEXEC), and the campaign factory lock is never touched
Then binds every image to the exact commit/tree via the provenance manifest.

Capture-hang hardening:
  - every untrusted capture driver runs in its OWN process session/group
    (start_new_session) so TERM/KILL targets the whole group, never a global
    pkill and with no display assumptions
  - the per-state capture timeout is configured
    (capture_timeout_seconds, default 120, ceiling 120); on timeout the whole
    group receives SIGTERM, is given the bounded cleanup grace
    (capture_cleanup_grace_seconds, default 5, ceiling 5), then SIGKILL, and
    is waited/reaped -- never relying only on the driver's own trap
  - on timeout or failure the partial/invalid image is removed and the lease
    is always released
  - state ids are validated before any path is constructed from them

Execution-path overrides: `VISUAL_AUDIT_CONFIG`, `VISUAL_AUDIT_SDK_DRIVER`, and
`VISUAL_AUDIT_VISION_MODEL` are test-only seams; they are honored only with the
explicit `RALPH_VISUAL_AUDIT_TESTING=1` marker. Production uses the tracked
`.factory/visual-audit.toml` config and never accepts caller text for the
environment binding (the provenance manifest computes the fixed SHA-256 of the
committed `.factory/environment.toml` bytes itself).
"""

from __future__ import annotations

import fcntl
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import time

STATE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def die(message: str) -> "NoReturn":
    raise SystemExit(f"visual-audit-capture: {message}")


TESTING_MARKER = "RALPH_VISUAL_AUDIT_TESTING"


def testing_mode() -> bool:
    return os.environ.get(TESTING_MARKER) == "1"


def gate_overrides() -> None:
    """Test-only execution overrides require the explicit marker."""
    if testing_mode():
        return
    for var in ("VISUAL_AUDIT_CONFIG", "VISUAL_AUDIT_SDK_DRIVER", "VISUAL_AUDIT_VISION_MODEL",
                "FACTORY_ENVIRONMENT_BLOB"):
        if os.environ.get(var):
            die(f"{var} is a test-only execution override; set {TESTING_MARKER}=1 to use it")


def is_tracked(root: Path, path: Path) -> bool:
    try:
        rel = path.relative_to(root)
    except ValueError:
        return False
    result = subprocess.run(["git", "-C", str(root), "ls-files", "--error-unmatch", "--", str(rel)],
                            capture_output=True, check=False)
    return result.returncode == 0


def remove_quietly(path: Path) -> None:
    """Remove a partial/invalid image without masking the real error."""
    try:
        if path.is_file() and not path.is_symlink():
            path.unlink()
    except OSError:
        pass


def run_driver(argv: list[str], cwd: Path, timeout: int, grace: int) -> tuple[int, bytes, bool]:
    """Run the untrusted driver in its own session/group under a hard timeout.

    Returns (returncode, captured stdout, timed_out). On timeout the whole
    process group is SIGTERM'd, given at most `grace` seconds to exit, then
    SIGKILL'd, and finally waited/reaped -- the driver's own traps are never
    trusted.
    """
    try:
        process = subprocess.Popen(
            argv, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    except OSError as exc:
        die(f"cannot start capture driver: {type(exc).__name__}: {exc}")
    assert process.stdout is not None
    timed_out = False
    try:
        out, _ = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass
        deadline = time.monotonic() + grace
        while time.monotonic() < deadline and process.poll() is None:
            time.sleep(0.05)
        if process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
        try:
            out, _ = process.communicate(timeout=grace + 5)
        except subprocess.TimeoutExpired:
            # Belt and braces: the group has been killed; reap regardless.
            process.kill()
            try:
                out, _ = process.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                out = b""
    return process.returncode, (out or b""), timed_out


def main() -> int:
    gate_overrides()
    root = Path.cwd()
    config_path = Path(os.environ.get("VISUAL_AUDIT_CONFIG", root / ".factory/visual-audit.toml"))
    if not config_path.is_absolute():
        config_path = root / config_path
    if not config_path.is_file() or config_path.is_symlink():
        die("config file is missing or unsafe")
    if not testing_mode() and (not is_tracked(root, config_path)
                               or config_path.stat().st_uid != os.getuid()
                               or config_path.stat().st_mode & 0o022):
        die("production config must be a tracked regular file owned by the current user")
    import tomllib
    with open(config_path, "rb") as stream:
        section = tomllib.load(stream).get("visual-audit", {})
    if section.get("enabled") is not True:
        die("visual audit is disabled")
    driver = section.get("capture_driver", "")
    capture_dir = section.get("capture_dir", "")
    lease_file = section.get("lease_file", "")
    inventory = section.get("inventory", "")
    if not all((driver, capture_dir, lease_file, inventory)):
        die("capture_driver, capture_dir, lease_file, inventory must be set")
    timeout = section.get("capture_timeout_seconds", 120)
    grace = section.get("capture_cleanup_grace_seconds", 5)
    if isinstance(timeout, bool) or not isinstance(timeout, int) or not 0 < timeout <= 120:
        die("capture_timeout_seconds must be an integer in (0, 120]")
    if isinstance(grace, bool) or not isinstance(grace, int) or not 0 <= grace <= 5:
        die("capture_cleanup_grace_seconds must be an integer in [0, 5]")
    for field, value in (("capture_driver", driver), ("capture_dir", capture_dir),
                         ("lease_file", lease_file), ("inventory", inventory)):
        path = Path(value)
        if not path.is_absolute():
            path = root / path
        if field == "inventory" and (not path.is_file() or path.is_symlink()):
            die("inventory is missing or unsafe")
        if field == "capture_driver" and (not path.is_file() or path.is_symlink() or not os.access(path, os.X_OK)):
            die("capture driver is missing or not executable")

    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip()
    # The manifest tree must equal HEAD^{tree}: a dirty index (staged changes)
    # must never masquerade as the committed tree the review will verify.
    tree = subprocess.check_output(["git", "rev-parse", "HEAD^{tree}"], cwd=root, text=True).strip()

    lease_path = Path(lease_file)
    if not lease_path.is_absolute():
        lease_path = root / lease_path
    lease_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    lease_fd = os.open(lease_path, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o600)
    try:
        try:
            fcntl.flock(lease_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            die("another capture holds the visual-audit lease (race refused)")
        os.ftruncate(lease_fd, 0)
        os.write(lease_fd, f"{os.getpid()}|capture".encode())
        os.fsync(lease_fd)

        out_dir = Path(capture_dir)
        if not out_dir.is_absolute():
            out_dir = root / out_dir
        out_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        if out_dir.stat().st_mode & 0o077:
            die("capture dir must not be group/world accessible")

        inventory_path = Path(inventory)
        if not inventory_path.is_absolute():
            inventory_path = root / inventory_path
        inv = json.loads(inventory_path.read_text(encoding="utf-8"))
        if inv.get("schema") != "ralph-visual-audit-inventory/v1":
            die("inventory schema invalid")
        states = inv.get("states", [])
        if not isinstance(states, list) or not states:
            die("inventory has no states")

        for state in states:
            if not isinstance(state, dict) or "id" not in state or "risk" not in state:
                die("inventory state schema invalid")
            if state.get("risk") not in {"critical", "high", "medium", "low"}:
                die(f"invalid risk for {state.get('id')}")
            state_id = state["id"]
            if not isinstance(state_id, str) or not STATE_NAME.fullmatch(state_id):
                die(f"invalid state id: {state_id!r}")
            # State ids are validated above BEFORE any path is built.
            output = out_dir / f"{state_id}.png"
            if output.is_symlink():
                die(f"capture output path must not be a symlink: {state_id}")
            if output.is_file() and output.stat().st_size > 0:
                print(f"visual-audit-capture: state {state_id} already captured; skipping (deterministic)")
                continue
            # Remove any stale empty file so a failed driver can never leave a partial image.
            remove_quietly(output)
            driver_path = Path(driver)
            if not driver_path.is_absolute():
                driver_path = root / driver_path
            print(f"visual-audit-capture: capturing state {state_id} (risk {state['risk']})", flush=True)
            rc, out_bytes, timed_out = run_driver(
                [str(driver_path), state_id, str(output), commit], root, timeout, grace)
            if out_bytes:
                sys.stdout.write(out_bytes.decode("utf-8", errors="replace"))
            if timed_out:
                remove_quietly(output)
                die(f"driver timed out for state {state_id} after {timeout}s; whole session group TERM'd, "
                    f"grace {grace}s, then KILL'd and reaped")
            if rc != 0:
                remove_quietly(output)
                die(f"driver failed for state {state_id} (rc={rc})")
            if not output.is_file() or output.is_symlink() or output.stat().st_size == 0:
                remove_quietly(output)
                die(f"driver produced no image for state {state_id}")
            with open(output, "rb") as fh:
                magic = fh.read(8)
            if magic != PNG_MAGIC:
                remove_quietly(output)
                die(f"driver produced a malformed (non-PNG) image for state {state_id}")
    finally:
        try:
            fcntl.flock(lease_fd, fcntl.LOCK_UN)
        except OSError:
            pass
        os.close(lease_fd)

    # Provenance manifest, bound to the exact commit/tree. The environment
    # binding is computed inside visual-audit-provenance.py from the committed
    # .factory/environment.toml bytes; caller text is never forwarded.
    scripts = root / "scripts"
    subprocess.run(
        [sys.executable, str(scripts / "visual-audit-provenance.py"), "manifest",
         "--out", str(out_dir), "--commit", commit, "--tree", tree],
        cwd=root, check=True, stdout=subprocess.DEVNULL,
    )
    subprocess.run(
        [sys.executable, str(scripts / "visual-audit-provenance.py"), "verify",
         "--out", str(out_dir)],
        cwd=root, check=True,
    )
    count = len(list(out_dir.glob("*.png")))
    print(f"visual-audit-capture: captured {count} state images at {commit[:12]}")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
