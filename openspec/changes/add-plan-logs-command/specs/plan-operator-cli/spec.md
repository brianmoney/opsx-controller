## ADDED Requirements

### Requirement: `opsx-plan logs` surfaces the latest relevant stage log for a resolved plan

The orchestrator SHALL provide an `opsx-plan logs` command for a resolved plan.

When invoked without a change or stage filter, the command SHALL select the most recent relevant stage log for that plan, print the selected log path, and print a tail of the log by default.

The orchestrator SHALL resolve the default target log from recorded plan state metadata first. If recorded state does not identify a usable log, the orchestrator SHALL fall back to deterministic `.opsx-plan/logs/` ordering.

#### Scenario: Default logs command uses recorded latest stage log metadata

- **GIVEN** the resolved plan state records `change-b` review round 2 as the latest usable stage log for that plan
- **WHEN** the operator runs `opsx-plan logs`
- **THEN** the command prints that log path
- **AND** the command prints a tail of that log

#### Scenario: Default logs command falls back to log-directory ordering

- **GIVEN** the resolved plan state does not identify a usable latest stage log
- **AND** `.opsx-plan/logs/` contains matching stage logs for the plan
- **WHEN** the operator runs `opsx-plan logs`
- **THEN** the command selects the most recent matching log by deterministic log-directory ordering
- **AND** the command prints the selected log path and a tail of that log

### Requirement: `opsx-plan logs` supports deterministic filtering and listing

The `opsx-plan logs` command SHALL support selecting logs by change id and stage for the resolved plan.

The command SHALL also support a listing mode that enumerates the available matching logs instead of tailing one selected log.

When change-id or stage filters are provided, the orchestrator SHALL apply those filters consistently whether the selected log comes from recorded state metadata or directory fallback.

#### Scenario: Operator selects a specific change and stage

- **GIVEN** the resolved plan has available logs for `change-a` implement and review and for `change-b` review
- **WHEN** the operator runs `opsx-plan logs --change change-a --stage review`
- **THEN** the command selects the latest matching `change-a` review log deterministically
- **AND** the command prints that log path and a tail of that log

#### Scenario: Operator lists available matching logs

- **GIVEN** the resolved plan has multiple available stage logs
- **WHEN** the operator runs `opsx-plan logs --list`
- **THEN** the command prints the available matching log paths for that plan instead of tailing one log

### Requirement: `opsx-plan logs` supports follow mode and clear missing-log handling

The `opsx-plan logs` command SHALL support a follow mode for an in-progress run using the same deterministic log selection rules as non-follow mode.

If no usable log matches the requested plan and filters, the command SHALL exit with a clear message rather than printing an empty tail.

#### Scenario: Follow mode tails the selected in-progress log

- **GIVEN** a resolved plan currently has an in-progress `change-c` implement log selected by the command's normal log-selection rules
- **WHEN** the operator runs `opsx-plan logs --follow`
- **THEN** the command follows that selected log instead of printing only a static tail

#### Scenario: Missing matching log reports a clear error

- **GIVEN** no usable log matches the resolved plan and requested filters
- **WHEN** the operator runs `opsx-plan logs --change missing-change --stage archive`
- **THEN** the command exits with a clear message that no matching log was found
- **AND** the command does not print an empty tail
