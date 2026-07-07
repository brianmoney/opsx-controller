## Context

`aggregate-plan-efficiency-metrics` provides a typed `aggregate()` function in `lib.metrics.aggregator` that reads `.opsx-plan/telemetry/<plan_name>.jsonl` and `.opsx-plan/<plan_name>.state.json` and returns `AggregationResult` dataclasses containing plan metrics, per-change metrics, stage aggregates, and model-combination leaderboard entries.

This change adds the first presentation layer on top of that API: a `report` subcommand on `opsx-plan` that formats the aggregation results for human and machine consumption. The `opsx-plan` CLI already has subcommands (`run`, `status`, `approve`, `accept`, `reset`, `compile`, `run-one`); the report command follows the same pattern.

## Goals / Non-Goals

**Goals:**

- Add an `opsx-plan report <plan>` command that emits deterministic output from telemetry and state.
- Default to human-readable tables (plan summary, per-change table, stage aggregates, model leaderboard) using only stdlib string formatting.
- Support `--json` for a stable JSON output that downstream tools (including `export-plan-efficiency-dashboard`) can parse.
- Support optional filters: `--change` (single change id), `--run-id` (specific run in multi-run telemetry file), `--stage` (implement/review/archive), `--model` (substring match on `provider:model_id`).
- Identify unresolved costs and missing usage explicitly in both table and JSON output.
- Be read-only: never modify telemetry, state, or any other file.
- Handle empty telemetry, missing state, and failed/incomplete runs gracefully.

**Non-Goals:**

- Do not add an HTML dashboard, live server, graphical charts, or Rich/tabulate dependency.
- Do not modify the aggregation API, pricing catalog, or cost estimation.
- Do not add cross-plan aggregation.
- Do not change plan execution policy based on report output.
- Do not write report output to disk (output goes to stdout; operators can redirect).

## Decisions

### 1. Report command is an `opsx-plan` subcommand, not a separate script

The `report` command is registered as a subparser under the existing `opsx-plan` argparse tree, consistent with `status`, `compile`, etc. It accepts the plan TOML path as a positional argument (same as `status`).

**Rationale:** Operators already run `opsx-plan run <plan>` and `opsx-plan status <plan>`. Adding `opsx-plan report <plan>` keeps the discovery surface minimal and avoids a new top-level binary.

### 2. Default output is human-readable tables; `--json` for machine output

Default output prints four sections:
1. Plan summary (total changes, completion/success rate, total duration, estimated cost, cost breakdown counts)
2. Per-change table (change_id, status, rounds, duration, tokens, cost, cost_status, review failures, flags)
3. Stage aggregates (avg/median rounds, avg duration per stage, review failure rate, avg tokens/cost)
4. Model leaderboard (provider:model_id per role, change_count, success/first_pass rate, avg rounds/duration/tokens/cost)

`--json` emits a single JSON object with the same structure plus `warnings`.

**Rationale:** Tables are immediately useful for interactive inspection. JSON is the integration surface for dashboards and scripts. Keeping both paths avoids coupling the CLI rendering to downstream consumers.

### 3. Filters narrow per-change and leaderboard output; plan summary remains global

When `--change <id>` is provided, only that change's `ChangeMetrics` entry is shown and the leaderboard is filtered to entries involving that change. When `--stage <stage>` is provided, stage aggregates narrow to that stage. When `--model <substring>` is provided, leaderboard entries are filtered where any role's `provider:model_id` contains the substring.

Plan-level summary metrics are always shown in full (unfiltered) to provide context.

**Rationale:** Planning decisions need whole-run context even when inspecting a single change. Filtering the plan summary would obscure whether the selected change is representative.

### 4. Unresolved cost and missing usage are explicit, not silent

Table columns for cost show `"$0.05"` for estimated, `"unresolved"` for unresolved, `"unavailable"` for unavailable, and `"—"` for null. JSON fields preserve the `cost_status` and explicit `None` values from the aggregation API.

**Rationale:** Operators must be able to distinguish "this model costs $0.00" from "we don't know what this model costs" at a glance.

### 5. Use plain stdlib string formatting, not tabulate or rich

Tables use fixed-width columns with `str.ljust()`/`str.rjust()` and `print()`. This avoids adding any dependency and keeps output deterministic.

**Rationale:** `opsx-plan.py` already has a stdlib-only policy (Python 3.11+, no pip packages). Adding a formatting dependency for tables would break that contract.

### 6. Duration and token values are formatted with human-readable suffixes

Durations are displayed as `"1m30s"` or `"45s"`, not raw milliseconds. Token counts use `"1.2K"`, `"3.5M"` notation. Cost uses `"$1.23"`. Raw values are preserved in JSON mode.

**Rationale:** Raw millisecond/token values are hard to scan in a terminal.

### 7. JSON output is the serialized AggregationResult plus metadata

The JSON output is a single object:
```json
{
  "command": "opsx-plan report",
  "plan_name": "...",
  "run_id": "...",
  "filters": {"change": null, "run_id": null, "stage": null, "model": null},
  "plan_metrics": {...},
  "change_metrics": [...],
  "stage_aggregates": {...},
  "model_leaderboard": [...],
  "warnings": [...]
}
```

**Rationale:** Wrapping the aggregation result in a command envelope allows the downstream dashboard change to validate filter context and plan identity without re-scanning telemetry.

### 8. Error handling defers to the aggregator's warnings

If the aggregator returns warnings (missing telemetry, missing state, schema version differences), the report command includes them in the warnings section. The report command itself does not add additional validation beyond reporting aggregator warnings.

**Rationale:** The aggregator already encapsulates input quality checks. Duplicating them would create two places to maintain the same logic.

## Risks / Trade-offs

- [Risk] Human-readable table output may change formatting over time. -> Mitigation: the JSON output is the stable API contract; table output is documented as presentation-only and may evolve. Tests verify JSON structure stability.
- [Risk] Very long model identifiers or change IDs may overflow table columns. -> Mitigation: truncate identifiers to a reasonable width (e.g., 30 chars) with `"…"` suffix in table mode; full values always available in JSON mode.
- [Risk] Large plans with many changes produce very long table output. -> Mitigation: table output already groups by sections; operators can use `--change` to narrow output. Future work could add pagination.
- [Risk] Operators might expect live-refreshing output. -> Mitigation: the command explicitly reads from disk once; documentation states it is a snapshot at invocation time.

## Migration Plan

No migration is required. The report command reads existing telemetry and state files without modification. Plans run before this change produce telemetry that is fully compatible with the report command.

No existing commands or behaviors are modified.

## Open Questions

- Should `--model` filter support regex or just substring match? (Design proposes substring for simplicity; regex can be added later if operators request it.)
- Should the report command accept a plan name directly (without requiring the TOML path) when telemetry already exists? (Design requires the TOML for consistent plan config loading, matching the `status` command.)
