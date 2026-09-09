#!/usr/bin/env python3
"""Strict committed plan sidecar tests (Phase 2D1).

Covers the ``factory-plan-archive/v1`` and ``factory-plan-history/v1``
sidecars: strict field validation, duplicate-key rejection, size/count/record
bounds, the completed-ID index, the composite plan binding digest, tamper
detection, and atomic committed writes.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
LOOP = ROOT / ".factory" / "loop"

sys.path.insert(0, str(LOOP))
import plan_sidecars  # noqa: E402
from plan_sidecars import (  # noqa: E402
    ARCHIVE_FILE,
    ARCHIVE_SCHEMA,
    HISTORY_FILE,
    HISTORY_SCHEMA,
    ArchiveRecord,
    HistoryRecord,
    PlanSidecarBindingError,
    PlanSidecarError,
    PlanSidecarTransitionError,
    append_archive,
    completed_ids,
    load_plan_binding,
    parse_archive,
    parse_history,
    plan_binding_digest,
    read_archive,
    serialize_archive,
    serialize_history,
    sidecar_digest,
    verify_sidecar_binding,
    write_archive,
    write_history,
)

COMMIT = "a" * 40
DIGEST = "b" * 64


def make_archive_record(task_id: int = 1, **overrides) -> ArchiveRecord:
    values = dict(
        schema=ARCHIVE_SCHEMA,
        task_id=task_id,
        title=f"Task {task_id}",
        priority=task_id,
        dependencies=[],
        status="complete",
        scope="scope",
        acceptance="acceptance",
        verification="verification",
        documentation_impact="docs",
        evidence_refs=["abc123"],
        archived_commit=COMMIT,
        provenance="migration",
    )
    values.update(overrides)
    return ArchiveRecord(**values)


def make_history_record(task_id: int = 1, **overrides) -> HistoryRecord:
    values = dict(
        schema=HISTORY_SCHEMA,
        event="archived",
        task_id=task_id,
        commit=COMMIT,
        plan_digest=DIGEST,
        detail="archived",
    )
    values.update(overrides)
    return HistoryRecord(**values)


class ArchiveRecordValidationTest(unittest.TestCase):
    def test_valid_record_roundtrips(self) -> None:
        record = make_archive_record()
        data = serialize_archive([record])
        parsed = parse_archive(data)
        self.assertEqual(len(parsed), 1)
        self.assertEqual(parsed[0].to_dict(), record.to_dict())

    def test_unknown_field_rejected(self) -> None:
        record = make_archive_record().to_dict()
        record["extra"] = "x"
        with self.assertRaises(PlanSidecarError) as caught:
            parse_archive(
                json.dumps(record, sort_keys=True).encode("utf-8") + b"\n"
            )
        self.assertIn("exactly the archive field set", str(caught.exception))

    def test_missing_field_rejected(self) -> None:
        record = make_archive_record().to_dict()
        del record["scope"]
        with self.assertRaises(PlanSidecarError):
            parse_archive(
                json.dumps(record, sort_keys=True).encode("utf-8") + b"\n"
            )

    def test_duplicate_json_key_rejected(self) -> None:
        line = (
            b'{"schema":"factory-plan-archive/v1","schema":"factory-plan-archive/v1"}'
        )
        with self.assertRaises(PlanSidecarError) as caught:
            parse_archive(line + b"\n")
        self.assertIn("duplicate JSON object key", str(caught.exception))

    def test_bad_commit_rejected(self) -> None:
        with self.assertRaises(PlanSidecarError) as caught:
            serialize_archive([make_archive_record(archived_commit="zzz")])
        self.assertIn("40-hex commit", str(caught.exception))

    def test_bad_provenance_rejected(self) -> None:
        with self.assertRaises(PlanSidecarError):
            serialize_archive([make_archive_record(provenance="hacker")])

    def test_duplicate_task_id_rejected(self) -> None:
        data = serialize_archive([make_archive_record(1), make_archive_record(1)])
        with self.assertRaises(PlanSidecarError) as caught:
            parse_archive(data)
        self.assertIn("repeats task_id", str(caught.exception))

    def test_oversized_line_rejected(self) -> None:
        record = make_archive_record(scope="x" * (plan_sidecars.RECORD_MAX_BYTES + 1))
        data = serialize_archive([record])
        with self.assertRaises(PlanSidecarError) as caught:
            parse_archive(data)
        self.assertIn("exceeds", str(caught.exception))

    def test_oversized_file_rejected(self) -> None:
        data = b"x" * (plan_sidecars.SIDECAR_MAX_BYTES + 1)
        with self.assertRaises(PlanSidecarError) as caught:
            parse_archive(data)
        self.assertIn("size cap", str(caught.exception))

    def test_record_count_cap(self) -> None:
        # Patch the count cap down so the count bound (not the size cap) is
        # the one exercised; the cap is a module constant the parser reads.
        original = plan_sidecars.RECORD_MAX_COUNT
        plan_sidecars.RECORD_MAX_COUNT = 5
        try:
            lines = []
            for i in range(1, 7):
                lines.append(
                    ('{"schema":"factory-plan-archive/v1","task_id":' + str(i)
                     + ',"title":"t","priority":1,"dependencies":[],'
                     '"status":"complete","scope":"","acceptance":"",'
                     '"verification":"","documentation_impact":"",'
                     '"evidence_refs":[],"archived_commit":"' + "a" * 40 + '",'
                     '"provenance":"migration"}').encode("utf-8")
                )
            data = b"\n".join(lines) + b"\n"
            with self.assertRaises(PlanSidecarError) as caught:
                parse_archive(data)
            self.assertIn("record cap", str(caught.exception))
        finally:
            plan_sidecars.RECORD_MAX_COUNT = original

    def test_malformed_line_rejected(self) -> None:
        with self.assertRaises(PlanSidecarError):
            parse_archive(b"{not json}\n")

    def test_empty_archive_is_valid(self) -> None:
        self.assertEqual(parse_archive(b""), [])
        self.assertEqual(completed_ids([]), frozenset())


class HistoryRecordValidationTest(unittest.TestCase):
    def test_valid_record_roundtrips(self) -> None:
        record = make_history_record()
        data = serialize_history([record])
        parsed = parse_history(data)
        self.assertEqual(parsed[0].to_dict(), record.to_dict())

    def test_null_task_id_allowed(self) -> None:
        record = make_history_record(task_id=None)
        parsed = parse_history(serialize_history([record]))
        self.assertIsNone(parsed[0].task_id)

    def test_bad_event_rejected(self) -> None:
        with self.assertRaises(PlanSidecarError):
            serialize_history([make_history_record(event="exploit")])

    def test_bad_plan_digest_rejected(self) -> None:
        with self.assertRaises(PlanSidecarError):
            serialize_history([make_history_record(plan_digest="zzz")])


class CompletedIndexTest(unittest.TestCase):
    def test_index_is_pure_function(self) -> None:
        records = [make_archive_record(1), make_archive_record(7)]
        self.assertEqual(completed_ids(records), frozenset({1, 7}))


class BindingDigestTest(unittest.TestCase):
    def test_composite_binding_changes_with_any_part(self) -> None:
        plan = b"plan"
        archive = b"archive"
        history = b"history"
        base = plan_binding_digest(plan, archive, history)
        self.assertEqual(
            base,
            hashlib.sha256(
                hashlib.sha256(plan).digest() + b"\x00"
                + hashlib.sha256(archive).digest() + b"\x00"
                + hashlib.sha256(history).digest()
            ).hexdigest(),
        )
        self.assertNotEqual(
            base, plan_binding_digest(plan + b"x", archive, history)
        )
        self.assertNotEqual(
            base, plan_binding_digest(plan, archive + b"x", history)
        )
        self.assertNotEqual(
            base, plan_binding_digest(plan, archive, history + b"x")
        )


class SidecarFileIOTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / ".factory").mkdir()
        (self.root / ".factory" / "artifacts").mkdir()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_write_and_read_roundtrip(self) -> None:
        digest = write_archive(self.root, [make_archive_record(1)])
        records = read_archive(self.root)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].task_id, 1)
        self.assertEqual(
            digest,
            sidecar_digest(
                (self.root / ".factory" / "artifacts" / ARCHIVE_FILE).read_bytes()
            ),
        )

    def test_append_is_append_only(self) -> None:
        write_archive(self.root, [make_archive_record(1)])
        append_archive(self.root, make_archive_record(2))
        records = read_archive(self.root)
        self.assertEqual([r.task_id for r in records], [1, 2])
        with self.assertRaises(PlanSidecarTransitionError):
            append_archive(self.root, make_archive_record(1))

    def test_missing_sidecar_fails_closed(self) -> None:
        with self.assertRaises(PlanSidecarError) as caught:
            read_archive(self.root)
        self.assertIn("missing", str(caught.exception))

    def test_symlink_sidecar_fails_closed(self) -> None:
        target = self.root / "target.jsonl"
        target.write_bytes(serialize_archive([make_archive_record(1)]))
        link = self.root / ".factory" / "artifacts" / ARCHIVE_FILE
        link.symlink_to(target)
        with self.assertRaises(PlanSidecarError) as caught:
            read_archive(self.root)
        self.assertIn("not a regular non-symlink file", str(caught.exception))

    def test_atomic_write_leaves_no_partial_file(self) -> None:
        path = self.root / ".factory" / "artifacts" / ARCHIVE_FILE
        plan_sidecars._atomic_write_committed(path, b"first\n")
        self.assertEqual(path.read_bytes(), b"first\n")
        plan_sidecars._atomic_write_committed(path, b"second\n")
        self.assertEqual(path.read_bytes(), b"second\n")


class BindingVerificationTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / ".factory").mkdir()
        (self.root / ".factory" / "artifacts").mkdir()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _v2_plan(self, archive_digest: str, history_digest: str) -> bytes:
        return (
            "---\n"
            "schema: factory-plan/v2\n"
            "spec_path: docs/SPEC.md\n"
            "spec_commit: " + "c" * 40 + "\n"
            "spec_blob: " + "d" * 40 + "\n"
            "base_commit: " + "e" * 40 + "\n"
            "status: active\n"
            f"sidecars: {json.dumps({'archive': archive_digest, 'history': history_digest}, sort_keys=True, separators=(',', ':'))}\n"
            "---\n"
            "\n"
            "# Implementation Plan\n"
            "\n"
            "## Goal and non-goals\n"
            "\n"
            "goal\n"
            "\n"
            "## Architecture and constraints\n"
            "\n"
            "arch\n"
            "\n"
            "## Task 1: Final documentation and specification audit\n"
            "\n"
            "- Status: pending\n"
            "- Dependencies: Task 2\n"
            "- Priority: 1\n"
            "- Scope: scope\n"
            "- Acceptance criteria: acceptance\n"
            "- Verification: verification\n"
            "- Documentation impact: docs\n"
            "\n"
        ).encode("utf-8")

    def test_verify_passes_on_matching_pair(self) -> None:
        archive = serialize_archive([make_archive_record(2)])
        history = serialize_history([make_history_record(2)])
        plan = self._v2_plan(sidecar_digest(archive), sidecar_digest(history))
        write_archive(self.root, [make_archive_record(2)])
        write_history(self.root, [make_history_record(2)])
        records = parse_archive(archive)
        verify_sidecar_binding(plan, archive, history, archive_records=records)
        loaded = load_plan_binding(self.root, plan)
        self.assertEqual(loaded["archive_digest"], sidecar_digest(archive))
        self.assertEqual(loaded["history_digest"], sidecar_digest(history))

    def test_tampered_archive_fails_closed(self) -> None:
        archive = serialize_archive([make_archive_record(2)])
        history = serialize_history([make_history_record(2)])
        plan = self._v2_plan(sidecar_digest(archive), sidecar_digest(history))
        records = parse_archive(archive)
        tampered = archive + b'{"schema":"factory-plan-archive/v1"}\n'
        with self.assertRaises(PlanSidecarBindingError):
            verify_sidecar_binding(
                plan, tampered, history, archive_records=records
            )

    def test_tampered_history_fails_closed(self) -> None:
        archive = serialize_archive([make_archive_record(2)])
        history = serialize_history([make_history_record(2)])
        plan = self._v2_plan(sidecar_digest(archive), sidecar_digest(history))
        records = parse_archive(archive)
        with self.assertRaises(PlanSidecarBindingError):
            verify_sidecar_binding(
                plan, archive, history + b"x", archive_records=records
            )

    def test_stale_plan_binding_fails_closed(self) -> None:
        archive = serialize_archive([make_archive_record(2)])
        history = serialize_history([make_history_record(2)])
        plan = self._v2_plan(sidecar_digest(archive), sidecar_digest(history))
        records = parse_archive(archive)
        # A plan whose binding names a different (stale) archive digest.
        stale = self._v2_plan("0" * 64, sidecar_digest(history))
        with self.assertRaises(PlanSidecarBindingError):
            verify_sidecar_binding(
                stale, archive, history, archive_records=records
            )

    def test_v1_plan_has_no_binding(self) -> None:
        v1 = (ROOT / ".factory" / "tests" / "fixtures" / "plan-valid-base.md")
        with self.assertRaises(PlanSidecarError) as caught:
            plan_sidecars.plan_sidecar_binding(v1.read_bytes())
        self.assertIn("no sidecar binding", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
