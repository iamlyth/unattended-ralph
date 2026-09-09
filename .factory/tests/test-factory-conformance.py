#!/usr/bin/env python3
"""Harness-owned conformance tests for the conformance sidecar validator (Task 14).

This test lives under the hidden ``.factory/tests/`` namespace because the
specification (HIDE-01, §3) keeps harness-only tests out of the adopting
product's visible test tree.  It is the deterministic verification for Task 14:

* the §24 registry (``factory-plan-v1/requirements``) is the exact-set
  acceptance boundary: sidecar, plan matrix, and requirement-policy map must
  each carry exactly the registry IDs, and any drift (missing, extra, or
  renumbered ID) fails closed;
* ``required_tier`` and ``required_capabilities`` are assigned by the separate
  requirement-policy map, never self-declared: a sidecar tier or capability
  set that differs from the policy fails, and a capability relaxation fails;
* a ``verified`` claim is never satisfied by evidence below the policy-required
  tier, by a human-tier self-attestation, or by a free-text row: receipt/
  artifact refs must be Git blobs at the declared evidence commit in planning
  mode as well as complete mode (stale working-tree-only refs fail), and the
  evidence commit must resolve to a real commit;
* path traversal is rejected: absolute refs, ``..`` components,
  prefix-boundary aliases (``.factoryx/…``), backslashes, empty components,
  and control characters all fail closed;
* ``blocked``/``partial`` rows are fact-bound (open, bidirectional) and
  ``missing``/``ambiguous``/``not_applicable`` rows may carry no evidence refs
  and may not claim any tier above the base — a stub row cannot be proxied
  into apparent acceptance;
* the complete mode still requires every row ``verified`` with evidenced
  capabilities, and an undeclared capability (e.g. the real-system runner
  the boilerplate does not provision) keeps its row blocked forever.

The suite is hermetic: every scenario runs in a test-owned temporary Git
repository with the committed validator copied in; it never writes to the
live repository and never touches ``.ralph/``.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
SCRIPTS = ROOT / ".factory" / "tools"
FIXTURES = ROOT / ".factory" / "tests" / "fixtures" / "conformance"

VALIDATOR = SCRIPTS / "validate-conformance.py"
FACTS_VALIDATOR = SCRIPTS / "validate-blocked-facts.py"
CAPABILITY_CHECKER = SCRIPTS / "check-capability-evidence.py"
CONTRACT_CHECKER = SCRIPTS / "check-capability-contracts.py"
RUNNER_CHECKER = SCRIPTS / "check-factory-runner-evidence.py"


def run(argv, cwd: Path, check: bool = False,
        env: dict | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        argv, cwd=cwd, text=True, capture_output=True, check=check, env=env,
    )


def git(cwd: Path, *argv: str) -> str:
    result = run(["git", "-C", str(cwd), *argv], cwd, check=True)
    return result.stdout.strip()


class ConformanceFixture:
    """One hermetic repository with the validator, plan, and ledgers."""

    def __init__(self, root: Path) -> None:
        self.root = root
        for rel in (".factory/artifacts", ".factory/schemas", ".factory/loop",
                    ".factory/tools", ".factory-state",
                    ".factory/tests/legacy/fixtures", "docs", "scripts",
                    "tests/fixtures"):
            (root / rel).mkdir(parents=True, exist_ok=True)
        for script in (VALIDATOR, FACTS_VALIDATOR, CAPABILITY_CHECKER,
                        CONTRACT_CHECKER, RUNNER_CHECKER,
                        SCRIPTS / "check-factory-environment.py"):
            shutil.copy2(script, root / ".factory" / "tools" / script.name)
        shutil.copy2(
            SCRIPTS / "factory_runner_artifacts.py",
            root / ".factory" / "tools" / "factory_runner_artifacts.py",
        )
        # The validators run every trusted Git call through the committed
        # pinned-Git authority (.factory/loop/gitutil.py).  The fixture
        # receives the exact committed module (never a weakened stub): the
        # same immutable-chain PATH-pinned resolver the real control plane
        # uses, so fake PATH/GIT_DIR/GIT_CONFIG fixtures cannot redirect it.
        gitutil = ROOT / ".factory" / "loop" / "gitutil.py"
        if gitutil.is_symlink() or not gitutil.is_file():
            raise AssertionError(f"missing pinned-Git authority: {gitutil}")
        shutil.copy2(gitutil, root / ".factory" / "loop" / "gitutil.py")
        (root / ".factory" / "tools" / "verify-boilerplate.sh").write_text(
            "#!/usr/bin/env bash\nexit 0\n", encoding="utf-8"
        )
        (root / ".factory" / "tools" / "verify-boilerplate.sh").chmod(0o755)
        (root / "docs" / "SPEC.md").write_text("# Spec\n", encoding="utf-8")
        (root / ".gitignore").write_text(".factory-state/\n", encoding="utf-8")
        (root / ".factory" / "environment.toml").write_text(
            "schema_version = 1\n", encoding="utf-8"
        )
        (root / ".factory" / "schemas" / "factory-plan-v1.requirements.json").write_text(
            json.dumps({"schema": "factory-plan/v1/requirements",
                        "requirement_ids": ["REQ-01", "REQ-02", "REQ-03"]}),
            encoding="utf-8",
        )
        self.plan_path = root / ".factory" / "artifacts" / "implementation-plan.md"
        self.sidecar_path = root / ".factory" / "artifacts" / "conformance.json"
        self.facts_path = root / ".factory" / "artifacts" / "blocked-facts.json"
        self.policy_path = root / ".factory" / "requirement-policy.json"
        git(root, "init", "-q", "-b", "develop")
        git(root, "config", "user.name", "test")
        git(root, "config", "user.email", "test@example.invalid")

    def commit_all(self, message: str) -> str:
        git(self.root, "add", "-A")
        git(self.root, "commit", "-qm", message)
        return git(self.root, "rev-parse", "HEAD")

    def write_plan(self) -> None:
        self.plan_path.write_text(textwrap.dedent("""\
            ---
            status: active
            ---
            # Implementation Plan
            ## Specification conformance matrix
            | ID | Spec § | Classification | Evidence | Task |
            |----|--------|--------------|----------|------|
            | REQ-01 | §1 | verified | probe evidence | Task 1 |
            | REQ-02 | §2 | partial | probe evidence | Task 1 |
            | REQ-03 | §3 | blocked | probe evidence | Task 1 |
            """), encoding="utf-8")

    def write_policy(self) -> None:
        self.policy_path.write_text(json.dumps(
            {"schema": "ralph-requirement-policy/v1", "requirements": [
                {"id": "REQ-01", "required_tier": "unit", "spec_sections": ["§1"], "required_capabilities": []},
                {"id": "REQ-02", "required_tier": "unit", "spec_sections": ["§2"], "required_capabilities": []},
                {"id": "REQ-03", "required_tier": "real_system", "spec_sections": ["§3"], "required_capabilities": ["probe-capability"]},
            ]},
        ), encoding="utf-8")

    def write_facts(self) -> None:
        self.facts_path.write_text(json.dumps(
            {"schema": "ralph-blocked-facts/v1", "facts": [
                {"id": "FACT-001", "title": "REQ-02 evidence unavailable",
                 "status": "open", "capabilities": [],
                 "requirements": ["REQ-02"],
                 "blocking_evidence": "fixture lacks the required probe",
                 "resolution": None},
                {"id": "FACT-002", "title": "REQ-03 evidence unavailable",
                 "status": "open", "capabilities": ["probe-capability"],
                 "requirements": ["REQ-03"],
                 "blocking_evidence": "no real-system probe in the fixture",
                 "resolution": None},
            ]},
        ), encoding="utf-8")

    def seed_refs(self) -> None:
        (self.root / ".factory" / "tests" / "legacy" / "fixtures" /
         "runner-manifest.json").write_text(
            json.dumps({"schema": "factory-runner-receipt/v1", "result": "pass", "exit_code": 0}),
            encoding="utf-8",
        )
        (self.root / ".factory" / "tests" / "legacy" / "probe.c").write_text(
            "int probe(void){return 0;}\n", encoding="utf-8",
        )

    def write_sidecar(self) -> None:
        head = git(self.root, "rev-parse", "HEAD")
        self.sidecar_path.write_text(json.dumps(
            {"schema": "ralph-conformance/v1", "requirements": [
                {"id": "REQ-01", "spec_sections": ["§1"], "classification": "verified",
                 "evidence_tier": "unit", "required_tier": "unit",
                 "required_capabilities": [], "evidence_commit": head,
                 "receipts": [".factory/tests/legacy/fixtures/runner-manifest.json"],
                 "artifacts": [".factory/tests/legacy/probe.c"], "fact_refs": [], "reason": ""},
                {"id": "REQ-02", "spec_sections": ["§2"], "classification": "partial",
                 "evidence_tier": "unit", "required_tier": "unit",
                 "required_capabilities": [], "evidence_commit": head,
                 "receipts": [], "artifacts": [], "fact_refs": ["FACT-001"],
                 "reason": ""},
                {"id": "REQ-03", "spec_sections": ["§3"], "classification": "blocked",
                 "evidence_tier": "private_integration", "required_tier": "real_system",
                 "required_capabilities": ["probe-capability"], "evidence_commit": head,
                 "receipts": [], "artifacts": [], "fact_refs": ["FACT-002"],
                 "reason": "capability is not real-system evidenced"},
            ]},
        ), encoding="utf-8")

    def blessed(self) -> None:
        """Build the blessed repository: every authority committed and valid."""
        self.write_plan()
        self.write_policy()
        self.write_facts()
        self.seed_refs()
        self.commit_all("plan and policy and refs")
        self.write_sidecar()
        self.commit_all("sidecar")

    def apply_fixture(self, name: str) -> None:
        """Overwrite the sidecar with a committed adversarial fixture.

        The fixture files under ``.factory/tests/fixtures/conformance/`` are
        the reviewable adversarial corpus of Task 14.  The fixture's
        ``evidence_commit`` is a documented placeholder: the hermetic
        repository rebinds it to the fixture repository HEAD so the planted
        defect (traversal, tier, capability, stale ref, missing row) is the
        only failing check.
        """
        source = FIXTURES / name
        if source.is_symlink() or not source.is_file():
            raise AssertionError(f"missing conformance fixture: {source}")
        data = json.loads(source.read_text(encoding="utf-8"))
        head = git(self.root, "rev-parse", "HEAD")
        for req in data["requirements"]:
            req["evidence_commit"] = head
        self.sidecar_path.write_text(json.dumps(data), encoding="utf-8")

    def validator(self, mode: str, *extra: str,
                  env: dict | None = None) -> subprocess.CompletedProcess:
        return run(
            [sys.executable, "./.factory/tools/validate-conformance.py", mode,
             ".factory/artifacts/conformance.json", *extra],
            self.root,
            env=env,
        )

    def sidecar_patch(self, rid: str, **fields) -> None:
        data = json.loads(self.sidecar_path.read_text(encoding="utf-8"))
        for req in data["requirements"]:
            if req["id"] == rid:
                req.update(fields)
        self.sidecar_path.write_text(json.dumps(data), encoding="utf-8")

    def policy_patch(self, rid: str, **fields) -> None:
        data = json.loads(self.policy_path.read_text(encoding="utf-8"))
        for entry in data["requirements"]:
            if entry["id"] == rid:
                entry.update(fields)
        self.policy_path.write_text(json.dumps(data), encoding="utf-8")


class ConformanceFixtureTests(unittest.TestCase):
    """Blessed state: planning valid, completion blocked by non-verified rows."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="factory-conformance-")
        self.fixture = ConformanceFixture(Path(self._tmp.name))
        self.fixture.blessed()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_blessed_planning_passes(self) -> None:
        result = self.fixture.validator("planning")
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_blessed_complete_rejects_unverified_rows(self) -> None:
        result = self.fixture.validator("complete")
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn("completion rejected", result.stderr)
        self.assertIn("partial", result.stderr)

    def test_capability_evidence_and_contract_gates_pass(self) -> None:
        for script in (CONTRACT_CHECKER, CAPABILITY_CHECKER):
            result = run(["python3", str(script)], self.fixture.root)
            self.assertEqual(result.returncode, 0, result.stderr)


class RuntimeReceiptTests(unittest.TestCase):
    """Live runtime receipts (``.factory-state/audit-receipts/<tag>.json``).

    A ``verified`` row may cite a live runtime receipt instead of a tracked
    Git blob: it is validated against the live filesystem (hardened
    no-follow owner/mode/link-count, ``ralph-audit-receipt/v1`` schema,
    exit 0, exact row evidence_commit, argv/stdout/stderr digests, and the
    protected coordinator round/nonce), while tracked artifacts always
    require a Git blob at the evidence commit.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="factory-conformance-rt-")
        self.fixture = ConformanceFixture(Path(self._tmp.name))
        self.fixture.blessed()
        root = self.fixture.root
        # The hidden evidence authority the validator loads for hardened
        # receipt validation, and the trusted receipt wrapper for minting.
        shutil.copy2(ROOT / ".factory" / "loop" / "evidence.py",
                     root / ".factory" / "loop" / "evidence.py")
        shutil.copy2(ROOT / ".factory" / "loop" / "lock.py",
                     root / ".factory" / "loop" / "lock.py")
        # lock.py validates the composite plan binding for v2 plans and
        # imports the plan parser/sidecar authorities from its own tree.
        shutil.copy2(ROOT / ".factory" / "loop" / "plan_parser.py",
                     root / ".factory" / "loop" / "plan_parser.py")
        shutil.copy2(ROOT / ".factory" / "loop" / "plan_sidecars.py",
                     root / ".factory" / "loop" / "plan_sidecars.py")
        shutil.copy2(ROOT / ".factory" / "tools" / "machine-receipt.py",
                     root / ".factory" / "tools" / "machine-receipt.py")
        self.state_dir = root / ".factory-state"
        self.state_dir.mkdir(mode=0o700, exist_ok=True)
        os.chmod(self.state_dir, 0o700)
        self.nonce = hashlib.sha256(os.urandom(32)).hexdigest()
        self.head = git(root, "rev-parse", "HEAD").strip()
        self._write_coordinator(1, self.head, self.nonce)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _write_coordinator(self, round_number: int, base: str, nonce: str) -> None:
        (self.state_dir / "audit-coordinator.json").write_text(
            json.dumps({
                "schema": "ralph-audit-coordinator/v1",
                "round": round_number,
                "base_commit": base,
                "nonce": nonce,
                "created_at": 1,
            }, sort_keys=True) + "\n", encoding="utf-8")
        os.chmod(self.state_dir / "audit-coordinator.json", 0o600)

    def _mint(self, tag: str, *argv: str) -> subprocess.CompletedProcess[str]:
        return run(
            [sys.executable, ".factory/tools/machine-receipt.py", "--root", str(self.fixture.root),
             "--tag", tag, "--audit-round", "1", "--evidence-commit", self.head,
             "--nonce", self.nonce, "--", *argv],
            self.fixture.root,
        )

    def _sidecar_with_receipt(self, ref: str, *, in_artifacts: bool = False) -> None:
        """Commit a sidecar whose REQ-01 cites ``ref``, then rebind the exact
        evidence head and the coordinator so a freshly minted receipt matches
        the row binding exactly."""
        data = json.loads(self.fixture.sidecar_path.read_text(encoding="utf-8"))
        for req in data["requirements"]:
            if req["id"] == "REQ-01":
                req["receipts"] = [] if in_artifacts else [ref]
                req["artifacts"] = [ref] if in_artifacts else [".factory/tests/legacy/probe.c"]
                req["evidence_commit"] = self.head
        self.fixture.sidecar_path.write_text(json.dumps(data), encoding="utf-8")
        self.head = self.fixture.commit_all("sidecar runtime receipt")
        self._write_coordinator(1, self.head, self.nonce)

    def test_live_runtime_receipt_accepts_verified_row(self) -> None:
        """A real minted installed-harness receipt under the runtime namespace
        satisfies the `receipts` field at the exact row evidence commit."""
        minted = self._mint("installed-harness-smoke", "true")
        self.assertEqual(minted.returncode, 0, minted.stderr)
        self._sidecar_with_receipt(
            ".factory-state/audit-receipts/installed-harness-smoke.json"
        )
        result = self.fixture.validator("planning")
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_runtime_receipt_traversal_is_rejected(self) -> None:
        self._mint("installed-harness-smoke", "true")
        self._sidecar_with_receipt(
            ".factory-state/audit-receipts/../audit-coordinator.json"
        )
        result = self.fixture.validator("planning")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("unsafe path component", result.stderr)

    def test_runtime_namespace_outside_audit_receipts_is_rejected(self) -> None:
        """`.factory-state/...` outside the exact audit-receipts shape is
        never a valid ref (only the allowlisted runtime shape is permitted)."""
        (self.state_dir / "installed-functional-evidence.env").write_text(
            "commit=0" * 20 + "\n", encoding="utf-8")
        self._sidecar_with_receipt(
            ".factory-state/installed-functional-evidence.env"
        )
        result = self.fixture.validator("planning")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("outside the only allowed shape", result.stderr)

    def test_runtime_receipt_in_artifacts_is_rejected(self) -> None:
        """Tracked artifacts always require a Git blob: the runtime shape is
        never a valid `artifacts` ref."""
        self._mint("installed-harness-smoke", "true")
        self._sidecar_with_receipt(
            ".factory-state/audit-receipts/installed-harness-smoke.json",
            in_artifacts=True,
        )
        result = self.fixture.validator("planning")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("outside the only allowed shape", result.stderr)

    def test_missing_runtime_receipt_fails_closed(self) -> None:
        self._sidecar_with_receipt(
            ".factory-state/audit-receipts/never-minted.json"
        )
        result = self.fixture.validator("planning")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("unsafe or invalid", result.stderr)

    def test_stale_runtime_receipt_fails_closed(self) -> None:
        """A receipt bound to a different evidence commit never satisfies the
        row's exact evidence_commit."""
        ancestor = self.head  # the pre-sidecar commit the receipt predates
        # Mint the receipt at the pre-sidecar commit first, then commit a
        # sidecar whose row is bound to that same (older) commit, and finally
        # rebind the row to the NEWER commit so the receipt's evidence_commit
        # no longer equals the row's evidence_commit.
        self._mint("installed-harness-smoke", "true")
        self._sidecar_with_receipt(
            ".factory-state/audit-receipts/installed-harness-smoke.json"
        )
        data = json.loads(self.fixture.sidecar_path.read_text(encoding="utf-8"))
        for req in data["requirements"]:
            if req["id"] == "REQ-01":
                req["evidence_commit"] = self.head
        self.fixture.sidecar_path.write_text(json.dumps(data), encoding="utf-8")
        self.fixture.commit_all("stale sidecar")
        result = self.fixture.validator("planning")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("does not equal the row evidence_commit", result.stderr)

    def test_failing_runtime_receipt_never_passes(self) -> None:
        """A self-consistent receipt that exited non-zero is never PASS."""
        self._sidecar_with_receipt(
            ".factory-state/audit-receipts/installed-harness-smoke.json"
        )
        self._mint("installed-harness-smoke", "true")
        receipts = self.state_dir / "audit-receipts"
        receipt = receipts / "installed-harness-smoke.json"
        data = json.loads(receipt.read_text(encoding="utf-8"))
        data["exit_code"] = 7
        # Keep the digest bindings self-consistent so the hardened schema/
        # digest validation passes and the exit-code gate is what fails.
        data["stdout_sha256"] = hashlib.sha256(
            (receipts / "installed-harness-smoke.stdout").read_bytes()
        ).hexdigest()
        data["stderr_sha256"] = hashlib.sha256(
            (receipts / "installed-harness-smoke.stderr").read_bytes()
        ).hexdigest()
        receipt.write_text(json.dumps(data, sort_keys=True, indent=2) + "\n",
                           encoding="utf-8")
        os.chmod(receipt, 0o600)
        result = self.fixture.validator("planning")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("did not exit 0", result.stderr)

    def test_symlinked_runtime_receipt_fails_closed(self) -> None:
        self._mint("installed-harness-smoke", "true")
        self._sidecar_with_receipt(
            ".factory-state/audit-receipts/installed-harness-smoke.json"
        )
        receipts = self.state_dir / "audit-receipts"
        target = receipts / "installed-harness-smoke.json"
        original = target.read_bytes()
        target.unlink()
        os.symlink("audit-coordinator.json", target)
        result = self.fixture.validator("planning")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("unsafe or invalid", result.stderr)
        target.unlink()
        target.write_bytes(original)
        os.chmod(target, 0o600)

    def test_wrong_coordinator_binding_fails_closed(self) -> None:
        """A receipt minted under a different coordinator nonce/round is
        rejected against the protected coordinator state."""
        self._mint("installed-harness-smoke", "true")
        self._sidecar_with_receipt(
            ".factory-state/audit-receipts/installed-harness-smoke.json"
        )
        other_nonce = hashlib.sha256(os.urandom(32)).hexdigest()
        self._write_coordinator(1, self.head, other_nonce)
        result = self.fixture.validator("planning")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("coordinator nonce", result.stderr)


class ConformanceRegistryTests(unittest.TestCase):
    """Exact-set binding to the committed section-24 registry."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="factory-conformance-reg-")
        self.fixture = ConformanceFixture(Path(self._tmp.name))
        self.fixture.blessed()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _rewrite_registry(self, ids: list[str]) -> None:
        path = self.fixture.root / ".factory" / "schemas" / "factory-plan-v1.requirements.json"
        path.write_text(json.dumps({"schema": "factory-plan/v1/requirements",
                                    "requirement_ids": ids}), encoding="utf-8")
        result = self.fixture.validator("planning")
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn("registry", result.stderr)

    def test_extra_registry_id_fails(self) -> None:
        self._rewrite_registry(["REQ-01", "REQ-02", "REQ-03", "REQ-99"])

    def test_missing_registry_id_fails(self) -> None:
        self._rewrite_registry(["REQ-01", "REQ-03"])


class PolicyMatchTests(unittest.TestCase):
    """Tier and capability matching against the requirement-policy map."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="factory-conformance-pol-")
        self.fixture = ConformanceFixture(Path(self._tmp.name))
        self.fixture.blessed()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_self_declared_tier_fails(self) -> None:
        self.fixture.policy_patch("REQ-01", required_tier="installed")
        result = self.fixture.validator("planning")
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn("required_tier", result.stderr)

    def test_capability_relaxation_fails(self) -> None:
        self.fixture.sidecar_patch("REQ-03", required_capabilities=[])
        result = self.fixture.validator("planning")
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn("required_capabilities", result.stderr)

    def test_policy_extra_id_fails(self) -> None:
        data = json.loads(self.fixture.policy_path.read_text(encoding="utf-8"))
        data["requirements"].append({"id": "REQ-04", "required_tier": "unit",
                                     "spec_sections": ["§4"], "required_capabilities": []})
        self.fixture.policy_path.write_text(json.dumps(data), encoding="utf-8")
        result = self.fixture.validator("planning")
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn("registry", result.stderr)


class RefSafetyTests(unittest.TestCase):
    """Blob-bound refs and path-traversal rejection in planning mode."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="factory-conformance-ref-")
        self.fixture = ConformanceFixture(Path(self._tmp.name))
        self.fixture.blessed()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_stale_working_tree_ref_fails_planning(self) -> None:
        (self.fixture.root / "tests" / "fixtures" / "stale-only.json").write_text(
            "{}", encoding="utf-8")
        self.fixture.apply_fixture("sidecar-ref-stale.json")
        result = self.fixture.validator("planning")
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn("not a Git blob", result.stderr)

    def test_traversal_refs_fail(self) -> None:
        for name, ref in (("sidecar-ref-traversal.json", ".factory/tests/legacy/../../escape"),):
            with self.subTest(ref=ref):
                self.fixture.apply_fixture(name)
                result = self.fixture.validator("planning")
                self.assertEqual(result.returncode, 1, result.stdout)

    def test_prefix_alias_ref_fails(self) -> None:
        self.fixture.apply_fixture("sidecar-ref-prefix-alias.json")
        result = self.fixture.validator("planning")
        self.assertEqual(result.returncode, 1, result.stdout)

    def test_absolute_ref_fails(self) -> None:
        self.fixture.apply_fixture("sidecar-ref-absolute.json")
        result = self.fixture.validator("planning")
        self.assertEqual(result.returncode, 1, result.stdout)

    def test_special_and_control_refs_fail(self) -> None:
        for ref in ("C:\\evil", ".factory/tests/legacy//x", ".factory/tests/legacy/a\\b", ".factory/tests/legacy/evil\x00name"):
            with self.subTest(ref=ref):
                self.fixture.sidecar_patch("REQ-01", artifacts=[ref])
                result = self.fixture.validator("planning")
                self.assertEqual(result.returncode, 1, result.stdout)


class ElevationTests(unittest.TestCase):
    """Proxy evidence can never be elevated into acceptance."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="factory-conformance-elev-")
        self.fixture = ConformanceFixture(Path(self._tmp.name))
        self.fixture.blessed()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_human_tier_rejected_in_planning(self) -> None:
        self.fixture.apply_fixture("sidecar-human-tier.json")
        result = self.fixture.validator("planning")
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn("human", result.stderr)

    def test_verified_below_required_tier_fails_planning(self) -> None:
        self.fixture.policy_patch("REQ-01", required_tier="installed")
        self.fixture.apply_fixture("sidecar-below-tier.json")
        result = self.fixture.validator("planning")
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn("below required tier", result.stderr)

    def test_capability_relaxation_fails(self) -> None:
        self.fixture.apply_fixture("sidecar-capability-relax.json")
        result = self.fixture.validator("planning")
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn("required_capabilities", result.stderr)

    def test_missing_row_cannot_carry_refs(self) -> None:
        self.fixture.apply_fixture("sidecar-missing-with-refs.json")
        result = self.fixture.validator("planning")
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn("may not carry", result.stderr)

    def test_missing_row_cannot_claim_high_tier(self) -> None:
        self.fixture.sidecar_patch("REQ-02", classification="missing",
                                   fact_refs=[], evidence_tier="installed")
        result = self.fixture.validator("planning")
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn("unit", result.stderr)

    def test_undeclared_capability_never_evidences(self) -> None:
        # The fixture runner capability is undeclared in this hermetic repo,
        # so capability evidence fails closed and real_system stays blocked.
        result = run(
            ["python3", str(CAPABILITY_CHECKER), "--capabilities", "hardware-runner"],
            self.fixture.root,
        )
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn("not declared", result.stderr)


class CompleteModeTests(unittest.TestCase):
    """Complete mode remains the exact-commit acceptance authority."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="factory-conformance-complete-")
        self.fixture = ConformanceFixture(Path(self._tmp.name))
        self.fixture.blessed()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_free_text_verified_row_fails(self) -> None:
        self.fixture.sidecar_patch("REQ-01", receipts=[], artifacts=[])
        result = self.fixture.validator("complete")
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn("free-text", result.stderr)

    def test_blocked_row_fails_completion(self) -> None:
        # `blocked` stays representable in planning but fails implementation
        # completion (Task 14 acceptance: blocked/not_applicable fail complete).
        result = self.fixture.validator("complete")
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn("blocked", result.stderr)

    def test_not_applicable_row_fails_completion(self) -> None:
        # With a spec-scoped reason planning accepts the row, but complete
        # mode still requires every row `verified`.
        data = json.loads(self.fixture.sidecar_path.read_text(encoding="utf-8"))
        for req in data["requirements"]:
            if req["id"] == "REQ-02":
                req["classification"] = "not_applicable"
                req["reason"] = "excluded by \u00a712 out-of-scope boundary"
                req["fact_refs"] = []
        self.fixture.sidecar_path.write_text(json.dumps(data), encoding="utf-8")
        plan = self.fixture.plan_path.read_text(encoding="utf-8")
        self.fixture.plan_path.write_text(
            plan.replace("| REQ-02 | §2 | partial |", "| REQ-02 | §2 | not_applicable |"),
            encoding="utf-8",
        )
        facts = json.loads(self.fixture.facts_path.read_text(encoding="utf-8"))
        facts["facts"] = [f for f in facts["facts"] if f["id"] != "FACT-001"]
        self.fixture.facts_path.write_text(json.dumps(facts), encoding="utf-8")
        result = self.fixture.validator("planning")
        self.assertEqual(result.returncode, 0, result.stderr)
        result = self.fixture.validator("complete")
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn("not_applicable", result.stderr)


class UnevidencedCapabilityTests(unittest.TestCase):
    """A verified row may never rest on an undeclared or unevidenced capability."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="factory-conformance-cap-")
        self.fixture = ConformanceFixture(Path(self._tmp.name))
        self.fixture.blessed()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _require_capability(self, capability: str) -> None:
        self.fixture.policy_patch("REQ-01", required_capabilities=[capability])
        self.fixture.sidecar_patch("REQ-01", required_capabilities=[capability])

    def _declare_runner(self) -> None:
        """Commit a valid signer-trust policy and a runner declaration so the
        capability-evidence checker reaches the aggregate-missing failure.

        The ephemeral ed25519 signer key is test-owned fixture state (never
        committed); the committed signer-trust policy and environment are the
        minimum valid trust inputs the strong runner-evidence checker needs
        before it can read the (deliberately absent) aggregate.
        """
        env = self.fixture.root / ".factory" / "environment.toml"
        env.write_text(
            "schema_version = 1\n\n[[runners]]\n"
            "name = \"probe-runner\"\ntransport = \"ssh\"\n"
            "ssh_config_alias = \"probe-runner\"\n"
            "working_directory = \"/srv/dev-runner/workspaces/probe\"\n"
            "capabilities = [\"probe-capability\"]\n"
            "verify_argv = [\"./.factory/tools/verify-boilerplate.sh\"]\n",
            encoding="utf-8",
        )
        signer_key = Path(self._tmp.name) / "signer-key"
        subprocess.run(
            ["ssh-keygen", "-q", "-t", "ed25519", "-N", "",
             "-f", str(signer_key)],
            check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        public_key = " ".join(
            (Path(str(signer_key) + ".pub").read_text(
                encoding="utf-8").split())[:2])
        (self.fixture.root / ".factory" / "signer-trust.json").write_text(
            json.dumps({
                "schema": "ralph-runner-signer-trust/v1",
                "description": "conformance-suite ephemeral fixture signer",
                "require_signature": True,
                "enabled": True,
                "namespace": "factory-runner-receipt",
                "public_keys": [{"principal": "probe-runner",
                                  "public_key": public_key}],
                "allowed_principals": ["probe-runner"],
            }, sort_keys=True, indent=2) + "\n",
            encoding="utf-8",
        )
        self.fixture.commit_all("declare probe-runner and signer trust")

    def test_undeclared_capability_fails_planning(self) -> None:
        # hardware-runner is never declared in the fixture environment: the
        # verified claim fails now, in planning mode, not only at completion.
        self._require_capability("hardware-runner")
        result = self.fixture.validator("planning")
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn("not declared", result.stderr)

    def test_declared_but_unevidenced_capability_fails_planning(self) -> None:
        # probe-capability is declared and has a tracked contract, but no
        # accepted runner receipt exists: unevidenced in planning mode.  The
        # committed signer-trust policy and runner declaration let the strong
        # runner-evidence checker reach the aggregate read, which is absent,
        # so the verified claim fails closed as unevidenced.
        self._declare_runner()
        (self.fixture.root / ".factory" / "capability-contracts.json").write_text(
            json.dumps({"schema": "ralph-capability-contract/v1", "capabilities": [
                {"name": "probe-capability",
                 "probe_argv": ["./.factory/tools/verify-boilerplate.sh"],
                 "probe_marker": "--- probe-capability contract ---",
                 "must_execute": True,
                 "must_not_skip": ["Skipped"],
                 "deny_simulated_markers": ["mock"]},
            ]}), encoding="utf-8")
        self._require_capability("probe-capability")
        env = {**os.environ,
               "FACTORY_CAMPAIGN_ID": "fixture",
               "FACTORY_READINESS_NONCE": "9" * 64}
        result = self.fixture.validator("planning", env=env)
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn("strong runner evidence rejected", result.stderr)
        self.assertIn("unsafe or missing evidence file", result.stderr)


class DuplicateAuthorityTests(unittest.TestCase):
    """Duplicate JSON keys and duplicate IDs fail closed in every authority."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="conformance-dup-")
        self.fixture = ConformanceFixture(Path(self._tmp.name))
        self.fixture.blessed()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _write(self, relpath: str, text: str) -> Path:
        path = self.fixture.root / relpath
        path.write_text(text, encoding="utf-8")
        return path

    # --- duplicate JSON object keys ------------------------------------------
    def test_duplicate_key_sidecar_fails(self) -> None:
        self._write(
            ".factory/artifacts/conformance.json",
            '{"schema": "ralph-conformance/v1", "requirements": [], "requirements": []}',
        )
        result = self.fixture.validator("planning")
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn("duplicate JSON object key", result.stderr)

    def test_duplicate_key_policy_fails(self) -> None:
        self._write(
            ".factory/requirement-policy.json",
            '{"schema": "ralph-requirement-policy/v1", "requirements": [], "requirements": []}',
        )
        result = self.fixture.validator("planning")
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn("duplicate JSON object key", result.stderr)

    def test_duplicate_key_registry_fails(self) -> None:
        self._write(
            ".factory/schemas/factory-plan-v1.requirements.json",
            '{"schema": "factory-plan/v1/requirements", "requirement_ids": [], "requirement_ids": []}',
        )
        result = self.fixture.validator("planning")
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn("duplicate JSON object key", result.stderr)

    def test_duplicate_key_facts_fails(self) -> None:
        self._write(
            ".factory/artifacts/blocked-facts.json",
            '{"schema": "ralph-blocked-facts/v1", "facts": [], "facts": []}',
        )
        result = run([sys.executable, "./.factory/tools/validate-blocked-facts.py",
                      "planning", ".factory/artifacts/blocked-facts.json"],
                     self.fixture.root)
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn("duplicate JSON object key", result.stderr)

    def test_duplicate_key_contracts_fails(self) -> None:
        # Build a valid contract authority first, then duplicate the key.
        env = self.fixture.root / ".factory" / "environment.toml"
        env.write_text(
            "schema_version = 1\n\n[[runners]]\n"
            "name = \"probe-runner\"\ntransport = \"ssh\"\n"
            "ssh_config_alias = \"probe-runner\"\n"
            "working_directory = \"/srv/dev-runner/workspaces/probe\"\n"
            "capabilities = [\"probe-capability\"]\n"
            "verify_argv = [\"./.factory/tools/verify-boilerplate.sh\"]\n",
            encoding="utf-8",
        )
        self._write(
            ".factory/capability-contracts.json",
            '{"schema": "ralph-capability-contract/v1", "capabilities": [], "capabilities": []}',
        )
        result = run([sys.executable, "./.factory/tools/check-capability-contracts.py"],
                     self.fixture.root)
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn("duplicate JSON object key", result.stderr)

    # --- duplicate IDs -------------------------------------------------------
    def test_duplicate_id_sidecar_fails(self) -> None:
        data = json.loads(self.fixture.sidecar_path.read_text(encoding="utf-8"))
        first = data["requirements"][0]
        data["requirements"].append(dict(first, id=first["id"]))
        self.fixture.sidecar_path.write_text(json.dumps(data), encoding="utf-8")
        result = self.fixture.validator("planning")
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn("duplicate requirement ID", result.stderr)

    def test_duplicate_id_policy_fails(self) -> None:
        data = json.loads(self.fixture.policy_path.read_text(encoding="utf-8"))
        data["requirements"].append(dict(data["requirements"][0], id="REQ-01"))
        self.fixture.policy_path.write_text(json.dumps(data), encoding="utf-8")
        result = self.fixture.validator("planning")
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn("duplicate id", result.stderr)

    def test_duplicate_id_registry_fails(self) -> None:
        self._write(
            ".factory/schemas/factory-plan-v1.requirements.json",
            '{"schema": "factory-plan/v1/requirements", "requirement_ids": ["REQ-01", "REQ-02", "REQ-03", "REQ-01"]}',
        )
        result = self.fixture.validator("planning")
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn("duplicate requirement ID", result.stderr)

    def test_duplicate_id_facts_fails(self) -> None:
        data = json.loads(self.fixture.facts_path.read_text(encoding="utf-8"))
        data["facts"].append(dict(data["facts"][0], id="FACT-001"))
        self.fixture.facts_path.write_text(json.dumps(data), encoding="utf-8")
        result = run([sys.executable, "./.factory/tools/validate-blocked-facts.py",
                      "planning", ".factory/artifacts/blocked-facts.json"],
                     self.fixture.root)
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn("fact IDs must be unique", result.stderr)

    def test_duplicate_id_contracts_fails(self) -> None:
        env = self.fixture.root / ".factory" / "environment.toml"
        env.write_text(
            "schema_version = 1\n\n[[runners]]\n"
            "name = \"probe-runner\"\ntransport = \"ssh\"\n"
            "ssh_config_alias = \"probe-runner\"\n"
            "working_directory = \"/srv/dev-runner/workspaces/probe\"\n"
            "capabilities = [\"probe-capability\"]\n"
            "verify_argv = [\"./.factory/tools/verify-boilerplate.sh\"]\n",
            encoding="utf-8",
        )
        self.fixture.root.joinpath(".factory/capability-contracts.json").write_text(
            json.dumps({"schema": "ralph-capability-contract/v1", "capabilities": [
                {"name": "probe-capability",
                 "probe_argv": ["./.factory/tools/verify-boilerplate.sh"],
                 "probe_marker": "--- probe-capability contract ---",
                 "must_execute": True,
                 "must_not_skip": ["Skipped"],
                 "deny_simulated_markers": ["mock"]},
                {"name": "probe-capability",
                 "probe_argv": ["./.factory/tools/verify-boilerplate.sh"],
                 "probe_marker": "--- probe-capability contract ---",
                 "must_execute": True,
                 "must_not_skip": ["Skipped"],
                 "deny_simulated_markers": ["mock"]},
            ]}), encoding="utf-8")
        result = run([sys.executable, "./.factory/tools/check-capability-contracts.py"],
                     self.fixture.root)
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn("contract names must be unique", result.stderr)


class GitBoundaryTests(unittest.TestCase):
    """Fake PATH / GIT_DIR / GIT_CONFIG and replace refs cannot redirect Git."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="factory-gitbound-")
        self.fixture = ConformanceFixture(Path(self._tmp.name))
        self.fixture.blessed()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _hostile_env(self, **overrides) -> dict:
        fake_bin = Path(self._tmp.name) / "fake-bin"
        fake_bin.mkdir(exist_ok=True)
        (fake_bin / "git").write_text(
            "#!/usr/bin/env python3\nprint('fake git executed', file=sys.stderr)\nexit 42\n",
            encoding="utf-8",
        )
        (fake_bin / "git").chmod(0o755)
        env = {
            **os.environ,
            "PATH": str(fake_bin),
            "GIT_DIR": str(Path(self._tmp.name) / "not-a-repo"),
            "GIT_WORK_TREE": str(Path(self._tmp.name) / "nowhere"),
            "GIT_OBJECT_DIRECTORY": str(Path(self._tmp.name) / "objects"),
            "GIT_ALTERNATE_OBJECT_DIRECTORIES": str(Path(self._tmp.name) / "alts"),
            "GIT_INDEX_FILE": str(Path(self._tmp.name) / "index"),
            "GIT_COMMON_DIR": str(Path(self._tmp.name) / "common"),
            "GIT_CONFIG": str(Path(self._tmp.name) / "evil-config"),
            "GIT_CONFIG_GLOBAL": str(Path(self._tmp.name) / "evil-global"),
            "GIT_CONFIG_SYSTEM": str(Path(self._tmp.name) / "evil-system"),
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "core.bare",
            "GIT_CONFIG_VALUE_0": "true",
            "GIT_CEILING_DIRECTORIES": str(Path(self._tmp.name)),
        }
        env.update(overrides)
        return env

    def test_fake_path_git_dir_git_config_cannot_redirect(self) -> None:
        # Every trusted Git call runs the pinned absolute executable with the
        # sanitized environment: the fake PATH `git` (exit 42), the bogus
        # GIT_DIR/object-store redirectors, and the GIT_CONFIG* family are all
        # inert, so the blessed repo still validates.
        result = self.fixture.validator("planning", env=self._hostile_env())
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_ref_names_are_rejected_as_commit_arguments(self) -> None:
        # Only full 40-hex commit IDs are ever handed to pinned Git: a ref
        # name resolves to a mutable/replaceable object and is refused at the
        # sidecar schema boundary (and again by require_sha before any Git
        # call).
        for bad in ("HEAD", "refs/heads/develop", "develop", "abc1234"):
            with self.subTest(ref=bad):
                self.fixture.sidecar_patch("REQ-01", evidence_commit=bad)
                result = self.fixture.validator("planning")
                self.assertEqual(result.returncode, 1, result.stdout)
                self.assertIn("40-character commit", result.stderr)

    def test_replace_refs_cannot_substitute_evidence(self) -> None:
        head = git(self.fixture.root, "rev-parse", "HEAD")
        # Tampered commit that deletes the evidenced artifact from its tree.
        (self.fixture.root / ".factory" / "tests" / "legacy" / "probe.c").unlink()
        git(self.fixture.root, "add", "-A")
        git(self.fixture.root, "commit", "-qm", "tamper: drop probe.c")
        tamper = git(self.fixture.root, "rev-parse", "HEAD")
        git(self.fixture.root, "replace", head, tamper)
        # Planning stays valid: GIT_NO_REPLACE_OBJECTS=1 makes object
        # resolution ignore refs/replace/*, so the original evidence commit
        # (with .factory/tests/legacy/probe.c) resolves and the ref is a real blob.
        result = self.fixture.validator("planning")
        self.assertEqual(result.returncode, 0, result.stderr)
        # Control: with the boundary absent (GIT_NO_REPLACE_OBJECTS unset) the
        # replace ref redirects object resolution and the evidence ref stops
        # resolving; the trusted path must never observe that behavior.
        clean = {k: v for k, v in os.environ.items()
                 if not k.startswith("GIT_")}
        control = subprocess.run(
            ["git", "-C", str(self.fixture.root), "cat-file", "-e",
             f"{head}:tests/probe.c"],
            capture_output=True, text=True, env=clean,
        )
        self.assertNotEqual(control.returncode, 0, "replace ref was not honored")


if __name__ == "__main__":
    unittest.main()
