# Open Bugs

Canonical queue of defects awaiting maintenance.

Schema: `ralph-bug-ledger/v1`

```json
[
  {
    "id": "BUG-0001",
    "title": "conformance test fixture lacks the signed runner-evidence machinery",
    "status": "open",
    "component": ".factory/tests/test-factory-conformance.py",
    "evidence": "At HEAD 37b4f195 the conformance suite was completely broken (all 46 tests errored: the fixture referenced the pre-migration scripts/validate-conformance.py path and missing directories). The Phase 2B2 maintenance pass repaired the stale script paths, the missing .factory/tools/ and tests/fixtures/ directories, the mismatched receipt/probe paths, and the missing runner-evidence checker modules, bringing the suite to 45/46. The remaining failure (test_declared_but_unevidenced_capability_fails_planning) requires a full signed runner-evidence fixture (issuance/current signer-trust policy at .factory/signer-trust.json, a signed runner manifest, and the aggregate under .factory-state/runner-evidence/) that the fixture does not provide; the checker fails earlier on the missing signer-trust policy instead of the asserted 'runner evidence aggregate is missing' message. The suite is not part of ./scripts/verify-boilerplate.sh (the gate runs the real .factory/tools/validate-conformance.py against the committed conformance sidecar, which passes).",
    "remediation": "Extend the ConformanceFixture to commit a valid ralph-runner-signer-trust/v1 policy and a signed runner manifest/aggregate so the capability-evidence checker reaches the aggregate-missing assertion, or re-scope the test to assert the honest earlier failure."
  }
]
```
