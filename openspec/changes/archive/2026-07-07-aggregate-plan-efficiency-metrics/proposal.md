## Why

Direct stage telemetry now records raw invocation records with token usage, model identity, and cost estimates. The missing piece is converting those raw records into comparable efficiency metrics at the plan, change, stage, and model-combination levels. Without aggregation, operators must manually calculate completion rates, first-pass review rates, token-per-change averages, and cost-per-change estimates.

This change bridges raw telemetry and reporting by providing a deterministic metrics aggregator that reads `.opsx-plan/` telemetry and state files, then computes normalized efficiency KPIs that downstream report and dashboard changes can consume.

## What Changes

- Add a metrics aggregation module (`lib/metrics/`) that reads plan-scoped telemetry JSONL files and plan state to compute normalized efficiency KPIs.
- Compute plan-level metrics: completion rate, change success rate, total duration, total tokens, total estimated cost.
- Compute change-level metrics: success/failure/blocked status, rounds consumed, duration, tokens, estimated cost, first-pass review rate, review failure count, no-progress and max-rounds flags, archive and fast-check failure flags.
- Compute stage-level aggregates: average/median rounds per change, average/median duration per stage type, review failure rate.
- Compute model-combination leaderboard entries: by implementer model, reviewer model, archiver model, and the full triple combination — including completion count, success rate, first-pass rate, average rounds, average duration, average tokens, and average estimated cost.
- Distinguish unknown cost from zero cost and from estimated cost in all aggregates; never fabricate zero cost for unresolved records.
- Add unit tests covering: successful change, failed change, blocked change, incomplete plan, multi-round change, unknown cost, mixed estimated/unresolved costs, and multiple runs in a single telemetry file.

No report rendering, dashboard export, CLI command, or plan execution policy changes are included.

## Capabilities

### Modified Capabilities

- `plan-run-observability`: Adds deterministic aggregation of stage telemetry into plan, change, stage, and model-combination efficiency metrics.

### New Capabilities

- None.

## Impact

- Affected specs: `openspec/specs/plan-run-observability/spec.md` (modified — defines aggregation contract, metric names, null-vs-zero cost handling, and model-combination leaderboard shape).
- New files: `lib/metrics/__init__.py`, `lib/metrics/aggregator.py` (aggregation logic and data types).
- New test files: `tests/lib/metrics/` directory with unit tests for aggregation.
- Downstream changes `add-opsx-plan-report-command` and `export-plan-efficiency-dashboard` will depend on the aggregation output types defined here.
- No runtime telemetry writes, cost calculations, or control-loop behavior are modified.
