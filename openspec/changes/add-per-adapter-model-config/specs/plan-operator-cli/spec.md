## ADDED Requirements

### Requirement: `opsx-plan models` exposes model resolution to operators

The orchestrator SHALL provide an `opsx-plan models` subcommand group alongside the other operator-facing commands.

`opsx-plan models show [--adapter <name>]` SHALL print one line per role giving the role name, the resolved model, and the resolution source, and SHALL report any identifier-syntax violations for the target adapter.

`opsx-plan models env [--adapter <name>]` SHALL emit shell `export` statements for the four resolved `OPSX_*_MODEL` variables and SHALL exit non-zero if any role is unresolved.

`opsx-plan models init` SHALL create the user-global model configuration file, pre-populating role values from the current environment where set, and SHALL NOT overwrite an existing file without an explicit force flag.

When `--adapter` is omitted, `models show` and `models env` SHALL resolve the adapter from the active plan using the same plan-resolution precedence as other operator commands. When `--adapter` is supplied, they SHALL NOT require a resolvable plan.

#### Scenario: Operator inspects the active plan's models

- **WHEN** an operator runs `opsx-plan models show` with an active plan whose adapter is `claude-code`
- **THEN** the command reports the four roles resolved for `claude-code`, each with its resolution source

#### Scenario: Operator inspects an explicit adapter without an active plan

- **WHEN** an operator runs `opsx-plan models show --adapter opencode` with no active plan set
- **THEN** the command resolves and prints the `opencode` model set instead of failing for lack of a plan

#### Scenario: Models init refuses to clobber an existing file

- **WHEN** an operator runs `opsx-plan models init` and the user-global configuration file already exists
- **THEN** the command leaves the existing file unchanged and reports that a force flag is required to replace it

## MODIFIED Requirements

### Requirement: `doctor` checks the known plan-independent environment gotchas

The `doctor` command SHALL check whether the installed orchestrator copy under `~/.local/bin` matches the repository orchestrator copy by content hash.

The `doctor` command SHALL check that every model role resolves for the target adapter, reporting each resolved model together with its resolution source, and SHALL fail when any role is unresolved. The target adapter SHALL be the resolved plan's adapter when a plan is available.

The `doctor` command SHALL check that each resolved model identifier is valid for the target adapter's identifier syntax, and SHALL fail when a resolved identifier is provider-prefixed for the `claude-code` adapter or lacks a `provider/` prefix for the `opencode` adapter.

The `doctor` command SHALL check that `openspec` and the configured adapter client executable are available on `PATH`.

The `doctor` command SHALL check that the tracked worktree contains no tracked `__pycache__` directories or tracked `.pyc` files.

The `doctor` command SHALL check that the tracked tree is clean.

#### Scenario: Doctor detects a stale installed orchestrator copy

- **WHEN** the repository copy of `opsx-plan` differs from the installed `~/.local/bin` copy
- **THEN** `opsx-plan doctor` reports that the install is stale and tells the operator to rerun the relevant installer

#### Scenario: Doctor detects unresolved model roles

- **WHEN** one or more model roles cannot be resolved for the target adapter
- **THEN** `opsx-plan doctor` reports each unresolved role and exits non-zero

#### Scenario: Doctor reports the resolution source for each model

- **WHEN** an operator runs `opsx-plan doctor` and all roles resolve
- **THEN** the model check reports each resolved model together with whether it came from a configuration file or the ambient environment

#### Scenario: Doctor rejects a provider-prefixed identifier under Claude Code

- **WHEN** the resolved plan's adapter is `claude-code` and a role resolves to a provider-prefixed identifier such as `deepseek/deepseek-v4-pro`
- **THEN** `opsx-plan doctor` fails the identifier-syntax check, names the offending role, and exits non-zero instead of allowing the run to fail later at stage dispatch

#### Scenario: Doctor rejects a bare identifier under OpenCode

- **WHEN** the resolved plan's adapter is `opencode` and a role resolves to an identifier with no `provider/` prefix
- **THEN** `opsx-plan doctor` fails the identifier-syntax check, names the offending role, and exits non-zero

#### Scenario: Doctor detects missing CLI dependencies

- **WHEN** `openspec` or the configured adapter client is not available on `PATH`
- **THEN** `opsx-plan doctor` reports the missing executable name and exits non-zero

#### Scenario: Doctor detects tracked bytecode artifacts

- **WHEN** the tracked tree contains a tracked `__pycache__` directory or tracked `.pyc` file
- **THEN** `opsx-plan doctor` reports the tracked bytecode artifact and tells the operator to remove it from version control

#### Scenario: Doctor detects a dirty tracked tree

- **WHEN** tracked files have uncommitted modifications
- **THEN** `opsx-plan doctor` reports that the tracked tree is dirty and tells the operator to clean or commit the changes before running unattended work
