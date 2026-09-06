#!/usr/bin/env python3
"""Validate the tracked, credential-free factory capability declaration."""

from __future__ import annotations

import argparse
import ipaddress
from pathlib import Path, PurePosixPath
import re
import tomllib
from urllib.parse import urlsplit

FORBIDDEN_KEYS = {
    "credential", "credentials", "host", "hostname", "identity_file", "key",
    "password", "port", "private_key", "secret", "token", "user", "username",
}
NAME_RE = re.compile(r"^[a-z0-9](?:[a-z0-9._-]{0,62}[a-z0-9])?$")
CAP_RE = NAME_RE
RUNNER_WORKDIR_RE = re.compile(r"^/(?:[A-Za-z0-9._-]+/)+[A-Za-z0-9._-]+$")


def fail(message: str) -> None:
    raise SystemExit(f"factory-environment: {message}")


def string_list(value: object, field: str) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(item, str) and item for item in value):
        fail(f"{field} must be an array of non-empty strings")
    return value


def reject_secrets(value: object, path: str = "") -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            lowered = key.lower()
            if lowered in FORBIDDEN_KEYS or any(word in lowered for word in ("password", "secret", "token", "private_key")):
                fail(f"forbidden credential or endpoint field: {path}{key}")
            reject_secrets(child, f"{path}{key}.")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            reject_secrets(child, f"{path}{index}.")
    elif isinstance(value, str):
        parsed = urlsplit(value)
        if parsed.scheme and (parsed.username or parsed.password):
            fail(f"embedded URL credentials at {path.rstrip('.')}")
        if "-----BEGIN " in value or re.search(r"(?i)(password|secret|token)\s*=", value):
            fail(f"credential-like value at {path.rstrip('.')}")


def validate(path: Path, require_empty: bool) -> None:
    if path.is_symlink() or not path.is_file():
        fail(f"{path} must be a regular tracked file")
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, tomllib.TOMLDecodeError) as exc:
        fail(f"cannot parse {path}: {exc}")
    if set(data) - {"schema_version", "tools", "runners"}:
        fail(f"unknown top-level keys: {sorted(set(data) - {'schema_version', 'tools', 'runners'})}")
    if data.get("schema_version") != 1:
        fail("schema_version must be 1")
    reject_secrets(data)
    tools = data.get("tools", [])
    runners = data.get("runners", [])
    if not isinstance(tools, list) or not isinstance(runners, list):
        fail("tools and runners must be arrays of tables")
    if require_empty and (tools or runners):
        fail("template must not declare tools or runners yet")

    names: set[str] = set()
    for index, tool in enumerate(tools):
        if not isinstance(tool, dict) or set(tool) != {"name", "command", "capabilities"}:
            fail(f"tools[{index}] must contain exactly name, command, and capabilities")
        name = tool["name"]
        command = tool["command"]
        capabilities = string_list(tool["capabilities"], f"tools[{index}].capabilities")
        if not isinstance(name, str) or not NAME_RE.fullmatch(name):
            fail(f"invalid tools[{index}].name")
        if not isinstance(command, str) or not command or any(char.isspace() for char in command):
            fail(f"tools[{index}].command must be one executable name or path")
        if name in names:
            fail(f"duplicate capability name: {name}")
        if not all(CAP_RE.fullmatch(item) for item in capabilities):
            fail(f"invalid tools[{index}] capability")
        names.add(name)

    for index, runner in enumerate(runners):
        required = {"name", "transport", "ssh_config_alias", "working_directory", "capabilities", "verify_argv"}
        if not isinstance(runner, dict) or set(runner) != required:
            fail(f"runners[{index}] must contain exactly {sorted(required)}")
        name = runner["name"]
        if not isinstance(name, str) or not NAME_RE.fullmatch(name):
            fail(f"invalid runners[{index}].name")
        if name in names:
            fail(f"duplicate capability name: {name}")
        if runner["transport"] != "ssh":
            fail(f"runners[{index}].transport must be ssh")
        alias = runner["ssh_config_alias"]
        if not isinstance(alias, str) or not NAME_RE.fullmatch(alias):
            fail(f"runners[{index}].ssh_config_alias must be a credential-free SSH config alias")
        try:
            ipaddress.ip_address(alias)
        except ValueError:
            pass
        else:
            fail(f"runners[{index}].ssh_config_alias must not be an IP endpoint")
        workdir = runner["working_directory"]
        if (
            not isinstance(workdir, str)
            or not RUNNER_WORKDIR_RE.fullmatch(workdir)
            or ".." in PurePosixPath(workdir).parts
        ):
            fail(f"runners[{index}].working_directory must be a canonical absolute non-root path")
        capabilities = string_list(runner["capabilities"], f"runners[{index}].capabilities")
        argv = string_list(runner["verify_argv"], f"runners[{index}].verify_argv")
        if not capabilities or len(capabilities) != len(set(capabilities)) or not all(CAP_RE.fullmatch(item) for item in capabilities):
            fail(f"runners[{index}] requires unique valid capabilities")
        sensitive_flags = {"-i", "--identity-file", "--password", "--private-key", "--token", "--secret", "--user", "-l", "--header", "-H"}
        if len(argv) > 64 or len(argv[0]) > 256 or argv[0].startswith("/") or ".." in PurePosixPath(argv[0]).parts:
            fail(f"runners[{index}].verify_argv must name a bounded repository-relative verifier")
        if any(any(ord(char) < 32 for char in item) or len(item.encode()) > 512 for item in argv):
            fail(f"runners[{index}].verify_argv must be control-character-free")
        if any(re.search(r"[;&|`$<>]", item) for item in argv):
            fail(f"runners[{index}].verify_argv contains shell syntax")
        if any(
            item.lower() in sensitive_flags
            or re.search(r"(?i)(authorization|bearer|password|private[-_]?key|token|secret|[a-z][a-z0-9+.-]*://)", item)
            for item in argv
        ):
            fail(f"runners[{index}].verify_argv contains a credential or endpoint option")
        names.add(name)

    print(f"factory-environment: valid ({len(tools)} tools, {len(runners)} runners)")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("path", nargs="?", default=".factory/environment.toml")
    parser.add_argument("--require-empty", action="store_true")
    args = parser.parse_args()
    validate(Path(args.path), args.require_empty)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
