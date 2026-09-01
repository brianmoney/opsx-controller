"""``opsx-plan logs`` command handler.

Moved verbatim from the ``orchestrator/opsx-plan.py`` entrypoint, together
with the private ``_describe_filters`` / ``_tail_log`` / ``_follow_log``
helpers only it uses (design D2). The check/selection functions it calls
live in the ``logs`` package module.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from lib.orchestrator import logs, planref


def cmd_logs(args: argparse.Namespace) -> int:
    """opsx-plan logs [plan] [--change <id>] [--stage <stage>] [--list] [--follow]"""
    repo = Path(args.repo).resolve()
    plan_src = planref.resolve_plan(repo, args.plan)
    cfg = planref.load_plan(planref._resolve_plan_path(repo, plan_src), repo=repo)
    plan_name = cfg["name"]

    change_filter = args.change if args.change else None
    stage_filter = args.stage if args.stage else None
    plan_change_ids: set[str] = set(cfg["changes"].keys())

    if args.list:
        entries = logs._collect_filtered_logs(repo, change_filter, stage_filter,
                                         plan_change_ids=plan_change_ids)
        if not entries:
            filters_desc = _describe_filters(change_filter, stage_filter, plan_name)
            print(f"No matching logs found{filters_desc}.")
            return 0
        print(f"Logs for plan '{plan_name}'" +
              (_describe_filters(change_filter, stage_filter, "")) +
              ":")
        for entry in entries:
            print(f"  {entry['path']}")
        return 0

    selected = logs._select_log(repo, plan_name, change_filter, stage_filter,
                           plan_change_ids=plan_change_ids)
    if selected is None:
        filters_desc = _describe_filters(change_filter, stage_filter, plan_name)
        print(f"No matching log found{filters_desc}.", file=sys.stderr)
        return 1

    log_path = Path(selected["path"])
    print(f"==> {log_path} <==")
    if args.follow:
        _follow_log(log_path)
    else:
        _tail_log(log_path)
    return 0


def _describe_filters(change_filter: str | None, stage_filter: str | None,
                      plan_name: str) -> str:
    """Build a human-readable filter description for error messages."""
    parts: list[str] = []
    if change_filter is not None:
        parts.append(f"change '{change_filter}'")
    if stage_filter is not None:
        parts.append(f"stage '{stage_filter}'")
    if not parts:
        return f" for plan '{plan_name}'" if plan_name else ""
    plan_part = f" (plan: {plan_name})" if plan_name else ""
    return f" for {', '.join(parts)}{plan_part}"


def _tail_log(log_path: Path, lines: int = 20) -> None:
    """Print the last *lines* lines of *log_path*."""
    import io as _io
    try:
        # Read the last N lines efficiently for large files.
        with open(log_path, "rb") as fh:
            # Seek to end and read backwards.
            buf_size = 4096
            fh.seek(0, _io.SEEK_END)
            file_size = fh.tell()
            if file_size == 0:
                return
            collected: list[bytes] = []
            remaining_lines = lines
            pos = file_size
            while pos > 0 and remaining_lines > 0:
                read_size = min(buf_size, pos)
                pos -= read_size
                fh.seek(pos)
                chunk = fh.read(read_size)
                collected.append(chunk)
                remaining_lines -= chunk.count(b"\n")
            data = b"".join(reversed(collected))
            text = data.decode("utf-8", errors="replace")
            # Keep only the last *lines* lines.
            all_lines = text.splitlines()
            tail_lines = all_lines[-lines:] if len(all_lines) > lines else all_lines
            for line in tail_lines:
                print(line)
    except OSError as exc:
        print(f"error: cannot read {log_path}: {exc}", file=sys.stderr)


def _follow_log(log_path: Path) -> None:
    """Follow *log_path* like ``tail -f``, forwarding new lines to stdout."""
    try:
        with open(log_path, "r", encoding="utf-8", errors="replace") as fh:
            # Start at end
            fh.seek(0, 2)
            while True:
                line = fh.readline()
                if line:
                    print(line, end="", flush=True)
                else:
                    time.sleep(0.25)
    except KeyboardInterrupt:
        pass
    except OSError as exc:
        print(f"error: cannot follow {log_path}: {exc}", file=sys.stderr)
