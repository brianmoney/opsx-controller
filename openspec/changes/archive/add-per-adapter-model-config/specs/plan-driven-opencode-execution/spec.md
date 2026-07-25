## ADDED Requirements

### Requirement: OpenCode direct stage invokes select the stage model explicitly

The `opencode` adapter defaults for `implement_invoke`, `review_invoke`, and `archive_invoke` SHALL pass the stage model to the OpenCode CLI as an explicit `--model` argument referencing the corresponding `OPSX_*_MODEL` environment variable, in the same manner as the `claude-code` adapter defaults.

Because the orchestrator activates resolved models for the process before dispatch, the expanded argument SHALL carry the model resolved for the `opencode` adapter.

A model change SHALL take effect on the next direct-dispatch run without re-running the adapter installer.

Each default SHALL remain overridable in the plan `[plan]` table.

#### Scenario: Stage command carries the resolved model

- **WHEN** `opsx-plan` dispatches an implement stage under the `opencode` adapter using adapter defaults
- **THEN** the dispatched command includes `--model` set to the model resolved for the `opencode` adapter, and the stage log line shows the expanded command

#### Scenario: Model change applies without reinstalling

- **WHEN** an operator changes the `opencode` implementer model in the configuration file and starts a new direct-dispatch run without re-running the adapter installer
- **THEN** the implement stage is dispatched with the new model

#### Scenario: Explicit model argument drives telemetry attribution

- **WHEN** a direct OpenCode stage completes and telemetry recovers the model identity from the invocation
- **THEN** attribution uses the explicit `--model` argument rather than reading the installed agent frontmatter

### Requirement: `/opsx-drive` is deprecated in favor of direct dispatch

The `/opsx-drive` nested-controller surface SHALL be deprecated.

`opsx-plan` SHALL NOT use `/opsx-drive` in its own execution path for plans that configure a full set of stage invokes.

The `/opsx-drive` surface SHALL remain functional for operators who still depend on it, and SHALL NOT be removed by this change.

Documentation for `/opsx-drive` across every adapter SHALL mark the surface as deprecated and SHALL direct operators to `opsx-run <change-id>` as the supported way to drive exactly one change outside a plan run.

When a resolved plan takes the nested-controller path because it configures fewer than all three stage invokes, `opsx-plan` SHALL emit a deprecation warning naming the nested-controller path and pointing at direct dispatch, and SHALL still execute the run.

#### Scenario: Nested-controller plan warns and still runs

- **WHEN** a plan that configures fewer than all three stage invokes is run
- **THEN** `opsx-plan` emits a deprecation warning about the nested-controller path and still executes the plan through that path

#### Scenario: Direct-dispatch plan emits no deprecation warning

- **WHEN** a plan configures all three stage invokes and takes the direct-dispatch path
- **THEN** no `/opsx-drive` deprecation warning is emitted

#### Scenario: Manual `/opsx-drive` use still works

- **WHEN** an operator manually invokes `/opsx-drive <change-id>` after this change lands
- **THEN** the single-change controller path still functions, and its documentation identifies it as deprecated and names `opsx-run <change-id>` as the supported replacement

## MODIFIED Requirements

### Requirement: Plan compilation invokes OpenCode with the controller model

`opsx-plan compile` SHALL invoke OpenCode to perform the markdown-to-TOML transformation and SHALL select the model by resolving the `controller` role against the `opencode` adapter.

Because the compile command shells out to the OpenCode CLI regardless of the active plan's adapter, it SHALL resolve against the `opencode` adapter specifically and SHALL NOT use the active plan's adapter for this resolution.

If the `controller` role cannot be resolved for the `opencode` adapter, the command SHALL fail before invoking OpenCode and explain that the controller model must be configured.

#### Scenario: Controller model is passed to OpenCode

- **WHEN** the `controller` role resolves for the `opencode` adapter and an operator runs `opsx-plan compile`
- **THEN** the spawned OpenCode command includes the resolved controller model for the transformation request

#### Scenario: Compile ignores a non-OpenCode active plan adapter

- **WHEN** the active plan's adapter is `claude-code` and an operator runs `opsx-plan compile`
- **THEN** the spawned OpenCode command uses the controller model resolved for the `opencode` adapter, not the one resolved for `claude-code`

#### Scenario: Missing controller model fails closed

- **WHEN** the `controller` role cannot be resolved for the `opencode` adapter
- **THEN** `opsx-plan compile` exits with a configuration error before spawning OpenCode

## REMOVED Requirements

### Requirement: `/opsx-drive` remains available for manual single-change control

**Reason**: The nested-controller surface is superseded by direct dispatch, which has been the default path for both the `opencode` and `claude-code` adapters since their `ADAPTER_DEFAULTS` began supplying all three stage invokes. Keeping `/opsx-drive` as a first-class supported surface is also the only remaining reason the OpenCode adapter must bake models into installed agent frontmatter, because nested subagents are spawned by OpenCode itself and cannot receive an orchestrator-supplied `--model`. This requirement is replaced by "`/opsx-drive` is deprecated in favor of direct dispatch", which keeps the surface functional but marks it deprecated.

**Migration**: Use `opsx-run <change-id>` (equivalently `opsx-plan run-one <change-id>`) to drive exactly one change outside a plan run. It executes the same direct implement, review, and archive loop with the same retry, no-progress, archive-verification, and fast-check gates. No manifest is required. `/opsx-drive` continues to work during the deprecation period; a later change will remove it.
