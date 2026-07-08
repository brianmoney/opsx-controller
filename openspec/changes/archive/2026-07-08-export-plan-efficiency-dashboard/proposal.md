## Why

`add-opsx-plan-report-command` gives operators human-readable tables and machine-readable JSON for plan-run efficiency metrics, but comparing model combinations across runs still requires mentally scanning dense terminal output. A static HTML dashboard artifact makes side-by-side model comparison immediate and visual — leaders are obvious, failures jump out, and cost/reliability/speed tradeoffs are visible in a single glance.

## What Changes

- Add a `dashboard` subcommand to `orchestrator/opsx-plan.py` that reads `.opsx-plan/` telemetry and state via the existing `lib.metrics.aggregator.aggregate()` API and emits a self-contained static HTML file.
- The dashboard includes seven sections: plan summary header, model leaderboard table, per-change detail table, failure breakdown section, cost breakdown summary, rounds histogram, and stage timeline.
- The HTML is fully self-contained with inline CSS through a `<style>` block — zero external dependencies, no JavaScript required.
- Support an `--output` flag to write the HTML to a specific file path (default: `.opsx-plan/dashboards/<plan_name>.html`).
- Support optional `--run-id` and `--change` filters (same semantics as the report command) so operators can scope the dashboard to a specific run or change.
- Unknown usage and unresolved costs are visually distinct from zero values using color coding and explicit text labels.
- Add unit tests covering: HTML structure validation for a completed plan, stable/deterministic output, missing telemetry handling, unresolved cost rendering, `--run-id` scoping, and file output path behavior.

No changes to telemetry writing, cost estimation, state management, aggregation API, or control loop behavior.

## Capabilities

### Modified Capabilities

- `plan-run-observability`: Adds a static HTML dashboard export command that reads telemetry and state via the aggregation API and produces deterministic, self-contained HTML artifacts with visual model-combination comparison.

### New Capabilities

- None.

## Impact

- Affected specs: `openspec/specs/plan-run-observability/spec.md` (modified — defines dashboard command behavior, HTML structure, visual distinction rules, output path conventions, filter semantics).
- Modified files: `orchestrator/opsx-plan.py` (added `cmd_dashboard()` function and `dashboard` subparser).
- New test code: dashboard command tests in `tests/orchestrator/test_opsx_plan.py`.
- No new dependencies beyond stdlib and the existing `lib.metrics.aggregator` module.
- No runtime telemetry writes, state mutations, or plan execution changes.
