## Why

Direct stage telemetry can already consume token usage from worker JSON and recognized log metadata, but OpenCode does not reliably expose complete stage usage through those paths. `opencode stats` is aggregate, OpenCode storage internals are not a stable integration surface, and log scraping cannot reliably join model usage back to a single `opsx-plan` stage.

An OpenCode plugin can observe model events while the stage runs and emit a dedicated sidecar for that exact stage. Before adding the plugin or orchestrator consumption, the sidecar shape and execution context need a stable contract so both later changes can be implemented against the same schema.

## What Changes

- Define the per-stage OpenCode usage sidecar as append-only JSONL written only when `OPSX_USAGE_PATH` is present.
- Define the `OPSX_*` environment variables that join plugin events back to `plan_name`, `run_id`, `change_id`, `stage`, and `round`.
- Define normalized usage fields, model identity fields, event types, final-vs-incremental semantics, and malformed-record handling.
- Define how sidecar-derived telemetry sets `usage.usage_source = "opencode_plugin"` and where it fits in existing usage precedence.
- Require missing, ambiguous, malformed, unreadable, or timed-out sidecar data to preserve unavailable usage instead of fabricating counts.

No OpenCode plugin implementation, `opsx-plan` subprocess changes, sidecar reader implementation, historical backfill, provider API calls, or dashboard/report changes are included.

## Capabilities

### Modified Capabilities

- `plan-run-observability`: Adds the OpenCode plugin sidecar usage contract and source-precedence requirements for future plugin-based usage telemetry.

### New Capabilities

- None.

## Impact

- Affected specs: `openspec/specs/plan-run-observability/spec.md` (modified by future archive; this change adds sidecar contract requirements).
- Affected code: None in this contract change.
- Affected tests: None in this contract change; later plugin and orchestrator changes should add fixture and orchestrator tests against this contract.
- Follow-up changes: `add-opencode-usage-emitter-plugin` and `consume-opencode-usage-sidecar`.
