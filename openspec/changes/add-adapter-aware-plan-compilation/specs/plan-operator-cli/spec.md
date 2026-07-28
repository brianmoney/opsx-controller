## MODIFIED Requirements

### Requirement: Operators can run a deterministic `opsx-plan doctor` preflight
The orchestrator SHALL provide an `opsx-plan doctor [plan] [--adapter <adapter>]` command that reports known local-environment failure modes before a run or compile starts.

The `doctor` command SHALL emit one human-readable pass/fail line per check.

Every failing `doctor` check SHALL include a remediation hint.

If any `doctor` check fails, the command SHALL exit non-zero.

#### Scenario: Doctor reports pass/fail lines and exits non-zero on failure
- **WHEN** an operator runs `opsx-plan doctor` and at least one preflight check fails
- **THEN** the command prints a distinct pass/fail line for each check, includes a remediation hint for each failing check, and exits non-zero

#### Scenario: Plan-less doctor selects a compile adapter
- **WHEN** an operator runs `opsx-plan doctor --adapter claude-code` with no resolvable plan
- **THEN** doctor preflights Claude Code model and client prerequisites

### Requirement: `doctor` checks the known plan-independent environment gotchas
The `doctor` command SHALL check whether the installed orchestrator copy under `~/.local/bin` matches the repository orchestrator copy by content hash.

The `doctor` command SHALL check that every model role resolves for the target adapter, reporting each resolved model together with its resolution source, and SHALL fail when any role is unresolved. The target adapter SHALL be the resolved plan's adapter when a plan is available; otherwise it SHALL be the explicit `--adapter` value or the default `opencode` value.

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
- **WHEN** the target adapter is `claude-code` and a role resolves to a provider-prefixed identifier such as `deepseek/deepseek-v4-pro`
- **THEN** `opsx-plan doctor` fails the identifier-syntax check, names the offending role, and exits non-zero instead of allowing the client to fail later

#### Scenario: Doctor rejects a bare identifier under OpenCode
- **WHEN** the target adapter is `opencode` and a role resolves to an identifier with no `provider/` prefix
- **THEN** `opsx-plan doctor` fails the identifier-syntax check, names the offending role, and exits non-zero

#### Scenario: Doctor detects missing CLI dependencies
- **WHEN** `openspec` or the configured adapter client is not available on `PATH`
- **THEN** `opsx-plan doctor` reports the missing executable name and exits non-zero
