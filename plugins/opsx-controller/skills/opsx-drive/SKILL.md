---
description: Start or resume the OpenSpec controller for one change. Use when you want a strict implement-review-archive loop for an accepted OpenSpec change in Claude Code.
argument-hint: [change-id]
disable-model-invocation: true
allowed-tools: Read Edit MultiEdit Write Glob Grep Bash Agent(opsx-controller:opsx-implementer) Agent(opsx-controller:opsx-reviewer) Agent(opsx-controller:opsx-archiver)
---

Start or resume the OpenSpec controller for exactly one change.

Resolved controller inputs:

- Requested change id: `$0`
- Unexpected second positional argument: `$1`
- State file: `.claude/opsx-controller/$0.json`

Always begin by:

1. Reading `CLAUDE.md` if it exists.
2. Reading `AGENTS.md` if it exists.
3. Reading the workflow state contract from the first file that exists:
   - `.claude/opsx-controller/README.md`
   - `$HOME/.claude/opsx-controller/README.md`
4. Running `git status --short`.
5. Announcing `Using change: <name>` and reminding the operator they can run
   `/opsx-controller:opsx-drive <other-change>` to override.

Input rules:

- If `$0` is empty, stop and report that `/opsx-controller:opsx-drive <change-id>` is required.
- If `$1` is non-empty, stop and report that only one change is supported per
  run.

Context cache rules:

- The controller owns one unified `context_cache` object inside the state file.
- Seed `context_cache` from repository guidance plus the OpenSpec change context
  you already must inspect before the first phase dispatch.
- Track cache validity with a compact `source_signature` derived from:
  - the repository guidance files you used
  - the `contextFiles` list returned by `openspec instructions apply`
  - the current contents or fingerprints of tracked context artifacts that exist
    on disk
- If the signature still matches on resume, keep `context_cache.valid=true` and
  reuse the cached summary for later phase dispatch.
- If any tracked source changes, set `context_cache.valid=false`, mark
  `context_cache.status=stale`, rebuild the cache before dispatch, and persist
  the fresh signature plus a short `refresh_reason`.

Archive scope tracking rules:

- The controller owns one deduplicated `tracked_change_files` list inside the
  state file for implementation, docs, tests, and change-artifact files that
  clearly belong to the active change.
- Seed `tracked_change_files` before the first phase dispatch from accepted
  change artifacts and current dirty worktree files that clearly match the
  active change.
- Keep `openspec/changes/<change>/` files in that list whenever they exist.
- After each successful implementation round, merge in the implementer's
  `files_touched` plus any broader `known_change_files` it returns.
- Before archive or archive retry, refresh `tracked_change_files` against the
  accepted change artifacts, successful implement history, and the current dirty
  worktree.

State file rules:

- State path: `.claude/opsx-controller/<change-id>.json`
- Ensure `.claude/opsx-controller/` exists before writing state.
- Persist state after initialization, after every phase result, and before any
  blocked or completed exit.

Required state shape:

```json
{
  "version": 3,
  "change": "<change-id>",
  "schema": "spec-driven",
  "status": "running|blocked|completed",
  "phase": "implement|review|archive|done",
  "round": 1,
  "max_rounds": 5,
  "no_progress_streak": 0,
  "latest_fix_prompt": "",
  "last_result": "",
  "task_counts": {"complete": 0, "total": 0},
  "tracked_change_files": [],
  "context_cache": {
    "valid": false,
    "status": "missing|ready|stale",
    "compiled_by": "controller|implementer",
    "updated_in_round": 0,
    "source_signature": "",
    "source_paths": [],
    "refresh_reason": "",
    "change_summary": ""
  },
  "last_review": {
    "verdict": "pending|pass|fail",
    "finding_counts": {"critical": 0, "warning": 0, "note": 0},
    "summary": "",
    "fix_prompt": ""
  },
  "archive": {
    "status": "not_started|passed|failed",
    "path": "",
    "commit": "",
    "reason": "",
    "spec_sync_status": "",
    "triage": {
      "scope_basis": "",
      "in_scope_files": [],
      "ambiguous_files": [],
      "retry_guidance": "",
      "retry_outlook": "unknown|same_failure|may_succeed"
    }
  },
  "history": []
}
```

OpenSpec context rules:

- Run `openspec status --change "<change>" --json`.
- Run `openspec instructions apply --change "<change>" --json`.
- Read every file from `contextFiles` before the first phase dispatch so the
  controller can seed `context_cache`.
- On resumed rounds with a valid cache, reread only what is still needed for
  correctness.

Dispatch contract:

- Use the Agent tool only with `opsx-controller:opsx-implementer`,
  `opsx-controller:opsx-reviewer`, and `opsx-controller:opsx-archiver`.
- Provide each phase agent a compact input block with these fields:
  - `CHANGE: <change-id>`
  - `ROUND: <round>`
  - `STATE_FILE: <path>`
  - `LATEST_FIX_PROMPT: <prompt or none>`
  - `TASK_COUNTS: <complete>/<total>`
  - `CONTEXT_CACHE_STATUS: <ready|stale|missing>`
  - `CONTEXT_CACHE_VALID: <true|false>`
  - `CONTEXT_CACHE_SUMMARY: <bounded summary or none>`
- Expect each phase agent to return exactly one line of JSON.
- Parse that JSON and update state directly from the payload.

Implementation phase rules:

- Dispatch `opsx-controller:opsx-implementer` when `phase=implement`.
- If the implementer reports `status=blocked`, persist blocked state and stop.
- If the implementer reports `progress_made=false`, increment
  `no_progress_streak`; otherwise reset it to zero.
- Merge `files_touched` and `known_change_files` from a successful implementer
  result into `tracked_change_files` before persisting state.
- After a successful implementer run, set `phase=review` and continue.

Review phase rules:

- Dispatch `opsx-controller:opsx-reviewer` when `phase=review`.
- The review gate is strict: any `critical`, `warning`, or `note` finding is
  blocking.
- Only treat review as passed when all three counts are zero and the reviewer
  returns `verdict=pass`.
- On review failure, persist `latest_fix_prompt`, persist `last_review`, and if
  `round < max_rounds`, increment `round`, set `phase=implement`, and continue.
- If the current round already equals `max_rounds`, stop with
  `status=blocked` and `last_result=max_rounds_reached`.

Archive phase rules:

- Dispatch `opsx-controller:opsx-archiver` only after a clean review.
- If archive succeeds, persist completed state with `phase=done`.
- If archive fails, persist blocked archive state including the returned triage.

Stop rules:

- Stop with blocked state after 2 consecutive no-progress implementation rounds.
- Stop if any phase agent returns malformed output instead of the required JSON.
- Never report success without a fresh archive success from the archive agent.

Final response rules:

- Return a concise human-readable summary of the current controller outcome.
- Include the active change id, current phase or archive result, and any blocked
  reason when relevant.
