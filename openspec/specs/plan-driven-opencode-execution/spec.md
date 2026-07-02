## Requirements

### Requirement: `opsx-plan` directly dispatches OpenCode phase workers

For plan runs using the OpenCode adapter, `opsx-plan` SHALL execute each ready accepted change as a sequence of bounded implement, review, and archive worker invocations instead of launching `/opsx-drive` as a nested controller.

The orchestrator SHALL:
- invoke at most one phase worker per subprocess
- construct the worker input for the active change, round, and phase
- persist plan-owned phase state before and after each worker run
- write stage-specific logs under `.opsx-plan/logs/`

#### Scenario: Ready change enters direct implement-review-archive flow
- **WHEN** a ready accepted change is selected in an OpenCode-backed plan run
- **THEN** `opsx-plan` dispatches implement, then review, then archive as separate worker invocations without calling `/opsx-drive`

### Requirement: Review failures loop inside `opsx-plan`

When the OpenCode review worker returns any non-zero `critical`, `warning`, or `note` finding count, `opsx-plan` SHALL treat the review as blocking, persist the returned fix prompt, increment the round when retry budget remains, and schedule the next implement worker run itself.

The orchestrator SHALL stop with a blocked or failed status when the retry budget is exhausted or when the no-progress ceiling is reached.

#### Scenario: Review finding triggers another implement round
- **WHEN** the review worker returns `verdict=fail` with a non-zero finding count and the change is below the round ceiling
- **THEN** `opsx-plan` records the fix prompt, advances the change back to implement, and launches a new implement worker run on the next dispatch

#### Scenario: Max rounds stops the change
- **WHEN** the review worker returns `verdict=fail` for a change already at the configured round ceiling
- **THEN** `opsx-plan` marks the change blocked or failed with a reason indicating the review retry budget was exhausted and does not launch another worker

### Requirement: Plan state is authoritative for resumed OpenCode plan execution

For OpenCode-backed direct execution, `.opsx-plan/<plan-name>.state.json` SHALL be the authoritative durable state for the active change's phase, round, latest fix prompt, review result, archive result, and last stage log.

`opsx-plan` SHALL be able to resume or reconcile an interrupted run from its own state and repository evidence without requiring `.opencode/opsx-controller/<change>.json` to exist or remain current.

#### Scenario: Interrupted review resumes from plan state
- **WHEN** an OpenCode-backed plan run is interrupted after implement completed and the persisted plan state says the change is in review
- **THEN** rerunning `opsx-plan` resumes the change from review using the stored plan state instead of invoking `/opsx-drive` recovery

### Requirement: Completion remains evidence-driven

`opsx-plan` SHALL mark an OpenCode-backed change done only after all of the following are true:
- the archive worker returns a machine-readable archived result for the current run
- `openspec/changes/<change>` no longer exists in the worktree
- a dated archive directory exists for the change
- an `archive(<change>):` commit is reachable from `HEAD`
- configured post-archive fast checks pass

The orchestrator SHALL NOT treat worker exit code or prose output alone as successful completion.

#### Scenario: Archive worker success without repository evidence does not complete the change
- **WHEN** the archive worker claims success but the change directory still exists or no archive commit is reachable
- **THEN** `opsx-plan` does not mark the change done and records the archive as failed or unverifiable

### Requirement: `/opsx-drive` remains available for manual single-change control

This change SHALL remove `/opsx-drive` from the `opsx-plan` execution path, but SHALL keep the manual `/opsx-drive <change-id>` controller surface available for operators who want to drive one change outside a plan run.

#### Scenario: Manual `/opsx-drive` use remains supported
- **WHEN** an operator manually invokes `/opsx-drive <change-id>` after this change lands
- **THEN** the single-change controller path still exists even though `opsx-plan` no longer calls it during plan execution
