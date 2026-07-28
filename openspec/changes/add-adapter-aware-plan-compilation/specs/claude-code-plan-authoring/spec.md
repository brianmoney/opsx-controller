## MODIFIED Requirements

### Requirement: Claude authoring reports compilation truthfully
The authoring agent SHALL run an `opsx-plan compile --adapter claude-code` self-check only when `opsx-plan` is available on PATH and a `controller` model resolves for the `claude-code` adapter.

When those prerequisites are available, the agent SHALL compile the authored document, correct plan structure or dependency defects exposed by compilation, and report the successful self-check. When either prerequisite is unavailable, it SHALL report that the Markdown document was authored but not compiled and state the missing Claude Code prerequisite.

The adapter SHALL NOT imply that successful Markdown authoring is successful TOML compilation.

#### Scenario: Claude-only authoring is not represented as compilation
- **WHEN** a Claude-only environment lacks `opsx-plan` or a Claude Code controller model
- **THEN** the agent reports the authored Markdown path and that compilation was not performed, including the missing Claude-selected prerequisite

#### Scenario: Available Claude compiler self-check passes
- **WHEN** `opsx-plan` is on PATH and a Claude Code controller model is configured
- **THEN** the agent runs `opsx-plan compile --adapter claude-code` for the authored document and reports compilation only after that command succeeds

### Requirement: Claude authoring is packaged for adapter and plugin use
The standalone Claude adapter SHALL package the `opsx-plan` skill and
`opsx-plan-author` agent. The standalone Claude plugin SHALL package equivalent
artifacts and expose `/opsx-controller:opsx-plan <planning request>` with
namespaced agent delegation.

The Claude global installer SHALL install the common `opsx-plan` and `opsx-run`
executables in addition to its adapter artifacts. Host-project, repository, and
plugin documentation SHALL state the authoring command, retain `/opsx-drive
<change-id>` for accepted single-change control, and document Claude-selected
compilation.

#### Scenario: Global and project installs include plan authoring
- **WHEN** the Claude adapter is installed globally or into a project
- **THEN** its installed skills include both `opsx-drive` and `opsx-plan`, and its installed agents include `opsx-plan-author`

#### Scenario: Global Claude install includes orchestrator commands
- **WHEN** an operator installs the Claude adapter globally
- **THEN** `opsx-plan` and `opsx-run` are available from the shared user-level executable location

#### Scenario: Plugin exposes the namespaced authoring command
- **WHEN** Claude Code loads `plugins/opsx-controller` through `--plugin-dir`
- **THEN** the plugin exposes `/opsx-controller:opsx-plan` and delegates its authoring request to the plugin-scoped `opsx-plan-author` agent
