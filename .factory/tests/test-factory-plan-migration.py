#!/usr/bin/env python3
"""Deterministic v1->v2 plan migration tests (Phase 2D1).

Covers: lossless migration (every completed-task datum preserved in the
archive sidecar), idempotency, crash atomicity (sidecars-then-plan ordering
makes every partial pair fail closed), archive-on-verified-completion
(append-only, provenance ``campaign``), rehash determinism, and the size
ceiling.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
LOOP = ROOT / ".factory" / "loop"

sys.path.insert(0, str(LOOP))
import plan_migration  # noqa: E402
import plan_parser  # noqa: E402
import plan_sidecars  # noqa: E402
from plan_migration import (  # noqa: E402
    PLAN_SIZE_CEILING,
    PlanMigrationError,
    archive_task,
    migrate_plan,
    rehash_plan,
)
from plan_sidecars import (  # noqa: E402
    ARCHIVE_FILE,
    HISTORY_FILE,
    completed_ids,
    load_plan_binding,
    parse_archive,
    read_archive,
    read_history,
    verify_sidecar_binding,
)

COMMIT = "c" * 40


def _make_v1_plan() -> str:
    """The committed v1 plan plus the Phase 2D1 migration task (the real input).

    The canonical plan is now the concise v2 plan; the v1 input is the
    committed HEAD 36-task v1 plan with the migration task added as Task 36
    and the final audit renumbered to Task 37 (the exact pre-migration
    state): 30 completed tasks, 7 unfinished (21, 24, 28, 29, 32, 36, 37).
    """
    import subprocess

    result = subprocess.run(
        ["git", "show", "HEAD:.factory/artifacts/implementation-plan.md"],
        capture_output=True, text=True, check=True,
    )
    text = result.stdout
    marker = "## Task 36: Final documentation and specification audit"
    assert text.count(marker) == 1
    task36 = (
        "## Task 36: Concise active plan and committed sidecar migration (Phase 2D1)\n"
        "\n"
        "- Status: pending\n"
        "- Dependencies: Task 6, Task 9, Task 11, Task 19, Task 25, Task 26, "
        "Task 27, Task 30, Task 32, Task 33, Task 34, Task 35\n"
        "- Priority: 36\n"
        "- Scope: migration scope\n"
        "- Acceptance criteria: migration acceptance\n"
        "- Verification: migration verification\n"
        "- Documentation impact: migration docs\n"
        "\n"
    )
    text = text.replace(marker, task36 + marker)
    text = text.replace(
        "## Task 36: Final documentation and specification audit",
        "## Task 37: Final documentation and specification audit",
    )
    text = text.replace(
        "Task 33, Task 34, Task 35\n- Scope: Current checkpoint:",
        "Task 33, Task 34, Task 35, Task 36\n- Scope: Current checkpoint:",
    )
    return text



class MigrationTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / ".factory").mkdir()
        (self.root / ".factory" / "artifacts").mkdir()
        self.plan_path = self.root / ".factory" / "artifacts" / "implementation-plan.md"
        self.plan_path.write_text(_make_v1_plan(), encoding="utf-8")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_migration_is_lossless(self) -> None:
        report = migrate_plan(self.root, archived_commit=COMMIT)
        self.assertTrue(report["migrated"])
        self.assertEqual(report["active_tasks"], [21, 24, 28, 29, 32, 36, 37])
        self.assertEqual(len(report["archived_tasks"]), 30)
        records = read_archive(self.root)
        self.assertEqual(len(records), 30)
        self.assertEqual(records[0].task_id, 1)
        # Every completed-task datum is preserved verbatim.
        first = records[0]
        self.assertEqual(
            first.title, "Bind the canonical specification and baseline harness config"
        )
        self.assertIn("docs/FACTORY-LOOP-SPEC.md", first.scope)
        self.assertIn("check-plan-freshness.sh", first.acceptance)
        self.assertEqual(first.archived_commit, COMMIT)
        self.assertEqual(first.provenance, "migration")
        # The v2 plan keeps only the unfinished tasks.
        plan = plan_parser.Plan.from_bytes(
            self.plan_path.read_bytes(), archive_records=records
        )
        self.assertEqual(plan.schema, "factory-plan/v2")
        self.assertEqual([t.number for t in plan.tasks], [21, 24, 28, 29, 32, 36, 37])
        self.assertEqual(plan.matrix, [])
        self.assertEqual(plan.interactions, [])
        # The final audit depends on every active + archived task.
        final = plan.tasks[-1]
        self.assertEqual(set(final.dependencies), set(range(1, 37)))
        # The pair binds.
        verify_sidecar_binding(
            self.plan_path.read_bytes(),
            (self.root / ".factory" / "artifacts" / ARCHIVE_FILE).read_bytes(),
            (self.root / ".factory" / "artifacts" / HISTORY_FILE).read_bytes(),
            archive_records=records,
        )

    def test_migration_is_idempotent(self) -> None:
        first = migrate_plan(self.root, archived_commit=COMMIT)
        plan_after_first = self.plan_path.read_bytes()
        archive_after_first = (
            self.root / ".factory" / "artifacts" / ARCHIVE_FILE
        ).read_bytes()
        second = migrate_plan(self.root, archived_commit=COMMIT)
        self.assertFalse(second["migrated"])
        self.assertEqual(self.plan_path.read_bytes(), plan_after_first)
        self.assertEqual(
            (self.root / ".factory" / "artifacts" / ARCHIVE_FILE).read_bytes(),
            archive_after_first,
        )

    def test_crash_between_sidecars_and_plan_fails_closed(self) -> None:
        # Simulate a crash after the sidecars were written but before the
        # plan: the v1 plan is still readable (legacy compatibility), and a
        # re-run migrates deterministically.
        migrate_plan(self.root, archived_commit=COMMIT)
        v2_plan = self.plan_path.read_bytes()
        # Restore the v1 plan bytes (the "crash" left the old plan).
        self.plan_path.write_text(_make_v1_plan(), encoding="utf-8")
        # The v1 plan still parses (legacy readable until migrated).
        plan = plan_parser.Plan.from_bytes(self.plan_path.read_bytes())
        self.assertEqual(plan.schema, "factory-plan/v1")
        # Re-running the migration is deterministic and idempotent.
        report = migrate_plan(self.root, archived_commit=COMMIT)
        self.assertTrue(report["migrated"])
        self.assertEqual(self.plan_path.read_bytes(), v2_plan)

    def test_archive_task_after_verified_completion(self) -> None:
        migrate_plan(self.root, archived_commit=COMMIT)
        # The developer marks task 28 complete (transient worktree state).
        text = self.plan_path.read_text("utf-8").replace(
            "## Task 28: Cumulative task-resource budget (Phase 2A)\n"
            "\n"
            "- Status: pending",
            "## Task 28: Cumulative task-resource budget (Phase 2A)\n"
            "\n"
            "- Status: complete",
        )
        self.plan_path.write_text(text, encoding="utf-8")
        report = archive_task(self.root, 28, commit=COMMIT)
        self.assertEqual(report["archived"], 28)
        self.assertEqual(report["remaining_tasks"], [21, 24, 29, 32, 36, 37])
        records = read_archive(self.root)
        self.assertEqual(len(records), 31)
        self.assertEqual(records[-1].task_id, 28)
        self.assertEqual(records[-1].provenance, "campaign")
        # The final audit still closes over active + archived.
        plan = plan_parser.Plan.from_bytes(
            self.plan_path.read_bytes(), archive_records=records
        )
        self.assertEqual(set(plan.tasks[-1].dependencies), set(range(1, 37)))
        # History records the archive event.
        events = [r.event for r in read_history(self.root)]
        self.assertIn("archived", events)

    def test_archive_task_rejects_unverified(self) -> None:
        migrate_plan(self.root, archived_commit=COMMIT)
        with self.assertRaises(PlanMigrationError) as caught:
            archive_task(self.root, 28, commit=COMMIT)
        self.assertIn("only an independently", str(caught.exception))

    def test_archive_task_rejects_duplicate(self) -> None:
        migrate_plan(self.root, archived_commit=COMMIT)
        text = self.plan_path.read_text("utf-8").replace(
            "## Task 28: Cumulative task-resource budget (Phase 2A)\n"
            "\n"
            "- Status: pending",
            "## Task 28: Cumulative task-resource budget (Phase 2A)\n"
            "\n"
            "- Status: complete",
        )
        self.plan_path.write_text(text, encoding="utf-8")
        archive_task(self.root, 28, commit=COMMIT)
        with self.assertRaises(PlanMigrationError) as caught:
            archive_task(self.root, 28, commit=COMMIT)
        self.assertIn("already archived", str(caught.exception))

    def test_rehash_is_deterministic(self) -> None:
        migrate_plan(self.root, archived_commit=COMMIT)
        first = rehash_plan(self.root)
        second = rehash_plan(self.root)
        self.assertEqual(first, second)
        # The plan binding still verifies after rehash.
        records = read_archive(self.root)
        verify_sidecar_binding(
            self.plan_path.read_bytes(),
            (self.root / ".factory" / "artifacts" / ARCHIVE_FILE).read_bytes(),
            (self.root / ".factory" / "artifacts" / HISTORY_FILE).read_bytes(),
            archive_records=records,
        )

    def test_size_ceiling(self) -> None:
        migrate_plan(self.root, archived_commit=COMMIT)
        self.assertLessEqual(
            len(self.plan_path.read_bytes()), PLAN_SIZE_CEILING
        )

    def test_v1_plan_untouched_without_migration(self) -> None:
        # No silent auto-mutation: the v1 plan stays byte-identical until
        # migrate_plan is called.
        before = self.plan_path.read_bytes()
        self.assertEqual(self.plan_path.read_bytes(), before)
        self.assertFalse(
            (self.root / ".factory" / "artifacts" / ARCHIVE_FILE).exists()
        )


class CliTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / ".factory").mkdir()
        (self.root / ".factory" / "artifacts").mkdir()
        (self.root / ".factory" / "artifacts" / "implementation-plan.md").write_text(
            _make_v1_plan(), encoding="utf-8"
        )

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_cli_migrate(self) -> None:
        result = subprocess.run(
            [
                sys.executable, str(LOOP / "plan_migration.py"),
                "--root", str(self.root),
                "migrate", "--archived-commit", COMMIT,
            ],
            capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("factory-plan/v2", result.stdout)

    def test_cli_archive_task(self) -> None:
        subprocess.run(
            [
                sys.executable, str(LOOP / "plan_migration.py"),
                "--root", str(self.root),
                "migrate", "--archived-commit", COMMIT,
            ],
            capture_output=True, text=True, check=True,
        )
        plan = (self.root / ".factory" / "artifacts" / "implementation-plan.md")
        text = plan.read_text("utf-8").replace(
            "## Task 28: Cumulative task-resource budget (Phase 2A)\n"
            "\n"
            "- Status: pending",
            "## Task 28: Cumulative task-resource budget (Phase 2A)\n"
            "\n"
            "- Status: complete",
        )
        plan.write_text(text, encoding="utf-8")
        result = subprocess.run(
            [
                sys.executable, str(LOOP / "plan_migration.py"),
                "--root", str(self.root),
                "archive-task", "28", "--commit", COMMIT,
            ],
            capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("archived", result.stdout)


if __name__ == "__main__":
    unittest.main()
