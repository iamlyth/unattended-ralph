#!/usr/bin/env python3
"""Explicitly migrate one stopped legacy campaign into durable supervision."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess

from factory_lock import FactoryLockError, locked
from factory_state_io import StateIOError, atomic_write_json, read_bytes, read_json

ROOT = Path(__file__).resolve().parent.parent
SHA256 = re.compile(r"^[0-9a-f]{64}$")
COUNTERS = ("stale_recoveries", "completion_recoveries", "no_progress_recoveries")


def fail(message: str) -> None:
    raise SystemExit(f"ralph-supervision-migrate: {message}")


def valid_counter(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 64


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--mode",
        choices=("planning", "implementation", "campaign-audit"),
        required=True,
    )
    parser.add_argument("--expected-campaign-sha256", required=True)
    args = parser.parse_args()
    if not SHA256.fullmatch(args.expected_campaign_sha256):
        fail("expected campaign digest is invalid")

    try:
        with locked(ROOT) as lock_descriptor:
            loop_lock = ROOT / ".ralph/loop.lock"
            if loop_lock.exists() or loop_lock.is_symlink():
                fail("Ralph loop lock still exists")

            raw = read_bytes(ROOT, "ralph-campaign.json", maximum=1024 * 1024)
            assert raw is not None
            digest = hashlib.sha256(raw).hexdigest()
            if digest != args.expected_campaign_sha256:
                fail("campaign state digest does not match the operator-confirmed value")
            campaign = json.loads(raw.decode("utf-8"))
            expected_phase = "audit" if args.mode == "campaign-audit" else args.mode
            if (
                not isinstance(campaign, dict)
                or campaign.get("schema") != "ralph-campaign/v2"
                or campaign.get("status") != "active"
                or campaign.get("phase") != expected_phase
                or not isinstance(campaign.get("round"), int)
                or isinstance(campaign.get("round"), bool)
            ):
                fail("campaign is not stopped in the requested active phase")

            # Reuse the campaign helper's strict future-field, ancestry, and
            # schema validation before authorizing a local migration.
            validation = subprocess.run(
                [str(ROOT / "scripts/ralph-campaign-state.py"), "show"],
                cwd=ROOT,
                text=True,
                capture_output=True,
                pass_fds=(lock_descriptor,),
            )
            if validation.returncode:
                fail("saved campaign does not pass strict state validation")
            if json.loads(validation.stdout) != campaign:
                fail("strict campaign validation changed the saved state")

            binding_output = subprocess.run(
                [str(ROOT / "scripts/campaign-verifier-binding.py")],
                cwd=ROOT,
                text=True,
                capture_output=True,
            )
            if binding_output.returncode:
                fail("current campaign verifier binding is invalid")
            verifier_binding = json.loads(binding_output.stdout)
            new_verifier_digest = verifier_binding.get("sha256")
            old_verifier_digest = campaign.get("verification_command_sha256")
            if not isinstance(new_verifier_digest, str) or not SHA256.fullmatch(new_verifier_digest):
                fail("current campaign verifier digest is invalid")
            if not isinstance(old_verifier_digest, str) or not SHA256.fullmatch(old_verifier_digest):
                fail("saved campaign verifier digest is invalid")

            cycle_material = json.dumps(
                {
                    "schema": "ralph-supervision-migration-cycle/v1",
                    "mode": args.mode,
                    "campaign_state_sha256": digest,
                    "legacy_verification_command_sha256": old_verifier_digest,
                    "verification_binding_sha256": new_verifier_digest,
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
            cycle = hashlib.sha256(cycle_material).hexdigest()
            state_name = f"ralph-supervision-{args.mode}.json"
            migration_name = f"ralph-supervision-migration-{args.mode}.json"
            existing_state = read_json(ROOT, state_name, maximum=16384, missing_ok=True)
            existing_migration = read_json(ROOT, migration_name, maximum=16384, missing_ok=True)
            if existing_migration is not None:
                fail("legacy supervision was already migrated")

            counter_values = {key: 0 for key in COUNTERS}
            if existing_state is not None:
                v1_keys = {"schema", "mode", *COUNTERS}
                v2_keys = {"schema", "mode", "cycle_id", *COUNTERS}
                if (
                    isinstance(existing_state, dict)
                    and set(existing_state) == v1_keys
                    and existing_state.get("schema") == "ralph-supervision/v1"
                    and existing_state.get("mode") == args.mode
                    and all(valid_counter(existing_state.get(key)) for key in COUNTERS)
                ):
                    counter_values = {key: existing_state[key] for key in COUNTERS}
                elif (
                    isinstance(existing_state, dict)
                    and set(existing_state) == v2_keys
                    and existing_state.get("schema") == "ralph-supervision/v2"
                    and existing_state.get("mode") == args.mode
                    and existing_state.get("cycle_id") == cycle
                    and all(valid_counter(existing_state.get(key)) for key in COUNTERS)
                ):
                    counter_values = {key: existing_state[key] for key in COUNTERS}
                else:
                    fail("existing supervision state is not the expected legacy or partial migration")

            state = {
                "schema": "ralph-supervision/v2",
                "mode": args.mode,
                "cycle_id": cycle,
                **counter_values,
            }
            migration = {
                "schema": "ralph-supervision-migration/v1",
                "mode": args.mode,
                "cycle_id": cycle,
                "campaign_state_sha256": digest,
                "round": campaign["round"],
                "legacy_verification_command_sha256": old_verifier_digest,
                "verification_binding_sha256": new_verifier_digest,
            }
            # State is written first. If interrupted before the marker, the
            # deterministic cycle permits exactly this partial write to be
            # completed on the next explicit invocation without resetting a
            # counter or authorizing a different campaign.
            if existing_state != state:
                atomic_write_json(ROOT, state_name, state)
            atomic_write_json(ROOT, migration_name, migration)
            print(json.dumps(migration, sort_keys=True))
    except (
        OSError,
        FactoryLockError,
        StateIOError,
        UnicodeError,
        json.JSONDecodeError,
    ) as exc:
        fail(str(exc))


if __name__ == "__main__":
    main()
