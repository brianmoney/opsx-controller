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

Before writing a document, the authoring agent SHALL read the installed
`plan-authoring.md` reference from the project controller support directory,
falling back to the global controller support directory, in addition to
available `CLAUDE.md` and `AGENTS.md` guidance, source material referenced by
the request, existing capabilities, and active and archived OpenSpec change
IDs.

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

### Requirement: Claude authoring reports compilation truthfully

The authoring agent SHALL run an `opsx-plan compile --adapter claude-code`
self-check only when `opsx-plan` is available on PATH and a `controller` model
resolves for the `claude-code` adapter.

When those prerequisites are available, the agent SHALL compile the authored
document, correct plan structure or dependency defects exposed by compilation,
and report the successful self-check. When either prerequisite is unavailable,
it SHALL report that the Markdown document was authored but not compiled and
state the missing Claude Code prerequisite.

The adapter SHALL NOT imply that successful Markdown authoring is successful
TOML compilation.

#### Scenario: Claude-only authoring is not represented as compilation

- **WHEN** a Claude-only environment lacks `opsx-plan` or a Claude Code
  controller model
- **THEN** the agent reports the authored Markdown path and that compilation was
  not performed, including the missing Claude-selected prerequisite

#### Scenario: Available compiler self-check passes

- **WHEN** `opsx-plan` is on PATH and a Claude Code controller model is configured
- **THEN** the agent runs `opsx-plan compile --adapter claude-code` for the authored document and
  reports compilation only after that command succeeds

### Requirement: Claude authoring is packaged for adapter and plugin use

The standalone Claude adapter SHALL package the `opsx-plan` skill and
`opsx-plan-author` agent. The standalone Claude plugin SHALL package equivalent
artifacts and expose `/opsx-controller:opsx-plan <planning request>` with
namespaced agent delegation. These surfaces SHALL defer plan conventions to
the installed shared reference and SHALL direct per-change propose, apply,
archive, and verify work to upstream OpenSpec. They SHALL use `opsx-run` only
for the supported manual single-change loop and SHALL not teach the deleted
`opsx-drive` workflow.

#### Scenario: Plugin exposes the namespaced authoring command

- **WHEN** Claude Code loads `plugins/opsx-controller` through `--plugin-dir`
- **THEN** the plugin exposes `/opsx-controller:opsx-plan` and delegates its
  authoring request to the plugin-scoped `opsx-plan-author` agent

#### Scenario: Guidance defers per-change work upstream

- **WHEN** an operator asks a Claude plan-authoring surface how to create or
  execute an individual change
- **THEN** it points to upstream OpenSpec per-change commands and reserves
  controller commands for plan compile, plan run, and the supported `opsx-run`
  manual loop

#### Scenario: Global Claude install includes orchestrator commands

- **WHEN** an operator installs the Claude adapter globally
- **THEN** `opsx-plan` and `opsx-run` are available from the shared user-level
  executable location
