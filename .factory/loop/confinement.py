#!/usr/bin/env python3
"""Task 8 confinement proof authority — Task 7 private proof seam only.

Task 7 review, obligation 2: a production ``ollama``-provider invocation
must NOT reach the model until the Task 8 confinement authority *proves*
that ``.factory/`` and the operator Ollama credential store(s) are
inaccessible/read-only to model tools, and that the Ollama usage-guard
source (``usage.py``, ``usage_fetch.py``) is executed only from its
exact-commit blob or the trusted external executable prefix.

Task 8 is pending and its real confinement authority is *not* implemented
here (this task must not build Task 8).  Until that authority exists, the
production gate fails closed: :func:`prove_confinement` always raises
:class:`ConfinementUnavailable`, so no production Ollama launch can
proceed.  The hermetic hidden suite mints a *private synthetic proof* via
:func:`_mint_synthetic_proof` (never exported on the public package
surface) and passes it to ``launch.authorize_launch(..., _confinement_proof=...)``;
``authorize_launch`` re-validates the proof against the exact invocation
and the exact executing guard-source bytes before the §10 guard runs.

A synthetic proof is not evidence of confinement: it exists only so the
Task 7 guard/decision-table machinery can be exercised end-to-end without
Task 8.  The capability evidence checker never accepts a synthetic proof
as real confinement evidence.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
import stat
from typing import Optional, Sequence, Tuple

# The private mint marker: only the mint functions below can produce a
# token, mirroring ``launch.LaunchAuthority._MINT_SECRET``.
_PROOF_MINT_SECRET = object()

# The guard-source modules whose exact executing bytes a proof binds.
GUARD_SOURCE_MODULES = ("usage.py", "usage_fetch.py")

# Bounded no-follow read for re-deriving the executing guard-source bytes.
_BLOB_READ_CHUNK = 65536
_MAX_GUARD_SOURCE_BYTES = 512 * 1024


class ConfinementError(Exception):
    """A confinement proof is missing, unbound, or tampered (fail closed)."""


class ConfinementUnavailable(ConfinementError):
    """The Task 8 production confinement authority is not yet available."""


class ConfinementProof:
    """Unforgeable Task 8 confinement proof token (skeleton; test-only mint).

    The proof binds an ``ollama``-provider invocation to:

    * the exact bound commit and workspace of the invocation;
    * the exact executing guard-source bytes (``usage.py`` and
      ``usage_fetch.py`` digests), so an operator-claimed or
      caller-controlled guard source can never be substituted;
    * the operator credential store path(s), each proven to live *outside*
      the model-visible workspace (Task 7 review, obligation 1).

    There is no public constructor: the proof is produced only by
    :func:`_mint_synthetic_proof` (hermetic hidden suite) or, once Task 8
    lands, by the real confinement authority.  ``validate_proof`` re-derives
    every binding from the exact invocation and the executing files and
    fails closed on any mismatch.
    """

    __slots__ = (
        "_bound_commit",
        "_workspace",
        "_provider",
        "_guard_source_digests",
        "_credential_stores",
        "_mint",
    )

    def __init__(
        self,
        *,
        bound_commit: str,
        workspace: str,
        provider: str,
        guard_source_digests: Tuple[str, str],
        credential_stores: Tuple[str, ...],
        _mint: object,
    ) -> None:
        if _mint is not _PROOF_MINT_SECRET:
            raise ConfinementError(
                "a ConfinementProof cannot be forged from operator claims; "
                "only the confinement authority may mint a proof"
            )
        self._bound_commit = bound_commit
        self._workspace = workspace
        self._provider = provider
        self._guard_source_digests = tuple(guard_source_digests)
        self._credential_stores = tuple(credential_stores)
        self._mint = _mint

    @property
    def bound_commit(self) -> str:
        return self._bound_commit

    @property
    def workspace(self) -> str:
        return self._workspace

    @property
    def provider(self) -> str:
        return self._provider

    @property
    def guard_source_digests(self) -> Tuple[str, str]:
        return self._guard_source_digests

    @property
    def credential_stores(self) -> Tuple[str, ...]:
        return self._credential_stores


def _module_bytes(module: str) -> str:
    """Bounded no-follow read of one executing guard-source module."""
    path = Path(__file__).resolve().with_name(module)
    flags = (
        os.O_RDONLY
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )
    try:
        descriptor = os.open(str(path), flags)
    except OSError as exc:
        raise ConfinementError(f"cannot open guard source {path}: {exc}") from exc
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise ConfinementError(f"guard source {path} is not a regular file")
        if info.st_size > _MAX_GUARD_SOURCE_BYTES:
            raise ConfinementError(
                f"guard source {path} exceeds the size bound"
            )
        digest = hashlib.sha256()
        while True:
            chunk = os.read(descriptor, _BLOB_READ_CHUNK)
            if not chunk:
                break
            digest.update(chunk)
        return digest.hexdigest()
    finally:
        os.close(descriptor)


def _executing_guard_source_digests() -> Tuple[str, str]:
    """SHA-256 of the exact executing ``usage.py`` / ``usage_fetch.py``."""
    return tuple(_module_bytes(module) for module in GUARD_SOURCE_MODULES)  # type: ignore[return-value]


def prove_confinement(binding: object) -> ConfinementProof:
    """The Task 8 *production* confinement authority (not yet available).

    Until Task 8 implements the real proof (that ``.factory/`` and the
    operator credential store(s) are inaccessible/read-only to model tools
    and that the guard source is executed from its exact-commit blob or the
    trusted external executable prefix), every production call fails closed
    so no ``ollama``-provider invocation can reach a model.
    """
    raise ConfinementUnavailable(
        "the Task 8 confinement proof authority is not yet available; an "
        "ollama-provider production launch fails closed until it proves "
        ".factory/ and the operator credential store(s) are "
        "inaccessible/read-only to model tools and the guard source is "
        "exact-commit bound"
    )


def _assert_store_outside(usage_guard, store, workspace) -> None:
    """Fail closed with a :class:`ConfinementError` on a workspace-scoped store.

    ``assert_store_outside_workspace`` raises the guard's config error for an
    operator store that lives inside the model-visible workspace; on the
    confinement surface that is a proof/tamper failure, so it is surfaced as
    a :class:`ConfinementError` and never leaks as a bare guard error.
    """
    try:
        usage_guard.assert_store_outside_workspace(store, workspace)
    except usage_guard.UsageConfigError as exc:
        raise ConfinementError(str(exc)) from exc


def _import_usage_guard():
    """The guard module in either import mode (package or flat harness)."""
    try:  # package-import mode (the hidden control-plane package)
        from . import usage as usage_guard
    except ImportError:  # flat-import mode used by the hidden harness suite
        import usage as usage_guard  # type: ignore[no-redef]
    return usage_guard


def validate_proof(proof: ConfinementProof, binding: object) -> None:
    """Bind a proof to the exact invocation and executing guard source.

    ``binding`` is the ``launch.InvocationBinding`` being authorized.  The
    proof must carry the exact bound commit, workspace, and provider of the
    invocation; its guard-source digests must equal the SHA-256 of the
    exact executing ``usage.py`` / ``usage_fetch.py`` bytes (so a
    substituted or operator-claimed guard source fails closed); and every
    operator credential store it names must live outside the model-visible
    workspace.
    """
    usage_guard = _import_usage_guard()
    if not isinstance(proof, ConfinementProof):
        raise ConfinementError("the confinement proof is not a genuine proof")
    try:
        if proof.bound_commit != binding.bound_commit:
            raise ConfinementError(
                "the confinement proof binds a different commit "
                f"({proof.bound_commit!r} != {binding.bound_commit!r})"
            )
        if proof.workspace != str(binding.workspace):
            raise ConfinementError(
                "the confinement proof binds a different workspace "
                f"({proof.workspace!r} != {str(binding.workspace)!r})"
            )
        if proof.provider != binding.provider.lower():
            raise ConfinementError(
                "the confinement proof binds a different provider "
                f"({proof.provider!r} != {binding.provider.lower()!r})"
            )
        executing = _executing_guard_source_digests()
        if proof.guard_source_digests != executing:
            raise ConfinementError(
                "the confinement proof does not bind the exact executing "
                "guard source (usage.py / usage_fetch.py digests differ); "
                "an operator-claimed or caller-controlled guard source is "
                "never accepted"
            )
        for store in proof.credential_stores:
            _assert_store_outside(usage_guard, store, binding.workspace)
    except AttributeError as exc:
        raise ConfinementError(
            f"the confinement proof cannot be bound to this invocation: {exc}"
        ) from exc


def _mint_synthetic_proof(
    binding: object,
    *,
    credential_stores: Optional[Sequence[str]] = None,
    guard_source_digests: Optional[Sequence[str]] = None,
) -> ConfinementProof:
    """**PRIVATE test seam** — mints a synthetic Task 8 proof.

    This seam exists only for the hermetic hidden suite so the Task 7
    guard/decision-table machinery can be exercised end-to-end without the
    pending Task 8 authority.  It is never exported on the public package
    surface and never accepted as real confinement evidence.

    The *binding* parts of the proof are still real: the guard-source
    digests are the SHA-256 of the exact executing ``usage.py`` /
    ``usage_fetch.py`` bytes (or caller-supplied values), and every
    credential store is verified to live outside the model-visible
    workspace.  Only the confinement claim itself (Task 8's proof that the
    model tools cannot reach ``.factory/`` and the stores) is synthetic.
    """
    workspace = str(binding.workspace)
    usage_guard = _import_usage_guard()

    if credential_stores is None:
        # Default: the exact operator store the guard will read (always
        # outside the model workspace by construction).
        stores = (usage_guard._default_env_file(),)
    else:
        stores = tuple(str(path) for path in credential_stores)
    if not stores:
        raise ConfinementError(
            "a synthetic proof must name at least one operator credential "
            "store to bind"
        )
    for store in stores:
        _assert_store_outside(usage_guard, store, binding.workspace)
    if guard_source_digests is None:
        digests = _executing_guard_source_digests()
    else:
        digests = tuple(str(value) for value in guard_source_digests)
        if len(digests) != len(GUARD_SOURCE_MODULES):
            raise ConfinementError(
                "a synthetic proof must carry one digest per guard-source "
                "module"
            )
    return ConfinementProof(
        bound_commit=binding.bound_commit,
        workspace=workspace,
        provider=binding.provider.lower(),
        guard_source_digests=digests,  # type: ignore[arg-type]
        credential_stores=stores,
        _mint=_PROOF_MINT_SECRET,
    )
