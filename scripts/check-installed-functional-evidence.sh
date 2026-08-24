#!/usr/bin/env bash
# check-installed-functional-evidence.sh — generic installed-functional evidence gate.
#
# Task 23: this checker accepts **only** the dedicated generic evidence
# namespace backed by a matching installed-harness receipt.  By default
# (boilerplate mode) it scans the hardened children of
# `.factory-state/generic-evidence/` and accepts the unique namespace whose
# record is bound to the exact live audit coordinator base — the namespace
# the trusted generic evidence publisher stages at the exact clean bound
# commit — and never the legacy foreign root file
# `.factory-state/installed-functional-evidence.env` (an adopting-product
# artifact that is ignored and never rewritten).  `--namespace PATH` selects
# an explicit safe-mode namespace for fixture authorities.
#
# The accepted evidence obeys the strict invariant
# ``coordinator.base_commit == receipt.evidence_commit == record.commit ==
# namespace name``: the namespace directory name, the evidence record's
# ``commit``, the installed-harness receipt's ``evidence_commit``, and the
# live audit coordinator's ``base_commit`` must all be the **same** commit.
# A planted descendant namespace (a receipt minted at a later commit under
# a coordinator bound to an earlier base) is cross-audit and fails closed,
# and two candidate paths can never both be valid under one coordinator
# (the single live coordinator base can equal only one namespace name), so
# the selection is always unique and a forged or stale namespace can never
# shadow it.
#
# The evidence commit and the repository's final HEAD are distinct
# bindings: the record's ``commit`` is the **evidence commit** — the exact
# pre-campaign bound commit at which the trusted publisher minted the
# installed-harness receipt — and the **final HEAD** is the repository's
# actual HEAD at check time.  Trusted planner/developer commits advance the
# campaign HEAD beyond that bound commit, so the evidence commit is accepted
# exactly when it is an ancestor of (or equal to) the final HEAD, every
# implementation/acceptance authority path is byte-identical between the
# evidence commit and the final HEAD, and the receipt is bound to that same
# evidence commit (``record.commit == receipt.evidence_commit``); a record
# bound to a different or non-ancestor commit is stale or forged and fails
# closed.
#
# Accepting the generic evidence requires a **matching installed-harness
# receipt**: the record must bind the receipt reference, the receipt byte
# digest, the evidence commit, and the coordinator round/nonce; the receipt
# must pass the hardened hidden evidence validation (no-follow, owner/mode/
# link-count/inode identity, argv/digest/coordinator bindings), exit 0, and
# carry exactly one of the allowlisted argv arrays of the committed
# `installed-harness-smoke` receipt-policy category
# (`.factory/campaign-receipt-policy.json`).  The certified suite stdout
# transcript must carry no skip marker.  The live protected audit
# coordinator state must still authorize the exact round/nonce the record
# and receipt bind, and its audit base must be **exactly** the evidence
# commit — evidence minted at any other commit (descendant, incomparable,
# or foreign) fails closed (a reused, never-overwritten coordinator keeps
# its nonce, so recovery and reuse preserve the binding).  The working tree
# must be clean: no uncommitted tracked change and no untracked non-ignored
# file anywhere — the production/acceptance inputs must be unchanged since
# the tested commit.
#
# Usage:
#   scripts/check-installed-functional-evidence.sh [--namespace PATH]
#   (run from the repository root; the exact final HEAD is derived from Git)
set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
if [[ -n "${FACTORY_VERIFIER_ROOT:-}" ]]; then
    # The gate child executes the committed script through a retained
    # descriptor (`/proc/self/fd/<fd>`), so `BASH_SOURCE[0]` names the fd
    # path, never the canonical repository path.  The trusted parent pins
    # the canonical root instead, and the script directory is re-derived
    # from it.
    PROJECT_ROOT=$(realpath -e -- "$FACTORY_VERIFIER_ROOT")
    SCRIPT_DIR="$PROJECT_ROOT/scripts"
else
    PROJECT_ROOT=$(cd -- "$SCRIPT_DIR/.." && pwd)
fi
PY=${PYTHON:-python3}

# -- controlled shell context -------------------------------------------------
# Strip the Git redirector families and Ollama/campaign/credential variables
# so a caller environment can never steer the trusted Git reads at a
# different object store, index, work tree, configuration set, or model
# backend.
unset GIT_DIR GIT_WORK_TREE GIT_INDEX_FILE GIT_OBJECT_DIRECTORY \
    GIT_ALTERNATE_OBJECT_DIRECTORIES GIT_COMMON_DIR GIT_NAMESPACE \
    GIT_CEILING_DIRECTORIES GIT_SSH GIT_SSH_COMMAND GIT_ASKPASS \
    GIT_TERMINAL_PROMPT GIT_EXEC_PATH GIT_TEMPLATE_DIR \
    GIT_CONFIG_PARAMETERS GIT_CONFIG GIT_CONFIG_SYSTEM GIT_CONFIG_GLOBAL \
    GIT_CONFIG_NOSYSTEM GIT_CONFIG_COUNT 2>/dev/null || true
unset "${!GIT_CONFIG_KEY_@}" "${!GIT_CONFIG_VALUE_@}" 2>/dev/null || true
unset OLLAMA_USAGE_ENV_FILE OLLAMA_COOKIE OLLAMA_HOST 2>/dev/null || true
unset FACTORY_CAMPAIGN_AUDIT_ROUND FACTORY_CAMPAIGN_AUDIT_BASE \
    FACTORY_CAMPAIGN_AUDIT_NONCE 2>/dev/null || true

namespace=
while [[ $# -gt 0 ]]; do
    case "$1" in
        --namespace)
            if [[ $# -lt 2 || -z "$2" ]]; then
                echo "check-installed-functional-evidence: --namespace requires a non-empty path argument" >&2
                exit 2
            fi
            namespace=$2
            shift 2
            ;;
        *)
            echo "check-installed-functional-evidence: usage: [--namespace PATH]" >&2
            exit 2
            ;;
    esac
done

# -- pinned Git (never a caller-controlled `git` from PATH) ------------------
# The same PATH-pinned absolute executable the hidden control plane resolves
# (`gitutil.GIT_EXECUTABLE`) is used for every trusted Git read of this
# checker; a poisoned PATH or GIT_* override can never redirect the binding
# reads.
PINNED_GIT=$(PYTHONDONTWRITEBYTECODE=1 "$PY" - "$PROJECT_ROOT" <<'PY'
import importlib.util, sys
root = sys.argv[1]
spec = importlib.util.spec_from_file_location("_fg", root + "/.factory/loop/gitutil.py")
module = importlib.util.module_from_spec(spec)
sys.modules["_fg"] = module
spec.loader.exec_module(module)
print(module.GIT_EXECUTABLE)
PY
)
[[ -n "$PINNED_GIT" && -x "$PINNED_GIT" ]] || {
    echo "check-installed-functional-evidence: cannot resolve the pinned Git executable" >&2
    exit 1
}
cd -- "$PROJECT_ROOT"
TOPLEVEL=$("$PINNED_GIT" rev-parse --show-toplevel) || {
    echo "check-installed-functional-evidence: cannot resolve the repository root" >&2
    exit 1
}
[[ "$TOPLEVEL" == "$PROJECT_ROOT" ]] || {
    echo "check-installed-functional-evidence: the canonical root is not the git top level" >&2
    exit 1
}
FINAL_HEAD=$("$PINNED_GIT" rev-parse HEAD) || {
    echo "check-installed-functional-evidence: cannot resolve HEAD" >&2
    exit 1
}
[[ "$FINAL_HEAD" =~ ^[0-9a-f]{40}$ ]] || {
    echo "check-installed-functional-evidence: cannot resolve an exact 40-hex HEAD" >&2
    exit 1
}

# -- namespace resolution and hardening ----------------------------------------
# Boilerplate mode (default) scans the dedicated generic evidence root
# `.factory-state/generic-evidence/` and accepts the unique valid namespace
# bound to the exact live audit coordinator base (see the Python authority
# below).  An explicit
# `--namespace` must be a safe relative or absolute path inside the
# repository root with no `.`/`..`/empty component, and every parent
# directory must be a real directory — never a symlink — so a substituted
# namespace can never redirect the evidence read.
NS_MODE=scan
if [[ -n "$namespace" ]]; then
    NS_MODE=explicit
    case "$namespace" in
        /*)
            [[ "$namespace" == "$PROJECT_ROOT/"* ]] || {
                echo "check-installed-functional-evidence: the explicit namespace must live inside the repository root: $namespace" >&2
                exit 1
            }
            ns_abs=$namespace
            ;;
        *) ns_abs=$PROJECT_ROOT/$namespace ;;
    esac
    IFS='/' read -r -a NS_PARTS <<<"$namespace"
    for ns_part in "${NS_PARTS[@]}"; do
        [[ -n "$ns_part" && "$ns_part" != "." && "$ns_part" != ".." ]] || {
            echo "check-installed-functional-evidence: unsafe namespace path component ('$ns_part')" >&2
            exit 1
        }
    done
    # Every parent of the namespace (relative to the repository root) must be
    # a real directory, never a symlink (explicit traversal/symlink-parent
    # hardening), and the resolved namespace must stay inside the root.
    cursor=$PROJECT_ROOT
    rel=${ns_abs#"$PROJECT_ROOT/"}
    [[ "$rel" == "$ns_abs" ]] && rel=""
    IFS='/' read -r -a REL_PARTS <<<"$rel"
    for rel_part in "${REL_PARTS[@]}"; do
        [[ -n "$rel_part" ]] || continue
        cursor=$cursor/$rel_part
        if [[ -L "$cursor" || ! -d "$cursor" ]]; then
            echo "check-installed-functional-evidence: namespace parent is a symlink or not a real directory: $cursor" >&2
            exit 1
        fi
    done
    resolved_abs=$(realpath -m -- "$ns_abs")
    case "$resolved_abs" in
        "$PROJECT_ROOT/"*|"$PROJECT_ROOT") : ;;
        *)
            echo "check-installed-functional-evidence: the namespace escapes the repository root: $ns_abs" >&2
            exit 1
            ;;
    esac
else
    # Boilerplate mode: scan the dedicated generic evidence root for the
    # unique valid namespace bound to the exact live audit coordinator base.
    ns_abs=$PROJECT_ROOT/.factory-state/generic-evidence
    cursor=$PROJECT_ROOT
    for rel_part in .factory-state generic-evidence; do
        cursor=$cursor/$rel_part
        if [[ -L "$cursor" || ! -d "$cursor" ]]; then
            echo "check-installed-functional-evidence: the generic evidence root is missing or unsafe ($cursor); run the trusted generic evidence publisher at the exact clean HEAD" >&2
            exit 1
        fi
    done
fi

# -- the record/receipt authority --------------------------------------------
# The Python authority loads the hidden evidence module (hardened no-follow
# receipt validation) and the hidden gitutil module (sanitized pinned Git)
# from the repository's own hidden loop namespace, verifies the exact clean
# HEAD, and — in boilerplate mode — scans every hardened generic-evidence
# child to select the unique valid namespace bound to the exact live audit
# coordinator base whose implementation/acceptance authority paths are
# unchanged since the evidence commit (later plan/conformance/audit metadata
# commits are allowed); a stale (authority-path-changed) or absent selection
# fails closed.  Explicit `--namespace` mode validates that single hardened
# namespace exactly.  Every namespace validates the strict binding chain
# (coordinator.base_commit == receipt.evidence_commit == record.commit ==
# namespace name; the evidence commit is an ancestor of or equal to the
# final HEAD; live coordinator round/nonce bindings), the allowlisted
# installed category argv, the receipt/transcript byte digests, and the
# no-skip-marker requirement.
PYTHONDONTWRITEBYTECODE=1 python3 - "$PROJECT_ROOT" "$FINAL_HEAD" "$ns_abs" "$NS_MODE" <<'PY'
import hashlib
import json
import os
import re
import stat
import sys
from pathlib import Path

root = Path(sys.argv[1]).resolve()
final_head = sys.argv[2]
namespace = Path(sys.argv[3])
ns_mode = sys.argv[4]

SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
SHA40_RE = re.compile(r"^[0-9a-f]{40}$")
SKIP_TOKEN = re.compile(r"(?<!\S)(?:SKIP|SKIPPED)(?:[^\w]|$)")
RECEIPT_TAG = "installed-harness-smoke"
EVIDENCE_SCHEMA = "factory-generic-installed-functional/v1"
MAX_ARTIFACT = 64 * 1024 * 1024
MAX_RECORD = 64 * 1024


def fail(message: str) -> None:
    raise SystemExit(f"installed-functional-evidence: {message}")


def secured_read_bytes(path: Path, what: str, maximum: int) -> bytes:
    """Bounded no-follow read with owner/mode/link-count/inode identity checks.

    A symlink, hardlink alias, foreign owner, wrong mode, or inode
    substitution fails closed exactly like the hidden evidence authority.
    """
    absolute = path.absolute()
    try:
        descriptor = os.open(
            absolute, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        )
    except OSError as exc:
        fail(f"cannot open {what}: {path}: {type(exc).__name__}")
    try:
        before = os.fstat(descriptor)
        named = absolute.lstat()
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_uid != os.getuid()
            or before.st_nlink != 1
            or before.st_mode & 0o022
            or (before.st_dev, before.st_ino) != (named.st_dev, named.st_ino)
            or before.st_size > maximum
        ):
            fail(f"unsafe {what} (owner/mode/link-count/inode): {path}")
        raw = b""
        remaining = maximum + 1
        while remaining:
            chunk = os.read(descriptor, min(65536, remaining))
            if not chunk:
                break
            raw += chunk
            remaining -= len(chunk)
        after = os.fstat(descriptor)
        named_after = absolute.lstat()
        if (
            len(raw) > maximum
            or (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
            != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
            or (after.st_dev, after.st_ino) != (named_after.st_dev, named_after.st_ino)
        ):
            fail(f"{what} changed while reading: {path}")
        return raw
    finally:
        os.close(descriptor)


# -- the hidden evidence and git authorities ---------------------------------
loop = root / ".factory" / "loop"
if str(loop) not in sys.path:
    sys.path.insert(0, str(loop))
import evidence as evidence_module  # noqa: E402
import gitutil  # noqa: E402

# -- exact clean HEAD ----------------------------------------------------------
# The evidence certifies the installed harness at the evidence commit; the
# repository's *current* state must be exactly the committed one — an
# uncommitted tracked change or an untracked non-ignored file anywhere means
# the production/acceptance inputs are no longer the tested bytes and the
# evidence is stale.
status = gitutil.git_run(
    ["status", "--porcelain", "-z", "--untracked-files=all"], cwd=root
)
if status.returncode != 0 or status.stdout.replace("\x00", "").strip():
    detail = gitutil.git_run(
        ["status", "--porcelain", "--untracked-files=all"], cwd=root
    ).stdout.strip().splitlines()[:10]
    fail(
        "the working tree is not clean; installed-functional evidence is "
        "undefined while uncommitted or untracked files exist "
        f"({' | '.join(detail) if detail else 'unknown'})"
    )


def is_ancestor(ancestor: str, descendant: str) -> bool:
    """Partial-order comparator: ``ancestor`` precedes (or equals) ``descendant``.

    ``git merge-base --is-ancestor`` is the reflexive ancestry relation: a
    commit is an ancestor of itself, any two commits on one linear lineage
    are comparable, and commits on unrelated branches are not.  A genuine
    Git failure (any verdict other than 0/1) fails closed with a clear
    error instead of being misread as a non-ancestry verdict.
    """
    result = gitutil.git_run(
        ["merge-base", "--is-ancestor", ancestor, descendant], cwd=root
    )
    if result.returncode not in (0, 1):
        fail(
            f"cannot test ancestry {ancestor[:12]}..{descendant[:12]} "
            "(the pinned Git invocation failed)"
        )
    return result.returncode == 0


def is_ancestor_of_head(commit: str) -> bool:
    """True when ``commit`` is an ancestor of (or equal to) the final HEAD."""
    return is_ancestor(commit, final_head)


# The implementation/acceptance authority paths of the generic evidence
# machinery: the selected evidence is accepted only when every one of these
# is byte-identical between the evidence commit and the final HEAD, so later
# plan/conformance/audit metadata commits are allowed while an authority
# change after the evidence was minted makes the evidence stale and fails
# closed.
AUTHORITY_PATHS = (
    "scripts/check-installed-functional-evidence.sh",
    "scripts/machine-receipt.py",
    ".factory/loop/generic_evidence.py",
    ".factory/loop/evidence.py",
    ".factory/loop/gitutil.py",
    ".factory/loop/state.py",
    ".factory/bin/publish-generic-evidence",
    ".factory/campaign-receipt-policy.json",
    ".factory/tests/test-factory-installed.sh",
)


def authority_paths_changed(evidence_commit: str) -> bool:
    """True when any authority path differs between evidence commit and HEAD."""
    result = gitutil.git_run(
        ["diff", "--name-only", evidence_commit, final_head, "--", *AUTHORITY_PATHS],
        cwd=root,
    )
    if result.returncode != 0:
        fail(
            f"cannot diff the generic-evidence authority between "
            f"{evidence_commit[:12]} and {final_head[:12]}"
        )
    return bool(result.stdout.strip())


def validate_namespace_record(
    namespace: Path, *, strict: bool
) -> tuple[dict, str, str | None]:
    """Fully validate one hardened generic evidence namespace.

    Returns ``(record, evidence_commit, reason)``; ``reason`` is ``None``
    when the namespace is valid.  In scan mode (``strict=False``) an invalid
    record is *excluded* from the candidate set (it can neither certify nor
    falsify evidence) and the fail-closed reason is returned for
    diagnostics, while explicit mode (``strict=True``) fails closed on any
    invalid record.  The validation covers the record schema/bindings, the
    matching hardened installed-harness receipt (allowlisted argv, exit 0,
    exact commit, coordinator round/nonce, byte digests), the certified
    transcripts (no skip marker), and the live audit coordinator freshness.
    """
    try:
        record, evidence_commit = _validate_namespace_record(namespace)
        return record, evidence_commit, None
    except SystemExit as exc:
        if strict:
            raise
        return None, None, str(exc)


def _validate_namespace_record(namespace: Path) -> tuple[dict, str]:
    record_path = namespace / "installed-functional.json"
    if record_path.is_symlink() or not record_path.is_file():
        fail(
            "missing or unsafe generic evidence record; run the trusted "
            "generic evidence publisher at the exact clean HEAD"
        )
    raw_record = secured_read_bytes(
        record_path, "generic evidence record", MAX_RECORD
    )
    try:
        record = json.loads(raw_record.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        fail(f"invalid generic evidence record: {exc}")
    if not isinstance(record, dict):
        fail("generic evidence record must be an object")
    expected = {
        "schema", "commit", "test", "result", "skipped", "receipt",
        "receipt_sha256", "suite_stdout_sha256", "coordinator_round",
        "coordinator_nonce",
    }
    if set(record) != expected or record.get("schema") != EVIDENCE_SCHEMA:
        fail("generic evidence record schema is invalid")
    if record.get("test") != "test_installed_functional":
        fail("generic evidence names the wrong test")
    if record.get("result") != "PASS" or record.get("skipped") != 0:
        fail("generic evidence is not a clean pass with zero skips")
    evidence_commit = record.get("commit")
    if not isinstance(evidence_commit, str) or not SHA40_RE.fullmatch(
        evidence_commit
    ):
        fail("generic evidence commit is invalid")
    # Strict invariant: the namespace directory name must be exactly the
    # evidence commit the record binds (``record.commit == namespace name``)
    # — a planted descendant, incomparable, or foreign namespace whose name
    # does not equal its record commit is never evidence.
    if evidence_commit != namespace.name:
        fail(
            "generic evidence record commit does not equal the namespace "
            f"name ({namespace.name})"
        )
    receipt_ref = record.get("receipt")
    if (
        not isinstance(receipt_ref, str)
        or not receipt_ref
        or receipt_ref.startswith("/")
        or ".." in Path(receipt_ref).parts
    ):
        fail("generic evidence receipt reference is unsafe")
    for key in ("receipt_sha256", "suite_stdout_sha256"):
        value = record.get(key)
        if not isinstance(value, str) or not SHA256_RE.fullmatch(value):
            fail(f"generic evidence {key} is invalid")
    round_number = record.get("coordinator_round")
    if type(round_number) is not int or round_number < 1:
        fail("generic evidence coordinator_round is invalid")
    coordinator_nonce = record.get("coordinator_nonce")
    if not isinstance(coordinator_nonce, str) or not SHA256_RE.fullmatch(
        coordinator_nonce
    ):
        fail("generic evidence coordinator_nonce is invalid")

    # The matching installed-harness receipt.
    receipt = evidence_module.validate_receipt(root, receipt_ref)
    if receipt["exit_code"] != 0:
        fail("the installed-harness receipt did not exit 0")
    if receipt["evidence_commit"] != evidence_commit:
        fail(
            "the installed-harness receipt is bound to "
            f"{receipt['evidence_commit'][:12]}, not the generic evidence "
            f"commit {evidence_commit[:12]}"
        )
    if receipt["coordinator_round"] != round_number:
        fail(
            "the installed-harness receipt coordinator round does not match "
            "the generic evidence binding"
        )
    if receipt["coordinator_nonce"] != coordinator_nonce:
        fail(
            "the installed-harness receipt coordinator nonce does not match "
            "the generic evidence binding"
        )
    if not isinstance(receipt["argv"], list) or not all(
        isinstance(item, str) and item for item in receipt["argv"]
    ):
        fail("installed-harness receipt argv is invalid")
    receipt_digest = hashlib.sha256(
        secured_read_bytes(
            Path(root) / receipt_ref, "installed-harness receipt", 1024 * 1024,
        )
    ).hexdigest()
    if receipt_digest != record["receipt_sha256"]:
        fail(
            "the installed-harness receipt digest does not match the generic "
            "evidence binding"
        )

    # Allowlisted installed category.
    policy_path = root / ".factory/campaign-receipt-policy.json"
    try:
        policy = json.loads(
            secured_read_bytes(
                policy_path, "receipt policy", 2 * 1024 * 1024
            ).decode("utf-8")
        )
    except (UnicodeError, json.JSONDecodeError) as exc:
        fail(f"invalid receipt policy: {exc}")
    category = None
    for candidate in policy.get("categories", []):
        if isinstance(candidate, dict) and candidate.get("name") == RECEIPT_TAG:
            category = candidate
            break
    if category is None or not isinstance(category.get("argv"), list) or not category["argv"]:
        fail(f"receipt policy has no allowlisted {RECEIPT_TAG} category")
    if not any(receipt["argv"] == list(allowed) for allowed in category["argv"]):
        fail(f"the installed-harness receipt argv is not allowlisted for {RECEIPT_TAG}")

    # The certified suite transcripts carry no skip marker.
    receipt_path = Path(root) / receipt_ref
    stdout_path = receipt_path.with_suffix(".stdout")
    stdout_bytes = secured_read_bytes(
        stdout_path, "installed-harness stdout transcript", MAX_ARTIFACT
    )
    if hashlib.sha256(stdout_bytes).hexdigest() != record["suite_stdout_sha256"]:
        fail(
            "the installed-harness suite stdout digest does not match the "
            "generic evidence binding"
        )
    if SKIP_TOKEN.search(stdout_bytes.decode("utf-8", "replace")):
        fail("the installed-harness suite printed a skip marker; it can never be PASS")
    stderr_path = receipt_path.with_suffix(".stderr")
    stderr_bytes = secured_read_bytes(
        stderr_path, "installed-harness stderr transcript", MAX_ARTIFACT
    )
    if hashlib.sha256(stderr_bytes).hexdigest() != receipt["stderr_sha256"]:
        fail("the installed-harness suite stderr digest does not match the receipt")
    if SKIP_TOKEN.search(stderr_bytes.decode("utf-8", "replace")):
        fail("the installed-harness suite stderr transcript contains a skip marker")

    # Live coordinator freshness (reuse/recovery never rewrites).
    coordinator = root / ".factory-state/audit-coordinator.json"
    if coordinator.is_symlink() or not coordinator.is_file():
        fail("the audit coordinator state is missing or unsafe")
    coordinator_info = coordinator.lstat()
    if (
        coordinator_info.st_uid != os.getuid()
        or coordinator_info.st_nlink != 1
        or coordinator_info.st_mode & 0o022
        or stat.S_IMODE(coordinator_info.st_mode) != 0o600
        or coordinator_info.st_size > MAX_RECORD
    ):
        fail("the audit coordinator state is unsafe (owner/mode/link-count)")
    try:
        coordinator_state = json.loads(coordinator.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        fail(f"the audit coordinator state is invalid: {exc}")
    expected = {"schema", "round", "base_commit", "nonce", "created_at"}
    if (
        not isinstance(coordinator_state, dict)
        or set(coordinator_state) != expected
        or coordinator_state.get("schema") != "ralph-audit-coordinator/v1"
        or type(coordinator_state.get("round")) is not int
        or coordinator_state["round"] < 1
        or not isinstance(coordinator_state.get("base_commit"), str)
        or not SHA40_RE.fullmatch(coordinator_state["base_commit"])
        or not isinstance(coordinator_state.get("nonce"), str)
        or not SHA256_RE.fullmatch(coordinator_state["nonce"])
        or not isinstance(coordinator_state.get("created_at"), int)
    ):
        fail("the audit coordinator state is invalid")
    if coordinator_state["round"] != round_number:
        fail(
            "the live audit coordinator round does not match the generic "
            "evidence binding (stale or cross-round evidence)"
        )
    if coordinator_state["nonce"] != coordinator_nonce:
        fail(
            "the live audit coordinator nonce does not match the generic "
            "evidence binding (the evidence belongs to a different audit)"
        )
    if coordinator_state["base_commit"] != evidence_commit:
        # Strict invariant: the live audit coordinator base must be exactly
        # the evidence commit (``coordinator.base_commit ==
        # receipt.evidence_commit == record.commit == namespace name``).  A
        # descendant, incomparable, or foreign namespace minted at any other
        # commit is cross-audit evidence and fails closed — a planted
        # descendant receipt under a base coordinator can never be valid.
        fail(
            "the live audit coordinator base commit "
            f"{coordinator_state['base_commit'][:12]} is not the generic "
            f"evidence commit {evidence_commit[:12]} (the strict binding "
            "coordinator.base_commit == receipt.evidence_commit == "
            "record.commit == namespace name is violated; descendant, "
            "incomparable, or foreign evidence)"
        )
    return record, evidence_commit


# -- selection ----------------------------------------------------------------
if ns_mode == "scan":
    # Boilerplate mode: scan the hardened generic-evidence children and
    # select the unique valid namespace — the one whose record is bound to
    # the exact live audit coordinator base
    # (``coordinator.base_commit == receipt.evidence_commit == record.commit
    # == namespace name``).  A child that is structurally unsafe (symlink,
    # foreign owner, wrong mode) fails closed; the publisher's private
    # `.partial-*` crash/temp namespaces and any other non-commit entry are
    # never evidence and are ignored.  An invalid record — including a
    # planted descendant, incomparable, or foreign namespace bound to any
    # commit other than the live coordinator base — is excluded from the
    # candidate set and can never shadow the selection.  Because the single
    # live coordinator base can equal exactly one namespace name, two
    # candidate paths can never both be valid under one coordinator: the
    # selection is always unique, so no descendant-most choice or ambiguity
    # resolution is ever needed.
    if namespace.is_symlink() or not namespace.is_dir():
        fail("the generic evidence root is missing or unsafe")
    candidates: list[tuple[str, Path, dict]] = []
    excluded_reasons: list[str] = []
    for child in sorted(namespace.iterdir()):
        name = child.name
        if not SHA40_RE.fullmatch(name):
            continue
        if child.is_symlink() or not child.is_dir():
            # A non-directory/symlinked child can never be evidence; it is
            # excluded from the candidate set (a planted or foreign entry can
            # neither certify nor falsify the selected evidence).
            excluded_reasons.append(f"{name}: not a real directory")
            continue
        info = child.lstat()
        if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
            excluded_reasons.append(
                f"{name}: unsafe directory (owner/mode)"
            )
            continue
        validated = validate_namespace_record(child, strict=False)
        if validated[0] is None:
            excluded_reasons.append(f"{name}: {validated[2]}")
            continue
        record, evidence_commit, _ = validated
        if not is_ancestor_of_head(evidence_commit):
            excluded_reasons.append(
                f"{name}: not an ancestor of the final HEAD"
            )
            continue
        candidates.append((evidence_commit, child, record))
    if len(candidates) == 0:
        fail(
            "no valid generic evidence namespace exists under "
            f"{namespace} (final head {final_head[:12]}); run the trusted "
            "generic evidence publisher at the exact clean HEAD"
            + (f" — excluded: {'; '.join(excluded_reasons)}" if excluded_reasons else "")
        )
    if len(candidates) > 1:
        # Unreachable under the strict binding (the single live coordinator
        # base can equal only one namespace name), but fail closed rather
        # than guess: two candidate paths cannot both be valid under one
        # coordinator.
        fail(
            "ambiguous generic evidence: multiple namespaces are bound to "
            "the same live audit coordinator; two candidate paths cannot "
            "both be valid under one coordinator"
        )
    evidence_commit, namespace, record = candidates[0]
    if authority_paths_changed(evidence_commit):
        fail(
            f"stale generic evidence: the implementation/acceptance "
            f"authority changed between the evidence commit "
            f"{evidence_commit[:12]} and the final HEAD {final_head[:12]}; "
            "re-run the trusted generic evidence publisher at the exact "
            "clean HEAD"
        )
else:
    # Explicit `--namespace` mode: exactly this hardened namespace is
    # validated; the evidence commit must still be an ancestor of the final
    # HEAD and the authority paths must be unchanged.
    validated = validate_namespace_record(namespace, strict=True)
    if validated[0] is None:
        fail("generic evidence record validation failed")
    record, evidence_commit, _ = validated
    if not is_ancestor_of_head(evidence_commit):
        fail(
            f"generic evidence commit {evidence_commit[:12]} is not an "
            f"ancestor of (or equal to) the final HEAD {final_head[:12]} "
            "(stale or forged evidence)"
        )
    if authority_paths_changed(evidence_commit):
        fail(
            f"stale generic evidence: the implementation/acceptance "
            f"authority changed between the evidence commit "
            f"{evidence_commit[:12]} and the final HEAD {final_head[:12]}; "
            "re-run the trusted generic evidence publisher at the exact "
            "clean HEAD"
        )

receipt_ref = record["receipt"]
print(
    f"installed-functional-evidence: PASS at {evidence_commit} "
    f"(final head {final_head[:12]}, zero skips, [receipt: {receipt_ref}])"
)
PY
