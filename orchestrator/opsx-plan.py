#!/usr/bin/env python3
"""opsx-plan: deterministic plan-level orchestrator for OpenSpec changes.

Iterates a TOML plan manifest of OpenSpec changes (a DAG). Depending on the
adapter, it either invokes a legacy single-command controller or owns the
implement/review/archive phase loop directly, verifies completion from ground
truth, and gates progress on configurable fast checks.

Design rules:
  - The orchestrator is deterministic. All LLM judgment lives inside the
    configured workers. This layer only does ordering, dispatch, and
    verification.
  - Never trust a worker or controller exit code or stdout as success. A
    change is done only when independent evidence agrees.
  - A failed change blocks its dependents; independent branches continue.
  - Changes with pause_before=true wait for explicit `approve`.
  - State is reconciled against the repository on startup, so the run can
    be killed and resumed at any time.

Requires Python 3.11+ (tomllib). Stdlib only.
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover
    sys.exit("opsx-plan requires Python 3.11+ (tomllib)")

# ---------------------------------------------------------------------------
# Adapter defaults. Both fields accept a {change} placeholder and may be
# overridden in the [plan] table. Verify the invoke command for your client
# version before an unattended run.
# ---------------------------------------------------------------------------
ADAPTER_DEFAULTS = {
    "opencode": {
        "invoke": 'opencode run "/opsx-drive {change}"',
        "state_file": ".opencode/opsx-controller/{change}.json",
        "implement_invoke": "opencode run --agent opsx-implementer",
        "review_invoke": "opencode run --agent opsx-reviewer",
        "archive_invoke": "opencode run --agent opsx-archiver",
    },
    "claude-code": {
        "invoke": 'claude -p "/opsx-drive {change}"',
        "state_file": ".claude/opsx-controller/{change}.json",
    },
    "codex-cli": {
        "invoke": 'codex exec "$opsx-drive {change}"',
        "state_file": ".opsx-controller/{change}.json",
    },
}

DONE = "done"
PENDING = "pending"
RUNNING = "running"
FAILED = "failed"
SKIPPED = "skipped"

ARCHIVE_DIR_RE = re.compile(r"^\d{4}-\d{2}-\d{2}-")
TASK_RE = re.compile(r"^- \[(?P<done>[ xX])\]\s+")
ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")

_current_proc: subprocess.Popen | None = None


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def log(msg: str) -> None:
    print(f"[opsx-plan {datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


# ---------------------------------------------------------------------------
# Plan manifest
# ---------------------------------------------------------------------------

class PlanError(Exception):
    pass


def load_plan(path: Path) -> dict:
    with open(path, "rb") as fh:
        raw = tomllib.load(fh)

    plan = raw.get("plan", {})
    changes = raw.get("changes", [])
    if not changes:
        raise PlanError("plan has no [[changes]] entries")

    adapter = plan.get("adapter", "opencode")
    if adapter not in ADAPTER_DEFAULTS and not (
        plan.get("invoke") and plan.get("state_file")
    ):
        raise PlanError(
            f"unknown adapter '{adapter}' and no invoke/state_file overrides given"
        )
    defaults = ADAPTER_DEFAULTS.get(adapter, {})

    cfg = {
        "name": plan.get("name") or path.stem,
        "adapter": adapter,
        "invoke": plan.get("invoke", defaults.get("invoke")),
        "state_file": plan.get("state_file", defaults.get("state_file")),
        "implement_invoke": plan.get(
            "implement_invoke", defaults.get("implement_invoke", "")
        ),
        "review_invoke": plan.get(
            "review_invoke", defaults.get("review_invoke", "")
        ),
        "archive_invoke": plan.get(
            "archive_invoke", defaults.get("archive_invoke", "")
        ),
        "timeout_minutes": float(plan.get("timeout_minutes", 90)),
        "max_attempts": int(plan.get("max_attempts", 2)),
        "max_rounds": int(plan.get("max_rounds", 5)),
        "no_progress_limit": int(plan.get("no_progress_limit", 2)),
        "fast_checks": list(plan.get("fast_checks", [])),
        "check_timeout_minutes": float(plan.get("check_timeout_minutes", 15)),
        "require_clean_tracked": bool(plan.get("require_clean_tracked", True)),
        # --- create stage (the /opsx-ff automation) ---
        "plan_doc": plan.get("plan_doc", ""),
        "create_invoke": plan.get("create_invoke", ""),
        "create_timeout_minutes": float(plan.get("create_timeout_minutes", 30)),
        "create_max_attempts": int(plan.get("create_max_attempts", 2)),
        "review_created": bool(plan.get("review_created", True)),
        "created_check": plan.get(
            "created_check", "openspec validate {change} --strict"
        ),
    }

    by_id: dict[str, dict] = {}
    for c in changes:
        cid = c.get("id")
        if not cid:
            raise PlanError("a [[changes]] entry is missing 'id'")
        if cid in by_id:
            raise PlanError(f"duplicate change id: {cid}")
        by_id[cid] = {
            "id": cid,
            "phase": c.get("phase"),
            "depends_on": list(c.get("depends_on", [])),
            "pause_before": bool(c.get("pause_before", False)),
            "enabled": bool(c.get("enabled", True)),
            "timeout_minutes": float(
                c.get("timeout_minutes", cfg["timeout_minutes"])
            ),
            "max_attempts": int(c.get("max_attempts", cfg["max_attempts"])),
            "create_invoke": c.get("create_invoke", cfg["create_invoke"]),
            "create_max_attempts": int(
                c.get("create_max_attempts", cfg["create_max_attempts"])
            ),
        }

    for c in by_id.values():
        for dep in c["depends_on"]:
            if dep not in by_id:
                raise PlanError(f"{c['id']}: unknown dependency '{dep}'")

    cfg["order"] = topo_sort(by_id)
    cfg["changes"] = by_id
    return cfg


def topo_sort(by_id: dict[str, dict]) -> list[str]:
    """Kahn's algorithm; deterministic (manifest order breaks ties)."""
    ids = list(by_id)
    indeg = {cid: len(by_id[cid]["depends_on"]) for cid in ids}
    dependents: dict[str, list[str]] = {cid: [] for cid in ids}
    for cid in ids:
        for dep in by_id[cid]["depends_on"]:
            dependents[dep].append(cid)

    queue = [cid for cid in ids if indeg[cid] == 0]
    order: list[str] = []
    while queue:
        cid = queue.pop(0)
        order.append(cid)
        for nxt in dependents[cid]:
            indeg[nxt] -= 1
            if indeg[nxt] == 0:
                queue.append(nxt)
    if len(order) != len(ids):
        cyclic = sorted(set(ids) - set(order))
        raise PlanError(f"dependency cycle involving: {', '.join(cyclic)}")
    return order


# ---------------------------------------------------------------------------
# Orchestrator state (.opsx-plan/<name>.state.json)
# ---------------------------------------------------------------------------

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
        "status": PENDING,
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
        "tracked_change_files": [],
        "context_cache": default_context_cache(),
        "last_review": default_last_review(),
        "archive": default_archive_state(),
        "history": [],
        "last_stage": default_last_stage(),
        "last_log": "",
    }


def merge_defaults(target: dict, defaults: dict) -> dict:
    for key, value in defaults.items():
        if key not in target:
            target[key] = copy.deepcopy(value)
        elif isinstance(target[key], dict) and isinstance(value, dict):
            merge_defaults(target[key], value)
    return target


def load_state(repo: Path, plan_name: str) -> dict:
    p = state_path(repo, plan_name)
    if p.exists():
        with open(p, encoding="utf-8") as fh:
            state = json.load(fh)
    else:
        state = {"plan": plan_name, "approvals": [], "changes": {}}
    state.setdefault("plan", plan_name)
    state.setdefault("approvals", [])
    state.setdefault("changes", {})
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
    r["updated_at"] = utcnow()


def is_direct_opencode(cfg: dict) -> bool:
    return cfg["adapter"] == "opencode" and all(
        cfg.get(name) for name in ("implement_invoke", "review_invoke", "archive_invoke")
    )


def change_task_counts(repo: Path, cid: str) -> dict:
    counts = {"complete": 0, "total": 0}
    tasks = change_dir(repo, cid) / "tasks.md"
    if not tasks.is_file():
        return counts
    for line in tasks.read_text(encoding="utf-8").splitlines():
        match = TASK_RE.match(line)
        if not match:
            continue
        counts["total"] += 1
        if match.group("done").lower() == "x":
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
    cdir = change_dir(repo, cid)
    if not cdir.is_dir():
        return []
    return sorted(str(path.relative_to(repo)) for path in cdir.rglob("*") if path.is_file())


def worker_state_path(repo: Path, plan_name: str, cid: str) -> Path:
    return repo / ".opsx-plan" / "workers" / plan_name / f"{cid}.json"


def save_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def save_worker_state(repo: Path, cfg: dict, state: dict, cid: str) -> None:
    if not is_direct_opencode(cfg):
        return
    r = rec(state, cid)
    payload = {
        "version": 3,
        "change": cid,
        "schema": "spec-driven",
        "status": (
            "completed" if r["status"] == DONE else "blocked"
            if r["status"] == FAILED else "running"
        ),
        "phase": r["phase"],
        "round": r["round"],
        "max_rounds": r["max_rounds"],
        "no_progress_streak": r["no_progress_streak"],
        "latest_fix_prompt": r["latest_fix_prompt"],
        "last_result": r["last_result"],
        "task_counts": r["task_counts"],
        "tracked_change_files": r["tracked_change_files"],
        "context_cache": r["context_cache"],
        "last_review": r["last_review"],
        "archive": r["archive"],
        "history": r["history"],
    }
    save_json(worker_state_path(repo, cfg["name"], cid), payload)


def persist_direct_state(repo: Path, cfg: dict, state: dict, cid: str) -> None:
    save_state(repo, cfg["name"], state)
    save_worker_state(repo, cfg, state, cid)


def single_line(value: str) -> str:
    compact = " ".join((value or "").split())
    return compact if compact else "none"


def build_worker_input(repo: Path, cfg: dict, state: dict, cid: str) -> str:
    r = rec(state, cid)
    update_task_counts(repo, state, cid)
    cache = r["context_cache"]
    return "\n".join(
        [
            f"CHANGE: {cid}",
            f"ROUND: {r['round']}",
            f"STATE_FILE: {worker_state_path(repo, cfg['name'], cid)}",
            f"LATEST_FIX_PROMPT: {single_line(r['latest_fix_prompt'])}",
            f"TASK_COUNTS: {r['task_counts']['complete']}/{r['task_counts']['total']}",
            f"CONTEXT_CACHE_STATUS: {cache['status']}",
            f"CONTEXT_CACHE_VALID: {'true' if cache['valid'] else 'false'}",
            f"CONTEXT_CACHE_SUMMARY: {single_line(cache['change_summary'])}",
        ]
    )


def next_stage_log_path(repo: Path, cid: str, stage: str, round_num: int) -> Path:
    log_dir = repo / ".opsx-plan" / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    existing = sorted(log_dir.glob(f"{cid}.{stage}.r{round_num}.*.log"))
    return log_dir / f"{cid}.{stage}.r{round_num}.{len(existing) + 1}.log"


def record_stage_log(
    state: dict,
    cid: str,
    stage: str,
    round_num: int,
    outcome: str,
    log_path: Path,
) -> None:
    r = rec(state, cid)
    r["last_log"] = str(log_path)
    r["last_stage"] = {
        "name": stage,
        "round": round_num,
        "outcome": outcome,
        "log_path": str(log_path),
        "updated_at": utcnow(),
    }


def parse_stage_json(log_path: Path) -> tuple[dict | None, str]:
    lines: list[str] = []
    for raw in log_path.read_text(encoding="utf-8").splitlines():
        stripped = ANSI_ESCAPE_RE.sub("", raw).strip()
        if not stripped or stripped.startswith("# "):
            continue
        lines.append(stripped)
    for candidate in reversed(lines):
        if not (candidate.startswith("{") and candidate.endswith("}")):
            continue
        try:
            payload = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if not isinstance(payload, dict):
            continue
        return payload, ""
    return None, f"expected a final JSON object line, got {len(lines)} non-comment lines"


def append_history(state: dict, cid: str, entry: dict) -> None:
    rec(state, cid)["history"].append(entry)


def reachable_commit(repo: Path, commit: str) -> bool:
    if not commit:
        return False
    res = git(repo, "merge-base", "--is-ancestor", commit, "HEAD")
    return res.returncode == 0


def verify_direct_archive_done(repo: Path, cid: str, record: dict) -> tuple[bool, str]:
    archive = record["archive"]
    if archive.get("status") != "passed":
        return False, "no fresh archive worker result recorded"
    if change_dir(repo, cid).exists():
        return False, f"openspec/changes/{cid} still exists"
    archive_path = archive.get("path", "")
    if not archive_path:
        return False, "archive worker did not record archive path"
    archive_dir = repo / archive_path
    if not archive_dir.is_dir():
        return False, f"archive path missing: {archive_path}"
    actual_archive = find_archive_dir(repo, cid)
    if actual_archive is None:
        return False, "no dated archive directory found"
    if actual_archive.resolve() != archive_dir.resolve():
        return False, (
            f"archive directory mismatch: expected {archive_path}, found "
            f"{actual_archive.relative_to(repo)}"
        )
    commit = archive.get("commit", "")
    if not commit:
        return False, "archive worker did not record archive commit"
    if not reachable_commit(repo, commit):
        return False, f"archive commit not reachable from HEAD: {commit}"
    if find_archive_commit(repo, cid) != commit:
        return False, "archive commit does not match latest archive(<change>) commit"
    return True, ""


def normalize_task_counts(payload: dict) -> dict:
    counts = payload.get("task_counts", {})
    if not isinstance(counts, dict):
        return {"complete": 0, "total": 0}
    return {
        "complete": int(counts.get("complete", 0)),
        "total": int(counts.get("total", 0)),
    }


def normalize_finding_counts(payload: dict) -> dict:
    counts = payload.get("finding_counts", {})
    if not isinstance(counts, dict):
        return {"critical": 0, "warning": 0, "note": 0}
    return {
        "critical": int(counts.get("critical", 0)),
        "warning": int(counts.get("warning", 0)),
        "note": int(counts.get("note", 0)),
    }


def invoke_direct_stage(
    repo: Path,
    cfg: dict,
    cid: str,
    stage: str,
    round_num: int,
    input_block: str,
) -> tuple[str, Path]:
    cmd = shlex.split(cfg[f"{stage}_invoke"]) + [input_block]
    log_path = next_stage_log_path(repo, cid, stage, round_num)
    timeout_s = cfg["changes"][cid]["timeout_minutes"] * 60
    log(
        f"  exec[{stage}]: {' '.join(cmd[:-1])} <input> "
        f"(timeout {timeout_s / 60:g}m, log {log_path})"
    )
    return run_logged_command(repo, cmd, log_path, timeout_s, stage, round_num)


def run_logged_command(
    repo: Path,
    cmd: list[str],
    log_path: Path,
    timeout_s: float,
    stage: str,
    attempt: int,
) -> tuple[str, Path]:
    global _current_proc
    try:
        with open(log_path, "w", encoding="utf-8") as lf:
            lf.write(f"# {utcnow()} {stage} attempt {attempt}: {' '.join(cmd)}\n")
            lf.flush()
            proc = subprocess.Popen(
                cmd,
                cwd=repo,
                stdout=lf,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
            _current_proc = proc
            try:
                proc.wait(timeout=timeout_s)
                return "exited", log_path
            except subprocess.TimeoutExpired:
                terminate_group(proc)
                return "timeout", log_path
            finally:
                _current_proc = None
    except FileNotFoundError:
        return "spawn_error", log_path


def apply_implement_result(
    repo: Path,
    cfg: dict,
    state: dict,
    cid: str,
    payload: dict,
) -> str:
    r = rec(state, cid)
    status = payload.get("status")
    if status == "blocked":
        r["last_result"] = "implement_blocked"
        update_task_counts(repo, state, cid)
        append_history(
            state,
            cid,
            {
                "round": r["round"],
                "phase": "implement",
                "status": "blocked",
                "summary": payload.get("summary", "implement blocked"),
                "reason": payload.get("reason", "implement blocked"),
            },
        )
        set_status(state, cid, FAILED, payload.get("reason", "implement blocked"))
        return "stop"
    if status != "implemented":
        set_status(state, cid, FAILED, f"implement returned unexpected status={status}")
        r["last_result"] = "implement_invalid"
        return "stop"
    r["task_counts"] = normalize_task_counts(payload)
    progress = bool(payload.get("progress_made"))
    r["no_progress_streak"] = 0 if progress else r["no_progress_streak"] + 1
    files_touched = [str(path) for path in payload.get("files_touched", [])]
    known_change_files = [str(path) for path in payload.get("known_change_files", [])]
    r["tracked_change_files"] = merge_paths(
        change_context_paths(repo, cid),
        r["tracked_change_files"],
        files_touched,
        known_change_files,
    )
    cache_update = payload.get("cache_update")
    if isinstance(cache_update, dict):
        cache = r["context_cache"]
        cache.update(
            {
                "valid": True,
                "status": "ready",
                "compiled_by": "opsx-implementer",
                "updated_in_round": r["round"],
                "change_summary": cache_update.get(
                    "change_summary", cache["change_summary"]
                ),
                "refresh_reason": cache_update.get(
                    "refresh_reason", cache["refresh_reason"]
                ),
                "source_paths": cache_update.get("source_paths", cache["source_paths"]),
                "scope_hint": cache_update.get("scope_hint", cache.get("scope_hint", "")),
            }
        )
    r["last_result"] = "implement_completed"
    append_history(
        state,
        cid,
        {
            "round": r["round"],
            "phase": "implement",
            "status": "implemented",
            "summary": payload.get("summary", "implementation round completed"),
            "progress_made": progress,
            "completed_tasks": payload.get("completed_tasks", []),
            "files_touched": files_touched,
        },
    )
    if r["no_progress_streak"] >= cfg["no_progress_limit"]:
        r["last_result"] = "no_progress"
        set_status(state, cid, FAILED, "no progress ceiling reached")
        return "stop"
    r["phase"] = "review"
    set_status(state, cid, PENDING, payload.get("summary", "implementation complete"))
    return "continue"


def apply_review_result(repo: Path, state: dict, cid: str, payload: dict) -> str:
    r = rec(state, cid)
    if payload.get("status") != "reviewed":
        set_status(
            state,
            cid,
            FAILED,
            f"review returned unexpected status={payload.get('status')}",
        )
        r["last_result"] = "review_invalid"
        return "stop"
    counts = normalize_finding_counts(payload)
    verdict = payload.get("verdict")
    summary = payload.get("summary", "review completed")
    fix_prompt = payload.get("fix_prompt", "")
    update_task_counts(repo, state, cid)
    r["last_review"] = {
        "verdict": verdict,
        "finding_counts": counts,
        "summary": summary,
        "fix_prompt": fix_prompt,
    }
    append_history(
        state,
        cid,
        {
            "round": r["round"],
            "phase": "review",
            "status": verdict,
            "summary": summary,
            "finding_counts": counts,
        },
    )
    passed = verdict == "pass" and counts == {"critical": 0, "warning": 0, "note": 0}
    if passed:
        r["latest_fix_prompt"] = ""
        r["last_result"] = "review_passed"
        r["phase"] = "archive"
        set_status(state, cid, PENDING, summary)
        return "continue"
    if verdict not in {"pass", "fail"}:
        set_status(state, cid, FAILED, f"review returned unexpected verdict={verdict}")
        r["last_result"] = "review_invalid"
        return "stop"
    r["latest_fix_prompt"] = fix_prompt
    if r["round"] >= r["max_rounds"]:
        r["last_result"] = "max_rounds_reached"
        set_status(state, cid, FAILED, "review retry budget exhausted")
        return "stop"
    r["last_result"] = "review_failed"
    r["round"] += 1
    r["phase"] = "implement"
    set_status(state, cid, PENDING, summary)
    return "continue"


def apply_archive_result(repo: Path, cfg: dict, state: dict, cid: str, payload: dict) -> str:
    r = rec(state, cid)
    archive = r["archive"]
    if payload.get("status") == "blocked":
        archive.update(
            {
                "status": "failed",
                "path": payload.get("archive_path", ""),
                "commit": payload.get("commit", ""),
                "reason": payload.get("reason", "archive blocked"),
                "spec_sync_status": payload.get("spec_sync_status", "not_started"),
                "triage": payload.get("triage", default_archive_state()["triage"]),
            }
        )
        r["last_result"] = "archive_failed"
        append_history(
            state,
            cid,
            {
                "round": r["round"],
                "phase": "archive",
                "status": "blocked",
                "summary": payload.get("summary", "archive blocked"),
                "reason": payload.get("reason", "archive blocked"),
            },
        )
        set_status(state, cid, FAILED, payload.get("reason", "archive blocked"))
        return "stop"
    if payload.get("status") != "archived":
        set_status(
            state,
            cid,
            FAILED,
            f"archive returned unexpected status={payload.get('status')}",
        )
        archive["status"] = "failed"
        archive["reason"] = "invalid archive output"
        r["last_result"] = "archive_invalid"
        return "stop"
    archive.update(
        {
            "status": "passed",
            "path": payload.get("archive_path", ""),
            "commit": payload.get("commit", ""),
            "reason": "",
            "spec_sync_status": payload.get("spec_sync_status", ""),
            "triage": default_archive_state()["triage"],
        }
    )
    append_history(
        state,
        cid,
        {
            "round": r["round"],
            "phase": "archive",
            "status": "archived",
            "summary": payload.get("summary", "archive completed"),
            "archive_path": archive["path"],
            "commit": archive["commit"],
        },
    )
    r["last_result"] = "archive_passed"
    ok, why = verify_direct_archive_done(repo, cid, r)
    if not ok:
        archive["status"] = "failed"
        archive["reason"] = why
        set_status(state, cid, FAILED, f"archive unverified: {why}")
        return "stop"
    checks_ok, check_why = run_fast_checks(repo, cfg)
    if not checks_ok:
        archive["status"] = "failed"
        archive["reason"] = f"post-archive {check_why}"
        r["last_result"] = "post_archive_check_failed"
        set_status(state, cid, FAILED, f"post-archive {check_why}")
        return "stop"
    r["phase"] = "done"
    set_status(state, cid, DONE, "verified + checks passed")
    return "done"


def run_direct_change(
    repo: Path,
    cfg: dict,
    state: dict,
    cid: str,
    budget_deadline: float | None = None,
) -> str:
    r = rec(state, cid)
    while True:
        if budget_deadline and time.monotonic() > budget_deadline:
            set_status(state, cid, PENDING, f"budget exhausted while waiting to run {r['phase']}")
            persist_direct_state(repo, cfg, state, cid)
            return "budget"
        stage = r["phase"]
        round_num = r["round"]
        if stage == "done":
            ok, why = verify_direct_archive_done(repo, cid, r)
            if ok:
                set_status(state, cid, DONE, "verified + checks passed")
            else:
                set_status(state, cid, FAILED, f"completed state no longer verifiable: {why}")
            persist_direct_state(repo, cfg, state, cid)
            return r["status"]
        if stage not in {"implement", "review", "archive"}:
            r["phase"] = "implement"
            stage = "implement"

        input_block = build_worker_input(repo, cfg, state, cid)
        set_status(state, cid, RUNNING, f"{stage} round {round_num}")
        persist_direct_state(repo, cfg, state, cid)
        outcome, log_path = invoke_direct_stage(repo, cfg, cid, stage, round_num, input_block)
        record_stage_log(state, cid, stage, round_num, outcome, log_path)

        if outcome == "spawn_error":
            rec(state, cid)["last_result"] = f"{stage}_spawn_error"
            set_status(state, cid, FAILED, f"could not spawn {stage}: {cfg[f'{stage}_invoke']}")
            persist_direct_state(repo, cfg, state, cid)
            return "spawn_error"
        if outcome == "timeout":
            rec(state, cid)["last_result"] = f"{stage}_timeout"
            set_status(state, cid, FAILED, f"{stage} timed out")
            persist_direct_state(repo, cfg, state, cid)
            return "failed"

        payload, parse_why = parse_stage_json(log_path)
        if payload is None:
            rec(state, cid)["last_result"] = "subagent_output_invalid"
            if stage == "archive":
                rec(state, cid)["archive"]["status"] = "failed"
                rec(state, cid)["archive"]["reason"] = parse_why
            set_status(state, cid, FAILED, f"{stage} output invalid: {parse_why}")
            persist_direct_state(repo, cfg, state, cid)
            return "failed"

        if stage == "implement":
            action = apply_implement_result(repo, cfg, state, cid, payload)
        elif stage == "review":
            action = apply_review_result(repo, state, cid, payload)
        else:
            action = apply_archive_result(repo, cfg, state, cid, payload)
        persist_direct_state(repo, cfg, state, cid)
        if action == "continue":
            continue
        return action


# ---------------------------------------------------------------------------
# Ground-truth verification
# ---------------------------------------------------------------------------

def git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True
    )


def read_controller_state(repo: Path, cfg: dict, cid: str) -> dict | None:
    p = repo / cfg["state_file"].format(change=cid)
    if not p.exists():
        return None
    try:
        with open(p, encoding="utf-8") as fh:
            return json.load(fh)
    except (json.JSONDecodeError, OSError):
        return {"_malformed": True}


def find_archive_dir(repo: Path, cid: str) -> Path | None:
    root = repo / "openspec" / "changes" / "archive"
    if not root.is_dir():
        return None
    for entry in sorted(root.iterdir(), reverse=True):
        if entry.is_dir() and entry.name.endswith(f"-{cid}") and ARCHIVE_DIR_RE.match(
            entry.name
        ):
            return entry
    return None


def find_archive_commit(repo: Path, cid: str) -> str:
    res = git(
        repo, "log", "--fixed-strings", f"--grep=archive({cid}):",
        "--format=%H", "-n", "1",
    )
    return res.stdout.strip() if res.returncode == 0 else ""


def verify_change_done(repo: Path, cfg: dict, cid: str) -> tuple[bool, str]:
    """A change is done when OpenSpec has archived it.

    Authoritative evidence (required): the change has left openspec/changes/
    and now lives under a dated openspec/changes/archive/ directory. That move
    is exactly what `openspec archive` produces atomically, and is the ground
    truth of completion.

    Corroborating signals (warn, never veto): an `archive(<id>):` commit
    reachable from HEAD, and the controller state file agreeing
    (completed/done/passed). These go stale when a change was archived by hand,
    squashed into another commit, or recovered after a drive error, so they are
    surfaced as notes but no longer fail a change that OpenSpec itself archived.
    """
    if (repo / "openspec" / "changes" / cid).exists():
        return False, f"openspec/changes/{cid} still exists"

    if find_archive_dir(repo, cid) is None:
        return False, "no dated archive directory found"

    warnings: list[str] = []
    if not find_archive_commit(repo, cid):
        warnings.append("no archive(<id>): commit (archived manually or squashed?)")

    cs = read_controller_state(repo, cfg, cid)
    if cs is not None:
        if cs.get("_malformed"):
            warnings.append("controller state file is malformed JSON")
        else:
            if cs.get("status") != "completed" or cs.get("phase") != "done":
                warnings.append(
                    f"controller state stale: status={cs.get('status')} "
                    f"phase={cs.get('phase')}"
                )
            arch = cs.get("archive", {})
            if arch.get("status") != "passed" or not arch.get("commit"):
                warnings.append("controller archive state not passed")

    if warnings:
        log(f"  note: {cid} archived but {'; '.join(warnings)}")

    return True, ""


def run_fast_checks(repo: Path, cfg: dict) -> tuple[bool, str]:
    timeout = cfg["check_timeout_minutes"] * 60
    for cmd in cfg["fast_checks"]:
        log(f"  check: {cmd}")
        try:
            res = subprocess.run(
                shlex.split(cmd), cwd=repo, capture_output=True, text=True,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            return False, f"check timed out: {cmd}"
        except FileNotFoundError as exc:
            return False, f"check command not found: {exc}"
        if res.returncode != 0:
            tail = (res.stdout + res.stderr).strip().splitlines()[-5:]
            return False, f"check failed: {cmd} :: " + " | ".join(tail)
    return True, ""


def change_dir(repo: Path, cid: str) -> Path:
    return repo / "openspec" / "changes" / cid


AUTHORED_ARTIFACTS = ("proposal.md", "tasks.md")


def change_authored(repo: Path, cid: str) -> bool:
    """True only when the change dir holds the required artifacts, not just a
    bare `openspec new change` scaffold (`.openspec.yaml`). The scheduler uses
    this — instead of mere directory existence — to decide whether a change
    still needs authoring, so a half-written scaffold no longer silently skips
    the create stage and gets driven as if complete."""
    cdir = change_dir(repo, cid)
    return cdir.is_dir() and all((cdir / a).is_file() for a in AUTHORED_ARTIFACTS)


def scaffold_is_clearable(repo: Path, cid: str) -> bool:
    """True when a change dir is a pure, untracked scaffold the orchestrator may
    remove before re-creating: it exists, holds no authored markdown, and has no
    tracked files. Any `.md` content or any tracked file makes it unsafe to
    delete (it may carry hand-written work), so we refuse and ask the operator."""
    cdir = change_dir(repo, cid)
    if not cdir.is_dir():
        return False
    if any(p.is_file() for p in cdir.rglob("*.md")):
        return False
    tracked = git(repo, "ls-files", "--", str(cdir.relative_to(repo)))
    return not tracked.stdout.strip()


def verify_change_created(
    repo: Path,
    cfg: dict,
    cid: str,
    before_tracked: tuple[str, str, str] | None = None,
) -> tuple[bool, str]:
    """A change counts as created only when independent evidence agrees:
    1. openspec/changes/<id> exists with proposal.md and tasks.md
    2. the configured created_check command (default
       `openspec validate <id> --strict`) exits 0
    3. when a pre-create snapshot is provided, creation touched no tracked files
       (change authoring is additive)
    """
    reasons: list[str] = []
    cdir = change_dir(repo, cid)
    if not cdir.is_dir():
        return False, f"openspec/changes/{cid} does not exist"
    for artifact in ("proposal.md", "tasks.md"):
        if not (cdir / artifact).is_file():
            reasons.append(f"missing {artifact}")

    check = cfg["created_check"].format(change=cid)
    if check.strip():
        try:
            res = subprocess.run(
                shlex.split(check), cwd=repo, capture_output=True, text=True,
                timeout=cfg["check_timeout_minutes"] * 60,
            )
            if res.returncode != 0:
                tail = (res.stdout + res.stderr).strip().splitlines()[-3:]
                reasons.append(f"created_check failed: " + " | ".join(tail))
        except subprocess.TimeoutExpired:
            reasons.append(f"created_check timed out: {check}")
        except FileNotFoundError as exc:
            reasons.append(f"created_check command not found: {exc}")

    if before_tracked is not None and tracked_worktree_snapshot(repo) != before_tracked:
        reasons.append(
            "creation modified tracked files; change authoring must be "
            "additive (review with `git status` before continuing)"
        )
    return (not reasons, "; ".join(reasons))


def tracked_worktree_snapshot(repo: Path) -> tuple[str, str, str]:
    """Return tracked-file state, excluding untracked files.

    Creation runs are allowed to add untracked OpenSpec files, but must not alter
    tracked files. Capturing the full tracked diff before and after the create
    stage avoids falsely failing when unrelated tracked edits already existed.
    """
    pieces: list[str] = []
    for args in (
        ("status", "--porcelain", "--untracked-files=no"),
        ("diff", "--no-ext-diff", "--binary"),
        ("diff", "--cached", "--no-ext-diff", "--binary"),
    ):
        res = git(repo, *args)
        if res.returncode != 0:
            pieces.append(f"git {' '.join(args)} failed: {res.stderr}")
        else:
            pieces.append(res.stdout)
    return pieces[0], pieces[1], pieces[2]


def tracked_tree_clean(repo: Path) -> bool:
    res = git(repo, "status", "--porcelain", "--untracked-files=no")
    if res.returncode != 0:
        return False
    lines = [
        ln for ln in res.stdout.splitlines()
        if ln.strip() and not ln[3:].startswith(".opsx-plan/")
    ]
    return not lines


# ---------------------------------------------------------------------------
# Retry policy: read the controller's own schema-v3 state to decide whether
# re-invoking /opsx-drive can plausibly succeed.
# ---------------------------------------------------------------------------

NO_RETRY_RESULTS = {"max_rounds_reached", "no_progress"}


def retry_makes_sense(controller_state: dict | None) -> tuple[bool, str]:
    if controller_state is None:
        return True, "no controller state written; treating as transient"
    if controller_state.get("_malformed"):
        return False, "controller state file is malformed; needs operator"
    last = controller_state.get("last_result", "")
    if last in NO_RETRY_RESULTS:
        return False, f"controller stopped with {last}; needs operator"
    if last == "archive_failed":
        outlook = (
            controller_state.get("archive", {})
            .get("triage", {})
            .get("retry_outlook", "unknown")
        )
        if outlook == "same_failure":
            return False, "archive triage says retry would fail the same way"
        return True, f"archive failed with retry_outlook={outlook}"
    return True, f"last_result={last or 'unset'}"


# ---------------------------------------------------------------------------
# Drive invocation
# ---------------------------------------------------------------------------

def run_stage(
    repo: Path, cfg: dict, cid: str, stage: str, invoke_tpl: str,
    timeout_minutes: float, attempt: int,
) -> tuple[str, Path]:
    """Run a templated stage command ('create' or 'drive'). Returns
    (outcome, log_path) where outcome is 'exited', 'timeout', or
    'spawn_error'. Output goes to a log file so it can be tailed live;
    the exit code is informational only."""
    log_dir = repo / ".opsx-plan" / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"{cid}.{stage}{attempt}.log"

    cmd = shlex.split(
        invoke_tpl.format(change=cid, plan_doc=cfg["plan_doc"])
    )
    timeout_s = timeout_minutes * 60
    log(f"  exec[{stage}]: {' '.join(cmd)}  "
        f"(timeout {timeout_s/60:g}m, log {log_path})")
    return run_logged_command(repo, cmd, log_path, timeout_s, stage, attempt)


def drive_change(repo: Path, cfg: dict, cid: str, attempt: int) -> tuple[str, Path]:
    return run_stage(
        repo, cfg, cid, "drive", cfg["invoke"],
        cfg["changes"][cid]["timeout_minutes"], attempt,
    )


def terminate_group(proc: subprocess.Popen, grace: float = 15.0) -> None:
    try:
        pgid = os.getpgid(proc.pid)
    except ProcessLookupError:
        return
    try:
        os.killpg(pgid, signal.SIGTERM)
        deadline = time.monotonic() + grace
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                return
            time.sleep(0.5)
        os.killpg(pgid, signal.SIGKILL)
        proc.wait(timeout=10)
    except (ProcessLookupError, PermissionError):
        pass


def handle_sigint(signum, frame):  # noqa: ARG001
    log("interrupted; terminating active stage process group")
    if _current_proc is not None:
        terminate_group(_current_proc)
    sys.exit(130)


# ---------------------------------------------------------------------------
# Scheduling
# ---------------------------------------------------------------------------

def classify(cfg: dict, state: dict, cid: str) -> str:
    """Computed status for reporting: includes blocked/awaiting_approval."""
    c = cfg["changes"][cid]
    r = rec(state, cid)
    if not c["enabled"]:
        return SKIPPED
    if r["status"] in (DONE, FAILED, RUNNING):
        return r["status"]
    for dep in c["depends_on"]:
        dep_status = classify(cfg, state, dep)
        if dep_status in (FAILED, "blocked"):
            return "blocked"
        if dep_status != DONE:
            return PENDING
    if c["pause_before"] and cid not in state["approvals"]:
        return "awaiting_approval"
    if (
        cfg["review_created"]
        and r.get("created_by_orchestrator")
        and not r.get("accepted")
    ):
        return "awaiting_acceptance"
    return "ready"


def reconcile(repo: Path, cfg: dict, state: dict) -> None:
    """Make recorded state agree with repository reality."""
    for cid in cfg["order"]:
        r = rec(state, cid)
        if is_direct_opencode(cfg):
            r["max_rounds"] = cfg["max_rounds"]
        if r["status"] == RUNNING:  # stale from a killed run
            set_status(state, cid, PENDING, "recovered from interrupted run")
        # A change that failed only because no create_invoke was configured
        # (so create never ran: create_attempts == 0) should re-queue once the
        # operator supplies one — otherwise the stale reason keeps reporting
        # "no create_invoke configured" even after the plan is fixed, and the
        # operator has to guess that a manual `reset` is required.
        if (
            r["status"] == FAILED
            and r.get("create_attempts", 0) == 0
            and not change_authored(repo, cid)
            and cfg["changes"][cid]["create_invoke"]
        ):
            set_status(state, cid, PENDING, "create_invoke now configured; will retry")
            log(f"reconcile: {cid} create config now present; re-queued")
            continue
        if r["status"] != DONE:
            if is_direct_opencode(cfg):
                archived_on_disk = (
                    not change_dir(repo, cid).exists() and find_archive_dir(repo, cid) is not None
                )
                if r["archive"].get("status") == "passed":
                    ok, why = verify_direct_archive_done(repo, cid, r)
                    if ok:
                        r["phase"] = "done"
                        set_status(
                            state,
                            cid,
                            DONE,
                            "verified from plan state + repository evidence",
                        )
                        log(f"reconcile: {cid} already archived; marked done")
                        continue
                    if archived_on_disk:
                        set_status(
                            state,
                            cid,
                            FAILED,
                            f"recorded archive success but evidence is inconsistent: {why}",
                        )
                        log(f"reconcile: {cid} archive evidence inconsistent: {why}")
                        continue
                elif archived_on_disk:
                    set_status(
                        state,
                        cid,
                        FAILED,
                        "repository archived change but plan state lacks archive worker evidence",
                    )
                    log(
                        f"reconcile: {cid} archived on disk without plan-owned archive evidence"
                    )
                    continue
            else:
                ok, _ = verify_change_done(repo, cfg, cid)
                if ok:
                    set_status(state, cid, DONE, "verified from repository evidence")
                    log(f"reconcile: {cid} already archived; marked done")
                    continue
            if (
                r["status"] == PENDING
                and r.get("create_attempts", 0) > 0
                and change_authored(repo, cid)
                and not r.get("created_by_orchestrator")
            ):
                created_ok, created_why = verify_change_created(repo, cfg, cid)
                if created_ok:
                    r["created_by_orchestrator"] = True
                    set_status(state, cid, PENDING, "created and verified")
                    log(f"reconcile: {cid} already created; marked for acceptance")
                else:
                    set_status(
                        state, cid, PENDING,
                        f"create verification pending: {created_why}",
                    )
        else:
            if is_direct_opencode(cfg):
                ok, why = verify_direct_archive_done(repo, cid, r)
            else:
                ok, why = verify_change_done(repo, cfg, cid)
            if not ok:
                set_status(
                    state, cid, FAILED,
                    f"recorded done but evidence missing: {why}",
                )
                log(f"reconcile: {cid} done-state no longer verifiable: {why}")


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def cmd_run(args: argparse.Namespace) -> int:
    repo = Path(args.repo).resolve()
    cfg = load_plan(Path(args.plan).resolve())
    state = load_state(repo, cfg["name"])
    signal.signal(signal.SIGINT, handle_sigint)

    reconcile(repo, cfg, state)
    save_state(repo, cfg["name"], state)

    if args.dry_run:
        return cmd_status_inner(cfg, state, header="dry run: planned order")

    budget_deadline = (
        time.monotonic() + args.budget_minutes * 60 if args.budget_minutes else None
    )
    ran = 0
    visited: set[str] = set()  # avoid re-picking the same change this run

    while True:
        if budget_deadline and time.monotonic() > budget_deadline:
            log("wall-clock budget exhausted; stopping")
            break
        if args.max_changes and ran >= args.max_changes:
            log("max-changes reached; stopping")
            break

        ready = [
            c for c in cfg["order"]
            if c not in visited and classify(cfg, state, c) == "ready"
        ]
        if args.only:
            ready = [c for c in ready if c in args.only]
        if not ready:
            break

        cid = ready[0]
        change_cfg = cfg["changes"][cid]
        r = rec(state, cid)
        needs_create = not change_authored(repo, cid)

        if cfg["require_clean_tracked"] and not tracked_tree_clean(repo):
            log("tracked worktree is dirty; refusing to start a new stage")
            log("commit/stash tracked modifications, then re-run")
            return 2

        # ----- create stage: automate the repetitive /opsx-ff invocation -----
        if needs_create:
            if not change_cfg["create_invoke"]:
                set_status(
                    state, cid, FAILED,
                    "change not created and no create_invoke configured",
                )
                save_state(repo, cfg["name"], state)
                continue
            # A previous attempt may have left a bare scaffold (just
            # .openspec.yaml). `openspec new change` refuses a populated dir, so
            # clear a pure untracked scaffold to let the author command start
            # clean; refuse if the dir holds authored or tracked content.
            if change_dir(repo, cid).is_dir():
                if scaffold_is_clearable(repo, cid):
                    shutil.rmtree(change_dir(repo, cid))
                    log(f"  removed incomplete scaffold openspec/changes/{cid}/ "
                        f"before re-create")
                else:
                    set_status(
                        state, cid, FAILED,
                        f"openspec/changes/{cid} exists but is incomplete "
                        f"(missing {', '.join(AUTHORED_ARTIFACTS)}) and holds "
                        f"authored or tracked content; finish or remove it, "
                        f"then reset",
                    )
                    save_state(repo, cfg["name"], state)
                    continue
            c_attempt = r["create_attempts"] + 1
            if c_attempt > change_cfg["create_max_attempts"]:
                set_status(state, cid, FAILED, "create retry budget exhausted")
                save_state(repo, cfg["name"], state)
                continue

            log(f"=== {cid} create "
                f"(attempt {c_attempt}/{change_cfg['create_max_attempts']}) ===")
            r["create_attempts"] = c_attempt
            set_status(state, cid, RUNNING, "creating change")
            save_state(repo, cfg["name"], state)
            before_tracked = tracked_worktree_snapshot(repo)

            outcome, log_path = run_stage(
                repo, cfg, cid, "create", change_cfg["create_invoke"],
                cfg["create_timeout_minutes"], c_attempt,
            )
            r["last_log"] = str(log_path)

            if outcome == "spawn_error":
                set_status(state, cid, FAILED,
                           f"could not spawn create: {change_cfg['create_invoke']}")
                save_state(repo, cfg["name"], state)
                return 2

            ok, why = verify_change_created(repo, cfg, cid, before_tracked)
            if ok:
                r["created_by_orchestrator"] = True
                set_status(state, cid, PENDING, "created and verified")
                log(f"  created: {cid}")
                if cfg["review_created"]:
                    log(f"  awaiting acceptance — review openspec/changes/{cid}/ "
                        f"then run: opsx-plan accept <plan> {cid}")
            else:
                if outcome == "timeout":
                    why = f"create timed out; {why}"
                if c_attempt < change_cfg["create_max_attempts"]:
                    set_status(state, cid, PENDING, f"create will retry: {why}")
                    log(f"  create not verified ({why}); retrying")
                else:
                    set_status(state, cid, FAILED, f"create failed: {why}")
                    log(f"  CREATE FAILED: {why}")
            save_state(repo, cfg["name"], state)
            # re-classify: acceptance gate may now hold this change
            continue

        if args.create_only:
            visited.add(cid)  # exists already; nothing to create, don't drive
            continue

        if is_direct_opencode(cfg):
            log(f"=== {cid} direct OpenCode execution (round {r['round']}) ===")
            result = run_direct_change(repo, cfg, state, cid, budget_deadline)
            if result == DONE:
                log(f"  done: {cid}")
                ran += 1
            elif result == "spawn_error":
                return 2
            visited.add(cid)
            continue

        # ----- drive stage -----
        attempt = r["attempts"] + 1
        if attempt > change_cfg["max_attempts"]:
            set_status(state, cid, FAILED, "retry budget exhausted")
            save_state(repo, cfg["name"], state)
            continue

        log(f"=== {cid} (attempt {attempt}/{change_cfg['max_attempts']}) ===")
        r["attempts"] = attempt
        set_status(state, cid, RUNNING)
        save_state(repo, cfg["name"], state)

        outcome, log_path = drive_change(repo, cfg, cid, attempt)
        rec(state, cid)["last_log"] = str(log_path)

        if outcome == "spawn_error":
            set_status(state, cid, FAILED, f"could not spawn: {cfg['invoke']}")
            save_state(repo, cfg["name"], state)
            return 2

        ok, why = verify_change_done(repo, cfg, cid)
        if ok:
            checks_ok, check_why = run_fast_checks(repo, cfg)
            if checks_ok:
                set_status(state, cid, DONE, "verified + checks passed")
                log(f"  done: {cid}")
                ran += 1
            else:
                # The change is archived but the repo fails checks. Re-driving
                # cannot fix this; an operator must intervene.
                set_status(state, cid, FAILED, f"post-archive {check_why}")
                log(f"  FAILED post-archive checks: {check_why}")
        else:
            cs = read_controller_state(repo, cfg, cid)
            can_retry, retry_why = retry_makes_sense(cs)
            if outcome == "timeout":
                why = f"drive timed out; {why}"
            detail = f"{why} :: {retry_why}"
            if can_retry and attempt < change_cfg["max_attempts"]:
                set_status(state, cid, PENDING, f"will retry: {detail}")
                log(f"  not done yet ({detail}); retrying")
            else:
                set_status(state, cid, FAILED, detail)
                log(f"  FAILED: {detail}")

        save_state(repo, cfg["name"], state)

    print()
    return cmd_status_inner(cfg, state, header="run finished")


def cmd_status(args: argparse.Namespace) -> int:
    repo = Path(args.repo).resolve()
    cfg = load_plan(Path(args.plan).resolve())
    state = load_state(repo, cfg["name"])
    reconcile(repo, cfg, state)
    save_state(repo, cfg["name"], state)
    return cmd_status_inner(cfg, state, header=f"plan: {cfg['name']}")


def display_order(cfg: dict) -> list[str]:
    """Phase-ascending for human reading (P0, P1, ...), with the scheduler's
    topological order as a stable tiebreaker within a phase. Changes without a
    phase sort last. cfg['order'] itself stays topological for dispatch."""
    topo_index = {cid: i for i, cid in enumerate(cfg["order"])}

    def key(cid: str) -> tuple:
        phase = cfg["changes"][cid].get("phase")
        return (phase is None, phase if phase is not None else 0, topo_index[cid])

    return sorted(cfg["order"], key=key)


def cmd_status_inner(cfg: dict, state: dict, header: str) -> int:
    print(header)
    width = max(len(c) for c in cfg["order"])
    failed = 0
    for cid in display_order(cfg):
        status = classify(cfg, state, cid)
        r = rec(state, cid)
        extra = f"  ({r['reason']})" if r.get("reason") and status != DONE else ""
        phase = cfg["changes"][cid].get("phase")
        phase_s = f"P{phase} " if phase is not None else ""
        print(f"  {phase_s}{cid.ljust(width)}  {status}{extra}")
        if status in (FAILED, "blocked"):
            failed += 1
    return 1 if failed else 0


def cmd_approve(args: argparse.Namespace) -> int:
    repo = Path(args.repo).resolve()
    cfg = load_plan(Path(args.plan).resolve())
    state = load_state(repo, cfg["name"])
    for cid in args.change:
        if cid not in cfg["changes"]:
            print(f"unknown change: {cid}", file=sys.stderr)
            return 2
        if cid not in state["approvals"]:
            state["approvals"].append(cid)
            log(f"approved: {cid}")
    save_state(repo, cfg["name"], state)
    return 0


def cmd_accept(args: argparse.Namespace) -> int:
    """Mark orchestrator-created changes as reviewed so drive may proceed."""
    repo = Path(args.repo).resolve()
    cfg = load_plan(Path(args.plan).resolve())
    state = load_state(repo, cfg["name"])
    for cid in args.change:
        if cid not in cfg["changes"]:
            print(f"unknown change: {cid}", file=sys.stderr)
            return 2
        ok, why = verify_change_created(repo, cfg, cid)
        if not ok:
            print(f"refusing to accept {cid}: {why}", file=sys.stderr)
            return 2
        rec(state, cid)["accepted"] = True
        log(f"accepted: {cid}")
    save_state(repo, cfg["name"], state)
    return 0


def cmd_reset(args: argparse.Namespace) -> int:
    repo = Path(args.repo).resolve()
    cfg = load_plan(Path(args.plan).resolve())
    state = load_state(repo, cfg["name"])
    for cid in args.change:
        if cid not in cfg["changes"]:
            print(f"unknown change: {cid}", file=sys.stderr)
            return 2
        state["changes"][cid] = new_change_record()
        state["changes"][cid]["max_rounds"] = cfg["max_rounds"]
        state["changes"][cid]["reason"] = "reset by operator"
        state["changes"][cid]["updated_at"] = utcnow()
        log(f"reset: {cid}")
    save_state(repo, cfg["name"], state)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(prog="opsx-plan", description=__doc__)
    ap.add_argument("--repo", default=".", help="host project root (default: cwd)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_run = sub.add_parser("run", help="run the plan")
    p_run.add_argument("plan", help="path to plan TOML")
    p_run.add_argument("--dry-run", action="store_true")
    p_run.add_argument("--only", nargs="*", default=None,
                       help="restrict to these change ids (deps must be done)")
    p_run.add_argument("--max-changes", type=int, default=0)
    p_run.add_argument("--budget-minutes", type=float, default=0)
    p_run.add_argument("--create-only", action="store_true",
                       help="create+verify ready changes without driving them")
    p_run.set_defaults(fn=cmd_run)

    p_status = sub.add_parser("status", help="reconcile and show plan status")
    p_status.add_argument("plan")
    p_status.set_defaults(fn=cmd_status)

    p_approve = sub.add_parser("approve", help="approve pause_before changes")
    p_approve.add_argument("plan")
    p_approve.add_argument("change", nargs="+")
    p_approve.set_defaults(fn=cmd_approve)

    p_accept = sub.add_parser(
        "accept", help="accept orchestrator-created changes for driving"
    )
    p_accept.add_argument("plan")
    p_accept.add_argument("change", nargs="+")
    p_accept.set_defaults(fn=cmd_accept)

    p_reset = sub.add_parser("reset", help="reset a failed change to pending")
    p_reset.add_argument("plan")
    p_reset.add_argument("change", nargs="+")
    p_reset.set_defaults(fn=cmd_reset)

    args = ap.parse_args()
    try:
        return args.fn(args)
    except PlanError as exc:
        print(f"plan error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
