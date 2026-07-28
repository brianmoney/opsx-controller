## MODIFIED Requirements

### Requirement: `opsx-plan` compiles markdown implementation plans
The orchestrator SHALL provide an `opsx-plan compile <source.md> -o <output.toml>` command that converts a markdown implementation-plan document into a TOML manifest accepted by the existing `opsx-plan` plan loader.

The command SHALL accept `--adapter <adapter>` and SHALL default to `opencode` when the flag is omitted. The compile command SHALL refuse to overwrite an existing output path unless the operator passes `--force`.

#### Scenario: Operator compiles an OpenCode markdown plan
- **WHEN** an operator runs `opsx-plan compile openspec/plans/example-plan.md -o openspec/plans/example-plan.toml`
- **THEN** the command creates an OpenCode TOML manifest at the requested output path that can be loaded by `opsx-plan status` and `opsx-plan run`

#### Scenario: Existing output requires force
- **WHEN** the requested output file already exists and the operator does not pass `--force`
- **THEN** `opsx-plan compile` exits with a clear error and leaves the existing output file unchanged

### Requirement: Plan compilation invokes the selected adapter with its controller model
When the selected compile adapter is `opencode`, `opsx-plan compile` SHALL invoke OpenCode and resolve the `controller` role against the `opencode` adapter.

If the `controller` role cannot be resolved for `opencode`, the command SHALL fail before invoking OpenCode and explain that the OpenCode controller model must be configured.

#### Scenario: Controller model is passed to OpenCode
- **WHEN** the `controller` role resolves for the `opencode` adapter and an operator runs `opsx-plan compile` without `--adapter`
- **THEN** the spawned OpenCode command includes the resolved OpenCode controller model for the transformation request

#### Scenario: Missing OpenCode controller model fails closed
- **WHEN** the `controller` role cannot be resolved for the `opencode` adapter
- **THEN** `opsx-plan compile` exits with a configuration error before spawning OpenCode

### Requirement: Compile prompts include source and OpenCode reference context
For OpenCode compilation, the compile command SHALL provide a self-contained prompt that includes the source markdown plan, the expected TOML manifest shape, dependency and phase interpretation rules, current OpenCode adapter defaults, and representative markdown/TOML template plan references when available in the repository.

The prompt SHALL instruct the model to emit only the compiled TOML manifest and not to include prose outside the TOML payload.

#### Scenario: Prompt contains template plans and schema guidance
- **WHEN** `opsx-plan compile` builds an OpenCode prompt for a source markdown plan
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
