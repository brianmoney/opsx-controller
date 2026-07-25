## Purpose

Define how adapters support per-adapter, per-role model configuration and resolution, enabling operators to select different models for different roles and adapters without modifying installation artifacts.

## Requirements

### Requirement: Model selection is stored per adapter and per role

The system SHALL store model selection in a TOML configuration file keyed by adapter and by role, where the roles are `controller`, `implementer`, `reviewer`, and `archiver`.

The file SHALL support an `[adapters.<adapter>]` table per adapter carrying zero or more role keys, and a `[defaults]` table carrying role keys that apply to every adapter that does not override them.

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

### Requirement: Model resolution follows a defined precedence order

Resolution of a `(adapter, role)` pair SHALL apply the following sources in order, highest precedence first:

1. `[adapters.<adapter>].<role>` in the repository-local configuration file
2. `[adapters.<adapter>].<role>` in the user-global configuration file
3. `[defaults].<role>` in the repository-local configuration file, then in the user-global configuration file
4. the ambient `OPSX_<ROLE>_MODEL` environment variable
5. unresolved

A configuration file value SHALL take precedence over the ambient environment variable for the same role.

When a role resolves to no value from any source, the role SHALL be reported as unresolved rather than defaulted to an arbitrary model.

Resolution SHALL record the source of each resolved value so that it can be reported to the operator.

#### Scenario: Configuration file overrides an exported environment variable

- **WHEN** `OPSX_IMPLEMENTER_MODEL` is exported in the environment and `[adapters.claude-code].implementer` is set in the configuration file
- **THEN** resolving the `implementer` role for `claude-code` returns the configuration file value and reports the configuration file as its source

#### Scenario: Environment variable is used when no file entry exists

- **WHEN** no configuration file exists at either location and `OPSX_REVIEWER_MODEL` is exported
- **THEN** resolving the `reviewer` role returns the exported value and reports the environment variable as its source

#### Scenario: Repository-local file overrides the user-global file

- **WHEN** both configuration files define `[adapters.opencode].controller`
- **THEN** resolution returns the repository-local value

#### Scenario: A role with no source is unresolved

- **WHEN** no configuration file defines the `archiver` role for the active adapter or in `[defaults]`, and `OPSX_ARCHIVER_MODEL` is unset or empty
- **THEN** resolution reports the `archiver` role as unresolved

### Requirement: Model resolution degrades safely on missing or malformed input

Resolution SHALL succeed when no configuration file exists at either location, falling through to the ambient environment.

Resolution SHALL succeed when invoked outside any git repository, in which case only the user-global configuration file is consulted.

When a configuration file exists but cannot be parsed as TOML, resolution SHALL fail with an error naming the offending file rather than silently ignoring it.

#### Scenario: No configuration file present

- **WHEN** neither `~/.config/opsx-controller/models.toml` nor a repository-local override exists
- **THEN** resolution completes using the ambient environment and does not raise an error

#### Scenario: Resolution outside a repository

- **WHEN** resolution is requested with no repository context
- **THEN** only the user-global configuration file and the ambient environment are consulted

#### Scenario: Malformed configuration file fails loudly

- **WHEN** a configuration file exists but contains invalid TOML
- **THEN** resolution fails with an error that names the file path

### Requirement: Resolved identifiers are validated against adapter identifier syntax

The system SHALL validate resolved model identifiers against the identifier syntax the target adapter accepts.

A resolved identifier containing `/` SHALL be reported as invalid for the `claude-code` adapter, because the Claude Code CLI rejects provider-prefixed identifiers.

A resolved identifier not containing `/` SHALL be reported as invalid for the `opencode` adapter, because OpenCode requires the `provider/model` form.

Validation SHALL report every violating role rather than stopping at the first.

#### Scenario: Provider-prefixed identifier is rejected for Claude Code

- **WHEN** the `implementer` role resolves to `deepseek/deepseek-v4-pro` for the `claude-code` adapter
- **THEN** validation reports that role as carrying a provider-prefixed identifier that Claude Code does not accept

#### Scenario: Bare identifier is rejected for OpenCode

- **WHEN** the `reviewer` role resolves to `gpt-5.4` for the `opencode` adapter
- **THEN** validation reports that role as missing the required `provider/` prefix

#### Scenario: Multiple violations are all reported

- **WHEN** two roles resolve to identifiers that violate the active adapter's syntax
- **THEN** validation reports both roles

### Requirement: Resolved models are activated for the whole orchestrator process

When the orchestrator constructs a plan configuration, it SHALL resolve all four roles against that configuration's adapter and SHALL export the resolved values as the corresponding `OPSX_<ROLE>_MODEL` environment variables for the remainder of the process.

Activation SHALL occur for every path that constructs a plan configuration, including plan-manifest loading and single-change execution.

The exported values SHALL remain in effect for the whole process rather than being scoped to individual subprocess invocations, so that consumers running after a stage completes observe the same values the stage was dispatched with.

#### Scenario: Stage dispatch receives the adapter-specific model

- **WHEN** a plan whose adapter is `claude-code` is loaded and a stage invoke references `$OPSX_IMPLEMENTER_MODEL`
- **THEN** the dispatched command receives the value resolved for the `claude-code` adapter

#### Scenario: Post-stage consumers observe the same values

- **WHEN** a stage completes and telemetry attribution re-expands the stage invoke string to recover the model identity
- **THEN** the resolved model values are still present in the environment and attribution reports the adapter-specific model

#### Scenario: Single-change execution activates models

- **WHEN** an operator runs a single change without a plan manifest
- **THEN** the synthesized configuration resolves and activates models the same way a manifest-backed run does

### Requirement: Unresolved roles fail closed before dispatch

When a required role is unresolved, the orchestrator SHALL fail with an error naming the unresolved role and SHALL NOT dispatch a worker with an empty or defaulted model.

#### Scenario: Unresolved role blocks the run

- **WHEN** a plan is loaded and the `reviewer` role cannot be resolved for its adapter
- **THEN** the orchestrator reports the unresolved role and does not dispatch a review worker

### Requirement: Operators can inspect and seed model configuration

The orchestrator SHALL provide a `models` command surface with the following subcommands:

- `opsx-plan models show [--adapter <name>]` SHALL print each role, its resolved model, and its resolution source, together with any identifier-syntax violations. When `--adapter` is omitted, it SHALL use the active plan's adapter.
- `opsx-plan models env [--adapter <name>]` SHALL print shell `export` statements for the four resolved variables, suitably quoted for evaluation by a shell, and SHALL exit non-zero when any role is unresolved.
- `opsx-plan models init` SHALL create `~/.config/opsx-controller/models.toml`, pre-populating role values from the current environment where they are set.

The `show` and `env` subcommands SHALL operate without a resolved plan when `--adapter` is supplied.

#### Scenario: Operator inspects resolution and source

- **WHEN** an operator runs `opsx-plan models show --adapter claude-code`
- **THEN** the command prints all four roles with their resolved models and states, for each, whether the value came from a configuration file or the ambient environment

#### Scenario: Operator inspects an adapter outside a repository

- **WHEN** an operator runs `opsx-plan models show --adapter opencode` from a directory that is not a git repository
- **THEN** the command resolves against the user-global configuration and ambient environment and does not fail for lack of a plan or repository

#### Scenario: Environment output is consumable by a shell

- **WHEN** an operator evaluates the output of `opsx-plan models env --adapter opencode` in a shell
- **THEN** the four `OPSX_*_MODEL` variables are set to the values resolved for the `opencode` adapter

#### Scenario: Environment output fails closed on an unresolved role

- **WHEN** `opsx-plan models env --adapter codex-cli` is run and at least one role is unresolved
- **THEN** the command exits non-zero and does not emit a partial set of export statements that would appear to succeed

#### Scenario: Operator seeds a configuration file from the environment

- **WHEN** an operator has the four `OPSX_*_MODEL` variables exported and runs `opsx-plan models init`
- **THEN** `~/.config/opsx-controller/models.toml` is created with those values pre-populated

### Requirement: Adapter installers resolve models through the resolver

Adapter installers that bake a model value into installed artifacts SHALL obtain that value from the resolver for the adapter being installed, rather than reading `OPSX_*_MODEL` environment variables directly.

Installers SHALL reach the resolver through the controller source tree rather than requiring the orchestrator to be installed on `PATH`, so that installing one adapter does not depend on another adapter having been installed first.

When resolution fails for the adapter being installed, the installer SHALL exit non-zero with guidance identifying the unresolved role and the configuration file to edit, and SHALL NOT install artifacts carrying an empty model value.

#### Scenario: Installer bakes the adapter-specific model

- **WHEN** an operator installs an adapter whose configuration file entry differs from the ambient environment
- **THEN** the installed artifacts carry the configuration file value

#### Scenario: Installer does not require the orchestrator on PATH

- **WHEN** an adapter installer runs on a machine where `opsx-plan` is not on `PATH`
- **THEN** the installer still resolves models successfully from the controller source tree

#### Scenario: Installer fails closed on unresolved roles

- **WHEN** an adapter installer runs and a required role is unresolved
- **THEN** the installer exits non-zero naming the unresolved role and installs no artifact containing an empty model value
