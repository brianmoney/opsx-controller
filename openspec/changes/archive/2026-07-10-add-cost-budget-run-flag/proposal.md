## Why

`opsx-plan` already supports a wall-clock run budget, but unattended operators may care more about estimated spend than elapsed minutes. Phase 3 of the operator workflow upgrades plan calls for a spend cap that uses the telemetry cost estimates already recorded for direct stage runs, while preserving resumability and never interrupting a stage already in flight.

This change extends the `plan-operator-cli` capability with a cost-budgeted run control for `opsx-plan run`.

## What Changes

- Add a `--budget-usd <amount>` flag to `opsx-plan run`.
- Accumulate estimated stage costs from the current run's telemetry records and stop dispatching new stages once the configured budget cap is reached.
- Mirror existing `--budget-minutes` stop semantics: never kill a stage in flight, stop cleanly between dispatches, and leave state resumable.
- Track and report both the number of stages with resolved estimated cost and the number of stages whose cost remained unresolved when a budget stop occurs.
- Preserve existing behavior when `--budget-usd` is omitted.
- Add unit tests for cap reached, cap not reached, and mixed resolved/unresolved cost telemetry.

## Capabilities

### New Capabilities

- None.

### Modified Capabilities

- `plan-operator-cli`: Adds a spend-based stop control for `opsx-plan run` using run telemetry cost estimates.

## Impact

- Affected specs: `openspec/specs/plan-operator-cli/spec.md`.
- Modified files later: `orchestrator/opsx-plan.py` run-loop budget checks, telemetry cost accumulation, and stop reporting.
- Runtime behavior later: `opsx-plan run` can stop between stages when cumulative estimated spend reaches a configured USD cap.
- Test coverage later: orchestrator unit tests for budget stop thresholds, unchanged runs without the flag, and mixed resolved/unresolved cost records.
- No provider billing reconciliation, no per-change spend limits, and no hard interruption of already-running workers.
