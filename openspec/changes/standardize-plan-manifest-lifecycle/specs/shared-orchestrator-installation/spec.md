## MODIFIED Requirements

### Requirement: Every global adapter install deploys the shared orchestrator

Each global adapter installer SHALL deploy the client-neutral `opsx-plan` and
`opsx-run` executables to `~/.local/bin` and the required `metrics`, `pricing`,
and `models` runtime packages to `~/.local/lib/opsx-controller/lib`.

Each global adapter installer SHALL additionally deploy the canonical sample plan pair to `~/.local/lib/opsx-controller/samples`, so that compile prompts carry a worked example regardless of which repository the orchestrator is invoked against.

The installed executable paths and runtime layout SHALL be identical regardless
of whether OpenCode, Claude Code, or Codex CLI performed the installation.

#### Scenario: Claude global install provides the executables

- **WHEN** an operator runs `bash adapters/claude-code/install.sh --global`
- **THEN** `~/.local/bin/opsx-plan`, `~/.local/bin/opsx-run`, and their runtime libraries are installed

#### Scenario: Codex global install provides the executables

- **WHEN** an operator runs `bash adapters/codex-cli/install.sh --global`
- **THEN** `~/.local/bin/opsx-plan`, `~/.local/bin/opsx-run`, and their runtime libraries are installed

#### Scenario: Global install provides the canonical sample pair

- **WHEN** an operator runs any adapter's global installer
- **THEN** the canonical sample plan markdown and its compiled TOML are installed under `~/.local/lib/opsx-controller/samples`

### Requirement: Shared installation remains idempotent and diagnosable

The shared runtime installation SHALL replace its managed runtime libraries,
sample plan pair, and executables on repeated global installs. `opsx-plan doctor` SHALL continue
to detect an installed executable whose content differs from the repository
copy, independent of which adapter installer last deployed it.

#### Scenario: A non-OpenCode install is detected as stale

- **WHEN** an executable installed through the Claude Code or Codex CLI installer differs from the repository `opsx-plan.py`
- **THEN** `opsx-plan doctor` reports the installed copy as stale and instructs the operator to rerun an installer

#### Scenario: Repeated install refreshes the sample pair

- **WHEN** an operator reruns a global installer after the canonical sample pair has changed in the repository
- **THEN** the installed sample pair is replaced with the current repository version
