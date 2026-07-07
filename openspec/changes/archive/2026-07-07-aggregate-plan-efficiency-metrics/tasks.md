## 1. Data Types

- [x] 1.1 Define typed dataclasses for `PlanMetrics`, `ChangeMetrics`, `StageAggregates`, and `ModelLeaderboardEntry` in `lib/metrics/`.
- [x] 1.2 Ensure all numeric aggregates use `None` for absent values (e.g., no completed changes) and never default to zero.
- [x] 1.3 Define the `AggregationResult` top-level dataclass with `plan_metrics`, `change_metrics` (list), `stage_aggregates`, `model_leaderboard` (list of entries), and `warnings` (list of strings).

## 2. Telemetry Reading

- [x] 2.1 Add a telemetry reader that parses `.opsx-plan/telemetry/<plan_name>.jsonl` and returns records grouped by `run_id`.
- [x] 2.2 Default to the latest `run_id` (by maximum `started_at` across records). Support an optional `run_id` parameter to select a specific run.
- [x] 2.3 Handle missing telemetry file gracefully (return empty records list, emit a warning).
- [x] 2.4 Handle schema version differences (record-level `schema_version` branching) without failing.

## 3. State Reading

- [x] 3.1 Read `.opsx-plan/<plan_name>.state.json` to extract plan metadata (`plan_name`, `started_at`, `status`) and per-change progress (`round`, `status`, `done`, `blocked`).
- [x] 3.2 Handle missing state file (emit warning, derive change list from telemetry when possible).
- [x] 3.3 Handle pre-telemetry state files that lack the `telemetry` linking field.

## 4. Plan-Level Aggregation

- [x] 4.1 Compute `total_changes`, `completed_changes`, `failed_changes`, `blocked_changes`, `incomplete_changes`.
- [x] 4.2 Compute `completion_rate` and `success_rate`.
- [x] 4.3 Compute `total_duration_ms`, `total_tokens`, `total_estimated_cost` from completed stage records.
- [x] 4.4 Compute `estimated_cost_changes`, `unresolved_cost_changes`, `unknown_cost_changes` breakdown.

## 5. Change-Level Aggregation

- [x] 5.1 For each change, derive `status` from plan state + telemetry (completed, failed, blocked, incomplete).
- [x] 5.2 Compute `total_rounds` from the maximum `round` value in telemetry or from plan state.
- [x] 5.3 Compute `duration_ms`, `tokens`, `estimated_cost` from the change's telemetry records.
- [x] 5.4 Compute aggregated `cost_status` (`"estimated"`, `"partial"`, `"unresolved"`, `"unavailable"`).
- [x] 5.5 Determine `first_pass_review` (round 1 review verdict `"pass"` and only one review stage).
- [x] 5.6 Compute `review_failures` (count of review stages with verdict `"fail"`).
- [x] 5.7 Detect `no_progress` (any implement-review cycle with no progress), `max_rounds_exceeded`, `archive_failed` (archive stage non-completed), `fast_check_failed` (first-round implement unsuccessful).

## 6. Stage-Level Aggregates

- [x] 6.1 Compute `average_rounds` and `median_rounds` across completed changes.
- [x] 6.2 Compute `average_duration_implement`, `average_duration_review`, `average_duration_archive` from completed stage records.
- [x] 6.3 Compute `review_failure_rate` as `review_failures / total_review_stages`.
- [x] 6.4 Compute `average_tokens_per_change` and `average_cost_per_change` using only changes with estimated cost (never divide by changes with unresolved cost).

## 7. Model Combination Leaderboard

- [x] 7.1 Group by implementer model (`model.provider` + `model.model_id` for `stage = "implement"` records).
- [x] 7.2 Group by reviewer model (`model.provider` + `model.model_id` for `stage = "review"` records).
- [x] 7.3 Group by archiver model (`model.provider` + `model.model_id` for `stage = "archive"` records).
- [x] 7.4 Group by full triple `(implementer_model, reviewer_model, archiver_model)` for each completed change.
- [x] 7.5 Compute `change_count`, `success_rate`, `first_pass_rate`, `average_rounds`, `average_duration_ms`, `average_tokens`, `average_cost` per leaderboard entry.
- [x] 7.6 Exclude changes with unknown model identity from the full-combination leaderboard (include in per-role leaderboards where identity is known).

## 8. Cost Null-Handling

- [x] 8.1 `"estimated"` cost records contribute to cost sums and averages.
- [x] 8.2 `"unresolved"` and `"unavailable"` cost records do NOT contribute to cost sums or averages.
- [x] 8.3 Zero estimated cost (`estimated_cost = 0.0`) is distinct from absent cost and is included in sums and averages.
- [x] 8.4 Averages use only the count of records with estimated cost as denominator; never divide by zero.

## 9. Unit Tests

- [x] 9.1 Test plan-level aggregation against a synthetic telemetry + state fixture representing a successful 2-change run.
- [x] 9.2 Test change-level aggregation for a completed change with one round (first-pass review pass).
- [x] 9.3 Test change-level aggregation for a change that required 3 rounds before passing.
- [x] 9.4 Test change-level aggregation for a failed change (max rounds exceeded).
- [x] 9.5 Test change-level aggregation for a blocked change.
- [x] 9.6 Test incomplete plan (some changes not yet started).
- [x] 9.7 Test cost aggregation with mixed estimated, unresolved, and unavailable costs.
- [x] 9.8 Test model leaderboard with fully identified models.
- [x] 9.9 Test model leaderboard with partially unknown model identity.
- [x] 9.10 Test multi-run telemetry file (two runs in one JSONL) with run_id filtering.
- [x] 9.11 Test missing telemetry file (empty result with warning).
- [x] 9.12 Test missing state file (derive from telemetry with warning).

## 10. Verification

- [x] 10.1 Run `python3 -m unittest tests/lib/metrics/` or `python3 -m unittest tests/orchestrator/test_opsx_plan.py`.
- [x] 10.2 Run `openspec validate aggregate-plan-efficiency-metrics --strict`.
- [x] 10.3 No runtime installers are modified by this change.
