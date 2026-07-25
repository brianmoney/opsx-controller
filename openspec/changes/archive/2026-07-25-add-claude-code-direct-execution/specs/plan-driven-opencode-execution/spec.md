## MODIFIED Requirements

### Requirement: `opsx-plan` directly dispatches OpenCode phase workers

For plan runs where all three of `implement_invoke`, `review_invoke`, and `archive_invoke` are configured, `opsx-plan` SHALL execute each ready accepted change as a sequence of bounded implement, review, and archive worker invocations instead of launching `/opsx-drive` as a nested controller. Direct dispatch SHALL be determined by this configuration alone and SHALL NOT be conditioned on adapter identity. The OpenCode adapter defaults supply all three invokes, so OpenCode plan runs take this path without manifest changes.

The orchestrator SHALL:
- invoke at most one phase worker per subprocess
- construct the worker input for the active change, round, and phase
- persist plan-owned phase state before and after each worker run
- write stage-specific logs under `.opsx-plan/logs/`

#### Scenario: Ready change enters direct implement-review-archive flow
- **WHEN** a ready accepted change is selected in an OpenCode-backed plan run
- **THEN** `opsx-plan` dispatches implement, then review, then archive as separate worker invocations without calling `/opsx-drive`

#### Scenario: Plan without a full set of stage invokes uses the nested controller
- **WHEN** a ready accepted change is selected in a plan that configures fewer than all three stage invokes
- **THEN** `opsx-plan` launches the configured `invoke` command as a nested controller instead of dispatching phase workers
