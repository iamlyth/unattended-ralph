#!/usr/bin/env python3
"""Phase 2C2a: authenticated task path-lease launch/confinement tests.

This suite lives under the hidden ``.factory/tests/`` namespace and is the
deterministic verification for the Phase 2C2a runtime-launch foundation: the
optional authenticated task path-lease bound into the existing signed launch
authority (HMAC/FD token), the exact deny-dominant write candidates granted
to the workspace confinement, and the immutable ``audit_required`` signal
for CI/security-sensitive scopes.  No campaign minting happens here — the
lease is minted by the trusted harness as DATA and the launch authority
re-validates it against the committed policy and the trusted context.

Coverage:

* **claim is DATA, never self-authorizing**: the claim digest alone is never
  authoritative — a real provider launch without an existing signed launch
  token (HMAC/FD readiness store) fails closed even with a perfectly valid
  claim, and a consumed/replayed token fails closed;
* **exact canonical bytes**: a delivered lease whose bytes differ from the
  bound canonical claim bytes (substitution/foreign claim) fails closed, and
  a tampered claim (granted set edited without a recomputed digest) fails
  closed at parse;
* **context/expiry**: a claim minted for a foreign campaign, an expired
  claim, and a not-yet-issued claim all fail closed at authorize; an expiry
  that lands between authorize and spawn fails closed immediately before
  spawn (supervisor revalidation) and inside the confined child (launcher
  revalidation);
* **committed-policy binding**: a worktree policy tamper (deny weakened
  after authorize) fails closed before spawn and inside the child via the
  policy digest;
* **deny-dominant at every step**: a forged claim that grants an immutable
  deny zone fails closed at validate (drift from the trusted expansion), and
  the confined child cannot write any deny zone;
* **exact write-path scope**: the lease adds WRITE-only rules for exactly
  the policy-expanded candidates — never read/execute/commands/credentials —
  and a real confined fixture proves the exact granted directory/prefix is
  writable while every sibling and deny zone stays denied (with an
  unconfined control proving the denial is not vacuous);
* **no-lease unchanged**: without a lease the confinement specification is
  byte-identical in behavior (no lease section, no lease rules, no
  ``audit_required``);
* **symlink/hardlink/mount**: a lease candidate that is a symlink, a
  hardlinked regular file, or on a different device than the workspace fails
  closed at specification construction;
* **audit_required immutable and result-bound**: a CI/security-sensitive
  claim sets the immutable signal on the verified binding and the launch
  result; the model can never clear it (a binding that claims
  ``audit_required`` without an authenticated lease is rejected);
* **cleanup**: a failed authorization removes every per-launch private
  directory it created.

The whole suite runs hermetically: synthetic tokens and loopback fixtures
only, no real credential is ever read, and every temporary fixture workspace
is removed.
"""

from __future__ import annotations

import base64
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import types
import unittest
import unittest.mock as mock
from datetime import datetime, timedelta, timezone

ROOT = Path(__file__).resolve().parents[2]
LOOP = ROOT / ".factory" / "loop"

sys.path.insert(0, str(LOOP))
import launch  # noqa: E402
import path_lease as lease  # noqa: E402
import readiness  # noqa: E402
import workspace_confinement as wc  # noqa: E402

# Reuse the committed launch-suite fixture workspace (exact .factory/tools
# layout, backend, plan/spec/role/policy blobs) and the confinement-suite
# Landlock probe helpers — never copied.
_LAUNCH_SUITE = ROOT / ".factory" / "tests" / "test-factory-launch.py"
_launch_spec = importlib.util.spec_from_file_location(
    "factory_lease_launch_suite", _LAUNCH_SUITE)
LAUNCH_SUITE = importlib.util.module_from_spec(_launch_spec)
assert _launch_spec.loader is not None
_launch_spec.loader.exec_module(LAUNCH_SUITE)

_CONF_SUITE = ROOT / ".factory" / "tests" / "test-factory-confinement.py"
_conf_spec = importlib.util.spec_from_file_location(
    "factory_lease_conf_suite", _CONF_SUITE)
CONF_SUITE = importlib.util.module_from_spec(_conf_spec)
assert _conf_spec.loader is not None
_conf_spec.loader.exec_module(CONF_SUITE)

PY = os.path.realpath(sys.executable)

# The committed fixture path-lease policy: product-owned verification
# surfaces (scripts, CI) plus the absolute immutable deny zones.  Deny is
# dominant and non-overridable.
FIXTURE_POLICY = {
    "schema": "factory-path-lease-policy/v1",
    "scopes": {
        "scripts": {
            "paths": ["scripts"],
            "patterns": ["scripts/*.sh", "scripts/*.py"],
            "audit_required": False,
        },
        "ci": {
            "paths": [".github"],
            "patterns": [],
            "audit_required": True,
        },
    },
    "deny": {
        "paths": [
            ".factory", ".factory-state", ".git", "docs/SPEC.md", ".env",
            "secrets", "release", "goldens", "goldens/approved",
        ],
        "patterns": [
            "**/*.pem", "*.pem", "**/*.key", "*.key",
            "**/.env*", ".env*", "**/*.secret", "*.secret",
        ],
    },
}

# An exact-file policy: the lease grants exactly one existing regular file,
# so the confined child can write it while every sibling stays denied.
EXACT_POLICY = {
    "schema": "factory-path-lease-policy/v1",
    "scopes": {
        "exact": {
            "paths": ["scripts/deploy.sh"],
            "patterns": [],
            "audit_required": False,
        },
    },
    "deny": {
        "paths": [
            ".factory", ".factory-state", ".git", "docs/SPEC.md", ".env",
            "secrets", "release", "goldens", "goldens/approved",
        ],
        "patterns": [
            "**/*.pem", "*.pem", "**/*.key", "*.key",
            "**/.env*", ".env*", "**/*.secret", "*.secret",
        ],
    },
}

DENY_ZONE_WRITES = ()


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _commit_policy(workspace: Path, policy: dict) -> None:
    (workspace / ".factory" / "path-lease-policy.json").write_text(
        json.dumps(policy), encoding="utf-8"
    )


# ---------------------------------------------------------------------------
# Launch-authority lease tests (synthetic + real-provider token binding)
# ---------------------------------------------------------------------------

class LeaseLaunchAuthorityTests(LAUNCH_SUITE._Base):
    """The lease is DATA: digest, context, expiry, and signed-token binding."""

    def setUp(self) -> None:
        super().setUp()
        # The launch authority stages the exact committed path-lease module
        # beside the confine launcher (F2); the fixture must commit it.
        shutil.copy2(
            LOOP / "path_lease.py",
            self.workspace / ".factory" / "loop" / "path_lease.py",
        )
        _commit_policy(self.workspace, FIXTURE_POLICY)
        (self.workspace / "scripts" / "deploy.sh").write_text(
            "#!/bin/sh\necho deploy\n", encoding="utf-8"
        )
        os.chmod(self.workspace / "scripts" / "deploy.sh", 0o700)
        (self.workspace / ".github" / "workflows").mkdir(parents=True)
        (self.workspace / ".github" / "workflows" / "ci.yml").write_text(
            "name: ci\n", encoding="utf-8"
        )
        self._git("add", "-A")
        self._git("commit", "-qm", "lease fixture")
        self.head = self._git("rev-parse", "HEAD").stdout.strip()

    def _mint(
        self, binding, scopes, *, campaign_id=None, attempt=None,
        issued=None, deadline=None,
    ):
        policy = lease.load_policy_config(self.workspace)
        claim = lease.mint_claim(
            campaign_id=campaign_id or binding.campaign_id,
            task_id=binding.task_id,
            attempt=attempt or binding.attempt,
            head_commit=binding.bound_commit,
            plan_digest=binding.plan_digest,
            policy_digest=lease.policy_digest(policy),
            requested_scopes=scopes,
            policy=policy,
            issued_at=issued,
            deadline=deadline,
        )
        return claim, policy

    def _lease_binding(
        self, scopes=("scripts",), *, campaign_id="campaign-1", attempt=1,
        **mint_kwargs,
    ):
        binding, role_bytes, agents_bytes, spec_bytes, plan_bytes = (
            self.make_binding(role="developer")
        )
        claim, _policy = self._mint(
            binding, scopes, campaign_id=campaign_id, attempt=attempt,
            **mint_kwargs,
        )
        claim_bytes = lease.claim_to_bytes(claim)
        binding = launch.replace(
            binding,
            campaign_id=campaign_id,
            attempt=attempt,
            lease_digest=claim.claim_digest,
            lease_bytes=claim_bytes,
        )
        return binding, claim_bytes, role_bytes, agents_bytes, spec_bytes, plan_bytes

    def _authorize_lease(
        self, binding, claim_bytes, *, store=None, token="", claims=None
    ):
        kwargs = {}
        if binding.role == "developer":
            kwargs["task_excerpt"] = launch.task_excerpt_bytes(
                (self.workspace / "plan.md").read_bytes(), binding.task_id
            )
        return launch.authorize_launch(
            binding,
            role_prompt=(self.workspace / "role.md").read_bytes(),
            agents=(self.workspace / "AGENTS.md").read_bytes(),
            spec=(self.workspace / "spec.md").read_bytes(),
            plan=(self.workspace / "plan.md").read_bytes(),
            lease=claim_bytes,
            _authorization_store=store,
            _authorization_token=token,
            _authorization_claims=claims,
            **kwargs,
        )

    def _locked_store(self) -> readiness.AuthorizationStore:
        root = Path(tempfile.mkdtemp(prefix="factory-lease-auth.", dir=str(ROOT)))
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        (root / "state" / "campaign").mkdir(parents=True, mode=0o700)
        lock_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
        self.addCleanup(os.close, lock_fd)
        nonce = "a" * 64
        doc = readiness.result_document(
            campaign_id="campaign-1", nonce=nonce, status="complete",
            bindings={}, results={"aggregate": "1" * 64},
        )
        store = readiness.AuthorizationStore(
            root, "state/campaign", "campaign-1", nonce, lock_fd, doc
        )
        self.addCleanup(store.close)
        return store

    def _commit_custom_policy(self, policy: dict) -> None:
        _commit_policy(self.workspace, policy)
        self._git("add", "-A")
        self._git("commit", "-qm", "custom lease policy")
        self.head = self._git("rev-parse", "HEAD").stdout.strip()

    def _forge(self, claim, **changes):
        """Rebuild a claim with edited fields and a recomputed digest.

        The trusted harness may mint only valid claims (a negative lease
        duration is rejected), so an expired/not-yet-issued claim is forged
        from a valid one with the digest recomputed — the launch authority
        must still reject it on context/expiry, never on the digest.
        """
        data = dict(claim.__dict__)
        data.update(changes)
        forged = lease.TaskPathLease(**data)
        return lease.TaskPathLease(
            **{**forged.__dict__, "claim_digest": lease.claim_digest(forged)}
        )

    # -- no-lease unchanged ------------------------------------------------

    def test_no_lease_unchanged(self) -> None:
        """Without a lease the confinement is unchanged: no lease section, no
        lease write rules, no audit_required signal."""
        binding, _role, _agents, _spec, _plan = self.make_binding(
            role="developer"
        )
        authority = self._authorize_lease(binding, None)
        self.assertNotIn("lease", authority._confinement_spec)
        self.assertFalse(authority._binding.audit_required)
        scripts = (self.workspace / "scripts").absolute()
        for rule in authority._confinement_spec["rules"]:
            if Path(str(rule["path"])).absolute() == scripts:
                self.assertNotEqual(
                    rule["access"], ["write"],
                    "no lease may add a WRITE-only rule for scripts",
                )

    # -- exact canonical bytes / digest ------------------------------------

    def test_lease_adds_exact_write_only_rule(self) -> None:
        """The authenticated lease adds exactly one WRITE-only rule for the
        policy-expanded candidate — never read/execute/commands."""
        binding, claim_bytes, *_ = self._lease_binding(("scripts",))
        authority = self._authorize_lease(binding, claim_bytes)
        scripts = (self.workspace / "scripts").absolute()
        scripts_rules = [
            rule for rule in authority._confinement_spec["rules"]
            if Path(str(rule["path"])).absolute() == scripts
        ]
        write_rules = [
            rule for rule in scripts_rules if rule["access"] == ["write"]
        ]
        self.assertEqual(
            len(write_rules), 1,
            "the lease must add exactly one WRITE-only rule for scripts",
        )
        self.assertEqual(
            write_rules[0]["access"], ["write"],
            "the lease rule must be WRITE-only — never read/execute",
        )

    def test_lease_section_embedded(self) -> None:
        """The exact claim/context travels in the confinement specification."""
        binding, claim_bytes, *_ = self._lease_binding(("scripts",))
        authority = self._authorize_lease(binding, claim_bytes)
        section = authority._confinement_spec["lease"]
        self.assertEqual(base64.b64decode(section["claim"]), claim_bytes)
        context = section["context"]
        self.assertEqual(context["campaign_id"], "campaign-1")
        self.assertEqual(context["task_id"], binding.task_id)
        self.assertEqual(context["attempt"], 1)
        self.assertEqual(context["head_commit"], binding.bound_commit)
        self.assertEqual(context["plan_digest"], binding.plan_digest)
        self.assertEqual(
            context["policy_digest"],
            lease.policy_digest(lease.load_policy_config(self.workspace)),
        )

    def test_lease_bytes_mismatch_fails(self) -> None:
        """A delivered lease differing from the bound canonical bytes fails."""
        binding, claim_bytes, *_ = self._lease_binding(("scripts",))
        other, _ = self._mint(binding, ("scripts",))
        other_bytes = lease.claim_to_bytes(other)
        self.assertNotEqual(other_bytes, claim_bytes)
        with self.assertRaises(launch.InvocationError) as caught:
            self._authorize_lease(binding, other_bytes)
        self.assertIn("differ", str(caught.exception).lower())

    def test_lease_digest_mismatch_fails(self) -> None:
        """A bound digest that does not match the claim digest fails."""
        binding, claim_bytes, *_ = self._lease_binding(("scripts",))
        binding = launch.replace(binding, lease_digest="0" * 64)
        with self.assertRaises(launch.InvocationError) as caught:
            self._authorize_lease(binding, claim_bytes)
        self.assertIn("digest", str(caught.exception).lower())

    def test_tampered_claim_fails(self) -> None:
        """A claim whose granted set is edited without a recomputed digest
        fails closed at parse (the digest alone is never authoritative)."""
        binding, claim_bytes, *_ = self._lease_binding(("scripts",))
        document = json.loads(claim_bytes.decode("utf-8"))
        document["granted_paths"] = ["src"]
        tampered = json.dumps(
            document, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        binding = launch.replace(binding, lease_bytes=tampered)
        with self.assertRaises(launch.InvocationError) as caught:
            self._authorize_lease(binding, tampered)
        self.assertIn("digest", str(caught.exception).lower())

    def test_deny_zone_claim_fails(self) -> None:
        """A forged claim that grants an immutable deny zone fails closed at
        validate (drift from the trusted deny-dominant expansion)."""
        binding, claim_bytes, *_ = self._lease_binding(("scripts",))
        document = json.loads(claim_bytes.decode("utf-8"))
        document["granted_paths"] = [".factory"]
        document["granted_patterns"] = []
        forged = lease.TaskPathLease(
            schema=document["schema"],
            campaign_id=document["campaign_id"],
            task_id=document["task_id"],
            attempt=document["attempt"],
            head_commit=document["head_commit"],
            plan_digest=document["plan_digest"],
            policy_digest=document["policy_digest"],
            requested_scopes=tuple(document["requested_scopes"]),
            granted_scopes=tuple(document["granted_scopes"]),
            granted_paths=tuple(document["granted_paths"]),
            granted_patterns=tuple(document["granted_patterns"]),
            issued_at=document["issued_at"],
            deadline=document["deadline"],
            nonce=document["nonce"],
            audit_required=document["audit_required"],
            claim_digest="",
        )
        forged = lease.TaskPathLease(
            **{**forged.__dict__, "claim_digest": lease.claim_digest(forged)}
        )
        forged_bytes = lease.claim_to_bytes(forged)
        binding = launch.replace(
            binding, lease_digest=forged.claim_digest, lease_bytes=forged_bytes
        )
        with self.assertRaises(launch.InvocationError) as caught:
            self._authorize_lease(binding, forged_bytes)
        self.assertIn("drift", str(caught.exception).lower())

    # -- role / binding shape ----------------------------------------------

    def test_lease_requires_developer_role(self) -> None:
        """A task path-lease is developer-only."""
        binding, claim_bytes, *_ = self._lease_binding(("scripts",))
        binding = launch.replace(
            binding, role="planner", task_id=None, task_excerpt_digest=None
        )
        with self.assertRaises(launch.InvocationError) as caught:
            self._authorize_lease(binding, claim_bytes)
        self.assertIn("developer", str(caught.exception).lower())

    def test_lease_requires_campaign_and_attempt(self) -> None:
        """A lease without the trusted campaign/attempt binding fails."""
        binding, claim_bytes, *_ = self._lease_binding(("scripts",))
        binding = launch.replace(binding, campaign_id="")
        with self.assertRaises(launch.InvocationError) as caught:
            self._authorize_lease(binding, claim_bytes)
        self.assertIn("campaign", str(caught.exception).lower())

    def test_audit_required_without_lease_fails(self) -> None:
        """The model can never claim audit_required without an authenticated
        lease (the signal is immutable and authority-derived)."""
        binding, _role, _agents, _spec, _plan = self.make_binding(
            role="developer"
        )
        binding = launch.replace(binding, audit_required=True)
        with self.assertRaises(launch.InvocationError) as caught:
            self._authorize_lease(binding, None)
        self.assertIn("audit_required", str(caught.exception).lower())

    # -- context / expiry --------------------------------------------------

    def test_replayed_claim_foreign_campaign_fails(self) -> None:
        """A claim minted for a different campaign is a replay/context
        mismatch and fails closed."""
        binding, _claim_bytes, *_ = self._lease_binding(("scripts",))
        foreign, _ = self._mint(binding, ("scripts",), campaign_id="other-campaign")
        foreign_bytes = lease.claim_to_bytes(foreign)
        binding = launch.replace(
            binding, lease_digest=foreign.claim_digest, lease_bytes=foreign_bytes
        )
        with self.assertRaises(launch.InvocationError) as caught:
            self._authorize_lease(binding, foreign_bytes)
        self.assertIn("campaign", str(caught.exception).lower())

    def test_expired_claim_fails(self) -> None:
        """An expired claim fails closed at authorize."""
        binding, _claim_bytes, *_ = self._lease_binding(("scripts",))
        claim, _ = self._mint(binding, ("scripts",))
        now = datetime.now(timezone.utc)
        expired = self._forge(
            claim,
            issued_at=(now - timedelta(seconds=7200)).isoformat(),
            deadline=(now - timedelta(seconds=3600)).isoformat(),
        )
        expired_bytes = lease.claim_to_bytes(expired)
        binding = launch.replace(
            binding, lease_digest=expired.claim_digest, lease_bytes=expired_bytes
        )
        with self.assertRaises(launch.InvocationError) as caught:
            self._authorize_lease(binding, expired_bytes)
        self.assertIn("expired", str(caught.exception).lower())

    def test_not_yet_issued_claim_fails(self) -> None:
        """A not-yet-issued claim fails closed at authorize."""
        binding, _claim_bytes, *_ = self._lease_binding(("scripts",))
        claim, _ = self._mint(binding, ("scripts",))
        future = self._forge(
            claim,
            issued_at=(
                datetime.now(timezone.utc) + timedelta(seconds=60)
            ).isoformat(),
        )
        future_bytes = lease.claim_to_bytes(future)
        binding = launch.replace(
            binding, lease_digest=future.claim_digest, lease_bytes=future_bytes
        )
        with self.assertRaises(launch.InvocationError) as caught:
            self._authorize_lease(binding, future_bytes)
        self.assertIn("not yet issued", str(caught.exception).lower())

    def test_expiry_before_spawn_fails_closed(self) -> None:
        """An expiry that lands between authorize and spawn fails closed
        immediately before spawn (supervisor revalidation)."""
        binding, claim_bytes, *_ = self._lease_binding(
            ("scripts",),
            deadline=datetime.now(timezone.utc) + timedelta(seconds=2),
        )
        authority = self._authorize_lease(binding, claim_bytes)
        time.sleep(2.5)
        supervisor = launch.LaunchSupervision(binding, kill_grace=0.3)
        with self.assertRaises(launch.SupervisionError) as caught:
            supervisor.run(authority)
        self.assertIn("lease", str(caught.exception).lower())
        self.assertIsNone(supervisor._child, "no child may be spawned")

    def test_worktree_policy_tamper_fails_before_spawn(self) -> None:
        """A worktree policy tamper (deny weakened after authorize) fails
        closed immediately before spawn via the policy digest."""
        binding, claim_bytes, *_ = self._lease_binding(("scripts",))
        authority = self._authorize_lease(binding, claim_bytes)
        policy = json.loads(
            (self.workspace / ".factory" / "path-lease-policy.json").read_text()
        )
        policy["deny"]["paths"] = []
        _commit_policy(self.workspace, policy)
        supervisor = launch.LaunchSupervision(binding, kill_grace=0.3)
        with self.assertRaises(launch.SupervisionError) as caught:
            supervisor.run(authority)
        self.assertIn("lease", str(caught.exception).lower())
        self.assertIsNone(supervisor._child, "no child may be spawned")

    # -- signed launch authority (HMAC/FD token) ---------------------------

    def test_lease_without_signed_launch_authority_fails(self) -> None:
        """A claim without an existing signed launch token never grants: a
        real-provider launch with a perfectly valid lease but no locked
        readiness store fails closed."""
        binding, claim_bytes, *_ = self._lease_binding(("scripts",))
        binding = launch.replace(binding, provider="ollama")
        with self.assertRaises(launch.InvocationError) as caught:
            self._authorize_lease(binding, claim_bytes)
        self.assertIn("readiness", str(caught.exception).lower())

    def test_lease_with_signed_launch_authority_works(self) -> None:
        """With the locked readiness store (HMAC/FD token) the authenticated
        lease is honored and audit_required is carried."""
        binding, claim_bytes, *_ = self._lease_binding(("ci",))
        binding = launch.replace(binding, provider="ollama")
        store = self._locked_store()
        claims = {"fixture": "lease-ollama"}
        token = store.mint(claims)
        authority = self._authorize_lease(
            binding, claim_bytes, store=store, token=token, claims=claims
        )
        self.assertTrue(authority._binding.audit_required)
        self.assertIn("lease", authority._confinement_spec)

    def test_launch_token_replay_fails(self) -> None:
        """A consumed launch token is one-use: replaying it fails closed."""
        binding, claim_bytes, *_ = self._lease_binding(("scripts",))
        binding = launch.replace(binding, provider="ollama")
        store = self._locked_store()
        claims = {"fixture": "replay"}
        token = store.mint(claims)
        self._authorize_lease(
            binding, claim_bytes, store=store, token=token, claims=claims
        )
        with self.assertRaises(launch.InvocationError) as caught:
            self._authorize_lease(
                binding, claim_bytes, store=store, token=token, claims=claims
            )
        self.assertIn("replay", str(caught.exception).lower())

    # -- audit_required result binding -------------------------------------

    def test_audit_required_result_bound(self) -> None:
        """The immutable audit_required signal is carried into the launch
        result for the Phase 2C2b scheduler."""
        binding, claim_bytes, *_ = self._lease_binding(("ci",))
        self.set_behavior("record")
        authority = self._authorize_lease(binding, claim_bytes)
        supervisor = launch.LaunchSupervision(binding, kill_grace=0.3)
        result = supervisor.run(authority)
        self.assertEqual(result.outcome, "completed")
        self.assertTrue(result.audit_required)
        self.assertTrue(result.to_dict()["audit_required"])

    def test_non_audit_lease_result_clear(self) -> None:
        """A non-security-sensitive lease leaves audit_required False."""
        binding, claim_bytes, *_ = self._lease_binding(("scripts",))
        self.set_behavior("record")
        authority = self._authorize_lease(binding, claim_bytes)
        supervisor = launch.LaunchSupervision(binding, kill_grace=0.3)
        result = supervisor.run(authority)
        self.assertEqual(result.outcome, "completed")
        self.assertFalse(result.audit_required)

    # -- symlink / hardlink / mount candidates -----------------------------

    def test_symlink_lease_candidate_fails_closed(self) -> None:
        """A lease candidate that is a symlink fails closed at specification
        construction (no-follow component walk)."""
        (self.workspace / "scripts" / "deploy.sh").unlink()
        (self.workspace / "scripts" / "deploy.sh").symlink_to("src/main.py")
        self._commit_custom_policy(EXACT_POLICY)
        binding, claim_bytes, *_ = self._lease_binding(("exact",))
        with self.assertRaises(launch.InvocationError) as caught:
            self._authorize_lease(binding, claim_bytes)
        self.assertIn("symlink", str(caught.exception).lower())

    def test_hardlink_lease_candidate_fails_closed(self) -> None:
        """A hardlinked regular-file candidate fails closed (single-link
        check, no hardlink alias risk)."""
        (self.workspace / "scripts" / "deploy.sh").unlink()
        os.link(
            self.workspace / "backend.py",
            self.workspace / "scripts" / "deploy.sh",
        )
        self._commit_custom_policy(EXACT_POLICY)
        binding, claim_bytes, *_ = self._lease_binding(("exact",))
        with self.assertRaises(launch.InvocationError) as caught:
            self._authorize_lease(binding, claim_bytes)
        self.assertIn("link", str(caught.exception).lower())

    def test_mount_escape_lease_candidate_fails_closed(self) -> None:
        """A candidate on a different device than the workspace is never
        granted.  No root is available, so a real bind mount is not
        fixture-capable; the st_dev containment check is exercised directly."""
        target = self.workspace / "scripts" / "deploy.sh"
        real_lstat = os.lstat

        def fake_lstat(path):
            info = real_lstat(path)
            if str(Path(path).absolute()) == str(target.absolute()):
                return types.SimpleNamespace(
                    st_dev=info.st_dev + 1,
                    st_mode=info.st_mode,
                    st_nlink=info.st_nlink,
                )
            return info

        with mock.patch("os.lstat", side_effect=fake_lstat):
            with self.assertRaises(wc.ConfinementError) as caught:
                wc._lease_write_path(target, self.workspace)
        self.assertIn("device", str(caught.exception).lower())

    def test_authorize_failure_cleans_private_dirs(self) -> None:
        """A failed authorization (symlink candidate) removes every per-launch
        private directory it created."""
        (self.workspace / "scripts" / "deploy.sh").unlink()
        (self.workspace / "scripts" / "deploy.sh").symlink_to("src/main.py")
        self._commit_custom_policy(EXACT_POLICY)
        binding, claim_bytes, *_ = self._lease_binding(("exact",))
        before = {
            path for prefix in ("factory-loop-exec-", "factory-loop-session-",
                                "factory-loop-home-")
            for path in Path("/tmp").glob(prefix + "*")
        }
        with self.assertRaises(launch.InvocationError):
            self._authorize_lease(binding, claim_bytes)
        after = {
            path for prefix in ("factory-loop-exec-", "factory-loop-session-",
                                "factory-loop-home-")
            for path in Path("/tmp").glob(prefix + "*")
        }
        self.assertEqual(
            after - before, set(),
            f"failed authorization left private dirs: {after - before}",
        )


# ---------------------------------------------------------------------------
# Real confined write tests (Landlock): exact grant vs sibling/deny zones
# ---------------------------------------------------------------------------

class LeaseConfinedWriteTests(CONF_SUITE._Base):
    """Real Landlock enforcement of the authenticated lease write grants."""

    @classmethod
    def setUpClass(cls) -> None:
        if not wc.confinement_primitive_available():
            raise unittest.SkipTest(
                "the Landlock LSM is unavailable on this host; the confined "
                "lease write matrix cannot run (fail closed, never simulated)"
            )

    def setUp(self) -> None:
        super().setUp()
        # The launch authority stages the exact committed path-lease module
        # beside the confine launcher (F2); the fixture must commit it.
        shutil.copy2(
            LOOP / "path_lease.py",
            self.workspace / ".factory" / "loop" / "path_lease.py",
        )
        _commit_policy(self.workspace, FIXTURE_POLICY)
        (self.workspace / "scripts").mkdir(exist_ok=True)
        (self.workspace / "scripts" / "deploy.sh").write_text(
            "#!/bin/sh\necho deploy\n", encoding="utf-8"
        )
        os.chmod(self.workspace / "scripts" / "deploy.sh", 0o700)
        (self.workspace / ".github" / "workflows").mkdir(parents=True)
        (self.workspace / ".github" / "workflows" / "ci.yml").write_text(
            "name: ci\n", encoding="utf-8"
        )
        # Deny-zone surfaces must exist so confined writes fail with a real
        # Landlock denial (PermissionError), never a vacuous ENOENT.
        for rel in ("docs", "secrets", "release", "goldens", "goldens/approved"):
            (self.workspace / rel).mkdir(parents=True, exist_ok=True)
        (self.workspace / "docs" / "SPEC.md").write_text("spec\n", encoding="utf-8")
        (self.workspace / "secrets" / "token").write_text("x\n", encoding="utf-8")
        (self.workspace / "release" / "artifact").write_text("x\n", encoding="utf-8")
        (self.workspace / "goldens" / "approved" / "x").write_text(
            "x\n", encoding="utf-8"
        )
        (self.workspace / ".env").write_text("x\n", encoding="utf-8")
        CONF_SUITE._git("add", "-A", cwd=self.workspace)
        CONF_SUITE._git("commit", "-qm", "lease fixture", cwd=self.workspace)
        self.head = CONF_SUITE._git(
            "rev-parse", "HEAD", cwd=self.workspace
        ).stdout.strip()

    def _conf_lease(
        self, scopes=("scripts",), *, campaign_id="campaign-1", attempt=1,
        **mint_kwargs,
    ):
        plan = (self.workspace / "plan.md").read_bytes()
        policy = lease.load_policy_config(self.workspace)
        claim = lease.mint_claim(
            campaign_id=campaign_id, task_id=1, attempt=attempt,
            head_commit=self.head,
            plan_digest=sha256(plan),
            policy_digest=lease.policy_digest(policy),
            requested_scopes=scopes, policy=policy, **mint_kwargs,
        )
        claim_bytes = lease.claim_to_bytes(claim)
        binding = launch.InvocationBinding(
            role="developer", model="synthetic-model", provider="synthetic",
            backend=self.workspace / "backend.py", workspace=self.workspace,
            bound_commit=self.head,
            role_prompt_digest=sha256(
                (self.workspace / "role.md").read_bytes()
            ),
            prompt_set_digest=sha256(b"campaign-set"),
            plan_digest=sha256(plan),
            policy_digest=sha256((self.workspace / "AGENTS.md").read_bytes()),
            specification_digest=sha256(
                (self.workspace / "spec.md").read_bytes()
            ),
            task_id=1,
            task_excerpt_digest=sha256(launch.task_excerpt_bytes(plan, 1)),
            campaign_id=campaign_id, attempt=attempt,
            lease_digest=claim.claim_digest, lease_bytes=claim_bytes,
        )
        return binding, claim_bytes

    def _conf_authorize(self, binding, claim_bytes):
        return launch.authorize_launch(
            binding,
            role_prompt=(self.workspace / "role.md").read_bytes(),
            agents=(self.workspace / "AGENTS.md").read_bytes(),
            spec=(self.workspace / "spec.md").read_bytes(),
            plan=(self.workspace / "plan.md").read_bytes(),
            task_excerpt=launch.task_excerpt_bytes(
                (self.workspace / "plan.md").read_bytes(), 1
            ),
            lease=claim_bytes,
        )

    def _write_targets(self, ws: str) -> list:
        return [
            {"op": "write", "path": f"{ws}/.github/workflows/new.yml",
             "label": "lease-dir"},
            {"op": "write", "path": f"{ws}/.factory/loop/launch.py",
             "label": "deny-factory"},
            {"op": "write", "path": f"{ws}/.git/HEAD", "label": "deny-git"},
            {"op": "write", "path": f"{ws}/.env", "label": "deny-env"},
            {"op": "write", "path": f"{ws}/.factory-state/factory-loop.json",
             "label": "deny-state"},
            {"op": "write", "path": f"{ws}/.ralph/secret.txt",
             "label": "deny-ralph"},
        ]

    def test_confined_lease_directory_write_and_deny_zones(self) -> None:
        """The authenticated lease grants WRITE to the exact policy-expanded
        directory (``.github`` is trusted-policy surface, denied by the
        default developer authority, so the grant is a real new capability);
        the confined child can create files there while every hidden deny
        zone stays denied (with an unconfined control proving the denial is
        not vacuous).  The lease never adds a WRITE rule for any immutable
        deny zone."""
        binding, claim_bytes = self._conf_lease(("ci",))
        authority = self._conf_authorize(binding, claim_bytes)
        spec = authority._confinement_spec
        ws = str(self.workspace)
        targets = self._write_targets(ws)
        result = self.run_confined("developer", targets, spec=spec)
        self.assertEqual(
            result.get("write:lease-dir"), "ok",
            "the exact granted directory must be writable",
        )
        for key in ("deny-factory", "deny-git", "deny-env", "deny-state",
                    "deny-ralph"):
            self.assertEqual(
                result.get(f"write:{key}"), "PermissionError",
                f"write {key} under confinement: expected PermissionError, "
                f"got {result.get(f'write:{key}')!r}",
            )
        free = self.run_unconfined(targets)
        for key in ("lease-dir", "deny-factory", "deny-git", "deny-env",
                    "deny-state", "deny-ralph"):
            self.assertEqual(
                free.get(f"write:{key}"), "ok",
                f"unconfined control {key} must be writable (non-vacuous)",
            )
        # The lease never grants an immutable deny zone: no WRITE rule names
        # a deny path or a path beneath one.
        deny_paths = tuple(
            lease.load_policy_config(self.workspace).deny_paths
        )
        workspace_abs = Path(self.workspace).absolute()
        for rule in spec["rules"]:
            # The lease rules are exactly WRITE-only; the default developer
            # authority carries read+write and is unchanged by the lease.
            if rule["access"] != ["write"]:
                continue
            rule_abs = Path(str(rule["path"])).absolute()
            try:
                rel = rule_abs.relative_to(workspace_abs)
            except ValueError:
                continue  # private per-launch home/scratch rules are not
                # workspace-relative and cannot be a deny zone
            rel_text = str(rel)
            self.assertFalse(
                any(
                    rel_text == deny or rel_text.startswith(deny + "/")
                    for deny in deny_paths
                ),
                f"a lease WRITE rule names the immutable deny zone {rel_text}",
            )

    def test_confined_lease_exact_file_sibling_denied(self) -> None:
        """An exact-file lease grants exactly one file under a default-denied
        namespace: the confined child writes it while every sibling stays
        denied."""
        exact_policy = {
            "schema": "factory-path-lease-policy/v1",
            "scopes": {
                "exact": {
                    "paths": [".github/workflows/ci.yml"],
                    "patterns": [],
                    "audit_required": False,
                },
            },
            "deny": {
                "paths": [
                    ".factory", ".factory-state", ".git", "docs/SPEC.md",
                    ".env", "secrets", "release", "goldens",
                    "goldens/approved",
                ],
                "patterns": [
                    "**/*.pem", "*.pem", "**/*.key", "*.key",
                    "**/.env*", ".env*", "**/*.secret", "*.secret",
                ],
            },
        }
        _commit_policy(self.workspace, exact_policy)
        CONF_SUITE._git("add", "-A", cwd=self.workspace)
        CONF_SUITE._git("commit", "-qm", "exact policy", cwd=self.workspace)
        self.head = CONF_SUITE._git(
            "rev-parse", "HEAD", cwd=self.workspace
        ).stdout.strip()
        binding, claim_bytes = self._conf_lease(("exact",))
        authority = self._conf_authorize(binding, claim_bytes)
        spec = authority._confinement_spec
        ws = str(self.workspace)
        targets = [
            {"op": "write", "path": f"{ws}/.github/workflows/ci.yml",
             "label": "exact"},
            {"op": "write", "path": f"{ws}/.github/workflows/other.yml",
             "label": "sibling"},
        ]
        result = self.run_confined("developer", targets, spec=spec)
        self.assertEqual(result.get("write:exact"), "ok")
        self.assertEqual(
            result.get("write:sibling"), "PermissionError",
            "the exact-file lease must not grant the sibling",
        )
        free = self.run_unconfined(targets)
        self.assertEqual(free.get("write:exact"), "ok")
        self.assertEqual(free.get("write:sibling"), "ok")

    def test_no_lease_confined_write_denied(self) -> None:
        """Without a lease the default confinement is unchanged: the
        trusted-policy CI surface is not writable."""
        binding = self.binding(role="developer")
        home = wc.sanitized_home_directory()
        self.addCleanup(shutil.rmtree, home, ignore_errors=True)
        spec = wc.confinement_spec(binding, sanitized_home=home)
        wc.validate_confinement_spec(spec, binding)
        ws = str(self.workspace)
        targets = [
            {"op": "write", "path": f"{ws}/.github/workflows/new.yml",
             "label": "no-lease"},
        ]
        result = self.run_confined("developer", targets, spec=spec)
        self.assertEqual(
            result.get("write:no-lease"), "PermissionError",
            "without a lease the trusted-policy CI surface must stay "
            "unwritable",
        )
        free = self.run_unconfined(targets)
        self.assertEqual(free.get("write:no-lease"), "ok")

    def test_expired_claim_fails_inside_child(self) -> None:
        """The confined launcher re-validates the lease inside the child: an
        expired claim fails closed before any Landlock rule is applied."""
        binding, claim_bytes = self._conf_lease(("scripts",))
        authority = self._conf_authorize(binding, claim_bytes)
        plan = (self.workspace / "plan.md").read_bytes()
        policy = lease.load_policy_config(self.workspace)
        valid = lease.mint_claim(
            campaign_id="campaign-1", task_id=1, attempt=1,
            head_commit=self.head,
            plan_digest=sha256(plan),
            policy_digest=lease.policy_digest(policy),
            requested_scopes=["scripts"], policy=policy,
        )
        expired = lease.TaskPathLease(
            **{
                **valid.__dict__,
                "deadline": (
                    datetime.now(timezone.utc) - timedelta(seconds=1)
                ).isoformat(),
            }
        )
        expired = lease.TaskPathLease(
            **{**expired.__dict__, "claim_digest": lease.claim_digest(expired)}
        )
        spec = dict(authority._confinement_spec)
        spec["lease"] = {
            "claim": base64.b64encode(
                lease.claim_to_bytes(expired)
            ).decode("ascii"),
            "context": authority._confinement_spec["lease"]["context"],
        }
        spec_path = self.diag / "spec.json"
        spec_path.write_text(
            json.dumps(spec, sort_keys=True, separators=(",", ":")),
            encoding="utf-8",
        )
        proc = self._run_confine_launcher(
            spec, spec_path, [PY, str(self.workspace / "probe.py"), "[]"]
        )
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn(b"fails closed", proc.stderr)

    def test_worktree_policy_tamper_fails_inside_child(self) -> None:
        """The confined launcher re-loads the committed policy: a worktree
        tamper (deny weakened) fails closed inside the child via the policy
        digest."""
        binding, claim_bytes = self._conf_lease(("scripts",))
        authority = self._conf_authorize(binding, claim_bytes)
        policy = json.loads(
            (self.workspace / ".factory" / "path-lease-policy.json").read_text()
        )
        policy["deny"]["paths"] = []
        _commit_policy(self.workspace, policy)
        spec = authority._confinement_spec
        spec_path = self.diag / "spec.json"
        spec_path.write_text(
            json.dumps(spec, sort_keys=True, separators=(",", ":")),
            encoding="utf-8",
        )
        proc = self._run_confine_launcher(
            spec, spec_path, [PY, str(self.workspace / "probe.py"), "[]"]
        )
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn(b"fails closed", proc.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2)
