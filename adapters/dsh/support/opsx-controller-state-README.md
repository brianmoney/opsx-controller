# Opsx Controller State (dsh)

`opsx-plan` keeps plan-level bookkeeping (approvals, per-change records, git
delivery state, notified events) in `.opsx-plan/<plan-name>.state.json`, and
persists the durable per-change controller state for direct plan execution in
`.opsx-controller/<change-id>.json` at the project root. That per-change file
is the authoritative state the controller writes and the dsh worker reads via
`STATE_FILE` when dispatching `opsx-dsh-worker --role implementer`,
`--role reviewer`, or `--role archiver`. The `.opsx-plan/workers/` snapshot
directory used by other adapters is not used for dsh: the per-change file the
worker sees is always `.opsx-controller/<change-id>.json`.

On resume, the controller validates the existing `.opsx-controller/<change-id>.json`
before regenerating it: malformed JSON, or a file belonging to a different
change, stops the run with an actionable diagnostic telling the operator to fix
or remove the broken state file.

The dsh adapter installs its support files to a client-neutral controller
support directory. This file can live in either location:

- project scope: `.opsx-controller/dsh/README.md`
- global scope: `~/.config/opsx-controller/dsh/README.md`

The `opsx-dsh-worker` shim resolves the role instruction files
(`opsx-implementer.md`, `opsx-reviewer.md`, `opsx-archiver.md`) project-first
from `.opsx-controller/dsh/agents/` relative to the working directory, then
global from `~/.config/opsx-controller/dsh/agents/`. dsh reads the project
`AGENTS.md` from the working directory natively; role specialization is
carried in the composed prompt, not in per-role startup files.

Tracked files:
- `README.md` documents the state contract.
- `agents/opsx-*.md` are the role instruction files.
- `plan-authoring.md` is the shared client-neutral plan-authoring reference.

Ignored runtime files:
- `.opsx-controller/<change-id>.json` stores the live controller state for one
  change.

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
  prompt set and the current OpenSpec `contextFiles`, the controller
  reuses the cached background summary instead of forcing each phase to reread
  every background artifact.
- If `tracked_change_files` is missing, stale, or obviously narrower than the
  current accepted change plus successful implement history, the controller
  rebuilds that archive-scope evidence before trusting a resumed archive retry.
- When `tracked_change_files` remains valid, a later archive retry can reuse
  that evidence instead of depending only on the latest narrow fix round.
- If the tracked prompt paths, `contextFiles` list, or tracked artifact
  fingerprints change, the controller marks `context_cache` stale, rebuilds it
  before the next phase dispatch, and persists the new signature in the same
  state file.
- `opsx-implementer` may return an optional cache-enrichment payload. The
  controller remains the only writer of the authoritative state file and merges
  that update into `context_cache` with implementer provenance.
- If the state file says `completed`, the controller trusts it only when the
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
- dsh runs headless via the installed `opsx-dsh-worker` shim; there is no dsh
  UI or plugin surface involved. Re-run
  `bash adapters/dsh/install.sh --global --verify` after any dsh adapter change
  so the installed shim, role files, and orchestrator runtime stay current.
- `opsx-plan` dispatches implement, review, and archive as direct stage
  invocations (`opsx-dsh-worker --role implementer`, etc.) — there is no
  nested-controller path.
