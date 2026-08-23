"""Static role-prompt set and campaign-bound prompt-set digest (Task 8).

ROLE-01 (§6) requires four distinct static role prompts (planner, developer,
tester, auditor) with no adaptive model roles; CTX-01/§20 require the static
role-prompt digest and the campaign-bound prompt-set digest to be bound at
campaign start and verified at every launch.  This module is the prompt-set
authority:

* the committed prompts under ``.factory/prompts/`` are the *only*
  role-prompt source (never an operator-claimed path);
* ``role_prompt_digest(role)`` is the SHA-256 of the committed prompt bytes;
* ``prompt_set_digest()`` deterministically binds the four role prompts
  (sorted by role) into one campaign-bound digest;
* ``factory-state/v1`` records ``role_prompt_digests`` (per role) and the
  planner binds the prompt set at campaign start, so a prompt change forces
  a fresh campaign binding instead of silently changing role behavior.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
import sys
from typing import Dict, Mapping, Sequence, Tuple

# The four static roles (§6); the prompt file per role.
ROLES: Tuple[str, ...] = ("planner", "developer", "tester", "auditor")

ROLE_PROMPT_FILES: Mapping[str, str] = {
    "planner": "planner.md",
    "developer": "developer.md",
    "tester": "tester.md",
    "auditor": "auditor.md",
}

PROMPTS_RELPATH = "prompts"
MAX_PROMPT_BYTES = 1024 * 1024
_BLOB_CHUNK = 65536


class PromptSetError(Exception):
    """A role prompt is missing, unsafe, oversized, or malformed."""


def prompts_directory() -> Path:
    """The canonical committed prompts directory (``.factory/prompts``)."""
    return Path(__file__).resolve().parents[1] / PROMPTS_RELPATH


def role_prompt_file(role: str) -> Path:
    """The canonical committed prompt file for one static role."""
    if role not in ROLES:
        raise PromptSetError(f"role must be one of {ROLES!r}, got {role!r}")
    return prompts_directory() / ROLE_PROMPT_FILES[role]


def read_role_prompt(role: str) -> bytes:
    """Bounded no-follow read of the committed role-prompt bytes.

    A symlink final component, a non-regular file, an oversized file, or a
    file that changes while being read fails closed; an operator-claimed
    path never qualifies.
    """
    path = role_prompt_file(role)
    flags = (
        os.O_RDONLY
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )
    try:
        descriptor = os.open(str(path), flags)
    except OSError as exc:
        raise PromptSetError(f"cannot open the role prompt {path}: {exc}") from exc
    try:
        info = os.fstat(descriptor)
        if not _is_regular(info):
            raise PromptSetError(f"the role prompt {path} is not a regular file")
        if info.st_size > MAX_PROMPT_BYTES:
            raise PromptSetError(
                f"the role prompt {path} exceeds the {MAX_PROMPT_BYTES}-byte bound"
            )
        before = (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)
        chunks: list[bytes] = []
        remaining = info.st_size + 1
        while remaining:
            chunk = os.read(descriptor, min(_BLOB_CHUNK, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        after = os.fstat(descriptor)
        if (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns) != before:
            raise PromptSetError(f"the role prompt {path} changed while being read")
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def _is_regular(info: object) -> bool:
    import stat

    return stat.S_ISREG(info.st_mode)


def role_prompt_digest(role: str) -> str:
    """SHA-256 of the exact committed role-prompt bytes."""
    return hashlib.sha256(read_role_prompt(role)).hexdigest()


def load_all_prompts() -> Dict[str, bytes]:
    """``{role: committed prompt bytes}`` for every static role."""
    return {role: read_role_prompt(role) for role in ROLES}


def prompt_set_digest() -> str:
    """The campaign-bound prompt-set digest over the four static role prompts.

    Deterministic: ``sha256(role0 \x00 prompt0 \x00 role1 \x00 prompt1 ...)``
    over roles sorted lexicographically, so the digest is a pure function of
    the committed prompt files.
    """
    digest = hashlib.sha256()
    for role in sorted(ROLES):
        digest.update(role.encode("utf-8"))
        digest.update(b"\x00")
        digest.update(read_role_prompt(role))
        digest.update(b"\x00")
    return digest.hexdigest()


def _cli(argv: Sequence[str]) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        prog="factory-promptset",
        description="Static role-prompt digests and the campaign-bound prompt-set digest.",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    p_role = sub.add_parser("role-digest", help="digest of one committed role prompt")
    p_role.add_argument("--role", required=True, choices=ROLES)
    p_set = sub.add_parser("prompt-set-digest", help="campaign-bound prompt-set digest")
    p_list = sub.add_parser("list", help="list role digests")
    args = parser.parse_args(argv)
    try:
        if args.command == "role-digest":
            print(role_prompt_digest(args.role))
        elif args.command == "prompt-set-digest":
            print(prompt_set_digest())
        else:
            for role in ROLES:
                print(f"{role}\t{role_prompt_digest(role)}")
    except PromptSetError as exc:
        print(f"factory-promptset: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(_cli(sys.argv[1:]))
