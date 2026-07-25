## Context

`opsx-plan` verifies archive completion with evidence, not worker prose. After
the archiver returns, `run_direct_change` checks archive evidence, runs fast
checks, then calls `verify_post_archive_clean` (`orchestrator/opsx-plan.py:3478`),
which fails the change when the tracked worktree is dirty:

> tracked worktree is dirty; archive must commit or restore tracked changes
> before the next change starts

Every archiver definition tells the worker to move `openspec/changes/<change>/`
into the dated archive directory and then stage an explicitly enumerated set:

- the archive path after the move
- changed files under `openspec/specs/` from delta sync
- implementation files from controller-owned `tracked_change_files`

That list has never included the source change directory. A filesystem move
leaves git seeing deletions at the old paths; nothing stages them. The commit is
created, archive evidence is real, `spec_sync_status` is `synced` — and then the
tracked-tree gate fires with `last_result = post_archive_dirty_tracked`. The
change ends `failed` despite the archive being materially correct.

The same enumeration is duplicated verbatim across five definitions:

| File | Format |
| --- | --- |
| `adapters/claude-code/agents/opsx-archiver.md` | markdown + YAML frontmatter |
| `adapters/opencode/agents/opsx-archiver.md` | markdown + YAML frontmatter |
| `adapters/codex-cli/agents/opsx-archiver.toml` | TOML `developer_instructions` |
| `adapters/codex-cli/plugin/agents/opsx-archiver.toml` | TOML `developer_instructions` |
| `plugins/opsx-controller/agents/opsx-archiver.md` | markdown + YAML frontmatter |

The claude-code definition is the terse variant (no `Post-move failure handling`
section, no explicit allowed-staged-set bullet list); the others are the fuller
variant. The fix has to land in both shapes.

## Goals / Non-Goals

**Goals:**

- Make the archive commit a true rename: change-directory deletions and dated
  archive additions in one commit.
- Keep the narrow-scope guarantee — widen the allowed set by exactly the change
  directory, nothing else.
- Make an unstaged deletion a worker-side blocked result rather than an
  orchestrator-side dirty-tree failure discovered after the commit exists.
- Keep all five definitions in lockstep, enforced by a test rather than by
  convention.

**Non-Goals:**

- Changing `verify_post_archive_clean` or any orchestrator gate. The gate is
  correct; loosening it would hide real dirt.
- Changing the archiver's JSON response shapes, the archive commit message, or
  the `tracked_change_files` scope-evidence mechanism.
- Reworking the duplication itself. Consolidating five prompts into one shared
  source is a real improvement but is a separate change; this one keeps the
  existing structure and adds a test to catch drift.
- Auto-running the adapter install scripts as part of implementation.

## Decisions

### Stage the deletion with `git add -A -- openspec/changes/<change>`

`git add -A` over the change path stages deletions (and any residue) under that
path only. Alternatives considered:

- **`git mv` instead of a filesystem move.** Would stage the rename atomically
  and is arguably the cleanest primitive, but it rewrites the move step in all
  five prompts and interacts awkwardly with the existing post-move restore
  handling, which assumes a plain filesystem move it can reverse. Rejected as a
  larger blast radius than the defect warrants.
- **`git add -A` with no pathspec.** Simplest, and exactly what a human would
  type — but it stages everything dirty in the worktree, destroying the
  narrow-scope guarantee that the ambiguity triage is built around. Rejected.
- **`git rm -r --cached` on the old path.** Stages the deletion but desyncs
  index and worktree if anything unexpected remains on disk. Rejected as more
  surprising than `add -A` with a pathspec.

The pathspec form gives the same result as `git mv` for the commit contents
while leaving the move/restore choreography untouched.

### Make the pre-commit check bidirectional

Today the inspection is one-directional: *no staged file may fall outside the
explicit set*. A missing deletion passes that check trivially, which is why the
defect escaped to the orchestrator. The check becomes:

1. no staged path outside the explicit archive set (unchanged), **and**
2. deletions under `openspec/changes/<change>/` are present in
   `git diff --cached --name-status`

Failing (2) returns blocked JSON. This is what converts the failure mode from
"commit lands, plan fails afterward with a dirty tree" into "archiver reports
blocked with actionable triage and no bad commit". Worth the extra step: the
orchestrator's message points at the tree, not at the cause.

### Add the change directory to the enumerated allowed staged set

The four fuller definitions list the allowed staged set explicitly; the
claude-code one does not. Both get the change directory named as in-scope — for
the fuller ones as a fourth bullet, for claude-code inline in its step 13
staging sentence. Naming it matters beyond documentation: the ambiguity rule
tells the worker to return `ambiguous archive commit scope` when it cannot name
the staged set up front, and a path it must stage but is not listed as allowed
is exactly the kind of contradiction that produces a spurious block.

### Enforce cross-adapter consistency with a content test

`tests/orchestrator/test_opsx_plan.py` already asserts prompt content across
agent definition files (see `OpenCodeAgentModeTests`, which checks `mode: all`
and the `$HOME` expansion rule across four files). A new test in that style
reads all five archiver definitions and asserts each mentions staging the
change-directory deletion and the deletion-present pre-commit check. This is the
only mechanism in the repo that has actually prevented adapter prompt drift, and
the codex TOML files are plain text reads like the rest.

## Risks / Trade-offs

- **Prompt compliance is probabilistic.** A worker can ignore an instruction, so
  a wrong-shaped commit is still possible. → Mitigated by the bidirectional
  pre-commit check (a compliant worker catches itself) and by
  `verify_post_archive_clean` remaining untouched as the backstop. The gate
  keeps working exactly as it does now if the prompt is ignored.
- **`git add -A` with a pathspec stages anything under the change directory,**
  including files an implementer left there unintentionally. → Those files are
  inside the change being archived and belong in the archive commit; the dated
  archive directory is where they end up. This is narrower than the archive
  path's own staging rule, which already stages the whole moved tree.
- **Five near-identical edits invite drift.** → The consistency test is the
  mitigation, and it fails loudly on the next adapter added without the rule.
- **Editing repo files does not change runtime behavior** — the adapters run
  from installed copies under `~/.claude/agents/`, `~/.config/opencode/`, and
  `~/.codex/agents/`. → Implementation ends with re-running each adapter's
  `install.sh`; verification of the real fix must happen against installed
  copies, not the repo tree.
- **Verification requires a full end-to-end run.** No unit test can prove the
  worker produces a clean tree. → The content test covers the instruction; a
  real plan run reaching `status = done` is the acceptance evidence, and that
  run is itself the first archive of this very change.

  **Acceptance evidence status:** this acceptance bar has not been met as of
  implementation. No live adapter dispatch was performed; tasks 4.4/4.5/6.12
  instead hand-simulated the git staging sequence in a throwaway repo, which
  proves the git mechanics but not that a dispatched worker follows the prompt.
  This was a deliberate, user-made trade-off to avoid API spend and an
  unplanned archive commit of this change, not an oversight — see tasks.md
  4.4/4.5/6.1. `verify_post_archive_clean` remains untouched and still fails
  closed if a worker ignores the instruction, bounding the residual risk.
  Archiving this change is itself the first opportunity to close the gap,
  since its own change directory is untracked and so exercises the
  `git ls-files` guard path added in section 6.

## Migration Plan

No data or format migration. Rollout is: edit the five definitions, add the
consistency test, re-run the three `install.sh` scripts, then confirm a plan run
archives to `status = done` rather than `post_archive_dirty_tracked`. Rollback
is reverting the prompt edits; nothing persists state that would need unwinding.

## Open Questions

- Should the deletion-present check be relaxed for a repository with no commits
  yet (the codex and opencode definitions already special-case empty history for
  `git log`)? **Resolved during implementation, and revised after verification
  found the first resolution wrong: a special case is needed.**

  The empirical observation was correct as far as it went: an untracked change
  directory shows as `?? openspec/changes/<change>/...` before the move and
  `?? openspec/changes/archive/.../...` after — never as a `D` deletion, staged
  or not. But the conclusion drawn from it was backwards. The deletion-present
  check does not silently no-op when the deletion is absent — it *fails
  closed* on absence, by design (see "Make the pre-commit check bidirectional"
  above). So for an untracked change directory, the check would unconditionally
  block every archive: there is never a deletion to find, so the check would
  always report one missing.

  The analysis was also incomplete: it considered only the empty-history repo
  the codex and opencode prompts already special-case for `git log`. It missed
  a second, more common case — an untracked change directory inside a repo that
  already has commits (e.g. this very change directory, which was never
  committed). Both cases hit the same failure, and neither is exotic.

  There is a second, sharper defect from the same root cause: the staging step
  itself. `git add -A -- openspec/changes/<change>` was written unconditionally.
  When the change directory is untracked, that pathspec matches nothing on disk
  and nothing in the index, and git fails hard with
  `fatal: pathspec 'openspec/changes/<change>' did not match any files` (exit
  128) — before the pre-commit check even runs. This regressed a case that
  worked before this change shipped: an untracked change directory moved to the
  archive path staged as plain additions and left a clean tree.

  The fix is a tracked/untracked guard, run once before staging:
  `git ls-files -- openspec/changes/<change>`. The index is untouched by the
  move (a filesystem move doesn't touch git's index), so this command gives the
  same answer before and after the move. Empty output means untracked — skip
  the `git add -A` deletion-staging call entirely, since there is nothing to
  stage, and treat an absent deletion in the pre-commit check as expected
  rather than a failure. Non-empty output means tracked — stage the deletion
  and require its presence in the pre-commit check, exactly as originally
  designed.

## Decisions

### Guard deletion-staging and the deletion-present check on `git ls-files`

Both the staging step and the pre-commit bidirectional check are conditioned on
whether `openspec/changes/<change>` is tracked, checked once via
`git ls-files -- openspec/changes/<change>` before the move. Tracked: stage the
deletion with `git add -A -- openspec/changes/<change>` and require it present
before committing. Untracked: skip the `git add -A` call (it would exit 128 on
an unmatched pathspec) and treat an absent deletion as expected, not a failure.
This covers both the untracked-with-commits case and the empty-history case
with one guard, since `git ls-files` returns empty in both.
