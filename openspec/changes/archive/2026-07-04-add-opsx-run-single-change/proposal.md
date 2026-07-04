## Why

Operators can run the strict implement-review-archive loop through `opsx-plan`, but today they must supply a plan manifest even when they only want to complete one existing OpenSpec change. This adds a lower-friction single-change runner while preserving the same deterministic state, review gate, and archive verification behavior.

## What Changes

- Add a no-manifest `opsx-run <change-id>` command for one accepted OpenSpec change.
- Reuse the existing direct OpenCode implement, review, retry, and archive orchestration rather than invoking `/opsx-drive` or duplicating controller logic.
- Allow the installed orchestrator script to expose both `opsx-plan` and `opsx-run` entrypoints.
- Document and test the single-change runner behavior, including review-failure retry and evidence-driven archive completion.

## Capabilities

### New Capabilities

- None.

### Modified Capabilities

- `plan-driven-opencode-execution`: Add single-change no-manifest execution as a supported OpenCode orchestration surface that reuses the plan-owned direct worker loop.

## Impact

- Affected code: `orchestrator/opsx-plan.py`, OpenCode installer script, orchestrator tests, and user-facing documentation.
- Affected commands: new `opsx-run <change-id>` executable alias and an equivalent `opsx-plan` subcommand for direct single-change execution.
- No dependency or persisted data migration is required; state remains under `.opsx-plan/`.
