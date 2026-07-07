## ADDED Requirements

### Requirement: Report command reads telemetry and state without mutation

The `opsx-plan report` command SHALL read `.opsx-plan/telemetry/<plan_name>.jsonl` and `.opsx-plan/<plan_name>.state.json` via the aggregation API and SHALL NOT write to either file. The report command SHALL NOT modify plan state, telemetry records, or any other file in the repository.

#### Scenario: Report runs without side effects

- **WHEN** an operator runs `opsx-plan report <plan>` against a completed plan's telemetry and state files
- **THEN** the telemetry JSONL file and state JSON file are unchanged, and report output is emitted to stdout

#### Scenario: Report can run after plan is complete

- **WHEN** a plan run has finished and the operator has not modified any files
- **THEN** running `opsx-plan report <plan>` twice produces identical output for both table and JSON modes

### Requirement: Default output is human-readable tables

When the `--json` flag is not set, the report command SHALL emit four human-readable table sections in order:

1. **Plan summary** SHALL include: plan name, run id, total changes count, completed/failed/blocked/incomplete counts, completion rate (percentage), success rate (percentage), total duration (human-readable), total tokens (human-readable), total estimated cost (dollar-formatted or "unresolved"), and cost breakdown counts (estimated_cost_changes, unresolved_cost_changes, unknown_cost_changes).

2. **Per-change table** SHALL include one row per change with columns: change_id, status, rounds, duration (human-readable), tokens (human-readable), cost (dollar-formatted, "unresolved", "unavailable", or "—"), cost_status, first_pass (yes/no/—), review_failures count, and boolean flags (no_progress, max_rounds, archive_failed, fast_check_failed).

3. **Stage aggregates** SHALL include: average rounds, median rounds, average implement duration, average review duration, average archive duration, review failure rate, average tokens per change, average cost per change. All absent values SHALL render as "—".

4. **Model leaderboard** SHALL include one row per model combination with columns: implementer model, reviewer model, archiver model, change count, success rate, first-pass rate, average rounds, average duration, average tokens, average cost.

#### Scenario: Table output for a completed single-change plan

- **WHEN** a plan has 1 completed change with implement (openai:gpt-4o, 5000 tokens, $0.05), review (pass, anthropic:claude-sonnet, 2000 tokens, $0.03), and archive (openai:gpt-4o-mini, 1000 tokens, $0.0002)
- **THEN** the plan summary shows total_changes=1, completed=1, estimated_cost_changes=1; the per-change table shows first_pass=yes, review_failures=0; the model leaderboard shows the full triple combination with change_count=1

#### Scenario: Table output identifies unresolved costs

- **WHEN** a completed change has estimated implement cost ($0.05) but unresolved review and archive costs
- **THEN** the per-change row shows cost_status as "partial" and the cost column shows "$0.05" (not summed with unresolved); the model leaderboard entry shows average_cost computed from only the stages with estimated cost

#### Scenario: Table output for empty telemetry

- **WHEN** no telemetry records exist for the plan
- **THEN** the plan summary shows total_changes from state, all completed/failed counts as 0, and the per-change table shows all changes as "incomplete" with "—" for all metric columns; warnings are displayed

### Requirement: JSON output is stable and complete

When the `--json` flag is set, the report command SHALL emit a single JSON object to stdout with the following top-level fields:

- `command`: the string `"opsx-plan report"`.
- `plan_name`: the plan name string.
- `run_id`: the selected run id string.
- `filters`: an object with keys `change`, `run_id`, `stage`, `model` and their applied values (or `null` when not set).
- `plan_metrics`: an object matching the `PlanMetrics` dataclass fields.
- `change_metrics`: an array of objects, each matching the `ChangeMetrics` dataclass fields.
- `stage_aggregates`: an object matching the `StageAggregates` dataclass fields.
- `model_leaderboard`: an array of objects, each matching the `ModelLeaderboardEntry` dataclass fields.
- `warnings`: an array of warning strings from the aggregator.

All `None` values in dataclass fields SHALL serialize as JSON `null`. Numeric values SHALL preserve their full precision. The JSON output SHALL be deterministic: repeated runs against the same telemetry and state SHALL produce byte-identical JSON.

#### Scenario: JSON output stability across two runs

- **WHEN** an operator runs `opsx-plan report <plan> --json` twice against unchanged telemetry and state
- **THEN** both invocations produce byte-identical JSON output

#### Scenario: JSON output with None values

- **WHEN** a plan has no completed changes (all fields like completion_rate, total_duration_ms, total_estimated_cost are `None`)
- **THEN** the JSON output contains `"completion_rate": null`, `"total_duration_ms": null`, `"total_estimated_cost": null`

#### Scenario: JSON output includes warnings from aggregator

- **WHEN** the aggregator emits warnings about missing state file or telemetry/state status conflicts
- **THEN** the `warnings` array in the JSON output contains those warning strings

### Requirement: Filters narrow displayed data without affecting plan summary

The report command SHALL support the following optional filters:

- `--change <id>`: SHALL narrow `change_metrics` to the single matching change. The per-change table and model leaderboard SHALL be filtered to data involving that change. The plan summary SHALL remain unfiltered (showing totals for all changes in the plan).
- `--run-id <id>`: SHALL select telemetry records for the specified run id. Without `--run-id`, the command SHALL use the aggregator's default (latest run by `started_at`).
- `--stage <stage>`: SHALL narrow stage aggregates display and leaderboard to records matching the specified stage (`"implement"`, `"review"`, or `"archive"`). Invalid stage values SHALL produce an error.
- `--model <substring>`: SHALL filter the model leaderboard to entries where any role's model identifier contains the substring (case-insensitive match). Per-change output SHALL NOT be filtered by model.

Filters SHALL be reflected in the `filters` field of JSON output. Table output SHALL indicate active filters in a header line.

#### Scenario: --change filter narrows output

- **WHEN** `opsx-plan report <plan> --change change-b` is run against a 3-change plan
- **THEN** the plan summary shows total_changes=3, the per-change table shows only the row for change-b, and the leaderboard is filtered to the model combination of change-b

#### Scenario: --run-id selects a specific run

- **WHEN** a telemetry file contains records for runs "run-a" and "run-b" and the operator passes `--run-id run-a`
- **THEN** the report output reflects only records with run_id "run-a"

#### Scenario: --model filter matches substring

- **WHEN** the operator passes `--model gpt-4o` and the leaderboard contains entries with `implementer_model = "openai:gpt-4o"` and `archiver_model = "openai:gpt-4o-mini"`
- **THEN** both entries appear in the filtered leaderboard

#### Scenario: Invalid --stage value produces an error

- **WHEN** the operator passes `--stage invalid`
- **THEN** the command exits with code 2 and an error message listing valid stage values

### Requirement: Cost and usage status are explicitly identified in all output modes

Table output SHALL render:
- Estimated cost as `"$X.XX"` (e.g., `"$0.05"`, `"$0.00"`).
- Unresolved cost as the string `"unresolved"`.
- Unavailable cost (no usage data) as the string `"unavailable"`.
- Absent cost/token/duration values as `"—"`.

JSON output SHALL preserve the aggregation API's `cost_status` field (`"estimated"`, `"partial"`, `"unresolved"`, `"unavailable"`) and `None` values for absent fields.

Zero estimated cost (`0.0`) SHALL render as `"$0.00"` in table mode and `0.0` in JSON mode, distinct from `"unresolved"` or `null`.

#### Scenario: Zero estimated cost is visually distinct from unresolved

- **WHEN** a completed change has a stage with `cost.status = "estimated"` and `cost.estimated_cost = 0.0`
- **THEN** the table output shows `"$0.00"` for that stage's cost column, not `"unresolved"`

#### Scenario: Unresolved cost is visually distinct from absent cost

- **WHEN** a completed change has a stage with `cost.status = "unresolved"` and another change with no usage data at all
- **THEN** the first renders as `"unresolved"` and the second renders as `"—"` in table mode

### Requirement: Duration and token values use human-readable formatting in table mode

Duration values in table mode SHALL be formatted as `"XmYs"` (e.g., `"1m30s"`, `"0m45s"`, `"2m0s"`) derived from milliseconds. Token values in table mode SHALL use suffix notation: values >= 1,000,000 as `"X.XM"`, values >= 1,000 as `"X.XK"`, otherwise as raw integers. JSON mode SHALL use raw numeric values (milliseconds, integer token counts, float costs) without formatting.

#### Scenario: Duration formatting

- **WHEN** a stage record has `duration_ms = 90000` (90 seconds)
- **THEN** the table output shows `"1m30s"` and the JSON output shows `90000`

#### Scenario: Token formatting

- **WHEN** a change has `tokens = 1500000`
- **THEN** the table output shows `"1.5M"` and the JSON output shows `1500000`

#### Scenario: Long identifiers are truncated in table mode

- **WHEN** a model identifier is longer than 30 characters
- **THEN** the table output truncates it to 27 characters followed by `"…"` and the JSON output preserves the full identifier

### Requirement: Report command handles errors and edge cases gracefully

The report command SHALL handle the following edge cases without crashing:

- Missing telemetry file: display plan summary with zero telemetry-derived metrics, show aggregator warnings, exit code 0.
- Missing state file: display what is available from telemetry, show aggregator warnings, exit code 0.
- Corrupt telemetry line: aggregator handles this; report command displays the aggregator warning.
- Aggregation error (e.g., invalid repo root): print error message, exit code 2.

#### Scenario: Missing telemetry file produces graceful output

- **WHEN** `opsx-plan report <plan>` is run for a plan that has never been executed
- **THEN** the command prints a warning that no telemetry was found and exits 0

#### Scenario: Aggregation failure produces error exit

- **WHEN** the aggregator raises `AggregationError`
- **THEN** the command prints the error message to stderr and exits with code 2
