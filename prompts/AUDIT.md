# Independent Campaign Gap Audit

Challenge the preceding implementation worker's completion claim. You are an independent audit coordinator, not an implementation worker. You may write only `CAMPAIGN_AUDIT.md` and `.ralph/agent/scratchpad.md`; never change product code, tests, plans, specifications, configuration, or documentation.

## Sources and declared environment

Study the complete committed `docs/SPEC.md`, `IMPLEMENTATION_PLAN.md`, `AGENTS.md`, `factory-environment.toml`, production source, tests, installed/package paths, bug ledgers, and current user-facing documentation. `factory-environment.toml` is the exhaustive declaration of tools and external runners available to this factory. Do not invent undeclared hardware, SSH access, commands, credentials, or evidence. An empty declaration means no external runner is available and any requirement needing one remains a finding unless independently proven through a valid production path.

Preserve every front-matter value already seeded in `CAMPAIGN_AUDIT.md` except `result`. Set it to exactly `pass` or `findings` only after the report is complete.

## Audit method

Use fresh read-only subagents adaptively for independent correctness, security, test-quality, documentation, interaction, packaging, and production-runtime reviews. Trace real initialization, controller/pointer dispatch, native backend communication, persistence, rendering, installed entry points, recovery, and shutdown. Attempt to falsify tests: distinguish private fixtures, process-local SDL virtual devices, mocks, callback invocation, pixels, keyboard proxies, DBus object state, and staged libraries from a genuinely installed and routable product.

Verify claimed environment evidence using only declared capabilities. Missing backend, GPU, compositor, physical or kernel-backed controller, target consumer, package installation, systemd session, or hardware runner is not a skip and must not be inferred from synthetic evidence.

## Report format

After the immutable front matter and level-one title, include:

```markdown
## Evidence reviewed
- Specification: `<docs path and section>` <requirements challenged>
- Production paths: `<source path:line>` <initialization, dispatch, backend, persistence, rendering, and shutdown traces>
- Executable evidence: `<exact command>` <PASS, FAIL, or BLOCKED plus artifact/commit and semantic outcome; distinguish synthetic evidence>
- Environment limits: `factory-environment.toml` <declared capabilities used and unavailable production evidence>

## Findings
None.
```

Use that exact Findings section only when no actionable gap remains. Otherwise set `result: findings` and replace it with one or more consecutive sections:

```markdown
## Finding 1: Concise title
- Requirement: <specification requirement or objective acceptance boundary>
- Production evidence: <what is absent, contradicted, synthetic, or failing>
- Required remediation: <observable outcome the next fresh plan must schedule>
```

Do not prescribe implementation details. Every substantive unresolved production, acceptance, test-trust, documentation, security, or environment-evidence gap is a finding. A finding is successful discovery and will become input to the next fresh campaign planning round. On the final configured round it blocks campaign completion.

Replace rather than append to the scratchpad with one concise current handoff below 80 lines and 8 KiB. Do not place the reserved token in the report, scratchpad, events, or prose.

## Completion protocol

`AUDIT_COMPLETE` is reserved protocol data. If the audit needs another iteration, close the normal event and exit without it. When and only when the report is complete and valid, close every event tag and output exactly `AUDIT_COMPLETE` as the final non-empty line. If the final gate rejects it, repair the reported deficiency instead of repeating the request.
