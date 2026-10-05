# Phase Protocol

Adapters should preserve a compact handoff between controller and phase runners.

Recommended phase input fields:

- `CHANGE`
- `ROUND`
- `STATE_FILE`
- `LATEST_FIX_PROMPT`
- `TASK_COUNTS`
- `CONTEXT_CACHE_STATUS`
- `CONTEXT_CACHE_VALID`
- `CONTEXT_CACHE_SUMMARY`

Implement phase:

- executes the next required change work
- keeps edits minimal and in scope
- updates the change task list as work completes
- returns machine-readable status, task counts, touched files, broader known
  change files, and optional cache enrichment

Review phase:

- validates the active change against current tasks, specs, and repo guidance
- treats any critical, warning, or note finding as a failure
- returns a compact verdict, finding counts, summary, and fix prompt

Archive phase:

- validates archive readiness non-interactively
- determines explicit archive scope before mutating files
- syncs delta specs when unambiguous
- archives the change and creates the archive commit only when the staged set is
  clean
- returns either a success payload or blocked triage

## Completion and deferral handoff

Apply the evidence and impact rules in
`controller-contract.md#evidence-based-completion-and-accepted-deferrals` in both
phases. Keep requirement, execution-path, observation-source, and verification
references in existing change artifacts. Summarize what was implemented,
verified, and explicitly deferred in the existing result `summary`.

An accepted deferral is reflected in the agreed scope/specs and retained as an
identified plain follow-up outside the active task checkboxes. It does not
produce another implementation round or a review finding solely because it is
unimplemented. A worker's deferral label alone is not acceptance; active
unchecked automatable tasks and unsupported required guarantees still block.
No new JSON fields, phase statuses, or controller-state edits are needed.
