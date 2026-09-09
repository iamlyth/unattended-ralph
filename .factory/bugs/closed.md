# Closed Bugs

Completed defects and their verification evidence.

Schema: `ralph-bug-ledger/v1`

```json
[
  {
    "id": "BUG-0001",
    "title": "conformance test fixture lacks the signed runner-evidence machinery",
    "status": "closed",
    "severity": "medium",
    "reported": "2026-09-09",
    "external": [],
    "contract_change": false,
    "reproduction": "At HEAD 37b4f195 the conformance suite was completely broken (all 46 tests errored: the fixture referenced the pre-migration scripts/validate-conformance.py path and missing directories). The Phase 2B2 maintenance pass repaired the stale script paths, the missing .factory/tools/ and tests/fixtures/ directories, the mismatched receipt/probe paths, and the missing runner-evidence checker modules, bringing the suite to 45/46. The remaining failure (test_declared_but_unevidenced_capability_fails_planning) requires a full signed runner-evidence fixture (issuance/current signer-trust policy at .factory/signer-trust.json, a signed runner manifest, and the aggregate under .factory-state/runner-evidence/) that the fixture does not provide; the checker fails earlier on the missing signer-trust policy instead of the asserted 'runner evidence aggregate is missing' message. The suite is not part of ./scripts/verify-boilerplate.sh (the gate runs the real .factory/tools/validate-conformance.py against the committed conformance sidecar, which passes).",
    "expected": "The conformance test proves that a declared-but-unevidenced required capability fails planning for the intended reason (no accepted runner receipt), and the full conformance suite is registered in the generic gate.",
    "actual": "The capability-evidence checker fails earlier on the missing signer-trust policy, so the test cannot reach the aggregate-missing failure; the suite is excluded from the generic gate.",
    "acceptance": "The conformance suite passes 46/46 with the unevidenced-capability test failing closed for the intended reason; the suite is registered in verify-boilerplate.sh; fake/tampered/missing signatures still fail closed.",
    "resolution": "Committed a valid ralph-runner-signer-trust/v1 policy and runner declaration so the capability-evidence checker reaches the aggregate-missing failure; renamed the misleading assertion; registered the conformance suite in verify-boilerplate.sh.",
    "verification": "python3 .factory/tests/test-factory-conformance.py passes 46/46; verify-boilerplate.sh passes.",
    "closed": "2026-09-09"
  }
]
```
