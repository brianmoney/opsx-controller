## Purpose

Define the commit hygiene requirements for archive operations when moving OpenSpec changes from `openspec/changes/<change>/` to `openspec/changes/archive/YYYY-MM-DD-<change>/`. Where the dated archive directory is gitignored, the orchestrator still needs it on disk as local evidence, but its contents are not part of the repository's tracked history.

## Requirements

### Requirement: The archive destination is never staged or committed

Where `openspec/changes/archive/` is gitignored, every `opsx-archiver` worker,
on every adapter, SHALL perform the physical move to
`openspec/changes/archive/YYYY-MM-DD-<change>/` but SHALL NOT force-stage or
commit any path under that destination.

#### Scenario: Archive directory stays untracked

- **WHEN** the archiver has moved a change to a gitignored `openspec/changes/archive/YYYY-MM-DD-<change>` and is preparing the archive commit
- **THEN** `git diff --cached --name-status` contains no path under `openspec/changes/archive/`, and `git status --short` shows the new directory as ignored, not staged

### Requirement: Archive-commit evidence follows the ignore status of the destination

The orchestrator SHALL decide whether an `archive(<change>):` commit is
required evidence of completion by asking git whether
`openspec/changes/archive/` is covered by an ignore rule, independent of
whether any path under it happens to be tracked.

#### Scenario: Tracked archive destination requires the archive commit

- **WHEN** `openspec/changes/archive/` is not gitignored and a change is archived with no recorded `archive(<change>):` commit, or with one that is unreachable from `HEAD`
- **THEN** the orchestrator fails the change rather than marking it done, because the archive was not durably recorded

#### Scenario: Ignored archive destination tolerates a missing archive commit

- **WHEN** `openspec/changes/archive/` is gitignored and a change is archived with no recorded `archive(<change>):` commit
- **THEN** the orchestrator logs a note and still marks the change done, treating the on-disk dated directory and the change directory's removal as the load-bearing evidence

### Requirement: The archive commit includes the change-directory deletion

Every `opsx-archiver` worker, on every adapter, SHALL stage the removal of
`openspec/changes/<change>/` as part of the archive commit, when the change
directory is tracked at archive time. The archiver SHALL NOT leave the
change-directory deletion as an unstaged worktree modification.

The archiver SHALL determine whether the change directory is tracked by
running `git ls-files -- openspec/changes/<change>` before staging. When that
check lists no files, the change directory was never committed: there is no
deletion to stage, and the archiver SHALL NOT run `git add -A` on that
pathspec, since it would fail with `fatal: pathspec ... did not match any
files`.

When the change directory was untracked and no other files (synced specs,
trusted implementation files) are in scope this round, there is nothing to
stage. The archiver SHALL skip creating a commit in that case, report
`commit` as an empty string, and still report `status=archived` — the
completed move is success on its own.

#### Scenario: Change-directory deletion is committed alone

- **WHEN** the archiver has moved a tracked `openspec/changes/<change>` to `openspec/changes/archive/YYYY-MM-DD-<change>` and is preparing the archive commit
- **THEN** `git diff --cached --name-status` shows only the deletions under `openspec/changes/<change>/` plus any synced specs or trusted implementation files, never anything under the dated archive directory

#### Scenario: Untracked change directory with nothing else in scope produces no commit

- **WHEN** the change directory was never committed, is moved to the dated archive directory, and no specs or implementation files are in scope this round
- **THEN** the archiver does not attempt to stage a deletion, does not create a commit, reports `commit` as an empty string, does not return blocked, and leaves a clean tracked tree

#### Scenario: Tracked worktree is clean after the archive commit

- **WHEN** the archiver returns `status=archived` for a change
- **THEN** `git status --short` reports no tracked modifications left behind by the archive, and `opsx-plan`'s post-archive tracked-tree check passes instead of failing with `post_archive_dirty_tracked`

### Requirement: The change directory is inside the explicit archive commit scope

Each archiver SHALL include paths under `openspec/changes/<change>/` in the explicit archive commit scope it names before mutating files, alongside the existing members of that scope:

- changed files under `openspec/specs/` created or updated by delta sync
- implementation files from controller-owned archive-scope evidence that live
  outside the change directory

Where `openspec/changes/archive/` is gitignored, the dated archive directory
under it is never a member of this scope, and naming it as in-scope is a
defect.

Widening the scope SHALL NOT relax the narrowness guarantee for any other path.
Files that are neither under the change directory, synced `openspec/specs/`
paths, nor the trusted implementation set remain out of scope, and untracked
files outside the archive set SHALL remain unstaged.

#### Scenario: Change-directory paths are in scope

- **WHEN** the archiver names its explicit staged set before syncing or moving anything
- **THEN** that set includes `openspec/changes/<change>/` and the archiver does not report `ambiguous archive commit scope` merely because the change directory will be deleted

#### Scenario: Unrelated dirty files stay out of the commit

- **WHEN** the worktree contains tracked edits or untracked files outside the explicit archive set at archive time
- **THEN** the archiver leaves those files unstaged and the archive commit contains only the explicit archive set

### Requirement: The pre-commit staged-set inspection requires the deletion

The pre-commit inspection of `git diff --cached` SHALL verify the staged set in
both directions: no staged path may fall outside the explicit archive set, and,
when the change directory was tracked before the move, the change-directory
deletions MUST be present.

If the change directory was tracked, was moved, and its deletions are not
staged, the archiver SHALL fail closed and return blocked JSON rather than
creating the commit or reporting `status=archived`. When the change directory
was untracked before the move, absent deletions are expected and SHALL NOT
block the archive.

#### Scenario: Missing deletion blocks the archive

- **WHEN** the archiver inspects the staged set before committing, the change directory was tracked, and the deletions under `openspec/changes/<change>/` are absent
- **THEN** the archiver returns a blocked result naming the unstaged change-directory deletion, and does not create the archive commit

#### Scenario: Extra staged path still blocks the archive

- **WHEN** the staged set contains a path outside the explicit archive set
- **THEN** the archiver fails closed exactly as before this change

### Requirement: Archiver definitions stay consistent across adapters

All installed `opsx-archiver` worker definitions SHALL carry the same
change-directory staging rule, so no adapter can archive successfully while
another leaves a dirty tree. The definitions covered are:

- `adapters/claude-code/agents/opsx-archiver.md`
- `adapters/opencode/agents/opsx-archiver.md`
- `adapters/codex-cli/agents/opsx-archiver.toml`
- `adapters/codex-cli/plugin/agents/opsx-archiver.toml`
- `plugins/opsx-controller/agents/opsx-archiver.md`

#### Scenario: Every adapter archives with a clean tree

- **WHEN** a plan run archives a change through the `claude-code`, `opencode`, or `codex-cli` adapter
- **THEN** the resulting archive commit contains the change-directory deletion and the run can reach `status = done`
