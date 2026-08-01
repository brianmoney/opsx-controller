# shared-orchestrator-installation Specification

## Purpose

Define the common orchestrator deployment performed by every global adapter
installer.

## Requirements

### Requirement: Every global adapter install deploys the shared orchestrator

Each global adapter installer SHALL deploy the client-neutral `opsx-plan`,
`opsx-run`, and `opsx-watch-plan` executables to `~/.local/bin` and the
required `metrics`, `pricing`, `models`, and `orchestrator` runtime packages to
`~/.local/lib/opsx-controller/lib`.

The `orchestrator` runtime package carries the orchestrator implementation
modules that the `opsx-plan` entrypoint imports at startup. The entrypoint is
not self-contained: an installation that omits this package is incomplete and
SHALL be treated as stale.

Each global adapter installer SHALL additionally deploy the canonical sample
plan pair to `~/.local/lib/opsx-controller/samples`, so that compile prompts
carry a worked example regardless of which repository the orchestrator is
invoked against.

The installed executable paths and runtime layout SHALL be identical regardless
of whether OpenCode, Claude Code, or Codex CLI performed the installation.

#### Scenario: Claude global install provides the executables

- **WHEN** an operator runs `bash adapters/claude-code/install.sh --global`
- **THEN** `~/.local/bin/opsx-plan`, `~/.local/bin/opsx-run`,
  `~/.local/bin/opsx-watch-plan`, and their runtime libraries are installed

#### Scenario: Codex global install provides the executables

- **WHEN** an operator runs `bash adapters/codex-cli/install.sh --global`
- **THEN** `~/.local/bin/opsx-plan`, `~/.local/bin/opsx-run`,
  `~/.local/bin/opsx-watch-plan`, and their runtime libraries are installed

#### Scenario: Global install provides the orchestrator runtime package

- **WHEN** an operator runs any adapter's global installer
- **THEN** `~/.local/lib/opsx-controller/lib/orchestrator` is installed
  alongside the `metrics`, `pricing`, and `models` packages
- **AND** the installed `opsx-plan` runs its subcommands without importing
  anything from the repository checkout

#### Scenario: Global install provides the canonical sample pair

- **WHEN** an operator runs any adapter's global installer
- **THEN** the canonical sample plan markdown and its compiled TOML are
  installed under `~/.local/lib/opsx-controller/samples`

#### Scenario: Installed watcher follows stage logs

- **WHEN** an operator runs the installed `opsx-watch-plan` from a repository
  with `.opsx-plan/logs/`
- **THEN** it follows the newest stage log and switches to a newer stage log
  when one is created

### Requirement: Shared installation remains idempotent and diagnosable

The shared runtime installation SHALL replace its managed runtime libraries,
sample plan pair, and executables on repeated global installs. `opsx-plan doctor` SHALL continue
to detect an installed executable whose content differs from the repository
copy, independent of which adapter installer last deployed it.

Because the orchestrator implementation is split between the entrypoint and the
installed `orchestrator` runtime package, `opsx-plan doctor` SHALL also report
the installation as stale when an installed module differs from its repository
counterpart, or when the package is absent entirely. Matching the entrypoint
alone SHALL NOT be sufficient to report the installation as current.

#### Scenario: A non-OpenCode install is detected as stale

- **WHEN** an executable installed through the Claude Code or Codex CLI installer differs from the repository `opsx-plan.py`
- **THEN** `opsx-plan doctor` reports the installed copy as stale and instructs the operator to rerun an installer

#### Scenario: A stale runtime module is detected

- **WHEN** the installed entrypoint matches the repository `opsx-plan.py` but
  an installed orchestrator module differs from its repository counterpart
- **THEN** `opsx-plan doctor` reports the installation as stale and instructs
  the operator to rerun an installer

#### Scenario: A missing orchestrator package is detected

- **WHEN** `~/.local/lib/opsx-controller/lib` contains the `metrics`,
  `pricing`, and `models` packages but no `orchestrator` package
- **THEN** `opsx-plan doctor` reports the installation as stale

#### Scenario: Repeated install refreshes the sample pair

- **WHEN** an operator reruns a global installer after the canonical sample pair has changed in the repository
- **THEN** the installed sample pair is replaced with the current repository version

#### Scenario: Repeated install refreshes the orchestrator package

- **WHEN** an operator reruns a global installer after an orchestrator module
  has changed in the repository
- **THEN** the installed `orchestrator` package is replaced with the current
  repository version, and modules deleted from the repository do not persist
  in the installed copy
