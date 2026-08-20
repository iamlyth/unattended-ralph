# Product Specification — Pending Human Supply

**Status:** SPEC_PENDING_HUMAN_SUPPLY

This repository is the product-neutral Ralph/factory boilerplate. It does not
define a product, and it must never be planned against.

`SPEC_PENDING_HUMAN_SUPPLY` is the canonical blocking marker. `scripts/check-spec-provided.sh`
hard-blocks planning and every completion gate while this marker is present:
an autonomous worker must never plan against a placeholder, infer a product
contract, or invent requirements from this file. No requirement can be
formulated, no conformance row can exist, and no campaign objective can be
satisfied until a human replaces this placeholder with the real canonical
specification of the adopting product and commits it (which removes the
marker).

Until then:

- The factory declares no tools and no runners (`.factory/environment.toml` is
  the exhaustive declaration).
- The conformance sidecar (`.factory/artifacts/conformance.json`) and the
  requirement policy map (`.factory/requirement-policy.json`) remain empty
  templates.
- Do not manufacture requirements, capabilities, evidence, or receipts from
  this placeholder.

## Supply procedure (human)

1. Write the adopting product's real technical specification into this file
   (or point `.factory/config.toml` `[project].spec` at a committed
   replacement path).
2. Remove every occurrence of `SPEC_PENDING_HUMAN_SUPPLY`.
3. Commit. The planning and completion gates unblock automatically.

The boilerplate's process documentation (`README.md`, `docs/FACTORY.md`,
`docs/OPERATIONS.md`, `AGENTS.md`) remains authoritative for how the factory
operates once a real specification is supplied.
