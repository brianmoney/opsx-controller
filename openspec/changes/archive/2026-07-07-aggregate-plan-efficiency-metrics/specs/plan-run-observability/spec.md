## ADDED Requirements

### Requirement: Metrics aggregator reads telemetry and state without mutation

A metrics aggregation module SHALL read `.opsx-plan/telemetry/<plan_name>.jsonl` and `.opsx-plan/<plan_name>.state.json` as input and SHALL NOT write to either file. The aggregator SHALL return typed aggregation results (plan metrics, per-change metrics, stage aggregates, and model-combination leaderboard entries) without modifying telemetry records, state files, or the plan execution environment.

#### Scenario: Aggregator runs without side effects

- **WHEN** an operator runs the aggregator against a completed plan's telemetry and state files
- **THEN** the telemetry JSONL file and state JSON file are unchanged, and aggregation results are returned as typed objects

#### Scenario: Aggregator can re-run deterministically

- **WHEN** the aggregator is run twice against the same telemetry and state files
- **THEN** both runs produce identical aggregation results

### Requirement: Aggregator groups records by run_id

The aggregator SHALL group telemetry records by `run_id`. When no explicit `run_id` is requested, the aggregator SHALL default to the `run_id` with the latest `started_at` timestamp across all records. The aggregator SHALL support an explicit `run_id` parameter to aggregate a specific run.

#### Scenario: Default to latest run in multi-run file

- **WHEN** a telemetry JSONL file contains records from two runs with different `run_id` values
- **THEN** the aggregator selects the run with the most recent `started_at` and returns metrics for that run only, and the `run_id` field in the result matches the selected run

#### Scenario: Explicit run_id selects a specific run

- **WHEN** the aggregator is invoked with a specific `run_id` parameter matching an earlier run
- **THEN** the aggregator returns metrics for only that run, ignoring records from the later run

### Requirement: Plan-level metrics include completion and cost breakdown

Plan-level aggregation SHALL compute the following metrics from telemetry and state:

- `total_changes`: count of all changes in the plan manifest.
- `completed_changes`: changes where plan state marks them done.
- `failed_changes`: changes that failed (including max-rounds exceeded).
- `blocked_changes`: changes explicitly marked blocked in plan state.
- `incomplete_changes`: remaining changes not yet finished.
- `completion_rate`: `completed_changes / total_changes`, or `None` when `total_changes = 0`.
- `success_rate`: `completed_changes / (completed_changes + failed_changes)`, or `None` when the denominator is zero.
- `total_duration_ms`: sum of `duration_ms` for all completed stage records.
- `total_tokens`: sum of `usage.total_tokens` for records where `usage.total_tokens` is non-null.
- `total_estimated_cost`: sum of `cost.estimated_cost` for records where `cost.status = "estimated"`.
- Cost breakdown counts: `estimated_cost_changes`, `unresolved_cost_changes`, `unknown_cost_changes`.

#### Scenario: Plan metrics for a completed 2-change run

- **WHEN** a plan has 2 changes, both with telemetry showing completed implement, review (pass), and archive stages with estimated costs
- **THEN** `total_changes = 2`, `completed_changes = 2`, `failed_changes = 0`, `blocked_changes = 0`, `incomplete_changes = 0`, `completion_rate = 1.0`, `success_rate = 1.0`, `total_estimated_cost` equals the sum of stage costs, `estimated_cost_changes = 2`, `unresolved_cost_changes = 0`

#### Scenario: Plan metrics for a partially complete run

- **WHEN** a plan has 3 changes, 1 completed with estimated cost, 1 failed (max rounds), 1 still in progress
- **THEN** `total_changes = 3`, `completed_changes = 1`, `failed_changes = 1`, `incomplete_changes = 1`, `completion_rate ≈ 0.333`, `success_rate = 0.5`, `estimated_cost_changes = 1`

### Requirement: Change-level metrics derive status from state and telemetry together

Per-change aggregation SHALL derive change status from plan state (authoritative for "is this change done?") combined with telemetry (authoritative for stage outcomes). Change status SHALL be one of:

- `"completed"`: plan state marks the change done.
- `"failed"`: plan state marks it done and telemetry shows non-passing outcomes.
- `"blocked"`: plan state marks it blocked.
- `"incomplete"`: plan state does not mark it done.

Per-change metrics SHALL include: `change_id`, `status`, `total_rounds`, `duration_ms`, `tokens`, `estimated_cost`, `cost_status`, `first_pass_review`, `review_failures`, `no_progress`, `max_rounds_exceeded`, `archive_failed`, `fast_check_failed`.

#### Scenario: Completed change with first-pass review

- **WHEN** a change has one implement stage (status completed), one review stage (verdict "pass"), and one archive stage (status completed), all with model identity and estimated cost
- **THEN** `status = "completed"`, `total_rounds = 1`, `first_pass_review = true`, `review_failures = 0`, `no_progress = false`, `max_rounds_exceeded = false`, `archive_failed = false`

#### Scenario: Failed change with max rounds

- **WHEN** a change cycled through implement-review 3 times (configured max rounds = 3) and the last review still returned "fail"
- **THEN** `status = "failed"`, `total_rounds = 3`, `first_pass_review = false`, `review_failures = 3`, `no_progress = false`, `max_rounds_exceeded = true`

#### Scenario: Blocked change with no recent telemetry

- **WHEN** plan state marks a change blocked and no telemetry records exist for the most recent round
- **THEN** `status = "blocked"`, `total_rounds` equals the last recorded round from state, telemetry-derived fields are `null` where unavailable

### Requirement: Cost aggregates separate estimated from unresolved and unknown costs

Every cost aggregate SHALL separate records with `cost.status = "estimated"` from those with `"unresolved"` or `"unavailable"`. The aggregator SHALL also separate records where model identity is unknown (`model.provider` is `null` or `model.model_id` is `null`) from records where model identity is known but pricing is unresolved.

Zero estimated cost (`estimated_cost = 0.0`) is distinct from absent cost. Zero-cost records SHALL contribute to cost sums and averages. Records with unresolved or unavailable cost status SHALL NOT contribute to cost sums or averages.

Averages SHALL use only the count of records with estimated cost as the denominator. When no records have estimated cost, the average SHALL be `None`.

#### Scenario: Mixed estimated and unresolved costs

- **WHEN** a plan has 3 completed changes: change A has estimated cost $1.00, change B has estimated cost $2.00, change C has all costs unresolved
- **THEN** `estimated_cost_changes = 2`, `unresolved_cost_changes = 1`, `total_estimated_cost = 3.0`, `average_cost_per_change = 1.5` (2/2, not 3/3)

#### Scenario: All costs unresolved

- **WHEN** a completed run has telemetry but no records have `cost.status = "estimated"`
- **THEN** `total_estimated_cost = None`, `average_cost_per_change = None`, `estimated_cost_changes = 0`

#### Scenario: Zero estimated cost is included

- **WHEN** a completed change has a stage with `cost.status = "estimated"` and `cost.estimated_cost = 0.0`
- **THEN** that stage contributes `0.0` to `total_estimated_cost` and the change counts toward `estimated_cost_changes`

### Requirement: Model-combination leaderboard groups by stage role

The aggregator SHALL produce model-combination leaderboard entries grouped by:

- Implementer model: `model.provider` + `model.model_id` from records where `stage = "implement"`.
- Reviewer model: `model.provider` + `model.model_id` from records where `stage = "review"`.
- Archiver model: `model.provider` + `model.model_id` from records where `stage = "archive"`.
- Full combination: the triple `(implementer_model, reviewer_model, archiver_model)` derived from each change's latest implement, review, and archive records.

Each leaderboard entry SHALL include: the model identifier(s), `change_count`, `success_rate`, `first_pass_rate`, `average_rounds`, `average_duration_ms`, `average_tokens`, `average_cost`.

For the full-combination leaderboard, only completed changes with all three model identities known SHALL be included. Changes with unknown model identity for any role SHALL be excluded from the full-combination leaderboard but included in per-role leaderboards where the specific role's identity is known.

#### Scenario: Full-combination leaderboard entry

- **WHEN** 3 completed changes all used `openai:gpt-4o` for implement, `anthropic:claude-sonnet` for review, `openai:gpt-4o-mini` for archive, with estimated costs
- **THEN** the full-combination leaderboard contains one entry with `implementer_model = "openai:gpt-4o"`, `reviewer_model = "anthropic:claude-sonnet"`, `archiver_model = "openai:gpt-4o-mini"`, `change_count = 3`, and all rate/average fields computed from those 3 changes

#### Scenario: Unknown model excluded from full combination

- **WHEN** a completed change has known implementer and reviewer models but unknown archiver model
- **THEN** the change is excluded from the full-combination leaderboard but included in the per-role implementer and reviewer leaderboards

#### Scenario: Per-role leaderboard with partial model identity

- **WHEN** a completed change has `openai:gpt-4o` for implement but unknown reviewer and archiver models
- **THEN** the change contributes to the implementer leaderboard entry for `openai:gpt-4o` but not to the reviewer or archiver leaderboards

### Requirement: Aggregator handles missing or incomplete input gracefully

When telemetry or state files are missing, empty, or incomplete, the aggregator SHALL return a result with available metrics populated and a `warnings` list describing what was missing. The aggregator SHALL NOT raise an unhandled exception for missing files or empty telemetry.

#### Scenario: Missing telemetry file

- **WHEN** `.opsx-plan/telemetry/<plan_name>.jsonl` does not exist
- **THEN** the aggregator returns an `AggregationResult` with empty change metrics, no model leaderboard entries, and a warning indicating the telemetry file was not found

#### Scenario: Missing state file

- **WHEN** `.opsx-plan/<plan_name>.state.json` does not exist but telemetry is present
- **THEN** the aggregator derives change identity from telemetry records, returns available metrics, and includes a warning about the missing state file

#### Scenario: Empty telemetry file

- **WHEN** the telemetry JSONL file exists but contains zero records
- **THEN** the aggregator returns `total_changes` from the plan state (if available), all change statuses as `"incomplete"`, and a warning about empty telemetry

### Requirement: Aggregator emits warnings for data quality issues

The aggregator SHALL include a `warnings` list in the `AggregationResult` for the following conditions:

- Telemetry file not found.
- Plan state file not found.
- Telemetry file contains records with unknown `schema_version`.
- A change has telemetry records but no corresponding entry in plan state.
- A change in plan state has no telemetry records.
- Conflicting status between telemetry and plan state for a change.
- Records with `cost.status = "unresolved"` (counted but flagged).

#### Scenario: Warnings emitted for discrepancies

- **WHEN** telemetry shows a completed archive stage for a change but plan state does not mark it done
- **THEN** the `warnings` list includes a warning about the status conflict for that `change_id`

### Requirement: Stage-level aggregates compute descriptive statistics

Stage-level aggregation SHALL compute:

- `average_rounds` and `median_rounds` across completed changes.
- `average_duration_implement`, `average_duration_review`, `average_duration_archive` from completed stage records.
- `review_failure_rate` as `total_review_failures / total_review_stages`.
- `average_tokens_per_change` and `average_cost_per_change` using only completed changes with at least one estimated cost record.

All averages SHALL be `None` when the denominator is zero.

#### Scenario: Stage aggregates for a multi-change run

- **WHEN** 2 completed changes have round counts of 1 and 3, review failures of 0 and 2, and implement durations of 30000ms and 45000ms
- **THEN** `average_rounds = 2.0`, `median_rounds = 2.0`, `average_duration_implement = 37500.0`, `review_failure_rate = 2 / (total review stages across both changes)`
