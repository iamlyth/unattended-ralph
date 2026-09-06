#!/usr/bin/env python3
"""Strict ordered pre-round hook registry and canonical result binding.

The registry contains only fixed control-plane implementations.  It is not a
command launcher: shell strings, paths, environment additions, and optional
failure policies are deliberately absent.  This keeps hooks inside the same
installed, exact-commit authority as the campaign and prevents a registry from
becoming a privileged arbitrary-command surface.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Callable, Dict, Mapping, Sequence, Tuple

REGISTRY_SCHEMA = "factory-pre-round-hooks/v1"
RESULT_SCHEMA = "factory-pre-round-hook-results/v1"
REGISTRY_MAX_BYTES = 16 * 1024
MAX_HOOKS = 32
IMPLEMENTATIONS = ("branch_guard",)
SAFE_ID_RE = re.compile(r"^[a-z][a-z0-9-]{0,63}$")
SHA40_RE = re.compile(r"^[0-9a-f]{40}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class PreRoundError(Exception):
    """A registry, binding, or mandatory hook failed closed."""


@dataclass(frozen=True)
class Hook:
    hook_id: str
    implementation: str
    enabled: bool
    mandatory: bool


@dataclass(frozen=True)
class Registry:
    hooks: Tuple[Hook, ...]
    raw_digest: str


@dataclass(frozen=True)
class HookResult:
    hook_id: str
    order: int
    enabled: bool
    mandatory: bool
    implementation: str
    implementation_digest: str
    outcome: str

    def to_dict(self) -> Dict[str, object]:
        return {
            "id": self.hook_id,
            "order": self.order,
            "enabled": self.enabled,
            "mandatory": self.mandatory,
            "implementation": self.implementation,
            "implementation_digest": self.implementation_digest,
            "outcome": self.outcome,
        }


def _object_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise PreRoundError(f"duplicate registry key {key!r}")
        result[key] = value
    return result


def parse_registry(raw: bytes) -> Registry:
    """Parse an exact registry blob with no defaults or extensible fields."""
    if not isinstance(raw, bytes) or not raw or len(raw) > REGISTRY_MAX_BYTES:
        raise PreRoundError("pre-round hook registry is empty or oversized")
    if raw.startswith(b"\xef\xbb\xbf"):
        raise PreRoundError("pre-round hook registry must not carry a UTF-8 BOM")
    try:
        document = json.loads(raw.decode("utf-8"), object_pairs_hook=_object_pairs)
    except PreRoundError:
        raise
    except (UnicodeDecodeError, ValueError) as exc:
        raise PreRoundError(f"pre-round hook registry is invalid JSON: {exc}") from exc
    if not isinstance(document, dict) or set(document) != {"schema", "hooks"}:
        raise PreRoundError("registry must contain exactly `schema` and `hooks`")
    if document["schema"] != REGISTRY_SCHEMA:
        raise PreRoundError(f"registry schema must be exactly {REGISTRY_SCHEMA!r}")
    hooks_raw = document["hooks"]
    if not isinstance(hooks_raw, list) or not hooks_raw or len(hooks_raw) > MAX_HOOKS:
        raise PreRoundError("registry hooks must be a nonempty bounded array")
    hooks = []
    seen_ids = set()
    seen_implementations = set()
    for index, item in enumerate(hooks_raw):
        if not isinstance(item, dict) or set(item) != {
            "id", "implementation", "enabled", "mandatory"
        }:
            raise PreRoundError(f"hook {index} has unknown or missing fields")
        hook_id = item["id"]
        implementation = item["implementation"]
        enabled = item["enabled"]
        mandatory = item["mandatory"]
        if not isinstance(hook_id, str) or SAFE_ID_RE.fullmatch(hook_id) is None:
            raise PreRoundError(f"hook {index} has an unsafe id")
        if hook_id in seen_ids:
            raise PreRoundError(f"duplicate hook id {hook_id!r}")
        if implementation not in IMPLEMENTATIONS:
            raise PreRoundError(f"hook {hook_id!r} has unknown implementation")
        if implementation in seen_implementations:
            raise PreRoundError(f"duplicate hook implementation {implementation!r}")
        if type(enabled) is not bool or type(mandatory) is not bool:
            raise PreRoundError(f"hook {hook_id!r} flags must be booleans")
        if not mandatory:
            raise PreRoundError(f"hook {hook_id!r} must be mandatory")
        hooks.append(Hook(hook_id, implementation, enabled, mandatory))
        seen_ids.add(hook_id)
        seen_implementations.add(implementation)
    return Registry(tuple(hooks), hashlib.sha256(raw).hexdigest())


def configuration_digest(
    registry: Registry,
    implementation_digests: Mapping[str, str],
    *,
    bound_commit: str,
) -> str:
    """Bind exact registry bytes and every named fixed implementation blob."""
    if set(implementation_digests) != {h.implementation for h in registry.hooks}:
        raise PreRoundError("implementation digest set does not match the registry")
    if not isinstance(bound_commit, str) or SHA40_RE.fullmatch(bound_commit) is None:
        raise PreRoundError("hook configuration commit must be SHA-1")
    payload = {
        "bound_commit": bound_commit,
        "registry_digest": registry.raw_digest,
        "implementations": [
            [hook.implementation, implementation_digests[hook.implementation]]
            for hook in registry.hooks
        ],
    }
    for _name, digest in payload["implementations"]:
        if not isinstance(digest, str) or SHA256_RE.fullmatch(digest) is None:
            raise PreRoundError("hook implementation digest must be SHA-256")
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def run_hooks(
    registry: Registry,
    *,
    implementation_digests: Mapping[str, str],
    execute: Callable[[Hook], None],
) -> Tuple[Tuple[HookResult, ...], bool]:
    """Run enabled hooks in array order, stopping at the first mandatory failure."""
    results = []
    success = True
    for order, hook in enumerate(registry.hooks, 1):
        digest = implementation_digests.get(hook.implementation, "")
        if SHA256_RE.fullmatch(digest) is None:
            raise PreRoundError(f"missing digest for hook {hook.hook_id!r}")
        if not hook.enabled:
            outcome = "disabled"
        elif success:
            try:
                execute(hook)
                outcome = "pass"
            except BaseException:
                outcome = "failed"
                success = False
        else:
            outcome = "not_run"
        results.append(HookResult(
            hook.hook_id, order, hook.enabled, hook.mandatory,
            hook.implementation, digest, outcome,
        ))
    return tuple(results), success


def result_bytes(
    *,
    campaign_id: str,
    round_number: int,
    commit: str,
    configuration_digest_value: str,
    results: Sequence[HookResult],
    postcondition_outcome: str = "pass",
) -> bytes:
    """Canonical typed result; it contains no hook output or remote text."""
    if not isinstance(campaign_id, str) or not campaign_id:
        raise PreRoundError("campaign id is required")
    if type(round_number) is not int or round_number < 1:
        raise PreRoundError("round number must be positive")
    if SHA40_RE.fullmatch(commit) is None:
        raise PreRoundError("hook result commit must be SHA-1")
    if SHA256_RE.fullmatch(configuration_digest_value) is None:
        raise PreRoundError("hook configuration digest must be SHA-256")
    if postcondition_outcome not in ("pass", "failed"):
        raise PreRoundError("hook postcondition outcome must be pass or failed")
    payload = {
        "schema": RESULT_SCHEMA,
        "campaign_id": campaign_id,
        "round": round_number,
        "commit": commit,
        "configuration_digest": configuration_digest_value,
        "hooks": [item.to_dict() for item in results],
        "postcondition_outcome": postcondition_outcome,
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


def chain_result_digest(previous: str, result: bytes) -> str:
    """Append one round result to the campaign-scoped digest chain."""
    if SHA256_RE.fullmatch(previous) is None:
        raise PreRoundError("previous hook result digest must be SHA-256")
    return hashlib.sha256(bytes.fromhex(previous) + result).hexdigest()
