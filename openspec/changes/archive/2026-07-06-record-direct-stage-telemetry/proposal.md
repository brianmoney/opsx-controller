## Why

The telemetry schema is defined (`plan-run-observability`) but no runtime code writes telemetry records. Every direct implement, review, or archive stage invocation completes with logs on disk but produces no structured, machine-readable telemetry that downstream tools (cost estimation, metrics aggregation, dashboards) can consume. Without runtime writes, the telemetry contract is inert.

This change bridges the schema and the orchestrator by wiring telemetry recording into every direct stage dispatch in `opsx-plan.py`.

## What Changes

- Add a `record_direct_stage_telemetry()` function in `orchestrator/opsx-plan.py` that writes a single JSONL telemetry record conforming to the `plan-run-observability` schema for each direct stage invocation.
- Integrate the recording call into `run_direct_change()` so every implement, review, and archive dispatch produces exactly one telemetry entry capturing start/end timestamps, duration, outcome, log path, change id, stage, round, and best-effort model identity.
- Link the latest stage telemetry entry from the per-change worker state so downstream tools can discover telemetry without scanning the entire JSONL file.
- Add unit tests covering successful stages, timeouts, spawn errors, and invalid worker JSON output.
- Route records to `.opsx-plan/telemetry/<plan_name>.jsonl`, creating the directory on first write.

No token parsing, cost calculation, or dashboard rendering is included.

## Capabilities

### Modified Capabilities

- `plan-run-observability`: Implements runtime telemetry writing for direct stage invocations against the existing schema, including state linking and error-mode coverage.

### New Capabilities

- None.

## Impact

- Affected specs: `openspec/specs/plan-run-observability/spec.md` (modified — adds runtime telemetry writing requirements, state-linking requirement, and error-mode scenarios).
- A new `.opsx-plan/telemetry/` directory is created on first telemetry write (already scoped in the schema requirement).
- No changes to the `opsx-plan` control loop semantics — the implement-review-archive cycle, resume behavior, and state persistence are unchanged.
- Downstream changes (`capture-worker-usage-metadata`, `add-model-pricing-catalog`, etc.) will populate `usage` and `cost` fields in records written by this change.
