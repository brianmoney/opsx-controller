## Context

`record-direct-stage-telemetry` writes one JSONL telemetry entry per direct stage invocation. `capture-worker-usage-metadata` populates nullable token counts and model identity. `estimate-stage-token-costs` attaches a `cost` object with status `"estimated"`, `"unresolved"`, or `"unavailable"`. Plan state files track per-change round counts, worker progress, and overall plan status.

This change reads those artifacts and produces deterministic, typed aggregation results that downstream report and dashboard changes can consume without re-parsing raw telemetry.

## Goals / Non-Goals

**Goals:**

- Compute plan-level metrics (completion rate, success rate, total duration, total tokens, total cost) from telemetry and state.
- Compute per-change metrics (status, rounds, duration, tokens, cost, failure flags) for every change in a plan.
- Compute stage-level aggregates (average/median rounds, duration per stage type, review failure rate).
- Compute model-combination leaderboards grouping by implementer model, reviewer model, archiver model, and full triple.
- Distinguish unknown cost from zero cost in every aggregate; never fabricate zero cost for `"unresolved"` or `"unavailable"` cost records.
- Handle partial runs (plan not yet complete), failed changes, blocked changes, multi-round changes, and multiple runs in a single telemetry file.

**Non-Goals:**

- Do not render reports, dashboards, or CLI output.
- Do not modify plan execution policy based on metrics.
- Do not write aggregated metrics back to telemetry or state files.
- Do not support cross-plan aggregation (single-plan scope only).

## Decisions

### 1. Read from telemetry JSONL and plan state; never modify them

The aggregator reads `.opsx-plan/telemetry/<plan_name>.jsonl` and `.opsx-plan/<plan_name>.state.json` as inputs and returns typed dataclass results. It never writes to either file, never mutates state, and is safe to run during or after a plan run.

**Rationale:** Aggregation is a read-only analysis pass. Keeping it separate from telemetry writing avoids coupling and allows operators to re-aggregate without re-running a plan.

### 2. Group records by `run_id` for multi-run files

A telemetry JSONL file may contain records from multiple runs (e.g., if an operator resets state and re-runs the same plan). The aggregator groups records by `run_id` and, by default, reports on the latest `run_id` (by maximum `started_at`). Operators may override this to aggregate a specific run.

**Rationale:** A single plan may be executed multiple times for model comparison. Aggregation should support both "latest run" and "specific run" modes without requiring file surgery.

### 3. Derive change-level status from plan state and telemetry together

Per-change status is derived from the plan state file (which tracks whether a change is pending, in progress, blocked, or done) combined with telemetry (which records actual stage outcomes). The aggregator uses these definitions:

- **completed change:** plan state marks it done AND the latest archive telemetry record has verdict `"pass"`.
- **failed change:** plan state marks it done AND the latest archive's archive status indicates failure, OR the change exceeded max rounds.
- **blocked change:** plan state marks it blocked AND no telemetry records exist for the most recent round.
- **incomplete change:** plan state does not mark it done (still pending or in progress).

**Rationale:** Telemetry alone cannot distinguish a change that has never been attempted (pending) from a change that is currently being worked on. Plan state provides the authoritative progress marker.

### 4. Model-combination metrics group by stage role

Model combination leaderboards group telemetry records by the model identity used for each stage role:

- **by implementer model:** group by `model.provider` + `model.model_id` for records where `stage = "implement"`.
- **by reviewer model:** group by `model.provider` + `model.model_id` for records where `stage = "review"`.
- **by archiver model:** group by `model.provider` + `model.model_id` for records where `stage = "archive"`.
- **by full combination:** group by the triple `(implementer_model, reviewer_model, archiver_model)` for each change, derived from the latest implement, review, and archive records for that change.

For the full-combination leaderboard, only completed changes with all three models identified are included. Changes with unknown model identity for any role are excluded from the full-combination leaderboard but included in per-role leaderboards where the role identity is known.

**Rationale:** The full triple is the most useful comparison unit for operator model selection. Per-role leaderboards help isolate which role's model choice has the most impact.

### 5. Cost aggregates separate estimated from unresolved

Every cost aggregate reports three counts alongside the sum:
- `estimated_cost_sum`: sum of `cost.estimated_cost` for records where `cost.status = "estimated"`.
- `estimated_count`: number of records contributing to the sum.
- `unresolved_count`: number of records where `cost.status` is `"unresolved"` or `"unavailable"`.
- `unknown_count`: number of records where model identity is unavailable (separate from unresolved pricing).

The "average estimated cost per change" metric uses only `estimated_cost_sum / estimated_count`. Aggregates never divide by zero; when `estimated_count = 0`, the average is `None`.

**Rationale:** Averaging unresolved costs as zero would understate true cost. Keeping the counts separate lets reports render "3 of 5 changes had estimated cost, avg $1.23" rather than silently dividing by the wrong denominator.

### 6. Use typed dataclasses for aggregation output

Aggregation results are returned as typed dataclasses with explicit `None` for absent values (e.g., `average_rounds = None` when no changes have completed). This makes consumption by downstream report/dashboard code safe and type-checkable without additional validation.

**Rationale:** The aggregator is an API layer between raw telemetry and presentation. Strong typing prevents report code from misinterpreting absent vs zero values.

## Metrics Reference

### Plan Metrics
| Metric | Type | Description |
|--------|------|-------------|
| `plan_name` | string | Plan name from state/telemetry |
| `run_id` | string | Run identifier |
| `total_changes` | int | Total changes in plan manifest |
| `completed_changes` | int | Changes that finished all phases |
| `failed_changes` | int | Changes that failed or exceeded max rounds |
| `blocked_changes` | int | Changes explicitly blocked |
| `incomplete_changes` | int | Changes not yet finished |
| `completion_rate` | float \| None | `completed_changes / total_changes` |
| `success_rate` | float \| None | `completed_changes / (completed_changes + failed_changes)` (excludes blocked/incomplete) |
| `total_duration_ms` | int \| None | Sum of all stage durations |
| `total_tokens` | int \| None | Sum of `usage.total_tokens` where available |
| `total_estimated_cost` | float \| None | Sum of `cost.estimated_cost` where estimated |
| `estimated_cost_changes` | int | Changes with at least one estimated cost record |
| `unresolved_cost_changes` | int | Changes where some costs are unresolved |
| `unknown_cost_changes` | int | Changes with no model identity |

### Change Metrics
| Metric | Type | Description |
|--------|------|-------------|
| `change_id` | string | OpenSpec change id |
| `status` | string | One of `"completed"`, `"failed"`, `"blocked"`, `"incomplete"` |
| `total_rounds` | int | Rounds consumed (from state or telemetry) |
| `duration_ms` | int \| None | Sum of all stage durations for this change |
| `tokens` | int \| None | Sum of `usage.total_tokens` where available |
| `estimated_cost` | float \| None | Sum of `cost.estimated_cost` where estimated |
| `cost_status` | string | `"estimated"` (all records estimated), `"partial"` (some estimated, some unresolved), `"unresolved"` (all unresolved), `"unavailable"` |
| `first_pass_review` | bool \| None | True if only one review stage (round 1 review verdict `"pass"`) |
| `review_failures` | int | Count of review stages with verdict `"fail"` |
| `no_progress` | bool | True if any implement-review cycle produced no progress |
| `max_rounds_exceeded` | bool | True if change hit max rounds limit |
| `archive_failed` | bool | True if archive stage had a non-completed status |
| `fast_check_failed` | bool | True if fast-check (first-round implement) failed |

### Stage Aggregates
| Metric | Type | Description |
|--------|------|-------------|
| `average_rounds` | float \| None | Mean rounds per completed change |
| `median_rounds` | float \| None | Median rounds per completed change |
| `average_duration_implement` | float \| None | Mean duration for implement stages |
| `average_duration_review` | float \| None | Mean duration for review stages |
| `average_duration_archive` | float \| None | Mean duration for archive stages |
| `review_failure_rate` | float \| None | `review_failures / total_review_stages` |
| `average_tokens_per_change` | float \| None | Mean tokens per completed change (only changes with estimated cost) |
| `average_cost_per_change` | float \| None | Mean estimated cost per completed change (only changes with estimated cost) |

### Model Combination Leaderboard Entry
| Metric | Type | Description |
|--------|------|-------------|
| `implementer_model` | string \| None | `provider:model_id` for implement stages |
| `reviewer_model` | string \| None | `provider:model_id` for review stages |
| `archiver_model` | string \| None | `provider:model_id` for archive stages |
| `change_count` | int | Number of completed changes using this combination |
| `success_rate` | float \| None | Completed changes / total changes with this combination |
| `first_pass_rate` | float \| None | First-pass review rate |
| `average_rounds` | float \| None | Mean rounds per change |
| `average_duration_ms` | float \| None | Mean total duration per change |
| `average_tokens` | float \| None | Mean tokens per change (changes with estimated cost only) |
| `average_cost` | float \| None | Mean estimated cost per change (changes with estimated cost only) |

## Risks / Trade-offs

- [Risk] Plan state and telemetry may disagree about change status. -> Mitigation: plan state is authoritative for "is this change done?" while telemetry is authoritative for "what happened during the stages?". The aggregator cross-references both and surfaces discrepancies as warnings.
- [Risk] Multi-run telemetry files may produce confusing aggregates if not filtered by run_id. -> Mitigation: default to latest run_id, document the run_id filter parameter, and include `run_id` in aggregation output.
- [Risk] Model-combination leaderboards may have very small sample sizes for some combinations. -> Mitigation: include `change_count` in every leaderboard entry so consumers can apply their own significance threshold.
- [Risk] Unknown model identity inflates the `unresolved_count` and reduces useful leaderboard data. -> Mitigation: separate unknown-model from unresolved-pricing in the cost breakdown so operators can distinguish "we don't know the model" from "we know the model but can't price it."

## Migration Plan

No migration is required. The aggregator reads existing telemetry and state files without modification.

Existing telemetry records with `cost.status = "unavailable"` (pre-cost-estimation) are treated identically to `"unresolved"` in aggregates: their cost is unknown and they do not contribute to cost sums or averages.

## Open Questions

- Should the aggregator also load the plan manifest to validate `total_changes` against expected changes, or trust telemetry+state alone? (Design assumes manifest is loaded for accurate `total_changes` count.)
- Should the aggregator emit warnings as a separate output channel (e.g., a `warnings` list on the result) or only through logging? (Design proposes a `warnings` list on the top-level result.)
