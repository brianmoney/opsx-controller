# universal-installer Specification

## Purpose

Provides a single repository-root installer that deploys every supported adapter and the shared orchestrator in one invocation.

## Requirements

### Requirement: Universal installer installs every adapter

The repository SHALL provide an `install.sh` at the repository root that installs all four supported adapters (opencode, claude-code, codex-cli, dsh) and the shared orchestrator in one invocation. Running the universal installer SHALL have the same effect as running each individual adapter installer's corresponding command.

#### Scenario: Universal global install deploys every adapter

- **WHEN** an operator runs `bash install.sh --global`
- **THEN** the global installs for opencode, claude-code, codex-cli, and dsh all complete successfully
- **AND** the shared `opsx-plan`, `opsx-run`, and `opsx-watch-plan` executables are installed to `~/.local/bin`

#### Scenario: Universal project install deploys every adapter

- **WHEN** an operator runs `bash install.sh --project /path/to/project`
- **THEN** each adapter's project-scoped artifacts are installed under the project
- **AND** the shared orchestrator runtime is installed under `/path/to/project/.opsx-controller`

#### Scenario: Project install provides a self-contained orchestrator runtime

- **WHEN** an operator runs `bash install.sh --project /path/to/project`
- **THEN** `opsx-plan`, `opsx-run`, and `opsx-watch-plan` are installed to `/path/to/project/.opsx-controller/bin`
- **AND** the `metrics`, `pricing`, `models`, and `orchestrator` runtime packages are installed under `/path/to/project/.opsx-controller/lib`
- **AND** the installed `opsx-plan` runs its subcommands without importing anything from the repository checkout
- **AND** the project runtime directory is ignored by the project's git history while the dsh adapter's tracked `.opsx-controller/dsh` support files are not

#### Scenario: Universal install delegates to adapter installers

- **WHEN** an operator runs the universal installer
- **THEN** each adapter's own `install.sh` is invoked with the same mode and flags rather than the universal installer reimplementing adapter-specific installation logic

### Requirement: Universal installer supports selection and verification flags

The universal installer SHALL accept `--global`, `--project <path>`, and `--verify` with the same semantics as the adapter installers, and SHALL accept an `--only <adapter>` flag that limits installation to a subset of adapters. When `--only` is omitted, all four adapters SHALL be installed.

#### Scenario: Installing a subset with --only

- **WHEN** an operator runs `bash install.sh --global --only opencode`
- **THEN** only the opencode adapter is installed globally
- **AND** the claude-code, codex-cli, and dsh adapters are not installed

#### Scenario: Verify flag reports installation status

- **WHEN** an operator runs `bash install.sh --global --verify`
- **THEN** the installer runs verification checks for each installed adapter and reports any missing artifacts

### Requirement: Universal installer preserves existing adapter installers

Each existing adapter `install.sh` SHALL remain directly runnable and unchanged in behavior. The universal installer SHALL NOT remove or alter the standalone adapter installers.

#### Scenario: Adapter installer still works standalone

- **WHEN** an operator runs `bash adapters/claude-code/install.sh --global` directly
- **THEN** the claude-code adapter and shared orchestrator are installed exactly as before the universal installer existed
