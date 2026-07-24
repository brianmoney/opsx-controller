## ADDED Requirements

### Requirement: `doctor` verifies worker agents for direct-dispatch plans

When the resolved plan uses direct dispatch, `doctor` SHALL check that the configured adapter's implement, review, and archive worker agents are installed in that adapter's agent directory.

The check SHALL report each missing worker agent by name, SHALL name the installer that provides it, and SHALL exit non-zero.

When no plan is resolved, or when the resolved plan does not use direct dispatch, the check SHALL be skipped without failing the command.

#### Scenario: Doctor detects a missing worker agent

- **WHEN** the resolved plan uses direct dispatch and one of the adapter's worker agents is not installed
- **THEN** `opsx-plan doctor` reports the missing agent by name, points at the adapter installer, and exits non-zero

#### Scenario: Doctor passes when all worker agents are installed

- **WHEN** the resolved plan uses direct dispatch and all three worker agents are present in the adapter's agent directory
- **THEN** the worker-agent check passes and does not affect the command outcome

#### Scenario: Doctor skips the check for a nested-controller plan

- **WHEN** the resolved plan does not configure a full set of stage invokes
- **THEN** `opsx-plan doctor` skips the worker-agent check and does not fail because of it
