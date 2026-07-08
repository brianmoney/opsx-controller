## Context

`add-opsx-plan-report-command` provides `opsx-plan report` with table and JSON output from the aggregation API. The report surfaces plan efficiency metrics but the terminal output becomes hard to scan when comparing multiple model combinations or looking for patterns across runs.

This change adds `opsx-plan dashboard` as a second presentation layer: a static HTML file that renders the same aggregation results as a visual dashboard. The HTML output is self-contained (all CSS inline in a `<style>` block, no external references) and deterministic — regenerating from the same telemetry produces an identical file.

The `opsx-plan` CLI already has subcommands (`run`, `status`, `report`, etc.); the dashboard command follows the same pattern with `dashboard` as a subparser and `--output` for file control.

## Goals / Non-Goals

**Goals:**

- Add an `opsx-plan dashboard <plan>` command that produces a self-contained static HTML file.
- Include seven dashboard sections: plan summary header, model leaderboard table, per-change detail table, failure breakdown, cost breakdown summary, rounds histogram, and stage timeline.
- Use inline CSS only; no external stylesheets, JavaScript, or CDN references.
- Support `--output <path>` flag (default: `.opsx-plan/dashboards/<plan_name>.html`).
- Support `--run-id` and `--change` filters matching the report command semantics.
- Make unknown usage and unresolved costs visually distinct from zero values with explicit labels and color coding.
- Produce deterministic output: same telemetry + same state = byte-identical HTML.
- Handle empty/missing telemetry gracefully with a clear "no data" message in the dashboard.
- Be read-only: never modify telemetry, state, or any other file except the output HTML.

**Non-Goals:**

- No JavaScript, interactive charts, live refresh, or server mode.
- No charting library dependency (e.g., Chart.js, D3).
- No cross-plan dashboard aggregation.
- No PDF or image export.
- No modification to the aggregation API, pricing catalog, or cost estimation.
- No change to plan execution policy based on dashboard content.
- No authentication, sharing, or hosted service.

## Decisions

### 1. Dashboard is an `opsx-plan` subcommand, parallel to `report`

The `dashboard` command is registered as a subparser under `opsx-plan`, like `report` and `status`. It accepts the plan TOML path as a positional argument.

**Rationale:** Consistent CLI surface. Operators already run `opsx-plan report <plan>`. `opsx-plan dashboard <plan>` is natural to discover and remember.

### 2. HTML output is self-contained with inline CSS

A single `.html` file with all styles in a `<style>` block. No `<link>`, no `<script>`, no external URLs. Fonts fall back to system monospace/sans-serif stacks.

**Rationale:** The artifact must be portable — openable from any filesystem, archivable alongside telemetry, and free of network dependencies. Deterministic output requires zero external references.

### 3. Seven dashboard sections, rendered in order

1. **Plan Summary Header** — plan name, run id, total/completed/failed/blocked/incomplete change counts, completion/success rates, total duration, total tokens, total estimated cost, and cost breakdown (estimated/unresolved/unknown counts). Rendered as a summary card at the top.

2. **Model Leaderboard Table** — rows for each model combination (implementer, reviewer, archiver) with columns: change_count, success_rate, first_pass_rate, avg_rounds, avg_duration, avg_tokens, avg_cost. Sorted by success_rate descending. Best values highlighted.

3. **Per-Change Table** — one row per change with: change_id, status (color-coded), rounds, duration, tokens, cost, cost_status, first_pass, review_failures, flags (no_progress, max_rounds, archive_failed, fast_check_failed). Statuses color-coded: green (completed), red (failed), yellow (blocked), gray (incomplete).

4. **Failure Breakdown** — list of failed changes with failure reason (max_rounds, archive_failed, review_failures). Only shown when there are failed changes; otherwise renders a "No failures" message.

5. **Cost Breakdown** — summary of estimated, unresolved, and unknown cost changes with counts. Rendered as a simple bar using CSS width percentages.

6. **Rounds Histogram** — distribution of round counts across completed changes, rendered as an ASCII-inspired CSS bar chart (div widths proportional to count).

7. **Stage Timeline** — chronological list of stage invocations sorted by `started_at`, one row per stage: change_id, stage, round, started_at timestamp, duration, status (color-coded). Shows the sequence of work for the run.

**Rationale:** These seven sections cover the full efficiency comparison surface described in the plan (reliability, speed, rework, cost) without requiring JavaScript. Each section is independently skimmable.

### 4. `--output` flag controls file destination; default is under `.opsx-plan/dashboards/`

`--output <path>` writes the HTML to the given path. Without `--output`, the default is `.opsx-plan/dashboards/<plan_name>.html`. The directory is created if it doesn't exist.

**Rationale:** Dashboard artifacts belong alongside telemetry and state. A dedicated `dashboards/` directory under `.opsx-plan/` keeps them organized and makes archival straightforward.

### 5. `--run-id` and `--change` filters mirror the report command

- `--run-id <id>`: select a specific run (default: latest via aggregator).
- `--change <id>`: scope the dashboard to a single change. The plan summary header still shows full-plan counts for context; per-change table, failure breakdown, and timeline narrow to the specified change.

**Rationale:** Same filter semantics as `report` reduces cognitive load and keeps the two commands consistent. The plan summary staying global aligns with the report command's design.

### 6. Unknown/unresolved costs are visually distinct with color and labels

- Estimated cost: rendered as `$X.XX` in normal text.
- Unresolved cost: rendered in amber/orange with label `(unresolved)`.
- Unavailable/missing: rendered in gray with label `—` or `(no data)`.
- Zero estimated cost: rendered as `$0.00` in normal text (not the same as unresolved).

**Rationale:** The plan's success criteria explicitly require "visually distinct" rendering. Color + label redundancy ensures the distinction is clear even if the HTML is printed or viewed on a monochrome display.

### 7. Simple CSS-based "charts" — no SVG, no Canvas, no JavaScript

The rounds histogram uses CSS `width` percentages on `<div>` elements. The cost breakdown uses horizontal bars. The timeline is a plain HTML table with color-coded status badges. The leaderboard table highlights best values with bold text.

**Rationale:** Keeping all visuals as styled HTML elements means the dashboard works in any browser, is trivially deterministic, and requires zero additional tooling. These are summary charts, not interactive analytics.

### 8. HTML generation uses Python string formatting, not a template engine

The `_render_dashboard_html()` function builds the HTML page as a single string with f-strings and `join()` calls. Aggregation dataclass values are embedded directly.

**Rationale:** `opsx-plan.py` is stdlib-only. A template engine (Jinja2, Mako) would violate that contract. String formatting is sufficient for the repetition patterns in these sections (tables, lists, bars).

### 9. Deterministic output: no timestamps or random IDs in the HTML

The dashboard HTML does not include a "generated at" timestamp, random element IDs, or any non-deterministic content. The `run_id` and `started_at` from telemetry are the only time-referencing fields, and they are read from disk.

**Rationale:** Deterministic output is a plan success criterion and enables snapshot testing.

### 10. Error handling: graceful degradation with HTML "no data" message

When telemetry is missing or empty, the dashboard HTML still renders with the plan name in the header and a clear "No telemetry data available" message in each section. Aggregator warnings are rendered in a dedicated warnings section at the bottom.

**Rationale:** The dashboard must always produce a valid HTML file, even for plans that haven't been run yet. This prevents "command failed" surprises in automation.

## Risks / Trade-offs

- [Risk] Large plans with many changes produce large HTML files. -> Mitigation: HTML is compressed output for browser consumption; file size is bounded by telemetry record count. A 100-change plan with full telemetry produces roughly 50-100 KB of HTML — negligible.
- [Risk] Browser rendering may vary across engines. -> Mitigation: CSS uses widely supported properties (flexbox, CSS variables, percentage widths). The dashboard is tested for structural correctness through assertions on content presence, not pixel-level rendering.
- [Risk] `--change` filter with the dashboard's multiple sections may produce confusing output (e.g., leaderboard shows one row). -> Mitigation: section headers clearly indicate filtering via `[Filtered: change=<id>]` annotation, matching the report command's convention.
- [Risk] The plan suggests Markdown as an alternative format. -> Mitigation: HTML is chosen over Markdown because it supports color coding, bar charts, and visual hierarchy that are central to the plan's "visually distinct" requirement. Markdown cannot express these without extensions. If demand arises, a `--format md` flag could be added later.

## Migration Plan

No migration is required. The dashboard command reads existing telemetry and state files without modification. Plans run before this change produce telemetry that is fully compatible with the dashboard command.

No existing commands or behaviors are modified.

## Open Questions

- Should the dashboard support `--format html|md` for a Markdown variant? (Design proposes HTML-only for now; Markdown can be added as a follow-up if requested.)
- Should the rounds histogram use log scale for wide distributions? (Design proposes linear scale with a "max rounds" annotation; log scale can be added if operators find linear unreadable for >10 rounds.)
