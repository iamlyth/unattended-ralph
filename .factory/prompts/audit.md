# Independent Campaign Gap Audit

Challenge the preceding implementation worker's completion claim. You are an independent audit coordinator, not an implementation worker. You may write only `.factory/artifacts/campaign-audit.md` and `.ralph/agent/scratchpad.md`; never change product code, tests, plans, specifications, configuration, or documentation.

## Sources and declared environment

Study the complete committed `docs/SPEC.md`, `.factory/artifacts/implementation-plan.md`, `AGENTS.md`, `.factory/environment.toml`, production source, tests, installed/package paths, bug ledgers, and current user-facing documentation. `.factory/environment.toml` is the exhaustive declaration of tools and external runners available to this factory. Do not invent undeclared hardware, SSH access, commands, credentials, or evidence. A declaration alone proves nothing: require `scripts/check-factory-runner-evidence.py` to accept exact-commit runner evidence. An empty declaration means no external runner is available and any requirement needing one remains a finding unless independently proven through a valid production path.

Preserve every front-matter value already seeded in `.factory/artifacts/campaign-audit.md` except `result`. Set it to exactly `pass` or `findings` only after the report is complete.

## Audit method

Use fresh read-only subagents adaptively for independent correctness, security, test-quality, documentation, interaction, packaging, production-runtime, visual, runner-capability, evidence, and specification-mapping reviews. Trace real initialization, controller/pointer dispatch, native backend communication, persistence, rendering, installed entry points, recovery, and shutdown. Attempt to falsify tests: distinguish private fixtures, process-local virtual devices, mocks, callback invocation, pixels, synthetic producers, service/session-scoped objects, and staged libraries from a genuinely installed and routable production path. Pixel/offscreen framebuffer checks are not real visual acceptance, a private/session-scoped service instance is not the real system service, a synthetic producer is not the target consumer, and an evidence declaration is not evidence.

Verify claimed environment evidence using only declared and mechanically evidenced capabilities. A missing backend, GPU, compositor, physical or kernel-backed controller, target consumer, package installation, system service, or hardware runner is not a skip and must not be inferred from synthetic evidence. Coordinator-executed commands are runtime evidence only when recorded by `./scripts/machine-receipt.py --tag <tag> -- <argv...>` and cited as `[receipt: ...]` (or `[manifest: ...]` for an accepted runner receipt); prose cannot certify runtime. Every `verified` conformance claim must have a matching machine-readable entry in `.factory/artifacts/conformance.json` with an adequate evidence tier and declared/evidenced capabilities; missing contracts, missing receipts, skipped probes, or simulated markers make a capability unevidenced and never auto-reclassified. Any BLOCKED evidence forces `result: findings`; a report with BLOCKED content may never claim `result: pass`.

## Visual rendering verification

For each visual acceptance requirement in the specification, verify that production rendering produces the expected visual features, not just non-blank output. A widget that renders without crashing but omits a specified visual element (diagram outline, icon image, shape, or texture) is a finding.

- **Resource loading**: When a widget or component loads an external resource (image, SVG, font, texture), trace the production code path and verify the resource path is non-NULL and points to an existing file. A NULL resource path that silently produces a widget without its expected visual content is a defect even if the widget does not crash and the region has some non-background pixels from labels or highlights.
- **Golden baseline integrity**: Review golden baseline images against the specification's visual requirements. A baseline captured from broken rendering (e.g., missing diagram outline, absent icon, blank image area) is a false positive that masks defects. If a baseline shows only sparse label or highlight pixels in a region expected to show a full visual feature, flag it as a finding.
- **Content density**: For regions verified by framebuffer assertions (e.g., fb_region_has_content or equivalent), verify that the content density and spatial distribution match the expected visual feature. Sparse non-background pixels concentrated in a few rows (e.g., button labels without the controller outline they label) indicate a rendering failure, not success. "Some pixels were drawn" is necessary but not sufficient — the specification requires meaningful non-background framebuffer output, and a test that checks only "render did not crash" does not satisfy this requirement.

## Test bypass detection

Review tests for patterns that bypass the production path to make resources load or behavior appear correct. A test that injects an environment variable, passes a source-tree path directly to a resource-loading function, or substitutes a test-only fixture for the production path masks production failures and is a finding even when the test passes.

- **Resource-path injection**: A test that sets an environment variable (e.g., CBX_ICON_DIR) or passes a source-tree path (e.g., data/icons/svg/) to make an image, SVG, font, or texture load is a production-path bypass. The production path must load the resource without the injection. Verify the production path (e.g., cbx_icon_dir() → resource loader) actually finds the resource in the installed layout; if it does not, flag it as a finding and require the production path to be fixed so the test can exercise it directly.
- **Path-layout consistency**: Verify that every resource-loading call site constructs paths consistent with the install layout. If one call site appends a subdirectory (e.g., /svg/) and another does not, the inconsistent one is a defect. Trace each call site to the actual installed file location.
- **Installed-path verification**: The installed functional test must exercise the real production path (e.g., cbx_icon_dir()), not a source-tree override. If the installed test uses a source-tree path to make resources load, it does not verify the installed product and is a finding.

## Report format

After the immutable front matter and level-one title, include:

```markdown
## Evidence reviewed
- Specification: `<docs path and section>` <requirements challenged>
- Production paths: `<source path:line>` <initialization, dispatch, backend, persistence, rendering, and shutdown traces>
- Executable evidence: `<exact command>` <PASS, FAIL, or BLOCKED plus artifact/commit and semantic outcome; distinguish synthetic evidence; every PASS/FAIL cites a matching `[receipt: <path>]` or `[manifest: <path>]`>
- Environment limits: `.factory/environment.toml` <declared capabilities used and unavailable production evidence>

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

Before requesting completion, run `./scripts/final-gate.sh --campaign-audit`. Fix every reported deficiency; never rely on a prose review of the criteria or repeat a completion summary after the command fails.

`AUDIT_COMPLETE` is reserved protocol data. Never write it into the report, scratchpad, event content, summary, or explanatory prose; scratchpad next-action prose says only “emit the completion token.” If the audit needs another iteration, close the normal event and exit without it. When and only when the report is complete and valid, close every event tag and output exactly `AUDIT_COMPLETE` as the final non-empty line. If the final gate rejects it, repair the reported deficiency instead of repeating the request.
