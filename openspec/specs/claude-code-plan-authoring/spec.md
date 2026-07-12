# claude-code-plan-authoring Specification

## Purpose
TBD - created by archiving change add-claude-code-plan-authoring. Update Purpose after archive.
## Requirements
### Requirement: Claude Code can author one implementation-plan document

The Claude Code adapter SHALL provide `/opsx-plan <planning request>` as the
authoring surface for exactly one phased OpenSpec implementation-plan Markdown
document.

The skill SHALL reject an empty planning request and SHALL delegate authoring
to an explicitly named `opsx-plan-author` agent. It SHALL NOT delegate to a
generic `build` agent or expose a second `/opsx-author` command.

#### Scenario: Valid request delegates to the dedicated author

- **WHEN** an operator invokes `/opsx-plan` with a non-empty planning request
- **THEN** the skill passes the complete request to `opsx-plan-author` and
  returns that agent's authoring result

#### Scenario: Empty request is rejected

- **WHEN** an operator invokes `/opsx-plan` without a planning request
- **THEN** the skill reports the required command syntax and does not author a
  plan document

### Requirement: Claude-authored plans follow the shared machine-read convention

Before writing a document, the authoring agent SHALL read available `CLAUDE.md`
and `AGENTS.md` guidance, all source material referenced by the request,
existing capabilities, and active and archived OpenSpec change IDs.

Unless the request supplies a path, the agent SHALL write to
`docs/plans/<kebab-case-topic>-plan.md`. It SHALL refuse to overwrite an
existing document unless the request explicitly asks to replace or revise it.

The authored document SHALL include the required frontmatter, purpose,
capability ownership where applicable, phase and change structure, dependency
syntax, scope boundaries, success parameters, recommended sequence, completion
criteria, and explicit non-goals recognized by `opsx-plan compile`. The agent
SHALL re-scan every dependency paragraph before reporting success.

#### Scenario: Existing plan is protected

- **WHEN** the resolved output path already exists and the request does not
  explicitly ask to replace or revise it
- **THEN** the agent reports the conflict and leaves the existing document
  unchanged

#### Scenario: Dependency syntax is checked before success

- **WHEN** the agent authors a plan containing change dependencies
- **THEN** it re-scans each `**Depends on:**` paragraph so only intended
  backticked change IDs and phase references create compiler DAG edges

### Requirement: Claude authoring reports OpenCode compilation truthfully

The authoring agent SHALL run an `opsx-plan compile` self-check only when
`opsx-plan` is available on PATH and `OPSX_CONTROLLER_MODEL` is non-empty.

When those prerequisites are available, the agent SHALL compile the authored
document, correct plan structure or dependency defects exposed by compilation,
and report the successful self-check. When either prerequisite is unavailable,
it SHALL report that the Markdown document was authored but not compiled and
state that an OpenCode-configured environment must run `opsx-plan compile`
before plan execution.

The adapter SHALL NOT imply that successful Markdown authoring is successful
TOML compilation.

#### Scenario: Claude-only authoring is not represented as compilation

- **WHEN** a Claude-only environment lacks `opsx-plan` or
  `OPSX_CONTROLLER_MODEL`
- **THEN** the agent reports the authored Markdown path and that compilation was
  not performed, including the OpenCode-backed prerequisite

#### Scenario: Available compiler self-check passes

- **WHEN** `opsx-plan` is on PATH and `OPSX_CONTROLLER_MODEL` is configured
- **THEN** the agent runs `opsx-plan compile` for the authored document and
  reports compilation only after that command succeeds

### Requirement: Claude authoring is packaged for adapter and plugin use

The standalone Claude adapter SHALL package the `opsx-plan` skill and
`opsx-plan-author` agent. The standalone Claude plugin SHALL package equivalent
artifacts and expose `/opsx-controller:opsx-plan <planning request>` with
namespaced agent delegation.

The existing generic Claude installer SHALL install the new skill through its
directory-copy behavior without feature-specific installer logic. Host-project,
repository, and plugin documentation SHALL state the authoring command, retain
`/opsx-drive <change-id>` for accepted single-change control, and document that
compilation currently requires OpenCode and `OPSX_CONTROLLER_MODEL`.

#### Scenario: Global and project installs include plan authoring

- **WHEN** the Claude adapter is installed globally or into a project
- **THEN** its installed skills include both `opsx-drive` and `opsx-plan`, and
  its installed agents include `opsx-plan-author`

#### Scenario: Plugin exposes the namespaced authoring command

- **WHEN** Claude Code loads `plugins/opsx-controller` through `--plugin-dir`
- **THEN** the plugin exposes `/opsx-controller:opsx-plan` and delegates its
  authoring request to the plugin-scoped `opsx-plan-author` agent

