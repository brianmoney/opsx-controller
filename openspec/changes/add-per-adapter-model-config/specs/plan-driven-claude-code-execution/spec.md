## MODIFIED Requirements

### Requirement: The `claude-code` adapter supplies direct stage invoke defaults

`ADAPTER_DEFAULTS` for `claude-code` SHALL provide `implement_invoke`, `review_invoke`, and `archive_invoke` in addition to `invoke` and `state_file`.

Each default SHALL invoke the Claude Code CLI in print mode against the corresponding installed worker agent (`opsx-implementer`, `opsx-reviewer`, `opsx-archiver`), select the stage model from the corresponding `OPSX_*_MODEL` environment variable, and request a machine-readable result envelope.

The value of each `OPSX_*_MODEL` variable SHALL be the value resolved for the `claude-code` adapter and activated by the orchestrator before dispatch, rather than a value shared across adapters. A model configured for a different adapter SHALL NOT reach a Claude Code stage invocation.

Each default SHALL be overridable in the plan `[plan]` table.

#### Scenario: Claude Code plan runs direct without manifest invoke overrides

- **WHEN** a plan sets `adapter = "claude-code"` and configures no stage invokes
- **THEN** the plan resolves all three stage invokes from adapter defaults and takes the direct dispatch path

#### Scenario: Stage model is the Claude Code value, not a shared one

- **WHEN** the configuration sets different `implementer` models for the `opencode` and `claude-code` adapters and a `claude-code` plan dispatches an implement stage
- **THEN** the dispatched command carries the `claude-code` value

#### Scenario: Operator overrides a single stage invoke

- **WHEN** a plan sets `adapter = "claude-code"` and overrides only `review_invoke`
- **THEN** the overridden command is used for review and adapter defaults are used for implement and archive

### Requirement: Stage invoke strings expand environment variables

Before executing a direct stage command, `opsx-plan` SHALL expand environment variable references in each argument of the resolved invoke string.

The `OPSX_*_MODEL` variables referenced by adapter default invokes SHALL be populated by per-adapter model resolution performed when the plan configuration is constructed, so that expansion yields the model configured for that plan's adapter.

When an argument expands to an empty value because a referenced variable is unset, `opsx-plan` SHALL fail the stage with a message naming the unset variable and SHALL NOT invoke the client.

#### Scenario: Per-stage model is selected from the environment

- **WHEN** `OPSX_IMPLEMENTER_MODEL` is set and the implement invoke references it
- **THEN** the dispatched command receives the expanded model value and the stage log line shows the expanded command

#### Scenario: Resolved per-adapter model populates the referenced variable

- **WHEN** a `claude-code` plan is loaded and the configuration resolves the `implementer` role for that adapter
- **THEN** `OPSX_IMPLEMENTER_MODEL` holds the resolved value at dispatch time and the implement invoke expands to it

#### Scenario: Unset variable fails the stage

- **WHEN** a stage invoke references a variable that is unset and cannot be resolved
- **THEN** `opsx-plan` fails the stage with a message naming the variable and does not invoke the client
