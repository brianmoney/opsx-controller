## Purpose

Define how adapters support per-adapter, per-role model configuration and resolution, enabling operators to select different models for different roles and adapters without modifying installation artifacts.

## Requirements

### Requirement: Model selection is stored per adapter and per role

The system SHALL store model selection in a TOML configuration file keyed by adapter and by role, where the required roles are `controller`, `implementer`, `reviewer`, and `archiver`, and the optional roles are `implementer_escalation` plus the supervised roles `supervisor`, `supervised_author`, `acceptance_reviewer`, `fixer`, and `verifier`.

A required role is one every run needs; an optional role is one that only some configurations use, whose absence is not an error on its own. The supervised roles are optional roles that only supervised jobs use; no legacy run requires them, and a configuration that defines none of them SHALL resolve and dispatch exactly as before.

The file SHALL support an `[adapters.<adapter>]` table per adapter carrying zero or more role keys, and a `[defaults]` table carrying role keys that apply to every adapter that does not override them. Optional roles SHALL be settable in the same tables and by the same key names as required roles. The same files SHALL also carry the `[allowlist]` table defined below.

The primary configuration location SHALL be `~/.config/opsx-controller/models.toml`. A repository-local `<repo>/.opsx-plan/models.toml` SHALL be honored as a machine-local override when present.

Optional roles SHALL use the existing activation and inspection machinery
generically: a resolved optional role SHALL be exported as its
`OPSX_<ROLE>_MODEL` variable, an unresolved optional role SHALL be left
explicitly unset during process activation, `opsx-plan models show` SHALL
include every optional role, and `opsx-plan models env` SHALL emit only
resolved optional-role exports without role-specific code.

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

#### Scenario: Supervised role is configured like any other role

- **WHEN** a configuration file sets `[adapters.opencode].acceptance_reviewer` and the `acceptance_reviewer` role is resolved for `opencode`
- **THEN** resolution returns that value and reports the configuration file as its source, and the four required roles resolve unaffected

#### Scenario: Unset optional role is not an error

- **WHEN** no configuration file or environment variable supplies `implementer_escalation` and models are resolved for an adapter
- **THEN** resolution reports that role as unresolved and reports the four required roles normally, without raising

#### Scenario: Unset supervised roles are not an error

- **WHEN** no configuration file or environment variable supplies any supervised role and models are resolved for an adapter
- **THEN** resolution reports each supervised role as unresolved, reports the required roles normally, and does not raise

#### Scenario: Resolved and unresolved optional roles activate generically

- **WHEN** a plan is loaded for an adapter where one optional role resolves and another does not
- **THEN** the resolved optional role's `OPSX_<ROLE>_MODEL` variable is exported, the unresolved optional role's variable is left unset, and activation succeeds

#### Scenario: Inspection output includes supervised roles

- **WHEN** `opsx-plan models show` runs against a configuration with no supervised roles
- **THEN** it prints each supervised role as unresolved rather than omitting it, and the command succeeds

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

#### Scenario: Environment output succeeds with an unresolved optional role

- **WHEN** `opsx-plan models env --adapter opencode` is run, all four required roles resolve, and `implementer_escalation` does not
- **THEN** the command exits zero and emits export statements for the four required roles only

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

### Requirement: The inexpensive allowlist is operator-maintained model configuration

The inexpensive-model allowlist SHALL be stored in the same model configuration files as roles, in an `[allowlist]` table with a `models` key whose value is an array of exact model-identifier strings. Each entry SHALL be a non-empty, non-whitespace exact identifier; there SHALL be no wildcard, pattern, prefix, or default entry, and entries SHALL NOT be inferred from any role.

The effective allowlist SHALL be resolved from the same file locations and in
the same precedence as role selection: repository-local configuration first,
then user-global. If the repository-local file has no `[allowlist]` table,
resolution SHALL fall through to the user-global table. A repository-local
`[allowlist].models` SHALL replace the user-global list wholesale; lists SHALL
NOT merge across files. The allowlist SHALL have no environment-variable
source.

When no `[allowlist]` table exists in either configuration file, the effective allowlist SHALL be empty rather than inherited or defaulted, and resolution SHALL succeed. An `[allowlist]` table present but malformed — a `models` value that is not an array, or an entry that is not a non-empty string — SHALL fail resolution with a named error identifying the offending configuration file, rather than being ignored or silently coerced.

The allowlist SHALL NOT be consulted for legacy unsupervised runs: a configuration that defines no supervised roles SHALL behave exactly as before, with no allowlist required and no new error path.

#### Scenario: Allowlist resolves from configuration

- **WHEN** `[allowlist].models` lists exact identifiers in a configuration file
- **THEN** the effective allowlist is that list and resolution reports the configuration file as its source

#### Scenario: Repository-local allowlist replaces the user-global list

- **WHEN** both configuration files define `[allowlist].models` with different entries
- **THEN** the effective allowlist contains exactly the repository-local entries, with no merging

#### Scenario: Missing repository-local table falls through to user-global

- **WHEN** a repository-local configuration file exists without an `[allowlist]` table and the user-global file defines `[allowlist].models`
- **THEN** the effective allowlist contains exactly the user-global entries and reports the user-global file as its source

#### Scenario: No allowlist table means an empty allowlist

- **WHEN** neither configuration file contains an `[allowlist]` table
- **THEN** the effective allowlist is empty and resolution succeeds

#### Scenario: Malformed allowlist fails with a named error

- **WHEN** `[allowlist].models` is present but is not an array of non-empty strings
- **THEN** resolution fails with a named error naming the offending configuration file

#### Scenario: Environment supplies no allowlist entries

- **WHEN** an ambient `OPSX_*` variable names a model and no `[allowlist]` table exists
- **THEN** the effective allowlist remains empty, because the environment never contributes allowlist entries

#### Scenario: A legacy run never consults the allowlist

- **WHEN** an ordinary, unsupervised run resolves models in a configuration that defines no supervised roles and no allowlist
- **THEN** resolution and dispatch behave exactly as before, with no allowlist-related error

### Requirement: Supervised model-policy checks fail closed without an allowlisted model

A supervised feature evaluating a dispatch SHALL use the pure model-policy check
before dispatch. The check SHALL report a named blocking error when a required
role is unresolved, when its resolved model is not on the effective allowlist,
when its resolved model differs from the exact policy pin, or when the resolved
model is unavailable. "Unavailable" SHALL mean only that the resolved
identifier fails the target adapter's existing identifier-syntax validation;
live availability probing is out of scope. There SHALL be no inherited,
defaulted, or fallback model for a supervised dispatch: when the check reports
a block, its consumer SHALL NOT dispatch. Applying this precondition to a live
journal belongs to the later dispatch and lifecycle changes.

The supervised dispatch roles SHALL be the existing dispatch roles `implementer`, `reviewer`, and `archiver`, plus `supervised_author`, `acceptance_reviewer`, `fixer`, `verifier`, and `implementer_escalation`; each SHALL be subject to the allowlist check. The `supervisor` role SHALL be exempt from the allowlist check, as the supervision policy defines. The legacy `controller` role SHALL NOT be a supervised dispatch role.

The model-selection payload for a supervised job SHALL record an explicit
`stages.create` mapping to `supervised_author`; the legacy `controller` role
SHALL govern only non-supervised compilation. Routing a live create dispatch
through that mapping belongs to the later dispatch and lifecycle changes.

The allowlist SHALL NOT be consulted for legacy unsupervised runs: a configuration that defines no supervised roles SHALL behave exactly as before, with no allowlist required and no new error path.

#### Scenario: A missing supervised role blocks before dispatch

- **WHEN** the pure model-policy check evaluates a supervised feature requiring a role that no configuration source resolves
- **THEN** it reports a named blocking error identifying the role before a consumer may dispatch

#### Scenario: An unallowlisted model blocks before dispatch

- **WHEN** the pure model-policy check evaluates a supervised dispatch role whose resolved model is not on the effective allowlist
- **THEN** it reports a named blocking error identifying the role and the model, and no fallback model is substituted

#### Scenario: A model different from the policy pin blocks

- **WHEN** the pure model-policy check evaluates a supervised dispatch role whose resolved model differs from the exact `model_selection.roles` pin
- **THEN** it reports a named blocking error and does not substitute another role's model

#### Scenario: An available allowlisted role proceeds

- **WHEN** the pure model-policy check evaluates a supervised dispatch role that resolves to a model on the effective allowlist and passes the adapter's identifier-syntax validation
- **THEN** the check passes and a later dispatch consumer may proceed with that model

#### Scenario: An identifier-syntax-invalid model counts as unavailable

- **WHEN** the pure model-policy check evaluates a supervised dispatch role whose resolved identifier fails the target adapter's existing identifier-syntax validation
- **THEN** it reports a named blocking error without making any live availability claim

#### Scenario: The frontier supervisor is exempt from the allowlist check

- **WHEN** the pure model-policy check evaluates the `supervisor` role whose resolved model is not on the effective allowlist
- **THEN** the allowlist condition does not block it

#### Scenario: A legacy run never consults the allowlist

- **WHEN** an ordinary, unsupervised run resolves models in a configuration that defines no supervised roles and no allowlist
- **THEN** resolution and dispatch behave exactly as before, with no allowlist-related error

### Requirement: Operators can inspect the effective allowlist and supervised role membership

`opsx-plan models show` SHALL display the effective allowlist and its resolution source when an allowlist is configured, and SHALL report for each supervised dispatch role whether its resolved model is a member of the effective allowlist. An unresolved supervised role SHALL be shown as unresolved rather than omitted, and inspection SHALL never fail for a configuration with no supervised roles.

#### Scenario: Operator inspects the effective allowlist and membership

- **WHEN** `opsx-plan models show --adapter opencode` runs with an `[allowlist]` table configured
- **THEN** it prints the effective allowlist entries and their source, and for each supervised dispatch role reports whether its resolved model is a member

#### Scenario: No allowlist is reported without failing

- **WHEN** `opsx-plan models show` runs with no `[allowlist]` table and no supervised roles
- **THEN** it reports the allowlist as absent or empty and the supervised roles as unconfigured, and the command succeeds

### Requirement: The supervised author override is explicit and leaves the legacy compile role unchanged

A supervised job's model-selection payload SHALL record its create-stage model
as the `supervised_author` role, resolved like any other optional role, in the
explicit `stages.create` mapping. The override SHALL be explicit: registering
or resolving `supervised_author` SHALL NOT change the resolution, activation,
or dispatch of the legacy `controller` role, and the legacy compile role for
non-supervised runs SHALL remain `controller`. Actual supervised create
routing belongs to the later dispatch and lifecycle changes.

#### Scenario: The supervised author resolves independently

- **WHEN** a configuration file sets `[adapters.opencode].supervised_author` and the role is resolved for `opencode`
- **THEN** resolution returns that value for `supervised_author`, and the `controller` role resolves from its own sources unchanged

#### Scenario: Configuring the override does not alter the legacy compile role

- **WHEN** `supervised_author` is configured and a non-supervised compile runs
- **THEN** the compile dispatches with the model resolved for `controller`, exactly as if `supervised_author` were unset

### Requirement: Supervised budget pricing binds each role to its pinned model identity

For a registered supervised job, cost estimation for budget purposes SHALL
price each dispatch using the exact model identifier pinned for that
dispatch's role in the job policy's `model_selection`, resolved through the
existing per-adapter, per-role precedence. There SHALL be no cross-role
fallback: a dispatch for one role SHALL NOT be priced using another role's
resolved or configured model, and an ambient or default model SHALL NOT be
substituted for the pin.

The `supervisor` role SHALL be priced by the same binding: its exemption
from the inexpensive allowlist does not exempt it from pricing or budget
counting. This is a pricing and reservation contract for the role pin; it does
not require this change to own a production supervisor-primary invocation. The
supervisor primary's call site is wired by `add-opencode-session-bridge`, which
routes its usage through this change's reserve/reconcile boundary.

A role whose pinned identifier cannot be resolved to a price SHALL block
budgeted dispatch with a named unknown-pricing error rather than falling
back to any other identity or an assumed cost, as required by the budget
policy's unknown-pricing rule.

#### Scenario: Pricing uses the exact role pin

- **WHEN** a reservation estimate is computed for a supervised dispatch
- **THEN** the price lookup uses the exact model identifier pinned for that
  dispatch's role in the job policy, resolved through the existing precedence

#### Scenario: No cross-role pricing fallback

- **WHEN** a supervised dispatch's role pin cannot be priced but another
  role's resolved model can be
- **THEN** the dispatch is not priced from the other role's identity; it is
  blocked with the named unknown-pricing error

#### Scenario: The allowlist-exempt supervisor is still priced

- **WHEN** a reservation estimate is computed for the `supervisor` role
- **THEN** its pinned frontier model is looked up in the pricing catalog and
  budget-counted like any other role, and an unpriceable supervisor pin
  blocks dispatch with the named unknown-pricing error
