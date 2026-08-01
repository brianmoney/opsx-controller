"""Dashboard command: HTML rendering for `opsx-plan dashboard`.

Depends on `base` and `planref` for plan resolution and on `report`
for `_resolve_for_change_plan`; reads metrics via
`lib.metrics.aggregator`.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from lib.orchestrator import base, planref, report

def _html_escape(s: str) -> str:
    """Escape text for safe HTML embedding."""
    return (
        s.replace("&", "&amp;")
         .replace("<", "&lt;")
         .replace(">", "&gt;")
         .replace('"', "&quot;")
         .replace("'", "&#x27;")
    )


def _fmt_rate(val: float | None) -> str:
    """Format a 0–1 fraction as 'XX%' string (HTML-safe)."""
    if val is None:
        return '<span class="null-value">—</span>'
    return f"{val * 100:.1f}%"


def _fmt_cost_html(estimated_cost: float | None, cost_status: str) -> str:
    """Return an HTML span with appropriate cost styling.

    - estimated: green text with $X.XX
    - unresolved: amber text with (unresolved) label
    - unavailable: gray text with —
    - zero estimated: $0.00 in normal (green) styling
    """
    if cost_status == "unavailable":
        return '<span class="cost-missing">—</span>'
    if cost_status == "unresolved":
        if estimated_cost is not None:
            return (
                f'<span class="cost-unresolved">'
                f'${estimated_cost:.2f} <small>(unresolved)</small>'
                f'</span>'
            )
        return '<span class="cost-unresolved">(unresolved)</span>'
    if estimated_cost is not None:
        return f'<span class="cost-estimated">${estimated_cost:.2f}</span>'
    return '<span class="cost-missing">—</span>'


def _fmt_nullable(val, fmt_fn) -> str:
    """Render optional value with *fmt_fn*, or gray '—' dash."""
    if val is None:
        return '<span class="null-value">—</span>'
    return fmt_fn(val)


def _fmt_duration_html(ms: int | float | None) -> str:
    """Format milliseconds as human-readable duration, fallback to '—'."""
    if ms is None:
        return '<span class="null-value">—</span>'
    total_s = int(ms) // 1000
    minutes = total_s // 60
    seconds = total_s % 60
    return f"{minutes}m{seconds}s"


def _fmt_tokens_html(n: int | float | None) -> str:
    """Format token count with K/M suffix, fallback to '—'."""
    if n is None:
        return '<span class="null-value">—</span>'
    n = int(n)
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}K"
    return str(n)


def _fmt_bool_html(val: bool | None) -> str:
    """Format optional boolean as '✓' / '✗' / '—'."""
    if val is None:
        return '<span class="null-value">—</span>'
    if val:
        return '<span class="bool-true">✓</span>'
    return '<span class="bool-false">✗</span>'


def _status_badge(status: str) -> str:
    """Return an HTML color-coded badge span for a change status."""
    color_map = {
        "completed": "green",
        "failed": "red",
        "yellow": "yellow",
        "blocked": "yellow",
        "incomplete": "gray",
    }
    color = color_map.get(status, "gray")
    return f'<span class="badge badge-{color}">{_html_escape(status)}</span>'


def _stage_status_badge(status: str) -> str:
    """Return an HTML color-coded badge span for a stage status."""
    if status == "completed":
        color = "green"
    elif status == "failed":
        color = "red"
    elif status == "timeout":
        color = "orange"
    else:
        color = "gray"
    return f'<span class="badge badge-{color}">{_html_escape(status)}</span>'


_DASHBOARD_CSS = """\
:root {
    --bg: #f5f5f5;
    --card-bg: #ffffff;
    --text: #1a1a2e;
    --text-secondary: #555;
    --border: #ddd;
    --green: #2e7d32;
    --red: #c62828;
    --yellow: #f9a825;
    --orange: #ef6c00;
    --amber: #e65100;
    --gray: #757575;
    --blue: #1565c0;
    --highlight-bg: #e8f5e9;
}

* { box-sizing: border-box; margin: 0; padding: 0; }

body {
    font-family: system-ui, -apple-system, sans-serif;
    background: var(--bg);
    color: var(--text);
    line-height: 1.5;
    padding: 20px;
}

header {
    margin-bottom: 24px;
}

header h1 {
    font-size: 1.6rem;
    color: var(--text);
}

.filter-annotation {
    font-size: 0.85rem;
    color: var(--blue);
    margin-top: 4px;
}

main {
    max-width: 1200px;
}

section {
    background: var(--card-bg);
    border: 1px solid var(--border);
    border-radius: 6px;
    padding: 20px;
    margin-bottom: 16px;
}

section h2 {
    font-size: 1.2rem;
    margin-bottom: 12px;
    padding-bottom: 6px;
    border-bottom: 2px solid var(--border);
}

/* --- Plan Summary Card --- */
.summary-grid {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
    gap: 12px;
}

.summary-item {
    padding: 8px 0;
}

.summary-item .label {
    font-size: 0.75rem;
    text-transform: uppercase;
    color: var(--text-secondary);
    letter-spacing: 0.5px;
}

.summary-item .value {
    font-size: 1.1rem;
    font-weight: 600;
}

/* --- Tables --- */
table {
    width: 100%;
    border-collapse: collapse;
    font-size: 0.9rem;
}

th, td {
    padding: 8px 10px;
    text-align: left;
    border-bottom: 1px solid var(--border);
    white-space: nowrap;
}

th {
    background: #fafafa;
    font-weight: 600;
    font-size: 0.8rem;
    text-transform: uppercase;
    color: var(--text-secondary);
    letter-spacing: 0.3px;
}

tr:hover td {
    background: #f0f4ff;
}

.best-value {
    font-weight: 700;
    color: var(--green);
}

/* --- Badges --- */
.badge {
    display: inline-block;
    padding: 2px 8px;
    border-radius: 10px;
    font-size: 0.78rem;
    font-weight: 600;
    color: #fff;
}

.badge-green { background: var(--green); }
.badge-red { background: var(--red); }
.badge-yellow { background: var(--yellow); color: #333; }
.badge-orange { background: var(--orange); }
.badge-gray { background: var(--gray); }

/* --- Cost styling --- */
.cost-estimated { color: var(--green); font-weight: 500; }
.cost-unresolved { color: var(--amber); font-weight: 500; }
.cost-missing { color: var(--gray); }
.null-value { color: var(--gray); }
.bool-true { color: var(--green); font-weight: 700; }
.bool-false { color: var(--red); }

/* --- Cost Breakdown Bars --- */
.bar-container {
    display: flex;
    height: 28px;
    border-radius: 4px;
    overflow: hidden;
    margin-bottom: 8px;
}

.bar-segment {
    display: flex;
    align-items: center;
    justify-content: center;
    font-size: 0.78rem;
    font-weight: 600;
    color: #fff;
    min-width: 0;
}

.bar-estimated { background: var(--green); }
.bar-unresolved { background: var(--amber); }
.bar-unknown { background: var(--gray); }

.bar-legend {
    display: flex;
    gap: 16px;
    font-size: 0.8rem;
    color: var(--text-secondary);
}

.bar-legend-item {
    display: flex;
    align-items: center;
    gap: 4px;
}

.bar-legend-swatch {
    width: 12px;
    height: 12px;
    border-radius: 2px;
}

/* --- Histogram --- */
.histogram {
    display: flex;
    align-items: flex-end;
    gap: 4px;
    height: 180px;
    padding: 4px 0;
    border-bottom: 1px solid var(--border);
}

.histogram-bar-wrapper {
    flex: 1;
    display: flex;
    flex-direction: column;
    align-items: center;
    min-width: 24px;
}

.histogram-bar {
    width: 100%;
    background: var(--blue);
    border-radius: 3px 3px 0 0;
    min-height: 2px;
    transition: background 0.2s;
}

.histogram-bar:hover {
    background: var(--orange);
}

.histogram-count {
    font-size: 0.72rem;
    font-weight: 600;
    margin-bottom: 2px;
}

.histogram-label {
    font-size: 0.7rem;
    color: var(--text-secondary);
    margin-top: 4px;
}

/* --- Empty state --- */
.empty-state {
    color: var(--text-secondary);
    font-style: italic;
    padding: 12px 0;
}

/* --- Warnings --- */
.warnings-list {
    list-style: none;
    padding: 0;
}

.warnings-list li {
    padding: 4px 0;
    color: var(--amber);
    font-size: 0.85rem;
}

.warnings-list li::before {
    content: "⚠ ";
}
"""


def _render_plan_summary_html(pm, plan_name, run_id, filters) -> str:
    """Render the plan summary header section as HTML."""
    parts: list[str] = []
    parts.append('<section class="plan-summary">')
    parts.append("<h2>Plan Summary</h2>")

    # Filter annotation
    active = {k: v for k, v in filters.items() if v}
    if active:
        filt_parts = [f"{_html_escape(k)}={_html_escape(str(v))}"
                       for k, v in active.items()]
        parts.append(
            f'<div class="filter-annotation">'
            f'[Filtered: {", ".join(filt_parts)}]'
            f'</div>'
        )

    parts.append('<div class="summary-grid">')

    def _kv(label: str, value: str) -> str:
        return (
            f'<div class="summary-item">'
            f'<div class="label">{_html_escape(label)}</div>'
            f'<div class="value">{value}</div>'
            f'</div>'
        )

    parts.append(_kv("Plan", _html_escape(plan_name)))
    parts.append(_kv("Run", _html_escape(run_id) if run_id else "—"))

    if pm.total_changes > 0:
        changes_detail = (
            f"{pm.total_changes} total "
            f"({pm.completed_changes} completed, "
            f"{pm.failed_changes} failed, "
            f"{pm.blocked_changes} blocked, "
            f"{pm.incomplete_changes} incomplete)"
        )
        parts.append(_kv("Changes", changes_detail))
        parts.append(_kv("Completion Rate", _fmt_rate(pm.completion_rate)))
        parts.append(_kv("Success Rate", _fmt_rate(pm.success_rate)))
        parts.append(_kv("Total Duration", _fmt_duration_html(pm.total_duration_ms)))
        parts.append(_kv("Total Tokens", _fmt_tokens_html(pm.total_tokens)))

        # Total cost
        if pm.total_estimated_cost is not None:
            if pm.unresolved_cost_changes > 0:
                cost_val = (
                    f'<span class="cost-unresolved">'
                    f'${pm.total_estimated_cost:.2f}'
                    f' <small>(partial)</small>'
                    f'</span>'
                )
            else:
                cost_val = (
                    f'<span class="cost-estimated">'
                    f'${pm.total_estimated_cost:.2f}'
                    f'</span>'
                )
        elif pm.unresolved_cost_changes > 0:
            cost_val = '<span class="cost-unresolved">(unresolved)</span>'
        else:
            cost_val = '<span class="cost-missing">—</span>'
        cost_detail = (
            f"{cost_val} ({pm.estimated_cost_changes} est, "
            f"{pm.unresolved_cost_changes} unr, "
            f"{pm.unknown_cost_changes} unk)"
        )
        parts.append(_kv("Total Cost", cost_detail))
    else:
        parts.append(_kv("Status", "No telemetry records found."))

    parts.append("</div>")  # summary-grid
    parts.append("</section>")
    return "\n".join(parts)


def _render_leaderboard_html(ml: list) -> str:
    """Render the model leaderboard table as HTML."""
    # Sort by success_rate descending (None sorts last).
    ml_sorted = sorted(ml, key=lambda e: (
        e.success_rate is not None,
        e.success_rate if e.success_rate is not None else -1.0,
    ), reverse=True)
    parts: list[str] = []
    parts.append('<section class="leaderboard">')
    parts.append("<h2>Model Leaderboard</h2>")

    if not ml_sorted:
        parts.append('<p class="empty-state">No leaderboard entries.</p>')
        parts.append("</section>")
        return "\n".join(parts)

    # Compute best values for highlighting
    best: dict[str, tuple[float, int]] = {}

    def _update_best(col: str, val: float, idx: int) -> None:
        if val is None:
            return
        if col not in best or val > best[col][0]:
            best[col] = (val, idx)

    for i, e in enumerate(ml_sorted):
        _update_best("success_rate", e.success_rate, i) if e.success_rate else None
        _update_best("first_pass_rate", e.first_pass_rate, i) if e.first_pass_rate else None
        # For avg_rounds, lower is better
        if e.average_rounds is not None:
            if "avg_rounds" not in best or e.average_rounds < best["avg_rounds"][0]:
                best["avg_rounds"] = (e.average_rounds, i)
        if e.average_duration_ms is not None:
            if "avg_duration" not in best or e.average_duration_ms < best["avg_duration"][0]:
                best["avg_duration"] = (e.average_duration_ms, i)
        if e.average_tokens is not None:
            if "avg_tokens" not in best or e.average_tokens < best["avg_tokens"][0]:
                best["avg_tokens"] = (e.average_tokens, i)
        if e.average_cost is not None:
            if "avg_cost" not in best or e.average_cost < best["avg_cost"][0]:
                best["avg_cost"] = (e.average_cost, i)

    best_rows: dict[str, set[int]] = {col: {info[1]} for col, info in best.items()}

    def _best_class(col: str, idx: int) -> str:
        if idx in best_rows.get(col, set()):
            return ' class="best-value"'
        return ""

    parts.append("<table><thead><tr>")
    headers = [
        "Implementer", "Reviewer", "Archiver",
        "Changes", "Success", "1st Pass",
        "Avg Rnds", "Avg Dur", "Avg Tokens", "Avg Cost",
    ]
    for h in headers:
        parts.append(f"<th>{_html_escape(h)}</th>")
    parts.append("</tr></thead><tbody>")

    for i, e in enumerate(ml_sorted):
        parts.append("<tr>")
        parts.append(
            f"<td{_best_class('success_rate', i)}>"
            f"{_html_escape(e.implementer_model or 'unknown')}</td>"
        )
        parts.append(
            f"<td>{_html_escape(e.reviewer_model or 'unknown')}</td>"
        )
        parts.append(
            f"<td>{_html_escape(e.archiver_model or 'unknown')}</td>"
        )
        parts.append(f"<td>{e.change_count}</td>")
        parts.append(
            f"<td{_best_class('success_rate', i)}>"
            f"{_fmt_rate(e.success_rate)}</td>"
        )
        parts.append(
            f"<td{_best_class('first_pass_rate', i)}>"
            f"{_fmt_rate(e.first_pass_rate)}</td>"
        )
        if e.average_rounds is not None:
            avg_rnds_val = f"{e.average_rounds:.1f}"
        else:
            avg_rnds_val = '<span class="null-value">—</span>'
        parts.append(
            f"<td{_best_class('avg_rounds', i)}>{avg_rnds_val}</td>"
        )
        parts.append(
            f"<td{_best_class('avg_duration', i)}>"
            f"{_fmt_duration_html(e.average_duration_ms)}</td>"
        )
        parts.append(
            f"<td{_best_class('avg_tokens', i)}>"
            f"{_fmt_tokens_html(e.average_tokens)}</td>"
        )
        parts.append(
            f"<td{_best_class('avg_cost', i)}>"
            f"{_fmt_cost_html(e.average_cost, 'estimated' if e.average_cost is not None else 'unavailable')}</td>"
        )
        parts.append("</tr>")

    parts.append("</tbody></table>")
    parts.append("</section>")
    return "\n".join(parts)


def _render_change_table_html(cm_list: list) -> str:
    """Render the per-change detail table as HTML."""
    parts: list[str] = []
    parts.append('<section class="per-change">')
    parts.append("<h2>Per-Change Details</h2>")

    if not cm_list:
        parts.append('<p class="empty-state">No change metrics available.</p>')
        parts.append("</section>")
        return "\n".join(parts)

    parts.append("<table><thead><tr>")
    headers = [
        "Change ID", "Status", "Rnds", "Duration", "Tokens",
        "Cost", "Cost Status", "1st Pass", "Rev Fails",
        "No Prog", "Max Rnd", "Arch Fail", "Fast Chk",
    ]
    for h in headers:
        parts.append(f"<th>{_html_escape(h)}</th>")
    parts.append("</tr></thead><tbody>")

    for c in cm_list:
        parts.append("<tr>")
        parts.append(f"<td>{_html_escape(c.change_id)}</td>")
        parts.append(f"<td>{_status_badge(c.status)}</td>")
        parts.append(f"<td>{c.total_rounds}</td>")
        parts.append(f"<td>{_fmt_duration_html(c.duration_ms)}</td>")
        parts.append(f"<td>{_fmt_tokens_html(c.tokens)}</td>")
        parts.append(f"<td>{_fmt_cost_html(c.estimated_cost, c.cost_status)}</td>")
        parts.append(f"<td>{_html_escape(c.cost_status)}</td>")
        parts.append(f"<td>{_fmt_bool_html(c.first_pass_review)}</td>")
        parts.append(f"<td>{c.review_failures}</td>")
        parts.append(f"<td>{_fmt_bool_html(c.no_progress)}</td>")
        parts.append(f"<td>{_fmt_bool_html(c.max_rounds_exceeded)}</td>")
        parts.append(f"<td>{_fmt_bool_html(c.archive_failed)}</td>")
        parts.append(f"<td>{_fmt_bool_html(c.fast_check_failed)}</td>")
        parts.append("</tr>")

    parts.append("</tbody></table>")
    parts.append("</section>")
    return "\n".join(parts)


def _render_failure_breakdown_html(cm_list: list) -> str:
    """Render the failure breakdown section."""
    parts: list[str] = []
    parts.append('<section class="failures">')
    parts.append("<h2>Failure Breakdown</h2>")

    failed = [c for c in cm_list if c.status == "failed"]
    if not failed:
        parts.append('<p class="empty-state">No failures</p>')
        parts.append("</section>")
        return "\n".join(parts)

    parts.append("<ul>")
    for c in failed:
        reasons: list[str] = []
        if c.max_rounds_exceeded:
            reasons.append("max_rounds_exceeded")
        if c.archive_failed:
            reasons.append("archive_failed")
        if c.review_failures > 0:
            reasons.append(f"review_failures: {c.review_failures}")
        if not reasons:
            reasons.append("unknown")
        reason_str = ", ".join(reasons)
        parts.append(
            f"<li><strong>{_html_escape(c.change_id)}</strong>: "
            f"{_html_escape(reason_str)}</li>"
        )
    parts.append("</ul>")
    parts.append("</section>")
    return "\n".join(parts)


def _render_cost_breakdown_html(pm) -> str:
    """Render the cost breakdown bar section."""
    parts: list[str] = []
    parts.append('<section class="cost-breakdown">')
    parts.append("<h2>Cost Breakdown</h2>")

    total = pm.estimated_cost_changes + pm.unresolved_cost_changes + pm.unknown_cost_changes
    if total == 0:
        parts.append('<p class="empty-state">No cost data available.</p>')
        parts.append("</section>")
        return "\n".join(parts)

    # Calculate percentages
    pct_est = pm.estimated_cost_changes / total * 100
    pct_unr = pm.unresolved_cost_changes / total * 100
    pct_unk = pm.unknown_cost_changes / total * 100

    parts.append('<div class="bar-container">')
    if pm.estimated_cost_changes > 0:
        parts.append(
            f'<div class="bar-segment bar-estimated" style="width:{pct_est:.1f}%">'
            f'{pm.estimated_cost_changes}</div>'
        )
    if pm.unresolved_cost_changes > 0:
        parts.append(
            f'<div class="bar-segment bar-unresolved" style="width:{pct_unr:.1f}%">'
            f'{pm.unresolved_cost_changes}</div>'
        )
    if pm.unknown_cost_changes > 0:
        parts.append(
            f'<div class="bar-segment bar-unknown" style="width:{pct_unk:.1f}%">'
            f'{pm.unknown_cost_changes}</div>'
        )
    parts.append("</div>")

    parts.append('<div class="bar-legend">')
    parts.append(
        f'<div class="bar-legend-item">'
        f'<div class="bar-legend-swatch" style="background:var(--green)"></div>'
        f'Estimated ({pm.estimated_cost_changes})'
        f'</div>'
    )
    parts.append(
        f'<div class="bar-legend-item">'
        f'<div class="bar-legend-swatch" style="background:var(--amber)"></div>'
        f'Unresolved ({pm.unresolved_cost_changes})'
        f'</div>'
    )
    parts.append(
        f'<div class="bar-legend-item">'
        f'<div class="bar-legend-swatch" style="background:var(--gray)"></div>'
        f'Unknown ({pm.unknown_cost_changes})'
        f'</div>'
    )
    parts.append("</div>")

    parts.append("</section>")
    return "\n".join(parts)


def _render_rounds_histogram_html(cm_list: list) -> str:
    """Render the rounds histogram section."""
    parts: list[str] = []
    parts.append('<section class="histogram-section">')
    parts.append("<h2>Rounds Histogram</h2>")

    completed = [c for c in cm_list if c.status == "completed"]
    if not completed:
        parts.append(
            '<p class="empty-state">No completed changes for histogram.</p>'
        )
        parts.append("</section>")
        return "\n".join(parts)

    # Build frequency map
    freq: dict[int, int] = {}
    for c in completed:
        rnd = c.total_rounds
        freq[rnd] = freq.get(rnd, 0) + 1

    max_count = max(freq.values())
    max_round = max(freq.keys())

    parts.append('<div class="histogram">')
    for r in range(1, max_round + 1):
        count = freq.get(r, 0)
        height_pct = (count / max_count * 100) if max_count > 0 else 0
        parts.append(
            f'<div class="histogram-bar-wrapper">'
            f'<div class="histogram-count">{count}</div>'
            f'<div class="histogram-bar" style="height:{height_pct:.1f}%"></div>'
            f'<div class="histogram-label">{r}</div>'
            f'</div>'
        )
    parts.append("</div>")

    parts.append("</section>")
    return "\n".join(parts)


def _render_timeline_html(records: list[dict]) -> str:
    """Render the stage timeline HTML section."""
    parts: list[str] = []
    parts.append('<section class="timeline">')
    parts.append("<h2>Stage Timeline</h2>")

    if not records:
        parts.append('<p class="empty-state">No stage records available.</p>')
        parts.append("</section>")
        return "\n".join(parts)

    # Sort by started_at ascending
    sorted_records = sorted(
        records,
        key=lambda r: r.get("started_at", ""),
    )

    parts.append("<table><thead><tr>")
    headers = ["Change ID", "Stage", "Round", "Started At", "Duration", "Status"]
    for h in headers:
        parts.append(f"<th>{_html_escape(h)}</th>")
    parts.append("</tr></thead><tbody>")

    for r in sorted_records:
        cid = r.get("change_id", "")
        stage = r.get("stage", "")
        rnd = r.get("round", "")
        started = r.get("started_at", "")
        dur = r.get("duration_ms")
        status = r.get("status", "")

        parts.append("<tr>")
        parts.append(f"<td>{_html_escape(cid)}</td>")
        parts.append(f"<td>{_html_escape(stage)}</td>")
        parts.append(f"<td>{rnd}</td>")
        parts.append(f"<td>{_html_escape(started)}</td>")
        parts.append(f"<td>{_fmt_duration_html(dur)}</td>")
        parts.append(f"<td>{_stage_status_badge(status)}</td>")
        parts.append("</tr>")

    parts.append("</tbody></table>")
    parts.append("</section>")
    return "\n".join(parts)


def _render_warnings_html(warnings: list[str]) -> str:
    """Render the warnings section."""
    if not warnings:
        return ""
    parts: list[str] = []
    parts.append('<section class="warnings">')
    parts.append(f"<h2>Warnings ({len(warnings)})</h2>")
    parts.append('<ul class="warnings-list">')
    for w in warnings:
        parts.append(f"<li>{_html_escape(w)}</li>")
    parts.append("</ul>")
    parts.append("</section>")
    return "\n".join(parts)


def _render_dashboard_html(
    result,
    plan_name: str,
    run_id: str,
    change_id: str | None = None,
    timeline_records: list[dict] | None = None,
    filters: dict | None = None,
) -> str:
    """Render the complete HTML dashboard as a self-contained document."""
    if filters is None:
        filters = {}
    if timeline_records is None:
        timeline_records = []

    pm = result.plan_metrics
    cm_list = result.change_metrics

    parts: list[str] = []
    parts.append("<!DOCTYPE html>")
    parts.append('<html lang="en">')
    parts.append("<head>")
    parts.append('<meta charset="utf-8">')
    parts.append('<meta name="viewport" content="width=device-width, initial-scale=1.0">')
    parts.append(
        f"<title>Plan Dashboard: {_html_escape(plan_name)}</title>"
    )
    parts.append(f"<style>{_DASHBOARD_CSS}</style>")
    parts.append("</head>")
    parts.append("<body>")
    parts.append(
        f"<header><h1>opsx-plan Dashboard: "
        f"{_html_escape(plan_name)}</h1></header>"
    )
    parts.append("<main>")

    # 1. Plan Summary Header
    parts.append(_render_plan_summary_html(pm, plan_name, run_id, filters))

    # 2. Model Leaderboard Table
    parts.append(_render_leaderboard_html(result.model_leaderboard))

    # 3. Per-Change Table
    parts.append(_render_change_table_html(cm_list))

    # 4. Failure Breakdown
    parts.append(_render_failure_breakdown_html(cm_list))

    # 5. Cost Breakdown
    parts.append(_render_cost_breakdown_html(pm))

    # 6. Rounds Histogram
    parts.append(_render_rounds_histogram_html(cm_list))

    # 7. Stage Timeline
    parts.append(_render_timeline_html(timeline_records))

    # Warnings
    if result.warnings:
        parts.append(_render_warnings_html(result.warnings))

    parts.append("</main>")
    parts.append("</body>")
    parts.append("</html>")

    return "\n".join(parts) + "\n"


def cmd_dashboard(args: argparse.Namespace) -> int:
    """opsx-plan dashboard <plan> [--output <path>] [--run-id <id>]
       [--change <id>] [--for-change <id>]"""
    repo = Path(args.repo).resolve()
    for_change_plan = report._resolve_for_change_plan(
        repo, getattr(args, "for_change", None), args.plan,
    )
    plan_src = for_change_plan if for_change_plan is not None else planref.resolve_plan(repo, args.plan)
    from lib.metrics.aggregator import (
        AggregationError,
        _build_leaderboard,
        _change_aggregation,
        _read_state,
        _read_telemetry,
        _select_run,
        aggregate,
    )

    for_change = getattr(args, "for_change", None)
    if for_change and for_change_plan == f"run-{for_change}":
        plan_name = for_change_plan
        cfg = {"name": plan_name, "adapter": "opencode", "changes": {}}
    else:
        cfg = planref.load_plan(planref._resolve_plan_path(repo, plan_src), repo=repo)
        plan_name = cfg["name"]
    run_id = args.run_id if args.run_id else None

    try:
        result = aggregate(repo, plan_name, run_id)
    except AggregationError as exc:
        print(f"dashboard error: {exc}", file=sys.stderr)
        return 2

    selected_run_id = result.plan_metrics.run_id or run_id or ""
    filters = {
        "change": args.change,
        "run_id": run_id,
    }

    # -- Determine output path ------------------------------------------------
    if args.output:
        output_path = Path(args.output).resolve()
    else:
        output_path = (
            repo / ".opsx-plan" / "dashboards" / f"{plan_name}.html"
        ).resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)

    # -- Gather timeline records ----------------------------------------------
    records, _ = _read_telemetry(repo, plan_name)
    selected_records, selected_run, _ = _select_run(records, run_id)

    # -- Apply --change filter ------------------------------------------------
    if args.change:
        # Keep unfiltered plan_metrics for the summary header
        # But filter change_metrics and rebuild leaderboard
        unfiltered_result = result
        result.change_metrics = [
            c for c in result.change_metrics if c.change_id == args.change
        ]

        # Rebuild leaderboard scoped to just this change
        change_records = [
            r for r in selected_records
            if r.get("change_id") == args.change
        ]
        state_for_lb, _ = _read_state(repo, plan_name)
        cm_list, _ = _change_aggregation(
            state_for_lb, change_records, plan_name, [],
        )
        result.model_leaderboard = _build_leaderboard(cm_list, change_records)

        # Narrow timeline to this change
        timeline_records = change_records
    else:
        timeline_records = selected_records

    # -- Render and write HTML ------------------------------------------------
    html = _render_dashboard_html(
        result,
        plan_name,
        selected_run_id,
        change_id=args.change,
        timeline_records=timeline_records,
        filters=filters,
    )

    # Atomic write
    tmp_path = output_path.with_suffix(output_path.suffix + ".tmp")
    try:
        with open(tmp_path, "w", encoding="utf-8") as fh:
            fh.write(html)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_path, output_path)
    except OSError as exc:
        print(f"dashboard error: failed to write {output_path}: {exc}",
              file=sys.stderr)
        return 2

    print(f"Dashboard written to: {output_path}")
    return 0
