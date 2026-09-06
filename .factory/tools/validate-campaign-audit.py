#!/usr/bin/env python3
"""Validate an independent campaign audit and its immutable Git binding."""

from __future__ import annotations

import argparse
import glob
import os
from pathlib import Path
import re
import stat
import subprocess
import tomllib

ROOT = Path(__file__).resolve().parents[2]
os.environ["GIT_NO_REPLACE_OBJECTS"] = "1"
SHA = r"[0-9a-f]{40}"
GIT_TIMEOUT = 120.0

# -- pinned immutable absolute Git (MED2) -----------------------------------
# Every trusted Git read of the campaign-audit validator uses a pinned
# absolute immutable executable — never a PATH-derived ``git`` — and a
# sanitized environment and finite timeout, so a poisoned PATH or GIT_*
# override can never redirect the binding reads.

GIT_ENV_STRIP = (
    "GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_COMMON_DIR", "GIT_NAMESPACE",
    "GIT_CEILING_DIRECTORIES", "GIT_SSH", "GIT_SSH_COMMAND", "GIT_ASKPASS",
    "GIT_TERMINAL_PROMPT", "GIT_CONFIG_PARAMETERS", "GIT_EXEC_PATH",
    "GIT_TEMPLATE_DIR",
)


def _immutable_chain(path: str) -> None:
    resolved = os.path.realpath(path)
    store_root = Path("/nix/store")
    if resolved.startswith(str(store_root) + os.sep):
        boundary = store_root
    else:
        boundary = Path(resolved).anchor
    current = Path(resolved)
    while True:
        try:
            info = current.lstat()
        except OSError as exc:
            fail(f"cannot stat pinned candidate component {current}: {exc}")
        if info.st_uid == os.getuid():
            sticky = stat.S_ISDIR(info.st_mode) and info.st_mode & stat.S_ISVTX
            if not sticky:
                fail(f"pinned candidate component {current} is caller-owned")
        if info.st_mode & 0o022 and not (stat.S_ISDIR(info.st_mode) and stat.S_ISVTX):
            fail(f"pinned candidate component {current} is group/other-writable")
        if current == boundary or current == current.parent:
            break
        current = current.parent


def _candidate_usable(candidate: str) -> bool:
    if not candidate.startswith("/"):
        return False
    try:
        _immutable_chain(candidate)
        info = os.stat(candidate)
    except (OSError, SystemExit):
        return False
    return stat.S_ISREG(info.st_mode) and bool(info.st_mode & 0o111)


def _resolve_pinned_git() -> str:
    if os.geteuid() == 0:
        fail("the pinned Git boundary refuses to resolve as root")
    for directory in ("/usr/bin", "/bin", "/run/current-system/sw/bin"):
        candidate = f"{directory}/git"
        if os.path.exists(candidate) and _candidate_usable(candidate):
            return candidate
    try:
        found = sorted(glob.glob("/nix/store/*/bin/git"))
    except OSError:
        found = []
    for candidate in found:
        if re.fullmatch(r"/nix/store/[0-9a-z]{32}-[^/]+/bin/git", candidate) and _candidate_usable(candidate):
            return candidate
    fail("no immutable absolute Git executable is available to the trusted boundary")
    raise AssertionError("unreachable")


def _sanitized_git_environment() -> dict:
    environment = dict(os.environ)
    for key in list(environment):
        if key == "GIT_CONFIG" or key.startswith("GIT_CONFIG_") or key in GIT_ENV_STRIP:
            environment.pop(key, None)
    return environment


GIT_EXECUTABLE = _resolve_pinned_git()


def fail(message: str) -> None:
    raise SystemExit(f"campaign-audit: {message}")


def _trusted_run(
    argv: list[str], *, what: str, **kwargs: object
) -> subprocess.CompletedProcess[str]:
    """One finite-bounded trusted subprocess (F4).

    Every trusted child of the campaign-audit validator runs with a
    sanitized Git environment (no ``GIT_*``/``GIT_CONFIG_*`` redirector can
    leak through) and a finite timeout, and a wedged or hostile child fails
    closed with a clean diagnostic instead of a traceback: a poisoned PATH
    or hostile env can never substitute the pinned absolute executables, and
    a hung runner-evidence check can never hang the independent audit.
    """
    try:
        if "stdout" not in kwargs and "stderr" not in kwargs:
            kwargs.setdefault("capture_output", True)
        kwargs.setdefault("text", True)
        return subprocess.run(
            argv, cwd=ROOT,
            env=_sanitized_git_environment(), timeout=GIT_TIMEOUT, **kwargs,
        )
    except subprocess.TimeoutExpired as exc:
        fail(f"{what} exceeded the {GIT_TIMEOUT:g}s trusted-execution bound")


def git(*args: str) -> str:
    result = _trusted_run(
        [GIT_EXECUTABLE, *args],
        what=f"Git binding {' '.join(args)}",
        text=True, capture_output=True,
    )
    if result.returncode:
        fail(f"Git binding failed: {' '.join(args)}")
    return result.stdout.strip()


def parse(path: Path) -> tuple[dict[str, str], str]:
    if path.is_symlink() or not path.is_file():
        fail("report must be a regular file")
    text = path.read_text(encoding="utf-8")
    match = re.match(r"\A---\n(.*?)\n---\n(.*)\Z", text, re.S)
    if not match:
        fail("missing front matter")
    meta: dict[str, str] = {}
    for line in match.group(1).splitlines():
        if ": " not in line:
            fail(f"malformed metadata line: {line}")
        key, value = line.split(": ", 1)
        if key in meta:
            fail(f"duplicate metadata key: {key}")
        meta[key] = value
    expected = ["schema", "round", "audit_base_commit", "plan_commit", "plan_blob", "environment_blob", "runner_evidence_sha256", "result"]
    if list(meta) != expected:
        fail(f"metadata keys/order must be exactly {expected}")
    return meta, match.group(2)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("metadata", "complete"))
    parser.add_argument("path", nargs="?", default=".factory/artifacts/campaign-audit.md")
    parser.add_argument("--expected-round", type=int)
    parser.add_argument("--expected-base")
    parser.add_argument("--expected-runner-evidence-sha256")
    args = parser.parse_args()
    meta, body = parse(ROOT / args.path)
    if meta["schema"] != "ralph-campaign-audit/v1":
        fail("unsupported schema")
    try:
        round_number = int(meta["round"])
    except ValueError:
        fail("round must be an integer")
    if round_number < 1:
        fail("round must be positive")
    for key in ("audit_base_commit", "plan_commit", "plan_blob", "environment_blob"):
        if not re.fullmatch(SHA, meta[key]):
            fail(f"{key} must be a full object ID")
    if not re.fullmatch(r"[0-9a-f]{64}", meta["runner_evidence_sha256"]):
        fail("runner_evidence_sha256 must be a SHA-256 digest")
    if git("replace", "-l"):
        fail("Git replacement objects are forbidden during an audit")
    git("cat-file", "-e", f"{meta['audit_base_commit']}^{{commit}}")
    if args.mode == "complete":
        if args.expected_round is None or not args.expected_base or not args.expected_runner_evidence_sha256:
            fail("completed validation requires supervisor-owned round, base, and runner evidence")
        if (
            round_number != args.expected_round
            or meta["audit_base_commit"] != args.expected_base
            or meta["runner_evidence_sha256"] != args.expected_runner_evidence_sha256
        ):
            fail("report binding does not match supervisor-owned campaign state")
        if not re.fullmatch(SHA, args.expected_base):
            fail("expected audit base is invalid")
    if _trusted_run(
        [GIT_EXECUTABLE, "merge-base", "--is-ancestor",
         meta["audit_base_commit"], "HEAD"],
        what="Git ancestor check",
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    ).returncode:
        fail("audit base is not an ancestor of HEAD")
    if git("rev-parse", f"{meta['audit_base_commit']}:.factory/artifacts/implementation-plan.md") != meta["plan_blob"]:
        fail("plan blob does not match audit base")
    if git("log", "-1", "--format=%H", meta["audit_base_commit"], "--", ".factory/artifacts/implementation-plan.md") != meta["plan_commit"]:
        fail("plan commit does not match audit base")
    if git("rev-parse", f"{meta['audit_base_commit']}:.factory/environment.toml") != meta["environment_blob"]:
        fail("environment blob does not match audit base")
    if args.mode == "complete":
        # F4: the outer runner-evidence checker is itself invoked with a
        # pinned absolute immutable executable, a sanitized environment (no
        # GIT_* / GIT_CONFIG_* redirector can leak through), and a finite
        # timeout — a hostile or wedged runner-evidence check can neither
        # redirect nor hang the independent campaign audit.
        evidence_digest = _trusted_run(
            [str(ROOT / ".factory/tools/check-factory-runner-evidence.py"),
             "--expected-commit", meta["audit_base_commit"], "--print-digest"],
            what="runner-evidence digest check",
        )
        if evidence_digest.returncode or evidence_digest.stdout.strip() != meta["runner_evidence_sha256"]:
            fail("runner evidence digest does not match the verified campaign phase")
    changed: set[str] = set(filter(None, git("diff", "--name-only", f"{meta['audit_base_commit']}..HEAD").splitlines()))
    commits = git("rev-list", f"{meta['audit_base_commit']}..HEAD").splitlines()
    if git("rev-list", "--min-parents=2", f"{meta['audit_base_commit']}..HEAD"):
        fail("merge commits are forbidden during an independent audit")
    for commit in commits:
        changed.update(filter(None, git("diff-tree", "--no-commit-id", "--name-only", "-r", "--root", commit).splitlines()))
    forbidden = changed - {".factory/artifacts/campaign-audit.md", ".ralph/agent/scratchpad.md"}
    if forbidden:
        fail(f"audit commits changed forbidden paths: {sorted(forbidden)}")
    if args.mode == "metadata":
        print(f"round={round_number} result={meta['result']}")
        return 0
    if meta["result"] not in {"pass", "findings"}:
        fail("completed result must be pass or findings")
    evidence = re.search(r"^## Evidence reviewed\s*\n(.*?)(?=^## |\Z)", body, re.M | re.S)
    if not evidence:
        fail("missing Evidence reviewed section")
    evidence_rules = {
        "Specification": r"`docs/[^`]+`",
        "Production paths": r"`[^`]*[/][^`]+`",
        "Executable evidence": r"`[^`]+`.*\b(PASS|FAIL|BLOCKED)\b",
        "Environment limits": r"`\.factory/environment\.toml`",
    }
    for field, detail_pattern in evidence_rules.items():
        match = re.search(rf"^- {field}:\s+(.+)$", evidence.group(1), re.M)
        if not match or not re.search(detail_pattern, match.group(1)):
            fail(f"Evidence reviewed requires a concrete '- {field}:' reference/result")
    findings = re.findall(r"^## Finding ([1-9][0-9]*):\s+(.+)$", body, re.M)
    if meta["result"] == "pass":
        if findings or not re.search(r"^## Findings\s*\n\s*None\.\s*$", body, re.M):
            fail("pass requires an explicit Findings section containing None.")
        with (ROOT / ".factory/config.toml").open("rb") as stream:
            required_capabilities = tomllib.load(stream).get("campaign", {}).get("required_capabilities", [])
        with (ROOT / ".factory/environment.toml").open("rb") as stream:
            environment = tomllib.load(stream)
        if not isinstance(required_capabilities, list) or not all(isinstance(item, str) and item for item in required_capabilities):
            fail("campaign.required_capabilities must be a string array")
        declared = {
            capability
            for entry in environment.get("tools", []) + environment.get("runners", [])
            for capability in entry.get("capabilities", [])
        }
        missing = sorted(set(required_capabilities) - declared)
        if missing:
            fail(f"pass lacks declared required environment capabilities: {missing}")
        if required_capabilities:
            evidence = _trusted_run(
                [str(ROOT / ".factory/tools/check-factory-runner-evidence.py"),
                 "--expected-commit", meta["audit_base_commit"],
                 "--print-capabilities"],
                what="runner-evidence capability check",
            )
            if evidence.returncode:
                fail("pass lacks valid commit-bound runner evidence")
            evidenced = set(filter(None, evidence.stdout.splitlines()))
            unevidenced = sorted(set(required_capabilities) - evidenced)
            if unevidenced:
                fail(f"pass lacks executed runner evidence for required capabilities: {unevidenced}")
    else:
        if not findings:
            fail("findings result requires at least one numbered Finding section")
        numbers = [int(number) for number, _ in findings]
        if numbers != list(range(1, len(numbers) + 1)):
            fail("findings must be uniquely and consecutively numbered")
        for number, _ in findings:
            block = re.search(rf"^## Finding {number}:.*?(?=^## Finding |\Z)", body, re.M | re.S)
            assert block
            for field in ("Requirement", "Production evidence", "Required remediation"):
                if not re.search(rf"^- {field}:\s+\S", block.group(0), re.M):
                    fail(f"Finding {number} requires '- {field}:'")
    print(f"campaign-audit: round {round_number} accepted with result {meta['result']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
