"""Orchestrator state accessors (``.opsx-plan/<name>.state.json``)."""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path

from lib.orchestrator import base, groundtruth


def state_path(repo: Path, plan_name: str) -> Path:
    return repo / ".opsx-plan" / f"{plan_name}.state.json"


def default_context_cache() -> dict:
    return {
        "valid": False,
        "status": "missing",
        "compiled_by": "",
        "updated_in_round": 0,
        "source_signature": "",
        "source_paths": [],
        "refresh_reason": "",
        "change_summary": "",
        "scope_hint": "",
    }


def default_last_review() -> dict:
    return {
        "verdict": "pending",
        "finding_counts": {"critical": 0, "warning": 0, "note": 0},
        "summary": "",
        "fix_prompt": "",
    }


def default_archive_state() -> dict:
    return {
        "status": "not_started",
        "path": "",
        "commit": "",
        "reason": "",
        "spec_sync_status": "",
        "triage": {
            "scope_basis": "",
            "in_scope_files": [],
            "ambiguous_files": [],
            "retry_guidance": "",
            "retry_outlook": "unknown",
        },
    }


def default_last_stage() -> dict:
    return {
        "name": "",
        "round": 0,
        "outcome": "",
        "log_path": "",
        "updated_at": "",
    }


def new_change_record() -> dict:
    return {
        "status": base.PENDING,
        "attempts": 0,
        "reason": "",
        "updated_at": "",
        "create_attempts": 0,
        "created_by_orchestrator": False,
        "accepted": False,
        "phase": "implement",
        "round": 1,
        "max_rounds": 5,
        "no_progress_streak": 0,
        "latest_fix_prompt": "",
        "last_result": "",
        "task_counts": {"complete": 0, "total": 0},
        "manual_tasks_pending": [],
        "tracked_change_files": [],
        "context_cache": default_context_cache(),
        "last_review": default_last_review(),
        "archive": default_archive_state(),
        "history": [],
        "last_stage": default_last_stage(),
        "last_log": "",
        "telemetry": {"latest_telemetry": ""},
        "escalation": {"active": False, "activated_round": 0, "model": ""},
    }


def merge_defaults(target: dict, defaults: dict) -> dict:
    for key, value in defaults.items():
        if key not in target:
            target[key] = copy.deepcopy(value)
        elif isinstance(target[key], dict) and isinstance(value, dict):
            merge_defaults(target[key], value)
    return target


def _default_git_delivery_state() -> dict:
    return {
        "base_ref": None,
        "branch_name": None,
        "delivery_status": "disabled",
        "pull_request_url": None,
        "remote_name": None,
    }


def load_state(repo: Path, plan_name: str) -> dict:
    p = state_path(repo, plan_name)
    if p.exists():
        with open(p, encoding="utf-8") as fh:
            state = json.load(fh)
    else:
        state = {"plan": plan_name, "approvals": [], "changes": {}}
    state.setdefault("plan", plan_name)
    state.setdefault("approvals", [])
    state.setdefault("notified_events", {})
    state.setdefault("changes", {})
    state.setdefault("git_delivery", _default_git_delivery_state())
    gd = state["git_delivery"]
    if not isinstance(gd, dict):
        state["git_delivery"] = _default_git_delivery_state()
    else:
        for key, value in _default_git_delivery_state().items():
            gd.setdefault(key, value)
    for cid, record in state["changes"].items():
        if isinstance(record, dict):
            merge_defaults(record, new_change_record())
            record.setdefault("change", cid)
    return state


def save_state(repo: Path, plan_name: str, state: dict) -> None:
    p = state_path(repo, plan_name)
    p.parent.mkdir(parents=True, exist_ok=True)
    gi = p.parent / ".gitignore"
    if not gi.exists():  # orchestrator state is operational, never committed
        gi.write_text("*\n", encoding="utf-8")
    tmp = p.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(state, fh, indent=2)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, p)


def rec(state: dict, cid: str) -> dict:
    record = state["changes"].setdefault(cid, new_change_record())
    merge_defaults(record, new_change_record())
    return record


def set_status(state: dict, cid: str, status: str, reason: str = "") -> None:
    r = rec(state, cid)
    r["status"] = status
    r["reason"] = reason
    r["updated_at"] = base.utcnow()


def _change_tasks_path(repo: Path, cid: str) -> Path:
    """Locate the change's tasks.md wherever the change currently lives.

    Prefers the active change directory (``openspec/changes/<cid>/``); once a
    change has been archived, falls back to the archive directory under
    ``openspec/changes/archive/``.
    """
    direct = groundtruth.change_dir(repo, cid) / "tasks.md"
    if direct.is_file():
        return direct
    archived = groundtruth.find_archive_dir(repo, cid)
    if archived is not None and (archived / "tasks.md").is_file():
        return archived / "tasks.md"
    return direct


def change_tasks(repo: Path, cid: str) -> list[dict]:
    """Parse the change's tasks file into per-task records.

    Each record has ``id`` (the task text after the checkbox), ``done``
    (bool), and ``manual`` (bool, ``True`` when the line ends in the
    ``(manual)`` marker).
    """
    tasks = _change_tasks_path(repo, cid)
    records: list[dict] = []
    if not tasks.is_file():
        return records
    for line in tasks.read_text(encoding="utf-8").splitlines():
        match = base.TASK_RE.match(line)
        if not match:
            continue
        records.append(
            {
                "id": line[match.end():].rstrip(),
                "done": match.group("done").lower() == "x",
                "manual": base.classify_task_line(line) == "manual",
            }
        )
    return records


def remaining_automatable_tasks(repo: Path, cid: str) -> list[str]:
    """Ids of unchecked automatable tasks in the change's tasks file."""
    return [
        task["id"]
        for task in change_tasks(repo, cid)
        if not task["done"] and not task["manual"]
    ]


def pending_manual_tasks(repo: Path, cid: str) -> list[str]:
    """Ids of unchecked manual tasks in the change's tasks file."""
    return [
        task["id"]
        for task in change_tasks(repo, cid)
        if not task["done"] and task["manual"]
    ]


def change_task_counts(repo: Path, cid: str) -> dict:
    counts = {"complete": 0, "total": 0}
    for task in change_tasks(repo, cid):
        counts["total"] += 1
        if task["done"]:
            counts["complete"] += 1
    return counts


def update_task_counts(repo: Path, state: dict, cid: str) -> None:
    rec(state, cid)["task_counts"] = change_task_counts(repo, cid)


def merge_paths(*groups: list[str]) -> list[str]:
    merged: list[str] = []
    seen: set[str] = set()
    for group in groups:
        for path in group:
            if not path or path in seen:
                continue
            seen.add(path)
            merged.append(path)
    return merged


def change_context_paths(repo: Path, cid: str) -> list[str]:
    cdir = groundtruth.change_dir(repo, cid)
    if not cdir.is_dir():
        return []
    return sorted(str(path.relative_to(repo)) for path in cdir.rglob("*") if path.is_file())
