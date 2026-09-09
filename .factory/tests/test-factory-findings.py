#!/usr/bin/env python3
"""Hidden findings-authority suite (Task 10; FIND-01, §16).

This test lives under the hidden ``.factory/tests/`` namespace and drives
``.factory/loop/findings.py`` — the structured tester/auditor findings
authority — plus the Task 10 §16 integration in ``campaign.py``, the
production ``InvocationBinding`` digest binding in ``launch.py``, and the
``findings-revised`` planner seam of the committed fixture driver.

The suite proves the §16 contract end to end:

* **positive flow**: verification/audit ``findings`` and ``blocked`` are
  minted by the trusted orchestrator as write-once no-replace receipts under
  the ignored ``.factory-state/`` namespace, bound to the exact campaign,
  round, phase, phase tag, phase-base commit, and the exact structured
  result bytes the orchestrator read; the next planning phase re-reads the
  receipts through the hardened no-follow bounded reader, re-validates every
  binding, and derives one deterministic ``factory-findings/v1`` payload
  that reaches only the next planner; the planner's ``findings-revised``
  seam incorporates the accepted findings into the canonical plan as
  revised/blocked tasks; only then does the next developer run;
* **no selector/evidence authority**: the deterministic selector is a pure
  function of plan + state and never reads the payload; the payload is never
  persisted, never handed to the developer/tester/auditor, and never stored
  as a runtime task ledger, memory, or context summary;
* **fail-closed classes**: malformed, oversized, symlinked, stale,
  foreign, synthetic, pass-phase, receipt-only, pre-planted, and tampered
  receipts (wrong outcome, wrong result digest, wrong phase-base commit,
  unrecorded ledger tag) each fail closed with a dedicated error class
  before the next planner launches;
* **exact result digest**: the receipt's ``result_digest`` is the SHA-256 of
  the exact structured result bytes the orchestrator consumed, recorded in
  the campaign result phase history, and re-bound at consumption.

It reuses the committed fixture workspace and helper conventions of the
sibling hidden campaign suite (``test-factory-campaign.py``) instead of
copying them.
"""

from __future__ import annotations

import dataclasses
import importlib.util
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
import unittest.mock

ROOT = Path(__file__).resolve().parents[2]
LOOP = ROOT / ".factory" / "loop"
STATE_DIR = ".factory-state"

sys.path.insert(0, str(LOOP))

# Load the sibling hidden campaign suite so the committed fixture workspace
# and helper conventions are reused, never copied.
_CAMPAIGN_SUITE = ROOT / ".factory" / "tests" / "test-factory-campaign.py"
_spec = importlib.util.spec_from_file_location(
    "factory_campaign_suite", _CAMPAIGN_SUITE)
FACTORY_CAMPAIGN = importlib.util.module_from_spec(_spec)
assert _spec.loader is not None
_spec.loader.exec_module(FACTORY_CAMPAIGN)

import campaign as campaign_module  # noqa: E402
import findings as findings_module  # noqa: E402
import launch as launch_module  # noqa: E402
import plan_parser  # noqa: E402
import selector as selector_module  # noqa: E402
import state as state_module  # noqa: E402

FixtureWorkspace = FACTORY_CAMPAIGN.FixtureWorkspace
TASK_SPECS = FACTORY_CAMPAIGN.TASK_SPECS
SUCCESS_SCENARIO = FACTORY_CAMPAIGN.SUCCESS_SCENARIO
assert_history = FACTORY_CAMPAIGN.assert_history
assert_terminal = FACTORY_CAMPAIGN.assert_terminal
gen_plan = FACTORY_CAMPAIGN.gen_plan
sha256 = FACTORY_CAMPAIGN.sha256
_git = FACTORY_CAMPAIGN._git
TRUE_EXECUTABLE = FACTORY_CAMPAIGN.TRUE_EXECUTABLE
FALSE_EXECUTABLE = FACTORY_CAMPAIGN.FALSE_EXECUTABLE
REQUIREMENT_REGISTRY = FACTORY_CAMPAIGN.REQUIREMENT_REGISTRY
DRIVER_REL = FACTORY_CAMPAIGN.DRIVER_REL
PLAN_REL = FACTORY_CAMPAIGN.PLAN_REL
BRANCH = FACTORY_CAMPAIGN.BRANCH

EXIT_ERROR = campaign_module.EXIT_ERROR

RESULT_SCHEMA_NAME = "factory-phase-result/v1"


def _canonical_result(outcome: str, findings=None, blocked_on=None) -> bytes:
    """The exact bytes the fixture tester/auditor writes (sorted canonical)."""
    data = {"schema": RESULT_SCHEMA_NAME, "outcome": outcome}
    if findings:
        data["findings"] = findings
    if blocked_on:
        data["blocked_on"] = blocked_on
    return json.dumps(data, sort_keys=True, separators=(",", ":")).encode("utf-8")


def receipt_rel(round_no: int, phase: str) -> str:
    return (
        f"{STATE_DIR}/factory-findings-receipt-round-{round_no}-{phase}.json"
    )


# ---------------------------------------------------------------------------
# Unit scaffolding: a scratch root with the hardened `.factory-state/` I/O
# ---------------------------------------------------------------------------


class _FindingsBase(unittest.TestCase):
    """Shared helpers: one fresh scratch root per test (no Git needed)."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="factory-findings-test."))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.root = self.tmp / "root"
        self.root.mkdir()
        self.campaign_id = "campaign"
        self.head = "a" * 40
        self.tag = "r1.verification.1.a1"
        # The campaign always owns a private `.factory-state/` namespace; the
        # unit scratch root mirrors that so missing-receipt reads are clean.
        (self.root / STATE_DIR).mkdir(mode=0o700)

    def _write_marker(self, name: str, data: bytes) -> None:
        directory = self.root / STATE_DIR
        directory.mkdir(mode=0o700, exist_ok=True)
        path = directory / name
        path.write_bytes(data)
        os.chmod(path, 0o600)

    def _read_marker(self, name: str) -> bytes:
        return (self.root / STATE_DIR / name).read_bytes()

    def _write_artifact(self, name: str, data: bytes) -> None:
        directory = self.root / STATE_DIR
        directory.mkdir(mode=0o700, exist_ok=True)
        path = directory / name
        path.write_bytes(data)
        os.chmod(path, 0o600)

    def preserve(self, round_no: int, phase: str, raw: bytes) -> str:
        """Preserve the exact phase-result bytes (REQ 3) like the mint does."""
        return findings_module.preserve_phase_result(
            self.root, round_no, phase, raw
        )

    def record(
        self, round_no: int, phase: str, outcome: str,
        result_digest: str = "",
    ) -> campaign_module.PhaseRecord:
        return campaign_module.PhaseRecord(
            round=round_no, phase=phase, attempt=1, outcome=outcome,
            head_commit=self.head, plan_digest="0" * 64,
            detail="", result_digest=result_digest,
        )

    def mint(self, **overrides) -> dict:
        params = dict(
            campaign_id="campaign", round_number=1, phase="verification",
            phase_tag=self.tag, phase_base_commit=self.head,
            outcome="findings",
            result_digest=sha256(_canonical_result("findings")),
            findings=["fixture finding"], blocked_on=[],
            gate_ran=True, gate_exit=0,
            capability_ran=False, capability_exit=None,
        )
        params.update(overrides)
        return findings_module.build_receipt(**params)

    def publish(self, receipt: dict) -> str:
        return findings_module.publish_receipt(self.root, receipt)

    def ledger(self, *tags: str) -> None:
        lines = [
            json.dumps({"tag": tag, "digest": "0" * 64},
                       sort_keys=True, separators=(",", ":"))
            for tag in tags
        ]
        self._write_marker(
            state_module.DIGEST_LEDGER_NAME,
            ("\n".join(lines) + "\n").encode("utf-8"),
        )

    def consume(
        self,
        records=(),
        *,
        source_round: int = 1,
        head: str | None = None,
        campaign_id: str | None = None,
        is_ancestor=lambda a, b: True,
    ):
        return findings_module.consume_next_round_findings(
            self.root,
            campaign_id=campaign_id or self.campaign_id,
            source_round=source_round,
            head=head or self.head,
            is_ancestor=is_ancestor,
            phase_records=tuple(records),
        )

    @staticmethod
    def payload(raw: bytes) -> dict:
        return json.loads(raw.decode("utf-8"))


# ---------------------------------------------------------------------------
# Receipt minting: write-once no-replace evidence, every binding validated
# ---------------------------------------------------------------------------


class FindingsMintUnit(_FindingsBase):
    """§16 mint: the trusted orchestrator mints write-once receipts."""

    def test_build_receipt_rejects_unsafe_bindings(self) -> None:
        for kwargs, fragment in (
            ({"campaign_id": "not safe!"}, "campaign"),
            ({"campaign_id": ""}, "campaign"),
            ({"round_number": 0}, "round"),
            ({"round_number": True}, "round"),
            ({"phase": "implementation"}, "phase"),
            ({"outcome": "pass"}, "findings"),
            ({"outcome": "success"}, "findings"),
            ({"phase_base_commit": "beef"}, "commit"),
            ({"phase_base_commit": "A" * 40}, "commit"),
            ({"result_digest": "beef"}, "digest"),
            ({"phase_tag": "bogus"}, "phase tag"),
            ({"phase_tag": "r2.verification.1.a1"}, "phase tag"),
            ({"gate_ran": 1}, "gate"),
            ({"capability_ran": None}, "capability"),
            ({"gate_exit": -1}, "gate"),
            ({"gate_exit": True}, "gate"),
        ):
            with self.assertRaises(findings_module.FindingsError, msg=fragment):
                self.mint(**kwargs)

    def test_publish_is_write_once_no_replace(self) -> None:
        receipt = self.mint()
        name = self.publish(receipt)
        # The canonical name is now owned.  A crash-window re-mint of the
        # SAME run (byte-exact) is accepted idempotently (REQ 1) — the
        # previous mint wrote these exact bytes and the state advance was
        # interrupted — while any different content fails closed instead of
        # silently replacing the evidence.
        self.assertEqual(self.publish(receipt), name)
        self.assertEqual(
            self.publish(self.mint()),
            "factory-findings-receipt-round-1-verification.json",
        )
        with self.assertRaises(findings_module.FindingsError):
            self.publish(self.mint(result_digest=sha256(b"other")))

    def test_preplanted_receipt_fails_the_mint_closed(self) -> None:
        # A pre-planted receipt at the canonical name with DIFFERENT bytes
        # (forged evidence) must never be silently replaced by a genuine
        # mint: the byte-exact idempotent recovery accepts only this run's
        # own crashed mint.
        planted = self.mint(result_digest=sha256(b"preplant"))
        self._write_marker(
            "factory-findings-receipt-round-1-verification.json",
            findings_module.receipt_bytes(planted),
        )
        with self.assertRaises(findings_module.FindingsError) as cm:
            self.publish(self.mint())
        self.assertIn("different bytes", str(cm.exception).lower())

    def test_preplanted_byte_exact_remint_is_crash_recovery(self) -> None:
        # The byte-exact pre-planted receipt IS this run's own mint that
        # survived the crash window (the state advance was lost); the rerun
        # accepts it idempotently and never wedges.
        receipt = self.mint()
        self._write_marker(
            "factory-findings-receipt-round-1-verification.json",
            findings_module.receipt_bytes(receipt),
        )
        self.assertEqual(
            self.publish(receipt),
            "factory-findings-receipt-round-1-verification.json",
        )

    def test_receipt_roundtrip_and_digest(self) -> None:
        receipt = self.mint(
            findings=["first", "second"],
            blocked_on=["external-capability-required"],
        )
        name = self.publish(receipt)
        self.assertEqual(
            name, "factory-findings-receipt-round-1-verification.json")
        data, raw_digest = findings_module.read_receipt(
            self.root, 1, "verification")
        assert data is not None
        self.assertEqual(data["schema"], "factory-findings-receipt/v1")
        self.assertEqual(data["outcome"], "findings")
        self.assertEqual(data["findings"], ["first", "second"])
        self.assertEqual(data["blocked_on"], ["external-capability-required"])
        # The digest is the digest of the exact canonical bytes on disk.
        self.assertEqual(raw_digest, sha256(self._read_marker(name)))
        self.assertEqual(
            raw_digest,
            findings_module.sha256(findings_module.receipt_bytes(data)),
        )

    def test_receipt_name_and_phase_gates(self) -> None:
        self.assertEqual(
            findings_module.receipt_name(1, "verification"),
            "factory-findings-receipt-round-1-verification.json",
        )
        for bad in (0, -1, True, "1", 1.5):
            with self.assertRaises(findings_module.FindingsError):
                findings_module.receipt_name(bad, "verification")
        for phase in ("planning", "implementation", "developer"):
            with self.assertRaises(findings_module.FindingsError):
                findings_module.receipt_name(1, phase)


# ---------------------------------------------------------------------------
# Consumption: every binding fail-closed, positive flow, no authority leak
# ---------------------------------------------------------------------------


class FindingsConsumeUnit(_FindingsBase):
    """§16: the next-planner payload is derived only from bound receipts."""

    def _positive_setup(self) -> None:
        self.ledger(self.tag)
        # REQ 3: the exact structured phase-result bytes are preserved first,
        # so consumption authenticates the receipt against the exact trusted
        # content (digest AND parsed findings/blocked_on), never a
        # self-digest only.
        self.preserve(
            1, "verification",
            _canonical_result("findings", findings=["fixture finding"]),
        )
        self.publish(self.mint(
            result_digest=sha256(_canonical_result(
                "findings", findings=["fixture finding"])),
        ))

    def test_consume_derives_deterministic_payload(self) -> None:
        self._positive_setup()
        result_digest = sha256(_canonical_result(
            "findings", findings=["fixture finding"]))
        record = self.record(1, "verification", "findings",
                             result_digest=result_digest)
        raw = self.consume([record])
        assert raw is not None
        payload = self.payload(raw)
        self.assertEqual(payload["schema"], "factory-findings/v1")
        self.assertEqual(payload["campaign_id"], "campaign")
        self.assertEqual(payload["source_round"], 1)
        self.assertEqual(len(payload["entries"]), 1)
        entry = payload["entries"][0]
        self.assertEqual(entry["phase"], "verification")
        self.assertEqual(entry["outcome"], "findings")
        self.assertEqual(entry["phase_base_commit"], self.head)
        self.assertEqual(entry["phase_tag"], self.tag)
        self.assertEqual(entry["result_digest"], result_digest)
        self.assertEqual(entry["gate_ran"], True)
        self.assertEqual(entry["gate_exit"], 0)
        self.assertEqual(entry["capability_ran"], False)
        self.assertIsNone(entry["capability_exit"])
        self.assertEqual(
            entry["receipt_path"],
            "factory-findings-receipt-round-1-verification.json",
        )
        self.assertEqual(
            entry["receipt_digest"],
            findings_module.sha256(self._read_marker(
                "factory-findings-receipt-round-1-verification.json")),
        )
        self.assertEqual(entry["findings"], ["fixture finding"])
        self.assertEqual(entry["blocked_on"], [])
        # Deterministic canonical bytes: same payload bytes every time.
        again = self.consume([record])
        self.assertEqual(again, raw)
        # No wall-clock time, no prose claims, no plan copies in the payload.
        self.assertNotIn(b"timestamp", raw)
        self.assertNotIn(b"prose", raw)

    def test_consume_binds_both_verification_and_audit(self) -> None:
        audit_tag = "r1.audit.1.a1"
        self.ledger(self.tag, audit_tag)
        self.preserve(
            1, "audit",
            _canonical_result("findings", findings=["audit finding"]),
        )
        self.preserve(
            1, "verification",
            _canonical_result("findings", findings=["fixture finding"]),
        )
        self.publish(self.mint(
            phase="audit", phase_tag=audit_tag, outcome="findings",
            result_digest=sha256(_canonical_result(
                "findings", findings=["audit finding"])),
            findings=["audit finding"], gate_ran=False, gate_exit=None,
        ))
        self.publish(self.mint(
            result_digest=sha256(_canonical_result(
                "findings", findings=["fixture finding"]))))
        raw = self.consume([
            self.record(1, "verification", "findings",
                        result_digest=sha256(_canonical_result(
                            "findings", findings=["fixture finding"]))),
            self.record(1, "audit", "findings",
                        result_digest=sha256(_canonical_result(
                            "findings", findings=["audit finding"]))),
        ])
        assert raw is not None
        payload = self.payload(raw)
        self.assertEqual(
            {entry["phase"] for entry in payload["entries"]},
            {"verification", "audit"},
        )
        self.assertEqual(len(payload["entries"]), 2)

    def test_consume_external_blockers_remain_structured_findings(self) -> None:
        self.ledger(self.tag)
        result_digest = sha256(_canonical_result(
            "blocked", blocked_on=["external-capability-required"]))
        self.preserve(
            1, "verification",
            _canonical_result("blocked",
                              blocked_on=["external-capability-required"]),
        )
        self.publish(self.mint(
            outcome="blocked", result_digest=result_digest,
            findings=[], blocked_on=["external-capability-required"],
        ))
        raw = self.consume([
            self.record(1, "verification", "blocked", result_digest=result_digest),
        ])
        assert raw is not None
        entry = self.payload(raw)["entries"][0]
        self.assertEqual(entry["outcome"], "blocked")
        self.assertEqual(entry["blocked_on"], ["external-capability-required"])

    def test_consume_returns_none_when_no_findings_flowed(self) -> None:
        # No receipts exist and no phase recorded findings/blocked: no
        # payload reaches the next planner.
        self.assertIsNone(self.consume([
            self.record(1, "verification", "pass"),
            self.record(1, "audit", "pass"),
        ]))

    def test_receipt_only_claim_fails_closed(self) -> None:
        # The phase recorded findings but its receipt is missing: a claim
        # without the orchestrator-minted evidence fails closed.
        with self.assertRaises(findings_module.FindingsReceiptError):
            self.consume([
                self.record(1, "verification", "findings",
                            result_digest=sha256(_canonical_result("findings"))),
            ])

    def test_synthetic_receipt_for_pass_phase_fails_closed(self) -> None:
        self.ledger(self.tag)
        self.publish(self.mint())
        with self.assertRaises(findings_module.FindingsSyntheticError):
            self.consume([self.record(1, "verification", "pass")])

    def test_synthetic_receipt_for_unrecorded_phase_fails_closed(self) -> None:
        self.ledger(self.tag)
        self.publish(self.mint())
        # No phase record for (1, verification): a leftover/foreign receipt.
        with self.assertRaises(findings_module.FindingsForeignError):
            self.consume([self.record(1, "audit", "pass")])

    def test_foreign_campaign_receipt_fails_closed(self) -> None:
        self.ledger(self.tag)
        self.publish(self.mint(campaign_id="other-campaign"))
        with self.assertRaises(findings_module.FindingsForeignError):
            self.consume([
                self.record(1, "verification", "findings",
                            result_digest=sha256(_canonical_result("findings"))),
            ])

    def test_stale_round_receipt_fails_closed(self) -> None:
        self.ledger("r2.verification.1.a1")
        self.publish(self.mint(
            round_number=2, phase_tag="r2.verification.1.a1"))
        # Place the stale content at the canonical source-round name so the
        # parser must validate its bound round instead of merely observing a
        # missing expected receipt.
        state_dir = self.root / STATE_DIR
        (state_dir / findings_module.receipt_name(2, "verification")).rename(
            state_dir / findings_module.receipt_name(1, "verification")
        )
        with self.assertRaises(findings_module.FindingsStaleError):
            self.consume([
                self.record(1, "verification", "findings",
                            result_digest=sha256(_canonical_result("findings"))),
            ])

    def test_stale_unreachable_commit_fails_closed(self) -> None:
        self.ledger(self.tag)
        self.publish(self.mint())
        with self.assertRaises(findings_module.FindingsStaleError):
            self.consume(
                [
                    self.record(1, "verification", "findings",
                                result_digest=sha256(_canonical_result("findings"))),
                ],
                is_ancestor=lambda a, b: False,
            )

    def test_forged_outcome_fails_closed(self) -> None:
        self.ledger(self.tag)
        self.publish(self.mint(outcome="blocked"))
        with self.assertRaises(findings_module.FindingsSyntheticError):
            self.consume([
                self.record(1, "verification", "findings",
                            result_digest=sha256(_canonical_result("findings"))),
            ])

    def test_tampered_result_digest_fails_closed(self) -> None:
        self.ledger(self.tag)
        self.publish(self.mint(
            result_digest=sha256(_canonical_result(
                "findings", findings=["other"]))))
        with self.assertRaises(findings_module.FindingsSyntheticError):
            self.consume([
                self.record(1, "verification", "findings",
                            result_digest=sha256(_canonical_result("findings"))),
            ])

    def test_forged_phase_base_commit_fails_closed(self) -> None:
        self.ledger(self.tag)
        self.publish(self.mint(phase_base_commit="b" * 40))
        with self.assertRaises(findings_module.FindingsSyntheticError):
            self.consume([
                self.record(1, "verification", "findings",
                            result_digest=sha256(_canonical_result("findings"))),
            ])

    def test_unrecorded_ledger_tag_fails_closed(self) -> None:
        # A receipt whose phase tag the state digest ledger never recorded is
        # synthetic: the phase never ran under that tag.
        self.publish(self.mint())
        with self.assertRaises(findings_module.FindingsSyntheticError):
            self.consume([
                self.record(1, "verification", "findings",
                            result_digest=sha256(_canonical_result("findings"))),
            ])

    def test_malformed_json_receipt_fails_closed(self) -> None:
        self._write_marker(
            "factory-findings-receipt-round-1-verification.json",
            b"{not json",
        )
        with self.assertRaises(findings_module.FindingsMalformedError):
            self.consume([
                self.record(1, "verification", "findings",
                            result_digest=sha256(_canonical_result("findings"))),
            ])

    def test_non_object_receipt_fails_closed(self) -> None:
        self._write_marker(
            "factory-findings-receipt-round-1-verification.json",
            b'["array", 1]',
        )
        with self.assertRaises(findings_module.FindingsMalformedError):
            self.consume([self.record(1, "verification", "findings")])

    def test_schema_violating_receipt_fails_closed(self) -> None:
        name = "factory-findings-receipt-round-1-verification.json"
        record = self.record(
            1, "verification", "findings",
            result_digest=sha256(_canonical_result("findings")))
        for mutate, fragment in (
            (lambda r: r.pop("outcome"), "required"),
            (lambda r: r.__setitem__("schema", "factory-findings/v1"), "enum"),
            (lambda r: r.__setitem__("extra", 1), "extra"),
            (lambda r: r.__setitem__("round", "1"), "type"),
            (lambda r: r.__setitem__("phase", "developer"), "enum"),
            (lambda r: r.__setitem__("result_digest", "short"), "pattern"),
        ):
            receipt = self.mint()
            mutate(receipt)
            self._write_marker(name, findings_module.receipt_bytes(receipt))
            with self.assertRaises(
                findings_module.FindingsMalformedError, msg=fragment):
                self.consume([record])

    def test_oversized_receipt_fails_closed(self) -> None:
        receipt = self.mint()
        receipt["findings"] = ["x" * (findings_module.MAX_RECEIPT_BYTES + 1)]
        self._write_marker(
            "factory-findings-receipt-round-1-verification.json",
            findings_module.receipt_bytes(receipt),
        )
        with self.assertRaises(findings_module.FindingsMalformedError):
            self.consume([
                self.record(1, "verification", "findings",
                            result_digest=sha256(_canonical_result("findings"))),
            ])

    def test_symlinked_receipt_fails_closed(self) -> None:
        # A symlink at the canonical receipt name is never followed: the
        # hardened no-follow reader fails closed.
        target = self.tmp / "elsewhere.json"
        target.write_text(json.dumps(self.mint()), encoding="utf-8")
        directory = self.root / STATE_DIR
        directory.mkdir(mode=0o700, exist_ok=True)
        (directory / "factory-findings-receipt-round-1-verification.json").symlink_to(
            target)
        with self.assertRaises(findings_module.FindingsMalformedError):
            self.consume([
                self.record(1, "verification", "findings",
                            result_digest=sha256(_canonical_result("findings"))),
            ])

    # -- Task 10 REQ 3: receipt content is authenticated against the exact
    # -- preserved phase-result bytes, never a self-digest only ------------

    def _post_mint_consume(self) -> None:
        self.ledger(self.tag)
        self.preserve(
            1, "verification",
            _canonical_result("findings", findings=["fixture finding"]),
        )
        self.publish(self.mint(
            result_digest=sha256(_canonical_result(
                "findings", findings=["fixture finding"])),
        ))

    def test_post_mint_tampered_findings_vs_preserved_result_fails(self) -> None:
        # After the mint the receipt's findings list is rewritten
        # (schema-valid but contradictory); consumption authenticates the
        # receipt against the exact preserved phase-result bytes and fails
        # closed.
        self._post_mint_consume()
        receipt = json.loads(self._read_marker(
            "factory-findings-receipt-round-1-verification.json"))
        receipt["findings"] = ["tampered finding"]
        self._write_marker(
            "factory-findings-receipt-round-1-verification.json",
            findings_module.receipt_bytes(receipt),
        )
        with self.assertRaises(findings_module.FindingsSyntheticError):
            self.consume([
                self.record(1, "verification", "findings",
                            result_digest=sha256(_canonical_result(
                                "findings", findings=["fixture finding"]))),
            ])

    def test_post_mint_tampered_blocked_refs_vs_preserved_result_fails(self) -> None:
        self.ledger(self.tag)
        self.preserve(
            1, "verification",
            _canonical_result("blocked",
                              blocked_on=["external-capability-required"]),
        )
        result_digest = sha256(_canonical_result(
            "blocked", blocked_on=["external-capability-required"]))
        self.publish(self.mint(
            outcome="blocked", result_digest=result_digest,
            findings=[], blocked_on=["external-capability-required"],
        ))
        receipt = json.loads(self._read_marker(
            "factory-findings-receipt-round-1-verification.json"))
        receipt["blocked_on"] = ["another-capability"]
        self._write_marker(
            "factory-findings-receipt-round-1-verification.json",
            findings_module.receipt_bytes(receipt),
        )
        with self.assertRaises(findings_module.FindingsSyntheticError):
            self.consume([
                self.record(1, "verification", "blocked",
                            result_digest=result_digest),
            ])

    def test_post_mint_tampered_outcome_vs_preserved_result_fails(self) -> None:
        self._post_mint_consume()
        receipt = json.loads(self._read_marker(
            "factory-findings-receipt-round-1-verification.json"))
        receipt["outcome"] = "blocked"
        self._write_marker(
            "factory-findings-receipt-round-1-verification.json",
            findings_module.receipt_bytes(receipt),
        )
        with self.assertRaises(findings_module.FindingsSyntheticError):
            self.consume([
                self.record(1, "verification", "findings",
                            result_digest=sha256(_canonical_result(
                                "findings", findings=["fixture finding"]))),
            ])

    def test_tampered_preserved_result_fails_closed(self) -> None:
        # The preserved phase-result artifact is tampered after the mint:
        # consumption re-binds the receipt's result_digest to the exact
        # artifact bytes and fails closed.
        self._post_mint_consume()
        self._write_marker(
            findings_module.result_name(1, "verification"),
            _canonical_result("findings", findings=["tampered"]),
        )
        with self.assertRaises(findings_module.FindingsSyntheticError):
            self.consume([
                self.record(1, "verification", "findings",
                            result_digest=sha256(_canonical_result(
                                "findings", findings=["fixture finding"]))),
            ])

    def test_symlinked_preserved_result_fails_closed(self) -> None:
        self._post_mint_consume()
        target = self.tmp / "result-elsewhere.json"
        target.write_text(json.dumps({"schema": "factory-phase-result/v1",
                                      "outcome": "findings"}),
                          encoding="utf-8")
        directory = self.root / STATE_DIR
        name = findings_module.result_name(1, "verification")
        (directory / name).unlink()
        (directory / name).symlink_to(target)
        with self.assertRaises(findings_module.FindingsMalformedError):
            self.consume([
                self.record(1, "verification", "findings",
                            result_digest=sha256(_canonical_result(
                                "findings", findings=["fixture finding"]))),
            ])

    def test_oversized_preserved_result_fails_closed(self) -> None:
        self._post_mint_consume()
        self._write_artifact(
            findings_module.result_name(1, "verification"),
            b"{" + b"x" * (findings_module.MAX_RESULT_BYTES + 1),
        )
        with self.assertRaises(findings_module.FindingsMalformedError):
            self.consume([
                self.record(1, "verification", "findings",
                            result_digest=sha256(_canonical_result(
                                "findings", findings=["fixture finding"]))),
            ])

    def test_malformed_preserved_result_json_fails_closed(self) -> None:
        self._post_mint_consume()
        self._write_artifact(
            findings_module.result_name(1, "verification"), b"{not json",
        )
        with self.assertRaises(findings_module.FindingsMalformedError):
            self.consume([
                self.record(1, "verification", "findings",
                            result_digest=sha256(_canonical_result(
                                "findings", findings=["fixture finding"]))),
            ])

    def test_duplicate_key_preserved_result_fails_closed(self) -> None:
        # A repeated JSON object key in the preserved phase-result artifact
        # is rejected at parse time (EVID-02 duplicate-key rejection): a
        # forged artifact cannot hide a drifted field behind a duplicate.
        self._post_mint_consume()
        self._write_artifact(
            findings_module.result_name(1, "verification"),
            b'{"schema":"factory-phase-result/v1","schema":"x",'
            b'"outcome":"findings","findings":["fixture finding"]}',
        )
        with self.assertRaises(findings_module.FindingsMalformedError) as caught:
            self.consume([
                self.record(1, "verification", "findings",
                            result_digest=sha256(_canonical_result(
                                "findings", findings=["fixture finding"]))),
            ])
        self.assertIn("duplicate JSON object key", str(caught.exception))

    def test_missing_preserved_result_fails_closed(self) -> None:
        # A receipt without its preserved phase-result artifact is a
        # receipt-only claim whose content cannot be authenticated.
        self.ledger(self.tag)
        self.publish(self.mint(
            result_digest=sha256(_canonical_result("findings")),
        ))
        with self.assertRaises(findings_module.FindingsReceiptError):
            self.consume([
                self.record(1, "verification", "findings",
                            result_digest=sha256(_canonical_result("findings"))),
            ])

    def test_malformed_ledger_fails_consumption_closed(self) -> None:
        # The strict state-ledger parser is the only ledger authority: a
        # malformed line fails the findings consumption closed instead of
        # being silently tolerated.
        self._post_mint_consume()
        self._write_marker(
            state_module.DIGEST_LEDGER_NAME, b"{not json\n",
        )
        with self.assertRaises(findings_module.FindingsError):
            self.consume([
                self.record(1, "verification", "findings",
                            result_digest=sha256(_canonical_result(
                                "findings", findings=["fixture finding"]))),
            ])


# ---------------------------------------------------------------------------
# Payload schema, bounds, and determinism
# ---------------------------------------------------------------------------


class FindingsPayloadUnit(_FindingsBase):
    def _entry(self, **overrides) -> dict:
        entry = {
            "phase": "verification",
            "phase_base_commit": self.head,
            "outcome": "findings",
            "phase_tag": self.tag,
            "result_digest": sha256(b"x"),
            "receipt_path": "factory-findings-receipt-round-1-verification.json",
            "receipt_digest": sha256(b"r"),
            "findings": ["f"],
            "blocked_on": [],
            "gate_ran": True,
            "gate_exit": 0,
            "capability_ran": False,
            "capability_exit": None,
        }
        entry.update(overrides)
        return entry

    def test_payload_schema_validation(self) -> None:
        payload = {
            "schema": "factory-findings/v1",
            "campaign_id": "campaign",
            "source_round": 1,
            "entries": [self._entry()],
        }
        findings_module.validate_payload(payload)
        for mutate, fragment in (
            (lambda p: p.pop("entries"), "entries"),
            (lambda p: p.__setitem__("schema", "other"), "enum"),
            (lambda p: p.__setitem__("source_round", 0), "minimum"),
            (lambda p: p.__setitem__("extra", 1), "extra"),
            (lambda p: p["entries"].pop(), "minItems"),
            (lambda p: p["entries"][0].__setitem__("outcome", "pass"), "enum"),
            (lambda p: p["entries"][0].__setitem__("phase", "planning"), "enum"),
            (lambda p: p["entries"][0].__setitem__(
                "phase_base_commit", "x"), "pattern"),
            (lambda p: p["entries"][0].__setitem__("extra", 1), "extra"),
            (lambda p: p["entries"][0].__setitem__("gate_ran", 1), "type"),
            (lambda p: p["entries"][0].__setitem__("gate_exit", -1), "minimum"),
            (lambda p: p["entries"][0].__setitem__("capability_exit", "x"), "type"),
            (lambda p: p["entries"][0].pop("gate_ran"), "required"),
        ):
            clone = json.loads(json.dumps(payload))
            mutate(clone)
            with self.assertRaises(
                findings_module.FindingsError, msg=fragment):
                findings_module.validate_payload(clone)

    def test_build_payload_requires_entries(self) -> None:
        with self.assertRaises(findings_module.FindingsError):
            findings_module.build_payload(
                campaign_id="campaign", source_round=1, entries=[])
        with self.assertRaises(findings_module.FindingsError):
            findings_module.build_payload(
                campaign_id="campaign", source_round=0,
                entries=[{"phase": "verification"}])

    def test_payload_bytes_are_canonical_and_bounded(self) -> None:
        entry = self._entry()
        payload = findings_module.build_payload(
            campaign_id="campaign", source_round=1, entries=[entry])
        raw = findings_module.payload_bytes(payload)
        self.assertLess(len(raw), findings_module.MAX_PAYLOAD_BYTES)
        # Canonical sorted-key JSON, no wall-clock time, no prose claims.
        self.assertEqual(
            raw, json.dumps(payload, sort_keys=True,
                            separators=(",", ":")).encode("utf-8"))
        self.assertNotIn(b"timestamp", raw)
        self.assertNotIn(b"prose", raw)
        # The same bytes are produced deterministically.
        self.assertEqual(
            raw,
            findings_module.payload_bytes(findings_module.build_payload(
                campaign_id="campaign", source_round=1, entries=[entry])),
        )


# ---------------------------------------------------------------------------
# Launch authority: planner-only digest-bound payload, never other roles
# ---------------------------------------------------------------------------


class FindingsLaunchUnit(unittest.TestCase):
    """§16: the findings payload is a digest-bound planner-only input."""

    def _make(self, role: str = "planner", **kwargs):
        role_bytes = b"role prompt"
        agents = b"AGENTS.md"
        spec = b"spec"
        plan = b"plan"
        binding = launch_module.InvocationBinding(
            role=role,
            model="synthetic-model",
            provider="synthetic",
            backend=Path(TRUE_EXECUTABLE),
            workspace=Path("/tmp"),
            bound_commit="a" * 40,
            role_prompt_digest=sha256(role_bytes),
            prompt_set_digest=sha256(b"set"),
            plan_digest=sha256(plan),
            policy_digest=sha256(agents),
            specification_digest=sha256(spec),
            allowed_tools=("bash",),
            runtime_limit=60.0,
            inactivity_limit=30.0,
            **kwargs,
        )
        return binding, role_bytes, agents, spec, plan

    def test_planner_findings_digest_binds_exact_bytes(self) -> None:
        findings = findings_module.payload_bytes({
            "schema": "factory-findings/v1", "campaign_id": "campaign",
            "source_round": 1, "entries": [],
        })
        binding, role_bytes, agents, spec, plan = self._make(
            findings_digest=sha256(findings))
        prompt = launch_module.compose_prompt(
            binding, role_prompt=role_bytes, agents=agents, spec=spec,
            plan=plan, findings=findings,
        )
        self.assertIn(b"## Findings from the previous round", prompt)
        self.assertIn(sha256(findings).encode("ascii"), prompt)
        self.assertIn(findings, prompt)
        # The prompt is deterministic: same inputs, same bytes.
        again = launch_module.compose_prompt(
            binding, role_prompt=role_bytes, agents=agents, spec=spec,
            plan=plan, findings=findings,
        )
        self.assertEqual(again, prompt)

    def test_planner_substituted_findings_fail_closed(self) -> None:
        findings = findings_module.payload_bytes({
            "schema": "factory-findings/v1", "campaign_id": "campaign",
            "source_round": 1, "entries": [],
        })
        binding, role_bytes, agents, spec, plan = self._make(
            findings_digest=sha256(findings))
        with self.assertRaises(launch_module.InvocationError):
            launch_module.compose_prompt(
                binding, role_prompt=role_bytes, agents=agents, spec=spec,
                plan=plan, findings=b"paraphrased findings",
            )

    def test_findings_without_digest_fail_closed(self) -> None:
        binding, role_bytes, agents, spec, plan = self._make()
        with self.assertRaises(launch_module.InvocationError):
            launch_module.compose_prompt(
                binding, role_prompt=role_bytes, agents=agents, spec=spec,
                plan=plan, findings=b"payload",
            )

    def test_malformed_findings_digest_fails_closed(self) -> None:
        for digest in ("short", "Z" * 64):
            binding, _, _, _, _ = self._make(findings_digest=digest)
            with self.assertRaises(launch_module.InvocationError):
                launch_module.verify_invocation(binding)

    def test_non_planner_findings_fail_closed(self) -> None:
        findings = b"payload"
        for role in ("developer", "tester", "auditor"):
            kwargs = {}
            if role == "developer":
                kwargs.update(task_id=1, task_excerpt_digest=sha256(b"task"))
            if role == "auditor":
                kwargs.update(audit_objective_digest=sha256(b"obj"))
            binding, _, _, _, _ = self._make(
                role=role, findings_digest=sha256(findings), **kwargs)
            with self.assertRaises(launch_module.InvocationError, msg=role):
                launch_module.verify_invocation(binding)

    def test_compose_prompt_rejects_findings_for_non_planner(self) -> None:
        findings = b"payload"
        binding, role_bytes, agents, spec, plan = self._make(
            role="tester", findings_digest=sha256(findings))
        with self.assertRaises(launch_module.InvocationError):
            launch_module.compose_prompt(
                binding, role_prompt=role_bytes, agents=agents, spec=spec,
                plan=plan, findings=findings,
            )


# ---------------------------------------------------------------------------
# Production launch: exact payload digest binding (planner)
# ---------------------------------------------------------------------------


class FindingsProductionLaunch(unittest.TestCase):
    """B2: the production planner launch binds the exact payload digest."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="factory-findings-launch."))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def _production_config(self, ws):
        # The real (non-driver) launch path requires a committed absolute
        # backend; the fixture driver file is an existing committed regular
        # file, and authorize_launch is mocked so no process is spawned.
        return dataclasses.replace(
            ws.derive_config(), backend=str(ws.root / DRIVER_REL))

    def test_production_planner_binds_exact_findings_digest(self) -> None:
        ws = FixtureWorkspace(self.tmp / "ws1", scenario=SUCCESS_SCENARIO)
        ws.commit_scenario()
        config = self._production_config(ws)
        head = _git(ws.root, "rev-parse", "HEAD").stdout.strip()
        findings = findings_module.payload_bytes({
            "schema": "factory-findings/v1", "campaign_id": "campaign",
            "source_round": 1, "entries": [],
        })
        captured: dict = {}

        def _authorize(binding, **kwargs):
            captured["findings"] = kwargs.get("findings")
            captured["findings_digest"] = binding.findings_digest
            return object()

        class _Supervisor:
            def __init__(self, binding):
                self.binding = binding

            def run(self, authority):
                return type("_Result", (), {"outcome": "exited",
                                            "returncode": 0})()

        with unittest.mock.patch.object(
            campaign_module.launch_module, "authorize_launch",
            side_effect=_authorize,
        ), unittest.mock.patch.object(
            campaign_module.launch_module, "LaunchSupervision",
            side_effect=_Supervisor,
        ):
            outcome = campaign_module.launch_role_attempt(
                config, role="planner", head=head, round_number=2,
                findings_payload=findings,
            )
        self.assertEqual(outcome.exit_status, 0)
        self.assertFalse(outcome.interrupted)
        self.assertEqual(captured["findings"], findings)
        self.assertEqual(captured["findings_digest"], sha256(findings))

    def test_production_launch_refuses_findings_for_developer(self) -> None:
        ws = FixtureWorkspace(self.tmp / "ws2", scenario=SUCCESS_SCENARIO)
        ws.commit_scenario()
        config = self._production_config(ws)
        head = _git(ws.root, "rev-parse", "HEAD").stdout.strip()
        with self.assertRaises(campaign_module.CampaignPhaseError):
            campaign_module.launch_role_attempt(
                config, role="developer", head=head, task_id=1,
                findings_payload=b"payload",
            )


# ---------------------------------------------------------------------------
# Positive end-to-end: findings -> receipt -> payload -> revised plan -> dev
# ---------------------------------------------------------------------------


class FindingsWorkspace(FixtureWorkspace):
    """Fixture workspace that also commits the findings-revised templates."""

    FINDING_MARKER = "fixture finding addressed"
    BLOCKER = "external-capability-required"

    def _generate_plans(self, common: dict) -> None:
        super()._generate_plans(common)
        ws = self.root
        # Round N (>= 2) findings-revised templates: tasks before N stay
        # complete, the runnable task N carries the accepted finding as a
        # scope revision, and the final audit task stays pending.
        for round_no in (2, 3):
            revised = [
                {
                    **t,
                    "scope": (
                        t.get("scope", "fixture-scoped work only.")
                        + (f" Revised after findings: {self.FINDING_MARKER}."
                           if t["number"] == round_no else "")
                    ),
                    "status": "complete" if t["number"] < round_no else t["status"],
                }
                for t in TASK_SPECS
            ]
            gen_plan(
                ws, common,
                f"fixture/templates/planner-findings-revised-{round_no}.md",
                revised,
            )
        # External-blocker variant: the planner keeps the unavailable
        # capability explicit as a blocked task with the exact reference
        # while the next runnable task carries the accepted finding.
        for round_no in (2, 3):
            blocked_revised = [
                {
                    **t,
                    "scope": (
                        t.get("scope", "fixture-scoped work only.")
                        + (f" | Revised after findings: {self.FINDING_MARKER}."
                           if t["number"] == round_no else "")
                    ),
                    "status": (
                        "blocked" if t["number"] == 1
                        else "complete" if t["number"] < round_no
                        else t["status"]
                    ),
                    "blocked_on": (
                        self.BLOCKER if t["number"] == 1
                        else t.get("blocked_on")
                    ),
                }
                for t in TASK_SPECS
            ]
            gen_plan(
                ws, common,
                f"fixture/templates/planner-findings-blocked-revised-{round_no}.md",
                blocked_revised,
            )


class _FindingsCampaign(unittest.TestCase):
    """Shared campaign-flow helpers (one fresh workspace per test)."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="factory-findings-campaign."))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self._workspace_count = 0

    def make(self, scenario: dict, **kwargs) -> FindingsWorkspace:
        self._workspace_count += 1
        ws = FindingsWorkspace(
            self.tmp / f"ws{self._workspace_count}",
            scenario=scenario, **kwargs,
        )
        ws.commit_scenario()
        return ws

    @staticmethod
    def _phase_record(data: dict, round_no: int, phase: str) -> dict:
        return next(
            r for r in data["phase_history"]
            if r["round"] == round_no and r["phase"] == phase
        )

    def _receipt(self, ws, round_no: int, phase: str) -> dict:
        raw = (ws.root / STATE_DIR /
               f"factory-findings-receipt-round-{round_no}-{phase}.json").read_bytes()
        return json.loads(raw.decode("utf-8"))

    def _ledger_tags(self, ws) -> list[str]:
        raw = (ws.root / STATE_DIR / state_module.DIGEST_LEDGER_NAME).read_text()
        return [
            json.loads(line)["tag"]
            for line in raw.splitlines() if line.strip()
        ]

    def _plan_at_commit(self, ws, subject: str) -> bytes:
        lines = _git(
            ws.root, "log", "--format=%H %s", "--all").stdout.strip().splitlines()
        for line in lines:
            commit, sep, rest = line.partition(" ")
            if sep and rest == subject:
                return _git(
                    ws.root, "show",
                    f"{commit}:{PLAN_REL}").stdout.encode("utf-8")
        self.fail(f"no commit with subject {subject!r}")

    def _write_marker(self, ws, name: str, data: bytes) -> None:
        directory = ws.root / STATE_DIR
        directory.mkdir(mode=0o700, exist_ok=True)
        path = directory / name
        path.write_bytes(data)
        os.chmod(path, 0o600)


class FindingsCampaignFlow(_FindingsCampaign):
    """§16 positive end-to-end flows."""

    def test_tester_findings_flow_to_next_planner_revision(self) -> None:
        # Round 1 tester findings -> orchestrator receipt -> round 2 planner
        # receives the deterministic payload and revises the canonical plan
        # -> only then the developer runs (the revised task).
        ws = self.make({
            "planner": {"behavior": {"1": "planned", "2": "findings-revised",
                                     "default": "planned"}},
            "developer": {"behavior": "complete"},
            "tester": {"behavior": {"1": "findings", "2": "pass",
                                    "default": "pass"}},
            "auditor": {"behavior": "pass"},
        }, rounds=2)
        rc, data = ws.run_cli()
        self.assertEqual(rc, 0)
        assert_terminal(self, data, terminal_phase="success",
                        terminal_outcome="pass", exit_code=0,
                        rounds_completed=2)
        assert_history(self, data, [
            (1, "planning", "planned"),
            (1, "implementation", "task_completed"),
            (1, "verification", "findings"),
            (1, "audit", "pass"),
            (2, "planning", "planned"),
            (2, "implementation", "task_completed"),
            (2, "verification", "pass"),
            (2, "audit", "pass"),
        ])
        # Exactly one receipt: round 1 verification (audit passed).
        self.assertTrue((ws.root / receipt_rel(1, "verification")).exists())
        self.assertFalse((ws.root / receipt_rel(1, "audit")).exists())
        receipt = self._receipt(ws, 1, "verification")
        self.assertEqual(receipt["campaign_id"], "campaign")
        self.assertEqual(receipt["round"], 1)
        self.assertEqual(receipt["phase"], "verification")
        self.assertEqual(receipt["outcome"], "findings")
        record = self._phase_record(data, 1, "verification")
        # Exact commit binding: the receipt's phase base equals the recorded
        # head of the phase it ran at, and the tag is in the digest ledger.
        self.assertEqual(receipt["phase_base_commit"], record["head_commit"])
        self.assertIn(receipt["phase_tag"], self._ledger_tags(ws))
        # Exact result binding: the receipt's result digest equals the digest
        # of the exact structured bytes the tester wrote and equals the
        # recorded phase result digest.
        expected = _canonical_result("findings", findings=["fixture finding"])
        self.assertEqual(receipt["result_digest"], sha256(expected))
        self.assertEqual(record["result_digest"], receipt["result_digest"])
        self.assertEqual(receipt["findings"], ["fixture finding"])
        self.assertEqual(receipt["gate_ran"], True)
        self.assertEqual(receipt["gate_exit"], 0)
        # The revised round-2 plan incorporates the finding as a revised task
        # before the developer worked task 2.
        revised_plan = self._plan_at_commit(
            ws, "factory-campaign: planning round 2")
        plan = plan_parser.Plan.from_bytes(revised_plan)
        task2 = next(t for t in plan.tasks if t.number == 2)
        self.assertEqual(task2.status, "pending")
        self.assertIn(FindingsWorkspace.FINDING_MARKER, task2.fields["Scope"])
        # The developer only ran after the revision: in newest-first git log
        # the task-2 completion commit appears before the round-2 planning
        # commit (the planning commit is older).
        log = _git(
            ws.root, "log", "--format=%s", "--all").stdout.strip().splitlines()
        self.assertLess(
            log.index("factory-campaign: task 2 complete"),
            log.index("factory-campaign: planning round 2"),
        )
        # The payload is evidence-only: no runtime payload file is ever
        # persisted (it flows in-memory to the planner prompt).
        names = [p.name for p in (ws.root / STATE_DIR).iterdir()]
        self.assertFalse(any(n.startswith("factory-findings-payload") for n in names))

    def test_auditor_findings_flow(self) -> None:
        ws = self.make({
            "planner": {"behavior": {"1": "planned", "2": "findings-revised",
                                     "default": "planned"}},
            "developer": {"behavior": "complete"},
            "tester": {"behavior": "pass"},
            "auditor": {"behavior": {"1": "findings", "2": "pass",
                                     "default": "pass"}},
        }, rounds=2)
        rc, data = ws.run_cli()
        self.assertEqual(rc, 0)
        assert_terminal(self, data, terminal_phase="success",
                        terminal_outcome="pass", exit_code=0,
                        rounds_completed=2)
        self.assertTrue((ws.root / receipt_rel(1, "audit")).exists())
        self.assertFalse((ws.root / receipt_rel(1, "verification")).exists())
        receipt = self._receipt(ws, 1, "audit")
        self.assertEqual(receipt["outcome"], "findings")
        self.assertEqual(receipt["findings"], ["fixture audit finding"])
        record = self._phase_record(data, 1, "audit")
        self.assertEqual(receipt["phase_base_commit"], record["head_commit"])
        self.assertEqual(
            receipt["result_digest"],
            sha256(_canonical_result(
                "findings", findings=["fixture audit finding"])),
        )
        self.assertEqual(receipt["result_digest"], record["result_digest"])
        self.assertIn(receipt["phase_tag"], self._ledger_tags(ws))

    def test_verification_and_audit_findings_both_flow(self) -> None:
        ws = self.make({
            "planner": {"behavior": {"1": "planned", "2": "findings-revised",
                                     "default": "planned"}},
            "developer": {"behavior": "complete"},
            "tester": {"behavior": {"1": "findings", "2": "pass",
                                    "default": "pass"}},
            "auditor": {"behavior": {"1": "findings", "2": "pass",
                                     "default": "pass"}},
        }, rounds=2)
        rc, data = ws.run_cli()
        self.assertEqual(rc, 0)
        assert_terminal(self, data, terminal_phase="success",
                        terminal_outcome="pass", exit_code=0,
                        rounds_completed=2)
        for phase in ("verification", "audit"):
            self.assertTrue((ws.root / receipt_rel(1, phase)).exists())
        # Both entries reached the round-2 planner (its driver validated the
        # payload) and the revised plan carries the finding.
        revised_plan = self._plan_at_commit(
            ws, "factory-campaign: planning round 2")
        plan = plan_parser.Plan.from_bytes(revised_plan)
        task2 = next(t for t in plan.tasks if t.number == 2)
        self.assertIn(FindingsWorkspace.FINDING_MARKER, task2.fields["Scope"])

    def test_external_blockers_flow_as_structured_findings(self) -> None:
        # Round 1 verification blocked (unavailable declared capability):
        # the receipt carries the exact external references and the round-2
        # planner keeps the blocker explicit in the plan.
        ws = self.make({
            "planner": {"behavior": {"1": "planned", "2": "findings-revised",
                                     "default": "planned"}},
            "developer": {"behavior": "complete"},
            "tester": {"behavior": {"1": "blocked", "2": "pass",
                                    "default": "pass"}},
            "auditor": {"behavior": "pass"},
        }, rounds=2)
        rc, data = ws.run_cli(
            extra=["--capability-command", str(FALSE_EXECUTABLE)])
        self.assertEqual(rc, 0)
        receipt = self._receipt(ws, 1, "verification")
        self.assertEqual(receipt["outcome"], "blocked")
        self.assertEqual(receipt["blocked_on"], ["external-capability-required"])
        record = self._phase_record(data, 1, "verification")
        self.assertEqual(record["outcome"], "blocked")
        self.assertEqual(receipt["result_digest"], record["result_digest"])
        # The blocked-revised template keeps the blocker explicit as a
        # blocked task with the exact reference while the next task carries
        # the accepted finding.
        revised_plan = self._plan_at_commit(
            ws, "factory-campaign: planning round 2")
        plan = plan_parser.Plan.from_bytes(revised_plan)
        task1 = next(t for t in plan.tasks if t.number == 1)
        self.assertEqual(task1.status, "blocked")
        self.assertEqual(task1.blocked_on, FindingsWorkspace.BLOCKER)
        task2 = next(t for t in plan.tasks if t.number == 2)
        self.assertIn(FindingsWorkspace.FINDING_MARKER, task2.fields["Scope"])

    def test_selector_is_not_a_findings_authority(self) -> None:
        # The deterministic selector is a pure function of plan + state; the
        # receipt payload is never consulted and never stored.
        ws = self.make({
            "planner": {"behavior": {"1": "planned", "2": "findings-revised",
                                     "default": "planned"}},
            "developer": {"behavior": "complete"},
            "tester": {"behavior": {"1": "findings", "2": "pass",
                                    "default": "pass"}},
            "auditor": {"behavior": "pass"},
        }, rounds=2)
        rc, data = ws.run_cli()
        self.assertEqual(rc, 0)
        revised_plan = self._plan_at_commit(
            ws, "factory-campaign: planning round 2")
        plan = plan_parser.Plan.from_bytes(revised_plan)
        selection = selector_module.select_task(plan)
        # The revised plan deterministically selects the revised task (2).
        self.assertTrue(selection.selected)
        self.assertEqual(selection.task_id, 2)
        # The selector API surface has no findings channel and the payload
        # is never persisted (the evidence namespace holds receipts only).
        names = [p.name for p in (ws.root / STATE_DIR).iterdir()]
        self.assertFalse(any(
            n.startswith("factory-findings") and "receipt" not in n
            for n in names))


class FindingsCrashWindow(_FindingsCampaign):
    """Task 10 REQ 1: the receipt-mint crash window reconciles deterministically.

    A crash between the trusted publish (preserved phase-result + write-once
    receipt) and the state-advance write leaves the state file recording the
    verification/audit phase at the round's planning base while HEAD already
    advanced through the round's own implementation commits.  The rerun
    re-validates the committed scope and completes the transition from the
    already-published trusted artifacts **without re-running the untrusted
    role** — re-execution could only produce a changed result that the
    byte-exact no-replace preserve would then reject (a wedge).  Torn or
    tampered mint artifacts fail closed for operator inspection.
    """

    def _crash_verification_advance(self, ws) -> dict:
        """Run the campaign, losing the verification -> audit state advance.

        The tester produced findings; the orchestrator preserved the exact
        result bytes and published the write-once receipt before the state
        advance write was lost.  Returns the pre-run config.
        """
        original = state_module.write_state
        crashed = {"raised": False}

        def crashing_write(root, state):
            if not crashed["raised"] and state.current_phase == "audit":
                crashed["raised"] = True
                raise state_module.StateError(
                    "simulated crash: verification state advance lost")
            return original(root, state)

        config = ws.derive_config()
        with unittest.mock.patch.object(
            campaign_module.state_module, "write_state",
            side_effect=crashing_write,
        ):
            with self.assertRaises(state_module.StateError):
                campaign_module.Campaign(config).run()
        self.assertTrue(crashed["raised"])
        # The receipt and preserved phase-result were already published, and
        # the state file still records the verification phase at the round
        # base (HEAD advanced through the implementation commits).
        self.assertTrue((ws.root / receipt_rel(1, "verification")).exists())
        self.assertTrue(
            (ws.root / STATE_DIR /
             findings_module.result_name(1, "verification")).exists())
        state = ws.load_state()
        self.assertEqual(state.current_phase, "verification")
        self.assertNotEqual(
            _git(ws.root, "rev-parse", "HEAD").stdout.strip(),
            state.phase_base_commit,
        )
        return config

    def _count_role_runs(self, config: dict):
        calls: list[str] = []
        original = campaign_module.Campaign._run_role

        def counting(self, role, state, head, **kwargs):
            calls.append(role)
            return original(self, role, state, head, **kwargs)

        with unittest.mock.patch.object(
            campaign_module.Campaign, "_run_role",
            autospec=True, side_effect=counting,
        ):
            result = campaign_module.Campaign(config).run()
        return result, calls

    def test_verification_crash_window_completes_from_published_receipt(
        self,
    ) -> None:
        ws = self.make({
            "planner": {"behavior": "planned"},
            "developer": {"behavior": "complete"},
            "tester": {"behavior": "findings"},
            "auditor": {"behavior": "pass"},
        }, rounds=1)
        config = self._crash_verification_advance(ws)
        result, calls = self._count_role_runs(config)
        self.assertEqual(result.terminal_phase, "success")
        self.assertEqual(result.terminal_outcome, "pass")
        self.assertEqual(result.rounds_completed, 1)
        # The verification transition was reconciled from the published
        # receipt: the untrusted tester never re-ran (only the audit ran).
        self.assertEqual(calls, ["auditor"])
        self.assertEqual(len(result.phase_history), 2)
        recovered = result.phase_history[0]
        self.assertEqual((recovered.round, recovered.phase),
                         (1, "verification"))
        self.assertEqual(recovered.outcome, "findings")
        self.assertEqual(
            recovered.detail, "reconciled from the published findings receipt")
        self.assertEqual(recovered.result_digest,
                         sha256(_canonical_result(
                             "findings", findings=["fixture finding"])))
        self.assertEqual(result.phase_history[1].phase, "audit")
        self.assertEqual(result.phase_history[1].outcome, "pass")
        # The receipt evidence survives intact with every binding.
        receipt = self._receipt(ws, 1, "verification")
        self.assertEqual(receipt["outcome"], "findings")
        self.assertEqual(
            receipt["phase_base_commit"],
            _git(ws.root, "rev-parse", "HEAD").stdout.strip(),
        )
        self.assertIn(receipt["phase_tag"], self._ledger_tags(ws))
        preserved = (ws.root / STATE_DIR /
                     findings_module.result_name(1, "verification")).read_bytes()
        self.assertEqual(receipt["result_digest"], sha256(preserved))

    def test_audit_crash_window_completes_terminal_from_published_receipt(
        self,
    ) -> None:
        # Final-round audit findings: the crash loses the terminal state
        # advance after the receipt was published; the rerun completes the
        # audit directly into the ``findings`` terminal from the receipt.
        ws = self.make({
            "planner": {"behavior": "planned"},
            "developer": {"behavior": "complete"},
            "tester": {"behavior": "pass"},
            "auditor": {"behavior": "findings"},
        }, rounds=1)
        original = state_module.write_state
        crashed = {"raised": False}

        def crashing_write(root, state):
            if not crashed["raised"] and state.current_phase == "findings":
                crashed["raised"] = True
                raise state_module.StateError(
                    "simulated crash: audit state advance lost")
            return original(root, state)

        config = ws.derive_config()
        with unittest.mock.patch.object(
            campaign_module.state_module, "write_state",
            side_effect=crashing_write,
        ):
            with self.assertRaises(state_module.StateError):
                campaign_module.Campaign(config).run()
        self.assertTrue(crashed["raised"])
        self.assertTrue((ws.root / receipt_rel(1, "audit")).exists())
        self.assertEqual(ws.load_state().current_phase, "audit")
        result, calls = self._count_role_runs(config)
        self.assertEqual(result.terminal_phase, "findings")
        self.assertEqual(result.terminal_outcome, "findings")
        self.assertEqual(result.rounds_completed, 1)
        # No role ran at all: the terminal was completed from the receipt.
        self.assertEqual(calls, [])
        self.assertEqual(len(result.phase_history), 1)
        recovered = result.phase_history[0]
        self.assertEqual((recovered.round, recovered.phase),
                         (1, "audit"))
        self.assertEqual(recovered.outcome, "findings")
        self.assertEqual(
            recovered.detail, "reconciled from the published findings receipt")

    def test_verification_crash_window_before_mint_reruns_phase_idempotently(
        self,
    ) -> None:
        # A crash DURING the untrusted verification phase (before any
        # preserve/mint) leaves no receipt and no preserved artifact; the
        # rerun re-validates the committed scope, re-runs the phase normally
        # at HEAD, and the fresh preserve/mint of the reproduced result
        # complete without wedging (REQ 1).
        ws = self.make({
            "planner": {"behavior": "planned"},
            "developer": {"behavior": "complete"},
            "tester": {"behavior": "findings"},
            "auditor": {"behavior": "pass"},
        }, rounds=1)
        config = ws.derive_config()
        original_run_role = campaign_module.Campaign._run_role
        crashed = {"raised": False}

        def crashing_run_role(self, role, state, head, **kwargs):
            if role == "tester" and not crashed["raised"]:
                crashed["raised"] = True
                raise state_module.StateError(
                    "simulated crash: the verifier died before the mint")
            return original_run_role(self, role, state, head, **kwargs)

        with unittest.mock.patch.object(
            campaign_module.Campaign, "_run_role",
            autospec=True, side_effect=crashing_run_role,
        ):
            with self.assertRaises(state_module.StateError):
                campaign_module.Campaign(config).run()
        self.assertTrue(crashed["raised"])
        # No mint artifacts exist: the crash predates the preserve/mint.
        self.assertFalse((ws.root / receipt_rel(1, "verification")).exists())
        self.assertFalse(
            (ws.root / STATE_DIR /
             findings_module.result_name(1, "verification")).exists())
        self.assertEqual(ws.load_state().current_phase, "verification")
        # The rerun re-runs the campaign from the verification phase; the
        # tester runs again and the phase completes normally.
        result, calls = self._count_role_runs(config)
        self.assertEqual(result.terminal_phase, "success")
        self.assertEqual(result.terminal_outcome, "pass")
        self.assertEqual(calls, ["tester", "auditor"])
        self.assertTrue((ws.root / receipt_rel(1, "verification")).exists())

    def test_torn_preserved_without_receipt_fails_closed_during_recovery(
        self,
    ) -> None:
        # A crash between the preserve and the mint leaves a preserved
        # phase-result with no receipt: recovery fails closed for operator
        # inspection instead of guessing or re-running over the evidence.
        ws = self.make({
            "planner": {"behavior": "planned"},
            "developer": {"behavior": "complete"},
            "tester": {"behavior": "findings"},
            "auditor": {"behavior": "pass"},
        }, rounds=1)
        config = self._crash_verification_advance(ws)
        (ws.root / receipt_rel(1, "verification")).unlink()
        with self.assertRaises(campaign_module.CampaignRecoveryError) as cm:
            campaign_module.Campaign(config).run()
        self.assertIn("without its findings receipt", str(cm.exception))

    def test_torn_receipt_without_preserved_fails_closed_during_recovery(
        self,
    ) -> None:
        # A receipt whose preserved phase-result artifact is missing is a
        # receipt-only claim whose content cannot be authenticated; recovery
        # fails closed instead of trusting the self-digest.
        ws = self.make({
            "planner": {"behavior": "planned"},
            "developer": {"behavior": "complete"},
            "tester": {"behavior": "findings"},
            "auditor": {"behavior": "pass"},
        }, rounds=1)
        config = self._crash_verification_advance(ws)
        (ws.root / STATE_DIR /
         findings_module.result_name(1, "verification")).unlink()
        with self.assertRaises(campaign_module.CampaignRecoveryError) as cm:
            campaign_module.Campaign(config).run()
        self.assertIn("no preserved phase-result artifact", str(cm.exception))

    def test_tampered_receipt_fails_closed_during_recovery(self) -> None:
        # A schema-valid but contradictory receipt (findings rewritten after
        # the mint) is authenticated against the exact preserved phase-result
        # bytes during recovery and fails closed.
        ws = self.make({
            "planner": {"behavior": "planned"},
            "developer": {"behavior": "complete"},
            "tester": {"behavior": "findings"},
            "auditor": {"behavior": "pass"},
        }, rounds=1)
        config = self._crash_verification_advance(ws)
        receipt = json.loads((ws.root / receipt_rel(1, "verification")).read_text())
        receipt["findings"] = ["forged finding"]
        self._write_marker(
            ws, "factory-findings-receipt-round-1-verification.json",
            findings_module.receipt_bytes(receipt),
        )
        with self.assertRaises(campaign_module.CampaignRecoveryError) as cm:
            campaign_module.Campaign(config).run()
        self.assertIn("contradict", str(cm.exception))


class FindingsCampaignFailClosed(_FindingsCampaign):
    """Pre-planted/malformed/oversized/symlinked receipts fail the campaign."""

    def test_preplanted_receipt_fails_the_mint_closed(self) -> None:
        # A forged receipt pre-planted at the canonical name of a phase that
        # will produce findings: the genuine mint cannot publish over it.
        ws = self.make({
            "planner": {"behavior": "planned"},
            "developer": {"behavior": "complete"},
            "tester": {"behavior": "findings"},
            "auditor": {"behavior": "pass"},
        }, rounds=1)
        planted = findings_module.build_receipt(
            campaign_id="campaign", round_number=1, phase="verification",
            phase_tag="r1.verification.1.a1", phase_base_commit="0" * 40,
            outcome="findings", result_digest=sha256(b"preplant"),
        )
        self._write_marker(
            ws, "factory-findings-receipt-round-1-verification.json",
            findings_module.receipt_bytes(planted))
        rc, data = ws.run_cli()
        self.assertEqual(rc, campaign_module.EXIT_ERROR)
        self.assertIsNone(data)

    def test_preplanted_synthetic_receipt_fails_consumption_closed(self) -> None:
        # A forged receipt for a phase that will record a pass: round 1 runs
        # clean, round 2 planning consumption rejects the synthetic receipt.
        ws = self.make(SUCCESS_SCENARIO, rounds=2)
        planted = findings_module.build_receipt(
            campaign_id="campaign", round_number=1, phase="verification",
            phase_tag="r1.verification.1.a1", phase_base_commit="0" * 40,
            outcome="findings", result_digest=sha256(b"preplant"),
        )
        self._write_marker(
            ws, "factory-findings-receipt-round-1-verification.json",
            findings_module.receipt_bytes(planted))
        rc, data = ws.run_cli()
        self.assertEqual(rc, campaign_module.EXIT_ERROR)
        self.assertIsNone(data)

    def test_malformed_receipt_fails_consumption_closed(self) -> None:
        ws = self.make(SUCCESS_SCENARIO, rounds=2)
        self._write_marker(
            ws, "factory-findings-receipt-round-1-verification.json",
            b"{not valid json")
        rc, data = ws.run_cli()
        self.assertEqual(rc, campaign_module.EXIT_ERROR)
        self.assertIsNone(data)

    def test_oversized_receipt_fails_closed(self) -> None:
        ws = self.make(SUCCESS_SCENARIO, rounds=2)
        planted = findings_module.build_receipt(
            campaign_id="campaign", round_number=1, phase="verification",
            phase_tag="r1.verification.1.a1", phase_base_commit="0" * 40,
            outcome="findings", result_digest=sha256(b"x"),
            findings=["x" * (findings_module.MAX_RECEIPT_BYTES + 1)],
        )
        self._write_marker(
            ws, "factory-findings-receipt-round-1-verification.json",
            findings_module.receipt_bytes(planted))
        rc, data = ws.run_cli()
        self.assertEqual(rc, campaign_module.EXIT_ERROR)
        self.assertIsNone(data)

    def test_symlinked_receipt_fails_closed(self) -> None:
        ws = self.make(SUCCESS_SCENARIO, rounds=2)
        directory = ws.root / STATE_DIR
        directory.mkdir(mode=0o700, exist_ok=True)
        target = directory / "elsewhere.json"
        target.write_text(json.dumps({
            "schema": "factory-findings-receipt/v1",
        }), encoding="utf-8")
        (directory / "factory-findings-receipt-round-1-verification.json").symlink_to(
            target)
        rc, data = ws.run_cli()
        self.assertEqual(rc, campaign_module.EXIT_ERROR)
        self.assertIsNone(data)


if __name__ == "__main__":
    unittest.main()
