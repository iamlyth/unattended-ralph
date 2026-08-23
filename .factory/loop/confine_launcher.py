#!/usr/bin/env python3
"""Confined-launch child: apply the Task 8 Landlock confinement, then exec.

This module is executed as the first stage of a *confined* model launch
(``python confine_launcher.py --spec-file <spec> -- <command...>``).  It is
staged from its exact committed blob by the launch authority (F2) and is
fully self-contained (standard library only, no imports from the hidden
package), because it runs from the private staging directory.

Sequence:

1. read the confinement specification (schema ``factory-confinement/v1``)
   through a bounded no-follow descriptor;
2. build a Landlock ruleset handling every supported access bit and add one
   allow rule per specification entry (deny-by-default: everything outside
   the allowlist is denied for read and write);
3. restrict the current process (children inherit the restriction through
   exec), so the secure wrapper and the model backend — and every tool they
   spawn — run under the same confinement;
4. install the sanitized private HOME/XDG environment from the
   specification;
5. exec the secure wrapper command (``execvpe``), never returning.

The access-bit table must match the canonical table in ``confinement.py``;
the hidden suite asserts the two tables agree.
"""

from __future__ import annotations

import ctypes
import errno
import json
import os
from pathlib import Path
import sys
from typing import Dict, List, Mapping, Sequence, Tuple

CONFINEMENT_SCHEMA = "factory-confinement/v1"
MAX_SPEC_BYTES = 1024 * 1024
_BLOB_CHUNK = 65536

# Landlock syscall numbers and constants (stable on Linux since 5.13).
_LANDLOCK_CREATE_RULESET = 444
_LANDLOCK_ADD_RULE = 445
_LANDLOCK_RESTRICT_SELF = 446
_LANDLOCK_CREATE_RULESET_VERSION = 0x1
_LANDLOCK_RULE_PATH_BENEATH = 0x1

# FS access bits (identical to ``confinement.RIGHT_BITS``).
_ACCESS_EXECUTE = 1 << 0
_ACCESS_WRITE_FILE = 1 << 1
_ACCESS_READ_FILE = 1 << 2
_ACCESS_READ_DIR = 1 << 3
_ACCESS_REMOVE_DIR = 1 << 4
_ACCESS_REMOVE_FILE = 1 << 5
_ACCESS_MAKE_CHAR = 1 << 6
_ACCESS_MAKE_DIR = 1 << 7
_ACCESS_MAKE_REG = 1 << 8
_ACCESS_MAKE_SOCK = 1 << 9
_ACCESS_MAKE_FIFO = 1 << 10
_ACCESS_MAKE_BLOCK = 1 << 11
_ACCESS_MAKE_SYM = 1 << 12
_ACCESS_REFER = 1 << 13
_ACCESS_TRUNCATE = 1 << 14

RIGHT_BITS: Mapping[str, int] = {
    "read": _ACCESS_READ_FILE | _ACCESS_READ_DIR,
    "write": (
        _ACCESS_WRITE_FILE | _ACCESS_REMOVE_DIR | _ACCESS_REMOVE_FILE
        | _ACCESS_MAKE_CHAR | _ACCESS_MAKE_DIR | _ACCESS_MAKE_REG
        | _ACCESS_MAKE_SOCK | _ACCESS_MAKE_FIFO | _ACCESS_MAKE_BLOCK
        | _ACCESS_MAKE_SYM | _ACCESS_REFER | _ACCESS_TRUNCATE
    ),
    "execute": _ACCESS_EXECUTE,
}

# Rights that can apply to a *file* rule (``landlock_append_fs_rule``
# rejects any other bit on a non-directory with EINVAL).  Directory rules
# keep the full handled set.
_FILE_RIGHTS = (
    _ACCESS_EXECUTE | _ACCESS_WRITE_FILE | _ACCESS_READ_FILE | _ACCESS_TRUNCATE
)


class ConfineLaunchError(Exception):
    """The confined launch failed closed before exec."""


def die(message: str) -> "NoReturn":
    raise SystemExit(f"confine-launcher: {message}")


def _read_spec(path_text: str) -> Dict[str, object]:
    """Bounded no-follow read + parse of the confinement specification."""
    flags = (
        os.O_RDONLY
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )
    try:
        descriptor = os.open(path_text, flags)
    except OSError as exc:
        raise ConfineLaunchError(f"cannot open the confinement spec {path_text}: {exc}")
    try:
        info = os.fstat(descriptor)
        if not stat_regular(info):
            raise ConfineLaunchError(f"the confinement spec {path_text} is not regular")
        if info.st_size > MAX_SPEC_BYTES:
            raise ConfineLaunchError(f"the confinement spec {path_text} is oversized")
        before = (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)
        chunks: List[bytes] = []
        remaining = info.st_size + 1
        while remaining:
            chunk = os.read(descriptor, min(_BLOB_CHUNK, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        after = os.fstat(descriptor)
        if (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns) != before:
            raise ConfineLaunchError(f"the confinement spec {path_text} changed while read")
        data = b"".join(chunks)
    finally:
        os.close(descriptor)
    try:
        document = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise ConfineLaunchError(f"the confinement spec is not valid JSON: {exc}")
    if not isinstance(document, dict):
        raise ConfineLaunchError("the confinement spec must be a JSON object")
    if document.get("schema") != CONFINEMENT_SCHEMA:
        raise ConfineLaunchError(
            f"the confinement spec schema must be {CONFINEMENT_SCHEMA!r}"
        )
    rules = document.get("rules")
    if not isinstance(rules, list) or not rules:
        raise ConfineLaunchError("the confinement spec carries no rules")
    for rule in rules:
        if not isinstance(rule, dict):
            raise ConfineLaunchError("a confinement rule must be an object")
        path = rule.get("path")
        access = rule.get("access")
        if not isinstance(path, str) or not path:
            raise ConfineLaunchError("a confinement rule must carry a path")
        if not isinstance(access, list) or not access:
            raise ConfineLaunchError(f"rule {path} must carry an access list")
        for right in access:
            if right not in RIGHT_BITS:
                raise ConfineLaunchError(f"rule {path} has unknown right {right!r}")
    return document  # type: ignore[return-value]


def stat_regular(info: object) -> bool:
    import stat

    return stat.S_ISREG(info.st_mode)


def _landlock_abi() -> int:
    libc = ctypes.CDLL(None, use_errno=True)
    try:
        result = libc.syscall(
            _LANDLOCK_CREATE_RULESET, None, 0, _LANDLOCK_CREATE_RULESET_VERSION
        )
    except (AttributeError, OSError):
        return 0
    return int(result) if result >= 0 else 0


def _handled_access_bits(abi: int) -> int:
    bits = (
        _ACCESS_EXECUTE | _ACCESS_WRITE_FILE | _ACCESS_READ_FILE | _ACCESS_READ_DIR
        | _ACCESS_REMOVE_DIR | _ACCESS_REMOVE_FILE | _ACCESS_MAKE_CHAR
        | _ACCESS_MAKE_DIR | _ACCESS_MAKE_REG | _ACCESS_MAKE_SOCK
        | _ACCESS_MAKE_FIFO | _ACCESS_MAKE_BLOCK | _ACCESS_MAKE_SYM
    )
    if abi >= 2:
        bits |= _ACCESS_REFER
    if abi >= 3:
        bits |= _ACCESS_TRUNCATE
    return bits


class _RulesetAttr(ctypes.Structure):
    _fields_ = [("handled_access_fs", ctypes.c_uint64)]


class _PathBeneath(ctypes.Structure):
    _fields_ = [
        ("allowed_access", ctypes.c_uint64),
        ("parent_fd", ctypes.c_int),
    ]


def _create_ruleset(handled: int) -> int:
    libc = ctypes.CDLL(None, use_errno=True)
    attr = _RulesetAttr(handled)
    descriptor = libc.syscall(
        _LANDLOCK_CREATE_RULESET, ctypes.byref(attr), ctypes.sizeof(attr), 0
    )
    if descriptor < 0:
        raise ConfineLaunchError(
            "landlock_create_ruleset failed: "
            + errno.errorcode.get(ctypes.get_errno(), str(ctypes.get_errno()))
        )
    return int(descriptor)


def _add_rule(descriptor: int, path_text: str, bits: int, handled: int) -> None:
    """Add one path-beneath allow rule, masked to what the kernel accepts.

    ``landlock_append_fs_rule`` rejects any non-``ACCESS_FILE`` bit on a
    *file* rule with EINVAL, and ``add_rule_path_beneath`` rejects any bit
    outside the ruleset's handled mask, so the requested bits are masked to
    ``handled`` and, for a non-directory final component, to the file rights
    (EXECUTE/WRITE_FILE/READ_FILE/TRUNCATE).  A rule that grants nothing
    after masking is a malformed specification and fails closed.
    """
    libc = ctypes.CDLL(None, use_errno=True)
    try:
        parent_fd = os.open(path_text, os.O_PATH | os.O_CLOEXEC)
    except OSError as exc:
        raise ConfineLaunchError(
            f"cannot open allowlisted path {path_text}: {exc}"
        )
    try:
        import stat as stat_module

        info = os.fstat(parent_fd)
        effective = bits & handled
        if not stat_module.S_ISDIR(info.st_mode):
            effective &= _FILE_RIGHTS
        if effective == 0:
            raise ConfineLaunchError(
                f"allowlisted path {path_text} grants no effective Landlock "
                "rights after masking; the confinement specification is "
                "malformed (fail closed)"
            )
        beneath = _PathBeneath(effective, parent_fd)
        result = libc.syscall(
            _LANDLOCK_ADD_RULE,
            descriptor,
            _LANDLOCK_RULE_PATH_BENEATH,
            ctypes.byref(beneath),
            0,
        )
        if result != 0:
            raise ConfineLaunchError(
                f"landlock_add_rule failed for {path_text}: "
                + errno.errorcode.get(ctypes.get_errno(), str(ctypes.get_errno()))
            )
    finally:
        os.close(parent_fd)


def _restrict_self(descriptor: int) -> None:
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.syscall(_LANDLOCK_RESTRICT_SELF, descriptor, 0) != 0:
        raise ConfineLaunchError(
            "landlock_restrict_self failed: "
            + errno.errorcode.get(ctypes.get_errno(), str(ctypes.get_errno()))
        )


def apply_confinement(spec: Mapping[str, object]) -> None:
    """Build the ruleset from the spec and restrict the current process.

    The restriction is irreversible for this process and is inherited by
    every child through exec; the secure wrapper and the model backend —
    and every tool they spawn — therefore run under the exact allowlists in
    the specification.  Any failure fails closed before exec.
    """
    abi = _landlock_abi()
    if abi < 1:
        raise ConfineLaunchError(
            "the Landlock LSM is unavailable; the confined launch cannot "
            "proceed (fail closed)"
        )
    handled = _handled_access_bits(abi)
    descriptor = _create_ruleset(handled)
    try:
        for rule in spec.get("rules", []):  # type: ignore[union-attr]
            bits = 0
            for right in rule["access"]:
                bits |= RIGHT_BITS[right]
            _add_rule(descriptor, str(rule["path"]), bits, handled)
        _restrict_self(descriptor)
    finally:
        os.close(descriptor)


def _install_home_environment(spec: Mapping[str, object]) -> None:
    """Install the sanitized private HOME/XDG environment from the spec."""
    env = spec.get("env")
    if not isinstance(env, dict):
        raise ConfineLaunchError("the confinement spec carries no sanitized env")
    for key, value in env.items():
        if not isinstance(key, str) or not isinstance(value, str):
            raise ConfineLaunchError("the sanitized environment must map str to str")
        os.environ[key] = value


def main(argv: Sequence[str]) -> int:
    if len(argv) < 4 or argv[0] != "--spec-file" or argv[2] != "--":
        die("usage: confine_launcher.py --spec-file <spec> -- <command...>")
    spec_path = argv[1]
    command = list(argv[3:])
    if not command:
        die("missing confined command")
    try:
        spec = _read_spec(spec_path)
        apply_confinement(spec)
        _install_home_environment(spec)
    except ConfineLaunchError as exc:
        die(str(exc))
    # Never return: exec the secure wrapper with the confined environment.
    os.execvpe(command[0], command, os.environ)
    die(f"cannot exec {command[0]}")


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
