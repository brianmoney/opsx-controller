"""Report command: table and JSON formatting for `opsx-plan report`.

Depends on `base` and `planref` for plan resolution; reads metrics via
`lib.metrics.aggregator`.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from lib.orchestrator import base, planref

def _fmt_duration(ms: int | float | None) -> str:
    """Format milliseconds as human-readable duration, e.g. '1m30s' or '—'."""
    if ms is None:
        return "—"
    total_s = int(ms) // 1000
    minutes = total_s // 60
    seconds = total_s % 60
    return f"{minutes}m{seconds}s"


def _fmt_tokens(n: int | float | None) -> str:
    """Format token count with K/M suffix, e.g. '1.2K', '3.5M', or '—'."""
    if n is None:
        return "—"
    n = int(n)
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}K"
    return str(n)


def _fmt_cost(estimated_cost: float | None, cost_status: str) -> str:
    """Format cost as '$X.XX', 'unresolved', 'unavailable', or '—'."""
    if cost_status == "unavailable":
        return "unavailable"
    if cost_status == "unresolved":
        return "unresolved"
    if estimated_cost is not None:
        return f"${estimated_cost:.2f}"
    return "—"


def _fmt_pct(val: float | None) -> str:
    """Format a 0–1 fraction as percentage string, or '—'."""
    if val is None:
        return "—"
    return f"{val * 100:.1f}%"


def _fmt_bool(val: bool | None) -> str:
    """Format optional boolean as 'yes', 'no', or '—'."""
    if val is None:
        return "—"
    return "yes" if val else "no"


def _truncate_id(s: str | None, max_len: int = 30) -> str:
    """Truncate long identifiers with '…'."""
    if s is None:
        return "—"
    if len(s) <= max_len:
        return s
    return s[: max_len - 1] + "…"


def _col_widths(headers: list[str], rows: list[list[str]],
                min_widths: dict[int, int] | None = None) -> list[int]:
    """Compute column widths that accommodate headers and all row values."""
    widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(cell))
    if min_widths:
        for i, w in min_widths.items():
            widths[i] = max(widths[i], w)
    return widths


def _print_plan_summary(pm, plan_name: str, run_id: str,
                        filters: dict) -> None:
    """Print plan summary section."""
    print("=== Plan Summary ===")

    # Show active filters
    active = {k: v for k, v in filters.items() if v}
    if active:
        parts = [f"{k}={v}" for k, v in active.items()]
        print(f"Filters: {', '.join(parts)}")

    print(f"Plan:       {plan_name}")
    print(f"Run:        {run_id or '—'}")
    print(
        f"Changes:    {pm.total_changes} total "
        f"({pm.completed_changes} completed, "
        f"{pm.failed_changes} failed, "
        f"{pm.blocked_changes} blocked, "
        f"{pm.incomplete_changes} incomplete)"
    )
    print(f"Completion: {_fmt_pct(pm.completion_rate)}")
    print(f"Success:    {_fmt_pct(pm.success_rate)}")
    print(f"Duration:   {_fmt_duration(pm.total_duration_ms)}")
    print(f"Tokens:     {_fmt_tokens(pm.total_tokens)}")

    if pm.total_estimated_cost is not None:
        cost_str = _fmt_cost(pm.total_estimated_cost,
                             "estimated" if pm.unresolved_cost_changes == 0 else "partial")
    elif pm.unresolved_cost_changes > 0:
        cost_str = "unresolved"
    else:
        cost_str = "—"
    print(
        f"Cost:       {cost_str} "
        f"({pm.estimated_cost_changes} estimated, "
        f"{pm.unresolved_cost_changes} unresolved, "
        f"{pm.unknown_cost_changes} unknown)"
    )

    if pm.total_changes == 0:
        print("\nNo telemetry records found.")


def _print_change_table(cm_list: list) -> None:
    """Print per-change metrics table."""
    if not cm_list:
        print("\n=== Per-Change Metrics ===\n"
              "  No change metrics available.")
        return

    headers = [
        "Change ID", "Status", "Rnds", "Duration", "Tokens",
        "Cost", "Cost Status", "First Pass", "Rev Fails",
        "No Prog", "Max Rnd", "Arch Fail", "Fast Chk",
    ]

    rows: list[list[str]] = []
    for c in cm_list:
        rows.append([
            _truncate_id(c.change_id),
            c.status,
            str(c.total_rounds),
            _fmt_duration(c.duration_ms),
            _fmt_tokens(c.tokens),
            _fmt_cost(c.estimated_cost, c.cost_status),
            c.cost_status,
            _fmt_bool(c.first_pass_review),
            str(c.review_failures),
            _fmt_bool(c.no_progress),
            _fmt_bool(c.max_rounds_exceeded),
            _fmt_bool(c.archive_failed),
            _fmt_bool(c.fast_check_failed),
        ])

    widths = _col_widths(headers, rows, {0: 12, 1: 12})
    fmt = "  " + "  ".join(
        f"{{:<{w}}}" for w in widths
    )

    print("\n=== Per-Change Metrics ===")
    print(fmt.format(*headers))
    print(fmt.format(*["-" * w for w in widths]))
    for row in rows:
        print(fmt.format(*row))


def _print_manual_follow_up(cm_list: list) -> None:
    """Print the operator manual-task checklist for archived changes."""
    entries = [
        (c.change_id, c.manual_tasks_pending)
        for c in cm_list
        if getattr(c, "manual_tasks_pending", None)
    ]
    if not entries:
        return
    print("\n=== Manual Follow-Up (operator checklist) ===")
    for cid, tasks in entries:
        print(f"  {cid}:")
        for task in tasks:
            print(f"    - {task}")


def _print_stage_aggregates(sa, stage_filter: str | None = None) -> None:
    """Print stage aggregates section."""
    print("\n=== Stage Aggregates ===")

    def _line(label: str, value: str) -> None:
        print(f"  {label.ljust(22)} {value}")

    if stage_filter is None:
        _line("Average Rounds:", (
            f"{sa.average_rounds:.1f}" if sa.average_rounds is not None else "—"
        ))
        _line("Median Rounds:", (
            f"{sa.median_rounds:.1f}" if sa.median_rounds is not None else "—"
        ))
        _line("Avg Implement Duration:", _fmt_duration(sa.average_duration_implement))
        _line("Avg Review Duration:", _fmt_duration(sa.average_duration_review))
        _line("Avg Archive Duration:", _fmt_duration(sa.average_duration_archive))
        _line("Review Failure Rate:", _fmt_pct(sa.review_failure_rate))
        _line("Avg Tokens / Change:", _fmt_tokens(sa.average_tokens_per_change))
        _line("Avg Cost / Change:", (
            f"${sa.average_cost_per_change:.2f}"
            if sa.average_cost_per_change is not None else "—"
        ))
    else:
        if stage_filter == "implement":
            _line("Avg Implement Duration:", _fmt_duration(sa.average_duration_implement))
        elif stage_filter == "review":
            _line("Avg Review Duration:", _fmt_duration(sa.average_duration_review))
            _line("Review Failure Rate:", _fmt_pct(sa.review_failure_rate))
        elif stage_filter == "archive":
            _line("Avg Archive Duration:", _fmt_duration(sa.average_duration_archive))
        _line("Average Rounds:", (
            f"{sa.average_rounds:.1f}" if sa.average_rounds is not None else "—"
        ))
        _line("Avg Tokens / Change:", _fmt_tokens(sa.average_tokens_per_change))
        _line("Avg Cost / Change:", (
            f"${sa.average_cost_per_change:.2f}"
            if sa.average_cost_per_change is not None else "—"
        ))


def _print_model_leaderboard(ml: list) -> None:
    """Print model leaderboard table."""
    if not ml:
        print("\n=== Model Leaderboard ===\n"
              "  No leaderboard entries.")
        return

    headers = [
        "Implementer", "Reviewer", "Archiver",
        "Changes", "Success", "1st Pass",
        "Avg Rnds", "Avg Dur", "Avg Tokens", "Avg Cost",
    ]

    rows: list[list[str]] = []
    for e in ml:
        rows.append([
            _truncate_id(e.implementer_model),
            _truncate_id(e.reviewer_model),
            _truncate_id(e.archiver_model),
            str(e.change_count),
            _fmt_pct(e.success_rate),
            _fmt_pct(e.first_pass_rate),
            (f"{e.average_rounds:.1f}" if e.average_rounds is not None else "—"),
            _fmt_duration(e.average_duration_ms),
            _fmt_tokens(e.average_tokens),
            _fmt_cost(e.average_cost,
                      "estimated" if e.average_cost is not None else "unavailable"),
        ])

    widths = _col_widths(headers, rows, {0: 14, 1: 12, 2: 12})
    fmt = "  " + "  ".join(f"{{:<{w}}}" for w in widths)

    print("\n=== Model Leaderboard ===")
    print(fmt.format(*headers))
    print(fmt.format(*["-" * w for w in widths]))
    for row in rows:
        print(fmt.format(*row))


def _change_model_keys(records: list[dict], change_id: str) -> set:
    """Return the set of 'provider:model_id' keys used by a change."""
    keys: set[str] = set()
    for r in records:
        if r.get("change_id") != change_id:
            continue
        model = r.get("model", {})
        provider = (model.get("provider") or "").strip()
        model_id = (model.get("model_id") or "").strip()
        if provider and model_id:
            keys.add(f"{provider}:{model_id}")
    return keys


def _leaderboard_matches_model(entry, substring: str) -> bool:
    """Check if any role's model contains *substring* (case-insensitive)."""
    sub = substring.lower()
    for model in (entry.implementer_model, entry.reviewer_model,
                  entry.archiver_model):
        if model and sub in model.lower():
            return True
    return False


def _leaderboard_matches_stage(entry, stage: str) -> bool:
    """Check if the entry has a model for the requested *stage* role."""
    role_map = {
        "implement": entry.implementer_model,
        "review": entry.reviewer_model,
        "archive": entry.archiver_model,
    }
    return role_map.get(stage) is not None


def _leaderboard_involves_change(entry, model_keys: set) -> bool:
    """Check if any role's model is in *model_keys*."""
    for model in (entry.implementer_model, entry.reviewer_model,
                  entry.archiver_model):
        if model and model in model_keys:
            return True
    return False


def _dataclass_to_dict(obj) -> dict:
    """Serialize a dataclass instance to a dict, preserving None as null."""
    import dataclasses
    if not dataclasses.is_dataclass(obj):
        return obj
    result = {}
    for field in dataclasses.fields(obj):
        value = getattr(obj, field.name)
        if dataclasses.is_dataclass(value):
            result[field.name] = _dataclass_to_dict(value)
        elif isinstance(value, list):
            result[field.name] = [
                _dataclass_to_dict(item) if dataclasses.is_dataclass(item) else item
                for item in value
            ]
        else:
            result[field.name] = value
    return result


def _print_report_json(result, plan_name: str, run_id: str,
                       filters: dict, warnings: list[str]) -> None:
    """Emit a single JSON object to stdout."""
    import dataclasses

    output = {
        "command": "opsx-plan report",
        "plan_name": plan_name,
        "run_id": run_id,
        "filters": filters,
        "plan_metrics": _dataclass_to_dict(result.plan_metrics),
        "change_metrics": [
            _dataclass_to_dict(c) for c in result.change_metrics
        ],
        "stage_aggregates": _dataclass_to_dict(result.stage_aggregates),
        "model_leaderboard": [
            _dataclass_to_dict(e) for e in result.model_leaderboard
        ],
        "warnings": warnings,
    }
    # Deterministic: sort keys, ensure_ascii=True for byte-identical output
    print(json.dumps(output, sort_keys=True, ensure_ascii=True))


def _resolve_for_change_plan(
    repo: Path, for_change: str | None, plan_arg: str | None
) -> str | None:
    """Resolve ``--for-change <id>`` to either a derived manifest path or a
    fallback plan name, without loading the plan.

    Returns the plan source string to use with ``resolve_plan``, or ``None``
    when *for_change* is ``None`` (caller should follow its normal plan
    resolution path).  Raises ``PlanError`` when *for_change* is given but
    neither the derived manifest nor the state file exists.
    """
    if not for_change:
        return None

    if plan_arg is not None:
        raise base.PlanError(
            "--for-change and a positional plan argument are mutually "
            "exclusive; pick one"
        )

    manifest = planref.single_change_manifest_path(repo, for_change)
    if manifest.is_file():
        return str(manifest.relative_to(repo))

    state = repo / ".opsx-plan" / f"run-{for_change}.state.json"
    if state.is_file():
        return f"run-{for_change}"

    raise base.PlanError(
        f"--for-change {for_change}: no derived manifest "
        f"(.opsx-plan/plans/run-{for_change}.toml) or state file "
        f"(.opsx-plan/run-{for_change}.state.json) found; "
        f"has `opsx-run {for_change}` been invoked yet?"
    )


def cmd_report(args: argparse.Namespace) -> int:
    """opsx-plan report <plan> [--json] [--change <id>] [--run-id <id>]
       [--stage <stage>] [--model <substr>] [--for-change <id>]"""
    repo = Path(args.repo).resolve()
    for_change_plan = _resolve_for_change_plan(
        repo, getattr(args, "for_change", None), args.plan,
    )
    plan_src = for_change_plan     if for_change_plan is not None else planref.resolve_plan(repo, args.plan)

    from lib.metrics.aggregator import (
        AggregationError,
        _build_leaderboard,
        _change_aggregation,
        _read_state,
        _read_telemetry,
        _select_run,
        aggregate,
    )

    # When --for-change resolves through the state-file fallback (no manifest
    # on disk), the plan source string is a bare name (e.g. "run-<id>") that
    # does not correspond to a loadable TOML file.  In that case we skip
    # load_plan and use the name directly so report still works.
    for_change = getattr(args, "for_change", None)
    if for_change and for_change_plan == f"run-{for_change}":
        plan_name = for_change_plan
        cfg = {"name": plan_name, "adapter": "opencode", "changes": {}}
    else:
        cfg = planref.load_plan(planref._resolve_plan_path(repo, plan_src), repo=repo)
        plan_name = cfg["name"]
    run_id = args.run_id if args.run_id else None

    # Validate --stage early
    if args.stage and args.stage not in {"implement", "review", "archive"}:
        print(
            f"error: invalid stage '{args.stage}'; "
            f"valid: implement, review, archive",
            file=sys.stderr,
        )
        return 2

    try:
        result = aggregate(repo, plan_name, run_id)
    except AggregationError as exc:
        print(f"report error: {exc}", file=sys.stderr)
        return 2

    all_warnings = list(result.warnings)

    # -- Apply filters --------------------------------------------------------

    if args.change:
        # Filter change_metrics to the matching change
        result.change_metrics = [
            c for c in result.change_metrics if c.change_id == args.change
        ]

        # Rebuild leaderboard scoped to just this change
        records, _ = _read_telemetry(repo, plan_name)
        selected_records, _, _ = _select_run(records, run_id)
        change_records = [
            r for r in selected_records
            if r.get("change_id") == args.change
        ]
        state_for_lb, _ = _read_state(repo, plan_name)
        cm_list, _ = _change_aggregation(
            state_for_lb, change_records, plan_name, [],
        )
        result.model_leaderboard = _build_leaderboard(
            cm_list, change_records,
        )

    if args.stage:
        # Filter leaderboard to entries with a model for the specified stage
        result.model_leaderboard = [
            e for e in result.model_leaderboard
            if _leaderboard_matches_stage(e, args.stage)
        ]

    if args.model:
        result.model_leaderboard = [
            e for e in result.model_leaderboard
            if _leaderboard_matches_model(e, args.model)
        ]

    filters = {
        "change": args.change,
        "run_id": run_id,
        "stage": args.stage,
        "model": args.model,
    }

    # -- Output ---------------------------------------------------------------

    selected_run_id = result.plan_metrics.run_id or run_id or ""

    if args.json:
        _print_report_json(result, plan_name, selected_run_id, filters,
                           all_warnings)
    else:
        # Show active filter header
        active = {k: v for k, v in filters.items() if v}
        if active:
            parts = [f"{k}={v}" for k, v in active.items()]
            print(f"[Filters: {', '.join(parts)}]")

        _print_plan_summary(result.plan_metrics, plan_name, selected_run_id,
                            filters)
        _print_change_table(result.change_metrics)
        _print_manual_follow_up(result.change_metrics)
        _print_stage_aggregates(result.stage_aggregates, args.stage)
        _print_model_leaderboard(result.model_leaderboard)

        # Warnings section
        if all_warnings:
            print(f"\n=== Warnings ({len(all_warnings)}) ===")
            for w in all_warnings:
                print(f"  - {w}")

    return 0
