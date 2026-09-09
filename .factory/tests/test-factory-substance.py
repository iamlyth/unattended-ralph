#!/usr/bin/env python3
"""Commit-substance classifier tests (Phase 2D1).

Covers the shared administrative-only set, the semantic plan-change
classifier (task add/remove/reorder, title/priority/dependencies/Scope/
Acceptance/blocker/latest-failure edits are meaningful; status/evidence/
prose edits are not), and the fail-closed commit classification.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
LOOP = ROOT / ".factory" / "loop"

sys.path.insert(0, str(LOOP))
import plan_sidecars  # noqa: E402
from substance import (  # noqa: E402
    ADMINISTRATIVE_PATHS,
    SubstanceError,
    classify_commit,
    is_administrative_only,
    plan_change_is_semantic,
)

CANONICAL = ROOT / ".factory" / "artifacts" / "implementation-plan.md"


def canonical_archive_records():
    sidecar = ROOT / ".factory" / "artifacts" / plan_sidecars.ARCHIVE_FILE
    if not sidecar.is_file():
        return None
    return plan_sidecars.parse_archive(sidecar.read_bytes())


class AdministrativeSetTest(unittest.TestCase):
    def test_plan_and_sidecars_are_administrative(self) -> None:
        self.assertTrue(
            is_administrative_only([
                ".factory/artifacts/implementation-plan.md",
                ".factory/artifacts/plan-archive.jsonl",
                ".factory/artifacts/plan-history.jsonl",
            ])
        )

    def test_bug_ledgers_are_administrative(self) -> None:
        self.assertTrue(
            is_administrative_only([
                ".factory/bugs/open.md", ".factory/bugs/closed.md",
            ])
        )

    def test_audit_sidecars_are_administrative(self) -> None:
        self.assertTrue(
            is_administrative_only([
                ".factory/artifacts/campaign-audit.md",
                ".factory/artifacts/conformance.json",
            ])
        )

    def test_product_code_is_substantive(self) -> None:
        self.assertFalse(
            is_administrative_only([
                ".factory/artifacts/implementation-plan.md", "src/main.c",
            ])
        )

    def test_empty_set_is_not_administrative(self) -> None:
        self.assertFalse(is_administrative_only([]))

    def test_every_admin_path_is_known(self) -> None:
        self.assertIn(".factory/artifacts/implementation-plan.md", ADMINISTRATIVE_PATHS)
        self.assertIn(".factory/artifacts/plan-archive.jsonl", ADMINISTRATIVE_PATHS)
        self.assertIn(".factory/artifacts/plan-history.jsonl", ADMINISTRATIVE_PATHS)


class SemanticChangeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.v1 = CANONICAL.read_bytes()
        self.records = canonical_archive_records()

    def _semantic(self, old_bytes, new_bytes) -> bool:
        return plan_change_is_semantic(
            old_bytes, new_bytes, archive_records=self.records
        )

    def test_identical_is_not_semantic(self) -> None:
        self.assertFalse(self._semantic(self.v1, self.v1))

    def test_title_edit_is_semantic(self) -> None:
        changed = self.v1.replace(
            b"## Task 21: Round-1 campaign audit objective coverage",
            b"## Task 21: A different title",
        )
        self.assertTrue(self._semantic(self.v1, changed))

    def test_priority_edit_is_semantic(self) -> None:
        changed = self.v1.replace(
            b"- Priority: 36\n- Scope: Introduce the generic concise",
            b"- Priority: 2\n- Scope: Introduce the generic concise",
        )
        self.assertTrue(self._semantic(self.v1, changed))

    def test_scope_edit_is_semantic(self) -> None:
        changed = self.v1.replace(
            b"- Scope: Introduce the generic concise active-plan format",
            b"- Scope: Something entirely different",
        )
        self.assertTrue(self._semantic(self.v1, changed))

    def test_status_edit_is_not_semantic(self) -> None:
        changed = self.v1.replace(
            b"- Status: pending\n- Dependencies: Task 6, Task 9, Task 11, Task 19,",
            b"- Status: in_progress\n- Dependencies: Task 6, Task 9, Task 11, Task 19,",
        )
        self.assertFalse(self._semantic(self.v1, changed))

    def test_evidence_edit_is_not_semantic(self) -> None:
        # The v2 plan has no Evidence field; a prose edit in a non-semantic
        # field (Verification) is not meaningful.
        changed = self.v1.replace(
            b"- Verification: `.factory/tests/test-factory-plan-sidecars.py`",
            b"- Verification: `.factory/tests/test-factory-plan-sidecars.py` (revised)",
        )
        self.assertFalse(self._semantic(self.v1, changed))

    def test_prose_edit_is_not_semantic(self) -> None:
        changed = self.v1.replace(
            b"Goal: ",
            b"Goal: (revised prose) ",
        )
        self.assertFalse(self._semantic(self.v1, changed))

    def test_spec_commit_sync_is_semantic(self) -> None:
        # A plan-contract binding sync (spec_commit) is a genuine plan edit:
        # the freshness checker independently validates the recorded binding.
        import re

        match = re.search(rb"spec_commit: ([0-9a-f]{40})", self.v1)
        self.assertIsNotNone(match)
        changed = self.v1.replace(
            match.group(0),
            b"spec_commit: " + b"f" * 40,
        )
        self.assertTrue(self._semantic(self.v1, changed))

    def test_unparsable_plan_fails_closed(self) -> None:
        with self.assertRaises(SubstanceError):
            plan_change_is_semantic(
                self.v1, b"---\nnot a plan\n---\n",
                archive_records=self.records,
            )


class ClassifyCommitTest(unittest.TestCase):
    def setUp(self) -> None:
        self.v1 = CANONICAL.read_bytes()
        self.records = canonical_archive_records()

    def test_substantive_commit_allowed(self) -> None:
        allowed, reason = classify_commit(
            ["src/main.c"], self.v1, self.v1
        )
        self.assertTrue(allowed)
        self.assertIn("substantive", reason)

    def test_admin_commit_with_semantic_change_allowed(self) -> None:
        changed = self.v1.replace(
            b"## Task 21: Round-1 campaign audit objective coverage",
            b"## Task 21: A different title",
        )
        allowed, reason = classify_commit(
            [".factory/artifacts/implementation-plan.md"], self.v1, changed,
            archive_records=self.records,
        )
        self.assertTrue(allowed)
        self.assertIn("semantic planning change", reason)

    def test_admin_commit_with_status_only_change_rejected(self) -> None:
        changed = self.v1.replace(
            b"- Status: pending\n- Dependencies: Task 6, Task 9, Task 11, Task 19,",
            b"- Status: in_progress\n- Dependencies: Task 6, Task 9, Task 11, Task 19,",
        )
        allowed, reason = classify_commit(
            [".factory/artifacts/implementation-plan.md"], self.v1, changed,
            archive_records=self.records,
        )
        self.assertFalse(allowed)
        self.assertIn("metadata-only", reason)

    def test_admin_commit_without_plan_change_rejected(self) -> None:
        allowed, reason = classify_commit(
            [".factory/artifacts/plan-archive.jsonl"], self.v1, self.v1
        )
        self.assertFalse(allowed)
        self.assertIn("no plan change", reason)

    def test_empty_staged_set_rejected(self) -> None:
        allowed, _reason = classify_commit([], self.v1, self.v1)
        self.assertFalse(allowed)


if __name__ == "__main__":
    unittest.main()
