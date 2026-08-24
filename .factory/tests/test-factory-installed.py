#!/usr/bin/env python3
"""Installed-tier evidence suite for the generic harness (Task 20, §19/§3).

The specification (HIDE-01 §3, EVID-01 §19, TEST-01 §22) requires a clean
exact-commit installation of the generic harness into test-owned external
and hidden prefixes, an exact physical-file inventory, and production
control-plane CLIs executed from the installed copy — never relabeled
source-tree or private-unit runs.  This hidden suite proves the installed
tier end to end:

1. the trusted installer (`.factory/loop/installer.py`) stages the
   committed harness into an external prefix and a hidden dot-prefixed
   prefix, both test-owned and outside the repository; committed content is
   staged from exact committed blob bytes and re-proven blob-exact;
   executable modes are preserved; symlink/special-inode/device escapes,
   unsafe paths, and group/other-writable modes fail closed;
2. the physical installed-file inventory (path, mode, owner, link count,
   size, sha256) is captured by the *installed* footprint authority and
   asserted to equal exactly the manifest + shared authorities + operator
   entrypoints — no product/foreign namespace, no product pollution;
3. the production CLIs run **from the installed copy** (module form through
   the external-prefix alias, the external-prefix launcher entry point, and
   the installed receipt wrapper) with a sanitized environment that carries
   no source-tree path, no `.factory-state`/`.ralph`/credential/Git/
   Ollama/campaign-binding variables, and no bytecode writes into the
   installed copy;
4. every installed-tier gate is minted as an exact-commit machine receipt
   bound to the audit coordinator in the fixture authority
   (`.factory-state/audit-coordinator.json` with the exact bound commit);
   a gate that exits nonzero or prints a skip marker is never PASS;
   `scripts/check-audit-receipts.py` exits 0 against the fixture audit
   report and rejects a PASS claim for a failing receipt;
5. the fresh installed-functional evidence gate accepts a commit-bound
   env in the fixture authority and rejects stale/skipped/failing records;
6. the installed copy is byte-identical after every gate (no runtime
   pollution), and the live repository's `.factory-state/` is never
   touched (the shell driver proves foreign-file byte preservation);
7. every installed-tier gate argv/stdout **binds the installed root**: a
   module-form gate is wrapped in an installed-root attestation that
   resolves the loaded `factory.loop` package's module root from the
   operator alias, prints `installed-root: <resolved>`, and fails (exit
   90) when the root is not the installed prefix — so a source-tree
   invocation can never mint an equivalent receipt — while the
   external-prefix launcher entry point binds the prefix by its own
   installed path and cleans up its operator alias on every exit path;
8. a clean-commit simulation (the whole installed surface committed in a
   fixture authority) proves the post-commit install never double-stages a
   declared entrypoint and reports an empty pending set — catching the
   H1 regression before the real Task-20 commit lands.

The live installed-functional evidence for the boilerplate audit base is
**two-phase**: this WIP phase builds and proves the installed-tier
machinery and fixture-authority receipts only — the live
`.factory-state/` foreign evidence is never touched, replaced, or relabeled
fixture — and the post-commit phase (once the Task-20 commit is the bound
commit) runs the installed suite at that exact commit and stages the fresh
live evidence under the generic evidence namespace.  Task 20 therefore
stays `pending` in the plan until that post-commit phase lands.

No external model, runner, hardware, or human is ever invoked: the suite
exercises only deterministic production CLIs of the installed harness, and
the receipts are capped at the ``installed`` evidence tier by the receipt
channel itself.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
LOOP = ROOT / ".factory" / "loop"
STATE_DIR = ".factory-state"
RECEIPTS_DIR = f"{STATE_DIR}/audit-receipts"
INSTALLER = LOOP / "installer.py"
CHECK_AUDIT_RECEIPTS = ROOT / "scripts" / "check-audit-receipts.py"
CHECK_INSTALLED_FUNCTIONAL = ROOT / "scripts" / "check-installed-functional-evidence.sh"
SMOKE_DIR = ROOT / ".factory" / "smoke"
FIXTURES_DIR = ROOT / ".factory" / "tests" / "fixtures"
SMOKE_BRANCH = "fixture-main"
SMOKE_PLAN_REL = ".factory/artifacts/implementation-plan.md"
SMOKE_EVIDENCE_REL = ".factory/artifacts/campaign-smoke-evidence.json"
SMOKE_PHASE_RESULT_REL = ".factory-state/evidence-smoke-phase-result.json"
SMOKE_AUDIT_RESULT_REL = ".factory-state/evidence-smoke-audit-result.json"

sys.path.insert(0, str(LOOP))
import evidence as evidence_module  # noqa: E402
import footprint  # noqa: E402
import gitutil  # noqa: E402
import installer as installer_module  # noqa: E402

sys.path.insert(0, str(SMOKE_DIR))
import evidence_smoke_common as smoke_common  # noqa: E402

sys.path.insert(0, str(FIXTURES_DIR))
import fixture_plan_tool as fixture_plan_tool  # noqa: E402

# The fixture task shape mirrors the canonical plan's pending set (Task 22
# the sole runnable pending task, Task 25 the final audit).
FIXTURE_TASKS: list[dict] = []
for number in range(1, 26):
    if number <= 20:
        status, blocked_on, deps = "complete", None, []
    elif number == 21:
        status, blocked_on, deps = "blocked", "external-human-runner-authority", [20]
    elif number == 22:
        status, blocked_on, deps = "pending", None, [20]
    elif number == 23:
        status, blocked_on, deps = "pending", None, [20, 22]
    elif number == 24:
        status, blocked_on, deps = "pending", None, [20, 22, 23]
    else:
        status, blocked_on, deps = "pending", None, []
    FIXTURE_TASKS.append({
        "number": number,
        "title": f"Fixture task {number}",
        "status": status,
        "priority": 10,
        "dependencies": deps,
        "blocked_on": blocked_on,
        "scope": "fixture-scoped work only.",
        "verification": f"`src/work-{number}.md`",
    })

SHA1 = re.compile(r"^[0-9a-f]{40}$")
SKIP_TOKEN = re.compile(r"(?<!\S)(?:SKIP|SKIPPED)(?!\S)")

# Deterministic digest placeholders for the installed state CLI gate.
DIGEST_A = "a" * 64
DIGEST_B = "b" * 64
DIGEST_C = "c" * 64
DIGEST_D = "d" * 64
DIGEST_E = "e" * 64

# Installed-root attestation: the module-form gates execute under the
# operator alias (`PYTHONPATH=<alias>`), so the loaded `factory.loop`
# package's resolved module root must equal the installed prefix's
# `.factory`.  The bootstrap prints the attestation into the certified
# stdout and exits 90 on a mismatch, so a receipt argv/stdout binds the
# installed prefix and a source-tree invocation can never mint an
# equivalent installed-tier receipt.  The program is one logical line of
# simple statements (a short-circuit expression replaces the conditional)
# with only single spaces — no newlines/tabs/double-spaces — so the
# audit-report evidence line round-trips through the visible checker's
# whitespace-split tokenizer and shlex round-trip.
INSTALLED_ROOT_BOOTSTRAP = (
    "import os, subprocess, sys; from pathlib import Path; import factory.loop; "
    "expected = Path(sys.argv[1]).resolve(); "
    "actual = Path(factory.loop.__file__).resolve().parents[1]; "
    "(actual != expected) and "
    "(print(f'installed-root mismatch: {actual} != {expected}', "
    "file=sys.stderr) or sys.exit(90)); "
    "print(f'installed-root: {actual}'); "
    "raise SystemExit(subprocess.run(sys.argv[2:], env=dict(os.environ)).returncode)"
)

# Minimal stub ``launch.py`` for the factory-launch signal test: writes its
# pid/ready markers, and per mode either delays termination on a forwarded
# signal (1.5 s, then exits 42), ignores the forwarded signals entirely
# (forcing the launcher's bounded KILL escalation), or keeps the default
# signal disposition (so the forwarded signal kills it and the launcher
# reports 128+signal).
LAUNCH_STUB = """\
import os, signal, sys, time
mode = sys.argv[1] if len(sys.argv) > 1 else "delay"
marker = os.environ["LAUNCH_TEST_MARKER"]
with open(marker + ".pid", "w", encoding="utf-8") as stream:
    stream.write(str(os.getpid()))
if mode == "delay":
    def _delayed(signum, frame):
        with open(marker + ".signal", "a", encoding="utf-8") as stream:
            stream.write(str(signum) + "\\n")
        time.sleep(1.5)
        sys.exit(42)
    for _signum in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP, signal.SIGQUIT):
        signal.signal(_signum, _delayed)
elif mode == "ignore":
    for _signum in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP, signal.SIGQUIT):
        signal.signal(_signum, signal.SIG_IGN)
with open(marker + ".ready", "w", encoding="utf-8") as stream:
    stream.write("ready\\n")
time.sleep(300)
"""


def _run(
    argv: list[str],
    *,
    cwd: Path | None = None,
    env: dict | None = None,
    check: bool = True,
    timeout: float = 300,
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


class InstalledTierSuite(unittest.TestCase):
    """End-to-end installed-tier evidence (Task 20)."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="factory-installed."))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.head = gitutil.resolve_head(ROOT)
        self.assertTrue(SHA1.fullmatch(self.head), "cannot resolve the bound commit")
        self.external = self.tmp / "external-prefix"
        self.hidden = self.tmp / ".hidden-prefix"
        self.manifest_ext = self.tmp / "manifest-external.json"
        self.manifest_hidden = self.tmp / "manifest-hidden.json"
        self.alias = self.tmp / "factory-alias"
        self.alias.mkdir()
        os.symlink(self.external / ".factory", self.alias / "factory")
        # The fixture authority: a test-owned git repository whose
        # `.factory-state/audit-coordinator.json` binds round=1, the exact
        # bound commit, and a fresh nonce.
        self.fixture = self.tmp / "authority"
        self.fixture.mkdir()
        self._git("init", "-q", "-b", "boilerplate-develop")
        self._git("config", "user.email", "factory@test")
        self._git("config", "user.name", "factory")
        (self.fixture / "marker").write_text("fixture marker\n", encoding="utf-8")
        self._git("add", "-A")
        self._git("commit", "-qm", "fixture base")
        self.fixture_commit = self._git("rev-parse", "HEAD").stdout.strip()
        self.assertTrue(SHA1.fullmatch(self.fixture_commit))
        state_dir = self.fixture / STATE_DIR
        state_dir.mkdir(mode=0o700)
        os.chmod(state_dir, 0o700)
        self.nonce = hashlib.sha256(os.urandom(32)).hexdigest()
        coordinator = {
            "schema": "ralph-audit-coordinator/v1",
            "round": 1,
            "base_commit": self.head,
            "nonce": self.nonce,
            "created_at": int(time.time()),
        }
        (state_dir / "audit-coordinator.json").write_text(
            json.dumps(coordinator, sort_keys=True) + "\n", encoding="utf-8"
        )
        os.chmod(state_dir / "audit-coordinator.json", 0o600)

    # -- helpers -------------------------------------------------------------

    def _git(self, *args: str) -> subprocess.CompletedProcess[str]:
        return _run([gitutil.GIT_EXECUTABLE, "-C", str(self.fixture), *args])

    def base_env(self) -> dict:
        """A minimal test environment that never writes bytecode."""
        return {
            "PATH": os.environ.get("PATH", ""),
            "HOME": str(self.tmp),
            "TMPDIR": str(self.tmp),
            "LANG": "C.UTF-8",
            "PYTHONDONTWRITEBYTECODE": "1",
        }

    def _make_repo(self, path: Path) -> None:
        path.mkdir()
        _run([gitutil.GIT_EXECUTABLE, "-C", str(path), "init", "-q",
              "-b", "boilerplate-develop"], check=True)
        _run([gitutil.GIT_EXECUTABLE, "-C", str(path), "config",
              "user.email", "factory@test"], check=True)
        _run([gitutil.GIT_EXECUTABLE, "-C", str(path), "config",
              "user.name", "factory"], check=True)

    def _commit_all(self, path: Path, message: str) -> str:
        _run([gitutil.GIT_EXECUTABLE, "-C", str(path), "add", "-A"], check=True)
        _run([gitutil.GIT_EXECUTABLE, "-C", str(path), "commit", "-qm",
              message], check=True)
        return _run([gitutil.GIT_EXECUTABLE, "-C", str(path),
                     "rev-parse", "HEAD"]).stdout.strip()

    def _copy_surface(self, target: Path) -> None:
        """Copy the installed surface (.factory/, .pi/, scripts/) into a
        fixture repository, mirroring the exact working-tree bytes while
        excluding ignored bytecode artifacts (the fixture has no
        ``.gitignore``, so stray ``__pycache__``/``*.pyc`` files would
        otherwise become untracked pending paths)."""
        ignore = shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo")
        for name in (".factory", ".pi", "scripts"):
            shutil.copytree(ROOT / name, target / name, ignore=ignore)

    @property
    def installed_root(self) -> str:
        """The resolved installed module root the gates must bind."""
        return str((self.external / ".factory").resolve())

    def attested_argv(self, argv: list[str]) -> list[str]:
        """Wrap a gate argv so the certified receipt binds the installed
        root: the bootstrap resolves the loaded module root from the
        operator alias and fails closed on any other root."""
        return [
            sys.executable, "-c", INSTALLED_ROOT_BOOTSTRAP,
            self.installed_root, *argv,
        ]

    def sanitized_env(self, *, for_receipt_check: bool = False) -> dict:
        """A minimal environment with zero source-tree or secret surface.

        Only PATH/HOME/TMPDIR/LANG survive; the complete Git/Ollama/campaign/
        credential/session variable families are absent, no value references
        the source repository root, and bytecode writes are disabled so a
        gate can never pollute the installed copy.
        """
        env = {
            "PATH": os.environ.get("PATH", ""),
            "HOME": str(self.tmp),
            "TMPDIR": str(self.tmp),
            "LANG": "C.UTF-8",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONPATH": str(self.alias),
            "FACTORY_INSTALL_RUNTIME": str(self.tmp),
        }
        for key in list(os.environ):
            if key in env:
                continue
            if key.startswith(("GIT_", "OLLAMA_", "FACTORY_CAMPAIGN_AUDIT_",
                               "FACTORY_RALPH_", "FACTORY_LOOP_LAUNCH_")):
                continue
            if key in ("FACTORY_VERIFIER_ROOT", "FACTORY_FINAL_GATE_ATTEST",
                       "FACTORY_PLANNING_BASE_COMMIT", "FACTORY_MAINTENANCE_BASE_COMMIT",
                       "PYTHONSTARTUP", "PYTHONHOME", "PYTHONUSERBASE", "BASH_ENV", "ENV"):
                continue
        root_text = str(ROOT)
        for value in env.values():
            self.assertNotIn(root_text, value,
                             "sanitized environment references the source tree")
        if for_receipt_check:
            env["FACTORY_CAMPAIGN_AUDIT_ROUND"] = "1"
            env["FACTORY_CAMPAIGN_AUDIT_BASE"] = self.head
            env["FACTORY_CAMPAIGN_AUDIT_NONCE"] = self.nonce
        return env

    def install(self, prefix: Path, manifest_out: Path) -> dict:
        result = _run(
            [
                sys.executable, str(INSTALLER), "install",
                "--root", str(ROOT),
                "--commit", self.head,
                "--prefix", str(prefix),
                "--manifest-out", str(manifest_out),
            ],
            cwd=ROOT,
            env=self.base_env(),
        )
        manifest = json.loads(manifest_out.read_text(encoding="utf-8"))
        self.assertEqual(manifest["schema"], "factory-install-manifest/v1")
        self.assertEqual(manifest["commit"], self.head)
        return manifest

    def assert_external_install_clean(self, prefix: Path, manifest: dict) -> None:
        files = [entry["path"] for entry in manifest["files"]]
        shared = [entry["path"] for entry in manifest["shared"]]
        entrypoints = [entry["path"] for entry in manifest["entrypoints"]]
        errors = footprint.verify_external_install(
            ROOT, prefix, manifest=files, entrypoints=entrypoints, shared=shared
        )
        self.assertEqual(errors, [], errors)

    def installed_inventory(self, prefix: Path, manifest_out: Path) -> dict:
        result = _run(
            [
                sys.executable, "-m", "factory.loop.footprint",
                "--installed-inventory", str(prefix),
                "--manifest", str(manifest_out),
            ],
            cwd=str(self.fixture),
            env=self.sanitized_env(),
        )
        data = json.loads(result.stdout)
        self.assertEqual(data["schema"], "factory-installed-inventory/v1")
        return data

    def mint(self, tag: str, argv: list[str], *, check: bool = True,
             env: dict | None = None) -> subprocess.CompletedProcess[str]:
        wrapper = self.external / "scripts" / "machine-receipt.py"
        command = [
            sys.executable, str(wrapper),
            "--root", str(self.fixture),
            "--tag", tag,
            "--audit-round", "1",
            "--evidence-commit", self.head,
            "--nonce", self.nonce,
            "--", *argv,
        ]
        return _run(command, cwd=str(self.fixture),
                    env=env if env is not None else self.sanitized_env(),
                    check=check)

    def assert_pass_receipt(self, tag: str,
                            expected_installed_root: str | None = None) -> dict:
        """A PASS gate: exit 0, no skip marker in the certified stdout, and
        (when given) the certified argv/stdout must bind the installed
        root so a source invocation can never mint an equivalent receipt."""
        ref = f"{RECEIPTS_DIR}/{tag}.json"
        receipt = evidence_module.validate_receipt(self.fixture, ref)
        self.assertEqual(receipt["exit_code"], 0,
                         f"gate {tag} must exit 0 to be certified PASS")
        self.assertEqual(receipt["evidence_commit"], self.head)
        self.assertEqual(receipt["coordinator_round"], 1)
        self.assertEqual(receipt["coordinator_nonce"], self.nonce)
        stdout = (self.fixture / RECEIPTS_DIR / f"{tag}.stdout").read_bytes()
        self.assertIsNone(
            SKIP_TOKEN.search(stdout.decode("utf-8", "replace")),
            f"gate {tag} printed a skip marker; it can never be PASS",
        )
        if expected_installed_root is not None:
            text = stdout.decode("utf-8", "replace")
            self.assertIn(f"installed-root: {expected_installed_root}", text,
                          f"gate {tag} did not attest the installed root")
            resolved = str(Path(expected_installed_root).resolve())
            self.assertIn(resolved, receipt["argv"],
                          f"gate {tag} argv does not bind the installed root")
        return receipt

    # -- the installed tier ------------------------------------------------

    def test_installed_harness_is_blob_exact_and_confined(self) -> None:
        for prefix, manifest_out in (
            (self.external, self.manifest_ext),
            (self.hidden, self.manifest_hidden),
        ):
            manifest = self.install(prefix, manifest_out)
            self.assert_external_install_clean(prefix, manifest)
            # Committed content is blob-exact at the bound commit: every
            # non-pending file's recorded blob is a committed blob and its
            # staged bytes hash back to that blob (the installer re-proved it;
            # the manifest records the oid for the reviewer).
            committed = [e for e in manifest["files"] if not e.get("pending")]
            self.assertTrue(committed)
            for entry in committed:
                self.assertTrue(SHA1.fullmatch(entry["blob"]), entry)
                self.assertEqual(len(entry["sha256"]), 64, entry)
            pending = [e["path"] for e in manifest["files"] if e.get("pending")]
            # Reviewer-mode pending staging is the exact Task-20 authority
            # set and nothing else: a stray or secret-named worktree file
            # under the installed surface is never staged.  A declared
            # shared/entrypoint path that is pending (the operator launcher)
            # is carried by its shared/entrypoint record instead of the
            # committed bulk set.  The expected set is derived
            # *independently* from the live repository (every installed-
            # surface path whose worktree bytes differ from the bound
            # commit, intersected with the reviewer allowlist) rather than
            # hard-coded: the allowlist may shrink (an authority committed
            # or a review-authority test no longer pending) without the
            # assertion becoming fragile, and a fully committed install
            # derives an *empty* pending set (valid post-commit).
            derived_pending = sorted(
                set(installer_module._pending_paths(ROOT, self.head))
                & set(installer_module.PENDING_ALLOWLIST)
            )
            all_pending = sorted(
                e["path"]
                for e in (manifest["files"] + manifest["shared"]
                          + manifest["entrypoints"])
                if e.get("pending")
            )
            self.assertEqual(
                all_pending,
                derived_pending,
                "pending staged set must equal the independently derived "
                "reviewer allowlist set",
            )
            declared_paths = {
                e["path"] for e in manifest["shared"]
            } | {e["path"] for e in manifest["entrypoints"]}
            file_pending = sorted(set(derived_pending) - declared_paths)
            self.assertEqual(sorted(pending), file_pending)
            launcher = next(
                e for e in manifest["entrypoints"]
                if e["path"] == ".factory/bin/factory-launch"
            )
            if ".factory/bin/factory-launch" in derived_pending:
                self.assertTrue(launcher["pending"], launcher)
            else:
                # Post-commit: the launcher is committed content and the
                # clean-commit fixture proves it is never double-staged.
                self.assertFalse(launcher["pending"], launcher)
            # The manifest binds the exact root and prefix the installer
            # staged from (root identity / prefix binding).
            self.assertEqual(Path(manifest["root"]).absolute(), ROOT)
            self.assertEqual(Path(manifest["prefix"]).absolute(), prefix)
            # Every installed directory (prefix included) is private 0700.
            self.assertEqual(stat.S_IMODE(prefix.stat().st_mode), 0o700, prefix)
            for rel, info in footprint._walk_nofollow(prefix, ""):
                if stat.S_ISDIR(info.st_mode):
                    self.assertEqual(stat.S_IMODE(info.st_mode), 0o700, rel)
            # Executable modes are preserved from the committed tree.
            inventory = self.installed_inventory(prefix, manifest_out)
            self.assertEqual(inventory["errors"], [], inventory["errors"])
            self.assertEqual(inventory["count"], inventory["expected_count"])
            by_path = {entry["path"]: entry for entry in inventory["entries"]}
            for entry in manifest["files"]:
                installed = by_path.get(entry["path"])
                self.assertIsNotNone(installed, entry["path"])
                self.assertEqual(installed["mode"], entry["mode"], entry["path"])
                self.assertEqual(installed["owner"], os.getuid(), entry["path"])
                self.assertEqual(installed["link_count"], 1, entry["path"])
                self.assertEqual(installed["sha256"], entry["sha256"], entry["path"])
                if not entry.get("pending"):
                    # exact committed blob: git hash-object of the installed
                    # bytes equals the committed blob id.
                    blob = _run(
                        [gitutil.GIT_EXECUTABLE, "-C", str(ROOT), "hash-object",
                         str(prefix / entry["path"])]
                    ).stdout.strip()
                    self.assertEqual(blob, entry["blob"], entry["path"])
            # No product pollution and only allowed prefixes.
            for entry in inventory["entries"]:
                first = entry["path"].split("/", 1)[0]
                self.assertNotIn(first, footprint.PRODUCT_POLLUTION_NAMESPACES,
                                 entry["path"])
                self.assertTrue(
                    first in footprint.HIDDEN_NAMESPACE_SET or first == "scripts",
                    entry["path"],
                )
            self.assertTrue(
                str(prefix).startswith(str(self.tmp)),
                "installed prefix must be test-owned",
            )
            self.assertTrue(str(prefix).startswith("/"), "prefix must be absolute")

    def test_installed_copy_has_no_symlink_or_special_inode(self) -> None:
        manifest = self.install(self.external, self.manifest_ext)
        for rel, info in footprint._walk_nofollow(self.external, ""):
            self.assertFalse(stat.S_ISLNK(info.st_mode), rel)
            self.assertTrue(
                stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode), rel
            )
        for entry in manifest["files"] + manifest["shared"] + manifest["entrypoints"]:
            self.assertFalse(
                (self.external / entry["path"]).is_symlink(), entry["path"]
            )

    def test_installer_rejects_unsafe_prefixes(self) -> None:
        head = self.head
        with self.assertRaises(AssertionError):
            _run([sys.executable, str(INSTALLER), "install", "--root", str(ROOT),
                  "--commit", head, "--prefix", "relative", "--manifest-out",
                  str(self.tmp / "m.json")], check=True)
        with self.assertRaises(AssertionError):
            _run([sys.executable, str(INSTALLER), "install", "--root", str(ROOT),
                  "--commit", head, "--prefix", str(ROOT / "installed-into-repo"),
                  "--manifest-out", str(self.tmp / "m.json")], check=True)
        existing = self.tmp / "existing"
        existing.mkdir()
        (existing / "planted").write_text("x", encoding="utf-8")
        with self.assertRaises(AssertionError):
            _run([sys.executable, str(INSTALLER), "install", "--root", str(ROOT),
                  "--commit", head, "--prefix", str(existing),
                  "--manifest-out", str(self.tmp / "m.json")], check=True)

    def test_installed_gates_run_from_the_installed_copy(self) -> None:
        self.install(self.external, self.manifest_ext)
        self.install(self.hidden, self.manifest_hidden)
        plan_path = self.external / ".factory/artifacts/implementation-plan.md"
        plan_text = plan_path.read_text(encoding="utf-8")
        plan_base = next(
            line.split(":", 1)[1].strip()
            for line in plan_text.splitlines()
            if line.startswith("base_commit:")
        )
        self.assertTrue(SHA1.fullmatch(plan_base), plan_base)
        self.assertTrue(plan_path.is_file())

        gates: list[tuple[str, list[str]]] = [
            (
                "gate-launch-module",
                [sys.executable, "-m", "factory.loop.launch", "--help"],
            ),
            (
                "gate-launch-entrypoint",
                [str(self.external / ".factory/bin/factory-launch"), "--help"],
            ),
            (
                "gate-launch-excerpt",
                [
                    sys.executable, "-m", "factory.loop.launch", "excerpt",
                    "--plan", str(plan_path), "--task-id", "20",
                ],
            ),
            (
                "gate-campaign",
                [sys.executable, "-m", "factory.loop.campaign", "--help"],
            ),
            (
                "gate-parser",
                [
                    sys.executable, "-m", "factory.loop.plan_parser", "parse",
                    str(plan_path),
                ],
            ),
            (
                "gate-selector",
                [
                    sys.executable, "-m", "factory.loop.selector", "select",
                    str(plan_path), "--bound-base-commit", plan_base,
                ],
            ),
            (
                "gate-state-init",
                [
                    sys.executable, "-m", "factory.loop.state",
                    "--root", str(self.fixture), "init",
                    "--campaign-id", "installed-smoke",
                    "--rounds", "1",
                    "--base-commit", self.fixture_commit,
                    "--spec-digest", DIGEST_A,
                    "--plan-digest", DIGEST_B,
                    "--audit-digest", DIGEST_E,
                    "--role-digest", f"planner={DIGEST_C}",
                    "--role-digest", f"developer={DIGEST_D}",
                    "--branch", "boilerplate-develop",
                ],
            ),
            (
                "gate-state-show",
                [
                    sys.executable, "-m", "factory.loop.state",
                    "--root", str(self.fixture), "show",
                ],
            ),
            (
                "gate-inventory-external",
                [
                    sys.executable, "-m", "factory.loop.footprint",
                    "--installed-inventory", str(self.external),
                    "--manifest", str(self.manifest_ext),
                ],
            ),
            (
                "gate-inventory-hidden",
                [
                    sys.executable, "-m", "factory.loop.footprint",
                    "--installed-inventory", str(self.hidden),
                    "--manifest", str(self.manifest_hidden),
                ],
            ),
            (
                "gate-receipt-wrapper",
                [
                    sys.executable, str(self.external / "scripts" / "machine-receipt.py"),
                    "--help",
                ],
            ),
        ]
        for tag, argv in gates:
            for item in argv:
                self.assertFalse(item.startswith(str(ROOT)),
                                 f"gate {tag} references the source tree: {item}")
            # Every installed-tier gate runs **from the installed copy**: the
            # module-form gates are wrapped in the installed-root
            # attestation (so the certified argv/stdout bind the installed
            # prefix), and the entrypoint gate binds the prefix by its own
            # installed path.
            attested = self.attested_argv(argv)
            minted = self.mint(tag, attested)
            self.assertIn(f"[receipt: {RECEIPTS_DIR}/{tag}.json]", minted.stdout)
            self.assert_pass_receipt(tag, expected_installed_root=self.installed_root)

        # The external-prefix launcher cleaned up its operator alias
        # directory on every exit path (no exec leak).
        leftovers = list(Path(self.tmp).glob("factory-launch-alias.*"))
        self.assertEqual(
            leftovers, [],
            "factory-launch leaked its operator alias directory",
        )

        # Every installed-tier gate carries an exact-commit receipt; the
        # fixture audit report reproduces the exact (attested) argv and the
        # visible checker accepts every receipt (PASS requires exit 0).
        report = self.fixture / "audit-report.md"
        lines = []
        for tag, argv in gates:
            attested = self.attested_argv(argv)
            command = " ".join(shlex.quote(item) for item in attested)
            lines.append(
                f"- Executable evidence: `{command}` PASS "
                f"[receipt: {RECEIPTS_DIR}/{tag}.json]"
            )
        report.write_text(
            "result: pass\n\n## Evidence reviewed\n\n" + "\n".join(lines) + "\n",
            encoding="utf-8",
        )
        check = _run(
            [sys.executable, str(CHECK_AUDIT_RECEIPTS), "--root", str(self.fixture),
             str(report)],
            cwd=str(self.fixture),
            env=self.sanitized_env(for_receipt_check=True),
        )
        self.assertIn("valid", check.stdout)

        # Running every gate left the installed copy byte-identical: the
        # re-captured inventory is still exact and clean.
        for prefix, manifest_out in (
            (self.external, self.manifest_ext),
            (self.hidden, self.manifest_hidden),
        ):
            after = self.installed_inventory(prefix, manifest_out)
            self.assertEqual(after["errors"], [], after["errors"])
            self.assertEqual(after["count"], after["expected_count"])

    def test_no_pass_when_gate_skips_or_fails(self) -> None:
        self.install(self.external, self.manifest_ext)
        # A failing command mints a receipt with a non-zero exit; the suite
        # never certifies it PASS, and the visible checker rejects a PASS
        # claim while accepting an honest FAIL claim.
        fail_argv = [
            sys.executable, "-c",
            "import sys; print('expected installed-tier failure'); sys.exit(7)",
        ]
        fail_mint = self.mint("gate-fail", fail_argv, check=False)
        self.assertIn("[receipt: .factory-state/audit-receipts/gate-fail.json]",
                      fail_mint.stdout)
        fail_receipt = evidence_module.validate_receipt(
            self.fixture, f"{RECEIPTS_DIR}/gate-fail.json"
        )
        self.assertNotEqual(fail_receipt["exit_code"], 0)
        with self.assertRaises(AssertionError):
            self.assert_pass_receipt("gate-fail")
        fail_command = " ".join(shlex.quote(item) for item in fail_argv)
        bad_report = self.fixture / "bad-pass.md"
        bad_report.write_text(
            "result: pass\n\n## Evidence reviewed\n\n"
            f"- Executable evidence: `{fail_command}` PASS "
            "[receipt: .factory-state/audit-receipts/gate-fail.json]\n",
            encoding="utf-8",
        )
        with self.assertRaises(AssertionError):
            _run(
                [sys.executable, str(CHECK_AUDIT_RECEIPTS), "--root",
                 str(self.fixture), str(bad_report)],
                cwd=str(self.fixture),
                env=self.sanitized_env(for_receipt_check=True),
            )
        honest_report = self.fixture / "honest-fail.md"
        honest_report.write_text(
            "result: findings\n\n## Evidence reviewed\n\n"
            f"- Executable evidence: `{fail_command}` FAIL "
            "[receipt: .factory-state/audit-receipts/gate-fail.json]\n",
            encoding="utf-8",
        )
        check = _run(
            [sys.executable, str(CHECK_AUDIT_RECEIPTS), "--root",
             str(self.fixture), str(honest_report)],
            cwd=str(self.fixture),
            env=self.sanitized_env(for_receipt_check=True),
        )
        self.assertIn("valid", check.stdout)

        # A command that prints a skip marker exits 0 but is never PASS: the
        # suite's gate loop fails closed on the skip token in the stdout.
        skip_argv = [sys.executable, "-c", "print('SKIPPED')"]
        skip_mint = self.mint("gate-skip", skip_argv)
        self.assertEqual(skip_mint.returncode, 0)
        skip_receipt = evidence_module.validate_receipt(
            self.fixture, f"{RECEIPTS_DIR}/gate-skip.json"
        )
        self.assertEqual(skip_receipt["exit_code"], 0)
        with self.assertRaises(AssertionError):
            self.assert_pass_receipt("gate-skip")

    def test_fresh_installed_functional_evidence_gate(self) -> None:
        """The fresh generic installed-functional gate accepts only the
        exact-HEAD generic namespace backed by a matching installed-harness
        receipt and rejects stale/skipped/tampered records; the foreign
        root env is never read."""
        fixture = self.tmp / "ifx"
        self._make_repo(fixture)
        ignore = shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo")
        for name in ("scripts", ".factory"):
            shutil.copytree(ROOT / name, fixture / name, ignore=ignore)
        # A deterministic stub installed-harness suite so the receipt wrapper
        # executes a clean pass; the real suite runs in
        # test-factory-generic-evidence.py.
        suite = fixture / ".factory/tests/test-factory-installed.sh"
        suite.write_text(
            "#!/usr/bin/env bash\n"
            "set -euo pipefail\n"
            "echo 'test-factory-installed: all checks passed'\n",
            encoding="utf-8",
        )
        suite.chmod(0o755)
        # The runtime evidence namespace is ignored exactly like the live
        # repository (the generic installed-functional gate requires a clean
        # tree, so the fixture's own evidence must never be untracked).
        (fixture / ".gitignore").write_text(
            ".factory-state/\n__pycache__/\n*.py[cod]\n", encoding="utf-8")
        (fixture / "marker").write_text("x\n", encoding="utf-8")
        _run([gitutil.GIT_EXECUTABLE, "-C", str(fixture), "add", "-A"], check=True)
        _run([gitutil.GIT_EXECUTABLE, "-C", str(fixture), "commit", "-qm",
              "fixture"], check=True)
        commit = _run([gitutil.GIT_EXECUTABLE, "-C", str(fixture),
                       "rev-parse", "HEAD"]).stdout.strip()
        state_dir = fixture / STATE_DIR
        state_dir.mkdir(mode=0o700)
        os.chmod(state_dir, 0o700)
        # The fixture audit coordinator binds round 1 and the exact commit.
        nonce = hashlib.sha256(os.urandom(32)).hexdigest()
        (state_dir / "audit-coordinator.json").write_text(
            json.dumps({
                "schema": "ralph-audit-coordinator/v1",
                "round": 1,
                "base_commit": commit,
                "nonce": nonce,
                "created_at": int(time.time()),
            }, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        os.chmod(state_dir / "audit-coordinator.json", 0o600)
        # The installed-harness receipt is minted through the real wrapper
        # (the stub suite exits 0) — never hand-fabricated.
        receipt_ref = f"{RECEIPTS_DIR}/installed-harness-smoke.json"
        minted = _run(
            [sys.executable, str(fixture / "scripts" / "machine-receipt.py"),
             "--root", str(fixture), "--tag", "installed-harness-smoke",
             "--audit-round", "1", "--evidence-commit", commit,
             "--nonce", nonce, "--", "./.factory/tests/test-factory-installed.sh"],
            cwd=str(fixture), env=self.base_env(),
        )
        self.assertIn(f"[receipt: {receipt_ref}]", minted.stdout)
        # The generic evidence record binds the receipt digest/commit/coordinator.
        namespace = state_dir / "generic-evidence" / commit
        namespace.mkdir(parents=True)
        os.chmod(namespace, 0o700)
        record_path = namespace / "installed-functional.json"
        record_path.write_text(
            json.dumps({
                "schema": "factory-generic-installed-functional/v1",
                "commit": commit,
                "test": "test_installed_functional",
                "result": "PASS",
                "skipped": 0,
                "receipt": receipt_ref,
                "receipt_sha256": _sha256((fixture / receipt_ref).read_bytes()),
                "suite_stdout_sha256": _sha256(
                    (fixture / f"{RECEIPTS_DIR}/installed-harness-smoke.stdout").read_bytes()
                ),
                "coordinator_round": 1,
                "coordinator_nonce": nonce,
            }, sort_keys=True, indent=2) + "\n",
            encoding="utf-8",
        )
        os.chmod(record_path, 0o600)
        checker = ["bash", str(fixture / "scripts" /
                               "check-installed-functional-evidence.sh")]
        ok = _run(checker, cwd=str(fixture))
        self.assertIn("PASS", ok.stdout)
        # A foreign root env is ignored: it never satisfies the exact-HEAD
        # generic gate and is never rewritten.
        foreign = state_dir / "installed-functional-evidence.env"
        foreign.write_text(
            "schema=factory-installed-functional/v1\n"
            f"commit={commit}\n"
            "test=test_installed_functional\n"
            "result=PASS\n"
            "skipped=0\n",
            encoding="utf-8",
        )
        ok = _run(checker, cwd=str(fixture))
        self.assertIn("PASS", ok.stdout)
        # A skipped record is rejected.
        record = json.loads(record_path.read_text(encoding="utf-8"))
        record["skipped"] = 1
        record_path.write_text(json.dumps(record, sort_keys=True, indent=2) + "\n",
                               encoding="utf-8")
        os.chmod(record_path, 0o600)
        with self.assertRaises(AssertionError):
            _run(checker, cwd=str(fixture))
        record["skipped"] = 0
        record_path.write_text(json.dumps(record, sort_keys=True, indent=2) + "\n",
                               encoding="utf-8")
        os.chmod(record_path, 0o600)
        # A stale commit is rejected (even though a valid foreign root env
        # and a valid receipt exist).
        record["commit"] = "0" * 40
        record_path.write_text(json.dumps(record, sort_keys=True, indent=2) + "\n",
                               encoding="utf-8")
        os.chmod(record_path, 0o600)
        with self.assertRaises(AssertionError):
            _run(checker, cwd=str(fixture))
        # A missing exact-HEAD namespace fails even with the foreign root env.
        shutil.rmtree(namespace)
        with self.assertRaises(AssertionError):
            _run(checker, cwd=str(fixture))

    # -- installed evidence-smoke campaign (Task 23/A) -------------------------

    def _build_installed_smoke_fixture(self) -> tuple[Path, str]:
        """An isolated committed fixture repository shaped like the canonical
        one: the full hidden loop surface, prompts, audit-objective registry,
        schemas, the designated smoke seam/gate/operator, the policy
        authorities, and the **real** git-commit-guard hooks, with a 25-task
        fixture plan (Task 22 pending, Task 25 the final audit) bound to the
        fixture's own commit/blob.  Returns ``(workspace, head)``."""
        ws = self.tmp / "smoke-fixture"
        ws.mkdir()
        _run([gitutil.GIT_EXECUTABLE, "-C", str(ws), "init", "-q",
              "-b", SMOKE_BRANCH])
        _run([gitutil.GIT_EXECUTABLE, "-C", str(ws), "config",
              "user.email", "factory@test"])
        _run([gitutil.GIT_EXECUTABLE, "-C", str(ws), "config",
              "user.name", "factory"])
        for rel in ("docs", "scripts", "src",
                    ".factory/prompts", ".factory/audit-objectives",
                    ".factory/artifacts", ".factory/schemas",
                    ".factory/smoke", ".factory/loop"):
            (ws / rel).mkdir(parents=True)
        for module in sorted(LOOP.glob("*.py")):
            shutil.copy2(module, ws / ".factory/loop" / module.name)
        for name in ("factory-plan-v1.requirements.json",
                     "factory-campaign-result-v1.schema.json",
                     "factory-phase-result-v1.schema.json"):
            shutil.copy2(ROOT / ".factory" / "schemas" / name,
                         ws / ".factory" / "schemas" / name)
        shutil.copy2(
            ROOT / ".factory/audit-objectives/registry.json",
            ws / ".factory/audit-objectives/registry.json",
        )
        for role in ("planner", "developer", "tester", "auditor"):
            (ws / ".factory/prompts" / f"{role}.md").write_text(
                f"# {role} fixture role prompt\n", encoding="utf-8")
        (ws / ".factory/config.toml").write_text(
            "[project]\n"
            'spec = "docs/FACTORY-LOOP-SPEC.md"\n'
            'plan = ".factory/artifacts/implementation-plan.md"\n'
            f'development_branch = "{SMOKE_BRANCH}"\n'
            'release_branch = "main"\n',
            encoding="utf-8",
        )
        shutil.copy2(ROOT / "docs/FACTORY-LOOP-SPEC.md",
                     ws / "docs/FACTORY-LOOP-SPEC.md")
        shutil.copy2(ROOT / ".factory/generic-leak-allowlist",
                     ws / ".factory/generic-leak-allowlist")
        for policy in ("campaign-receipt-policy.json", "requirement-policy.json",
                       "capability-contracts.json"):
            shutil.copy2(ROOT / ".factory" / policy,
                         ws / ".factory" / policy)
        for name in ("evidence_smoke_common.py", "evidence_smoke_driver.py",
                     "evidence_smoke_gate.py", "evidence_smoke.py"):
            shutil.copy2(SMOKE_DIR / name, ws / ".factory/smoke" / name)
        for name in ("evidence_smoke_driver.py", "evidence_smoke_gate.py",
                     "evidence_smoke.py"):
            os.chmod(ws / ".factory/smoke" / name, 0o755)
        for script in ("factory_state_io.py", "credential-guard.py",
                       "check-plan-freshness.sh", "check-generic-leakage.sh",
                       "check-docs-sync.sh"):
            shutil.copy2(ROOT / "scripts" / script, ws / "scripts" / script)
        shutil.copy2(ROOT / "scripts/git-commit-guard.sh",
                     ws / "scripts/git-commit-guard.sh")
        shutil.copy2(ROOT / "scripts/install-git-commit-guard.sh",
                     ws / "scripts/install-git-commit-guard.sh")
        os.chmod(ws / "scripts/git-commit-guard.sh", 0o755)
        os.chmod(ws / "scripts/install-git-commit-guard.sh", 0o755)
        (ws / "scripts/verify-boilerplate.sh").write_text(
            "#!/usr/bin/env bash\n"
            "set -euo pipefail\n"
            "echo 'fixture verifier stub'\n", encoding="utf-8")
        os.chmod(ws / "scripts/verify-boilerplate.sh", 0o755)
        (ws / ".gitignore").write_text(
            ".factory-state/\n__pycache__/\n*.pyc\n", encoding="utf-8")
        # The real Git commit boundary: the production guard and its six
        # launcher hooks are installed before any fixture commit, so every
        # campaign commit (and fixture commit) runs through it.
        _run(["bash", str(ws / "scripts/install-git-commit-guard.sh")],
             cwd=str(ws))
        _run([gitutil.GIT_EXECUTABLE, "-C", str(ws), "add", "-A"])
        _run([gitutil.GIT_EXECUTABLE, "-C", str(ws), "commit", "-qm",
              "smoke fixture base"])
        head1 = _run([gitutil.GIT_EXECUTABLE, "-C", str(ws),
                      "rev-parse", "HEAD"]).stdout.strip()
        spec_blob = _run([gitutil.GIT_EXECUTABLE, "-C", str(ws), "rev-parse",
                          f"HEAD:docs/FACTORY-LOOP-SPEC.md"]).stdout.strip()
        spec = ws / "fixture-spec.json"
        spec.write_text(json.dumps({
            "spec_path": "docs/FACTORY-LOOP-SPEC.md",
            "spec_commit": head1,
            "spec_blob": spec_blob,
            "base_commit": head1,
            "lifecycle": "active",
            "tasks": FIXTURE_TASKS,
        }), encoding="utf-8")
        _run([sys.executable, str(FIXTURES_DIR / "fixture_plan_tool.py"),
              "--spec", str(spec),
              "--registry", str(ROOT / ".factory/schemas/factory-plan-v1.requirements.json"),
              "--out", str(ws / SMOKE_PLAN_REL)],
             cwd=str(ws))
        spec.unlink()
        _run([gitutil.GIT_EXECUTABLE, "-C", str(ws), "add", "-A"])
        _run([gitutil.GIT_EXECUTABLE, "-C", str(ws), "commit", "-qm",
              "smoke fixture plan and seam"])
        head2 = _run([gitutil.GIT_EXECUTABLE, "-C", str(ws),
                      "rev-parse", "HEAD"]).stdout.strip()
        self.assertTrue(SHA1.fullmatch(head2))
        return ws, head2

    def test_installed_evidence_smoke_campaign_runs_from_installed_copy(self) -> None:
        """One full 4-phase evidence-smoke campaign (planning -> implementation
        -> verification -> audit) runs against an isolated committed fixture
        repository with the real Git commit boundary, driven by the
        **installed** control-plane copy.  The certified receipt argv/stdout
        bind the installed root (a source-tree invocation can never mint an
        equivalent receipt) and carry the phase/state/digest summary: the
        campaign result JSON with the exact four-phase history, the phase
        plan/result digests, the terminal state, and the state digests."""
        self.install(self.external, self.manifest_ext)
        fixture, head = self._build_installed_smoke_fixture()
        campaign_id = smoke_common.smoke_campaign_id(head)
        gate = "./" + smoke_common.GATE_REL
        acceptance = [gate, "--root", str(fixture), "--evidence",
                      smoke_common.DESIGNATED_EVIDENCE_REL, "--task", "22",
                      "--campaign-id", campaign_id, "--mode", "acceptance"]
        verification = [gate, "--root", str(fixture), "--evidence",
                        smoke_common.DESIGNATED_EVIDENCE_REL, "--task", "22",
                        "--campaign-id", campaign_id, "--mode", "verify"]
        run_argv = [
            sys.executable, "-m", "factory.loop.campaign",
            "--root", str(fixture),
            "run",
            "--campaign-id", campaign_id,
            "--rounds", "1",
            "--branch", SMOKE_BRANCH,
            "--provider", "synthetic",
            "--model", "fixture-model",
            "--role-driver", smoke_common.DESIGNATED_DRIVER_REL,
            "--developer-evidence-path", smoke_common.DESIGNATED_EVIDENCE_REL,
            "--phase-result", smoke_common.PHASE_RESULT_REL,
            "--audit-result", smoke_common.AUDIT_RESULT_REL,
            "--role-timeout", "900",
            "--gate-timeout", "1800",
            "--acceptance-command", json.dumps(acceptance),
            "--verification-command", json.dumps(verification),
            "--evidence-smoke",
            "--evidence-bound-commit", head,
        ]
        attested = self.attested_argv(run_argv)
        tag = "installed-smoke-campaign"
        minted = self.mint(tag, attested)
        self.assertIn(f"[receipt: {RECEIPTS_DIR}/{tag}.json]", minted.stdout)
        self.assert_pass_receipt(tag, expected_installed_root=self.installed_root)
        stdout = (self.fixture / RECEIPTS_DIR / f"{tag}.stdout").read_bytes()
        text = stdout.decode("utf-8", "replace")
        # The certified stdout binds the installed root: the module-form
        # campaign resolved its `factory.loop` package from the installed
        # prefix (a source-tree invocation would have exited 90).
        self.assertIn(f"installed-root: {self.installed_root}", text)
        # No source module resolution: the campaign's own output is a
        # machine-readable result JSON (never a traceback importing the
        # source tree) and the installed-root attestation is in the same
        # certified stdout.
        summary, _ = json.JSONDecoder().raw_decode(
            text[text.index("{"):]
        )
        self.assertEqual(summary["schema"], "factory-campaign-result/v1")
        self.assertEqual(summary["campaign_id"], campaign_id)
        self.assertEqual(summary["terminal_phase"], "success")
        self.assertEqual(summary["terminal_outcome"], "pass")
        self.assertEqual(summary["rounds_completed"], 1)
        # The pre-campaign evidence-bound commit (`head`) and the campaign's
        # final head are distinct: the trusted planner/developer roles commit
        # the plan revision and the completed evidence task during the round,
        # advancing the repository HEAD beyond the bound commit the receipt
        # binds.  The final head must be the actual repo HEAD after the
        # campaign and the evidence-bound commit must be an ancestor of it
        # (the campaign never re-binds or rewrites the evidence it observed).
        final_head = _run(
            [gitutil.GIT_EXECUTABLE, "-C", str(fixture), "rev-parse", "HEAD"],
            cwd=str(fixture),
        ).stdout.strip()
        self.assertTrue(SHA1.fullmatch(final_head), final_head)
        self.assertEqual(
            summary["head_commit"], final_head,
            "the campaign result head_commit must be the actual repo HEAD "
            "after the trusted planner/developer commits",
        )
        ancestor = _run(
            [gitutil.GIT_EXECUTABLE, "-C", str(fixture),
             "merge-base", "--is-ancestor", head, final_head],
            cwd=str(fixture),
        )
        self.assertEqual(
            ancestor.returncode, 0,
            f"the pre-campaign evidence-bound commit {head} must be an "
            f"ancestor of the final head {final_head}",
        )
        # Phase/state/digest summary: the exact four-phase round with every
        # phase's outcome and digest fields.
        history = [
            (record["phase"], record["outcome"])
            for record in summary["phase_history"]
        ]
        self.assertEqual(
            history,
            [("planning", "planned"),
             ("implementation", "task_completed"),
             ("verification", "pass"),
             ("audit", "pass")],
        )
        for record in summary["phase_history"]:
            self.assertTrue(SHA1.fullmatch(record["head_commit"]), record)
            self.assertEqual(len(record["plan_digest"]), 64, record)
        # The evidence-smoke round left the exact task complete in the plan
        # and the final audit task pending (the round proves one full phase
        # cycle, not acceptance).
        committed_plan = _run(
            [gitutil.GIT_EXECUTABLE, "-C", str(fixture), "show",
             f"{head}:.factory/artifacts/implementation-plan.md"],
            cwd=str(fixture),
        ).stdout.encode("utf-8")
        self.assertIn("## Task 22: Fixture task 22", committed_plan.decode("utf-8"))
        self.assertIn("- Status: complete", committed_plan.decode("utf-8"))
        # The installed copy is byte-identical after the campaign.
        after = self.installed_inventory(self.external, self.manifest_ext)
        self.assertEqual(after["errors"], [], after["errors"])

    def test_clean_committed_install_never_double_stages(self) -> None:
        """H1 regression: once the Task-20 authorities are committed, a
        clean exact-commit install must never double-stage a declared
        entrypoint and must report an empty pending set.  This simulates
        the post-commit state before the real Task-20 commit lands."""
        fixture = self.tmp / "clean-commit"
        self._make_repo(fixture)
        self._copy_surface(fixture)
        commit = self._commit_all(fixture, "surface committed")
        self.assertTrue(SHA1.fullmatch(commit))
        prefix = self.tmp / "clean-prefix"
        manifest_out = self.tmp / "clean-manifest.json"
        _run(
            [
                sys.executable, str(INSTALLER), "install",
                "--root", str(fixture),
                "--commit", commit,
                "--prefix", str(prefix),
                "--manifest-out", str(manifest_out),
            ],
            cwd=ROOT,
            env=self.base_env(),
        )
        manifest = json.loads(manifest_out.read_text(encoding="utf-8"))
        self.assertEqual(manifest["schema"], "factory-install-manifest/v1")
        self.assertEqual(manifest["commit"], commit)
        pending = [e["path"] for e in manifest["files"] if e.get("pending")]
        self.assertEqual(
            pending, [],
            "a fully committed install must have an empty pending set",
        )
        files = [e["path"] for e in manifest["files"]]
        entrypoints = [e["path"] for e in manifest["entrypoints"]]
        shared = [e["path"] for e in manifest["shared"]]
        # The entrypoint appears exactly once, as an entrypoint record —
        # never double-staged into the committed bulk set.
        self.assertNotIn(".factory/bin/factory-launch", files)
        self.assertIn(".factory/bin/factory-launch", entrypoints)
        self.assertIn("scripts/machine-receipt.py", entrypoints)
        self.assertIn("scripts/factory_state_io.py", shared)
        self.assertTrue((prefix / ".factory/bin/factory-launch").is_file())
        self.assert_external_install_clean(prefix, manifest)
        errors = installer_module.verify_staged(fixture, prefix, manifest)
        self.assertEqual(errors, [], errors)

    def test_pending_staging_allowlist_rejects_strays_and_secrets(self) -> None:
        """Reviewer-mode pending staging is the exact allowlist: a stray or
        secret-named worktree file under the installed surface fails closed
        and the created prefix is rolled back identity-safely."""
        fixture = self.tmp / "stray"
        self._make_repo(fixture)
        self._copy_surface(fixture)
        commit = self._commit_all(fixture, "surface committed")
        prefix = self.tmp / "stray-prefix"
        manifest_out = self.tmp / "stray-manifest.json"
        command = [
            sys.executable, str(INSTALLER), "install",
            "--root", str(fixture),
            "--commit", commit,
            "--prefix", str(prefix),
            "--manifest-out", str(manifest_out),
        ]
        # A stray untracked file under .factory/ is never staged.
        (fixture / ".factory" / "loop" / "rogue_file.py").write_text(
            "x\n", encoding="utf-8"
        )
        failed = _run(command, cwd=ROOT, env=self.base_env(), check=False)
        self.assertNotEqual(failed.returncode, 0)
        self.assertIn("not allowlisted", failed.stderr)
        self.assertFalse(prefix.exists(),
                         "failed install must roll back the created prefix")
        os.unlink(fixture / ".factory" / "loop" / "rogue_file.py")
        # A secret-named pending file is never staged.
        (fixture / ".factory" / "loop" / "secret_token.py").write_text(
            "x\n", encoding="utf-8"
        )
        failed = _run(command, cwd=ROOT, env=self.base_env(), check=False)
        self.assertNotEqual(failed.returncode, 0)
        self.assertIn("not allowlisted", failed.stderr)
        self.assertFalse(prefix.exists(),
                         "failed install must roll back the created prefix")
        os.unlink(fixture / ".factory" / "loop" / "secret_token.py")
        # The declared entrypoint surface is enforced: a shared authority
        # outside .factory/.pi/scripts fails closed before any staging.
        with self.assertRaises(installer_module.InstallerError):
            installer_module.install_harness(
                fixture, self.tmp / "bad-shared-prefix", commit,
                shared=("src/evil.py",),
                entrypoints=installer_module.DEFAULT_ENTRYPOINTS,
            )
        self.assertFalse((self.tmp / "bad-shared-prefix").exists())

    def test_source_invocation_cannot_mint_equivalent(self) -> None:
        """A receipt argv/stdout must bind the installed prefix: running the
        same gate under a source-tree alias resolves a different module root,
        so the attestation fails and no equivalent installed-tier receipt can
        ever be minted from the source tree."""
        self.install(self.external, self.manifest_ext)
        source_alias = self.tmp / "source-alias"
        source_alias.mkdir()
        os.symlink(ROOT / ".factory", source_alias / "factory")
        env = self.sanitized_env()
        env["PYTHONPATH"] = str(source_alias)
        gate = [sys.executable, "-m", "factory.loop.launch", "--help"]
        argv = self.attested_argv(gate)
        result = _run(argv, cwd=str(self.fixture), env=env, check=False)
        self.assertNotEqual(result.returncode, 0,
                            "source-tree module root must fail the attestation")
        self.assertIn("installed-root mismatch", result.stderr)
        # Even through the receipt wrapper the failing gate is never PASS.
        minted = self.mint("gate-source-equiv", argv, check=False, env=env)
        self.assertEqual(minted.returncode, 90)
        receipt = evidence_module.validate_receipt(
            self.fixture, f"{RECEIPTS_DIR}/gate-source-equiv.json"
        )
        self.assertEqual(receipt["exit_code"], 90)
        with self.assertRaises(AssertionError):
            self.assert_pass_receipt(
                "gate-source-equiv", expected_installed_root=self.installed_root
            )

    def _wait_until(self, predicate, what: str, timeout: float = 30.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(0.05)
        self.fail(f"timed out waiting for {what}")

    def test_factory_launch_forwards_signals_and_keeps_alias_until_child_gone(self) -> None:
        """The external-prefix launcher forwards INT/TERM/HUP/QUIT to the
        child, bounded-waits and reaps it *before* cleanup, preserves the
        child's actual exit status (or 128+signal), and keeps the operator
        alias directory until the child is truly gone — including a child
        that delays termination and a child that ignores the forwarded
        signal (bounded KILL escalation)."""
        harness = self.tmp / "launch-harness"
        (harness / ".factory" / "loop").mkdir(parents=True)
        (harness / ".factory" / "bin").mkdir(parents=True)
        (harness / ".factory" / "loop" / "__init__.py").write_text(
            "", encoding="utf-8"
        )
        (harness / ".factory" / "loop" / "launch.py").write_text(
            LAUNCH_STUB, encoding="utf-8"
        )
        shutil.copy2(
            ROOT / ".factory/bin/factory-launch",
            harness / ".factory/bin/factory-launch",
        )
        runtime = self.tmp / "launch-runtime"
        runtime.mkdir()

        def aliases():
            return sorted(str(p) for p in runtime.glob("factory-launch-alias.*"))

        def env_for(marker: Path, **extra: str):
            env = dict(self.base_env())
            env["FACTORY_INSTALL_RUNTIME"] = str(runtime)
            env["LAUNCH_TEST_MARKER"] = str(marker)
            env.update(extra)
            return env

        def cleanup_proc(proc: subprocess.Popen):
            if proc.poll() is None:
                proc.kill()
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    pass

        # 1. Delayed termination: the child handles TERM, delays ~1.5 s,
        # then exits 42.  The launcher forwards the signal, keeps the alias
        # while the child is still running, reaps the child's actual status,
        # and removes the alias only after the child is gone.
        marker = self.tmp / "launch-marker-delay"
        proc = subprocess.Popen(
            ["bash", str(harness / ".factory/bin/factory-launch"), "delay"],
            env=env_for(marker),
        )
        self.addCleanup(cleanup_proc, proc)
        self._wait_until(
            lambda: Path(str(marker) + ".ready").exists(), "delayed child ready"
        )
        self.assertEqual(len(aliases()), 1)
        os.kill(proc.pid, signal.SIGTERM)
        self._wait_until(
            lambda: Path(str(marker) + ".signal").exists(), "signal forwarded"
        )
        # The child is still inside its 1.5 s delayed handler: the alias
        # must remain until the child is truly gone.
        self.assertEqual(
            len(aliases()), 1,
            "alias must remain while the child is still running",
        )
        rc = proc.wait(timeout=30)
        self.assertEqual(rc, 42,
                         "the child's actual exit status must be preserved")
        self.assertEqual(aliases(), [],
                         "alias must be removed only after the child is gone")

        # 2. Bounded KILL escalation: a child that ignores the forwarded
        # TERM is KILLed after the bounded grace (137 = 128+9), reaped, and
        # the alias is then removed — the launcher never hangs and never
        # removes the alias while the child still runs.
        marker = self.tmp / "launch-marker-ignore"
        proc = subprocess.Popen(
            ["bash", str(harness / ".factory/bin/factory-launch"), "ignore"],
            env=env_for(marker, FACTORY_LAUNCH_GRACE="1"),
        )
        self.addCleanup(cleanup_proc, proc)
        self._wait_until(
            lambda: Path(str(marker) + ".ready").exists(), "ignoring child ready"
        )
        self.assertEqual(len(aliases()), 1)
        started = time.monotonic()
        os.kill(proc.pid, signal.SIGTERM)
        rc = proc.wait(timeout=30)
        elapsed = time.monotonic() - started
        self.assertEqual(rc, 137, "the KILL escalation must report 128+9")
        self.assertGreaterEqual(
            elapsed, 0.5, "the grace must elapse before the KILL escalation"
        )
        self.assertLess(elapsed, 25.0, "the bounded wait must stay bounded")
        self.assertEqual(aliases(), [])

        # 3. A child killed by the forwarded signal reports 128+signal
        # (default TERM disposition), and the alias is removed after reap.
        marker = self.tmp / "launch-marker-die"
        proc = subprocess.Popen(
            ["bash", str(harness / ".factory/bin/factory-launch"), "die"],
            env=env_for(marker),
        )
        self.addCleanup(cleanup_proc, proc)
        self._wait_until(
            lambda: Path(str(marker) + ".ready").exists(), "die-mode child ready"
        )
        self.assertEqual(len(aliases()), 1)
        os.kill(proc.pid, signal.SIGTERM)
        rc = proc.wait(timeout=30)
        self.assertEqual(
            rc, 143,
            "a child killed by the forwarded signal must report 128+15",
        )
        self.assertEqual(aliases(), [])

    def test_prefix_swap_fails_before_repo_writes(self) -> None:
        """Adversarial race: a symlink/bind-mount swap of the installed
        prefix path after its creation must fail closed *before* any
        repository write.  Every staged write revalidates the anchored
        directory fd identity and the named/resolved containment, so a
        path replaced by a symlink into the repository (or by a different
        directory inode) can never receive a staged file."""
        fixture = self.tmp / "swap-repo"
        self._make_repo(fixture)
        self._copy_surface(fixture)
        self._commit_all(fixture, "surface committed")
        prefix = self.tmp / "swap-prefix"
        root_fd, identity = installer_module._create_prefix(Path(ROOT), prefix)
        self.addCleanup(os.close, root_fd)
        self.assertEqual(stat.S_IMODE(os.fstat(root_fd).st_mode), 0o700)
        # The prefix is fresh and empty; swap the *path* to a symlink that
        # resolves into the repository (the same failure class as a
        # bind-mount swap: the name no longer resolves to the anchored
        # inode, and the resolved path lies inside the repo).
        os.rmdir(prefix)
        os.symlink(fixture, prefix)
        with self.assertRaises(installer_module.InstallerError) as caught:
            installer_module._stage_bytes(
                prefix, root_fd, identity, Path(ROOT),
                ".factory/loop/swapped.py", b"never\n", 0o644,
            )
        self.assertTrue(
            "swapped" in str(caught.exception)
            or "resolves inside" in str(caught.exception),
            caught.exception,
        )
        # Nothing reached the repository and the swapped path is untouched.
        self.assertFalse((fixture / ".factory/loop/swapped.py").exists())
        self.assertEqual(os.readlink(prefix), str(fixture))
        # A replaced *directory* inode (the bind-mount failure mode without
        # privileges) is detected the same way: named identity mismatch.
        os.unlink(prefix)
        prefix2 = self.tmp / "swap-prefix-2"
        fd2, identity2 = installer_module._create_prefix(Path(ROOT), prefix2)
        self.addCleanup(os.close, fd2)
        os.rmdir(prefix2)
        os.mkdir(prefix2)
        with self.assertRaises(installer_module.InstallerError) as caught:
            installer_module._stage_bytes(
                prefix2, fd2, identity2, Path(ROOT),
                ".factory/loop/swapped.py", b"never\n", 0o644,
            )
        self.assertIn("swapped", str(caught.exception))

    def test_committed_secret_name_is_never_staged(self) -> None:
        """A secret/credential-named path that is *committed* under the
        installed surface is never staged either: the committed tree is not
        an exemption from the no-secret installed-copy invariant, and the
        failed install rolls the created prefix back identity-safely."""
        fixture = self.tmp / "secret-commit"
        self._make_repo(fixture)
        self._copy_surface(fixture)
        (fixture / ".factory" / "loop" / "secret_token.py").write_text(
            "# committed secret-shaped authority\n", encoding="utf-8"
        )
        commit = self._commit_all(fixture, "secret committed")
        prefix = self.tmp / "secret-commit-prefix"
        manifest_out = self.tmp / "secret-commit-manifest.json"
        failed = _run(
            [sys.executable, str(INSTALLER), "install",
             "--root", str(fixture), "--commit", commit,
             "--prefix", str(prefix), "--manifest-out", str(manifest_out)],
            cwd=ROOT, env=self.base_env(), check=False,
        )
        self.assertNotEqual(failed.returncode, 0)
        self.assertIn("secret name", failed.stderr)
        self.assertFalse(prefix.exists(),
                         "failed install must roll back the created prefix")

    def test_batch_parser_errors_are_installer_errors(self) -> None:
        """Every malformed batched-Git transcript fails closed as an
        InstallerError, never a raw ValueError/IndexError leaking from the
        parser (cat-file sizes, record framing, non-UTF-8 text)."""
        fixture = self.tmp / "parse-repo"
        self._make_repo(fixture)
        self._copy_surface(fixture)
        commit = self._commit_all(fixture, "surface committed")

        # A non-numeric cat-file size is a wrapped ValueError.
        def bad_size(root, argv, maximum, input=None):
            return b"<oid> blob not-a-size\n"

        with mock.patch.object(installer_module, "_git_bytes_bounded",
                               side_effect=bad_size):
            with self.assertRaises(installer_module.InstallerError) as caught:
                installer_module._committed_blobs(fixture, ["a" * 40])
        self.assertIn("size", str(caught.exception))

        # A truncated record framing is a fail-closed parse error.
        def truncated(root, argv, maximum, input=None):
            return b"<oid> blob 100\nshort\n"

        with mock.patch.object(installer_module, "_git_bytes_bounded",
                               side_effect=truncated):
            with self.assertRaises(installer_module.InstallerError) as caught:
                installer_module._committed_blobs(fixture, ["a" * 40])
        self.assertIn("truncated", str(caught.exception))

        # Non-UTF-8 batch text is a wrapped UnicodeDecodeError.
        def binary(root, argv, maximum, input=None):
            return b"\xff\xfe\x00"

        with mock.patch.object(installer_module, "_git_bytes_bounded",
                               side_effect=binary):
            with self.assertRaises(installer_module.InstallerError) as caught:
                installer_module._ls_tree_surface(fixture, commit)
        self.assertIn("UTF-8", str(caught.exception))


if __name__ == "__main__":
    unittest.main(verbosity=2)
