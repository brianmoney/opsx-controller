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
- when `LATEST_FIX_PROMPT` is non-empty, treats every finding, corrective
  guideline, and verification requirement in that handoff as the
  highest-priority retry scope
- if the handoff conflicts with live artifacts or repository evidence, returns
  a blocked result instead of inventing an alternative correction
- returns machine-readable status, task counts, touched files, broader known
  change files, and optional cache enrichment
- classifies a task line whose text ends with `(manual)` as an operator-only
  manual task; `status=implemented` requires every non-`(manual)` task to be
  checked, and an automatable task that cannot be completed is reported as
  `blocked` rather than left unchecked under `implemented`

Review phase:

- validates the active change against current tasks, specs, and repo guidance
- treats any critical, warning, or note finding as a failure
- for a failed review, returns a self-contained corrective `fix_prompt` with
  labeled `CHANGE`, `FINDINGS`, `CORRECTIVE GUIDANCE`, and `VERIFY` sections
  so the next implementer can act without rediscovering the reviewer's findings
- `CHANGE` identifies the active change; `FINDINGS` lists every blocking
  finding with severity, relevant file or symbol, observed behavior, and
  required behavior; `CORRECTIVE GUIDANCE` prescribes the implementation
  approach; `VERIFY` names the focused regressions and validation commands
- returns an empty `fix_prompt` only for a zero-finding passing verdict
- when `TASK_COUNTS.complete < total`, reads the tasks file and fails the
  review with a blocking finding per unchecked non-`(manual)` task; unchecked
  `(manual)` tasks never produce findings on their own

Archive phase:

- validates archive readiness non-interactively
- determines explicit archive scope before mutating files
- syncs delta specs when unambiguous
- archives the change and creates the archive commit only when the staged set is
  clean
- fails closed on unchecked `- [ ]` tasks except those marked `(manual)`;
  pending `(manual)` tasks are surfaced to the operator as a post-archive
  checklist
- returns either a success payload or blocked triage

Machine-readable outputs should be JSON when the host client supports it.

## Round Budget

Rounds are the shared retry budget for a change (`max_rounds`, default 5).
Both review failures and implement-side completeness retries consume the
budget: when implement returns `implemented` with unchecked automatable tasks,
the controller re-enters implement with a corrective prompt naming those
tasks and increments `round`; exhausting `max_rounds` fails the change naming
the remaining task ids. Telemetry and report views treat these the same as
any other consumed round.
