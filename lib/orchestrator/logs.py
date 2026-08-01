"""Log discovery, parsing, and selection for ``opsx-plan logs``."""

from __future__ import annotations

import re
from pathlib import Path

from lib.orchestrator import state as state_mod

_LOG_RE = re.compile(
    r"^(?P<change>[^.]+)\."
    r"(?:"
    r"(?P<stage_direct>[^.]+)\.r(?P<round>\d+)\.(?P<seq>\d+)\.log"
    r"|"
    r"(?P<legacy_stage>\D+)(?P<legacy_seq>\d+)\.log"
    r")$"
)


def _parse_log_name(filename: str) -> dict | None:
    """Parse a log filename into {change, stage, round, seq, path}.

    Handles two naming patterns:
    - Direct: ``<cid>.<stage>.r<round>.<seq>.log``
    - Legacy: ``<cid>.<stage><seq>.log`` (round is unknown, set to 0)
    """
    m = _LOG_RE.match(filename)
    if not m:
        return None
    change = m.group("change")
    if m.group("round") is not None:
        # Direct pattern
        stage = m.group("stage_direct")
        round_num = int(m.group("round"))
        seq = int(m.group("seq"))
    else:
        # Legacy pattern
        stage = m.group("legacy_stage")
        round_num = 0
        seq = int(m.group("legacy_seq"))
    return {"change": change, "stage": stage, "round": round_num,
            "seq": seq, "filename": filename}


def _collect_logs(repo: Path) -> list[dict]:
    """Scan ``.opsx-plan/logs/`` and return parsed log entries sorted by
    modification time descending (newest first)."""
    log_dir = repo / ".opsx-plan" / "logs"
    if not log_dir.is_dir():
        return []
    entries: list[dict] = []
    for path in sorted(log_dir.iterdir()):
        if not path.is_file():
            continue
        parsed = _parse_log_name(path.name)
        if parsed is None:
            continue
        parsed["path"] = str(path)
        parsed["mtime"] = path.stat().st_mtime
        entries.append(parsed)
    # Newest first by mtime, then by round desc, then by seq desc
    entries.sort(key=lambda e: (e["mtime"], e["round"], e["seq"]), reverse=True)
    return entries


def _select_log_from_state(
    repo: Path,
    plan_name: str,
    change_filter: str | None,
    stage_filter: str | None,
    plan_change_ids: set[str] | None = None,
) -> dict | None:
    """Try to select the default log from recorded plan state metadata.

    When *plan_change_ids* is provided and no explicit *change_filter* is
    given, only changes belonging to one of those change ids are considered.
    This scopes state-backed selection to the resolved plan.

    Returns a dict with keys ``path`` (str), ``change``, ``stage``,
    ``round``, ``seq``, or ``None`` when state does not identify a usable log.
    """
    state = state_mod.load_state(repo, plan_name)
    candidates: list[dict] = []
    for cid, record in state.get("changes", {}).items():
        if not isinstance(record, dict):
            continue
        # Scope to plan change ids when no explicit change_filter is given.
        if change_filter is None and plan_change_ids is not None:
            if cid not in plan_change_ids:
                continue
        ls = record.get("last_stage", {})
        if not isinstance(ls, dict):
            continue
        log_path = ls.get("log_path", "")
        if not log_path:
            continue
        p = Path(log_path)
        # When the path is relative, resolve against repo.
        if not p.is_absolute():
            p = repo / p
        if not p.is_file():
            continue
        parsed = _parse_log_name(p.name)
        if parsed is None:
            continue
        parsed["path"] = str(p)
        parsed["mtime"] = p.stat().st_mtime
        if change_filter is not None and parsed["change"] != change_filter:
            continue
        if stage_filter is not None and parsed["stage"] != stage_filter:
            continue
        candidates.append(parsed)
    if not candidates:
        return None
    # Newest first: prefer highest mtime, then highest round, then highest seq
    candidates.sort(key=lambda e: (e["mtime"], e["round"], e["seq"]), reverse=True)
    return candidates[0]


def _select_log_from_directory(
    repo: Path,
    change_filter: str | None,
    stage_filter: str | None,
    plan_change_ids: set[str] | None = None,
) -> dict | None:
    """Select the latest matching log from ``.opsx-plan/logs/`` via
    deterministic ordering.

    When *plan_change_ids* is provided and no explicit *change_filter* is
    given, only logs belonging to one of those change ids are considered.
    This scopes the fallback to the resolved plan.

    Returns a parsed log dict or ``None`` when no log matches.
    """
    entries = _collect_logs(repo)
    for entry in entries:
        if change_filter is not None:
            if entry["change"] != change_filter:
                continue
        elif plan_change_ids is not None and entry["change"] not in plan_change_ids:
            continue
        if stage_filter is not None and entry["stage"] != stage_filter:
            continue
        return entry
    return None


def _select_log(
    repo: Path,
    plan_name: str,
    change_filter: str | None,
    stage_filter: str | None,
    plan_change_ids: set[str] | None = None,
) -> dict | None:
    """Select the target log: state metadata first, then directory fallback.

    Returns a parsed log dict with at least ``path``, ``change``, ``stage``,
    or ``None`` when no matching log is found.
    """
    # When no filters are given, prefer recorded state.
    result = _select_log_from_state(repo, plan_name, change_filter, stage_filter,
                                    plan_change_ids=plan_change_ids)
    if result is not None:
        return result
    return _select_log_from_directory(repo, change_filter, stage_filter,
                                      plan_change_ids=plan_change_ids)


def _collect_filtered_logs(
    repo: Path,
    change_filter: str | None,
    stage_filter: str | None,
    plan_change_ids: set[str] | None = None,
) -> list[dict]:
    """Return all matching log entries sorted newest-first.

    When *plan_change_ids* is provided and no explicit *change_filter* is
    given, only logs belonging to one of those change ids are considered.
    This scopes ``--list`` output to the resolved plan.
    """
    entries = _collect_logs(repo)
    return [
        e for e in entries
        if (
            change_filter is not None
            and e["change"] == change_filter
            or (
                change_filter is None
                and (plan_change_ids is None or e["change"] in plan_change_ids)
            )
        )
        and (stage_filter is None or e["stage"] == stage_filter)
    ]
