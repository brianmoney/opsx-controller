# State Schema

Adapters should persist one per-change state file that survives interrupted
runs.

Recommended minimum fields:

- `version`
- `change`
- `schema`
- `status`
- `phase`
- `round`
- `max_rounds`
- `no_progress_streak`
- `latest_fix_prompt`
- `last_result`
- `task_counts`
- `tracked_change_files`
- `context_cache`
- `last_review`
- `archive`
- `history`

Resume requirements:

- reuse valid cached background context when signatures still match
- rebuild cached context when tracked prompts or artifacts change
- preserve a deduplicated change-owned file inventory for archive scope decisions
- trust a completed state only when archive metadata still matches the working
  tree
- resume blocked implement or archive runs without losing the latest fix prompt
