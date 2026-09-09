#!/usr/bin/env python3
"""Harness-owned conformance tests for the Phase 2C1 path-lease authority.

This test lives under the hidden `.factory/tests/` namespace because the
specification (HIDE-01, §3) keeps harness-only tests out of the adopting
product's visible test tree. It is the deterministic verification for Task 32
(Phase 2C1 generic lease foundation, LEASE-01):

* the committed `factory-path-lease-policy/v1` config loads, its canonical
  digest is deterministic, and every documented defect class is rejected
  (absolute/traversal/backslash/control/unsafe-glob/symlink-ambiguous
  patterns, duplicate keys, overlapping deny escapes, unknown fields);
* deny zones are absolute and non-overridable — no carve-out field exists in
  the schema, and goldens/approval/release/human-authority paths are never
  grantable;
* deny-dominant expansion grants exactly the trusted intersection and fails
  closed on unknown/duplicate/malformed scopes and on scopes that grant
  nothing;
* `factory-task-path-lease/v1` claims bind campaign/task/attempt/HEAD/plan
  digest/policy digest/scopes/expanded paths/issued-deadline/nonce and are
  sealed by a canonical digest; parse/validate/context validation fail closed
  on forgery, drift, replay, and expiry;
* security-sensitive scopes mark `audit_required`;
* the no-follow bounded policy load rejects symlinks and oversized documents.
"""

from __future__ import annotations

import json
import os
import stat
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
LOOP = ROOT / ".factory" / "loop"
POLICY_FILE = ROOT / ".factory" / "path-lease-policy.json"

sys.path.insert(0, str(LOOP))
import path_lease as pl  # noqa: E402

NOW = datetime(2026, 7, 14, 12, 0, 0, tzinfo=timezone.utc)


def _base_bindings(policy: pl.PathLeasePolicy) -> dict:
    return {
        "campaign_id": "camp-01",
        "task_id": 32,
        "attempt": 1,
        "head_commit": "a" * 40,
        "plan_digest": "b" * 64,
        "policy_digest": pl.policy_digest(policy),
    }


def _mint(policy: pl.PathLeasePolicy, scopes, **overrides) -> pl.TaskPathLease:
    kwargs = {**_base_bindings(policy), "requested_scopes": scopes,
              "policy": policy, "now": NOW, "nonce": "c" * 64}
    kwargs.update(overrides)
    return pl.mint_claim(**kwargs)


def _policy_document(**overrides) -> dict:
    """A minimal valid policy document; overrides replace whole sections."""
    document = {
        "schema": "factory-path-lease-policy/v1",
        "scopes": {
            "scripts": {
                "paths": ["scripts"],
                "patterns": ["scripts/*.sh"],
                "audit_required": False,
            },
        },
        "deny": {
            "paths": [".factory", "goldens"],
            "patterns": ["**/*.pem"],
        },
    }
    document.update(overrides)
    return document


def _parse_document(document: dict) -> pl.PathLeasePolicy:
    return pl.parse_policy(json.dumps(document).encode("utf-8"))


class CommittedPolicyTest(unittest.TestCase):
    """The committed policy loads, is deterministic, and is non-overridable."""

    def test_committed_policy_loads(self) -> None:
        policy = pl.load_policy_config(ROOT)
        self.assertEqual(
            {scope.scope_id for scope in policy.scopes},
            {"scripts", "nix", "packaging", "ci"},
        )
        self.assertIn(".factory", policy.deny_paths)
        self.assertIn(".factory-state", policy.deny_paths)
        self.assertIn(".git", policy.deny_paths)
        self.assertIn("docs/SPEC.md", policy.deny_paths)
        self.assertIn(".env", policy.deny_paths)
        self.assertIn("secrets", policy.deny_paths)
        self.assertIn("release", policy.deny_paths)
        self.assertIn("goldens", policy.deny_paths)
        self.assertIn("goldens/approved", policy.deny_paths)

    def test_no_goldens_scope_is_grantable(self) -> None:
        policy = pl.load_policy_config(ROOT)
        self.assertNotIn("goldens", {s.scope_id for s in policy.scopes})
        with self.assertRaises(pl.PathLeaseClaimError) as caught:
            pl.expand_request(["goldens"], policy)
        self.assertIn("unknown write scopes", str(caught.exception))

    def test_no_override_field_exists(self) -> None:
        # The schema is closed: a `deny_overrides` field is an unknown field
        # and fails closed.  Deny zones are absolute and non-overridable.
        document = _policy_document(
            deny_overrides={"paths": ["goldens/approved"], "patterns": []}
        )
        with self.assertRaises(pl.PathLeasePolicyError) as caught:
            _parse_document(document)
        self.assertIn("exactly the documented field set", str(caught.exception))

    def test_policy_digest_is_deterministic(self) -> None:
        policy = pl.load_policy_config(ROOT)
        self.assertEqual(pl.policy_digest(policy), pl.policy_digest(policy))
        self.assertEqual(len(pl.policy_digest(policy)), 64)
        # Canonical bytes are stable and key-sorted.
        canonical = pl.canonical_policy_bytes(policy)
        self.assertEqual(
            canonical,
            json.dumps(
                json.loads(canonical.decode("utf-8")),
                sort_keys=True, separators=(",", ":"),
            ).encode("utf-8"),
        )

    def test_committed_policy_matches_its_schema(self) -> None:
        # The committed policy document is well-formed JSON, declares the
        # exact schema identity, and is accepted by the module authority.
        # (The module's closed-field parser is the authoritative validator;
        # the JSON Schema file is the human/audit mirror of the same closed
        # grammar.)
        schema = json.loads(
            (ROOT / ".factory" / "schemas"
             / "factory-path-lease-policy-v1.schema.json").read_text("utf-8")
        )
        self.assertEqual(schema["$id"], pl.SCHEMA_NAME)
        self.assertEqual(
            set(schema["required"]), {"schema", "scopes", "deny"}
        )
        self.assertNotIn("deny_overrides", schema["properties"])
        document = json.loads(POLICY_FILE.read_text("utf-8"))
        self.assertEqual(document["schema"], pl.SCHEMA_NAME)
        pl.parse_policy(POLICY_FILE.read_bytes())


class PolicyDefectRejectionTest(unittest.TestCase):
    """Every documented policy defect class fails closed at load."""

    def _rejects(self, document: dict, fragment: str) -> None:
        with self.assertRaises(pl.PathLeasePolicyError) as caught:
            _parse_document(document)
        self.assertIn(fragment, str(caught.exception))

    def test_wrong_schema(self) -> None:
        self._rejects(_policy_document(schema="factory-path-lease-policy/v2"),
                      "schema must be exactly")

    def test_unknown_top_field(self) -> None:
        self._rejects(_policy_document(extra_field=1), "exactly the documented")

    def test_missing_deny(self) -> None:
        document = _policy_document()
        del document["deny"]
        self._rejects(document, "missing")

    def test_duplicate_key(self) -> None:
        raw = json.dumps(_policy_document()).replace(
            '"schema": "factory-path-lease-policy/v1"',
            '"schema": "factory-path-lease-policy/v1", "schema": "x"',
        )
        with self.assertRaises(pl.PathLeasePolicyError) as caught:
            pl.parse_policy(raw.encode("utf-8"))
        self.assertIn("duplicate JSON object key", str(caught.exception))

    def test_unsafe_paths(self) -> None:
        for bad in ("/etc/passwd", "a/../b", "a/./b", "a//b", "a\\b",
                    "a b", "a\tb", "a\x00b", "a/*", "a?b", "a[b]"):
            with self.subTest(path=bad):
                self._rejects(
                    _policy_document(scopes={"s": {"paths": [bad],
                                                  "patterns": [], "audit_required": False}}),
                    "not a safe repository-relative",
                )

    def test_unsafe_grant_patterns(self) -> None:
        for bad in ("**", "a/**", "**/a", "a/{b,c}", "a/!b", "a/^b",
                    "a/$b", "a/`b", "a/;b", "a/ b", "/a/*", "a/../*"):
            with self.subTest(pattern=bad):
                self._rejects(
                    _policy_document(scopes={"s": {"paths": [], "patterns": [bad],
                                                  "audit_required": False}}),
                    "not a safe bounded glob",
                )

    def test_deny_patterns_may_use_doublestar(self) -> None:
        policy = _parse_document(_policy_document(
            deny={"paths": [".factory"], "patterns": ["**/*.pem", "**/.env*"]}
        ))
        self.assertIn("**/*.pem", policy.deny_patterns)

    def test_root_level_credential_suffixes_are_denied(self) -> None:
        # Root-level credential suffix files must be denied too: the
        # ``**/``-prefixed deny patterns alone would not match a root-level
        # file, so the committed policy carries explicit root patterns.
        policy = pl.load_policy_config(ROOT)
        for path in ("foo.pem", "secret.key", ".env", ".env.local", "a.secret"):
            with self.subTest(path=path):
                self.assertTrue(pl._path_in_deny(path, policy), path)
        # A scope whose only grant is a root-level credential path grants
        # nothing after deny-dominant expansion (root patterns included).
        root_patterns = ["**/*.pem", "*.pem", "**/*.key", "*.key",
                         "**/.env*", ".env*", "**/*.secret", "*.secret"]
        for path in ("foo.pem", "secret.key", ".env", "a.secret"):
            with self.subTest(expand=path):
                with self.assertRaises(pl.PathLeaseClaimError) as caught:
                    pl.expand_request(["s"], _parse_document(_policy_document(
                        scopes={"s": {"paths": [path], "patterns": [],
                                      "audit_required": False}},
                        deny={"paths": [".factory"], "patterns": root_patterns},
                    )))
                self.assertIn("grants nothing", str(caught.exception))

    def test_duplicate_paths_and_patterns(self) -> None:
        self._rejects(
            _policy_document(scopes={"s": {"paths": ["a", "a"], "patterns": [],
                                           "audit_required": False}}),
            "duplicate path",
        )
        self._rejects(
            _policy_document(scopes={"s": {"paths": [], "patterns": ["a/*", "a/*"],
                                           "audit_required": False}}),
            "duplicate pattern",
        )

    def test_invalid_scope_id(self) -> None:
        for bad in ("Scripts", "1scripts", "scripts/", "scripts..", "a b"):
            with self.subTest(scope=bad):
                self._rejects(
                    _policy_document(scopes={bad: {"paths": [], "patterns": [],
                                                   "audit_required": False}}),
                    "must match",
                )

    def test_scope_path_containing_deny_path(self) -> None:
        # A scope path that contains a deny path grants the deny zone itself.
        self._rejects(
            _policy_document(scopes={"s": {"paths": ["a"], "patterns": [],
                                           "audit_required": False}},
                             deny={"paths": ["a/b"], "patterns": []}),
            "overlapping deny escape",
        )

    def test_scope_pattern_reaching_deny_path(self) -> None:
        self._rejects(
            _policy_document(scopes={"s": {"paths": [], "patterns": ["a/*"],
                                           "audit_required": False}},
                             deny={"paths": ["a"], "patterns": []}),
            "overlapping deny escape",
        )

    def test_scope_pattern_matching_prefix_of_deeper_deny_path(self) -> None:
        # M1: a grant pattern that can match a prefix of a deeper immutable
        # deny path is an overlapping deny escape — a write to that prefix
        # could create the deeper deny path inside it.
        self._rejects(
            _policy_document(scopes={"s": {"paths": [], "patterns": ["scripts/*"],
                                           "audit_required": False}},
                             deny={"paths": ["scripts/secret/deep"], "patterns": []}),
            "overlapping deny escape",
        )

    def test_pattern_reaches_deeper_deny_path_directly(self) -> None:
        # Direct unit check of the M1 predicate: a shorter grant pattern
        # that matches a prefix of a deeper deny path reaches the deny zone.
        self.assertTrue(pl._pattern_reaches_deny_path(
            "scripts/*", "scripts/secret/deep"))
        self.assertTrue(pl._pattern_reaches_deny_path(
            "scripts/*", "scripts/secret"))
        self.assertFalse(pl._pattern_reaches_deny_path(
            "scripts/*.sh", "scripts/secret/deep"))

    def test_scope_pattern_overlapping_deny_pattern(self) -> None:
        # A grant pattern that overlaps a deny pattern is not a load-time
        # escape (deny patterns are matched at request time); deny-dominant
        # expansion drops the pattern, and a scope that then grants nothing
        # fails closed.
        policy = _parse_document(_policy_document(
            scopes={"s": {"paths": [], "patterns": ["a/*.pem"],
                          "audit_required": False}}
        ))
        with self.assertRaises(pl.PathLeaseClaimError) as caught:
            pl.expand_request(["s"], policy)
        self.assertIn("grants nothing", str(caught.exception))

    def test_scope_path_inside_deny_zone_is_not_an_escape(self) -> None:
        # A scope path inside a deny zone is not an escape: deny-dominant
        # expansion removes it at request time (and the scope then grants
        # nothing, which fails closed).
        policy = _parse_document(_policy_document(
            scopes={"s": {"paths": ["goldens/approved"], "patterns": [],
                          "audit_required": False}}
        ))
        with self.assertRaises(pl.PathLeaseClaimError) as caught:
            pl.expand_request(["s"], policy)
        self.assertIn("grants nothing", str(caught.exception))

    def test_scope_path_equals_deny_path(self) -> None:
        policy = _parse_document(_policy_document(
            scopes={"s": {"paths": ["goldens"], "patterns": [],
                          "audit_required": False}}
        ))
        with self.assertRaises(pl.PathLeaseClaimError) as caught:
            pl.expand_request(["s"], policy)
        self.assertIn("grants nothing", str(caught.exception))

    def test_audit_required_must_be_boolean(self) -> None:
        self._rejects(
            _policy_document(scopes={"s": {"paths": [], "patterns": [],
                                           "audit_required": "yes"}}),
            "must be a boolean",
        )


class ExpansionTest(unittest.TestCase):
    """Deny-dominant expansion grants exactly the trusted intersection."""

    def setUp(self) -> None:
        self.policy = pl.load_policy_config(ROOT)

    def test_scripts_and_nix(self) -> None:
        scopes, paths, patterns, audit = pl.expand_request(
            ["scripts", "nix"], self.policy
        )
        self.assertEqual(scopes, ["scripts", "nix"])
        self.assertEqual(paths, ["flake.nix", "nix", "scripts", "shell.nix"])
        self.assertEqual(patterns, ["nix/*.nix", "scripts/*.py", "scripts/*.sh"])
        self.assertFalse(audit)

    def test_ci_marks_audit_required(self) -> None:
        scopes, paths, patterns, audit = pl.expand_request(["ci"], self.policy)
        self.assertEqual(scopes, ["ci"])
        self.assertTrue(audit)

    def test_unknown_scope_fails_closed(self) -> None:
        with self.assertRaises(pl.PathLeaseClaimError) as caught:
            pl.expand_request(["scripts", "bogus"], self.policy)
        self.assertIn("unknown write scopes", str(caught.exception))

    def test_duplicate_scope_fails_closed(self) -> None:
        with self.assertRaises(pl.PathLeaseClaimError) as caught:
            pl.expand_request(["scripts", "scripts"], self.policy)
        self.assertIn("duplicate requested scope", str(caught.exception))

    def test_malformed_scope_fails_closed(self) -> None:
        for bad in (".factory", "Scripts", "a/b", "a b"):
            with self.subTest(scope=bad):
                with self.assertRaises(pl.PathLeaseClaimError):
                    pl.expand_request([bad], self.policy)

    def test_deny_dominant_removes_deny_zone_paths(self) -> None:
        # A scope whose paths are all inside a deny zone grants nothing.
        policy = _parse_document(_policy_document(
            scopes={"s": {"paths": ["goldens/approved"], "patterns": [],
                          "audit_required": False}}
        ))
        with self.assertRaises(pl.PathLeaseClaimError) as caught:
            pl.expand_request(["s"], policy)
        self.assertIn("grants nothing", str(caught.exception))

    def test_deny_pattern_removes_grant_path(self) -> None:
        policy = _parse_document(_policy_document(
            scopes={"s": {"paths": ["a"], "patterns": [], "audit_required": False}},
            deny={"paths": [".factory"], "patterns": ["a/*.pem"]},
        ))
        scopes, paths, patterns, audit = pl.expand_request(["s"], policy)
        self.assertEqual(paths, ["a"])
        self.assertEqual(patterns, [])


class ClaimMintParseTest(unittest.TestCase):
    """Claims bind the exact context and are sealed by a canonical digest."""

    def setUp(self) -> None:
        self.policy = pl.load_policy_config(ROOT)
        self.bindings = _base_bindings(self.policy)

    def test_mint_binds_everything(self) -> None:
        claim = _mint(self.policy, ["scripts", "nix"])
        self.assertEqual(claim.schema, pl.CLAIM_SCHEMA_NAME)
        self.assertEqual(claim.campaign_id, "camp-01")
        self.assertEqual(claim.task_id, 32)
        self.assertEqual(claim.attempt, 1)
        self.assertEqual(claim.head_commit, "a" * 40)
        self.assertEqual(claim.plan_digest, "b" * 64)
        self.assertEqual(claim.policy_digest, pl.policy_digest(self.policy))
        self.assertEqual(claim.requested_scopes, ("scripts", "nix"))
        self.assertEqual(claim.granted_scopes, ("scripts", "nix"))
        self.assertEqual(claim.issued_at, NOW.isoformat())
        self.assertEqual(
            claim.deadline,
            (NOW + timedelta(seconds=pl.DEFAULT_LEASE_SECONDS)).isoformat(),
        )
        self.assertEqual(claim.nonce, "c" * 64)
        self.assertFalse(claim.audit_required)
        self.assertEqual(len(claim.claim_digest), 64)

    def test_audit_required_for_sensitive_scope(self) -> None:
        claim = _mint(self.policy, ["ci"])
        self.assertTrue(claim.audit_required)

    def test_nonce_is_unique_per_attempt(self) -> None:
        first = _mint(self.policy, ["scripts"], nonce=None)
        second = _mint(self.policy, ["scripts"], nonce=None)
        self.assertNotEqual(first.nonce, second.nonce)
        self.assertNotEqual(first.claim_digest, second.claim_digest)

    def test_roundtrip_parse(self) -> None:
        claim = _mint(self.policy, ["scripts"])
        raw = pl.claim_to_bytes(claim)
        parsed = pl.parse_claim(raw)
        self.assertEqual(parsed, claim)
        self.assertEqual(pl.claim_to_bytes(parsed), raw)

    def test_canonical_bytes_are_deterministic(self) -> None:
        claim = _mint(self.policy, ["scripts"])
        self.assertEqual(pl.canonical_claim_bytes(claim),
                         pl.canonical_claim_bytes(claim))
        self.assertEqual(pl.claim_digest(claim), claim.claim_digest)

    def test_policy_digest_mismatch_fails_closed(self) -> None:
        with self.assertRaises(pl.PathLeaseClaimError) as caught:
            _mint(self.policy, ["scripts"], policy_digest="d" * 64)
        self.assertIn("does not match the committed path-lease policy",
                      str(caught.exception))

    def test_bad_bindings_fail_closed(self) -> None:
        for key, bad in (("campaign_id", "bad/campaign"), ("task_id", 0),
                         ("attempt", 0), ("head_commit", "xyz"),
                         ("plan_digest", "short"), ("policy_digest", "short")):
            with self.subTest(key=key):
                kwargs = dict(self.bindings)
                kwargs[key] = bad
                with self.assertRaises(pl.PathLeaseClaimError):
                    pl.mint_claim(**kwargs, requested_scopes=["scripts"],
                                  policy=self.policy, now=NOW, nonce="c" * 64)

    def test_lease_duration_bounds(self) -> None:
        with self.assertRaises(pl.PathLeaseClaimError):
            _mint(self.policy, ["scripts"], deadline=NOW)
        with self.assertRaises(pl.PathLeaseClaimError):
            _mint(self.policy, ["scripts"],
                  deadline=NOW + timedelta(seconds=pl.MAX_LEASE_SECONDS + 1))

    def test_naive_datetime_rejected_at_mint(self) -> None:
        # A naive (tz-naive) issued/deadline would serialize to a non-UTC
        # timestamp and be rejected later; reject it at mint so no claim is
        # ever minted with an ambiguous bound.
        naive = datetime(2026, 7, 14, 12, 0, 0)
        with self.assertRaises(pl.PathLeaseClaimError) as caught:
            _mint(self.policy, ["scripts"], issued_at=naive)
        self.assertIn("aware UTC", str(caught.exception))
        with self.assertRaises(pl.PathLeaseClaimError):
            _mint(self.policy, ["scripts"], deadline=naive)

    def test_bad_nonce_fails_closed(self) -> None:
        with self.assertRaises(pl.PathLeaseClaimError):
            _mint(self.policy, ["scripts"], nonce="short")


class ClaimParseAdversarialTest(unittest.TestCase):
    """Malformed, forged, and drifted claims fail closed at parse time."""

    def setUp(self) -> None:
        self.policy = pl.load_policy_config(ROOT)
        self.claim = _mint(self.policy, ["scripts"])
        self.raw = pl.claim_to_bytes(self.claim)

    def _mutate(self, **changes) -> bytes:
        document = json.loads(self.raw.decode("utf-8"))
        document.update(changes)
        return json.dumps(document).encode("utf-8")

    def test_forged_digest(self) -> None:
        with self.assertRaises(pl.PathLeaseClaimError) as caught:
            pl.parse_claim(self._mutate(claim_digest="f" * 64))
        self.assertIn("does not match the canonical claim bytes",
                      str(caught.exception))

    def test_duplicate_key(self) -> None:
        raw = self.raw.decode("utf-8").replace(
            '"schema":"factory-task-path-lease/v1"',
            '"schema":"factory-task-path-lease/v1","schema":"x"',
        )
        with self.assertRaises(pl.PathLeaseClaimError) as caught:
            pl.parse_claim(raw.encode("utf-8"))
        self.assertIn("duplicate JSON object key", str(caught.exception))

    def test_extra_and_missing_fields(self) -> None:
        with self.assertRaises(pl.PathLeaseClaimError):
            pl.parse_claim(self._mutate(extra_field=1))
        document = json.loads(self.raw.decode("utf-8"))
        del document["nonce"]
        with self.assertRaises(pl.PathLeaseClaimError):
            pl.parse_claim(json.dumps(document).encode("utf-8"))

    def test_wrong_schema(self) -> None:
        with self.assertRaises(pl.PathLeaseClaimError):
            pl.parse_claim(self._mutate(schema="factory-task-path-lease/v2"))

    def test_granted_not_subset_of_requested(self) -> None:
        with self.assertRaises(pl.PathLeaseClaimError) as caught:
            pl.parse_claim(self._mutate(granted_scopes=["ci"]))
        self.assertIn("not requested", str(caught.exception))

    def test_duplicate_scopes(self) -> None:
        with self.assertRaises(pl.PathLeaseClaimError):
            pl.parse_claim(self._mutate(requested_scopes=["scripts", "scripts"]))
        with self.assertRaises(pl.PathLeaseClaimError):
            pl.parse_claim(self._mutate(granted_scopes=["scripts", "scripts"]))

    def test_unsafe_granted_paths_and_patterns(self) -> None:
        with self.assertRaises(pl.PathLeaseClaimError):
            pl.parse_claim(self._mutate(granted_paths=["/etc/passwd"]))
        with self.assertRaises(pl.PathLeaseClaimError):
            pl.parse_claim(self._mutate(granted_patterns=["**/*"]))

    def test_deadline_not_after_issued(self) -> None:
        with self.assertRaises(pl.PathLeaseClaimError):
            pl.parse_claim(self._mutate(deadline=self.claim.issued_at))

    def test_oversized_lease(self) -> None:
        with self.assertRaises(pl.PathLeaseClaimError):
            pl.parse_claim(self._mutate(
                deadline=(NOW + timedelta(seconds=pl.MAX_LEASE_SECONDS + 1)).isoformat()
            ))

    def test_bad_nonce_and_audit_flag(self) -> None:
        with self.assertRaises(pl.PathLeaseClaimError):
            pl.parse_claim(self._mutate(nonce="short"))
        with self.assertRaises(pl.PathLeaseClaimError):
            pl.parse_claim(self._mutate(audit_required="yes"))

    def test_oversized_arrays_fail_closed(self) -> None:
        # parse_claim enforces the same array count bounds as the policy
        # loader so an attacker-sized claim cannot carry unbounded arrays.
        with self.assertRaises(pl.PathLeaseClaimError) as caught:
            pl.parse_claim(self._mutate(
                granted_paths=["a"] * (pl.MAX_PATHS + 1)))
        self.assertIn("at most", str(caught.exception))
        with self.assertRaises(pl.PathLeaseClaimError):
            pl.parse_claim(self._mutate(
                granted_patterns=["a/*"] * (pl.MAX_PATTERNS + 1)))
        with self.assertRaises(pl.PathLeaseClaimError):
            pl.parse_claim(self._mutate(
                requested_scopes=["scripts"] * (pl.MAX_SCOPES + 1)))


class ClaimValidationTest(unittest.TestCase):
    """validate_claim and validate_claim_context fail closed on drift."""

    def setUp(self) -> None:
        self.policy = pl.load_policy_config(ROOT)
        self.bindings = _base_bindings(self.policy)
        self.claim = _mint(self.policy, ["scripts", "nix"])

    def test_valid_claim_passes(self) -> None:
        pl.validate_claim(self.claim, self.policy)
        pl.validate_claim_context(self.claim, now=NOW, **self.bindings)

    def test_policy_drift(self) -> None:
        other = _parse_document(_policy_document(
            scopes={"scripts": {"paths": ["scripts"], "patterns": ["scripts/*.sh"],
                                "audit_required": False}},
            deny={"paths": [".factory"], "patterns": []},
        ))
        with self.assertRaises(pl.PathLeaseClaimError) as caught:
            pl.validate_claim(self.claim, other)
        self.assertIn("policy drift", str(caught.exception))

    def test_granted_set_drift(self) -> None:
        forged = pl.TaskPathLease(
            **{**self.claim.__dict__,
               "granted_scopes": ("scripts",),
               "claim_digest": ""},
        )
        forged = pl.TaskPathLease(
            **{**forged.__dict__, "claim_digest": pl.claim_digest(forged)}
        )
        with self.assertRaises(pl.PathLeaseClaimError) as caught:
            pl.validate_claim(forged, self.policy)
        self.assertIn("drifts from the trusted policy", str(caught.exception))

    def test_replay_across_context(self) -> None:
        for key, value in (("campaign_id", "camp-02"), ("task_id", 33),
                           ("attempt", 2), ("head_commit", "d" * 40),
                           ("plan_digest", "e" * 64),
                           ("policy_digest", "f" * 64)):
            with self.subTest(key=key):
                kwargs = dict(self.bindings)
                kwargs[key] = value
                with self.assertRaises(pl.PathLeaseContextError) as caught:
                    pl.validate_claim_context(self.claim, now=NOW, **kwargs)
                self.assertIn("does not match the trusted launch context",
                              str(caught.exception))

    def test_expiry(self) -> None:
        with self.assertRaises(pl.PathLeaseContextError) as caught:
            pl.validate_claim_context(
                self.claim, now=NOW + timedelta(seconds=7200), **self.bindings
            )
        self.assertIn("expired", str(caught.exception))

    def test_not_yet_issued(self) -> None:
        with self.assertRaises(pl.PathLeaseContextError) as caught:
            pl.validate_claim_context(
                self.claim, now=NOW - timedelta(seconds=1), **self.bindings
            )
        self.assertIn("not yet issued", str(caught.exception))


class NoFollowLoadTest(unittest.TestCase):
    """The bounded no-follow policy load rejects symlinks and oversize."""

    def test_symlinked_policy_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".factory").mkdir()
            target = root / "real-policy.json"
            target.write_text(POLICY_FILE.read_text("utf-8"))
            os.symlink(target, root / ".factory" / "path-lease-policy.json")
            with self.assertRaises(pl.PathLeasePolicyError) as caught:
                pl.load_policy_config(root)
            self.assertIn("cannot open", str(caught.exception))

    def test_symlinked_parent_component_fails_closed(self) -> None:
        # Every path component is opened dirfd/no-follow: a symlinked
        # ``.factory`` parent directory must fail closed too.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            real = root / "real"
            real.mkdir()
            (real / "path-lease-policy.json").write_text(
                POLICY_FILE.read_text("utf-8"))
            os.symlink(real, root / ".factory")
            with self.assertRaises(pl.PathLeasePolicyError) as caught:
                pl.load_policy_config(root)
            self.assertIn("cannot open", str(caught.exception))

    def test_missing_policy_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(pl.PathLeasePolicyError):
                pl.load_policy_config(Path(tmp))

    def test_oversized_policy_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".factory").mkdir()
            (root / ".factory" / "path-lease-policy.json").write_text(
                "x" * (pl.MAX_POLICY_BYTES + 1)
            )
            with self.assertRaises(pl.PathLeasePolicyError):
                pl.load_policy_config(root)

    def test_non_regular_policy_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".factory").mkdir()
            (root / ".factory" / "path-lease-policy.json").mkdir()
            with self.assertRaises(pl.PathLeasePolicyError):
                pl.load_policy_config(root)


class CliTest(unittest.TestCase):
    """The CLI validates the committed policy and expands requests."""

    def test_policy_validate(self) -> None:
        result = subprocess_run([sys.executable, str(LOOP / "path_lease.py"),
                                 "--root", str(ROOT), "policy-validate"])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("policy scopes=", result.stdout)

    def test_expand_ok(self) -> None:
        result = subprocess_run([sys.executable, str(LOOP / "path_lease.py"),
                                 "--root", str(ROOT), "expand", "scripts", "nix"])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("granted_scopes=scripts,nix", result.stdout)

    def test_expand_unknown_fails(self) -> None:
        result = subprocess_run([sys.executable, str(LOOP / "path_lease.py"),
                                 "--root", str(ROOT), "expand", "goldens"])
        self.assertEqual(result.returncode, 2)
        self.assertIn("unknown write scopes", result.stderr)


def subprocess_run(argv):
    import subprocess
    return subprocess.run(argv, capture_output=True, text=True)


if __name__ == "__main__":
    suite = unittest.defaultTestLoader.loadTestsFromModule(sys.modules[__name__])
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    raise SystemExit(0 if result.wasSuccessful() else 1)
