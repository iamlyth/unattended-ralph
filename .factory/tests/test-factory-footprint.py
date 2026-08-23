#!/usr/bin/env python3
"""Hidden harness-footprint inventory conformance suite (HIDE-01, §3; Task 13).

This test lives under the hidden ``.factory/tests/`` namespace because the
specification (HIDE-01, §3) keeps harness-only tests out of the adopting
product's visible test tree.  It is the deterministic verification for Task 13
and drives the hidden authority ``.factory/loop/footprint.py``:

* every harness-installed/generated path stays inside ``.factory/``, the
  ignored mode-0700 ``.factory-state/``, or the ``.pi/`` required role
  definitions — never in the product source, test, packaging, or build
  trees, and never in the product root;
* tracked runtime state (``.factory-state/**``) and tracked symlink/gitlink
  harness entries fail closed;
* absolute, traversal (``..``/``.``/empty), backslash, and control-character
  harness paths are rejected;
* case-fold (``.Factory/``) and Unicode-normalization (``．factory/``, an
  NFKC aliasing) namespace escapes are rejected;
* symlink escapes inside the hidden namespaces and product-side symlinks
  into the harness are rejected; a harness inode hardlinked into a product
  path fails closed;
* legacy root harness files (``PROMPT.md``, ``IMPLEMENTATION_PLAN.md``,
  ``factory.toml``, ...) never reappear — tracked or on disk;
* product source/test/packaging/build discovery excludes the hidden
  namespaces by construction, and a product install prefix never receives
  hidden-namespace content;
* an external-prefix harness install carries exactly the hidden-namespace
  manifest, never product files;
* deleting the hidden namespaces removes the harness without touching a
  single product byte.

The suite builds small committed fixture repositories under its own
``TMPDIR`` using the pinned Git boundary; it never writes to the live
repository, never touches ``.ralph/``, and creates no branch/worktree/stash.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
LOOP = ROOT / ".factory" / "loop"
FOOTPRINT_SCRIPT = LOOP / "footprint.py"

sys.path.insert(0, str(LOOP))
import footprint  # noqa: E402
import gitutil  # noqa: E402


def _run(root: Path, *argv: str) -> str:
    result = gitutil.git_run(["-C", str(root), *argv], timeout=60)
    if result.returncode != 0:
        raise AssertionError(
            f"git {' '.join(argv)} failed: {result.stderr.strip()}"
        )
    return result.stdout


def make_repo(root: Path, files: dict) -> None:
    """Create a committed fixture repository with ``{relpath: content}``."""
    for rel, content in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            path.write_bytes(content)
        else:
            path.write_text(content, encoding="utf-8")
    _run(root, "init", "-q")
    _run(root, "config", "user.email", "factory@test")
    _run(root, "config", "user.name", "factory")
    _run(root, "add", "-A")
    _run(root, "commit", "-qm", "init")


def lstat_simulating(paths, **mutations):
    """``os.lstat`` substitute altering stat fields for the exact paths.

    Simulates conditions a test cannot create for real (an entry on a
    different device/mount, a differently owned ``.factory-state``) while
    keeping every other path's real ``lstat`` result.
    """
    real = os.lstat
    wanted = {str(p) for p in paths}
    keys = ("st_mode", "st_ino", "st_dev", "st_nlink", "st_uid",
            "st_gid", "st_size", "st_atime", "st_mtime", "st_ctime")

    def fake(path):
        info = real(path)
        if os.fspath(path) not in wanted:
            return info
        fields = [info.st_mode, info.st_ino, info.st_dev, info.st_nlink,
                  info.st_uid, info.st_gid, info.st_size, info.st_atime,
                  info.st_mtime, info.st_ctime]
        for key, value in mutations.items():
            fields[keys.index(key)] = value
        return os.stat_result(tuple(fields))

    return fake


def harness_files(extra: dict | None = None) -> dict:
    """A minimal harness installation as tracked fixture content."""
    files = {
        ".factory/config.toml": "[project]\nspec = 'docs/SPEC.md'\n",
        ".factory/loop/plan_parser.py": "VERSION = 1\n",
        ".factory/prompts/plan.md": "# planner\n",
        ".pi/subagents.json": "{}",
        ".pi/agents/reviewer.md": "---\ntools: []\n---\n",
    }
    if extra:
        files.update(extra)
    return files


def product_files() -> dict:
    """A minimal adopting-product tree (source/test/packaging/build)."""
    return {
        "CMakeLists.txt": "cmake_minimum_required(VERSION 3.16)\n",
        "README.md": "# sample product\n",
        "docs/SPEC.md": "# Product specification\n",
        "src/main.c": "int main(void) { return 0; }\n",
        "tests/test_main.c": "#include <assert.h>\n",
        "packaging/control": "Package: sample\n",
        ".gitignore": ".factory-state/\n.ralph/\n__pycache__/\n",
    }


def harnessed_product_repo(root: Path) -> None:
    """A fixture adopting product with the harness installed and committed."""
    make_repo(root, {**product_files(), **harness_files()})


class FootprintTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(
            prefix="factory-footprint-", dir="/tmp"
        )
        self.root = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def repo(self, files: dict) -> Path:
        root = self.root / "repo"
        make_repo(root, files)
        return root


class TestPathSafety(FootprintTestCase):
    def test_safe_relpath_accepts_normal(self) -> None:
        for path in (".factory/loop/plan_parser.py",
                     ".factory-state/factory-loop.json",
                     ".pi/subagents.json",
                     "src/main.c"):
            self.assertEqual(footprint.safe_relpath(path), path)

    def test_safe_relpath_rejects_absolute(self) -> None:
        for path in ("/etc/passwd", "/.factory/loop", "//factory/x"):
            self.assertIsNone(footprint.safe_relpath(path))

    def test_safe_relpath_rejects_traversal(self) -> None:
        for path in ("../.factory/x", ".factory/../../etc/x", ".factory/./x",
                     ".factory//x", ".factory/../", "", ".."):
            self.assertIsNone(footprint.safe_relpath(path))

    def test_safe_relpath_rejects_control_and_backslash(self) -> None:
        for path in (".factory/loop/plan\\parser.py",
                     ".factory/\x00state", ".factory/state\n", ".factory/\x1b"):
            self.assertIsNone(footprint.safe_relpath(path))

    def test_classify_path(self) -> None:
        self.assertEqual(footprint.classify_path(".factory/loop/x.py"),
                         "harness")
        self.assertEqual(footprint.classify_path(".factory-state/factory-loop.json"),
                         "harness")
        self.assertEqual(footprint.classify_path(".pi/agents/reviewer.md"),
                         "harness")
        self.assertEqual(footprint.classify_path(".ralph/agent/scratchpad.md"),
                         "legacy")
        self.assertEqual(footprint.classify_path("src/main.c"), "product")

    def test_namespace_escape_detection(self) -> None:
        self.assertIsNone(footprint._namespace_escape(".factory"))
        self.assertEqual(footprint._namespace_escape(".Factory"), ".factory")
        self.assertEqual(footprint._namespace_escape(".FACTORY-STATE"),
                         ".factory-state")
        self.assertEqual(footprint._namespace_escape(".PI"), ".pi")
        # Fullwidth characters NFKC-normalize to the ASCII namespace.
        self.assertEqual(footprint._namespace_escape("\uff0efactory"), ".factory")
        self.assertEqual(footprint._namespace_escape(".f\uff41ctory"), ".factory")
        self.assertIsNone(footprint._namespace_escape("docs"))

    def test_namespace_escape_trailing_dot_and_space(self) -> None:
        # Windows/macOS silently strip trailing dots and spaces when they
        # create or read directory entries: ``.factory.`` and ``.factory ``
        # alias ``.factory`` there, so every trailing-dot/space variant must
        # fail closed, in any case/normalization combination.
        for alias in (".factory.", ".factory..", ".factory ",
                      ".factory. ", ".FACTORY. ", ".factory-state.",
                      ".factory-state ", ".pi.", ".pi ", ".PI. "):
            self.assertEqual(
                footprint._namespace_escape(alias),
                alias.rstrip(". ").casefold(),
                f"{alias!r} must alias its stripped canonical namespace",
            )
        self.assertIsNone(footprint._namespace_escape("docs"))
        self.assertIsNone(footprint._namespace_escape("docs."))
        self.assertIsNone(footprint._namespace_escape("data "))
        self.assertIsNone(footprint._namespace_escape(".factory"))


class TestTrackedInventory(FootprintTestCase):
    def test_tracked_harness_confined(self) -> None:
        repo = self.repo(harness_files())
        self.assertTrue(footprint.inventory(repo).ok)

    def test_tracked_factory_state_fails(self) -> None:
        repo = self.repo(
            {**harness_files(), ".factory-state/factory-loop.json": "{}"}
        )
        report = footprint.inventory(repo)
        self.assertFalse(report.ok)
        self.assertTrue(
            any("runtime state is tracked" in error for error in report.errors)
        )

    def test_tracked_pi_role_defs_accepted(self) -> None:
        repo = self.repo(harness_files())
        report = footprint.inventory(repo)
        self.assertTrue(report.ok)

    def test_tracked_pi_runtime_output_fails(self) -> None:
        repo = self.repo({**harness_files(), ".pi/output/transcript.log": "x"})
        report = footprint.inventory(repo)
        self.assertFalse(report.ok)
        self.assertTrue(
            any(".pi/ must contain only required role definitions" in error
                for error in report.errors)
        )

    def test_tracked_ralph_growth_fails(self) -> None:
        repo = self.repo(
            {**harness_files(), ".ralph/agent/tasks.jsonl": "[]"}
        )
        report = footprint.inventory(repo)
        self.assertFalse(report.ok)
        self.assertTrue(
            any("legacy namespace gains a new tracked entry" in error
                for error in report.errors)
        )

    def test_tracked_root_forbidden_file_fails(self) -> None:
        repo = self.repo({**harness_files(), "PROMPT.md": "prompt"})
        report = footprint.inventory(repo)
        self.assertFalse(report.ok)
        self.assertTrue(
            any("legacy root harness file is tracked" in error
                for error in report.errors)
        )

    def test_tracked_root_harness_filename_marker_fails(self) -> None:
        # A harness config-artifact basename at the product root (tracked)
        # is a leaked marker even when it is not one of the legacy root
        # factory files: ``factory-loop.json`` must fail closed.
        repo = self.repo({**harness_files(), "factory-loop.json": "{}"})
        report = footprint.inventory(repo)
        self.assertFalse(report.ok)
        self.assertTrue(
            any("harness config artifact is tracked at the product root"
                in error for error in report.errors)
        )

    def test_tracked_trailing_dot_namespace_escape_fails(self) -> None:
        # ``.factory.`` is created as ``.factory`` on Windows/macOS; a
        # tracked path under such a name must fail closed.
        repo = self.repo({**harness_files(), ".factory./config.toml": "x"})
        report = footprint.inventory(repo)
        self.assertFalse(report.ok)
        self.assertTrue(
            any("case/Unicode namespace escape" in error
                for error in report.errors)
        )

    def test_tracked_trailing_space_namespace_escape_fails(self) -> None:
        # ``.pi `` (a trailing space) aliases ``.pi`` on dot/space-trimming
        # filesystems; the nested-segment scan must catch it.
        repo = self.repo({**harness_files(), ".pi /roles.md": "x"})
        report = footprint.inventory(repo)
        self.assertFalse(report.ok)
        self.assertTrue(
            any("case/Unicode namespace escape" in error
                for error in report.errors)
        )

    def test_tracked_case_escape_fails(self) -> None:
        repo = self.repo({**harness_files(), ".Factory/config.toml": "x"})
        report = footprint.inventory(repo)
        self.assertFalse(report.ok)
        self.assertTrue(
            any("case/Unicode namespace escape" in error
                for error in report.errors)
        )

    def test_tracked_unicode_nfkc_escape_fails(self) -> None:
        repo = self.repo({**harness_files(), "\uff0efactory/x.py": "x"})
        report = footprint.inventory(repo)
        self.assertFalse(report.ok)
        self.assertTrue(
            any("case/Unicode namespace escape" in error
                for error in report.errors)
        )

    def test_tracked_product_tree_contamination_fails(self) -> None:
        repo = self.repo(
            {**product_files(), "src/factory-loop.json": "{}",
             "packaging/.factory/manifest": "x"}
        )
        report = footprint.inventory(repo)
        self.assertFalse(report.ok)
        self.assertTrue(
            any("harness namespace inside product tree" in error
                for error in report.errors)
        )
        self.assertTrue(
            any("harness config artifact inside product tree" in error
                for error in report.errors)
        )

    def test_tracked_harness_symlink_mode_fails(self) -> None:
        root = self.root / "repo"
        root.mkdir()
        for rel, content in harness_files().items():
            path = root / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        os.symlink("../../src/main.c", root / ".factory" / "lnk")
        _run(root, "init", "-q")
        _run(root, "config", "user.email", "factory@test")
        _run(root, "config", "user.name", "factory")
        _run(root, "add", "-A")
        _run(root, "commit", "-qm", "init")
        report = footprint.inventory(root)
        self.assertFalse(report.ok)
        self.assertTrue(
            any("tracked symlink/gitlink" in error for error in report.errors)
        )

    def test_tracked_modes_space_and_tab_safe(self) -> None:
        # ``tracked_modes`` must parse the NUL/TAB ``git ls-files -s -z``
        # records so filenames with spaces or tabs never alias each other
        # or lose a suffix.
        files = dict(harness_files())
        files[".factory/loop/has space.py"] = "x = 1\n"
        files[".factory/loop/has\ttab.py"] = "x = 2\n"
        files["src/has space.c"] = "int x;\n"
        repo = self.repo(files)
        modes = footprint.tracked_modes(repo)
        self.assertEqual(modes[".factory/loop/has space.py"], "100644")
        self.assertEqual(modes[".factory/loop/has\ttab.py"], "100644")
        self.assertEqual(modes["src/has space.c"], "100644")
        # A whitespace-splitting parser would corrupt the tab path into the
        # truncated key ``.factory/loop/has``; it must not exist.
        self.assertNotIn(".factory/loop/has", modes)

    def test_tracked_space_names_inventory_clean(self) -> None:
        # Space-containing harness/product names are safe relpaths and must
        # inventory clean; the control-character rule rejects TABs as a
        # separate fail-closed class, so this fixture avoids them.
        files = dict(harness_files())
        files[".factory/loop/has space.py"] = "x = 1\n"
        files["src/has space.c"] = "int x;\n"
        repo = self.repo(files)
        self.assertTrue(footprint.inventory(repo).ok)

    def test_tracked_symlink_mode_space_and_tab_safe(self) -> None:
        # A tracked symlink whose *name* contains a space or a tab is still
        # detected with the exact path: the mode parser is tab-safe (M1) and
        # the symlink/gitlink mode check applies to every ``.factory`` entry
        # (M2).
        root = self.root / "repo"
        root.mkdir()
        for rel, content in harness_files().items():
            path = root / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        os.symlink("../../src/main.c", root / ".factory" / "lnk name.txt")
        os.symlink("../../src/main.c", root / ".factory" / "lnk\ttab.txt")
        _run(root, "init", "-q")
        _run(root, "config", "user.email", "factory@test")
        _run(root, "config", "user.name", "factory")
        _run(root, "add", "-A")
        _run(root, "commit", "-qm", "init")
        modes = footprint.tracked_modes(root)
        self.assertEqual(modes[".factory/lnk name.txt"], "120000")
        self.assertEqual(modes[".factory/lnk\ttab.txt"], "120000")
        report = footprint.inventory(root)
        self.assertFalse(report.ok)
        symlink_errors = [e for e in report.errors
                          if "tracked symlink/gitlink" in e]
        self.assertTrue(any("lnk name.txt" in e for e in symlink_errors))
        # The tab-named symlink fails closed too (the control-character rule
        # rejects it before the mode check runs; the error message uses the
        # backslash-escaped repr of the tab).
        self.assertTrue(any("lnk\\ttab.txt" in e for e in report.errors))

    def test_tracked_pi_symlink_mode_fails(self) -> None:
        # The symlink/gitlink mode rejection applies to the ``.pi`` role
        # namespace too, not only ``.factory``.
        root = self.root / "repo"
        root.mkdir()
        (root / ".factory").mkdir()
        (root / ".factory" / "config.toml").write_text("[project]\n")
        (root / ".pi" / "agents").mkdir(parents=True)
        (root / ".pi" / "agents" / "reviewer.md").write_text("---\n")
        os.symlink("subagents.json", root / ".pi" / "subagents.json")
        _run(root, "init", "-q")
        _run(root, "config", "user.email", "factory@test")
        _run(root, "config", "user.name", "factory")
        _run(root, "add", "-A")
        _run(root, "commit", "-qm", "init")
        report = footprint.inventory(root)
        self.assertTrue(
            any("tracked symlink/gitlink" in e and ".pi/subagents.json" in e
                for e in report.errors)
        )

    def test_tracked_ralph_symlink_mode_fails(self) -> None:
        # The symlink/gitlink mode check also covers the allowlisted legacy
        # ``.ralph/`` entry (M2: apply mode to .factory/.pi/.ralph all).
        root = self.root / "repo"
        root.mkdir()
        (root / ".factory" / "config.toml").parent.mkdir(parents=True)
        (root / ".factory" / "config.toml").write_text("[project]\n")
        (root / ".ralph" / "agent").mkdir(parents=True)
        os.symlink("scratchpad.md",
                   root / ".ralph" / "agent" / "scratchpad.md")
        _run(root, "init", "-q")
        _run(root, "config", "user.email", "factory@test")
        _run(root, "config", "user.name", "factory")
        _run(root, "add", "-A")
        _run(root, "commit", "-qm", "init")
        report = footprint.inventory(root)
        self.assertTrue(
            any("tracked symlink/gitlink" in e and ".ralph" in e
                for e in report.errors)
        )

    def test_tracked_nested_case_escape_fails(self) -> None:
        # A case-folded namespace variant in a *non-first* product segment
        # (``src/.Factory/``) aliases the namespace on case-insensitive
        # filesystems and must fail closed.
        repo = self.repo({**harness_files(), "src/.Factory/impl.py": "x\n"})
        report = footprint.inventory(repo)
        self.assertTrue(
            any("case/Unicode namespace escape in product path" in e
                for e in report.errors)
        )

    def test_tracked_nested_nfkc_escape_fails(self) -> None:
        # NFKC fullwidth aliasing in a deep product segment.
        repo = self.repo(
            {**harness_files(), "tests/sub/\uff0epi/roles.py": "x\n"}
        )
        report = footprint.inventory(repo)
        self.assertTrue(
            any("case/Unicode namespace escape in product path" in e
                for e in report.errors)
        )

    def test_tracked_nested_root_marker_fails(self) -> None:
        # A root harness marker basename at any depth (even outside the
        # declared product trees, e.g. under ``docs/``) is a leaked harness
        # artifact, tracked or on disk.
        repo = self.repo({**harness_files(), "docs/PROMPT.md": "x"})
        report = footprint.inventory(repo)
        self.assertTrue(
            any("legacy root harness file leaked into product tree" in e
                for e in report.errors)
        )

    def test_tracked_nested_harness_marker_in_product_tree_fails(self) -> None:
        repo = self.repo(
            {**harness_files(), "src/tools/factory-loop.json": "{}"}
        )
        report = footprint.inventory(repo)
        self.assertTrue(
            any("harness config artifact inside product tree" in e
                for e in report.errors)
        )

    def test_tracked_nested_hidden_namespace_in_product_fails(self) -> None:
        repo = self.repo({**harness_files(), "packaging/.factory/x": "y"})
        report = footprint.inventory(repo)
        self.assertTrue(
            any("harness namespace inside product tree" in e
                for e in report.errors)
        )


class TestOnDiskInventory(FootprintTestCase):
    def test_factory_state_mode_0700_ok(self) -> None:
        repo = self.root / "repo"
        make_repo(repo, {**product_files(), **harness_files()})
        (repo / ".factory-state").mkdir(mode=0o700)
        (repo / ".factory-state" / "factory-loop.json").write_text("{}")
        report = footprint.inventory(repo)
        self.assertTrue(report.ok)

    def test_factory_state_mode_group_accessible_fails(self) -> None:
        repo = self.root / "repo"
        make_repo(repo, {**product_files(), **harness_files()})
        (repo / ".factory-state").mkdir(mode=0o755)
        (repo / ".factory-state" / "factory-loop.json").write_text("{}")
        report = footprint.inventory(repo)
        self.assertFalse(report.ok)
        self.assertTrue(
            any(".factory-state must be created mode 0700" in error
                for error in report.errors)
        )

    def test_factory_state_symlink_fails(self) -> None:
        repo = self.root / "repo"
        make_repo(repo, {**product_files(), **harness_files()})
        target = self.root / "outside"
        target.mkdir()
        os.symlink(target, repo / ".factory-state")
        report = footprint.inventory(repo)
        self.assertFalse(report.ok)
        self.assertTrue(
            any("hidden namespace is a symlink" in error
                for error in report.errors)
        )

    def test_harness_symlink_escape_fails(self) -> None:
        repo = self.root / "repo"
        make_repo(repo, {**product_files(), **harness_files()})
        os.symlink("../../src/main.c", repo / ".factory" / "escape")
        report = footprint.inventory(repo)
        self.assertFalse(report.ok)
        self.assertTrue(
            any("symlink inside hidden namespace" in error
                for error in report.errors)
        )

    def test_product_symlink_into_harness_fails(self) -> None:
        repo = self.root / "repo"
        make_repo(repo, {**product_files(), **harness_files()})
        os.symlink("../.factory/loop/plan_parser.py",
                   repo / "src" / "alias.py")
        report = footprint.inventory(repo)
        self.assertFalse(report.ok)
        self.assertTrue(
            any("product path is a symlink into a hidden namespace" in error
                for error in report.errors)
        )

    def test_hardlink_alias_fails(self) -> None:
        repo = self.root / "repo"
        make_repo(repo, {**product_files(), **harness_files()})
        os.link(repo / ".factory" / "loop" / "plan_parser.py",
                repo / "src" / "alias.py")
        report = footprint.inventory(repo)
        self.assertFalse(report.ok)
        self.assertTrue(
            any("hardlink alias" in error for error in report.errors)
        )

    def test_root_forbidden_file_present_fails(self) -> None:
        repo = self.root / "repo"
        make_repo(repo, {**product_files(), **harness_files()})
        (repo / "IMPLEMENTATION_PLAN.md").write_text("leaked")
        report = footprint.inventory(repo)
        self.assertFalse(report.ok)
        self.assertTrue(
            any("legacy root harness file is present" in error
                for error in report.errors)
        )

    def test_root_harness_filename_marker_present_fails(self) -> None:
        # An on-disk harness config-artifact basename at the product root
        # (``factory-loop.json``) fails closed even though it is not one of
        # the legacy root factory files.
        repo = self.root / "repo"
        make_repo(repo, {**product_files(), **harness_files()})
        (repo / "factory-loop.json").write_text("{}")
        report = footprint.inventory(repo)
        self.assertFalse(report.ok)
        self.assertTrue(
            any("harness config artifact is present at the product root"
                in error for error in report.errors)
        )

    def test_on_disk_trailing_dot_namespace_escape_fails(self) -> None:
        # ``.factory.`` at the repository root aliases ``.factory`` on
        # dot/space-trimming filesystems and must fail closed on disk.
        repo = self.root / "repo"
        make_repo(repo, {**product_files(), **harness_files()})
        (repo / ".factory.").mkdir()
        (repo / ".factory." / "config.toml").write_text("x")
        report = footprint.inventory(repo)
        self.assertFalse(report.ok)
        self.assertTrue(
            any("case/Unicode namespace escape is present" in error
                for error in report.errors)
        )

    def test_on_disk_nested_trailing_space_escape_fails(self) -> None:
        # A trailing-space alias in a deep product segment (``src/.pi ``)
        # must fail closed on disk.
        repo = self.root / "repo"
        make_repo(repo, {**product_files(), **harness_files()})
        (repo / "src" / ".pi ").mkdir(parents=True)
        (repo / "src" / ".pi " / "roles.md").write_text("x")
        report = footprint.inventory(repo)
        self.assertFalse(report.ok)
        self.assertTrue(
            any("case/Unicode namespace escape is present in product" in error
                for error in report.errors)
        )

    def test_top_level_case_escape_present_fails(self) -> None:
        repo = self.root / "repo"
        make_repo(repo, {**product_files(), **harness_files()})
        (repo / ".Factory").mkdir()
        report = footprint.inventory(repo)
        self.assertFalse(report.ok)
        self.assertTrue(
            any("case/Unicode namespace escape is present" in error
                for error in report.errors)
        )

    def test_top_level_nfkc_escape_present_fails(self) -> None:
        repo = self.root / "repo"
        make_repo(repo, {**product_files(), **harness_files()})
        (repo / "\uff0efactory").mkdir()
        report = footprint.inventory(repo)
        self.assertFalse(report.ok)
        self.assertTrue(
            any("case/Unicode namespace escape is present" in error
                for error in report.errors)
        )

    def test_factory_state_mode_0600_fails(self) -> None:
        # The permission check requires *exactly* 0700: a mode without the
        # user execute bit (0600) is not 0700 and must fail.
        repo = self.root / "repo"
        make_repo(repo, {**product_files(), **harness_files()})
        # An empty 0600 directory (owner mode without the execute bit) is
        # not exactly 0700; the mode check must fail closed.  (A non-empty
        # 0600 directory is unreadable and fails the walk separately.)
        (repo / ".factory-state").mkdir(mode=0o600)
        report = footprint.inventory(repo)
        self.assertFalse(report.ok)
        self.assertTrue(
            any(".factory-state must be created mode 0700" in error
                for error in report.errors)
        )

    def test_factory_state_wrong_owner_fails(self) -> None:
        # The ownership check is unconditional (not skipped for root): a
        # simulated foreign-owned runtime directory must fail closed with a
        # clear message.
        repo = self.root / "repo"
        make_repo(repo, {**product_files(), **harness_files()})
        (repo / ".factory-state").mkdir(mode=0o700)
        (repo / ".factory-state" / "factory-loop.jsonl").write_text("{}")
        target = repo / ".factory-state"
        with mock.patch("os.lstat", side_effect=lstat_simulating(
                [target], st_uid=os.getuid() + 1)):
            report = footprint.inventory(repo)
        self.assertFalse(report.ok)
        self.assertTrue(
            any("must be owned by the invoking user" in error
                for error in report.errors)
        )

    def test_on_disk_ralph_symlink_fails(self) -> None:
        repo = self.root / "repo"
        make_repo(repo, {**product_files(), **harness_files()})
        (repo / ".ralph" / "agent").mkdir(parents=True)
        os.symlink("../../src/main.c", repo / ".ralph" / "agent" / "alias")
        report = footprint.inventory(repo)
        self.assertFalse(report.ok)
        self.assertTrue(
            any("symlink inside legacy namespace" in error
                for error in report.errors)
        )

    def test_on_disk_ralph_ignored_runtime_allowed(self) -> None:
        # git-ignored legacy runtime output under .ralph/ is legitimate
        # recovery history, not new harness content.
        repo = self.root / "repo"
        make_repo(repo, {**product_files(), **harness_files()})
        ralph = repo / ".ralph"
        ralph.mkdir()
        (ralph / "events-20260816-180346.jsonl").write_text("[]")
        (ralph / "diagnostics" / "logs").mkdir(parents=True)
        (ralph / "diagnostics" / "logs" / "ralph-1.log").write_text("log")
        report = footprint.inventory(repo)
        self.assertTrue(report.ok, "\n".join(report.errors))

    def test_on_disk_ralph_unignored_growth_fails(self) -> None:
        # A non-ignored on-disk entry under ``.ralph/`` outside the migration
        # allowlist is new legacy content the new path must never create.
        files = dict(product_files())
        files[".gitignore"] = ".factory-state/\n__pycache__/\n"
        repo = self.root / "repo"
        make_repo(repo, files)
        (repo / ".ralph" / "agent").mkdir(parents=True)
        (repo / ".ralph" / "agent" / "new.md").write_text("x")
        report = footprint.inventory(repo)
        self.assertFalse(report.ok)
        self.assertTrue(
            any("legacy namespace gains a new on-disk entry" in error
                for error in report.errors)
        )

    def test_on_disk_ralph_fifo_fails(self) -> None:
        repo = self.root / "repo"
        make_repo(repo, {**product_files(), **harness_files()})
        (repo / ".ralph").mkdir()
        os.mkfifo(repo / ".ralph" / "pipe")
        report = footprint.inventory(repo)
        self.assertFalse(report.ok)
        self.assertTrue(
            any("legacy namespace contains a special file" in error
                for error in report.errors)
        )

    def test_on_disk_nested_case_escape_fails(self) -> None:
        repo = self.root / "repo"
        make_repo(repo, {**product_files(), **harness_files()})
        (repo / "src" / ".Factory").mkdir()
        (repo / "src" / ".Factory" / "impl.py").write_text("x")
        report = footprint.inventory(repo)
        self.assertFalse(report.ok)
        self.assertTrue(
            any("case/Unicode namespace escape is present in product" in error
                for error in report.errors)
        )

    def test_on_disk_nested_root_marker_fails(self) -> None:
        repo = self.root / "repo"
        make_repo(repo, {**product_files(), **harness_files()})
        (repo / "docs" / "PROMPT.md").write_text("leaked")
        report = footprint.inventory(repo)
        self.assertFalse(report.ok)
        self.assertTrue(
            any("legacy root harness file leaked into product tree" in error
                for error in report.errors)
        )

    def test_on_disk_nested_hidden_namespace_in_product_fails(self) -> None:
        repo = self.root / "repo"
        make_repo(repo, {**product_files(), **harness_files()})
        (repo / "src" / ".factory" / "manifest").parent.mkdir(parents=True)
        (repo / "src" / ".factory" / "manifest").write_text("x")
        report = footprint.inventory(repo)
        self.assertFalse(report.ok)
        self.assertTrue(
            any("harness namespace inside product tree" in error
                for error in report.errors)
        )

    def test_on_disk_product_harness_marker_fails(self) -> None:
        repo = self.root / "repo"
        make_repo(repo, {**product_files(), **harness_files()})
        (repo / "src" / "factory-loop.json").write_text("{}")
        report = footprint.inventory(repo)
        self.assertFalse(report.ok)
        self.assertTrue(
            any("harness config artifact in product tree" in error
                for error in report.errors)
        )

    def test_hidden_namespace_fifo_fails(self) -> None:
        repo = self.root / "repo"
        make_repo(repo, {**product_files(), **harness_files()})
        os.mkfifo(repo / ".factory" / "pipe")
        report = footprint.inventory(repo)
        self.assertFalse(report.ok)
        self.assertTrue(
            any("hidden namespace contains a special file" in error
                and "pipe" in error for error in report.errors)
        )

    def test_hidden_namespace_unix_socket_fails(self) -> None:
        import socket
        repo = self.root / "repo"
        make_repo(repo, {**product_files(), **harness_files()})
        sock = socket.socket(socket.AF_UNIX)
        sock.bind(str(repo / ".pi" / "agent.sock"))
        sock.close()
        report = footprint.inventory(repo)
        self.assertFalse(report.ok)
        self.assertTrue(
            any("hidden namespace contains a special file" in error
                for error in report.errors)
        )

    def test_hidden_namespace_mount_crossing_fails(self) -> None:
        # A simulated ``st_dev`` change (a mount point) beneath a hidden
        # namespace must fail closed.
        repo = self.root / "repo"
        make_repo(repo, {**product_files(), **harness_files()})
        target = repo / ".factory" / "loop"
        dev = os.lstat(target).st_dev + 1
        with mock.patch("os.lstat", side_effect=lstat_simulating(
                [target], st_dev=dev)):
            report = footprint.inventory(repo)
        self.assertFalse(report.ok)
        self.assertTrue(
            any("hidden namespace crosses a mount point" in error
                for error in report.errors)
        )


class TestDiscoveryExclusion(FootprintTestCase):
    def test_product_discovery_excludes_hidden_namespaces(self) -> None:
        repo = self.root / "repo"
        make_repo(repo, {**product_files(), **harness_files(),
                         ".ralph/agent/scratchpad.md": "handoff"})
        discovered = footprint.product_discovery(repo)
        product = sorted(product_files())
        self.assertEqual(discovered, product)
        for path in discovered:
            self.assertNotIn(path.split("/", 1)[0],
                             footprint.HIDDEN_NAMESPACE_SET)

    def test_discover_on_disk_excludes_hidden_namespaces(self) -> None:
        repo = self.root / "repo"
        make_repo(repo, {**product_files(), **harness_files()})
        (repo / ".factory-state").mkdir(mode=0o700, parents=True, exist_ok=True)
        (repo / ".factory-state" / "factory-loop.json").write_text("{}")
        discovered = footprint.discover_on_disk(repo)
        self.assertNotIn(".factory/loop/plan_parser.py", discovered)
        self.assertNotIn(".factory-state/factory-loop.json", discovered)
        self.assertNotIn(".pi/subagents.json", discovered)
        for rel in product_files():
            self.assertIn(rel, discovered)

    def test_product_install_manifest_never_contains_harness(self) -> None:
        repo = self.root / "repo"
        make_repo(repo, {**product_files(), **harness_files()})
        discovered = footprint.product_discovery(repo)
        manifest = {path for path in discovered
                    if path.startswith(("src/", "tests/", "packaging/", "CMake"))}
        for path in manifest:
            self.assertFalse(path.startswith(
                (".factory", ".factory-state", ".pi")
            ))

    def test_product_discovery_excludes_root_harness_markers(self) -> None:
        # A tracked harness config-artifact basename at the product root is
        # excluded from tracked discovery (never a packaging glob input).
        repo = self.repo({**product_files(), **harness_files(),
                          "factory-loop.json": "{}",
                          "factory-plan-v1.schema.json": "{}"})
        discovered = footprint.product_discovery(repo)
        for marker in ("factory-loop.json", "factory-plan-v1.schema.json"):
            self.assertNotIn(marker, discovered, f"{marker} leaked")
        self.assertIn("src/main.c", discovered)

    def test_discover_on_disk_excludes_root_harness_markers(self) -> None:
        # An on-disk root config-artifact basename never reaches on-disk
        # discovery.
        repo = self.root / "repo"
        make_repo(repo, {**product_files(), **harness_files()})
        (repo / "factory-loop.json").write_text("{}")
        discovered = footprint.discover_on_disk(repo)
        self.assertNotIn("factory-loop.json", discovered)

    def test_discover_on_disk_excludes_namespace_alias(self) -> None:
        # A namespace-alias directory (``.factory.``) must never reach
        # on-disk discovery either.
        repo = self.root / "repo"
        make_repo(repo, {**product_files(), **harness_files()})
        (repo / ".factory.").mkdir()
        (repo / ".factory." / "config.toml").write_text("x")
        discovered = footprint.discover_on_disk(repo)
        self.assertNotIn(".factory./config.toml", discovered)

    def test_discover_on_disk_excludes_ignored_secrets_and_build(self) -> None:
        # On-disk discovery must be built from the pinned Git
        # ``-co --exclude-standard`` set so git-ignored credentials, caches,
        # logs, build artifacts, runtime state, and root harness markers can
        # never reach a packaging glob — even though they exist on disk.
        files = dict(product_files())
        files[".gitignore"] = (
            ".factory-state/\n.ralph/\n__pycache__/\n"
            "*.env\nbuild/\n*.log\n"
        )
        repo = self.root / "repo"
        make_repo(repo, files)
        (repo / ".factory-state").mkdir(mode=0o700)
        (repo / ".factory-state" / "state.json").write_text("{}")
        (repo / "build").mkdir()
        (repo / "build" / "out.o").write_bytes(b"\x7fELF")
        (repo / "credentials.env").write_text("token=secret")
        (repo / "debug.log").write_text("log")
        (repo / "PROMPT.md").write_text("root marker")
        discovered = footprint.discover_on_disk(repo)
        for leaked in ("build/out.o", "credentials.env", "debug.log",
                       ".factory-state/state.json", "PROMPT.md"):
            self.assertTrue((repo / leaked).exists(),
                            f"fixture missing {leaked}")
            self.assertNotIn(leaked, discovered, f"{leaked} leaked")
        for rel in product_files():
            if rel != ".gitignore":
                self.assertIn(rel, discovered)


class TestRemovalWithoutLoss(FootprintTestCase):
    def test_delete_harness_preserves_product(self) -> None:
        repo = self.root / "repo"
        harnessed_product_repo(repo)
        (repo / ".factory-state").mkdir(mode=0o700, parents=True, exist_ok=True)
        (repo / ".factory-state" / "factory-loop.json").write_text("{}")
        product_paths = sorted(
            rel for rel in footprint.tracked_files(repo)
            if footprint.classify_path(rel) == "product"
        )
        before = {rel: (repo / rel).read_bytes() for rel in product_paths}
        removed = footprint.remove_harness(repo)
        self.assertEqual(sorted(removed),
                         sorted(footprint.HIDDEN_NAMESPACES))
        for rel, content in before.items():
            self.assertTrue((repo / rel).exists(), f"product file lost: {rel}")
            self.assertEqual((repo / rel).read_bytes(), content)
        for namespace in footprint.HIDDEN_NAMESPACES:
            self.assertFalse((repo / namespace).exists())
        # The removal is a working-tree operation: every Git status change
        # must be a deletion confined to the hidden namespaces, and no
        # product path may appear as modified, deleted, or untracked.
        status = _run(repo, "status", "--porcelain")
        self.assertTrue(status.strip())
        for line in status.splitlines():
            rel = line[3:]
            first = rel.split("/", 1)[0]
            self.assertIn(
                first, footprint.HIDDEN_NAMESPACE_SET,
                f"removal touched a non-harness path: {line}",
            )
            self.assertNotIn(rel, before, f"removal deleted product file: {rel}")

    def test_remove_harness_refuses_mount_crossing_root(self) -> None:
        # A namespace root on a different device (a mount point) must refuse
        # the entire removal, leaving every namespace untouched.
        repo = self.root / "repo"
        harnessed_product_repo(repo)
        (repo / ".factory-state").mkdir(mode=0o700)
        target = repo / ".factory-state"
        dev = os.lstat(target).st_dev + 1
        with mock.patch("os.lstat", side_effect=lstat_simulating(
                [target], st_dev=dev)):
            with self.assertRaises(footprint.FootprintError):
                footprint.remove_harness(repo)
        for name in footprint.HIDDEN_NAMESPACES:
            self.assertTrue((repo / name).exists(), f"{name} was removed")

    def test_remove_harness_refuses_descendant_crossing(self) -> None:
        # A descendant that crosses a filesystem boundary (simulated) must
        # refuse the removal before anything is deleted.
        repo = self.root / "repo"
        harnessed_product_repo(repo)
        (repo / ".factory-state").mkdir(mode=0o700)
        (repo / ".factory-state" / "state.json").write_text("{}")
        target = repo / ".factory-state" / "state.json"
        dev = os.lstat(target).st_dev + 1
        with mock.patch("os.lstat", side_effect=lstat_simulating(
                [target], st_dev=dev)):
            with self.assertRaises(footprint.FootprintError):
                footprint.remove_harness(repo)
        for name in footprint.HIDDEN_NAMESPACES:
            self.assertTrue((repo / name).exists(), f"{name} was removed")


class TestExternalInstall(FootprintTestCase):
    def _install(self, root: Path, prefix: Path, manifest: list[str]) -> None:
        prefix.mkdir(parents=True)
        for rel in manifest:
            source = root / rel
            if source.is_file():
                target = prefix / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, target)

    def test_external_install_manifest_ok(self) -> None:
        repo = self.root / "repo"
        make_repo(repo, {**product_files(), **harness_files()})
        prefix = self.root / "prefix"
        manifest = [".factory/loop/plan_parser.py", ".pi/agents/reviewer.md"]
        self._install(repo, prefix, manifest)
        errors = footprint.verify_external_install(repo, prefix, manifest)
        self.assertEqual(errors, [])

    def test_external_install_manifest_product_path_fails(self) -> None:
        repo = self.root / "repo"
        make_repo(repo, {**product_files(), **harness_files()})
        prefix = self.root / "prefix"
        manifest = ["src/main.c"]
        self._install(repo, prefix, manifest)
        errors = footprint.verify_external_install(repo, prefix, manifest)
        self.assertTrue(any("non-harness path" in error for error in errors))

    def test_external_install_prefix_must_be_absolute(self) -> None:
        repo = self.root / "repo"
        make_repo(repo, {**product_files(), **harness_files()})
        errors = footprint.verify_external_install(
            repo, Path("relative-prefix"), [])
        self.assertTrue(any("not absolute" in error for error in errors))

    def test_external_install_prefix_outside_product_tree(self) -> None:
        repo = self.root / "repo"
        make_repo(repo, {**product_files(), **harness_files()})
        errors = footprint.verify_external_install(repo, repo / "prefix", [])
        self.assertTrue(
            any("outside the product tree" in error for error in errors)
        )

    def test_external_install_prefix_symlink_fails(self) -> None:
        # The prefix itself must be a real directory: a symlinked prefix
        # could redirect the inventory walk (or a later removal) outside
        # the declared location and is rejected outright.
        repo = self.root / "repo"
        make_repo(repo, {**product_files(), **harness_files()})
        outside = self.root / "outside"
        outside.mkdir()
        os.symlink(outside, self.root / "prefix")
        errors = footprint.verify_external_install(repo, self.root / "prefix", [])
        self.assertTrue(any("prefix is a symlink" in error for error in errors))

    def test_external_install_resolved_containment_fails(self) -> None:
        # Containment compares the *resolved* paths: a product root reached
        # through a symlinked alias must still reject a prefix that
        # physically lives inside the tree (the lexical comparison alone
        # would miss it).
        repo = self.root / "repo"
        make_repo(repo, {**product_files(), **harness_files()})
        alias = self.root / "alias-root"
        os.symlink(repo, alias)
        errors = footprint.verify_external_install(alias, repo / "prefix", [])
        self.assertTrue(any("outside the product tree" in error
                            for error in errors))

    def test_external_install_symlink_fails(self) -> None:
        repo = self.root / "repo"
        make_repo(repo, {**product_files(), **harness_files()})
        prefix = self.root / "prefix"
        self._install(repo, prefix, [".factory/loop/plan_parser.py"])
        os.symlink("/etc/passwd", prefix / ".factory" / "escape")
        errors = footprint.verify_external_install(repo, prefix, [])
        self.assertTrue(any("symlink" in error for error in errors))

    def test_external_install_omitted_manifest_file_fails(self) -> None:
        # Completeness: every manifest file must be physically present; an
        # omitted file fails closed (a missing harness blob would silently
        # break the installed copy).
        repo = self.root / "repo"
        make_repo(repo, {**product_files(), **harness_files()})
        prefix = self.root / "prefix"
        manifest = [".factory/loop/plan_parser.py", ".pi/agents/reviewer.md"]
        self._install(repo, prefix, manifest)
        (prefix / ".pi" / "agents" / "reviewer.md").unlink()
        errors = footprint.verify_external_install(repo, prefix, manifest)
        self.assertTrue(any("omits manifest file" in error
                            for error in errors))

    def test_external_install_extra_unmanifested_fails(self) -> None:
        # Exactness: no physical file may exist outside the manifest; an
        # extra file (for example a product file slipped into the copy)
        # must fail closed.
        repo = self.root / "repo"
        make_repo(repo, {**product_files(), **harness_files()})
        prefix = self.root / "prefix"
        manifest = [".factory/loop/plan_parser.py"]
        self._install(repo, prefix, manifest)
        (prefix / ".factory" / "extra.toml").write_text("x")
        errors = footprint.verify_external_install(repo, prefix, manifest)
        self.assertTrue(any("unmanifested path" in error
                            for error in errors))

    def test_external_install_entrypoint_allowed(self) -> None:
        # An explicitly listed trusted executable entrypoint is the only
        # non-manifest content an external install may carry.
        repo = self.root / "repo"
        make_repo(repo, {**product_files(), **harness_files()})
        prefix = self.root / "prefix"
        manifest = [".factory/loop/plan_parser.py"]
        self._install(repo, prefix, manifest)
        (prefix / "bin").mkdir()
        (prefix / "bin" / "factory").write_text("#!/bin/sh\nexit 0\n")
        (prefix / "bin" / "factory").chmod(0o755)
        errors = footprint.verify_external_install(
            repo, prefix, manifest, entrypoints=["bin/factory"])
        self.assertEqual(errors, [])

    def test_external_install_entrypoint_not_executable_fails(self) -> None:
        repo = self.root / "repo"
        make_repo(repo, {**product_files(), **harness_files()})
        prefix = self.root / "prefix"
        manifest = [".factory/loop/plan_parser.py"]
        self._install(repo, prefix, manifest)
        (prefix / "bin").mkdir()
        (prefix / "bin" / "factory").write_text("#!/bin/sh\nexit 0\n")
        errors = footprint.verify_external_install(
            repo, prefix, manifest, entrypoints=["bin/factory"])
        self.assertTrue(any("not an explicit trusted executable" in error
                            for error in errors))

    def test_external_install_entrypoint_symlink_fails(self) -> None:
        # An entrypoint may never be a symlink: the installed copy must be
        # fully self-contained under the prefix.
        repo = self.root / "repo"
        make_repo(repo, {**product_files(), **harness_files()})
        prefix = self.root / "prefix"
        manifest = [".factory/loop/plan_parser.py"]
        self._install(repo, prefix, manifest)
        (prefix / "bin").mkdir()
        os.symlink("/bin/sh", prefix / "bin" / "factory")
        errors = footprint.verify_external_install(
            repo, prefix, manifest, entrypoints=["bin/factory"])
        self.assertTrue(any("symlink" in error for error in errors))


class TestProductInstallPrefix(FootprintTestCase):
    def test_product_install_clean(self) -> None:
        prefix = self.root / "staged"
        (prefix / "bin").mkdir(parents=True)
        (prefix / "lib").mkdir()
        (prefix / "bin" / "sample").write_text("#!/bin/sh\n")
        errors = footprint.verify_product_install(prefix)
        self.assertEqual(errors, [])

    def test_product_install_harness_contamination_fails(self) -> None:
        prefix = self.root / "staged"
        (prefix / "bin").mkdir(parents=True)
        (prefix / ".factory").mkdir()
        (prefix / ".factory" / "config.toml").write_text("x")
        errors = footprint.verify_product_install(prefix)
        self.assertTrue(any("harness namespace" in error for error in errors))

    def test_product_install_root_harness_file_fails(self) -> None:
        prefix = self.root / "staged"
        prefix.mkdir()
        (prefix / "PROMPT.md").write_text("x")
        errors = footprint.verify_product_install(prefix)
        self.assertTrue(any("root harness file" in error for error in errors))

    def test_product_install_nested_harness_namespace_fails(self) -> None:
        # A hidden namespace at any depth of the staged prefix is
        # packaging/install contamination, not only at the top level.
        prefix = self.root / "staged"
        (prefix / "usr" / "share" / ".factory").mkdir(parents=True)
        (prefix / "usr" / "share" / ".factory" / "config.toml").write_text("x")
        errors = footprint.verify_product_install(prefix)
        self.assertTrue(any("harness namespace" in error for error in errors))

    def test_product_install_nested_harness_marker_fails(self) -> None:
        # A harness config-artifact basename at any depth of the staged
        # prefix fails closed.
        prefix = self.root / "staged"
        (prefix / "usr" / "share").mkdir(parents=True)
        (prefix / "usr" / "share" / "factory-loop.json").write_text("{}")
        errors = footprint.verify_product_install(prefix)
        self.assertTrue(any("harness config artifact" in error
                            for error in errors))

    def test_product_install_nested_namespace_alias_fails(self) -> None:
        # A trailing-dot namespace alias inside the staged prefix is
        # rejected the same way as in the repository tree.
        prefix = self.root / "staged"
        (prefix / "usr" / "share" / ".pi.").mkdir(parents=True)
        errors = footprint.verify_product_install(prefix)
        self.assertTrue(any("namespace alias" in error for error in errors))

    def test_product_install_symlink_fails(self) -> None:
        prefix = self.root / "staged"
        (prefix / "bin").mkdir(parents=True)
        os.symlink("/bin/sh", prefix / "bin" / "lnk")
        errors = footprint.verify_product_install(prefix)
        self.assertTrue(any("symlink" in error for error in errors))

    def test_product_install_prefix_symlink_fails(self) -> None:
        prefix = self.root / "staged"
        target = self.root / "real-staged"
        target.mkdir()
        os.symlink(target, prefix)
        errors = footprint.verify_product_install(prefix)
        self.assertTrue(any("prefix is a symlink" in error for error in errors))

    def test_product_install_special_file_fails(self) -> None:
        prefix = self.root / "staged"
        prefix.mkdir()
        os.mkfifo(prefix / "pipe")
        errors = footprint.verify_product_install(prefix)
        self.assertTrue(any("special file" in error for error in errors))

    def test_product_install_mount_crossing_fails(self) -> None:
        # A staged entry on a different device (a mount point) fails closed.
        prefix = self.root / "staged"
        (prefix / "usr").mkdir(parents=True)
        target = prefix / "usr"
        dev = os.lstat(target).st_dev + 1
        with mock.patch("os.lstat", side_effect=lstat_simulating(
                [target], st_dev=dev)):
            errors = footprint.verify_product_install(prefix)
        self.assertTrue(any("mount point" in error for error in errors))


class TestPinnedExternalExecutables(FootprintTestCase):
    def test_pinned_git_is_trusted(self) -> None:
        errors = footprint.check_pinned_external_executables()
        self.assertEqual(errors, [])

    def test_trusted_prefixes_are_supported_executable_roots(self) -> None:
        # The trust boundary is exactly the supported immutable executable
        # roots; /etc (configuration) and /lib//lib64 (shared libraries)
        # are not executable roots, never hold a pinned candidate, and must
        # not widen the boundary.
        for forbidden in ("/etc", "/lib", "/lib64"):
            self.assertNotIn(forbidden, footprint.TRUSTED_EXTERNAL_PREFIXES)
        for required in ("/usr/bin", "/bin", "/sbin",
                         "/run/current-system", "/nix/store"):
            self.assertIn(required, footprint.TRUSTED_EXTERNAL_PREFIXES)


class TestCLI(FootprintTestCase):
    def run_cli(self, root: Path, *extra: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, str(FOOTPRINT_SCRIPT), "--root", str(root),
             *extra],
            capture_output=True, text=True, timeout=120,
        )

    def test_cli_clean_repo_exit_zero(self) -> None:
        repo = self.repo({**product_files(), **harness_files()})
        result = self.run_cli(repo)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_cli_contamination_exit_one(self) -> None:
        repo = self.repo({**product_files(), **harness_files()})
        (repo / ".Factory").mkdir()
        result = self.run_cli(repo)
        self.assertEqual(result.returncode, 1)
        self.assertIn("case/Unicode namespace escape", result.stderr)

    def test_cli_product_discovery(self) -> None:
        repo = self.repo({**product_files(), **harness_files()})
        result = self.run_cli(repo, "--product-discovery")
        self.assertEqual(result.returncode, 0)
        lines = set(result.stdout.splitlines())
        self.assertIn("src/main.c", lines)
        for path in lines:
            first = path.split("/", 1)[0]
            self.assertNotIn(first, footprint.HIDDEN_NAMESPACE_SET)


class TestLiveBoilerplate(FootprintTestCase):
    def test_live_inventory_clean(self) -> None:
        report = footprint.inventory(ROOT)
        self.assertTrue(report.ok, "\n".join(report.errors))

    def test_live_product_discovery_has_no_hidden_paths(self) -> None:
        for rel in footprint.product_discovery(ROOT):
            first = rel.split("/", 1)[0]
            self.assertNotIn(first, footprint.HIDDEN_NAMESPACE_SET)

    def test_live_factory_state_is_ignored(self) -> None:
        result = gitutil.git_run(["-C", str(ROOT), "check-ignore", "-q",
                                  ".factory-state"])
        self.assertEqual(result.returncode, 0,
                         ".factory-state must be git-ignored")

    def test_live_no_tracked_factory_state(self) -> None:
        for rel in footprint.tracked_files(ROOT):
            self.assertFalse(rel.startswith(".factory-state/"),
                             f"tracked runtime state: {rel}")

    def test_live_no_root_harness_file(self) -> None:
        for rel in footprint.tracked_files(ROOT):
            if "/" in rel:
                continue
            self.assertNotIn(rel, footprint.FORBIDDEN_ROOT_FACTORY_FILES)
            self.assertNotIn(rel, footprint._HARNESS_FILENAME_MARKERS)


if __name__ == "__main__":
    unittest.main(verbosity=2)
