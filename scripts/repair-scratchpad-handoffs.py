#!/usr/bin/env python3
"""Fail-closed recovery repair for an appended Ralph scratchpad handoff."""

import argparse
import os
import re
import secrets
import stat
import sys

MAX_BYTES = 1024 * 1024
H1 = re.compile(r"^#[ \t]+\S.*?(?:\r?\n)?$")
SINGLE_HASH = re.compile(r"^#(?!#)")
MARKDOWN_HEADING = re.compile(r"^#{1,6}(?:[ \t]+|$)")


def fail(message: str) -> "None":
    raise SystemExit(f"scratchpad-repair: {message}")


def open_owned_directory(name: str, *, dir_fd: int | None = None) -> int:
    flags = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(name, flags, dir_fd=dir_fd)
    except OSError as exc:
        fail(f"unsafe directory {name}: {exc}")
    info = os.fstat(fd)
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
        os.close(fd)
        fail(f"directory is not owned by the current user: {name}")
    return fd


def repair(project_root: str, dry_run: bool) -> None:
    root_fd = open_owned_directory(project_root)
    ralph_fd = agent_fd = scratch_fd = None
    try:
        ralph_fd = open_owned_directory(".ralph", dir_fd=root_fd)
        agent_fd = open_owned_directory("agent", dir_fd=ralph_fd)
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        try:
            scratch_fd = os.open("scratchpad.md", flags, dir_fd=agent_fd)
        except OSError as exc:
            fail(f"cannot safely open scratchpad: {exc}")
        original = os.fstat(scratch_fd)
        if (not stat.S_ISREG(original.st_mode) or original.st_uid != os.getuid()
                or original.st_nlink != 1):
            fail("scratchpad is not an owned, singly linked regular file")
        if original.st_size == 0:
            fail("scratchpad has no content")
        if original.st_size > MAX_BYTES:
            fail(f"scratchpad exceeds the {MAX_BYTES}-byte recovery limit")
        raw = bytearray()
        while len(raw) <= MAX_BYTES:
            chunk = os.read(scratch_fd, min(65536, MAX_BYTES + 1 - len(raw)))
            if not chunk:
                break
            raw.extend(chunk)
        if len(raw) > MAX_BYTES or os.read(scratch_fd, 1):
            fail(f"scratchpad exceeds the {MAX_BYTES}-byte recovery limit")
        try:
            text = bytes(raw).decode("utf-8")
        except UnicodeError as exc:
            fail(f"scratchpad is not UTF-8: {exc}")
        if "\x00" in text:
            fail("scratchpad contains a NUL byte")

        lines = text.splitlines(keepends=True)
        headings: list[int] = []
        for index, line in enumerate(lines):
            if H1.match(line):
                headings.append(index)
            elif SINGLE_HASH.match(line):
                fail(f"malformed level-one heading at line {index + 1}")
        if not headings:
            fail("scratchpad contains no level-one handoff document")

        # Every identified document must carry real handoff content. This avoids
        # manufacturing a valid-looking current document from truncated output.
        for number, start in enumerate(headings):
            stop = headings[number + 1] if number + 1 < len(headings) else len(lines)
            meaningful = False
            for line in lines[start + 1:stop]:
                body = line.rstrip("\r\n")
                if body.strip() and not MARKDOWN_HEADING.match(body):
                    meaningful = True
                    break
            if not meaningful:
                fail(f"level-one handoff at line {start + 1} has no content")

        if len(headings) == 1:
            print("scratchpad-repair: one current handoff already present; no repair needed")
            return

        # Adding one '#' is a lossless demotion of every older title. The last
        # level-one title remains the current handoff, matching append order.
        for index in headings[:-1]:
            lines[index] = "#" + lines[index]
        repaired = "".join(lines).encode("utf-8")
        if len(repaired) > MAX_BYTES:
            fail(f"repaired scratchpad exceeds the {MAX_BYTES}-byte recovery limit")
        if dry_run:
            print(f"scratchpad-repair: would archive {len(headings) - 1} earlier handoff(s)")
            return

        current = os.stat("scratchpad.md", dir_fd=agent_fd, follow_symlinks=False)
        if (current.st_dev, current.st_ino, current.st_size, current.st_mtime_ns) != (
                original.st_dev, original.st_ino, original.st_size, original.st_mtime_ns):
            fail("scratchpad changed while recovery repair was in progress")
        temporary = f".scratchpad.md.repair-{secrets.token_hex(16)}"
        out_fd = None
        try:
            out_fd = os.open(
                temporary,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                stat.S_IMODE(original.st_mode),
                dir_fd=agent_fd,
            )
            view = memoryview(repaired)
            while view:
                written = os.write(out_fd, view)
                if written <= 0:
                    fail("short write while repairing scratchpad")
                view = view[written:]
            os.fsync(out_fd)
            os.close(out_fd)
            out_fd = None
            os.replace(temporary, "scratchpad.md", src_dir_fd=agent_fd, dst_dir_fd=agent_fd)
            os.fsync(agent_fd)
        except BaseException:
            if out_fd is not None:
                os.close(out_fd)
            try:
                os.unlink(temporary, dir_fd=agent_fd)
            except FileNotFoundError:
                pass
            raise
        print(f"scratchpad-repair: archived {len(headings) - 1} earlier handoff(s); latest remains current")
    finally:
        if scratch_fd is not None:
            os.close(scratch_fd)
        for fd in (agent_fd, ralph_fd, root_fd):
            if fd is not None:
                os.close(fd)


def main() -> None:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--project-root", required=True)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    repair(args.project_root, args.dry_run)


if __name__ == "__main__":
    main()
