# Opsx Controller State

`/opsx-drive <change-id>` persists durable per-change controller state in this
directory.

This file can live in either location:

- project scope: `.opencode/opsx-controller/README.md`
- global scope: `~/.config/opencode/opsx-controller/README.md`

Tracked files:
- `README.md` documents the state contract.

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
- `task_counts`: current completed/total task counts
- `tracked_change_files`: deduplicated file inventory the controller trusts as
  belonging to the active change for later archive scope decisions
- `context_cache`: unified reusable background context, including:
  - `valid`: whether the cached summary matches the active tracked sources
  - `status`: `ready`, `stale`, or `missing`
  - `compiled_by`: `opsx-controller` or `opsx-implementer`
  - `updated_in_round`: controller round that last compiled or enriched it
  - `source_signature`: compact signature for the tracked prompt and artifact
    sources that must match before the cache is trusted on resume
  - `source_paths`: resolved global prompt paths plus current OpenSpec
    `contextFiles` used to derive the cache
  - `change_summary`: bounded background summary of goals, constraints,
    remaining work, and likely next files to inspect
  - `refresh_reason`: short reason when the controller rebuilds or invalidates
    the cache
- `last_review`: persisted strict review verdict and finding counts
- `archive`: persisted archive path, commit, failure reason, and blocked-run
  triage when relevant, including:
  - `triage.scope_basis`: short description of how archive scope was derived
  - `triage.in_scope_files`: trusted files already classified as belonging to
    the change
  - `triage.ambiguous_files`: changed files the archiver could not classify
    safely
  - `triage.retry_guidance`: direct next step for the operator
  - `triage.retry_outlook`: whether an immediate retry is expected to fail the
    same way
- `history`: compact round-by-round phase history

Resume semantics:
- If `context_cache.source_signature` still matches the required global
  `/opsx-*` prompt set and the current OpenSpec `contextFiles`, `/opsx-drive`
  reuses the cached background summary instead of forcing each phase to reread
  every background artifact.
- If `tracked_change_files` is missing, stale, or obviously narrower than the
  current accepted change plus successful implement history, `/opsx-drive`
  rebuilds that archive-scope evidence before trusting a resumed archive retry.
- When `tracked_change_files` remains valid, a later archive retry can reuse
  that evidence instead of depending only on the latest narrow fix round.
- If the tracked prompt paths, `contextFiles` list, or tracked artifact
  fingerprints change, `/opsx-drive` marks `context_cache` stale, rebuilds it
  before the next phase dispatch, and persists the new signature in the same
  state file.
- `opsx-implementer` may return an optional cache-enrichment payload. The
  controller remains the only writer of the authoritative state file and merges
  that update into `context_cache` with implementer provenance.
- If the state file says `completed`, `/opsx-drive` trusts it only when the
  archive metadata and on-disk archive path still match. Otherwise it downgrades
  the run to blocked archive state instead of reporting false success.
- If the state file says `blocked` and `phase=implement`, the controller resumes
  the next implementation round with `latest_fix_prompt` intact.
- If the state file says `blocked` and `phase=archive`, the controller retries
  archive after reporting the stored archive failure reason.

Safety rules:
- The controller supports exactly one change per run.
- Cached background context only reduces repeated setup reads. It never replaces
  live `openspec status`, `openspec instructions apply`, `openspec validate`,
  review inspection, or archive safety checks.
- `tracked_change_files` is reusable archive-scope evidence, not blanket
  permission to stage every dirty file. Unclear files stay out of scope and must
  be surfaced in blocked archive triage.
- The review gate is strict: any critical, warning, or note finding blocks
  archive.
- Only a fresh machine-readable `opsx-archiver` success may mark a change
  completed. Existing git history, archive directories, or prior blocked state
  must not be reconciled into success.
- The controller stops after 5 failed review rounds or 2 consecutive no-progress
  implementation rounds.

Operational note:
- After changing `opencode.json`, `.opencode/commands/`, or `.opencode/agents/`,
  restart OpenCode so the updated controller workflow is loaded.
