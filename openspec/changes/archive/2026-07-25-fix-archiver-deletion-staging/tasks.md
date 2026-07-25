## 1. Fuller-variant archiver definitions

- [x] 1.1 In `adapters/opencode/agents/opsx-archiver.md`, add `openspec/changes/<change>/` as a fourth bullet of the allowed staged set in the "Determine the narrow explicit archive commit scope" step
- [x] 1.2 In the same file, add an explicit staging instruction after the move step: stage the change-directory deletion with `git add -A -- openspec/changes/<change>` so the move commits as one rename
- [x] 1.3 In the same file, make the pre-commit staged-set inspection bidirectional — fail closed when any staged path falls outside the explicit set **and** when the deletions under `openspec/changes/<change>/` are absent from `git diff --cached --name-status`
- [x] 1.4 Apply the same three edits to `adapters/codex-cli/agents/opsx-archiver.toml` inside `developer_instructions`, matching that file's unquoted-path prose style
- [x] 1.5 Apply the same three edits to `adapters/codex-cli/plugin/agents/opsx-archiver.toml`, keeping it byte-consistent with the non-plugin codex definition apart from any pre-existing differences
- [x] 1.6 Apply the same three edits to `plugins/opsx-controller/agents/opsx-archiver.md`
- [x] 1.7 Fix the duplicate step number `15` in `adapters/opencode/agents/opsx-archiver.md` while renumbering around the inserted step

## 2. Claude Code archiver definition

- [x] 2.1 In `adapters/claude-code/agents/opsx-archiver.md`, name `openspec/changes/<change>/` as in-scope in the explicit-staging step (step 13), which has no separate allowed-staged-set bullet list
- [x] 2.2 Add the `git add -A -- openspec/changes/<change>` staging instruction after the move step (step 12)
- [x] 2.3 Make the pre-commit inspection (step 14) bidirectional, as in 1.3
- [x] 2.4 Confirm the terse variant still reads coherently end to end after renumbering — no dangling references to old step numbers

## 3. Cross-adapter consistency test

- [x] 3.1 Add a test class to `tests/orchestrator/test_opsx_plan.py` in the style of `OpenCodeAgentModeTests` that reads all five archiver definitions from repo-relative paths
- [x] 3.2 Assert each definition instructs staging the change-directory deletion (match on the `git add -A -- openspec/changes` pathspec form)
- [x] 3.3 Assert each definition requires the deletion to be present in the staged set before committing
- [x] 3.4 Assert the allowed staged set names the change directory in the four definitions that enumerate it
- [x] 3.5 Run `python3 -m pytest tests/orchestrator/test_opsx_plan.py` (or the repo's runner) and confirm the new test passes and no existing test regresses

## 4. Install and verify

- [x] 4.1 Re-run `adapters/claude-code/install.sh`, `adapters/opencode/install.sh`, and `adapters/codex-cli/install.sh` so the runtime reads the updated definitions
- [x] 4.2 Diff each installed archiver copy against its repo source to confirm the staging rule actually landed in the installed file
- [x] 4.3 Run `openspec validate fix-archiver-deletion-staging --strict`
- [x] 4.4 Verify the fix end to end: archive a change through at least one adapter and confirm the archive commit contains both the change-directory deletions and the dated archive directory (`git show --name-status <sha>`)

  Verified by hand-simulating the exact staging sequence (move, `git add -A -- openspec/changes/<change>`, stage archive path + synced specs, inspect, commit) in a throwaway git repo rather than dispatching a real subagent run — user chose this over a live `opsx-plan run-one` to avoid real API spend and an unplanned real archive commit of this change. `git diff --cached --name-status` showed both `R100` renames (deletion+addition paired) and the synced spec `M`; `git status --short` was empty after commit. A second sim reproduced the original bug (staging only the archive path, omitting `git add -A -- openspec/changes/<change>`) and confirmed it leaves `D openspec/changes/<change>/...` after commit — the exact `post_archive_dirty_tracked` failure this change fixes — and that the new bidirectional check would have caught it pre-commit.

  This proves the git mechanics but not worker compliance: the acceptance evidence design.md names for this change — a real plan run reaching `status = done` through a dispatched adapter — has NOT been produced. That is an accepted, user-made trade-off made explicitly to avoid API spend and an unplanned archive commit, not an oversight. The residual risk is bounded: `verify_post_archive_clean` is untouched by this change and still fails closed if a worker ignores the staging instruction, so a non-compliant worker is caught at the orchestrator gate rather than silently succeeding. Archiving this very change is itself the natural first live exercise — its own change directory is untracked, so it exercises the remediated `git ls-files` guard path exactly as described above.
- [x] 4.5 Confirm the run reaches `status = done` rather than failing with `last_result = post_archive_dirty_tracked`, and record which adapter was exercised

  Not exercised through a real adapter dispatch (see 4.4). The git-mechanics simulation is adapter-agnostic — all five definitions now issue the identical `git add -A -- openspec/changes/<change>` pathspec, verified by the new `ArchiverDeletionStagingTests`. A live end-to-end run through `opsx-plan run-one` (or a real plan) remains the outstanding acceptance evidence the design doc calls for; the user can run it when ready.

  To be explicit: the design.md acceptance bar (a dispatched worker reaching `status = done`) has NOT been met by this task. This was a deliberate, user-made deferral to avoid API spend and an unplanned archive commit of this change, not an oversight. Risk is bounded because `verify_post_archive_clean` remains untouched and still fails closed on a dirty tree if a dispatched worker doesn't follow the prompt. Archiving this change through a real adapter is itself the first opportunity to close this gap, since the change directory is untracked and so exercises the untracked/`git ls-files` guard path added in section 6.
- [x] 4.6 Resolve the design's open question — whether an empty-history repository needs a special case for the deletion-present check — and note the answer in the change before archiving

  First resolution (superseded — see below): no special case needed, on the reasoning that an untracked change directory shows as `??` before and after the move, never as `D`, so the deletion-present check would no-op.

  That reasoning was backwards and was reversed during verification (section 6): the bidirectional check fails closed on absence, so "never shows as `D`" actually means "always reports a missing deletion and blocks every archive," not "harmlessly no-ops." The final resolution is that a special case IS needed: a `git ls-files -- openspec/changes/<change>` guard, run once before staging, that covers both the untracked-with-commits case and the empty-history case. See `design.md` Open Questions (the resolution note and the second Decisions entry beneath it) and task 6.7, which performed this rewrite.

## 5. Close out

- [x] 5.1 Update the `opsx-archiver-git-hygiene-defect` memory to record the fix, or delete it if fully resolved

## 6. Remediation: untracked-change-directory regression found in verification

Verification of section 4 found a critical defect the original implementation missed:
`git add -A -- openspec/changes/<change>` was written unconditionally. When the change
directory was never committed, that pathspec matches nothing on disk or in the index and
git fails hard with `fatal: pathspec 'openspec/changes/<change>' did not match any files`
(exit 128). The new bidirectional pre-commit check compounded it: with an untracked
directory there are no deletions in `git diff --cached --name-status`, and the check as
originally written would fail closed on that absence unconditionally, blocking every
archive of an untracked change directory. This regressed a case that worked before this
change shipped — an untracked change directory moved to the archive path used to stage as
plain additions and leave a clean tree. The design doc's Open Questions section had
concluded the opposite ("no special case is needed") from the same empirical evidence,
because it read "never staged as `D`" as proof the check was a harmless no-op rather than
proof it would always report a missing deletion.

- [x] 6.1 Rewrite the staging step in all five archiver definitions to guard on
  `git ls-files -- openspec/changes/<change>`: run `git add -A -- openspec/changes/<change>`
  only when it lists files; skip staging (there is no deletion) when it lists none

  This fixes the prompt text and is verified by simulation (4.4, 6.12) and by the
  content test (6.8), but it does not by itself close the live-dispatch gap noted
  under 4.4/4.5: no dispatched worker has actually run this guard. That evidence
  is still outstanding and deferred deliberately, not closed here.
- [x] 6.2 Rewrite the pre-commit inspection step in all five definitions so the
  deletion-present half of the bidirectional check applies only when the change directory
  was tracked; an untracked directory's absent deletion is expected, not a failure
- [x] 6.3 Preserve every substring the consistency test asserts on
  (`git add -A -- openspec/changes`, `deletions under`, `absent from the staged`,
  `(the deletion left by the move)`, `the change-directory deletion staged in step`)
  across the rewrite
- [x] 6.4 Qualify the allowed-staged-set bullet in the four fuller definitions and the
  inline scope sentence in `plugins/opsx-controller/agents/opsx-archiver.md` with
  "when that is tracked" / "when tracked", keeping the parenthetical marker intact
- [x] 6.5 Fix the stale cross-reference in `adapters/opencode/agents/opsx-archiver.md`
  step 16 ("the implementation files from step 10" → "step 6", where `tracked_change_files`
  is actually read)
- [x] 6.6 Update `specs/archive-commit-hygiene/spec.md` and
  `specs/codex-cli-adapter/spec.md` to scope the deletion-staging and deletion-present
  requirements to "when the change directory is tracked", and add scenarios covering the
  untracked case reaching `status=archived` with no block
- [x] 6.7 Rewrite the design doc's Open Questions resolution: a special case (the
  `git ls-files` guard) is needed, covering both the untracked-with-commits and
  empty-history cases; add a matching Decisions entry
- [x] 6.8 Extend `ArchiverDeletionStagingTests` with assertions that all five definitions
  contain `git ls-files -- openspec/changes` and describe the untracked case as a
  non-failure
- [x] 6.9 Run the full suite (created a repo-local `.venv` since none existed; `.venv/bin/activate`
  referenced by the runbook was not present in the working tree) — 459 passed, 9 subtests
  passed (457 baseline + 2 new assertions), no regressions
- [x] 6.10 `openspec validate fix-archiver-deletion-staging --strict` — passed after
  restructuring one requirement so `SHALL` stayed on the first line of the requirement
  text (the validator only reads the first line, and moving the tracked-time qualifier to
  the front of the paragraph had pushed `SHALL` to a continuation line)
- [x] 6.11 Re-ran `adapters/claude-code/install.sh --global --verify`,
  `adapters/opencode/install.sh --global --verify`, and `adapters/codex-cli/install.sh`
  (codex-cli's flag parser has a pre-existing order-dependent bug — `--verify` must
  precede `--global` for both flags to take effect — out of scope for this change);
  diffed installed copies against repo sources: claude-code and codex-cli identical,
  opencode differs only by the `{env:OPSX_ARCHIVER_MODEL}` → model expansion
- [x] 6.12 Verified all three cases in a throwaway git repo: tracked change directory →
  `git add -A` exits 0, `git diff --cached --name-status` shows `R100`, clean tree after
  commit; untracked change directory in a repo with commits → guard skips the add, stages
  as a plain `A`, clean tree after commit, no fatal error; empty-history repo → same as
  untracked-with-commits, no fatal error

## 7. Remediation: staging-step contradiction found in `/opsx:verify`

Verification of the remediated change found a second, milder inconsistency. Section 6
correctly added the deletion-staging step and the tracked-qualified pre-commit check to
all five definitions, but three of them left the *following* step's enumeration
untouched: "using explicit staging **only** for the archive path, synced
`openspec/specs/` files, and the implementation files from step N" — a three-item list
that omits the deletion staged one step earlier. `adapters/claude-code` (which appends
"and the change-directory deletion staged in step 13") and `plugins/opsx-controller`
(which says "Stage only the rest of the explicit archive set") did not have this
problem. A worker reading "only" as an exclusive scope could have unstaged the deletion
it had just staged; the step-17 deletion-present check would then have blocked the
archive. Not a live failure — the pre-commit check catches it — but it contradicted the
`Archiver definitions stay consistent across adapters` requirement, which asserts all
five carry the same rule.

- [x] 7.1 Append the deletion to the explicit-staging enumeration in
  `adapters/codex-cli/agents/opsx-archiver.toml` and
  `adapters/codex-cli/plugin/agents/opsx-archiver.toml` step 13 ("…, and the
  change-directory deletion staged in step 12"), keeping the two files byte-identical
- [x] 7.2 Rewrite `adapters/opencode/agents/opsx-archiver.md` step 16 to mirror the
  `plugins/opsx-controller` phrasing rather than extend the "only" list: "staging only
  the rest of the explicit archive set: … Leave the change-directory deletion from step
  15 staged; do not unstage it"

  User chose this wording over the four-item-list form used for the codex files, so the
  ambiguity is removed at its source rather than patched around.
- [x] 7.3 Re-ran all three install scripts (`adapters/claude-code/install.sh --global
  --verify`, `adapters/opencode/install.sh --global --verify`,
  `adapters/codex-cli/install.sh --verify --global` — note the flag order, per the
  pre-existing codex arg-parsing bug recorded in 6.11) and diffed installed copies
  against repo sources: claude-code and codex-cli byte-identical, opencode differs only
  by the `{env:OPSX_ARCHIVER_MODEL}` → model expansion. Confirmed the new clause is
  present in each installed file, not only in the repo tree
- [x] 7.4 Pin the new wording in `ArchiverDeletionStagingTests`: added a
  `STAGING_STEP_RECONCILES_DELETION` per-file clause map (the three variants word it
  differently, so one shared substring will not do), a `_read_unwrapped` helper that
  collapses line wrapping before matching, and
  `test_staging_step_reconciles_the_deletion_in_every_definition`, which replaces the
  narrower `test_claude_code_names_change_directory_inline` and also asserts the map
  covers `ARCHIVER_FILES` exactly, so a sixth adapter cannot be added without a pinned
  clause

  Trade-off: the pinned clauses contain literal step numbers, so renumbering any
  definition fails this test until the map is updated. Intentional — a stale step
  cross-reference is the same defect class as 6.5 — and noted in a comment above the map.
- [x] 7.5 Negative-checked the new assertion by patching `_read` in memory to revert
  opencode step 16 to the old three-item list: the test failed with the intended
  message, confirming it pins rather than passing vacuously
- [x] 7.6 Re-ran the full suite (459 passed, 9 subtests — unchanged totals, since 7.4
  replaced a test rather than adding one) and
  `openspec validate fix-archiver-deletion-staging --strict` (valid)

Not addressed, carried forward as known and out of scope:

- The live-dispatch acceptance evidence from 4.4/4.5 remains outstanding. Nothing in
  section 7 changes that; these were prompt-text and test edits only.
- `adapters/opencode/agents/opsx-archiver.md` post-move failure handling still says "If
  a failure happens after step 13", but the move is step 14 (step 13 is delta-spec
  sync), so it also covers a sync-stage failure where nothing was moved. Pre-existing
  and off by one before this change too; the codex definitions correctly reference their
  move step. Left alone deliberately.
