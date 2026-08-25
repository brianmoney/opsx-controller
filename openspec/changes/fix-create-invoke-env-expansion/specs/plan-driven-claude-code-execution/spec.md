# plan-driven-claude-code-execution Specification

## MODIFIED Requirements

### Requirement: Stage invoke strings expand environment variables

Before executing a direct stage command or the templated create-stage
command, `opsx-plan` SHALL expand `$VAR` and `${VAR}` references in each
argument of the resolved invoke string.

The `OPSX_*_MODEL` variables referenced by adapter default invokes SHALL be
populated by per-adapter model resolution performed when the plan
configuration is constructed, so that expansion yields the model configured
for that plan's adapter.

For the create-stage command, `opsx-plan` SHALL apply `{change}`,
`{plan_doc}`, and `{controller_model}` substitutions before environment
variable expansion, preserving the existing placeholder behavior while
allowing substituted values and invoke text to contain environment
references.

When a referenced variable is unset, `opsx-plan` SHALL fail the stage with a
message naming the unset variable and SHALL NOT invoke the client. A literal
unexpanded variable reference SHALL never be passed to the client.

When a referenced variable expands to an empty value, `opsx-plan` SHALL drop
that empty argument and any preceding standalone flag that would otherwise
dangle, matching the established direct-stage behavior.

#### Scenario: Per-stage model is selected from the environment

- **WHEN** `OPSX_IMPLEMENTER_MODEL` is set and the implement invoke references it
- **THEN** the dispatched command receives the expanded model value and the stage log line shows the expanded command

#### Scenario: Resolved per-adapter model populates the referenced variable

- **WHEN** a `claude-code` plan is loaded and the configuration resolves the `implementer` role for that adapter
- **THEN** `OPSX_IMPLEMENTER_MODEL` holds the resolved value at dispatch time and the implement invoke expands to it

#### Scenario: Unset variable fails the stage

- **WHEN** a stage invoke references a variable that is unset and cannot be resolved
- **THEN** `opsx-plan` fails the stage with a message naming the variable and does not invoke the client

#### Scenario: Create invoke expands the controller model

- **WHEN** `create_invoke` references `$OPSX_CONTROLLER_MODEL` and that variable is set to a resolved controller model
- **THEN** the dispatched create command receives the resolved model value and its exec/log command shows the expanded value rather than the literal variable reference

#### Scenario: Braced create variable expands

- **WHEN** `create_invoke` references `${OPSX_CONTROLLER_MODEL}` and that variable is set to a resolved controller model
- **THEN** the dispatched create command receives the resolved model value and does not pass the literal braced reference

#### Scenario: Unset create variable fails closed

- **WHEN** `create_invoke` references a genuinely unset environment variable
- **THEN** the create stage returns the existing `env_error` outcome with a message naming the variable, does not invoke the client, and the run treats the deterministic configuration error as terminal rather than retrying change verification

#### Scenario: Create placeholders remain supported

- **WHEN** `create_invoke` uses `{change}`, `{plan_doc}`, and `{controller_model}` placeholders
- **THEN** those placeholders are substituted before token expansion and the spawned create command receives their existing values
