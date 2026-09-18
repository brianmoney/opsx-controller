# shared-orchestrator-installation Specification

## Purpose

Define the common orchestrator deployment performed by every global adapter
installer.

## Requirements

### Requirement: Every global adapter install deploys the shared orchestrator

Each global adapter installer SHALL deploy the client-neutral `opsx-plan`,
`opsx-run`, and `opsx-watch-plan` executables to `~/.local/bin` and the
required `metrics`, `pricing`, `models`, `orchestrator`, and `supervisor`
runtime packages to `~/.local/lib/opsx-controller/lib`.

The `orchestrator` runtime package carries the orchestrator implementation
modules that the `opsx-plan` entrypoint imports at startup. The entrypoint is
not self-contained: an installation that omits this package is incomplete and
SHALL be treated as stale.

The `supervisor` runtime package carries the durable supervision ledger
modules. It SHALL be deployed on every global install even though no
subcommand imports it yet, so that intermediate installs performed while
supervision changes land always carry the complete runtime.

Each global adapter installer SHALL additionally deploy the canonical sample
plan pair to `~/.local/lib/opsx-controller/samples`, so that compile prompts
carry a worked example regardless of which repository the orchestrator is
invoked against.

Each global adapter installer SHALL additionally deploy the client-neutral
plan-authoring reference to `~/.local/lib/opsx-controller/plan-authoring.md`.

Each global adapter installer SHALL additionally deploy the supervision service
packaging: the versioned systemd user unit template for the supervision service
host and the provisioning document that describes the manual account, store,
and activation steps. These artifacts SHALL be deployed into the installed
runtime tree, SHALL be replaced on a repeated install, and SHALL be deployed
disabled — the installer SHALL NOT enable, start, or activate the service and
SHALL NOT create or modify operating-system accounts.

The universal installer SHALL deploy the shared orchestrator and the
supervision service packaging through the same mechanism as the adapter
installers, producing an installed layout identical to an adapter-only install.

The installed executable paths, runtime layout, sample paths, reference path,
and service-packaging paths SHALL be identical regardless of whether OpenCode,
Claude Code, Codex CLI, dsh, or the universal installer performed the
installation.

#### Scenario: Claude global install provides the executables

- **WHEN** an operator runs `bash adapters/claude-code/install.sh --global`
- **THEN** `~/.local/bin/opsx-plan`, `~/.local/bin/opsx-run`,
  `~/.local/bin/opsx-watch-plan`, and their runtime libraries are installed

#### Scenario: Codex global install provides the executables

- **WHEN** an operator runs `bash adapters/codex-cli/install.sh --global`
- **THEN** `~/.local/bin/opsx-plan`, `~/.local/bin/opsx-run`,
  `~/.local/bin/opsx-watch-plan`, and their runtime libraries are installed

#### Scenario: dsh global install provides the executables

- **WHEN** an operator runs `bash adapters/dsh/install.sh --global`
- **THEN** `~/.local/bin/opsx-plan`, `~/.local/bin/opsx-run`,
  `~/.local/bin/opsx-watch-plan`, and their runtime libraries are installed

#### Scenario: Universal global install provides the executables

- **WHEN** an operator runs `bash install.sh --global`
- **THEN** `~/.local/bin/opsx-plan`, `~/.local/bin/opsx-run`,
  `~/.local/bin/opsx-watch-plan`, and their runtime libraries are installed

#### Scenario: Global install provides the orchestrator runtime package

- **WHEN** an operator runs any adapter's global installer or the universal installer
- **THEN** `~/.local/lib/opsx-controller/lib/orchestrator` is installed
  alongside the `metrics`, `pricing`, `models`, and `supervisor` packages
- **AND** the installed `opsx-plan` runs its subcommands without importing
  anything from the repository checkout

#### Scenario: Global install provides the supervisor runtime package

- **WHEN** an operator runs any adapter's global installer or the universal
  installer
- **THEN** `~/.local/lib/opsx-controller/lib/supervisor` is installed
  alongside the other runtime packages, and its modules import without
  referencing the repository checkout

#### Scenario: Global install provides the canonical sample pair

- **WHEN** an operator runs any adapter's global installer
- **THEN** the canonical sample plan markdown and its compiled TOML are
  installed under `~/.local/lib/opsx-controller/samples`

#### Scenario: Global install provides the plan-authoring reference

- **WHEN** an operator runs any adapter's global installer
- **THEN** `~/.local/lib/opsx-controller/plan-authoring.md` contains the
  current repository reference document

#### Scenario: Global install provides the disabled supervision service artifacts

- **WHEN** an operator runs any adapter's global installer or the universal
  installer
- **THEN** the supervision unit template and provisioning document are installed
  in the runtime tree, the service is neither enabled nor started, and no
  operating-system account is created or modified

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

### Requirement: The installed supervisor package includes the model-policy module

Every global adapter installer and the universal installer SHALL deploy the `lib/supervisor` package including its model-policy module (`model_policy.py`), so the installed runtime can validate and decode supervised model policy without importing from the repository checkout. The deployed `lib/supervisor` package SHALL remain standard-library only and SHALL NOT import `lib.models` or any other runtime package. An installation whose deployed `lib/supervisor` package omits the model-policy module SHALL be treated as stale, consistent with the existing runtime-package staleness rule.

`opsx-plan doctor` SHALL diagnose the supervised model configuration alongside the existing installation checks: it SHALL report each supervised role's resolution state and the effective allowlist's presence and coverage. An unresolved supervised role or an absent allowlist SHALL NOT make an installation stale or fail the doctor check for an operator who configures no supervised roles.

The installation staleness probe SHALL also compare the deployed
`lib/supervisor` package, including `model_policy.py`, with the repository
package. A missing or differing supervisor module SHALL make the installation
stale and SHALL direct the operator to rerun an installer.

#### Scenario: A global install deploys the model-policy module

- **WHEN** an operator runs any adapter's global installer or the universal installer into a temporary installation sandbox
- **THEN** the deployed `lib/supervisor` package includes `model_policy.py`, and it imports without referencing the repository checkout

#### Scenario: The installed model-policy module preserves the package boundary

- **WHEN** the installed `model_policy.py` is imported with only the installed runtime on `sys.path`
- **THEN** it imports and validates policy payloads using only the standard library and the `lib.supervisor` package

#### Scenario: A stale supervisor module is detected

- **WHEN** the installed `lib/supervisor/model_policy.py` is missing or differs from the repository copy
- **THEN** `opsx-plan doctor` reports the installation as stale and directs the operator to rerun an installer

#### Scenario: Doctor reports the supervised model configuration

- **WHEN** an operator runs `opsx-plan doctor` with supervised roles and an allowlist configured
- **THEN** doctor reports each supervised role's resolution state and the effective allowlist's presence and coverage alongside the existing checks

#### Scenario: Doctor stays green without supervised roles

- **WHEN** an operator runs `opsx-plan doctor` with no supervised roles and no allowlist configured
- **THEN** the installation is reported exactly as before, with the supervised roles shown as unconfigured rather than as an error
