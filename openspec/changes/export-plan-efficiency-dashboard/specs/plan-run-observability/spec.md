## ADDED Requirements

### Requirement: Dashboard command reads telemetry and state without mutation

The `opsx-plan dashboard` command SHALL read `.opsx-plan/telemetry/<plan_name>.jsonl` and `.opsx-plan/<plan_name>.state.json` via the aggregation API and SHALL NOT write to either file. The dashboard command SHALL NOT modify plan state, telemetry records, or any other file in the repository except the output HTML file.

#### Scenario: Dashboard runs without side effects on source files

- **WHEN** an operator runs `opsx-plan dashboard <plan>` against a completed plan's telemetry and state files
- **THEN** the telemetry JSONL file and state JSON file are unchanged

#### Scenario: Dashboard can run after plan is complete

- **WHEN** a plan run has finished and the operator has not modified any files
- **THEN** running `opsx-plan dashboard <plan>` twice with the same `--output` path produces byte-identical HTML files

### Requirement: Dashboard output is a self-contained static HTML file

The dashboard command SHALL emit a single self-contained HTML file. The HTML SHALL include all styles in one or more `<style>` blocks. The HTML SHALL NOT reference any external resources: no `<link rel="stylesheet">`, no `<script src="...">`, no remote URLs in `href` or `src` attributes. No JavaScript SHALL be included.

#### Scenario: HTML output contains no external references

- **WHEN** an operator runs `opsx-plan dashboard <plan>`
- **THEN** the output HTML file contains zero occurrences of `http://` or `https://` in attribute values

#### Scenario: All styles are inline

- **WHEN** the dashboard HTML is generated
- **THEN** all CSS rules reside inside `<style>` blocks within the `<head>` element

### Requirement: Dashboard output path is configurable

The dashboard command SHALL accept an `--output <path>` flag specifying the output file path. When `--output` is not provided, the default output path SHALL be `.opsx-plan/dashboards/<plan_name>.html`. The parent directory SHALL be created if it does not exist.

The output file SHALL be written atomically: first to a temporary file in the same directory, then renamed to the target path.

#### Scenario: Default output path

- **WHEN** an operator runs `opsx-plan dashboard my-plan` without `--output`
- **THEN** the HTML file is written to `.opsx-plan/dashboards/my-plan.html`

#### Scenario: Custom output path

- **WHEN** an operator runs `opsx-plan dashboard my-plan --output /tmp/dashboard.html`
- **THEN** the HTML file is written to `/tmp/dashboard.html`

#### Scenario: Output directory is created

- **WHEN** an operator runs `opsx-plan dashboard my-plan` and `.opsx-plan/dashboards/` does not exist
- **THEN** the directory is created and the file is written

### Requirement: Dashboard contains seven required sections

The dashboard HTML SHALL include the following seven sections in order:

1. **Plan Summary Header**: plan name, run id, total/completed/failed/blocked/incomplete change counts, completion rate (percentage), success rate (percentage), total duration (human-readable), total tokens (human-readable), total estimated cost (dollar-formatted or "unresolved"), and cost breakdown counts (estimated_cost_changes, unresolved_cost_changes, unknown_cost_changes).

2. **Model Leaderboard Table**: one row per model combination with columns for implementer model, reviewer model, archiver model, change count, success rate, first-pass rate, average rounds, average duration, average tokens, average cost. Rows SHALL be sorted by success_rate descending. The best value in each numeric column SHALL be highlighted.

3. **Per-Change Table**: one row per change with columns for change_id, status (color-coded badge), rounds, duration, tokens, cost, cost_status, first_pass, review_failures, and boolean flags (no_progress, max_rounds_exceeded, archive_failed, fast_check_failed).

4. **Failure Breakdown**: a list of failed changes with the reason for failure (max_rounds_exceeded, archive_failed, review_failure count). When no changes have failed, this section SHALL render a "No failures" message.

5. **Cost Breakdown**: a visual summary using CSS bars showing the proportion of changes with estimated, unresolved, and unknown costs. Counts for each category SHALL be displayed as labels.

6. **Rounds Histogram**: a CSS bar chart showing the distribution of round counts across completed changes. Round count labels on the X-axis, frequency count on bars. When no changes are completed, this section SHALL render an empty-state message.

7. **Stage Timeline**: a table of stage invocations sorted by `started_at` ascending, with columns for change_id, stage, round, started_at (ISO-8601 timestamp), duration (human-readable), and status (color-coded badge).

#### Scenario: Dashboard for a completed single-change plan contains all seven sections

- **WHEN** a plan has 1 completed change with implement, review (pass), and archive stages all having estimated costs
- **THEN** the dashboard HTML contains the plan summary header, model leaderboard (1 row), per-change table (1 row), failure breakdown ("No failures"), cost breakdown (1 estimated), rounds histogram (1 bar at round 1), and stage timeline (3 rows sorted by started_at)

#### Scenario: Dashboard for a failed plan shows failures

- **WHEN** a plan has 1 failed change due to max_rounds_exceeded with 3 rounds
- **THEN** the failure breakdown section lists the failed change with the max_rounds reason, and the rounds histogram shows the distribution from the failed change

### Requirement: Estimated cost is visually distinct from unresolved and unavailable cost

In all dashboard sections, estimated costs SHALL render as `$X.XX` in normal text. Unresolved costs SHALL render with an amber/orange visual treatment and an explicit `(unresolved)` text label. Unavailable costs (no usage data) SHALL render with a gray visual treatment and a `—` or `(no data)` label. Zero estimated cost SHALL render as `$0.00` (normal text), distinct from unresolved.

Stage status badges SHALL use color coding: green for `completed`, red for `failed`, orange for `timeout`, gray for all other statuses.

#### Scenario: Zero estimated cost is visually distinct from unresolved

- **WHEN** a completed change has a stage with `cost.status = "estimated"` and `cost.estimated_cost = 0.0`
- **THEN** the dashboard renders `$0.00` for that stage's cost cell using normal text styling, not amber/unresolved styling

#### Scenario: Unresolved cost is visually distinct from absent cost

- **WHEN** a completed change has a stage with `cost.status = "unresolved"` and another change with no usage data at all
- **THEN** the first renders with amber `(unresolved)` label and the second renders with gray `—` label

### Requirement: Dashboard respects --run-id and --change filters

The dashboard command SHALL support optional `--run-id` and `--change` filters:

- `--run-id <id>`: SHALL select telemetry records for the specified run id. Without `--run-id`, the command SHALL use the aggregator's default (latest run by `started_at`).
- `--change <id>`: SHALL narrow per-change table, failure breakdown, rounds histogram (if the change is completed), and stage timeline to the specified change. The plan summary header SHALL remain unfiltered. The model leaderboard SHALL be scoped to records involving the specified change.

When filters are active, the dashboard header SHALL include a `[Filtered: ...]` annotation listing active filter criteria.

#### Scenario: --run-id selects a specific run in multi-run telemetry

- **WHEN** a telemetry file contains records for run "run-a" and "run-b" and the operator passes `--run-id run-a`
- **THEN** the dashboard reflects only records with run_id "run-a"

#### Scenario: --change filter narrows dashboard sections

- **WHEN** `opsx-plan dashboard <plan> --change change-b` is run against a 3-change plan
- **THEN** the plan summary header shows total_changes=3, the per-change table shows only the row for change-b, and the stage timeline shows only stages for change-b

### Requirement: Dashboard handles missing telemetry gracefully

When telemetry records are missing or empty for a plan, the dashboard command SHALL still produce a valid HTML file. The plan summary header SHALL show the plan name and `run_id` derived from available data. Each metric section SHALL render an appropriate empty-state message (e.g., "No telemetry data available"). Aggregator warnings SHALL be rendered in a dedicated warnings section at the bottom of the dashboard.

#### Scenario: Empty telemetry produces a valid dashboard

- **WHEN** `opsx-plan dashboard <plan>` is run for a plan that has been created but never executed
- **THEN** the command produces a valid HTML file with the plan name in the header, "No telemetry data available" messages in each section, and aggregator warnings displayed

### Requirement: Dashboard output is deterministic

Given the same telemetry and state files, two invocations of `opsx-plan dashboard <plan>` with the same filter arguments SHALL produce byte-identical HTML output. The HTML SHALL NOT include any non-deterministic content such as generation timestamps, random element IDs, or process-specific data.

#### Scenario: Repeated dashboard generation produces identical output

- **WHEN** an operator runs `opsx-plan dashboard <plan>` twice against unchanged telemetry and state with the same arguments
- **THEN** both invocations produce byte-identical HTML files

### Requirement: Dashboard HTML is well-formed and valid

The generated HTML SHALL be well-formed XHTML/HTML5: begin with `<!DOCTYPE html>`, include `<html>`, `<head>`, and `<body>` elements, and contain properly closed tags. All user-provided text (plan names, change IDs, model identifiers, error messages) SHALL be HTML-escaped to prevent injection.

#### Scenario: Plan name with angle brackets is escaped

- **WHEN** a plan name contains `<script>alert(1)</script>`
- **THEN** the dashboard HTML renders the name as `&lt;script&gt;alert(1)&lt;/script&gt;` without executing it

### Requirement: Dashboard command handles aggregation errors gracefully

When the aggregator raises `AggregationError` (e.g., invalid repo root), the dashboard command SHALL print the error message to stderr and exit with code 2. When the aggregator succeeds but returns warnings, the dashboard SHALL include those warnings in a dedicated warnings section without affecting the exit code (exit 0).

#### Scenario: Aggregation failure produces error exit

- **WHEN** the aggregator raises `AggregationError`
- **THEN** the command prints the error message to stderr and exits with code 2

#### Scenario: Aggregation warnings are included in the dashboard

- **WHEN** the aggregator emits warnings about missing state file or status conflicts
- **THEN** the dashboard HTML includes a warnings section listing those warnings, and the command exits 0
