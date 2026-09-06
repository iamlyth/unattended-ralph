"""Hidden ``.factory/`` control-plane package root (FACTORY-LOOP-SPEC).

The fresh-context launcher is exposed only as ``python -m factory.loop.launch``
(no visible bare ``.factory/tools/`` wrapper): the hidden ``.factory/loop`` package is
importable under the public ``factory.loop`` name through the external-prefix
alias mechanism — the operator or installed launcher places ``factory`` on
``PYTHONPATH`` resolving to the canonical ``.factory/`` directory, and the
hidden namespace never gains a visible alias file in the repository.  The
committed ``AGENTS.md`` policy keeps the entire control plane confined to this
hidden namespace (HIDE-01); ``.factory-state/`` and ``.pi/`` are the only
runtime siblings.
"""
