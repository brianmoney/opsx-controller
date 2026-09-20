# opsx-controller core

Client-neutral OpenSpec controller contract for driving one accepted change
through implement, review, and archive rounds with durable state.

This directory documents the workflow semantics that adapters should preserve,
plus the shared plan-authoring reference for `opsx-plan compile`.

- `plan-authoring.md`: the single client-neutral reference for writing
  compilable markdown implementation plans
- `controller-contract.md`: lifecycle, phase order, and stop conditions
- `state-schema.md`: durable state expectations and resume behavior
- `phase-protocol.md`: input and output contracts for implement, review, and
  archive phases
- `plan-supervision.md`: client-neutral durable supervision contract — the
  supervisor ledger's record model, schema versioning, trusted-location rule,
  and journal-before-side-effects semantics

Current adapters:

- `adapters/opencode/`: OpenCode commands, agents, installer, and templates
- `adapters/claude-code/`: Claude Code skill, agents, installer, and templates
- `adapters/codex-cli/`: Codex CLI skill, agents, installer, and plugin bundle
- `adapters/dsh/`: dsh worker shim, role instruction files, installer, and templates
- `plugins/opsx-controller/`: Claude Code plugin package for namespaced distribution
- `skills/opsx-controller/`: Vercel `npx skill` package for discovery and guided use

## Upstream / Controller boundary

Upstream OpenSpec provides the per-change operations — propose, apply, archive,
and verify — through each client's upstream commands (`/opsx-apply`,
`/opsx:apply`, etc.). Those operations are how a single change is authored and
maintained; they are not a controller-owned per-change workflow.

The controller sits above OpenSpec and owns plan-level orchestration. `opsx-plan`
compiles a markdown plan into a TOML dependency DAG and sequences each change
through the direct implement-review-archive loop, persisting durable per-change
state and enforcing the strict review gate. Direct dispatch is the only plan-run
execution model: a plan missing any of `implement_invoke`, `review_invoke`, or
`archive_invoke` fails closed at load time naming the required keys. Plan
documents are authored against the shared `plan-authoring.md` reference above.

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
