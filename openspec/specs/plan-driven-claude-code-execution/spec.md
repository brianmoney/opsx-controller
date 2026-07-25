## Purpose

Define how `opsx-plan` directly dispatches Claude Code phase workers when the `claude-code` adapter is configured with all three stage invokes.

## Requirements

### Requirement: `opsx-plan` directly dispatches Claude Code phase workers

For plan runs using the `claude-code` adapter with direct dispatch configured, `opsx-plan` SHALL execute each ready accepted change as a sequence of bounded implement, review, and archive worker invocations instead of launching `/opsx-drive` as a nested controller.

The orchestrator SHALL apply the same plan-owned round control, retry budgets, no-progress ceiling, stage logging, and evidence-driven completion verification that it applies to any other direct-dispatch adapter.

#### Scenario: Ready change enters direct implement-review-archive flow under Claude Code

- **WHEN** a ready accepted change is selected in a plan whose adapter is `claude-code` and whose three stage invokes are configured
- **THEN** `opsx-plan` dispatches implement, then review, then archive as separate worker invocations without calling `/opsx-drive`

#### Scenario: Review finding loops inside the plan under Claude Code

- **WHEN** the Claude Code review worker returns a non-zero finding count and the change is below the round ceiling
- **THEN** `opsx-plan` persists the fix prompt, increments the round, and dispatches another implement worker itself

### Requirement: The `claude-code` adapter supplies direct stage invoke defaults

`ADAPTER_DEFAULTS` for `claude-code` SHALL provide `implement_invoke`, `review_invoke`, and `archive_invoke` in addition to `invoke` and `state_file`.

Each default SHALL invoke the Claude Code CLI in print mode against the corresponding installed worker agent (`opsx-implementer`, `opsx-reviewer`, `opsx-archiver`), select the stage model from the corresponding `OPSX_*_MODEL` environment variable, and request a machine-readable result envelope.

Each default SHALL be overridable in the plan `[plan]` table.

#### Scenario: Claude Code plan runs direct without manifest invoke overrides

- **WHEN** a plan sets `adapter = "claude-code"` and configures no stage invokes
- **THEN** the plan resolves all three stage invokes from adapter defaults and takes the direct dispatch path

#### Scenario: Operator overrides a single stage invoke

- **WHEN** a plan sets `adapter = "claude-code"` and overrides only `review_invoke`
- **THEN** the overridden command is used for review and adapter defaults are used for implement and archive

### Requirement: The worker input block is passed as the client prompt

For direct stage dispatch, `opsx-plan` SHALL append the constructed worker input block as the final positional argument of the resolved stage command.

#### Scenario: Input block reaches the Claude Code worker

- **WHEN** `opsx-plan` dispatches an implement stage under the `claude-code` adapter
- **THEN** the worker input block is passed as the trailing positional prompt argument and the stage log records the command with the input elided

### Requirement: Stage invoke strings expand environment variables

Before executing a direct stage command, `opsx-plan` SHALL expand environment variable references in each argument of the resolved invoke string.

When an argument expands to an empty value because a referenced variable is unset, `opsx-plan` SHALL fail the stage with a message naming the unset variable and SHALL NOT invoke the client.

#### Scenario: Per-stage model is selected from the environment

- **WHEN** `OPSX_IMPLEMENTER_MODEL` is set and the implement invoke references it
- **THEN** the dispatched command receives the expanded model value and the stage log line shows the expanded command

#### Scenario: Unset model variable fails the stage closed

- **WHEN** a resolved stage invoke references an unset environment variable
- **THEN** the stage fails with a message naming the unset variable and no client subprocess is started

### Requirement: Claude Code worker output is unwrapped from the result envelope

When a direct stage produces a Claude Code result envelope, `opsx-plan` SHALL select the last envelope object in the stage log, extract the final result text, and locate the worker's single-line JSON object within that text.

Envelope unwrapping SHALL apply the same recognition rules used for unwrapped stage output, including single-line JSON object detection and the existing permission-rejection and provider-failure transcript markers.

#### Scenario: Worker JSON is recovered from an envelope

- **WHEN** a Claude Code stage completes and stdout is a result envelope whose result text ends with one line of worker JSON
- **THEN** `opsx-plan` parses the worker JSON and drives the control loop from it

#### Scenario: Envelope with no worker JSON is reported as invalid output

- **WHEN** a Claude Code stage completes and the envelope result text contains no parseable single-line JSON object
- **THEN** the stage outcome is `invalid_output` and the reported reason describes the missing JSON object

#### Scenario: Permission rejection inside an envelope is reported actionably

- **WHEN** a Claude Code stage produces an envelope whose result text carries a permission-rejection marker and no worker JSON
- **THEN** the failure reason identifies the permission rejection rather than reporting a generic parse failure

### Requirement: Claude Code direct workers run without interactive permission prompts

Default `claude-code` stage invokes SHALL select a permission posture that completes unattended, and worker tool scope SHALL be bounded by the installed agent definitions rather than by interactive approval.

#### Scenario: Unattended stage completes without a permission prompt

- **WHEN** an implement worker edits files and runs commands during an unattended direct stage
- **THEN** the stage completes without requiring interactive approval and without a permission-rejection transcript
