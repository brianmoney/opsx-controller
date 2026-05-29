# Opsx Controller State

`/opsx-drive <change-id>` persists durable per-change controller state in this
directory.

This file can live in either location:

- project scope: `.claude/opsx-controller/README.md`
- global scope: `~/.claude/opsx-controller/README.md`

Ignored runtime files:

- `<change-id>.json` stores the live controller state for one change.

Expected JSON fields:

- `version`: state schema version, currently `3`
- `change`: OpenSpec change id
- `schema`: workflow schema from `openspec status`
- `status`: `running`, `blocked`, or `completed`
- `phase`: `implement`, `review`, `archive`, or `done`
- `round`: current controller round
- `max_rounds`: hard stop for repeated review failures
- `no_progress_streak`: consecutive no-progress implementation rounds
- `latest_fix_prompt`: exact reviewer handoff used to resume a blocked run
- `task_counts`: current completed and total task counts
- `tracked_change_files`: deduplicated file inventory the controller trusts as
  belonging to the active change for later archive scope decisions
- `context_cache`: unified reusable background context
- `last_review`: persisted strict review verdict and finding counts
- `archive`: persisted archive path, commit, failure reason, and blocked-run
  triage when relevant
- `history`: compact round-by-round phase history

Safety rules:

- The controller supports exactly one change per run.
- Cached background context never replaces live OpenSpec validation or current
  file inspection.
- `tracked_change_files` is reusable archive-scope evidence, not blanket
  permission to stage every dirty file.
- The review gate is strict: any critical, warning, or note finding blocks
  archive.
- Only a fresh machine-readable `opsx-archiver` success may mark a change
  completed.

Operational note:

- After changing `.claude/agents/`, restart Claude Code so updated agents are
  loaded.
