#!/usr/bin/env python3
"""Hidden migration/deprecation conformance suite (Task 15; MIG-01, CTX-02).

This test lives under the hidden ``.factory/tests/`` namespace (HIDE-01, §3).
It is the deterministic verification for Task 15 (FACTORY-LOOP-SPEC §21):

* *read isolation* — deriving the ``factory-migration/v1`` snapshot never
  opens, reads, parses, or imports the legacy runtime surfaces: ``.ralph/``
  state, the stale ``.factory/artifacts/context-summary.md`` mirror, and the
  legacy workspace ``.ollama-usage-env`` credential store.  A probe
  instruments every ``os.open``/``builtins.open``/``os.read`` in the process
  and fails the test on any legacy-path open, while synthetic secret bytes
  prove they never reach the snapshot or the published report;
* *exact preservation* — the snapshot reproduces the committed plan binding
  and digest, the live HEAD, the full porcelain dirty-work surface (never
  reset, never rewritten), evidence artifact metadata under the ignored
  ``.factory-state/`` namespace, and the structured blocker sidecar;
* *no runtime imports* — the migration authority never imports or invokes
  Ralph task, memory, event-stream, or completion-token machinery (AST
  scan plus the machine-checkable ``no_import`` contract fields);
* *one control-state authority* — the operator ``migrate`` command publishes
  exactly one ``factory-state/v1`` file through the trusted no-replace
  ``init_state`` authority: it refuses to overwrite an existing state file,
  refuses a second campaign digest binding, refuses a symlinked/unsafe/tampered
  state path, and never creates a second mutable control authority.  The
  published state carries exact ownership (current UID), mode (0600), and
  single-link identity inside the private 0700 directory;
* *freeze surface* — the tracked ``.factory/ralph-freeze`` marker freezes
  every new legacy Ralph launch; the ``status``/``freeze`` migration commands
  report it; the marker itself is trusted only as a regular tracked file (a
  symlink fails closed);
* *context-summary unwiring* — the stale mirror is absent from the tracked
  tree and every new-path control step never invokes the deprecated
  generator/verifier, which fail closed when called;
* *legacy-store metadata-only* — the store is detected with ``lstat``
  metadata only and the operator command refuses state conflicts without
  ever reading a legacy byte.

The suite never touches the live repository's ``.ralph/`` directory, never
moves or reads a real legacy store, and never runs a legacy campaign.  All
repositories are test-owned temporary Git fixtures; live-repo checks are
read-only (CLI derivation, freeze/status inspection, launcher help).
"""

from __future__ import annotations

import ast
import builtins
import errno
import hashlib
import inspect
import json
import os
from pathlib import Path
import re
import socket
import stat
import subprocess
import sys
import tempfile
import time
import unittest
import unittest.mock as mock

ROOT = Path(__file__).resolve().parents[2]
LOOP = ROOT / ".factory" / "loop"
MIGRATION_SCRIPT = LOOP / "migration.py"

sys.path.insert(0, str(LOOP))
import gitutil  # noqa: E402  (PATH-pinned Git runner; also used by fixtures)
import migration  # noqa: E402
import plan_parser  # noqa: E402
import state as state_module  # noqa: E402
from state import STATE_FILE_NAME  # noqa: E402

# The frozen launch set must stay in exact agreement with the launchers' own
# freeze gates and with the migration module's freeze authority.
FROZEN_LAUNCHERS = (
    "scripts/ralph-campaign.sh",
    "scripts/ralph-plan.sh",
    "scripts/ralph-run.sh",
    "scripts/ralph-audit.sh",
    "scripts/ralph-maintenance-plan.sh",
    "scripts/ralph-maintenance-run.sh",
)

# Synthetic secret markers: never real credentials.  Their presence in any
# snapshot, report, log, or stdout proves a legacy byte leaked.
RALPH_SECRET = b"RALPH-RUNTIME-SECRET-9f4c1a"
SUMMARY_SECRET = b"CONTEXT-SUMMARY-SECRET-77aa2b"
ENV_SECRET = b"OLLAMA_LEGACY_SECRET=7f3b9c"

LEGACY_ENV_STORE = ".ollama-usage-env"
CONTEXT_SUMMARY = ".factory/artifacts/context-summary.md"

PLAN_TEMPLATE = """\
---
spec_path: docs/SPEC.md
spec_commit: 0000000000000000000000000000000000000000
spec_blob: 0000000000000000000000000000000000000000
base_commit: 0000000000000000000000000000000000000000
status: active
---

# Implementation Plan

## Goal and non-goals

Goal: exercise the `factory-plan/v1` grammar deterministically.

## Architecture and constraints

Plan: Python 3.11 standard library only; the migration never reads legacy state.

## Specification conformance matrix

| ID | Spec § | Classification | Evidence | Task |
|----|--------|--------------|----------|------|
| AUTH-01 | §5, §7 | missing | fixture exercises a registry-bound matrix | Task 1 |
| CTX-01 | §5, §9 | missing | fixture exercises a registry-bound matrix | Task 1 |
| CTX-02 | §5, §18 | missing | fixture exercises a registry-bound matrix | Task 1 |
| ROLE-01 | §6 | missing | fixture exercises a registry-bound matrix | Task 1 |
| PLAN-01 | §7 | missing | fixture exercises a registry-bound matrix | Task 1 |
| TASK-01 | §7, §8 | missing | fixture exercises a registry-bound matrix | Task 1 |
| TASK-02 | §9, §20 | missing | fixture exercises a registry-bound matrix | Task 1 |
| QUOTA-01 | §10 | missing | fixture exercises a registry-bound matrix | Task 1 |
| QUOTA-02 | §10 | missing | fixture exercises a registry-bound matrix | Task 1 |
| STATE-01 | §11, §17 | missing | fixture exercises a registry-bound matrix | Task 1 |
| LOCK-01 | §12 | missing | fixture exercises a registry-bound matrix | Task 1 |
| PROC-01 | §9, §12, §17 | missing | fixture exercises a registry-bound matrix | Task 1 |
| GIT-01 | §12, §17 | missing | fixture exercises a registry-bound matrix | Task 1 |
| PHASE-01 | §13, §14 | missing | fixture exercises a registry-bound matrix | Task 1 |
| COMPLETE-01 | §15 | missing | fixture exercises a registry-bound matrix | Task 1 |
| FIND-01 | §16 | missing | fixture exercises a registry-bound matrix | Task 1 |
| CRED-01 | §18 | missing | fixture exercises a registry-bound matrix | Task 1 |
| EVID-01 | §19 | missing | fixture exercises a registry-bound matrix | Task 1 |
| VIS-01 | §19 | missing | fixture exercises a registry-bound matrix | Task 1 |
| RUNNER-01 | §19 | missing | fixture exercises a registry-bound matrix | Task 1 |
| HIDE-01 | §3 | missing | fixture exercises a registry-bound matrix | Task 1 |
| MIG-01 | §21 | missing | fixture exercises a registry-bound matrix | Task 1 |
| TEST-01 | §22 | missing | fixture exercises a registry-bound matrix | Task 1 |
| ACCEPT-01 | §23 | missing | fixture exercises a registry-bound matrix | Task 2 |

## Interaction acceptance inventory

- input boundary: only the committed plan bytes reach the parser.
- semantic boundary: no legacy task, memory, event, or completion authority.
- production boundary: the migration never writes product code.
- evidence boundary: the snapshot is a deterministic function of Git state.

## Task 1: Migrate the fixture control plane

- Status: pending
- Dependencies: None
- Priority: 3
- Scope: derive the snapshot without importing legacy runtime state.
- Acceptance criteria: the snapshot preserves the exact plan/head/dirty/evidence.
- Verification: `.factory/tests/test-factory-migration.py`.
- Documentation impact: none.

## Task 2: Final documentation and specification audit

- Status: pending
- Dependencies: Task 1
- Scope: fixture audit task for the acceptance plan.
- Acceptance criteria: the fixture remains parseable.
- Verification: the fixture suite.
- Documentation impact: none.
"""


def _git(root: Path, *argv: str) -> str:
    result = gitutil.git_run(["-C", str(root), *argv], timeout=120.0)
    if result.returncode != 0:
        raise AssertionError(
            f"fixture git {argv!r} failed: {result.stderr.strip()}"
        )
    return result.stdout


def _git_bytes(root: Path, *argv: str) -> bytes:
    result = gitutil.git_bytes(["-C", str(root), *argv], timeout=120.0)
    if result.returncode != 0:
        raise AssertionError(
            f"fixture git {argv!r} failed: {result.stderr.strip()}"
        )
    return result.stdout


class FixtureRepo:
    """A test-owned temporary Git repository with a valid committed plan.

    ``oversize`` maps a repository-relative path to content that replaces the
    default baseline bytes *before* the plan is bound, so a test can commit
    an intentionally oversized spec/prompt/audit/plan blob that the plan
    (or the migration) actually binds to.
    """

    def __init__(self, testcase: unittest.TestCase, *, seed_state: bool = False,
                 seed_ledger: bool = False,
                 oversize: dict[str, bytes] | None = None):
        tmp = tempfile.TemporaryDirectory(prefix="factory-migration-")
        testcase.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.oversize = oversize or {}
        _git(self.root, "init", "-q", "-b", "boilerplate-develop")
        self._write_baseline(seed_state=seed_state, seed_ledger=seed_ledger)
        self._commit_all("baseline harness")
        head = _git(self.root, "rev-parse", "HEAD").strip()
        self._bind_plan(head)
        self._commit_all("bind the canonical plan to the spec blob")
        self._seed_legacy_surfaces()

    # -- construction -------------------------------------------------------
    def _write_baseline(self, *, seed_state: bool, seed_ledger: bool) -> None:
        root = self.root
        (root / "docs").mkdir(parents=True)
        (root / ".factory" / "artifacts").mkdir(parents=True)
        (root / ".factory" / "prompts").mkdir(parents=True)
        (root / ".factory" / "audit-objectives").mkdir(parents=True)
        (root / ".factory-state").mkdir(mode=0o700)
        # The evidence namespace and the legacy credential store are ignored
        # in the fixture exactly as in production; only committed authority
        # files reach Git history.
        (root / ".gitignore").write_text(
            ".factory-state/\n.ollama-usage-env\n", encoding="utf-8"
        )
        (root / "docs" / "SPEC.md").write_bytes(
            b"# Fixture specification\n\nDeterministic spec bytes.\n"
        )
        (root / "product.txt").write_bytes(b"product baseline\n")
        for role in ("planner", "developer", "tester", "auditor"):
            (root / ".factory" / "prompts" / f"{role}.md").write_text(
                f"# {role} fixture prompt\n"
            )
        (root / ".factory" / "audit-objectives" / "registry.json").write_text(
            json.dumps(
                {
                    "schema": "audit-objectives/v1",
                    "version": 1,
                    "objectives": [
                        {
                            "id": "AUD-01",
                            "title": "Migration integrity",
                            "objective": "Falsify migration byte isolation.",
                        }
                    ],
                }
            )
        )
        (root / ".factory" / "artifacts" / "blocked-facts.json").write_text(
            json.dumps(
                {
                    "schema": "ralph-blocked-facts/v1",
                    "facts": [
                        {
                            "id": "FACT-001",
                            "title": "MIG-01 fixture fact",
                            "status": "open",
                            "requirements": ["MIG-01"],
                            "blocking_evidence": "fixture",
                        }
                    ],
                }
            )
        )
        (root / ".factory" / "artifacts" / "implementation-plan.md").write_text(
            PLAN_TEMPLATE
        )
        # A test-declared oversize surface replaces the committed baseline
        # bytes before the plan is bound (so the plan binds the oversized
        # blob), while every other baseline file stays untouched.
        for rel, content in self.oversize.items():
            path = root / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
        # Evidence-shaped artifacts under the ignored evidence namespace.
        private = root / ".factory-state"
        (private / "audit-receipts").mkdir()
        (private / "runner-evidence").mkdir()
        self._write_private(private / "audit-receipts" / "a-000001.json",
                            b'{"schema": "audit-receipt/v1"}\n')
        self._write_private(private / "runner-evidence" / "re-1.json",
                            b'{"schema": "runner-evidence/v1"}\n')
        if seed_state:
            state_module.init_state(
                root,
                campaign_id="pre-existing",
                rounds_requested=1,
                specification_digest="a" * 64,
                plan_digest="b" * 64,
                role_prompt_digests={"planner": "c" * 64},
                audit_objectives_digest="d" * 64,
                phase_base_commit="f" * 40,
                branch="boilerplate-develop",
                now=1_700_000_000_000_000_000,
            )
        if seed_ledger:
            # A second campaign binding refusal needs a present ledger with a
            # validated entry; the state authority owns it (seed_state=False
            # here means the ledger is the only prior lifecycle authority).
            state_module.init_state(
                root,
                campaign_id="prior-campaign",
                rounds_requested=1,
                specification_digest="a" * 64,
                plan_digest="b" * 64,
                role_prompt_digests={"planner": "c" * 64},
                audit_objectives_digest="d" * 64,
                phase_base_commit="f" * 40,
                branch="boilerplate-develop",
                now=1_700_000_000_000_000_000,
            )
            state_module.record_phase_digest(root, "planning")
            # The second-campaign conflict is specifically the *ledger* binding:
            # remove the state file so only the append-only digest ledger
            # remains as the prior lifecycle authority (the migration must
            # refuse to create a second campaign under it).
            (root / ".factory-state" / STATE_FILE_NAME).unlink()

    @staticmethod
    def _write_private(path: Path, data: bytes) -> None:
        path.parent.mkdir(mode=0o700, exist_ok=True)
        fd = os.open(
            path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600
        )
        try:
            os.write(fd, data)
        finally:
            os.close(fd)

    def _commit_all(self, message: str) -> None:
        _git(self.root, "add", "-A")
        _git(
            self.root,
            "-c", "user.name=migration-test",
            "-c", "user.email=migration-test@example.invalid",
            "commit", "-q", "-m", message,
        )

    def _bind_plan(self, head: str) -> None:
        spec_blob = _git(self.root, "rev-parse", f"HEAD:docs/SPEC.md").strip()
        plan_path = self.root / ".factory" / "artifacts" / "implementation-plan.md"
        text = plan_path.read_text(encoding="utf-8")
        text = text.replace("spec_commit: " + "0" * 40, "spec_commit: " + head)
        text = text.replace("spec_blob: " + "0" * 40, "spec_blob: " + spec_blob)
        text = text.replace("base_commit: " + "0" * 40, "base_commit: " + head)
        plan_path.write_text(text, encoding="utf-8")

    def _seed_legacy_surfaces(self) -> None:
        root = self.root
        ralph = root / ".ralph"
        (ralph / "agent").mkdir(parents=True)
        (ralph / "agent" / "tasks.jsonl").write_bytes(b"RLP-" + RALPH_SECRET + b"\n")
        (ralph / "current-events").write_bytes(RALPH_SECRET + b"\n")
        (ralph / "loop.lock").write_bytes(b"fixture lock\n")
        (root / CONTEXT_SUMMARY).parent.mkdir(parents=True, exist_ok=True)
        (root / CONTEXT_SUMMARY).write_bytes(
            b"# stale context summary\n" + SUMMARY_SECRET + b"\n"
        )
        (root / LEGACY_ENV_STORE).write_bytes(
            b"export __Secure_session=fixture\n" + ENV_SECRET + b"\n"
        )

    def seed_dirty(self) -> None:
        root = self.root
        (root / "product.txt").write_text("product modified\n")
        (root / "notes.txt").write_text("untracked work\n")
        (root / ".ralph" / "scratch.md").write_text("handoff\n")
        (root / ".factory-state" / "campaign-result-1.json").write_bytes(
            b'{"phase": "planning"}\n'
        )

    # -- independent derivation helpers ------------------------------------
    def git(self, *argv: str) -> str:
        return _git(self.root, *argv)

    def git_bytes(self, *argv: str) -> bytes:
        return _git_bytes(self.root, *argv)

    def head(self) -> str:
        return self.git("rev-parse", "HEAD").strip()

    def plan_blob(self) -> bytes:
        blob = self.git("rev-parse", "HEAD:.factory/artifacts/implementation-plan.md").strip()
        return self.git_bytes("cat-file", "blob", blob)

    def porcelain_entries(self) -> list[dict[str, str]]:
        """Independent NUL-safe parse of the dirty-work surface."""
        raw = self.git_bytes("status", "--porcelain", "-z", "--untracked-files=all")
        entries: list[dict[str, str]] = []
        index = 0
        while index < len(raw):
            end = raw.find(b"\x00", index)
            if end < 0:
                raise AssertionError("malformed porcelain stream")
            entry = raw[index:end]
            entries.append(
                {
                    "flags": entry[0:2].decode("ascii", "replace"),
                    "path": entry[3:].decode("utf-8", "replace"),
                }
            )
            index = end + 1
        return entries


# ---------------------------------------------------------------------------
# Legacy read isolation probe
# ---------------------------------------------------------------------------


class OpenReadProbe:
    """Records every ``os.open``/``builtins.open``/``os.read`` and asserts the
    migration never opens a forbidden legacy surface.

    ``dir_fd``-based paths are resolved through ``/proc/self/fd`` so state I/O
    (which opens relative to the private directory) is reported as the real
    path.  Opening a forbidden path raises immediately — the test then also
    inspects the recorded trail for diagnostics.
    """

    def __init__(self, root: Path):
        self.root = os.path.abspath(str(root))
        self.opens: list[str] = []
        self.reads: list[tuple[int, str]] = []
        self._fd_to_path: dict[int, str] = {}
        self._patchers = []

    # -- forbidden-path predicate -------------------------------------------
    def _resolve(self, path: object, dir_fd: int | None = None) -> str:
        if isinstance(path, (bytes, bytearray)):
            path = os.fsdecode(bytes(path))
        elif isinstance(path, os.PathLike):
            path = os.fspath(path)
        if dir_fd is not None:
            try:
                base = os.readlink(f"/proc/self/fd/{dir_fd}")
            except OSError:
                base = f"<fd:{dir_fd}>"
            path = os.path.join(base, path)
        return os.path.abspath(path)

    def _forbidden(self, resolved: str) -> bool:
        root = self.root
        exact = {
            os.path.join(root, LEGACY_ENV_STORE),
            os.path.join(root, CONTEXT_SUMMARY),
            os.path.join(root, ".ralph"),
        }
        if resolved in exact:
            return True
        return resolved.startswith(os.path.join(root, ".ralph") + os.sep)

    # -- installation -------------------------------------------------------
    def install(self) -> "OpenReadProbe":
        probe = self
        real_open = os.open
        real_builtin_open = builtins.open
        real_read = os.read

        def fake_open(path, *args, **kwargs):
            resolved = probe._resolve(path, kwargs.get("dir_fd"))
            probe.opens.append(resolved)
            if probe._forbidden(resolved):
                raise AssertionError(f"forbidden legacy open/read: {resolved}")
            fd = real_open(path, *args, **kwargs)
            flags = args[1] if len(args) >= 2 else kwargs.get("flags", 0)
            if not (flags & (os.O_DIRECTORY | os.O_PATH)):
                probe._fd_to_path[fd] = resolved
            return fd

        def fake_builtin_open(path, *args, **kwargs):
            resolved = probe._resolve(path)
            probe.opens.append(resolved)
            if probe._forbidden(resolved):
                raise AssertionError(f"forbidden legacy open/read: {resolved}")
            handle = real_builtin_open(path, *args, **kwargs)
            probe._fd_to_path[handle.fileno()] = resolved
            return handle

        def fake_read(fd, n):
            probe.reads.append((fd, probe._fd_to_path.get(fd, "<unknown>")))
            return real_read(fd, n)

        # Patching ``os.open`` moves it out of the cached ``os.supports_dir_fd``
        # frozenset, which would make the state authority's
        # ``require_linux_primitives`` guard report dir_fd unavailable.  The
        # probe therefore re-registers the patched open in a copied set so
        # the real production guard still passes for the fixture work.
        supported = frozenset(os.supports_dir_fd) | {fake_open}
        self._patchers = [
            mock.patch.object(os, "open", fake_open),
            mock.patch.object(os, "supports_dir_fd", supported),
            mock.patch.object(builtins, "open", fake_builtin_open),
            mock.patch.object(os, "read", fake_read),
        ]
        for patcher in self._patchers:
            patcher.start()
        return self

    def restore(self) -> None:
        for patcher in reversed(self._patchers):
            patcher.stop()

    def assert_no_legacy_access(self, testcase: unittest.TestCase) -> None:
        forbidden = [path for path in self.opens if self._forbidden(path)]
        testcase.assertEqual(
            forbidden, [], f"legacy surface opened: {forbidden}"
        )
        testcase.assertFalse(
            any(self._forbidden(path) for _, path in self.reads),
            "legacy surface read through a descriptor",
        )


# ---------------------------------------------------------------------------
# Read isolation and byte-isolation of the derivation
# ---------------------------------------------------------------------------


class DerivationReadIsolationTest(unittest.TestCase):
    """The snapshot derivation never opens/reads a legacy byte."""

    def test_derive_never_opens_legacy_surfaces(self) -> None:
        fx = FixtureRepo(self)
        fx.seed_dirty()
        probe = OpenReadProbe(fx.root).install()
        self.addCleanup(probe.restore)
        snapshot = migration.derive_migration_snapshot(fx.root)
        snapshot.validate()
        probe.assert_no_legacy_access(self)
        self.assertFalse(snapshot.ralph_imported)
        self.assertFalse(snapshot.context_summary_read)
        self.assertFalse(snapshot.env_store_read)
        self.assertTrue(snapshot.legacy.ralph_runtime_present)
        self.assertTrue(snapshot.legacy.context_summary_present)
        self.assertIsNotNone(snapshot.legacy.env_store)
        self.assertTrue(snapshot.legacy.env_store.present)
        self.assertFalse(snapshot.legacy.env_store.read_bytes)

    def test_snapshot_contains_no_legacy_bytes(self) -> None:
        fx = FixtureRepo(self)
        snapshot = migration.derive_migration_snapshot(fx.root)
        report = json.dumps(snapshot.to_dict(), sort_keys=True).encode("utf-8")
        for secret in (RALPH_SECRET, SUMMARY_SECRET, ENV_SECRET):
            self.assertNotIn(secret, report)

    def test_report_contains_no_legacy_bytes(self):
        fx = FixtureRepo(self)
        probe = OpenReadProbe(fx.root).install()
        self.addCleanup(probe.restore)
        snapshot = migration.derive_migration_snapshot(fx.root)
        report_rel = migration.write_migration_report(fx.root, snapshot)
        probe.assert_no_legacy_access(self)
        self.assertEqual(report_rel, ".factory-state/migration.json")
        report_path = fx.root / ".factory-state" / "migration.json"
        raw = report_path.read_bytes()
        for secret in (RALPH_SECRET, SUMMARY_SECRET, ENV_SECRET):
            self.assertNotIn(secret, raw)
        payload = json.loads(raw.decode("utf-8"))
        self.assertEqual(payload["schema"], "factory-migration/v1")
        self.assertFalse(payload["no_import"]["env_store_read"])
        self.assertEqual(payload["legacy"]["env_store"]["read_bytes"], False)
        # The report is evidence, never a second control-state authority.
        self.assertFalse((fx.root / ".factory-state" / STATE_FILE_NAME).exists())

    def test_derive_stdout_contains_no_legacy_bytes(self):
        fx = FixtureRepo(self)
        result = subprocess.run(
            [sys.executable, str(MIGRATION_SCRIPT), "--root", str(fx.root),
             "derive", "--no-report"],
            capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        for secret in (b"RALPH-RUNTIME-SECRET", b"CONTEXT-SUMMARY-SECRET",
                       b"OLLAMA_LEGACY_SECRET"):
            self.assertNotIn(secret, result.stdout.encode("utf-8"))

    def test_env_store_metadata_is_exact_lstat(self):
        fx = FixtureRepo(self)
        snapshot = migration.derive_migration_snapshot(fx.root)
        store = snapshot.legacy.env_store
        self.assertIsNotNone(store)
        info = os.lstat(fx.root / LEGACY_ENV_STORE)
        self.assertEqual(store.path, LEGACY_ENV_STORE)
        self.assertTrue(store.is_regular)
        self.assertFalse(store.is_symlink)
        self.assertEqual(store.nlink, info.st_nlink)
        self.assertEqual(store.size, info.st_size)
        self.assertEqual(store.mode_octal, oct(stat.S_IMODE(info.st_mode)))
        self.assertEqual(store.uid, info.st_uid)
        self.assertEqual(store.mtime_ns, info.st_mtime_ns)
        self.assertFalse(store.read_bytes)
        self.assertTrue("outside the model workspace" in store.operator_action)


# ---------------------------------------------------------------------------
# Exact preservation of plan/head/dirty/evidence/blockers
# ---------------------------------------------------------------------------


class ExactDerivationTest(unittest.TestCase):
    def test_exact_plan_binding_and_digest(self):
        fx = FixtureRepo(self)
        snapshot = migration.derive_migration_snapshot(fx.root)
        raw = fx.plan_blob()
        plan = plan_parser.Plan.from_bytes(raw)
        self.assertEqual(snapshot.head_commit, fx.head())
        self.assertEqual(snapshot.plan_path, ".factory/artifacts/implementation-plan.md")
        self.assertEqual(snapshot.plan_digest, hashlib.sha256(raw).hexdigest())
        self.assertEqual(snapshot.spec_path, plan.spec_path)
        self.assertEqual(snapshot.spec_commit, plan.spec_commit)
        self.assertEqual(snapshot.spec_blob, plan.spec_blob)
        self.assertEqual(snapshot.base_commit, plan.base_commit)
        self.assertTrue(re.fullmatch(r"[0-9a-f]{40}", snapshot.spec_commit))
        self.assertTrue(re.fullmatch(r"[0-9a-f]{40}", snapshot.spec_blob))
        snapshot.validate()

    def test_exact_dirty_surface_preserved(self):
        fx = FixtureRepo(self)
        fx.seed_dirty()
        snapshot = migration.derive_migration_snapshot(fx.root)
        expected = fx.porcelain_entries()
        actual = [
            {
                "flags": entry.flags,
                "path": entry.path,
                "harness_runtime_untracked": entry.harness_runtime_untracked,
            }
            for entry in snapshot.dirty
        ]
        self.assertEqual(
            [(e["flags"], e["path"]) for e in actual],
            [(e["flags"], e["path"]) for e in expected],
        )
        # The derived surface is never rewritten: the working tree is intact.
        self.assertTrue((fx.root / "product.txt").read_text().endswith("modified\n"))
        self.assertTrue((fx.root / "notes.txt").exists())
        self.assertTrue((fx.root / ".ralph" / "scratch.md").exists())
        # .ralph untracked entries are flagged as harness runtime, never work.
        ralph_dirty = [e for e in actual if e["path"].startswith(".ralph/")]
        self.assertTrue(ralph_dirty)
        for entry in ralph_dirty:
            self.assertTrue(entry["harness_runtime_untracked"])

    def test_exact_evidence_metadata(self):
        fx = FixtureRepo(self)
        snapshot = migration.derive_migration_snapshot(fx.root)
        by_path = {entry.path: entry for entry in snapshot.evidence}
        for rel in (
            ".factory-state/audit-receipts/a-000001.json",
            ".factory-state/runner-evidence/re-1.json",
        ):
            self.assertIn(rel, by_path, by_path.keys())
            info = os.lstat(fx.root / rel)
            self.assertEqual(by_path[rel].size, info.st_size)
            self.assertEqual(
                by_path[rel].mode_octal, oct(stat.S_IMODE(info.st_mode))
            )
            self.assertFalse(by_path[rel].read_bytes)
        # A campaign result seeded as dirty evidence is enumerated too.
        fx.seed_dirty()
        snapshot2 = migration.derive_migration_snapshot(fx.root)
        paths2 = {entry.path for entry in snapshot2.evidence}
        self.assertIn(".factory-state/campaign-result-1.json", paths2)

    def test_exact_blockers(self):
        fx = FixtureRepo(self)
        snapshot = migration.derive_migration_snapshot(fx.root)
        self.assertEqual(
            list(snapshot.blockers),
            [
                {
                    "id": "FACT-001",
                    "title": "MIG-01 fixture fact",
                    "status": "open",
                    "requirements": ["MIG-01"],
                }
            ],
        )

    def test_absent_control_state_is_reported_not_created(self):
        fx = FixtureRepo(self)
        snapshot = migration.derive_migration_snapshot(fx.root)
        self.assertIsNone(snapshot.state)
        self.assertIsNone(snapshot.state_error)
        # Derivation never creates the control state file.
        self.assertFalse((fx.root / ".factory-state" / STATE_FILE_NAME).exists())


# ---------------------------------------------------------------------------
# Blocked-facts sidecar hardening: anchored no-follow read, identity, races
# ---------------------------------------------------------------------------


class BlockersHardeningTest(unittest.TestCase):
    """``_read_blockers`` fails closed on unsafe files and pathname races."""

    @staticmethod
    def _blockers(fx: FixtureRepo):
        return fx.root / ".factory" / "artifacts" / "blocked-facts.json"

    def test_symlink_swap_to_legacy_env_fails_without_opening_target(self):
        fx = FixtureRepo(self)
        path = self._blockers(fx)
        path.unlink()
        path.symlink_to(fx.root / LEGACY_ENV_STORE)
        probe = OpenReadProbe(fx.root).install()
        self.addCleanup(probe.restore)
        with self.assertRaises(migration.MigrationUnavailableError):
            migration.derive_migration_snapshot(fx.root)
        probe.assert_no_legacy_access(self)
        # The symlink was never followed: it is still in place, untouched.
        self.assertTrue(path.is_symlink())

    def test_fifo_blockers_fail_closed_without_blocking(self):
        fx = FixtureRepo(self)
        path = self._blockers(fx)
        path.unlink()
        os.mkfifo(path)
        start = time.monotonic()
        with self.assertRaises(migration.MigrationUnavailableError):
            migration.derive_migration_snapshot(fx.root)
        # O_NONBLOCK must turn the FIFO swap into an immediate rejection.
        self.assertLess(time.monotonic() - start, 5.0)

    def test_duplicate_json_keys_fail_closed(self):
        fx = FixtureRepo(self)
        self._blockers(fx).write_text(
            '{"schema": "ralph-blocked-facts/v1", "facts": ['
            '{"id": "FACT-001", "id": "FACT-002", "title": "dup"}]}',
            encoding="utf-8",
        )
        with self.assertRaises(migration.MigrationUnavailableError) as caught:
            migration.derive_migration_snapshot(fx.root)
        self.assertIn("duplicate", str(caught.exception).lower())

    def test_replacement_after_open_fails_closed(self):
        """A pathname swap between open and read trips the pre-read identity."""
        fx = FixtureRepo(self)
        path = self._blockers(fx)
        real_open = os.open
        swapped = {"done": False}

        def racy_open(target, *args, **kwargs):
            fd = real_open(target, *args, **kwargs)
            if (
                not swapped["done"]
                and kwargs.get("dir_fd") is not None
                and os.fsdecode(target) == "blocked-facts.json"
            ):
                swapped["done"] = True
                path.unlink()
                path.write_text('{"facts": [{"id": "SWAP"}]}', encoding="utf-8")
            return fd

        supported = frozenset(os.supports_dir_fd) | {racy_open}
        with mock.patch.object(os, "open", racy_open), \
                mock.patch.object(os, "supports_dir_fd", supported):
            with self.assertRaises(migration.MigrationUnavailableError) as caught:
                migration.derive_migration_snapshot(fx.root)
        self.assertIn("changed while opening", str(caught.exception))

    def test_replacement_during_read_fails_closed(self):
        """A swap mid-read is caught by the post-read name↔descriptor check."""
        fx = FixtureRepo(self)
        path = self._blockers(fx)
        facts = [
            {
                "id": f"FACT-{i:06d}",
                "title": f"fixture fact {i}",
                "status": "open",
                "requirements": ["MIG-01"],
            }
            for i in range(2000)
        ]
        payload = json.dumps(
            {"schema": "ralph-blocked-facts/v1", "facts": facts}
        ).encode("utf-8")
        self.assertGreater(len(payload), 65536)
        path.write_bytes(payload)
        real_open = os.open
        real_read = os.read
        state = {"blockers_fd": None, "swapped": False}

        def recording_open(target, *args, **kwargs):
            fd = real_open(target, *args, **kwargs)
            if (
                kwargs.get("dir_fd") is not None
                and os.fsdecode(target) == "blocked-facts.json"
            ):
                state["blockers_fd"] = fd
            return fd

        def racing_read(fd, n):
            chunk = real_read(fd, n)
            # Swap only on the anchored blockers descriptor: earlier reads in
            # the process belong to pinned-Git subprocess pipes and must not
            # be mistaken for the sidecar read.
            if (
                not state["swapped"]
                and chunk
                and fd == state["blockers_fd"]
            ):
                state["swapped"] = True
                path.unlink()
                path.write_bytes(b'{"facts": []}')
            return chunk

        supported = frozenset(os.supports_dir_fd) | {recording_open}
        with mock.patch.object(os, "open", recording_open), \
                mock.patch.object(os, "supports_dir_fd", supported), \
                mock.patch.object(os, "read", racing_read):
            with self.assertRaises(migration.MigrationUnavailableError) as caught:
                migration.derive_migration_snapshot(fx.root)
        self.assertIn("changed while reading", str(caught.exception))

    def test_group_writable_mode_fails_closed(self):
        fx = FixtureRepo(self)
        os.chmod(self._blockers(fx), 0o664)
        with self.assertRaises(migration.MigrationUnavailableError):
            migration.derive_migration_snapshot(fx.root)

    def test_hardlinked_blockers_fails_closed(self):
        fx = FixtureRepo(self)
        os.link(self._blockers(fx), fx.root / "docs" / "blockers-hardlink")
        with self.assertRaises(migration.MigrationUnavailableError):
            migration.derive_migration_snapshot(fx.root)

    def test_wrong_owner_fails_closed_deterministically(self):
        """The owner rejection branch runs without chown (private hook)."""
        fx = FixtureRepo(self)
        with self.assertRaises(migration.MigrationUnavailableError) as caught:
            migration._read_blockers(fx.root, _expected_uid=os.getuid() + 1)
        self.assertIn("not a safe owned", str(caught.exception))

    def test_oversized_blockers_fails_closed(self):
        fx = FixtureRepo(self)
        self._blockers(fx).write_bytes(
            b'{"facts": ' + b" " * migration.BLOCKED_FACTS_MAX + b"}"
        )
        with self.assertRaises(migration.MigrationUnavailableError):
            migration.derive_migration_snapshot(fx.root)


# ---------------------------------------------------------------------------
# No runtime task/memory/event/completion imports
# ---------------------------------------------------------------------------


class NoRuntimeImportTest(unittest.TestCase):
    FORBIDDEN_IMPORTS = re.compile(
        r"^(ralph|\.?ralph\.|.*\.events?$|.*\.memory|.*\.task|.*\.completion)"
        r"|(ralph\s*(emit|run|plan|campaign|audit))"
    )

    def _ast_imports(self) -> list[str]:
        source = MIGRATION_SCRIPT.read_text(encoding="utf-8")
        tree = ast.parse(source)
        names: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    names.append(alias.name)
            elif isinstance(node, ast.ImportFrom):
                names.append(node.module or "")
            elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                # Only literal subprocess argv strings: no dynamic exec surface.
                if node.func.attr == "run":
                    for arg in node.args:
                        if isinstance(arg, ast.List):
                            for elt in arg.elts:
                                if isinstance(elt, ast.Constant) and isinstance(
                                    elt.value, str
                                ):
                                    names.append("argv:" + elt.value)
        return names

    def test_migration_module_has_no_runtime_imports(self):
        names = self._ast_imports()
        for name in names:
            if name.startswith("argv:"):
                self.assertNotIn("ralph", name.lower().split("argv:")[1].lower())
            else:
                self.assertFalse(
                    self.FORBIDDEN_IMPORTS.search(name.lower()),
                    f"forbidden import in migration authority: {name}",
                )
        self.assertEqual(
            migration.MIGRATION_SCHEMA, "factory-migration/v1"
        )

    def test_no_import_contract_flags_are_machine_checkable(self):
        fx = FixtureRepo(self)
        snapshot = migration.derive_migration_snapshot(fx.root)
        payload = snapshot.to_dict()
        self.assertEqual(
            payload["no_import"],
            {
                "ralph_imported": False,
                "context_summary_read": False,
                "env_store_read": False,
            },
        )

    def test_loop_package_has_no_legacy_runtime_import(self):
        """No new-path control module imports Ralph task/memory/event/completion."""
        pattern = re.compile(
            r"^\s*(from\s+\S*ralph\S*\s+import|import\s+\S*ralph\S*|"
            r"from\s+\S+\.(events?|tasks?|memor\S*|completion\S*)\s+import)"
        )
        offenders = []
        for path in sorted(LOOP.glob("*.py")):
            if path.name == "__init__.py":
                continue
            for line in path.read_text(encoding="utf-8").splitlines():
                if pattern.search(line):
                    offenders.append(f"{path.name}: {line.strip()}")
        self.assertEqual(offenders, [])


# ---------------------------------------------------------------------------
# Single control-state authority: no-replace, conflict, symlink, mode, owner
# ---------------------------------------------------------------------------


class MigrateAuthorityTest(unittest.TestCase):
    def _migrate_cli(self, fx: FixtureRepo, *extra: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(MIGRATION_SCRIPT), "--root", str(fx.root),
             "migrate", "--campaign-id", "migrated", "--rounds", "2", *extra],
            capture_output=True, text=True,
        )

    def _assert_private_state(self, root: Path) -> None:
        state_file = root / ".factory-state" / STATE_FILE_NAME
        info = os.stat(state_file, follow_symlinks=False)
        self.assertTrue(stat.S_ISREG(info.st_mode))
        self.assertEqual(info.st_uid, os.getuid())
        self.assertEqual(info.st_nlink, 1)
        self.assertEqual(stat.S_IMODE(info.st_mode), 0o600)
        dir_info = os.stat(root / ".factory-state", follow_symlinks=False)
        self.assertTrue(stat.S_ISDIR(dir_info.st_mode))
        self.assertEqual(stat.S_IMODE(dir_info.st_mode) & 0o077, 0)

    def test_migrate_publishes_one_state_with_exact_bindings(self):
        fx = FixtureRepo(self)
        probe = OpenReadProbe(fx.root).install()
        self.addCleanup(probe.restore)
        migration.migrate_control_state(
            fx.root, campaign_id="migrated", rounds_requested=2
        )
        probe.assert_no_legacy_access(self)
        state_file = fx.root / ".factory-state" / STATE_FILE_NAME
        self.assertTrue(state_file.exists())
        self._assert_private_state(fx.root)
        # Exactly one mutable control-state authority exists: the state file
        # plus the digest ledger it owns — never a second state file.
        self.assertEqual(
            list((fx.root / ".factory-state").glob("factory-*.json")),
            [state_file],
        )
        loaded = state_module.load_state(fx.root)
        self.assertEqual(loaded.campaign_id, "migrated")
        self.assertEqual(loaded.rounds_requested, 2)
        self.assertEqual(loaded.current_round, 1)
        self.assertEqual(loaded.current_phase, "planning")
        self.assertEqual(loaded.branch, "boilerplate-develop")
        self.assertEqual(loaded.phase_base_commit, fx.head())
        snapshot = migration.derive_migration_snapshot(fx.root)
        self.assertEqual(loaded.plan_digest, snapshot.plan_digest)
        self.assertEqual(
            loaded.specification_digest,
            hashlib.sha256(
                fx.git_bytes("cat-file", "blob", snapshot.spec_blob)
            ).hexdigest(),
        )
        # The migration published its evidence report alongside the state.
        self.assertTrue((fx.root / ".factory-state" / "migration.json").exists())

    def test_migrate_derives_live_branch_when_unspecified(self):
        fx = FixtureRepo(self)
        migration.migrate_control_state(
            fx.root, campaign_id="migrated", rounds_requested=1
        )
        loaded = state_module.load_state(fx.root)
        self.assertEqual(loaded.branch, "boilerplate-develop")

    def test_migrate_cli_refuses_second_authority(self):
        fx = FixtureRepo(self)
        first = self._migrate_cli(fx)
        self.assertEqual(first.returncode, 0, first.stderr)
        state_file = fx.root / ".factory-state" / STATE_FILE_NAME
        before = state_file.read_bytes()
        second = self._migrate_cli(fx)
        self.assertEqual(second.returncode, 2)
        self.assertIn("refusing to overwrite", second.stderr)
        self.assertEqual(state_file.read_bytes(), before)

    def test_migrate_refuses_pre_existing_state_without_reading_legacy(self):
        fx = FixtureRepo(self, seed_state=True)
        probe = OpenReadProbe(fx.root).install()
        self.addCleanup(probe.restore)
        state_file = fx.root / ".factory-state" / STATE_FILE_NAME
        before = state_file.read_bytes()
        with self.assertRaises(state_module.StateError) as caught:
            migration.migrate_control_state(
                fx.root, campaign_id="migrated", rounds_requested=2
            )
        self.assertIn("refusing to overwrite", str(caught.exception))
        probe.assert_no_legacy_access(self)
        self.assertEqual(state_file.read_bytes(), before)

    def test_migrate_refuses_second_campaign_digest_binding(self):
        fx = FixtureRepo(self, seed_ledger=True)
        result = self._migrate_cli(fx)
        self.assertEqual(result.returncode, 2)
        self.assertIn("second campaign binding", result.stderr)
        self.assertFalse((fx.root / ".factory-state" / STATE_FILE_NAME).exists())
        # The refusal happens without ever reading the legacy store.
        probe = OpenReadProbe(fx.root).install()
        self.addCleanup(probe.restore)
        with self.assertRaises(state_module.StateError):
            migration.migrate_control_state(
                fx.root, campaign_id="migrated", rounds_requested=2
            )
        probe.assert_no_legacy_access(self)

    def test_migrate_refuses_symlinked_state_path(self):
        fx = FixtureRepo(self)
        target = fx.root / "docs" / "SPEC.md"
        state_file = fx.root / ".factory-state" / STATE_FILE_NAME
        state_file.symlink_to(target)
        result = self._migrate_cli(fx)
        self.assertEqual(result.returncode, 2)
        self.assertTrue(state_file.is_symlink())
        # The symlink target is untouched.
        self.assertIn(b"Fixture specification", target.read_bytes())

    def test_tampered_state_mode_fails_closed(self):
        fx = FixtureRepo(self, seed_state=True)
        state_file = fx.root / ".factory-state" / STATE_FILE_NAME
        os.chmod(state_file, 0o666)
        snapshot = migration.derive_migration_snapshot(fx.root)
        self.assertIsNotNone(snapshot.state_error)
        self.assertIsNone(snapshot.state)
        result = self._migrate_cli(fx)
        self.assertEqual(result.returncode, 2)
        # The tampered state fails closed before any write: the CLI reports the
        # control-state failure and never publishes a replacement.
        self.assertIn("control state failed closed", result.stderr)

    def test_owner_tamper_fails_closed(self):
        fx = FixtureRepo(self, seed_state=True)
        gate = state_module.owner_tamper_gate(fx.root)
        if not gate["available"]:
            # The kernel refused the ownership change: the owner check is
            # genuinely unavailable in this process.  Assert the gate declared
            # the unavailability with a fail-closed reason; never claim
            # owner-tamper coverage.
            self.assertTrue(
                gate["reason"], "unavailable owner gate must declare a reason"
            )
            self.assertIn("owner", gate["reason"].lower())
            return
        self.assertIsNone(gate["reason"])
        state_file = fx.root / ".factory-state" / STATE_FILE_NAME
        target = 65534 if os.getuid() != 65534 else 65533
        os.chown(state_file, target, -1)
        snapshot = migration.derive_migration_snapshot(fx.root)
        self.assertIsNotNone(snapshot.state_error)
        self.assertIsNone(snapshot.state)

    def test_migrate_without_optional_branch_derives_live_branch(self):
        fx = FixtureRepo(self)
        result = self._migrate_cli(fx)
        self.assertEqual(result.returncode, 0, result.stderr)


# ---------------------------------------------------------------------------
# Freeze surface
# ---------------------------------------------------------------------------


class FreezeSurfaceTest(unittest.TestCase):
    def _marker_root(self, kind: str):
        """A test-owned root whose freeze marker is ``kind`` (or missing)."""
        tmp = tempfile.TemporaryDirectory(prefix="factory-migration-marker-")
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        (root / ".factory").mkdir(parents=True)
        marker = root / ".factory" / "ralph-freeze"
        if kind == "missing":
            return root
        if kind == "symlink":
            marker.symlink_to(root / "docs")
        elif kind == "fifo":
            os.mkfifo(marker)
        elif kind == "socket":
            sock = socket.socket(socket.AF_UNIX)
            self.addCleanup(sock.close)
            sock.bind(str(marker))
        elif kind == "device":
            os.mknod(str(marker), 0o600 | stat.S_IFCHR, os.makedev(1, 3))
        else:
            raise AssertionError(f"unknown marker kind {kind!r}")
        return root

    def test_frozen_set_matches_launcher_freeze_gates(self):
        self.assertEqual(
            tuple(migration.FROZEN_LAUNCHERS), FROZEN_LAUNCHERS
        )
        for rel in FROZEN_LAUNCHERS:
            text = (ROOT / rel).read_text(encoding="utf-8")
            self.assertIn(".factory/ralph-freeze", text)
            self.assertIn("FACTORY_RALPH_FREEZE_OVERRIDE", text)
            self.assertIn('exit 2', text)

    def test_live_repo_is_frozen(self):
        self.assertTrue(migration.is_ralph_frozen(ROOT))

    def test_freezer_marker_must_be_regular_not_symlink(self):
        marker = ROOT / ".factory" / "ralph-freeze"
        info = marker.lstat()
        self.assertTrue(stat.S_ISREG(info.st_mode))
        self.assertFalse(stat.S_ISLNK(info.st_mode))

    def test_freeze_cli_commands(self):
        status = subprocess.run(
            [sys.executable, str(MIGRATION_SCRIPT), "--root", str(ROOT), "status"],
            capture_output=True, text=True,
        )
        self.assertEqual(status.returncode, 0, status.stderr)
        payload = json.loads(status.stdout)
        self.assertEqual(payload["schema"], "factory-migration/v1")
        self.assertTrue(payload["freeze_active"])
        self.assertEqual(payload["freeze_marker"], ".factory/ralph-freeze")
        self.assertEqual(payload["freeze_override"], "FACTORY_RALPH_FREEZE_OVERRIDE")
        freeze = subprocess.run(
            [sys.executable, str(MIGRATION_SCRIPT), "--root", str(ROOT), "freeze"],
            capture_output=True, text=True,
        )
        self.assertEqual(freeze.returncode, 0, freeze.stderr)
        self.assertEqual(freeze.stdout.strip(), "frozen")

    def test_symlinked_marker_fails_closed(self):
        tmp = tempfile.TemporaryDirectory(prefix="factory-migration-symlink-")
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        (root / ".factory").mkdir(parents=True)
        (root / ".factory" / "ralph-freeze").symlink_to(root / "docs")
        with self.assertRaises(migration.MigrationUnavailableError):
            migration.is_ralph_frozen(root)

    def test_missing_marker_semantics_unchanged(self):
        self.assertFalse(migration.is_ralph_frozen(self._marker_root("missing")))

    def test_fifo_marker_fails_closed(self):
        with self.assertRaises(migration.MigrationUnavailableError):
            migration.is_ralph_frozen(self._marker_root("fifo"))

    def test_socket_marker_fails_closed(self):
        with self.assertRaises(migration.MigrationUnavailableError):
            migration.is_ralph_frozen(self._marker_root("socket"))

    def test_device_marker_fails_closed_or_declares_unavailability(self):
        try:
            root = self._marker_root("device")
        except OSError as exc:
            # mknod of a device node needs root or a device-capable
            # filesystem; declare the exact unavailability and never claim
            # device-node coverage.
            self.assertIn(exc.errno, (errno.EPERM, errno.EACCES, errno.EINVAL))
            return
        with self.assertRaises(migration.MigrationUnavailableError):
            migration.is_ralph_frozen(root)

    def test_freeze_cli_fails_closed_on_unsafe_marker(self):
        root = self._marker_root("fifo")
        result = subprocess.run(
            [sys.executable, str(MIGRATION_SCRIPT), "--root", str(root), "freeze"],
            capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("regular file", result.stderr)

    def test_override_is_exact_in_launchers_and_recovery(self):
        for rel in FROZEN_LAUNCHERS:
            text = (ROOT / rel).read_text(encoding="utf-8")
            self.assertIn("${FACTORY_RALPH_FREEZE_OVERRIDE:-0} != 1", text)
        recover = (ROOT / "scripts" / "ralph-recover.sh").read_text(
            encoding="utf-8"
        )
        self.assertIn("FACTORY_RALPH_FREEZE_OVERRIDE=1", recover)

    def test_docs_name_flat_report_path_and_exact_override(self):
        docs = (ROOT / "docs" / "OPERATIONS.md").read_text(encoding="utf-8")
        self.assertIn(".factory-state/migration.json", docs)
        self.assertNotIn(".factory-state/migration/", docs)
        self.assertIn("FACTORY_RALPH_FREEZE_OVERRIDE=1", docs)


# ---------------------------------------------------------------------------
# Context-summary unwiring and legacy deprecation fail-closed behavior
# ---------------------------------------------------------------------------


class ContextSummaryUnwireTest(unittest.TestCase):
    NEW_PATH_CONTROL = (
        "scripts/final-gate.sh",
        "scripts/git-commit-hook.sh",
        "scripts/ralph-run.sh",
        "scripts/verify-boilerplate.sh",
    )
    # The four control steps that previously invoked the deprecated authority.
    INVOKING_CONTROL = (
        "scripts/final-gate.sh",
        "scripts/git-commit-hook.sh",
        "scripts/ralph-run.sh",
    )

    def test_stale_mirror_absent_from_tracked_tree(self):
        self.assertFalse((ROOT / CONTEXT_SUMMARY).exists())
        tracked = subprocess.run(
            ["git", "-C", str(ROOT), "ls-files", "--error-unmatch", CONTEXT_SUMMARY],
            capture_output=True, text=True,
        )
        self.assertNotEqual(tracked.returncode, 0)

    def test_new_path_control_never_invokes_deprecated_authority(self):
        # Scan the actual control steps for real *invocations* of the
        # deprecated scripts.  ``verify-boilerplate.sh`` itself must name the
        # deprecated paths in its rejection assertions (it is the enforcement,
        # not a caller), so it is checked separately below.
        for rel in self.INVOKING_CONTROL:
            text = (ROOT / rel).read_text(encoding="utf-8")
            for token in ("check-context-summary.py", "ralph-context-summary.py"):
                self.assertNotIn(
                    token, text, f"{rel} still invokes the deprecated {token}"
                )
            self.assertNotIn(
                "test-context-summary.sh", text,
                f"{rel} still invokes the deprecated context-summary suite",
            )

    def test_verifier_enforces_the_unwiring(self):
        text = (ROOT / "scripts" / "verify-boilerplate.sh").read_text(
            encoding="utf-8"
        )
        # The gate names the deprecated paths only in its own rejection
        # assertions and never executes them.
        self.assertIn("check-context-summary", text)
        self.assertIn("ralph-context-summary", text)
        self.assertIn("still wires the deprecated", text)

    def test_verifier_gates_exclude_deprecated_suite(self):
        acceptance = json.loads(
            (ROOT / ".factory" / "verifier-acceptance.json").read_text(encoding="utf-8")
        )
        gates = acceptance["gates"]
        self.assertNotIn(
            {"name": "test-context-summary.sh", "args": []}, gates
        )
        for gate in gates:
            self.assertNotEqual(gate["name"], "test-context-summary.sh")

    def test_deprecated_scripts_fail_closed(self):
        for script in ("scripts/ralph-context-summary.py",
                       "scripts/check-context-summary.py"):
            result = subprocess.run(
                [sys.executable, str(ROOT / script)], capture_output=True, text=True,
            )
            self.assertEqual(result.returncode, 2)
            self.assertIn("DEPRECATED", result.stderr)
            self.assertIn("Task 15 migration", result.stderr)

    def test_deprecated_launcher_marks_itself(self):
        result = subprocess.run(
            [str(ROOT / "tests" / "test-context-summary.sh")],
            capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 0)
        self.assertIn("DEPRECATED", result.stderr)


# ---------------------------------------------------------------------------
# Task 15 F1: per-kind blob bounds fail closed before any body byte is read
# ---------------------------------------------------------------------------


class BlobBoundHardeningTest(unittest.TestCase):
    """Every migration blob — plan, specification, each role prompt, and the
    audit-objectives registry — is exact-size-pre-checked against its
    per-kind cap *before* any body byte is read (strict 40-hex ID, bounded
    no-replace read).  An oversized blob fails the whole translation and the
    ``git cat-file blob`` body read is never issued for it."""

    def _recording_bytes(self, recorded):
        real = migration._git_bytes

        def recording(root, argv, *, maximum=None):
            recorded.append(list(argv))
            return real(root, argv, maximum=maximum)

        return recording

    def _assert_no_body_read(self, recorded, blob_id):
        self.assertNotIn(
            ["cat-file", "blob", blob_id], recorded,
            "the blob body was read before the exact-size per-kind pre-check",
        )

    def test_oversized_plan_fails_before_body_read(self):
        fx = FixtureRepo(self, oversize={
            ".factory/artifacts/implementation-plan.md":
                PLAN_TEMPLATE.encode("utf-8")
                + b"#" * (migration.PLAN_BLOB_MAX + 1),
        })
        recorded = []
        with mock.patch.object(
            migration, "_git_bytes", side_effect=self._recording_bytes(recorded)
        ):
            with self.assertRaises(migration.MigrationUnavailableError) as caught:
                migration.derive_migration_snapshot(fx.root)
        self.assertIn("per-kind cap", str(caught.exception))
        plan_blob = fx.git(
            "rev-parse", "HEAD:.factory/artifacts/implementation-plan.md"
        ).strip()
        self.assertTrue(re.fullmatch(r"[0-9a-f]{40}", plan_blob))
        self._assert_no_body_read(recorded, plan_blob)

    def test_oversized_spec_fails_before_body_read(self):
        fx = FixtureRepo(self, oversize={
            "docs/SPEC.md": b"# oversized specification\n"
            + b"s" * migration.SPEC_BLOB_MAX,
        })
        recorded = []
        with mock.patch.object(
            migration, "_git_bytes", side_effect=self._recording_bytes(recorded)
        ):
            with self.assertRaises(migration.MigrationUnavailableError) as caught:
                migration.migrate_control_state(
                    fx.root, campaign_id="migrated", rounds_requested=2
                )
        self.assertIn("per-kind cap", str(caught.exception))
        spec_blob = fx.git("rev-parse", "HEAD:docs/SPEC.md").strip()
        self._assert_no_body_read(recorded, spec_blob)

    def test_oversized_role_prompt_fails_before_body_read(self):
        for role in ("planner", "developer", "tester", "auditor"):
            with self.subTest(role=role):
                fx = FixtureRepo(self, oversize={
                    f".factory/prompts/{role}.md":
                        b"# oversized role prompt\n"
                        + b"p" * migration.ROLE_PROMPT_MAX,
                })
                recorded = []
                with mock.patch.object(
                    migration, "_git_bytes", side_effect=self._recording_bytes(recorded)
                ):
                    with self.assertRaises(
                        migration.MigrationUnavailableError
                    ) as caught:
                        migration.migrate_control_state(
                            fx.root, campaign_id="migrated", rounds_requested=1
                        )
                self.assertIn("per-kind cap", str(caught.exception))
                blob = fx.git(
                    "rev-parse", f"HEAD:.factory/prompts/{role}.md"
                ).strip()
                self.assertTrue(re.fullmatch(r"[0-9a-f]{40}", blob))
                self._assert_no_body_read(recorded, blob)

    def test_oversized_audit_registry_fails_before_body_read(self):
        fx = FixtureRepo(self, oversize={
            ".factory/audit-objectives/registry.json":
                b'{"schema": "audit-objectives/v1"}\n'
                + b"a" * migration.AUDIT_OBJECTIVES_MAX,
        })
        recorded = []
        with mock.patch.object(
            migration, "_git_bytes", side_effect=self._recording_bytes(recorded)
        ):
            with self.assertRaises(migration.MigrationUnavailableError) as caught:
                migration.migrate_control_state(
                    fx.root, campaign_id="migrated", rounds_requested=1
                )
        self.assertIn("per-kind cap", str(caught.exception))
        audit_blob = fx.git(
            "rev-parse", "HEAD:.factory/audit-objectives/registry.json"
        ).strip()
        self.assertTrue(re.fullmatch(r"[0-9a-f]{40}", audit_blob))
        self._assert_no_body_read(recorded, audit_blob)

    def test_migrate_digests_exactly_match_the_committed_blobs(self):
        """The bounded reads feed the exact committed bytes into the digests."""
        fx = FixtureRepo(self)
        migration.migrate_control_state(
            fx.root, campaign_id="migrated", rounds_requested=2
        )
        loaded = state_module.load_state(fx.root)
        snapshot = migration.derive_migration_snapshot(fx.root)
        self.assertEqual(
            loaded.specification_digest,
            hashlib.sha256(
                fx.git_bytes("cat-file", "blob", snapshot.spec_blob)
            ).hexdigest(),
        )
        for role in ("planner", "developer", "tester", "auditor"):
            blob = fx.git(
                "rev-parse", f"HEAD:.factory/prompts/{role}.md"
            ).strip()
            self.assertEqual(
                loaded.role_prompt_digests[role],
                hashlib.sha256(fx.git_bytes("cat-file", "blob", blob)).hexdigest(),
            )
        audit_blob = fx.git(
            "rev-parse", "HEAD:.factory/audit-objectives/registry.json"
        ).strip()
        self.assertEqual(
            loaded.audit_objectives_digest,
            hashlib.sha256(
                fx.git_bytes("cat-file", "blob", audit_blob)
            ).hexdigest(),
        )


# ---------------------------------------------------------------------------
# Bounded Git byte capture: fair drain, group termination, no zombie
# ---------------------------------------------------------------------------


class GitBytesBoundedTests(unittest.TestCase):
    """``gitutil.git_bytes_bounded`` never deadlocks (stdout/stderr are
    drained fairly against one shared deadline), never captures an over-bound
    stream, terminates and reaps the child's *entire* process group on a
    wedged capture, and leaves no zombie behind."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="factory-gitbounded-")
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)

    def _script(self, name: str, body: str) -> Path:
        path = self.dir / name
        path.write_text("#!/usr/bin/env python3\n" + body, encoding="utf-8")
        path.chmod(0o755)
        return path

    def test_fair_drain_reads_stderr_flood_without_deadlock(self):
        """A child that floods stderr while stdout stalls must be drained
        fairly: a sequential stdout-then-stderr drain wedges on the full
        stderr pipe and fails spuriously, the fair drain completes."""
        flood = self._script(
            "flood-git",
            "import os, sys\n"
            "for _ in range(256):\n"
            "    os.write(2, b's' * 512)\n"
            "sys.stdout.write('stdout-ok\\n')\n"
            "sys.stdout.flush()\n",
        )
        with mock.patch.object(gitutil, "GIT_EXECUTABLE", str(flood)):
            started = time.monotonic()
            result = gitutil.git_bytes_bounded(
                ["cat-file", "blob", "x"],
                maximum=1024 * 1024,
                timeout=10.0,
            )
            elapsed = time.monotonic() - started
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, b"stdout-ok\n")
        self.assertEqual(len(result.stderr), 256 * 512)
        self.assertLess(
            elapsed, 8.0, "fair drain must complete without a spurious timeout"
        )

    def test_oversized_stdout_fails_closed_bounded(self):
        fx = FixtureRepo(self)
        (fx.root / "big.txt").write_bytes(b"x" * 65536)
        fx.git("add", "big.txt")
        fx.git(
            "-c", "user.name=t", "-c", "user.email=t@t",
            "commit", "-qm", "big blob",
        )
        blob = fx.git("rev-parse", "HEAD:big.txt").strip()
        started = time.monotonic()
        with self.assertRaises(gitutil.GitBoundaryError) as caught:
            gitutil.git_bytes_bounded(
                ["-C", str(fx.root), "cat-file", "blob", blob],
                maximum=8192,
            )
        self.assertIn("wrote more than", str(caught.exception))
        self.assertLess(time.monotonic() - started, 10.0)

    def test_bounded_timeout_kills_group_reaps_no_zombie(self):
        """A TERM-ignoring wedged capture is bounded, kills the child's whole
        process group (grandchild included), reaps the leader, and leaves no
        zombie of the caller."""
        marker = self.dir / "wedge.pid"
        grand = self.dir / "wedge-grandchild.pid"
        wedge = self._script(
            "wedge-git",
            "import os, signal, sys, time\n"
            "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
            f"with open({str(marker)!r}, 'w', encoding='utf-8') as stream:\n"
            "    stream.write(str(os.getpid()))\n"
            "pid = os.fork()\n"
            "if pid == 0:\n"
            f"    with open({str(grand)!r}, 'w', encoding='utf-8') as stream:\n"
            "        stream.write(str(os.getpid()))\n"
            "    time.sleep(300)\n"
            "    os._exit(0)\n"
            "os.waitpid(pid, 0)\n"
            "time.sleep(300)\n"
            "os._exit(0)\n",
        )
        with mock.patch.object(gitutil, "GIT_EXECUTABLE", str(wedge)):
            started = time.monotonic()
            with self.assertRaises(gitutil.GitBoundaryError):
                gitutil.git_bytes_bounded(
                    ["anything"], maximum=1024, timeout=1.0
                )
            elapsed = time.monotonic() - started
        self.assertLess(
            elapsed, 12.0, "bounded group termination must stay bounded"
        )
        leader = int(marker.read_text())
        grandchild = int(grand.read_text())
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if not os.path.exists(f"/proc/{grandchild}") and not os.path.exists(
                f"/proc/{leader}"
            ):
                break
            time.sleep(0.02)
        self.assertFalse(
            os.path.exists(f"/proc/{grandchild}"),
            "a process-group member survived the bounded termination",
        )
        self.assertFalse(
            os.path.exists(f"/proc/{leader}"),
            "the wedged child was left as a zombie",
        )


# ---------------------------------------------------------------------------
# F4: evidence directory/artifact owner/mode/link metadata fails closed
# ---------------------------------------------------------------------------


class EvidenceMetadataHardeningTest(unittest.TestCase):
    """Evidence enumeration fails closed on an unsafe private directory or
    artifact: wrong owner, wrong mode, a symlink, or an extra hard link can
    never be blessed as evidence metadata."""

    def test_evidence_dir_mode_tamper_fails_closed(self):
        fx = FixtureRepo(self)
        os.chmod(fx.root / ".factory-state", 0o755)
        with self.assertRaises(migration.MigrationUnavailableError) as caught:
            migration.derive_migration_snapshot(fx.root)
        self.assertIn("0700", str(caught.exception))

    def test_evidence_dir_symlink_fails_closed(self):
        fx = FixtureRepo(self)
        private = fx.root / ".factory-state"
        backup = fx.root / "state-dir-backup"
        os.rename(private, backup)
        private.symlink_to(backup)
        with self.assertRaises(migration.MigrationUnavailableError):
            migration.derive_migration_snapshot(fx.root)

    def test_evidence_dir_foreign_owner_fails_closed_deterministically(self):
        """The owner-rejection branch runs without chown (private hook)."""
        fx = FixtureRepo(self)
        with self.assertRaises(migration.MigrationUnavailableError) as caught:
            migration._evidence_entries(fx.root, _expected_uid=os.getuid() + 1)
        self.assertIn("owned", str(caught.exception))

    def test_evidence_artifact_hardlink_fails_closed(self):
        fx = FixtureRepo(self)
        os.link(
            fx.root / ".factory-state" / "audit-receipts" / "a-000001.json",
            fx.root / "docs" / "evidence-hardlink.json",
        )
        with self.assertRaises(migration.MigrationUnavailableError) as caught:
            migration.derive_migration_snapshot(fx.root)
        self.assertIn("single-link", str(caught.exception))

    def test_evidence_artifact_group_writable_fails_closed(self):
        fx = FixtureRepo(self)
        os.chmod(fx.root / ".factory-state" / "audit-receipts" / "a-000001.json", 0o664)
        with self.assertRaises(migration.MigrationUnavailableError) as caught:
            migration.derive_migration_snapshot(fx.root)
        self.assertIn("safe owned", str(caught.exception))

    def test_evidence_artifact_foreign_owner_fails_closed(self):
        fx = FixtureRepo(self)
        gate = state_module.owner_tamper_gate(fx.root)
        if not gate["available"]:
            # The kernel refused the ownership change: the owner check is
            # genuinely unavailable in this process.  Assert the gate
            # declared the unavailability with a fail-closed reason; never
            # claim owner-tamper coverage.
            self.assertTrue(gate["reason"])
            self.assertIn("owner", gate["reason"].lower())
            return
        self.assertIsNone(gate["reason"])
        target = 65534 if os.getuid() != 65534 else 65533
        os.chown(
            fx.root / ".factory-state" / "audit-receipts" / "a-000001.json",
            target, -1,
        )
        with self.assertRaises(migration.MigrationUnavailableError) as caught:
            migration.derive_migration_snapshot(fx.root)
        self.assertIn("not a safe owned", str(caught.exception))


# --------------------------------------------------------------------------
# F2: every state descriptor is close-on-exec (state fd non-inheritance)
# --------------------------------------------------------------------------


class StateIoCloexecTest(unittest.TestCase):
    """The private state authority opens every directory/marker/temporary
    descriptor with ``O_CLOEXEC`` and the primitive gate requires it, so a
    state descriptor can never be inherited into a spawned or exec'd child
    (even one launched with ``close_fds=False``)."""

    def _seeded(self):
        fx = FixtureRepo(self)
        state_module.init_state(
            fx.root,
            campaign_id="cloexec",
            rounds_requested=1,
            specification_digest="a" * 64,
            plan_digest="b" * 64,
            role_prompt_digests={"planner": "c" * 64},
            audit_objectives_digest="d" * 64,
            phase_base_commit="f" * 40,
            branch="boilerplate-develop",
            now=1_700_000_000_000_000_000,
        )
        return fx

    def test_every_state_io_open_carries_o_cloexec(self):
        fx = self._seeded()
        fio = state_module._fio
        recorded: list = []
        real_open = os.open

        def recording_open(target, flags, *args, **kwargs):
            recorded.append((os.fsdecode(target), flags))
            return real_open(target, flags, *args, **kwargs)

        supported = frozenset(os.supports_dir_fd) | {recording_open}
        with mock.patch.object(os, "open", recording_open), \
                mock.patch.object(os, "supports_dir_fd", supported):
            with fio.state_dir(fx.root, create=False):
                fio.read_json(fx.root, STATE_FILE_NAME)
                fio.atomic_write_json(fx.root, "marker-io.json", {"k": 1})
                fio.read_json(fx.root, "marker-io.json")
                fio.remove(fx.root, "marker-io.json")
                state_module.record_phase_digest(fx.root, "planning")
                state_module.read_phase_digest_ledger(fx.root)
        self.assertTrue(recorded)
        for name, flags in recorded:
            self.assertTrue(
                flags & getattr(os, "O_CLOEXEC", 0),
                f"state open of {name!r} lacks O_CLOEXEC (flags={oct(flags)})",
            )

    def test_primitive_gate_requires_o_cloexec(self):
        fio = state_module._fio
        fio.require_linux_primitives()
        source = inspect.getsource(fio.require_linux_primitives)
        self.assertIn("O_CLOEXEC", source)

    def test_state_dir_fd_is_not_inherited_across_exec(self):
        """A real exec probe launched with ``close_fds=False`` must not see
        the open ``.factory-state`` directory descriptor in ``/proc/self/fd``
        (the kernel drops it because every descriptor is close-on-exec)."""
        fx = self._seeded()
        fio = state_module._fio
        probe = fx.root / "fdscan.py"
        probe.write_text(
            "import os, sys\n"
            "root = sys.argv[1]\n"
            "target = os.stat(os.path.join(root, '.factory-state'), "
            "follow_symlinks=False)\n"
            "found = []\n"
            "for item in os.listdir('/proc/self/fd'):\n"
            "    try:\n"
            "        info = os.stat('/proc/self/fd/' + item)\n"
            "    except OSError:\n"
            "        continue\n"
            "    if (info.st_dev, info.st_ino) == "
            "(target.st_dev, target.st_ino):\n"
            "        found.append(item)\n"
            "print('clean' if not found else 'inherited:' + ','.join(found))\n",
            encoding="utf-8",
        )
        with fio.state_dir(fx.root, create=False) as directory_fd:
            self.assertIsInstance(directory_fd, int)
            result = subprocess.run(
                [sys.executable, str(probe), str(fx.root)],
                capture_output=True, text=True, close_fds=False,
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            result.stdout.strip(), "clean",
            "a state directory descriptor leaked across exec",
        )


# ---------------------------------------------------------------------------
# Live-repo smoke: the derive CLI runs against the real repository
# ---------------------------------------------------------------------------


class LiveRepoSmokeTest(unittest.TestCase):
    def test_live_repo_derive_is_read_only(self):
        result = subprocess.run(
            [sys.executable, str(MIGRATION_SCRIPT), "--root", str(ROOT),
             "derive", "--no-report"],
            capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["schema"], "factory-migration/v1")
        self.assertFalse(payload["no_import"]["ralph_imported"])
        self.assertFalse(payload["no_import"]["context_summary_read"])
        self.assertFalse(payload["no_import"]["env_store_read"])
        # The live tree was not rewritten by a read-only derivation.
        self.assertTrue((ROOT / ".factory" / "ralph-freeze").is_file())
        self.assertFalse((ROOT / ".factory-state" / STATE_FILE_NAME).exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
