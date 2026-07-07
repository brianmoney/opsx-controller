## 1. CLI Registration

- [x] 1.1 Add `report` subparser to `main()` in `orchestrator/opsx-plan.py` with `plan` positional argument and `--json`, `--change`, `--run-id`, `--stage`, `--model` flags.
- [x] 1.2 Wire `cmd_report` as the handler function via `set_defaults(fn=cmd_report)`.

## 2. Aggregation Call

- [x] 2.1 Implement `cmd_report()` to load plan config via `load_plan()`, then call `aggregate(repo, plan_name, run_id)` from `lib.metrics.aggregator`.
- [x] 2.2 Honor `--run-id` filter by passing it directly to `aggregate()`; without `--run-id`, use the aggregator's default (latest run).
- [x] 2.3 Handle `AggregationError` gracefully (print message, return exit code 2).

## 3. Table Formatting (Default Output)

- [x] 3.1 Implement plan summary section: plan name, run id, total/completed/failed/blocked/incomplete counts, completion/success rates, total duration, total tokens, total cost, and cost breakdown counts.
- [x] 3.2 Implement per-change table: columns for change_id, status, rounds, duration, tokens, cost, cost_status, first_pass, review_failures, no_progress, max_rounds, archive_failed, fast_check_failed.
- [x] 3.3 Implement stage aggregates section: avg/median rounds, avg duration per stage, review failure rate, avg tokens and cost per change.
- [x] 3.4 Implement model leaderboard section: columns for implementer/reviewer/archiver model, change_count, success_rate, first_pass_rate, avg_rounds, avg_duration, avg_tokens, avg_cost.
- [x] 3.5 Format duration as human-readable (e.g., "1m30s"), tokens with K/M suffixes, cost as "$X.XX".
- [x] 3.6 Truncate long identifiers (>30 chars) with "…" in table output.
- [x] 3.7 Render unresolved cost as "unresolved", unavailable as "unavailable", null/None as "—".

## 4. JSON Output (`--json` Flag)

- [x] 4.1 When `--json` is set, emit a single JSON object with `command`, `plan_name`, `run_id`, `filters` (applied filter values), `plan_metrics`, `change_metrics`, `stage_aggregates`, `model_leaderboard`, `warnings`.
- [x] 4.2 Serialize `None` as JSON `null`, preserve all numeric precision from aggregation dataclasses.
- [x] 4.3 Ensure JSON output is stable: repeated runs against the same telemetry produce byte-identical JSON.

## 5. Filter Implementation

- [x] 5.1 `--change <id>`: filter `change_metrics` to the matching change; filter leaderboard to entries involving that change; plan summary remains unfiltered.
- [x] 5.2 `--stage <stage>`: filter `stage_aggregates` display to the specified stage; filter leaderboard to entries where the stage role matches.
- [x] 5.3 `--model <substring>`: filter `model_leaderboard` to entries where any role's `provider:model_id` contains the substring (case-insensitive).
- [x] 5.4 Document filter behavior in the command help text.

## 6. Edge Cases and Graceful Handling

- [x] 6.1 Empty telemetry: show plan summary with zeroes, empty tables with "No telemetry records found" message, include aggregator warnings.
- [x] 6.2 Missing state file: show what is available from telemetry, include aggregator warnings.
- [x] 6.3 Failed/incomplete plan: show all available metrics; distinguish incomplete changes from failed ones per the aggregation API.
- [x] 6.4 Multi-run telemetry: default to latest run; support explicit `--run-id`.
- [x] 6.5 Unknown model identity: show "unknown" in leaderboard and per-change output.
- [x] 6.6 Zero estimated cost (distinct from unresolved): show "$0.00" rather than "unresolved".

## 7. Unit Tests

- [x] 7.1 Test table output for a completed single-change plan with estimated costs.
- [x] 7.2 Test table output for a completed multi-change plan (2 changes, mixed cost statuses).
- [x] 7.3 Test table output for a failed plan with max-rounds and review failures.
- [x] 7.4 Test JSON output structure and stability (byte-identical across two runs).
- [x] 7.5 Test `--json` output matches aggregation dataclass fields.
- [x] 7.6 Test `--change` filter: only the specified change appears in change_metrics.
- [x] 7.7 Test `--run-id` filter: selects the correct run in a multi-run file.
- [x] 7.8 Test `--model` filter: leaderboard narrowed to matching model substring.
- [x] 7.9 Test `--stage` filter.
- [x] 7.10 Test empty telemetry: graceful output with warnings.
- [x] 7.11 Test unresolved and unavailable cost rendering in both table and JSON.
- [x] 7.12 Test zero estimated cost rendered as "$0.00" (not "unresolved").
- [x] 7.13 Test long change/model ID truncation in table mode only.
- [x] 7.14 Test report command does not modify telemetry or state files.

## 8. Verification

- [x] 8.1 Run `python3 -m unittest tests/orchestrator/test_opsx_plan.py`.
- [x] 8.2 Run `openspec validate add-opsx-plan-report-command --strict`.
- [x] 8.3 Re-run `bash adapters/opencode/install.sh --global --verify` since `orchestrator/opsx-plan.py` is modified.
