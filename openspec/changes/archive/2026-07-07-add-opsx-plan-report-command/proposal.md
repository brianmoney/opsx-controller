## Why

`aggregate-plan-efficiency-metrics` provides typed aggregation dataclasses from telemetry and state, but operators have no way to inspect results without writing custom scripts. A deterministic CLI report command makes plan efficiency metrics instantly accessible from the same tool that runs plans, removing the friction between running a plan and understanding its model-efficiency characteristics.

This change adds the `opsx-plan report` command as the first presentation layer on top of the aggregation API, supporting both human-readable tables and machine-readable JSON to enable both interactive inspection and downstream dashboard consumption.

## What Changes

- Add a `report` subcommand to `orchestrator/opsx-plan.py` that reads `.opsx-plan/` telemetry and state via the existing `lib.metrics.aggregator.aggregate()` API.
- Default output: human-readable tables showing plan summary, per-change breakdown, stage aggregates, and model leaderboard, using only stdlib formatting.
- `--json` flag: emit a stable JSON structure with the full `AggregationResult` (plan metrics, per-change list, stage aggregates, model leaderboard, warnings) as a single JSON object.
- Support optional filters: `--change` (single change id), `--run-id` (specific run), `--stage` (implement/review/archive), `--model` (provider:model_id substring match).
- Filters narrow the output to matching data; plan-level totals remain unfiltered for context.
- Report identifies unresolved costs and missing usage explicitly in table columns and JSON fields.
- Add unit tests covering: table output for a completed plan, JSON output stability, filter behavior, empty telemetry, missing cost data rendering, and multi-run telemetry with --run-id selection.

No changes to telemetry writing, cost estimation, state management, or control loop behavior.

## Capabilities

### Modified Capabilities

- `plan-run-observability`: Adds a deterministic CLI report command that reads telemetry and state via the aggregation API and emits human-readable tables and machine-readable JSON with filter support.

### New Capabilities

- None.

## Impact

- Affected specs: `openspec/specs/plan-run-observability/spec.md` (modified — defines report command behavior, output stability, filter semantics, unresolved cost rendering).
- Modified files: `orchestrator/opsx-plan.py` (added `cmd_report()` function and `report` subparser).
- New test code: report command tests in `tests/orchestrator/test_opsx_plan.py`.
- Downstream change: `export-plan-efficiency-dashboard` depends on the JSON output stability defined here.
- No new dependencies beyond stdlib and the existing `lib.metrics.aggregator` module.
- No runtime telemetry writes, state mutations, or plan execution changes.
