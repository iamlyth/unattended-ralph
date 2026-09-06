#!/usr/bin/env python3
"""Exact-commit Pi2 adapter executed inside the factory confinement boundary.

The control plane replaces the two immutable runtime markers while staging this
committed source. The trusted parent carries the operator ``auth.json`` bytes
in one anonymous memfd whose descriptor number arrives in this adapter's
transient argv (``--auth-fd N``). After Landlock is active, this adapter
materialises a private mode-0600 credential file in the launch-owned home so
the model CLI can initialize without dereferencing the deliberately denied
``/proc`` tree. Around every common ``tool_call`` boundary, the exact-commit
extension verifies and detaches that file before tool execution, closes every
inherited descriptor alias, then restores the private file from extension-owned
memory before Pi's next authenticated turn. Landlock also denies ``/proc``.
The descriptor number is consumed here and is absent from model argv.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import resource
import stat
import sys

NODE_EXECUTABLE = "@@FACTORY_PI2_NODE@@"
PI_CLI = "@@FACTORY_PI2_CLI@@"


def _parse_auth_fd(argv: list) -> tuple:
    """Extract ``--auth-fd N`` from the adapter argv (fail closed)."""
    auth_fd = -1
    remaining: list = []
    index = 0
    while index < len(argv):
        token = argv[index]
        if token == "--auth-fd":
            if auth_fd >= 0:
                raise SystemExit("factory-pi2-backend: --auth-fd was supplied twice")
            if index + 1 >= len(argv):
                raise SystemExit("factory-pi2-backend: --auth-fd requires a value")
            try:
                auth_fd = int(argv[index + 1])
            except ValueError:
                raise SystemExit("factory-pi2-backend: --auth-fd is not an integer")
            if auth_fd < 0:
                raise SystemExit("factory-pi2-backend: --auth-fd is negative")
            index += 2
            continue
        remaining.append(token)
        index += 1
    return auth_fd, remaining


def _runtime_binding(argv: list) -> tuple[str, str]:
    """Extract the one trusted provider/model pair from generated Pi argv."""
    values = {}
    index = 0
    while index < len(argv):
        token = argv[index]
        if token in ("--provider", "--model"):
            if token in values or index + 1 >= len(argv):
                raise SystemExit(f"factory-pi2-backend: malformed {token} binding")
            value = argv[index + 1]
            if not value or len(value) > 256 or any(ord(char) < 0x20 for char in value):
                raise SystemExit(f"factory-pi2-backend: unsafe {token} binding")
            values[token] = value
            index += 2
            continue
        index += 1
    if values.get("--provider") != "openai-codex" or "--model" not in values:
        raise SystemExit("factory-pi2-backend: openai-codex provider/model binding missing")
    return values["--provider"], values["--model"].removeprefix("openai-codex/")


def _write_runtime_settings(agent_dir: Path, provider: str, model: str) -> None:
    """Write the minimal non-secret provider registration Pi requires.

    Pi 0.84 does not register the built-in openai-codex provider when its
    private ``settings.json`` is an empty object, even when both CLI flags are
    explicit. Packages, sessions, themes, and every operator preference stay
    excluded; only the exact trusted argv binding is persisted in this
    launch-owned home.
    """
    settings = agent_dir / "settings.json"
    payload = json.dumps(
        {"defaultProvider": provider, "defaultModel": model},
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8") + b"\n"
    fd = -1
    try:
        fd = os.open(
            settings, os.O_WRONLY | os.O_TRUNC | os.O_NOFOLLOW | os.O_CLOEXEC
        )
        info = os.fstat(fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or stat.S_IMODE(info.st_mode) != 0o600
            or info.st_uid != os.getuid()
            or info.st_nlink != 1
        ):
            raise OSError("private settings.json failed identity verification")
        view = memoryview(payload)
        while view:
            written = os.write(fd, view)
            if written <= 0:
                raise OSError("short settings.json write")
            view = view[written:]
        os.fsync(fd)
    except OSError as exc:
        raise SystemExit(f"factory-pi2-backend: cannot bind runtime settings: {exc}")
    finally:
        if fd >= 0:
            os.close(fd)


def _materialize_auth_file(agent_dir: Path, auth_fd: int) -> os.stat_result:
    """Create the launch-private credential file and return its identity.

    A ``/proc/self/fd/N`` symlink is unusable because production Landlock
    correctly denies that credential channel. The regular file is reachable
    only in the private home and is removed by the common tool boundary before
    any model-selected tool implementation runs.
    """
    if auth_fd < 0:
        raise SystemExit("factory-pi2-backend: no credential descriptor was supplied")
    try:
        info = os.fstat(auth_fd)
    except OSError as exc:
        raise SystemExit(
            f"factory-pi2-backend: cannot inspect the credential descriptor: {exc}"
        )
    if not stat.S_ISREG(info.st_mode):
        raise SystemExit("factory-pi2-backend: credential descriptor is not regular")
    target = agent_dir / "auth.json"
    if target.is_symlink() or target.exists():
        raise SystemExit("factory-pi2-backend: auth.json already exists in the agent dir")
    target_fd = -1
    try:
        target_fd = os.open(
            target,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
            0o600,
        )
        os.lseek(auth_fd, 0, os.SEEK_SET)
        while True:
            chunk = os.read(auth_fd, 65536)
            if not chunk:
                break
            view = memoryview(chunk)
            while view:
                written = os.write(target_fd, view)
                if written <= 0:
                    raise OSError("short auth.json write")
                view = view[written:]
        os.fsync(target_fd)
        os.lseek(auth_fd, 0, os.SEEK_SET)
        file_info = os.fstat(target_fd)
        if (
            not stat.S_ISREG(file_info.st_mode)
            or stat.S_IMODE(file_info.st_mode) != 0o600
            or file_info.st_uid != os.getuid()
            or file_info.st_nlink != 1
            or file_info.st_size != info.st_size
        ):
            raise OSError("materialised auth.json failed identity verification")
        return file_info
    except OSError as exc:
        try:
            os.unlink(target)
        except OSError:
            pass
        raise SystemExit(f"factory-pi2-backend: cannot materialise auth.json: {exc}")
    finally:
        if target_fd >= 0:
            os.close(target_fd)


def main() -> None:
    home = Path(os.environ["HOME"])
    agent_dir = home / ".pi" / "agent2"
    if not agent_dir.is_dir():
        raise SystemExit("factory-pi2-backend: private agent directory is missing")
    node = Path(NODE_EXECUTABLE)
    cli = Path(PI_CLI)
    if not node.is_absolute() or not cli.is_absolute():
        raise SystemExit("factory-pi2-backend: runtime binding is not absolute")
    auth_fd, remaining = _parse_auth_fd(sys.argv[1:])
    provider, model = _runtime_binding(remaining)
    _write_runtime_settings(agent_dir, provider, model)
    auth_file_identity = _materialize_auth_file(agent_dir, auth_fd)
    env = dict(os.environ)
    env["PI_CODING_AGENT_DIR"] = str(agent_dir)
    env["PI_PACKAGE_DIR"] = str(cli.parents[1])
    env["NODE_PATH"] = str(cli.parents[3])
    # Non-secret identity metadata tells the exact-commit extension which
    # descriptor/file pair to detach around each common tool boundary. The
    # extension fstats and matches the identities first; malformed/missing or
    # reused bindings block the tool without touching another inode.
    identity = os.fstat(auth_fd)
    # Bound the exact Node/tool process descriptor table so the extension can
    # synchronously inspect every possible numeric alias before a tool runs.
    # 4096 is ample for Pi/build tooling while making all-alias closure finite;
    # preserve an already-lower operator hard/soft limit.
    soft_limit, hard_limit = resource.getrlimit(resource.RLIMIT_NOFILE)
    finite_soft = 4096 if soft_limit == resource.RLIM_INFINITY else soft_limit
    finite_hard = 4096 if hard_limit == resource.RLIM_INFINITY else hard_limit
    fd_limit = min(4096, finite_soft, finite_hard)
    if fd_limit <= auth_fd or fd_limit < 64:
        raise SystemExit("factory-pi2-backend: descriptor limit cannot bound auth aliases")
    # Lower the hard limit too: untrusted model code must be unable to raise
    # the soft limit and duplicate the credential above the scanned range.
    resource.setrlimit(resource.RLIMIT_NOFILE, (fd_limit, fd_limit))
    enforced_soft, enforced_hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    if (enforced_soft, enforced_hard) != (fd_limit, fd_limit):
        raise SystemExit("factory-pi2-backend: descriptor alias bound was not enforced")
    env["PI_FACTORY_TOOL_FD"] = str(auth_fd)
    env["PI_FACTORY_TOOL_FD_DEV"] = str(identity.st_dev)
    env["PI_FACTORY_TOOL_FD_INO"] = str(identity.st_ino)
    env["PI_FACTORY_TOOL_FD_LIMIT"] = str(fd_limit)
    env["PI_FACTORY_TOOL_FILE"] = str(agent_dir / "auth.json")
    env["PI_FACTORY_TOOL_FILE_DEV"] = str(auth_file_identity.st_dev)
    env["PI_FACTORY_TOOL_FILE_INO"] = str(auth_file_identity.st_ino)
    os.execve(str(node), [str(node), str(cli), *remaining], env)


if __name__ == "__main__":
    main()
