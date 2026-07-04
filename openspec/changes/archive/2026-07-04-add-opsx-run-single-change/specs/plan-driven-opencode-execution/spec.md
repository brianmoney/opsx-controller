## ADDED Requirements

### Requirement: Single-change runner executes without a plan manifest

The OpenCode adapter SHALL provide an `opsx-run <change-id>` command surface that starts or resumes the direct implement, review, and archive worker loop for exactly one existing accepted OpenSpec change without requiring a plan TOML manifest.

The runner SHALL synthesize the minimal one-change orchestration configuration needed by the existing direct OpenCode execution path and SHALL persist durable state under `.opsx-plan/`.

#### Scenario: Operator runs one existing change
- **WHEN** an operator invokes `opsx-run vault-gardening-suggestions` from a repository with an authored `openspec/changes/vault-gardening-suggestions/` change
- **THEN** the runner dispatches the OpenCode implementer, reviewer, and archiver workers through the direct OpenCode loop without reading a plan manifest

#### Scenario: Equivalent script subcommand is available
- **WHEN** an operator invokes the orchestrator script through `opsx-plan run-one vault-gardening-suggestions`
- **THEN** the orchestrator uses the same single-change execution behavior as `opsx-run vault-gardening-suggestions`

### Requirement: Single-change runner preserves direct loop gates

The single-change runner SHALL preserve the existing direct OpenCode review retry, no-progress, archive verification, and post-archive fast-check behavior used by OpenCode-backed plan execution.

The runner SHALL NOT mark the change done based only on worker process exit status or prose output.

#### Scenario: Review failure loops back to implementation
- **WHEN** the reviewer returns `verdict=fail` with any non-zero finding count and retry budget remains during an `opsx-run` execution
- **THEN** the runner records the fix prompt, increments the round, and dispatches the implementer again before another review attempt

#### Scenario: Clean review archives and verifies completion
- **WHEN** the reviewer returns `verdict=pass` with zero finding counts during an `opsx-run` execution
- **THEN** the runner dispatches the archiver and marks the change done only after archive worker output, repository archive evidence, archive commit evidence, and configured fast checks agree

### Requirement: Single-change runner fails closed for invalid inputs

The single-change runner SHALL accept exactly one change id and SHALL fail before worker dispatch if the change is missing, not authored, or if tracked files are dirty when tracked-clean enforcement is enabled.

The runner SHALL NOT create missing OpenSpec change artifacts.

#### Scenario: Missing change is rejected
- **WHEN** an operator invokes `opsx-run missing-change` and `openspec/changes/missing-change/` is absent or lacks required authored artifacts
- **THEN** the runner exits with a clear error and does not dispatch implementer, reviewer, or archiver workers

#### Scenario: Extra positional arguments are rejected
- **WHEN** an operator invokes `opsx-run first-change second-change`
- **THEN** the runner exits with a usage error and does not dispatch any worker
