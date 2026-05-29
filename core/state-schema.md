# State Schema

Adapters should persist one per-change state file that survives interrupted
runs.

Recommended minimum fields:

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

Resume requirements:

- reuse valid cached background context when signatures still match
- rebuild cached context when tracked prompts or artifacts change
- preserve a deduplicated change-owned file inventory for archive scope decisions
- trust a completed state only when archive metadata still matches the working
  tree
- resume blocked implement or archive runs without losing the latest fix prompt
