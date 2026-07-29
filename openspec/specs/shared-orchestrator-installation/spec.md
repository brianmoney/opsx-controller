# shared-orchestrator-installation Specification

## Purpose

Define the common orchestrator deployment performed by every global adapter
installer.

## Requirements

### Requirement: Every global adapter install deploys the shared orchestrator

Each global adapter installer SHALL deploy the client-neutral `opsx-plan` and
`opsx-run` executables to `~/.local/bin` and the required `metrics`, `pricing`,
and `models` runtime packages to `~/.local/lib/opsx-controller/lib`.

The installed executable paths and runtime layout SHALL be identical regardless
of whether OpenCode, Claude Code, or Codex CLI performed the installation.

#### Scenario: Claude global install provides the executables

- **WHEN** an operator runs `bash adapters/claude-code/install.sh --global`
- **THEN** `~/.local/bin/opsx-plan`, `~/.local/bin/opsx-run`, and their runtime libraries are installed

#### Scenario: Codex global install provides the executables

- **WHEN** an operator runs `bash adapters/codex-cli/install.sh --global`
- **THEN** `~/.local/bin/opsx-plan`, `~/.local/bin/opsx-run`, and their runtime libraries are installed

### Requirement: Shared installation remains idempotent and diagnosable

The shared runtime installation SHALL replace its managed runtime libraries
and executables on repeated global installs. `opsx-plan doctor` SHALL continue
to detect an installed executable whose content differs from the repository
copy, independent of which adapter installer last deployed it.

#### Scenario: A non-OpenCode install is detected as stale

- **WHEN** an executable installed through the Claude Code or Codex CLI installer differs from the repository `opsx-plan.py`
- **THEN** `opsx-plan doctor` reports the installed copy as stale and instructs the operator to rerun an installer
