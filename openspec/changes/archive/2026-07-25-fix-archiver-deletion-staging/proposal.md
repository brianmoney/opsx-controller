## Why

Every `opsx-archiver` worker moves `openspec/changes/<change>/` into
`openspec/changes/archive/YYYY-MM-DD-<change>/` and then stages only the archive
path, the synced `openspec/specs/` files, and trusted implementation files. The
deletion of the original change directory is never staged, so the archive commit
lands with those paths still showing as deleted-but-unstaged. `opsx-plan`'s
post-archive tracked-tree gate (`verify_post_archive_clean`) then correctly
refuses to continue and the change ends `status = failed` with
`last_result = post_archive_dirty_tracked` — even though the archive directory,
the archive commit, and `spec_sync_status = synced` are all present and correct.

This is the standing blocker to any adapter reaching `status = done` through
archive, first observed 2026-07-25 on a `claude-code` direct-dispatch run and
deliberately deferred out of `add-claude-code-direct-execution`.

## What Changes

- Add the change-directory deletion to the archiver's allowed staged set, so the
  rename (delete old path + add archive path) is committed as one unit.
- Give every archiver worker an explicit staging step that captures deletions
  under `openspec/changes/<change>/` before the pre-commit staged-set inspection.
- Tighten the pre-commit staged-set check so it *requires* the change-path
  deletions to be present, not merely tolerates them: an archive commit that
  leaves them unstaged is a failure the archiver reports as blocked rather than
  claiming success and letting the orchestrator discover the dirty tree.
- Apply the identical rule to all five archiver definitions so the adapters do
  not drift:
  - `adapters/claude-code/agents/opsx-archiver.md`
  - `adapters/opencode/agents/opsx-archiver.md`
  - `adapters/codex-cli/agents/opsx-archiver.toml`
  - `adapters/codex-cli/plugin/agents/opsx-archiver.toml`
  - `plugins/opsx-controller/agents/opsx-archiver.md`
- Keep the existing narrow-scope guarantee intact: the widened set adds only
  paths under the change directory being archived. Nothing else becomes
  stageable, and untracked files outside the archive set still stay unstaged.

No orchestrator change is proposed. `verify_post_archive_clean` is behaving
correctly; the defect is entirely in the worker instructions.

## Capabilities

### New Capabilities
- `archive-commit-hygiene`: the adapter-independent contract for what an
  archive commit must contain — the change-directory deletion together with the
  dated archive directory and synced specs — and how an archiver must fail when
  it cannot produce that commit.

### Modified Capabilities
- `codex-cli-adapter`: the archiver agent requirement enumerates the explicit
  archive commit scope and the pre-commit staged-file inspection; both gain the
  change-directory deletion.

## Impact

- Prompt/instruction files only: the five archiver agent definitions above.
- Affects all three adapters (`claude-code`, `opencode`, `codex-cli`) and the
  bundled controller plugin.
- Downstream effect on `opsx-plan`: `run_direct_change` can reach
  `phase = done` instead of stopping at `post_archive_dirty_tracked`.
- Installed copies are what the runtime reads, so the adapter install scripts
  must be re-run after the edits land.
- No Python, schema, or state-file format changes; no migration.
