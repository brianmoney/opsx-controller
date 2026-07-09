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
    try:
        with open(path, "rb") as fh:
            raw = tomllib.load(fh)
    except Exception as exc:
        raise PlanError(f"cannot parse plan {path.name}: {exc}") from exc

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


# ---------------------------------------------------------------------------
# Active plan pointer
# ---------------------------------------------------------------------------

ACTIVE_PLAN_FILENAME = "active-plan"


def active_plan_pointer_path(repo: Path) -> Path:
    """Path to the active-plan pointer file under .opsx-plan/."""
    return repo / ".opsx-plan" / ACTIVE_PLAN_FILENAME


def read_active_plan(repo: Path) -> str | None:
    """Read the active plan pointer, returning the repo-relative TOML path or None.

    The pointer file contains a single line with a repo-relative path.
    Leading/trailing whitespace is stripped.
    """
    p = active_plan_pointer_path(repo)
    if not p.is_file():
        return None
    content = p.read_text(encoding="utf-8").strip()
    if not content:
        return None
    return content.splitlines()[0].strip() or None


def write_active_plan(repo: Path, plan_rel: str) -> None:
    """Write or update the active-plan pointer file.

    The pointer is stored as a single line: the repo-relative path to the
    plan TOML.  The .opsx-plan/ directory (and its .gitignore) is created
    when missing.
    """
    p = active_plan_pointer_path(repo)
    p.parent.mkdir(parents=True, exist_ok=True)
    gi = p.parent / ".gitignore"
    if not gi.exists():
        gi.write_text("*\n", encoding="utf-8")
    tmp = p.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(plan_rel.strip() + "\n")
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, p)


def validate_active_plan(repo: Path, plan_rel: str) -> Path:
    """Validate that the active plan target exists and can be loaded.

    Returns the resolved absolute Path.  Raises PlanError when the target
    file is missing or the TOML is invalid.
    """
    plan_path = (repo / plan_rel).resolve()
    if not plan_path.is_file():
        raise PlanError(
            f"active plan target does not exist: {plan_rel}"
        )
    # Verify it is loadable through the existing parser
    try:
        load_plan(plan_path)
    except PlanError as exc:
        raise PlanError(f"active plan cannot be loaded: {exc}")
    return plan_path


def resolve_plan(repo: Path, explicit: str | None) -> str:
    """Resolve a plan path using the standard precedence:

    1. Explicit CLI argument
    2. ``OPSX_PLAN`` environment variable
    3. Active-plan pointer file under ``.opsx-plan/``

    Raises PlanError when no plan can be resolved or when the stored
    pointer references a missing file (fail-closed).
    """
    if explicit:
        return explicit

    env_plan = os.environ.get("OPSX_PLAN", "").strip()
    if env_plan:
        norm = str(Path(env_plan))
        log(f"using plan from OPSX_PLAN: {norm}")
        return norm

    pointer = read_active_plan(repo)
    if pointer:
        plan_path = repo / pointer
        if not plan_path.is_file():
            raise PlanError(
                f"active plan pointer references missing file: {pointer}\n"
                f"Set a new active plan with: opsx-plan use <plan.toml>"
            )
        log(f"using active plan: {pointer}")
        return pointer

    raise PlanError(
        "no plan specified\n"
        "Activate a plan with: opsx-plan use <plan.toml>\n"
        "Or set the OPSX_PLAN environment variable."
    )


def _resolve_plan_path(repo: Path, plan_src: str) -> Path:
    """Resolve a plan source string to an absolute ``Path``.

    When *plan_src* is relative it is resolved against *repo*, not the
    current working directory.  This ensures repo-relative active plan
    pointers and ``OPSX_PLAN`` values work correctly with ``--repo``.
    """
    p = Path(plan_src)
    if p.is_absolute():
        return p.resolve()
    return (repo / p).resolve()


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


# ---------------------------------------------------------------------------
# Usage / model metadata extraction for direct stage telemetry
# ---------------------------------------------------------------------------

# Recognized token field names mapped to normalized schema keys.
_TOKEN_FIELD_MAP = {
    "input_tokens": "input_tokens",
    "inputTokens": "input_tokens",
    "prompt_tokens": "input_tokens",
    "promptTokens": "input_tokens",
    "output_tokens": "output_tokens",
    "outputTokens": "output_tokens",
    "completion_tokens": "output_tokens",
    "completionTokens": "output_tokens",
    "cached_input_tokens": "cached_input_tokens",
    "cachedInputTokens": "cached_input_tokens",
    "cache_read_input_tokens": "cached_input_tokens",
    "reasoning_tokens": "reasoning_tokens",
    "reasoningTokens": "reasoning_tokens",
    "thinking_tokens": "reasoning_tokens",
    "thinkingTokens": "reasoning_tokens",
    "total_tokens": "total_tokens",
    "totalTokens": "total_tokens",
}

_MODEL_FIELD_MAP = {
    "provider": "provider",
    "model_id": "model_id",
    "modelId": "model_id",
    "model": "model_id",
    "model_alias": "model_alias",
    "modelAlias": "model_alias",
}


def _valid_token_count(value):
    """Only non-negative ``int`` values are accepted. Booleans are rejected."""
    if isinstance(value, bool):
        return False
    return isinstance(value, int) and value >= 0


def _extract_token_fields(obj):
    """Return ``(normalized_token_dict, found_any)`` from *obj*.

    Inspects top-level keys and a nested ``usage`` sub-dict when
    present.  Each recognized field is validated as a non-negative
    integer; the first valid value for each normalized key wins.
    """
    result = {
        "input_tokens": None,
        "output_tokens": None,
        "cached_input_tokens": None,
        "reasoning_tokens": None,
        "total_tokens": None,
    }
    found_any = False

    candidates = [obj]
    usage = obj.get("usage")
    if isinstance(usage, dict):
        candidates.append(usage)

    for source in candidates:
        for key, value in source.items():
            norm = _TOKEN_FIELD_MAP.get(key)
            if norm is None:
                continue
            if result[norm] is not None:
                continue  # first source wins
            if _valid_token_count(value):
                result[norm] = int(value)
                found_any = True

    return result, found_any


def _extract_model_fields(obj):
    """Return normalized ``{provider, model_id, model_alias}`` dict.

    Inspects top-level keys and a nested ``model`` sub-dict when
    present.  Only non-empty string values are accepted.
    """
    result = {
        "provider": None,
        "model_id": None,
        "model_alias": None,
    }

    candidates = [obj]
    model = obj.get("model")
    if isinstance(model, dict):
        candidates.append(model)

    for source in candidates:
        for key, value in source.items():
            norm = _MODEL_FIELD_MAP.get(key)
            if norm is None:
                continue
            if result[norm] is not None:
                continue
            if isinstance(value, str) and value.strip():
                result[norm] = value.strip()

    return result


def _try_parse_json_line(line):
    """Parse *line* as a JSON object dict, or return ``None``."""
    stripped = line.strip()
    if not (stripped.startswith("{") and stripped.endswith("}")):
        return None
    try:
        obj = json.loads(stripped)
    except json.JSONDecodeError:
        return None
    return obj if isinstance(obj, dict) else None


def _scan_log_for_usage(log_path):
    """Scan every line of *log_path* for JSON objects that carry token fields.

    Returns ``(normalized_token_dict, found_any)``.
    """
    result = {
        "input_tokens": None,
        "output_tokens": None,
        "cached_input_tokens": None,
        "reasoning_tokens": None,
        "total_tokens": None,
    }
    found_any = False

    try:
        for line in log_path.read_text(encoding="utf-8").splitlines():
            obj = _try_parse_json_line(line)
            if obj is None:
                continue
            tokens, any_found = _extract_token_fields(obj)
            if not any_found:
                continue
            for key in result:
                if result[key] is None and tokens[key] is not None:
                    result[key] = tokens[key]
                    found_any = True
    except OSError:
        pass

    return result, found_any


def _scan_log_for_model(log_path):
    """Scan every line of *log_path* for JSON objects that carry model fields.

    Returns normalized ``{provider, model_id, model_alias}`` dict.
    """
    result = {
        "provider": None,
        "model_id": None,
        "model_alias": None,
    }

    try:
        for line in log_path.read_text(encoding="utf-8").splitlines():
            obj = _try_parse_json_line(line)
            if obj is None:
                continue
            model = _extract_model_fields(obj)
            for key in result:
                if result[key] is None and model[key] is not None:
                    result[key] = model[key]
    except OSError:
        pass

    return result


def _parse_invocation_model_value(model_value):
    """Parse an invocation-configured model string into normalized fields.

    Recognizes the common ``provider/model_id`` form used by installed
    OpenCode agent configs. When no provider prefix is present, preserves the
    raw value as ``model_id`` and leaves ``provider`` unset.
    """
    result = {
        "provider": None,
        "model_id": None,
        "model_alias": None,
    }
    if not isinstance(model_value, str):
        return result
    value = model_value.strip()
    if not value:
        return result
    if "/" in value:
        provider, model_id = value.split("/", 1)
        provider = provider.strip()
        model_id = model_id.strip()
        if provider and model_id:
            result["provider"] = provider
            result["model_id"] = model_id
            return result
    result["model_id"] = value
    return result


def _extract_invocation_model(worker_command):
    """Return model identity from the configured worker invocation.

    Supports either an explicit ``--model`` argument or an OpenCode
    ``--agent`` reference whose installed agent frontmatter declares a
    ``model:`` value.
    """
    result = {
        "provider": None,
        "model_id": None,
        "model_alias": None,
    }
    if not isinstance(worker_command, str) or not worker_command.strip():
        return result

    try:
        parts = shlex.split(worker_command)
    except ValueError:
        return result

    agent_name = None
    i = 0
    while i < len(parts):
        part = parts[i]
        if part == "--model" and i + 1 < len(parts):
            return _parse_invocation_model_value(parts[i + 1])
        if part.startswith("--model="):
            return _parse_invocation_model_value(part.split("=", 1)[1])
        if part == "--agent" and i + 1 < len(parts):
            agent_name = parts[i + 1].strip() or None
            i += 2
            continue
        if part.startswith("--agent="):
            agent_name = part.split("=", 1)[1].strip() or None
        i += 1

    if not agent_name:
        return result

    agent_path = Path.home() / ".config" / "opencode" / "agents" / f"{agent_name}.md"
    try:
        lines = agent_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return result

    if not lines or lines[0].strip() != "---":
        return result

    for line in lines[1:]:
        stripped = line.strip()
        if stripped == "---":
            break
        if not stripped.startswith("model:"):
            continue
        _, _, raw_value = stripped.partition(":")
        model_value = raw_value.strip()
        if len(model_value) >= 2 and model_value[0] == model_value[-1] and model_value[0] in {'"', "'"}:
            model_value = model_value[1:-1].strip()
        return _parse_invocation_model_value(model_value)

    return result


def extract_usage_and_model(payload, log_path):
    """Extract usage and model metadata for a completed stage invocation.

    **Precedence:**
    1. Usage & model from parsed worker JSON (*payload*) are preferred.
    2. When *payload* carries no token usage, the stage log is scanned for
       recognizable token metadata.
    3. When *payload* carries no model identity fields, the stage log is
       scanned for model identity fields.  Log model scan never
       supplements a partial worker model.

    Returns ``(usage_dict, model_dict)`` where *usage_dict* includes
    every normalised token field (int or None), ``usage_available``, and
    ``usage_source``.
    """
    usage = {
        "input_tokens": None,
        "output_tokens": None,
        "cached_input_tokens": None,
        "reasoning_tokens": None,
        "total_tokens": None,
        "usage_available": False,
        "usage_source": None,
    }
    model = {
        "provider": None,
        "model_id": None,
        "model_alias": None,
    }

    worker_usage_found = False
    worker_model_found = False

    # 1. Worker JSON -------------------------------------------------------
    if isinstance(payload, dict):
        tokens, wu_found = _extract_token_fields(payload)
        if wu_found:
            worker_usage_found = True
            for key in ("input_tokens", "output_tokens", "cached_input_tokens",
                         "reasoning_tokens", "total_tokens"):
                if tokens[key] is not None:
                    usage[key] = tokens[key]
            usage["usage_available"] = True
            usage["usage_source"] = "worker_json"

        wm = _extract_model_fields(payload)
        for key in ("provider", "model_id", "model_alias"):
            if wm[key] is not None:
                model[key] = wm[key]
                worker_model_found = True

    # 2. Log fallback ------------------------------------------------------
    if log_path is not None:
        if not worker_usage_found:
            log_tokens, log_found = _scan_log_for_usage(log_path)
            if log_found:
                for key in ("input_tokens", "output_tokens", "cached_input_tokens",
                             "reasoning_tokens", "total_tokens"):
                    if log_tokens[key] is not None:
                        usage[key] = log_tokens[key]
                usage["usage_available"] = True
                usage["usage_source"] = "log_metadata"

        if not worker_model_found:
            log_model = _scan_log_for_model(log_path)
            for key in ("provider", "model_id", "model_alias"):
                if model[key] is None and log_model[key] is not None:
                    model[key] = log_model[key]

    return usage, model


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
    # Populate usage and model metadata when a payload was parsed
    # (extraction is best-effort; never fail telemetry write).
    try:
        usage, model = extract_usage_and_model(payload, log_path)
        if model["provider"] is None and model["model_id"] is None:
            invocation_model = _extract_invocation_model(cfg[f"{stage}_invoke"])
            for key in ("provider", "model_id", "model_alias"):
                if model[key] is None and invocation_model[key] is not None:
                    model[key] = invocation_model[key]
        record["usage"].update(usage)
        record["model"].update(model)
    except Exception:
        pass

    # Attempt cost estimation (best-effort; never fail telemetry write).
    try:
        cost = estimate_stage_cost(record["usage"], record["model"], repo=repo)
        record["cost"].update(cost)
    except Exception:
        pass

    write_telemetry_record(repo, cfg["name"], record)
    rec(state, cid)["telemetry"] = {"latest_telemetry": record["uid"]}


# ---------------------------------------------------------------------------
# Cost estimation for direct stage telemetry
# ---------------------------------------------------------------------------

# Subscription usage denominator configuration.
# Maps provider -> model_id -> denominator (positive float).
# Populated by the operator for subscription-billed models.
SUBSCRIPTION_DENOMINATORS: dict[str, dict[str, float]] = {}

# Module-level catalog instance (lazy-init).
_cost_catalog: object = None  # PricingCatalog | None


def _get_catalog(repo: Path | None = None):
    """Lazily initialise and return the pricing catalog.

    When *repo* is provided, its resolved path is prepended to
    ``sys.path`` before the deferred import so that installed
    copies of the orchestrator can discover ``lib.pricing``.

    Returns ``(PricingCatalog, UnresolvedPrice)`` or None on failure.
    """
    global _cost_catalog
    if _cost_catalog is None:
        try:
            # Ensure repo root is on sys.path so an installed
            # ~/.local/bin/opsx-plan can resolve ``from lib.pricing``.
            if repo is not None:
                repo_str = str(repo.resolve())
                if repo_str not in sys.path:
                    sys.path.insert(0, repo_str)

            from lib.pricing import PricingCatalog, UnresolvedPrice  # noqa: F811

            _cost_catalog = (PricingCatalog(), UnresolvedPrice)
        except Exception:
            _cost_catalog = False  # Sentinel for failed init
    if _cost_catalog is False:
        return None
    return _cost_catalog  # (PricingCatalog, UnresolvedPrice) tuple


def _build_price_snapshot(resolved_price, catalog_version,
                          denom_value=None, denom_source=None):
    """Build a ``price_snapshot`` dict from a resolved pricing entry.

    For per_token models, includes all rate fields from the catalog entry.
    For subscription models, also includes denominator fields when present.
    Returns ``None`` when *resolved_price* is None.
    """
    if resolved_price is None:
        return None

    snapshot = {
        "provider": resolved_price.provider,
        "model_id": resolved_price.model_id,
        "display_name": resolved_price.display_name,
        "billing_mode": resolved_price.billing_mode,
        "currency": resolved_price.currency,
        "effective_date": resolved_price.effective_date,
        "catalog_version": catalog_version,
    }

    if resolved_price.billing_mode == "per_token":
        snapshot["input_price_per_mtok"] = resolved_price.input_price_per_mtok
        snapshot["output_price_per_mtok"] = resolved_price.output_price_per_mtok
        snapshot["cached_input_price_per_mtok"] = resolved_price.cached_input_price_per_mtok
        snapshot["reasoning_price_per_mtok"] = resolved_price.reasoning_price_per_mtok
    elif resolved_price.billing_mode == "subscription":
        snapshot["subscription_period"] = resolved_price.subscription_period
        snapshot["subscription_price"] = resolved_price.subscription_price
        if denom_value is not None:
            snapshot["usage_denominator_units"] = denom_value
            snapshot["usage_denominator_source"] = denom_source or "config"

    return snapshot


def _compute_per_token_cost(usage, resolved_price):
    """Compute per-token cost or return ``(None, unresolved_reason)``.

    When *estimated_cost* is not None the estimate succeeded.
    When *unresolved_reason* is not None estimation was not possible.
    """
    total = 0.0

    token_categories = [
        ("input_tokens", "input_price_per_mtok"),
        ("output_tokens", "output_price_per_mtok"),
        ("cached_input_tokens", "cached_input_price_per_mtok"),
        ("reasoning_tokens", "reasoning_price_per_mtok"),
    ]

    for token_field, rate_field in token_categories:
        token_count = usage.get(token_field)
        rate = getattr(resolved_price, rate_field, None)

        # null token counts are unavailable — skip
        if token_count is None:
            continue

        # positive usage with no matching rate is unresolved
        if token_count > 0 and rate is None:
            return None, f"missing rate for observed token category: {token_field}"

        if token_count > 0 and rate is not None:
            total += (token_count / 1_000_000.0) * rate

    return total, None


def _compute_subscription_cost(usage, resolved_price, denominator):
    """Compute subscription cost or return ``(None, unresolved_reason)``."""
    if denominator is None:
        return None, "missing subscription denominator"
    if not isinstance(denominator, (int, float)):
        return None, "invalid subscription denominator"
    # NaN is technically a float, but it is not a usable number.
    # NaN != NaN evaluates to True, which is the standard Python idiom for NaN
    # detection without importing math.
    if denominator != denominator:
        return None, "invalid subscription denominator"
    if denominator <= 0:
        return None, "invalid subscription denominator"

    # Derive stage usage units
    stage_units = usage.get("total_tokens")
    if stage_units is None:
        # Fall back to the sum of non-null token categories
        stage_units = 0
        found_any = False
        for field in ("input_tokens", "output_tokens",
                       "cached_input_tokens", "reasoning_tokens"):
            val = usage.get(field)
            if isinstance(val, (int, float)):
                stage_units += val
                found_any = True
        if not found_any:
            return None, "usage unavailable"

    return resolved_price.subscription_price * (stage_units / denominator), None


def estimate_stage_cost(usage, model,
                        subscription_denominators=None,
                        repo: Path | None = None):
    """Estimate stage cost from telemetry *usage*, *model*, and pricing catalog.

    Args:
        usage: Normalised usage dict from ``extract_usage_and_model``.
        model: Normalised model dict from ``extract_usage_and_model``.
        subscription_denominators: Optional ``provider -> model_id -> float``
            mapping.  When ``None``, uses the module-level
            ``SUBSCRIPTION_DENOMINATORS``.
        repo: Optional repo-root path.  Passed through to the catalog
            loader so installed orchestrator copies can discover
            ``lib.pricing``.

    Returns a dict matching the telemetry ``cost`` schema with keys
    ``status``, ``pricing_catalog_version``, ``price_snapshot``,
    ``unresolved_reason``, and ``estimated_cost``.
    """
    result = {
        "status": "unavailable",
        "pricing_catalog_version": None,
        "price_snapshot": None,
        "unresolved_reason": None,
        "estimated_cost": None,
    }

    # Check usage availability -----------------------------------------------
    if not usage.get("usage_available"):
        result["status"] = "unresolved"
        result["unresolved_reason"] = "usage unavailable"
        return result

    # Check model identity ---------------------------------------------------
    provider = (model.get("provider") or "").strip()
    model_id = (model.get("model_id") or "").strip()
    if not provider or not model_id:
        result["status"] = "unresolved"
        result["unresolved_reason"] = "model identity unavailable"
        return result

    # Resolve pricing --------------------------------------------------------
    catalog_info = _get_catalog(repo)
    if catalog_info is None:
        result["status"] = "unresolved"
        result["unresolved_reason"] = "pricing catalog failed to load"
        return result

    catalog, UnresolvedPriceCls = catalog_info
    catalog_version = catalog.get_catalog_version()
    result["pricing_catalog_version"] = catalog_version

    price_result = catalog.resolve(provider, model_id)

    if isinstance(price_result, UnresolvedPriceCls):
        result["status"] = "unresolved"
        result["unresolved_reason"] = price_result.reason
        return result

    # Compute estimate based on billing mode ---------------------------------
    if price_result.billing_mode == "per_token":
        estimated_cost, unresolved = _compute_per_token_cost(usage, price_result)
        if unresolved is not None:
            result["status"] = "unresolved"
            result["unresolved_reason"] = unresolved
        else:
            result["status"] = "estimated"
            result["estimated_cost"] = estimated_cost
            result["price_snapshot"] = _build_price_snapshot(price_result, catalog_version)

    elif price_result.billing_mode == "subscription":
        denoms = (subscription_denominators
                  if subscription_denominators is not None
                  else SUBSCRIPTION_DENOMINATORS)
        denom_value = None
        denom_source = None
        provider_denoms = denoms.get(provider, {})
        if model_id in provider_denoms:
            denom_value = provider_denoms[model_id]
            denom_source = "config"

        estimated_cost, unresolved = _compute_subscription_cost(
            usage, price_result, denom_value,
        )
        if unresolved is not None:
            result["status"] = "unresolved"
            result["unresolved_reason"] = unresolved
        else:
            result["status"] = "estimated"
            result["estimated_cost"] = estimated_cost
            result["price_snapshot"] = _build_price_snapshot(
                price_result, catalog_version, denom_value, denom_source,
            )

    return result


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
    latest_commit = find_archive_commit(repo, cid)
    if latest_commit and latest_commit != resolved_commit:
        log(
            f"  note: {cid} archive state recorded {resolved_commit[:12]} but newer "
            f"archive(<change>) commit {latest_commit[:12]} is reachable"
        )
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
    clean_ok, clean_why = verify_post_archive_clean(repo, cfg)
    if not clean_ok:
        archive["status"] = "failed"
        archive["reason"] = f"post-archive {clean_why}"
        r["last_result"] = "post_archive_dirty_tracked"
        set_status(state, cid, FAILED, f"post-archive {clean_why}")
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


def verify_post_archive_clean(repo: Path, cfg: dict) -> tuple[bool, str]:
    """Refuse completion when archive/check steps leave tracked edits behind."""
    if not cfg.get("require_clean_tracked", True):
        return True, ""
    if tracked_tree_clean(repo):
        return True, ""
    return (
        False,
        "tracked worktree is dirty; archive must commit or restore tracked changes "
        "before the next change starts",
    )


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

def cmd_use(args: argparse.Namespace) -> int:
    """opsx-plan use <plan.toml> — activate a plan for subsequent commands."""
    repo = Path(args.repo).resolve()
    plan_arg = args.plan
    plan_path = (repo / plan_arg).resolve()
    if not plan_path.is_file():
        print(f"error: plan not found: {plan_arg}", file=sys.stderr)
        return 2
    # Validate through the existing plan loader before writing the pointer
    try:
        load_plan(plan_path)
    except (PlanError, Exception) as exc:
        # tomllib.TOMLDecodeError and PlanError both indicate invalid plan
        print(f"error: invalid plan: {exc}", file=sys.stderr)
        return 2
    try:
        rel = str(plan_path.relative_to(repo))
    except ValueError:
        print(f"error: plan must be inside the repository: {plan_path}", file=sys.stderr)
        return 2
    write_active_plan(repo, rel)
    log(f"active plan set to: {rel}")
    print(f"Activated: {rel}")
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    repo = Path(args.repo).resolve()
    plan_src = resolve_plan(repo, args.plan)
    plan_abs = _resolve_plan_path(repo, plan_src)
    cfg = load_plan(plan_abs)
    # Auto-activate when an explicit path was supplied (only after load_plan
    # succeeds to avoid rewriting the pointer on failed explicit runs).
    if args.plan:
        try:
            rel = str(plan_abs.relative_to(repo))
            write_active_plan(repo, rel)
            log(f"active plan set to: {rel}")
        except ValueError:
            pass  # plan outside repo — skip auto-activation
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
    plan_src = resolve_plan(repo, args.plan)
    cfg = load_plan(_resolve_plan_path(repo, plan_src))
    state = load_state(repo, cfg["name"])
    reconcile(repo, cfg, state)
    save_state(repo, cfg["name"], state)
    sync_direct_worker_state(repo, cfg, state)
    header = f"plan: {cfg['name']}"
    active = read_active_plan(repo)
    if active:
        header += f"  (active: {active})"
    # Determine the effective plan source for the [inspected:] note.
    inspected = None
    if args.plan:
        inspected = args.plan
    else:
        env_plan = os.environ.get("OPSX_PLAN", "").strip()
        if env_plan:
            inspected = str(Path(env_plan))
    if inspected and active and inspected != active:
        header += f"  [inspected: {inspected}]"
    return cmd_status_inner(cfg, state, header=header)


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
    # Heuristic: when the first positional doesn't look like a TOML path,
    # reinterpret it as a change ID and resolve the plan.
    if args.plan is not None and not (
        args.plan.endswith(".toml") or "/" in args.plan or "\\" in args.plan
    ):
        args.change.insert(0, args.plan)
        args.plan = None
    if not args.change:
        print("error: at least one change id is required", file=sys.stderr)
        return 2
    plan_path = resolve_plan(repo, args.plan)
    cfg = load_plan(_resolve_plan_path(repo, plan_path))
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
    # Heuristic: when the first positional doesn't look like a TOML path,
    # reinterpret it as a change ID and resolve the plan.
    if args.plan is not None and not (
        args.plan.endswith(".toml") or "/" in args.plan or "\\" in args.plan
    ):
        args.change.insert(0, args.plan)
        args.plan = None
    if not args.change:
        print("error: at least one change id is required", file=sys.stderr)
        return 2
    plan_path = resolve_plan(repo, args.plan)
    cfg = load_plan(_resolve_plan_path(repo, plan_path))
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
    # Heuristic: when the first positional doesn't look like a TOML path,
    # reinterpret it as a change ID and resolve the plan.
    if args.plan is not None and not (
        args.plan.endswith(".toml") or "/" in args.plan or "\\" in args.plan
    ):
        args.change.insert(0, args.plan)
        args.plan = None
    if not args.change:
        print("error: at least one change id is required", file=sys.stderr)
        return 2
    plan_path = resolve_plan(repo, args.plan)
    cfg = load_plan(_resolve_plan_path(repo, plan_path))
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

    # 4.1 Auto-activate the output plan after successful compile
    try:
        rel = str(output_path.resolve().relative_to(repo))
        write_active_plan(repo, rel)
        log(f"  active plan set to: {rel}")
    except ValueError:
        log(f"  warning: compiled plan {output_path} is outside the repo; cannot auto-activate")

    return 0


# ---------------------------------------------------------------------------
# Report command
# ---------------------------------------------------------------------------


# -- formatting helpers -------------------------------------------------------

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


# -- table rendering ----------------------------------------------------------

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


# -- leaderboard filter helpers -----------------------------------------------

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


# -- JSON output --------------------------------------------------------------

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


# -- cmd_report ---------------------------------------------------------------

def cmd_report(args: argparse.Namespace) -> int:
    """opsx-plan report <plan> [--json] [--change <id>] [--run-id <id>]
       [--stage <stage>] [--model <substr>]"""
    repo = Path(args.repo).resolve()
    plan_src = resolve_plan(repo, args.plan)
    repo_str = str(repo)
    if repo_str not in sys.path:
        sys.path.insert(0, repo_str)

    from lib.metrics.aggregator import (
        AggregationError,
        _build_leaderboard,
        _change_aggregation,
        _read_state,
        _read_telemetry,
        _select_run,
        aggregate,
    )

    cfg = load_plan(_resolve_plan_path(repo, plan_src))
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
        _print_stage_aggregates(result.stage_aggregates, args.stage)
        _print_model_leaderboard(result.model_leaderboard)

        # Warnings section
        if all_warnings:
            print(f"\n=== Warnings ({len(all_warnings)}) ===")
            for w in all_warnings:
                print(f"  - {w}")

    return 0


# ---------------------------------------------------------------------------
# Dashboard command
# ---------------------------------------------------------------------------

# -- HTML formatting helpers --------------------------------------------------


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


# -- CSS block ----------------------------------------------------------------


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


# -- Dashboard HTML renderer ------------------------------------------------


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


# -- cmd_dashboard -----------------------------------------------------------


def cmd_dashboard(args: argparse.Namespace) -> int:
    """opsx-plan dashboard <plan> [--output <path>] [--run-id <id>]
       [--change <id>]"""
    repo = Path(args.repo).resolve()
    plan_src = resolve_plan(repo, args.plan)
    repo_str = str(repo)
    if repo_str not in sys.path:
        sys.path.insert(0, repo_str)

    from lib.metrics.aggregator import (
        AggregationError,
        _build_leaderboard,
        _change_aggregation,
        _read_state,
        _read_telemetry,
        _select_run,
        aggregate,
    )

    cfg = load_plan(_resolve_plan_path(repo, plan_src))
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

    p_use = sub.add_parser("use", help="activate a plan for subsequent commands")
    p_use.add_argument("plan", help="path to plan TOML")
    p_use.set_defaults(fn=cmd_use)

    p_run = sub.add_parser("run", help="run the plan")
    p_run.add_argument("plan", nargs="?", default=None, help="path to plan TOML")
    p_run.add_argument("--dry-run", action="store_true")
    p_run.add_argument("--only", nargs="*", default=None,
                       help="restrict to these change ids (deps must be done)")
    p_run.add_argument("--max-changes", type=int, default=0)
    p_run.add_argument("--budget-minutes", type=float, default=0)
    p_run.add_argument("--create-only", action="store_true",
                       help="create+verify ready changes without driving them")
    p_run.set_defaults(fn=cmd_run)

    p_status = sub.add_parser("status", help="reconcile and show plan status")
    p_status.add_argument("plan", nargs="?", default=None, help="path to plan TOML")
    p_status.set_defaults(fn=cmd_status)

    p_approve = sub.add_parser("approve", help="approve pause_before changes")
    p_approve.add_argument("plan", nargs="?", default=None, help="path to plan TOML")
    p_approve.add_argument("change", nargs="*")
    p_approve.set_defaults(fn=cmd_approve)

    p_accept = sub.add_parser(
        "accept", help="accept orchestrator-created changes for driving"
    )
    p_accept.add_argument("plan", nargs="?", default=None, help="path to plan TOML")
    p_accept.add_argument("change", nargs="*")
    p_accept.set_defaults(fn=cmd_accept)

    p_reset = sub.add_parser("reset", help="reset a failed change to pending")
    p_reset.add_argument("plan", nargs="?", default=None, help="path to plan TOML")
    p_reset.add_argument("change", nargs="*")
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

    p_report = sub.add_parser(
        "report",
        help="report plan-run efficiency metrics from telemetry and state",
        description=(
            "Read telemetry and state for a plan, then emit deterministic "
            "human-readable tables (default) or JSON (--json)."
        ),
    )
    p_report.add_argument("plan", nargs="?", default=None, help="path to plan TOML")
    p_report.add_argument(
        "--json", action="store_true",
        help="emit a single JSON object instead of tables",
    )
    p_report.add_argument(
        "--change", default=None,
        help="filter per-change output and leaderboard to this change id",
    )
    p_report.add_argument(
        "--run-id", default=None,
        help="select a specific run id (default: latest by started_at)",
    )
    p_report.add_argument(
        "--stage", default=None,
        choices=["implement", "review", "archive"],
        help="filter stage aggregates and leaderboard to this stage",
    )
    p_report.add_argument(
        "--model", default=None,
        help="filter leaderboard to entries with model IDs containing this "
             "substring (case-insensitive)",
    )
    p_report.set_defaults(fn=cmd_report)

    p_dashboard = sub.add_parser(
        "dashboard",
        help="generate a static HTML efficiency dashboard from telemetry",
        description=(
            "Read telemetry and state for a plan, then emit a self-contained "
            "static HTML dashboard file."
        ),
    )
    p_dashboard.add_argument("plan", nargs="?", default=None, help="path to plan TOML")
    p_dashboard.add_argument(
        "--output", default=None,
        help="output HTML path (default: .opsx-plan/dashboards/<plan_name>.html)",
    )
    p_dashboard.add_argument(
        "--run-id", default=None,
        help="select a specific run id (default: latest by started_at)",
    )
    p_dashboard.add_argument(
        "--change", default=None,
        help="filter per-change output and timeline to this change id",
    )
    p_dashboard.set_defaults(fn=cmd_dashboard)

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
