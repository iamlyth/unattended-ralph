"""factory-runner-policy.py — root-configured runner class policy (shared).

The runner protocol is class-based. A class is a root-configured mapping of a
dedicated unprivileged account to its workspace root, its approved project
verifier argv, and the exact capability allowlist it may claim. The root
endpoint (factory-runner-server.py) and the root-owned signer
(factory-runner-signer.py) both derive every authorization decision from this
policy; neither hardcodes product names, verifier paths, capability names, or
workspace roots.

The policy file lives out-of-tree at /etc/factory-runner/runner-policy.json
and is root-owned and runner-unreadable-for-write. Environment overrides exist
only for the disposable test harness; sudo resets the environment in
production, so the installed defaults always apply there. A runner can never
invent a class, widen its allowlist, or swap its verifier: the executing UID
is bound to exactly one class and the requested capability set must equal the
class allowlist exactly.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

DEFAULT_POLICY_PATH = Path(
    os.environ.get("FACTORY_RUNNER_POLICY", "/etc/factory-runner/runner-policy.json")
)
NAME = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
TOKEN = re.compile(r"^[^\x00-\x1f\x7f]{1,128}$")


class PolicyError(Exception):
    """The runner policy is absent, unsafe, or structurally invalid."""


def policy_path() -> Path:
    return DEFAULT_POLICY_PATH


def validate_argv(argv: object, where: str) -> None:
    if (
        not isinstance(argv, list) or not argv
        or not all(isinstance(item, str) and item for item in argv)
    ):
        raise PolicyError(f"{where} verify_argv must be a non-empty array of strings")
    if any(any(ord(char) < 32 for char in item) for item in argv):
        raise PolicyError(f"{where} verify_argv must be control-character-free")
    for item in argv:
        if not TOKEN.fullmatch(item):
            raise PolicyError(f"{where} verify_argv contains an invalid token")
    first = argv[0]
    if "/" in first:
        if first.startswith("/"):
            parts = Path(first).parts
            if len(parts) < 2 or parts[1] not in {"bin", "usr"} or ".." in parts:
                raise PolicyError(f"{where} verify_argv[0] must be a /bin|/usr binary")
        else:
            parts = Path(first).parts
            if ".." in parts or len(parts) != 2 or parts[0] not in {"scripts", "tests"}:
                raise PolicyError(f"{where} verify_argv[0] must be a scripts/tests path")
    elif not NAME.fullmatch(first):
        raise PolicyError(f"{where} verify_argv[0] is not a bare command name")


def validate_capabilities(capabilities: object, where: str) -> None:
    if (
        not isinstance(capabilities, list) or not capabilities
        or len(capabilities) != len(set(capabilities))
        or not all(isinstance(item, str) and NAME.fullmatch(item) for item in capabilities)
    ):
        raise PolicyError(f"{where} allowed_capabilities must be a non-empty unique name list")


def load_policy() -> dict:
    """Load and strictly validate the root-configured runner policy.

    Returns the validated policy dict. Every structural property is checked so
    a malformed or tampered policy fails closed everywhere it is consumed.
    """
    path = policy_path()
    if path.is_symlink():
        raise PolicyError(f"runner policy must not be a symlink: {path}")
    try:
        raw = path.read_text(encoding="utf-8")
        data = json.loads(raw)
    except OSError as exc:
        raise PolicyError(f"cannot read runner policy {path}: {type(exc).__name__}") from exc
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise PolicyError(f"runner policy is invalid JSON: {path}") from exc
    expected = {"schema", "namespace", "classes"}
    if not isinstance(data, dict) or set(data) != expected:
        raise PolicyError("runner policy top-level fields are invalid")
    if data.get("schema") != "factory-runner-policy/v1":
        raise PolicyError("runner policy schema is not factory-runner-policy/v1")
    namespace = data.get("namespace")
    if not isinstance(namespace, str) or not namespace or "\n" in namespace:
        raise PolicyError("runner policy namespace is invalid")
    classes = data.get("classes")
    if not isinstance(classes, list) or not classes:
        raise PolicyError("runner policy must declare at least one class")
    seen_names: set[str] = set()
    seen_uids: set[int] = set()
    for index, entry in enumerate(classes):
        if not isinstance(entry, dict):
            raise PolicyError(f"runner policy classes[{index}] is not an object")
        fields = {
            "name", "uid", "workspace_root", "verify_argv", "allowed_capabilities",
            "signer_helper", "signer_key", "signer_principal_file",
        }
        if set(entry) != fields:
            raise PolicyError(f"runner policy classes[{index}] fields are invalid")
        name = entry["name"]
        if not isinstance(name, str) or not NAME.fullmatch(name):
            raise PolicyError(f"runner policy classes[{index}].name is invalid")
        if name in seen_names:
            raise PolicyError(f"runner policy declares duplicate class {name}")
        uid = entry["uid"]
        if not isinstance(uid, int) or isinstance(uid, bool) or uid <= 0:
            raise PolicyError(f"runner policy classes[{index}].uid is invalid")
        if uid in seen_uids:
            raise PolicyError(f"runner policy declares duplicate uid {uid}")
        workspace_root = entry["workspace_root"]
        if (
            not isinstance(workspace_root, str) or not workspace_root.startswith("/")
            or ".." in Path(workspace_root).parts
            or workspace_root == "/"
        ):
            raise PolicyError(f"runner policy classes[{index}].workspace_root is invalid")
        validate_argv(entry["verify_argv"], f"classes[{index}]")
        validate_capabilities(entry["allowed_capabilities"], f"classes[{index}]")
        for field in ("signer_helper", "signer_key", "signer_principal_file"):
            value = entry[field]
            if (
                not isinstance(value, str) or not value.startswith("/")
                or ".." in Path(value).parts
            ):
                raise PolicyError(f"runner policy classes[{index}].{field} is invalid")
        seen_names.add(name)
        seen_uids.add(uid)
    return data


def class_for_uid(policy: dict, uid: int) -> dict:
    matches = [entry for entry in policy["classes"] if entry["uid"] == uid]
    if len(matches) != 1:
        raise PolicyError(f"executing uid {uid} is not bound to exactly one runner class")
    return matches[0]


def class_for_name(policy: dict, name: str) -> dict:
    matches = [entry for entry in policy["classes"] if entry["name"] == name]
    if len(matches) != 1:
        raise PolicyError(f"runner class {name!r} is not declared in the runner policy")
    return matches[0]
