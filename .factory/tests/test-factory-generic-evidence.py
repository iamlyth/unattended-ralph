#!/usr/bin/env python3
"""Generic evidence-scope authority tests (Task 23; EVID-01 §19, CTX-02 §18).

The trusted generic evidence publisher (`.factory/loop/generic_evidence.py`)
is the two-stage owner of the **live** installed-tier evidence for the
generic harness at the exact bound commit.  Every test runs in a test-owned
temporary fixture repository that carries the committed installed surface
plus a valid ``factory-state/v1`` control state and a foreign
``installed-functional-evidence.env`` root artifact, and then exercises the
real publisher (prepare + publish) and the real checker
(``.factory/tools/check-installed-functional-evidence.sh``):

1. the foreign old root environment is ignored by the checker and preserved
   byte/mode/mtime-identically by the publisher (the preservation proof
   records the before/after snapshot and only the new generic namespace,
   coordinator, and receipt paths may be added);
2. a successful publication mints the installed-harness machine receipt
   through the trusted wrapper authority (allowlisted
   ``installed-harness-smoke`` argv, exact commit, coordinator round/nonce)
   and only then publishes the installed-functional evidence record binding
   the receipt digest/commit/coordinator;
3. duplicate publication, symlink/hardlink/mode/commit/receipt tampering,
   and stale generic namespaces all fail closed;
4. a failed or skipped installed suite leaves no artifacts and mutates no
   pre-existing ``.factory-state`` file;
5. the real committed installed harness suite runs end-to-end at the exact
   fixture commit and the checker accepts the publication.

No model, runner, hardware, or human is invoked and the live repository's
``.factory-state/`` is never touched (the shell driver proves byte
preservation of the live runtime state around the whole suite).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
LOOP = ROOT / ".factory" / "loop"
STATE_DIR = ".factory-state"
PUBLISHER = LOOP / "generic_evidence.py"
CHECKER = ROOT / ".factory" / "tools" / "check-installed-functional-evidence.sh"
STUB_SUITE = (
    "#!/usr/bin/env bash\n"
    "set -euo pipefail\n"
    "echo 'test-factory-installed: all checks passed'\n"
)
FAILING_SUITE = (
    "#!/usr/bin/env bash\n"
    "set -euo pipefail\n"
    "echo 'expected installed-suite failure' >&2\n"
    "exit 3\n"
)
SKIPPING_SUITE = (
    "#!/usr/bin/env bash\n"
    "set -euo pipefail\n"
    "echo 'SKIPPED: fixture environment unavailable'\n"
    "exit 0\n"
)
SHA1 = re.compile(r"^[0-9a-f]{40}$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")

sys.path.insert(0, str(LOOP))
import evidence as evidence_module  # noqa: E402
import generic_evidence as generic_evidence_module  # noqa: E402
import gitutil  # noqa: E402
import state as state_module  # noqa: E402


def _run(
    argv: list[str],
    *,
    cwd: Path | None = None,
    env: dict | None = None,
    check: bool = True,
    timeout: float = 600,
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        argv, cwd=cwd, text=True, capture_output=True, env=env, timeout=timeout
    )
    if check and result.returncode:
        raise AssertionError(
            (argv, result.returncode, result.stdout[-3000:], result.stderr[-3000:])
        )
    return result


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class GenericEvidenceSuite(unittest.TestCase):
    """End-to-end generic evidence publication and acceptance (Task 23)."""

    def test_staging_snapshot_may_exceed_evidence_record_bound(self) -> None:
        staging = Path(tempfile.mkdtemp(prefix="generic-staging-bound."))
        self.addCleanup(shutil.rmtree, staging, ignore_errors=True)
        os.chmod(staging, 0o700)
        record = staging / generic_evidence_module.STAGING_NAME
        record.write_text(
            json.dumps({
                "schema": generic_evidence_module.STAGING_SCHEMA,
                "snapshot_fixture": "x" * (generic_evidence_module.MAX_RECORD + 1),
            }),
            encoding="utf-8",
        )
        os.chmod(record, 0o600)
        parsed = generic_evidence_module._read_staging(staging)
        self.assertEqual(parsed["schema"], generic_evidence_module.STAGING_SCHEMA)

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="factory-generic-evidence."))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.head = gitutil.resolve_head(ROOT)
        self.assertTrue(SHA1.fullmatch(self.head), "cannot resolve the bound commit")
        self.fixture = self.tmp / "fixture"
        self._make_repo(self.fixture)
        self._copy_surface(self.fixture)
        self._replace_suite(STUB_SUITE)
        self.commit = self._commit_all(self.fixture, "surface committed")
        self.state_dir = self.fixture / STATE_DIR
        self.state_dir.mkdir(mode=0o700)
        os.chmod(self.state_dir, 0o700)
        self._init_state()
        self.foreign = self.state_dir / "installed-functional-evidence.env"
        self.foreign.write_text(
            "schema=factory-installed-functional/v1\n"
            "commit=61356a0af6d26a8207ccfc2fc25c86f27c982c4b\n"
            "test=test_installed_functional\n"
            "result=PASS\n"
            "skipped=0\n",
            encoding="utf-8",
        )
        self.staging = self.tmp / "staging"

    # -- fixture helpers -----------------------------------------------------

    def _make_repo(self, path: Path) -> None:
        path.mkdir()
        _run([gitutil.GIT_EXECUTABLE, "-C", str(path), "init", "-q",
              "-b", "boilerplate-develop"])
        _run([gitutil.GIT_EXECUTABLE, "-C", str(path), "config",
              "user.email", "factory@test"])
        _run([gitutil.GIT_EXECUTABLE, "-C", str(path), "config",
              "user.name", "factory"])
        (path / ".gitignore").write_text(".factory-state/\n", encoding="utf-8")

    def _copy_surface(self, target: Path) -> None:
        ignore = shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo")
        # ``docs`` is part of the surface because the real committed installed
        # suite (``test_factory_installed_suite_publication_end_to_end``) runs
        # the installed evidence-smoke campaign, whose fixture plan binds the
        # canonical spec at ``docs/FACTORY-LOOP-SPEC.md`` from this fixture.
        for name in (".factory", ".pi", "scripts", "docs"):
            shutil.copytree(ROOT / name, target / name, ignore=ignore)

    def _replace_suite(self, body: str) -> None:
        suite = self.fixture / ".factory/tests/test-factory-installed.sh"
        suite.write_text(body, encoding="utf-8")
        suite.chmod(0o755)

    def _commit_all(self, path: Path, message: str) -> str:
        _run([gitutil.GIT_EXECUTABLE, "-C", str(path), "add", "-A"])
        _run([gitutil.GIT_EXECUTABLE, "-C", str(path), "commit", "-qm", message])
        return _run([gitutil.GIT_EXECUTABLE, "-C", str(path),
                     "rev-parse", "HEAD"]).stdout.strip()

    def _init_state(self) -> None:
        state_module.init_state(
            self.fixture,
            campaign_id="generic-evidence-test",
            rounds_requested=1,
            specification_digest="a" * 64,
            plan_digest="b" * 64,
            role_prompt_digests={"developer": "c" * 64},
            audit_objectives_digest="d" * 64,
            phase_base_commit=self.commit,
            branch="boilerplate-develop",
            now=1_234_567_890,
        )

    def base_env(self) -> dict:
        return {
            "PATH": os.environ.get("PATH", ""),
            "HOME": str(self.tmp),
            "TMPDIR": str(self.tmp),
            "LANG": "C.UTF-8",
            "PYTHONDONTWRITEBYTECODE": "1",
        }

    def publisher(self, *args: str, check: bool = True):
        return _run(
            [sys.executable, str(PUBLISHER), "--root", str(self.fixture), *args],
            cwd=str(self.fixture),
            env=self.base_env(),
            check=check,
        )

    def prepare(self, check: bool = True):
        return self.publisher("prepare", "--staging", str(self.staging), check=check)

    def publish(self, check: bool = True):
        return self.publisher("publish", "--staging", str(self.staging), check=check)

    def checker(self, check: bool = True):
        return _run(
            ["bash", str(self.fixture / ".factory/tools/check-installed-functional-evidence.sh")],
            cwd=str(self.fixture),
            env=self.base_env(),
            check=check,
        )

    def state_files(self) -> dict:
        """sha256/mode/mtime of every fixture `.factory-state` file."""
        result: dict = {}
        for path in sorted(self.state_dir.rglob("*")):
            if path.is_symlink() or not path.is_file():
                continue
            info = path.lstat()
            result[str(path.relative_to(self.state_dir))] = {
                "sha256": _sha256(path.read_bytes()),
                "mode": stat.S_IMODE(info.st_mode),
                "mtime": int(info.st_mtime),
            }
        return result

    @property
    def namespace(self) -> Path:
        return self.state_dir / "generic-evidence" / self.commit

    def record(self) -> dict:
        return json.loads(
            (self.namespace / "installed-functional.json").read_text(encoding="utf-8")
        )

    def receipt(self) -> dict:
        return json.loads(
            (self.state_dir / "audit-receipts/installed-harness-smoke.json")
            .read_text(encoding="utf-8")
        )

    def publish_all(self) -> None:
        self.prepare()
        self.publish()

    # -- publication flow and receipt binding --------------------------------

    def test_publication_mints_receipt_and_publishes_evidence_after_acceptance(self) -> None:
        before = self.state_files()
        self.publish_all()
        after = self.state_files()
        # Only the new generic namespace, coordinator, and receipt paths were
        # added; every pre-existing file is byte/mode/mtime identical.
        added = sorted(set(after) - set(before))
        self.assertEqual(
            added,
            [
                "audit-coordinator.json",
                "audit-receipts/installed-harness-smoke.json",
                "audit-receipts/installed-harness-smoke.stderr",
                "audit-receipts/installed-harness-smoke.stdout",
                f"generic-evidence/{self.commit}/installed-functional.json",
                f"generic-evidence/{self.commit}/preservation.json",
            ],
            "only the allowed new generic/coordinator/receipt paths may appear",
        )
        for rel, entry in before.items():
            self.assertEqual(after[rel], entry, f"foreign file changed: {rel}")
        # Preservation proof records the before/after and the allowed deltas.
        # The proof is the final artifact, so its own ``after`` snapshot (and
        # therefore its ``added`` set) intentionally excludes the proof file
        # itself; every other new path is recorded.
        proof = json.loads(
            (self.namespace / "preservation.json").read_text(encoding="utf-8")
        )
        self.assertEqual(proof["schema"], "factory-generic-preservation/v1")
        self.assertTrue(proof["preserved"])
        self.assertEqual(proof["added"], [item for item in added
                                           if item != f"generic-evidence/{self.commit}/preservation.json"])
        self.assertEqual(proof["removed"], [])
        self.assertEqual(proof["changed"], [])
        # The receipt is a genuine hardened machine receipt bound to the exact
        # commit and the bridged coordinator round/nonce.
        receipt = evidence_module.validate_receipt(
            self.fixture, ".factory-state/audit-receipts/installed-harness-smoke.json"
        )
        self.assertEqual(receipt["argv"], ["./.factory/tests/test-factory-installed.sh"])
        self.assertEqual(receipt["exit_code"], 0)
        self.assertEqual(receipt["evidence_commit"], self.commit)
        self.assertEqual(receipt["coordinator_round"], 1)
        # The evidence record binds the receipt digest/commit/coordinator.
        record = self.record()
        self.assertEqual(record["schema"], "factory-generic-installed-functional/v1")
        self.assertEqual(record["commit"], self.commit)
        self.assertEqual(record["test"], "test_installed_functional")
        self.assertEqual(record["result"], "PASS")
        self.assertEqual(record["skipped"], 0)
        self.assertEqual(
            record["receipt"], ".factory-state/audit-receipts/installed-harness-smoke.json"
        )
        self.assertEqual(
            record["receipt_sha256"],
            _sha256(
                (self.state_dir / "audit-receipts/installed-harness-smoke.json")
                .read_bytes()
            ),
        )
        self.assertEqual(record["coordinator_round"], 1)
        self.assertEqual(record["coordinator_nonce"], receipt["coordinator_nonce"])
        self.assertEqual(
            record["suite_stdout_sha256"],
            _sha256(
                (self.state_dir / "audit-receipts/installed-harness-smoke.stdout")
                .read_bytes()
            ),
        )
        # Artifact hardening: private dirs, 0600 no-replace single-link files.
        self.assertEqual(stat.S_IMODE(self.namespace.stat().st_mode), 0o700)
        for rel in (
            "audit-coordinator.json",
            "audit-receipts/installed-harness-smoke.json",
            f"generic-evidence/{self.commit}/installed-functional.json",
            f"generic-evidence/{self.commit}/preservation.json",
        ):
            path = self.state_dir / rel
            info = path.lstat()
            self.assertEqual(info.st_uid, os.getuid(), rel)
            self.assertEqual(info.st_nlink, 1, rel)
            self.assertEqual(stat.S_IMODE(info.st_mode), 0o600, rel)
            self.assertFalse(path.is_symlink(), rel)
        # The checker accepts the fresh generic evidence.
        check = self.checker()
        self.assertEqual(check.returncode, 0, check.stderr)
        self.assertIn(f"PASS at {self.commit}", check.stdout)

    def test_foreign_root_environment_is_ignored_and_preserved(self) -> None:
        """The foreign old root env is never read and never rewritten."""
        before = {
            "sha256": _sha256(self.foreign.read_bytes()),
            "mode": stat.S_IMODE(self.foreign.lstat().st_mode),
            "mtime": int(self.foreign.stat().st_mtime),
        }
        self.publish_all()
        check = self.checker()
        self.assertEqual(check.returncode, 0, check.stderr)
        # A valid foreign root env alone never satisfies the checker: the
        # exact-HEAD generic namespace is the only authority.
        shutil.rmtree(self.namespace)
        rejected = self.checker(check=False)
        self.assertNotEqual(rejected.returncode, 0,
                            "a valid foreign root env must never satisfy the "
                            "exact-HEAD generic checker")
        self.assertEqual(
            {
                "sha256": _sha256(self.foreign.read_bytes()),
                "mode": stat.S_IMODE(self.foreign.lstat().st_mode),
                "mtime": int(self.foreign.stat().st_mtime),
            },
            before,
            "the foreign root env must be preserved byte/mode/mtime-identically",
        )

    # -- duplicate publication ----------------------------------------------

    def test_duplicate_publication_fails_closed(self) -> None:
        self.prepare()
        self.publish()
        # A second prepare refuses to replace the staging record; a second
        # publish refuses to overwrite the coordinator/namespace/receipts.
        second_prepare = self.prepare(check=False)
        self.assertNotEqual(second_prepare.returncode, 0)
        self.assertIn("already exists", second_prepare.stderr)
        second_publish = self.publish(check=False)
        self.assertNotEqual(second_publish.returncode, 0)
        self.assertIn("must not preexist", second_publish.stderr)
        # Nothing was duplicated or replaced.
        receipt = self.receipt()
        self.assertEqual(receipt["exit_code"], 0)
        self.assertEqual(len(list((self.state_dir / "audit-receipts").iterdir())), 3)

    # -- tamper negatives ---------------------------------------------------

    def test_symlink_and_hardlink_and_mode_tamper_fail(self) -> None:
        self.publish_all()
        record_path = self.namespace / "installed-functional.json"
        receipt_path = self.state_dir / "audit-receipts/installed-harness-smoke.json"
        original = record_path.read_bytes()
        try:
            # A symlinked evidence record is never read.
            record_path.unlink()
            os.symlink(receipt_path, record_path)
            self.assertNotEqual(self.checker(check=False).returncode, 0,
                                "a symlinked evidence record must fail closed")
            record_path.unlink()
            record_path.write_bytes(original)
            os.chmod(record_path, 0o600)
            # A hardlink alias on the receipt breaks the single-link identity.
            os.link(receipt_path, self.tmp / "receipt-alias.json")
            self.assertNotEqual(self.checker(check=False).returncode, 0,
                                "a hardlinked receipt must fail closed")
            os.unlink(self.tmp / "receipt-alias.json")
            # A group/other-writable record is unsafe.
            os.chmod(record_path, 0o666)
            self.assertNotEqual(self.checker(check=False).returncode, 0,
                                "a group/other-writable record must fail closed")
            os.chmod(record_path, 0o600)
            # A symlinked receipt fails the hardened validation.
            receipt_backup = receipt_path.read_bytes()
            receipt_path.unlink()
            os.symlink(self.state_dir / "audit-coordinator.json", receipt_path)
            self.assertNotEqual(self.checker(check=False).returncode, 0,
                                "a symlinked receipt must fail closed")
            receipt_path.unlink()
            receipt_path.write_bytes(receipt_backup)
            os.chmod(receipt_path, 0o600)
        finally:
            # The checker is authoritative again after restoration.
            self.assertEqual(self.checker().returncode, 0, self.checker().stderr)

    def test_commit_and_receipt_tamper_fail(self) -> None:
        self.publish_all()
        record_path = self.namespace / "installed-functional.json"
        receipt_path = self.state_dir / "audit-receipts/installed-harness-smoke.json"
        try:
            # A stale/non-matching commit in the record is rejected.
            original = record_path.read_text(encoding="utf-8")
            record_path.write_text(original.replace(self.commit, "0" * 40))
            os.chmod(record_path, 0o600)
            self.assertNotEqual(self.checker(check=False).returncode, 0,
                                "a record bound to the wrong commit must fail")
            record_path.write_text(original)
            os.chmod(record_path, 0o600)
            # A forged receipt (different exit code) breaks the digest binding
            # and the hardened validation.
            receipt_data = json.loads(receipt_path.read_text(encoding="utf-8"))
            receipt_data["exit_code"] = 7
            receipt_path.write_text(
                json.dumps(receipt_data, sort_keys=True, indent=2) + "\n",
                encoding="utf-8",
            )
            os.chmod(receipt_path, 0o600)
            self.assertNotEqual(self.checker(check=False).returncode, 0,
                                "a forged receipt must fail closed")
            # A receipt minted for a different command is never accepted.
            receipt_data = json.loads(receipt_path.read_text(encoding="utf-8"))
            receipt_data["argv"] = ["./.factory/tools/true"]
            receipt_data["argv_sha256"] = _sha256(
                json.dumps(["./.factory/tools/true"], separators=(",", ":")).encode()
            )
            receipt_path.write_text(
                json.dumps(receipt_data, sort_keys=True, indent=2) + "\n",
                encoding="utf-8",
            )
            os.chmod(receipt_path, 0o600)
            self.assertNotEqual(self.checker(check=False).returncode, 0,
                                "a relabeled receipt argv must fail closed")
        finally:
            self.assertNotEqual(self.checker(check=False).returncode, 0,
                                "the tampered receipt must stay rejected")

    def test_stale_generic_namespace_is_ignored(self) -> None:
        """Only the exact-HEAD namespace is read; a stale one never counts."""
        self.publish_all()
        stale_commit = "0" * 40
        stale_ns = self.state_dir / "generic-evidence" / stale_commit
        stale_ns.mkdir(parents=True)
        record = self.record()
        record["commit"] = stale_commit
        (stale_ns / "installed-functional.json").write_text(
            json.dumps(record), encoding="utf-8"
        )
        os.chmod(stale_ns / "installed-functional.json", 0o600)
        # The exact-HEAD publication still passes; the stale namespace is
        # ignored (even though its own record is "valid").
        self.assertEqual(self.checker().returncode, 0, self.checker().stderr)
        # With the exact-HEAD namespace removed, the checker fails even though
        # the stale namespace (and the foreign root env) exist.
        shutil.rmtree(self.namespace)
        rejected = self.checker(check=False)
        self.assertNotEqual(rejected.returncode, 0,
                            "a stale generic namespace must never be read")

    # -- ancestor namespace semantics (Task 23) ------------------------------

    def test_genuine_base_namespace_remains_selected_after_metadata_commit(self) -> None:
        """The genuine base namespace — the evidence minted at the exact
        live audit coordinator base — remains selected after a later
        plan/conformance/audit metadata commit (docs here): the evidence
        commit is an ancestor of (or equal to) the final HEAD and the
        implementation/acceptance authority paths are unchanged since the
        evidence commit."""
        self.publish_all()
        docs = self.fixture / "docs" / "FACTORY.md"
        docs.write_text(
            docs.read_text(encoding="utf-8")
            + "\n<!-- post-evidence metadata-only commit -->\n",
            encoding="utf-8",
        )
        new_head = self._commit_all(self.fixture, "metadata-only sidecar commit")
        self.assertNotEqual(new_head, self.commit)
        check = self.checker()
        self.assertEqual(check.returncode, 0, check.stderr)
        self.assertIn(f"PASS at {self.commit}", check.stdout)

    def test_forged_candidate_never_shadows_valid_base_namespace(self) -> None:
        """A forged/invalid namespace at the final HEAD is excluded from the
        candidate set; the genuine base namespace still satisfies the checker
        (a planted namespace can neither certify nor falsify, and a forged
        candidate never shadows the unique valid selection)."""
        self.publish_all()
        docs = self.fixture / "docs" / "FACTORY.md"
        docs.write_text(
            docs.read_text(encoding="utf-8")
            + "\n<!-- post-evidence metadata-only commit -->\n",
            encoding="utf-8",
        )
        new_head = self._commit_all(self.fixture, "metadata-only sidecar commit")
        forged_ns = self.state_dir / "generic-evidence" / new_head
        forged_ns.mkdir(mode=0o700)
        forged = {
            "schema": "factory-generic-installed-functional/v1",
            "commit": new_head,
            "test": "test_installed_functional",
            "result": "PASS",
            "skipped": 0,
            "receipt": ".factory-state/audit-receipts/installed-harness-smoke.json",
            "receipt_sha256": "0" * 64,
            "suite_stdout_sha256": "0" * 64,
            "coordinator_round": 1,
            "coordinator_nonce": "0" * 64,
        }
        (forged_ns / "installed-functional.json").write_text(
            json.dumps(forged, sort_keys=True, indent=2) + "\n", encoding="utf-8"
        )
        os.chmod(forged_ns / "installed-functional.json", 0o600)
        check = self.checker()
        self.assertEqual(check.returncode, 0, check.stderr)
        self.assertIn(f"PASS at {self.commit}", check.stdout)
        # With the genuine base namespace removed, only the forged namespace
        # remains and the checker fails (invalid evidence can never satisfy
        # it; a forged candidate never shadows).
        shutil.rmtree(self.namespace)
        rejected = self.checker(check=False)
        self.assertNotEqual(rejected.returncode, 0)

    def test_planted_descendant_receipt_under_base_coordinator_is_rejected(self) -> None:
        """A hand-planted descendant namespace — a self-consistent receipt
        bound to a later metadata commit under the base coordinator — is
        cross-audit evidence: the strict binding
        (``coordinator.base_commit == receipt.evidence_commit == record.commit
        == namespace name``) excludes it, so the genuine base namespace
        remains selected and the planted descendant can neither replace nor
        shadow it.  Once the base namespace is gone, the planted descendant
        alone can never satisfy the checker."""
        self.publish_all()
        older = self.commit
        docs = self.fixture / "docs" / "FACTORY.md"
        docs.write_text(
            docs.read_text(encoding="utf-8")
            + "\n<!-- post-evidence metadata-only commit -->\n",
            encoding="utf-8",
        )
        newer = self._commit_all(self.fixture, "metadata-only sidecar commit")
        self.assertNotEqual(newer, older)
        # Planted descendant: its receipt is minted at ``newer`` under the
        # base coordinator (round 1 / base nonce), but the live coordinator
        # base is ``older``, so the record can never bind it.
        self._plant_namespace(newer)
        check = self.checker()
        self.assertEqual(check.returncode, 0, check.stderr)
        self.assertIn(f"PASS at {older}", check.stdout)
        self.assertNotIn(f"PASS at {newer}", check.stdout)
        # The planted descendant alone (genuine base removed) is rejected: a
        # descendant receipt under a base coordinator is never valid.
        shutil.rmtree(self.namespace)
        rejected = self.checker(check=False)
        self.assertNotEqual(rejected.returncode, 0,
                            "a planted descendant namespace must be rejected")
        self.assertIn("no valid generic evidence namespace", rejected.stderr)

    def test_two_candidate_paths_cannot_both_be_valid_under_one_coordinator(self) -> None:
        """Two candidate namespaces on incomparable branches of one merged
        history are each self-consistent at their own commit, but the single
        live coordinator base can equal only one commit: the strict binding
        (``coordinator.base_commit == receipt.evidence_commit == record.commit
        == namespace name``) leaves at most one valid candidate.  The genuine
        base namespace remains selected while present; without it, neither
        candidate is valid under the one coordinator and the checker fails
        closed naming both exclusions — never a traceback, never a guess
        between the two paths."""
        self.publish_all()
        base = self.commit
        self._git("checkout", "-q", "-b", "other")
        other_doc = self.fixture / "docs" / "OPERATIONS.md"
        other_doc.write_text(
            other_doc.read_text(encoding="utf-8")
            + "\n<!-- candidate branch -->\n",
            encoding="utf-8",
        )
        self._commit_all(self.fixture, "candidate branch Y")
        y_commit = self._git("rev-parse", "HEAD")
        self._git("checkout", "-q", "boilerplate-develop")
        main_doc = self.fixture / "docs" / "FACTORY.md"
        main_doc.write_text(
            main_doc.read_text(encoding="utf-8")
            + "\n<!-- main branch X -->\n",
            encoding="utf-8",
        )
        self._commit_all(self.fixture, "main branch X")
        x_commit = self._git("rev-parse", "HEAD")
        self._git("merge", "--no-ff", "-q", "-m", "merge candidate branch", "other")
        self._git("rev-parse", "HEAD")  # the merged HEAD
        self.assertNotEqual(x_commit, base)
        self.assertNotEqual(y_commit, base)
        self.assertNotEqual(x_commit, y_commit)
        # The coordinator stays bound to the genuine base; the two planted
        # candidates are cross-audit and can never both become valid.
        self._plant_namespace(x_commit)
        self._plant_namespace(y_commit)
        check = self.checker()
        self.assertEqual(check.returncode, 0, check.stderr)
        self.assertIn(f"PASS at {base}", check.stdout)
        self.assertNotIn(f"PASS at {x_commit}", check.stdout)
        self.assertNotIn(f"PASS at {y_commit}", check.stdout)
        # Without the genuine base, neither candidate is valid under the one
        # coordinator: the checker fails closed naming both exclusions.
        shutil.rmtree(self.namespace)
        rejected = self.checker(check=False)
        self.assertNotEqual(rejected.returncode, 0,
                            "two candidate paths cannot both be valid under "
                            "one coordinator")
        self.assertIn("no valid generic evidence namespace", rejected.stderr)
        self.assertIn(x_commit[:12], rejected.stderr)
        self.assertIn(y_commit[:12], rejected.stderr)
        self.assertNotIn("Traceback", rejected.stderr,
                         "the rejection must be a clear error, never a traceback")

    def test_authority_path_change_after_evidence_is_stale(self) -> None:
        """A commit that changes an implementation/acceptance authority path
        after the evidence was minted makes the ancestor evidence stale and
        the checker fails closed (metadata commits are allowed, authority
        commits are not)."""
        self.publish_all()
        checker = self.fixture / ".factory/tools/check-installed-functional-evidence.sh"
        checker.write_text(
            checker.read_text(encoding="utf-8") + "\n# authority drift\n",
            encoding="utf-8",
        )
        checker.chmod(0o755)
        self._commit_all(self.fixture, "authority drift commit")
        rejected = self.checker(check=False)
        self.assertNotEqual(rejected.returncode, 0)
        self.assertIn("authority changed", rejected.stderr)

    # -- explicit namespace validation (Task 23) -----------------------------

    def test_explicit_namespace_missing_argument_fails(self) -> None:
        result = _run(
            ["bash", str(self.fixture / ".factory/tools/check-installed-functional-evidence.sh"),
             "--namespace"],
            cwd=str(self.fixture),
            env=self.base_env(),
            check=False,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("requires a non-empty path", result.stderr)

    def test_explicit_namespace_outside_root_fails(self) -> None:
        self.publish_all()
        result = _run(
            ["bash", str(self.fixture / ".factory/tools/check-installed-functional-evidence.sh"),
             "--namespace", "/tmp/outside-root"],
            cwd=str(self.fixture),
            env=self.base_env(),
            check=False,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("inside the repository root", result.stderr)

    def test_explicit_namespace_traversal_fails(self) -> None:
        self.publish_all()
        result = _run(
            ["bash", str(self.fixture / ".factory/tools/check-installed-functional-evidence.sh"),
             "--namespace", "../generic-evidence"],
            cwd=str(self.fixture),
            env=self.base_env(),
            check=False,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("unsafe namespace path component", result.stderr)

    def test_explicit_namespace_symlink_parent_fails(self) -> None:
        self.publish_all()
        symlink_parent = self.state_dir / "link"
        os.symlink(self.state_dir / "generic-evidence", symlink_parent)
        result = _run(
            ["bash", str(self.fixture / ".factory/tools/check-installed-functional-evidence.sh"),
             "--namespace", ".factory-state/link/" + self.commit],
            cwd=str(self.fixture),
            env=self.base_env(),
            check=False,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("symlink", result.stderr)

    def test_explicit_namespace_valid_fixture_path_passes(self) -> None:
        """An explicit safe-mode namespace under the repository root that is
        a valid publication still satisfies the checker."""
        self.publish_all()
        check = _run(
            ["bash", str(self.fixture / ".factory/tools/check-installed-functional-evidence.sh"),
             "--namespace", f".factory-state/generic-evidence/{self.commit}"],
            cwd=str(self.fixture),
            env=self.base_env(),
        )
        self.assertIn(f"PASS at {self.commit}", check.stdout)

    def test_skip_marker_in_certified_transcript_is_rejected(self) -> None:
        """A self-consistent transcript that carries a skip marker can never
        be certified PASS: the checker re-scans the certified suite stdout
        even when every digest binding still matches."""
        self.publish_all()
        transcript = (
            self.state_dir / "audit-receipts/installed-harness-smoke.stdout"
        )
        receipt_path = self.state_dir / "audit-receipts/installed-harness-smoke.json"
        record_path = self.namespace / "installed-functional.json"
        original_transcript = transcript.read_bytes()
        try:
            # A self-consistent forged transcript with a skip marker: update
            # the transcript, the receipt stdout digest, and the record's
            # suite-stdout binding so every digest still matches.
            forged = original_transcript + b"\nSKIPPED: fixture unavailable\n"
            transcript.write_bytes(forged)
            os.chmod(transcript, 0o600)
            receipt_data = json.loads(receipt_path.read_text(encoding="utf-8"))
            receipt_data["stdout_sha256"] = _sha256(forged)
            receipt_path.write_text(
                json.dumps(receipt_data, sort_keys=True, indent=2) + "\n",
                encoding="utf-8",
            )
            os.chmod(receipt_path, 0o600)
            record = json.loads(record_path.read_text(encoding="utf-8"))
            record["suite_stdout_sha256"] = _sha256(forged)
            # The record's receipt digest binding must follow the forged
            # receipt bytes so every digest stays self-consistent and the
            # checker reaches the transcript skip-marker scan.
            record["receipt_sha256"] = _sha256(receipt_path.read_bytes())
            record_path.write_text(
                json.dumps(record, sort_keys=True, indent=2) + "\n",
                encoding="utf-8",
            )
            os.chmod(record_path, 0o600)
            rejected = self.checker(check=False)
            self.assertNotEqual(rejected.returncode, 0,
                                "a certified skip-marker transcript must fail")
            self.assertIn("skip marker", rejected.stderr)
        finally:
            self.assertNotEqual(self.checker(check=False).returncode, 0,
                                "the forged skip transcript must stay rejected")

    # -- coordinator sequencing and crash recovery ---------------------------

    def _plant_coordinator(self, nonce: str | None = None, *, round_number: int = 1,
                           base_commit: str | None = None) -> str:
        """Plant an audit coordinator bound to the fixture (default exact match)."""
        if nonce is None:
            nonce = hashlib.sha256(os.urandom(32)).hexdigest()
        (self.state_dir / "audit-coordinator.json").write_text(
            json.dumps({
                "schema": "ralph-audit-coordinator/v1",
                "round": round_number,
                "base_commit": base_commit if base_commit is not None else self.commit,
                "nonce": nonce,
                "created_at": int(time.time()),
            }, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        os.chmod(self.state_dir / "audit-coordinator.json", 0o600)
        return nonce

    def _git(self, *args: str, check: bool = True) -> str:
        """Run one pinned Git command in the fixture; return trimmed stdout."""
        return _run(
            [gitutil.GIT_EXECUTABLE, "-C", str(self.fixture), *args],
            check=check,
        ).stdout.strip()

    def _plant_namespace(self, commit: str) -> dict:
        """Hand-construct one self-consistent hardened namespace at ``commit``
        **for negative tests only**.

        Positive evidence always comes from the real publisher + trusted
        wrapper (``publish_all``); the trusted publisher refuses duplicate
        publication (every canonical path must not preexist), so the
        rejection tests plant a candidate namespace by hand: a
        byte-consistent installed-harness receipt (json + stdout + stderr
        transcripts, hardened owner/mode/link-count, allowlisted argv, the
        exact commit and the live coordinator round/nonce) plus the evidence
        record binding every digest exactly like a real publication.  Under
        the strict invariant
        (``coordinator.base_commit == receipt.evidence_commit == record.commit
        == namespace name``) such a hand-planted namespace at any commit
        other than the live coordinator base is cross-audit evidence and is
        rejected/excluded by the checker.  Returns the record.
        """
        coordinator = json.loads(
            (self.state_dir / "audit-coordinator.json").read_text(encoding="utf-8")
        )
        round_number = coordinator["round"]
        nonce = coordinator["nonce"]
        argv = ["./.factory/tests/test-factory-installed.sh"]
        namespace = self.state_dir / "generic-evidence" / commit
        namespace.mkdir(mode=0o700, parents=True)
        os.chmod(namespace, 0o700)
        stdout = b"test-factory-installed: all checks passed\n"
        stderr = b""
        receipt = {
            "schema": "ralph-audit-receipt/v1",
            "tag": "installed-harness-smoke",
            "argv": argv,
            "argv_sha256": _sha256(
                json.dumps(argv, separators=(",", ":")).encode("utf-8")
            ),
            "exit_code": 0,
            "stdout_sha256": _sha256(stdout),
            "stderr_sha256": _sha256(stderr),
            "started_at": int(time.time()),
            "finished_at": int(time.time()),
            "evidence_commit": commit,
            "coordinator_round": round_number,
            "coordinator_nonce": nonce,
        }
        receipts = self.state_dir / "audit-receipts"
        receipts.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(receipts, 0o700)
        stem = f"installed-harness-smoke-{commit}"
        receipt_path = receipts / f"{stem}.json"
        receipt_path.write_text(
            json.dumps(receipt, sort_keys=True, indent=2) + "\n", encoding="utf-8"
        )
        os.chmod(receipt_path, 0o600)
        (receipts / f"{stem}.stdout").write_bytes(stdout)
        os.chmod(receipts / f"{stem}.stdout", 0o600)
        (receipts / f"{stem}.stderr").write_bytes(stderr)
        os.chmod(receipts / f"{stem}.stderr", 0o600)
        record = {
            "schema": "factory-generic-installed-functional/v1",
            "commit": commit,
            "test": "test_installed_functional",
            "result": "PASS",
            "skipped": 0,
            "receipt": f".factory-state/audit-receipts/{stem}.json",
            "receipt_sha256": _sha256(receipt_path.read_bytes()),
            "suite_stdout_sha256": _sha256(stdout),
            "coordinator_round": round_number,
            "coordinator_nonce": nonce,
        }
        record_path = namespace / "installed-functional.json"
        record_path.write_text(
            json.dumps(record, sort_keys=True, indent=2) + "\n", encoding="utf-8"
        )
        os.chmod(record_path, 0o600)
        return record

    def test_existing_exact_matching_coordinator_is_reused(self) -> None:
        """An existing exact-matching coordinator is reused, never overwritten:
        the evidence record binds the existing nonce, not the staged one."""
        existing_nonce = self._plant_coordinator()
        self.publish_all()
        record = self.record()
        self.assertEqual(record["coordinator_nonce"], existing_nonce,
                         "the reused coordinator's nonce must be adopted")
        coordinator = json.loads(
            (self.state_dir / "audit-coordinator.json").read_text(encoding="utf-8")
        )
        self.assertEqual(coordinator["nonce"], existing_nonce)
        self.assertEqual(coordinator["created_at"], coordinator["created_at"])
        self.assertEqual(self.checker().returncode, 0, self.checker().stderr)
        # The staged nonce was never written anywhere.
        self.assertNotEqual(record["coordinator_nonce"],
                            json.loads(
                                (self.staging / "staging.json").read_text(
                                    encoding="utf-8"))["nonce"])

    def test_existing_mismatched_coordinator_fails_closed(self) -> None:
        """A coordinator bound to a different round/commit is never
        overwritten: the publication fails closed before any write."""
        self._plant_coordinator(round_number=7)
        before = self.state_files()
        self.prepare()
        failed = self.publish(check=False)
        self.assertNotEqual(failed.returncode, 0)
        self.assertIn("does not match", failed.stderr)
        self.assertEqual(self.state_files(), before,
                         "a mismatched coordinator must abort before any write")
        self.assertFalse((self.state_dir / "generic-evidence").exists())

    def test_partial_receipt_crash_fails_closed_no_deletion(self) -> None:
        """A torn canonical receipt set (crash during receipt publication) is
        never deleted or overwritten: publish fails explicitly."""
        self._plant_coordinator()
        receipts = self.state_dir / "audit-receipts"
        receipts.mkdir(mode=0o700)
        os.chmod(receipts, 0o700)
        partial = receipts / "installed-harness-smoke.stdout"
        partial.write_bytes(b"test-factory-installed: all checks passed\n")
        os.chmod(partial, 0o600)
        before = self.state_files()
        self.prepare()
        failed = self.publish(check=False)
        self.assertNotEqual(failed.returncode, 0)
        self.assertIn("partial crash", failed.stderr)
        self.assertEqual(self.state_files(), before,
                         "a torn receipt set must never be deleted or replaced")
        self.assertTrue(partial.exists())
        self.assertFalse((self.state_dir / "generic-evidence").exists())

    def test_resume_accepts_complete_validated_receipt_after_crash(self) -> None:
        """Crash recovery: a complete validated canonical receipt set (crash
        after the receipt publish, before the evidence record) resumes
        deterministically — the receipt is never re-executed or re-minted and
        the evidence record binds the existing artifacts."""
        os.chmod(self.state_dir, 0o700)
        existing_nonce = self._plant_coordinator()
        # Mint the full receipt set canonically through the trusted wrapper
        # BEFORE prepare (simulating the crashed run's published receipt).
        minted = self.publisher_mint()
        self.assertIn("installed-harness-smoke.json", minted.stdout)
        before = self.state_files()
        self.prepare()
        self.publish()
        record = self.record()
        self.assertEqual(record["coordinator_nonce"], existing_nonce)
        receipt = self.receipt()
        self.assertEqual(record["receipt_sha256"],
                         _sha256((self.state_dir / "audit-receipts/"
                                  "installed-harness-smoke.json").read_bytes()))
        # The resumed receipt was not re-minted: its transcript is the
        # original stub suite output and its coordinator binding is the
        # pre-existing coordinator's nonce.
        self.assertEqual(receipt["coordinator_nonce"], existing_nonce)
        self.assertEqual(receipt["exit_code"], 0)
        check = self.checker()
        self.assertEqual(check.returncode, 0, check.stderr)
        self.assertIn(f"PASS at {self.commit}", check.stdout)
        added = sorted(set(self.state_files()) - set(before))
        self.assertEqual(
            added,
            [f"generic-evidence/{self.commit}/installed-functional.json",
             f"generic-evidence/{self.commit}/preservation.json"],
            "the resume must only add the missing evidence record and proof",
        )

    def _new_staging(self) -> Path:
        staging = self.tmp / f"staging-{len(list(self.tmp.iterdir()))}"
        return staging

    def test_empty_partial_namespace_resume_finalizes(self) -> None:
        """SIGKILL window: a crash after the namespace appeared but before any
        canonical file was written leaves an empty canonical namespace.  The
        re-publication (fresh staging record + coordinator + validated
        receipt) finalizes it when the expected record bytes match."""
        self.publish_all()
        # Simulate the crash window: the canonical namespace exists but is
        # empty (no record, no proof yet).
        (self.namespace / "installed-functional.json").unlink()
        (self.namespace / "preservation.json").unlink()
        before = self.state_files()
        staging = self._new_staging()
        self.publisher("prepare", "--staging", str(staging))
        self.publisher("publish", "--staging", str(staging))
        record = self.record()
        self.assertEqual(record["commit"], self.commit)
        self.assertEqual(record["result"], "PASS")
        self.assertTrue((self.namespace / "preservation.json").is_file())
        # Only the record and proof were added on top of the crashed state.
        after = self.state_files()
        added = sorted(set(after) - set(before))
        self.assertEqual(
            added,
            [f"generic-evidence/{self.commit}/installed-functional.json",
             f"generic-evidence/{self.commit}/preservation.json"],
            "the empty-partial resume must only add the missing files",
        )
        self.assertEqual(self.checker().returncode, 0, self.checker().stderr)

    def test_record_only_partial_namespace_resume_finalizes(self) -> None:
        """SIGKILL window: a crash after the record was published but before
        the preservation proof leaves a record-only namespace.  The
        re-publication validates the record bytes against the exact staging
        record + coordinator + validated receipt and only then writes the
        missing proof."""
        self.publish_all()
        (self.namespace / "preservation.json").unlink()
        expected_record = (self.namespace / "installed-functional.json").read_bytes()
        before = self.state_files()
        staging = self._new_staging()
        self.publisher("prepare", "--staging", str(staging))
        self.publisher("publish", "--staging", str(staging))
        self.assertEqual(
            (self.namespace / "installed-functional.json").read_bytes(),
            expected_record,
            "the resume must never rewrite the existing record",
        )
        self.assertTrue((self.namespace / "preservation.json").is_file())
        after = self.state_files()
        added = sorted(set(after) - set(before))
        self.assertEqual(added, [f"generic-evidence/{self.commit}/preservation.json"])
        self.assertEqual(self.checker().returncode, 0, self.checker().stderr)

    def test_tampered_partial_namespace_fails_without_deletion(self) -> None:
        """A partial namespace whose record does not match the expected
        binding bytes (tampered commit) fails closed with no deletion and no
        completion."""
        self.publish_all()
        (self.namespace / "preservation.json").unlink()
        record_path = self.namespace / "installed-functional.json"
        record_path.write_text(
            record_path.read_text(encoding="utf-8").replace(self.commit, "0" * 40),
            encoding="utf-8",
        )
        os.chmod(record_path, 0o600)
        staging = self._new_staging()
        self.publisher("prepare", "--staging", str(staging))
        failed = self.publisher("publish", "--staging", str(staging), check=False)
        self.assertNotEqual(failed.returncode, 0)
        self.assertIn("does not match the expected binding", failed.stderr)
        self.assertTrue(record_path.exists(), "the tampered record must not be deleted")
        self.assertFalse(
            (self.namespace / "preservation.json").exists(),
            "a tampered partial must never be completed",
        )

    def test_partial_namespace_unexpected_file_fails_without_deletion(self) -> None:
        """A partial namespace carrying an unexpected file is ambiguous and
        fails closed with no deletion."""
        self.publish_all()
        (self.namespace / "preservation.json").unlink()
        intruder = self.namespace / "intruder.json"
        intruder.write_text("{}", encoding="utf-8")
        staging = self._new_staging()
        failed = self.publisher("prepare", "--staging", str(staging), check=False)
        self.assertNotEqual(failed.returncode, 0)
        self.assertIn("unexpected files", failed.stderr)
        self.assertTrue(intruder.exists(), "the unexpected file must not be deleted")
        self.assertTrue((self.namespace / "installed-functional.json").exists())

    def test_republish_after_complete_namespace_fails_closed_no_deletion(self) -> None:
        """A completed namespace (record + proof) can never be re-published:
        duplicate publication fails closed and the completed artifacts are
        untouched."""
        self.publish_all()
        record_bytes = (self.namespace / "installed-functional.json").read_bytes()
        proof_bytes = (self.namespace / "preservation.json").read_bytes()
        staging = self._new_staging()
        failed = self.publisher("prepare", "--staging", str(staging), check=False)
        self.assertNotEqual(failed.returncode, 0)
        self.assertIn("must not preexist", failed.stderr)
        self.assertEqual(
            (self.namespace / "installed-functional.json").read_bytes(), record_bytes
        )
        self.assertEqual(
            (self.namespace / "preservation.json").read_bytes(), proof_bytes
        )

    def test_hostile_passing_suite_extra_file_leaves_zero_canonical_artifacts(self) -> None:
        """A passing suite that adds any file to `.factory-state` (an
        addition, mutation, or deletion) aborts before the first canonical
        write: no coordinator, no receipts, no evidence namespace."""
        self._replace_suite(
            "#!/usr/bin/env bash\n"
            "set -euo pipefail\n"
            "echo 'test-factory-installed: all checks passed'\n"
            "echo hostile > \"$PWD/.factory-state/hostile-extra\"\n"
        )
        self.commit = self._commit_all(self.fixture, "hostile passing suite")
        before = self.state_files()
        self.prepare()
        failed = self.publish(check=False)
        self.assertNotEqual(failed.returncode, 0)
        self.assertIn("no additions", failed.stderr)
        # Zero canonical artifacts: the coordinator, receipts, and evidence
        # namespace were never created, and no pre-existing file changed.
        self.assertFalse((self.state_dir / "audit-coordinator.json").exists())
        self.assertFalse((self.state_dir / "audit-receipts").exists())
        self.assertFalse((self.state_dir / "generic-evidence").exists())
        after = self.state_files()
        for rel, entry in before.items():
            self.assertEqual(after[rel], entry, f"foreign file changed: {rel}")

    def publisher_mint(self) -> subprocess.CompletedProcess[str]:
        """Mint the installed-harness receipt through the real wrapper against
        the fixture's planted coordinator (the stub suite exits 0)."""
        coordinator = json.loads(
            (self.state_dir / "audit-coordinator.json").read_text(encoding="utf-8")
        )
        return _run(
            [sys.executable,
             str(self.fixture / ".factory" / "tools" / "machine-receipt.py"),
             "--root", str(self.fixture), "--tag", "installed-harness-smoke",
             "--audit-round", str(coordinator["round"]),
             "--evidence-commit", str(coordinator["base_commit"]),
             "--nonce", str(coordinator["nonce"]),
             "--", "./.factory/tests/test-factory-installed.sh"],
            cwd=str(self.fixture),
            env=self.base_env(),
        )

    # -- stderr channel skip markers (E) -------------------------------------

    def test_stderr_skip_marker_is_rejected(self) -> None:
        """A skip marker on the stderr channel disqualifies the suite even
        when stdout is clean and the exit code is 0."""
        self._replace_suite(
            "#!/usr/bin/env bash\n"
            "set -euo pipefail\n"
            "echo 'test-factory-installed: all checks passed'\n"
            "echo 'SKIPPED: fixture environment unavailable' >&2\n"
        )
        self.commit = self._commit_all(self.fixture, "stderr-skipping suite")
        before = self.state_files()
        self.prepare()
        skipped = self.publish(check=False)
        self.assertNotEqual(skipped.returncode, 0)
        self.assertIn("skip marker", skipped.stderr)
        self.assertEqual(self.state_files(), before,
                         "a stderr-skipped suite must not add or change any "
                         ".factory-state file")
        self.assertFalse((self.state_dir / "audit-coordinator.json").exists())

    # -- failed/skipped suites leave no artifacts ---------------------------

    def test_failed_suite_leaves_no_artifacts(self) -> None:
        self._replace_suite(FAILING_SUITE)
        self.commit = self._commit_all(self.fixture, "failing suite")
        before = self.state_files()
        self.prepare()
        failed = self.publish(check=False)
        self.assertNotEqual(failed.returncode, 0)
        self.assertIn("exited nonzero", failed.stderr)
        after = self.state_files()
        self.assertEqual(after, before,
                         "a failed installed suite must not add or change any "
                         ".factory-state file")
        self.assertFalse((self.state_dir / "audit-coordinator.json").exists())
        self.assertFalse((self.state_dir / "generic-evidence").exists())
        self.assertFalse((self.state_dir / "audit-receipts").exists())

    def test_skipped_suite_leaves_no_artifacts(self) -> None:
        self._replace_suite(SKIPPING_SUITE)
        self.commit = self._commit_all(self.fixture, "skipping suite")
        before = self.state_files()
        self.prepare()
        skipped = self.publish(check=False)
        self.assertNotEqual(skipped.returncode, 0)
        self.assertIn("skip marker", skipped.stderr)
        after = self.state_files()
        self.assertEqual(after, before,
                         "a skipped installed suite must not add or change any "
                         ".factory-state file")
        self.assertFalse((self.state_dir / "audit-coordinator.json").exists())
        self.assertFalse((self.state_dir / "generic-evidence").exists())
        self.assertFalse((self.state_dir / "audit-receipts").exists())

    # -- the real installed harness suite end-to-end -------------------------

    def test_real_installed_suite_publication_end_to_end(self) -> None:
        """The real committed installed suite runs at the exact fixture commit.

        The fixture carries the full committed installed surface (no stub),
        so the publisher executes the genuine Task-20 installed harness suite
        through the bounded sanitized runner and the whole publication and
        acceptance chain holds.
        """
        self._replace_suite("")
        real_suite = self.fixture / ".factory/tests/test-factory-installed.sh"
        real_suite.write_bytes(
            (ROOT / ".factory/tests/test-factory-installed.sh").read_bytes()
        )
        real_suite.chmod(0o755)
        self.commit = self._commit_all(self.fixture, "real suite surface")
        before = self.state_files()
        self.prepare()
        self.publish()
        after = self.state_files()
        added = sorted(set(after) - set(before))
        self.assertEqual(len(added), 6, added)
        check = self.checker()
        self.assertEqual(check.returncode, 0, check.stderr)
        self.assertIn(f"PASS at {self.commit}", check.stdout)
        receipt = self.receipt()
        self.assertEqual(receipt["exit_code"], 0)
        self.assertEqual(receipt["evidence_commit"], self.commit)


if __name__ == "__main__":
    unittest.main(verbosity=2)
