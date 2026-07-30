# opsx-controller core

Client-neutral OpenSpec controller contract for driving one accepted change
through implement, review, and archive rounds with durable state.

This directory documents the workflow semantics that adapters should preserve.

- `controller-contract.md`: lifecycle, phase order, and stop conditions
- `state-schema.md`: durable state expectations and resume behavior
- `phase-protocol.md`: input and output contracts for implement, review, and
  archive phases

Current adapters:

- `adapters/opencode/`: OpenCode commands, agents, installer, and templates
- `adapters/claude-code/`: Claude Code skill, phase agents, installer, and templates
- `plugins/opsx-controller/`: Claude Code plugin package for namespaced distribution
- `skills/opsx-controller/`: Vercel `npx skill` package for discovery and guided use

## Operator Workflow

- `opsx-watch-plan`: live stage-log follower installed alongside `opsx-plan` and
  `opsx-run`. Run it from a repository root to follow the newest direct-stage
  log under `.opsx-plan/logs/`. Each log begins with a comment-prefixed
  `OPSX WORKER INPUT` block showing the exact dispatched fields including
  corrective handoffs (`LATEST_FIX_PROMPT`). The watcher automatically
  switches to a newer log when a new stage begins.
- Direct-stage logs are written under `.opsx-plan/logs/<change>.<stage>.r<round>.<n>.log`.
  The comment-prefixed metadata (lines starting with `# `) is excluded from
  JSON result parsing and failure-marker detection so it never interferes with
  controller state transitions.

## Model Efficiency Workflow

- `model-efficiency-workflow.md`: end-to-end operator workflow for benchmarking
  OPSX model choices using plan-run telemetry, cost estimation, reporting, and
  dashboards. Covers configuring model sets, running comparable plans,
  maintaining the pricing catalog, interpreting cost estimates, comparing model
  combinations, and known limitations.
