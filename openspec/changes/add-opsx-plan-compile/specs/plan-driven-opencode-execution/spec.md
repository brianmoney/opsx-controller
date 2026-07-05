## ADDED Requirements

### Requirement: `opsx-plan` compiles markdown implementation plans

The orchestrator SHALL provide an `opsx-plan compile <source.md> -o <output.toml>` command that converts a markdown implementation-plan document into a TOML manifest accepted by the existing `opsx-plan` plan loader.

The compile command SHALL refuse to overwrite an existing output path unless the operator passes `--force`.

#### Scenario: Operator compiles a markdown plan
- **WHEN** an operator runs `opsx-plan compile openspec/plans/example-plan.md -o openspec/plans/example-plan.toml`
- **THEN** the command creates a TOML manifest at the requested output path that can be loaded by `opsx-plan status` and `opsx-plan run`

#### Scenario: Existing output requires force
- **WHEN** the requested output file already exists and the operator does not pass `--force`
- **THEN** `opsx-plan compile` exits with a clear error and leaves the existing output file unchanged

### Requirement: Plan compilation invokes OpenCode with the controller model

`opsx-plan compile` SHALL invoke OpenCode to perform the markdown-to-TOML transformation and SHALL select the model from the `OPSX_CONTROLLER_MODEL` environment variable.

If `OPSX_CONTROLLER_MODEL` is unset or empty, the command SHALL fail before invoking OpenCode and explain that the controller model must be configured.

#### Scenario: Controller model is passed to OpenCode
- **WHEN** `OPSX_CONTROLLER_MODEL` is set and an operator runs `opsx-plan compile`
- **THEN** the spawned OpenCode command includes the configured controller model for the transformation request

#### Scenario: Missing controller model fails closed
- **WHEN** `OPSX_CONTROLLER_MODEL` is unset or empty
- **THEN** `opsx-plan compile` exits with a configuration error before spawning OpenCode

### Requirement: Compile prompts include source and reference context

The compile command SHALL provide the OpenCode invocation with a self-contained prompt that includes the source markdown plan, the expected TOML manifest shape, dependency and phase interpretation rules, current adapter defaults, and representative markdown/TOML template plan references when available in the repository.

The prompt SHALL instruct the model to emit only the compiled TOML manifest and not to include prose outside the TOML payload.

#### Scenario: Prompt contains template plans and schema guidance
- **WHEN** `opsx-plan compile` builds the OpenCode prompt for a source markdown plan
- **THEN** the prompt includes the source plan content, manifest field guidance for `[plan]` and `[[changes]]`, dependency-resolution guidance, and at least one available repository template plan pair or an explicit note that no template pair was found

#### Scenario: Prompt forbids prose output
- **WHEN** the prompt is sent to OpenCode
- **THEN** it instructs the model to return TOML only so the result can be validated and written without manual cleanup

### Requirement: Compiled manifests are validated before write success

`opsx-plan compile` SHALL parse the generated TOML and validate it with the same plan-loading path used by `opsx-plan status` and `opsx-plan run` before reporting success.

If validation fails, the command SHALL exit with a clear error and SHALL NOT replace an existing output file.

#### Scenario: Valid generated TOML is written atomically
- **WHEN** OpenCode returns TOML that parses successfully and passes the existing plan loader validation
- **THEN** `opsx-plan compile` writes the output manifest atomically and reports the output path

#### Scenario: Invalid generated TOML is rejected
- **WHEN** OpenCode returns malformed TOML or a manifest with invalid dependency references
- **THEN** `opsx-plan compile` exits with a validation error and does not report the manifest as compiled
