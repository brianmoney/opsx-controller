## 1. CLI Registration

- [x] 1.1 Add `dashboard` subparser to `main()` in `orchestrator/opsx-plan.py` with `plan` positional argument and `--output`, `--run-id`, `--change` flags.
- [x] 1.2 Wire `cmd_dashboard` as the handler function via `set_defaults(fn=cmd_dashboard)`.

## 2. Aggregation Call

- [x] 2.1 Implement `cmd_dashboard()` to load plan config via `load_plan()`, then call `aggregate(repo, plan_name, run_id)` from `lib.metrics.aggregator`.
- [x] 2.2 Honor `--run-id` filter by passing it directly to `aggregate()`; without `--run-id`, use the aggregator's default (latest run).
- [x] 2.3 Honor `--change` filter: re-scope change_metrics and leaderboard via the same strategy used by `cmd_report()`.
- [x] 2.4 Handle `AggregationError` gracefully (print message, return exit code 2).

## 3. HTML Rendering Infrastructure

- [x] 3.1 Implement `_render_dashboard_html()` taking `AggregationResult`, `plan_name`, `run_id`, and optional `change_id` filter, returning a complete HTML string.
- [x] 3.2 The HTML SHALL be self-contained: all CSS in a single `<style>` block, no external references, no JavaScript.
- [x] 3.3 Implement `_html_escape()` helper for safe text embedding.
- [x] 3.4 Implement formatting helpers: `_fmt_duration()` (ms → "1m30s"), `_fmt_tokens()` (integer → "1.5M"/"2.3K"), `_fmt_cost()` (float or None → "$X.XX"), `_fmt_rate()` (float → "XX%").

## 4. Dashboard Sections

- [x] 4.1 Plan Summary Header: render as a styled card with plan name, run id, change counts (total/completed/failed/blocked/incomplete), completion/success rates, total duration, total tokens, total estimated cost, and cost breakdown counts (estimated/unresolved/unknown).
- [x] 4.2 Model Leaderboard Table: render rows sorted by success_rate descending; columns for implementer/reviewer/archiver model, change_count, success_rate, first_pass_rate, avg_rounds, avg_duration, avg_tokens, avg_cost. Highlight best values per column.
- [x] 4.3 Per-Change Table: render one row per change with change_id, status (color-coded badge: green=completed, red=failed, yellow=blocked, gray=incomplete), rounds, duration, tokens, cost, cost_status, first_pass, review_failures, flags.
- [x] 4.4 Failure Breakdown: render a list of failed changes with failure reason (max_rounds_exceeded, archive_failed, review_failure count). When no failures exist, render "No failures" message. Only shown for non-empty failure list.
- [x] 4.5 Cost Breakdown: render a summary bar showing estimated/unresolved/unknown cost change counts as proportional CSS width bars with labels and counts.
- [x] 4.6 Rounds Histogram: render a CSS bar chart showing the distribution of round counts across completed changes. X-axis labels for round counts, bar heights proportional to frequency. Empty state shown when no completed changes.
- [x] 4.7 Stage Timeline: render a table of stage invocations sorted by `started_at` ascending, with columns for change_id, stage, round, started_at (ISO-8601), duration (human-readable), status (color-coded badge). The timeline captures the sequence from telemetry records for the selected run.

## 5. Visual Distinction Rules

- [x] 5.1 Estimated cost: render as `$X.XX` in normal text. Zero estimated cost renders as `$0.00`.
- [x] 5.2 Unresolved cost: render with amber/orange styling and explicit `(unresolved)` label.
- [x] 5.3 Unavailable cost (no usage data): render with gray styling and `—` or `(no data)` label.
- [x] 5.4 Null/None values: render as `—` in gray text.
- [x] 5.5 Stage status badges: color-coded (green=completed, red=failed, orange=timeout, gray=other).

## 6. Output File Handling

- [x] 6.1 Default output path: `.opsx-plan/dashboards/<plan_name>.html`. Create the `dashboards/` directory if it does not exist.
- [x] 6.2 `--output <path>` overrides the default path. The parent directory is created if it does not exist.
- [x] 6.3 The HTML file is written atomically (write to temp file, then rename) to avoid partial files on error.

## 7. Edge Cases and Graceful Handling

- [x] 7.1 Empty telemetry: render dashboard with plan name header and "No telemetry data available" messages in each section; include aggregator warnings in a warnings section.
- [x] 7.2 Missing state file: derive what is available from telemetry; render available sections; include aggregator warnings.
- [x] 7.3 Failed/incomplete plan: show all available metrics; leaderboard includes all changes (not only completed); histogram only uses completed changes.
- [x] 7.4 Multi-run telemetry with --run-id: default to latest run; support explicit `--run-id`.
- [x] 7.5 Unknown model identity: show "unknown" in leaderboard and per-change table.
- [x] 7.6 Zero estimated cost distinct from unresolved: render as `$0.00` (not "unresolved" styling).

## 8. Unit Tests

- [x] 8.1 Test dashboard HTML structure for a completed single-change plan with estimated costs.
- [x] 8.2 Test dashboard HTML structure for a completed multi-change plan (2 changes, mixed cost statuses).
- [x] 8.3 Test dashboard HTML contains all seven required sections.
- [x] 8.4 Test deterministic output: same telemetry produces byte-identical HTML across two runs.
- [x] 8.5 Test resolved cost renders as `$X.XX` (not "unresolved").
- [x] 8.6 Test unresolved cost renders with amber styling and `(unresolved)` label.
- [x] 8.7 Test zero estimated cost renders as `$0.00`.
- [x] 8.8 Test that `--run-id` selects the correct run in a multi-run telemetry file.
- [x] 8.9 Test that `--change` filter narrows change_metrics, leaderboard, and timeline.
- [x] 8.10 Test that `--output <path>` writes to the specified file path.
- [x] 8.11 Test that default output writes to `.opsx-plan/dashboards/<plan_name>.html`.
- [x] 8.12 Test empty telemetry renders a valid HTML file with "no data" messages.
- [x] 8.13 Test dashboard command does not modify telemetry or state files.
- [x] 8.14 Test HTML is self-contained: no external `<link>`, `<script src>`, or remote URLs.
- [x] 8.15 Test stage timeline entries are sorted by `started_at` ascending.
- [x] 8.16 Test failed plan failure breakdown lists failed changes with reasons.
- [x] 8.17 Test rounds histogram shows distribution for completed changes only.

## 9. Verification

- [x] 9.1 Run `python3 -m unittest tests/orchestrator/test_opsx_plan.py`.
- [x] 9.2 Run `openspec validate export-plan-efficiency-dashboard --strict`.
- [x] 9.3 Run `bash adapters/opencode/install.sh --global --verify` since `orchestrator/opsx-plan.py` will be modified.
