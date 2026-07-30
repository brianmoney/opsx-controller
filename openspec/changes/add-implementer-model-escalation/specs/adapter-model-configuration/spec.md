## MODIFIED Requirements

### Requirement: Model selection is stored per adapter and per role

The system SHALL store model selection in a TOML configuration file keyed by adapter and by role, where the required roles are `controller`, `implementer`, `reviewer`, and `archiver`, and `implementer_escalation` is an additional optional role.

A required role is one every run needs; an optional role is one that only some configurations use, whose absence is not an error on its own.

The file SHALL support an `[adapters.<adapter>]` table per adapter carrying zero or more role keys, and a `[defaults]` table carrying role keys that apply to every adapter that does not override them. Optional roles SHALL be settable in the same tables and by the same key names as required roles.

The primary configuration location SHALL be `~/.config/opsx-controller/models.toml`. A repository-local `<repo>/.opsx-plan/models.toml` SHALL be honored as a machine-local override when present.

#### Scenario: Distinct adapters resolve distinct identifiers for the same role

- **WHEN** a configuration file sets `[adapters.opencode].implementer` to a provider-prefixed identifier and `[adapters.claude-code].implementer` to a bare Anthropic alias
- **THEN** resolving the `implementer` role for `opencode` returns the provider-prefixed identifier and resolving it for `claude-code` returns the bare alias, with no operator action between the two

#### Scenario: Adapter table overrides the defaults table

- **WHEN** `[defaults].reviewer` and `[adapters.claude-code].reviewer` are both set and the `reviewer` role is resolved for `claude-code`
- **THEN** resolution returns the `[adapters.claude-code]` value

#### Scenario: Defaults table covers an adapter with no override

- **WHEN** `[defaults].archiver` is set, `[adapters.opencode]` declares no `archiver` key, and the `archiver` role is resolved for `opencode`
- **THEN** resolution returns the `[defaults]` value

#### Scenario: Optional role is configured like any other role

- **WHEN** a configuration file sets `[adapters.opencode].implementer_escalation` and the `implementer_escalation` role is resolved for `opencode`
- **THEN** resolution returns that value and reports the configuration file as its source

#### Scenario: Unset optional role is not an error

- **WHEN** no configuration file or environment variable supplies `implementer_escalation` and models are resolved for an adapter
- **THEN** resolution reports that role as unresolved and reports the four required roles normally, without raising

### Requirement: Resolved models are activated for the whole orchestrator process

When the orchestrator constructs a plan configuration, it SHALL resolve every role against that configuration's adapter and SHALL export the resolved values as the corresponding `OPSX_<ROLE>_MODEL` environment variables for the remainder of the process.

The four required roles SHALL always be exported. An optional role SHALL be exported when it resolves and SHALL be left unset when it does not, so that an unset optional role remains distinguishable from one configured to an empty value.

Activation SHALL occur for every path that constructs a plan configuration, including plan-manifest loading and single-change execution.

The exported values SHALL remain in effect for the whole process rather than being scoped to individual subprocess invocations, so that consumers running after a stage completes observe the same values the stage was dispatched with. A value that the orchestrator deliberately re-sets between stage dispatches SHALL be exempt from this, provided it is re-set deterministically before each dispatch that reads it.

#### Scenario: Stage dispatch receives the adapter-specific model

- **WHEN** a plan whose adapter is `claude-code` is loaded and a stage invoke references `$OPSX_IMPLEMENTER_MODEL`
- **THEN** the dispatched command receives the value resolved for the `claude-code` adapter

#### Scenario: Post-stage consumers observe the same values

- **WHEN** a stage completes and telemetry attribution re-expands the stage invoke string to recover the model identity
- **THEN** the resolved model values are still present in the environment and attribution reports the adapter-specific model

#### Scenario: Single-change execution activates models

- **WHEN** an operator runs a single change without a plan manifest
- **THEN** the synthesized configuration resolves and activates models the same way a manifest-backed run does

#### Scenario: Resolved optional role is exported

- **WHEN** a plan is loaded for an adapter whose `implementer_escalation` role resolves
- **THEN** `OPSX_IMPLEMENTER_ESCALATION_MODEL` is exported with that value for the remainder of the process

#### Scenario: Unresolved optional role is left unset

- **WHEN** a plan is loaded for an adapter whose `implementer_escalation` role does not resolve
- **THEN** `OPSX_IMPLEMENTER_ESCALATION_MODEL` is not exported and activation succeeds

### Requirement: Unresolved roles fail closed before dispatch

When a required role is unresolved, the orchestrator SHALL fail with an error naming the unresolved role and SHALL NOT dispatch a worker with an empty or defaulted model.

An unresolved optional role SHALL NOT by itself block a run. When a configuration setting elsewhere depends on an optional role, that setting SHALL be responsible for failing closed on the role's absence.

#### Scenario: Unresolved role blocks the run

- **WHEN** a plan is loaded and the `reviewer` role cannot be resolved for its adapter
- **THEN** the orchestrator reports the unresolved role and does not dispatch a review worker

#### Scenario: Unresolved optional role alone does not block the run

- **WHEN** a plan is loaded, all four required roles resolve, the `implementer_escalation` role does not resolve, and no configuration setting depends on it
- **THEN** the run proceeds and no unresolved-role error is raised

### Requirement: Operators can inspect and seed model configuration

The orchestrator SHALL provide a `models` command surface with the following subcommands:

- `opsx-plan models show [--adapter <name>]` SHALL print each role, including optional roles, together with its resolved model, its resolution source, and any identifier-syntax violations. An unresolved optional role SHALL be shown as unresolved rather than omitted. When `--adapter` is omitted, it SHALL use the active plan's adapter.
- `opsx-plan models env [--adapter <name>]` SHALL print shell `export` statements for the resolved variables, suitably quoted for evaluation by a shell. It SHALL exit non-zero when any required role is unresolved, and SHALL omit the export statement for an unresolved optional role without failing on that account.
- `opsx-plan models init` SHALL create `~/.config/opsx-controller/models.toml`, pre-populating role values, including optional roles, from the current environment where they are set.

The `show` and `env` subcommands SHALL operate without a resolved plan when `--adapter` is supplied.

#### Scenario: Operator inspects resolution and source

- **WHEN** an operator runs `opsx-plan models show --adapter claude-code`
- **THEN** the command prints every role with its resolved model and states, for each, whether the value came from a configuration file or the ambient environment

#### Scenario: Operator inspects an adapter outside a repository

- **WHEN** an operator runs `opsx-plan models show --adapter opencode` from a directory that is not a git repository
- **THEN** the command resolves against the user-global configuration and ambient environment and does not fail for lack of a plan or repository

#### Scenario: Environment output is consumable by a shell

- **WHEN** an operator evaluates the output of `opsx-plan models env --adapter opencode` in a shell
- **THEN** the `OPSX_*_MODEL` variables for the resolved roles are set to the values resolved for the `opencode` adapter

#### Scenario: Environment output fails closed on an unresolved required role

- **WHEN** `opsx-plan models env --adapter codex-cli` is run and at least one required role is unresolved
- **THEN** the command exits non-zero and does not emit a partial set of export statements that would appear to succeed

#### Scenario: Environment output succeeds with an unresolved optional role

- **WHEN** `opsx-plan models env --adapter opencode` is run, all four required roles resolve, and `implementer_escalation` does not
- **THEN** the command exits zero and emits export statements for the four required roles only

#### Scenario: Operator seeds a configuration file from the environment

- **WHEN** an operator has `OPSX_*_MODEL` variables exported and runs `opsx-plan models init`
- **THEN** `~/.config/opsx-controller/models.toml` is created with those values pre-populated, including any optional role that was exported
