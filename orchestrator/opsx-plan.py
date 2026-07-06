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
import uuid
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


def build_single_change_config(repo: Path, change_id: str) -> dict:
    """Build a minimal one-change OpenCode direct-execution config.

    Synthesizes a config dict that mirrors the output of ``load_plan`` for
    exactly one already-authored OpenSpec change, without requiring a TOML
    manifest.  Fails early when the change dir is missing or unauthored.
    """
    cdir = change_dir(repo, change_id)
    if not cdir.is_dir():
        raise PlanError(f"openspec/changes/{change_id} does not exist")
    if not change_authored(repo, change_id):
        raise PlanError(
            f"openspec/changes/{change_id} is missing required artifacts "
            f"({', '.join(AUTHORED_ARTIFACTS)})"
        )

    defaults = ADAPTER_DEFAULTS["opencode"]
    plan_name = f"run-{change_id}"

    cfg = {
        "name": plan_name,
        "adapter": "opencode",
        "invoke": defaults["invoke"],
        "state_file": defaults["state_file"],
        "implement_invoke": defaults["implement_invoke"],
        "review_invoke": defaults["review_invoke"],
        "archive_invoke": defaults["archive_invoke"],
        "timeout_minutes": 90,
        "max_attempts": 2,
        "max_rounds": 5,
        "no_progress_limit": 2,
        "fast_checks": [],
        "check_timeout_minutes": 15,
        "require_clean_tracked": True,
        "plan_doc": "",
        "create_invoke": "",
        "create_timeout_minutes": 30,
        "create_max_attempts": 2,
        "review_created": False,
        "created_check": "openspec validate {change} --strict",
    }

    by_id = {
        change_id: {
            "id": change_id,
            "phase": None,
            "depends_on": [],
            "pause_before": False,
            "enabled": True,
            "timeout_minutes": 90,
            "max_attempts": 2,
            "create_invoke": "",
            "create_max_attempts": 2,
        }
    }

    cfg["order"] = [change_id]
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
        "telemetry": {"latest_telemetry": ""},
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
        "telemetry": r["telemetry"],
    }
    save_json(worker_state_path(repo, cfg["name"], cid), payload)


def persist_direct_state(repo: Path, cfg: dict, state: dict, cid: str) -> None:
    save_state(repo, cfg["name"], state)
    save_worker_state(repo, cfg, state, cid)


def sync_direct_worker_state(repo: Path, cfg: dict, state: dict) -> None:
    if not is_direct_opencode(cfg):
        return
    for cid in cfg["order"]:
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


# ---------------------------------------------------------------------------
# Telemetry recording (plan-run-observability schema v1)
# ---------------------------------------------------------------------------

TELEMETRY_SCHEMA_VERSION = 1


def compute_duration_ms(started_at: str, ended_at: str) -> int:
    start = datetime.fromisoformat(started_at)
    end = datetime.fromisoformat(ended_at)
    return int((end - start).total_seconds() * 1000)


def build_telemetry_record(
    *,
    plan_name: str,
    run_id: str,
    change_id: str,
    stage: str,
    round_num: int,
    status: str,
    started_at: str,
    ended_at: str,
    duration_ms: int,
    adapter: str,
    worker_command: str,
    timeout_seconds: int,
    retry_attempt: int = 0,
    log_path: str = "",
    stage_status: str | None = None,
    error_message: str | None = None,
    verdict: str | None = None,
    critical_count: int | None = None,
    warning_count: int | None = None,
    note_count: int | None = None,
) -> dict:
    return {
        "schema_version": TELEMETRY_SCHEMA_VERSION,
        "uid": str(uuid.uuid4()),
        "plan_name": plan_name,
        "run_id": run_id,
        "change_id": change_id,
        "stage": stage,
        "round": round_num,
        "status": status,
        "started_at": started_at,
        "ended_at": ended_at,
        "duration_ms": duration_ms,
        "invocation": {
            "adapter": adapter,
            "worker_command": worker_command,
            "args_sample": None,
            "timeout_seconds": timeout_seconds,
            "retry_attempt": retry_attempt,
        },
        "model": {
            "provider": None,
            "model_id": None,
            "model_alias": None,
        },
        "result": {
            "log_path": log_path,
            "stage_status": stage_status,
            "error_message": error_message,
            "verdict": verdict,
            "critical_count": critical_count,
            "warning_count": warning_count,
            "note_count": note_count,
        },
        "usage": {
            "usage_available": False,
            "input_tokens": None,
            "output_tokens": None,
            "cached_input_tokens": None,
            "reasoning_tokens": None,
            "total_tokens": None,
            "usage_source": None,
        },
        "cost": {
            "status": "unavailable",
            "pricing_catalog_version": None,
            "price_snapshot": None,
            "unresolved_reason": None,
            "estimated_cost": None,
        },
    }


def write_telemetry_record(repo: Path, plan_name: str, record: dict) -> None:
    telemetry_dir = repo / ".opsx-plan" / "telemetry"
    telemetry_dir.mkdir(parents=True, exist_ok=True)
    jsonl_path = telemetry_dir / f"{plan_name}.jsonl"
    line = json.dumps(record, ensure_ascii=False) + "\n"
    with open(jsonl_path, "a", encoding="utf-8") as fh:
        fh.write(line)
        fh.flush()
        os.fsync(fh.fileno())


def get_or_create_run_id(repo: Path, cfg: dict, state: dict) -> str:
    run_id = state.get("run_id", "")
    if run_id:
        return run_id
    started_at = state.get("started_at", "")
    if started_at:
        # Derive stable run_id from plan started_at timestamp
        run_id = started_at.replace(":", "").replace("-", "").replace("T", "-")
    else:
        # First run: generate UUID, persist started_at and run_id
        now = utcnow()
        state["started_at"] = now
        run_id = now.replace(":", "").replace("-", "").replace("T", "-")
    state["run_id"] = run_id
    save_state(repo, cfg["name"], state)
    return run_id


def _record_stage_telemetry(
    repo: Path,
    cfg: dict,
    state: dict,
    cid: str,
    stage: str,
    round_num: int,
    started_at: str,
    ended_at: str,
    duration_ms: int,
    telemetry_status: str,
    error_message: str | None,
    payload: dict | None,
    log_path: Path,
) -> None:
    run_id = get_or_create_run_id(repo, cfg, state)
    stage_status = payload.get("status") if isinstance(payload, dict) else None
    verdict = None
    critical_count = None
    warning_count = None
    note_count = None
    if isinstance(payload, dict) and stage == "review":
        verdict = payload.get("verdict")
        counts = payload.get("finding_counts")
        if isinstance(counts, dict):
            critical_count = counts.get("critical")
            warning_count = counts.get("warning")
            note_count = counts.get("note")
    rel_log_path = str(log_path.relative_to(repo)) if log_path else ""

    record = build_telemetry_record(
        plan_name=cfg["name"],
        run_id=run_id,
        change_id=cid,
        stage=stage,
        round_num=round_num,
        status=telemetry_status,
        started_at=started_at,
        ended_at=ended_at,
        duration_ms=duration_ms,
        adapter=cfg["adapter"],
        worker_command=cfg[f"{stage}_invoke"],
        timeout_seconds=int(cfg["changes"][cid]["timeout_minutes"] * 60),
        log_path=rel_log_path,
        stage_status=stage_status,
        error_message=error_message,
        verdict=verdict,
        critical_count=critical_count,
        warning_count=warning_count,
        note_count=note_count,
    )
    write_telemetry_record(repo, cfg["name"], record)
    rec(state, cid)["telemetry"] = {"latest_telemetry": record["uid"]}


PERMISSION_REJECTION_MARKERS = [
    "permission requested",
    "auto-rejecting",
    "The user rejected permission",
    "external_directory permission denied",
]


def parse_stage_json(log_path: Path) -> tuple[dict | None, str]:
    lines: list[str] = []
    for raw in log_path.read_text(encoding="utf-8").splitlines():
        stripped = ANSI_ESCAPE_RE.sub("", raw).strip()
        if not stripped or stripped.startswith("# "):
            continue
        lines.append(stripped)
    for candidate in reversed(lines):
        if candidate.startswith("`") and candidate.endswith("`"):
            candidate = candidate.strip("`").strip()
        if not (candidate.startswith("{") and candidate.endswith("}")):
            continue
        try:
            payload = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if not isinstance(payload, dict):
            continue
        return payload, ""
    # No valid JSON object found: inspect for permission-rejection markers
    joined = " ".join(line.lower() for line in lines)
    for marker in PERMISSION_REJECTION_MARKERS:
        if marker.lower() in joined:
            return None, (
                f"permission denied before JSON output "
                f"(marker: {marker!r} found in {len(lines)} lines)"
            )
    return None, f"expected a final JSON object line, got {len(lines)} non-comment lines"


def record_archive_evidence(repo: Path, record: dict, cid: str) -> bool:
    archive_dir = find_archive_dir(repo, cid)
    commit = find_archive_commit(repo, cid)
    if archive_dir is None or not commit:
        return False
    record["archive"].update(
        {
            "status": "passed",
            "path": str(archive_dir.relative_to(repo)),
            "commit": commit,
            "reason": "",
        }
    )
    return True


def append_history(state: dict, cid: str, entry: dict) -> None:
    rec(state, cid)["history"].append(entry)


def reachable_commit(repo: Path, commit: str) -> bool:
    if not commit:
        return False
    res = git(repo, "merge-base", "--is-ancestor", commit, "HEAD")
    return res.returncode == 0


def resolve_commit(repo: Path, commit: str) -> str:
    if not commit:
        return ""
    res = git(repo, "rev-parse", "--verify", commit)
    return res.stdout.strip() if res.returncode == 0 else ""


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
    resolved_commit = resolve_commit(repo, commit)
    if not resolved_commit:
        return False, f"archive commit could not be resolved: {commit}"
    if find_archive_commit(repo, cid) != resolved_commit:
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

        # 3.1 Capture started_at before invocation
        started_at = utcnow()

        def _write_telemetry(telemetry_status: str, error_message: str | None) -> None:
            """Write a telemetry record. Logs a warning on failure; never raises."""
            try:
                _record_stage_telemetry(
                    repo, cfg, state, cid, stage, round_num,
                    started_at, ended_at, duration_ms,
                    telemetry_status, error_message,
                    payload, log_path,
                )
            except Exception as exc:
                log(f"warning: failed to write telemetry for {cid}/{stage} r{round_num}: {exc}")

        outcome, log_path = invoke_direct_stage(repo, cfg, cid, stage, round_num, input_block)
        record_stage_log(state, cid, stage, round_num, outcome, log_path)

        # 3.2 Capture ended_at, compute duration, determine telemetry status
        ended_at = utcnow()
        duration_ms = compute_duration_ms(started_at, ended_at)
        payload: dict | None = None
        parse_why = ""

        if outcome == "spawn_error":
            _write_telemetry(
                "spawn_error",
                f"could not spawn {stage}: {cfg[f'{stage}_invoke']}",
            )
            rec(state, cid)["last_result"] = f"{stage}_spawn_error"
            set_status(state, cid, FAILED, f"could not spawn {stage}: {cfg[f'{stage}_invoke']}")
            persist_direct_state(repo, cfg, state, cid)
            return "spawn_error"

        if outcome == "timeout":
            _write_telemetry("timeout", f"{stage} timed out")
            rec(state, cid)["last_result"] = f"{stage}_timeout"
            set_status(state, cid, FAILED, f"{stage} timed out")
            persist_direct_state(repo, cfg, state, cid)
            return "failed"

        payload, parse_why = parse_stage_json(log_path)
        if payload is None:
            _write_telemetry("invalid_output", parse_why)
            rec(state, cid)["last_result"] = "subagent_output_invalid"
            if stage == "archive":
                rec(state, cid)["archive"]["status"] = "failed"
                rec(state, cid)["archive"]["reason"] = parse_why
            set_status(state, cid, FAILED, f"{stage} output invalid: {parse_why}")
            persist_direct_state(repo, cfg, state, cid)
            return "failed"

        # Parseable payload: apply control-flow dispatch first, then record
        # telemetry with the definitive outcome.
        if stage == "implement":
            action = apply_implement_result(repo, cfg, state, cid, payload)
        elif stage == "review":
            action = apply_review_result(repo, state, cid, payload)
        else:
            action = apply_archive_result(repo, cfg, state, cid, payload)
        persist_direct_state(repo, cfg, state, cid)

        # Determine telemetry status from the control-flow decision.
        if action == "stop":
            telemetry_status = "failed"
            last_result = rec(state, cid).get("last_result", "")
            reason = rec(state, cid).get("reason", "")
            error_message = f"control flow stopped: {last_result}"
            if reason:
                error_message += f" - {reason}"
        else:
            telemetry_status = "completed"
            error_message = None

        _write_telemetry(telemetry_status, error_message)

        if action == "continue":
            continue
        # Persist after telemetry write so telemetry.latest_telemetry is saved
        # for stop/done outcomes (e.g. blocked implement, archived archive).
        persist_direct_state(repo, cfg, state, cid)
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
    cdir = change_dir(repo, cid)
    if cdir.exists():
        return False, f"{cdir.relative_to(repo)} still exists"

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
    direct = repo / "openspec" / "changes" / cid
    if direct.is_dir():
        return direct
    changes_dir = repo / "openspec" / "changes"
    if changes_dir.is_dir():
        for entry in sorted(changes_dir.iterdir(), reverse=True):
            if entry.is_dir() and entry.name.endswith(f"-{cid}"):
                return entry
    return direct


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

    check = cfg["created_check"].format(change=cdir.name)
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
        archived_on_disk = (
            not change_dir(repo, cid).exists() and find_archive_dir(repo, cid) is not None
        )
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
            and not archived_on_disk
            and cfg["changes"][cid]["create_invoke"]
        ):
            set_status(state, cid, PENDING, "create_invoke now configured; will retry")
            log(f"reconcile: {cid} create config now present; re-queued")
            continue
        if r["status"] != DONE:
            if is_direct_opencode(cfg):
                if archived_on_disk and record_archive_evidence(repo, r, cid):
                    ok, why = verify_direct_archive_done(repo, cid, r)
                    if ok:
                        r["phase"] = "done"
                        set_status(
                            state,
                            cid,
                            DONE,
                            "verified from repository archive evidence",
                        )
                        log(f"reconcile: {cid} already archived; marked done")
                        continue
                    r["archive"]["status"] = "failed"
                    r["archive"]["reason"] = why
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
# Compile helpers
# ---------------------------------------------------------------------------

def resolve_compile_source(repo: Path, source: str) -> Path:
    """Resolve a compile source path relative to *repo*.

    Returns the absolute ``Path`` or raises ``PlanError`` when the source
    does not exist or is not a ``.md`` file.
    """
    p = (repo / source).resolve()
    if not p.is_file():
        raise PlanError(f"source not found: {p}")
    if p.suffix.lower() != ".md":
        raise PlanError(f"source must be a markdown file (.md): {p}")
    return p


def resolve_compile_output(repo: Path, output: str, force: bool) -> Path:
    """Resolve compile output path, refusing overwrite unless *force*.

    Returns the absolute ``Path`` for the output file.  The parent
    directory is created if it does not already exist.
    """
    p = (repo / output).resolve()
    if p.exists() and not force:
        raise PlanError(
            f"output exists: {p}  (use --force to overwrite)"
        )
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def check_controller_model() -> str:
    """Return the configured controller model for compile.

    Raises ``PlanError`` when ``OPSX_CONTROLLER_MODEL`` is unset or empty.
    """
    model = os.environ.get("OPSX_CONTROLLER_MODEL", "").strip()
    if not model:
        raise PlanError(
            "OPSX_CONTROLLER_MODEL is not set; "
            "compile requires a controller model to invoke OpenCode"
        )
    return model


def discover_template_pairs(repo: Path) -> list[tuple[Path, Path | None]]:
    """Find repository template plan pairs (md + matching toml).

    Returns a list of ``(md_path, toml_path_or_None)`` tuples for each
    ``.md`` file under ``openspec/plans/`` in ``repo``.
    """
    plans_dir = repo / "openspec" / "plans"
    if not plans_dir.is_dir():
        return []
    pairs: list[tuple[Path, Path | None]] = []
    for md_path in sorted(plans_dir.glob("*.md")):
        toml_path = md_path.with_suffix(".toml")
        pairs.append((md_path, toml_path if toml_path.is_file() else None))
    return pairs


def build_schema_guidance() -> str:
    """Build manifest schema guidance derived from ``load_plan()`` behavior.

    Covers ``[plan]`` fields, ``[[changes]]`` entries, dependency edges,
    gate defaults, adapter defaults, and fields consumed by the parser.
    """
    default_adapter = "opencode"
    defaults = ADAPTER_DEFAULTS[default_adapter]
    return (
        "## Expected TOML manifest shape\n"
        "\n"
        "The manifest is a TOML document with a ``[plan]`` table and\n"
        "one or more ``[[changes]]`` entries.\n"
        "\n"
        "### ``[plan]`` table fields (all optional with defaults shown)\n"
        "\n"
        "| Field | Type | Default | Description |\n"
        "|-------|------|---------|-------------|\n"
        "| name | string | stems from filename | plan display name |\n"
        "| adapter | string | ``\"opencode\"`` | adapter key (``ADAPTER_DEFAULTS``) |\n"
        "| invoke | string | ``opencode run \\\"/opsx-drive {change}\\\"`` | legacy drive command |\n"
        "| state_file | string | ``.opencode/opsx-controller/{change}.json`` | controller state path |\n"
        "| implement_invoke | string | ``opencode run --agent opsx-implementer`` | direct implement command |\n"
        "| review_invoke | string | ``opencode run --agent opsx-reviewer`` | direct review command |\n"
        "| archive_invoke | string | ``opencode run --agent opsx-archiver`` | direct archive command |\n"
        "| timeout_minutes | float | ``90`` | per-change stage timeout |\n"
        "| max_attempts | int | ``2`` | legacy drive retry ceiling |\n"
        "| max_rounds | int | ``5`` | implement-review loop ceiling |\n"
        "| no_progress_limit | int | ``2`` | consecutive no-progress rounds before failing |\n"
        "| fast_checks | list[str] | ``[]`` | post-archive CLI checks |\n"
        "| check_timeout_minutes | float | ``15`` | fast-check timeout |\n"
        "| require_clean_tracked | bool | ``true`` | refuse to run when tracked tree is dirty |\n"
        "| plan_doc | string | ``\"\"`` | path to the source markdown plan for ``create_invoke`` |\n"
        "| create_invoke | string | ``\"\"`` | authoring command for auto-creating changes |\n"
        "| create_timeout_minutes | float | ``30`` | create stage timeout |\n"
        "| create_max_attempts | int | ``2`` | create retry ceiling |\n"
        "| review_created | bool | ``true`` | require operator ``accept`` before driving created changes |\n"
        "| created_check | string | ``\"openspec validate {change} --strict\"`` | post-create validation command |\n"
        "\n"
        "### ``[[changes]]`` entry fields\n"
        "\n"
        "| Field | Type | Default | Description |\n"
        "|-------|------|---------|-------------|\n"
        "| id | string | **required** | unique change identifier (slug) |\n"
        "| phase | int | ``None`` | phase number (e.g. 1, 2, 3) |\n"
        "| depends_on | list[str] | ``[]`` | ids of changes that must complete first |\n"
        "| pause_before | bool | ``false`` | wait for ``opsx-plan approve`` before running |\n"
        "| enabled | bool | ``true`` | set ``false`` to defer a change |\n"
        "| timeout_minutes | float | plan-level timeout | per-change stage timeout override |\n"
        "| max_attempts | int | plan-level max_attempts | legacy drive attempt override |\n"
        "| create_invoke | string | ``\"\"`` | per-change authoring command override |\n"
        "| create_max_attempts | int | plan-level value | per-change create attempt override |\n"
        "\n"
        "### Dependency semantics\n"
        "\n"
        "- ``depends_on`` lists only canonical change ids (slugs). Each id must\n"
        "  appear as another ``[[changes]]`` entry.\n"
        "- A change cannot depend on itself (no self-loops).\n"
        "- The orchestrator validates that every dependency id is present and\n"
        "  that the resulting DAG has no cycles.\n"
        "- ``depends_on = []`` means no dependencies.\n"
        "- Backticked known change ids from the source doc become edges.\n"
        "- ``Phase N`` references expand to that phase's changes.\n"
        "- Text starting with ``None`` or containing independence wording\n"
        "  (\"independent\", \"in parallel\", \"may proceed\") produces no\n"
        "  edges even when other changes are mentioned.\n"
        "\n"
        "### Gate manual defaults\n"
        "\n"
        "- First change of each capability marked ``(proposed`` in the source\n"
        "  gets ``pause_before = true``.\n"
        "- ``deferred`` wording sets ``enabled = false``.\n"
        "- Manual phase-exit gates (``pause_before = true``) are added by the\n"
        "  operator; the compiler records but does not invent them.\n"
        "\n"
        "### Adapter defaults (opencode)\n"
        "\n"
        "```toml\n"
        f"[plan]\n"
        f"adapter = \"{default_adapter}\"\n"
        f"invoke = \"{defaults['invoke']}\"\n"
        f"state_file = \"{defaults['state_file']}\"\n"
        f"implement_invoke = \"{defaults['implement_invoke']}\"\n"
        f"review_invoke = \"{defaults['review_invoke']}\"\n"
        f"archive_invoke = \"{defaults['archive_invoke']}\"\n"
        "```\n"
    )


def build_compile_prompt(source_content: str, source_path: Path,
                         repo: Path) -> str:
    """Build the complete compile prompt for OpenCode.

    Includes: source markdown, manifest schema guidance, template plan
    pairs from the repository, and model instructions.
    """
    try:
        rel_source = str(source_path.resolve().relative_to(repo.resolve()))
    except ValueError:
        rel_source = str(source_path)

    parts: list[str] = []

    parts.append("## Source plan markdown\n")
    parts.append(source_content)

    parts.append(build_schema_guidance())

    template_pairs = discover_template_pairs(repo)
    if template_pairs:
        parts.append("## Repository template plans\n")
        for md, toml in template_pairs:
            rel = md.relative_to(repo)
            parts.append(f"### Template: `{rel}`\n")
            try:
                parts.append(md.read_text(encoding="utf-8"))
            except OSError:
                pass
            if toml is not None:
                rel_toml = toml.relative_to(repo)
                parts.append(f"### Template manifest: `{rel_toml}`\n")
                try:
                    parts.append(toml.read_text(encoding="utf-8"))
                except OSError:
                    pass
    else:
        parts.append(
            "## Repository template plans\n\n"
            "No `openspec/plans/*.md` template plan pairs were found "
            "in this repository.\n"
        )

    parts.append(
        "## Compile instructions\n"
        "\n"
        "Convert the source plan markdown above into a valid opsx-plan TOML "
        "manifest that can be loaded by `opsx-plan status` and "
        "`opsx-plan run`. Follow these rules:\n"
        "\n"
        "1. **Output only TOML.** Do not include any prose, explanation, "
        "markdown headers, or commentary outside the TOML payload. "
        "Output raw TOML or a single fenced ```toml block.\n"
        "2. **Emit a `[plan]` table** with at least `name`, `adapter` "
        "(\"opencode\"), and `plan_doc` set to exactly "
        f"\"{rel_source}\".\n"
        "3. **Emit one `[[changes]]` entry per change** described in the "
        "source plan, in phase order.\n"
        "4. **Preserve dependency semantics:** backticked known change ids "
        "in the source doc become `depends_on` entries. Independence wording "
        "(\"independent\", \"in parallel\", \"may proceed\") means no "
        "dependency edge. Deferred wording means `enabled = false`.\n"
        "5. **Preserve manual gates:** `pause_before = true` for any change "
        "that introduces a proposed capability (marked with `(proposed` "
        "in the source) or has an explicit gate note.\n"
        "6. **Preserve phase numbers** as `phase` fields on each change.\n"
        "7. **Every change id must be unique** and every `depends_on` id "
        "must reference another change in the manifest.\n"
        "8. **The DAG must have no cycles.**\n"
    )

    return "\n".join(parts)


# ---------------------------------------------------------------------------
# OpenCode invocation for compile
# ---------------------------------------------------------------------------

def run_opencode_for_compile(repo: Path, model: str,
                              prompt: str) -> tuple[str, str]:
    """Invoke OpenCode non-interactively for plan compilation.

    Returns ``(stdout, stderr)`` as a tuple.  Raises ``PlanError`` on
    spawn failure.  A non-zero exit is surfaced in the return value — the
    caller decides how to interpret it.
    """
    try:
        proc = subprocess.run(
            ["opencode", "run", "--model", model, prompt],
            cwd=repo,
            capture_output=True,
            text=True,
            timeout=600,  # 10 minute timeout for model invocation
        )
    except FileNotFoundError:
        raise PlanError(
            "could not spawn opencode; is it installed and on PATH?"
        )
    except subprocess.TimeoutExpired:
        raise PlanError("opencode compile invocation timed out after 600s")
    if proc.returncode != 0:
        raise PlanError(
            f"opencode exited with code {proc.returncode}\n"
            f"stderr: {proc.stderr[:500]}"
        )
    return proc.stdout, proc.stderr


def extract_toml(output: str) -> str:
    """Extract a TOML payload from raw model output.

    Accepts a single clean fenced ``toml` block or a bare TOML
    payload.  Raises ``PlanError`` for ambiguous output: multiple
    fenced blocks, extra prose or non-whitespace content surrounding a
    fenced block, or no TOML content at all.
    """
    stripped = output.strip()
    if not stripped:
        raise PlanError("opencode returned empty output; no TOML to compile")

    fenced_matches = list(re.finditer(r"```(?:toml)?\s*\n(.*?)```", stripped, re.DOTALL))
    if len(fenced_matches) > 1:
        raise PlanError(
            "ambiguous model output: multiple fenced TOML blocks found; "
            "expected a single clean TOML payload"
        )
    if len(fenced_matches) == 1:
        match = fenced_matches[0]
        before = stripped[:match.start()].strip()
        after = stripped[match.end():].strip()
        if before or after:
            raise PlanError(
                "ambiguous model output: extra content found around "
                "the fenced TOML payload; expected only the TOML block"
            )
        return match.group(1).strip()

    if "[" in stripped:
        return stripped

    raise PlanError(
        "could not extract TOML from opencode output; "
        "output does not contain a fenced toml block or bare TOML"
    )


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
    sync_direct_worker_state(repo, cfg, state)

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

        create_only_ok = {"ready", "awaiting_approval"} if args.create_only else {"ready"}
        ready = [
            c for c in cfg["order"]
            if c not in visited and classify(cfg, state, c) in create_only_ok
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
    sync_direct_worker_state(repo, cfg, state)
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


def resolve_changes(cfg: dict, args: list[str]) -> list[str] | None:
    """Resolve each arg: P<N> maps to all changes in that phase; else exact slug."""
    resolved: list[str] = []
    for arg in args:
        m = re.fullmatch(r"P(\d+)", arg)
        if m:
            phase = int(m.group(1))
            matched = [c for c in cfg["order"] if cfg["changes"][c].get("phase") == phase]
            if not matched:
                print(f"no changes found for phase P{phase}", file=sys.stderr)
                return None
            resolved.extend(matched)
        elif arg in cfg["changes"]:
            resolved.append(arg)
        else:
            print(f"unknown change: {arg}", file=sys.stderr)
            return None
    return resolved


def cmd_approve(args: argparse.Namespace) -> int:
    repo = Path(args.repo).resolve()
    cfg = load_plan(Path(args.plan).resolve())
    state = load_state(repo, cfg["name"])
    changes = resolve_changes(cfg, args.change)
    if changes is None:
        return 2
    for cid in changes:
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
    changes = resolve_changes(cfg, args.change)
    if changes is None:
        return 2
    for cid in changes:
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
    changes = resolve_changes(cfg, args.change)
    if changes is None:
        return 2
    for cid in changes:
        state["changes"][cid] = new_change_record()
        state["changes"][cid]["max_rounds"] = cfg["max_rounds"]
        state["changes"][cid]["reason"] = "reset by operator"
        state["changes"][cid]["updated_at"] = utcnow()
        log(f"reset: {cid}")
    save_state(repo, cfg["name"], state)
    return 0


def cmd_run_one(args: argparse.Namespace) -> int:
    """Run exactly one authored OpenSpec change through the direct OpenCode loop."""
    repo = Path(args.repo).resolve()
    change_id = args.change

    cdir = change_dir(repo, change_id)
    if not cdir.is_dir():
        print(f"error: openspec/changes/{change_id} does not exist", file=sys.stderr)
        return 2
    if not change_authored(repo, change_id):
        print(
            f"error: openspec/changes/{change_id} is missing required artifacts "
            f"({', '.join(AUTHORED_ARTIFACTS)})",
            file=sys.stderr,
        )
        return 2

    cfg = build_single_change_config(repo, change_id)
    state = load_state(repo, cfg["name"])
    signal.signal(signal.SIGINT, handle_sigint)

    if cfg["require_clean_tracked"] and not tracked_tree_clean(repo):
        print(
            "error: tracked worktree is dirty; commit/stash then re-run",
            file=sys.stderr,
        )
        return 2

    reconcile(repo, cfg, state)
    save_state(repo, cfg["name"], state)
    sync_direct_worker_state(repo, cfg, state)

    r = rec(state, change_id)
    if r["status"] == DONE:
        log(f"{change_id} is already done")
        return 0

    log(f"=== {change_id} direct OpenCode execution (round {r['round']}) ===")
    result = run_direct_change(repo, cfg, state, change_id)

    if result == DONE:
        log(f"  done: {change_id}")
    elif result == "spawn_error":
        failed_stage = r.get("phase")
        failed_invoke = (
            cfg.get(f"{failed_stage}_invoke", "")
            if failed_stage in {"implement", "review", "archive"}
            else ""
        )
        print(
            f"error: could not start direct worker dispatch for openspec/changes/{change_id}: "
            f"{failed_invoke or r.get('reason', 'unknown direct worker')}",
            file=sys.stderr,
        )
        return 2

    display = r["status"]
    if r.get("reason"):
        display += f" ({r['reason']})"
    print(f"  {change_id}  {display}")
    return 0 if result == DONE else 1


def cmd_compile(args: argparse.Namespace) -> int:
    """opsx-plan compile <source.md> -o <output.toml> [--force]"""
    repo = Path(args.repo).resolve()

    source_path = resolve_compile_source(repo, args.source)
    output_path = resolve_compile_output(repo, args.output, args.force)
    model = check_controller_model()

    log(f"compile: {source_path} -> {output_path}  (model: {model})")

    source_content = source_path.read_text(encoding="utf-8")
    prompt = build_compile_prompt(source_content, source_path, repo)
    log(f"  prompt size: {len(prompt)} chars")

    log("  invoking opencode ...")
    stdout, stderr = run_opencode_for_compile(repo, model, prompt)
    if stderr.strip():
        log(f"  opencode stderr: {stderr.strip()[:500]}")

    toml_text = extract_toml(stdout)
    if not toml_text:
        raise PlanError("extracted TOML payload is empty")

    # Validate through existing load_plan() path
    try:
        parsed = tomllib.loads(toml_text)
    except Exception as exc:
        raise PlanError(f"generated TOML is not valid TOML: {exc}")

    tmp_path = output_path.with_suffix(output_path.suffix + ".compile-tmp")
    try:
        tmp_path.write_text(toml_text, encoding="utf-8")
        cfg = load_plan(tmp_path)
    except PlanError:
        tmp_path.unlink(missing_ok=True)
        raise
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise

    os.replace(tmp_path, output_path)
    log(f"  validated: {len(cfg['order'])} changes, {cfg['changes'].get(cfg['order'][0], {}).get('phase', 'no-phase') or 'no phase'}")

    change_count = len(cfg["order"])
    phases = sorted({cfg["changes"][cid].get("phase") for cid in cfg["order"] if cfg["changes"][cid].get("phase") is not None})
    gated = [cid for cid in cfg["order"] if cfg["changes"][cid].get("pause_before")]
    disabled = [cid for cid in cfg["order"] if not cfg["changes"][cid].get("enabled", True)]

    print(f"Compiled: {output_path}")
    print(f"  Changes: {change_count}")
    if phases:
        print(f"  Phases:  {', '.join(str(p) for p in phases)}")
    if gated:
        print(f"  Gates:   {len(gated)} change(s) with pause_before")
    if disabled:
        print(f"  Deferred: {len(disabled)} change(s) disabled")
    print(f"  Review the DAG with: opsx-plan status {output_path}")
    return 0


def main() -> int:
    # Executable-name dispatch: opsx-run <change-id> [--repo <path>]
    exe_name = os.path.basename(sys.argv[0])
    if exe_name in ("opsx-run", "opsx-run.py"):
        if len(sys.argv) < 2 or sys.argv[1] in ("-h", "--help"):
            print(
                "usage: opsx-run <change-id> [--repo <path>]",
                file=sys.stderr,
            )
            return 2 if len(sys.argv) < 2 else 0

        repo_arg = "."
        change_id = None
        i = 1
        while i < len(sys.argv):
            if sys.argv[i] == "--repo" and i + 1 < len(sys.argv):
                repo_arg = sys.argv[i + 1]
                i += 2
            elif not sys.argv[i].startswith("-") and change_id is None:
                change_id = sys.argv[i]
                i += 1
            else:
                print(
                    f"error: unexpected argument: {sys.argv[i]}",
                    file=sys.stderr,
                )
                return 2

        if change_id is None:
            print("usage: opsx-run <change-id> [--repo <path>]", file=sys.stderr)
            return 2

        args = argparse.Namespace(repo=repo_arg, change=change_id)
        return cmd_run_one(args)

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

    p_compile = sub.add_parser(
        "compile", help="compile a markdown plan to TOML"
    )
    p_compile.add_argument(
        "source", help="path to source markdown plan (.md)"
    )
    p_compile.add_argument(
        "-o", "--output", required=True, help="output TOML path"
    )
    p_compile.add_argument(
        "--force", action="store_true", help="overwrite existing output"
    )
    p_compile.set_defaults(fn=cmd_compile)

    p_run_one = sub.add_parser(
        "run-one", help="run a single authored OpenSpec change directly"
    )
    p_run_one.add_argument("change", help="change id")
    p_run_one.set_defaults(fn=cmd_run_one)

    args = ap.parse_args()
    try:
        return args.fn(args)
    except PlanError as exc:
        print(f"plan error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
