#!/usr/bin/env python3
"""Harness-owned conformance tests for the strict structured verifier-failure
artifact (EVID-02).

This test lives under the hidden `.factory/tests/` namespace because the
specification (HIDE-01, §3) keeps harness-only tests out of the adopting
product's visible test tree.  It is the deterministic verification for the
structured-handoff foundation:

* the committed schema `factory-verifier-failure/v1` accepts exactly the
  documented field set with closed enums and bounded sizes;
* strings are data, never executable path/argv authority: the `command`
  field is an exact argv record and the validator never executes it;
* duplicate JSON keys, oversized documents, unknown enum values, invalid
  commit/campaign bindings, and malformed fields fail closed;
* the canonical bytes are deterministic and round-trip through
  parse -> bytes -> parse without semantic loss;
* `build_artifact` mints only schema-valid artifacts from trusted inputs.
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
LOOP = ROOT / ".factory" / "loop"
SCHEMAS = ROOT / ".factory" / "schemas"

sys.path.insert(0, str(LOOP))
from verifier_failure import (  # noqa: E402
    CAPABILITY_CLASSIFICATIONS,
    ENVIRONMENT_CLASSIFICATIONS,
    MAX_ARTIFACT_BYTES,
    MAX_CHANGED_FILES,
    MAX_COMMAND_ITEMS,
    MAX_OUTPUT_TAIL_BYTES,
    MAX_REF_BYTES,
    MAX_TEXT_BYTES,
    PHASES,
    RERUN_SCOPES,
    SCHEMA_FILE,
    SCHEMA_NAME,
    VerifierFailureError,
    VerifierFailureMalformedError,
    artifact_digest,
    failure_fingerprint,
    publish_artifact,
    read_artifact,
    validate_consumption,
    artifact_bytes,
    build_artifact,
    parse_artifact,
    validate_artifact,
)

COMMIT = "a" * 40
CAMPAIGN = "verifier-failure-conformance"


def valid_artifact(**overrides):
    artifact = {
        "schema": SCHEMA_NAME,
        "campaign_id": CAMPAIGN,
        "phase": "verification",
        "task_id": 3,
        "commit": COMMIT,
        "command": ["./.factory/tools/verify-boilerplate.sh"],
        "exit_status": 1,
        "expected": "exit 0 with a clean tree",
        "observed": "exit 1 with a dirty tree",
        "output_tail": "verify-boilerplate: all generic gates passed\n",
        "output_ref": ".factory-state/generic-evidence/verifier-output.tail",
        "changed_files": ["src/main.c", "tests/test_main.c"],
        "artifact_refs": [".factory-state/audit-receipts/verifier-failure.json"],
        "environment_classification": "dirty",
        "capability_classification": "available",
        "rerun_scope": "targeted",
    }
    artifact.update(overrides)
    return artifact


class SchemaContractTest(unittest.TestCase):
    """The committed schema is present, JSON, and carries the documented
    closed enums and bounded sizes."""

    def test_schema_file_exists_and_is_json(self) -> None:
        path = SCHEMAS / SCHEMA_FILE
        self.assertTrue(path.is_file())
        data = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(data.get("$id"), SCHEMA_NAME)
        self.assertIs(data.get("additionalProperties"), False)

    def test_closed_enums_are_documented(self) -> None:
        path = SCHEMAS / SCHEMA_FILE
        data = json.loads(path.read_text(encoding="utf-8"))
        props = data["properties"]
        self.assertEqual(
            props["phase"]["enum"], list(PHASES))
        self.assertEqual(
            props["environment_classification"]["enum"],
            list(ENVIRONMENT_CLASSIFICATIONS))
        self.assertEqual(
            props["capability_classification"]["enum"],
            list(CAPABILITY_CLASSIFICATIONS))
        self.assertEqual(
            props["rerun_scope"]["enum"], list(RERUN_SCOPES))

    def test_bounded_sizes_are_documented(self) -> None:
        path = SCHEMAS / SCHEMA_FILE
        data = json.loads(path.read_text(encoding="utf-8"))
        props = data["properties"]
        self.assertEqual(props["command"]["maxItems"], MAX_COMMAND_ITEMS)
        self.assertEqual(props["output_tail"]["maxLength"], MAX_OUTPUT_TAIL_BYTES)
        self.assertEqual(props["changed_files"]["maxItems"], MAX_CHANGED_FILES)
        self.assertEqual(props["expected"]["maxLength"], MAX_TEXT_BYTES)
        self.assertEqual(props["observed"]["maxLength"], MAX_TEXT_BYTES)
        self.assertEqual(props["output_ref"]["maxLength"], MAX_REF_BYTES)


class PositiveTest(unittest.TestCase):
    """Schema-valid artifacts parse, validate, and round-trip."""

    def test_valid_artifact_validates(self) -> None:
        validate_artifact(valid_artifact())

    def test_valid_artifact_roundtrips_bytes(self) -> None:
        artifact = valid_artifact()
        raw = artifact_bytes(artifact)
        reparsed = parse_artifact(raw)
        self.assertEqual(reparsed, artifact)

    def test_build_artifact_mints_valid_artifact(self) -> None:
        artifact = build_artifact(
            campaign_id=CAMPAIGN,
            phase="verification",
            commit=COMMIT,
            command=["./.factory/tools/verify-boilerplate.sh"],
            exit_status=2,
            expected="exit 0",
            observed="exit 2",
            output_tail="gate failed",
            changed_files=["src/a.c"],
            artifact_refs=[".factory-state/audit-receipts/x.json"],
            environment_classification="clean",
            capability_classification="unavailable",
            rerun_scope="full",
            task_id=3,
        )
        self.assertEqual(artifact["schema"], SCHEMA_NAME)
        self.assertEqual(artifact["exit_status"], 2)
        self.assertEqual(artifact["task_id"], 3)
        validate_artifact(artifact)

    def test_audit_phase_is_accepted(self) -> None:
        artifact = valid_artifact(phase="audit")
        artifact.pop("task_id")
        validate_artifact(artifact)

    def test_optional_fields_may_be_absent(self) -> None:
        artifact = valid_artifact()
        for name in ("output_tail", "output_ref", "changed_files", "artifact_refs"):
            artifact.pop(name, None)
        validate_artifact(artifact)

    def test_negative_exit_status_is_accepted(self) -> None:
        # A signal/termination status is a legitimate exact status.
        validate_artifact(valid_artifact(exit_status=-15))

    def test_deterministic_bytes(self) -> None:
        first = artifact_bytes(valid_artifact())
        second = artifact_bytes(valid_artifact())
        self.assertEqual(first, second)


class MalformedInputTest(unittest.TestCase):
    """Every documented defect class fails closed."""

    def _reject(self, artifact, fragment: str) -> None:
        with self.assertRaises(VerifierFailureMalformedError) as caught:
            validate_artifact(artifact)
        self.assertIn(fragment, str(caught.exception))

    def test_duplicate_json_key_is_rejected(self) -> None:
        raw = (
            b'{"schema":"factory-verifier-failure/v1","schema":"x",'
            b'"campaign_id":"c","phase":"verification","commit":"' + COMMIT.encode() +
            b'","command":["g"],"exit_status":1,"expected":"e","observed":"o",'
            b'"environment_classification":"clean",'
            b'"capability_classification":"not_required","rerun_scope":"full"}'
        )
        with self.assertRaises(VerifierFailureMalformedError) as caught:
            parse_artifact(raw)
        self.assertIn("duplicate JSON object key", str(caught.exception))

    def test_oversized_document_is_rejected(self) -> None:
        raw = b" " * (MAX_ARTIFACT_BYTES + 1)
        with self.assertRaises(VerifierFailureMalformedError) as caught:
            parse_artifact(raw)
        self.assertIn("oversized", str(caught.exception))

    def test_non_utf8_is_rejected(self) -> None:
        with self.assertRaises(VerifierFailureMalformedError) as caught:
            parse_artifact(b"\xff\xfe\x00")
        self.assertIn("not UTF-8", str(caught.exception))

    def test_not_json_is_rejected(self) -> None:
        with self.assertRaises(VerifierFailureMalformedError) as caught:
            parse_artifact(b"not json")
        self.assertIn("not valid JSON", str(caught.exception))

    def test_unknown_phase_is_rejected(self) -> None:
        self._reject(valid_artifact(phase="planning"), "phase")

    def test_unknown_environment_classification_is_rejected(self) -> None:
        self._reject(
            valid_artifact(environment_classification="unknown"), "environment")

    def test_unknown_capability_classification_is_rejected(self) -> None:
        self._reject(
            valid_artifact(capability_classification="unknown"), "capability")

    def test_unknown_rerun_scope_is_rejected(self) -> None:
        self._reject(valid_artifact(rerun_scope="sometimes"), "rerun_scope")

    def test_invalid_commit_is_rejected(self) -> None:
        self._reject(valid_artifact(commit="HEAD"), "commit")

    def test_invalid_campaign_id_is_rejected(self) -> None:
        self._reject(valid_artifact(campaign_id="../escape"), "campaign_id")

    def test_empty_command_is_rejected(self) -> None:
        self._reject(valid_artifact(command=[]), "command")

    def test_command_with_empty_arg_is_rejected(self) -> None:
        self._reject(valid_artifact(command=[""]), "command")

    def test_oversized_command_arg_is_rejected(self) -> None:
        self._reject(
            valid_artifact(command=["x" * (MAX_TEXT_BYTES + 1)]), "command")

    def test_oversized_command_array_is_rejected(self) -> None:
        self._reject(
            valid_artifact(command=["g"] * (MAX_COMMAND_ITEMS + 1)), "command")

    def test_bool_exit_status_is_rejected(self) -> None:
        self._reject(valid_artifact(exit_status=True), "exit_status")

    def test_exit_status_out_of_range_is_rejected(self) -> None:
        self._reject(valid_artifact(exit_status=256), "exit_status")
        self._reject(valid_artifact(exit_status=-129), "exit_status")

    def test_empty_expected_or_observed_is_rejected(self) -> None:
        self._reject(valid_artifact(expected=""), "expected")
        self._reject(valid_artifact(observed=""), "observed")

    def test_oversized_expected_is_rejected(self) -> None:
        self._reject(
            valid_artifact(expected="x" * (MAX_TEXT_BYTES + 1)), "expected")

    def test_oversized_output_tail_is_rejected(self) -> None:
        self._reject(
            valid_artifact(output_tail="x" * (MAX_OUTPUT_TAIL_BYTES + 1)),
            "output_tail")

    def test_oversized_output_ref_is_rejected(self) -> None:
        self._reject(
            valid_artifact(output_ref="x" * (MAX_REF_BYTES + 1)), "output_ref")

    def test_oversized_changed_files_is_rejected(self) -> None:
        self._reject(
            valid_artifact(changed_files=["f"] * (MAX_CHANGED_FILES + 1)),
            "changed_files")

    def test_oversized_artifact_refs_is_rejected(self) -> None:
        self._reject(
            valid_artifact(artifact_refs=["r"] * 65), "artifact_refs")

    def test_extra_field_is_rejected(self) -> None:
        self._reject(valid_artifact(extra="x"), "extra field")

    def test_missing_required_field_is_rejected(self) -> None:
        artifact = valid_artifact()
        artifact.pop("command")
        self._reject(artifact, "missing required field")

    def test_non_object_is_rejected(self) -> None:
        with self.assertRaises(VerifierFailureMalformedError):
            validate_artifact(["not", "an", "object"])


class DataBoundaryTest(unittest.TestCase):
    """Strings are data, never executable path/argv authority."""

    def test_command_is_never_executed(self) -> None:
        # The validator and parser only ever read the command as data; a
        # command that would be dangerous to execute must parse and validate
        # without any side effect.
        artifact = valid_artifact(
            command=["rm", "-rf", "/"], exit_status=1,
        )
        validate_artifact(artifact)
        raw = artifact_bytes(artifact)
        reparsed = parse_artifact(raw)
        self.assertEqual(reparsed["command"], ["rm", "-rf", "/"])

    def test_refs_are_bounded_references_not_paths(self) -> None:
        # Refs are bounded repository-relative references; traversal-shaped,
        # absolute, backslash, control-char, and dot/dotdot/empty-segment
        # refs are rejected (no executable path authority).
        for bad in (
            "../../etc/passwd",
            "/etc/passwd",
            "a\\b",
            "a/../b",
            "a/./b",
            "a//b",
            "a/\x00b",
        ):
            with self.assertRaises(VerifierFailureMalformedError) as caught:
                validate_artifact(valid_artifact(output_ref=bad))
            self.assertIn("output_ref", str(caught.exception))

    def test_safe_ref_syntax_is_accepted(self) -> None:
        # Repository-relative refs and bare non-path identifiers are
        # preserved; they carry no traversal or absolute authority.
        for good in (
            ".factory-state/generic-evidence/verifier-output.tail",
            "src/main.c",
            "verifier-output.tail",
            "sha256:abc123",
        ):
            validate_artifact(valid_artifact(output_ref=good))
            validate_artifact(valid_artifact(changed_files=[good]))
            validate_artifact(valid_artifact(artifact_refs=[good]))

    def test_unsafe_changed_files_and_artifact_refs_are_rejected(self) -> None:
        for field in ("changed_files", "artifact_refs"):
            for bad in (
                "/etc/passwd",
                "../escape",
                "./x",
                "a//b",
                "a\\b",
                "a/\x1fb",
            ):
                with self.assertRaises(VerifierFailureMalformedError) as caught:
                    validate_artifact(valid_artifact(**{field: [bad]}))
                self.assertIn(field, str(caught.exception))


class StaleCommitTest(unittest.TestCase):
    """A stale or forged commit binding fails closed."""

    def test_commit_must_be_exact_40_hex(self) -> None:
        self._reject(valid_artifact(commit="b" * 39), "commit")
        self._reject(valid_artifact(commit="B" * 40), "commit")

    def _reject(self, artifact, fragment: str) -> None:
        with self.assertRaises(VerifierFailureMalformedError) as caught:
            validate_artifact(artifact)
        self.assertIn(fragment, str(caught.exception))


class PublicationAndConsumptionTest(unittest.TestCase):
    """Phase 2B1: write-once publication and sealed consumption validation."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="factory-vf-test."))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        (self.tmp / ".factory-state").mkdir(mode=0o700)

    def test_publish_read_roundtrip(self) -> None:
        artifact = valid_artifact()
        digest, name = publish_artifact(self.tmp, CAMPAIGN, artifact)
        self.assertEqual(len(digest), 64)
        self.assertTrue(name.startswith("verifier-failure-"))
        loaded = read_artifact(self.tmp, CAMPAIGN, digest)
        self.assertEqual(loaded, artifact)

    def test_publish_is_write_once(self) -> None:
        artifact = valid_artifact()
        digest, _ = publish_artifact(self.tmp, CAMPAIGN, artifact)
        # A byte-identical re-publication is accepted (crash idempotency).
        digest2, _ = publish_artifact(self.tmp, CAMPAIGN, artifact)
        self.assertEqual(digest, digest2)
        # A different artifact at the same content-addressed name fails
        # closed (a forged or tampered marker is never silently replaced).
        path = self.tmp / ".factory-state" / f"verifier-failure-{digest}.json"
        path.write_text('{"schema":"factory-verifier-failure/v1","tampered":true}')
        with self.assertRaises(VerifierFailureError):
            publish_artifact(self.tmp, CAMPAIGN, artifact)

    def test_read_missing_and_tampered_fail_closed(self) -> None:
        with self.assertRaises(VerifierFailureError):
            read_artifact(self.tmp, CAMPAIGN, "0" * 64)
        artifact = valid_artifact()
        digest, _ = publish_artifact(self.tmp, CAMPAIGN, artifact)
        path = self.tmp / ".factory-state" / f"verifier-failure-{digest}.json"
        path.write_text('{"schema":"factory-verifier-failure/v1","tampered":true}')
        with self.assertRaises(VerifierFailureError):
            read_artifact(self.tmp, CAMPAIGN, digest)

    def test_validate_consumption_binds_commit_campaign_task_digest(self) -> None:
        artifact = valid_artifact()
        digest = artifact_digest(artifact)
        validate_consumption(
            artifact, expected_commit=COMMIT,
            expected_campaign_id=CAMPAIGN, expected_task_id=3,
            expected_digest=digest,
        )
        with self.assertRaisesRegex(VerifierFailureError, "commit"):
            validate_consumption(
                artifact, expected_commit="0" * 40,
                expected_campaign_id=CAMPAIGN, expected_task_id=3,
                expected_digest=digest,
            )
        with self.assertRaisesRegex(VerifierFailureError, "campaign"):
            validate_consumption(
                artifact, expected_commit=COMMIT,
                expected_campaign_id="other", expected_task_id=3,
                expected_digest=digest,
            )
        with self.assertRaisesRegex(VerifierFailureError, "task"):
            validate_consumption(
                artifact, expected_commit=COMMIT,
                expected_campaign_id=CAMPAIGN, expected_task_id=9,
                expected_digest=digest,
            )
        with self.assertRaisesRegex(VerifierFailureError, "digest"):
            validate_consumption(
                artifact, expected_commit=COMMIT,
                expected_campaign_id=CAMPAIGN, expected_task_id=3,
                expected_digest="1" * 64,
            )

    def test_failure_fingerprint_is_stable_and_output_tail_insensitive(self) -> None:
        first = valid_artifact()
        second = valid_artifact(output_tail="different tail bytes")
        self.assertEqual(failure_fingerprint(first), failure_fingerprint(second))
        changed = valid_artifact(changed_files=["src/other.c"])
        self.assertNotEqual(failure_fingerprint(first), failure_fingerprint(changed))


if __name__ == "__main__":
    unittest.main()
