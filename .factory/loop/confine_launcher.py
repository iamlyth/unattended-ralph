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
3. make the fresh trusted process an unrelated-child-free subreaper, then
   fork its confined target; the target applies Landlock, asks that dedicated
   ptrace broker to trace it and every descendant, then installs an inherited
   seccomp exec filter;
4. kill every non-native ABI (including x86 compat and x32) in the kernel
   before syscall dispatch and route native ``execve`` to a ptrace stop;
5. replace an approved request with ``execveat(approved_fd, "", ..., AT_EMPTY_PATH)``
   where ``approved_fd`` is a broker-retained, inherited descriptor whose slot
   cannot be closed or replaced under the seccomp filter.  The kernel therefore
   executes the approved inode, never a pathname re-read from mutable tracee
   memory.  User-issued ``execveat`` and direct PT_INTERP loaders are denied;
6. install the sanitized private HOME/XDG environment and execute the secure
   wrapper; every descendant inherits Landlock, seccomp, the protected approved
   descriptor table, and ptrace supervision.

Every allowlist rule is applied from an inherited ``O_PATH`` descriptor opened
by the trusted parent.  The specification binds each descriptor's
dev/inode/type/owner/link-count identity; this child never reopens an
allowlisted pathname, eliminating path-substitution races.

The access-bit table must match the canonical table in
``workspace_confinement.py``; the hidden suite asserts the two tables agree.
"""

from __future__ import annotations

import base64
import ctypes
import errno
import fcntl
import json
import os
from pathlib import Path
import platform
import resource
import signal
import sys
import time
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
_PR_SET_NO_NEW_PRIVS = 38
_PR_GET_NO_NEW_PRIVS = 39
_PR_SET_PDEATHSIG = 1
_PR_SET_CHILD_SUBREAPER = 36

# Atomic inode-bound executable broker.  Landlock must grant the ELF
# PT_INTERP loader EXECUTE for normal dynamic programs, but that loader is
# never present in the approved descriptor table.  Native execve enters a
# ptrace seccomp stop; the trusted ancestor rewrites an approved request to
# execveat on a protected inherited O_PATH descriptor with AT_EMPTY_PATH.
# No notification continuation exists in this design: mutable tracee pathname
# bytes only choose an approved descriptor or denial, and are never consumed by the
# kernel as the execution target.  The BPF prologue validates
# seccomp_data.arch before reading the syscall number and kills compat/x32
# calls in-kernel.
_SECCOMP_SET_MODE_FILTER = 1
_SECCOMP_RET_KILL_PROCESS = 0x80000000
_SECCOMP_RET_TRACE = 0x7FF00000
_SECCOMP_RET_ALLOW = 0x7FFF0000
_BPF_LD_W_ABS = 0x20
_BPF_JMP_JEQ_K = 0x15
_BPF_JMP_JGT_K = 0x25
_BPF_JMP_JGE_K = 0x35
_BPF_JMP_JSET_K = 0x45
_BPF_RET_K = 0x06
_AUDIT_ARCH_X86_64 = 0xC000003E
_AUDIT_ARCH_AARCH64 = 0xC00000B7
_X32_SYSCALL_BIT = 0x40000000
_CLONE_UNTRACED = 0x00800000
_AT_FDCWD = -100
_AT_EMPTY_PATH = 0x1000

# ptrace constants (stable Linux UAPI).  PTRACE_O_EXITKILL gives the kernel
# enforcement boundary its fail-closed supervisor-death behavior; fork/clone
# tracing keeps every ordinary descendant under the same atomic rewrite.
_PTRACE_TRACEME = 0
_PTRACE_CONT = 7
_PTRACE_GETREGS = 12
_PTRACE_SETREGS = 13
_PTRACE_SET_SYSCALL = 23
_PTRACE_SETOPTIONS = 0x4200
_PTRACE_GETEVENTMSG = 0x4201
_PTRACE_GETREGSET = 0x4204
_PTRACE_SETREGSET = 0x4205
_PTRACE_EVENT_FORK = 1
_PTRACE_EVENT_VFORK = 2
_PTRACE_EVENT_CLONE = 3
_PTRACE_EVENT_EXEC = 4
_PTRACE_EVENT_EXIT = 6
_PTRACE_EVENT_SECCOMP = 7
_PTRACE_O_TRACEFORK = 1 << _PTRACE_EVENT_FORK
_PTRACE_O_TRACEVFORK = 1 << _PTRACE_EVENT_VFORK
_PTRACE_O_TRACECLONE = 1 << _PTRACE_EVENT_CLONE
_PTRACE_O_TRACEEXEC = 1 << _PTRACE_EVENT_EXEC
_PTRACE_O_TRACEEXIT = 1 << _PTRACE_EVENT_EXIT
_PTRACE_O_TRACESECCOMP = 1 << _PTRACE_EVENT_SECCOMP
_PTRACE_O_EXITKILL = 1 << 20
_PTRACE_OPTIONS = (
    _PTRACE_O_TRACEFORK | _PTRACE_O_TRACEVFORK | _PTRACE_O_TRACECLONE
    | _PTRACE_O_TRACEEXEC | _PTRACE_O_TRACEEXIT | _PTRACE_O_TRACESECCOMP
    | _PTRACE_O_EXITKILL
)
_WAIT_WALL = 0x40000000
_NT_PRSTATUS = 1

# One-byte trusted lifecycle protocol to the outer launch supervisor.  The
# confined target closes this descriptor before applying Landlock or running
# untrusted code, so only this ptrace broker can report whether a descendant
# outlived the target.  A missing/malformed byte fails closed at the outer
# supervisor; direct launcher tests may omit the optional descriptor.
_SUPERVISION_CLEAN = b"C"
_SUPERVISION_ESCAPED = b"E"
_SUPERVISION_FAILED = b"F"
_DESCENDANT_REAP_TIMEOUT = 2.0

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
        identity = rule.get("identity")
        if set(rule) != {"path", "access", "identity"}:
            raise ConfineLaunchError(
                "a confinement rule must carry exactly path/access/identity"
            )
        if not isinstance(path, str) or not path:
            raise ConfineLaunchError("a confinement rule must carry a path")
        if not isinstance(access, list) or not access:
            raise ConfineLaunchError(f"rule {path} must carry an access list")
        identity_keys = {"dev", "ino", "type", "uid", "nlink"}
        if (
            not isinstance(identity, dict)
            or set(identity) != identity_keys
            or not all(type(identity[key]) is int and identity[key] >= 0
                       for key in identity_keys)
        ):
            raise ConfineLaunchError(
                f"rule {path} has an invalid dev/ino/type/owner/nlink identity"
            )
        for right in access:
            if right not in RIGHT_BITS:
                raise ConfineLaunchError(f"rule {path} has unknown right {right!r}")
    return document  # type: ignore[return-value]


def stat_regular(info: object) -> bool:
    import stat

    return stat.S_ISREG(info.st_mode)


def _validate_lease(spec: Mapping[str, object]) -> None:
    """Revalidate the authenticated task path-lease inside the confined child.

    Phase 2C2a: the exact canonical claim bytes and the trusted context
    travel in the confinement specification; the committed path-lease policy
    is re-loaded from the workspace through the exact no-follow authority
    (the staged ``path_lease`` sibling is the exact bound-commit module, F2).
    Schema, deny-dominant expansion, policy digest, and context/expiry
    (campaign/task/attempt/HEAD/plan/policy digest, issued/deadline) are
    re-derived before any Landlock rule is applied, so a stale, tampered,
    replayed, or foreign claim — including a worktree policy tamper — fails
    closed.  The claim digest alone is never authoritative.
    """
    lease = spec.get("lease")
    if lease is None:
        return
    if not isinstance(lease, Mapping):
        raise ConfineLaunchError("the confinement lease section is malformed")
    claim_text = lease.get("claim")
    context = lease.get("context")
    if not isinstance(claim_text, str) or not isinstance(context, Mapping):
        raise ConfineLaunchError(
            "the confinement lease claim/context is malformed"
        )
    try:
        claim_bytes = base64.b64decode(claim_text, validate=True)
    except (ValueError, TypeError) as exc:
        raise ConfineLaunchError(
            f"the confinement lease claim is not base64: {exc}"
        ) from exc
    here = os.path.dirname(os.path.abspath(__file__))
    if here not in sys.path:
        sys.path.insert(0, here)
    try:
        import path_lease  # staged exact-commit sibling (F2)
    except ImportError as exc:
        raise ConfineLaunchError(
            "the staged path-lease authority is unavailable"
        ) from exc
    try:
        claim = path_lease.parse_claim(claim_bytes)
        policy = path_lease.load_policy_config(Path(str(spec.get("workspace"))))
        path_lease.validate_claim(claim, policy)
        path_lease.validate_claim_context(
            claim,
            campaign_id=context.get("campaign_id"),
            task_id=context.get("task_id"),
            attempt=context.get("attempt"),
            head_commit=context.get("head_commit"),
            plan_digest=context.get("plan_digest"),
            policy_digest=context.get("policy_digest"),
        )
    except path_lease.PathLeaseError as exc:
        raise ConfineLaunchError(
            f"the task path-lease fails closed: {exc}"
        ) from exc


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


def _fd_identity(descriptor: int) -> Dict[str, int]:
    import stat as stat_module

    info = os.fstat(descriptor)
    return {
        "dev": int(info.st_dev),
        "ino": int(info.st_ino),
        "type": int(stat_module.S_IFMT(info.st_mode)),
        "uid": int(info.st_uid),
        "nlink": int(info.st_nlink),
    }


def _add_rule(
    descriptor: int,
    parent_fd: int,
    path_text: str,
    identity: Mapping[str, object],
    bits: int,
    handled: int,
) -> None:
    """Add one descriptor-anchored path-beneath allow rule.

    ``path_text`` is diagnostic text only and is never opened.  The inherited
    descriptor must still match the specification's complete filesystem
    identity immediately before the Landlock syscall.  Thus file, directory,
    symlink, and same-name substitutions after parent validation all fail
    closed or remain irrelevant to the retained descriptor.
    """
    libc = ctypes.CDLL(None, use_errno=True)
    import stat as stat_module

    try:
        actual_identity = _fd_identity(parent_fd)
    except OSError as exc:
        raise ConfineLaunchError(
            f"cannot inspect inherited rule descriptor for {path_text}: {exc}"
        ) from exc
    if actual_identity != dict(identity):
        raise ConfineLaunchError(
            f"inherited descriptor identity mismatch for {path_text} "
            "(dev/ino/type/owner/nlink); refusing pathname substitution"
        )
    info = os.fstat(parent_fd)
    if stat_module.S_ISLNK(info.st_mode):
        raise ConfineLaunchError(
            f"inherited rule descriptor for {path_text} names a symlink"
        )
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


def _set_no_new_privs() -> None:
    """Set and verify PR_SET_NO_NEW_PRIVS before Landlock restriction."""
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(_PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) != 0:
        raise ConfineLaunchError(
            "PR_SET_NO_NEW_PRIVS failed: "
            + errno.errorcode.get(ctypes.get_errno(), str(ctypes.get_errno()))
        )
    if libc.prctl(_PR_GET_NO_NEW_PRIVS, 0, 0, 0, 0) != 1:
        raise ConfineLaunchError(
            "PR_SET_NO_NEW_PRIVS could not be verified before Landlock"
        )


def _restrict_self(descriptor: int) -> None:
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.syscall(_LANDLOCK_RESTRICT_SELF, descriptor, 0) != 0:
        raise ConfineLaunchError(
            "landlock_restrict_self failed: "
            + errno.errorcode.get(ctypes.get_errno(), str(ctypes.get_errno()))
        )


def apply_confinement(
    spec: Mapping[str, object], rule_fds: Sequence[int]
) -> None:
    """Build the ruleset from inherited anchors and restrict this process."""
    rules = spec.get("rules", [])
    if not isinstance(rules, list) or len(rule_fds) != len(rules):
        raise ConfineLaunchError(
            "the inherited rule descriptor count does not match the specification"
        )
    if len(set(rule_fds)) != len(rule_fds) or any(fd < 0 for fd in rule_fds):
        raise ConfineLaunchError("the inherited rule descriptor list is invalid")
    abi = _landlock_abi()
    if abi < 3:
        raise ConfineLaunchError(
            "Landlock ABI 3 or newer is required so truncate is mediated; "
            f"host reported ABI {abi} (fail closed)"
        )
    handled = _handled_access_bits(abi)
    descriptor = -1
    try:
        # Landlock restrict_self requires no_new_privs for an unprivileged
        # caller.  Set and verify it before creating/applying the ruleset so
        # the launcher and the primitive probe exercise the same sequence.
        _set_no_new_privs()
        descriptor = _create_ruleset(handled)
        for rule, parent_fd in zip(rules, rule_fds):
            bits = 0
            for right in rule["access"]:
                bits |= RIGHT_BITS[right]
            _add_rule(
                descriptor,
                parent_fd,
                str(rule["path"]),
                rule["identity"],
                bits,
                handled,
            )
        _restrict_self(descriptor)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        for parent_fd in rule_fds:
            try:
                os.close(parent_fd)
            except OSError:
                pass


class _SockFilter(ctypes.Structure):
    _fields_ = [
        ("code", ctypes.c_ushort), ("jt", ctypes.c_ubyte),
        ("jf", ctypes.c_ubyte), ("k", ctypes.c_uint32),
    ]


class _SockFprog(ctypes.Structure):
    _fields_ = [("len", ctypes.c_ushort), ("filter", ctypes.POINTER(_SockFilter))]


class _X86UserRegs(ctypes.Structure):
    _fields_ = [(name, ctypes.c_uint64) for name in (
        "r15", "r14", "r13", "r12", "rbp", "rbx", "r11", "r10",
        "r9", "r8", "rax", "rcx", "rdx", "rsi", "rdi", "orig_rax",
        "rip", "cs", "eflags", "rsp", "ss", "fs_base", "gs_base",
        "ds", "es", "fs", "gs",
    )]


class _Aarch64UserRegs(ctypes.Structure):
    _fields_ = [
        ("regs", ctypes.c_uint64 * 31), ("sp", ctypes.c_uint64),
        ("pc", ctypes.c_uint64), ("pstate", ctypes.c_uint64),
    ]


class _Iovec(ctypes.Structure):
    _fields_ = [("iov_base", ctypes.c_void_p), ("iov_len", ctypes.c_size_t)]


def _seccomp_abi() -> Tuple[int, int, int, int, bool]:
    """Return seccomp/exec syscall numbers and the one accepted audit ABI."""
    machine = platform.machine().lower()
    if machine in ("x86_64", "amd64"):
        return 317, 59, 322, _AUDIT_ARCH_X86_64, True
    if machine in ("aarch64", "arm64"):
        return 277, 221, 281, _AUDIT_ARCH_AARCH64, False
    raise ConfineLaunchError(
        f"seccomp executable broker has no fail-closed syscall table for {machine}"
    )


def _seccomp_numbers() -> Tuple[int, int, int]:
    """Compatibility helper used by tests and ptrace dispatch."""
    seccomp_nr, execve_nr, execveat_nr, _arch, _x32 = _seccomp_abi()
    return seccomp_nr, execve_nr, execveat_nr


def _native_audit_arch() -> int:
    return _seccomp_abi()[3]


def _brokered_scalar_syscalls() -> Dict[str, int]:
    """Native syscall table for descriptor-slot and broker isolation.

    Calls able to close/replace a protected executable descriptor are routed
    to ptrace so the broker can allow harmless scalar forms and reject an
    overlap.  io_uring is denied because IORING_OP_CLOSE would otherwise be a
    second, unfiltered close primitive.  Tracee ptrace/process-vm access is
    denied so descendants cannot attack their supervising ancestor. Native
    clone is brokered only when CLONE_UNTRACED is present; clone3 is always
    brokered because its flags live behind a mutable tracee pointer and cannot
    be authorized race-free.
    """
    machine = platform.machine().lower()
    if machine in ("x86_64", "amd64"):
        return {
            "close": 3, "mmap": 9, "mprotect": 10, "munmap": 11,
            "mremap": 25, "madvise": 28, "shmat": 30, "dup2": 33,
            "clone": 56, "fcntl": 72, "ptrace": 101,
            "remap_file_pages": 216, "dup3": 292,
            "process_vm_readv": 310, "process_vm_writev": 311,
            "userfaultfd": 323, "pkey_mprotect": 329,
            "io_uring_setup": 425, "io_uring_enter": 426,
            "io_uring_register": 427, "clone3": 435,
            "close_range": 436, "pidfd_getfd": 438,
        }
    if machine in ("aarch64", "arm64"):
        return {
            "dup3": 24, "fcntl": 25, "close": 57, "ptrace": 117,
            "shmat": 196, "clone": 220, "munmap": 215,
            "mremap": 216, "mmap": 222, "mprotect": 226,
            "madvise": 233, "remap_file_pages": 234,
            "process_vm_readv": 270, "process_vm_writev": 271,
            "userfaultfd": 282, "pkey_mprotect": 288,
            "io_uring_setup": 425, "io_uring_enter": 426,
            "io_uring_register": 427, "clone3": 435,
            "close_range": 436, "pidfd_getfd": 438,
        }
    raise ConfineLaunchError(
        f"seccomp descriptor broker has no fail-closed syscall table for {machine}"
    )


def _install_exec_trace_filter(
    exec_fds: Sequence[int] = (), exec_token: int = 0
) -> None:
    """Install native-ABI exec tracing plus protected descriptor slots.

    Safe execveat is a kernel-checkable scalar form only: exact AT_EMPTY_PATH
    and a descriptor inside the contiguous broker-created table.  Such a slot
    always names an immutable approved regular file and cannot be closed,
    CLOEXEC-marked, or replaced after this filter is installed.  Every other
    exec form stops in ptrace; it is never continued by pathname.
    """
    seccomp_nr, execve_nr, execveat_nr, audit_arch, reject_x32 = _seccomp_abi()
    slots = sorted(set(int(fd) for fd in exec_fds))
    if slots and not 0 < exec_token < (1 << 64):
        raise ConfineLaunchError("atomic exec token is unavailable")
    if slots and slots != list(range(slots[0], slots[-1] + 1)):
        raise ConfineLaunchError("approved executable descriptors are not contiguous")
    raw = [
        # seccomp_data.arch is offset 4. A non-native ABI is killed before the
        # syscall number is loaded, so compat int 0x80 cannot enter dispatch.
        _SockFilter(_BPF_LD_W_ABS, 0, 0, 4),
        _SockFilter(_BPF_JMP_JEQ_K, 1, 0, audit_arch),
        _SockFilter(_BPF_RET_K, 0, 0, _SECCOMP_RET_KILL_PROCESS),
        _SockFilter(_BPF_LD_W_ABS, 0, 0, 0),
    ]
    if reject_x32:
        # x32 reports AUDIT_ARCH_X86_64 but sets bit 30 in the syscall number.
        raw.extend([
            _SockFilter(_BPF_JMP_JSET_K, 0, 1, _X32_SYSCALL_BIT),
            _SockFilter(_BPF_RET_K, 0, 0, _SECCOMP_RET_KILL_PROCESS),
        ])
    raw.extend([
        _SockFilter(_BPF_JMP_JEQ_K, 0, 1, execve_nr),
        _SockFilter(_BPF_RET_K, 0, 0, _SECCOMP_RET_TRACE),
    ])

    if slots:
        # seccomp_data.args starts at offset 16; each argument is uint64.
        # Every 64-bit scalar is checked in both halves before allow.
        execveat_validation = [
            # args[5] is outside execveat's five-argument ABI.  A fresh
            # 64-bit launch token is embedded in this inherited filter and
            # set only by the ptrace broker.  Direct tracee execveat cannot
            # enter the safe path merely by naming a protected fd.
            _SockFilter(_BPF_LD_W_ABS, 0, 0, 60),       # args[5] high
            _SockFilter(_BPF_JMP_JEQ_K, 1, 0, exec_token >> 32),
            _SockFilter(_BPF_RET_K, 0, 0, _SECCOMP_RET_TRACE),
            _SockFilter(_BPF_LD_W_ABS, 0, 0, 56),       # args[5] low
            _SockFilter(_BPF_JMP_JEQ_K, 1, 0, exec_token & 0xFFFFFFFF),
            _SockFilter(_BPF_RET_K, 0, 0, _SECCOMP_RET_TRACE),
            _SockFilter(_BPF_LD_W_ABS, 0, 0, 52),       # args[4] high
            _SockFilter(_BPF_JMP_JEQ_K, 1, 0, 0),
            _SockFilter(_BPF_RET_K, 0, 0, _SECCOMP_RET_TRACE),
            _SockFilter(_BPF_LD_W_ABS, 0, 0, 48),       # args[4] low
            _SockFilter(_BPF_JMP_JEQ_K, 1, 0, _AT_EMPTY_PATH),
            _SockFilter(_BPF_RET_K, 0, 0, _SECCOMP_RET_TRACE),
            _SockFilter(_BPF_LD_W_ABS, 0, 0, 20),       # args[0] high
            _SockFilter(_BPF_JMP_JEQ_K, 1, 0, 0),
            _SockFilter(_BPF_RET_K, 0, 0, _SECCOMP_RET_TRACE),
            _SockFilter(_BPF_LD_W_ABS, 0, 0, 16),       # args[0] low
            _SockFilter(_BPF_JMP_JGE_K, 1, 0, slots[0]),
            _SockFilter(_BPF_RET_K, 0, 0, _SECCOMP_RET_TRACE),
            _SockFilter(_BPF_JMP_JGT_K, 0, 1, slots[-1]),
            _SockFilter(_BPF_RET_K, 0, 0, _SECCOMP_RET_TRACE),
            _SockFilter(_BPF_RET_K, 0, 0, _SECCOMP_RET_ALLOW),
        ]
    else:
        execveat_validation = [
            _SockFilter(_BPF_RET_K, 0, 0, _SECCOMP_RET_TRACE),
        ]
    if len(execveat_validation) > 255:
        raise ConfineLaunchError("seccomp execveat validation jump is oversized")
    raw.append(
        _SockFilter(
            _BPF_JMP_JEQ_K, 0, len(execveat_validation), execveat_nr
        )
    )
    raw.extend(execveat_validation)
    scalar = _brokered_scalar_syscalls()

    # clone(2) carries flags as a scalar first argument, so classic BPF can
    # reject exactly the bit that disables PTRACE_O_TRACECLONE while leaving
    # ordinary Pi thread/process creation on the native traced path. clone3
    # carries flags behind a tracee pointer: inspecting that memory and then
    # continuing would be a TOCTOU authorization race, so every clone3 call is
    # routed to the broker and denied.
    clone_block = [
        _SockFilter(_BPF_LD_W_ABS, 0, 0, 16),       # clone flags low
        _SockFilter(_BPF_JMP_JSET_K, 0, 1, _CLONE_UNTRACED),
        _SockFilter(_BPF_RET_K, 0, 0, _SECCOMP_RET_TRACE),
        _SockFilter(_BPF_RET_K, 0, 0, _SECCOMP_RET_ALLOW),
    ]
    raw.append(
        _SockFilter(_BPF_JMP_JEQ_K, 0, len(clone_block), scalar["clone"])
    )
    raw.extend(clone_block)
    raw.extend([
        _SockFilter(_BPF_JMP_JEQ_K, 0, 1, scalar["clone3"]),
        _SockFilter(_BPF_RET_K, 0, 0, _SECCOMP_RET_TRACE),
    ])

    def append_fd_slot_dispatch(number: int, low_offset: int) -> None:
        if not slots:
            return
        # Linux declares these descriptor parameters as ``unsigned int`` (or
        # ``int`` with the same low-32-bit syscall conversion).  seccomp_data
        # preserves the complete 64-bit register, but the syscall itself
        # discards the high half.  Comparing that high half would therefore
        # let (1 << 32) | slot reach the kernel as ``slot`` without a broker
        # stop.  Dispatch solely on the low word, exactly as the kernel does.
        block = [
            _SockFilter(_BPF_LD_W_ABS, 0, 0, low_offset),
            _SockFilter(_BPF_JMP_JGE_K, 1, 0, slots[0]),
            _SockFilter(_BPF_RET_K, 0, 0, _SECCOMP_RET_ALLOW),
            _SockFilter(_BPF_JMP_JGT_K, 0, 1, slots[-1]),
            _SockFilter(_BPF_RET_K, 0, 0, _SECCOMP_RET_ALLOW),
            _SockFilter(_BPF_RET_K, 0, 0, _SECCOMP_RET_TRACE),
        ]
        raw.append(_SockFilter(_BPF_JMP_JEQ_K, 0, len(block), number))
        raw.extend(block)

    # Only operations whose kernel-converted 32-bit fd/range overlaps the
    # protected table need a ptrace stop.  Ordinary close calls remain
    # native-speed (subprocess implementations may probe every fd up to
    # RLIMIT_NOFILE).
    append_fd_slot_dispatch(scalar["close"], 16)
    append_fd_slot_dispatch(scalar["fcntl"], 16)
    if "dup2" in scalar:
        append_fd_slot_dispatch(scalar["dup2"], 24)
    append_fd_slot_dispatch(scalar["dup3"], 24)
    if slots:
        # close_range(2) converts both endpoints to unsigned int.  As above,
        # only their low words describe the range that the kernel will close.
        close_range_block = [
            _SockFilter(_BPF_LD_W_ABS, 0, 0, 16),       # first low
            _SockFilter(_BPF_JMP_JGT_K, 0, 1, slots[-1]),
            _SockFilter(_BPF_RET_K, 0, 0, _SECCOMP_RET_ALLOW),
            _SockFilter(_BPF_LD_W_ABS, 0, 0, 24),       # last low
            _SockFilter(_BPF_JMP_JGE_K, 0, 1, slots[0]),
            _SockFilter(_BPF_RET_K, 0, 0, _SECCOMP_RET_TRACE),
            _SockFilter(_BPF_RET_K, 0, 0, _SECCOMP_RET_ALLOW),
        ]
        raw.append(
            _SockFilter(
                _BPF_JMP_JEQ_K, 0, len(close_range_block),
                scalar["close_range"],
            )
        )
        raw.extend(close_range_block)
    for name in (
        "mmap", "mprotect", "pkey_mprotect", "munmap", "madvise",
        "mremap", "shmat", "remap_file_pages", "userfaultfd",
        "io_uring_setup", "io_uring_enter", "io_uring_register", "ptrace",
        "process_vm_readv", "process_vm_writev", "pidfd_getfd",
    ):
        raw.extend([
            _SockFilter(_BPF_JMP_JEQ_K, 0, 1, scalar[name]),
            _SockFilter(_BPF_RET_K, 0, 0, _SECCOMP_RET_TRACE),
        ])
    raw.append(_SockFilter(_BPF_RET_K, 0, 0, _SECCOMP_RET_ALLOW))

    instructions = (_SockFilter * len(raw))(*raw)
    program = _SockFprog(len(instructions), instructions)
    libc = ctypes.CDLL(None, use_errno=True)
    result = libc.syscall(
        seccomp_nr, _SECCOMP_SET_MODE_FILTER, 0, ctypes.byref(program),
    )
    if result != 0:
        raise ConfineLaunchError(
            "seccomp executable broker is unavailable: "
            + errno.errorcode.get(ctypes.get_errno(), str(ctypes.get_errno()))
        )


def _read_tracee_string(
    pid: int, address: int, maximum: int = 4096
) -> Tuple[str, int]:
    """Read only a request selector and return its existing NUL address.

    The returned string is never continued as an execve pathname.  It can
    select only a descriptor from the pre-opened approved table.  The rewrite
    points execveat at the observed terminating NUL; if another thread changes
    that byte, a non-empty path relative to a regular-file fd fails ENOTDIR and
    still cannot select a different inode.
    """
    if address == 0:
        raise ConfineLaunchError("exec notification carries a null pathname")
    try:
        descriptor = os.open(f"/proc/{pid}/mem", os.O_RDONLY | os.O_CLOEXEC)
        try:
            raw = os.pread(descriptor, maximum, address)
        finally:
            os.close(descriptor)
    except OSError as exc:
        raise ConfineLaunchError(
            f"cannot inspect exec pathname for confined pid {pid}: {exc}"
        ) from exc
    value, separator, _tail = raw.partition(b"\x00")
    if not separator:
        raise ConfineLaunchError("exec pathname exceeds the broker bound")
    try:
        return value.decode("utf-8"), address + len(value)
    except UnicodeError as exc:
        raise ConfineLaunchError("exec pathname is not UTF-8") from exc


def _tracee_vdso_range(pid: int) -> Tuple[int, int]:
    """Return the stopped tracee's kernel-created read-only vDSO mapping."""
    try:
        with open(f"/proc/{pid}/maps", "r", encoding="ascii") as stream:
            text = stream.read(1024 * 1024 + 1)
    except OSError as exc:
        raise ConfineLaunchError(
            f"cannot inspect the confined pid {pid} vDSO mapping: {exc}"
        ) from exc
    if len(text) > 1024 * 1024:
        raise ConfineLaunchError("tracee memory map exceeds the broker bound")
    matches: List[Tuple[int, int]] = []
    for line in text.splitlines():
        fields = line.split()
        if not fields or fields[-1] != "[vdso]":
            continue
        try:
            start_text, end_text = fields[0].split("-", 1)
            start, end = int(start_text, 16), int(end_text, 16)
        except (ValueError, IndexError):
            raise ConfineLaunchError("tracee vDSO mapping is malformed") from None
        if start <= 0 or end <= start or end - start > 1024 * 1024:
            raise ConfineLaunchError("tracee vDSO mapping has unsafe bounds")
        matches.append((start, end))
    if len(matches) != 1:
        raise ConfineLaunchError("tracee has no unique kernel vDSO mapping")
    return matches[0]


def _immutable_tracee_nul(pid: int) -> int:
    """Select a NUL byte in the kernel-created, broker-protected vDSO.

    The inherited filter brokers every syscall that can make this mapping
    writable, replace it, or remove it.  Ordinary stores therefore cannot
    change the byte, and the execveat pathname consumed by the kernel is an
    immutable empty string rather than tracee-owned request storage.
    """
    start, end = _tracee_vdso_range(pid)
    try:
        descriptor = os.open(f"/proc/{pid}/mem", os.O_RDONLY | os.O_CLOEXEC)
        try:
            raw = os.pread(descriptor, end - start, start)
        finally:
            os.close(descriptor)
    except OSError as exc:
        raise ConfineLaunchError(
            f"cannot inspect the confined pid {pid} vDSO bytes: {exc}"
        ) from exc
    if len(raw) != end - start:
        raise ConfineLaunchError("tracee vDSO changed while selecting an empty string")
    offset = raw.find(b"\x00")
    if offset < 0 or _tracee_vdso_range(pid) != (start, end):
        raise ConfineLaunchError("tracee vDSO has no stable empty-string byte")
    return start + offset


def _range_overlaps(
    address: int, length: int, protected: Tuple[int, int]
) -> bool:
    if length <= 0:
        return False
    maximum = 1 << 64
    end = min(maximum, address + length)
    return address < protected[1] and end > protected[0]


def _loader_basename(path: str) -> bool:
    basename = os.path.basename(path).lower()
    return (
        "ld-linux" in basename or "ld-musl" in basename
        or basename.startswith("ld-") or basename.startswith("ld.so")
    )


def _open_immutable_exec(path: str) -> Tuple[int, Tuple[int, int]]:
    """Open one canonical immutable executable and retain its identity.

    The exact spelling must already be the real absolute path: relative,
    symlinked, dot-component, duplicate-separator, and proc-fd aliases are
    rejected. Every component through the trust boundary is privileged-owned
    and non-writable by the caller. The sticky Nix store root is the sole
    directory-mode exception; package entries beneath it remain non-writable.
    """
    import stat as stat_module

    if (
        not path.startswith("/") or "\x00" in path
        or path == "/proc" or path.startswith("/proc/")
        or os.path.normpath(path) != path
    ):
        raise ConfineLaunchError("exec pathname is not an absolute canonical path")
    try:
        resolved = os.path.realpath(path, strict=True)
    except (OSError, TypeError) as exc:
        raise ConfineLaunchError(f"cannot resolve exec pathname {path}: {exc}") from exc
    if resolved != path:
        raise ConfineLaunchError("symlinked or aliased exec pathname is forbidden")

    store = Path("/nix/store")
    in_store = path.startswith(str(store) + os.sep)
    boundary = store if in_store else Path(path).anchor
    privileged_uids = {0}
    if in_store:
        try:
            privileged_uids.add(int(store.lstat().st_uid))
        except OSError as exc:
            raise ConfineLaunchError(f"cannot inspect immutable store root: {exc}") from exc
    current = Path(path)
    while True:
        try:
            info = current.lstat()
        except OSError as exc:
            raise ConfineLaunchError(
                f"cannot inspect exec pathname component {current}: {exc}"
            ) from exc
        if stat_module.S_ISLNK(info.st_mode):
            raise ConfineLaunchError("symlinked exec pathname component is forbidden")
        if info.st_uid not in privileged_uids:
            raise ConfineLaunchError(
                f"exec pathname component is not privileged-owned: {current}"
            )
        sticky_store_root = (
            in_store and current == store and stat_module.S_ISDIR(info.st_mode)
            and bool(info.st_mode & stat_module.S_ISVTX)
        )
        if info.st_mode & 0o022 and not sticky_store_root:
            raise ConfineLaunchError(
                f"exec pathname component is group/other-writable: {current}"
            )
        if current == boundary or current == current.parent:
            break
        current = current.parent

    flags = (
        getattr(os, "O_PATH", os.O_RDONLY) | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )
    try:
        descriptor = os.open(path, flags)
        info = os.fstat(descriptor)
    except OSError as exc:
        raise ConfineLaunchError(f"cannot retain immutable exec {path}: {exc}") from exc
    if not stat_module.S_ISREG(info.st_mode) or not info.st_mode & 0o111:
        os.close(descriptor)
        raise ConfineLaunchError(f"immutable exec target is not executable: {path}")
    return descriptor, (int(info.st_dev), int(info.st_ino))


def _immutable_exec_alias_target(path: str) -> str | None:
    """Resolve one immutable Nix-store alias to a canonical approved target.

    PATH-facing Nix commands are frequently symlinks (for example nix-shell ->
    nix). The alias is only a selector: every lexical component from the store
    root through the alias must be privileged-owned and non-writable, the
    resolved target must stay in the store, and execution still uses the
    pre-opened descriptor of an already approved canonical inode. Mutable,
    relative, proc-fd, escaping, or non-store aliases are never resolved.
    """
    import stat as stat_module

    store = Path("/nix/store")
    if (
        not path.startswith(str(store) + os.sep) or "\x00" in path
        or os.path.normpath(path) != path
    ):
        return None
    try:
        store_info = store.lstat()
        store_uid = int(store_info.st_uid)
    except OSError:
        return None
    if (
        not stat_module.S_ISDIR(store_info.st_mode)
        or store_uid == os.getuid()
        or (store_info.st_mode & 0o022
            and not bool(store_info.st_mode & stat_module.S_ISVTX))
    ):
        return None
    allowed_uids = {0, store_uid}
    current = store
    relative = Path(path).relative_to(store)
    try:
        for part in relative.parts:
            current = current / part
            info = current.lstat()
            if info.st_uid not in allowed_uids:
                return None
            # Symlink permission bits are not consulted by Linux and are
            # conventionally 0777; immutability comes from the non-writable
            # privileged-owned parent plus the validated target chain.
            if not stat_module.S_ISLNK(info.st_mode) and info.st_mode & 0o022:
                return None
            if not (
                stat_module.S_ISDIR(info.st_mode)
                or stat_module.S_ISREG(info.st_mode)
                or stat_module.S_ISLNK(info.st_mode)
            ):
                return None
        resolved = os.path.realpath(path, strict=True)
    except (OSError, TypeError, ValueError):
        return None
    if not resolved.startswith(str(store) + os.sep) or resolved == path:
        return None
    return resolved


def _approved_exec_targets(
    spec: Mapping[str, object], rule_fds: Sequence[int]
) -> Dict[Tuple[int, int], Tuple[str, int]]:
    """Bind canonical immutable exec paths to broker-retained descriptors."""
    rules = spec.get("rules", [])
    if not isinstance(rules, list) or len(rules) != len(rule_fds):
        raise ConfineLaunchError(
            "seccomp executable broker rule descriptor count mismatch"
        )
    approved: Dict[Tuple[int, int], Tuple[str, int]] = {}
    try:
        for rule, anchor_fd in zip(rules, rule_fds):
            if "execute" not in rule["access"]:
                continue
            path = str(rule["path"])
            # PT_INTERP is a kernel implementation prerequisite, never a
            # userspace command. Mutable staged files are data and likewise
            # never become direct targets; approved interpreters read them.
            if _loader_basename(path):
                continue
            try:
                retained_fd, identity = _open_immutable_exec(path)
            except ConfineLaunchError:
                continue
            expected = rule["identity"]
            complete = _fd_identity(retained_fd)
            if complete != dict(expected) or _fd_identity(anchor_fd) != dict(expected):
                os.close(retained_fd)
                raise ConfineLaunchError(
                    f"immutable exec identity changed before broker start: {path}"
                )
            if identity in approved:
                os.close(retained_fd)
                if approved[identity][0] != path:
                    raise ConfineLaunchError(
                        "one immutable executable inode has ambiguous approved pathnames"
                    )
                continue
            approved[identity] = (path, retained_fd)
    except BaseException:
        for _path, descriptor in approved.values():
            os.close(descriptor)
        raise
    if not approved:
        raise ConfineLaunchError(
            "seccomp executable broker has no immutable approved targets"
        )
    return approved


def _reserve_exec_descriptors(
    approved: Mapping[Tuple[int, int], Tuple[str, int]],
    rule_fds: Sequence[int],
) -> Dict[Tuple[int, int], Tuple[str, int]]:
    """Move approved O_PATH descriptors into one protected contiguous table.

    The table is created before untrusted code exists and above every open
    descriptor.  F_DUPFD must therefore return each requested slot exactly;
    any concurrent allocation or resource-limit ambiguity fails closed.  The
    slots are inheritable across approved execs, while the seccomp filter
    brokers every primitive that could close, CLOEXEC-mark, or replace one.
    """
    try:
        open_fds = [
            int(name) for name in os.listdir("/proc/self/fd") if name.isdigit()
        ]
    except OSError as exc:
        raise ConfineLaunchError(
            f"cannot inventory descriptors for the atomic exec table: {exc}"
        ) from exc
    ordered = sorted(
        ((identity, path, descriptor)
         for identity, (path, descriptor) in approved.items()),
        key=lambda item: item[1],
    )
    soft_limit, _hard_limit = resource.getrlimit(resource.RLIMIT_NOFILE)
    start = max([127, *open_fds, *rule_fds]) + 32
    if soft_limit != resource.RLIM_INFINITY and start + len(ordered) >= soft_limit:
        raise ConfineLaunchError(
            "descriptor limit cannot hold the protected approved exec table"
        )
    reserved: Dict[Tuple[int, int], Tuple[str, int]] = {}
    try:
        for index, (identity, path, source_fd) in enumerate(ordered):
            expected_fd = start + index
            descriptor = fcntl.fcntl(source_fd, fcntl.F_DUPFD, expected_fd)
            if descriptor != expected_fd:
                os.close(descriptor)
                raise ConfineLaunchError(
                    "approved executable descriptor table was concurrently occupied"
                )
            os.set_inheritable(descriptor, True)
            info = os.fstat(descriptor)
            if (
                not stat_regular(info)
                or (int(info.st_dev), int(info.st_ino)) != identity
            ):
                os.close(descriptor)
                raise ConfineLaunchError(
                    f"approved executable descriptor identity drifted: {path}"
                )
            reserved[identity] = (path, descriptor)
    except BaseException:
        for _path, descriptor in reserved.values():
            try:
                os.close(descriptor)
            except OSError:
                pass
        raise
    finally:
        for _path, descriptor in approved.values():
            try:
                os.close(descriptor)
            except OSError:
                pass
    return reserved


def _approved_exec_identities(spec: Mapping[str, object]) -> set[Tuple[int, int]]:
    """Diagnostic view: immutable direct targets only (loaders/staging excluded)."""
    approved: set[Tuple[int, int]] = set()
    for rule in spec.get("rules", []):
        if "execute" not in rule["access"]:
            continue
        path = str(rule["path"])
        if _loader_basename(path):
            continue
        descriptor = -1
        try:
            descriptor, identity = _open_immutable_exec(path)
            if _fd_identity(descriptor) == dict(rule["identity"]):
                approved.add(identity)
        except (ConfineLaunchError, OSError):
            continue
        finally:
            if descriptor >= 0:
                os.close(descriptor)
    return approved


def _ptrace_result(
    request: int, pid: int, address: object = None, data: object = None
) -> int:
    libc = ctypes.CDLL(None, use_errno=True)
    libc.ptrace.restype = ctypes.c_long
    ctypes.set_errno(0)
    result = int(libc.ptrace(request, pid, address, data))
    if result == -1 and ctypes.get_errno() != 0:
        error = ctypes.get_errno()
        raise ConfineLaunchError(
            f"ptrace request {request:#x} failed for pid {pid}: "
            + errno.errorcode.get(error, str(error))
        )
    return result


def _get_tracee_regs(pid: int) -> object:
    machine = platform.machine().lower()
    if machine in ("x86_64", "amd64"):
        registers = _X86UserRegs()
        _ptrace_result(_PTRACE_GETREGS, pid, None, ctypes.byref(registers))
        return registers
    if machine in ("aarch64", "arm64"):
        registers = _Aarch64UserRegs()
        iovec = _Iovec(
            ctypes.cast(ctypes.pointer(registers), ctypes.c_void_p),
            ctypes.sizeof(registers),
        )
        _ptrace_result(
            _PTRACE_GETREGSET, pid, ctypes.c_void_p(_NT_PRSTATUS),
            ctypes.byref(iovec),
        )
        if iovec.iov_len < ctypes.sizeof(registers):
            raise ConfineLaunchError("ptrace returned a truncated aarch64 register set")
        return registers
    raise ConfineLaunchError("ptrace register ABI is unsupported")


def _set_tracee_regs(pid: int, registers: object) -> None:
    if isinstance(registers, _X86UserRegs):
        _ptrace_result(_PTRACE_SETREGS, pid, None, ctypes.byref(registers))
        return
    if isinstance(registers, _Aarch64UserRegs):
        iovec = _Iovec(
            ctypes.cast(ctypes.pointer(registers), ctypes.c_void_p),
            ctypes.sizeof(registers),
        )
        _ptrace_result(
            _PTRACE_SETREGSET, pid, ctypes.c_void_p(_NT_PRSTATUS),
            ctypes.byref(iovec),
        )
        return
    raise ConfineLaunchError("cannot write an unknown ptrace register ABI")


def _tracee_syscall(registers: object) -> Tuple[int, Tuple[int, ...]]:
    if isinstance(registers, _X86UserRegs):
        return int(registers.orig_rax), (
            int(registers.rdi), int(registers.rsi), int(registers.rdx),
            int(registers.r10), int(registers.r8), int(registers.r9),
        )
    if isinstance(registers, _Aarch64UserRegs):
        return int(registers.regs[8]), tuple(
            int(registers.regs[index]) for index in range(6)
        )
    raise ConfineLaunchError("cannot decode an unknown ptrace register ABI")


def _skip_tracee_syscall(pid: int, registers: object, result: int) -> None:
    """Skip one ptrace-stopped syscall and provide a bounded return value."""
    encoded = result & ((1 << 64) - 1)
    skipped = (1 << 64) - 1
    if isinstance(registers, _X86UserRegs):
        registers.orig_rax = skipped
        registers.rax = encoded
    elif isinstance(registers, _Aarch64UserRegs):
        registers.regs[8] = skipped
        registers.regs[0] = encoded
    else:
        raise ConfineLaunchError("cannot skip an unknown ptrace register ABI")
    _set_tracee_regs(pid, registers)
    if isinstance(registers, _Aarch64UserRegs):
        _ptrace_result(
            _PTRACE_SET_SYSCALL, pid, None, ctypes.c_void_p(skipped)
        )


def _deny_tracee_syscall(pid: int, registers: object) -> None:
    """Skip one ptrace-stopped syscall and return EACCES to the tracee."""
    _skip_tracee_syscall(pid, registers, -errno.EACCES)


def _rewrite_tracee_exec(
    pid: int, registers: object, descriptor: int, empty_path: int,
    exec_token: int,
) -> None:
    """Atomically bind an execve request to one approved descriptor inode."""
    _seccomp_nr, _execve_nr, execveat_nr = _seccomp_numbers()
    _nr, args = _tracee_syscall(registers)
    argv_pointer, env_pointer = args[1], args[2]
    if isinstance(registers, _X86UserRegs):
        registers.orig_rax = execveat_nr
        registers.rdi = descriptor
        registers.rsi = empty_path
        registers.rdx = argv_pointer
        registers.r10 = env_pointer
        registers.r8 = _AT_EMPTY_PATH
        registers.r9 = exec_token
    elif isinstance(registers, _Aarch64UserRegs):
        registers.regs[8] = execveat_nr
        registers.regs[0] = descriptor
        registers.regs[1] = empty_path
        registers.regs[2] = argv_pointer
        registers.regs[3] = env_pointer
        registers.regs[4] = _AT_EMPTY_PATH
        registers.regs[5] = exec_token
    else:
        raise ConfineLaunchError("cannot rewrite an unknown ptrace register ABI")
    _set_tracee_regs(pid, registers)
    if isinstance(registers, _Aarch64UserRegs):
        _ptrace_result(
            _PTRACE_SET_SYSCALL, pid, None, ctypes.c_void_p(execveat_nr)
        )


def _handle_seccomp_stop(
    pid: int,
    approved: Mapping[Tuple[int, int], Tuple[str, int]],
    exec_token: int,
) -> None:
    """Handle one seccomp ptrace stop without pathname continuation.

    A possibly-raced pathname is merely a selector into ``approved``.  Once
    selected, the rewrite names the retained immutable inode by a protected
    descriptor and points the kernel at a NUL in the read-only vDSO.  The
    filter protects that mapping from mprotect/unmap/fixed-map races and
    allows the rewritten execveat only with the broker's fresh 64-bit token.
    Mutable request storage is never the kernel execution pathname.
    """
    registers = _get_tracee_regs(pid)
    nr, args = _tracee_syscall(registers)
    _seccomp_nr, execve_nr, execveat_nr = _seccomp_numbers()
    scalar = _brokered_scalar_syscalls()
    slots = {descriptor for _path, descriptor in approved.values()}

    if nr == execve_nr:
        try:
            path, _request_nul = _read_tracee_string(pid, args[0])
        except ConfineLaunchError:
            _deny_tracee_syscall(pid, registers)
            return
        selected = next(
            ((identity, descriptor)
             for identity, (approved_path, descriptor) in approved.items()
             if approved_path == path),
            None,
        )
        if selected is None:
            alias_target = _immutable_exec_alias_target(path)
            if alias_target is not None:
                selected = next(
                    ((identity, descriptor)
                     for identity, (approved_path, descriptor) in approved.items()
                     if approved_path == alias_target),
                    None,
                )
        if selected is None:
            _deny_tracee_syscall(pid, registers)
            return
        identity, descriptor = selected
        try:
            info = os.fstat(descriptor)
        except OSError:
            _deny_tracee_syscall(pid, registers)
            return
        if (
            not stat_regular(info)
            or (int(info.st_dev), int(info.st_ino)) != identity
        ):
            raise ConfineLaunchError(
                f"protected executable descriptor identity changed: {path}"
            )
        try:
            empty_path = _immutable_tracee_nul(pid)
        except ConfineLaunchError:
            _deny_tracee_syscall(pid, registers)
            return
        _rewrite_tracee_exec(
            pid, registers, descriptor, empty_path, exec_token
        )
        return

    # Unsafe user-issued execveat forms reach ptrace.  The only execveat form
    # the BPF permits directly has exact AT_EMPTY_PATH and an occupied,
    # protected approved slot, so it is already inode-bound.
    if nr == execveat_nr:
        _deny_tracee_syscall(pid, registers)
        return

    if nr == scalar["clone"]:
        # Only CLONE_UNTRACED reaches this stop; ordinary clone remains native
        # and PTRACE_O_TRACECLONE attaches the child before it can execute.
        _deny_tracee_syscall(pid, registers)
        return
    if nr == scalar["clone3"]:
        # Pointer-backed struct clone_args cannot be inspected and continued
        # race-free, so clone3 is denied in every form. ENOSYS deliberately
        # activates libc's safe legacy-clone fallback, preserving ordinary
        # traced thread/process creation needed by the Pi runtime.
        _skip_tracee_syscall(pid, registers, -errno.ENOSYS)
        return

    # The kernel converts every descriptor/range argument below to a 32-bit
    # int/unsigned int.  Ptrace exposes the original 64-bit register, so the
    # broker must make the same conversion before comparing protected slots;
    # otherwise a nonzero high half would disagree with the syscall target.
    kernel_u32 = lambda value: value & 0xFFFFFFFF
    if nr == scalar["close"]:
        if kernel_u32(args[0]) in slots:
            _deny_tracee_syscall(pid, registers)
        return
    if nr == scalar["fcntl"]:
        if kernel_u32(args[0]) in slots:
            _deny_tracee_syscall(pid, registers)
        return
    if nr in {scalar.get("dup2", -1), scalar["dup3"]}:
        if kernel_u32(args[1]) in slots:
            _deny_tracee_syscall(pid, registers)
        return
    if nr == scalar["close_range"]:
        first = kernel_u32(args[0])
        last = kernel_u32(args[1])
        if slots and first <= max(slots) and last >= min(slots):
            # Emulate success without closing the protected table.  Returning
            # EACCES makes glibc/Python fall back to a million-close loop and
            # is unnecessary: all other inherited descriptors were already
            # owned by this tracee, while the immutable slots must survive.
            _skip_tracee_syscall(pid, registers, 0)
        return

    # Protect the kernel-created read-only mapping that supplies execveat's
    # immutable empty pathname.  mmap without MAP_FIXED cannot replace an
    # existing mapping and remains available to dynamic loaders/JITs; fixed
    # mappings and the obscure remap/userfault primitives fail closed.
    if nr == scalar["mmap"]:
        map_fixed = 0x10 | 0x100000  # MAP_FIXED | MAP_FIXED_NOREPLACE
        if args[3] & map_fixed:
            try:
                vdso = _tracee_vdso_range(pid)
            except ConfineLaunchError:
                _deny_tracee_syscall(pid, registers)
                return
            if _range_overlaps(args[0], args[1], vdso):
                _deny_tracee_syscall(pid, registers)
        return
    if nr in {
        scalar["mprotect"], scalar["pkey_mprotect"], scalar["munmap"],
        scalar["madvise"],
    }:
        try:
            vdso = _tracee_vdso_range(pid)
        except ConfineLaunchError:
            _deny_tracee_syscall(pid, registers)
            return
        if _range_overlaps(args[0], args[1], vdso):
            _deny_tracee_syscall(pid, registers)
        return
    if nr in {
        scalar["mremap"], scalar["shmat"], scalar["remap_file_pages"],
        scalar["userfaultfd"],
    }:
        _deny_tracee_syscall(pid, registers)
        return
    if nr in {
        scalar["io_uring_setup"], scalar["io_uring_enter"],
        scalar["io_uring_register"], scalar["ptrace"],
        scalar["process_vm_readv"], scalar["process_vm_writev"],
        scalar["pidfd_getfd"],
    }:
        _deny_tracee_syscall(pid, registers)
        return
    # A table/filter mismatch is a security boundary failure, never an allow.
    _deny_tracee_syscall(pid, registers)


def _tracee_lifecycle(pid: int) -> Tuple[bool, int | None]:
    """Return ``(live, process_group)`` for one identity-pinned ptracee."""
    try:
        text = Path("/proc", str(pid), "stat").read_text(
            encoding="ascii", errors="replace"
        )
        fields = text.rsplit(") ", 1)[1].split()
        if not fields or fields[0] == "Z":
            return False, None
        return True, int(fields[2])
    except (OSError, ValueError, IndexError):
        return False, None


def _ptrace_continue(pid: int, signal_number: int = 0) -> None:
    try:
        _ptrace_result(
            _PTRACE_CONT, pid, None, ctypes.c_void_p(signal_number)
        )
    except ConfineLaunchError:
        # A tracee can exit between waitpid and CONT.  ESRCH is the only
        # benign race; every other ptrace failure is a boundary failure.
        if ctypes.get_errno() != errno.ESRCH:
            raise


def _forward_broker_signal(
    target: int,
    traced: set[int],
    target_group_retained: bool,
    signum: int,
) -> None:
    """Forward one queued supervisor signal without a released PGID race.

    While the target is ptrace-retained, its PID also pins the process-group
    identity and group delivery is safe.  The target's PTRACE_EVENT_EXIT stop
    disables group forwarding *before* it may complete/release that identity;
    thereafter every delivery names only a still-ptrace-pinned tracee.  A
    foreign group that later reuses the numeric target/PGID is therefore never
    a signal target.
    """
    if target_group_retained:
        try:
            os.killpg(target, signum)
        except ProcessLookupError:
            pass
        return
    for pid in tuple(sorted(traced)):
        try:
            # Ptrace retains this exact process identity until its terminal
            # wait status is consumed and it is removed from ``traced``.
            os.kill(pid, signum)
        except ProcessLookupError:
            pass


def _broker_execs(
    target: int,
    approved: Mapping[Tuple[int, int], Tuple[str, int]],
    exec_token: int,
    termination: Dict[str, float],
    pending_signals: List[int],
) -> Tuple[int, bool]:
    """Supervise, terminate, and reap the complete ptrace descendant tree.

    Once the target exits, a traced descendant outside its process group is an
    escaped lifecycle: it can no longer contribute to the completed attempt and
    must not keep the broker's stdout/stderr descriptors (or the outer Popen)
    open. Same-group descendants remain under the outer timeout/termination
    policy. Every tracee PID remains identity-pinned by ptrace until its terminal wait
    status is consumed, so direct SIGKILL is reuse-safe even after ``setsid``.
    The broker drains every final ptrace wait status within a hard bound and
    reports the escape separately from the target's own exit status.
    """
    target_status: int | None = None
    escaped_descendant = False
    escaped_reap_deadline: float | None = None
    escaped_tracees: set[int] = set()
    traced = {target}
    # PTRACE_EVENT_EXIT is the last point at which target still pins its
    # numeric PID/PGID.  The flag is cleared at that stop before CONT permits
    # final exit, never after a terminal wait has already released identity.
    target_group_retained = True
    _ptrace_continue(target)
    while target_status is None or traced:
        while pending_signals:
            _forward_broker_signal(
                target, traced, target_group_retained, pending_signals.pop(0)
            )
        now = time.monotonic()
        if target_status is not None and traced:
            # Descendants that remain in the target's process group are still
            # inside the outer supervisor's ordinary TERM/INT/HUP→KILL scope.
            # Keep tracing them so pipe-holding same-group children trigger the
            # configured inactivity/runtime path.  A setsid descendant has a
            # different pgid and has escaped that scope: kill it by its ptrace-
            # pinned PID immediately, then drain its terminal wait status.
            escaped_live: list[int] = []
            for pid in sorted(traced):
                live, process_group = _tracee_lifecycle(pid)
                if live and process_group != target:
                    escaped_live.append(pid)
            if escaped_live:
                escaped_descendant = True
                escaped_tracees.update(escaped_live)
                if escaped_reap_deadline is None:
                    escaped_reap_deadline = now + _DESCENDANT_REAP_TIMEOUT
                for pid in escaped_live:
                    try:
                        os.kill(pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
            escaped_tracees.intersection_update(traced)
            if (
                escaped_tracees
                and escaped_reap_deadline is not None
                and now >= escaped_reap_deadline
            ):
                raise ConfineLaunchError(
                    "escaped ptrace descendants were not terminated and reaped "
                    f"within the {_DESCENDANT_REAP_TIMEOUT:.1f}s bound"
                )
        if termination and now >= termination["deadline"]:
            # Escalate every still-attached identity, covering a target that
            # already exited and any setsid transition racing the lifecycle
            # classifier.  These PIDs remain pinned by ptrace.
            for pid in tuple(traced):
                try:
                    os.kill(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            termination["deadline"] = now + 0.1
        try:
            waited, status = os.waitpid(
                -1, os.WNOHANG | os.WUNTRACED | _WAIT_WALL
            )
        except ChildProcessError:
            waited, status = 0, 0
        if waited == 0:
            # Ptrace fork/exec/seccomp stops are frequent and short-lived.
            # A 10 ms polling quantum made the fixed 40-attempt atomic race
            # regression approach its unchanged 20 s command bound under a
            # loaded full suite; 1 ms keeps signal/deadline checks bounded
            # while avoiding artificial per-stop latency.
            time.sleep(0.001)
            continue
        if os.WIFEXITED(status) or os.WIFSIGNALED(status):
            # A normal PTRACE_O_TRACEEXIT path cleared this before CONT.  Keep
            # the terminal-status fallback fail-safe as well: handlers only
            # queue signals, so none can numeric-killpg in the tiny interval
            # between waitpid returning and this assignment.
            if waited == target:
                target_group_retained = False
                target_status = status
            traced.discard(waited)
            continue
        if not os.WIFSTOPPED(status):
            continue

        traced.add(waited)
        stop_signal = os.WSTOPSIG(status)
        event = status >> 16
        if event in (
            _PTRACE_EVENT_FORK, _PTRACE_EVENT_VFORK, _PTRACE_EVENT_CLONE
        ):
            child = ctypes.c_ulonglong()
            _ptrace_result(
                _PTRACE_GETEVENTMSG, waited, None, ctypes.byref(child)
            )
            if child.value:
                traced.add(int(child.value))
            _ptrace_continue(waited)
            continue
        if event == _PTRACE_EVENT_EXEC:
            former = ctypes.c_ulonglong()
            _ptrace_result(
                _PTRACE_GETEVENTMSG, waited, None, ctypes.byref(former)
            )
            if former.value and int(former.value) != waited:
                traced.discard(int(former.value))
            traced.add(waited)
            _ptrace_continue(waited)
            continue
        if event == _PTRACE_EVENT_SECCOMP:
            _handle_seccomp_stop(waited, approved, exec_token)
            _ptrace_continue(waited)
            continue
        if event == _PTRACE_EVENT_EXIT:
            if waited == target:
                # Disable numeric group forwarding while ptrace still retains
                # target identity.  Once CONT allows final exit, queued/new
                # signals can reach only the exact identities in ``traced``.
                target_group_retained = False
            _ptrace_continue(waited)
            continue

        # Auto-attached clone children report SIGSTOP before running.  Suppress
        # that synthetic stop; preserve ordinary delivery stops (including a
        # genuine SIGTRAP) so ptrace does not alter application semantics.
        delivered = 0 if stop_signal == signal.SIGSTOP else stop_signal
        _ptrace_continue(waited, delivered)

    if target_status is None:
        raise ConfineLaunchError("ptrace broker lost the target exit status")
    return target_status, escaped_descendant


def _install_dedicated_subreaper() -> None:
    """Make this fresh executable broker the command lineage's subreaper.

    The outer launcher creates one new broker process for each attempt.  This
    check runs before the target fork and requires that broker to have no
    existing children, so every later direct/adopted child is proven to belong
    to this one command.  No baseline-child subtraction can confuse an
    unrelated child that forks and exits concurrently with the attempt.
    """
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(_PR_SET_CHILD_SUBREAPER, 1, 0, 0, 0) != 0:
        raise ConfineLaunchError(
            "cannot install the dedicated executable-broker subreaper: "
            + errno.errorcode.get(ctypes.get_errno(), str(ctypes.get_errno()))
        )
    broker_pid = os.getpid()
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            fields = entry.joinpath("stat").read_text(
                encoding="ascii", errors="replace"
            ).rsplit(") ", 1)[1].split()
            if len(fields) >= 2 and int(fields[1]) == broker_pid:
                raise ConfineLaunchError(
                    "dedicated executable broker has a pre-existing child"
                )
        except (OSError, ValueError, IndexError):
            continue


def _launch_with_exec_broker(
    spec: Mapping[str, object], rule_fds: Sequence[int],
    command: Sequence[str], supervision_fd: int | None = None,
    target_only_fds: Sequence[int] = (),
) -> Tuple[int, bool]:
    """Fork a confined session leader under a dedicated atomic exec broker.

    The trusted ancestor has no unrelated children, installs itself as the
    lineage's subreaper, opens every approved executable before untrusted code
    exists, and ptrace-supervises the complete descendant tree. Approved
    execve requests are rewritten to execveat on retained handles.
    PTRACE_O_EXITKILL and PDEATHSIG make broker death fail closed.
    """
    _install_dedicated_subreaper()
    approved = _reserve_exec_descriptors(
        _approved_exec_targets(spec, rule_fds), rule_fds
    )
    exec_token = int.from_bytes(os.urandom(8), "big") or 1
    broker_pid = os.getpid()
    target = os.fork()
    if target == 0:
        # The lifecycle report is broker-only.  Close it before any untrusted
        # code or descendant can inherit/spoof/hold the outer status pipe.
        if supervision_fd is not None:
            try:
                os.close(supervision_fd)
            except OSError:
                os._exit(1)
        try:
            libc = ctypes.CDLL(None, use_errno=True)
            if libc.prctl(_PR_SET_PDEATHSIG, signal.SIGKILL, 0, 0, 0) != 0:
                raise ConfineLaunchError("cannot install target parent-death signal")
            if os.getppid() != broker_pid:
                raise ConfineLaunchError("executable broker died before target setup")
            os.setsid()
            if libc.ptrace(_PTRACE_TRACEME, 0, None, None) != 0:
                raise ConfineLaunchError(
                    "target cannot enter ptrace supervision: "
                    + errno.errorcode.get(ctypes.get_errno(), str(ctypes.get_errno()))
                )
            os.kill(os.getpid(), signal.SIGSTOP)
            if os.getppid() != broker_pid:
                raise ConfineLaunchError("executable broker died during target setup")
            apply_confinement(spec, rule_fds)
            _install_home_environment(spec)
            _install_exec_trace_filter(
                [descriptor for _path, descriptor in approved.values()],
                exec_token,
            )
            os.execvpe(command[0], list(command), os.environ)
        except BaseException as exc:
            print(f"confine-launcher: {exc}", file=sys.stderr)
        os._exit(1)

    # Credential and similar launch descriptors belong only to the target.
    # The fork duplicates them into the child; close the broker copies before
    # its first wait/ptrace operation so a long-lived trusted broker never
    # retains model credential authority.
    for descriptor in target_only_fds:
        try:
            os.close(descriptor)
        except OSError:
            pass
    for descriptor in rule_fds:
        try:
            os.close(descriptor)
        except OSError:
            pass
    target_released = False
    try:
        waited, initial_status = os.waitpid(target, 0)
        if waited != target:
            raise ConfineLaunchError(
                "confined target did not enter its initial ptrace stop"
            )
        if os.WIFEXITED(initial_status) or os.WIFSIGNALED(initial_status):
            target_released = True
        if (
            not os.WIFSTOPPED(initial_status)
            or os.WSTOPSIG(initial_status) != signal.SIGSTOP
        ):
            raise ConfineLaunchError(
                "confined target did not enter its initial ptrace stop"
            )
        _ptrace_result(
            _PTRACE_SETOPTIONS, target, None, ctypes.c_void_p(_PTRACE_OPTIONS)
        )
    except BaseException:
        # Before the initial stop is continued the target cannot have created
        # descendants.  If waitpid already consumed a terminal status, its
        # numeric PID/PGID is released and must never be signaled.  Otherwise
        # it is an exact retained direct child, so kill/reap only that PID.
        if not target_released:
            try:
                os.kill(target, signal.SIGKILL)
            except ProcessLookupError:
                pass
            try:
                os.waitpid(target, 0)
            except ChildProcessError:
                pass
        for _path, descriptor in approved.values():
            os.close(descriptor)
        raise

    previous_handlers: Dict[int, object] = {}
    termination: Dict[str, float] = {}
    pending_signals: List[int] = []

    def forward(signum: int, _frame: object) -> None:
        # Python handlers run between broker bytecodes.  Queue only: delivery
        # occurs in the ptrace loop after lifecycle state has been updated, so
        # a handler can never killpg between a terminal wait and identity
        # release bookkeeping.
        pending_signals.append(signum)
        termination["deadline"] = time.monotonic() + 0.1

    for signum in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP, signal.SIGQUIT):
        previous_handlers[signum] = signal.getsignal(signum)
        signal.signal(signum, forward)
    try:
        return _broker_execs(
            target, approved, exec_token, termination, pending_signals
        )
    finally:
        for _path, descriptor in approved.values():
            os.close(descriptor)
        for signum, handler in previous_handlers.items():
            signal.signal(signum, handler)


def _install_home_environment(spec: Mapping[str, object]) -> None:
    """Install the sanitized private HOME/XDG environment from the spec."""
    env = spec.get("env")
    if not isinstance(env, dict):
        raise ConfineLaunchError("the confinement spec carries no sanitized env")
    for key, value in env.items():
        if not isinstance(key, str) or not isinstance(value, str):
            raise ConfineLaunchError("the sanitized environment must map str to str")
        os.environ[key] = value


def _write_supervision_status(descriptor: int | None, value: bytes) -> None:
    """Write exactly one trusted lifecycle byte, retrying an interrupted write."""
    if descriptor is None:
        return
    if value not in (_SUPERVISION_CLEAN, _SUPERVISION_ESCAPED, _SUPERVISION_FAILED):
        raise ConfineLaunchError("the supervision lifecycle status is invalid")
    while True:
        try:
            written = os.write(descriptor, value)
            break
        except InterruptedError:
            continue
        except OSError as exc:
            raise ConfineLaunchError(
                f"cannot report the confined descendant lifecycle: {exc}"
            ) from exc
    if written != 1:
        raise ConfineLaunchError("the confined descendant lifecycle report was short")


def main(argv: Sequence[str]) -> int:
    usage = (
        "usage: confine_launcher.py --spec-file <spec> --rule-fds <fd,fd,...> "
        "[--target-only-fds <fd,fd,...>] [--supervision-fd <fd>] -- <command...>"
    )
    if len(argv) < 6 or argv[0] != "--spec-file" or argv[2] != "--rule-fds":
        die(usage)
    spec_path = argv[1]
    fd_text = argv[3]
    index = 4
    target_only_fds: Tuple[int, ...] = ()
    if index < len(argv) and argv[index] == "--target-only-fds":
        if index + 1 >= len(argv):
            die(usage)
        target_fd_text = argv[index + 1]
        if not target_fd_text or any(
            not item.isdigit() for item in target_fd_text.split(",")
        ):
            die(usage)
        target_only_fds = tuple(int(item) for item in target_fd_text.split(","))
        index += 2
    supervision_fd: int | None = None
    if index < len(argv) and argv[index] == "--supervision-fd":
        if index + 1 >= len(argv) or not argv[index + 1].isdigit():
            die(usage)
        supervision_fd = int(argv[index + 1])
        index += 2
    if index >= len(argv) or argv[index] != "--":
        die(usage)
    command = list(argv[index + 1:])
    if not command:
        die("missing confined command")
    reported = False
    try:
        if not fd_text or any(not item.isdigit() for item in fd_text.split(",")):
            raise ConfineLaunchError("the inherited rule descriptor list is invalid")
        rule_fds = tuple(int(item) for item in fd_text.split(","))
        if (
            len(set(target_only_fds)) != len(target_only_fds)
            or any(fd < 3 for fd in target_only_fds)
            or set(target_only_fds) & set(rule_fds)
        ):
            raise ConfineLaunchError(
                "the target-only descriptor list is invalid or overlaps a rule descriptor"
            )
        for descriptor in target_only_fds:
            os.fstat(descriptor)
        if supervision_fd is not None:
            if supervision_fd in rule_fds or supervision_fd in target_only_fds:
                raise ConfineLaunchError(
                    "the supervision descriptor overlaps another inherited descriptor"
                )
            os.fstat(supervision_fd)
        spec = _read_spec(spec_path)
        _validate_lease(spec)
        status, escaped = _launch_with_exec_broker(
            spec, rule_fds, command, supervision_fd, target_only_fds
        )
        _write_supervision_status(
            supervision_fd,
            _SUPERVISION_ESCAPED if escaped else _SUPERVISION_CLEAN,
        )
        reported = True
        if os.WIFEXITED(status):
            return os.WEXITSTATUS(status)
        if os.WIFSIGNALED(status):
            # Forwardable handlers were restored by the broker; SIGKILL and
            # SIGSTOP are uncatchable and already have default disposition.
            os.kill(os.getpid(), os.WTERMSIG(status))
        return 1
    except (ConfineLaunchError, OSError) as exc:
        if not reported:
            try:
                _write_supervision_status(supervision_fd, _SUPERVISION_FAILED)
            except ConfineLaunchError:
                pass
        die(str(exc))
    finally:
        for descriptor in target_only_fds:
            try:
                os.close(descriptor)
            except OSError:
                pass
        if supervision_fd is not None:
            try:
                os.close(supervision_fd)
            except OSError:
                pass


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
