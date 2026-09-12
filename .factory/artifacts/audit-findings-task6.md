# Spec Compliance Audit — Task 6: Resolve orphaned study-report files

## Scope

Task 6 requires keeping `git status` clean: the repo root must contain no
untracked files, and the two orphaned study artifacts (`architecture-study.md`,
`subsystem-ui-report.md`) must be relocated under `.factory/artifacts/`, committed
deliberately, or removed — without silently discarding valuable evidence. No
production code, build, or test-surface change is permitted.

## Verified state

- **Acceptance met:** Repo root contains no untracked files
  (`git ls-files --others --exclude-standard | grep -v '^\.factory/'` → empty).
- **Both artifacts relocated and tracked:** `architecture-study.md` (119 lines) and
  `subsystem-ui-report.md` (230 lines) now live under `.factory/artifacts/` and are
  tracked. Content is substantive (real analysis, not stubs), so evidence was
  preserved by moving, not discarded.
- **No dangling references:** No `architecture-study` / `subsystem-ui-report`
  references remain in `src/`, `tests/CMakeLists.txt`, `CMakeLists.txt`, or `docs/`.
- **Production tree reverted cleanly:** Prior incidental edits to
  `src/manager/manager.c`, `settings_tab.c/h`, and `tests/CMakeLists.txt` were
  stripped (see `db20d2bd`); no residual `test_settings_controllers_sync` or
  subsystem-study references remain in production sources.
- **Build state properly ignored:** `build/`, `build-planner/`, `.factory-state/`,
  `.pi/output/`, `.ralph/` are gitignored, not accidentally version-controlled.
- **Verification command** `git status --porcelain` reports the tree clean except
  for the note below.

## Findings

### WARN — Stray untracked file keeps `git status --porcelain` non-empty

**File path:** `.factory/artifacts/audit-findings-task6-security.md`

After the final task-6 commit, a separate (concurrent security-auditor)
artifact was produced and left **untracked** in `.factory/artifacts/`. Running the
advertised verification `git status --porcelain` therefore currently returns
`?? .factory/artifacts/audit-findings-task6-security.md` rather than empty output.

This does not violate Task 6's core acceptance (the file is not at the repo root, and
Task 6's two study artifacts are correctly relocated and tracked), and it is not a
Task-6 implementation defect — it is a by-product of a parallel audit run that
happened after the task's commit. It does, however, mean the strict
`git status --porcelain` → empty assertion is not literally true at this instant.

**Recommendation:** commit the security-audit findings file (or the orchestrating
process's audit batch) so the working tree is fully clean, matching the Task-6
verification exactly. Do not attribute this to the Task-6 change; it is a separate
audit-output hygiene item.

## Conclusion

Task 6's implementation is compliant with the specification. Both study artifacts
are relocated to `.factory/artifacts/` and tracked, the repo root has no untracked
files, no production/build/test surface was altered, and no evidence was discarded.
The single untracked file noted above is an external audit by-product, not a Task-6
defect, and is captured here so the tree can be restored to completely empty output.
