## Why

The first-run workflow for a Claude Code user fails before they can compile or
run a plan: its installer does not install the client-neutral executables, and
`compile` always invokes OpenCode. This contradicts the controller's portable
adapter contract at the product's most visible entry point.

## What Changes

- Install the shared `opsx-plan` and `opsx-run` runtime from every global
  adapter installer.
- Add adapter selection to `opsx-plan compile`, with OpenCode retained as the
  compatibility default and Claude Code supported as a compile client.
- Resolve the compile controller model, prompt defaults, client invocation,
  diagnostics, and errors against the selected adapter.
- Preserve fail-closed TOML extraction, with narrowly defined Claude-specific
  handling only when its known output envelope requires it.
- Add plan-less adapter selection to `opsx-plan doctor` so operators can check
  compile prerequisites before a manifest exists.
- Explicitly reject Codex CLI compilation for this launch while still
  installing the shared executables through its installer.
- Update quick starts and Claude plan-authoring guidance to use the selected
  compile adapter rather than requiring OpenCode globally.

## Capabilities

### New Capabilities
- `adapter-aware-plan-compilation`: Compile Markdown plans using a selected
  supported adapter, validate the resulting manifest, and expose accurate
  diagnostics for unsupported adapters.
- `shared-orchestrator-installation`: Make the common orchestrator executables
  and their Python runtime available after any global adapter installation.

### Modified Capabilities
- `plan-driven-opencode-execution`: Replace its OpenCode-only compilation
  requirements with the adapter-aware compilation contract while retaining
  OpenCode as the default compile adapter.
- `plan-operator-cli`: Allow doctor to select a target adapter without a plan
  so it can preflight compilation prerequisites.
- `claude-code-plan-authoring`: Change Claude plan-authoring self-checks from
  an OpenCode prerequisite to Claude-selected compilation.

## Impact

- `orchestrator/opsx-plan.py` CLI parsing, compile helpers, prompt generation,
  doctor selection, and diagnostics.
- `adapters/*/install.sh` plus a new shared installer helper.
- Orchestrator unit tests and installer verification coverage.
- README, adapter, operator-workflow, plugin, and maintainer deployment
  documentation.
