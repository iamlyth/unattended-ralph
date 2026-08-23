#!/usr/bin/env python3
"""Hidden Task 11 credential/security boundary suite (CRED-01, §18; GIT-01).

This suite lives under the hidden ``.factory/tests/`` namespace (HIDE-01
keeps harness-only tests out of the adopting product's visible ``tests/``
tree) and is the deterministic verification for Task 11: output-content
redaction through the exact committed credential guard, the stripped
deterministic-gate environment, the model-facing Git shim PATH pinning, and
the external-backend configuration authority.  Only fake secrets are used —
every "credential" is a clearly-labelled FAKE_* placeholder that exists
solely to prove fail-closed behavior; no real secret material and no real
HOME/credential store is ever read or written.

Coverage:

* **guard-source binding** (Task 7 review obligation 3, co-owned with Task
  8): the executing redaction guard is always the exact committed blob of
  ``scripts/credential-guard.py`` at the bound commit.  A missing,
  symlinked, oversized, foreign-owned, group/other-writable, tampered
  (byte-divergent), or uncommitted working-tree guard fails closed before
  any output is redacted; a guard that does not compile or lacks the
  documented API never executes;
* **bounded output capture redaction** (Task 6 review; §18): a synthetic
  credential rendered into child output is masked from the captured result
  tail; a private-key block that straddles the eviction boundary is still
  masked (the block state is scanned incrementally over the evicted
  fragment and the retained tail is seeded); non-UTF-8 bytes are replaced
  before redaction; a redaction failure fails the tail closed to the fixed
  ``[REDACTION FAILED]`` marker; the retained tail and redaction input stay
  bounded;
* **sanitized deterministic-gate environment** (Task 11): deterministic
  gates receive only the documented allowlist, every lock-key prefix and
  Git redirector is stripped, and any surviving credential-shaped key
  fails closed — a credential in the operator's environment can never
  reach a gate child;
* **gate output/detail/results carry no secrets** (Task 11): a synthetic
  credential rendered into deterministic gate output is redacted before it
  can enter the campaign result, the phase-history detail, or the published
  result file, and gate detail stays bounded;
* **the actual CLI and the Node extension** (CRED-01, §18): the tracked
  ``scripts/credential-guard.py`` CLI and the exported
  ``scripts/pi-ralph-emit-extension.mjs`` tool_call/tool_result redaction
  helpers are exercised with synthetic secrets through real subprocesses;
* **model-viewed Git shim** (GIT-01, Task 11): a caller-controlled PATH
  with a forged ``git`` cannot redirect the shim — the real executable is
  resolved from fixed absolute candidates only — and the root refusal (and
  the absence of any PATH fallback) is asserted at source level when the
  root namespace is unavailable to the harness;
* **external backend configuration authority** (Task 11): an external
  (non-workspace) model backend is never accepted through the private
  synthetic-proof seam; it is accepted only under the real Task 8
  confinement authority and an immutable trusted executable path, and a
  mutable/untrusted external path fails closed.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
import unittest.mock

ROOT = Path(__file__).resolve().parents[2]
LOOP = ROOT / ".factory" / "loop"
FIXTURES = ROOT / ".factory" / "tests" / "fixtures"
SCHEMAS = ROOT / ".factory" / "schemas"
GUARD_RELPATH = "scripts/credential-guard.py"

sys.path.insert(0, str(LOOP))
import campaign as campaign_module  # noqa: E402
import confinement as confinement_module  # noqa: E402
import gitutil  # noqa: E402
import launch as launch_module  # noqa: E402
import redaction  # noqa: E402
from launch import (  # noqa: E402
    _BoundedStream,
    InvocationBinding,
    InvocationError,
    OUTPUT_DIGEST_CAP,
    OUTPUT_TAIL_CAP,
    authorize_launch,
)

GIT = gitutil.GIT_EXECUTABLE
REAL_GUARD = ROOT / GUARD_RELPATH
HEAD = subprocess.run(
    [GIT, "-C", str(ROOT), "rev-parse", "HEAD"],
    check=True, capture_output=True, text=True,
).stdout.strip()
assert len(HEAD) == 40

# Synthetic fake-secret material (never real credential material).
SYNTHETIC_SECRET = "FAKE_TOOL_RESULT_SECRET_998877665544332211"
SYNTHETIC_COOKIE = "FAKE_COOKIE_VALUE_11223344556677889900"
SYNTHETIC_TOKEN = "FAKE_TOKEN_VALUE_00112233445566778899"
PRIVATE_KEY_MATERIAL = "FAKE_PRIVATE_KEY_MATERIAL_abcdef0123456789"
NODE_SECRET = "FAKE_NODE_TOOL_SECRET_556677889900"

# Credential-shaped environment tokens the sanitized gate environment must
# never let survive (defense in depth over the allowlist copy).
CREDENTIAL_TOKENS = (
    "COOKIE", "TOKEN", "PASSWORD", "PASSWD", "API_KEY", "SECRET",
    "CREDENTIAL", "PRIVATE_KEY", "AUTH",
)


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def run(
    command: list[str],
    *,
    check: bool = True,
    input_data: bytes | None = None,
    env: dict | None = None,
) -> subprocess.CompletedProcess:
    result = subprocess.run(
        command, input=input_data, capture_output=True, env=env,
    )
    if check and result.returncode:
        raise AssertionError(
            (command, result.returncode, result.stdout[-2000:], result.stderr[-2000:])
        )
    return result


def _git(workspace: Path, *args: str) -> subprocess.CompletedProcess:
    return run([GIT, "-C", str(workspace), *args], check=True)


def _committed_head(workspace: Path) -> str:
    head = _git(workspace, "rev-parse", "HEAD").stdout.decode().strip()
    assert len(head) == 40
    return head


class _FixtureWorkspace:
    """One small committed repository whose HEAD carries the exact guard.

    ``commit_guard=True`` (the default) commits the exact
    ``scripts/credential-guard.py`` blob; ``commit_guard=False`` leaves the
    guard absent from HEAD (the uncommitted/missing-blob fixtures build on
    that).  The working tree starts clean and owned 0755 so the guarded
    no-follow read passes unless a test deliberately tampers with it.
    """

    def __init__(self, tmp: Path, *, commit_guard: bool = True) -> None:
        self.root = tmp / "workspace"
        scripts = self.root / "scripts"
        scripts.mkdir(parents=True)
        # A placeholder so the empty (no-guard) base repo still has one
        # committed file (git refuses an empty initial commit).
        (self.root / "README").write_text("fixture\n", encoding="utf-8")
        if commit_guard:
            shutil.copy2(REAL_GUARD, self.root / GUARD_RELPATH)
        _git(self.root, "init", "-q", "-b", "fixture-main")
        _git(self.root, "config", "user.email", "factory@test")
        _git(self.root, "config", "user.name", "factory")
        _git(self.root, "add", "-A")
        _git(self.root, "commit", "-qm", "fixture base")
        self.head = _committed_head(self.root)
        if not commit_guard:
            # A committed repo whose HEAD does *not* carry the guard: commit
            # the empty base, then add the guard to the working tree only.
            shutil.copy2(REAL_GUARD, self.root / GUARD_RELPATH)


# ---------------------------------------------------------------------------
# 1. Exact committed guard-source binding (fail closed on every substitution)
# ---------------------------------------------------------------------------


class GuardSourceBindingTests(unittest.TestCase):
    """The redaction authority executes only the exact committed guard blob.

    Fail-closed classes: missing, symlinked, uncommitted (worktree != HEAD
    blob), tampered bytes, oversized, foreign-owned, group/other-writable,
    non-regular path, missing committed blob, uncompilable guard, and a
    guard without the documented API.  ``redactor_from_bytes`` likewise
    rejects a divergent or oversized byte pair.
    """

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="factory-redaction-src."))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def test_exact_committed_guard_verified_and_redacts(self) -> None:
        ws = _FixtureWorkspace(self.tmp)
        redactor = redaction.redactor_for(ws.root, ws.head)
        out = redactor.redact_text(f"TOKEN={SYNTHETIC_TOKEN}\n")
        self.assertNotIn(SYNTHETIC_TOKEN, out)
        self.assertIn("[REDACTED]", out)
        self.assertEqual(
            redaction.REDACTION_GUARD_RELPATH, GUARD_RELPATH
        )

    def test_missing_guard_fails_closed(self) -> None:
        ws = _FixtureWorkspace(self.tmp, commit_guard=False)
        # The guard exists in the working tree but is NOT committed at HEAD.
        self.assertTrue((ws.root / GUARD_RELPATH).is_file())
        with self.assertRaises(redaction.OutputRedactionError):
            redaction.redactor_for(ws.root, ws.head)

    def test_symlinked_guard_fails_closed(self) -> None:
        ws = _FixtureWorkspace(self.tmp)
        target = ws.root / "scripts" / "credential-guard.py"
        link = ws.root / "scripts" / "guard-link.py"
        link.symlink_to(target.name)
        with self.assertRaises(redaction.OutputRedactionError) as caught:
            redaction._read_worktree_source(
                ws.root, "scripts/guard-link.py", redaction.MAX_GUARD_SOURCE_BYTES
            )
        self.assertIn("open", str(caught.exception))

    def test_uncommitted_tampered_guard_fails_closed(self) -> None:
        ws = _FixtureWorkspace(self.tmp)
        with open(ws.root / GUARD_RELPATH, "ab") as stream:
            stream.write(b"\n# tampered\n")
        with self.assertRaises(redaction.OutputRedactionError) as caught:
            redaction.redactor_for(ws.root, ws.head)
        self.assertIn("not the exact committed", str(caught.exception))

    def test_tampered_byte_pair_fails_closed(self) -> None:
        ws = _FixtureWorkspace(self.tmp)
        committed = _git(ws.root, "show", f"{ws.head}:{GUARD_RELPATH}").stdout
        tampered = committed + b"\n# tampered\n"
        with self.assertRaises(redaction.OutputRedactionError) as caught:
            redaction.redactor_from_bytes(tampered, committed, ws.head)
        self.assertIn("not the exact committed blob", str(caught.exception))

    def test_oversized_guard_fails_closed(self) -> None:
        ws = _FixtureWorkspace(self.tmp)
        with open(ws.root / GUARD_RELPATH, "wb") as stream:
            stream.write(b"x" * (redaction.MAX_GUARD_SOURCE_BYTES + 1))
        with self.assertRaises(redaction.OutputRedactionError) as caught:
            redaction.read_worktree_guard_source(ws.root)
        self.assertIn("bound", str(caught.exception))
        # The byte-pair verifier applies the same bound.
        big = b"x" * (redaction.MAX_GUARD_SOURCE_BYTES + 1)
        with self.assertRaises(redaction.OutputRedactionError):
            redaction.redactor_from_bytes(big, big, ws.head)

    def test_group_writable_guard_fails_closed(self) -> None:
        ws = _FixtureWorkspace(self.tmp)
        os.chmod(ws.root / GUARD_RELPATH, 0o666)
        with self.assertRaises(redaction.OutputRedactionError) as caught:
            redaction.read_worktree_guard_source(ws.root)
        self.assertIn("writable", str(caught.exception))

    @unittest.skipUnless(os.geteuid() == 0, "requires root to chown")
    def test_foreign_owned_guard_fails_closed(self) -> None:
        ws = _FixtureWorkspace(self.tmp)
        os.chown(ws.root / GUARD_RELPATH, 0, 0)
        with self.assertRaises(redaction.OutputRedactionError) as caught:
            redaction.read_worktree_guard_source(ws.root)
        self.assertIn("owned", str(caught.exception))

    def test_non_regular_guard_path_fails_closed(self) -> None:
        ws = _FixtureWorkspace(self.tmp)
        path = ws.root / GUARD_RELPATH
        os.unlink(path)
        path.mkdir()
        with self.assertRaises(redaction.OutputRedactionError) as caught:
            redaction.read_worktree_guard_source(ws.root)
        self.assertIn("regular", str(caught.exception))

    def test_non_utf8_guard_source_rejected(self) -> None:
        # A guard whose exact committed bytes do not compile never executes
        # (the byte pair matches, so the compile gate is the failure).
        ws = _FixtureWorkspace(self.tmp)
        with open(ws.root / GUARD_RELPATH, "wb") as stream:
            stream.write(b"\xff\xfe not python")
        _git(ws.root, "add", "-A")
        _git(ws.root, "commit", "-qm", "non-utf8 guard")
        ws.head = _committed_head(ws.root)
        with self.assertRaises(redaction.OutputRedactionError) as caught:
            redaction.redactor_for(ws.root, ws.head)
        self.assertIn("compile", str(caught.exception))

    def test_guard_without_api_fails_closed(self) -> None:
        ws = _FixtureWorkspace(self.tmp)
        module = b"VALUE = 1\n"
        committed = _git(ws.root, "show", f"{ws.head}:{GUARD_RELPATH}").stdout
        with self.assertRaises(redaction.OutputRedactionError) as caught:
            redaction.redactor_from_bytes(module, module, ws.head)
        self.assertIn("missing the", str(caught.exception))
        # The committed-blob path fails the same way: a guard committed at
        # HEAD without the API is never executed.
        with self.assertRaises(redaction.OutputRedactionError):
            redaction.redactor_from_bytes(committed, module, ws.head)

    def test_non_bytes_guard_pair_fails_closed(self) -> None:
        ws = _FixtureWorkspace(self.tmp)
        with self.assertRaises(redaction.OutputRedactionError):
            redaction.redactor_from_bytes("text", b"bytes", ws.head)

    def test_missing_committed_blob_fails_closed(self) -> None:
        ws = _FixtureWorkspace(self.tmp, commit_guard=False)
        # Working-tree guard exists; the committed blob at HEAD does not.
        self.assertTrue((ws.root / GUARD_RELPATH).is_file())
        with self.assertRaises(redaction.OutputRedactionError) as caught:
            redaction.redactor_for(ws.root, ws.head)
        self.assertIn("not tracked at the bound commit", str(caught.exception))

    def test_invalid_bound_commit_rejected(self) -> None:
        with self.assertRaises(redaction.OutputRedactionError):
            redaction._committed_guard_bytes(ROOT, "not-a-commit")


# ---------------------------------------------------------------------------
# 2. Redactor tail / streaming private-key block state
# ---------------------------------------------------------------------------


class RedactTailTests(unittest.TestCase):
    """Masked tails, split-marker completion, block seeding, output bounds."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="factory-redaction-tail."))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.redactor = redaction.redactor_for(ROOT, HEAD)

    def test_redact_text_is_idempotent_and_masks_assignments(self) -> None:
        text = f"export OLLAMA_COOKIE={SYNTHETIC_COOKIE}\nAPI_KEY={SYNTHETIC_TOKEN}\n"
        once = self.redactor.redact_text(text)
        twice = self.redactor.redact_text(once)
        self.assertEqual(once, twice)
        self.assertNotIn(SYNTHETIC_COOKIE, once)
        self.assertNotIn(SYNTHETIC_TOKEN, once)
        self.assertIn("[REDACTED]", once)

    def test_redact_tail_in_block_masks_open_block(self) -> None:
        tail = (
            f"-----END PRIVATE KEY-----\n"
            f"TOKEN={SYNTHETIC_TOKEN}\n"
        )
        out = self.redactor.redact_tail(tail, in_block=True, partial="")
        self.assertNotIn(SYNTHETIC_TOKEN, out)
        self.assertNotIn("PRIVATE KEY", out)
        self.assertIn("[REDACTED-PRIVATE-KEY-BLOCK-END]", out)

    def test_redact_tail_partial_completes_split_marker(self) -> None:
        # The BEGIN marker was split exactly at the capture boundary; the
        # partial is completed before redaction, so no key material survives.
        out = self.redactor.redact_tail(
            f"-----END PRIVATE KEY-----\n",
            in_block=False,
            partial="-----BEGIN PRIVATE KEY-----\n",
        )
        self.assertIn("[REDACTED-PRIVATE-KEY-BLOCK]", out)
        self.assertIn("[REDACTED-PRIVATE-KEY-BLOCK-END]", out)
        self.assertNotIn("BEGIN", out.replace("[REDACTED-PRIVATE-KEY-BLOCK]", ""))

    def test_redact_tail_bound_bytes_caps_output(self) -> None:
        tail = f"TOKEN={SYNTHETIC_TOKEN}\n" * 200
        out = self.redactor.redact_tail(tail, False, "", bound_bytes=256)
        self.assertLessEqual(len(out.encode("utf-8")), 256)
        self.assertNotIn(SYNTHETIC_TOKEN, out)

    def test_redact_tail_rejects_bad_bounds(self) -> None:
        for bad in (-1, True, 1.5, "10"):
            with self.assertRaises(redaction.OutputRedactionError):
                self.redactor.redact_tail("x", False, "", bound_bytes=bad)  # type: ignore[arg-type]

    def test_redact_tail_empty(self) -> None:
        self.assertEqual(
            self.redactor.redact_tail("", False, ""), ""
        )

    def test_redact_tail_mid_line_drops_incomplete_first_line(self) -> None:
        # A tail that begins mid-line (the retained half of a line whose
        # start was evicted) is dropped before redaction; the complete
        # lines that follow are still masked.
        out = self.redactor.redact_tail(
            f"ABCDEFGHIJ\nTOKEN={SYNTHETIC_TOKEN}\n",
            False, "FAKE_OPAQUE_TOKEN_=", mid_line=True,
        )
        self.assertNotIn("ABCDEFGHIJ", out)
        self.assertNotIn(SYNTHETIC_TOKEN, out)
        self.assertIn("[REDACTED]", out)

    def test_redact_tail_mid_line_split_begin_marker_still_masks_block(self) -> None:
        # A private-key BEGIN marker split exactly at the eviction boundary:
        # the evicted fragment + the dropped first line re-form the marker,
        # the block state is repaired before redaction, and the block
        # content that follows stays masked.
        out = self.redactor.redact_tail(
            f"VATE KEY-----\n{PRIVATE_KEY_MATERIAL}\n"
            f"-----END PRIVATE KEY-----\n",
            False, "-----BEGIN PRI", mid_line=True,
        )
        self.assertNotIn(PRIVATE_KEY_MATERIAL, out)
        self.assertNotIn("BEGIN", out.replace("[REDACTED-PRIVATE-KEY-BLOCK]", ""))
        self.assertIn("[REDACTED-PRIVATE-KEY-BLOCK-END]", out)

    def test_redact_tail_mid_line_split_end_marker_closes_block(self) -> None:
        # An END marker split at the boundary while the block is open: the
        # repair scan sees the completed marker, closes the block, and the
        # tail that follows is masked as ordinary output.
        out = self.redactor.redact_tail(
            f" PRIVATE KEY-----\nTOKEN={SYNTHETIC_TOKEN}\n",
            True, "-----END", mid_line=True,
        )
        self.assertNotIn(SYNTHETIC_TOKEN, out)
        self.assertIn("[REDACTED]", out)

    def test_scan_key_block_state_split_marker(self) -> None:
        in_block, partial = redaction.scan_key_block_state(
            False, "", b"line\n-----BEGIN PRI"
        )
        self.assertFalse(in_block)
        in_block, partial = redaction.scan_key_block_state(
            in_block, partial, b"VATE KEY-----\nMIIKEY\n"
        )
        self.assertTrue(in_block)
        in_block, partial = redaction.scan_key_block_state(
            in_block, partial, b"-----END PRIVATE KEY-----\n"
        )
        self.assertFalse(in_block)

    def test_scan_key_block_state_evicted_fragment_seeds(self) -> None:
        # A BEGIN marker deep inside a giant evicted fragment still opens the
        # block state even though the partial tail line is window-capped.
        fragment = b"x" * 50000 + b"-----BEGIN PRIVATE KEY-----"
        in_block, partial = redaction.scan_key_block_state(False, "", fragment)
        self.assertTrue(in_block)
        self.assertLessEqual(len(partial.encode("utf-8")), redaction.KEY_BLOCK_WINDOW)

    def test_scan_key_block_state_end_outside_block_is_ignored(self) -> None:
        in_block, _ = redaction.scan_key_block_state(
            False, "", b"-----END PRIVATE KEY-----\n"
        )
        self.assertFalse(in_block)


# ---------------------------------------------------------------------------
# 3. Bounded capture streams (launch._BoundedStream) redaction
# ---------------------------------------------------------------------------


class _FailingRedactor:
    def redact_tail(self, *args, **kwargs):  # noqa: ANN002, ANN003
        raise redaction.OutputRedactionError("simulated guard failure")


class BoundedStreamRedactionTests(unittest.TestCase):
    """Child-output captures redact secrets; eviction boundary is block-safe."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="factory-redaction-stream."))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.redactor = redaction.redactor_for(ROOT, HEAD)

    def test_synthetic_secret_never_reaches_result_tail(self) -> None:
        stream = _BoundedStream(redactor=self.redactor)
        stream.feed(b"build ok\n")
        stream.feed(f"TOKEN={SYNTHETIC_TOKEN}\n".encode("utf-8"))
        stream.feed(b"done\n")
        result = stream.result()
        self.assertNotIn(SYNTHETIC_TOKEN, result.tail)
        self.assertIn("[REDACTED]", result.tail)
        self.assertFalse(result.truncated)
        self.assertEqual(result.bytes, stream._total)

    def test_private_key_block_split_at_eviction_boundary_is_masked(self) -> None:
        # The BEGIN marker is inside the *evicted* oldest region; the block
        # state must be seeded at the new tail boundary so the END line and
        # the trailing secret in the retained tail are still masked.
        stream = _BoundedStream(redactor=self.redactor)
        chunk1 = (
            b"-----BEGIN PRIVATE KEY-----\n"
            + f"{PRIVATE_KEY_MATERIAL}\n".encode("utf-8")
            + b"padding-line\n" * 40000
        )
        self.assertGreater(len(chunk1), OUTPUT_TAIL_CAP)
        stream.feed(chunk1)
        self.assertTrue(stream.truncated)
        self.assertTrue(stream._tail_start_block)
        stream.feed(
            f"-----END PRIVATE KEY-----\nAPI_KEY={SYNTHETIC_SECRET}\n".encode("utf-8")
        )
        result = stream.result()
        self.assertNotIn(SYNTHETIC_SECRET, result.tail)
        self.assertNotIn(PRIVATE_KEY_MATERIAL, result.tail)
        self.assertIn("[REDACTED-PRIVATE-KEY-BLOCK-END]", result.tail)

    def test_private_key_block_fully_inside_tail_is_masked(self) -> None:
        stream = _BoundedStream(redactor=self.redactor)
        stream.feed(
            b"prefix\n"
            b"-----BEGIN PRIVATE KEY-----\n"
            + f"{PRIVATE_KEY_MATERIAL}\n".encode("utf-8")
            + b"-----END PRIVATE KEY-----\n"
            + f"API_KEY={SYNTHETIC_SECRET}\n".encode("utf-8")
        )
        result = stream.result()
        self.assertNotIn(SYNTHETIC_SECRET, result.tail)
        self.assertNotIn(PRIVATE_KEY_MATERIAL, result.tail)
        self.assertIn("[REDACTED-PRIVATE-KEY-BLOCK]", result.tail)

    def test_non_utf8_bytes_replaced_and_redacted(self) -> None:
        stream = _BoundedStream(redactor=self.redactor)
        stream.feed(b"\xff\xfe\x00 garbage\n")
        stream.feed(f"API_KEY={SYNTHETIC_SECRET}\n".encode("utf-8"))
        result = stream.result()
        self.assertNotIn(SYNTHETIC_SECRET, result.tail)
        # The raw non-UTF-8 bytes never survive (replaced with U+FFFD).
        self.assertNotIn("\xff", result.tail)

    def test_output_tail_cap_bounded(self) -> None:
        stream = _BoundedStream(redactor=self.redactor)
        payload = b"a" * (OUTPUT_TAIL_CAP * 2 + 4096)
        stream.feed(payload)
        result = stream.result()
        self.assertTrue(result.truncated)
        self.assertEqual(result.bytes, len(payload))
        self.assertLessEqual(len(result.tail.encode("utf-8")), OUTPUT_TAIL_CAP)

    def test_opaque_token_straddling_boundary_leaks_no_fragment(self) -> None:
        # Task 11 review regression: a >KEY_BLOCK_WINDOW opaque TOKEN value
        # cut at the eviction boundary must never leak a raw fragment.  The
        # eviction boundary lands inside the token value more than 512 bytes
        # from the line start, so the evicted fragment is window-capped and
        # the split line cannot be faithfully reconstructed; the incomplete
        # first line of the retained tail is dropped before redaction.
        stream = _BoundedStream(redactor=self.redactor)
        token_value = "B" * 700
        token_line = f"FAKE_OPAQUE_TOKEN_={token_value}\n".encode("utf-8")
        prefix = b"x" * 64
        padding = b"z" * (
            OUTPUT_TAIL_CAP + 600 - len(prefix) - len(token_line)
        )
        # The eviction boundary falls at stream byte 600, inside the token
        # value (which spans bytes 64..783) and >512 bytes from its start.
        stream.feed(prefix + token_line + padding)
        self.assertTrue(stream.truncated)
        result = stream.result()
        self.assertNotIn("FAKE_OPAQUE_TOKEN_", result.tail)
        self.assertNotIn("B", result.tail)
        self.assertNotIn("x", result.tail)
        self.assertEqual(result.digest, hashlib.sha256(
            (prefix + token_line + padding)[:OUTPUT_DIGEST_CAP]
        ).hexdigest())

    def test_newline_free_oversized_tail_yields_no_raw_fragment(self) -> None:
        # The whole retained tail is one incomplete line (a newline-free
        # oversized value); the incomplete line is dropped in full, so no
        # raw fragment survives.
        stream = _BoundedStream(redactor=self.redactor)
        stream.feed(
            b"FAKE_OPAQUE_TOKEN_=" + b"C" * (OUTPUT_TAIL_CAP + 200)
        )
        result = stream.result()
        self.assertTrue(result.truncated)
        self.assertEqual(result.tail, "")
        self.assertNotIn("C", result.tail)
        self.assertNotIn("FAKE_OPAQUE_TOKEN_", result.tail)

    def test_redaction_failure_fails_tail_closed(self) -> None:
        stream = _BoundedStream(redactor=_FailingRedactor())
        stream.feed(f"TOKEN={SYNTHETIC_TOKEN}\n".encode("utf-8"))
        result = stream.result()
        self.assertEqual(result.tail, redaction.REDACTION_FAILED)

    def test_digest_covers_bounded_raw_prefix(self) -> None:
        stream = _BoundedStream(redactor=self.redactor)
        stream.feed(f"TOKEN={SYNTHETIC_TOKEN}\n".encode("utf-8"))
        result = stream.result()
        # The digest is over the exact raw prefix (never over masked bytes),
        # so it stays a deterministic byte-hash of the child's output.
        self.assertEqual(
            result.digest,
            hashlib.sha256(f"TOKEN={SYNTHETIC_TOKEN}\n".encode("utf-8")).hexdigest(),
        )

    def test_stream_without_redactor_keeps_raw_diagnostic_tail(self) -> None:
        # A redactor-less stream is the pre-Task-11 diagnostics mode; it
        # must never be used for a production capture but keeps raw tails.
        stream = _BoundedStream()
        stream.feed(b"plain\n")
        self.assertEqual(stream.result().tail, "plain\n")


# ---------------------------------------------------------------------------
# 4. Sanitized deterministic-gate environment
# ---------------------------------------------------------------------------


class SanitizedGateEnvironmentTests(unittest.TestCase):
    """Gates get an allowlisted env; lock keys, Git redirectors, and
    credential-shaped keys never survive."""

    def test_allowlist_only_keys_copied(self) -> None:
        parent = {
            "PATH": "/usr/bin",
            "HOME": "/home/factory",
            "LANG": "C",
            "TERM": "xterm",
            "TZ": "UTC",
            "SHELL": "/bin/bash",
            "RANDOM_JUNK": "never-copied",
            "LD_PRELOAD": "/evil.so",
        }
        env = campaign_module.sanitized_gate_environment(parent)
        self.assertEqual(env["PATH"], "/usr/bin")
        self.assertEqual(env["HOME"], "/home/factory")
        self.assertNotIn("RANDOM_JUNK", env)
        self.assertNotIn("LD_PRELOAD", env)

    def test_lock_metadata_prefixes_stripped(self) -> None:
        parent = {
            "PATH": "/usr/bin",
            "FACTORY_LOOP_LOCK_HELD": "1",
            "FACTORY_LOOP_LOCK_ROOT": "/secret/root",
            "FACTORY_LOOP_LOCK_ID": "secret-id",
            "FACTORY_LOCK_FD": "7",  # legacy lifecycle prefix
            "FACTORY_LOCK_ROOT": "/legacy/secret",
        }
        env = campaign_module.sanitized_gate_environment(parent)
        for key in ("FACTORY_LOOP_LOCK_HELD", "FACTORY_LOOP_LOCK_ROOT",
                    "FACTORY_LOOP_LOCK_ID", "FACTORY_LOCK_FD", "FACTORY_LOCK_ROOT"):
            self.assertNotIn(key, env)

    def test_git_redirectors_stripped(self) -> None:
        parent = {
            "PATH": "/usr/bin",
            "GIT_DIR": "/evil",
            "GIT_WORK_TREE": "/evil",
            "GIT_INDEX_FILE": "/evil",
            "GIT_CONFIG": "/evil/config",
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "core.hooksPath",
            "GIT_CONFIG_VALUE_0": "/evil",
            "GIT_SSH_COMMAND": "ssh -o ProxyCommand=evil",
        }
        env = campaign_module.sanitized_gate_environment(parent)
        for key in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_CONFIG",
                    "GIT_CONFIG_COUNT", "GIT_CONFIG_KEY_0", "GIT_CONFIG_VALUE_0",
                    "GIT_SSH_COMMAND"):
            self.assertNotIn(key, env)
        self.assertEqual(env["PATH"], "/usr/bin")

    def test_credential_shaped_keys_never_survive_allowlist(self) -> None:
        # The allowlist itself carries no credential-shaped key, so a
        # synthetic credential variable in the operator's environment never
        # reaches a gate child.
        for token in CREDENTIAL_TOKENS:
            key = f"FAKE_{token}"
            parent = {"PATH": "/usr/bin", key: "synthetic-value"}
            env = campaign_module.sanitized_gate_environment(parent)
            self.assertNotIn(key, env, token)

    def test_allowlist_has_no_credential_shaped_key(self) -> None:
        for key in campaign_module.GATE_ENV_ALLOWLIST:
            upper = key.upper()
            for token in CREDENTIAL_TOKENS:
                self.assertNotIn(token, upper, f"{key} contains {token}")

    def test_surviving_credential_shaped_key_fails_closed(self) -> None:
        # Defense in depth: even if the allowlist were (wrongly) extended
        # with a credential-shaped key, the gate env constructor fails
        # closed rather than spawning the gate with it.
        with unittest.mock.patch.object(
            campaign_module, "GATE_ENV_ALLOWLIST", ("PATH", "FAKE_COOKIE")
        ):
            with self.assertRaises(campaign_module.CampaignError) as caught:
                campaign_module.sanitized_gate_environment(
                    {"PATH": "/usr/bin", "FAKE_COOKIE": "n=v"}
                )
            self.assertIn("FAKE_COOKIE", str(caught.exception))

    def test_sanitized_gate_environment_defaults_to_os_environ(self) -> None:
        with unittest.mock.patch.dict(
            os.environ,
            {
                "FACTORY_LOOP_LOCK_HELD": "1",
                "GIT_CONFIG_COUNT": "1",
                "PATH": "/usr/bin",
            },
            clear=False,
        ):
            env = campaign_module.sanitized_gate_environment()
        self.assertEqual(env["PATH"], "/usr/bin")
        self.assertNotIn("FACTORY_LOOP_LOCK_HELD", env)
        self.assertNotIn("GIT_CONFIG_COUNT", env)


# ---------------------------------------------------------------------------
# 5. Campaign gate output redaction (unit + end-to-end)
# ---------------------------------------------------------------------------


class _FakeCampaignGit:
    def __init__(self, head: str, guard_blob: bytes) -> None:
        self._head = head
        self._guard_blob = guard_blob

    def head(self) -> str:
        return self._head

    def blob_at(self, commit: str, relpath: str) -> bytes:
        if relpath != GUARD_RELPATH:
            raise AssertionError(f"unexpected blob {relpath}")
        return self._guard_blob


class CampaignGateRedactionTests(unittest.TestCase):
    """Deterministic gate output is bounded and redacted before it can enter
    results, logs, receipts, or repository state."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="factory-redaction-gate."))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.ws = _FixtureWorkspace(self.tmp)
        self.committed = _git(
            self.ws.root, "show", f"{self.ws.head}:{GUARD_RELPATH}"
        ).stdout

    def _campaign_stub(self):
        campaign = object.__new__(campaign_module.Campaign)
        campaign._root = self.ws.root
        campaign._git = _FakeCampaignGit(self.ws.head, self.committed)
        campaign._redactor = None
        return campaign

    def test_gate_detail_redacts_synthetic_secret(self) -> None:
        campaign = self._campaign_stub()
        detail = campaign._redact_gate_detail(
            f"TOKEN={SYNTHETIC_TOKEN}\n",
            f"API_KEY={SYNTHETIC_SECRET}\n",
        )
        self.assertNotIn(SYNTHETIC_TOKEN, detail)
        self.assertNotIn(SYNTHETIC_SECRET, detail)
        self.assertIn("[REDACTED]", detail)

    def test_gate_detail_is_bounded(self) -> None:
        campaign = self._campaign_stub()
        detail = campaign._redact_gate_detail("x" * 20000, "")
        self.assertLessEqual(len(detail), campaign_module.GATE_DETAIL_MAX)
        # The secret inside the bounded window is masked.
        detail = campaign._redact_gate_detail(
            "ok\n" + f"API_KEY={SYNTHETIC_SECRET}\n" + "y" * 20000, ""
        )
        self.assertNotIn(SYNTHETIC_SECRET, detail)

    def test_gate_detail_fails_closed_without_verified_guard(self) -> None:
        # A tampered working-tree guard (no longer the exact committed blob)
        # makes the gate redaction authority fail closed: no gate output may
        # reach results, logs, receipts, or repository state.
        ws = _FixtureWorkspace(self.tmp / "ws2")
        with open(ws.root / GUARD_RELPATH, "ab") as stream:
            stream.write(b"\n# tampered\n")
        campaign = object.__new__(campaign_module.Campaign)
        campaign._root = ws.root
        campaign._git = _FakeCampaignGit(
            ws.head,
            _git(ws.root, "show", f"{ws.head}:{GUARD_RELPATH}").stdout,
        )
        campaign._redactor = None
        with self.assertRaises(campaign_module.CampaignError) as caught:
            campaign._redact_gate_detail("raw secret", "")
        self.assertIn("redaction", str(caught.exception).lower())

    def test_gate_redactor_reused_across_gates(self) -> None:
        campaign = self._campaign_stub()
        first = campaign._redact_gate_detail(f"TOKEN={SYNTHETIC_TOKEN}\n", "")
        self.assertNotIn(SYNTHETIC_TOKEN, first)
        self.assertIsNotNone(campaign._redactor)
        second = campaign._redact_gate_detail(f"API_KEY={SYNTHETIC_SECRET}\n", "")
        self.assertNotIn(SYNTHETIC_SECRET, second)


# End-to-end: load the sibling campaign suite so the committed fixture
# workspace and CLI conventions are reused, never copied.
_CAMPAIGN_SUITE = ROOT / ".factory" / "tests" / "test-factory-campaign.py"
_campaign_spec = importlib.util.spec_from_file_location(
    "factory_campaign_suite", _CAMPAIGN_SUITE)
FACTORY_CAMPAIGN = importlib.util.module_from_spec(_campaign_spec)
assert _campaign_spec.loader is not None
_campaign_spec.loader.exec_module(FACTORY_CAMPAIGN)


class CampaignGateEndToEndTests(unittest.TestCase):
    """A failing deterministic gate that prints a synthetic credential leaves
    no trace of it in the campaign result, the phase-history detail, or the
    persisted result file."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="factory-redaction-campaign."))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def test_verification_gate_secret_never_reaches_results(self) -> None:
        ws = FACTORY_CAMPAIGN.FixtureWorkspace(
            self.tmp / "ws",
            scenario={
                "planner": {"behavior": "planned"},
                "developer": {"behavior": "complete"},
                "tester": {"behavior": "pass"},
                "auditor": {"behavior": "findings"},
            },
        )
        # Task 12: the deterministic verifier is bound to its committed blob
        # before the untrusted phase, so the leak gate must be a committed
        # repo-relative executable (an unbound caller-owned script now fails
        # closed as infrastructure_failure and never runs).
        leak = ws.root / "src" / "leak-gate.py"
        leak.parent.mkdir(parents=True, exist_ok=True)
        leak.write_text(
            "#!/usr/bin/env python3\n"
            "import sys\n"
            f"print('TOKEN={SYNTHETIC_TOKEN}', file=sys.stderr)\n"
            "print('gate detail starts')\n"
            f"print('API_KEY={SYNTHETIC_SECRET}')\n"
            "sys.exit(1)\n",
            encoding="utf-8",
        )
        os.chmod(leak, 0o755)
        _git(ws.root, "add", "-A")
        _git(ws.root, "commit", "-qm", "leak gate fixture")
        ws.commit_scenario()
        rc, data = ws.run_cli(
            extra=["--verification-command", "./src/leak-gate.py"])
        self.assertEqual(rc, 1)
        self.assertEqual(data["phase_history"][2]["phase"], "verification")
        self.assertEqual(data["phase_history"][2]["outcome"], "findings")
        raw = json.dumps(data)
        self.assertNotIn(SYNTHETIC_TOKEN, raw)
        self.assertNotIn(SYNTHETIC_SECRET, raw)
        detail = data["phase_history"][2]["detail"]
        self.assertIn("[REDACTED]", detail)
        # The persisted result file carries the same redacted detail.
        result_file = ws.root / ".factory-state" / "campaign-result-campaign.json"
        persisted = result_file.read_text(encoding="utf-8")
        self.assertNotIn(SYNTHETIC_TOKEN, persisted)
        self.assertNotIn(SYNTHETIC_SECRET, persisted)
        self.assertIn("[REDACTED]", persisted)


# ---------------------------------------------------------------------------
# 6. The actual credential-guard CLI
# ---------------------------------------------------------------------------


class CredentialGuardCliTests(unittest.TestCase):
    """The tracked scripts/credential-guard.py CLI, driven as a subprocess."""

    GUARD = str(REAL_GUARD)

    def test_check_command_blocks_proc_read(self) -> None:
        result = run(
            [sys.executable, self.GUARD, "check-command",
             "--command", "cat /proc/self/environ"],
            check=False,
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn(b'"verdict":"block"', result.stdout)
        self.assertIn(b'"reason":"procfs-environ-cmdline"', result.stdout)
        self.assertNotIn(b"cat /proc", result.stdout)

    def test_check_command_allows_ordinary(self) -> None:
        result = run(
            [sys.executable, self.GUARD, "check-command",
             "--command", "cmake --build build-check --parallel"],
            check=False,
        )
        self.assertEqual(result.returncode, 0)
        self.assertIn(b'"verdict":"allow"', result.stdout)

    def test_redact_stream_masks_synthetic_secret(self) -> None:
        source = f"TOKEN={SYNTHETIC_TOKEN}\nbuild ok\n".encode("utf-8")
        result = run(
            [sys.executable, self.GUARD, "redact"],
            input_data=source,
            check=False,
        )
        self.assertEqual(result.returncode, 0)
        self.assertNotIn(SYNTHETIC_TOKEN.encode(), result.stdout)
        self.assertIn(b"[REDACTED]", result.stdout)
        self.assertIn(b"build ok", result.stdout)

    def test_redact_is_idempotent(self) -> None:
        source = f"API_KEY={SYNTHETIC_SECRET}\n".encode("utf-8")
        once = run([sys.executable, self.GUARD, "redact"],
                   input_data=source, check=False).stdout
        twice = run([sys.executable, self.GUARD, "redact"],
                    input_data=once, check=False).stdout
        self.assertEqual(once, twice)

    def test_check_stdin_invalid_utf8_fails_closed(self) -> None:
        result = run(
            [sys.executable, self.GUARD, "check-command-stdin"],
            input_data=b"\xff\xfe", check=False,
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn(b'"reason":"invalid-utf-8"', result.stdout)

    def test_check_path_stdin_dotenv_fails_closed(self) -> None:
        # Exactly one path, no embedded newline (check-path-stdin rejects
        # structural violations before classification).
        result = run(
            [sys.executable, self.GUARD, "check-path-stdin"],
            input_data=b".env", check=False,
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn(b'"reason":"dotenv-store"', result.stdout)

    def test_redact_json_stdin_masks_nested_values(self) -> None:
        payload = json.dumps(
            {"a": {"b": f"TOKEN={SYNTHETIC_TOKEN}"}, "c": ["safe"]}
        ).encode("utf-8")
        result = run(
            [sys.executable, self.GUARD, "redact-json-stdin"],
            input_data=payload, check=False,
        )
        self.assertEqual(result.returncode, 0)
        out = result.stdout.decode("utf-8")
        self.assertNotIn(SYNTHETIC_TOKEN, out)
        parsed = json.loads(out)
        self.assertIn("[REDACTED]", parsed["a"]["b"])
        self.assertEqual(parsed["c"], ["safe"])


# ---------------------------------------------------------------------------
# 7. The Node extension's exported tool_call / tool_result redaction
# ---------------------------------------------------------------------------


NODE_FIXTURE = r'''
import assert from 'node:assert/strict';
import { pathToFileURL } from 'node:url';

const NODE_SECRET = '__NODE_SECRET__';
const extensionUrl = process.argv[2];
const extension = await import(pathToFileURL(extensionUrl));

// 1. Text redaction through the tracked sibling guard.
const r = extension.redactText(`TOKEN=${NODE_SECRET}\n`);
assert.equal(r.ok, true);
assert(!r.text.includes(`${NODE_SECRET}`), 'redactText left the secret');
assert(r.text.includes('[REDACTED]'), 'redactText produced no mask');

// 2. Deep JSON redaction never echoes the secret.
const d = extension.redactDeep({ a: { b: `API_KEY=${NODE_SECRET}` }, c: ['safe'] });
assert.equal(d.ok, true);
assert(!JSON.stringify(d.value).includes(`${NODE_SECRET}`));
assert.equal(d.value.c[0], 'safe');

// 3. Tool-input guardrail: sensitive reads block, ordinary commands pass.
const blocked = extension.guardToolCallInput({
  toolName: 'bash', input: { command: `cat /proc/self/environ ${NODE_SECRET}` },
});
assert(blocked && blocked.block === true);
assert(!blocked.reason.includes(`${NODE_SECRET}`), 'block reason echoed input');
assert.equal(
  extension.guardToolCallInput({ toolName: 'bash', input: { command: 'echo hello' } }),
  null,
);

// 4. Tool-result patch redaction.
const patch = extension.redactToolResultPatch({
  content: [{ type: 'text', text: `TOKEN=${NODE_SECRET}\n` }],
  details: { stderr: `API_KEY=${NODE_SECRET}\n` },
});
assert.equal(patch.failed, false);
assert(!JSON.stringify(patch.patch).includes(`${NODE_SECRET}`));

// 5. Fail-closed result never includes original text.
const failed = extension.failRedactedResult({
  content: [{ type: 'text', text: `RAW ${NODE_SECRET}` }],
  details: { raw: `${NODE_SECRET}` },
});
assert(JSON.stringify(failed).includes('[REDACTION FAILED]'));
assert(!JSON.stringify(failed).includes(`${NODE_SECRET}`));
console.log('NODE_REDACTION_OK');
'''


class NodeExtensionRedactionTests(unittest.TestCase):
    """The exported extension helpers redact synthetic tool results via Node."""

    def setUp(self) -> None:
        if shutil.which("node") is None:
            self.skipTest("node is unavailable on this host")
        self.tmp = Path(tempfile.mkdtemp(prefix="factory-redaction-node."))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.fixture = self.tmp / "fixture.mjs"
        self.fixture.write_text(
            NODE_FIXTURE.replace("__NODE_SECRET__", NODE_SECRET),
            encoding="utf-8",
        )

    def test_exported_tool_call_and_result_redaction(self) -> None:
        # ``--input-type=module`` applies to stdin input only; the fixture
        # is piped through stdin exactly like the visible extension suite.
        # The expected exact committed guard digest is forwarded through the
        # sanitized launch environment the way the trusted pre-spawn
        # authority does, so the extension can bind its worktree guard
        # without any Git access (Task 11 review).
        guard_digest = hashlib.sha256(REAL_GUARD.read_bytes()).hexdigest()
        node_env = dict(os.environ)
        node_env[launch_module.PI_RALPH_GUARD_DIGEST_ENV] = guard_digest
        result = run(
            ["node", "--input-type=module", "-",
             str(ROOT / "scripts" / "pi-ralph-emit-extension.mjs")],
            input_data=self.fixture.read_bytes(),
            check=False,
            env=node_env,
        )
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        self.assertIn(b"NODE_REDACTION_OK", result.stdout)
        self.assertNotIn(NODE_SECRET.encode(), result.stdout)


# ---------------------------------------------------------------------------
# 8. The model-viewed Git shim boundary (fixed absolute candidates)
# ---------------------------------------------------------------------------


class GitShimTests(unittest.TestCase):
    """A caller-controlled PATH cannot redirect the model-facing git shim."""

    SHIM_SOURCE = ROOT / "scripts" / "pi-cli-shims" / "git"

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="factory-git-shim."))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        shims = self.tmp / "shims"
        shims.mkdir()
        self.shim = shims / "git"
        shutil.copy2(self.SHIM_SOURCE, self.shim)
        os.chmod(self.shim, 0o755)
        self.fake_dir = self.tmp / "fake-bin"
        self.fake_dir.mkdir()
        self.marker = self.tmp / "fake-git-ran"
        fake = self.fake_dir / "git"
        fake.write_text(
            "#!/bin/sh\ntouch \"$FAKE_MARKER\"\necho FAKE_GIT_RAN\n",
            encoding="utf-8",
        )
        os.chmod(fake, 0o755)

    def test_fake_path_cannot_redirect(self) -> None:
        env = dict(os.environ)
        env["PATH"] = str(self.fake_dir) + os.pathsep + env.get("PATH", "")
        env["FAKE_MARKER"] = str(self.marker)
        result = subprocess.run(
            [str(self.shim), "--version"],
            env=env, capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 0)
        self.assertIn("git version", result.stdout)
        self.assertNotIn("FAKE_GIT_RAN", result.stdout + result.stderr)
        self.assertFalse(self.marker.exists(),
                         "the fake PATH git executed behind the shim")

    def test_commit_bypass_still_rejected_under_fake_path(self) -> None:
        # PATH still carries the real bash directory so the shebang can
        # launch the shim; the fake ``git`` stays first on PATH and must
        # never be consulted by the pinned resolver.
        env = dict(os.environ)
        env["PATH"] = str(self.fake_dir) + os.pathsep + env.get("PATH", "")
        env["FAKE_MARKER"] = str(self.marker)
        result = subprocess.run(
            [str(self.shim), "commit", "--no-verify", "-m", "x"],
            env=env, capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("--no-verify", result.stderr)
        self.assertFalse(self.marker.exists())

    @unittest.skipUnless(os.geteuid() == 0, "requires root for the live check")
    def test_root_refusal_live(self) -> None:
        result = subprocess.run(
            [str(self.shim), "status"],
            capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("root", result.stderr.lower())

    def test_root_refusal_source_level(self) -> None:
        source = self.SHIM_SOURCE.read_text(encoding="utf-8")
        # The boundary refuses to run as root (caller-owned path components
        # can never be proven immutable) and never consults PATH.
        self.assertIn("UID", source)
        self.assertIn("-eq 0", source)
        self.assertIn("refuses to run as root", source)
        for candidate in ("/usr/bin/git", "/bin/git",
                          "/run/current-system/sw/bin/git",
                          "/nix/store/*/bin/git"):
            self.assertIn(candidate, source)
        self.assertNotIn("for dir in $PATH", source)
        self.assertIn("PATH-resolved", source)

    def test_immutable_chain_validation_present_source_level(self) -> None:
        # Task 11 review: the shim validates root ownership, non-writability,
        # and the immutable Nix chain exactly like the control plane's
        # ``gitutil._immutable_chain`` authority — a caller-owned or
        # group/other-writable candidate (or a symlink redirecting into one)
        # is rejected, never executed.
        source = self.SHIM_SOURCE.read_text(encoding="utf-8")
        self.assertIn("readlink -f", source)
        self.assertIn("stat -Lc '%u %f'", source)
        self.assertIn("boundary", source)
        self.assertIn("/nix/store", source)
        self.assertIn("sticky", source)
        self.assertIn("0x200", source)  # the sticky bit
        self.assertIn("0x12", source)  # group/other write bits
        self.assertIn("validate_pinned_candidate", source)
        self.assertIn("immutable", source)


# ---------------------------------------------------------------------------
# 9. External backend configuration authority
# ---------------------------------------------------------------------------


class ExternalBackendTests(unittest.TestCase):
    """An external model backend is confined and transported only under the
    real confinement + immutable trusted path authority; the private
    synthetic seam rejects it."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="factory-external-backend."))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        # A launch-style fixture: wrapper + backend + guard committed at HEAD.
        self.workspace = self.tmp / "workspace"
        scripts = self.workspace / "scripts"
        scripts.mkdir(parents=True)
        shutil.copy2(ROOT / launch_module.SECURE_WRAPPER,
                     scripts / Path(launch_module.SECURE_WRAPPER).name)
        shutil.copy2(REAL_GUARD, scripts / Path(GUARD_RELPATH).name)
        self.backend = self.workspace / "backend.py"
        self.backend.write_text("#!/usr/bin/env python3\nprint('ok')\n",
                                encoding="utf-8")
        os.chmod(self.backend, 0o700)
        (self.workspace / "plan.md").write_bytes(
            (FIXTURES / "plan-valid-base.md").read_bytes())
        (self.workspace / "spec.md").write_text("spec\n", encoding="utf-8")
        (self.workspace / "role.md").write_text("role\n", encoding="utf-8")
        (self.workspace / "AGENTS.md").write_text("policy\n", encoding="utf-8")
        _git(self.workspace, "init", "-q", "-b", "fixture-main")
        _git(self.workspace, "config", "user.email", "factory@test")
        _git(self.workspace, "config", "user.name", "factory")
        _git(self.workspace, "add", "-A")
        _git(self.workspace, "commit", "-qm", "fixture")
        self.head = _committed_head(self.workspace)
        self.role_prompt = (self.workspace / "role.md").read_bytes()
        self.agents = (self.workspace / "AGENTS.md").read_bytes()
        self.spec = (self.workspace / "spec.md").read_bytes()
        self.plan = (self.workspace / "plan.md").read_bytes()

    def binding(self, backend: Path | None = None) -> InvocationBinding:
        return InvocationBinding(
            role="planner",
            model="synthetic-model",
            provider="synthetic",
            backend=backend or self.backend,
            workspace=self.workspace,
            bound_commit=self.head,
            role_prompt_digest=sha256(self.role_prompt),
            prompt_set_digest=sha256(b"set"),
            plan_digest=sha256(self.plan),
            policy_digest=sha256(self.agents),
            specification_digest=sha256(self.spec),
        )

    def test_backend_is_external_unit(self) -> None:
        external = self.tmp / "outside" / "backend.py"
        external.parent.mkdir()
        self.assertFalse(launch_module._backend_is_external(self.binding()))
        self.assertTrue(
            launch_module._backend_is_external(self.binding(backend=external))
        )
        # A workspace symlink resolving outside the workspace is external;
        # one resolving back inside is not.
        outside = self.tmp / "outside" / "real.py"
        outside.write_text("print('x')\n", encoding="utf-8")
        link_out = self.workspace / "link-out.py"
        link_out.symlink_to(outside)
        self.assertTrue(
            launch_module._backend_is_external(self.binding(backend=link_out))
        )
        link_in = self.workspace / "link-in.py"
        link_in.symlink_to(self.backend.name)
        self.assertFalse(
            launch_module._backend_is_external(self.binding(backend=link_in))
        )

    def test_external_backend_rejected_by_synthetic_seam(self) -> None:
        external = self.tmp / "external-backend.py"
        external.write_text("print('x')\n", encoding="utf-8")
        binding = self.binding(backend=external)
        proof = confinement_module._mint_synthetic_proof(binding)
        with self.assertRaises(launch_module.InvocationError) as caught:
            launch_module.authorize_launch(
                binding,
                role_prompt=self.role_prompt,
                agents=self.agents,
                spec=self.spec,
                plan=self.plan,
                _confinement_proof=proof,
            )
        self.assertIn("external", str(caught.exception).lower())

    def test_external_backend_without_real_confinement_fails_closed(self) -> None:
        external = self.tmp / "external-backend.py"
        external.write_text("print('x')\n", encoding="utf-8")
        binding = self.binding(backend=external)
        with self.assertRaises(launch_module.InvocationError) as caught:
            launch_module.authorize_launch(
                binding,
                role_prompt=self.role_prompt,
                agents=self.agents,
                spec=self.spec,
                plan=self.plan,
            )
        self.assertIn("confinement", str(caught.exception).lower())

    def test_in_workspace_backend_ok_under_synthetic_seam(self) -> None:
        # Task 6's private synthetic-proof path remains authorized for an
        # in-workspace committed backend (the seam never produces real
        # confinement and is never evidence of it).
        binding = self.binding()
        authority = launch_module.authorize_launch(
            binding,
            role_prompt=self.role_prompt,
            agents=self.agents,
            spec=self.spec,
            plan=self.plan,
            _confinement_proof=confinement_module._mint_synthetic_proof(binding),
        )
        self.assertIsInstance(authority, launch_module.LaunchAuthority)


# Reuse the Task 8 confinement suite's real fixture workspace for the real
# confinement + immutable external-path tests (never duplicated).
_CONFINEMENT_SUITE = ROOT / ".factory" / "tests" / "test-factory-confinement.py"
_confinement_spec_mod = importlib.util.spec_from_file_location(
    "factory_confinement_suite", _CONFINEMENT_SUITE)
FACTORY_CONFINEMENT = importlib.util.module_from_spec(_confinement_spec_mod)
assert _confinement_spec_mod.loader is not None
_confinement_spec_mod.loader.exec_module(FACTORY_CONFINEMENT)


class ExternalBackendRealConfinementTests(FACTORY_CONFINEMENT._Base):
    """External backend sources: immutable trusted path + real confinement."""

    @classmethod
    def setUpClass(cls) -> None:
        if not FACTORY_CONFINEMENT.wc.confinement_primitive_available():
            raise unittest.SkipTest(
                "the Landlock LSM is unavailable on this host; the real "
                "confinement external-backend path cannot run"
            )

    def test_external_trusted_executable_accepted_under_real_spec(self) -> None:
        # The pinned Git executable is an immutable Nix-store path (the
        # trusted external-executable class). Under the real confinement
        # authority the external backend is revalidated through the
        # immutable-chain check and accepted.
        external = Path(gitutil.GIT_EXECUTABLE)
        binding = self.binding(role="planner", backend=external)
        home = FACTORY_CONFINEMENT.wc.sanitized_home_directory()
        self.addCleanup(shutil.rmtree, home, ignore_errors=True)
        spec = FACTORY_CONFINEMENT.wc.confinement_spec(
            binding, sanitized_home=home)
        authority = launch_module.authorize_launch(
            binding,
            role_prompt=(self.workspace / "role.md").read_bytes(),
            agents=(self.workspace / "AGENTS.md").read_bytes(),
            spec=(self.workspace / "spec.md").read_bytes(),
            plan=(self.workspace / "plan.md").read_bytes(),
            _confinement_spec=spec,
            _sanitized_home=home,
        )
        self.assertIsInstance(authority, launch_module.LaunchAuthority)
        self.assertFalse(authority._confinement_proof.synthetic)
        resolved = os.path.realpath(str(external))
        self.assertIn(resolved, authority._external_paths)
        self.addCleanup(shutil.rmtree, authority._exec_dir, ignore_errors=True)
        self.addCleanup(
            shutil.rmtree, Path(authority._prompt_path).parent, ignore_errors=True)
        self.addCleanup(
            shutil.rmtree, authority._session_dir, ignore_errors=True)

    def test_untrusted_external_path_fails_closed(self) -> None:
        # A mutable caller-owned external backend (not an immutable store
        # path, not a committed workspace blob) fails the immutable-chain
        # authority before any bytes can run.
        bogus = self.diag / "external-backend.py"
        bogus.write_text("print('x')\n", encoding="utf-8")
        binding = self.binding(role="planner", backend=bogus)
        home = FACTORY_CONFINEMENT.wc.sanitized_home_directory()
        self.addCleanup(shutil.rmtree, home, ignore_errors=True)
        spec = FACTORY_CONFINEMENT.wc.confinement_spec(
            binding, sanitized_home=home)
        with self.assertRaises(launch_module.InvocationError) as caught:
            launch_module.authorize_launch(
                binding,
                role_prompt=(self.workspace / "role.md").read_bytes(),
                agents=(self.workspace / "AGENTS.md").read_bytes(),
                spec=(self.workspace / "spec.md").read_bytes(),
                plan=(self.workspace / "plan.md").read_bytes(),
                _confinement_spec=spec,
                _sanitized_home=home,
            )
        self.assertIn("external", str(caught.exception).lower())


if __name__ == "__main__":
    unittest.main()
