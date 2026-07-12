## Why

Claude Code users can drive a single accepted change with `/opsx-drive`, but
they do not have a supported Claude-native surface for authoring the phased
Markdown implementation plans consumed by `opsx-plan compile`. The existing
compiler invokes OpenCode with `OPSX_CONTROLLER_MODEL`, so Markdown authoring
must not be represented as successful TOML compilation in Claude-only
environments.

## What Changes

- Add a Claude Code `/opsx-plan <planning request>` skill that delegates one
  plan document to an explicitly named `opsx-plan-author` agent.
- Require the authoring agent to follow the existing machine-readable plan
  convention, inspect repository guidance, source material, capabilities, and
  active or archived change IDs, and refuse unintended overwrites.
- Run the compile self-check only when `opsx-plan` and
  `OPSX_CONTROLLER_MODEL` are available; otherwise report that Markdown was
  authored but not compiled and name the OpenCode requirement.
- Package matching authoring skill and agent artifacts in both the Claude
  adapter and standalone Claude plugin, without adding `/opsx-author` or
  changing the generic skill-copy installer.
- Document the Claude authoring command and the OpenCode-backed compilation
  limitation in the host-project snippet, repository README, and plugin README.

## Capabilities

### New Capabilities

- `claude-code-plan-authoring`: Defines Claude Code plan-document authoring,
  packaging, and truthful compile-status reporting.

### Modified Capabilities

- None.

## Impact

- Affected specs: new `openspec/specs/claude-code-plan-authoring/spec.md`.
- Affected adapter artifacts: Claude Code skill and dedicated authoring agent,
  plus their standalone plugin equivalents.
- Affected documentation: the Claude host-project snippet, repository README,
  and plugin README.
- No change to `opsx-plan compile`, its OpenCode invocation, or the
  single-change `/opsx-drive` controller.
- No installer logic change: the existing Claude installer already copies every
  skill directory and every agent Markdown file.
