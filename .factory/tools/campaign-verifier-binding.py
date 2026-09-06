#!/usr/bin/env python3
"""Bind campaign verification to tracked config and executable identity/content."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import subprocess
import sys
import tomllib

ROOT = Path(__file__).resolve().parents[2]
CONFIG = ROOT / ".factory/config.toml"
ACCEPTANCE = ROOT / ".factory/verifier-acceptance.json"
HELPER_RELATIVE = ".factory/tools/campaign-verifier-binding.py"
HELPER_MODE = "100755"


def fail(message: str) -> None:
    raise SystemExit(f"campaign-verifier-binding: {message}")


def git(*args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=ROOT, text=True, capture_output=True,
        env={**os.environ, "GIT_NO_REPLACE_OBJECTS": "1"},
    )
    if result.returncode:
        fail(f"Git binding failed for {' '.join(args)}")
    return result.stdout.strip()


def secure_read(path: Path, maximum: int) -> tuple[bytes, os.stat_result]:
    if sys.platform != "linux" or not hasattr(os, "O_NOFOLLOW"):
        fail("required Linux no-follow primitive is unavailable")
    absolute = path.absolute()
    try:
        if absolute.resolve(strict=True) != absolute:
            fail(f"path contains a symlink: {path.relative_to(ROOT)}")
    except OSError as exc:
        fail(f"path is unavailable: {exc}")
    descriptor = os.open(absolute, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        before = os.fstat(descriptor)
        named = absolute.lstat()
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_uid != os.getuid()
            or before.st_nlink != 1
            or before.st_mode & 0o022
            or (before.st_dev, before.st_ino) != (named.st_dev, named.st_ino)
            or before.st_size > maximum
        ):
            fail(f"unsafe file: {path.relative_to(ROOT)}")
        chunks: list[bytes] = []
        remaining = maximum + 1
        while remaining:
            chunk = os.read(descriptor, min(65536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        data = b"".join(chunks)
        after = os.fstat(descriptor)
        named_after = absolute.lstat()
        if (
            len(data) > maximum
            or (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
            != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
            or (after.st_dev, after.st_ino) != (named_after.st_dev, named_after.st_ino)
        ):
            fail(f"file changed while binding: {path.relative_to(ROOT)}")
        return data, before
    finally:
        os.close(descriptor)


def blob_id(data: bytes) -> str:
    object_format = git("rev-parse", "--show-object-format")
    if object_format not in {"sha1", "sha256"}:
        fail("unsupported Git object format")
    payload = f"blob {len(data)}\0".encode("ascii") + data
    return hashlib.new(object_format, payload).hexdigest()


def tracked_blob(path: str, data: bytes, expected_mode: str) -> str:
    entry = git("ls-files", "-s", "--", path).split()
    if len(entry) != 4 or entry[0] != expected_mode or entry[3] != path:
        fail(f"tracked file has the wrong mode or identity: {path}")
    committed = git("rev-parse", f"HEAD:{path}")
    if blob_id(data) != committed or entry[1] != committed:
        fail(f"working file does not match its committed blob: {path}")
    return committed


def retained_helper_binding() -> dict[str, str] | None:
    """Bind this helper through its retained descriptor when so invoked.

    The campaign opens and retains an immutable descriptor to the helper before
    any untrusted phase. When the helper is executed through that descriptor
    (`/proc/self/fd/N`), `__file__` is that procfs path; the descriptor still
    refers to the exact committed inode opened at campaign startup, so a
    workspace pathname swap cannot substitute helper code. Direct pathname
    invocation (tests, debugging) performs no self-binding.
    """
    script = Path(__file__)
    match = re.fullmatch(r"/proc/self/fd/([0-9]+)", str(script))
    if not match:
        return None
    descriptor = int(match.group(1))
    try:
        info = os.fstat(descriptor)
    except OSError as exc:
        fail(f"cannot validate retained binding helper: {exc}")
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.getuid()
        or info.st_nlink != 1
        or info.st_mode & 0o022
        or not info.st_mode & 0o111
    ):
        fail("retained binding helper is not a safe executable file")
    # The retained descriptor is shared with the campaign shell's open-file
    # description, so its position persists across helper invocations. Rewind
    # it before binding the exact committed inode it pins.
    os.lseek(descriptor, 0, os.SEEK_SET)
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = os.read(descriptor, 65536)
        if not chunk:
            break
        total += len(chunk)
        if total > 16 * 1024 * 1024:
            fail("retained binding helper exceeds the read limit")
        chunks.append(chunk)
    raw = b"".join(chunks)
    after = os.fstat(descriptor)
    if (info.st_size, info.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        fail("retained binding helper changed while reading")
    helper_blob = tracked_blob(HELPER_RELATIVE, raw, HELPER_MODE)
    return {
        "path": HELPER_RELATIVE,
        "sha256": hashlib.sha256(raw).hexdigest(),
        "blob": helper_blob,
        "mode": format(stat.S_IMODE(info.st_mode), "04o"),
    }


def acceptance_contract() -> tuple[bytes, list[str], list[dict], str]:
    """Read the tracked verifier acceptance manifest (test discovery).

    The manifest is the authoritative gate list the verifier entrypoint runs.
    It is part of the binding so a gate-list change is detected; the campaign
    classifies a strict superset growth as legitimate strengthening and
    auto-rebinds with an audit record, while a shrink or entrypoint change
    requires the audited operator pathway.

    The binding carries both the plain gate names (for reporting) and the
    canonical ordered structured entries (name + exact args), because a
    name-only view would hide arg edits and reordering from strengthening
    classification.
    """
    raw, _ = secure_read(ACCEPTANCE, 1024 * 1024)
    try:
        manifest = json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        fail(f"invalid verifier acceptance manifest: {exc}")
    if (
        not isinstance(manifest, dict)
        or manifest.get("schema") != "ralph-verifier-acceptance/v1"
        or not isinstance(manifest.get("gates"), list)
        or not manifest["gates"]
    ):
        fail("verifier acceptance manifest must declare a non-empty gates list")
    names: list[str] = []
    entries: list[dict] = []
    for gate in manifest["gates"]:
        if not isinstance(gate, dict) or set(gate) != {"name", "args"}:
            fail("verifier acceptance gate must be an object with name and args")
        name = gate["name"]
        args = gate["args"]
        if (
            not isinstance(name, str)
            or not name
            or "/" in name
            or name.startswith(".")
            or not isinstance(args, list)
            or not all(isinstance(arg, str) and arg for arg in args)
        ):
            fail(f"verifier acceptance gate is invalid: {gate!r}")
        if name in names:
            fail(f"verifier acceptance manifest repeats gate name: {name!r}")
        names.append(name)
        entries.append({"name": name, "args": list(args)})
    acceptance_blob = tracked_blob(".factory/verifier-acceptance.json", raw, "100644")
    return raw, names, entries, acceptance_blob


def binding(mode: str = "campaign") -> tuple[dict[str, object], str, list[str], Path, bytes]:
    """Bind the configured verifier command (``campaign`` or ``maintenance``).

    ``--mode maintenance`` (Task 16) binds ``verification.maintenance_command``
    so the deprecated maintenance finalize path routes its maintenance
    verifier through the same retained descriptor authority as the campaign
    verifier instead of a bare shell exec.
    """
    config_bytes, _ = secure_read(CONFIG, 1024 * 1024)
    try:
        config = tomllib.loads(config_bytes.decode("utf-8"))
    except (UnicodeError, tomllib.TOMLDecodeError) as exc:
        fail(f"invalid factory config: {exc}")
    if mode == "maintenance":
        command = config.get("verification", {}).get("maintenance_command")
        if (
            not isinstance(command, list)
            or not command
            or not all(isinstance(arg, str) and arg and "\x00" not in arg for arg in command)
        ):
            fail("verification.maintenance_command must be a non-empty argv array")
        label = "maintenance"
    else:
        command = config.get("verification", {}).get("campaign_command")
        if (
            not isinstance(command, list)
            or not command
            or not all(isinstance(arg, str) and arg and "\x00" not in arg for arg in command)
        ):
            fail("verification.campaign_command must be a non-empty argv array")
        label = "campaign"
    executable_arg = command[0]
    if not executable_arg.startswith("./"):
        fail(f"{label} verifier executable must be canonical repository-relative ./path")
    relative = PurePosixPath(executable_arg[2:])
    if relative.is_absolute() or not relative.parts or any(part in {"", ".", ".."} for part in relative.parts):
        fail(f"{label} verifier executable path is not canonical")
    canonical_arg = "./" + relative.as_posix()
    if canonical_arg != executable_arg:
        fail(f"{label} verifier executable path is not canonical")
    executable = ROOT.joinpath(*relative.parts)
    executable_bytes, executable_info = secure_read(executable, 16 * 1024 * 1024)
    if not executable_info.st_mode & 0o111:
        fail(f"{label} verifier is not executable")
    relative_text = relative.as_posix()
    executable_blob = tracked_blob(relative_text, executable_bytes, "100755")
    config_blob = tracked_blob(".factory/config.toml", config_bytes, "100644")
    acceptance_raw, acceptance_gates, acceptance_entries, acceptance_blob = acceptance_contract()
    binding = {
        "schema": "campaign-verifier-binding/v1",
        "argv": command,
        "executable": relative_text,
        "executable_sha256": hashlib.sha256(executable_bytes).hexdigest(),
        "executable_blob": executable_blob,
        "executable_mode": format(stat.S_IMODE(executable_info.st_mode), "04o"),
        "config_sha256": hashlib.sha256(config_bytes).hexdigest(),
        "config_blob": config_blob,
        "acceptance_sha256": hashlib.sha256(acceptance_raw).hexdigest(),
        "acceptance_blob": acceptance_blob,
        "acceptance_gates": acceptance_gates,
        "acceptance_entries": acceptance_entries,
    }
    digest = hashlib.sha256(
        json.dumps(binding, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return binding, digest, command, executable, executable_bytes


def execute_verified(command: list[str], executable: Path, expected_bytes: bytes) -> None:
    if not hasattr(os, "O_NOFOLLOW"):
        fail("required Linux no-follow execution primitive is unavailable")
    descriptor = os.open(executable, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_uid != os.getuid()
            or before.st_nlink != 1
            or before.st_mode & 0o022
            or not before.st_mode & 0o111
        ):
            fail("campaign verifier became unsafe before execution")
        raw = b""
        while len(raw) <= len(expected_bytes):
            chunk = os.read(descriptor, min(65536, len(expected_bytes) + 1 - len(raw)))
            if not chunk:
                break
            raw += chunk
        after = os.fstat(descriptor)
        if (
            raw != expected_bytes
            or (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
            != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
        ):
            fail("campaign verifier changed after binding")
        named = executable.lstat()
        if (before.st_dev, before.st_ino) != (named.st_dev, named.st_ino):
            fail("campaign verifier pathname changed immediately before execution")
        # Execute the exact opened verifier inode through the retained
        # descriptor instead of reopening command[0]: the kernel resolves
        # /proc/self/fd/N to this open file description, so a pathname swap in
        # the final validation->exec race can never substitute another file.
        # The descriptor must survive the interpreter's reopen of the script
        # path, so it is explicitly made inheritable across the exec.
        os.set_inheritable(descriptor, True)
        # Task 16 F1: pin the canonical repository root into the child
        # environment so a repo-relative shell verifier can resolve its own
        # root without relying on ``$0`` (the kernel shebang dispatch
        # replaces the script argument with the descriptor path).
        environ = {**os.environ, "FACTORY_VERIFIER_ROOT": str(ROOT)}
        os.execve(f"/proc/self/fd/{descriptor}", command, environ)
    except OSError as exc:
        fail(f"cannot execute bound campaign verifier: {exc}")
    finally:
        os.close(descriptor)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", default="campaign",
                        choices=("campaign", "maintenance"),
                        help="bind the campaign or maintenance verifier command")
    parser.add_argument("--expected-digest")
    parser.add_argument("--exec", dest="execute", action="store_true")
    args = parser.parse_args()
    helper = retained_helper_binding()
    bound, digest, command, executable, executable_bytes = binding(args.mode)
    if args.expected_digest is not None and args.expected_digest != digest:
        fail("verification binding changed immediately before execution")
    if args.execute:
        if args.expected_digest is None:
            fail("--exec requires --expected-digest")
        execute_verified(command, executable, executable_bytes)
    print(json.dumps(
        {"binding": bound, "sha256": digest, "helper": helper},
        sort_keys=True, separators=(",", ":"),
    ))


if __name__ == "__main__":
    main()
